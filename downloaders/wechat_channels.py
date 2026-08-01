from __future__ import annotations

from dataclasses import dataclass
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import requests

from downloaders import youtube
from downloaders.article_common import safe_filename, unique_path
from downloaders.paths import author_output_root
from services.cancellation import DownloadCancelled, raise_if_cancelled


LogFn = Callable[[str], None]
PROFILE_API = "https://sph.litao.workers.dev/api/fetch_video_profile"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)
MEDIA_HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": "https://channels.weixin.qq.com/",
    "Accept-Encoding": "identity",
}
URL_RE = re.compile(
    r"https?://weixin\.qq\.com/sph/[A-Za-z0-9_-]+[^\s<>'\"]*"
    r"|https?://channels\.weixin\.qq\.com/finder-preview/pages/sph\?[^\s<>'\"]+",
    re.IGNORECASE,
)
_TRAILING_PUNCTUATION = "。，、；;！!）)]}】》」』"


class WechatChannelsDownloadError(RuntimeError):
    """Raised when a WeChat Channels video cannot be parsed or verified."""


@dataclass(frozen=True)
class ChannelsCandidate:
    url: str
    label: str
    preference: int


@dataclass(frozen=True)
class ChannelsProbe:
    candidate: ChannelsCandidate
    content_length: int
    content_type: str


@dataclass(frozen=True)
class ChannelsProfile:
    source_url: str
    content_id: str
    title: str
    author: str
    candidates: tuple[ChannelsCandidate, ...]


def extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in URL_RE.finditer(text or ""):
        raw = match.group(0).rstrip(_TRAILING_PUNCTUATION)
        try:
            canonical = canonicalize_url(raw)
        except ValueError:
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        urls.append(canonical)
    return urls


def canonicalize_url(url: str) -> str:
    content_id = content_id_from_url(url)
    if not content_id:
        raise ValueError("这不是可识别的微信视频号分享链接。")
    return f"https://weixin.qq.com/sph/{content_id}"


def content_id_from_url(url: str) -> str:
    parts = urlsplit(str(url or "").strip())
    host = (parts.hostname or "").lower()
    if host == "weixin.qq.com":
        match = re.fullmatch(r"/sph/([A-Za-z0-9_-]+)/*", parts.path)
        return match.group(1) if match else ""
    if host == "channels.weixin.qq.com" and parts.path.rstrip("/") == "/finder-preview/pages/sph":
        value = (parse_qs(parts.query).get("id") or [""])[0]
        return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else ""
    return ""


def is_channels_url(url: str) -> bool:
    return bool(content_id_from_url(url))


def fetch_profile(
    url: str,
    *,
    session: requests.Session | None = None,
) -> ChannelsProfile:
    canonical = canonicalize_url(url)
    client = session or requests
    try:
        response = client.post(
            PROFILE_API,
            json={"url": canonical},
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=(8, 45),
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise WechatChannelsDownloadError(
            "视频号解析服务暂时不可用。该功能使用参考项目提供的公开解析服务，"
            "没有修改系统代理或安装证书；请稍后重试。"
        ) from exc
    return profile_from_payload(payload, canonical)


def profile_from_payload(payload: object, source_url: str) -> ChannelsProfile:
    if not isinstance(payload, dict):
        raise WechatChannelsDownloadError("视频号解析服务返回了无法识别的数据。")
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    feed = data.get("feedInfo")
    author_info = data.get("authorInfo")
    feed = feed if isinstance(feed, dict) else {}
    author_info = author_info if isinstance(author_info, dict) else {}

    h264_info = feed.get("h264VideoInfo")
    h265_info = feed.get("h265VideoInfo")
    h264_info = h264_info if isinstance(h264_info, dict) else {}
    h265_info = h265_info if isinstance(h265_info, dict) else {}
    h264_url = _http_url(h264_info.get("videoUrl"))
    h265_url = _http_url(h265_info.get("videoUrl"))
    default_url = _http_url(feed.get("videoUrl"))
    original_url = derive_original_url(h264_url or default_url or h265_url)

    raw_candidates = (
        ChannelsCandidate(original_url, "原始质量", 30),
        ChannelsCandidate(h264_url, "H.264 在线版", 20),
        ChannelsCandidate(h265_url, "H.265 在线版", 10),
        ChannelsCandidate(default_url, "默认在线版", 5),
    )
    candidates: list[ChannelsCandidate] = []
    seen: set[str] = set()
    for candidate in raw_candidates:
        if not candidate.url or candidate.url in seen:
            continue
        seen.add(candidate.url)
        candidates.append(candidate)
    if not candidates:
        error = payload.get("error") or feed.get("errMsg") or "没有返回视频地址"
        raise WechatChannelsDownloadError(f"视频号作品没有可下载的视频流：{error}")

    title = _clean_text(feed.get("description")) or f"视频号作品_{content_id_from_url(source_url)}"
    author = _clean_text(author_info.get("nickname")) or "未知作者"
    return ChannelsProfile(
        source_url=canonicalize_url(source_url),
        content_id=content_id_from_url(source_url),
        title=title,
        author=author,
        candidates=tuple(candidates),
    )


def derive_original_url(url: str) -> str:
    parts = urlsplit(str(url or ""))
    if parts.scheme not in {"http", "https"} or not (parts.hostname or "").endswith("video.qq.com"):
        return ""
    query = parse_qs(parts.query)
    encfilekey = (query.get("encfilekey") or [""])[0]
    token = (query.get("token") or [""])[0]
    if not encfilekey or not token:
        return ""
    return urlunsplit(
        (
            "https",
            parts.netloc,
            parts.path,
            urlencode((("encfilekey", encfilekey), ("token", token))),
            "",
        )
    )


def select_best_candidate(
    profile: ChannelsProfile,
    *,
    session: requests.Session | None = None,
    log: LogFn | None = None,
) -> ChannelsProbe:
    logger = log or (lambda _message: None)
    client = session or requests
    probes: list[ChannelsProbe] = []
    errors: list[str] = []
    for candidate in profile.candidates:
        try:
            probe = probe_candidate(candidate, session=client)
            probes.append(probe)
            size_text = (
                f"{probe.content_length / 1024 / 1024:.1f} MiB"
                if probe.content_length > 0
                else "大小未知"
            )
            logger(f"视频号候选：{candidate.label}，{size_text}")
        except WechatChannelsDownloadError as exc:
            errors.append(f"{candidate.label}：{exc}")
    if not probes:
        detail = "；".join(errors[-3:])
        raise WechatChannelsDownloadError(
            "视频号解析成功，但所有媒体地址都已失效。"
            + (f" 详情：{detail}" if detail else "")
        )

    original = next(
        (probe for probe in probes if probe.candidate.label == "原始质量"),
        None,
    )
    if original is not None:
        return original
    return max(
        probes,
        key=lambda probe: (
            probe.content_length,
            probe.candidate.preference,
        ),
    )


def probe_candidate(
    candidate: ChannelsCandidate,
    *,
    session: requests.Session | None = None,
) -> ChannelsProbe:
    raise_if_cancelled()
    client = session or requests
    try:
        response = client.head(
            candidate.url,
            headers=MEDIA_HEADERS,
            allow_redirects=True,
            timeout=(8, 30),
        )
        response.raise_for_status()
        content_type = str(response.headers.get("Content-Type") or "").lower()
        content_length = _positive_int(response.headers.get("Content-Length"))
        if content_length <= 0:
            response.close()
            response = client.get(
                candidate.url,
                headers={**MEDIA_HEADERS, "Range": "bytes=0-0"},
                stream=True,
                allow_redirects=True,
                timeout=(8, 30),
            )
            response.raise_for_status()
            content_type = str(response.headers.get("Content-Type") or "").lower()
            content_length = _content_range_total(
                response.headers.get("Content-Range")
            ) or _positive_int(response.headers.get("Content-Length"))
        if "video" not in content_type and "octet-stream" not in content_type:
            raise WechatChannelsDownloadError(
                f"媒体响应类型异常：{content_type or '未知'}"
            )
        return ChannelsProbe(
            candidate=candidate,
            content_length=content_length,
            content_type=content_type,
        )
    except requests.RequestException as exc:
        raise WechatChannelsDownloadError(f"媒体地址不可访问：{exc}") from exc
    finally:
        if "response" in locals():
            response.close()


def download_video(
    url: str,
    output_root: str | Path,
    *,
    max_workers: int = 4,
    organize_by_author: bool = True,
    log: LogFn | None = None,
) -> dict:
    logger = log or (lambda _message: None)
    raise_if_cancelled()
    canonical = canonicalize_url(url)
    ffprobe = youtube.find_executable("ffprobe")
    if not ffprobe:
        raise WechatChannelsDownloadError(
            "视频号下载结果必须由 FFprobe 验证音视频轨，请使用已捆绑 FFprobe 的正式版。"
        )

    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    logger("正在通过公开解析服务读取视频号作品信息...")
    with requests.Session() as client:
        profile = fetch_profile(canonical, session=client)
        item_root = author_output_root(root, profile.author, organize_by_author)
        existing = find_existing_video(item_root, profile.content_id, ffprobe)
        if existing is not None:
            path, media = existing
            logger(f"已存在并通过音视频验证，跳过：{path}")
            return build_report(
                profile,
                path,
                media,
                "已存在",
                path.stat().st_size,
                "skipped",
            )
        selected = select_best_candidate(profile, session=client, log=logger)

    selected_size = selected.content_length
    logger(
        f"已选择视频号{selected.candidate.label}"
        + (
            f"：{selected_size / 1024 / 1024:.1f} MiB"
            if selected_size > 0
            else ""
        )
    )

    try:
        with tempfile.TemporaryDirectory(prefix=".wechat-channel-", dir=root) as temp_name:
            temp_path = Path(temp_name) / "media.mp4"
            format_info = {
                "url": selected.candidate.url,
                "protocol": "https",
                "filesize": selected_size,
                "http_headers": MEDIA_HEADERS,
            }
            try:
                youtube.download_parallel_stream(
                    format_info,
                    temp_path,
                    "微信视频号",
                    logger,
                    max_workers=max_workers,
                )
            except youtube._ParallelDownloadUnavailable as exc:
                logger(f"并行下载不可用，改用稳定单连接：{exc}")
                download_stream_sequential(
                    selected.candidate.url,
                    temp_path,
                    expected_size=selected_size,
                    log=logger,
                )

            media = youtube.probe_media(temp_path, ffprobe)
            if not media.get("has_video") or not media.get("has_audio"):
                missing = "视频" if not media.get("has_video") else "音频"
                raise WechatChannelsDownloadError(
                    f"视频号下载结果缺少{missing}流，未保存为完成品。"
                )
            stem = safe_filename(
                f"微信视频号_{profile.author}_{profile.title}_{profile.content_id}",
                150,
            )
            target = unique_path(item_root / f"{stem}.mp4")
            shutil.move(str(temp_path), str(target))
    except WechatChannelsDownloadError:
        raise
    except (
        OSError,
        ValueError,
        requests.RequestException,
        subprocess.SubprocessError,
    ) as exc:
        raise WechatChannelsDownloadError(f"视频号下载失败：{exc}") from exc

    report = build_report(
        profile,
        target,
        media,
        selected.candidate.label,
        target.stat().st_size,
        "ok",
    )
    logger(
        f"微信视频号下载完成：{report['resolution']}，{report['fps']:.2f} FPS，"
        f"视频 {report['video_codec']}，音频 {report['audio_codec']}"
    )
    logger(str(target))
    return report


def download_stream_sequential(
    url: str,
    target: Path,
    *,
    expected_size: int = 0,
    log: LogFn | None = None,
) -> None:
    logger = log or (lambda _message: None)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    started = time.monotonic()
    downloaded = 0
    last_percent = -10
    try:
        with requests.get(
            url,
            headers=MEDIA_HEADERS,
            stream=True,
            allow_redirects=True,
            timeout=(10, 60),
        ) as response:
            response.raise_for_status()
            response_size = _positive_int(response.headers.get("Content-Length"))
            total = expected_size or response_size
            with target.open("wb") as output:
                for chunk in response.iter_content(1024 * 1024):
                    raise_if_cancelled()
                    if not chunk:
                        continue
                    output.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        percent = min(100, int(downloaded / total * 100))
                        if percent >= last_percent + 10 or downloaded >= total:
                            last_percent = percent
                            elapsed = max(time.monotonic() - started, 0.001)
                            logger(
                                f"微信视频号下载进度：{percent}%"
                                f"（{downloaded / 1024 / 1024 / elapsed:.2f} MiB/s）"
                            )
    except (DownloadCancelled, OSError, requests.RequestException):
        target.unlink(missing_ok=True)
        raise
    if expected_size > 0 and downloaded != expected_size:
        target.unlink(missing_ok=True)
        raise WechatChannelsDownloadError(
            f"视频号文件大小校验失败：预期 {expected_size}，实际 {downloaded}。"
        )


def find_existing_video(
    output_root: Path,
    content_id: str,
    ffprobe: str,
) -> tuple[Path, dict] | None:
    for path in sorted(output_root.glob(f"微信视频号_*_{content_id}*.mp4")):
        if not path.is_file() or path.stat().st_size <= 0:
            continue
        try:
            media = youtube.probe_media(path, ffprobe)
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        if media.get("has_video") and media.get("has_audio"):
            return path, media
    return None


def build_report(
    profile: ChannelsProfile,
    output: Path,
    media: dict,
    selected_format: str,
    size: int,
    status: str,
) -> dict:
    return {
        "platform": "微信视频号",
        "id": profile.content_id,
        "title": profile.title,
        "author": profile.author,
        "source_url": profile.source_url,
        "output": str(output),
        "output_path": str(output),
        "status": status,
        "selected_format": selected_format,
        "resolution": str(media.get("resolution") or "未知分辨率"),
        "fps": float(media.get("fps") or 0),
        "video_codec": str(media.get("video_codec") or "未知"),
        "audio_codec": str(media.get("audio_codec") or "未知"),
        "size": int(size),
    }


def _http_url(value: object) -> str:
    url = str(value or "").strip()
    return url if url.startswith(("http://", "https://")) else ""


def _clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _positive_int(value: object) -> int:
    try:
        result = int(str(value or "0").strip())
    except (TypeError, ValueError):
        return 0
    return max(0, result)


def _content_range_total(value: object) -> int:
    match = re.fullmatch(r"bytes\s+\d+-\d+/(\d+)", str(value or "").strip(), re.IGNORECASE)
    return int(match.group(1)) if match else 0
