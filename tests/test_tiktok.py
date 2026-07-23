from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from downloaders import tiktok
from services.task_runner import TaskOptions, extract_task_inputs, run_task


TEST_URL = "https://www.tiktok.com/@squidgamenetflix/video/7465383132565409070"


class TikTokUrlTests(unittest.TestCase):
    def test_extracts_direct_and_short_urls_without_duplicate_work_ids(self) -> None:
        text = (
            f"作品 {TEST_URL}?is_from_webapp=1，"
            "重复 https://www.tiktok.com/@other/video/7465383132565409070。"
            "短链 https://vm.tiktok.com/ZMexample/"
        )

        self.assertEqual(
            tiktok.extract_urls(text),
            [
                f"{TEST_URL}?is_from_webapp=1",
                "https://vm.tiktok.com/ZMexample/",
            ],
        )

    def test_rejects_profile_page(self) -> None:
        with self.assertRaises(tiktok.TikTokDownloadError):
            tiktok.extract_url("https://www.tiktok.com/@squidgamenetflix")

    def test_task_input_dispatch_enforces_single_work(self) -> None:
        urls = extract_task_inputs(
            "TikTok",
            f"{TEST_URL} https://www.tiktok.com/@creator/video/1234567890123456789",
            single=False,
        )

        self.assertEqual(urls, [TEST_URL])
        self.assertEqual(app.TIKTOK_FEATURES, ("视频媒体",))


class TikTokMediaTests(unittest.TestCase):
    def test_quality_strategy_filters_watermarked_before_best_fallback(self) -> None:
        self.assertIn("format_note!*=watermarked", tiktok.FORMAT_SELECTOR)
        self.assertTrue(tiktok.FORMAT_SELECTOR.endswith("/best"))
        self.assertEqual(tiktok.FORMAT_SORT, ("res", "fps", "br"))

    def test_build_options_select_highest_quality_and_single_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            options = tiktok.build_ydl_options(
                Path(temp_name),
                "C:/tools/ffmpeg.exe",
                lambda _message: None,
                lambda _status: None,
                max_workers=12,
            )

        self.assertEqual(options["format"], tiktok.FORMAT_SELECTOR)
        self.assertEqual(options["format_sort"], ["res", "fps", "br"])
        self.assertTrue(options["noplaylist"])
        self.assertEqual(options["concurrent_fragment_downloads"], 8)
        self.assertEqual(options["merge_output_format"], "mkv")

    def test_resolve_downloaded_path_uses_verified_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            media = root / "media.mp4"
            media.write_bytes(b"media")

            result = tiktok.resolve_downloaded_path(
                {"requested_downloads": [{"filepath": str(media)}]},
                root,
            )

        self.assertEqual(result.name, "media.mp4")

    def test_summarize_streams_requires_video_and_audio(self) -> None:
        summary = tiktok.summarize_streams(
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "hevc",
                        "width": 1080,
                        "height": 1920,
                        "r_frame_rate": "30/1",
                    },
                    {"codec_type": "audio", "codec_name": "aac"},
                ]
            }
        )

        self.assertTrue(summary["has_video"])
        self.assertTrue(summary["has_audio"])
        self.assertEqual(summary["resolution"], "1080x1920")
        self.assertEqual(summary["fps"], 30.0)

    def test_unique_path_never_overwrites_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            original = Path(temp_name) / "TikTok_test_123.mp4"
            original.write_bytes(b"old")

            result = tiktok.unique_path(original)

        self.assertEqual(result.name, "TikTok_test_123_2.mp4")

    def test_private_error_explains_public_single_work_boundary(self) -> None:
        message = tiktok._friendly_download_error(
            Exception("This video is private; login required")
        )

        self.assertIn("仅支持公开单作品", message)
        self.assertIn("私密", message)

    def test_log_encoding_failure_does_not_turn_download_into_failure(self) -> None:
        messages: list[str] = []

        def gbk_console(message: str) -> None:
            if "🙂" in message:
                raise UnicodeEncodeError("gbk", "🙂", 0, 1, "illegal multibyte")
            messages.append(message)

        tiktok._emit_log(gbk_console, "TikTok_标题🙂_123.mp4")

        self.assertEqual(messages, ["TikTok_标题?_123.mp4"])

    @patch("downloaders.tiktok.probe_media")
    @patch("downloaders.tiktok.resolve_downloaded_path")
    @patch("downloaders.tiktok.YoutubeDL")
    @patch("downloaders.tiktok.find_executable")
    def test_download_video_moves_verified_media_to_safe_unique_name(
        self,
        find_executable,
        youtube_dl,
        resolve_path,
        probe_media,
    ) -> None:
        find_executable.side_effect = lambda name: f"C:/tools/{name}.exe"
        info = {
            "id": "7465383132565409070",
            "title": "Smile 🙂",
            "uploader": "squidgamenetflix",
            "webpage_url": TEST_URL,
            "format_id": "bytevc1_1080p_542129-1",
            "format_note": "",
            "width": 1080,
            "height": 1920,
            "vcodec": "h265",
            "acodec": "aac",
            "tbr": 542,
        }
        ydl = youtube_dl.return_value.__enter__.return_value
        ydl.extract_info.return_value = info

        def fake_resolve(_info, temp_dir):
            media = Path(temp_dir) / "media.mp4"
            media.write_bytes(b"verified media")
            return media

        resolve_path.side_effect = fake_resolve
        probe_media.return_value = {
            "has_video": True,
            "has_audio": True,
            "resolution": "1080x1920",
            "fps": 30.0,
            "video_codec": "hevc",
            "audio_codec": "aac",
        }

        with tempfile.TemporaryDirectory() as temp_name:
            report = tiktok.download_video(
                TEST_URL,
                Path(temp_name),
                max_workers=4,
            )
            output = Path(report["output_path"])

            self.assertTrue(output.is_file())
            self.assertIn("TikTok_squidgamenetflix_Smile", output.name)
            self.assertIn("7465383132565409070", output.name)
            self.assertFalse(report["watermarked"])
            self.assertEqual(report["format_ids"], ["bytevc1_1080p_542129-1"])

        ydl.extract_info.assert_called_once_with(TEST_URL, download=True)

    @patch("services.task_runner.tiktok.download_video")
    def test_task_runner_aggregates_single_tiktok_failure(self, download_video) -> None:
        download_video.side_effect = tiktok.TikTokDownloadError("作品受限")
        with tempfile.TemporaryDirectory() as temp_name:
            messages: list[str] = []
            report = run_task(
                TaskOptions(
                    platform="TikTok",
                    feature="视频媒体",
                    inputs=[TEST_URL],
                    output_root=Path(temp_name),
                    max_workers=4,
                ),
                log=messages.append,
            )

        self.assertEqual(report["items"], [])
        self.assertEqual(report["failures"][0]["error"], "作品受限")
        self.assertTrue(any("公开单作品" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
