from __future__ import annotations

import threading


class DownloadCancelled(RuntimeError):
    """Raised when the user asks the active download task to stop."""


_CANCEL_EVENT = threading.Event()


def reset_cancellation() -> None:
    _CANCEL_EVENT.clear()


def request_cancellation() -> bool:
    """Request cancellation and return True only for the first request."""
    already_requested = _CANCEL_EVENT.is_set()
    _CANCEL_EVENT.set()
    return not already_requested


def cancellation_requested() -> bool:
    return _CANCEL_EVENT.is_set()


def raise_if_cancelled() -> None:
    if _CANCEL_EVENT.is_set():
        raise DownloadCancelled("用户已终止下载。")
