from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

import requests
from yt_dlp import YoutubeDL
from yt_dlp.cookies import CookieLoadError
from yt_dlp.utils import DownloadError

from . import covers
from .paths import author_output_root

LogFn = Callable[[str], None]

FORMAT_SELECTOR = "bestvideo+bestaudio/best"
FORMAT_SORT = ("res", "fps", "br")
HTTP_CHUNK_SIZE = 4 * 1024 * 1024
RANGE_RETRIES = 3
MAX_RANGE_WORKERS = 8
YOUTUBE_AUTH_DIR_NAME = "YouTube登录态"
YOUTUBE_AUTH_CONFIG_NAME = "config.json"
YOUTUBE_COOKIE_FILE_NAME = "youtube_cookies.txt"
YOUTUBE_COOKIE_EXTENSION_URL = (
    "https://chromewebstore.google.com/detail/get-cookiestxt-locally/"
    "cclelndahbckbenkjhflpdbgdldlbecc"
)
YOUTUBE_COOKIE_FAQ_URL = (
    "https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp"
)
YOUTUBE_COOKIE_EXPORT_GUIDE_URL = (
    "https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies"
)
YOUTUBE_ROBOTS_URL = "https://www.youtube.com/robots.txt"
YOUTUBE_COOKIE_MAX_BYTES = 10 * 1024 * 1024
YOUTUBE_COOKIE_SEARCH_MAX_AGE_SECONDS = 48 * 60 * 60
YOUTUBE_BROWSER_AUTH_MODES = {"firefox", "chrome", "edge"}
YOUTUBE_AUTH_MODES = {"anonymous", "cookie_file", *YOUTUBE_BROWSER_AUTH_MODES}
YOUTUBE_COOKIE_DOMAINS = ("youtube.com", "google.com")
YOUTUBE_ACCOUNT_COOKIE_NAMES = {
    "APISID",
    "HSID",
    "LOGIN_INFO",
    "SAPISID",
    "SID",
    "SSID",
    "__Secure-1PAPISID",
    "__Secure-1PSID",
    "__Secure-3PAPISID",
    "__Secure-3PSID",
}
_TRAILING_PUNCTUATION = ").,;!?]}>'\"，。；！？）】》」』"
_YOUTUBE_URL_RE = re.compile(
    r"https?://(?:(?:www|m|music)\.)?youtube\.com/(?:watch\?[^\s<>\"']+|shorts/[A-Za-z0-9_-]{11}[^\s<>\"']*|live/[A-Za-z0-9_-]{11}[^\s<>\"']*)"
    r"|https?://youtu\.be/[A-Za-z0-9_-]{11}[^\s<>\"']*",
    re.IGNORECASE,
)
class YouTubeDownloadError(RuntimeError):
    """Raised when a YouTube video cannot be parsed, downloaded, or verified."""


class _ParallelDownloadUnavailable(RuntimeError):
    """Raised when the optimized HTTP Range path should fall back to yt-dlp."""


def youtube_auth_dir() -> Path:
    return app_base_dir() / YOUTUBE_AUTH_DIR_NAME


def youtube_auth_config_path() -> Path:
    return youtube_auth_dir() / YOUTUBE_AUTH_CONFIG_NAME


def youtube_cookie_file_path() -> Path:
    return youtube_auth_dir() / YOUTUBE_COOKIE_FILE_NAME


def read_youtube_auth_context() -> dict:
    config_path = youtube_auth_config_path()
    if not config_path.is_file():
        return {
            "mode": "anonymous",
            "configured": False,
            "description": "匿名模式",
        }
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {
            "mode": "invalid",
            "configured": False,
            "description": "登录配置损坏",
            "error": f"YouTube 登录配置无法读取，请在登录设置中清除后重新配置：{exc}",
        }

    mode = str(data.get("mode") or "").strip()
    if mode not in YOUTUBE_AUTH_MODES:
        return {
            "mode": "invalid",
            "configured": False,
            "description": "登录配置无效",
            "error": "YouTube 登录配置包含无法识别的模式，请清除后重新配置。",
        }
    if mode in YOUTUBE_BROWSER_AUTH_MODES:
        browser_label = {
            "firefox": "Firefox",
            "chrome": "Chrome",
            "edge": "Edge",
        }[mode]
        return {
            "mode": mode,
            "configured": True,
            "description": f"{browser_label} 登录状态",
        }
    if mode == "cookie_file":
        cookie_file = youtube_cookie_file_path()
        if not cookie_file.is_file():
            return {
                "mode": "invalid",
                "configured": False,
                "description": "Cookie 文件缺失",
                "error": "已选择 YouTube Cookie 文件模式，但本地文件不存在，请重新导入。",
            }
        return {
            "mode": mode,
            "configured": True,
            "description": "已导入 cookies.txt",
            "cookie_file": str(cookie_file),
        }
    return {
        "mode": "anonymous",
        "configured": False,
        "description": "匿名模式",
    }


def configure_firefox_auth() -> dict:
    firefox = find_firefox_browser()
    if not firefox:
        raise YouTubeDownloadError(
            "未找到 Firefox。请先安装并在 Firefox 中登录 YouTube，"
            "或改用“导入 cookies.txt”。"
        )
    _write_auth_config("firefox")
    return {
        "mode": "firefox",
        "configured": True,
        "description": "Firefox 登录状态",
        "browser_path": firefox,
    }


def configure_browser_auth(browser: str) -> dict:
    browser = str(browser or "").strip().lower()
    if browser not in YOUTUBE_BROWSER_AUTH_MODES:
        raise YouTubeDownloadError("不支持的 YouTube 浏览器登录模式。")
    if browser == "firefox":
        return configure_firefox_auth()
    _write_auth_config(browser)
    browser_label = "Chrome" if browser == "chrome" else "Edge"
    return {
        "mode": browser,
        "configured": True,
        "description": f"{browser_label} 登录状态",
    }


def open_youtube_login_in_firefox() -> dict:
    firefox = find_firefox_browser()
    if not firefox:
        raise YouTubeDownloadError(
            "未找到 Firefox。请先安装并在 Firefox 中登录 YouTube，"
            "或改用“导入 cookies.txt”。"
        )
    try:
        subprocess.Popen(  # noqa: S603 - explicit locally installed browser path.
            [firefox, "-new-window", "https://www.youtube.com/"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except OSError as exc:
        raise YouTubeDownloadError(f"无法打开 Firefox：{exc}") from exc
    _write_auth_config("firefox")
    return {
        "mode": "firefox",
        "configured": True,
        "description": "Firefox 登录状态",
        "browser_path": firefox,
    }


def find_firefox_browser() -> str | None:
    candidates = [
        shutil.which("firefox.exe" if sys.platform == "win32" else "firefox"),
        str(Path(os.environ.get("PROGRAMFILES", "")) / "Mozilla Firefox" / "firefox.exe"),
        str(Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Mozilla Firefox" / "firefox.exe"),
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Mozilla Firefox" / "firefox.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    return None


def import_youtube_cookie_file(source: Path) -> dict:
    source = Path(source)
    kept_lines, cookie_names = _read_youtube_cookie_source(source)

    auth_dir = youtube_auth_dir()
    auth_dir.mkdir(parents=True, exist_ok=True)
    target = youtube_cookie_file_path()
    temporary = target.with_suffix(".tmp")
    try:
        temporary.write_text(
            "# Netscape HTTP Cookie File\r\n"
            "# 仅供融合下载器本机 YouTube 下载使用，请勿分享。\r\n"
            + "\r\n".join(kept_lines)
            + "\r\n",
            encoding="utf-8",
            newline="",
        )
        os.replace(temporary, target)
        _write_auth_config("cookie_file")
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise YouTubeDownloadError(f"保存 YouTube Cookie 失败：{exc}") from exc

    return {
        "mode": "cookie_file",
        "configured": True,
        "description": "已导入 cookies.txt",
        "cookie_file": str(target),
        "cookie_count": len(kept_lines),
        "account_cookie_found": bool(cookie_names & YOUTUBE_ACCOUNT_COOKIE_NAMES),
    }


def auto_import_latest_youtube_cookie(
    search_dirs: list[Path] | tuple[Path, ...] | None = None,
    *,
    max_age_seconds: int = YOUTUBE_COOKIE_SEARCH_MAX_AGE_SECONDS,
    now: float | None = None,
) -> dict:
    source = find_recent_youtube_cookie_file(
        search_dirs,
        max_age_seconds=max_age_seconds,
        now=now,
    )
    context = import_youtube_cookie_file(source)
    return {
        **context,
        "source_path": str(source),
        "source_name": source.name,
    }


def find_recent_youtube_cookie_file(
    search_dirs: list[Path] | tuple[Path, ...] | None = None,
    *,
    max_age_seconds: int = YOUTUBE_COOKIE_SEARCH_MAX_AGE_SECONDS,
    now: float | None = None,
) -> Path:
    roots = list(search_dirs) if search_dirs is not None else default_youtube_cookie_search_dirs()
    current_time = time.time() if now is None else float(now)
    valid_account_files: list[tuple[float, Path]] = []
    guest_cookie_files = 0

    for root in _unique_existing_directories(roots):
        try:
            candidates = list(root.iterdir())
        except OSError:
            continue
        for source in candidates:
            if source.suffix.lower() != ".txt":
                continue
            try:
                stat = source.stat()
            except OSError:
                continue
            if not source.is_file() or stat.st_size <= 0 or stat.st_size > YOUTUBE_COOKIE_MAX_BYTES:
                continue
            if current_time - stat.st_mtime > max(0, int(max_age_seconds)):
                continue
            try:
                _kept_lines, cookie_names = _read_youtube_cookie_source(source)
            except YouTubeDownloadError:
                continue
            if cookie_names & YOUTUBE_ACCOUNT_COOKIE_NAMES:
                valid_account_files.append((stat.st_mtime, source))
            else:
                guest_cookie_files += 1

    if valid_account_files:
        return max(valid_account_files, key=lambda item: item[0])[1]
    if guest_cookie_files:
        raise YouTubeDownloadError(
            "在桌面或下载目录找到了 YouTube Cookie 文件，但其中没有账号登录凭据。"
            "请确认无痕窗口已经登录 YouTube，再按教程重新导出；也可以手动选择文件。"
        )
    raise YouTubeDownloadError(
        "没有在桌面或下载目录找到最近 48 小时导出的有效 YouTube Cookie。"
        "请先按教程导出，保存后再点击自动导入；也可以改用“手动选择 cookies.txt”。"
    )


def default_youtube_cookie_search_dirs() -> list[Path]:
    candidates: list[Path] = []
    user_profile = Path(os.environ.get("USERPROFILE") or Path.home())
    candidates.extend((user_profile / "Desktop", user_profile / "Downloads"))

    one_drive = os.environ.get("OneDrive")
    if one_drive:
        candidates.extend((Path(one_drive) / "Desktop", Path(one_drive) / "Downloads"))

    if sys.platform == "win32":
        try:
            import winreg

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                for value_name in (
                    "Desktop",
                    "{374DE290-123F-4565-9164-39C4925E467B}",
                ):
                    try:
                        raw_value, _value_type = winreg.QueryValueEx(key, value_name)
                    except OSError:
                        continue
                    if raw_value:
                        candidates.append(Path(os.path.expandvars(str(raw_value))))
        except OSError:
            pass

    return _unique_existing_directories(candidates)


def _unique_existing_directories(paths: list[Path] | tuple[Path, ...]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for raw_path in paths:
        try:
            path = Path(raw_path).expanduser()
            identity = os.path.normcase(str(path.resolve()))
        except (OSError, RuntimeError):
            continue
        if identity in seen or not path.is_dir():
            continue
        seen.add(identity)
        result.append(path)
    return result


def _read_youtube_cookie_source(source: Path) -> tuple[list[str], set[str]]:
    source = Path(source)
    if not source.is_file():
        raise YouTubeDownloadError("选择的 cookies.txt 文件不存在。")
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise YouTubeDownloadError(f"无法检查 cookies.txt：{exc}") from exc
    if size <= 0:
        raise YouTubeDownloadError("选择的 cookies.txt 是空文件，请重新导出。")
    if size > YOUTUBE_COOKIE_MAX_BYTES:
        raise YouTubeDownloadError("cookies.txt 超过 10 MiB，已拒绝导入。")
    try:
        text = source.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise YouTubeDownloadError(f"无法读取 cookies.txt：{exc}") from exc

    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if first_line not in {"# Netscape HTTP Cookie File", "# HTTP Cookie File"}:
        raise YouTubeDownloadError(
            "Cookie 文件不是 Netscape/Mozilla 格式。"
            "第一行应为“# Netscape HTTP Cookie File”。"
        )

    kept_lines: list[str] = []
    cookie_names: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip("\r\n")
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        data_line = line.removeprefix("#HttpOnly_")
        parts = data_line.split("\t")
        if len(parts) != 7:
            continue
        domain = parts[0].lstrip(".").lower()
        if not any(
            domain == allowed or domain.endswith(f".{allowed}")
            for allowed in YOUTUBE_COOKIE_DOMAINS
        ):
            continue
        kept_lines.append(line)
        cookie_names.add(parts[5])

    if not kept_lines:
        raise YouTubeDownloadError(
            "Cookie 文件中没有找到 youtube.com 或 google.com 的 Cookie，"
            "请在已登录 YouTube 的浏览器中重新导出。"
        )
    return kept_lines, cookie_names


def clear_youtube_auth() -> None:
    youtube_auth_config_path().unlink(missing_ok=True)
    youtube_cookie_file_path().unlink(missing_ok=True)
    auth_dir = youtube_auth_dir()
    try:
        auth_dir.rmdir()
    except OSError:
        pass


def inspect_youtube_auth_context() -> dict:
    context = read_youtube_auth_context()
    if context.get("mode") in {"anonymous", "invalid"}:
        return context
    try:
        options = {
            "quiet": True,
            "no_warnings": True,
            "logger": _YtDlpLogger(lambda _message: None),
        }
        options.update(_auth_ydl_options(context))
        with YoutubeDL(options) as ydl:
            cookies = [
                cookie
                for cookie in ydl.cookiejar
                if _is_youtube_cookie_domain(str(cookie.domain or ""))
            ]
    except (CookieLoadError, DownloadError, OSError, ValueError) as exc:
        return {
            **context,
            "configured": False,
            "error": _friendly_auth_error(exc, context),
        }
    names = {str(cookie.name or "") for cookie in cookies}
    return {
        **context,
        "configured": bool(cookies),
        "cookie_count": len(cookies),
        "account_cookie_found": bool(names & YOUTUBE_ACCOUNT_COOKIE_NAMES),
    }


def _write_auth_config(mode: str) -> None:
    if mode not in YOUTUBE_AUTH_MODES:
        raise ValueError(f"Unsupported YouTube auth mode: {mode}")
    auth_dir = youtube_auth_dir()
    auth_dir.mkdir(parents=True, exist_ok=True)
    target = youtube_auth_config_path()
    temporary = target.with_suffix(".tmp")
    try:
        temporary.write_text(
            json.dumps({"mode": mode}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _is_youtube_cookie_domain(domain: str) -> bool:
    normalized = domain.lstrip(".").lower()
    return any(
        normalized == allowed or normalized.endswith(f".{allowed}")
        for allowed in YOUTUBE_COOKIE_DOMAINS
    )


def _auth_ydl_options(context: dict | None) -> dict:
    context = context or {"mode": "anonymous"}
    if context.get("error"):
        raise YouTubeDownloadError(str(context["error"]))
    mode = str(context.get("mode") or "anonymous")
    if mode == "anonymous":
        return {}
    if mode in YOUTUBE_BROWSER_AUTH_MODES:
        return {"cookiesfrombrowser": (mode, None, None, None)}
    if mode == "cookie_file":
        cookie_file = str(context.get("cookie_file") or youtube_cookie_file_path())
        if not Path(cookie_file).is_file():
            raise YouTubeDownloadError("YouTube Cookie 文件不存在，请重新导入。")
        return {"cookiefile": cookie_file}
    raise YouTubeDownloadError("YouTube 登录配置无效，请清除后重新配置。")


def _friendly_auth_error(exc: Exception, context: dict) -> str:
    source = {
        "firefox": "Firefox 登录状态",
        "chrome": "Chrome 登录状态",
        "edge": "Edge 登录状态",
        "cookie_file": "Cookie 文件",
    }.get(str(context.get("mode") or ""), "登录状态")
    browser_help = (
        "如果使用 Chrome/Edge，请先完全关闭该浏览器再检查；"
        "若仍失败，说明 Windows 加密不允许直接读取，请改为导入 cookies.txt。"
        if context.get("mode") in {"chrome", "edge"}
        else "如果使用 Firefox，请完成登录后关闭 Firefox再检查；"
    )
    return (
        f"无法读取 YouTube {source}：{exc}。"
        f"{browser_help}也可以切回匿名模式。"
    )


def extract_url(text: str) -> str:
    urls = extract_urls(text)
    if not urls:
        raise YouTubeDownloadError("没有识别到 YouTube 视频链接，请粘贴完整分享文本或链接。")
    return urls[0]


def extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in _YOUTUBE_URL_RE.finditer(text):
        url = re.split(r"[，。；！？）】》」』]", match.group(0), maxsplit=1)[0]
        url = url.rstrip(_TRAILING_PUNCTUATION)
        video_id = video_id_from_url(url)
        if video_id and video_id not in seen:
            seen.add(video_id)
            urls.append(url)
    return urls


def video_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    if host.endswith("youtu.be"):
        candidate = parsed.path.strip("/").split("/", 1)[0]
    elif host.endswith("youtube.com"):
        if parsed.path.rstrip("/") == "/watch":
            candidate = (parse_qs(parsed.query).get("v") or [""])[0]
        else:
            parts = [part for part in parsed.path.split("/") if part]
            candidate = parts[1] if len(parts) >= 2 and parts[0] in {"shorts", "live"} else ""
    else:
        return ""
    return candidate if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate) else ""


def download_video(
    url: str,
    output_root: Path,
    log: LogFn | None = None,
    max_workers: int = 4,
    auth_context: dict | None = None,
    download_cover: bool = False,
    organize_by_author: bool = False,
) -> dict:
    logger = log or (lambda _message: None)
    url = extract_url(url)
    ffmpeg = find_executable("ffmpeg")
    ffprobe = find_executable("ffprobe")
    deno = find_executable("deno")
    if not ffmpeg or not ffprobe:
        raise YouTubeDownloadError(
            "YouTube 最高质量视频需要 FFmpeg 合并并验证音视频。"
            "请使用已捆绑 ffmpeg 和 ffprobe 的正式版。"
        )
    if not deno:
        raise YouTubeDownloadError(
            "缺少 YouTube 解析所需的 Deno JavaScript 运行时。"
            "请使用已捆绑 Deno 的正式版，避免最高质量格式缺失。"
        )

    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    auth_context = auth_context or read_youtube_auth_context()
    logger("正在解析 YouTube 公开视频和可用清晰度...")
    progress = _ProgressHook(logger)

    try:
        with tempfile.TemporaryDirectory(prefix=".youtube-", dir=output_root) as temp_name:
            temp_dir = Path(temp_name)
            options = build_ydl_options(
                temp_dir,
                ffmpeg,
                deno,
                logger,
                progress,
                max_workers,
                auth_context=auth_context,
            )
            parse_started = time.monotonic()
            with YoutubeDL(options) as ydl:
                info = _unwrap_info(ydl.extract_info(url, download=False))
                logger(f"YouTube 解析完成，耗时 {time.monotonic() - parse_started:.1f} 秒。")
                try:
                    media_path = download_selected_media(
                        info,
                        temp_dir,
                        ffmpeg,
                        logger,
                        max_workers=max_workers,
                    )
                    transfer_engine = "yt-dlp 解析 + 并行 Range + FFmpeg"
                except _ParallelDownloadUnavailable as exc:
                    logger(f"并行下载不可用，自动切换到稳定模式：{exc}")
                    info = _unwrap_info(ydl.extract_info(url, download=True))
                    media_path = resolve_downloaded_path(info, temp_dir)
                    transfer_engine = "yt-dlp 稳定分段 + Deno + FFmpeg"
            probe = probe_media(media_path, ffprobe)
            if not probe["has_video"] or not probe["has_audio"]:
                missing = "视频" if not probe["has_video"] else "音频"
                raise YouTubeDownloadError(f"下载结果缺少{missing}流，未将其保存为完成品。")

            if transfer_engine.startswith("yt-dlp 稳定"):
                requested = info.get("requested_formats")
                requested = requested if isinstance(requested, list) else [info]
                for item in requested:
                    if isinstance(item, dict):
                        logger(f"已选择格式：{format_description(item)}")

            stem = safe_filename(
                f"YouTube_{info.get('uploader') or info.get('channel') or '未知作者'}_"
                f"{info.get('title') or info.get('id') or '未命名视频'}_{info.get('id') or ''}",
                180,
            )
            author = str(info.get("uploader") or info.get("channel") or "未知作者")
            item_root = author_output_root(output_root, author, organize_by_author)
            target = unique_path(item_root / f"{stem}{media_path.suffix.lower() or '.mkv'}")
            shutil.move(str(media_path), str(target))
            report = build_report(info, target, probe)
            report["download_engine"] = transfer_engine
            report["auth_mode"] = str(auth_context.get("mode") or "anonymous")
            if download_cover:
                try:
                    report["cover"] = download_cover_from_info(info, item_root, logger)
                except covers.CoverDownloadError as exc:
                    report["cover_error"] = str(exc)
                    logger(f"YouTube 封面获取失败：{exc}")
            logger(
                f"YouTube 下载完成：{report['resolution']}，"
                f"视频 {report['video_codec']}，音频 {report['audio_codec']}"
            )
            logger(str(target))
            return report
    except YouTubeDownloadError:
        raise
    except (CookieLoadError, DownloadError) as exc:
        raise YouTubeDownloadError(_friendly_download_error(exc)) from exc
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise YouTubeDownloadError(f"YouTube 视频处理失败：{exc}") from exc


def download_cover_only(
    url: str,
    output_root: Path,
    log: LogFn | None = None,
    auth_context: dict | None = None,
    organize_by_author: bool = False,
) -> dict:
    logger = log or (lambda _message: None)
    url = extract_url(url)
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    auth_context = auth_context or read_youtube_auth_context()
    options = {
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 30,
        "retries": 3,
        "logger": _YtDlpLogger(logger),
        "quiet": True,
        "no_warnings": False,
    }
    deno = find_executable("deno")
    if deno:
        options["js_runtimes"] = {"deno": {"path": str(Path(deno).resolve())}}
    options.update(_auth_ydl_options(auth_context))
    logger("正在解析 YouTube 作品封面，不下载视频媒体...")
    try:
        with YoutubeDL(options) as ydl:
            info = _unwrap_info(ydl.extract_info(url, download=False))
        author = str(info.get("uploader") or info.get("channel") or "未知作者")
        item_root = author_output_root(output_root, author, organize_by_author)
        cover = download_cover_from_info(info, item_root, logger)
        report = covers.build_cover_only_report(
            "YouTube",
            str(info.get("id") or ""),
            str(info.get("title") or info.get("id") or "未命名视频"),
            author,
            str(info.get("webpage_url") or info.get("original_url") or url),
            cover,
        )
        report["auth_mode"] = str(auth_context.get("mode") or "anonymous")
        logger(f"YouTube 仅封面任务完成：{cover.get('resolution', '未知分辨率')}")
        return report
    except YouTubeDownloadError:
        raise
    except covers.CoverDownloadError as exc:
        raise YouTubeDownloadError(f"YouTube 封面获取失败：{exc}") from exc
    except (CookieLoadError, DownloadError) as exc:
        raise YouTubeDownloadError(_friendly_download_error(exc)) from exc
    except (OSError, ValueError) as exc:
        raise YouTubeDownloadError(f"YouTube 封面处理失败：{exc}") from exc


def build_ydl_options(
    temp_dir: Path,
    ffmpeg: str,
    deno: str,
    logger: LogFn,
    progress: Callable[[dict], None],
    max_workers: int,
    auth_context: dict | None = None,
) -> dict:
    options = {
        "format": FORMAT_SELECTOR,
        "format_sort": list(FORMAT_SORT),
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "http_chunk_size": HTTP_CHUNK_SIZE,
        "concurrent_fragment_downloads": max(1, min(int(max_workers or 1), 8)),
        "outtmpl": str(Path(temp_dir) / "media.%(ext)s"),
        "merge_output_format": "mkv",
        "ffmpeg_location": str(Path(ffmpeg).resolve().parent),
        "js_runtimes": {"deno": {"path": str(Path(deno).resolve())}},
        "logger": _YtDlpLogger(logger),
        "progress_hooks": [progress],
        "quiet": True,
        "no_warnings": False,
    }
    options.update(_auth_ydl_options(auth_context))
    return options


def _unwrap_info(info) -> dict:
    if not isinstance(info, dict):
        raise YouTubeDownloadError("YouTube 返回了无法识别的视频信息。")
    entries = info.get("entries")
    if entries is not None:
        first = next((entry for entry in entries if isinstance(entry, dict)), None)
        if first is None:
            raise YouTubeDownloadError("YouTube 视频列表为空。")
        return first
    return info


def download_selected_media(
    info: dict,
    temp_dir: Path,
    ffmpeg: str,
    logger: LogFn,
    max_workers: int = 4,
) -> Path:
    requested = info.get("requested_formats")
    requested = requested if isinstance(requested, list) else []
    video = next(
        (
            item
            for item in requested
            if isinstance(item, dict)
            and item.get("vcodec") not in (None, "none")
            and item.get("acodec") in (None, "none")
        ),
        None,
    )
    audio = next(
        (
            item
            for item in requested
            if isinstance(item, dict)
            and item.get("acodec") not in (None, "none")
            and item.get("vcodec") in (None, "none")
        ),
        None,
    )

    if video and audio:
        logger(f"已选择格式：{format_description(video)}")
        logger(f"已选择格式：{format_description(audio)}")
        video_path = temp_dir / f"video.{safe_extension(video.get('ext'), 'webm')}"
        audio_path = temp_dir / f"audio.{safe_extension(audio.get('ext'), 'm4a')}"
        output = temp_dir / "media.mkv"
        try:
            download_parallel_stream(video, video_path, "视频", logger, max_workers=max_workers)
            download_parallel_stream(audio, audio_path, "音频", logger, max_workers=max_workers)
            merge_started = time.monotonic()
            merge_streams(video_path, audio_path, output, ffmpeg)
            logger(f"音视频无重编码合并完成，耗时 {time.monotonic() - merge_started:.1f} 秒。")
        except (_ParallelDownloadUnavailable, OSError, subprocess.SubprocessError) as exc:
            for path in (video_path, audio_path, output):
                path.unlink(missing_ok=True)
            if isinstance(exc, _ParallelDownloadUnavailable):
                raise
            raise _ParallelDownloadUnavailable(f"并行下载后的音视频合并失败：{exc}") from exc
        return output

    combined = requested[0] if len(requested) == 1 and isinstance(requested[0], dict) else info
    if combined.get("vcodec") in (None, "none") or combined.get("acodec") in (None, "none"):
        raise _ParallelDownloadUnavailable("解析结果不是可直接下载的 HTTP 音视频流。")
    logger(f"已选择格式：{format_description(combined)}")
    output = temp_dir / f"media.{safe_extension(combined.get('ext'), 'mp4')}"
    download_parallel_stream(combined, output, "媒体", logger, max_workers=max_workers)
    return output


def download_parallel_stream(
    format_info: dict,
    target: Path,
    label: str,
    logger: LogFn,
    max_workers: int = 4,
) -> None:
    url = str(format_info.get("url") or "")
    protocol = str(format_info.get("protocol") or "https").lower()
    if not url.startswith(("http://", "https://")) or protocol not in {"http", "https", "http_dash_segments"}:
        raise _ParallelDownloadUnavailable(f"{label}流不是可并行下载的普通 HTTP 地址。")
    if format_info.get("fragments"):
        raise _ParallelDownloadUnavailable(f"{label}流使用站点分片清单。")

    total = _to_int(format_info.get("filesize"))
    if total <= 0:
        raise _ParallelDownloadUnavailable(f"{label}流缺少可验证的精确文件大小。")
    workers = max(1, min(_to_int(max_workers) or 1, MAX_RANGE_WORKERS))
    headers = media_request_headers(format_info)
    ranges = build_ranges(total, HTTP_CHUNK_SIZE)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    with target.open("wb") as output:
        output.truncate(total)

    logger(f"开始并行下载{label}流：{workers} 路连接，4 MiB 分段。")
    started = time.monotonic()
    completed = 0
    last_percent = -10
    executor = ThreadPoolExecutor(max_workers=workers)
    futures = {
        executor.submit(_download_range_chunk, url, headers, start, end): (start, end)
        for start, end in ranges
    }
    try:
        with target.open("r+b") as output:
            for future in as_completed(futures):
                start, data = future.result()
                output.seek(start)
                output.write(data)
                completed += len(data)
                percent = min(100, int(completed / total * 100))
                if percent >= last_percent + 10 or completed >= total:
                    last_percent = percent
                    elapsed = max(time.monotonic() - started, 0.001)
                    speed = completed / 1024 / 1024 / elapsed
                    logger(f"{label}流下载进度：{percent}%（{speed:.2f} MiB/s）")
    except Exception as exc:
        for future in futures:
            future.cancel()
        target.unlink(missing_ok=True)
        if isinstance(exc, _ParallelDownloadUnavailable):
            raise
        raise _ParallelDownloadUnavailable(f"{label}流并行分段失败：{exc}") from exc
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    actual = target.stat().st_size if target.is_file() else 0
    if completed != total or actual != total:
        target.unlink(missing_ok=True)
        raise _ParallelDownloadUnavailable(
            f"{label}流大小校验失败：预期 {total}，实际写入 {completed}，文件 {actual}。"
        )
    elapsed = max(time.monotonic() - started, 0.001)
    logger(f"{label}流下载完成：{total / 1024 / 1024:.1f} MiB，平均 {total / 1024 / 1024 / elapsed:.2f} MiB/s。")


def _download_range_chunk(
    url: str,
    headers: dict[str, str],
    start: int,
    end: int,
) -> tuple[int, bytes]:
    expected = end - start + 1
    last_error: Exception | None = None
    for attempt in range(RANGE_RETRIES + 1):
        request_headers = dict(headers)
        request_headers["Range"] = f"bytes={start}-{end}"
        try:
            with requests.get(url, headers=request_headers, stream=True, timeout=(10, 30)) as response:
                if response.status_code != 206:
                    raise requests.HTTPError(f"HTTP {response.status_code}", response=response)
                range_start, range_end, _range_total = parse_content_range(
                    response.headers.get("Content-Range", "")
                )
                if range_start != start or range_end != end:
                    raise requests.ConnectionError(
                        f"Content-Range 不匹配：预期 {start}-{end}，实际 {range_start}-{range_end}"
                    )
                data = b"".join(chunk for chunk in response.iter_content(256 * 1024) if chunk)
                if len(data) != expected:
                    raise requests.ConnectionError(
                        f"分段长度不完整：预期 {expected}，实际 {len(data)}"
                    )
                return start, data
        except requests.RequestException as exc:
            last_error = exc
            if attempt < RANGE_RETRIES:
                time.sleep(attempt + 1)
    raise _ParallelDownloadUnavailable(
        f"Range {start}-{end} 已重试 {RANGE_RETRIES} 次：{last_error}"
    ) from last_error


def build_ranges(total: int, chunk_size: int = HTTP_CHUNK_SIZE) -> list[tuple[int, int]]:
    if total <= 0 or chunk_size <= 0:
        return []
    return [
        (start, min(total - 1, start + chunk_size - 1))
        for start in range(0, total, chunk_size)
    ]


def parse_content_range(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", str(value).strip(), re.IGNORECASE)
    if not match:
        return -1, -1, 0
    total = 0 if match.group(3) == "*" else int(match.group(3))
    return int(match.group(1)), int(match.group(2)), total


def media_request_headers(format_info: dict) -> dict[str, str]:
    headers = {
        str(key): str(value)
        for key, value in dict(format_info.get("http_headers") or {}).items()
        if str(key).lower() != "cookie"
    }
    headers["Accept-Encoding"] = "identity"
    return headers


def merge_streams(video: Path, audio: Path, output: Path, ffmpeg: str) -> None:
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video),
            "-i",
            str(audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c",
            "copy",
            str(output),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=True,
    )


def safe_extension(value, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", str(value or "").lower())
    return cleaned or fallback


def resolve_downloaded_path(info: dict, temp_dir: Path) -> Path:
    temp_root = Path(temp_dir).resolve()
    requested = info.get("requested_downloads")
    requested = requested if isinstance(requested, list) else []
    candidates = [Path(str(item.get("filepath"))) for item in requested if isinstance(item, dict) and item.get("filepath")]
    candidates.extend(
        path
        for path in temp_root.iterdir()
        if path.is_file() and path.suffix.lower() not in {".part", ".ytdl", ".json", ".jpg", ".jpeg", ".webp"}
    )
    for candidate in reversed(candidates):
        resolved = candidate.resolve()
        if resolved.is_relative_to(temp_root) and resolved.is_file() and resolved.stat().st_size > 0:
            return resolved
    raise YouTubeDownloadError("yt-dlp 没有生成可验证的 YouTube 视频文件。")


def probe_media(path: Path, ffprobe: str) -> dict:
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,r_frame_rate",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=True,
    )
    return summarize_streams(json.loads(result.stdout or "{}"))


def summarize_streams(payload: dict) -> dict:
    streams = payload.get("streams") if isinstance(payload, dict) else []
    streams = streams if isinstance(streams, list) else []
    video = next((item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"), {})
    width = _to_int(video.get("width"))
    height = _to_int(video.get("height"))
    return {
        "has_video": bool(video),
        "has_audio": bool(audio),
        "width": width,
        "height": height,
        "resolution": f"{width}x{height}" if width and height else "未知分辨率",
        "fps": _parse_rate(video.get("r_frame_rate")),
        "video_codec": str(video.get("codec_name") or "未知"),
        "audio_codec": str(audio.get("codec_name") or "未知"),
    }


def build_report(info: dict, target: Path, probe: dict) -> dict:
    requested = info.get("requested_formats")
    requested = requested if isinstance(requested, list) else [info]
    format_ids = [str(item.get("format_id")) for item in requested if isinstance(item, dict) and item.get("format_id")]
    return {
        "platform": "YouTube",
        "kind": "video",
        "video_id": str(info.get("id") or ""),
        "title": str(info.get("title") or info.get("id") or "未命名视频"),
        "author": str(info.get("uploader") or info.get("channel") or "未知作者"),
        "webpage_url": str(info.get("webpage_url") or info.get("original_url") or ""),
        "output_dir": str(target.parent),
        "output_path": str(target),
        "filename": target.name,
        "filesize": target.stat().st_size,
        "resolution": probe.get("resolution", "未知分辨率"),
        "fps": probe.get("fps", 0.0),
        "video_codec": probe.get("video_codec", "未知"),
        "audio_codec": probe.get("audio_codec", "未知"),
        "format_ids": format_ids,
        "download_engine": "yt-dlp + Deno + FFmpeg",
    }


def youtube_cover_candidates(info: dict) -> list[covers.CoverCandidate]:
    video_id = str(info.get("id") or "")
    extra: list[covers.CoverCandidate] = []
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        extra = [
            covers.CoverCandidate(
                f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
                "youtube:maxresdefault",
                1280,
                720,
                100,
            ),
            covers.CoverCandidate(
                f"https://i.ytimg.com/vi_webp/{video_id}/maxresdefault.webp",
                "youtube:maxresdefault_webp",
                1280,
                720,
                99,
            ),
            covers.CoverCandidate(
                f"https://i.ytimg.com/vi/{video_id}/hq720.jpg",
                "youtube:hq720",
                1280,
                720,
                90,
            ),
            covers.CoverCandidate(
                f"https://i.ytimg.com/vi/{video_id}/sddefault.jpg",
                "youtube:sddefault",
                640,
                480,
                80,
            ),
            covers.CoverCandidate(
                f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                "youtube:hqdefault",
                480,
                360,
                70,
            ),
        ]
    return covers.info_cover_candidates(info, source_prefix="youtube", extra=extra)


def download_cover_from_info(
    info: dict,
    output_root: str | Path,
    logger: LogFn | None = None,
) -> dict:
    video_id = str(info.get("id") or "")
    stem = safe_filename(
        f"YouTube_{info.get('uploader') or info.get('channel') or '未知作者'}_"
        f"{info.get('title') or video_id or '未命名视频'}_{video_id}",
        150,
    )
    referer = str(
        info.get("webpage_url")
        or info.get("original_url")
        or (f"https://www.youtube.com/watch?v={video_id}" if video_id else "")
    )
    return covers.download_best_cover(
        youtube_cover_candidates(info),
        output_root,
        stem,
        referer=referer,
        log=logger,
    )


def format_description(format_info: dict) -> str:
    width = _to_int(format_info.get("width"))
    height = _to_int(format_info.get("height"))
    resolution = f"{width}x{height}" if width and height else "纯音频"
    fps = _to_int(format_info.get("fps"))
    fps_text = f"/{fps}fps" if fps else ""
    codec = str(
        format_info.get("vcodec")
        if format_info.get("vcodec") not in (None, "none")
        else format_info.get("acodec") or "未知编码"
    )
    bitrate = format_info.get("tbr") or format_info.get("abr")
    bitrate_text = f"，约 {float(bitrate):.0f} kbps" if bitrate else ""
    return f"格式 {format_info.get('format_id') or '未知'}，{resolution}{fps_text}，{codec}{bitrate_text}"


def find_executable(name: str) -> str | None:
    executable = f"{name}.exe" if sys.platform == "win32" else name
    bundle_dir = Path(str(getattr(sys, "_MEIPASS", ""))) if getattr(sys, "_MEIPASS", "") else None
    candidates = [
        shutil.which(executable),
        str(Path(sys.executable).resolve().parent / executable),
        str(app_base_dir() / executable),
        str(bundle_dir / executable) if bundle_dir else None,
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    return None


def app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 10_000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise YouTubeDownloadError(f"无法为输出文件生成唯一名称：{path.name}")


def safe_filename(value: str, limit: int = 180) -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", str(value))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned or "YouTube_未命名视频")[:limit].rstrip(" .")


def _friendly_download_error(exc: Exception) -> str:
    message = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).strip()
    lower = message.lower()
    if "failed to load cookies" in lower or "could not copy" in lower and "cookie" in lower:
        return (
            "YouTube 登录状态读取失败。请先完全关闭所选浏览器再重试；"
            "如果 Chrome/Edge 仍因 Windows 加密无法读取，请改用 Firefox 或导入 cookies.txt："
            f"{message}"
        )
    if "http error 429" in lower or "too many requests" in lower:
        return (
            "YouTube 当前网络/IP触发了临时限流（HTTP 429）。"
            "请停止重复尝试，关闭代理或 VPN、等待一段时间或切换网络后再试；"
            f"登录状态也不能保证解除已经发生的 IP 限制：{message}"
        )
    if "not a bot" in lower or "confirm you" in lower and "bot" in lower:
        return (
            "YouTube 要求确认当前请求不是机器人。"
            "可在“YouTube 登录设置”中读取现有浏览器登录状态或导入 cookies.txt；"
            "如果已经使用登录状态仍出现此提示，请停止重复请求并等待或切换网络。"
            f"原始信息：{message}"
        )
    if (
        "age-restricted" in lower
        or "confirm your age" in lower
        or "private video" in lower
        or "members-only" in lower
        or "join this channel" in lower
        or "sign in" in lower
        or "login" in lower
    ):
        return (
            "当前视频需要账号登录、年龄验证或额外内容权限。"
            "请配置有效的 YouTube 登录状态；会员或私享内容仍以该账号实际权限为准："
            f"{message}"
        )
    return f"YouTube 下载失败：{message}"


def _to_int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _parse_rate(value) -> float:
    text = str(value or "0")
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            return round(float(numerator) / float(denominator), 3) if float(denominator) else 0.0
        return round(float(text), 3)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


class _YtDlpLogger:
    def __init__(self, logger: LogFn) -> None:
        self.logger = logger
        self._seen: set[tuple[str, str]] = set()

    def debug(self, _message: str) -> None:
        pass

    def info(self, _message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        self._emit("提示", message)

    def error(self, message: str) -> None:
        self._emit("错误", message)

    def _emit(self, level: str, message: str) -> None:
        message = message.strip()
        key = (level, message)
        if not message or key in self._seen:
            return
        self._seen.add(key)
        self.logger(f"YouTube {level}：{message}")


class _ProgressHook:
    def __init__(self, logger: LogFn) -> None:
        self.logger = logger
        self._started = False
        self._last_percent: dict[str, int] = {}

    def __call__(self, status: dict) -> None:
        state = str(status.get("status") or "")
        if state == "downloading":
            if not self._started:
                self._started = True
                self.logger("开始下载 YouTube 最高质量音视频流...")
            info = status.get("info_dict") if isinstance(status.get("info_dict"), dict) else {}
            label = _stream_label(info)
            filename = str(status.get("filename") or label)
            total = _to_int(status.get("total_bytes")) or _to_int(status.get("total_bytes_estimate"))
            downloaded = _to_int(status.get("downloaded_bytes"))
            if total <= 0:
                return
            percent = min(100, int(downloaded / total * 100))
            previous = self._last_percent.get(filename, -10)
            if percent >= previous + 10 or percent >= 100:
                self._last_percent[filename] = percent
                self.logger(f"{label}流下载进度：{percent}%")


def _stream_label(info: dict) -> str:
    has_video = info.get("vcodec") not in (None, "none")
    has_audio = info.get("acodec") not in (None, "none")
    if has_video and not has_audio:
        return "视频"
    if has_audio and not has_video:
        return "音频"
    return "媒体"
