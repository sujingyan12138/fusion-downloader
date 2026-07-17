from __future__ import annotations

import http.cookiejar
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from . import douyin


LogFn = Callable[[str], None]

_BILIBILI_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?bilibili\.com/video/(?:BV[A-Za-z0-9]+|av\d+)[^\s<>\"']*"
    r"|https?://b23\.tv/[A-Za-z0-9]+[^\s<>\"']*",
    re.IGNORECASE,
)
_TRAILING_PUNCTUATION = ").,;!?]}>'\"，。；！？）】》」』"
_RANGE_CHUNK_SIZE = 4 * 1024 * 1024
BILIBILI_LOGIN_URL = "https://passport.bilibili.com/login"
BILIBILI_HOME_URL = "https://www.bilibili.com/"
FORMAT_SELECTOR = "bestvideo+bestaudio/best"
FORMAT_SORT = ("res", "fps", "br")


class BilibiliDownloadError(RuntimeError):
    """Raised when a Bilibili video cannot be parsed, downloaded, or verified."""


def extract_url(text: str) -> str:
    urls = extract_urls(text)
    if not urls:
        raise BilibiliDownloadError("没有识别到 Bilibili 视频链接，请粘贴完整分享文本或链接。")
    return urls[0]


def extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in _BILIBILI_URL_RE.finditer(text):
        url = re.split(r"[，。；！？）】》」』]", match.group(0), maxsplit=1)[0]
        url = url.rstrip(_TRAILING_PUNCTUATION)
        key = _url_key(url)
        if key and key not in seen:
            seen.add(key)
            urls.append(url)
    return urls


def _url_key(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.").removeprefix("m.")
    path = parsed.path.rstrip("/")
    return f"{host}{path}".lower()


def download_video(
    url: str,
    output_root: Path,
    log: LogFn | None = None,
    max_workers: int = 4,
    cookie_header: str = "",
) -> dict:
    logger = log or (lambda _message: None)
    url = extract_url(url)
    ffmpeg = find_executable("ffmpeg")
    ffprobe = find_executable("ffprobe")
    if not ffmpeg or not ffprobe:
        raise BilibiliDownloadError(
            "Bilibili 最高质量视频需要 FFmpeg 合并并验证音视频。"
            "请安装 ffmpeg 和 ffprobe，或使用已捆绑这两个工具的正式版。"
        )

    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    logger("正在解析 Bilibili 视频和可用清晰度...")

    try:
        with tempfile.TemporaryDirectory(prefix=".bilibili-", dir=output_root) as temp_name:
            temp_dir = Path(temp_name)
            options = {
                "format": FORMAT_SELECTOR,
                "format_sort": list(FORMAT_SORT),
                "noplaylist": True,
                "socket_timeout": 30,
                "retries": 3,
                "logger": _YtDlpLogger(logger),
                "quiet": True,
                "no_warnings": False,
            }
            with YoutubeDL(options) as ydl:
                load_bilibili_cookies(ydl, cookie_header)
                info = ydl.extract_info(url, download=False)
                info = _unwrap_info(info)
                attach_backup_urls(info, ydl, logger, cookie_header=cookie_header)
            media_path = download_selected_media(info, temp_dir, ffmpeg, logger, max_workers=max_workers)
            probe = probe_media(media_path, ffprobe)
            if not probe["has_video"] or not probe["has_audio"]:
                missing = "视频" if not probe["has_video"] else "音频"
                raise BilibiliDownloadError(f"下载结果缺少{missing}流，未将其保存为完成品。")

            target = unique_path(output_root / media_path.name)
            shutil.move(str(media_path), str(target))
            report = build_report(info, target, probe)
            report["authenticated"] = bool(cookie_header)
            logger(
                f"Bilibili 下载完成：{report['resolution']}，"
                f"视频 {report['video_codec']}，音频 {report['audio_codec']}"
            )
            logger(str(target))
            return report
    except BilibiliDownloadError:
        raise
    except DownloadError as exc:
        raise BilibiliDownloadError(_friendly_download_error(exc)) from exc
    except (OSError, requests.RequestException, subprocess.SubprocessError, ValueError) as exc:
        raise BilibiliDownloadError(f"Bilibili 视频处理失败：{exc}") from exc


def _unwrap_info(info) -> dict:
    if not isinstance(info, dict):
        raise BilibiliDownloadError("Bilibili 返回了无法识别的视频信息。")
    entries = info.get("entries")
    if entries is not None:
        first = next((entry for entry in entries if isinstance(entry, dict)), None)
        if first is None:
            raise BilibiliDownloadError("Bilibili 视频列表为空。")
        return first
    return info


def attach_backup_urls(info: dict, ydl: YoutubeDL, logger: LogFn, cookie_header: str = "") -> None:
    webpage_url = str(info.get("webpage_url") or info.get("original_url") or "")
    bvid_match = re.search(r"/video/(BV[A-Za-z0-9]+)", webpage_url, re.IGNORECASE)
    if not webpage_url or not bvid_match:
        return
    headers = {"User-Agent": "Mozilla/5.0", "Referer": webpage_url}
    if cookie_header:
        headers["Cookie"] = cookie_header
    try:
        page = requests.get(webpage_url, headers=headers, timeout=(10, 30)).text
        cid = extract_cid(page)
        if not cid:
            return
        extractor = ydl.get_info_extractor("BiliBili")
        extractor.initialize()
        play_info = extractor._download_playinfo(  # noqa: SLF001 - yt-dlp omits Bilibili backup URLs from public formats.
            bvid_match.group(1), cid, headers={"Referer": webpage_url}, query={"try_look": 1}, fatal=False
        )
        if not isinstance(play_info, dict):
            return
        requested = info.get("requested_formats")
        requested = requested if isinstance(requested, list) else [info]
        dash = play_info.get("dash") if isinstance(play_info.get("dash"), dict) else {}
        stream_items = list(dash.get("video") or []) + list(dash.get("audio") or [])
        attached = 0
        for selected in requested:
            if not isinstance(selected, dict):
                continue
            format_id = str(selected.get("format_id") or "")
            for item in stream_items:
                if not isinstance(item, dict):
                    continue
                urls = stream_item_urls(item)
                if not urls or not format_matches(format_id, selected, item, urls):
                    continue
                selected["_download_urls"] = unique_urls([str(selected.get("url") or ""), *urls])
                attached += max(0, len(selected["_download_urls"]) - 1)
                break
        if attached:
            logger(f"已读取 Bilibili 备用媒体节点：{attached} 个。")
    except Exception as exc:  # noqa: BLE001 - backup enrichment is optional; primary URL remains usable.
        logger(f"Bilibili 备用节点读取失败，将使用主节点：{exc}")


def extract_cid(page_html: str) -> int:
    match = re.search(r"window\.__INITIAL_STATE__\s*=\s*", page_html)
    if not match:
        return 0
    try:
        state = json.JSONDecoder().raw_decode(page_html[match.end() :])[0]
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0
    video_data = state.get("videoData") if isinstance(state, dict) else {}
    return _to_int(video_data.get("cid") if isinstance(video_data, dict) else 0)


def stream_item_urls(item: dict) -> list[str]:
    base = item.get("baseUrl") or item.get("base_url") or item.get("url")
    backups = item.get("backupUrl") or item.get("backup_url") or []
    backups = backups if isinstance(backups, list) else []
    return unique_urls([str(base or ""), *(str(url or "") for url in backups)])


def format_matches(format_id: str, selected: dict, item: dict, urls: list[str]) -> bool:
    if format_id and any(format_id in url for url in urls):
        return True
    if format_id and format_id == str(item.get("id") or ""):
        return True
    selected_codec = str(selected.get("vcodec") or selected.get("acodec") or "").lower()
    item_codec = str(item.get("codecs") or "").lower()
    return bool(selected_codec and item_codec and selected_codec == item_codec)


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
        (item for item in requested if isinstance(item, dict) and item.get("vcodec") not in (None, "none")),
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
    stem = safe_filename(f"Bilibili_{info.get('title') or info.get('id') or '未命名视频'}_{info.get('id') or ''}", 180)

    if video and audio:
        logger(f"已选择视频：{format_description(video)}")
        logger(f"已选择音频：{format_description(audio)}")
        video_path = temp_dir / f"video.{safe_extension(video.get('ext'), 'mp4')}"
        audio_path = temp_dir / f"audio.{safe_extension(audio.get('ext'), 'm4a')}"
        download_stream(video, video_path, "视频", logger, max_workers=max_workers)
        download_stream(audio, audio_path, "音频", logger, max_workers=max_workers)
        container = ".mp4" if video_path.suffix.lower() == ".mp4" and audio_path.suffix.lower() in {".m4a", ".mp4"} else ".mkv"
        output = temp_dir / f"{stem}{container}"
        merge_streams(video_path, audio_path, output, ffmpeg)
        return output

    combined = info
    if combined.get("vcodec") in (None, "none") or combined.get("acodec") in (None, "none"):
        raise BilibiliDownloadError("没有找到可同时组成视频和音频的最高质量格式。")
    logger(f"已选择单文件格式：{format_description(combined)}")
    output = temp_dir / f"{stem}.{safe_extension(combined.get('ext'), 'mp4')}"
    download_stream(combined, output, "媒体", logger, max_workers=max_workers)
    return output


def download_stream(
    format_info: dict,
    target: Path,
    label: str,
    logger: LogFn,
    max_workers: int = 4,
) -> None:
    del max_workers  # Reserved for a future bounded parallel range implementation.
    url = str(format_info.get("url") or "")
    if not url.startswith(("http://", "https://")):
        raise BilibiliDownloadError(f"{label}流没有可下载的 HTTP 地址。")
    urls = unique_urls([url, *(format_info.get("_download_urls") or [])])
    headers = media_request_headers(format_info)
    url = choose_working_url(urls, headers, label, logger)
    active_url_index = urls.index(url)
    total = _to_int(format_info.get("filesize")) or _to_int(format_info.get("filesize_approx"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    position = 0
    last_percent = -10
    logger(f"开始下载{label}流...")

    with requests.Session() as session:
        while total <= 0 or position < total:
            requested_end = position + _RANGE_CHUNK_SIZE - 1
            if total > 0:
                requested_end = min(requested_end, total - 1)
            chunk_start = position
            for attempt in range(1, 5):
                request_url = urls[(active_url_index + attempt - 1) % len(urls)]
                request_headers = dict(headers)
                request_headers["Range"] = f"bytes={chunk_start}-{requested_end}"
                try:
                    with session.get(request_url, headers=request_headers, stream=True, timeout=(10, 30)) as response:
                        if response.status_code not in (200, 206):
                            raise BilibiliDownloadError(f"{label}流返回 HTTP {response.status_code}。")
                        range_start, range_end, range_total = parse_content_range(response.headers.get("Content-Range", ""))
                        if response.status_code == 206 and range_start != chunk_start:
                            raise BilibiliDownloadError(
                                f"{label}流返回了错误的分段起点：预期 {chunk_start}，实际 {range_start}。"
                            )
                        if range_total > 0:
                            total = range_total
                            requested_end = min(requested_end, total - 1)
                        elif total <= 0:
                            total = _to_int(response.headers.get("Content-Length"))
                        mode = "ab" if chunk_start else "wb"
                        written = 0
                        with target.open(mode) as output:
                            for data in response.iter_content(128 * 1024):
                                if data:
                                    output.write(data)
                                    written += len(data)
                        expected = (range_end - range_start + 1) if range_end >= range_start >= 0 else written
                        if written != expected:
                            raise requests.ConnectionError(f"分段长度不完整：预期 {expected}，实际 {written}")
                        position = chunk_start + written
                        active_url_index = urls.index(request_url)
                        break
                except (requests.RequestException, BilibiliDownloadError) as exc:
                    with target.open("ab") as output:
                        output.truncate(chunk_start)
                    if attempt >= 4:
                        raise BilibiliDownloadError(f"{label}流分段下载失败（已重试 3 次）：{exc}") from exc
                    logger(f"{label}流连接中断，正在重试 {attempt}/3...")
                    time.sleep(attempt)
            if total > 0:
                percent = min(100, int(position / total * 100))
                if percent >= last_percent + 10 or position >= total:
                    last_percent = percent
                    logger(f"{label}流下载进度：{percent}%")
            if position <= chunk_start:
                raise BilibiliDownloadError(f"{label}流下载没有取得进展。")

    if total > 0 and target.stat().st_size != total:
        raise BilibiliDownloadError(f"{label}流大小校验失败：预期 {total}，实际 {target.stat().st_size}。")


def choose_working_url(urls: list[str], headers: dict[str, str], label: str, logger: LogFn) -> str:
    successes: list[tuple[float, str]] = []
    for candidate in urls:
        request_headers = dict(headers)
        request_headers["Range"] = "bytes=0-65535"
        started = time.monotonic()
        try:
            with requests.get(candidate, headers=request_headers, stream=True, timeout=(5, 15)) as response:
                if response.status_code != 206:
                    continue
                received = sum(len(data) for data in response.iter_content(64 * 1024) if data)
                if received == 65_536:
                    successes.append((time.monotonic() - started, candidate))
        except requests.RequestException:
            continue
    if not successes:
        logger(f"{label}流节点预检未返回数据，将由正式下载重试。")
        return urls[0]
    elapsed, selected = min(successes, key=lambda item: item[0])
    logger(f"{label}流节点已就绪，首段响应约 {elapsed:.1f} 秒。")
    return selected


def media_request_headers(format_info: dict) -> dict[str, str]:
    headers = {str(key): str(value) for key, value in dict(format_info.get("http_headers") or {}).items()}
    headers["Accept-Encoding"] = "identity"
    headers.pop("Cookie", None)
    return headers


def unique_urls(urls) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in urls:
        url = str(value or "")
        if url.startswith(("http://", "https://")) and url not in seen:
            seen.add(url)
            output.append(url)
    return output


def parse_content_range(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", str(value).strip(), re.IGNORECASE)
    if not match:
        return -1, -1, 0
    return int(match.group(1)), int(match.group(2)), 0 if match.group(3) == "*" else int(match.group(3))


def merge_streams(video_path: Path, audio_path: Path, output: Path, ffmpeg: str) -> None:
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
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
        check=False,
    )
    if result.returncode != 0 or not output.is_file():
        detail = (result.stderr or result.stdout or "未知错误").strip()
        raise BilibiliDownloadError(f"FFmpeg 合并音视频失败：{detail}")


def format_description(format_info: dict) -> str:
    width = _to_int(format_info.get("width"))
    height = _to_int(format_info.get("height"))
    resolution = f"{width}x{height}" if width and height else "纯音频"
    codec = str(
        format_info.get("vcodec")
        if format_info.get("vcodec") not in (None, "none")
        else format_info.get("acodec") or "未知编码"
    )
    bitrate = format_info.get("tbr") or format_info.get("abr")
    bitrate_text = f"，约 {float(bitrate):.0f} kbps" if bitrate else ""
    return f"格式 {format_info.get('format_id') or '未知'}，{resolution}，{codec}{bitrate_text}"


def safe_extension(value, fallback: str) -> str:
    extension = re.sub(r"[^A-Za-z0-9]", "", str(value or "").lower())
    return extension or fallback


def safe_filename(value: str, limit: int = 180) -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", str(value))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned or "Bilibili_未命名视频")[:limit].rstrip(" .")


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
    if not isinstance(requested, list):
        requested = [info]
    format_ids = [str(item.get("format_id")) for item in requested if isinstance(item, dict) and item.get("format_id")]
    return {
        "platform": "Bilibili",
        "kind": "video",
        "video_id": str(info.get("id") or ""),
        "title": str(info.get("title") or info.get("id") or "未命名视频"),
        "author": str(info.get("uploader") or info.get("uploader_id") or "未知作者"),
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
        "download_engine": "yt-dlp + FFmpeg",
    }


def find_executable(name: str) -> str | None:
    executable = f"{name}.exe" if sys.platform == "win32" else name
    candidates = [
        shutil.which(executable),
        str(app_base_dir() / executable),
        str(Path(getattr(sys, "_MEIPASS", "")) / executable) if getattr(sys, "_MEIPASS", "") else None,
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    return None


def app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def bilibili_browser_profile_dir() -> Path:
    return app_base_dir() / "Bilibili浏览器登录态"


def open_bilibili_login_browser() -> Path:
    browser_path = douyin.find_chromium_browser()
    if not browser_path:
        raise BilibiliDownloadError("未找到 Chrome 或 Edge，无法打开 Bilibili 登录窗口。")
    profile_dir = bilibili_browser_profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)
    douyin.launch_chromium_cdp_browser(browser_path, profile_dir, visible=True, url=BILIBILI_LOGIN_URL)
    return profile_dir


def read_bilibili_login_context() -> dict:
    profile_dir = bilibili_browser_profile_dir()
    if not profile_dir.exists() or not (profile_dir / "Local State").is_file():
        return {"cookie": "", "logged_in": False, "vip": False}
    browser_path = douyin.find_chromium_browser()
    if not browser_path:
        raise BilibiliDownloadError("未找到 Chrome 或 Edge，无法读取 Bilibili 登录态。")
    try:
        import websocket  # type: ignore[import-not-found]
    except ImportError as exc:
        raise BilibiliDownloadError("缺少 websocket-client 依赖，无法读取 Bilibili 登录态。") from exc

    process = None
    reuse_existing = False
    cookie_header = ""
    try:
        port, process, reuse_existing = douyin.open_comment_cdp_port(browser_path, profile_dir)
        try:
            target = douyin.create_cdp_target(port)
        except Exception:
            if process is not None and not reuse_existing:
                douyin.terminate_process(process)
            process = None
            reuse_existing = False
            douyin.remove_devtools_port_file(profile_dir)
            port, process, reuse_existing = douyin.open_comment_cdp_port(
                browser_path, profile_dir, force_new=True
            )
            target = douyin.create_cdp_target(port)
        ws_url = str(target.get("webSocketDebuggerUrl") or "")
        if not ws_url:
            raise BilibiliDownloadError("Chrome DevTools 没有返回 WebSocket 地址。")
        ws = websocket.create_connection(ws_url, timeout=8)
        try:
            cdp = douyin.CdpClient(ws)
            cdp.call("Page.enable", timeout=8)
            cdp.call("Network.enable", timeout=8)
            cdp.call("Page.navigate", {"url": BILIBILI_HOME_URL}, timeout=8)
            douyin.evaluate_after_navigation(cdp, "(() => document.readyState)()", timeout=30)
            cookie_header = get_bilibili_cookie_header(cdp)
        finally:
            try:
                ws.close()
            except Exception:
                pass
    finally:
        if process is not None and not reuse_existing:
            douyin.terminate_process(process)

    context = {"cookie": cookie_header, "logged_in": "SESSDATA=" in cookie_header, "vip": False}
    if not cookie_header:
        return context
    try:
        response = requests.get(
            "https://api.bilibili.com/x/web-interface/nav",
            headers={"User-Agent": "Mozilla/5.0", "Referer": BILIBILI_HOME_URL, "Cookie": cookie_header},
            timeout=(10, 30),
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else {}
        context["logged_in"] = bool(data.get("isLogin")) or context["logged_in"]
        vip = data.get("vip") if isinstance(data.get("vip"), dict) else {}
        context["vip"] = bool(data.get("vipStatus") == 1 or vip.get("status") == 1)
    except (requests.RequestException, ValueError):
        pass
    return context


def get_bilibili_cookie_header(cdp: "douyin.CdpClient") -> str:
    try:
        result = cdp.call("Network.getAllCookies", timeout=8)
    except Exception:
        return ""
    raw_cookies = result.get("cookies") if isinstance(result.get("cookies"), list) else []
    parts: list[str] = []
    seen: set[str] = set()
    for cookie in raw_cookies:
        if not isinstance(cookie, dict) or "bilibili.com" not in str(cookie.get("domain") or ""):
            continue
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def load_bilibili_cookies(ydl: YoutubeDL, cookie_header: str) -> None:
    """Load browser cookies into yt-dlp without using a global Cookie header."""
    for part in cookie_header.split(";"):
        name, separator, value = part.strip().partition("=")
        if not separator or not name:
            continue
        ydl.cookiejar.set_cookie(
            http.cookiejar.Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=".bilibili.com",
                domain_specified=True,
                domain_initial_dot=True,
                path="/",
                path_specified=True,
                secure=True,
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": None},
                rfc2109=False,
            )
        )


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 10_000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise BilibiliDownloadError(f"无法为输出文件生成唯一名称：{path.name}")


def _friendly_download_error(exc: Exception) -> str:
    message = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).strip()
    if "login" in message.lower() or "cookie" in message.lower() or "会员" in message:
        return f"当前视频或目标清晰度需要登录/会员权限：{message}"
    return f"Bilibili 下载失败：{message}"


def _to_int(value) -> int:
    try:
        return int(value or 0)
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

    def debug(self, message: str) -> None:
        if message.startswith("[download] Destination:"):
            self.logger(message.replace("[download] ", "", 1))

    def info(self, _message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        self._emit("提示", message)

    def error(self, message: str) -> None:
        if message.lstrip().startswith("Deprecated Feature:"):
            self._emit("提示", message)
            return
        self._emit("错误", message)

    def _emit(self, level: str, message: str) -> None:
        message = message.strip()
        key = ("提示" if message.startswith("Deprecated Feature:") else level, message)
        if not message or key in self._seen:
            return
        self._seen.add(key)
        self.logger(f"Bilibili {level}：{message}")
