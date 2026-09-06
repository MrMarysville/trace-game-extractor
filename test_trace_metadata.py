"""Synthetic, offline regression tests; no Trace account or real footage used."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import trace_game_extractor as extractor
import trace_metadata as metadata


GAME = {
    "game_id": "12345678", "hls_folders": ["h1/master.m3u8", "h2/master.m3u8"],
    "all_events": [{"time": 12.5, "utc_time": 1700000012500, "meta": {"gids": ["home_7"]}}],
}
RADAR = {
    "setup": {"version": "2-gid", "fps": 2, "field_aspect": 1.65,
              "utc_start_time": [2026, 1, 1, 0, 0, 0, 0],
              "athletes": {"a": {"id": "a", "team": "home"}, "b": {"id": "b", "team": "unknown"}}},
    "frm": [{"t": 50, "a": {"a": {"l": [200, 300], "h": [0, 0]}}},
            {"t": 100, "a": {"a": {"l": [1010, 300]}, "b": {"l": None}}},
            {"t": 150, "a": {}}],
}
VIEWER = {"data": {"game": {
    "game_id": 12345678,
    "moments": [{"half": 1, "start_time": 1700000000000, "dynamic_start": 0,
                 "sources": [{"camera": {"type": "Superfly", "name": "sf_home_7"},
                              "base_path": "https://example.test/game/",
                              "dynamic_hls": "view/master.m3u8?token=fixture-secret"}]}],
}}}
ANALYTICS = {"data": {"gameStats": {
    "available": "full", "player_touches": {"items": [
        {"start": 1700000012500, "x": 200, "y": 300, "global_id": "unverified-player"}
    ]},
}}}


class MetadataDocumentTests(unittest.TestCase):
    def test_game_events_preserve_original_clock_and_ids(self):
        document = metadata.make_document("game", GAME)
        self.assertEqual(document["data"]["all_events"], GAME["all_events"])
        self.assertEqual(document["summary"]["event_records"], 1)
        self.assertEqual(document["association"], "declared_unverified")

    def test_viewer_response_unwraps_and_preserves_camera_timing(self):
        document = metadata.make_document("viewer", VIEWER)
        self.assertEqual(document["advertised_camera_manifest_counts"], {"PlayerCam": 1})
        self.assertEqual(document["data"]["moments"][0]["start_time"], 1700000000000)
        self.assertNotIn("fixture-secret", json.dumps(document))
        self.assertEqual(VIEWER["data"]["game"]["moments"][0]["sources"][0]["dynamic_hls"],
                         "view/master.m3u8?token=fixture-secret")  # No mutation of input.

    def test_inventory_counts_distinct_base_paths_and_old_schema(self):
        old = {"meta": {"camera": {"type": "Superfly"}}, "dynamic": {"hls": "old.m3u8"}}
        current = [{"camera": {"type": "Trace"}, "base_path": base, "dynamic_hls": "master.m3u8"}
                   for base in ["one/", "two/"]]
        result = metadata.camera_inventory({"events": [old, old], "data": {"game": {"sources": current}}})
        self.assertEqual(result, {"PlayerCam": 1, "Trace": 2})

    def test_radar_profiles_tracks_not_people_and_does_not_drop_bad_positions(self):
        document = metadata.make_document("radar-h2", RADAR)
        summary = document["summary"]
        self.assertEqual(document["half"], 2)
        self.assertEqual(document["data"], RADAR)
        self.assertEqual(summary["frame_count"], 3)
        self.assertEqual(summary["empty_frames"], 1)
        self.assertEqual(summary["position_rows"], 3)
        self.assertEqual(summary["finite_xy_rows"], 2)
        self.assertEqual(summary["outside_nominal_field_rows"], 1)
        self.assertEqual(summary["observed_track_keys"], 2)
        self.assertEqual(summary["off_grid_steps"], 0)
        self.assertEqual(summary["first_t"], 50)
        self.assertEqual(summary["coverage_status"], "unverified_against_video")

    def test_radar_reports_duplicates_disorder_and_orphans_without_repairing(self):
        radar = copy.deepcopy(RADAR)
        radar["frm"][1]["t"] = 50
        radar["frm"][2] = {"t": 0, "a": {"missing": {"l": [0, 0]}}}
        summary = metadata.make_document("radar-h1", radar)["summary"]
        self.assertEqual(summary["duplicate_timestamps"], 1)
        self.assertEqual(summary["non_increasing_steps"], 2)
        self.assertEqual(summary["off_grid_steps"], 2)
        self.assertEqual(summary["orphan_track_refs"], 1)

    def test_empty_radar_is_not_full_coverage(self):
        radar = copy.deepcopy(RADAR)
        radar["frm"] = []
        summary = metadata.make_document("radar-h1", radar)["summary"]
        self.assertEqual(summary["frame_count"], 0)
        self.assertIsNone(summary["first_t"])
        self.assertEqual(summary["coverage_status"], "unverified_against_video")

    def test_radar_rejects_unsupported_or_malformed_schema(self):
        for setup_change in [{"fps": 0}, {"fps": True}, {"version": "unknown"}, {"athletes": []}]:
            with self.subTest(setup_change=setup_change):
                radar = copy.deepcopy(RADAR)
                radar["setup"].update(setup_change)
                with self.assertRaises(metadata.MetadataError):
                    metadata.make_document("radar-h1", radar)
        for frame in [{"t": True, "a": {}}, {"t": 50, "a": []}, {}]:
            radar = copy.deepcopy(RADAR)
            radar["frm"] = [frame]
            with self.assertRaises(metadata.MetadataError):
                metadata.make_document("radar-h1", radar)

    def test_analytics_are_kept_as_unverified_claims(self):
        document = metadata.make_document("analytics", ANALYTICS)
        self.assertEqual(document["summary"]["event_records_by_group"], {"player_touches": 1})
        self.assertEqual(document["data"]["player_touches"]["items"][0]["global_id"], "unverified-player")

    def test_empty_heatmap_is_preserved_without_invented_values(self):
        document = metadata.make_document("heatmap", {"data": {"playerGameHeatMap": {"map": []}}})
        self.assertEqual(document["summary"], {"rows": 0, "orientation": "unverified"})

    def test_failed_graphql_response_and_request_log_are_rejected(self):
        for data in [{"data": {"game": None}}, {"data": VIEWER["data"], "errors": [{"message": "failure"}]},
                     {"headers": {"Authorization": "Bearer fixture-secret"}, "responseBody": VIEWER}]:
            with self.assertRaises(metadata.MetadataError):
                metadata.make_document("viewer", data)

    def test_credential_fields_and_url_queries_are_removed_recursively(self):
        data = copy.deepcopy(GAME)
        data["extra"] = {"Authorization": "Bearer fixture-a", "access_token": "fixture-b",
                         "links": ["https://user:fixture-c@example.test/view.json?token=fixture-d#fixture-e",
                                   "child.m3u8?Signature=fixture-f"], "password": "fixture-g",
                         "csrfToken": "fixture-h", "auth_token": "fixture-i",
                         "caption": "Source: HTTPS://user:fixture-j@example.test/source?key=fixture-k",
                         "warning": "Authorization: fixture-l"}
        document = metadata.make_document("game", data)
        self.assertNotIn("fixture-", json.dumps(document))
        self.assertEqual(document["data"]["extra"]["links"], ["https://example.test/view.json", "child.m3u8"])
        self.assertEqual(document["data"]["extra"]["caption"], "Source: https://example.test/source")
        digest = hashlib.sha256(json.dumps(document["data"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(document["data_sha256"], digest)

    def test_no_nonfinite_json_can_enter_a_bundle(self):
        for value in [float("nan"), float("inf"), float("-inf")]:
            data = copy.deepcopy(GAME)
            data["extra"] = value
            with self.assertRaises(metadata.MetadataError):
                metadata.make_document("game", data)

    def test_mismatched_game_ids_fail_closed(self):
        with self.assertRaises(metadata.MetadataError):
            metadata.build_bundle([], game_ref="example1-87654321", game_data=GAME)

    def test_duplicate_or_unknown_source_kinds_do_not_fetch(self):
        for sources in [[("radar-h1", "one"), ("radar-h1", "two")], [("invalid", "one")]]:
            with mock.patch.object(metadata, "read_json_source") as read:
                with self.assertRaises(metadata.MetadataError):
                    metadata.build_bundle(sources)
                read.assert_not_called()


class MetadataIOTests(unittest.TestCase):
    def test_https_read_is_bounded_and_redirect_remains_https(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = "https://example.test/radar.json"
        response.read.return_value = json.dumps(RADAR).encode()
        with mock.patch.object(metadata.urllib.request, "build_opener") as factory:
            factory.return_value.open.return_value = response
            self.assertEqual(metadata.read_json_source("https://example.test/radar.json"), RADAR)
        response.read.assert_called_once_with(metadata.MAX_JSON_BYTES + 1)
        factory.return_value.open.assert_called_once_with("https://example.test/radar.json", timeout=30)
        response.geturl.return_value = "http://example.test/radar.json"
        with mock.patch.object(metadata.urllib.request, "build_opener") as factory:
            factory.return_value.open.return_value = response
            with self.assertRaises(metadata.MetadataError):
                metadata.read_json_source("https://example.test/radar.json")

    def test_unsafe_redirect_is_blocked_before_request_is_created(self):
        request = metadata.urllib.request.Request("https://example.test/source.json")
        handler = metadata._HTTPSOnlyRedirect()
        for target in ["http://example.test/unsafe", "https://user:fixture@example.test/unsafe", "file:///tmp/unsafe"]:
            with self.subTest(target=target), mock.patch.object(metadata.urllib.request.HTTPRedirectHandler, "redirect_request") as follow:
                with self.assertRaises(metadata.MetadataError):
                    handler.redirect_request(request, None, 302, "Found", {}, target)
                follow.assert_not_called()
        redirected = handler.redirect_request(request, None, 302, "Found", {}, "https://example.test/safe.json")
        self.assertEqual(redirected.full_url, "https://example.test/safe.json")

    def test_unauthorized_request_does_not_retry_or_leak_url(self):
        url = "https://example.test/missing.json?token=fixture-secret"
        error = urllib.error.HTTPError(url, 403, "fixture-secret", None, None)
        with mock.patch.object(metadata.urllib.request, "build_opener") as factory:
            factory.return_value.open.side_effect = error
            with self.assertRaises(metadata.MetadataError) as caught:
                metadata.read_json_source(url)
        self.assertIn("403", str(caught.exception))
        self.assertNotIn("fixture-secret", str(caught.exception))
        factory.return_value.open.assert_called_once()

    def test_rejects_http_userinfo_unsupported_scheme_and_missing_file(self):
        for source in ["http://example.test/a.json", "https://user:pass@example.test/a.json",
                       "ftp://example.test/a.json", "/missing-file-for-test.json"]:
            with mock.patch.object(metadata.urllib.request, "build_opener") as fetch:
                with self.assertRaises(metadata.MetadataError):
                    metadata.read_json_source(source)
                fetch.assert_not_called()

    def test_local_document_size_and_json_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            for content in ["[]", "<html>not JSON</html>", '{"bad": NaN}']:
                path.write_text(content)
                with self.assertRaises(metadata.MetadataError):
                    metadata.read_json_source(str(path))
            path.write_text(json.dumps(GAME))
            with mock.patch.object(metadata, "MAX_JSON_BYTES", 8):
                with self.assertRaises(metadata.MetadataError):
                    metadata.read_json_source(str(path))

    def test_atomic_private_file_and_explicit_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new" / "metadata.json"
            bundle = metadata.build_bundle([], game_data=GAME)
            metadata.write_bundle(path, bundle)
            self.assertEqual(json.loads(path.read_text()), bundle)
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(metadata.MetadataError):
                metadata.write_bundle(path, {"new": True})
            self.assertEqual(json.loads(path.read_text()), bundle)
            metadata.write_bundle(path, {"new": True}, overwrite=True)
            self.assertEqual(json.loads(path.read_text()), {"new": True})
            self.assertEqual(list(path.parent.glob(".trace-metadata-*")), [])

    def test_destination_race_preserves_other_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metadata.json"
            link = os.link
            def race(source, destination):
                destination.write_text("other writer")
                return link(source, destination)
            with mock.patch.object(metadata.os, "link", side_effect=race):
                with self.assertRaises(metadata.MetadataError):
                    metadata.write_bundle(path, {"new": True})
            self.assertEqual(path.read_text(), "other writer")
            self.assertEqual(list(path.parent.glob(".trace-metadata-*")), [])


class MetadataCLITests(unittest.TestCase):
    def call_main(self, args):
        self.output = io.StringIO()
        with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output):
            return extractor.main(args)

    def test_metadata_only_collects_game_and_radar_without_video_or_ffmpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            radar = root / "radar.json"
            radar.write_text(json.dumps(RADAR))
            output = root / "bundle.json"
            with mock.patch.object(extractor, "read_json_source", return_value=GAME) as fetch, \
                 mock.patch.object(extractor, "extract_game") as extract, \
                 mock.patch.object(extractor, "resolve_game_sources") as resolve:
                result = self.call_main(["--game", "example1-12345678", "--metadata-only",
                                         "--metadata-source", "radar-h1", str(radar),
                                         "--metadata-output", str(output), "--ffmpeg", "missing-ffmpeg"])
            self.assertEqual(result, 0, self.output.getvalue())
            extract.assert_not_called()
            resolve.assert_not_called()
            fetch.assert_called_once()
            bundle = json.loads(output.read_text())
            self.assertEqual([d["kind"] for d in bundle["documents"]], ["game", "radar-h1"])
            self.assertEqual(bundle["trust"]["timeline_alignment"], "unverified")
            self.assertNotIn("export", bundle)

    def test_offline_viewer_and_analytics_capture_has_no_network_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            viewer, stats = root / "viewer.json", root / "stats.json"
            viewer.write_text(json.dumps(VIEWER))
            stats.write_text(json.dumps(ANALYTICS))
            with mock.patch.object(metadata.urllib.request, "build_opener") as fetch:
                result = self.call_main(["--metadata-only", "--metadata-source", "viewer", str(viewer),
                                        "--metadata-source", "analytics", str(stats),
                                        "--metadata-output", str(root / "bundle.json")])
            self.assertEqual(result, 0, self.output.getvalue())
            fetch.assert_not_called()

    def test_existing_destination_rejected_before_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "metadata.json"
            output.write_text("preserve")
            with mock.patch.object(extractor, "read_json_source") as fetch:
                result = self.call_main(["--game", "example1-12345678", "--metadata-only", "--metadata-output", str(output)])
            self.assertEqual(result, 1)
            fetch.assert_not_called()
            self.assertEqual(output.read_text(), "preserve")

    def test_metadata_input_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game.json"
            before = json.dumps(GAME)
            path.write_text(before)
            result = self.call_main(["--metadata-only", "--metadata-source", "game", str(path),
                                    "--metadata-output", str(path), "--overwrite"])
            self.assertEqual(result, 1)
            self.assertEqual(path.read_text(), before)

    def test_video_sidecar_cannot_be_metadata_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "clip.mp4"
            video.touch()
            with mock.patch.object(extractor, "extract_game") as extract:
                result = self.call_main([str(video), "--output", str(root / "output.mp4"), "--metadata",
                                        "--metadata-output", str(root / "output.sidecar.json")])
            self.assertEqual(result, 1)
            extract.assert_not_called()

    def test_invalid_or_ignored_metadata_options_fail_clearly(self):
        options = [["--metadata-source", "game", "file.json"], ["--metadata-output", "file.json"],
                   ["--metadata-only", "--output", "video.mp4"], ["--metadata-only", "clip.mp4"],
                   ["--metadata-only", "--with-audio"], ["--metadata-only"],
                   ["--metadata-only", "--game", "example1-12345678", "--list-views"],
                   ["--metadata-only", "--game", "example1-12345678", "--metadata-source", "invalid", "file.json"],
                   ["--metadata-only", "--game", "example1-12345678", "--metadata-source", "game", "file.json"]]
        for args in options:
            with self.subTest(args=args), mock.patch.object(metadata.urllib.request, "build_opener") as fetch:
                self.assertEqual(self.call_main(args), 1, self.output.getvalue())
                fetch.assert_not_called()

    def test_failed_metadata_capture_never_starts_video(self):
        with mock.patch.object(extractor, "read_json_source", side_effect=metadata.MetadataError("Metadata GET failed (HTTP 403).")), \
             mock.patch.object(extractor, "extract_game") as extract:
            self.assertEqual(self.call_main(["--game", "example1-12345678", "--metadata"]), 1)
        extract.assert_not_called()

    def test_with_metadata_reuses_game_record_and_binds_primary_video_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "video.mp4"
            def fake_extract(sources, destination, **kwargs):
                destination.write_bytes(b"synthetic-video-for-hash")
                extractor.sidecar_path(destination).write_text(json.dumps({"schema": "fixture", "periods": []}))
            def read(source):
                return GAME if source.startswith("https://") else metadata.read_json_source(source)
            with mock.patch.object(extractor, "read_json_source", side_effect=read), \
                 mock.patch.object(extractor, "resolve_game_sources", return_value=["https://example.test/half.m3u8"]) as resolve, \
                 mock.patch.object(extractor, "extract_game", side_effect=fake_extract):
                result = self.call_main(["--game", "example1-12345678", "--metadata", "--output", str(output)])
            self.assertEqual(result, 0, self.output.getvalue())
            resolve.assert_called_once_with("example1-12345678", metadata=GAME)
            bundle = json.loads(output.with_suffix(".metadata.json").read_text())
            self.assertEqual(bundle["export"]["video_sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
            self.assertEqual(bundle["export"]["timeline_alignment"], "unverified")


if __name__ == "__main__":
    unittest.main()
