# Trace game extractor

Local FFmpeg wrapper for joining captured Trace game parts. Sound is not required:
the default output contains video only, even when an input contains audio.

This repository contains the standalone extractor and synthetic tests only.
Original footage and existing extraction outputs remain outside this repository.

## Requirements

- Python 3.10 or newer
- `ffmpeg` and `ffprobe` on `PATH`

Check the local tools:

```bash
python3 --version
ffmpeg -version
ffprobe -version
```

## Usage

Clone the private repository, using an account with access:

```bash
git clone https://github.com/MrMarysville/trace-game-extractor.git
```

`example1-12345678` below is a dummy reference; replace it with your own game.
Only use footage and source links you are authorized to access.

Easiest: pass the Trace game reference (`team-gameid`, visible in the game's
poster/API URL). The extractor discovers every half from Trace's public game
metadata and selects the highest declared pixel count, then bandwidth (no upscaling):

```bash
cd trace-game-extractor
python3 trace_game_extractor.py --game example1-12345678
```

Default destination for `--game` is `Trace-<team-gameid>.video-only.mp4` beside
the script.

Or pass captured HLS manifests or media parts in game order:

```bash
cd trace-game-extractor
python3 trace_game_extractor.py half-1.m3u8 half-2.m3u8
```

Default destination:

```text
Trace-Full-Game.video-only.mp4 (beside the script)
```

The extractor maps the first video stream and explicitly disables audio at both
the per-part and final join steps. It accepts a mix of silent and audio-bearing
parts.

After a successful join it also writes a sibling sidecar:

```text
Trace-Full-Game.video-only.sidecar.json
```

That file has schema `trace-source-sidecar-v1`. When you pass exactly two parts,
`periods` is first half `0…duration(part1)` and second half `duration(part1)…sum`.
One part does not invent halves. Sources are filenames only (no signed URLs).
Roster, kit, and attacking end are not in this file.

To choose another destination:

```bash
python3 trace_game_extractor.py half-1.m3u8 half-2.m3u8 \
  --output "/path/to/game.video-only.mp4"
```

Audio is opt-in and remains best effort. A missing audio stream never blocks the
extraction:

```bash
python3 trace_game_extractor.py half-1.m3u8 half-2.m3u8 --with-audio
```

This writes `Trace-Full-Game.with-audio.mp4` unless `--output` is supplied.

## Alternate views (5 September 2026)

Keep the full game and import an existing PlayerCam or MultiCam manifest/MP4 beside
it. Each extra becomes a separate video and sidecar; existing games are preserved.

```bash
python3 trace_game_extractor.py --game example1-12345678 \
  --extra-view playercam captured-playercam.m3u8 \
  --extra-view multicam phone-clip.mp4
```

`--extra-view VIEW SOURCE` is repeatable. A default full-game output named
`Trace-…video-only.mp4` receives siblings such as
`Trace-…video-only.playercam-01.mp4`. Supply only views you can already access;
this command does not generate paid highlights or guess private endpoints.

Import just a close-up without downloading a full match again:

```bash
python3 trace_game_extractor.py captured-playercam.m3u8 --view playercam \
  --output playercam-example.mp4
```

If its full-game start time is independently known, add
`--source-start-seconds 120.5`. This accepts one contiguous input only. Otherwise
timeline alignment stays explicitly **unknown**. A camera label is declared, not
visually verified; a close-up does not prove correct player identity or extra
native detail. Two alternate clips never create fictitious first/second halves.

Inspect the game's advertised metadata without fetching media:

```bash
python3 trace_game_extractor.py --game example1-12345678 --list-views
```

Inspection follows the public frontend's nested `event.sources`, `meta.camera`
and `dynamic.hls` fields (`Superfly` is displayed as PlayerCam). It prints counts,
not signed URLs. No advertised alternate source is not proof none exists: use
an explicitly captured manifest or downloaded clip when available. Automatic
alternate-stream URL resolution is not implemented; `--game --view playercam`
fails clearly instead of downloading the wide view under a misleading name.

Both MP4 and HLS inputs are stream-copied. Explicit HLS masters select the largest
declared rendition. No neural enhancement, invented detail, or new dependency.

Publication is atomic **per video**, not across the set: if a later extra fails,
earlier completed outputs remain. Existing outputs and sidecars are checked before
starting, and no output may replace any primary or alternate input.

## Safety and credentials

- Existing destinations are preserved unless `--overwrite` is explicit.
- Work happens in a temporary directory beside the destination; the validated MP4
  is moved into place only after all parts finish.
- Input URLs are hidden from progress and command-error output. Prefer captured
  local manifests because Trace URLs can contain expiring credentials.
- Do not commit, paste, or share captured manifests containing signed URLs.
- The sidecar never stores hosts, queries, or credentials.

## Test

Tests generate tiny synthetic clips in a temporary directory. They do not run a
Trace download or touch `Trace-Full-Game.mp4`.

```bash
cd trace-game-extractor
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_trace_game_extractor.py
```

## Current boundaries

- No Trace login automation, credential storage, radar/analytics ingestion, or
  camera calibration is implemented in this snapshot.
- View labels and supplied start offsets are declarations, not verified identity,
  native resolution, timing, or field geometry. PlayerCam can follow the wrong player.
- Public game metadata may omit alternate views available in the current viewer.
- Original HLS/UTC timestamps are not preserved by the remux as a certified mapping;
  metadata joins need separately verified, half-aware alignment.
- The root `.gitignore` is an allowlist. Only the extractor, tests, README, and
  ignore file are tracked. Add new source files deliberately; keep real media,
  captured manifests, private metadata, browser sessions, and credentials out of Git.
- Tests use synthetic media and mocked metadata. Passing tests is not real-game
  acceptance. No hosted workflow or remote compute is configured here.
