from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from downloaders import bilibili
from services.task_runner import TaskOptions, extract_task_inputs, run_task


class BilibiliUrlTests(unittest.TestCase):
    def test_extracts_full_and_short_urls_without_duplicates(self) -> None:
        text = (
            "完整 https://www.bilibili.com/video/BV1oHNv6kEzB/?share_source=copy_web，"
            "重复 https://www.bilibili.com/video/BV1oHNv6kEzB/ "
            "短链 https://b23.tv/AbC123。"
        )

        self.assertEqual(
            bilibili.extract_urls(text),
            [
                "https://www.bilibili.com/video/BV1oHNv6kEzB/?share_source=copy_web",
                "https://b23.tv/AbC123",
            ],
        )

    def test_rejects_non_video_page(self) -> None:
        with self.assertRaises(bilibili.BilibiliDownloadError):
            bilibili.extract_url("https://www.bilibili.com/")

    def test_task_input_dispatch_uses_bilibili_parser(self) -> None:
        urls = extract_task_inputs(
            "Bilibili",
            "【视频】https://www.bilibili.com/video/BV1oHNv6kEzB/",
            single=False,
        )
        self.assertEqual(urls, ["https://www.bilibili.com/video/BV1oHNv6kEzB/"])


class BilibiliMediaTests(unittest.TestCase):
    def test_quality_strategy_is_not_capped_at_1080p(self) -> None:
        self.assertEqual(bilibili.FORMAT_SELECTOR, "bestvideo+bestaudio/best")
        self.assertEqual(bilibili.FORMAT_SORT, ("res", "fps", "br"))

    def test_cookie_header_only_keeps_bilibili_domains(self) -> None:
        class FakeCdp:
            def call(self, _method, timeout=8):
                self.timeout = timeout
                return {
                    "cookies": [
                        {"domain": ".bilibili.com", "name": "SESSDATA", "value": "member"},
                        {"domain": ".example.com", "name": "secret", "value": "ignore"},
                    ]
                }

        self.assertEqual(bilibili.get_bilibili_cookie_header(FakeCdp()), "SESSDATA=member")

    def test_media_cdn_headers_never_include_account_cookie(self) -> None:
        headers = bilibili.media_request_headers(
            {"http_headers": {"Referer": "https://www.bilibili.com/", "Cookie": "SESSDATA=member"}}
        )
        self.assertEqual(headers["Referer"], "https://www.bilibili.com/")
        self.assertNotIn("Cookie", headers)

    def test_yt_dlp_cookiejar_scopes_account_cookie_to_bilibili(self) -> None:
        with bilibili.YoutubeDL({"quiet": True}) as ydl:
            bilibili.load_bilibili_cookies(ydl, "SESSDATA=member; bili_jct=csrf=value")

            self.assertIn("SESSDATA=member", ydl.cookiejar.get_cookie_header("https://www.bilibili.com/video/BV1"))
            self.assertIn("bili_jct=csrf=value", ydl.cookiejar.get_cookie_header("https://api.bilibili.com/"))
            self.assertIsNone(ydl.cookiejar.get_cookie_header("https://example.com/video"))
            self.assertIsNone(ydl.cookiejar.get_cookie_header("https://cdn.bilivideo.com/video.m4s"))

    def test_yt_dlp_logger_deduplicates_deprecation_notice(self) -> None:
        messages: list[str] = []
        logger = bilibili._YtDlpLogger(messages.append)
        notice = "Deprecated Feature: Passing cookies as a header is a potential security risk"

        logger.warning(notice)
        logger.error(notice)
        logger.warning(notice)

        self.assertEqual(messages, [f"Bilibili 提示：{notice}"])

    def test_yt_dlp_logger_keeps_real_errors(self) -> None:
        messages: list[str] = []
        logger = bilibili._YtDlpLogger(messages.append)

        logger.error("Unable to extract video data")

        self.assertEqual(messages, ["Bilibili 错误：Unable to extract video data"])

    def test_extract_cid_from_initial_state(self) -> None:
        html = '<script>window.__INITIAL_STATE__ = {"videoData":{"cid":39966543072}};</script>'
        self.assertEqual(bilibili.extract_cid(html), 39966543072)

    def test_parse_content_range(self) -> None:
        self.assertEqual(bilibili.parse_content_range("bytes 0-4194303/250831695"), (0, 4194303, 250831695))
        self.assertEqual(bilibili.parse_content_range(""), (-1, -1, 0))

    def test_stream_item_urls_includes_backups(self) -> None:
        urls = bilibili.stream_item_urls(
            {"baseUrl": "https://main.example/video.m4s", "backupUrl": ["https://backup.example/video.m4s"]}
        )
        self.assertEqual(urls, ["https://main.example/video.m4s", "https://backup.example/video.m4s"])

    def test_summarize_streams_requires_video_and_audio(self) -> None:
        summary = bilibili.summarize_streams(
            {
                "streams": [
                    {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "r_frame_rate": "25/1"},
                    {"codec_type": "audio", "codec_name": "aac"},
                ]
            }
        )

        self.assertTrue(summary["has_video"])
        self.assertTrue(summary["has_audio"])
        self.assertEqual(summary["resolution"], "1920x1080")
        self.assertEqual(summary["fps"], 25.0)

    def test_unique_path_never_overwrites_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            original = Path(temp_name) / "video.mp4"
            original.write_bytes(b"existing")
            self.assertEqual(bilibili.unique_path(original).name, "video_2.mp4")

    @patch("services.task_runner.bilibili.read_bilibili_login_context")
    @patch("services.task_runner.bilibili.download_video")
    def test_task_runner_aggregates_bilibili_success_and_failure(self, download_video, read_login) -> None:
        read_login.return_value = {"cookie": "", "logged_in": False, "vip": False}
        download_video.side_effect = [
            {"video_id": "BV1", "output_path": "one.mp4"},
            bilibili.BilibiliDownloadError("受限"),
        ]
        with tempfile.TemporaryDirectory() as temp_name:
            options = TaskOptions(
                platform="Bilibili",
                feature="视频媒体",
                inputs=["https://www.bilibili.com/video/BV1aaaa/", "https://www.bilibili.com/video/BV1bbbb/"],
                output_root=Path(temp_name),
            )
            report = run_task(options, log=lambda _message: None)

        self.assertEqual(len(report["items"]), 1)
        self.assertEqual(len(report["failures"]), 1)
        self.assertEqual(report["failures"][0]["error"], "受限")


if __name__ == "__main__":
    unittest.main()
