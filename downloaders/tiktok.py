from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


LogFn = Callable[[str], None]

FORMAT_SELECTOR = (
    "bestvideo*[format_note!*=watermarked]+bestaudio/"
    "best[format_note!*=watermarked]/best"
)
FORMAT_SORT = ("res", "fps", "br")
MAX_FRAGMENT_WORKERS = 8
_TRAILING_PUNCTUATION = ").,;!?]}>'\"，。；！？）】》」』"
_TIKTOK_URL_RE = re.compile(
    r"https?://(?:(?:www|m)\.)?tiktok\.com/@[^/\s<>\"']+/video/\d+[^\s<>\"']*"
    r"|https?://(?:vm|vt)\.tiktok\.com/[A-Za-z0-9_-]+[^\s<>\"']*"
    r"|https?://(?:www\.)?tiktok\.com/t/[A-Za-z0-9_-]+[^\s<>\"']*",
    re.IGNORECASE,
)


class TikTokDownloadError(RuntimeError):
    """Raised when a TikTok work cannot be parsed, downloaded, or verified."""


def extract_url(text: str) -> str:
    urls = extract_urls(text)
    if not urls:
        raise TikTokDownloadError("没有识别到 TikTok 单个作品链接，请粘贴完整分享文本或链接。")
    return urls[0]


def extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in _TIKTOK_URL_RE.finditer(text):
        url = re.split(r"[，。；！？）】》」』]", match.group(0), maxsplit=1)[0]
        url = url.rstrip(_TRAILING_PUNCTUATION)
        key = video_id_from_url(url) or normalized_url_key(url)
        if key and key not in seen:
            seen.add(key)
            urls.append(url)
    return urls


def video_id_from_url(url: str) -> str:
    match = re.search(r"/video/(\d+)", urlparse(url).path, re.IGNORECASE)
    return match.group(1) if match else ""


def normalized_url_key(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    if not host.endswith("tiktok.com"):
        return ""
    return f"{host}{parsed.path.rstrip('/')}".lower()


def download_video(
    url: str,
    output_root: Path,
    log: LogFn | None = None,
    max_workers: int = 4,
) -> dict:
    raw_logger = log or (lambda _message: None)
    logger = lambda message: _emit_log(raw_logger, message)
    url = extract_url(url)
    ffmpeg = find_executable("ffmpeg")
    ffprobe = find_executable("ffprobe")
    if not ffmpeg or not ffprobe:
        raise TikTokDownloadError(
            "TikTok 最高质量视频需要 FFmpeg 处理并由 FFprobe 验证音视频。"
            "请使用已捆绑 ffmpeg 和 ffprobe 的正式版。"
        )

    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    logger("正在解析 TikTok 公开作品和可用清晰度...")
    progress = _ProgressHook(logger)

    try:
        with tempfile.TemporaryDirectory(prefix=".tiktok-", dir=output_root) as temp_name:
            temp_dir = Path(temp_name)
            options = build_ydl_options(
                temp_dir,
                ffmpeg,
                logger,
                progress,
                max_workers=max_workers,
            )
            with YoutubeDL(options) as ydl:
                info = _unwrap_info(ydl.extract_info(url, download=True))
            media_path = resolve_downloaded_path(info, temp_dir)
            probe = probe_media(media_path, ffprobe)
            if not probe["has_video"] or not probe["has_audio"]:
                missing = "视频" if not probe["has_video"] else "音频"
                raise TikTokDownloadError(f"下载结果缺少{missing}流，未将其保存为完成品。")

            logger(f"已选择格式：{format_description(info)}")
            stem = safe_filename(
                f"TikTok_{info.get('uploader') or info.get('creator') or '未知作者'}_"
                f"{info.get('title') or info.get('description') or info.get('id') or '未命名作品'}_"
                f"{info.get('id') or video_id_from_url(url)}",
                180,
            )
            suffix = media_path.suffix.lower() or ".mp4"
            target = unique_path(output_root / f"{stem}{suffix}")
            shutil.move(str(media_path), str(target))
            report = build_report(info, target, probe)
            logger(
                f"TikTok 下载完成：{report['resolution']}，"
                f"视频 {report['video_codec']}，音频 {report['audio_codec']}"
            )
            logger(str(target))
            return report
    except TikTokDownloadError:
        raise
    except DownloadError as exc:
        raise TikTokDownloadError(_friendly_download_error(exc)) from exc
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise TikTokDownloadError(f"TikTok 视频处理失败：{exc}") from exc


def build_ydl_options(
    temp_dir: Path,
    ffmpeg: str,
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
        "concurrent_fragment_downloads": max(
            1,
            min(int(max_workers or 1), MAX_FRAGMENT_WORKERS),
        ),
        "outtmpl": str(Path(temp_dir) / "media.%(ext)s"),
        "merge_output_format": "mkv",
        "ffmpeg_location": str(Path(ffmpeg).resolve().parent),
        "logger": _YtDlpLogger(logger),
        "progress_hooks": [progress],
        "quiet": True,
        "no_warnings": False,
    }


def _unwrap_info(info) -> dict:
    if not isinstance(info, dict):
        raise TikTokDownloadError("TikTok 返回了无法识别的作品信息。")
    entries = info.get("entries")
    if entries is not None:
        first = next((entry for entry in entries if isinstance(entry, dict)), None)
        if first is None:
            raise TikTokDownloadError("TikTok 作品列表为空。")
        return first
    return info


def resolve_downloaded_path(info: dict, temp_dir: Path) -> Path:
    temp_root = Path(temp_dir).resolve()
    requested = info.get("requested_downloads")
    requested = requested if isinstance(requested, list) else []
    candidates = [
        Path(str(item.get("filepath")))
        for item in requested
        if isinstance(item, dict) and item.get("filepath")
    ]
    candidates.extend(
        path
        for path in temp_root.iterdir()
        if path.is_file()
        and path.suffix.lower()
        not in {".part", ".ytdl", ".json", ".jpg", ".jpeg", ".webp"}
    )
    for candidate in reversed(candidates):
        resolved = candidate.resolve()
        if (
            resolved.is_relative_to(temp_root)
            and resolved.is_file()
            and resolved.stat().st_size > 0
        ):
            return resolved
    raise TikTokDownloadError("yt-dlp 没有生成可验证的 TikTok 视频文件。")


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
    video = next(
        (
            item
            for item in streams
            if isinstance(item, dict) and item.get("codec_type") == "video"
        ),
        {},
    )
    audio = next(
        (
            item
            for item in streams
            if isinstance(item, dict) and item.get("codec_type") == "audio"
        ),
        {},
    )
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
    requested = requested if isinstance(requested, list) else []
    format_ids = [
        str(item.get("format_id"))
        for item in requested
        if isinstance(item, dict) and item.get("format_id")
    ]
    if not format_ids and info.get("format_id"):
        format_ids = [str(info["format_id"])]
    format_note = str(info.get("format_note") or "")
    return {
        "platform": "TikTok",
        "kind": "video",
        "video_id": str(info.get("id") or ""),
        "title": str(
            info.get("title")
            or info.get("description")
            or info.get("id")
            or "未命名作品"
        ),
        "author": str(info.get("uploader") or info.get("creator") or "未知作者"),
        "webpage_url": str(
            info.get("webpage_url") or info.get("original_url") or ""
        ),
        "output_dir": str(target.parent),
        "output_path": str(target),
        "filename": target.name,
        "filesize": target.stat().st_size,
        "resolution": probe.get("resolution", "未知分辨率"),
        "fps": probe.get("fps", 0.0),
        "video_codec": probe.get("video_codec", "未知"),
        "audio_codec": probe.get("audio_codec", "未知"),
        "format_ids": format_ids,
        "format_note": format_note,
        "watermarked": "watermarked" in format_note.lower(),
        "download_engine": "yt-dlp + FFmpeg/FFprobe",
    }


def format_description(info: dict) -> str:
    width = _to_int(info.get("width"))
    height = _to_int(info.get("height"))
    resolution = f"{width}x{height}" if width and height else "未知分辨率"
    fps = _to_int(info.get("fps"))
    fps_text = f"/{fps}fps" if fps else ""
    codec = str(info.get("vcodec") or "未知视频编码")
    audio = str(info.get("acodec") or "未知音频编码")
    bitrate = info.get("tbr") or info.get("vbr")
    bitrate_text = f"，约 {float(bitrate):.0f} kbps" if bitrate else ""
    return (
        f"{info.get('format_id') or '未知'}，{resolution}{fps_text}，"
        f"{codec} + {audio}{bitrate_text}"
    )


def find_executable(name: str) -> str | None:
    executable = f"{name}.exe" if sys.platform == "win32" else name
    bundle_dir = (
        Path(str(getattr(sys, "_MEIPASS", "")))
        if getattr(sys, "_MEIPASS", "")
        else None
    )
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
    raise TikTokDownloadError(f"无法为输出文件生成唯一名称：{path.name}")


def safe_filename(value: str, limit: int = 180) -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", str(value))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned or "TikTok_未命名作品")[:limit].rstrip(" .")


def _friendly_download_error(exc: Exception) -> str:
    message = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).strip()
    lower = message.lower()
    if any(
        marker in lower
        for marker in (
            "login",
            "sign in",
            "private",
            "not available",
            "region",
            "restricted",
        )
    ):
        return (
            "当前 TikTok 作品可能需要登录、属于私密内容或受地区限制；"
            f"本阶段仅支持公开单作品：{message}"
        )
    return f"TikTok 下载失败：{message}"


def _emit_log(logger: LogFn, message: str) -> None:
    try:
        logger(message)
    except UnicodeEncodeError as exc:
        encoding = exc.encoding or "gbk"
        fallback = message.encode(encoding, errors="replace").decode(
            encoding,
            errors="replace",
        )
        try:
            logger(fallback)
        except UnicodeError:
            pass


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
            return (
                round(float(numerator) / float(denominator), 3)
                if float(denominator)
                else 0.0
            )
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
        self.logger(f"TikTok {level}：{message}")


class _ProgressHook:
    def __init__(self, logger: LogFn) -> None:
        self.logger = logger
        self._started = False
        self._last_percent = -10

    def __call__(self, status: dict) -> None:
        if str(status.get("status") or "") != "downloading":
            return
        if not self._started:
            self._started = True
            self.logger("开始下载 TikTok 最高质量视频...")
        total = _to_int(status.get("total_bytes")) or _to_int(
            status.get("total_bytes_estimate")
        )
        downloaded = _to_int(status.get("downloaded_bytes"))
        if total <= 0:
            return
        percent = min(100, int(downloaded / total * 100))
        if percent >= self._last_percent + 10 or percent >= 100:
            self._last_percent = percent
            self.logger(f"TikTok 视频下载进度：{percent}%")
