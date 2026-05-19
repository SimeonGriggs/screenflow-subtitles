# screenflow-subtitles

Transcribe audio and inject timed subtitle text clips directly into a ScreenFlow document. No manual subtitle work needed — just point it at an audio file and a `.screenflow` project and it handles transcription, chunking, and binary document injection.

## Requirements

- **whisper-cpp** — `brew install whisper-cpp`
- **ffmpeg** — `brew install ffmpeg`
- **Python 3.9+** (stdlib only, no pip packages)
- **Whisper model** — download once:
  ```
  mkdir -p ~/.local/share/whisper-cpp
  curl -L -o ~/.local/share/whisper-cpp/ggml-large-v3-turbo.bin \
    https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin
  ```

## Usage

```
python3 screenflow-subtitles.py <audio_file> <screenflow_document> [options]
```

The script creates a subtitled copy of the ScreenFlow document in an `output/` directory next to the original.

### Examples

```sh
# Full pipeline: transcribe + inject
python3 screenflow-subtitles.py audio.m4a project.screenflow

# Use an existing VTT file (skip transcription)
python3 screenflow-subtitles.py audio.m4a project.screenflow --from-vtt transcript.vtt

# Preview segments without writing (dry run)
python3 screenflow-subtitles.py audio.m4a project.screenflow --dry-run

# Custom font and position
python3 screenflow-subtitles.py audio.m4a project.screenflow \
  --font-name GillSans --font-size 80 --y-position -0.5
```

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--model PATH` | `~/.local/share/whisper-cpp/ggml-large-v3-turbo.bin` | Path to whisper GGML model |
| `--language CODE` | `en` | Whisper language code |
| `--font-name NAME` | `Inter-Bold` | PostScript font name |
| `--font-size SIZE` | `100` | Font size in points |
| `--y-position Y` | `-0.671` | Normalized Y position (0 = center, negative = toward bottom) |
| `--max-chars N` | `39` | Max characters per subtitle line (0 = no limit) |
| `--no-sentence-break` | — | Don't split subtitles at sentence boundaries (`.!?`) |
| `--from-vtt PATH` | — | Skip transcription, use an existing VTT file |
| `--dry-run` | — | Transcribe and print segments only, don't modify the document |
| `--vtt-only` | — | Output VTT file only, don't write to ScreenFlow |
| `--output-dir DIR` | `output/` next to input | Directory for output files |

## How it works

1. **Transcribe** — converts audio to 16kHz WAV, runs whisper-cpp, outputs a VTT file
2. **Chunk** — splits subtitle segments by character count (default 39), preferring breaks at sentence endings (`.!?`) and clause boundaries (`,;:`) using dynamic programming for balanced line lengths
3. **Inject** — copies the ScreenFlow bundle, parses the Core Data binary store, creates TextClip + MediaSource entity pairs for each subtitle on a new track, quantizes all timings to frame boundaries, and writes the modified document

All timing is auto-quantized to the document's frame rate (detected from the source media metadata) so clips can be freely edited in ScreenFlow without save errors.

## Format documentation

See [SCREENFLOW-FORMAT.md](SCREENFLOW-FORMAT.md) for reverse-engineering notes on the ScreenFlow binary document format.
# screenflow-subtitles
