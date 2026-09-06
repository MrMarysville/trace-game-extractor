#!/usr/bin/env python3
"""Regression tests for the local Trace game extractor."""

from __future__ import annotations

import json
import hashlib
import contextlib
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trace_game_extractor import (
    build_concat_command,
    build_part_command,
    build_sidecar_payload,
    extract_game,
    sidecar_path,
    source_basename,
)


class CommandConstructionTests(unittest.TestCase):
    def test_default_part_command_explicitly_omits_audio(self) -> None:
        command = build_part_command(
            "ffmpeg", "half-1.m3u8", Path("part.mp4"), with_audio=False
        )

        self.assertIn("-an", command)
        self.assertIn("0:v:0", command)
        self.assertNotIn("0:a:0?", command)

    def test_audio_mode_keeps_audio_optional(self) -> None:
        command = build_part_command(
            "ffmpeg", "half-1.m3u8", Path("part.mp4"), with_audio=True
        )

        self.assertNotIn("-an", command)
        self.assertIn("0:a:0?", command)

    def test_default_join_explicitly_omits_audio(self) -> None:
        command = build_concat_command(
            "ffmpeg", Path("concat.txt"), Path("joined.mp4"), with_audio=False
        )

        self.assertIn("-an", command)
        self.assertNotIn("0:a:0?", command)


class SidecarContractTests(unittest.TestCase):
    def test_source_basename_strips_signed_urls(self) -> None:
        url = "https://cdn.example/half-1.m3u8?signature=secret&Policy=abc"
        self.assertEqual(source_basename(url), "half-1.m3u8")

    def test_windows_drive_path_is_not_a_url_scheme(self) -> None:
        from trace_game_extractor import ExtractorError, _local_source
        with mock.patch("trace_game_extractor.Path.is_file", return_value=True):
            resolved = _local_source("C:\\Users\\Example\\half-1.m3u8")
        self.assertTrue(resolved.lower().endswith("half-1.m3u8"))
        with self.assertRaises(ExtractorError):
            _local_source("ftp://example/half-1.m3u8")

    def test_two_parts_become_half_periods(self) -> None:
        payload = build_sidecar_payload(
            output=Path("Trace-Full-Game.video-only.mp4"),
            sources=["/tmp/half-1.m3u8", "https://cdn.example/half-2.m3u8?token=x"],
            part_durations=[1800.0, 1740.25],
            with_audio=False,
        )
        self.assertEqual(payload["schema"], "trace-source-sidecar-v1")
        self.assertEqual(payload["source"], "extractor")
        self.assertEqual(payload["sources"], ["half-1.m3u8", "half-2.m3u8"])
        self.assertNotIn("token", json.dumps(payload))
        self.assertEqual(
            payload["periods"],
            [
                {
                    "label": "First half",
                    "start_time_seconds": 0.0,
                    "end_time_seconds": 1800.0,
                },
                {
                    "label": "Second half",
                    "start_time_seconds": 1800.0,
                    "end_time_seconds": 3540.25,
                },
            ],
        )
        self.assertNotIn("roster", payload)
        self.assertNotIn("attacking_goal", json.dumps(payload))

    def test_one_part_does_not_invent_halves(self) -> None:
        payload = build_sidecar_payload(
            output=Path("game.mp4"),
            sources=["only.mp4"],
            part_durations=[90.0],
            with_audio=False,
        )
        self.assertEqual(payload["part_count"], 1)
        self.assertNotIn("periods", payload)


class GameDiscoveryTests(unittest.TestCase):
    def test_pixel_count_precedes_bandwidth_without_upscaling(self) -> None:
        from trace_game_extractor import _pick_top_variant
        master = (
            '#EXT-X-STREAM-INF:BANDWIDTH=9000000,RESOLUTION=1280x720\n720.m3u8\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=4000000,RESOLUTION=1920x1080\n1080.m3u8\n'
        )
        self.assertEqual(_pick_top_variant(master), '1080.m3u8')

    def test_captured_remote_master_selects_best_variant(self) -> None:
        from trace_game_extractor import resolve_captured_source
        with mock.patch('trace_game_extractor._fetch_text', return_value=(
            '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=4000\nbest.m3u8?token=private\n'
        )):
            result = resolve_captured_source('https://example.test/view/master.m3u8')
        self.assertEqual(result, 'https://example.test/view/best.m3u8?token=private')

    def test_captured_media_playlist_stays_unchanged(self) -> None:
        from trace_game_extractor import resolve_captured_source
        url = 'https://example.test/view.m3u8?token=private'
        with mock.patch('trace_game_extractor._fetch_text', return_value='#EXTM3U\n#EXTINF:4\na.ts\n'):
            self.assertEqual(resolve_captured_source(url), url)

    def test_local_master_resolves_relative_to_its_directory(self) -> None:
        from trace_game_extractor import resolve_captured_source
        with tempfile.TemporaryDirectory() as directory:
            master = Path(directory) / 'master.m3u8'
            master.write_text('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=4000\nchild.m3u8\n')
            self.assertEqual(resolve_captured_source(str(master)), str(master.parent / 'child.m3u8'))

    def test_parse_game_ref_accepts_bare_ref_and_urls(self) -> None:
        from trace_game_extractor import ExtractorError, parse_game_ref
        self.assertEqual(parse_game_ref("example1-12345678"), "example1-12345678")
        self.assertEqual(
            parse_game_ref(
                "https://go.traceup.com/api/teams/example1/games/example1-12345678/poster.jpg"
            ),
            "example1-12345678",
        )
        with self.assertRaises(ExtractorError):
            parse_game_ref("no game reference here")

    def test_pick_top_variant_prefers_highest_bandwidth(self) -> None:
        from trace_game_extractor import ExtractorError, _pick_top_variant
        master = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=854x480\n"
            "video_1000k.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080\n"
            "video_3000k.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720\n"
            "video_2000k.m3u8\n"
        )
        self.assertEqual(_pick_top_variant(master), "video_3000k.m3u8")
        with self.assertRaises(ExtractorError):
            _pick_top_variant("#EXTM3U\n")

    def test_resolve_game_sources_walks_folders_and_picks_top_tier(self) -> None:
        from trace_game_extractor import resolve_game_sources
        game_json = json.dumps(
            {"hls_folders": ["gamevideo1.hls/game_video.m3u8", "gamevideo2.hls/game_video.m3u8"]}
        )
        master = (
            "#EXT-X-STREAM-INF:BANDWIDTH=1000000\nvideo_1000k.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=5000000\nvideo_3000k.m3u8\n"
        )
        with mock.patch(
            "trace_game_extractor._fetch_text", side_effect=[game_json, master, master]
        ):
            sources = resolve_game_sources("example1-12345678")
        self.assertEqual(
            sources,
            [
                "https://go.traceup.com/api/teams/example1/games/example1-12345678/gamevideo1.hls/video_3000k.m3u8",
                "https://go.traceup.com/api/teams/example1/games/example1-12345678/gamevideo2.hls/video_3000k.m3u8",
            ],
        )

    def test_resolve_game_sources_rejects_empty_folder_list(self) -> None:
        from trace_game_extractor import ExtractorError, resolve_game_sources
        with mock.patch(
            "trace_game_extractor._fetch_text", return_value=json.dumps({"hls_folders": []})
        ):
            with self.assertRaises(ExtractorError):
                resolve_game_sources("example1-12345678")


class AlternateViewTests(unittest.TestCase):
    def test_alternate_clips_do_not_invent_halves_or_alignment(self) -> None:
        payload = build_sidecar_payload(output=Path('playercam.mp4'), sources=['a.mp4', 'b.mp4'],
                                        part_durations=[12, 8], with_audio=False, view='playercam')
        self.assertNotIn('periods', payload)
        self.assertEqual(payload['view'], 'playercam')
        self.assertEqual(payload['timeline_alignment'], {'status': 'unknown'})

    def test_explicit_alignment_and_invalid_values(self) -> None:
        from trace_game_extractor import ExtractorError
        kwargs = dict(output=Path('clip.mp4'), sources=['a.mp4'], part_durations=[12],
                      with_audio=False, view='multicam')
        payload = build_sidecar_payload(**kwargs, source_start_seconds=120.5)
        self.assertEqual(payload['timeline_alignment']['source_start_seconds'], 120.5)
        self.assertEqual(payload['timeline_alignment']['status'], 'user_supplied')
        for start in [-1, float('nan'), float('inf')]:
            with self.subTest(start=start), self.assertRaises(ExtractorError):
                build_sidecar_payload(**kwargs, source_start_seconds=start)
        kwargs['part_durations'] = [12, 8]
        with self.assertRaises(ExtractorError):
            build_sidecar_payload(**kwargs, source_start_seconds=120)

    def test_inventory_explicit_sources_only_and_never_prints_urls(self) -> None:
        from trace_game_extractor import inspect_views
        source = {'meta': {'camera': {'type': 'Superfly'}},
                  'dynamic': {'hls': 'https://example.test/a.m3u8?token=secret'}}
        result = inspect_views({'hls_folders': ['h1', 'h2'],
                                'events': [{'sources': [source]}], 'all_events': [source]})
        self.assertEqual(result['explicit_camera_manifest_counts'], {'PlayerCam': 1})
        self.assertNotIn('secret', json.dumps(result))
        self.assertEqual(inspect_views({'events': [{'clip': {'hls': 'a.m3u8'}}]})[
            'explicit_camera_manifest_counts'], {})

    def test_list_views_never_downloads_video(self) -> None:
        from trace_game_extractor import main
        with mock.patch('trace_game_extractor.fetch_game_metadata', return_value={'hls_folders': ['a', 'b']}), \
             mock.patch('trace_game_extractor.extract_game') as extract, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--game', 'example1-12345678', '--list-views']), 0)
        extract.assert_not_called()

    def test_game_cannot_fabricate_playercam(self) -> None:
        from trace_game_extractor import main
        with mock.patch('trace_game_extractor.fetch_game_metadata') as fetch, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--game', 'example1-12345678', '--view', 'playercam']), 1)
        fetch.assert_not_called()

    def test_extra_views_are_separate_and_inputs_protected_even_with_overwrite(self) -> None:
        from trace_game_extractor import main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary, extra, output = root / 'wide.mp4', root / 'close.mp4', root / 'game.mp4'
            primary.touch()
            extra.touch()
            with mock.patch('trace_game_extractor.extract_game') as extract, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main([str(primary), '--extra-view', 'playercam', str(extra), '--output', str(output)]), 0)
                self.assertEqual(extract.call_count, 2)
                self.assertEqual(extract.call_args_list[1].args[1], root / 'game.playercam-01.mp4')
                self.assertEqual(extract.call_args_list[1].kwargs['view'], 'playercam')
            with mock.patch('trace_game_extractor.extract_game') as extract, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main([str(primary), '--extra-view', 'playercam', str(extra),
                                       '--output', str(extra), '--overwrite']), 1)
            extract.assert_not_called()

    def test_original_master_cannot_be_replaced_by_output_or_sidecar(self) -> None:
        from trace_game_extractor import main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master, variant = root / 'master.m3u8', root / 'variant.mp4'
            master.touch()
            variant.touch()
            with mock.patch('trace_game_extractor.resolve_captured_source', return_value=str(variant)), \
                 mock.patch('trace_game_extractor.extract_game') as extract, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main([str(master), '--output', str(master), '--overwrite']), 1)
            extract.assert_not_called()

    def test_output_home_alias_cannot_replace_alternate_input(self) -> None:
        from trace_game_extractor import main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary, alternate = root / 'wide.mp4', root / 'extra.mp4'
            primary.touch()
            alternate.touch()
            real_expand = Path.expanduser
            def expand(path):
                return alternate if str(path) == '~/extra.mp4' else real_expand(path)
            with mock.patch.object(Path, 'expanduser', expand), \
                 mock.patch('trace_game_extractor.extract_game') as extract, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main([str(primary), '--extra-view', 'playercam', str(alternate),
                                       '--output', '~/extra.mp4', '--overwrite']), 1)
            extract.assert_not_called()


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg and FFprobe are required for the integration test",
)
class ExtractionIntegrationTests(unittest.TestCase):
    def _make_part(self, path: Path, *, with_audio: bool, duration: str = "0.4") -> None:
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=160x90:rate=10:duration={duration}",
        ]
        if with_audio:
            command.extend(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency=440:sample_rate=44100:duration={duration}",
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c:a",
                    "aac",
                    "-shortest",
                ]
            )
        else:
            command.append("-an")
        command.extend(["-c:v", "mpeg4", "-q:v", "5", "-y", str(path)])
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _stream_types(self, media: Path) -> list[str]:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "json",
                str(media),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return [stream["codec_type"] for stream in json.loads(result.stdout)["streams"]]

    def test_default_joins_mixed_parts_as_video_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            part_with_audio = root / "with-audio.mp4"
            silent_part = root / "silent.mp4"
            output = root / "output.mp4"
            self._make_part(part_with_audio, with_audio=True)
            self._make_part(silent_part, with_audio=False)

            extract_game([str(part_with_audio), str(silent_part)], output)

            self.assertEqual(self._stream_types(output), ["video"])
            sidecar = sidecar_path(output)
            self.assertTrue(sidecar.is_file())
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(payload["part_count"], 2)
            self.assertEqual(len(payload["periods"]), 2)
            self.assertGreater(payload["periods"][0]["end_time_seconds"], 0)
            self.assertEqual(
                payload["periods"][1]["start_time_seconds"],
                payload["periods"][0]["end_time_seconds"],
            )
            self.assertEqual(payload["sources"], ["with-audio.mp4", "silent.mp4"])

    def test_cli_remuxes_alternate_without_changing_primary_input(self) -> None:
        from trace_game_extractor import main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary, close, output = root / 'wide.mp4', root / 'close.mp4', root / 'game.mp4'
            self._make_part(primary, with_audio=False)
            self._make_part(close, with_audio=True)
            before = primary.read_bytes()
            self.assertEqual(main([str(primary), '--output', str(output),
                                   '--extra-view', 'playercam', str(close)]), 0)
            alternate = root / 'game.playercam-01.mp4'
            self.assertEqual(self._stream_types(alternate), ['video'])
            self.assertEqual(primary.read_bytes(), before)
            payload = json.loads(sidecar_path(alternate).read_text())
            self.assertEqual(payload['view'], 'playercam')
            self.assertNotIn('periods', payload)
            self.assertEqual(payload['timeline_alignment']['status'], 'unknown')

    def test_cli_metadata_capture_with_real_synthetic_remux(self) -> None:
        from trace_game_extractor import main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second, output = root / "first.mp4", root / "second.mp4", root / "game.mp4"
            self._make_part(first, with_audio=True)
            self._make_part(second, with_audio=False)
            originals = [path.read_bytes() for path in (first, second)]
            source = root / "game.json"
            source.write_text(json.dumps({"all_events": [{"utc_time": 1700000000500}], "hls_folders": []}))
            with mock.patch("urllib.request.build_opener") as network:
                self.assertEqual(main([str(first), str(second), "--output", str(output), "--metadata",
                                       "--metadata-source", "game", str(source)]), 0)
                network.assert_not_called()
            bundle = json.loads(output.with_suffix(".metadata.json").read_text())
            sidecar = json.loads(sidecar_path(output).read_text())
            self.assertEqual(bundle["export"]["video_sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
            self.assertEqual(bundle["export"]["sidecar"], sidecar)
            self.assertEqual(len(sidecar["periods"]), 2)
            self.assertEqual(bundle["documents"][0]["data"]["all_events"][0]["utc_time"], 1700000000500)
            self.assertEqual(bundle["trust"]["timeline_alignment"], "unverified")
            self.assertEqual(self._stream_types(output), ["video"])
            self.assertEqual([path.read_bytes() for path in (first, second)], originals)


if __name__ == "__main__":
    unittest.main()
