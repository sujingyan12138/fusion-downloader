from __future__ import annotations

from contextlib import nullcontext
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from downloaders import douyin
from services import network_policy
from services.cancellation import (
    DownloadCancelled,
    cancellation_requested,
    raise_if_cancelled,
    request_cancellation,
    reset_cancellation,
)
from services.task_runner import TaskOptions, run_task, run_url_batch


class NetworkPolicyTests(unittest.TestCase):
    def test_only_loopback_proxy_endpoints_are_eligible_for_bypass(self) -> None:
        self.assertEqual(
            network_policy._proxy_endpoint("http://127.0.0.1:10808"),
            ("127.0.0.1", 10808),
        )
        self.assertEqual(
            network_policy._proxy_endpoint("socks5://localhost:1080"),
            ("localhost", 1080),
        )
        self.assertIsNone(network_policy._proxy_endpoint("http://proxy.example.com:8080"))

    @patch("services.network_policy._proxy_endpoint_reachable", return_value=False)
    @patch(
        "services.network_policy.requests.utils.get_environ_proxies",
        return_value={"https": "http://127.0.0.1:10808"},
    )
    def test_dead_loopback_proxy_is_detected(self, _get_proxies, _reachable) -> None:
        self.assertEqual(
            network_policy.find_unreachable_loopback_proxy(),
            ("127.0.0.1", 10808),
        )

    @patch(
        "services.network_policy.find_unreachable_loopback_proxy",
        return_value=("127.0.0.1", 10808),
    )
    def test_bypass_is_task_scoped_and_restores_environment(self, _find_proxy) -> None:
        original_upper = os.environ.get("NO_PROXY")
        original_lower = os.environ.get("no_proxy")
        messages: list[str] = []
        try:
            with network_policy.bypass_unreachable_loopback_proxy(messages.append):
                self.assertEqual(os.environ.get("NO_PROXY"), "*")
                self.assertEqual(os.environ.get("no_proxy"), "*")
            self.assertEqual(os.environ.get("NO_PROXY"), original_upper)
            self.assertEqual(os.environ.get("no_proxy"), original_lower)
        finally:
            if original_upper is None:
                os.environ.pop("NO_PROXY", None)
            else:
                os.environ["NO_PROXY"] = original_upper
            if original_lower is None:
                os.environ.pop("no_proxy", None)
            else:
                os.environ["no_proxy"] = original_lower
        self.assertIn("本次任务已自动切换为直连", messages[0])


class CancellationTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_cancellation()

    def test_cancellation_request_is_idempotent_and_raises_typed_error(self) -> None:
        reset_cancellation()
        self.assertTrue(request_cancellation())
        self.assertFalse(request_cancellation())
        self.assertTrue(cancellation_requested())
        with self.assertRaisesRegex(DownloadCancelled, "用户已终止下载"):
            raise_if_cancelled()

    def test_sequential_url_batch_stops_before_next_item(self) -> None:
        reset_cancellation()
        visited: list[str] = []

        def worker(index: int, url: str, _workers: int) -> dict:
            visited.append(url)
            if index == 1:
                request_cancellation()
            return {"index": index}

        with self.assertRaises(DownloadCancelled):
            run_url_batch(["first", "second"], 1, 1, worker)
        self.assertEqual(visited, ["first"])

    def test_run_task_propagates_cancellation_before_dispatch(self) -> None:
        reset_cancellation()
        request_cancellation()
        options = TaskOptions(
            platform="TikTok",
            feature="视频媒体",
            inputs=["https://www.tiktok.com/@demo/video/123"],
            output_root=Path(tempfile.gettempdir()),
        )
        with patch("services.task_runner.bypass_unreachable_loopback_proxy", return_value=nullcontext()):
            with patch("services.task_runner.tiktok.download_video") as download_video:
                with self.assertRaises(DownloadCancelled):
                    run_task(options, lambda _message: None)
        download_video.assert_not_called()

    def test_douyin_partial_file_is_removed_when_cancelled(self) -> None:
        class FakeResponse:
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def iter_content(self, chunk_size: int):
                self.chunk_size = chunk_size
                yield b"a" * 64
                request_cancellation()
                yield b"b" * 64

        class FakeSession:
            def get(self, *_args, **_kwargs):
                return FakeResponse()

        reset_cancellation()
        with tempfile.TemporaryDirectory() as temp_name:
            target = Path(temp_name) / "partial.mp4"
            with self.assertRaises(DownloadCancelled):
                douyin.download_binary(FakeSession(), "https://cdn.example/media", target, "")
            self.assertFalse(target.exists())


class GuiCancellationTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_cancellation()

    def test_primary_button_becomes_cancel_action_while_running(self) -> None:
        class AliveWorker:
            @staticmethod
            def is_alive() -> bool:
                return True

        root = app.UnifiedDownloaderApp()
        root.withdraw()
        try:
            root.worker = AliveWorker()  # type: ignore[assignment]
            reset_cancellation()
            root._set_buttons_state("disabled")
            self.assertEqual(root.start_button.cget("state"), "normal")
            self.assertEqual(root.start_button.cget("text"), "终止下载")

            root.cancel_current_task()
            self.assertTrue(cancellation_requested())
            self.assertEqual(root.start_button.cget("state"), "disabled")
            self.assertEqual(root.start_button.cget("text"), "正在终止…")

            root._set_buttons_state("normal")
            self.assertEqual(root.start_button.cget("state"), "normal")
            self.assertEqual(root.start_button.cget("text"), "开始下载")
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
