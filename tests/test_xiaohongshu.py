from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from downloaders import xiaohongshu


def make_box(kind: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


def make_heic(width: int, height: int) -> bytes:
    ftyp = make_box(b"ftyp", b"heic" + b"\x00\x00\x00\x00" + b"mif1" + b"heic")
    ispe = make_box(
        b"ispe",
        b"\x00\x00\x00\x00" + width.to_bytes(4, "big") + height.to_bytes(4, "big"),
    )
    return ftyp + make_box(b"meta", b"\x00\x00\x00\x00" + ispe) + b"\x00" * 32


class XiaohongshuHighestQualityTests(unittest.TestCase):
    def test_favorite_script_uses_profile_page_when_account_api_is_unavailable(self) -> None:
        script = xiaohongshu.build_favorite_video_script(limit=1)

        self.assertIn("function currentUserId()", script)
        self.assertIn("function openMyPage()", script)
        self.assertIn("currentUserId() || ''", script)
        self.assertIn("loginRequired:false, authenticated:true", script)
        self.assertNotIn("String(meRes.data && meRes.data.code) === '-101'", script)

    def test_login_probe_requires_an_account_identifier(self) -> None:
        script = xiaohongshu.build_login_context_script()

        self.assertIn("accountDetected", script)
        self.assertIn("accountIdFromPage", script)
        self.assertIn("loginRequired:!accountDetected", script)

    def test_probe_image_recognizes_full_size_heic_container(self) -> None:
        content = make_heic(4032, 3024)
        response = MagicMock()
        response.status_code = 200
        response.content = content
        response.headers = {"Content-Type": "image/heic"}
        session = MagicMock()
        session.get.return_value = response

        result = xiaohongshu.probe_image(
            session,
            xiaohongshu.Candidate("https://ci.xiaohongshu.com/raw", "ci.xiaohongshu.com:raw"),
            "https://www.xiaohongshu.com/explore/test",
            declared_width=3024,
            declared_height=4032,
        )

        self.assertEqual(result.image_format, "heic")
        self.assertEqual(result.extension, "heic")
        self.assertEqual((result.width, result.height), (3024, 4032))
        self.assertEqual(result.bytes_count, len(content))

    @patch("downloaders.xiaohongshu.probe_image")
    @patch("downloaders.xiaohongshu.candidate_urls")
    def test_choose_best_image_does_not_stop_at_first_declared_size_candidate(
        self,
        candidate_urls,
        probe_image,
    ) -> None:
        first = xiaohongshu.Candidate("https://cdn.test/declared.png", "page:default")
        second = xiaohongshu.Candidate("https://cdn.test/larger.png", "page:alternate")
        candidate_urls.return_value = [first, second]
        probe_image.side_effect = [
            xiaohongshu.ProbeResult(first, b"a" * 100, "png", "png", 100, 100, bytes_count=100),
            xiaohongshu.ProbeResult(second, b"b" * 200, "png", "png", 200, 200, bytes_count=200),
        ]

        result = xiaohongshu.choose_best_image(
            MagicMock(),
            xiaohongshu.ImageItem(index=1, declared_width=100, declared_height=100),
            "https://www.xiaohongshu.com/explore/test",
        )

        self.assertIs(result.candidate, second)
        self.assertEqual(probe_image.call_count, 2)

    @patch("downloaders.xiaohongshu.make_session")
    @patch("downloaders.xiaohongshu.choose_best_image")
    def test_download_media_task_reuses_identical_heic_without_duplicate(
        self,
        choose_best_image,
        _make_session,
    ) -> None:
        content = make_heic(4032, 3024)
        candidate = xiaohongshu.Candidate(
            "https://ci.xiaohongshu.com/raw",
            "ci.xiaohongshu.com:raw",
        )
        choose_best_image.return_value = xiaohongshu.ProbeResult(
            candidate,
            content,
            "heic",
            "heic",
            3024,
            4032,
            bytes_count=len(content),
        )
        image = xiaohongshu.ImageItem(index=1, declared_width=3024, declared_height=4032)

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            existing = root / "小红书_作者_标题_note_001_3024x4032.heic"
            existing.write_bytes(content)

            result = xiaohongshu.download_media_task(
                "image",
                1,
                image,
                root,
                "https://www.xiaohongshu.com/explore/test",
                xiaohongshu.DownloadEngine(),
                "小红书_作者_标题_note",
            )

            item = result["item"]
            self.assertEqual(item["status"], "skipped")
            self.assertTrue(item["reused"])
            self.assertEqual(Path(item["file"]), existing)
            self.assertEqual(list(root.glob("*.heic")), [existing])


if __name__ == "__main__":
    unittest.main()
