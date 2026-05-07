# ScreenFlow Document Format: Reverse Engineering Notes

## Overview

A `.screenflow` file is a **macOS bundle** (package directory) containing the project data, media files, and metadata. This document describes the internal structure as discovered through reverse engineering `hyperdrive-social.screenflow` (created with ScreenFlow 10.5.2).

## Bundle Structure

```
hyperdrive-social.screenflow/
├── version.plist              # App version info (binary plist)
├── thumbnail.jpg              # Project thumbnail
├── ScreenFlowDocument.dat     # Core Data binary store (the main project file)
└── Media/                     # Media assets
    ├── Recording-*.scc        # Screen/camera recordings (ScreenFlow container)
    └── *.jpg                  # Image assets
```

### version.plist

```
CreationVersion: "10.5.2"
CreationVersionTag: "32103"
SCAsynchronousSavingEnabled: false
```

## ScreenFlowDocument.dat Format

This is a **Core Data Binary Store** (not SQLite, not XML). Its structure is:

### File Header (64 bytes)

| Offset | Size | Endian | Description |
|--------|------|--------|-------------|
| 0 | 8 | - | Magic: `CoreData` (ASCII) |
| 8 | 8 | BE | Version/flags: `0x0000000108040800` |
| 16 | 8 | BE | Metadata bplist offset (e.g., `249660`) |
| 24 | 8 | BE | Metadata bplist length (e.g., `3257`) |
| 32 | 8 | BE | Main data bplist offset (always `64`) |
| 40 | 8 | BE | Main data bplist length (e.g., `249596`) |
| 48 | 8 | BE | **Max primary key** in the store (e.g., `1532`). Must be updated when adding new records. |
| 56 | 8 | BE | Reserved (`0`) |

**Verification:** `header(64) + main_data_length + metadata_length = file_size`

### Main Data (NSKeyedArchiver bplist)

Starting at offset 64, this is a **binary plist** containing an `NSKeyedArchiver`-encoded object graph.

```
$version: 100000
$archiver: "NSKeyedArchiver"
$top: { mapData: UID(1) }
$objects: [ ... 3665 objects ... ]
```

The root object at `$top.mapData` is an `NSMutableDictionary` mapping **586 record entries**. Each entry maps an integer primary key to an `NSDictionaryMapNode` object.

### Metadata (NSKeyedArchiver bplist)

At the end of the file, contains Core Data model version hashes, the store UUID, and one critical field:

- **`kMDItemDurationSeconds`**: `203.3` (total project duration in seconds)
- **`NSStoreType`**: `"Binary"`
- **`NSStoreUUID`**: `"13C465F8-26A9-4AC2-B880-FD62C05C75D7"`

The metadata also lists all **33 Core Data entity types** with their version hashes.

## Entity Model

Each record in the store is an `NSDictionaryMapNode` with:

```
$class: NSDictionaryMapNode
NSPrimaryKey64: <integer>        # Unique record ID
NSEntityName: <string>           # Entity type name
NSAttributeValues: NSArray       # Ordered array of attribute values
NSRelatedNodes: NSMutableDictionary  # Relationships (optional)
```

### Entity Type Counts (this document)

| Entity | Count | Description |
|--------|-------|-------------|
| `Track` | 10 | Timeline tracks |
| `MediaClip` | 13 | Video/audio clip instances |
| `ScreenMediaClip` | 11 | Screen recording clip instances |
| `TextClip` | 5 | Text overlay clip instances |
| `MediaSource` | 55 | Source media references |
| `AudioFilter` | 364 | Audio processing filters |
| `AudioChannelMix` | 18 | Audio channel routing |
| `AudioChannelsMix` | 18 | Audio channel group routing |
| `Transition` | 87 | Clip transitions |
| `VideoAction` | 4 | Video actions (zoom, pan, etc.) |
| `DocumentProperties` | 1 | Global project settings |

Other entity types exist in the model but had zero instances: `Marker`, `NestedMediaClip`, `Annotation`, `TouchCallout`, `AnnotationsClip`, `TemplatePlaceholderClip`, `VideoFilter`, `ClipMarker`, `CalloutAction`, `Caption`, `ComputerAudioAppSource`, `AnnotationsAction`, `SnapbackAction`, `Filter`, `ComputerAudioAppStream`, `SizeableClip`, `ScreenRecordingAction`, `TitleClip`, `Action`, `MediaClipGroup`, `AudioAction`, `SpeechClip`.

## Time Base and Frame Quantum

**All times are stored in units of 1/3000th of a second** (time base = 3000).

This is stored in `DocumentProperties.attr[8] = 3000`.

Conversion:
```
seconds = stored_value / 3000
stored_value = seconds * 3000
```

Examples:
- `15000` units = `5.0` seconds
- `603000` units = `201.0` seconds
- `609900` units = `203.3` seconds (matches `kMDItemDurationSeconds`)

### Frame Quantum

Clip start times and durations must be **multiples of the frame quantum** — the number of time base units per video frame. This is derived from the source media's frame rate (found in `MediaSource.attr[14].framerate`):

```
frame_quantum = time_base / fps
```

| FPS | Quantum (at time_base=3000) |
|-----|----------------------------|
| 24  | 125                        |
| 25  | 120                        |
| 30  | 100                        |
| 50  | 60                         |
| 60  | 50                         |

See Writing Rules section 8 for details on quantization errors.

## Track Entity

**4 attributes:**

| Index | Type | Description |
|-------|------|-------------|
| 0 | int | Always `1` (enabled flag?) |
| 1 | int | Always `1` (visible flag?) |
| 2 | int | **Track index** (see below) |
| 3 | NSNull | Padding/reserved |

### Track Index Convention

- **Positive indices (0, 1, 2, 3, 4, 5...)**: Video/text layers. Higher = rendered on top.
- **Negative indices (-1, -2, -3, -4...)**: Audio-only layers.

Clips reference their track by storing the track index in `attr[45]`, not by a Core Data relationship.

### Track Layout (this document)

| Track | Content |
|-------|---------|
| 5 | Text clips: "Hello", "World" |
| 4 | (empty) |
| 3 | Additional video (freeze frames, intro media) |
| 2 | Video overlays + text ("Hyperdrive", "Watch the full video") |
| 1 | Secondary video + text ("youtube.com/planetscale") |
| 0 | Primary video/audio (camera, microphone) |
| -1 | Audio-only (Cam Link audio) |
| -2 | Audio-only |
| -3 | Audio-only |
| -4 | Audio-only |

## TextClip Entity (Key Focus)

**83 attributes.** These are the fields identified through comparison of the "Hello" and "World" text clips:

### Critical Fields

| Index | Type | Description | Hello Value | World Value |
|-------|------|-------------|-------------|-------------|
| 17 | int | **Duration** (time base units) | `15000` (5.0s) | `15000` (5.0s) |
| 43 | int | **Start time** (time base units) | `0` (0.0s) | `15000` (5.0s) |
| 45 | int | **Track index** | `5` | `5` |
| 46 | string | **Size** as `{width, height}` | `{212, 112}` | `{253, 112}` |
| 65 | NSAttributedString | **The actual text content** with font/color/paragraph attributes | `"Hello"` | `"World"` |

### Animation/Appearance Fields

| Index | Type | Description | Value |
|-------|------|-------------|-------|
| 55 | NSMutableString | Build-in animation type | `"move"` |
| 56 | NSDictionary | Build-in animation params | `{buildMode: "byLine", easing: "easeInOutQuad", fade: true, distance: 0.5, overlap: 0.0, direction: "w"}` |
| 57 | float | Build-in animation duration | `0.75` |
| 58 | int | Build-in animation enabled | `0` or `1` |
| 59 | NSMutableString | Build-out animation type | `"move"` |
| 60 | NSDictionary | Build-out animation params | Same structure as build-in |

### Position and Layout

Position is stored as a **normalized offset from canvas center**. A value of `0.0` means centered along that axis. The normalization factor is half the canvas dimension (so `1.0` = edge of canvas).

| Index | Type | Description |
|-------|------|-------------|
| 24 | float | **X position** (normalized). `0.0` = centered horizontally. Pixel offset = `value * (canvasWidth / 2)` |
| 25 | float | **Y position** (normalized). `0.0` = centered vertically. **Positive = up, negative = down** |
| 26 | float | **Rotation** in degrees |
| 46 | string | **Clip size** in absolute pixels as `"{width, height}"` |
| 35 | float | **Scale X** | 
| 36 | float | **Scale Y** |
| 37 | float | **Scale Z** |
| 14 | string | **Crop rect** as `"[left bottom right top]"`, normalized. `"[-1 -1 1 1]"` = no crop |
| 61 | float | **Padding left** (pixels) |
| 62 | float | **Padding top** (pixels) |
| 63 | float | **Padding right** (pixels) |
| 64 | float | **Padding bottom** (pixels) |

#### Position Examples (canvas 3840x2160)

| Clip | X (norm) | Y (norm) | X (px from center) | Y (px from center) | Size |
|------|----------|----------|---------------------|---------------------|------|
| "Hello" | 0.0109 | ~0.0 | 21px right | centered | {212, 112} |
| "World" | 0.0216 | ~0.0 | 42px right | centered | {253, 112} |
| "Hyperdrive" | ~0.0 | ~0.0 | centered | centered | {3840, 606} |
| "Watch the full video" | 0.0 | 0.1403 | centered | 152px below center | {3840, 244} |
| "youtube.com/planetscale" | ~0.0 | -0.1403 | centered | 152px above center | {3840, 244} |

**Y axis is inverted**: negative values move toward the bottom of the screen. For subtitles near the bottom, use `y = -0.80` to `-0.85` (roughly 80-85% of half-canvas below center on a 2160px canvas).

### Visual Properties

| Index | Type | Description | Value |
|-------|------|-------------|-------|
| 0 | int | Unknown (always `6`) | `6` |
| 1 | float | **Opacity** | `1.0` |
| 2 | int | Unknown flag | `0` or `1` |
| 4 | float | Unknown | `0.1` |
| 6 | float | Unknown | `0.5` |
| 7 | NSMutableString | Presence mode | `"Presence"` |
| 10 | float | Unknown | `0.8` |
| 12 | float | Unknown | `4.0` |
| 15 | float | Overall scale multiplier | `1.0` |
| 18 | int | Unknown (always `1`) | `1` |
| 27 | string/null | Clip name/label (shown in timeline) | `null` or `"Text Clip"` |
| 29 | float | Unknown | `0.3` |
| 42 | float | Unknown | `1.0` |
| 50 | int | Unknown (always `1`) | `1` |
| 53 | float | Unknown | `0.75` |
| 66 | int | **Background fill enabled** (`1` = show backdrop box, `0` = no backdrop) | `0` or `1` |
| 67 | int | Unknown flag (possibly alternate background mode) | `0` or `1` |
| 68-82 | NSNull | Reserved/unused | all `null` |

### Shadow Properties

| Index | Type | Description | Value |
|-------|------|-------------|-------|
| 38 | float | **Shadow angle** (degrees) | `-45.0` |
| 39 | NSColor | **Shadow color** | RGB `"0 0 0"` (black), NSColorSpace=2 |
| 40 | float | **Shadow blur radius** | `40.0` |
| 41 | float | **Shadow opacity** | `0.75` |

### Background Color

| Index | Type | Description | Value |
|-------|------|-------------|-------|
| 47 | NSColor | **Background color** (behind text box) | RGBA `"0 0 0 0.75"` (black at 75% opacity), NSColorSpace=1 |

The background is the rectangular area behind the text. For subtitles, this acts as the "subtitle box" backdrop.

## NSColor Encoding

Colors are NSColor objects with different color spaces:

| NSColorSpace | Format | Fields | Example |
|-------------|--------|--------|---------|
| 1 | Calibrated RGBA | `NSRGB`: space-separated `"R G B A"` | `"0 0 0 0.75"` = black 75% |
| 2 | Device RGB | `NSRGB`: space-separated `"R G B"` | `"0 0 0"` = black |
| 3 | Custom (grayscale + ICC) | `NSWhite`, `NSComponents`, `NSCustomColorSpace` | `"1"` = white |

Values are floats from 0.0 to 1.0. They are stored as null-terminated byte strings.

## NSAttributedString Format (attr[65])

The text content is stored as an `NSAttributedString` with this structure:

```
NSAttributedString {
    NSString: "Hello"                    # The plain text
    NSAttributes: NSDictionary {         # Formatting attributes
        NSParagraphStyle: NSMutableParagraphStyle {
            NSAlignment: 4               # See alignment table below
            NSAllowsTighteningForTruncation: 1
            NSDefaultTabInterval: 28.0
        }
        NSStrokeWidth: 3                 # Outline width (0 = no outline)
        Fill: true                       # Fill the text glyphs
        Stroke: false                    # Don't stroke the outline
        FillType: 0                      # Solid fill
        NSFont: NSFont {
            NSName: "GillSans"           # PostScript font name
            NSSize: 96.0                 # Font size in points
            NSfFlags: 16                 # Font descriptor flags
        }
        NSColor: NSColor {
            NSColorSpace: 3              # Grayscale with custom ICC profile
            NSWhite: "1\0"               # White
            NSComponents: "1 1"          # (value, alpha)
            NSCustomColorSpace: NSColorSpace { NSID: 9, NSICC: <bytes>, NSModel: 0 }
            NSLinearExposure: "1"
        }
    }
}
```

### NSAlignment Values

| Value | Meaning |
|-------|---------|
| 0 | Left |
| 1 | Right |
| 2 | Center |
| 3 | Justified |
| 4 | Natural (follows system locale, effectively center for this use) |

### Font Comparison Across All Text Clips

| Clip | Font Name | Size | Notes |
|------|-----------|------|-------|
| "Hello" | `GillSans` | 96pt | Standard weight |
| "World" | `GillSans` | 96pt | Standard weight |
| "Hyperdrive" | `Inter-Medium` | 500pt | Large title |
| "Watch the full video" | `Inter-Medium` | 200pt | Medium weight |
| "youtube.com/planetscale" | `Inter-Bold` | 200pt | Bold weight |

All text clips use **white text** (NSColor grayscale `1`), **no stroke/outline** (Stroke=false), and **solid fill** (Fill=true, FillType=0).

## MediaSource Entity

Each clip references a `MediaSource` via `NSRelatedNodes.source`. MediaSources come in two flavors: **file-backed** (video/audio/image) and **text** (for TextClips).

**33 attributes.** Key fields:

| Index | Type | Description | Notes |
|-------|------|-------------|-------|
| 0 | NSDate | Creation date | File-backed only |
| 1 | int | Source type | `2` = file, `0` = text |
| 5 | string | **UUID identifier** | Unique per source |
| 6 | float | **Duration** (seconds) | File-backed only (e.g., `448.0`) |
| 7 | int | **Timescale** | File-backed only (e.g., `1000000`) |
| 8 | int | **Duration in timescale units** | `attr[6] * attr[7]` (e.g., `448000000`) |
| 9 | string | File path | File-backed only |
| 14 | NSDictionary | **Media metadata** | File-backed only; see below |
| 17 | string | Display name | `"Text Clip"` for text, `"clip.mp4"` for files |
| 22 | string | **Media type** | `"text"` or `"file"` |
| 24 | int | Video height (pixels) | File-backed only (e.g., `1080`) |
| 25 | int | Unknown | File-backed only (e.g., `250`) |
| 26 | int | Video width (pixels) | File-backed only (e.g., `1920`) |

### MediaSource attr[14] — Media Metadata Dictionary

For file-backed MediaSources, `attr[14]` is an `NSDictionary` containing technical metadata about the source file. This is critical for determining the **frame rate** needed for duration quantization (see Writing Rules, section 8).

| Key | Type | Example | Description |
|-----|------|---------|-------------|
| `resolution` | string | `"1920 x 1080"` | Video resolution |
| `framerate` | string | `"60.00 fps"` | **Frame rate** — parse this to compute frame quantum |
| `videoBitrate` | string | `"1536 Kb/s"` | Video bitrate |
| `codecs` | string | `"H.264, MPEG-4 AAC"` | Codec names |
| `audioChannels` | string | `"2"` | Audio channel count |
| `audioBitrate` | string | `"127 Kb/s"` | Audio bitrate |

### Text-only MediaSource

For text clips, the MediaSource is a lightweight reference. Most fields are `NSNull`, `0`, or `-1`. The key fields are `attr[5]` (UUID), `attr[17]` = `"Text Clip"`, and `attr[22]` = `"text"`.

## DocumentProperties Entity

**10 attributes:**

| Index | Type | Description | Value |
|-------|------|-------------|-------|
| 0 | string | Canvas rect | `"{{0, 0}, {3840, 2160}}"` |
| 1 | record_ref | Primary MediaSource | -> MediaSource pk=1 |
| 2 | NSNull | - | - |
| 3 | string | Canvas size | `"{3840, 2160}"` |
| 4 | string | File path | `"/Users/.../hyperdrive-social.screenflow"` |
| 5 | record_ref | Primary MediaClip | -> MediaClip pk=100 |
| 6 | int | Number of video tracks? | `5` |
| 7 | NSDictionary | **UI state** (window frame, zoom, timeline scale, transport time, export properties, etc.) | see below |
| 8 | int | **Time base (units per second)** | `3000` |
| 9 | float | Unknown | `1.0` |

### UI State Dictionary (attr[7]) Notable Fields

```
timelineHorizontalScale: 61.54
currentTransportTime: 24600      # Playhead position (in time base units)
zoom: 0.2
showCaptionTrack: false
captionsVisible: false
showWaveforms: true
currentCaptionLanguage: "en"
documentSaveCounter: 677
```

## Relationship Structure

Entities link to each other through `NSRelatedNodes`, which is an `NSMutableDictionary` mapping relationship names to arrays of record ID references.

**TextClip relationships:**
```
NSRelatedNodes: {
    source: [<record_id>]    # -> MediaSource entity
}
```

**Track entities have NO relationships** -- clips reference tracks by storing the track index integer in `attr[45]`, not via Core Data relationships.

## Primary Key Space

Record IDs (primary keys) range from `1` to `1532` in this document. New records must use IDs above the current maximum. The IDs do not need to be sequential.

---

---

## Writing to ScreenFlow Documents

The following documents the complete, tested process for programmatically adding TextClip entities to a ScreenFlow document. This has been verified with ScreenFlow 10.5.2 — documents open correctly, render properly, and save without errors.

### Critical Rules (Lessons Learned)

#### 1. Null Values Must Be Proper NSNull Objects

Core Data attribute arrays require **NSNull instances** for null/empty values — NOT the `$null` sentinel string at `$objects[0]`.

**Wrong** (causes crash on open):
```python
null_uid = 0  # Points to '$null' string
plistlib.UID(null_uid)
```

**Correct**:
```python
nsnull_cls = find_or_create_class(objects, "NSNull", ["NSNull", "NSObject"])
null_obj = {"$class": plistlib.UID(nsnull_cls)}
null_uid = add_object(objects, null_obj)
plistlib.UID(null_uid)
```

Each NSNull instance is a separate `{"$class": UID(nsnull_cls)}` dict in the `$objects` array. You can (and should) reuse a single instance across multiple attribute slots by referencing the same UID index.

#### 2. Bidirectional Relationships Are Required

TextClip and MediaSource have a **bidirectional** Core Data relationship. Both sides must be populated:

- **TextClip** `NSRelatedNodes.source` → `[MediaSource_PK]`
- **MediaSource** `NSRelatedNodes.clips` → `[TextClip_PK]`

Missing the MediaSource → TextClip back-reference causes a crash (`EXC_BAD_ACCESS` / `KERN_INVALID_ADDRESS at 0x0000000000000000`) when ScreenFlow attempts to fulfill Core Data faults via `-[NSDictionaryMapNode valueForKey:]`.

#### 3. NSFont.NSName Must Be a UID Reference

The `NSName` field in an `NSFont` object must be a **UID reference** to a string in `$objects`, not a raw string value.

**Wrong** (font silently falls back to system font):
```python
{"NSName": "Inter-Bold", "NSSize": 120.0, ...}
```

**Correct**:
```python
name_uid = add_object(objects, "Inter-Bold")
{"NSName": plistlib.UID(name_uid), "NSSize": 120.0, ...}
```

#### 4. Reuse Shared Objects to Avoid Save Failures

Creating duplicate objects for every subtitle clip (NSNull instances, strings, colors, fonts, etc.) bloats the `$objects` array. With 137 subtitle clips, this grew from 3,665 to 27,648 objects and caused ScreenFlow to fail with "internal validation error" on save.

**Solution**: Pre-create a shared object pool before the subtitle loop. Each subtitle should only add objects that are unique to it (text string, NSAttributedString, start time, duration, primary keys, relationship nodes). Everything else should reference shared UID indices.

With shared objects, 137 subtitles only adds ~2,300 objects (total ~5,950), and ScreenFlow saves successfully.

#### 5. Update Header Max Primary Key

The header field at offset 48 (8 bytes, big-endian uint64) stores the **maximum primary key** in the store. This must be updated when adding new records:

```python
struct.pack_into(">Q", header, 48, new_max_pk)
```

#### 6. NSAttributedString Text Color — Use RGB, Not Grayscale

The original document uses grayscale NSColor (colorspace 3) with `NSCustomColorSpace` containing ICC profile data. Reproducing this exactly is complex. Using **RGB colorspace 1** for white text works reliably:

```python
# White text via RGB
{"NSRGB": b"1 1 1\x00", "NSColorSpace": 1, "$class": plistlib.UID(color_cls)}
```

#### 7. Background Fill Flag

`attr[66] = 1` enables the background fill box behind text. Without this, the background color in `attr[47]` is ignored:

| attr[66] | attr[67] | Effect |
|----------|----------|--------|
| `1` | `0` | Background fill visible (used by Hello/World clips) |
| `0` | `1` | Alternate mode (used by Hyperdrive/title clips) |
| `0` | `0` | No background fill |

#### 8. Clip Duration and Start Time Must Be Quantized to Frame Boundaries

Start times (`attr[43]`) and durations (`attr[17]`) must be **multiples of the frame quantum** (see Time Base and Frame Quantum section above). For example, with `time_base=3000` and `60fps`, the quantum is `50` units. All start times and durations must be multiples of 50.

The frame rate is auto-detected from `MediaSource.attr[14].framerate` (see MediaSource Entity section).

**Quantization formula:**
```python
frame_quantum = time_base // fps  # e.g., 3000 // 60 = 50
start_units = round(seconds * time_base / frame_quantum) * frame_quantum
```

If values are not properly quantized, ScreenFlow will allow the document to **open** but will refuse to **save** after any manual edits to clip timing, with the error: `"Clip Duration is not quantized correctly"`.

#### 9. Y-Axis Is Inverted

Positive Y values move **up** (toward top of screen), negative values move **down** (toward bottom). For subtitles at the bottom of a 4K canvas, use `y_position ≈ -0.82`.

### Per-Subtitle Object Requirements

For each subtitle, create **2 Core Data entity records** plus their supporting objects:

#### TextClip Entity

An `NSDictionaryMapNode` with:
- `NSPrimaryKey64`: unique integer > max existing PK
- `NSEntityName`: UID → `"TextClip"` string
- `NSAttributeValues`: UID → NSArray of 83 attribute UIDs
- `NSRelatedNodes`: UID → NSMutableDictionary with `source` → `[MediaSource_PK]`

Per-subtitle unique objects (everything else is shared):
- The subtitle text string
- An `NSAttributedString` wrapping the text + shared attribute dict
- Start time integer
- Duration integer
- The attribute array itself (83 UIDs, mostly shared references)
- The `NSRelatedNodes` dict and its reference array
- Root map key (the PK integer) and value (the node UID)

#### MediaSource Entity

An `NSDictionaryMapNode` with:
- `NSPrimaryKey64`: unique integer
- `NSEntityName`: UID → `"MediaSource"` string
- `NSAttributeValues`: UID → NSArray of 33 attribute UIDs
- `NSRelatedNodes`: UID → NSMutableDictionary with `clips` → `[TextClip_PK]`

Per-subtitle unique objects:
- The attribute array (33 UIDs, mostly shared references)
- The `NSRelatedNodes` dict and its reference array
- Root map key/value entries

#### Track Entity (one per subtitle layer)

An `NSDictionaryMapNode` with:
- `NSPrimaryKey64`: unique integer
- `NSEntityName`: UID → `"Track"` string
- `NSAttributeValues`: UID → NSArray of 4 attributes `[1, 1, track_index, NSNull]`
- No `NSRelatedNodes` (tracks don't have relationships)

### Shared Object Pool (Create Once, Reuse Across All Subtitles)

| Object | Type | Notes |
|--------|------|-------|
| NSNull instance | `{"$class": UID(nsnull_cls)}` | One instance referenced ~30 times per subtitle |
| Common integers | `0`, `1`, `3`, `6`, `-1` | Attribute values |
| Common floats | `0.0`, `0.1`, `0.3`, `0.5`, `0.75`, `0.8`, `1.0`, `4.0`, `20.0`, `25.0`, `-45.0`, `40.0` | Attribute values, padding, shadow |
| Entity name strings | `"TextClip"`, `"MediaSource"` | Referenced by every entity node |
| Relationship key strings | `"source"`, `"clips"` | Referenced by every relationship dict |
| MediaSource strings | `"Text Clip"`, `"text"` | attr[17] and attr[22] |
| Attribute key strings | `"NSParagraphStyle"`, `"NSStrokeWidth"`, `"Fill"`, `"Stroke"`, `"FillType"`, `"NSFont"`, `"NSColor"` | NSAttributedString attribute dict keys |
| Boolean values | `True`, `False` | Fill/Stroke flags |
| NSFont | `{NSName: UID→font_name, NSSize: size, NSfFlags: 16}` | Same font for all subtitles |
| NSParagraphStyle | `{NSAlignment: 2, ...}` | Same alignment for all |
| NSColor (text) | RGB white `"1 1 1\0"` | |
| NSColor (shadow) | RGB black `"0 0 0\0"` | |
| NSColor (background) | RGBA `"0 0 0 0.75\0"` | |
| Build animation params | NSDictionary with easing/direction | Identical for all, disabled via attr[58]=0 |
| Clip size string | `"{3840, 242}"` | Full canvas width, height based on font size |
| Y-position float | e.g., `-0.82` | |
| Track index integer | e.g., `6` | |
| MediaSource UUID string | Shared UUID | All text MediaSources can share one UUID |
| NSAttributedString attribute dict | The attributes dict (font, color, para style) | Shared; only `NSString` varies per subtitle |

### Complete Write Procedure

```
1. Copy .screenflow bundle to output path
2. Read ScreenFlowDocument.dat:
   a. Parse 64-byte header (offsets, lengths)
   b. Parse main bplist via plistlib.loads()
   c. Preserve metadata bytes unchanged
3. Analyze existing data:
   a. Find max primary key
   b. Find max track index
   c. Get time base and canvas size from DocumentProperties
4. Create shared object pool (see table above)
5. Create Track entity for subtitles (track_index = max + 1)
6. For each subtitle segment:
   a. Allocate 2 primary keys (MediaSource PK, TextClip PK)
   b. Create text string + NSAttributedString (referencing shared attr dict)
   c. Create start time + duration integers
   d. Build TextClip attribute array (83 UIDs, mostly shared)
   e. Build TextClip NSRelatedNodes (source → MediaSource PK)
   f. Build TextClip entity node
   g. Build MediaSource attribute array (33 UIDs, mostly shared)
   h. Build MediaSource NSRelatedNodes (clips → TextClip PK)
   i. Build MediaSource entity node
   j. Add both to root map (NS.keys + NS.objects)
7. Write ScreenFlowDocument.dat:
   a. Serialize main plist: plistlib.dumps(main_plist, fmt=FMT_BINARY)
   b. Rebuild header with new offsets/lengths and max PK
   c. Write: header(64) + main_data + metadata_bytes
```

### plistlib Re-serialization Note

`plistlib.dumps()` produces a **more compact** binary plist than the original (e.g., 249KB → 143KB for the same data). This is because it uses more efficient integer/offset encoding. The data is semantically identical — a round-trip read/write with zero changes produces a document that ScreenFlow opens and saves correctly.

### Reference Implementation

See `screenflow-subtitles.py` in this directory for a complete, working implementation including:
- Whisper-cpp transcription (with proper temp file cleanup)
- VTT parsing with word-count rechunking
- `SharedPool` class that pre-creates all reusable objects once
- Full TextClip + MediaSource entity creation using shared pool references
- All output files (VTT, subtitled .screenflow) written to an `output/` directory
- CLI with `--dry-run`, `--from-vtt`, `--vtt-only`, `--font-name`, `--font-size`, `--y-position`, `--max-words`, `--output-dir`
