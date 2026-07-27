from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from downloaders import youtube
from services.task_runner import TaskOptions, extract_task_inputs, run_task


class YouTubeUrlTests(unittest.TestCase):
    def test_extracts_watch_short_and_shorts_urls_without_duplicates(self) -> None:
        text = (
            "完整 https://www.youtube.com/watch?v=EvjZ7ckgYTg&feature=share，"
            "重复 https://youtu.be/EvjZ7ckgYTg?t=3 "
            "Shorts https://www.youtube.com/shorts/dQw4w9WgXcQ。"
        )

        self.assertEqual(
            youtube.extract_urls(text),
            [
                "https://www.youtube.com/watch?v=EvjZ7ckgYTg&feature=share",
                "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            ],
        )

    def test_rejects_non_video_page(self) -> None:
        with self.assertRaises(youtube.YouTubeDownloadError):
            youtube.extract_url("https://www.youtube.com/")

    def test_task_input_dispatch_uses_youtube_parser(self) -> None:
        urls = extract_task_inputs(
            "YouTube",
            "【视频】https://www.youtube.com/watch?v=EvjZ7ckgYTg",
            single=False,
        )
        self.assertEqual(urls, ["https://www.youtube.com/watch?v=EvjZ7ckgYTg"])


class YouTubeMediaTests(unittest.TestCase):
    @patch("downloaders.youtube.app_base_dir")
    def test_auth_defaults_to_anonymous_and_can_be_cleared(self, app_base_dir) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            app_base_dir.return_value = Path(temp_name)
            self.assertEqual(youtube.read_youtube_auth_context()["mode"], "anonymous")

            youtube._write_auth_config("firefox")
            self.assertEqual(youtube.read_youtube_auth_context()["mode"], "firefox")

            youtube.configure_browser_auth("chrome")
            self.assertEqual(youtube.read_youtube_auth_context()["mode"], "chrome")

            youtube.clear_youtube_auth()
            self.assertEqual(youtube.read_youtube_auth_context()["mode"], "anonymous")
            self.assertFalse(youtube.youtube_auth_dir().exists())

    @patch("downloaders.youtube.app_base_dir")
    def test_cookie_import_keeps_only_youtube_and_google_domains(self, app_base_dir) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            app_base_dir.return_value = root
            source = root / "source.txt"
            source.write_text(
                "# Netscape HTTP Cookie File\n"
                ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSAPISID\tyoutube-value\n"
                ".google.com\tTRUE\t/\tTRUE\t2147483647\tSID\tgoogle-value\n"
                ".example.com\tTRUE\t/\tTRUE\t2147483647\tOTHER\tshould-not-be-saved\n",
                encoding="utf-8",
            )

            context = youtube.import_youtube_cookie_file(source)
            saved = youtube.youtube_cookie_file_path().read_text(encoding="utf-8")

        self.assertEqual(context["mode"], "cookie_file")
        self.assertEqual(context["cookie_count"], 2)
        self.assertTrue(context["account_cookie_found"])
        self.assertIn(".youtube.com", saved)
        self.assertIn(".google.com", saved)
        self.assertNotIn("example.com", saved)

    @patch("downloaders.youtube.app_base_dir")
    def test_cookie_import_rejects_non_netscape_file(self, app_base_dir) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            app_base_dir.return_value = root
            source = root / "cookies.json"
            source.write_text('{"cookie": "value"}', encoding="utf-8")

            with self.assertRaisesRegex(youtube.YouTubeDownloadError, "Netscape"):
                youtube.import_youtube_cookie_file(source)

    @patch("downloaders.youtube.app_base_dir")
    def test_cookie_import_rejects_empty_file_with_clear_message(self, app_base_dir) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            app_base_dir.return_value = root
            source = root / "cookie.txt"
            source.write_text("", encoding="utf-8")

            with self.assertRaisesRegex(youtube.YouTubeDownloadError, "空文件"):
                youtube.import_youtube_cookie_file(source)

    @patch("downloaders.youtube.app_base_dir")
    def test_auto_import_selects_newest_logged_in_youtube_cookie(self, app_base_dir) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            desktop = root / "Desktop"
            downloads = root / "Downloads"
            desktop.mkdir()
            downloads.mkdir()
            app_base_dir.return_value = root / "app"
            now = time.time()

            (desktop / "cookie.txt").write_text("", encoding="utf-8")
            (downloads / "unrelated.txt").write_text(
                "# Netscape HTTP Cookie File\n"
                ".example.com\tTRUE\t/\tTRUE\t2147483647\tSID\tunrelated\n",
                encoding="utf-8",
            )
            older = desktop / "youtube-old.txt"
            older.write_text(
                "# Netscape HTTP Cookie File\n"
                ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSAPISID\told-private\n",
                encoding="utf-8",
            )
            newest = downloads / "www.youtube.com_cookies.txt"
            newest.write_text(
                "# Netscape HTTP Cookie File\n"
                ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSAPISID\tnew-private\n"
                ".google.com\tTRUE\t/\tTRUE\t2147483647\tSID\tnew-google\n",
                encoding="utf-8",
            )
            os.utime(older, (now - 120, now - 120))
            os.utime(newest, (now - 10, now - 10))

            context = youtube.auto_import_latest_youtube_cookie(
                [desktop, downloads],
                now=now,
            )
            saved = youtube.youtube_cookie_file_path().read_text(encoding="utf-8")

        self.assertEqual(context["source_name"], newest.name)
        self.assertEqual(context["cookie_count"], 2)
        self.assertTrue(context["account_cookie_found"])
        self.assertIn("new-private", saved)
        self.assertNotIn("old-private", saved)
        self.assertNotIn("unrelated", saved)

    def test_auto_import_rejects_guest_only_or_old_cookie_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            now = time.time()
            guest = root / "guest.txt"
            guest.write_text(
                "# Netscape HTTP Cookie File\n"
                ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tPREF\tguest\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(youtube.YouTubeDownloadError, "没有账号登录凭据"):
                youtube.find_recent_youtube_cookie_file([root], now=now)

            logged_in = root / "old.txt"
            logged_in.write_text(
                "# Netscape HTTP Cookie File\n"
                ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSAPISID\told-private\n",
                encoding="utf-8",
            )
            os.utime(logged_in, (now - 49 * 60 * 60, now - 49 * 60 * 60))
            guest.unlink()

            with self.assertRaisesRegex(youtube.YouTubeDownloadError, "最近 48 小时"):
                youtube.find_recent_youtube_cookie_file([root], now=now)

    def test_cookie_tutorial_links_use_official_sources(self) -> None:
        self.assertIn(
            "cclelndahbckbenkjhflpdbgdldlbecc",
            youtube.YOUTUBE_COOKIE_EXTENSION_URL,
        )
        self.assertTrue(youtube.YOUTUBE_COOKIE_FAQ_URL.startswith("https://github.com/yt-dlp/"))
        self.assertEqual(youtube.YOUTUBE_ROBOTS_URL, "https://www.youtube.com/robots.txt")

    @patch("downloaders.youtube.app_base_dir")
    def test_imported_cookie_can_be_loaded_without_exposing_values(self, app_base_dir) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            app_base_dir.return_value = root
            source = root / "source.txt"
            source.write_text(
                "# Netscape HTTP Cookie File\n"
                ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSAPISID\tprivate-value\n",
                encoding="utf-8",
            )
            youtube.import_youtube_cookie_file(source)

            context = youtube.inspect_youtube_auth_context()

        self.assertTrue(context["configured"])
        self.assertEqual(context["cookie_count"], 1)
        self.assertTrue(context["account_cookie_found"])
        self.assertNotIn("private-value", str(context))

    def test_quality_strategy_is_not_capped_at_1080p(self) -> None:
        self.assertEqual(youtube.FORMAT_SELECTOR, "bestvideo+bestaudio/best")
        self.assertEqual(youtube.FORMAT_SORT, ("res", "fps", "br"))

    def test_build_options_enable_deno_ejs_and_mkv_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            options = youtube.build_ydl_options(
                Path(temp_name),
                "C:/tools/ffmpeg.exe",
                "C:/tools/deno.exe",
                lambda _message: None,
                lambda _status: None,
                max_workers=12,
            )

        self.assertEqual(options["js_runtimes"], {"deno": {"path": "C:\\tools\\deno.exe"}})
        self.assertEqual(options["merge_output_format"], "mkv")
        self.assertEqual(options["concurrent_fragment_downloads"], 8)
        self.assertEqual(options["http_chunk_size"], 4 * 1024 * 1024)
        self.assertNotIn("cookiesfrombrowser", options)
        self.assertNotIn("cookiefile", options)

    def test_build_options_support_firefox_and_cookie_file_auth(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            cookie_file = root / "cookies.txt"
            cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
            common_args = (
                root,
                "C:/tools/ffmpeg.exe",
                "C:/tools/deno.exe",
                lambda _message: None,
                lambda _status: None,
                4,
            )

            firefox_options = youtube.build_ydl_options(
                *common_args,
                auth_context={"mode": "firefox"},
            )
            cookie_options = youtube.build_ydl_options(
                *common_args,
                auth_context={"mode": "cookie_file", "cookie_file": str(cookie_file)},
            )
            chrome_options = youtube.build_ydl_options(
                *common_args,
                auth_context={"mode": "chrome"},
            )

        self.assertEqual(firefox_options["cookiesfrombrowser"], ("firefox", None, None, None))
        self.assertEqual(chrome_options["cookiesfrombrowser"], ("chrome", None, None, None))
        self.assertEqual(cookie_options["cookiefile"], str(cookie_file))

    def test_range_builder_covers_file_without_gaps(self) -> None:
        self.assertEqual(youtube.build_ranges(10, 4), [(0, 3), (4, 7), (8, 9)])
        self.assertEqual(youtube.parse_content_range("bytes 4-7/10"), (4, 7, 10))

    def test_media_headers_strip_cookie_and_disable_compression(self) -> None:
        headers = youtube.media_request_headers(
            {"http_headers": {"User-Agent": "test", "Cookie": "secret", "Accept-Encoding": "gzip"}}
        )

        self.assertEqual(headers["User-Agent"], "test")
        self.assertEqual(headers["Accept-Encoding"], "identity")
        self.assertNotIn("Cookie", headers)

    @patch("downloaders.youtube._download_range_chunk")
    def test_parallel_stream_writes_completed_ranges_at_offsets(self, download_chunk) -> None:
        download_chunk.side_effect = lambda _url, _headers, start, end: (
            start,
            bytes(range(start, end + 1)),
        )
        with tempfile.TemporaryDirectory() as temp_name:
            target = Path(temp_name) / "video.bin"
            youtube.download_parallel_stream(
                {"url": "https://example.test/video", "protocol": "https", "filesize": 10},
                target,
                "视频",
                lambda _message: None,
                max_workers=4,
            )

            self.assertEqual(target.read_bytes(), bytes(range(10)))

    @patch("downloaders.youtube.time.sleep")
    @patch("downloaders.youtube.requests.get")
    def test_range_chunk_retries_then_requests_stable_fallback(self, get, _sleep) -> None:
        class Response:
            status_code = 503
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        get.return_value = Response()

        with self.assertRaises(youtube._ParallelDownloadUnavailable):
            youtube._download_range_chunk("https://example.test/video", {}, 0, 3)

        self.assertEqual(get.call_count, youtube.RANGE_RETRIES + 1)

    @patch("downloaders.youtube.merge_streams")
    @patch("downloaders.youtube.download_parallel_stream")
    def test_selected_media_downloads_video_and_audio_then_merges(self, download_stream, merge) -> None:
        def fake_download(_format, target, _label, _logger, max_workers=4):
            self.assertEqual(max_workers, 8)
            target.write_bytes(b"stream")

        def fake_merge(_video, _audio, output, _ffmpeg):
            output.write_bytes(b"merged")

        download_stream.side_effect = fake_download
        merge.side_effect = fake_merge
        info = {
            "requested_formats": [
                {"url": "https://example.test/video", "ext": "webm", "vcodec": "vp9", "acodec": "none"},
                {"url": "https://example.test/audio", "ext": "m4a", "vcodec": "none", "acodec": "aac"},
            ]
        }
        with tempfile.TemporaryDirectory() as temp_name:
            result = youtube.download_selected_media(
                info,
                Path(temp_name),
                "C:/tools/ffmpeg.exe",
                lambda _message: None,
                max_workers=8,
            )

            self.assertEqual(result.read_bytes(), b"merged")

        self.assertEqual(download_stream.call_count, 2)
        merge.assert_called_once()

    @patch("downloaders.youtube.probe_media")
    @patch("downloaders.youtube.resolve_downloaded_path")
    @patch("downloaders.youtube.download_selected_media")
    @patch("downloaders.youtube.YoutubeDL")
    @patch("downloaders.youtube.find_executable")
    def test_download_video_falls_back_to_yt_dlp_after_parallel_failure(
        self,
        find_executable,
        youtube_dl,
        download_selected,
        resolve_path,
        probe_media,
    ) -> None:
        find_executable.side_effect = lambda name: f"C:/tools/{name}.exe"
        info = {
            "id": "EvjZ7ckgYTg",
            "title": "Test",
            "uploader": "Channel",
            "requested_formats": [
                {"format_id": "315", "vcodec": "vp9", "acodec": "none"},
                {"format_id": "140", "vcodec": "none", "acodec": "aac"},
            ],
        }
        ydl = youtube_dl.return_value.__enter__.return_value
        ydl.extract_info.side_effect = [info, info]
        download_selected.side_effect = youtube._ParallelDownloadUnavailable("range failed")

        def fake_resolve(_info, temp_dir):
            path = Path(temp_dir) / "media.mkv"
            path.write_bytes(b"media")
            return path

        resolve_path.side_effect = fake_resolve
        probe_media.return_value = {
            "has_video": True,
            "has_audio": True,
            "resolution": "3840x2160",
            "fps": 50.0,
            "video_codec": "vp9",
            "audio_codec": "aac",
        }
        logs: list[str] = []
        with tempfile.TemporaryDirectory() as temp_name:
            report = youtube.download_video(
                "https://www.youtube.com/watch?v=EvjZ7ckgYTg",
                Path(temp_name),
                log=logs.append,
                max_workers=8,
            )

        self.assertEqual(ydl.extract_info.call_args_list[0].kwargs["download"], False)
        self.assertEqual(ydl.extract_info.call_args_list[1].kwargs["download"], True)
        self.assertIn("稳定分段", report["download_engine"])
        self.assertTrue(any("自动切换到稳定模式" in message for message in logs))

    def test_resolve_downloaded_path_uses_verified_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            media = root / "media.mkv"
            media.write_bytes(b"media")

            result = youtube.resolve_downloaded_path(
                {"requested_downloads": [{"filepath": str(media)}]},
                root,
            )

        self.assertEqual(result.name, "media.mkv")

    def test_summarize_streams_requires_video_and_audio(self) -> None:
        summary = youtube.summarize_streams(
            {
                "streams": [
                    {"codec_type": "video", "codec_name": "vp9", "width": 3840, "height": 2160, "r_frame_rate": "50/1"},
                    {"codec_type": "audio", "codec_name": "aac"},
                ]
            }
        )

        self.assertTrue(summary["has_video"])
        self.assertTrue(summary["has_audio"])
        self.assertEqual(summary["resolution"], "3840x2160")
        self.assertEqual(summary["fps"], 50.0)

    def test_rate_limit_error_is_not_misreported_as_private_video(self) -> None:
        message = youtube._friendly_download_error(Exception("HTTP Error 429: Too Many Requests"))

        self.assertIn("网络/IP", message)
        self.assertIn("临时限流", message)
        self.assertNotIn("私享", message)

    def test_bot_confirmation_suggests_optional_auth(self) -> None:
        message = youtube._friendly_download_error(Exception("Sign in to confirm you’re not a bot"))

        self.assertIn("不是机器人", message)
        self.assertIn("YouTube 登录设置", message)
        self.assertIn("浏览器登录状态", message)

    def test_login_required_error_explains_account_permission_boundary(self) -> None:
        message = youtube._friendly_download_error(Exception("Sign in to confirm your age"))

        self.assertIn("账号登录", message)
        self.assertIn("年龄验证", message)

    @patch("services.task_runner.youtube.read_youtube_auth_context")
    @patch("services.task_runner.youtube.download_video")
    def test_task_runner_aggregates_youtube_success_and_failure(
        self,
        download_video,
        read_auth_context,
    ) -> None:
        read_auth_context.return_value = {
            "mode": "firefox",
            "configured": True,
            "description": "Firefox 登录状态",
        }
        download_video.side_effect = [
            {"video_id": "one", "output_path": "one.mkv"},
            youtube.YouTubeDownloadError("受限"),
        ]
        with tempfile.TemporaryDirectory() as temp_name:
            options = TaskOptions(
                platform="YouTube",
                feature="视频媒体",
                inputs=[
                    "https://www.youtube.com/watch?v=EvjZ7ckgYTg",
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                ],
                output_root=Path(temp_name),
            )
            messages: list[str] = []
            report = run_task(options, log=messages.append)

        self.assertEqual(len(report["items"]), 1)
        self.assertEqual(len(report["failures"]), 1)
        self.assertEqual(report["failures"][0]["error"], "受限")
        self.assertIn("Firefox 登录状态", messages[0])
        for call in download_video.call_args_list:
            self.assertEqual(call.kwargs["auth_context"]["mode"], "firefox")


if __name__ == "__main__":
    unittest.main()
