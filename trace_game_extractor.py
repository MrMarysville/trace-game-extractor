#!/usr/bin/env python3
"""Join captured Trace game parts without requiring an audio stream."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Sequence
from urllib.parse import urljoin, urlparse


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_VIDEO_ONLY_OUTPUT = SCRIPT_DIR / "Trace-Full-Game.video-only.mp4"
DEFAULT_WITH_AUDIO_OUTPUT = SCRIPT_DIR / "Trace-Full-Game.with-audio.mp4"
HLS_PROTOCOLS = "file,http,https,tcp,tls,crypto,data"
SIDECAR_SCHEMA = "trace-source-sidecar-v1"
VIEWS = ("tracecam", "playercam", "multicam")


class ExtractorError(RuntimeError):
    """Raised when an extraction command or output validation fails."""


def _is_hls_source(source: str) -> bool:
    return urlparse(source).path.lower().endswith((".m3u8", ".m3u"))


def sidecar_path(output: Path) -> Path:
    """Sibling JSON next to the published MP4: name.mp4 -> name.sidecar.json."""
    return output.with_name(f"{output.stem}.sidecar.json")


def source_basename(source: str) -> str:
    """Filename only. Never keep a host, query, or signed URL."""
    parsed = urlparse(source)
    name = Path(parsed.path).name if parsed.scheme else Path(source).name
    return name or "part"


TRACE_API_BASE = "https://go.traceup.com/api/teams"
GAME_REF_PATTERN = re.compile(r"\b([a-z0-9]{6,12})-(\d{6,12})\b")


def parse_game_ref(value: str) -> str:
    """Accept `team-gameid` (e.g. example1-12345678) or any Trace URL containing it."""
    match = GAME_REF_PATTERN.search(value)
    if not match:
        raise ExtractorError(
            "Could not find a team-gameid reference (like example1-12345678) in --game. "
            "Copy it from the game's poster/API URL."
        )
    return f"{match.group(1)}-{match.group(2)}"


def _fetch_text(url: str, *, timeout: float = 30.0) -> str:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise ExtractorError(f"Fetch failed: {_redact_error(str(error))}") from error


def _pick_top_variant(master_text: str) -> str:
    """Prefer encoded pixel count, then bandwidth; never upscale the source."""
    best_uri, best_rank, pending = None, (-1, -1), None
    for line in master_text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            match = re.search(r"(?:[:,])BANDWIDTH=(\d+)", line)
            size = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
            pixels = int(size.group(1)) * int(size.group(2)) if size else 0
            pending = (pixels, int(match.group(1)) if match else 0)
        elif line and not line.startswith("#") and pending is not None:
            if pending > best_rank:
                best_uri, best_rank = line, pending
            pending = None
    if not best_uri:
        raise ExtractorError("Master playlist listed no variants.")
    return best_uri


def resolve_captured_source(source: str) -> str:
    """Select the best rendition of an explicitly supplied master, if present."""
    source = _local_source(source)
    if not _is_hls_source(source):
        return source
    if urlparse(source).scheme in {"http", "https"}:
        text = _fetch_text(source)
    else:
        try:
            text = Path(source).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ExtractorError("Captured manifest could not be read.") from error
    if "#EXT-X-STREAM-INF:" not in text:
        return source
    variant = _pick_top_variant(text)
    if urlparse(source).scheme in {"http", "https"}:
        return urljoin(source, variant)
    if urlparse(variant).scheme in {"http", "https"}:
        return variant
    if variant.startswith("//") or (urlparse(variant).scheme and not _is_windows_drive_path(variant)):
        raise ExtractorError("Local master needs a local variant or explicit HTTP(S) variant URL.")
    return str(Path(source).parent / variant)


def fetch_game_metadata(game_ref: str) -> dict:
    """Read the existing game record; does not request a rendered highlight."""
    team_id = game_ref.split("-", 1)[0]
    game_url = f"{TRACE_API_BASE}/{team_id}/games/{game_ref}/game.json"
    try:
        metadata = json.loads(_fetch_text(game_url))
    except json.JSONDecodeError as error:
        raise ExtractorError("Game metadata was not readable JSON.") from error
    if not isinstance(metadata, dict):
        raise ExtractorError("Game metadata must be a JSON object.")
    return metadata


def inspect_views(metadata: dict) -> dict:
    """Inventory explicit frontend event.sources/meta.camera/dynamic.hls evidence.

    Camera type Superfly is displayed as PlayerCam by Trace's public frontend.
    Unknown camera types are preserved; no synthetic URLs or assumed MultiCam.
    """
    cameras, seen = {}, set()
    def visit(event):
        if not isinstance(event, dict):
            return
        meta, dynamic = event.get("meta"), event.get("dynamic")
        camera = meta.get("camera") if isinstance(meta, dict) else None
        manifest = dynamic.get("hls") if isinstance(dynamic, dict) else None
        if isinstance(camera, dict) and isinstance(manifest, str) and manifest:
            kind = camera.get("type")
            if isinstance(kind, str) and (kind, manifest) not in seen:
                seen.add((kind, manifest))
                label = "PlayerCam" if kind == "Superfly" else kind
                cameras[label] = cameras.get(label, 0) + 1
        sources = event.get("sources", [])
        if isinstance(sources, list):
            for source in sources:
                visit(source)
    for key in ("events", "all_events"):
        events = metadata.get(key, [])
        if isinstance(events, list):
            for event in events:
                visit(event)
    folders = metadata.get("hls_folders", [])
    return {"full_game_parts": len(folders) if isinstance(folders, list) else 0,
            "explicit_camera_manifest_counts": cameras,
            "note": "Only advertised metadata; absence does not prove no other view exists. No video fetched."}


def resolve_game_sources(game_ref: str) -> list[str]:
    """Game ref -> the highest-quality variant manifest URL for each half, in order."""
    metadata = fetch_game_metadata(game_ref)
    team_id = game_ref.split("-", 1)[0]
    folders = metadata.get("hls_folders")
    if not isinstance(folders, list) or not folders or not all(
        isinstance(item, str) and item for item in folders
    ):
        raise ExtractorError("Game metadata listed no video parts.")

    base = f"{TRACE_API_BASE}/{team_id}/games/{game_ref}/"
    sources = []
    for folder in folders:
        master_url = urljoin(base, folder)
        variant = _pick_top_variant(_fetch_text(master_url))
        sources.append(urljoin(master_url, variant))
    print(f"Resolved {len(sources)} part(s) at the highest available quality.")
    return sources


def build_part_command(
    ffmpeg: str,
    source: str,
    output: Path,
    *,
    with_audio: bool,
) -> list[str]:
    """Build the FFmpeg remux command for one captured game part."""
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostats", "-y"]
    if _is_hls_source(source):
        # Captured local manifests can reference HTTPS segments and encryption keys.
        command.extend(
            [
                "-protocol_whitelist",
                HLS_PROTOCOLS,
                "-allowed_extensions",
                "ALL",
            ]
        )

    command.extend(["-fflags", "+genpts", "-i", source, "-map", "0:v:0"])
    if with_audio:
        # The trailing ? keeps audio best-effort: silent games still extract successfully.
        command.extend(["-map", "0:a:0?"])
    else:
        command.append("-an")

    command.extend(
        [
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    return command


def build_concat_command(
    ffmpeg: str,
    concat_file: Path,
    output: Path,
    *,
    with_audio: bool,
) -> list[str]:
    """Build the FFmpeg command that joins normalized game parts."""
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-map",
        "0:v:0",
    ]
    if with_audio:
        command.extend(["-map", "0:a:0?"])
    else:
        command.append("-an")

    command.extend(["-c", "copy", "-movflags", "+faststart", str(output)])
    return command


def _redact_error(message: str) -> str:
    message = re.sub(r"https?://[^\s'\"]+", "<redacted-url>", message)
    return re.sub(
        r"(?i)\b(authorization|cookie|token|signature|policy|key-pair-id)=\S+",
        r"\1=<redacted>",
        message,
    )


def _run(command: Sequence[str], label: str) -> None:
    result = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return

    detail = _redact_error(result.stderr.strip())
    suffix = f"\n{detail}" if detail else ""
    raise ExtractorError(f"{label} failed (exit {result.returncode}).{suffix}")


def _probe_stream_types(ffprobe: str, media: Path) -> list[str]:
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "json",
            str(media),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = _redact_error(result.stderr.strip())
        suffix = f"\n{detail}" if detail else ""
        raise ExtractorError(f"Output validation failed.{suffix}")

    try:
        payload = json.loads(result.stdout)
        return [stream["codec_type"] for stream in payload.get("streams", [])]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise ExtractorError("Output validation returned unreadable stream data.") from error


def _probe_duration_seconds(ffprobe: str, media: Path) -> float:
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(media),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = _redact_error(result.stderr.strip())
        suffix = f"\n{detail}" if detail else ""
        raise ExtractorError(f"Duration probe failed.{suffix}")

    try:
        duration = float(json.loads(result.stdout)["format"]["duration"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise ExtractorError("Duration probe returned unreadable data.") from error
    if not (duration > 0):
        raise ExtractorError("Duration probe returned a non-positive length.")
    return duration


def build_sidecar_payload(
    *,
    output: Path,
    sources: Sequence[str],
    part_durations: Sequence[float],
    with_audio: bool,
    view: str = "tracecam",
    source_start_seconds: float | None = None,
) -> dict:
    """Measured part lengths only. No roster, kit, or signed URLs."""
    if view not in VIEWS:
        raise ExtractorError("Unknown camera view.")
    if source_start_seconds is not None and (
        not 0 <= source_start_seconds < float("inf") or len(part_durations) != 1
    ):
        raise ExtractorError("Source start requires one contiguous input and finite non-negative seconds.")
    payload: dict = {
        "schema": SIDECAR_SCHEMA,
        "source": "extractor",
        "video": output.name,
        "with_audio": with_audio,
        "part_count": len(part_durations),
        "part_durations_seconds": [round(duration, 3) for duration in part_durations],
        "sources": [source_basename(source) for source in sources],
        "view": view,
        "view_provenance": "declared_not_visually_verified",
        "timeline_alignment": (
            {"status": "user_supplied", "source_start_seconds": source_start_seconds}
            if source_start_seconds is not None else {"status": "unknown"}
        ),
    }
    # Two alternate-view clips are not evidence of two playing halves.
    if len(part_durations) == 2 and view == "tracecam":
        first, second = part_durations
        start_second = first
        payload["periods"] = [
            {
                "label": "First half",
                "start_time_seconds": 0.0,
                "end_time_seconds": round(first, 3),
            },
            {
                "label": "Second half",
                "start_time_seconds": round(start_second, 3),
                "end_time_seconds": round(first + second, 3),
            },
        ]
    return payload


def write_sidecar(output: Path, payload: dict) -> Path:
    path = sidecar_path(output)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _is_windows_drive_path(source: str) -> bool:
    return len(source) >= 2 and source[0].isalpha() and source[1] == ":"


def _local_source(source: str) -> str:
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"}:
        return source
    # urlparse("C:\\game.mp4") reports scheme "c". That is a drive, not a URL.
    if parsed.scheme and not _is_windows_drive_path(source):
        raise ExtractorError(f"Unsupported input scheme: {parsed.scheme}")

    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise ExtractorError(f"Input file does not exist: {path}")
    return str(path)


def _ffconcat_line(path: Path) -> str:
    escaped = str(path).replace("'", "'\\''")
    return f"file '{escaped}'\n"


def extract_game(
    sources: Sequence[str],
    output: Path,
    *,
    with_audio: bool = False,
    overwrite: bool = False,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    view: str = "tracecam",
    source_start_seconds: float | None = None,
) -> Path:
    """Remux game parts in order and publish one validated MP4 atomically."""
    if not sources:
        raise ExtractorError("At least one input is required.")
    # Validate metadata before downloading or publishing anything.
    build_sidecar_payload(output=output, sources=sources, part_durations=[1.0] * len(sources),
                          with_audio=with_audio, view=view, source_start_seconds=source_start_seconds)
    if shutil.which(ffmpeg) is None:
        raise ExtractorError(f"FFmpeg executable not found: {ffmpeg}")
    if shutil.which(ffprobe) is None:
        raise ExtractorError(f"FFprobe executable not found: {ffprobe}")

    normalized_sources = [_local_source(source) for source in sources]
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and not overwrite:
        raise ExtractorError(
            f"Output already exists: {output}\n"
            "Choose another --output or pass --overwrite explicitly."
        )

    local_inputs = {
        Path(source).resolve()
        for source in normalized_sources
        if urlparse(source).scheme not in {"http", "https"}
    }
    if output in local_inputs:
        raise ExtractorError("Output cannot replace an input file.")

    with tempfile.TemporaryDirectory(
        prefix=".trace-extractor-", dir=output.parent
    ) as temporary_directory:
        workspace = Path(temporary_directory)
        parts: list[Path] = []
        for index, source in enumerate(normalized_sources, start=1):
            part = workspace / f"part-{index:02d}.mp4"
            print(f"Extracting part {index}/{len(normalized_sources)} (source hidden)...")
            _run(
                build_part_command(
                    ffmpeg, source, part, with_audio=with_audio
                ),
                f"Part {index}",
            )
            parts.append(part)

        part_durations = [_probe_duration_seconds(ffprobe, part) for part in parts]

        candidate = parts[0]
        if len(parts) > 1:
            concat_file = workspace / "concat.txt"
            concat_file.write_text(
                "".join(_ffconcat_line(part) for part in parts), encoding="utf-8"
            )
            candidate = workspace / "joined.mp4"
            print(f"Joining {len(parts)} parts...")
            _run(
                build_concat_command(
                    ffmpeg, concat_file, candidate, with_audio=with_audio
                ),
                "Join",
            )

        stream_types = _probe_stream_types(ffprobe, candidate)
        if "video" not in stream_types:
            raise ExtractorError("Output validation found no video stream.")
        if not with_audio and "audio" in stream_types:
            raise ExtractorError("Video-only output unexpectedly contains audio.")

        if output.exists() and not overwrite:
            raise ExtractorError(
                f"Output appeared during extraction and was preserved: {output}"
            )
        os.replace(candidate, output)
        sidecar = write_sidecar(
            output,
            build_sidecar_payload(
                output=output,
                sources=normalized_sources,
                part_durations=part_durations,
                with_audio=with_audio,
                view=view,
                source_start_seconds=source_start_seconds,
            ),
        )

    print(f"Wrote: {output}")
    print(f"Sidecar: {sidecar}")
    print("Audio: best effort" if with_audio else "Audio: omitted")
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Join captured Trace HLS manifests or media parts. Output is video-only "
            "unless --with-audio is explicitly requested. After a successful join, "
            "writes a sibling .sidecar.json with measured part durations."
        )
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        help="Captured .m3u8 manifest(s) or media part(s), in game order.",
    )
    parser.add_argument(
        "--game",
        help=(
            "Trace game reference (team-gameid, e.g. example1-12345678) or a URL "
            "containing it. Discovers every half and downloads the highest quality."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Destination MP4. Defaults beside this script to "
            "Trace-Full-Game.video-only.mp4."
        ),
    )
    parser.add_argument(
        "--with-audio",
        action="store_true",
        help="Include the first audio stream when present; audio remains optional.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing destination. Without this flag, it is preserved.",
    )
    parser.add_argument("--view", choices=VIEWS, default="tracecam",
                        help="View of explicit inputs; alternate views get separate default filenames.")
    parser.add_argument("--list-views", action="store_true",
                        help="With --game, inspect advertised camera metadata only; no media download.")
    parser.add_argument("--extra-view", nargs=2, action="append", default=[], metavar=("VIEW", "SOURCE"),
                        help="Also import a playercam/multicam manifest or MP4 as a separate clip. Repeatable.")
    parser.add_argument("--source-start-seconds", type=float,
                        help="Known full-game start time of one explicit contiguous input; otherwise alignment stays unknown.")
    parser.add_argument("--ffmpeg", default="ffmpeg", help=argparse.SUPPRESS)
    parser.add_argument("--ffprobe", default="ffprobe", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.game and args.inputs:
            raise ExtractorError("Pass either --game or manifest inputs, not both.")
        if args.game and args.view != "tracecam":
            raise ExtractorError("Game metadata does not establish an alternate view. Supply its captured manifest/MP4 with --view, or use --extra-view alongside --game.")
        if args.game and args.source_start_seconds is not None:
            raise ExtractorError("--source-start-seconds is only for one explicit input, not --game.")
        if any(view not in ("playercam", "multicam") for view, _ in args.extra_view):
            raise ExtractorError("--extra-view accepts playercam or multicam only.")
        if args.list_views:
            if not args.game or args.extra_view or args.output or args.with_audio or args.overwrite:
                raise ExtractorError("--list-views requires --game and cannot be combined with extraction options.")
            print(json.dumps(inspect_views(fetch_game_metadata(parse_game_ref(args.game))), indent=2))
            return 0
        if args.game:
            game_ref = parse_game_ref(args.game)
            sources: Sequence[str] = resolve_game_sources(game_ref)
            default_name = f"Trace-{game_ref}.{'with-audio' if args.with_audio else 'video-only'}.mp4"
            default_output = SCRIPT_DIR / default_name
        else:
            if not args.inputs:
                raise ExtractorError("At least one input (or --game) is required.")
            sources = [resolve_captured_source(source) for source in args.inputs]
            default_output = (
                DEFAULT_WITH_AUDIO_OUTPUT if args.with_audio else DEFAULT_VIDEO_ONLY_OUTPUT
            )
            if args.view != "tracecam":
                default_output = default_output.with_name(f"Trace-{args.view}.{'with-audio' if args.with_audio else 'video-only'}.mp4")
        output = (args.output if args.output is not None else default_output).expanduser().resolve()

        # Each alternate is kept separate: its timebase and identity may differ.
        extras = [(view, resolve_captured_source(source),
                   output.with_name(f"{output.stem}.{view}-{index:02d}.mp4"))
                  for index, (view, source) in enumerate(args.extra_view, 1)]
        original_sources = [_local_source(source) for source in [*args.inputs, *(item[1] for item in args.extra_view)]]
        local_inputs = {Path(source).expanduser().resolve() for source in [*original_sources, *sources, *(item[1] for item in extras)]
                        if urlparse(source).scheme not in {"http", "https"}}
        for destination in [output, *(item[2] for item in extras)]:
            if destination.resolve() in local_inputs or sidecar_path(destination).resolve() in local_inputs:
                raise ExtractorError("No output or sidecar may replace any primary or alternate input.")
            if not args.overwrite and (destination.exists() or sidecar_path(destination).exists()):
                raise ExtractorError(f"Destination or sidecar already exists: {destination}. Choose another --output.")

        extract_game(
            sources,
            output,
            with_audio=args.with_audio,
            overwrite=args.overwrite,
            ffmpeg=args.ffmpeg,
            ffprobe=args.ffprobe,
            view=args.view,
            source_start_seconds=args.source_start_seconds,
        )
        for view, source, destination in extras:
            extract_game([source], destination, with_audio=args.with_audio,
                         overwrite=args.overwrite, ffmpeg=args.ffmpeg, ffprobe=args.ffprobe, view=view)
        if extras:
            print("Alternate clips preserved separately; alignment and player identity require verification.")
    except ExtractorError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled; completed outputs, if any, were preserved.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
