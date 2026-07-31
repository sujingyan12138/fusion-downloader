from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time


_BROWSER_EXECUTABLES = {
    "brave.exe",
    "chrome.exe",
    "chromium.exe",
    "msedge.exe",
}
_CHROMIUM_WINDOW_CLASS = "Chrome_WidgetWin_1"
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WM_CLOSE = 0x0010
_WINDOW_CALLBACK = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
_WNDENUMPROC = _WINDOW_CALLBACK(
    wintypes.BOOL,
    wintypes.HWND,
    wintypes.LPARAM,
)


@dataclass(frozen=True)
class BrowserWindow:
    handle: int
    title: str
    process_name: str


class OpenCliWindowGuard:
    """Close only the Chromium window temporarily leased by an OpenCLI task."""

    def __init__(self) -> None:
        self._before = _browser_windows()
        self._owned_handle: int | None = None

    def observe_owned_window(self) -> int | None:
        """Identify a new window or a pre-existing OpenCLI blank window in use."""
        if self._owned_handle is not None:
            return self._owned_handle

        current = _browser_windows()
        candidates: list[int] = []
        for handle, window in current.items():
            previous = self._before.get(handle)
            if previous is None:
                if not _is_opencli_blank(window.title):
                    candidates.append(handle)
                continue
            if _is_opencli_blank(previous.title) and not _is_opencli_blank(window.title):
                candidates.append(handle)

        if len(candidates) == 1:
            self._owned_handle = candidates[0]
        return self._owned_handle

    def close_released_window(self, timeout_seconds: float = 2.0) -> bool:
        """Close the identified window only after OpenCLI has reset it to blank."""
        handle = self._owned_handle
        if handle is None or os.name != "nt":
            return False

        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            window = _browser_windows().get(handle)
            if window is None:
                return True
            if _is_opencli_blank(window.title):
                if not _request_window_close(handle):
                    return False
                while time.monotonic() < deadline:
                    if handle not in _browser_windows():
                        return True
                    time.sleep(0.05)
                return handle not in _browser_windows()
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


def opencli_command() -> list[str] | None:
    """Resolve OpenCLI without opening a visible console window on Windows."""
    launcher = shutil.which("opencli.cmd") or shutil.which("opencli")
    if not launcher:
        return None
    launcher_path = Path(launcher)
    if launcher_path.suffix.lower() in {".cmd", ".ps1"}:
        node = launcher_path.parent / "node.exe"
        main = (
            launcher_path.parent
            / "node_modules"
            / "@jackwener"
            / "opencli"
            / "dist"
            / "src"
            / "main.js"
        )
        if node.is_file() and main.is_file():
            return [str(node), str(main)]
    return [launcher]


def run_opencli_json(
    args: list[str],
    *,
    cwd: str | Path,
    timeout_seconds: int = 60,
):
    """Run one OpenCLI command and decode its first JSON envelope."""
    command = opencli_command()
    if not command:
        raise RuntimeError("未找到 OpenCLI。")
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    completed = subprocess.run(
        [*command, *args],
        cwd=Path(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
        **kwargs,
    )
    if completed.returncode != 0:
        detail = re.sub(
            r"\s+",
            " ",
            completed.stderr or completed.stdout,
        ).strip()
        raise RuntimeError(
            f"OpenCLI 执行失败：{detail or f'退出码 {completed.returncode}'}"
        )
    try:
        result, _ = json.JSONDecoder().raw_decode(completed.stdout.lstrip())
        return result
    except json.JSONDecodeError as exc:
        preview = re.sub(r"\s+", " ", completed.stdout).strip()[:300]
        raise RuntimeError(f"无法解析 OpenCLI 返回值：{preview}") from exc


def _is_opencli_blank(title: str) -> bool:
    return title.strip().casefold().startswith("about:blank")


def _browser_windows() -> dict[int, BrowserWindow]:
    if os.name != "nt":
        return {}

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_windows = user32.EnumWindows
    enum_windows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    enum_windows.restype = wintypes.BOOL
    is_window_visible = user32.IsWindowVisible
    is_window_visible.argtypes = [wintypes.HWND]
    is_window_visible.restype = wintypes.BOOL
    get_window_text_length = user32.GetWindowTextLengthW
    get_window_text_length.argtypes = [wintypes.HWND]
    get_window_text_length.restype = ctypes.c_int
    get_window_text = user32.GetWindowTextW
    get_window_text.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    get_window_text.restype = ctypes.c_int
    get_class_name = user32.GetClassNameW
    get_class_name.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    get_class_name.restype = ctypes.c_int
    get_window_pid = user32.GetWindowThreadProcessId
    get_window_pid.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    get_window_pid.restype = wintypes.DWORD

    windows: dict[int, BrowserWindow] = {}

    @_WNDENUMPROC
    def collect(hwnd, _lparam) -> bool:
        if not is_window_visible(hwnd):
            return True
        class_buffer = ctypes.create_unicode_buffer(256)
        if get_class_name(hwnd, class_buffer, len(class_buffer)) <= 0:
            return True
        if class_buffer.value != _CHROMIUM_WINDOW_CLASS:
            return True

        process_id = wintypes.DWORD()
        get_window_pid(hwnd, ctypes.byref(process_id))
        process_name = _process_name(process_id.value)
        if process_name.casefold() not in _BROWSER_EXECUTABLES:
            return True

        title_buffer = ctypes.create_unicode_buffer(
            max(1, get_window_text_length(hwnd) + 1)
        )
        get_window_text(hwnd, title_buffer, len(title_buffer))
        handle = int(hwnd)
        windows[handle] = BrowserWindow(
            handle=handle,
            title=title_buffer.value,
            process_name=process_name,
        )
        return True

    enum_windows(collect, 0)
    return windows


def _process_name(process_id: int) -> str:
    if not process_id or os.name != "nt":
        return ""

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    query_image = kernel32.QueryFullProcessImageNameW
    query_image.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    query_image.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    process = open_process(
        _PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        process_id,
    )
    if not process:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not query_image(process, 0, buffer, ctypes.byref(size)):
            return ""
        return Path(buffer.value).name
    finally:
        close_handle(process)


def _request_window_close(handle: int) -> bool:
    if os.name != "nt":
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    is_window = user32.IsWindow
    is_window.argtypes = [wintypes.HWND]
    is_window.restype = wintypes.BOOL
    post_message = user32.PostMessageW
    post_message.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    post_message.restype = wintypes.BOOL
    hwnd = wintypes.HWND(handle)
    return bool(is_window(hwnd) and post_message(hwnd, _WM_CLOSE, 0, 0))
