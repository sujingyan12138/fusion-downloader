from __future__ import annotations

import tempfile
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
    def test_public_mode_has_no_login_state_api(self) -> None:
        self.assertFalse(hasattr(youtube, "open_youtube_login_browser"))
        self.assertFalse(hasattr(youtube, "read_youtube_login_context"))
        self.assertFalse(hasattr(youtube, "load_youtube_cookies"))

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

    def test_login_required_error_explains_public_only_boundary(self) -> None:
        message = youtube._friendly_download_error(Exception("Sign in to confirm your age"))

        self.assertIn("仅支持公开可访问", message)
        self.assertIn("年龄验证", message)

    @patch("services.task_runner.youtube.download_video")
    def test_task_runner_aggregates_youtube_success_and_failure(self, download_video) -> None:
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
        self.assertIn("公开可获取的最高画质", messages[0])
        self.assertNotIn("登录态", "\n".join(messages))
        for call in download_video.call_args_list:
            self.assertNotIn("cookies", call.kwargs)


if __name__ == "__main__":
    unittest.main()
