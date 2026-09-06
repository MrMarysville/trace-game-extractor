# Trace game extractor

Local FFmpeg wrapper for joining captured Trace game parts. Sound is not required:
the default output contains video only, even when an input contains audio.
Optionally capture Trace JSON as private source evidence alongside the video, or
without downloading video at all. Metadata is not certified tracking or calibration.

This repository contains the standalone extractor and synthetic tests only.
Original footage and existing extraction outputs remain outside this repository.

## Requirements

- Python 3.10 or newer
- `ffmpeg` and `ffprobe` on `PATH` for video operations (not metadata-only mode)

Python code uses the standard library only; no `pip install` is required.

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

Inspection supports nested legacy `meta.camera` / `dynamic.hls` and current viewer
`camera` / `dynamic_hls` fields (`Superfly` is displayed as PlayerCam). It prints counts,
not signed URLs. No advertised alternate source is not proof none exists: use
an explicitly captured manifest or downloaded clip when available. Automatic
alternate-stream URL resolution is not implemented; `--game --view playercam`
fails clearly instead of downloading the wide view under a misleading name.

Both MP4 and HLS inputs are stream-copied. Explicit HLS masters select the largest
declared rendition. No neural enhancement, invented detail, or new dependency.

Publication is atomic **per video**, not across the set: if a later extra fails,
earlier completed outputs remain. Existing outputs and sidecars are checked before
starting, and no output may replace any primary or alternate input.

## Metadata capture

Download the video and save its public game metadata:

```bash
python3 trace_game_extractor.py --game example1-12345678 --metadata
```

This adds `<video-stem>.metadata.json`, separate from the existing duration
sidecar. The bundle includes a SHA-256 of the primary MP4 and a copy of its
measured sidecar. That binds a snapshot to a file, **not** to a verified timeline.
The video path is unchanged when metadata flags are absent.

Already have the video? Capture JSON only, adding explicitly supplied radar files:

```bash
python3 trace_game_extractor.py --game example1-12345678 --metadata-only \
  --metadata-source radar-h1 /path/to/h1-radar.json \
  --metadata-source radar-h2 /path/to/h2-radar.json \
  --metadata-output /path/to/game.metadata.json
```

`--metadata-only` never resolves video manifests or calls FFmpeg. With `--game`,
it fetches the public `game.json` once. Without `--game`, it reads only the supplied
sources; all-local sources work offline:

```bash
python3 trace_game_extractor.py --metadata-only \
  --metadata-source viewer /path/to/game-response.json \
  --metadata-source analytics /path/to/stats-response.json \
  --metadata-source heatmap /path/to/heatmap-response.json \
  --metadata-output /path/to/game.metadata.json
```

Supply JSON **response bodies**, not HAR files, request logs, or browser sessions.
Each `--metadata-source KIND SOURCE` accepts a local file or an authorized HTTPS
URL, with a 32 MiB limit per document. It never follows links inside JSON, guesses
endpoints, signs in, supplies authorization headers, or retries an access failure.
HTTP URLs and redirects to HTTP or userinfo-bearing URLs are rejected.

Supported kinds:

| Kind | Expected response body |
| --- | --- |
| `game` | Public object with `all_events` and `hls_folders` arrays |
| `viewer` | `data.game`, or its unwrapped object with `game_id` and `moments` |
| `radar-h1`, `radar-h2` | `setup.version: "2-gid"`, positive `fps`, `athletes`, and `frm` |
| `analytics` | `data.gameStats`, or its unwrapped object with `available` and event `items` groups |
| `heatmap` | `data.playerGameHeatMap`, or its unwrapped object with a `map` array |

Game, viewer, and each half's radar may occur once; analytics and heatmaps are
repeatable. Do not supply `game` alongside `--game`, which already captures it.
Declared game-ID conflicts fail; missing IDs and assigned radar halves remain
unverified. Radar and authenticated viewer/analytics sources are **not discovered
automatically**. Supply accessible files or links yourself.

The `trace-metadata-bundle-v1` format preserves sanitized source data, original
timestamps, per-document hashes, and diagnostic summaries. Hashes cover stored,
sanitized canonical JSON, not original HTTP bytes. Radar summaries count track
keys, finite positions, off-field rows, empty frames, missing references, and
irregular timestamps. Track keys are not a count of real players. Positions are
not clipped or silently dropped; timestamps are not repaired or aligned.

For the supported radar schema, coordinates are normalized field values
(`0…1000`), not metres, and `t` uses centiseconds. Half clocks stay separate.
No conversion from radar, UTC events, heatmaps, or camera timing fields to exported
video time is certified. Identity, calibration, video coverage, orientation, and
timeline alignment remain unverified; PlayerCam can follow the wrong player.

Without `--metadata-output`, metadata-only saves `Trace-<team-gameid>.metadata.json`
beside the script, or `Trace.metadata.json` without a game reference. It rejects
video options such as `--output`; choose `--metadata-output` instead.

Metadata is captured and validated before video extraction. Its output is published
atomically after the primary video, before optional extra clips. Publication is
**per file**, not a transaction across video, sidecar, and bundle: if metadata
publication fails, a completed video remains. Metadata-only does not hash or bind
an existing video. Existing metadata is preserved unless `--overwrite` is explicit;
metadata may never overwrite an input or video sidecar. Atomic no-overwrite
publication requires a filesystem supporting hard links.

## Safety and credentials

- Existing destinations are preserved unless `--overwrite` is explicit.
- Work happens in a temporary directory beside the destination; the validated MP4
  is moved into place only after all parts finish.
- Input URLs are hidden from progress and command-error output. Prefer captured
  local manifests because Trace URLs can contain expiring credentials.
- Do not commit, paste, or share captured manifests containing signed URLs.
- The sidecar never stores hosts, queries, or credentials.
- Metadata capture removes known credential fields and URL queries, fragments,
  and userinfo. This is a precaution, **not a guarantee that arbitrary source
  text is secret-free**. Review imported files and keep them private.
- Metadata still contains private match/player information. Files are created
  owner-only (`0600`) on POSIX filesystems; Windows access follows local ACLs.
  Neither metadata nor media belongs in Git, even in this private repository.

## Test

Tests generate tiny synthetic clips in a temporary directory. They do not run a
Trace download or touch `Trace-Full-Game.mp4`.

```bash
cd trace-game-extractor
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v
python3 -m py_compile trace_game_extractor.py trace_metadata.py test_trace_game_extractor.py test_trace_metadata.py
```

Optional lint, when Ruff is installed:

```bash
python3 -m ruff check trace_game_extractor.py trace_metadata.py test_trace_game_extractor.py test_trace_metadata.py
```

Tests cover synthetic FFmpeg extraction plus metadata capture, offline imports,
schema and game-ID checks, credential redaction, safe redirects, output collisions,
atomic metadata publication, and unchanged default extraction. No static type
checker or hosted CI is configured.

## Current boundaries

- No Trace login automation, credential storage, automatic radar/analytics
  discovery, camera calibration, or analytics-app integration is implemented.
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
