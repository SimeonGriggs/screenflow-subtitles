#!/usr/bin/env python3
"""
screenflow-subtitles.py — Transcribe audio and inject subtitle TextClips
into a ScreenFlow document.

Usage:
    python3 screenflow-subtitles.py <audio_file> <screenflow_document> [options]

Requirements:
    - whisper-cpp (brew install whisper-cpp)
    - ffmpeg (brew install ffmpeg)
    - Python 3.9+ (uses only stdlib: plistlib, struct, argparse, subprocess, etc.)

No pip packages required.

Output files are written to an `output/` directory next to the ScreenFlow
document by default. Use --output-dir to override.
"""

import argparse
import os
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid


# ---------------------------------------------------------------------------
# VTT Parsing
# ---------------------------------------------------------------------------

def parse_vtt_timestamp(ts: str) -> float:
    """Parse a VTT timestamp like '00:01:23.456' into seconds."""
    parts = ts.strip().split(":")
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h = "0"
        m, s = parts
    else:
        raise ValueError(f"Bad timestamp: {ts}")
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse_vtt(path: str) -> list[dict]:
    """Parse a WebVTT file into a list of {start, end, text} dicts."""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    segments = []
    pattern = re.compile(
        r"(\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}\.\d{3})\s*\n(.+?)(?=\n\n|\n\d{2}:|\Z)",
        re.DOTALL,
    )
    for m in pattern.finditer(content):
        start = parse_vtt_timestamp(m.group(1))
        end = parse_vtt_timestamp(m.group(2))
        text = m.group(3).strip()
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\[.*?\]", "", text)
        text = re.sub(r"^-\s*", "", text)
        text = text.strip()
        if text:
            segments.append({"start": start, "end": end, "text": text})

    return segments


def rechunk_segments(segments: list[dict], max_chars: int = 39) -> list[dict]:
    """Re-chunk subtitle segments so each has at most max_chars characters.

    Splits on word boundaries only (never splits a word). Prefers breaking
    after sentence-ending punctuation (.!?) and clause-ending punctuation (,;:)
    when possible to produce natural-reading subtitle lines.

    Uses balanced splitting: determines the number of chunks first, then
    distributes words to keep chunk lengths close to the target while
    respecting the max and avoiding short orphan fragments.

    Timing is interpolated proportionally by character count within each
    original segment.
    """
    chunked = []
    for seg in segments:
        text = seg["text"]
        if len(text) <= max_chars:
            chunked.append(seg)
            continue

        words = text.split()
        total_chars = len(text)

        # Determine how many chunks we need
        n_chunks = -(-total_chars // max_chars)  # ceil division
        target_chars = total_chars / n_chunks

        # Score each possible word boundary as a break point.
        # Build cumulative character lengths to know the line length for any
        # word range [i..j).
        cum_len = []  # cum_len[i] = len(" ".join(words[:i+1]))
        c = 0
        for i, w in enumerate(words):
            if i > 0:
                c += 1  # space
            c += len(w)
            cum_len.append(c)

        def line_len(start_idx, end_idx):
            """Character length of ' '.join(words[start_idx:end_idx])."""
            if end_idx <= start_idx:
                return 0
            length = cum_len[end_idx - 1]
            if start_idx > 0:
                length -= cum_len[start_idx - 1] + 1  # subtract prior + space
            return length

        # Use dynamic programming to find optimal split points.
        # For each (word_index, chunks_remaining) find the best set of breaks.
        # Cost = sum of squared deviation from target_chars for each chunk,
        #        with a bonus for breaking after punctuation.
        import functools

        INF = float("inf")

        @functools.cache
        def best_cost(wi, remaining):
            """Min cost to split words[wi:] into exactly `remaining` chunks."""
            if remaining == 1:
                ll = line_len(wi, len(words))
                if ll > max_chars:
                    return INF, (len(words),)
                return (ll - target_chars) ** 2, (len(words),)

            best = INF
            best_breaks = None
            # Try each possible break point
            for brk in range(wi + 1, len(words) - remaining + 2):
                ll = line_len(wi, brk)
                if ll > max_chars:
                    break
                # Cost for this chunk
                cost = (ll - target_chars) ** 2
                # Bonus for breaking after punctuation (reduce cost)
                if words[brk - 1][-1] in ".!?":
                    cost -= target_chars * 3  # strong preference
                elif words[brk - 1][-1] in ",;:":
                    cost -= target_chars * 1.5  # moderate preference
                # Recurse for the rest
                sub_cost, sub_breaks = best_cost(brk, remaining - 1)
                total = cost + sub_cost
                if total < best:
                    best = total
                    best_breaks = (brk,) + sub_breaks
            if best_breaks is None:
                return INF, ()
            return best, best_breaks

        _, breaks = best_cost(0, n_chunks)

        # If DP failed (shouldn't happen), fall back to even splitting
        if not breaks or len(breaks) != n_chunks:
            # Simple fallback: split evenly by word count
            words_per = len(words) / n_chunks
            breaks = tuple(
                round(words_per * (i + 1)) for i in range(n_chunks - 1)
            ) + (len(words),)

        # Convert word-index breaks to timed segments
        seg_duration = seg["end"] - seg["start"]
        wi = 0
        for brk in breaks:
            chunk_text = " ".join(words[wi:brk])
            char_start = cum_len[wi - 1] + 1 if wi > 0 else 0
            char_end = cum_len[brk - 1] if brk > 0 else 0
            t_start = round(seg["start"] + (char_start / total_chars) * seg_duration, 3)
            t_end = round(seg["start"] + (char_end / total_chars) * seg_duration, 3)
            chunked.append({
                "start": t_start,
                "end": t_end,
                "text": chunk_text,
            })
            wi = brk

    return chunked


# ---------------------------------------------------------------------------
# Whisper transcription
# ---------------------------------------------------------------------------

DEFAULT_MODEL = os.path.expanduser(
    "~/.local/share/whisper-cpp/ggml-large-v3-turbo.bin"
)


def transcribe(
    audio_path: str,
    output_dir: str,
    model_path: str = DEFAULT_MODEL,
    language: str = "en",
    prompt: str = "",
) -> str:
    """Transcribe audio_path using whisper-cpp, return path to generated .vtt file.

    The VTT is written directly into output_dir. Temp files are cleaned up.
    """
    whisper_bin = shutil.which("whisper-cli")
    if not whisper_bin:
        sys.exit(
            "Error: whisper-cli not found. Install with: brew install whisper-cpp"
        )
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        sys.exit("Error: ffmpeg not found. Install with: brew install ffmpeg")
    if not os.path.isfile(model_path):
        sys.exit(
            f"Error: Whisper model not found at {model_path}\n"
            "Download with:\n"
            "  mkdir -p ~/.local/share/whisper-cpp\n"
            "  curl -L -o ~/.local/share/whisper-cpp/ggml-large-v3-turbo.bin \\\n"
            "    https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin"
        )

    tmpdir = tempfile.mkdtemp(prefix="sf-subtitles-")

    try:
        # Convert to 16kHz mono WAV (whisper-cpp requirement)
        wav_path = os.path.join(tmpdir, "audio.wav")
        print(f"Converting audio to WAV...")
        subprocess.run(
            [
                ffmpeg_bin, "-i", audio_path,
                "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
                "-y", wav_path,
            ],
            capture_output=True,
            check=True,
        )

        # Run whisper-cli — output into the tmpdir first
        transcript_base = os.path.join(tmpdir, "transcript")
        print(f"Transcribing with whisper-cpp (model: {os.path.basename(model_path)})...")
        cmd = [
            whisper_bin,
            "-m", model_path,
            "-f", wav_path,
            "-l", language,
            "-ovtt",
            "-of", transcript_base,
            "--no-prints",
        ]
        if prompt:
            cmd.extend(["--prompt", prompt])
            print(f"  Whisper prompt: {prompt}")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"whisper-cli stderr: {result.stderr}", file=sys.stderr)
            sys.exit(f"Error: whisper-cli failed with exit code {result.returncode}")

        tmp_vtt = transcript_base + ".vtt"
        if not os.path.isfile(tmp_vtt):
            sys.exit(f"Error: Expected VTT output not found at {tmp_vtt}")

        # Move VTT into output_dir
        audio_stem = os.path.splitext(os.path.basename(audio_path))[0]
        final_vtt = os.path.join(output_dir, f"{audio_stem}.vtt")
        shutil.move(tmp_vtt, final_vtt)

        return final_vtt

    finally:
        # Always clean up the temp directory
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# ScreenFlow Core Data Binary Store: Reader
# ---------------------------------------------------------------------------

def read_screenflow_store(dat_path: str):
    """
    Read ScreenFlowDocument.dat and return:
    (main_plist, metadata_bytes, header_bytes)
    """
    with open(dat_path, "rb") as f:
        data = f.read()

    magic = data[:8]
    if magic != b"CoreData":
        raise ValueError(f"Not a Core Data binary store (magic: {magic})")

    metadata_offset = struct.unpack(">Q", data[16:24])[0]
    metadata_length = struct.unpack(">Q", data[24:32])[0]
    main_offset = struct.unpack(">Q", data[32:40])[0]
    main_length = struct.unpack(">Q", data[40:48])[0]

    assert main_offset == 64, f"Unexpected main offset: {main_offset}"
    assert main_offset + main_length == metadata_offset
    assert metadata_offset + metadata_length == len(data)

    main_bplist = data[main_offset : main_offset + main_length]
    metadata_bytes = data[metadata_offset : metadata_offset + metadata_length]

    main_plist = plistlib.loads(main_bplist)

    return main_plist, metadata_bytes, data[:64]


def resolve(objects, uid):
    """Resolve a plistlib.UID to its object."""
    if isinstance(uid, plistlib.UID):
        return objects[uid.data if hasattr(uid, "data") else int(uid)]
    return uid


def uid_val(uid):
    """Get the integer value of a UID."""
    if isinstance(uid, plistlib.UID):
        return uid.data if hasattr(uid, "data") else int(uid)
    return uid


def find_entities(main_plist):
    """
    Walk the root map and return:
    - entities: dict[entity_name] -> list of (record_id, node_uid_index, node)
    - max_pk: highest primary key
    - max_track: highest track index
    - time_base: from DocumentProperties
    - canvas_size: (width, height)
    - frame_quantum: time base units per frame (for duration quantization)
    """
    objects = main_plist["$objects"]
    root = objects[uid_val(main_plist["$top"]["mapData"])]
    ns_keys = root["NS.keys"]
    ns_objects = root["NS.objects"]

    entities = {}
    max_pk = 0
    max_track = 0
    time_base = 3000
    canvas_size = (3840, 2160)

    for i in range(len(ns_objects)):
        node = resolve(objects, ns_objects[i])
        if isinstance(node, dict) and "NSEntityName" in node:
            entity_name = resolve(objects, node["NSEntityName"])
            record_id = resolve(objects, ns_keys[i])
            if entity_name not in entities:
                entities[entity_name] = []
            entities[entity_name].append(
                (record_id, uid_val(ns_objects[i]), node)
            )
            if isinstance(record_id, int) and record_id > max_pk:
                max_pk = record_id

    # Get time_base and canvas from DocumentProperties
    for _, _, node in entities.get("DocumentProperties", []):
        attrs = resolve(objects, node.get("NSAttributeValues"))
        if attrs and isinstance(attrs, dict):
            vals = attrs.get("NS.objects", [])
            if len(vals) > 8:
                tb = resolve(objects, vals[8])
                if isinstance(tb, int) and tb > 0:
                    time_base = tb
            if len(vals) > 3:
                size_str = resolve(objects, vals[3])
                if isinstance(size_str, str):
                    m = re.match(r"\{(\d+(?:\.\d+)?),\s*(\d+(?:\.\d+)?)\}", size_str)
                    if m:
                        canvas_size = (float(m.group(1)), float(m.group(2)))

    # Get max track index
    for _, _, node in entities.get("Track", []):
        attrs = resolve(objects, node.get("NSAttributeValues"))
        if attrs and isinstance(attrs, dict):
            vals = attrs.get("NS.objects", [])
            if len(vals) > 2:
                idx = resolve(objects, vals[2])
                if isinstance(idx, int) and idx > max_track:
                    max_track = idx

    # Detect frame rate from MediaSource metadata to determine frame quantum.
    # MediaSource attr[14] is a dict that may contain a "framerate" key like "60.00 fps".
    # The frame quantum = time_base / fps (e.g., 3000/60 = 50 units per frame).
    frame_quantum = 1  # default: no quantization beyond integer
    for _, _, node in entities.get("MediaSource", []):
        attrs = resolve(objects, node.get("NSAttributeValues"))
        if attrs and isinstance(attrs, dict):
            vals = attrs.get("NS.objects", [])
            if len(vals) > 14:
                info_dict = resolve(objects, vals[14])
                if isinstance(info_dict, dict) and "NS.keys" in info_dict:
                    info_keys = [resolve(objects, k) for k in info_dict["NS.keys"]]
                    info_vals = [resolve(objects, v) for v in info_dict["NS.objects"]]
                    for k, v in zip(info_keys, info_vals):
                        if k == "framerate" and isinstance(v, str):
                            fps_match = re.match(r"([\d.]+)", v)
                            if fps_match:
                                fps = float(fps_match.group(1))
                                if fps > 0:
                                    q = time_base / fps
                                    if q == int(q) and int(q) > 0:
                                        frame_quantum = int(q)
                                    break
            if frame_quantum > 1:
                break

    return entities, max_pk, max_track, time_base, canvas_size, frame_quantum


# ---------------------------------------------------------------------------
# ScreenFlow Core Data Binary Store: Object helpers
# ---------------------------------------------------------------------------

def find_class_uid(objects, classname: str):
    """Find the UID index of an existing $class object by classname."""
    for i, obj in enumerate(objects):
        if (
            isinstance(obj, dict)
            and "$classname" in obj
            and obj["$classname"] == classname
        ):
            return i
    return None


def find_or_create_class(objects, classname: str, classes: list[str]):
    """Find or create a $class dict in objects. Returns the UID index."""
    idx = find_class_uid(objects, classname)
    if idx is not None:
        return idx
    obj = {"$classname": classname, "$classes": classes}
    objects.append(obj)
    return len(objects) - 1


def add_object(objects, obj):
    """Append an object to the $objects array and return its UID index."""
    objects.append(obj)
    return len(objects) - 1


def make_ns_null(objects):
    """Create an NSNull instance and return its UID index.

    Core Data expects null attribute values to be NSNull instances, NOT the
    '$null' sentinel string at index 0.
    """
    nsnull_cls = find_or_create_class(
        objects, "NSNull", ["NSNull", "NSObject"]
    )
    obj = {"$class": plistlib.UID(nsnull_cls)}
    return add_object(objects, obj)


def make_ns_string(objects, value: str, mutable: bool = False):
    """Create an NSString/NSMutableString object and return its UID index."""
    if mutable:
        cls_idx = find_or_create_class(
            objects,
            "NSMutableString",
            ["NSMutableString", "NSString", "NSObject"],
        )
    else:
        cls_idx = find_or_create_class(
            objects, "NSString", ["NSString", "NSObject"]
        )
    obj = {"NS.string": value, "$class": plistlib.UID(cls_idx)}
    return add_object(objects, obj)


def make_ns_color_rgba(objects, r: float, g: float, b: float, a: float):
    """Create an RGBA NSColor (colorSpace=1) and return its UID index."""
    cls_idx = find_or_create_class(
        objects, "NSColor", ["NSColor", "NSObject"]
    )
    rgba_str = f"{r} {g} {b}"
    if a < 1.0:
        rgba_str += f" {a}"
    obj = {
        "NSRGB": (rgba_str + "\x00").encode("utf-8"),
        "NSColorSpace": 1,
        "$class": plistlib.UID(cls_idx),
    }
    return add_object(objects, obj)


def make_ns_font(objects, name: str, size: float):
    """Create an NSFont object and return its UID index.

    NSName must be a UID reference to a string in $objects, not a raw string.
    """
    cls_idx = find_or_create_class(
        objects, "NSFont", ["NSFont", "NSObject"]
    )
    name_uid = add_object(objects, name)
    obj = {
        "NSName": plistlib.UID(name_uid),
        "NSSize": size,
        "NSfFlags": 16,
        "$class": plistlib.UID(cls_idx),
    }
    return add_object(objects, obj)


def make_ns_paragraph_style(objects, alignment: int = 2):
    """Create an NSMutableParagraphStyle and return its UID index."""
    cls_idx = find_or_create_class(
        objects,
        "NSMutableParagraphStyle",
        ["NSMutableParagraphStyle", "NSParagraphStyle", "NSObject"],
    )
    array_cls = find_or_create_class(
        objects, "NSArray", ["NSArray", "NSObject"]
    )
    tabs = add_object(objects, {"NS.objects": [], "$class": plistlib.UID(array_cls)})
    text_lists = add_object(objects, {"NS.objects": [], "$class": plistlib.UID(array_cls)})

    obj = {
        "NSTabStops": plistlib.UID(tabs),
        "NSAlignment": alignment,
        "NSAllowsTighteningForTruncation": 1,
        "NSDefaultTabInterval": 28.0,
        "NSTextLists": plistlib.UID(text_lists),
        "$class": plistlib.UID(cls_idx),
    }
    return add_object(objects, obj)


# ---------------------------------------------------------------------------
# ScreenFlow Core Data Binary Store: Writer
# ---------------------------------------------------------------------------

def make_track(objects, primary_key: int, track_index: int):
    """Create a Track entity. Returns (node_uid_index, primary_key)."""
    map_node_cls = find_or_create_class(
        objects,
        "NSDictionaryMapNode",
        ["NSDictionaryMapNode", "NSStoreMapNode", "NSObject"],
    )
    array_cls = find_or_create_class(
        objects, "NSArray", ["NSArray", "NSObject"]
    )

    attr_values = [
        plistlib.UID(add_object(objects, 1)),
        plistlib.UID(add_object(objects, 1)),
        plistlib.UID(add_object(objects, track_index)),
        plistlib.UID(make_ns_null(objects)),
    ]

    attrs_obj = {"NS.objects": attr_values, "$class": plistlib.UID(array_cls)}
    attrs_uid = add_object(objects, attrs_obj)
    entity_name_uid = add_object(objects, "Track")

    node = {
        "$class": plistlib.UID(map_node_cls),
        "NSAttributeValues": plistlib.UID(attrs_uid),
        "NSPrimaryKey64": primary_key,
        "NSEntityName": plistlib.UID(entity_name_uid),
    }
    node_uid = add_object(objects, node)
    return node_uid, primary_key


def write_screenflow_store(
    dat_path: str,
    main_plist,
    metadata_bytes: bytes,
    original_header: bytes,
    max_primary_key: int = 0,
):
    """Serialize and write the modified Core Data binary store."""
    main_data = plistlib.dumps(main_plist, fmt=plistlib.FMT_BINARY)

    main_offset = 64
    main_length = len(main_data)
    metadata_offset = main_offset + main_length
    metadata_length = len(metadata_bytes)

    header = bytearray(original_header)
    struct.pack_into(">Q", header, 16, metadata_offset)
    struct.pack_into(">Q", header, 24, metadata_length)
    struct.pack_into(">Q", header, 32, main_offset)
    struct.pack_into(">Q", header, 40, main_length)
    if max_primary_key > 0:
        struct.pack_into(">Q", header, 48, max_primary_key)

    with open(dat_path, "wb") as f:
        f.write(bytes(header))
        f.write(main_data)
        f.write(metadata_bytes)


# ---------------------------------------------------------------------------
# Shared object pool
# ---------------------------------------------------------------------------

class SharedPool:
    """Pre-created objects that are reused across all subtitle clips.

    Creating duplicate NSNull, string, color, font objects per subtitle bloats
    the $objects array and causes ScreenFlow save failures. This pool creates
    each shared object once and stores its UID for reuse.
    """

    def __init__(self, objects, font_name, font_size, y_position, canvas_size, subtitle_track_index):
        # Class UIDs
        self.map_node_cls = find_or_create_class(
            objects, "NSDictionaryMapNode",
            ["NSDictionaryMapNode", "NSStoreMapNode", "NSObject"],
        )
        self.array_cls = find_or_create_class(
            objects, "NSArray", ["NSArray", "NSObject"]
        )
        self.mdict_cls = find_or_create_class(
            objects, "NSMutableDictionary",
            ["NSMutableDictionary", "NSDictionary", "NSObject"],
        )
        dict_cls = find_or_create_class(
            objects, "NSDictionary", ["NSDictionary", "NSObject"]
        )
        self.attr_str_cls = find_or_create_class(
            objects, "NSAttributedString",
            ["NSAttributedString", "NSObject"],
        )

        # Single NSNull instance
        self.null = plistlib.UID(make_ns_null(objects))

        # Primitive integers
        self.int = {}
        for v in [0, 1, 3, 6]:
            self.int[v] = plistlib.UID(add_object(objects, v))
        self.int_neg1 = plistlib.UID(add_object(objects, -1))

        # Primitive floats
        self.flt = {}
        for v in [0.0, 0.1, 0.3, 0.5, 0.75, 0.8, 1.0, 4.0, 20.0, 25.0, -45.0, 40.0]:
            self.flt[v] = plistlib.UID(add_object(objects, v))

        # Shared strings
        self.str = {}
        for s in [
            "TextClip", "MediaSource", "source", "clips", "Text Clip",
            "text", "[-1 -1 1 1]",
        ]:
            self.str[s] = plistlib.UID(add_object(objects, s))

        # Booleans
        self.true = plistlib.UID(add_object(objects, True))
        self.false = plistlib.UID(add_object(objects, False))

        # Colors
        self.shadow_color = plistlib.UID(make_ns_color_rgba(objects, 0, 0, 0, 1.0))
        self.bg_color = plistlib.UID(make_ns_color_rgba(objects, 0, 0, 0, 0.75))
        self.text_color = plistlib.UID(make_ns_color_rgba(objects, 1, 1, 1, 1.0))

        # Font and paragraph style
        self.font = plistlib.UID(make_ns_font(objects, font_name, font_size))
        self.para_style = plistlib.UID(make_ns_paragraph_style(objects, alignment=2))

        # Presence and build animation strings
        self.presence = plistlib.UID(make_ns_string(objects, "Presence", mutable=True))
        self.build_in_type = plistlib.UID(make_ns_string(objects, "move", mutable=True))
        self.build_out_type = plistlib.UID(make_ns_string(objects, "move", mutable=True))

        # Build animation param dicts (identical for in/out, both disabled)
        self.build_in_params = plistlib.UID(self._make_build_params(objects, dict_cls))
        self.build_out_params = plistlib.UID(self._make_build_params(objects, dict_cls))

        # Layout values
        clip_height = int(font_size * 1.6 + 50)
        self.clip_size = plistlib.UID(add_object(objects, f"{{{canvas_size[0]}, {clip_height}}}"))
        self.y_pos = plistlib.UID(add_object(objects, y_position))
        self.track_idx = plistlib.UID(add_object(objects, subtitle_track_index))

        # Shared UUID for all MediaSources
        self.uuid = plistlib.UID(add_object(objects, str(uuid.uuid4()).upper()))

        # Shared NSAttributedString attribute dict (font, color, para style)
        attr_key_names = [
            "NSParagraphStyle", "NSStrokeWidth", "Fill", "Stroke",
            "FillType", "NSFont", "NSColor",
        ]
        attr_keys = [plistlib.UID(add_object(objects, k)) for k in attr_key_names]
        attr_vals = [
            self.para_style, self.int[3], self.true, self.false,
            self.int[0], self.font, self.text_color,
        ]
        text_attr_dict = {
            "NS.keys": attr_keys,
            "NS.objects": attr_vals,
            "$class": plistlib.UID(dict_cls),
        }
        self.text_attr_dict = plistlib.UID(add_object(objects, text_attr_dict))

    def _make_build_params(self, objects, dict_cls):
        """Create a build animation params dict. Returns UID index."""
        keys = []
        vals = []
        for k, v in [
            ("buildMode", "byLine"), ("easing", "easeInOutQuad"),
            ("fade", True), ("distance", 0.5),
            ("overlap", 0.0), ("direction", "w"),
        ]:
            keys.append(plistlib.UID(add_object(objects, k)))
            if isinstance(v, bool):
                vals.append(plistlib.UID(add_object(objects, v)))
            elif isinstance(v, str):
                vals.append(plistlib.UID(add_object(objects, v)))
            else:
                vals.append(plistlib.UID(add_object(objects, v)))
        return add_object(objects, {
            "NS.keys": keys,
            "NS.objects": vals,
            "$class": plistlib.UID(dict_cls),
        })


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def inject_subtitles(
    screenflow_path: str,
    segments: list[dict],
    output_path: str,
    font_name: str = "Inter-Bold",
    font_size: float = 60.0,
    y_position: float = 0.82,
):
    """Inject subtitle segments into a copy of the ScreenFlow document."""
    # Copy the bundle
    if os.path.exists(output_path):
        shutil.rmtree(output_path)
    shutil.copytree(screenflow_path, output_path)

    dat_path = os.path.join(output_path, "ScreenFlowDocument.dat")
    if not os.path.isfile(dat_path):
        sys.exit(f"Error: ScreenFlowDocument.dat not found in {output_path}")

    # Read
    main_plist, metadata_bytes, header = read_screenflow_store(dat_path)
    entities, max_pk, max_track, time_base, canvas_size, frame_quantum = find_entities(main_plist)

    objects = main_plist["$objects"]
    root = objects[uid_val(main_plist["$top"]["mapData"])]

    print(f"  Canvas: {int(canvas_size[0])}x{int(canvas_size[1])}")
    print(f"  Time base: {time_base} units/sec")
    print(f"  Frame quantum: {frame_quantum} units/frame ({time_base // frame_quantum}fps)")
    print(f"  Existing tracks: {max_track + 1}, max index = {max_track}")
    print(f"  Max primary key: {max_pk}")
    print(f"  Subtitle segments to inject: {len(segments)}")

    subtitle_track_index = max_track + 1
    next_pk = max_pk + 1

    # Create the Track entity
    track_node_uid, track_pk = make_track(objects, next_pk, subtitle_track_index)
    next_pk += 1
    root["NS.keys"].append(plistlib.UID(add_object(objects, track_pk)))
    root["NS.objects"].append(plistlib.UID(track_node_uid))

    # Create shared object pool
    pool = SharedPool(objects, font_name, font_size, y_position, canvas_size, subtitle_track_index)
    print(f"  Shared object pool created")

    # Create subtitle clips
    created = 0
    for seg in segments:
        start_units = round(seg["start"] * time_base / frame_quantum) * frame_quantum
        end_units = round(seg["end"] * time_base / frame_quantum) * frame_quantum
        duration_units = end_units - start_units
        if duration_units <= 0:
            continue

        ms_pk = next_pk
        tc_pk = next_pk + 1
        next_pk += 2

        # --- TextClip ---
        text_string_uid = plistlib.UID(add_object(objects, seg["text"]))
        attr_str_uid = plistlib.UID(add_object(objects, {
            "NSString": text_string_uid,
            "NSAttributes": pool.text_attr_dict,
            "$class": plistlib.UID(pool.attr_str_cls),
        }))
        start_uid = plistlib.UID(add_object(objects, start_units))
        dur_uid = plistlib.UID(add_object(objects, duration_units))

        p = pool  # shorthand
        tc_attrs = [
            p.int[6],          p.flt[1.0],     p.int[0],       p.int[0],        # 0-3
            p.flt[0.1],        p.int[0],        p.flt[0.5],     p.presence,      # 4-7
            p.null,             p.null,          p.flt[0.8],     p.int[0],        # 8-11
            p.flt[4.0],        p.flt[1.0],      p.str["[-1 -1 1 1]"], p.flt[1.0],# 12-15
            p.int[0],          dur_uid,          p.int[1],       p.int[0],        # 16-19
            p.int[0],          p.int[0],         p.int[0],       p.int[0],        # 20-23
            p.flt[0.0],        p.y_pos,          p.flt[0.0],     p.null,          # 24-27
            p.int[0],          p.flt[0.3],       p.int[0],       p.flt[0.0],      # 28-31
            p.flt[0.0],        p.flt[0.0],       p.flt[0.0],     p.flt[1.0],      # 32-35
            p.flt[1.0],        p.flt[1.0],       p.flt[-45.0],   p.shadow_color,  # 36-39
            p.flt[40.0],       p.flt[0.75],      p.flt[1.0],     start_uid,       # 40-43
            p.flt[1.0],        p.track_idx,      p.clip_size,    p.bg_color,      # 44-47
            p.null,             p.null,           p.int[1],       p.null,          # 48-51
            p.int[0],          p.flt[0.75],      p.int[0],       p.build_in_type, # 52-55
            p.build_in_params, p.flt[0.75],      p.int[0],       p.build_out_type,# 56-59
            p.build_out_params,p.flt[20.0],      p.flt[0.0],     p.flt[0.0],      # 60-63
            p.flt[25.0],       attr_str_uid,     p.int[1],       p.int[0],        # 64-67
        ]
        # 68-82: null
        tc_attrs.extend([p.null] * 15)

        tc_attrs_uid = add_object(objects, {
            "NS.objects": tc_attrs,
            "$class": plistlib.UID(p.array_cls),
        })

        # TextClip NSRelatedNodes: source -> MediaSource PK
        ms_record_id_uid = add_object(objects, ms_pk)
        source_ref_uid = add_object(objects, {
            "NS.objects": [plistlib.UID(ms_record_id_uid)],
            "$class": plistlib.UID(p.array_cls),
        })
        tc_related_uid = add_object(objects, {
            "NS.keys": [p.str["source"]],
            "NS.objects": [plistlib.UID(source_ref_uid)],
            "$class": plistlib.UID(p.mdict_cls),
        })

        tc_node_uid = add_object(objects, {
            "$class": plistlib.UID(p.map_node_cls),
            "NSRelatedNodes": plistlib.UID(tc_related_uid),
            "NSAttributeValues": plistlib.UID(tc_attrs_uid),
            "NSPrimaryKey64": tc_pk,
            "NSEntityName": p.str["TextClip"],
        })

        # --- MediaSource ---
        ms_attrs = [
            p.null,     p.int[0],   p.null,          p.int_neg1,  # 0-3
            p.int_neg1, p.uuid,     p.null,           p.null,      # 4-7
            p.null,     p.null,     p.null,           p.int[0],    # 8-11
            p.int[1],   p.int[0],   p.null,           p.null,      # 12-15
            p.null,     p.str["Text Clip"], p.null,   p.int[1],    # 16-19
            p.int[0],   p.int[0],   p.str["text"],    p.null,      # 20-23
            p.null,     p.int[0],   p.null,           p.null,      # 24-27
            p.null,     p.null,     p.null,           p.null,      # 28-31
            p.null,                                                # 32
        ]
        ms_attrs_uid = add_object(objects, {
            "NS.objects": ms_attrs,
            "$class": plistlib.UID(p.array_cls),
        })

        # MediaSource NSRelatedNodes: clips -> TextClip PK
        tc_pk_uid = add_object(objects, tc_pk)
        clips_ref_uid = add_object(objects, {
            "NS.objects": [plistlib.UID(tc_pk_uid)],
            "$class": plistlib.UID(p.array_cls),
        })
        ms_related_uid = add_object(objects, {
            "NS.keys": [p.str["clips"]],
            "NS.objects": [plistlib.UID(clips_ref_uid)],
            "$class": plistlib.UID(p.mdict_cls),
        })

        ms_node_uid = add_object(objects, {
            "$class": plistlib.UID(p.map_node_cls),
            "NSRelatedNodes": plistlib.UID(ms_related_uid),
            "NSAttributeValues": plistlib.UID(ms_attrs_uid),
            "NSPrimaryKey64": ms_pk,
            "NSEntityName": p.str["MediaSource"],
        })

        # Add both to root map
        root["NS.keys"].append(plistlib.UID(add_object(objects, ms_pk)))
        root["NS.objects"].append(plistlib.UID(ms_node_uid))
        root["NS.keys"].append(plistlib.UID(add_object(objects, tc_pk)))
        root["NS.objects"].append(plistlib.UID(tc_node_uid))

        created += 1

    write_screenflow_store(dat_path, main_plist, metadata_bytes, header, next_pk - 1)

    print(f"  Created {created} subtitle clips on track {subtitle_track_index}")
    print(f"  Output: {output_path}")

    return output_path


# ---------------------------------------------------------------------------
# Dictionary (spelling corrections)
# ---------------------------------------------------------------------------

DICTIONARY_FILENAME = "dictionary.txt"


def load_dictionary_file() -> dict[str, str]:
    """Load dictionary.txt from the same directory as this script.

    Format: one WRONG=RIGHT per line. Lines starting with # are comments.
    Blank lines are ignored. Returns an empty dict if the file doesn't exist.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    dict_path = os.path.join(script_dir, DICTIONARY_FILENAME)
    if not os.path.isfile(dict_path):
        return {}
    result = {}
    with open(dict_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                print(f"Warning: {DICTIONARY_FILENAME}:{line_num}: Ignoring malformed entry (expected WRONG=RIGHT): {line}", file=sys.stderr)
                continue
            wrong, right = line.split("=", 1)
            wrong, right = wrong.strip(), right.strip()
            if wrong and right:
                result[wrong] = right
    return result


def parse_dictionary(cli_entries: "list[str] | None") -> dict[str, str]:
    """Load dictionary from file and merge with any --dictionary CLI overrides.

    CLI entries take precedence over file entries.
    """
    # Load from dictionary.txt
    result = load_dictionary_file()

    # Merge CLI overrides
    if cli_entries:
        for entry in cli_entries:
            if "=" not in entry:
                print(f"Warning: Ignoring malformed --dictionary entry (expected WRONG=RIGHT): {entry}", file=sys.stderr)
                continue
            wrong, right = entry.split("=", 1)
            wrong, right = wrong.strip(), right.strip()
            if wrong and right:
                result[wrong] = right
    return result


def build_whisper_prompt(dictionary: dict[str, str]) -> str:
    """Build a whisper --prompt string from dictionary correct spellings.

    Uses a glossary-style prompt with the correct spellings so Whisper
    is biased toward using them during transcription.
    """
    if not dictionary:
        return ""
    correct_terms = sorted(set(dictionary.values()))
    return "Glossary: " + ", ".join(correct_terms)


def apply_dictionary(segments: list[dict], dictionary: dict[str, str]) -> list[dict]:
    """Post-process subtitle segments, replacing misspellings with correct forms.

    Performs case-insensitive whole-word replacement to catch variations like
    "Vitesse" -> "Vitess" regardless of surrounding context.
    """
    if not dictionary:
        return segments
    # Build a single regex that matches any of the wrong terms (case-insensitive, whole-word)
    patterns = []
    for wrong, right in dictionary.items():
        patterns.append((re.compile(r"\b" + re.escape(wrong) + r"\b", re.IGNORECASE), right))
    for seg in segments:
        text = seg["text"]
        for pattern, replacement in patterns:
            text = pattern.sub(replacement, text)
        seg["text"] = text
    return segments


# ---------------------------------------------------------------------------
# Output directory helpers
# ---------------------------------------------------------------------------

def ensure_output_dir(output_dir: str) -> str:
    """Create the output directory if it doesn't exist. Returns the path."""
    os.makedirs(output_dir, exist_ok=True)
    return output_dir


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Transcribe audio and inject subtitles into a ScreenFlow document.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full pipeline: transcribe + inject
  python3 screenflow-subtitles.py audio.m4a project.screenflow

  # Use existing VTT (skip transcription)
  python3 screenflow-subtitles.py audio.m4a project.screenflow --from-vtt transcript.vtt

  # Dry run (transcribe only, print segments)
  python3 screenflow-subtitles.py audio.m4a project.screenflow --dry-run

  # Custom styling
  python3 screenflow-subtitles.py audio.m4a project.screenflow \\
    --font-name GillSans --font-size 80 --y-position 0.35

  # Spelling corrections (prompt + post-processing)
  python3 screenflow-subtitles.py audio.m4a project.screenflow \\
    --dictionary Vitesse=Vitess --dictionary Niki=Neki

  # Custom output directory
  python3 screenflow-subtitles.py audio.m4a project.screenflow \\
    --output-dir ./build
""",
    )
    parser.add_argument("audio", help="Path to audio file (m4a, mp3, wav, etc.)")
    parser.add_argument("screenflow", help="Path to .screenflow document")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Path to whisper GGML model (default: {DEFAULT_MODEL})",
    )
    parser.add_argument("--language", default="en", help="Whisper language code (default: en)")
    parser.add_argument("--font-name", default="Inter-Bold", help="PostScript font name (default: Inter-Bold)")
    parser.add_argument("--font-size", type=float, default=100.0, help="Font size in points (default: 100)")
    parser.add_argument("--y-position", type=float, default=-0.671, help="Normalized Y position, 0=center, negative=toward bottom (default: -0.671)")
    parser.add_argument("--max-chars", type=int, default=39, help="Max characters per subtitle segment (default: 39, 0=no limit)")
    parser.add_argument("--dry-run", action="store_true", help="Transcribe and print segments only, don't write")
    parser.add_argument("--vtt-only", action="store_true", help="Output VTT file only, don't write to ScreenFlow")
    parser.add_argument("--from-vtt", help="Skip transcription, use existing VTT file")
    parser.add_argument(
        "--dictionary",
        action="append",
        metavar="WRONG=RIGHT",
        help="Additional spelling corrections (merged with dictionary.txt). "
             "e.g. --dictionary Vitesse=Vitess. "
             "Feeds correct spellings into Whisper's prompt and applies post-processing replacement.",
    )
    parser.add_argument(
        "--output-dir",
        help="Directory for output files (default: output/ next to ScreenFlow doc)",
    )

    args = parser.parse_args()

    # Validate inputs
    if not os.path.isfile(args.audio):
        sys.exit(f"Error: Audio file not found: {args.audio}")
    if not os.path.isdir(args.screenflow):
        sys.exit(f"Error: ScreenFlow document not found: {args.screenflow}")

    # Parse dictionary for spelling corrections (file + CLI overrides)
    dictionary = parse_dictionary(args.dictionary)
    whisper_prompt = build_whisper_prompt(dictionary)
    if dictionary:
        print(f"Dictionary: {len(dictionary)} correction(s) (from {DICTIONARY_FILENAME} + CLI)")
        for wrong, right in dictionary.items():
            print(f"  {wrong} -> {right}")

    # Determine output directory
    if args.output_dir:
        output_dir = args.output_dir
    else:
        output_dir = os.path.join(os.path.dirname(os.path.abspath(args.screenflow)), "output")
    ensure_output_dir(output_dir)

    # Step 1: Transcribe or load VTT
    if args.from_vtt:
        if not os.path.isfile(args.from_vtt):
            sys.exit(f"Error: VTT file not found: {args.from_vtt}")
        vtt_path = args.from_vtt
        print(f"Using existing VTT: {vtt_path}")
    else:
        vtt_path = transcribe(args.audio, output_dir, args.model, args.language, prompt=whisper_prompt)
        print(f"VTT saved: {vtt_path}")

    # Step 2: Parse VTT, apply dictionary corrections, and rechunk
    segments = parse_vtt(vtt_path)
    if dictionary:
        segments = apply_dictionary(segments, dictionary)
        print(f"Applied {len(dictionary)} dictionary correction(s) to segments")
    if args.max_chars and args.max_chars > 0:
        segments = rechunk_segments(segments, args.max_chars)
    print(f"\n{len(segments)} subtitle segments:")
    for i, seg in enumerate(segments):
        start_f = f"{int(seg['start']//60):02d}:{seg['start']%60:06.3f}"
        end_f = f"{int(seg['end']//60):02d}:{seg['end']%60:06.3f}"
        text_preview = seg["text"][:60] + ("..." if len(seg["text"]) > 60 else "")
        print(f"  [{i+1:3d}] {start_f} -> {end_f}  {text_preview}")

    if args.dry_run:
        print("\n(Dry run -- not writing to ScreenFlow document)")
        return

    if args.vtt_only:
        # If using --from-vtt, copy into output dir; if transcribed, it's already there
        audio_stem = os.path.splitext(os.path.basename(args.audio))[0]
        out_vtt = os.path.join(output_dir, f"{audio_stem}.vtt")
        if os.path.abspath(vtt_path) != os.path.abspath(out_vtt):
            shutil.copy2(vtt_path, out_vtt)
        print(f"\nVTT written to: {out_vtt}")
        return

    # Step 3: Determine output .screenflow path
    sf_stem = os.path.basename(args.screenflow.rstrip("/"))
    if sf_stem.endswith(".screenflow"):
        sf_stem = sf_stem[: -len(".screenflow")]
    output_path = os.path.join(output_dir, f"{sf_stem}-subtitled.screenflow")

    # Step 4: Inject subtitles
    print(f"\nInjecting subtitles into ScreenFlow document...")
    inject_subtitles(
        screenflow_path=args.screenflow,
        segments=segments,
        output_path=output_path,
        font_name=args.font_name,
        font_size=args.font_size,
        y_position=args.y_position,
    )

    # Save VTT alongside output for reference
    audio_stem = os.path.splitext(os.path.basename(args.audio))[0]
    out_vtt = os.path.join(output_dir, f"{audio_stem}.vtt")
    if os.path.abspath(vtt_path) != os.path.abspath(out_vtt):
        shutil.copy2(vtt_path, out_vtt)
    print(f"  VTT: {out_vtt}")

    print("\nDone! Open the output in ScreenFlow to verify.")


if __name__ == "__main__":
    main()
