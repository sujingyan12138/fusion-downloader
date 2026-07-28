from __future__ import annotations

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from downloaders import bilibili, covers, douyin, tiktok, xiaohongshu, youtube
from services.task_runner import TaskOptions, run_task


def make_image_bytes(size: tuple[int, int], image_format: str = "JPEG") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color=(30, 120, 220)).save(buffer, format=image_format)
    return buffer.getvalue()


class FakeResponse:
    def __init__(
        self,
        content: bytes,
        *,
        status_code: int = 200,
        content_type: str = "image/jpeg",
    ) -> None:
        self.content = content
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}


class CoverCoreTests(unittest.TestCase):
    @patch("downloaders.covers.requests.Session")
    def test_selects_by_actual_decoded_dimensions_not_declared_size(self, session_class) -> None:
        session = session_class.return_value
        session.get.side_effect = lambda url, **_kwargs: {
            "https://example.test/declared-large.jpg": FakeResponse(
                make_image_bytes((320, 180))
            ),
            "https://example.test/actual-large.jpg": FakeResponse(
                make_image_bytes((1280, 720))
            ),
        }[url]
        candidates = [
            covers.CoverCandidate(
                "https://example.test/declared-large.jpg",
                "declared-large",
                4000,
                3000,
            ),
            covers.CoverCandidate(
                "https://example.test/actual-large.jpg",
                "actual-large",
                640,
                360,
            ),
        ]

        with tempfile.TemporaryDirectory() as temp_name:
            report = covers.download_best_cover(
                candidates,
                temp_name,
                "平台_作者_标题_ID",
            )
            target = Path(report["path"])
            self.assertTrue(target.is_file())
            self.assertEqual((report["width"], report["height"]), (1280, 720))
            self.assertIn("_封面_1280x720.jpg", target.name)

        self.assertEqual(session.get.call_count, 2)

    @patch("downloaders.covers.requests.Session")
    def test_existing_valid_cover_is_reused_without_network(self, session_class) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            target = Path(temp_name) / "平台_作者_标题_ID_封面_640x360.jpg"
            target.write_bytes(make_image_bytes((640, 360)))

            report = covers.download_best_cover(
                [covers.CoverCandidate("https://example.test/new.jpg", "new")],
                temp_name,
                "平台_作者_标题_ID",
            )

        self.assertEqual(report["status"], "skipped")
        session_class.assert_not_called()

    @patch("downloaders.covers.requests.Session")
    def test_invalid_image_response_is_rejected(self, session_class) -> None:
        session_class.return_value.get.return_value = FakeResponse(b"not-an-image" * 8)
        with tempfile.TemporaryDirectory() as temp_name:
            with self.assertRaises(covers.CoverDownloadError):
                covers.download_best_cover(
                    [covers.CoverCandidate("https://example.test/bad.jpg", "bad")],
                    temp_name,
                    "测试封面",
                )

    def test_youtube_candidates_include_maximum_resolution_endpoint(self) -> None:
        info = {"id": "7xTGNNLPyMI", "thumbnails": []}
        candidates = youtube.youtube_cover_candidates(info)
        self.assertEqual(candidates[0].source, "youtube:maxresdefault")
        self.assertEqual((candidates[0].width, candidates[0].height), (1280, 720))


class PlatformCoverAdapterTests(unittest.TestCase):
    @patch("downloaders.douyin.covers.save_cover_bytes")
    @patch("downloaders.douyin.choose_best_image")
    @patch("downloaders.douyin.make_session")
    def test_douyin_video_uses_static_cover_candidates(
        self,
        _make_session,
        choose_best,
        save_cover,
    ) -> None:
        choose_best.return_value = SimpleNamespace(
            content=make_image_bytes((1080, 1920)),
            width=1080,
            height=1920,
            extension="jpg",
            image_format="jpeg",
            candidate=SimpleNamespace(source="video.origin_cover", url="https://cdn/cover.jpg"),
        )
        save_cover.return_value = {"status": "ok", "resolution": "1080x1920"}
        aweme = {
            "video": {
                "origin_cover": {
                    "width": 1080,
                    "height": 1920,
                    "url_list": ["https://cdn/cover.jpg"],
                }
            }
        }

        with tempfile.TemporaryDirectory() as temp_name:
            report = douyin.download_cover_from_aweme(
                aweme,
                [],
                temp_name,
                "抖音_作者_标题_123",
                "https://www.douyin.com/video/123",
            )

        cover_item = choose_best.call_args.args[1]
        self.assertTrue(
            any("origin_cover" in candidate.source for candidate in cover_item.candidates)
        )
        self.assertEqual(report["resolution"], "1080x1920")

    @patch("downloaders.xiaohongshu.covers.save_cover_bytes")
    @patch("downloaders.xiaohongshu.choose_best_image")
    @patch("downloaders.xiaohongshu.make_session")
    def test_xhs_cover_uses_first_note_image(
        self,
        _make_session,
        choose_best,
        save_cover,
    ) -> None:
        first = xiaohongshu.ImageItem(index=1, url_default="https://cdn/first.jpg")
        second = xiaohongshu.ImageItem(index=2, url_default="https://cdn/second.jpg")
        choose_best.return_value = SimpleNamespace(
            content=make_image_bytes((2000, 1500)),
            width=2000,
            height=1500,
            extension="jpg",
            image_format="jpeg",
            candidate=SimpleNamespace(source="ci:raw", url="https://cdn/first.jpg"),
        )
        save_cover.return_value = {"status": "ok", "resolution": "2000x1500"}

        with tempfile.TemporaryDirectory() as temp_name:
            report = xiaohongshu.download_cover_from_note(
                [first, second],
                temp_name,
                "小红书_作者_标题_abc",
                "https://www.xiaohongshu.com/explore/abc",
            )

        self.assertIs(choose_best.call_args.args[1], first)
        self.assertEqual(report["resolution"], "2000x1500")

    def test_yt_dlp_platform_adapters_share_real_thumbnail_candidates(self) -> None:
        info = {
            "thumbnail": "https://cdn.example/cover.jpg",
            "thumbnail_width": 1920,
            "thumbnail_height": 1080,
        }
        self.assertEqual(
            covers.info_cover_candidates(info, source_prefix="bilibili")[0].url,
            "https://cdn.example/cover.jpg",
        )
        self.assertTrue(callable(bilibili.download_cover_from_info))
        self.assertTrue(callable(tiktok.download_cover_from_info))


class CoverOnlyPlatformTests(unittest.TestCase):
    def test_douyin_cover_only_returns_before_media_engine(self) -> None:
        url = "https://www.douyin.com/video/7666007676588920091"
        cover = {
            "path": "cover.jpg",
            "filename": "cover.jpg",
            "resolution": "1080x1920",
        }
        with patch(
            "downloaders.douyin.make_session",
            return_value=MagicMock(),
        ), patch(
            "downloaders.douyin.fetch_page",
            return_value=("<html></html>", url),
        ), patch(
            "downloaders.douyin.find_aweme",
            return_value={"aweme_id": "7666007676588920091"},
        ), patch(
            "downloaders.douyin.extract_images",
            return_value=[],
        ), patch(
            "downloaders.douyin.extract_videos",
            return_value=[{"url": "https://cdn.example/video.mp4"}],
        ), patch(
            "downloaders.douyin.extract_browser_video_streams",
        ) as browser_streams, patch(
            "downloaders.douyin.download_cover_from_aweme",
            return_value=cover,
        ), patch(
            "downloaders.douyin.make_download_engine",
        ) as media_engine, tempfile.TemporaryDirectory() as temp_name:
            report = douyin.download_note(url, temp_name, cover_only=True)

        self.assertEqual(report["kind"], "cover")
        browser_streams.assert_not_called()
        media_engine.assert_not_called()

    def test_xhs_cover_only_returns_before_media_engine(self) -> None:
        url = "https://www.xiaohongshu.com/discovery/item/6a5ccd970000000014005241"
        image = MagicMock()
        cover = {
            "path": "cover.jpg",
            "filename": "cover.jpg",
            "resolution": "2112x2816",
        }
        with patch(
            "downloaders.xiaohongshu.make_session",
            return_value=MagicMock(),
        ), patch(
            "downloaders.xiaohongshu.fetch_note_html",
            return_value=("<html></html>", url),
        ), patch(
            "downloaders.xiaohongshu.parse_initial_state",
            return_value={},
        ), patch(
            "downloaders.xiaohongshu.find_note",
            return_value=(
                "6a5ccd970000000014005241",
                {"user": {"nickname": "作者"}, "title": "示例"},
            ),
        ), patch(
            "downloaders.xiaohongshu.extract_images",
            return_value=[image],
        ), patch(
            "downloaders.xiaohongshu.extract_note_videos",
            return_value=[{"url": "https://cdn.example/video.mp4"}],
        ), patch(
            "downloaders.xiaohongshu.extract_browser_video_streams",
        ) as browser_streams, patch(
            "downloaders.xiaohongshu.download_cover_from_note",
            return_value=cover,
        ), patch(
            "downloaders.xiaohongshu.make_download_engine",
        ) as media_engine, tempfile.TemporaryDirectory() as temp_name:
            report = xiaohongshu.download_note(url, temp_name, cover_only=True)

        self.assertEqual(report["kind"], "cover")
        browser_streams.assert_not_called()
        media_engine.assert_not_called()

    def test_youtube_cover_only_extracts_metadata_without_media_download(self) -> None:
        info = {
            "id": "7xTGNNLPyMI",
            "title": "示例",
            "uploader": "作者",
            "webpage_url": "https://youtu.be/7xTGNNLPyMI",
        }
        ydl = MagicMock()
        ydl.__enter__.return_value = ydl
        ydl.extract_info.return_value = info
        with tempfile.TemporaryDirectory() as temp_name, patch(
            "downloaders.youtube.YoutubeDL",
            return_value=ydl,
        ), patch(
            "downloaders.youtube.find_executable",
            return_value=None,
        ), patch(
            "downloaders.youtube.download_cover_from_info",
            return_value={
                "path": str(Path(temp_name) / "cover.jpg"),
                "filename": "cover.jpg",
                "resolution": "1280x720",
            },
        ):
            report = youtube.download_cover_only(
                info["webpage_url"],
                Path(temp_name),
                auth_context={"mode": "anonymous"},
            )

        ydl.extract_info.assert_called_once_with(info["webpage_url"], download=False)
        self.assertEqual(report["kind"], "cover")
        self.assertEqual(report["output_path"], str(Path(temp_name) / "cover.jpg"))

    def test_bilibili_cover_only_extracts_metadata_without_media_download(self) -> None:
        url = "https://www.bilibili.com/video/BV16cNEeXEer/"
        info = {
            "id": "BV16cNEeXEer",
            "title": "示例",
            "uploader": "作者",
            "webpage_url": url,
        }
        ydl = MagicMock()
        ydl.__enter__.return_value = ydl
        ydl.extract_info.return_value = info
        with tempfile.TemporaryDirectory() as temp_name, patch(
            "downloaders.bilibili.YoutubeDL",
            return_value=ydl,
        ), patch(
            "downloaders.bilibili.load_bilibili_cookies",
        ), patch(
            "downloaders.bilibili.download_cover_from_info",
            return_value={
                "path": str(Path(temp_name) / "cover.jpg"),
                "filename": "cover.jpg",
                "resolution": "1414x900",
            },
        ):
            report = bilibili.download_cover_only(url, Path(temp_name))

        ydl.extract_info.assert_called_once_with(url, download=False)
        self.assertEqual(report["kind"], "cover")

    def test_tiktok_cover_only_extracts_metadata_without_media_download(self) -> None:
        url = "https://www.tiktok.com/@demo/video/7648229711117569293"
        info = {
            "id": "7648229711117569293",
            "title": "示例",
            "uploader": "作者",
            "webpage_url": url,
        }
        ydl = MagicMock()
        ydl.__enter__.return_value = ydl
        ydl.extract_info.return_value = info
        with tempfile.TemporaryDirectory() as temp_name, patch(
            "downloaders.tiktok.YoutubeDL",
            return_value=ydl,
        ), patch(
            "downloaders.tiktok.download_cover_from_info",
            return_value={
                "path": str(Path(temp_name) / "cover.jpg"),
                "filename": "cover.jpg",
                "resolution": "540x960",
            },
        ):
            report = tiktok.download_cover_only(url, Path(temp_name))

        ydl.extract_info.assert_called_once_with(url, download=False)
        self.assertEqual(report["kind"], "cover")


class CoverDispatchTests(unittest.TestCase):
    @patch("services.task_runner.youtube.read_youtube_auth_context")
    @patch("services.task_runner.youtube.download_video")
    def test_youtube_dispatch_forwards_cover_option(self, download_video, read_auth) -> None:
        read_auth.return_value = {"mode": "anonymous"}
        download_video.return_value = {"source_url": "yt", "cover": {"status": "ok"}}
        self._run_single("YouTube", "https://youtu.be/7xTGNNLPyMI", download_video)

    @patch("services.task_runner.bilibili.read_bilibili_login_context")
    @patch("services.task_runner.bilibili.download_video")
    def test_bilibili_dispatch_forwards_cover_option(self, download_video, read_login) -> None:
        read_login.return_value = {"cookie": "", "logged_in": False, "vip": False}
        download_video.return_value = {"source_url": "bili", "cover": {"status": "ok"}}
        self._run_single(
            "Bilibili",
            "https://www.bilibili.com/video/BV16cNEeXEer/",
            download_video,
        )

    @patch("services.task_runner.tiktok.download_video")
    def test_tiktok_dispatch_forwards_cover_option(self, download_video) -> None:
        download_video.return_value = {"source_url": "tiktok", "cover": {"status": "ok"}}
        self._run_single(
            "TikTok",
            "https://www.tiktok.com/@woodcarpenter1384/video/7648229711117569293",
            download_video,
        )

    @patch("services.task_runner.douyin.download_comment_images")
    def test_douyin_comment_function_forwards_cover_option(self, download_comments) -> None:
        download_comments.return_value = {"source_url": "douyin", "cover": {"status": "ok"}}
        self._run_single("抖音", "https://v.douyin.com/example/", download_comments, feature="评论区图片")

    @patch("services.task_runner.xiaohongshu.download_comment_images")
    def test_xhs_comment_function_forwards_cover_option(self, download_comments) -> None:
        download_comments.return_value = {"source_url": "xhs", "cover": {"status": "ok"}}
        self._run_single(
            "小红书",
            "https://www.xiaohongshu.com/discovery/item/6a5ccd970000000014005241",
            download_comments,
            feature="评论区图片",
        )

    @patch("services.task_runner.download_collection")
    def test_douyin_collection_forwards_cover_option(self, download_collection) -> None:
        download_collection.return_value = {"items": [], "failures": [], "output_dir": "out"}
        with tempfile.TemporaryDirectory() as temp_name:
            run_task(
                TaskOptions(
                    platform="抖音",
                    feature="收藏夹",
                    inputs=[],
                    output_root=Path(temp_name),
                    collection_id="123",
                    collection_name="收藏",
                    download_cover=True,
                ),
                log=lambda _message: None,
            )
        self.assertTrue(download_collection.call_args.kwargs["download_cover"])

    @patch("services.task_runner.xiaohongshu.download_collection")
    def test_xhs_collection_forwards_cover_option(self, download_collection) -> None:
        download_collection.return_value = {"items": [], "failures": [], "output_dir": "out"}
        with tempfile.TemporaryDirectory() as temp_name:
            run_task(
                TaskOptions(
                    platform="小红书",
                    feature="收藏作品",
                    inputs=[],
                    output_root=Path(temp_name),
                    collection_id="__all_favorites__",
                    collection_name="全部收藏作品",
                    download_cover=True,
                ),
                log=lambda _message: None,
            )
        self.assertTrue(download_collection.call_args.kwargs["download_cover"])

    @patch("services.task_runner.youtube.read_youtube_auth_context")
    @patch("services.task_runner.youtube.download_video")
    def test_cover_failure_is_reported_without_turning_media_into_failure(
        self,
        download_video,
        read_auth,
    ) -> None:
        read_auth.return_value = {"mode": "anonymous"}
        download_video.return_value = {
            "source_url": "https://youtu.be/7xTGNNLPyMI",
            "output_path": "video.mkv",
            "cover_error": "封面受限",
        }
        with tempfile.TemporaryDirectory() as temp_name:
            report = run_task(
                TaskOptions(
                    platform="YouTube",
                    feature="视频媒体",
                    inputs=["https://youtu.be/7xTGNNLPyMI"],
                    output_root=Path(temp_name),
                    download_cover=True,
                ),
                log=lambda _message: None,
            )
        self.assertEqual(report["failures"], [])
        self.assertEqual(report["cover_failures"][0]["error"], "封面受限")

    def test_youtube_cover_only_dispatch_never_calls_media_download(self) -> None:
        with patch(
            "services.task_runner.youtube.read_youtube_auth_context",
            return_value={"mode": "anonymous"},
        ), patch(
            "services.task_runner.youtube.download_cover_only",
            return_value={"kind": "cover", "cover": {"status": "ok"}},
        ) as download_only, patch(
            "services.task_runner.youtube.download_video",
        ) as download_media, tempfile.TemporaryDirectory() as temp_name:
            report = run_task(
                TaskOptions(
                    platform="YouTube",
                    feature="视频媒体",
                    inputs=["https://youtu.be/7xTGNNLPyMI"],
                    output_root=Path(temp_name),
                    cover_only=True,
                ),
                log=lambda _message: None,
            )

        self.assertEqual(report["failures"], [])
        download_only.assert_called_once()
        download_media.assert_not_called()

    def test_bilibili_cover_only_dispatch_never_calls_media_download(self) -> None:
        with patch(
            "services.task_runner.bilibili.read_bilibili_login_context",
            return_value={"cookie": "", "logged_in": False, "vip": False},
        ), patch(
            "services.task_runner.bilibili.download_cover_only",
            return_value={"kind": "cover", "cover": {"status": "ok"}},
        ) as download_only, patch(
            "services.task_runner.bilibili.download_video",
        ) as download_media, tempfile.TemporaryDirectory() as temp_name:
            report = run_task(
                TaskOptions(
                    platform="Bilibili",
                    feature="视频媒体",
                    inputs=["https://www.bilibili.com/video/BV16cNEeXEer/"],
                    output_root=Path(temp_name),
                    cover_only=True,
                ),
                log=lambda _message: None,
            )

        self.assertEqual(report["failures"], [])
        download_only.assert_called_once()
        download_media.assert_not_called()

    def test_tiktok_cover_only_dispatch_never_calls_media_download(self) -> None:
        with patch(
            "services.task_runner.tiktok.download_cover_only",
            return_value={"kind": "cover", "cover": {"status": "ok"}},
        ) as download_only, patch(
            "services.task_runner.tiktok.download_video",
        ) as download_media, tempfile.TemporaryDirectory() as temp_name:
            report = run_task(
                TaskOptions(
                    platform="TikTok",
                    feature="视频媒体",
                    inputs=["https://www.tiktok.com/@demo/video/7648229711117569293"],
                    output_root=Path(temp_name),
                    cover_only=True,
                ),
                log=lambda _message: None,
            )

        self.assertEqual(report["failures"], [])
        download_only.assert_called_once()
        download_media.assert_not_called()

    def test_douyin_cover_only_overrides_comment_download(self) -> None:
        with patch(
            "services.task_runner.douyin.download_note",
            return_value={"kind": "cover", "cover": {"status": "ok"}},
        ) as download_only, patch(
            "services.task_runner.douyin.download_comment_images",
        ) as download_comments, tempfile.TemporaryDirectory() as temp_name:
            report = run_task(
                TaskOptions(
                    platform="抖音",
                    feature="评论区图片",
                    inputs=["https://v.douyin.com/example/"],
                    output_root=Path(temp_name),
                    cover_only=True,
                ),
                log=lambda _message: None,
            )

        self.assertEqual(report["failures"], [])
        self.assertTrue(download_only.call_args.kwargs["cover_only"])
        download_comments.assert_not_called()

    def test_xhs_cover_only_overrides_comment_download(self) -> None:
        with patch(
            "services.task_runner.xiaohongshu.download_note",
            return_value={"kind": "cover", "cover": {"status": "ok"}},
        ) as download_only, patch(
            "services.task_runner.xiaohongshu.download_comment_images",
        ) as download_comments, tempfile.TemporaryDirectory() as temp_name:
            report = run_task(
                TaskOptions(
                    platform="小红书",
                    feature="评论区图片",
                    inputs=["https://www.xiaohongshu.com/discovery/item/6a5ccd970000000014005241"],
                    output_root=Path(temp_name),
                    cover_only=True,
                ),
                log=lambda _message: None,
            )

        self.assertEqual(report["failures"], [])
        self.assertTrue(download_only.call_args.kwargs["cover_only"])
        download_comments.assert_not_called()

    def test_collection_cover_only_is_forwarded(self) -> None:
        with patch(
            "services.task_runner.download_collection",
            return_value={"items": [], "failures": [], "output_dir": "out"},
        ) as douyin_collection, tempfile.TemporaryDirectory() as temp_name:
            run_task(
                TaskOptions(
                    platform="抖音",
                    feature="收藏夹",
                    inputs=[],
                    output_root=Path(temp_name),
                    collection_id="123",
                    collection_name="收藏",
                    cover_only=True,
                ),
                log=lambda _message: None,
            )
        self.assertTrue(douyin_collection.call_args.kwargs["cover_only"])
        self.assertTrue(douyin_collection.call_args.kwargs["download_cover"])
        with patch(
            "services.task_runner.xiaohongshu.download_collection",
            return_value={"items": [], "failures": [], "output_dir": "out"},
        ) as xhs_collection, tempfile.TemporaryDirectory() as temp_name:
            run_task(
                TaskOptions(
                    platform="小红书",
                    feature="收藏作品",
                    inputs=[],
                    output_root=Path(temp_name),
                    collection_id="__all_favorites__",
                    collection_name="全部收藏作品",
                    cover_only=True,
                ),
                log=lambda _message: None,
            )
        self.assertTrue(xhs_collection.call_args.kwargs["cover_only"])
        self.assertTrue(xhs_collection.call_args.kwargs["download_cover"])

    def _run_single(
        self,
        platform: str,
        url: str,
        mocked_download,
        *,
        feature: str = "视频媒体",
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            report = run_task(
                TaskOptions(
                    platform=platform,
                    feature=feature,
                    inputs=[url],
                    output_root=Path(temp_name),
                    download_cover=True,
                ),
                log=lambda _message: None,
            )
        self.assertEqual(report["failures"], [])
        self.assertTrue(mocked_download.call_args.kwargs["download_cover"])


if __name__ == "__main__":
    unittest.main()
