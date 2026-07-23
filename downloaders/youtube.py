from __future__ import annotations

import json
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
from yt_dlp.utils import DownloadError

LogFn = Callable[[str], None]

FORMAT_SELECTOR = "bestvideo+bestaudio/best"
FORMAT_SORT = ("res", "fps", "br")
HTTP_CHUNK_SIZE = 4 * 1024 * 1024
RANGE_RETRIES = 3
MAX_RANGE_WORKERS = 8
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
    logger("正在解析 YouTube 公开视频和可用清晰度...")
    progress = _ProgressHook(logger)

    try:
        with tempfile.TemporaryDirectory(prefix=".youtube-", dir=output_root) as temp_name:
            temp_dir = Path(temp_name)
            options = build_ydl_options(temp_dir, ffmpeg, deno, logger, progress, max_workers)
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
            target = unique_path(output_root / f"{stem}{media_path.suffix.lower() or '.mkv'}")
            shutil.move(str(media_path), str(target))
            report = build_report(info, target, probe)
            report["download_engine"] = transfer_engine
            logger(
                f"YouTube 下载完成：{report['resolution']}，"
                f"视频 {report['video_codec']}，音频 {report['audio_codec']}"
            )
            logger(str(target))
            return report
    except YouTubeDownloadError:
        raise
    except DownloadError as exc:
        raise YouTubeDownloadError(_friendly_download_error(exc)) from exc
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise YouTubeDownloadError(f"YouTube 视频处理失败：{exc}") from exc


def build_ydl_options(
    temp_dir: Path,
    ffmpeg: str,
    deno: str,
    logger: LogFn,
    progress: Callable[[dict], None],
    max_workers: int,
) -> dict:
    return {
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
    if "sign in" in lower or "login" in lower or "cookie" in lower or "age-restricted" in lower:
        return f"当前视频需要登录、年龄验证或额外账号权限；本软件目前仅支持公开可访问的 YouTube 视频：{message}"
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
