from __future__ import annotations

from contextlib import nullcontext
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from services import updater


class FakeResponse:
    def __init__(self, *, json_data=None, content: bytes = b"", status_code: int = 200) -> None:
        self._json_data = json_data
        self._content = content
        self.status_code = status_code

    def json(self):
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise updater.requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int):
        for index in range(0, len(self._content), chunk_size):
            yield self._content[index:index + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


class VersionTests(unittest.TestCase):
    def test_release_tags_are_compared_numerically(self) -> None:
        self.assertEqual(updater.normalized_version("v2.1"), "2.1")
        self.assertGreater(updater.compare_versions("2.10", "2.9.9"), 0)
        self.assertEqual(updater.compare_versions("2.1", "2.1.0"), 0)


class ReleaseTests(unittest.TestCase):
    def test_selects_release_exe_and_github_digest(self) -> None:
        asset = updater.select_release_asset(
            [
                {
                    "name": "Fusion.Downloader.exe",
                    "browser_download_url": "https://github.com/demo/repo/releases/download/v2.2/Fusion.Downloader.exe",
                    "size": 123,
                    "digest": "sha256:" + "a" * 64,
                }
            ]
        )

        self.assertIsNotNone(asset)
        self.assertEqual(asset.sha256, "a" * 64)

    @patch("services.updater.bypass_unreachable_loopback_proxy", return_value=nullcontext())
    @patch("services.updater.requests.Session")
    def test_check_for_update_parses_latest_release(self, session_type, _bypass) -> None:
        session = session_type.return_value
        session.get.return_value = FakeResponse(
            json_data={
                "tag_name": "v2.3.0",
                "name": "Fusion Downloader 2.3",
                "body": "修复解析",
                "html_url": "https://github.com/demo/repo/releases/tag/v2.3.0",
                "published_at": "2026-08-01T00:00:00Z",
                "draft": False,
                "prerelease": False,
                "assets": [
                    {
                        "name": "Fusion.Downloader.exe",
                        "browser_download_url": "https://github.com/demo/repo/releases/download/v2.3.0/Fusion.Downloader.exe",
                        "size": 321,
                        "digest": "sha256:" + "b" * 64,
                    }
                ],
            }
        )

        info = updater.check_for_update("2.2.0")

        self.assertTrue(info.update_available)
        self.assertEqual(info.latest_version, "2.3.0")
        self.assertEqual(info.asset.sha256, "b" * 64)


class DownloadTests(unittest.TestCase):
    @patch("services.updater.bypass_unreachable_loopback_proxy", return_value=nullcontext())
    @patch("services.updater.requests.Session")
    def test_download_is_size_and_sha256_verified(self, session_type, _bypass) -> None:
        content = b"verified update bytes"
        digest = hashlib.sha256(content).hexdigest()
        session_type.return_value.get.return_value = FakeResponse(content=content)
        info = updater.UpdateInfo(
            current_version="2.2.0",
            latest_version="2.3.0",
            title="v2.3.0",
            notes="",
            release_url="https://github.com/demo/repo/releases/tag/v2.3.0",
            published_at="",
            asset=updater.ReleaseAsset(
                name="Fusion.Downloader.exe",
                download_url="https://github.com/demo/repo/releases/download/v2.3.0/Fusion.Downloader.exe",
                size=len(content),
                sha256=digest,
            ),
        )
        progress: list[tuple[int, int]] = []
        with tempfile.TemporaryDirectory() as temp_name:
            path = updater.download_update(info, Path(temp_name), progress=lambda done, total: progress.append((done, total)))

            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(updater.sha256_file(path), digest)
            self.assertFalse(path.with_suffix(".exe.part").exists())
        self.assertEqual(progress[-1], (len(content), len(content)))

    @patch("services.updater.bypass_unreachable_loopback_proxy", return_value=nullcontext())
    @patch("services.updater.requests.Session")
    def test_bad_digest_is_deleted_and_not_installed(self, session_type, _bypass) -> None:
        content = b"tampered"
        session_type.return_value.get.return_value = FakeResponse(content=content)
        info = updater.UpdateInfo(
            current_version="2.2.0",
            latest_version="2.3.0",
            title="v2.3.0",
            notes="",
            release_url="https://github.com/demo/repo/releases/tag/v2.3.0",
            published_at="",
            asset=updater.ReleaseAsset(
                name="Fusion.Downloader.exe",
                download_url="https://github.com/demo/repo/releases/download/v2.3.0/Fusion.Downloader.exe",
                size=len(content),
                sha256="0" * 64,
            ),
        )
        with tempfile.TemporaryDirectory() as temp_name:
            update_dir = Path(temp_name) / "更新临时文件"
            with self.assertRaisesRegex(updater.UpdateError, "SHA-256"):
                updater.download_update(info, Path(temp_name))
            self.assertEqual(list(update_dir.glob("*.part")), [])


class HelperLaunchTests(unittest.TestCase):
    @patch("services.updater.subprocess.Popen")
    def test_helper_is_copied_beside_update_before_launch(self, popen) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            app_dir = Path(temp_name)
            current = app_dir / "Fusion Downloader.exe"
            current.write_bytes(b"old")
            update_dir = app_dir / "更新临时文件"
            update_dir.mkdir()
            staged = update_dir / "Fusion.Downloader-v2.3.0.exe"
            staged.write_bytes(b"new")
            helper_source = app_dir / "source-helper.ps1"
            helper_source.write_text("exit 0", encoding="utf-8")

            updater.launch_update_helper(
                staged,
                helper_source,
                "2.3.0",
                current_version="2.2.0",
                current_executable=current,
            )

            helper_copy = update_dir / "fusion_update_helper.ps1"
            self.assertEqual(helper_copy.read_text(encoding="utf-8"), "exit 0")
            command = popen.call_args.args[0]
            self.assertIn(str(helper_copy), command)
            self.assertIn(str(current.resolve()), command)


if __name__ == "__main__":
    unittest.main()
