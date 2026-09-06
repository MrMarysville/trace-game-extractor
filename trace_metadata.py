"""Capture explicitly supplied Trace JSON as private, unverified source evidence."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


KINDS = ("game", "viewer", "radar-h1", "radar-h2", "analytics", "heatmap")
MAX_JSON_BYTES = 32 * 1024 * 1024
SECRET_KEYS = {
    "password", "passwd", "authorization", "cookie", "setcookie", "token",
    "accesstoken", "refreshtoken", "idtoken", "apikey", "secret", "clientsecret",
    "signature", "policy", "keypairid", "jwt", "credentials", "headers",
    "requestheaders", "responseheaders", "postdata", "variables",
}


class MetadataError(ValueError):
    """Invalid, unavailable, or unsafe metadata; messages never include inputs."""


class _HTTPSOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise MetadataError("Metadata redirects must remain HTTPS without userinfo.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _number(value: object) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _secret_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in SECRET_KEYS or normalized.endswith(("token", "password", "secret", "apikey"))


def _sanitize_url(value: str) -> str:
    try:
        url = urlsplit(value)
        return urlunsplit((url.scheme, url.netloc.rsplit("@", 1)[-1], url.path, "", ""))
    except ValueError:
        return "<redacted-url>"


def sanitize(value):
    """Remove credential fields and URL query/fragment/userinfo, not player IDs."""
    if isinstance(value, dict):
        return {
            key: sanitize(item) for key, item in value.items()
            if not _secret_key(key)
        }
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?:https?://|//)[^\s<>\"']+", lambda match: _sanitize_url(match[0]), value, flags=re.I)
        # A relative manifest can also contain credentials in its query.
        if not re.search(r"\s", value) and re.search(
            r"\.(?:m3u8?|json|mp4|webp|jpg|png)(?:[?#]|$)", value, re.I
        ):
            return _sanitize_url(value)
        if re.search(r"(?i)\b(?:bearer\s+|(?:token|password|signature|authorization)\s*[:=])", value):
            return "<redacted>"
    return value


def _canonical(data) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def read_json_source(source: str) -> dict:
    """One bounded local read or HTTPS GET; no auth, discovery, or recursive fetch."""
    try:
        parsed = urlsplit(source)
        if parsed.scheme == "https":
            if parsed.username or parsed.password:
                raise MetadataError("Metadata URLs must not contain userinfo credentials.")
            opener = urllib.request.build_opener(_HTTPSOnlyRedirect())
            with opener.open(source, timeout=30) as response:
                if urlsplit(response.geturl()).scheme != "https":
                    raise MetadataError("Metadata redirects must remain HTTPS.")
                raw = response.read(MAX_JSON_BYTES + 1)
        elif parsed.scheme and not (len(parsed.scheme) == 1 and source[1:2] == ":"):
            raise MetadataError("Metadata sources must be local JSON files or HTTPS URLs.")
        else:
            with Path(source).expanduser().open("rb") as stream:
                raw = stream.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise MetadataError("Metadata document exceeds the 32 MiB limit.")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise MetadataError("Metadata must be a JSON object, not a HAR or request log.")
        _canonical(data)  # Reject non-standard NaN/Infinity, including in unused fields.
        return data
    except urllib.error.HTTPError as error:
        raise MetadataError(f"Metadata GET failed (HTTP {error.code}); no fallback attempted.") from None
    except MetadataError:
        raise
    except (OSError, ValueError, RecursionError):
        raise MetadataError("Metadata could not be read as finite, valid JSON.") from None


def camera_inventory(data: dict) -> dict:
    """Support public event metadata and current viewer game/source responses."""
    stack, cameras, seen = [data], {}, set()
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            meta = node.get("meta")
            camera = node.get("camera") or (meta.get("camera") if isinstance(meta, dict) else None)
            dynamic = node.get("dynamic")
            manifest = node.get("dynamic_hls") or (
                dynamic.get("hls") if isinstance(dynamic, dict) else None
            )
            kind = camera.get("type") if isinstance(camera, dict) else None
            if isinstance(kind, str) and isinstance(manifest, str) and manifest:
                # Distinct base paths may use the same relative manifest filename.
                key = (kind, str(node.get("base_path", "")), manifest)
                if key not in seen:
                    seen.add(key)
                    label = "PlayerCam" if kind == "Superfly" else kind
                    cameras[label] = cameras.get(label, 0) + 1
            stack.extend(node.values())
    return cameras


def _radar_summary(data: dict) -> dict:
    setup, frames = data.get("setup"), data.get("frm")
    if not isinstance(setup, dict) or not isinstance(frames, list):
        raise MetadataError("Radar needs setup and frm fields.")
    fps, definitions = setup.get("fps"), setup.get("athletes")
    if setup.get("version") != "2-gid" or not _number(fps) or fps <= 0 or not isinstance(definitions, dict):
        raise MetadataError("Supported radar schema is 2-gid with positive fps and athlete definitions.")
    times, observed = [], set()
    rows = finite = outside = orphans = empty = 0
    for frame in frames:
        if not isinstance(frame, dict) or not _number(frame.get("t")) or not isinstance(frame.get("a"), dict):
            raise MetadataError("Radar frames need finite t and an a object.")
        times.append(frame["t"])
        empty += not frame["a"]
        for key, position in frame["a"].items():
            observed.add(key)
            rows += 1
            orphans += key not in definitions
            xy = position.get("l") if isinstance(position, dict) else None
            if isinstance(xy, list) and len(xy) == 2 and all(_number(v) for v in xy):
                finite += 1
                outside += any(v < 0 or v > 1000 for v in xy)
    return {
        "frame_count": len(frames), "fps": fps, "empty_frames": empty,
        "clock": "trace_centiseconds", "first_t": times[0] if times else None,
        "last_t": times[-1] if times else None,
        "duplicate_timestamps": len(times) - len(set(times)),
        "non_increasing_steps": sum(b <= a for a, b in zip(times, times[1:])),
        "off_grid_steps": sum(not math.isclose(b - a, 100 / fps, abs_tol=1e-6)
                              for a, b in zip(times, times[1:])),
        "track_definitions": len(definitions), "observed_track_keys": len(observed),
        "position_rows": rows, "finite_xy_rows": finite,
        "outside_nominal_field_rows": outside, "orphan_track_refs": orphans,
        "coordinates": "normalized_field_0_1000_not_metres",
        "coverage_status": "unverified_against_video",
    }


def make_document(kind: str, data: dict) -> dict:
    if kind not in KINDS:
        raise MetadataError("Unknown metadata kind; use game, viewer, radar-h1, radar-h2, analytics, or heatmap.")
    if not isinstance(data, dict) or data.get("errors"):
        raise MetadataError("Import a successful JSON response body, not an error or request capture.")
    try:
        _canonical(data)
        wrapper = {"viewer": "game", "analytics": "gameStats", "heatmap": "playerGameHeatMap"}.get(kind)
        if wrapper and "data" in data:
            data = data["data"][wrapper]
        if not isinstance(data, dict):
            raise MetadataError("Metadata response has no usable data object.")
        if kind.startswith("radar-"):
            summary = _radar_summary(data)
        elif kind == "game":
            if not isinstance(data.get("all_events"), list) or not isinstance(data.get("hls_folders"), list):
                raise MetadataError("Game metadata needs all_events and hls_folders arrays.")
            summary = {"event_records": len(data["all_events"]), "advertised_parts": len(data["hls_folders"])}
        elif kind == "viewer":
            if data.get("game_id") is None or not isinstance(data.get("moments"), list):
                raise MetadataError("Viewer metadata needs a game_id and moments array.")
            summary = {"moment_records": len(data["moments"])}
        elif kind == "analytics":
            groups = {key: len(value["items"]) for key, value in data.items()
                      if isinstance(value, dict) and isinstance(value.get("items"), list)}
            if "available" not in data or not groups:
                raise MetadataError("Analytics metadata needs availability and event-item groups.")
            summary = {"available": data["available"], "event_records_by_group": groups,
                       "coordinates": "normalized_field_not_metres_orientation_unverified"}
        else:
            if not isinstance(data.get("map"), list):
                raise MetadataError("Heatmap metadata needs a map array.")
            summary = {"rows": len(data["map"]), "orientation": "unverified"}
        cleaned = sanitize(data)
        return {
            "kind": kind, "half": int(kind[-1]) if kind.startswith("radar-") else None,
            "association": "declared_unverified", "data_sha256": hashlib.sha256(_canonical(cleaned)).hexdigest(),
            "summary": sanitize(summary), "advertised_camera_manifest_counts": camera_inventory(cleaned),
            "data": cleaned,
        }
    except MetadataError:
        raise
    except (KeyError, TypeError, ValueError, RecursionError):
        raise MetadataError("Metadata does not match its declared document kind.") from None


def validate_kinds(kinds: list[str]) -> None:
    if not kinds or any(kind not in KINDS for kind in kinds):
        raise MetadataError("Metadata mode needs --game or explicitly typed --metadata-source inputs.")
    if any(kinds.count(kind) > 1 for kind in ("game", "viewer", "radar-h1", "radar-h2")):
        raise MetadataError("Supply at most one game, viewer, and radar document for each half.")


def build_bundle(sources: list, *, game_ref: str | None = None, game_data: dict | None = None) -> dict:
    validate_kinds([kind for kind, _ in sources] + (["game"] if game_data is not None else []))
    documents = [make_document("game", game_data)] if game_data is not None else []
    documents.extend(make_document(kind, read_json_source(source)) for kind, source in sources)
    ids = set()
    if game_ref:
        ids.add(game_ref.rsplit("-", 1)[-1])
    for document in documents:
        data = document["data"]
        game = data.get("game")
        value = data.get("game_id", game.get("game_id") if isinstance(game, dict) else None)
        if value is not None:
            ids.add(str(value).rsplit("-", 1)[-1])
    if len(ids) > 1:
        raise MetadataError("Metadata documents refer to different games.")
    return {
        "schema": "trace-metadata-bundle-v1", "created_at": datetime.now(timezone.utc).isoformat(),
        "game_ref": game_ref, "documents": documents,
        "trust": {"identity": "unverified_trace_claims", "calibration": "not_certified",
                  "timeline_alignment": "unverified", "video_coverage": "unverified"},
        "redaction": "credential_fields_and_url_queries_removed_not_a_public_export",
    }


def validate_destination(output: Path, *, overwrite: bool, protected=()) -> Path:
    output = output.expanduser().resolve()
    if output.suffix.lower() != ".json":
        raise MetadataError("Metadata output must end in .json.")
    if output in {Path(path).expanduser().resolve() for path in protected}:
        raise MetadataError("Metadata output cannot replace an input, video, or video sidecar.")
    if output.is_dir() or (output.exists() and not overwrite):
        raise MetadataError("Metadata destination already exists; choose another path or use --overwrite.")
    return output


def write_bundle(output: Path, bundle: dict, *, overwrite: bool = False) -> None:
    """Private file, atomic publication, and no-clobber even if a destination races us."""
    output = validate_destination(output, overwrite=overwrite)
    temporary = None
    try:
        encoded = _canonical(bundle)
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".trace-metadata-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, output)
        else:
            os.link(temporary, output)
    except (OSError, ValueError, RecursionError):
        raise MetadataError("Metadata publication failed; existing destination was not truncated.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
