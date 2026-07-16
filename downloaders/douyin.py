from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests
from requests.adapters import HTTPAdapter
from PIL import Image, UnidentifiedImageError


LogFn = Callable[[str], None]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": "https://www.douyin.com/",
}

ACTIVE_PROXIES: list["HeaderProxyServer"] = []
DOUYIN_LOGIN_URL = "https://www.douyin.com/"
APP_VERSION = "2026-07-16-speed-v4"
NO_WINDOW_KWARGS = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
IDM_LARGE_FILE_THRESHOLD = 80 * 1024 * 1024
IDM_VIDEO_FILE_THRESHOLD = 20 * 1024 * 1024


def format_bytes(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)}{unit}"
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{int(size)}B"


class DouyinDownloadError(RuntimeError):
    """Raised when a Douyin post cannot be parsed or downloaded."""


@dataclass
class DownloadEngine:
    mode: str = "builtin"
    idm_path: Path | None = None
    proxy: "HeaderProxyServer | None" = None
    idm_threshold_bytes: int = IDM_LARGE_FILE_THRESHOLD
    idm_video_threshold_bytes: int = IDM_VIDEO_FILE_THRESHOLD

    @property
    def name(self) -> str:
        if self.mode == "idm" and self.idm_path:
            return f"IDM ({self.idm_path})"
        if self.mode == "smart" and self.idm_path:
            return f"智能模式（视频>{format_bytes(self.idm_video_threshold_bytes)}或大小未知用 IDM）"
        return "内置下载器"

    def should_use_idm(self, size: int) -> bool:
        if not self.idm_path or not self.proxy:
            return False
        if self.mode == "idm":
            return True
        if self.mode == "smart":
            return size >= self.idm_threshold_bytes
        return False

    def should_use_idm_for_video(self, size: int) -> bool:
        if not self.idm_path or not self.proxy:
            return False
        if self.mode == "idm":
            return True
        if self.mode == "smart":
            return size == 0 or size >= self.idm_video_threshold_bytes
        return False


@dataclass
class ImageCandidate:
    url: str
    source: str
    width: int = 0
    height: int = 0
    declared_size: int = 0
    preview: bool = False
    watermark: bool = False

    @property
    def declared_area(self) -> int:
        return self.width * self.height


@dataclass
class ImageItem:
    index: int
    width: int = 0
    height: int = 0
    candidates: list[ImageCandidate] = field(default_factory=list)
    comment_user: str = ""
    comment_text: str = ""


@dataclass
class ImageProbeResult:
    candidate: ImageCandidate
    content: bytes
    extension: str
    image_format: str
    width: int
    height: int
    content_type: str = ""

    @property
    def score(self) -> tuple[int, int, int, int, int]:
        watermark_rank = 0 if self.candidate.watermark else 1
        preview_rank = 0 if self.candidate.preview else 1
        size = len(self.content) or self.candidate.declared_size
        return (watermark_rank, preview_rank, self.width * self.height, size, self.candidate.declared_area)


@dataclass
class MediaCandidate:
    url: str
    source: str
    codec: str = ""
    width: int = 0
    height: int = 0
    bitrate: int = 0
    declared_size: int = 0
    backup: bool = False
    watermark: bool = False
    audio_url: str = ""

    @property
    def declared_area(self) -> int:
        return self.width * self.height


@dataclass
class MediaProbeResult:
    candidate: MediaCandidate
    content_length: int = 0
    content_type: str = ""

    @property
    def score(self) -> tuple[int, int, int, int, int]:
        codec_rank = {"h266": 4, "h265": 3, "av1": 2, "h264": 1}.get(self.candidate.codec.lower(), 0)
        size = self.content_length or self.candidate.declared_size
        return (
            0 if self.candidate.watermark else 1,
            self.candidate.declared_area,
            self.candidate.bitrate,
            size,
            codec_rank,
        )


def download_note(
    input_text: str,
    output_root: str | Path,
    prefer_format: str = "original",
    log: LogFn | None = None,
    max_workers: int = 4,
    use_idm: bool | str = "smart",
    cookie_header: str = "",
    fallback_aweme: dict | None = None,
) -> dict:
    if prefer_format != "original":
        raise ValueError('Only prefer_format="original" is supported.')

    logger = log or (lambda _message: None)
    source_url = extract_url(input_text)
    logger(f"识别链接：{source_url}")
    logger(f"程序版本：{APP_VERSION}")
    logger("评论区图片策略：优先读取 comment/list 接口 origin_url 原图，DOM 缩略图只作兜底。")

    session = make_session(cookie_header=cookie_header)
    html_text = ""
    final_url = source_url
    try:
        html_text, final_url = fetch_page(session, source_url)
    except DouyinDownloadError as exc:
        logger(f"页面读取失败，继续使用详情接口/内置浏览器兜底：{exc}")
        final_url = resolve_final_url(session, source_url) or source_url
    aweme_id = extract_aweme_id(final_url) or extract_aweme_id(source_url)
    aweme = find_aweme(html_text, aweme_id) if html_text else {}
    browser_captured = False
    if not aweme and aweme_id:
        aweme = fetch_aweme_detail(session, aweme_id, final_url)
    if not aweme:
        aweme, browser_final_url = fetch_browser_aweme(final_url or source_url, logger)
        browser_captured = bool(aweme)
        if browser_final_url:
            final_url = browser_final_url
            aweme_id = extract_aweme_id(final_url) or aweme_id
    if not aweme and isinstance(fallback_aweme, dict) and (extract_images(fallback_aweme) or extract_videos(fallback_aweme)):
        logger("详情链路未读取到作品媒体，使用收藏夹接口数据兜底。")
        aweme = fallback_aweme
    if not aweme:
        raise DouyinDownloadError("没有在页面数据里找到抖音作品详情，链接可能过期或需要登录验证。")

    aweme_id = str(aweme.get("aweme_id") or aweme.get("awemeId") or aweme_id or "unknown")
    author = author_name(aweme)
    title = note_title(aweme, aweme_id)
    images = extract_images(aweme)
    videos = extract_videos(aweme)

    browser_streams = extract_browser_video_streams(final_url, aweme, logger) if videos and not browser_captured else []
    if browser_streams:
        videos = browser_streams

    if not images and not videos:
        aweme, browser_final_url = fetch_browser_aweme(final_url or source_url, logger)
        if browser_final_url:
            final_url = browser_final_url
            aweme_id = extract_aweme_id(final_url) or aweme_id
        images = extract_images(aweme)
        videos = extract_videos(aweme)

    if not images and not videos:
        raise DouyinDownloadError("没有在该作品里解析到图片或视频。")

    note_dir = Path(output_root)
    note_dir.mkdir(parents=True, exist_ok=True)
    file_prefix = safe_filename(f"抖音_{author}_{title}_{aweme_id}", 120)

    logger(f"作者：{author}")
    logger(f"图片数量：{len(images)}")
    if videos:
        logger(f"视频数量：{len(videos)}")
    logger(f"保存目录：{note_dir}")

    report = {
        "input": input_text,
        "source_url": source_url,
        "final_url": final_url,
        "aweme_id": aweme_id,
        "author": author,
        "title": title,
        "output_dir": str(note_dir),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "images": [],
        "videos": [],
        "failures": [],
        "skipped": [],
    }

    existing_media_names = existing_media_filenames(note_dir)
    if videos and has_existing_aweme_nowm_video(note_dir, aweme_id, existing_media_names):
        logger(f"已存在无水印视频，跳过下载：{aweme_id}")
        report["skipped"].append({"aweme_id": aweme_id, "reason": "exists_nowm_video"})
        report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        report["elapsed_seconds"] = 0
        return report
    if images and not videos and has_existing_aweme_nowm_images(note_dir, aweme_id, existing_media_names):
        logger(f"已存在无水印原图下载结果，跳过下载：{aweme_id}")
        report["skipped"].append({"aweme_id": aweme_id, "reason": "exists_nowm_orig_images"})
        report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        report["elapsed_seconds"] = 0
        return report

    engine = make_download_engine(use_idm)
    report["download_engine"] = engine.name
    logger(f"下载引擎：{engine.name}")
    image_engine = DownloadEngine()
    if engine.mode == "idm" and images:
        if videos:
            logger("图片使用内置并发下载，避免 IDM 队列/代理导致漏下；视频继续使用 IDM。")
        else:
            logger("图片使用内置并发下载，避免 IDM 队列/代理导致漏下。")

    started_at = time.perf_counter()
    image_tasks = [("image", image.index, image) for image in images]
    video_tasks = [("video", index, stream) for index, stream in enumerate(videos, start=1)]
    if engine.mode == "idm":
        image_workers = max(1, min(max_workers, 6))
        media_workers = max(1, min(max_workers, 8))
    elif engine.mode == "smart" and engine.idm_path:
        image_workers = max(1, min(max_workers, 3))
        media_workers = max(1, min(max_workers, 6))
    else:
        image_workers = max(1, min(max_workers, 3))
        media_workers = max(1, min(max_workers, 4))

    run_task_group(image_tasks, image_workers, note_dir, final_url, image_engine, report, logger, "图片", file_prefix=file_prefix)
    run_task_group(video_tasks, media_workers, note_dir, final_url, engine, report, logger, "视频", file_prefix=file_prefix)

    report["images"].sort(key=lambda item: item.get("index", 0))
    report["videos"].sort(key=lambda item: item.get("index", 0))
    report["elapsed_seconds"] = round(time.perf_counter() - started_at, 3)
    report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if engine.proxy:
        report["idm_proxy"] = engine.proxy.base_url

    image_ok_count = sum(1 for item in report["images"] if item.get("status") == "ok")
    video_ok_count = sum(1 for item in report["videos"] if item.get("status") == "ok")
    total_count = len(images) + len(videos)
    total_ok_count = image_ok_count + video_ok_count
    logger(f"完成：图片 {image_ok_count}/{len(images)}，视频 {video_ok_count}/{len(videos)}")
    if total_count and total_ok_count == 0:
        raise DouyinDownloadError("所有媒体都下载失败，请检查链接是否失效或是否触发访问验证。")
    if engine.mode == "idm" and videos:
        logger("已投递到 IDM。请保持软件窗口打开，直到 IDM 完成下载。")
    return report


def download_comment_images(
    input_text: str,
    output_root: str | Path,
    limit: int | None = None,
    log: LogFn | None = None,
    max_workers: int = 6,
) -> dict:
    logger = log or (lambda _message: None)
    source_url = extract_url(input_text)
    logger(f"识别链接：{source_url}")
    logger(f"程序版本：{APP_VERSION}")
    logger("评论区图片策略：优先复用 Chrome 登录态读取 comment/list 签名接口，再直接分页下载 origin_url 原图。")

    if limit is not None and limit <= 0:
        limit = None

    session = make_session()
    html_text, final_url = fetch_page(session, source_url)
    aweme_id = extract_aweme_id(final_url) or extract_aweme_id(source_url)
    aweme = find_aweme(html_text, aweme_id)
    if not aweme and aweme_id:
        aweme = fetch_aweme_detail(session, aweme_id, final_url)

    snapshot = read_comment_image_snapshot(final_url or source_url, limit, logger)
    raw_images = snapshot.get("commentImages") if isinstance(snapshot.get("commentImages"), list) else []
    raw_origin_count = sum(1 for item in raw_images if isinstance(item, dict) and "sc=image" in str(item.get("src") or ""))
    raw_thumb_count = sum(1 for item in raw_images if isinstance(item, dict) and "sc=thumb" in str(item.get("src") or ""))
    profile_dir = str(snapshot.get("profileDir") or "")
    if profile_dir:
        logger(f"浏览器登录态目录：{profile_dir}")
    activated_tab = str(snapshot.get("activatedCommentTab") or "")
    if activated_tab:
        logger(f"已切换到页面标签：{activated_tab}")
    elif raw_origin_count:
        logger("已通过评论接口读取原图候选。")
    else:
        logger("未确认切换到评论标签，若图片数量偏少，请确认页面是否展示评论区。")
    if snapshot.get("loginRequired"):
        logger("检测到页面仍像未登录状态：评论区可能只能加载少量内容。请点击软件里的“登录抖音”，扫码登录后再爬取。")
    if isinstance(snapshot.get("detail"), dict) and snapshot.get("detail"):
        aweme = prepare_browser_aweme_detail(snapshot["detail"], snapshot)
    browser_final_url = str(snapshot.get("url") or "")
    if browser_final_url:
        final_url = browser_final_url
        aweme_id = extract_aweme_id(final_url) or aweme_id

    aweme_id = str((aweme or {}).get("aweme_id") or (aweme or {}).get("awemeId") or aweme_id or "unknown")
    author = author_name(aweme or {})
    title = note_title(aweme or {"desc": snapshot.get("title")}, aweme_id)

    logger(f"评论图片候选：原图 {raw_origin_count}，缩略图 {raw_thumb_count}")
    images = extract_comment_images(raw_images, limit)
    if not images:
        raise DouyinDownloadError("没有在评论区解析到图片，或当前页面需要登录/验证后才能加载评论。")

    note_dir = Path(output_root)
    note_dir.mkdir(parents=True, exist_ok=True)
    file_prefix = safe_filename(f"抖音评论_{author}_{title}_{aweme_id}", 120)

    logger(f"作者：{author}")
    logger(f"评论区图片数量：{len(images)}" + (f"（上限 {limit}）" if limit else ""))
    logger(f"保存目录：{note_dir}")
    logger("评论区图片使用内置并发下载，避免 IDM 队列漏下。")

    report = {
        "input": input_text,
        "source_url": source_url,
        "final_url": final_url,
        "aweme_id": aweme_id,
        "author": author,
        "title": title,
        "output_dir": str(note_dir),
        "limit": limit,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "images": [],
        "videos": [],
        "failures": [],
        "download_engine": "内置下载器",
        "browser_engine": str(snapshot.get("browserEngine") or ""),
        "app_version": APP_VERSION,
        "raw_origin_candidates": raw_origin_count,
        "raw_thumb_candidates": raw_thumb_count,
    }

    started_at = time.perf_counter()
    worker_count = max(1, min(max_workers, 10))
    tasks = [("image", image.index, image) for image in images]
    run_task_group(tasks, worker_count, note_dir, final_url, DownloadEngine(), report, logger, "评论图片", file_prefix=file_prefix)

    report["images"].sort(key=lambda item: item.get("index", 0))
    report["elapsed_seconds"] = round(time.perf_counter() - started_at, 3)
    report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    ok_count = sum(1 for item in report["images"] if item.get("status") == "ok")
    logger(f"完成：评论图片 {ok_count}/{len(images)}")
    if ok_count == 0:
        raise DouyinDownloadError("评论区图片全部下载失败，请检查链接是否失效或是否触发访问验证。")
    return report


def open_douyin_login_browser() -> Path:
    browser_path = find_chromium_browser()
    if not browser_path:
        raise DouyinDownloadError("未找到 Chrome 或 Edge，无法打开抖音登录窗口。")
    profile_dir = douyin_browser_profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)
    launch_chromium_cdp_browser(browser_path, profile_dir, visible=True, url=DOUYIN_LOGIN_URL)
    return profile_dir


def read_comment_image_snapshot(url: str, limit: int | None, logger: LogFn) -> dict:
    logger("正在使用软件内置浏览器滚动读取评论区图片（复用软件的抖音登录态）...")
    snapshot = read_builtin_browser_comment_snapshot(url, limit)
    if isinstance(snapshot, dict):
        snapshot["browserEngine"] = "Built-in CDP"
    return snapshot


def make_session(cookie_header: str = "") -> requests.Session:
    session = requests.Session()
    session.headers.update(BASE_HEADERS)
    adapter = HTTPAdapter(pool_connections=16, pool_maxsize=16)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    if cookie_header:
        session.headers["Cookie"] = cookie_header
    return session


def make_download_engine(use_idm: bool | str = "smart") -> DownloadEngine:
    mode = normalize_download_engine_mode(use_idm)
    if mode == "builtin":
        return DownloadEngine()
    idm_path = find_idm()
    if not idm_path:
        return DownloadEngine()
    proxy = HeaderProxyServer()
    ACTIVE_PROXIES.append(proxy)
    return DownloadEngine(mode="idm" if mode == "auto" else "smart", idm_path=idm_path, proxy=proxy)


def normalize_download_engine_mode(value: bool | str) -> str:
    if isinstance(value, bool):
        return "auto" if value else "builtin"
    mode = str(value or "").strip().lower()
    if mode in {"auto", "idm"}:
        return "auto"
    if mode in {"smart", "hybrid"}:
        return "smart"
    return "builtin"


class HeaderProxyServer:
    def __init__(self) -> None:
        self.routes: dict[str, tuple[str, str]] = {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def register(self, url: str, referer: str) -> str:
        token = uuid.uuid4().hex
        self.routes[token] = (url, referer)
        return f"{self.base_url}/{token}"

    def _handler(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args) -> None:
                return

            def do_HEAD(self) -> None:  # noqa: N802
                self._proxy(send_body=False)

            def do_GET(self) -> None:  # noqa: N802
                self._proxy(send_body=True)

            def _proxy(self, send_body: bool) -> None:
                token = self.path.strip("/").split("?", 1)[0]
                target = parent.routes.get(token)
                if not target:
                    self.send_error(404, "Unknown token")
                    return
                target_url, referer = target
                headers = dict(BASE_HEADERS)
                headers["Referer"] = referer or "https://www.douyin.com/"
                headers["Accept"] = "*/*"
                if self.headers.get("Range"):
                    headers["Range"] = self.headers["Range"]
                try:
                    with requests.get(target_url, headers=headers, timeout=(6, 60), stream=True) as response:
                        self.send_response(response.status_code)
                        excluded = {"transfer-encoding", "connection", "content-encoding"}
                        for key, value in response.headers.items():
                            if key.lower() not in excluded:
                                self.send_header(key, value)
                        self.end_headers()
                        if not send_body:
                            return
                        for chunk in response.iter_content(chunk_size=1024 * 512):
                            if chunk:
                                self.wfile.write(chunk)
                except Exception as exc:  # noqa: BLE001
                    try:
                        self.send_error(502, str(exc))
                    except OSError:
                        pass

        return Handler


def extract_url(text: str) -> str:
    urls = extract_urls(text)
    if not urls:
        raise DouyinDownloadError("没有识别到抖音链接，请粘贴完整分享文本或链接。")
    return urls[0]


def extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    allowed = ("douyin.com", "iesdouyin.com")
    for match in re.finditer(r"https?://[^\s，。！!）)\]}>\"']+", text):
        url = match.group(0).strip().rstrip(".,;，。；")
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if not any(host == domain or host.endswith("." + domain) for domain in allowed):
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def fetch_page(session: requests.Session, url: str) -> tuple[str, str]:
    try:
        response = session.get(url, timeout=(8, 25), allow_redirects=True)
    except requests.RequestException as exc:
        raise DouyinDownloadError(f"请求作品页面失败：{exc}") from exc
    if response.status_code in {403, 406, 418, 429}:
        raise DouyinDownloadError(f"访问被限制（HTTP {response.status_code}），可能需要登录浏览器或稍后重试。")
    if response.status_code >= 400:
        raise DouyinDownloadError(f"请求作品页面失败（HTTP {response.status_code}）。")
    response.encoding = response.apparent_encoding or response.encoding
    return response.text, response.url


def resolve_final_url(session: requests.Session, url: str) -> str:
    headers = dict(BASE_HEADERS)
    headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    try:
        with session.get(url, headers=headers, timeout=(5, 8), allow_redirects=True, stream=True) as response:
            return response.url or url
    except requests.RequestException:
        return url


def find_aweme(page_html: str, aweme_id: str = "") -> dict:
    for state in parse_page_states(page_html):
        found = find_aweme_in_value(state, aweme_id)
        if found:
            return found
    return {}


def parse_page_states(page_html: str) -> list[object]:
    states: list[object] = []
    render_match = re.search(
        r'<script[^>]+id=["\']RENDER_DATA["\'][^>]*>(.*?)</script>',
        page_html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if render_match:
        decoded = html.unescape(render_match.group(1))
        for raw in (decoded, unquote(decoded)):
            try:
                states.append(json.loads(raw))
                break
            except json.JSONDecodeError:
                continue

    for marker in ("window.__INITIAL_STATE__=", "window._ROUTER_DATA = ", "window._ROUTER_DATA="):
        start = page_html.find(marker)
        if start < 0:
            continue
        json_start = page_html.find("{", start + len(marker))
        if json_start < 0:
            continue
        try:
            json_end = find_matching_brace(page_html, json_start)
            states.append(json.loads(page_html[json_start : json_end + 1]))
        except (json.JSONDecodeError, DouyinDownloadError):
            continue

    for match in re.finditer(r'"aweme_id"\s*:\s*"\d{8,}"', page_html):
        start = page_html.rfind("{", 0, match.start())
        if start < 0:
            continue
        try:
            end = find_matching_brace(page_html, start)
            states.append(json.loads(page_html[start : end + 1]))
        except (json.JSONDecodeError, DouyinDownloadError):
            continue
    return states


def find_matching_brace(text: str, opening_index: int) -> int:
    depth = 0
    in_string = False
    quote = ""
    escaped = False
    for index in range(opening_index, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            continue
        if char in {'"', "'"}:
            in_string = True
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    raise DouyinDownloadError("页面初始数据不完整，无法匹配 JSON 结束位置。")


def find_aweme_in_value(value, aweme_id: str = "") -> dict:
    decoded = decode_json_container(value)
    if decoded is not value:
        return find_aweme_in_value(decoded, aweme_id)
    if isinstance(value, dict):
        current_id = str(value.get("aweme_id") or value.get("awemeId") or value.get("id") or "")
        has_media = isinstance(value.get("video"), dict) or isinstance(value.get("images"), list)
        if has_media and current_id and (not aweme_id or current_id == aweme_id):
            return value
        for key in ("aweme_detail", "awemeDetail", "aweme", "awemeInfo", "note"):
            child = value.get(key)
            if isinstance(child, dict):
                found = find_aweme_in_value(child, aweme_id)
                if found:
                    return found
        for child in value.values():
            if isinstance(child, (dict, list, str)):
                found = find_aweme_in_value(child, aweme_id)
                if found:
                    return found
    elif isinstance(value, list):
        for child in value:
            found = find_aweme_in_value(child, aweme_id)
            if found:
                return found
    return {}


def fetch_aweme_detail(session: requests.Session, aweme_id: str, referer: str) -> dict:
    api_url = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
    params = {
        "aweme_id": aweme_id,
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
    }
    headers = dict(BASE_HEADERS)
    headers["Referer"] = referer or f"https://www.douyin.com/video/{aweme_id}"
    headers["Accept"] = "application/json, text/plain, */*"
    try:
        response = session.get(api_url, params=params, headers=headers, timeout=(8, 20))
    except requests.RequestException:
        return {}
    if response.status_code >= 400:
        return {}
    try:
        data = response.json()
    except json.JSONDecodeError:
        return {}
    detail = data.get("aweme_detail") or data.get("awemeDetail") or {}
    return detail if isinstance(detail, dict) else {}


def fetch_browser_aweme(url: str, logger: LogFn) -> tuple[dict, str]:
    if is_note_url(url):
        logger("页面初始数据未命中，尝试使用内置浏览器快速读取图文作品...")
        aweme, final_url = fetch_builtin_browser_aweme(url, logger)
        if aweme:
            return aweme, final_url

    logger("页面初始数据未命中，尝试使用软件内置浏览器兜底...")
    return fetch_builtin_browser_aweme(url, logger)


def is_note_url(url: str) -> bool:
    return "/note/" in urlparse(url).path.lower()


def fetch_builtin_browser_aweme(url: str, logger: LogFn) -> tuple[dict, str]:
    browser_path = find_chromium_browser()
    if not browser_path:
        logger("内置浏览器兜底失败：未找到 Chrome 或 Edge。")
        return {}, ""
    try:
        snapshot = read_builtin_browser_aweme_snapshot(browser_path, url)
    except Exception as exc:  # noqa: BLE001 - keep normal parser errors readable.
        logger(f"内置浏览器兜底失败：{exc}")
        return {}, ""
    if not isinstance(snapshot, dict):
        return {}, ""
    aweme = browser_snapshot_to_aweme(snapshot)
    final_url = str(snapshot.get("url") or "")
    if aweme:
        logger(f"已通过内置浏览器读取作品媒体：{Path(browser_path).name}")
    return aweme, final_url


def find_chromium_browser() -> str:
    candidates = [
        os.environ.get("CHROME_PATH", ""),
        os.environ.get("EDGE_PATH", ""),
        shutil.which("chrome"),
        shutil.which("chrome.exe"),
        shutil.which("msedge"),
        shutil.which("msedge.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return ""


def app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def douyin_browser_profile_dir() -> Path:
    return app_base_dir() / "抖音浏览器登录态"


def launch_chromium_cdp_browser(browser_path: str, profile_dir: Path, visible: bool, url: str = "about:blank") -> subprocess.Popen:
    args = [
        browser_path,
        "--remote-debugging-port=0",
        "--remote-allow-origins=*",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--disable-default-apps",
        "--disable-popup-blocking",
        "--mute-audio",
        "--autoplay-policy=document-user-activation-required",
        "--disable-features=PreloadMediaEngagementData,MediaEngagementBypassAutoplayPolicies",
        "--window-size=1400,1000",
    ]
    if not visible:
        args.append("--window-position=-32000,-32000")
    args.append(url)
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **NO_WINDOW_KWARGS)


def existing_cdp_port(profile_dir: Path) -> int:
    port_path = profile_dir / "DevToolsActivePort"
    if not port_path.exists():
        return 0
    try:
        lines = port_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return 0
    if not lines:
        return 0
    port = to_int(lines[0])
    if not port:
        return 0
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.5) as response:
            if response.status < 400:
                return port
    except (OSError, urllib.error.URLError, TimeoutError):
        return 0
    return 0


def read_builtin_browser_aweme_snapshot(browser_path: str, url: str) -> dict:
    try:
        import websocket  # type: ignore[import-not-found]
    except ImportError as exc:
        raise DouyinDownloadError("缺少 websocket-client 依赖，请重新运行启动脚本安装依赖。") from exc

    profile_dir = douyin_browser_profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)
    process: subprocess.Popen | None = None
    reuse_existing = False
    try:
        port, process, reuse_existing = open_comment_cdp_port(browser_path, profile_dir)
        try:
            try:
                target = create_cdp_target(port)
            except Exception:
                if process is not None and not reuse_existing:
                    terminate_process(process)
                process = None
                reuse_existing = False
                remove_devtools_port_file(profile_dir)
                port, process, reuse_existing = open_comment_cdp_port(browser_path, profile_dir, force_new=True)
                target = create_cdp_target(port)
            ws_url = str(target.get("webSocketDebuggerUrl") or "")
            if not ws_url:
                raise DouyinDownloadError("Chrome DevTools 没有返回 WebSocket 地址。")
            ws = websocket.create_connection(ws_url, timeout=8)
            try:
                cdp = CdpClient(ws)
                cdp.call("Page.enable", timeout=8)
                cdp.call("Runtime.enable", timeout=8)
                cdp.call("Page.navigate", {"url": url}, timeout=8)
                value = evaluate_after_navigation(cdp, build_browser_snapshot_script())
                if isinstance(value, dict):
                    return value
                return {}
            finally:
                try:
                    ws.close()
                except Exception:
                    pass
        finally:
            pass
    finally:
        if process is not None and not reuse_existing:
            terminate_process(process)


def read_builtin_browser_comment_snapshot(url: str, limit: int | None = None) -> dict:
    try:
        import websocket  # type: ignore[import-not-found]
    except ImportError as exc:
        raise DouyinDownloadError("缺少 websocket-client 依赖，请重新运行启动脚本安装依赖。") from exc

    browser_path = find_chromium_browser()
    if not browser_path:
        raise DouyinDownloadError("未找到 Chrome 或 Edge，无法读取评论区。")
    profile_dir = douyin_browser_profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)
    process: subprocess.Popen | None = None
    reuse_existing = False
    try:
        port, process, reuse_existing = open_comment_cdp_port(browser_path, profile_dir)
        try:
            target = create_cdp_target(port)
        except Exception:
            if process is not None and not reuse_existing:
                terminate_process(process)
            process = None
            reuse_existing = False
            remove_devtools_port_file(profile_dir)
            port, process, reuse_existing = open_comment_cdp_port(browser_path, profile_dir, force_new=True)
            target = create_cdp_target(port)
        ws_url = str(target.get("webSocketDebuggerUrl") or "")
        if not ws_url:
            raise DouyinDownloadError("Chrome DevTools 没有返回 WebSocket 地址。")
        ws = websocket.create_connection(ws_url, timeout=8)
        try:
            cdp = CdpClient(ws)
            cdp.call("Page.enable", timeout=8)
            cdp.call("Runtime.enable", timeout=8)
            cdp.call("Page.navigate", {"url": url}, timeout=8)
            value = evaluate_after_navigation(cdp, build_comment_image_snapshot_script(limit), timeout=comment_browser_timeout(limit))
            if isinstance(value, dict):
                value["profileDir"] = str(profile_dir)
                value["reusedBrowser"] = reuse_existing
                return value
            return {"profileDir": str(profile_dir), "reusedBrowser": reuse_existing}
        finally:
            try:
                ws.close()
            except Exception:
                pass
    finally:
        if process is not None and not reuse_existing:
            terminate_process(process)


def read_opencli_comment_snapshot(opencli_path: str, url: str, limit: int | None = None) -> dict:
    session_name = f"douyin-comments-{uuid.uuid4().hex[:8]}"
    try:
        run_opencli(opencli_path, ["browser", session_name, "open", url], timeout=60)
        script = build_comment_image_snapshot_script(limit)
        stdout = run_opencli(opencli_path, ["browser", session_name, "eval", script], timeout=comment_browser_timeout(limit))
        data = parse_json_from_output(stdout)
        return data if isinstance(data, dict) else {}
    finally:
        try:
            run_opencli(opencli_path, ["browser", session_name, "close"], timeout=10)
        except Exception:
            pass


def read_opencli_comment_api_snapshot(opencli_path: str, url: str, limit: int | None, logger: LogFn) -> dict:
    session_name = f"douyin-comment-api-{uuid.uuid4().hex[:8]}"
    max_images = max(20, int(limit or 5000))
    try:
        run_opencli(opencli_path, ["browser", session_name, "open", url], timeout=60)
        stdout = run_opencli(opencli_path, ["browser", session_name, "eval", build_comment_api_template_script()], timeout=75)
        data = parse_json_from_output(stdout)
        if not isinstance(data, dict):
            return {}
        template = str(data.get("template") or "")
        if not template:
            return data
        logger("已拿到登录态评论接口签名，开始直接分页读取评论原图...")
        cookie = str(data.get("cookie") or "")
        images = collect_comment_api_images(template, max_images, cookie)
        data["commentImages"] = images
        data["apiTemplateFound"] = True
        return data
    finally:
        try:
            run_opencli(opencli_path, ["browser", session_name, "close"], timeout=10)
        except Exception:
            pass


def collect_comment_api_images(template: str, max_images: int, cookie: str = "") -> list[dict]:
    session = make_session()
    output: list[dict] = []
    seen: set[str] = set()
    cursor = 0
    max_pages = max(30, min(800, max_images * 3))
    empty_rounds = 0
    for _page in range(max_pages):
        page_url = comment_api_page_url(template, cursor)
        try:
            response = session.get(page_url, headers=comment_api_headers(template, cookie), timeout=(8, 25))
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, json.JSONDecodeError):
            break
        comments = data.get("comments") if isinstance(data.get("comments"), list) else []
        before = len(output)
        for comment in comments:
            push_comment_api_images(comment, output, seen, max_images)
            if len(output) >= max_images:
                break
        if len(output) >= max_images:
            break
        empty_rounds = 0 if len(output) > before else empty_rounds + 1
        if empty_rounds >= 12:
            break
        has_more = bool(data.get("has_more"))
        next_cursor = to_int(data.get("cursor"))
        if not has_more and next_cursor <= cursor:
            break
        cursor = next_cursor if next_cursor > cursor else cursor + 20
    return output


def comment_api_page_url(template: str, cursor: int) -> str:
    parsed = urlparse(template)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["cursor"] = [str(cursor)]
    query["count"] = ["20"]
    encoded_parts: list[str] = []
    for key, values in query.items():
        for value in values:
            encoded_parts.append(f"{quote(key, safe='')}={quote(str(value), safe='')}")
    return parsed._replace(query="&".join(encoded_parts)).geturl()


def comment_api_headers(template: str, cookie: str = "") -> dict:
    headers = dict(BASE_HEADERS)
    headers["Accept"] = "application/json, text/plain, */*"
    headers["Referer"] = "https://www.douyin.com/"
    parsed = urlparse(template)
    if parsed.netloc:
        headers["Host"] = parsed.netloc
    if cookie:
        headers["Cookie"] = cookie
    return headers


def push_comment_api_images(comment, output: list[dict], seen: set[str], max_images: int) -> None:
    if not isinstance(comment, dict):
        return
    user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
    user_name = str(user.get("nickname") or user.get("unique_id") or user.get("short_id") or "").strip()
    text = str(comment.get("text") or "").strip()
    image_list = comment.get("image_list") if isinstance(comment.get("image_list"), list) else []
    for image in image_list:
        if not isinstance(image, dict):
            continue
        for field_name in ("origin_url", "large_url", "download_url", "medium_url", "thumb_url"):
            item = image.get(field_name) if isinstance(image.get(field_name), dict) else {}
            urls = item.get("url_list") if isinstance(item.get("url_list"), list) else []
            urls = [normalize_url(str(value)) for value in urls if value]
            if not urls:
                continue
            width = to_int(item.get("width")) or to_int(image.get("width"))
            height = to_int(item.get("height")) or to_int(image.get("height"))
            for src in urls[:2]:
                if not is_http_url(src):
                    continue
                key = comment_image_identity(src)
                if not key or key in seen:
                    continue
                seen.add(key)
                output.append(
                    {
                        "index": 200000 + len(output),
                        "src": src,
                        "width": width,
                        "height": height,
                        "clientWidth": 0,
                        "clientHeight": 0,
                        "top": 0,
                        "left": 0,
                        "text": f"评论接口 {user_name} {text} 分享 回复",
                        "commentUser": user_name,
                        "commentText": text,
                        "chain": [],
                    }
                )
                if len(output) >= max_images:
                    return
            if field_name == "origin_url":
                break
    replies = comment.get("reply_comment") if isinstance(comment.get("reply_comment"), list) else []
    for reply in replies:
        push_comment_api_images(reply, output, seen, max_images)
        if len(output) >= max_images:
            return


def comment_browser_timeout(limit: int | None) -> int:
    if not limit:
        return 420
    return max(300, min(1200, int(limit) * 2 + 240))


def open_comment_cdp_port(browser_path: str, profile_dir: Path, force_new: bool = False) -> tuple[int, subprocess.Popen | None, bool]:
    if not force_new:
        port = existing_cdp_port(profile_dir)
        if port:
            return port, None, True
    remove_devtools_port_file(profile_dir)
    process = launch_chromium_cdp_browser(browser_path, profile_dir, visible=False)
    try:
        port = wait_for_devtools_port(profile_dir / "DevToolsActivePort")
    except Exception:
        terminate_process(process)
        raise
    return port, process, False


def remove_devtools_port_file(profile_dir: Path) -> None:
    try:
        (profile_dir / "DevToolsActivePort").unlink(missing_ok=True)
    except OSError:
        pass


def terminate_process(process: subprocess.Popen) -> None:
    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def evaluate_after_navigation(cdp: "CdpClient", expression: str, timeout: int = 55):
    last_error: Exception | None = None
    for attempt in range(6):
        time.sleep(2 if attempt == 0 else 1)
        try:
            return cdp.evaluate(expression, timeout=timeout)
        except DouyinDownloadError as exc:
            last_error = exc
            message = str(exc)
            if "navigated or closed" not in message and "Execution context was destroyed" not in message:
                raise
    if last_error:
        raise last_error
    return {}


def wait_for_devtools_port(path: Path) -> int:
    deadline = time.time() + 30
    while time.time() < deadline:
        if path.exists():
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            if lines:
                port = to_int(lines[0])
                if port and is_cdp_port_alive(port):
                    return port
        time.sleep(0.2)
    raise DouyinDownloadError("Chrome DevTools 启动超时。")


def is_cdp_port_alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.5) as response:
            return response.status < 400
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def create_cdp_target(port: int) -> dict:
    endpoint = f"http://127.0.0.1:{port}/json/new?about:blank"
    request = urllib.request.Request(endpoint, method="PUT")
    last_error: Exception | None = None
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(request, timeout=4) as response:
                return json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=4) as response:
                    targets = json.loads(response.read().decode("utf-8", errors="replace"))
                if isinstance(targets, list) and targets:
                    return targets[0]
            except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as list_exc:
                last_error = list_exc
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(0.4)
    raise DouyinDownloadError(f"无法创建 Chrome DevTools 页面：{last_error}")


class CdpClient:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.next_id = 0

    def call(self, method: str, params: dict | None = None, timeout: int = 20) -> dict:
        self.next_id += 1
        message_id = self.next_id
        self.ws.settimeout(timeout)
        self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self.ws.recv()
            data = json.loads(raw)
            if data.get("id") != message_id:
                continue
            if data.get("error"):
                raise DouyinDownloadError(f"Chrome DevTools 调用失败({method})：{data['error']}")
            result = data.get("result")
            return result if isinstance(result, dict) else {}
        raise DouyinDownloadError(f"Chrome DevTools 调用超时：{method}")

    def evaluate(self, expression: str, timeout: int = 55):
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": True,
            },
            timeout=timeout,
        )
        remote = result.get("result") if isinstance(result.get("result"), dict) else {}
        if remote.get("subtype") == "error":
            raise DouyinDownloadError(str(remote.get("description") or remote.get("value") or "页面脚本执行失败"))
        return remote.get("value")


def read_browser_aweme_snapshot(opencli_path: str, session_name: str) -> dict:
    script = build_browser_snapshot_script()

    last_error: Exception | None = None
    for _attempt in range(10):
        try:
            stdout = run_opencli(opencli_path, ["browser", session_name, "eval", script], timeout=25)
            data = parse_json_from_output(stdout)
            if isinstance(data, dict):
                videos = data.get("videos") if isinstance(data.get("videos"), list) else []
                images = data.get("images") if isinstance(data.get("images"), list) else []
                detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
                if detail or any(snapshot_video_url(item) for item in videos if isinstance(item, dict)) or images:
                    return data
        except Exception as exc:  # noqa: BLE001 - retry while React/player initializes.
            last_error = exc
        time.sleep(1)
    if last_error:
        raise last_error
    return {}


def build_browser_snapshot_script() -> str:
    return (
        "(async () => {"
        "const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));"
        "async function makeSnapshot() {"
        "const entries = performance.getEntriesByType('resource').map((entry, index) => ({index, src: entry.name || ''}));"
        "const pathMatch = location.pathname.match(/\\/(?:video|note)\\/(\\d+)/);"
        "const modalMatch = location.href.match(/[?&]modal_id=(\\d+)/);"
        "const awemeId = (pathMatch && pathMatch[1]) || (modalMatch && modalMatch[1]) || '';"
        "const resourceDetailUrl = entries.map(e => e.src).find(src => src.includes('/aweme/v1/web/aweme/detail/')) || '';"
        "let detailUrl = resourceDetailUrl;"
        "let detail = null;"
        "let detailStatus = 0;"
        "async function fetchDetail(url) {"
        "  if (!url) return null;"
        "  try {"
        "    const response = await fetch(url, {credentials: 'include', headers: {accept:'application/json, text/plain, */*'}});"
        "    detailStatus = response.status;"
        "    const text = await response.text();"
        "    if (!text) return null;"
        "    const data = JSON.parse(text);"
        "    return data.aweme_detail || data.awemeDetail || null;"
        "  } catch (_error) { return null; }"
        "}"
        "if (awemeId) {"
        "  const direct = new URL('/aweme/v1/web/aweme/detail/', location.origin || 'https://www.douyin.com');"
        "  direct.searchParams.set('aweme_id', awemeId);"
        "  direct.searchParams.set('device_platform', 'webapp');"
        "  direct.searchParams.set('aid', '6383');"
        "  direct.searchParams.set('channel', 'channel_pc_web');"
        "  detailUrl = direct.toString();"
        "  detail = await fetchDetail(detailUrl);"
        "}"
        "if (detailUrl) {"
        "  detail = detail || await fetchDetail(resourceDetailUrl);"
        "}"
        "const videoItems = Array.from(document.querySelectorAll('video')).map((v, index) => ({"
        "index, src: v.currentSrc || v.src || v.getAttribute('src') || '',"
        "width: v.videoWidth || 0, height: v.videoHeight || 0, kind: 'dom'"
        "}));"
        "const resources = entries.map(item => ({"
        "index: item.index, src: item.src, width: 0, height: 0, kind: 'resource'"
        "})).filter(item => /\\.mp4(\\?|$)|douyinvod|amemv|media-audio/i.test(item.src));"
        "const images = Array.from(document.images).map((img, index) => ({"
        "index, src: img.currentSrc || img.src || '', width: img.naturalWidth || 0, height: img.naturalHeight || 0,"
        "clientWidth: img.clientWidth || 0, clientHeight: img.clientHeight || 0"
        "})).filter(item => item.src && item.width >= 240 && item.height >= 240);"
        "return {"
        "url: location.href,"
        "detailUrl,"
        "detailStatus,"
        "detail,"
        "title: document.title || '',"
        "h1: Array.from(document.querySelectorAll('h1,h2,h3')).map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 20),"
        "links: Array.from(document.querySelectorAll('a[href*=user]')).map(a => (a.innerText || a.textContent || '').trim()).filter(Boolean).slice(0, 50),"
        "text: (document.body.innerText || '').slice(0, 4000),"
        "videos: videoItems.concat(resources).filter(item => item.src),"
        "images,"
        "awemeImageCount: images.filter(item => /biz_tag=aweme_images|tplv-dy-aweme-images|PackSourceEnum_AWEME_DETAIL/i.test(item.src) && !/pcweb_cover|sc=cover/i.test(item.src)).length,"
        "workVideoCount: videoItems.concat(resources).filter(item => /douyinvod|amemv|aweme\\/v1\\/play/i.test(item.src) && !/douyinstatic\\.com\\/obj\\/douyin-pc-web/i.test(item.src)).length"
        "};"
        "}"
        "let fallback = null;"
        "let lastMediaKey = '';"
        "let stableCount = 0;"
        "for (let attempt = 0; attempt < 45; attempt++) {"
        "  const snapshot = await makeSnapshot();"
        "  if (snapshot.detail) return snapshot;"
        "  const hasMedia = snapshot.workVideoCount > 0 || snapshot.awemeImageCount > 0;"
        "  if (hasMedia) {"
        "    fallback = snapshot;"
        "    const mediaKey = `${snapshot.workVideoCount}:${snapshot.awemeImageCount}:${(snapshot.images || []).length}`;"
        "    stableCount = mediaKey === lastMediaKey ? stableCount + 1 : 0;"
        "    lastMediaKey = mediaKey;"
        "    if (snapshot.workVideoCount > 0 || stableCount >= 1 || attempt >= 5) return snapshot;"
        "  }"
        "  await sleep(500);"
        "}"
        "return fallback || await makeSnapshot();"
        "})()"
    )


def build_comment_image_snapshot_script(limit: int | None = None) -> str:
    max_images = max(0, int(limit or 0))
    if max_images >= 20:
        return build_comment_api_snapshot_script(max_images)
    max_scrolls = 300 if not max_images else max(24, min(140, max_images * 4 + 30))
    return (
        "(async () => {"
        "const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));"
        f"const maxImages = {max_images};"
        f"const maxScrolls = {max_scrolls};"
        "const apiPreferred = maxImages >= 20;"
        "const allImages = new Map();"
        "function textOf(el) { return ((el && (el.innerText || el.textContent)) || '').trim(); }"
        "function visibleRect(el) { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth ? r : null; }"
        "function imageKey(url) {"
        "  try {"
        "    const u = new URL(url, location.href);"
        "    let path = u.pathname.replace(/^\\/obj\\//, '/');"
        "    path = path.split('~tplv-')[0];"
        "    return path.toLowerCase();"
        "  } catch (_) { return String(url || '').split('~tplv-')[0]; }"
        "}"
        "function imageArea(item) { return (Number(item && item.width) || 0) * (Number(item && item.height) || 0); }"
        "function isHighResCommentUrl(url) { return /biz_tag=aweme_comment/i.test(url || '') && /sc=image/i.test(url || ''); }"
        "function clickElement(el) {"
        "  const r = visibleRect(el);"
        "  if (!r) return false;"
        "  const opts = {bubbles: true, cancelable: true, view: window, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2};"
        "  for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) el.dispatchEvent(new MouseEvent(type, opts));"
        "  if (typeof el.click === 'function') el.click();"
        "  return true;"
        "}"
        "function activateCommentTab() {"
        "  const candidates = Array.from(document.querySelectorAll('button,div,span')).map(el => ({el, text: textOf(el), rect: visibleRect(el)}))"
        "    .filter(item => item.rect && /^评论\\s*(\\(\\d+\\))?$/.test(item.text))"
        "    .sort((a, b) => (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height));"
        "  for (const item of candidates) {"
        "    let node = item.el;"
        "    for (let depth = 0; node && depth < 4; depth++, node = node.parentElement) {"
        "      if (clickElement(node)) return item.text;"
        "    }"
        "  }"
        "  return '';"
        "}"
        "function imageInfo(img, index) {"
        "  const rect = img.getBoundingClientRect();"
        "  let el = img;"
        "  const chain = [];"
        "  let chainText = '';"
        "  for (let depth = 0; el && depth < 8; depth++, el = el.parentElement) {"
        "    const itemText = textOf(el).slice(0, 260);"
        "    chainText += ' ' + itemText;"
        "    chain.push({tag: el.tagName, className: String(el.className || ''), role: el.getAttribute('role') || '', text: itemText});"
        "  }"
        "  const src = img.currentSrc || img.src || '';"
        "  const meta = commentMeta(img);"
        "  return {"
        "    index, src, width: img.naturalWidth || 0, height: img.naturalHeight || 0,"
        "    clientWidth: img.clientWidth || 0, clientHeight: img.clientHeight || 0,"
        "    top: Math.round(rect.top), left: Math.round(rect.left), text: chainText.slice(0, 600), chain,"
        "    commentUser: meta.user, commentText: meta.text"
        "  };"
        "}"
        "function commentMeta(img) {"
        "  let row = img;"
        "  for (let depth = 0; row && depth < 12; depth++, row = row.parentElement) {"
        "    const rowText = textOf(row);"
        "    const userEl = row.querySelector && row.querySelector('[class*=comment-item-info-wrap]');"
        "    if (!userEl || !/分享|回复|点赞|小时前|分钟前|天前|周前|月前|年前/.test(rowText)) continue;"
        "    const user = textOf(userEl);"
        "    let body = '';"
        "    for (let el = img.parentElement; el && el !== row; el = el.parentElement) {"
        "      const value = textOf(el);"
        "      if (value && value !== user && !/分享|回复|点赞|小时前|分钟前|天前|周前|月前|年前/.test(value)) body = value;"
        "    }"
        "    if (!body && user) body = rowText.replace(user, '').replace(/\\d+\\s*(分钟前|小时前|天前|周前|月前|年前).*/, '').trim();"
        "    return {user, text: body};"
        "  }"
        "  return {user: '', text: ''};"
        "}"
        "function isCommentContext(item) { return /回复|分享|点赞|展开|刚刚|分钟前|小时前|天前|周前|月前|年前/.test(item.text || ''); }"
        "function dimensionsFromUrl(url) {"
        "  const m = String(url || '').match(/[:_-](\\d{2,4}):(\\d{2,4}):q\\d+/);"
        "  return m ? {width: Number(m[1]) || 300, height: Number(m[2]) || 300} : {width: 300, height: 300};"
        "}"
        "function collect() {"
        "  const byKey = new Map();"
        "  function keep(item) {"
        "    const key = imageKey(item.src || '');"
        "    if (!key) return;"
        "    const old = byKey.get(key);"
        "    const highBonus = isHighResCommentUrl(item.src) ? 1000000000000 : 0;"
        "    const oldBonus = old && isHighResCommentUrl(old.src) ? 1000000000000 : 0;"
        "    if (!old || imageArea(item) + highBonus > imageArea(old) + oldBonus) {"
        "      if (old) {"
        "        if (!item.commentUser) item.commentUser = old.commentUser || '';"
        "        if (!item.commentText) item.commentText = old.commentText || '';"
        "        if ((!item.text || item.text === '评论图片资源 分享 回复') && old.text) item.text = old.text;"
        "      }"
        "      byKey.set(key, item);"
        "    }"
        "  }"
        "  Array.from(document.images).forEach((img, index) => {"
        "    const item = imageInfo(img, index);"
        "    if (!item.src || !/^https?:/i.test(item.src)) return;"
        "    if (!isCommentContext(item)) return;"
        "    keep(item);"
        "  });"
        "  performance.getEntriesByType('resource').forEach((entry, index) => {"
        "    const src = entry.name || '';"
        "    if (!/^https?:/i.test(src)) return;"
        "    if (!/biz_tag=aweme_comment|tos-cn-i-p14lwwcsbr|tos-cn-o-0812/i.test(src)) return;"
        "    const size = dimensionsFromUrl(src);"
        "    keep({index: 100000 + index, src, width: size.width, height: size.height, clientWidth: 0, clientHeight: 0, top: 0, left: 0, text: '评论图片资源 分享 回复', chain: []});"
        "  });"
        "  return Array.from(byKey.values());"
        "}"
        "function pushApiCommentImages(comment, output) {"
        "  if (!comment || typeof comment !== 'object') return;"
        "  const user = comment.user && (comment.user.nickname || comment.user.unique_id || comment.user.short_id) || '';"
        "  const text = comment.text || '';"
        "  const images = Array.isArray(comment.image_list) ? comment.image_list : [];"
        "  for (const image of images) {"
        "    for (const field of ['origin_url', 'crop_url', 'medium_url', 'thumb_url', 'download_url']) {"
        "      const item = image && image[field];"
        "      const urls = item && Array.isArray(item.url_list) ? item.url_list : [];"
        "      for (const src of urls) {"
        "        if (!src || !/^https?:/i.test(src)) continue;"
        "        output.push({"
        "          index: 200000 + output.length, src,"
        "          width: Number(item.width || image.width || 0), height: Number(item.height || image.height || 0),"
        "          clientWidth: 0, clientHeight: 0, top: 0, left: 0,"
        "          text: `评论接口 ${user} ${text} 分享 回复`, commentUser: user, commentText: text, chain: []"
        "        });"
        "      }"
        "    }"
        "  }"
        "  const replies = Array.isArray(comment.reply_comment) ? comment.reply_comment : [];"
        "  for (const reply of replies) pushApiCommentImages(reply, output);"
        "}"
        "function commentListUrls() {"
        "  return Array.from(new Set(performance.getEntriesByType('resource').map(entry => entry.name || '').filter(src => /\\/aweme\\/v1\\/web\\/comment\\/list\\//.test(src))));"
        "}"
        "async function waitForCommentListTemplate(timeoutMs) {"
        "  const deadline = Date.now() + timeoutMs;"
        "  while (Date.now() < deadline) {"
        "    const urls = commentListUrls();"
        "    if (urls.length) return urls;"
        "    await sleep(500);"
        "  }"
        "  return commentListUrls();"
        "}"
        "async function collectApiImages(entries) {"
        "  let urls = Array.from(new Set((entries || []).filter(src => /\\/aweme\\/v1\\/web\\/comment\\/list\\//.test(src))));"
        "  if (!urls.length) urls = await waitForCommentListTemplate(apiPreferred ? 12000 : 3000);"
        "  const output = [];"
        "  const template = urls[0] || urls[urls.length - 1] || '';"
        "  if (template) {"
        "    let cursor = 0;"
        "    const maxPages = maxImages ? Math.max(12, Math.min(320, Math.ceil(maxImages * 0.8) + 30)) : 160;"
        "    for (let page = 0; page < maxPages; page++) {"
        "      try {"
        "        const pageUrl = new URL(template);"
        "        pageUrl.searchParams.set('cursor', String(cursor));"
        "        pageUrl.searchParams.set('count', '20');"
        "        const data = await fetch(pageUrl.toString(), {credentials: 'include'}).then(response => response.json());"
        "        const comments = Array.isArray(data.comments) ? data.comments : [];"
        "        for (const comment of comments) pushApiCommentImages(comment, output);"
        "        if (maxImages && output.length >= maxImages * 6) break;"
        "        if (!data.has_more) break;"
        "        const nextCursor = Number(data.cursor);"
        "        cursor = Number.isFinite(nextCursor) && nextCursor > cursor ? nextCursor : cursor + 20;"
        "      } catch (_error) { break; }"
        "    }"
        "  }"
        "  for (const url of urls.slice(-24)) {"
        "    try {"
        "      const data = await fetch(url, {credentials: 'include'}).then(response => response.json());"
        "      const comments = Array.isArray(data.comments) ? data.comments : [];"
        "      for (const comment of comments) pushApiCommentImages(comment, output);"
        "    } catch (_error) {}"
        "  }"
        "  return output;"
        "}"
        "function remember(items) {"
        "  for (const item of items || []) {"
        "    const key = imageKey(item.src || '');"
        "    if (!key) continue;"
        "    const old = allImages.get(key);"
        "    const highBonus = isHighResCommentUrl(item.src) ? 1000000000000 : 0;"
        "    const oldBonus = old && isHighResCommentUrl(old.src) ? 1000000000000 : 0;"
        "    if (!old || imageArea(item) + highBonus > imageArea(old) + oldBonus) {"
        "      if (old) {"
        "        if (!item.commentUser) item.commentUser = old.commentUser || '';"
        "        if (!item.commentText) item.commentText = old.commentText || '';"
        "        if ((!item.text || item.text === '评论图片资源 分享 回复') && old.text) item.text = old.text;"
        "      }"
        "      allImages.set(key, item);"
        "    }"
        "  }"
        "  return Array.from(allImages.values());"
        "}"
        "const previewedKeys = new Set();"
        "function commentPhotoElements() {"
        "  return Array.from(document.images).map((img, index) => ({img, item: imageInfo(img, index), rect: visibleRect(img)}))"
        "    .filter(row => row.rect && row.item.src && /biz_tag=aweme_comment/i.test(row.item.src) && /p14lwwcsbr/i.test(row.item.src) && !/sticker|emoji|avatar|pcweb_cover|sc=cover/i.test(row.item.src) && isCommentContext(row.item))"
        "    .sort((a, b) => (a.rect.top - b.rect.top) || (a.rect.left - b.rect.left));"
        "}"
        "function closePreview() {"
        "  document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', code: 'Escape', keyCode: 27, which: 27, bubbles: true}));"
        "}"
        "async function captureVisiblePreviews() {"
        "  const rows = commentPhotoElements();"
        "  for (const row of rows) {"
        "    const key = imageKey(row.item.src);"
        "    if (!key || previewedKeys.has(key)) continue;"
        "    if (maxImages && previewedKeys.size >= maxImages) break;"
        "    previewedKeys.add(key);"
        "    row.img.scrollIntoView({block: 'center', inline: 'center'});"
        "    await sleep(120);"
        "    clickElement(row.img);"
        "    await sleep(650);"
        "    remember(collect());"
        "    closePreview();"
        "    await sleep(180);"
        "  }"
        "}"
        "function scrollCandidates() {"
        "  const commentRoots = Array.from(document.querySelectorAll('div,main,section')).filter(el => {"
        "    const rect = visibleRect(el);"
        "    if (!rect) return false;"
        "    const text = textOf(el);"
        "    return /全部评论|留下你的精彩评论|大家都在搜|分享回复/.test(text);"
        "  });"
        "  const scoped = [];"
        "  for (const root of commentRoots) {"
        "    for (let el = root; el; el = el.parentElement) {"
        "      const rect = visibleRect(el);"
        "      const style = getComputedStyle(el);"
        "      if (rect && el.clientHeight > 180 && (el.scrollHeight > el.clientHeight + 20 || /auto|scroll/.test(style.overflowY))) scoped.push(el);"
        "    }"
        "  }"
        "  const pool = scoped.length ? scoped : Array.from(document.querySelectorAll('div,main,section'));"
        "  return Array.from(new Set(pool)).filter(el => {"
        "    const style = getComputedStyle(el);"
        "    const rect = visibleRect(el);"
        "    return rect && el.clientHeight > 180 && style.display !== 'none' && style.visibility !== 'hidden' && (el.scrollHeight > el.clientHeight + 20 || /auto|scroll/.test(style.overflowY));"
        "  }).sort((a, b) => {"
        "    const at = textOf(a); const bt = textOf(b);"
        "    const ac = /全部评论|留下你的精彩评论|大家都在搜|分享回复/.test(at) ? 1 : 0;"
        "    const bc = /全部评论|留下你的精彩评论|大家都在搜|分享回复/.test(bt) ? 1 : 0;"
        "    return (bc - ac) || ((b.clientHeight * b.clientWidth) - (a.clientHeight * a.clientWidth));"
        "  }).slice(0, 6);"
        "}"
        "async function makeSnapshot() {"
        "  const entries = performance.getEntriesByType('resource').map(entry => entry.name || '').filter(Boolean);"
        "  const detailUrl = entries.find(src => src.includes('/aweme/v1/web/aweme/detail/')) || '';"
        "  let detail = null;"
        "  if (detailUrl) {"
        "    try {"
        "      const response = await fetch(detailUrl, {credentials: 'include'});"
        "      const data = await response.json();"
        "      detail = data.aweme_detail || data.awemeDetail || null;"
        "    } catch (_error) {}"
        "  }"
        "  const bodyText = (document.body && document.body.innerText || '').slice(0, 3000);"
        "  const loginRequired = /扫码登录|验证码登录|密码登录|登录后|打开「抖音APP」|登录即代表同意/.test(bodyText);"
        "  remember(collect());"
        "  remember(await collectApiImages(entries));"
        "  return {url: location.href, title: document.title || '', detailUrl, detail, loginRequired, commentImages: Array.from(allImages.values())};"
        "}"
        "await sleep(1200);"
        "const activatedTab = activateCommentTab();"
        "if (activatedTab) await sleep(1400);"
        "let best = await makeSnapshot();"
        "best.activatedCommentTab = activatedTab;"
        "if (!apiPreferred) await captureVisiblePreviews();"
        "best = await makeSnapshot();"
        "best.activatedCommentTab = activatedTab;"
        "if (apiPreferred && (best.commentImages || []).some(item => /sc=image/i.test(item.src || ''))) return best;"
        "let stableRounds = 0;"
        "for (let attempt = 0; attempt < maxScrolls; attempt++) {"
        "  const scrollers = scrollCandidates();"
        "  if (scrollers.length) {"
        "    for (const scroller of scrollers) scroller.scrollTop += Math.max(500, Math.floor(scroller.clientHeight * 0.85));"
        "  } else {"
        "    window.scrollBy(0, 900);"
        "  }"
        "  await sleep(attempt < 4 ? 800 : 300);"
        "  if (!apiPreferred) await captureVisiblePreviews();"
        "  const snapshot = await makeSnapshot();"
        "  snapshot.activatedCommentTab = activatedTab;"
        "  if ((snapshot.commentImages || []).length > (best.commentImages || []).length) {"
        "    best = snapshot;"
        "    stableRounds = 0;"
        "  } else {"
        "    stableRounds += 1;"
        "  }"
        "  if (maxImages && (best.commentImages || []).length >= maxImages && stableRounds >= 2) break;"
        "  if (!maxImages && stableRounds >= 45 && attempt > 70) break;"
        "}"
        "return best;"
        "})()"
    )


def build_comment_api_template_script() -> str:
    return (
        "(async () => {"
        "const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));"
        "function textOf(el) { return ((el && (el.innerText || el.textContent)) || '').trim(); }"
        "function visibleRect(el) { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth ? r : null; }"
        "function clickElement(el) {"
        "  const r = visibleRect(el);"
        "  if (!r) return false;"
        "  const opts = {bubbles: true, cancelable: true, view: window, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2};"
        "  for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) el.dispatchEvent(new MouseEvent(type, opts));"
        "  if (typeof el.click === 'function') el.click();"
        "  return true;"
        "}"
        "function activateCommentTab() {"
        "  const candidates = Array.from(document.querySelectorAll('button,div,span')).map(el => ({el, text: textOf(el), rect: visibleRect(el)}))"
        "    .filter(item => item.rect && (/^评论\\s*(\\(\\d+\\))?$/.test(item.text) || item.text === '全部评论'))"
        "    .sort((a, b) => (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height));"
        "  for (const item of candidates) {"
        "    let node = item.el;"
        "    for (let depth = 0; node && depth < 5; depth++, node = node.parentElement) {"
        "      if (clickElement(node)) return item.text;"
        "    }"
        "  }"
        "  return '';"
        "}"
        "function commentUrls() {"
        "  return Array.from(new Set(performance.getEntriesByType('resource').map(entry => entry.name || '').filter(src => /\\/aweme\\/v1\\/web\\/comment\\/list\\//.test(src))));"
        "}"
        "function scrollComments() {"
        "  const roots = Array.from(document.querySelectorAll('div,main,section')).filter(el => visibleRect(el) && /全部评论|留下你的精彩评论|分享\\s*回复|展开\\d+条回复/.test(textOf(el)));"
        "  const scrollers = [];"
        "  for (const root of roots) {"
        "    for (let el = root; el; el = el.parentElement) {"
        "      const rect = visibleRect(el);"
        "      if (!rect) continue;"
        "      const style = getComputedStyle(el);"
        "      if (el.clientHeight > 180 && (el.scrollHeight > el.clientHeight + 20 || /auto|scroll/.test(style.overflowY))) scrollers.push(el);"
        "    }"
        "  }"
        "  const target = Array.from(new Set(scrollers))[0];"
        "  if (target) target.scrollTop += Math.max(500, Math.floor(target.clientHeight * 0.8));"
        "  else window.scrollBy(0, 700);"
        "}"
        "let activatedTab = '';"
        "await sleep(2500);"
        "for (let attempt = 0; attempt < 55; attempt++) {"
        "  const urls = commentUrls();"
        "  if (urls.length) {"
        "    const bodyText = (document.body && document.body.innerText || '').slice(0, 3000);"
        "    const loginRequired = /扫码登录|验证码登录|密码登录|登录后|请先登录|登录即代表同意/.test(bodyText);"
        "    return {url: location.href, title: document.title || '', template: urls[urls.length - 1], templates: urls.slice(-6), cookie: document.cookie || '', activatedCommentTab: activatedTab, loginRequired, bodyText};"
        "  }"
        "  if (attempt % 5 === 0) activatedTab = activateCommentTab() || activatedTab;"
        "  scrollComments();"
        "  await sleep(attempt < 12 ? 700 : 1000);"
        "}"
        "const bodyText = (document.body && document.body.innerText || '').slice(0, 3000);"
        "const loginRequired = /扫码登录|验证码登录|密码登录|登录后|请先登录|登录即代表同意/.test(bodyText);"
        "return {url: location.href, title: document.title || '', template: '', templates: [], cookie: document.cookie || '', activatedCommentTab: activatedTab, loginRequired, bodyText};"
        "})()"
    )


def build_comment_api_snapshot_script(max_images: int) -> str:
    max_images = max(20, int(max_images or 0))
    max_pages = max(40, min(500, max_images * 2))
    return (
        "(async () => {"
        "const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));"
        f"const maxImages = {max_images};"
        f"const maxPages = {max_pages};"
        "function textOf(el) { return ((el && (el.innerText || el.textContent)) || '').trim(); }"
        "function visibleRect(el) { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth ? r : null; }"
        "function clickElement(el) {"
        "  const r = visibleRect(el);"
        "  if (!r) return false;"
        "  const opts = {bubbles: true, cancelable: true, view: window, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2};"
        "  for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) el.dispatchEvent(new MouseEvent(type, opts));"
        "  if (typeof el.click === 'function') el.click();"
        "  return true;"
        "}"
        "function activateCommentTab() {"
        "  const candidates = Array.from(document.querySelectorAll('button,div,span')).map(el => ({el, text: textOf(el), rect: visibleRect(el)}))"
        "    .filter(item => item.rect && /^评论\\s*(\\(\\d+\\))?$/.test(item.text))"
        "    .sort((a, b) => (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height));"
        "  for (const item of candidates) {"
        "    let node = item.el;"
        "    for (let depth = 0; node && depth < 4; depth++, node = node.parentElement) {"
        "      if (clickElement(node)) return item.text;"
        "    }"
        "  }"
        "  return '';"
        "}"
        "function commentListUrls() {"
        "  return Array.from(new Set(performance.getEntriesByType('resource').map(entry => entry.name || '').filter(src => /\\/aweme\\/v1\\/web\\/comment\\/list\\//.test(src))));"
        "}"
        "function scrollCandidates() {"
        "  const roots = Array.from(document.querySelectorAll('div,main,section')).filter(el => {"
        "    const rect = visibleRect(el);"
        "    if (!rect) return false;"
        "    return /全部评论|评论\\(|留下你的精彩评论|分享\\s*回复|展开/.test(textOf(el));"
        "  });"
        "  const scoped = [];"
        "  for (const root of roots) {"
        "    for (let el = root; el; el = el.parentElement) {"
        "      const rect = visibleRect(el);"
        "      if (!rect) continue;"
        "      const style = getComputedStyle(el);"
        "      if (el.clientHeight > 180 && (el.scrollHeight > el.clientHeight + 24 || /auto|scroll/.test(style.overflowY))) scoped.push(el);"
        "    }"
        "  }"
        "  const pool = scoped.length ? scoped : Array.from(document.querySelectorAll('div,main,section'));"
        "  return Array.from(new Set(pool)).filter(el => {"
        "    const rect = visibleRect(el);"
        "    if (!rect) return false;"
        "    const style = getComputedStyle(el);"
        "    return el.clientHeight > 180 && style.display !== 'none' && style.visibility !== 'hidden' && (el.scrollHeight > el.clientHeight + 24 || /auto|scroll/.test(style.overflowY));"
        "  }).sort((a, b) => {"
        "    const at = textOf(a); const bt = textOf(b);"
        "    const ac = /全部评论|评论\\(|留下你的精彩评论|分享\\s*回复|展开/.test(at) ? 1 : 0;"
        "    const bc = /全部评论|评论\\(|留下你的精彩评论|分享\\s*回复|展开/.test(bt) ? 1 : 0;"
        "    return (bc - ac) || ((b.clientHeight * b.clientWidth) - (a.clientHeight * a.clientWidth));"
        "  }).slice(0, 8);"
        "}"
        "function triggerCommentLoad(attempt) {"
        "  if (attempt % 10 === 0) activateCommentTab();"
        "  const scrollers = scrollCandidates();"
        "  if (scrollers.length) {"
        "    for (const scroller of scrollers) {"
        "      const step = Math.max(420, Math.floor(scroller.clientHeight * 0.7));"
        "      scroller.scrollTop += step;"
        "      scroller.dispatchEvent(new WheelEvent('wheel', {bubbles: true, deltaY: step}));"
        "    }"
        "  } else {"
        "    window.scrollBy(0, 720);"
        "    document.dispatchEvent(new WheelEvent('wheel', {bubbles: true, deltaY: 720}));"
        "  }"
        "}"
        "async function waitForTemplate(timeoutMs) {"
        "  const started = Date.now();"
        "  let lastUrls = [];"
        "  for (let attempt = 0; Date.now() - started < timeoutMs; attempt++) {"
        "    const urls = commentListUrls();"
        "    if (urls.length) return urls[0];"
        "    lastUrls = urls;"
        "    if (attempt % 3 === 0) triggerCommentLoad(attempt);"
        "    await sleep(attempt < 12 ? 700 : 1000);"
        "  }"
        "  const urls = commentListUrls();"
        "  return urls[0] || lastUrls[0] || '';"
        "}"
        "function imageKey(url) {"
        "  try { const u = new URL(url, location.href); return u.pathname.replace(/^\\/obj\\//, '/').split('~tplv-')[0].toLowerCase(); }"
        "  catch (_) { return String(url || '').split('~tplv-')[0]; }"
        "}"
        "function pushOne(comment, out, seen) {"
        "  if (!comment || typeof comment !== 'object') return;"
        "  const user = comment.user && (comment.user.nickname || comment.user.unique_id || comment.user.short_id) || '';"
        "  const text = comment.text || '';"
        "  const images = Array.isArray(comment.image_list) ? comment.image_list : [];"
        "  for (const image of images) {"
        "    let urls = [];"
        "    let width = 0;"
        "    let height = 0;"
        "    for (const field of ['origin_url', 'crop_url', 'medium_url', 'thumb_url', 'download_url']) {"
        "      const item = image && image[field];"
        "      const list = item && Array.isArray(item.url_list) ? item.url_list.filter(Boolean) : [];"
        "      if (!list.length) continue;"
        "      if (!urls.length) { urls = list.slice(0, 2); width = Number(item.width || image.width || 0); height = Number(item.height || image.height || 0); }"
        "      if (field === 'origin_url') break;"
        "    }"
        "    for (const src of urls) {"
        "      if (!/^https?:/i.test(src)) continue;"
        "      const key = imageKey(src);"
        "      if (!key || seen.has(key)) continue;"
        "      seen.add(key);"
        "      out.push({index: 200000 + out.length, src, width, height, clientWidth: 0, clientHeight: 0, top: 0, left: 0, text: `评论接口 ${user} ${text} 分享 回复`, commentUser: user, commentText: text, chain: []});"
        "      if (out.length >= maxImages) return;"
        "    }"
        "  }"
        "  const replies = Array.isArray(comment.reply_comment) ? comment.reply_comment : [];"
        "  for (const reply of replies) {"
        "    pushOne(reply, out, seen);"
        "    if (out.length >= maxImages) return;"
        "  }"
        "}"
        "async function fetchPage(template, cursor) {"
        "  const url = new URL(template);"
        "  url.searchParams.set('cursor', String(cursor));"
        "  url.searchParams.set('count', '20');"
        "  const response = await fetch(url.toString(), {credentials: 'include'});"
        "  if (!response.ok) return null;"
        "  return await response.json();"
        "}"
        "async function collectFromApi(template) {"
        "  const out = [];"
        "  const seen = new Set();"
        "  const batchSize = 8;"
        "  let emptyRounds = 0;"
        "  for (let start = 0; start < maxPages; start += batchSize) {"
        "    const cursors = Array.from({length: Math.min(batchSize, maxPages - start)}, (_, i) => (start + i) * 20);"
        "    const pages = await Promise.all(cursors.map(cursor => fetchPage(template, cursor).catch(() => null)));"
        "    let batchImages = 0;"
        "    for (const data of pages) {"
        "      const before = out.length;"
        "      const comments = data && Array.isArray(data.comments) ? data.comments : [];"
        "      for (const comment of comments) {"
        "        pushOne(comment, out, seen);"
        "        if (out.length >= maxImages) break;"
        "      }"
        "      batchImages += out.length - before;"
        "      if (out.length >= maxImages) break;"
        "    }"
        "    if (out.length >= maxImages) break;"
        "    emptyRounds = batchImages ? 0 : emptyRounds + 1;"
        "    if (emptyRounds >= 8) break;"
        "  }"
        "  return out;"
        "}"
        "await sleep(1600);"
        "let activatedTab = '';"
        "for (let attempt = 0; attempt < 5 && !activatedTab; attempt++) {"
        "  activatedTab = activateCommentTab();"
        "  await sleep(1200);"
        "  if (!activatedTab) triggerCommentLoad(attempt);"
        "}"
        "const waitMs = Math.max(45000, Math.min(90000, maxImages * 120));"
        "const template = await waitForTemplate(waitMs);"
        "const bodyText = (document.body && document.body.innerText || '').slice(0, 3000);"
        "const loginRequired = /扫码登录|验证码登录|密码登录|登录后|打开「抖音APP」|登录即代表同意/.test(bodyText);"
        "const commentImages = template ? await collectFromApi(template) : [];"
        "return {url: location.href, title: document.title || '', detailUrl: '', detail: null, loginRequired, activatedCommentTab: activatedTab, commentImages, apiTemplateFound: !!template};"
        "})()"
    )


def browser_snapshot_to_aweme(snapshot: dict) -> dict:
    detail = snapshot.get("detail")
    if isinstance(detail, dict) and detail:
        return prepare_browser_aweme_detail(detail, snapshot)

    final_url = str(snapshot.get("url") or "")
    aweme_id = extract_aweme_id(final_url) or "browser"
    title = browser_snapshot_title(snapshot) or f"作品-{aweme_id}"
    author = browser_snapshot_author(snapshot)
    videos = snapshot.get("videos") if isinstance(snapshot.get("videos"), list) else []
    video_urls = all_browser_video_urls(videos)
    video_url = video_urls[0] if video_urls else ""
    audio_url = first_browser_audio_url(videos)

    aweme: dict = {
        "aweme_id": aweme_id,
        "desc": title,
        "author": {"nickname": author},
    }
    if video_url:
        width, height = first_browser_video_dimensions(videos)
        aweme["video"] = {
            "url": video_url,
            "url_list": video_urls,
            "audio_url": audio_url,
            "width": width,
            "height": height,
            "format": "mp4",
            "codec": "browser-player",
        }
        return aweme

    images = filter_snapshot_aweme_images(snapshot.get("images") if isinstance(snapshot.get("images"), list) else [])
    image_items = []
    for item in images:
        if not isinstance(item, dict):
            continue
        url = normalize_url(str(item.get("src") or ""))
        if not is_http_url(url):
            continue
        image_items.append(
            {
                "url_list": [url],
                "width": to_int(item.get("width")),
                "height": to_int(item.get("height")),
            }
        )
    if image_items:
        aweme["images"] = image_items
    return aweme if aweme.get("video") or aweme.get("images") else {}


def filter_snapshot_aweme_images(images: list) -> list[dict]:
    aweme_images: list[dict] = []
    fallback_images: list[dict] = []
    seen: set[str] = set()
    for item in images:
        if not isinstance(item, dict):
            continue
        url = normalize_url(str(item.get("src") or ""))
        if not is_http_url(url):
            continue
        width = to_int(item.get("width"))
        height = to_int(item.get("height"))
        if width < 240 or height < 240:
            continue
        key = snapshot_image_identity(url)
        if not key or key in seen:
            continue
        if is_aweme_detail_image_url(url):
            seen.add(key)
            aweme_images.append(item)
        elif is_reasonable_fallback_image_url(url):
            fallback_images.append(item)
    if aweme_images:
        return aweme_images

    result: list[dict] = []
    seen.clear()
    for item in fallback_images:
        key = snapshot_image_identity(normalize_url(str(item.get("src") or "")))
        if key and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def extract_comment_images(raw_images: list, limit: int | None = None) -> list[ImageItem]:
    images: list[ImageItem] = []
    grouped: dict[str, dict] = {}
    order: list[str] = []
    for raw in raw_images:
        if not isinstance(raw, dict):
            continue
        url = normalize_url(str(raw.get("src") or ""))
        if not is_comment_image_url(url):
            continue
        width = to_int(raw.get("width"))
        height = to_int(raw.get("height"))
        client_width = to_int(raw.get("clientWidth"))
        client_height = to_int(raw.get("clientHeight"))
        if width < 180 or height < 180:
            continue
        if client_width and client_height and client_width < 40 and client_height < 40:
            continue
        key = comment_image_identity(url)
        if not key:
            continue
        if key not in grouped:
            grouped[key] = {
                "urls": [],
                "width": width,
                "height": height,
                "comment_user": str(raw.get("commentUser") or "").strip()[:80],
                "comment_text": str(raw.get("commentText") or raw.get("text") or "")[:300],
            }
            order.append(key)
        group = grouped[key]
        if url not in group["urls"]:
            group["urls"].append(url)
        if width * height > int(group["width"]) * int(group["height"]):
            group["width"] = width
            group["height"] = height
        if not group.get("comment_user"):
            group["comment_user"] = str(raw.get("commentUser") or "").strip()[:80]
        if not group.get("comment_text"):
            group["comment_text"] = str(raw.get("commentText") or raw.get("text") or "")[:300]

    for key in order:
        group = grouped[key]
        index = len(images) + 1
        images.append(
            ImageItem(
                index=index,
                width=int(group["width"]),
                height=int(group["height"]),
                candidates=comment_image_candidates(group["urls"], int(group["width"]), int(group["height"]), f"comment_{index:03d}"),
                comment_user=str(group.get("comment_user") or ""),
                comment_text=str(group.get("comment_text") or "")[:300],
            )
        )
        if limit and len(images) >= limit:
            break
    return images


def comment_image_candidates(urls: str | list[str], width: int, height: int, source: str) -> list[ImageCandidate]:
    candidates: list[ImageCandidate] = []
    values = [urls] if isinstance(urls, str) else urls
    candidate_urls: list[str] = []
    for url in values:
        for expanded_url in expanded_comment_image_urls(url):
            if expanded_url not in candidate_urls:
                candidate_urls.append(expanded_url)
    for index, candidate_url in enumerate(candidate_urls, start=1):
        lower_url = candidate_url.lower()
        candidates.append(
            ImageCandidate(
                url=candidate_url,
                source=f"{source}.candidate_{index}",
                width=width,
                height=height,
                preview="sc=thumb" in lower_url or "sc=watermark" in lower_url or "medium-webp" in lower_url or "x2-q75-r" in lower_url,
            )
        )
    return unique_image_candidates(candidates)


def expanded_comment_image_urls(url: str) -> list[str]:
    values = [url]
    if "~tplv-" in url:
        base, suffix = url.split("~tplv-", 1)
        query = "?" + suffix.split("?", 1)[1] if "?" in suffix else ""
        values.append(base + query)
        if "medium-webp" in url:
            values.insert(0, url.replace("medium-webp", "x2-q75-r").replace(".webp", ".image"))
    return unique_nonempty(values)


def is_comment_image_url(url: str) -> bool:
    lower = normalize_url(url).lower()
    if not is_http_url(lower):
        return False
    excluded = (
        "aweme-avatar",
        "avatar",
        "twemoji",
        "emoji",
        "ies.fe.effect",
        "sticker",
        "emblem.png",
        "douyin_web/media",
        "pcweb_cover",
        "biz_tag=pcweb_cover",
        "sc=cover",
        "sc=avatar",
        "s=profile",
        "passport-fe",
        "ucenter-web",
        "data:image",
    )
    if any(marker in lower for marker in excluded):
        return False
    accepted = (
        "p14lwwcsbr",
        "tos-cn-o-0812",
        "tos-cn-i-0813",
        "tos-cn-i-0813c",
        "tos-cn-i-p14",
        "douyinpic.com/obj/tos-cn-o",
        "douyinpic.com/tos-cn-i",
    )
    return "douyinpic.com" in lower and any(marker in lower for marker in accepted)


def comment_image_identity(url: str) -> str:
    parsed = urlparse(normalize_url(url))
    if not parsed.netloc or not parsed.path:
        return ""
    path = parsed.path
    if path.startswith("/obj/"):
        path = path[4:]
    if "~tplv-" in path:
        path = path.split("~tplv-", 1)[0]
    return path.lower()


def snapshot_image_identity(url: str) -> str:
    parsed = urlparse(normalize_url(url))
    if not parsed.netloc or not parsed.path:
        return ""
    return f"{parsed.netloc.lower()}{parsed.path}"


def is_aweme_detail_image_url(url: str) -> bool:
    lower = url.lower()
    if "pcweb_cover" in lower or "sc=cover" in lower or "biz_tag=pcweb_cover" in lower:
        return False
    return (
        "biz_tag=aweme_images" in lower
        or "packsourceenum_aweme_detail" in lower
        or "tplv-dy-aweme-images" in lower
    )


def is_reasonable_fallback_image_url(url: str) -> bool:
    lower = url.lower()
    excluded = (
        "aweme-avatar",
        "biz_tag=aweme_comment",
        "sticker_comment",
        "twemoji",
        "emoji",
        "flame_icon",
        "douyin_web/media",
        "pcweb_cover",
        "sc=cover",
        "sc=avatar",
        "s=profile",
    )
    return not any(marker in lower for marker in excluded)


def prepare_browser_aweme_detail(detail: dict, snapshot: dict) -> dict:
    audio_url = best_browser_audio_url(detail, snapshot)
    video = detail.get("video") if isinstance(detail.get("video"), dict) else {}
    bit_rates = video.get("bit_rate") if isinstance(video.get("bit_rate"), list) else []
    if audio_url:
        for item in bit_rates:
            if not isinstance(item, dict):
                continue
            video_format = str(item.get("format") or item.get("codec") or item.get("gear_name") or "").lower()
            is_split_stream = video_format == "dash" or item.get("is_bytevc1") == 1
            if is_split_stream:
                item["audio_url"] = audio_url
    return detail


def best_browser_audio_url(detail: dict, snapshot: dict) -> str:
    video = detail.get("video") if isinstance(detail.get("video"), dict) else {}
    for value in iter_audio_url_values(video.get("bit_rate_audio")):
        url = normalize_url(value)
        if is_http_url(url) and is_audio_url(url):
            return url
    videos = snapshot.get("videos") if isinstance(snapshot.get("videos"), list) else []
    return first_browser_audio_url(videos)


def iter_audio_url_values(value):
    decoded = decode_json_container(value)
    if decoded is not value:
        yield from iter_audio_url_values(decoded)
        return
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, list):
        for item in value:
            yield from iter_audio_url_values(item)
        return
    if not isinstance(value, dict):
        return
    for key in ("main_url", "backup_url", "fallback_url", "url", "src"):
        item = value.get(key)
        if isinstance(item, str):
            yield item
    for key in ("url_list", "urlList", "urls"):
        item = value.get(key)
        if isinstance(item, list):
            for url in item:
                if isinstance(url, str):
                    yield url
        elif isinstance(item, dict):
            yield from iter_audio_url_values(item)
    for child in value.values():
        if isinstance(child, (dict, list, str)):
            yield from iter_audio_url_values(child)


def browser_snapshot_title(snapshot: dict) -> str:
    h1 = snapshot.get("h1")
    if isinstance(h1, list):
        for item in h1:
            text = str(item or "").strip()
            if text and text != "推荐视频":
                return text[:80]
    text = str(snapshot.get("text") or "")
    for line in text.splitlines():
        line = line.strip()
        if len(line) > 10 and any(marker in line for marker in ("#", "@", "？", "?", "！", "!")):
            return line[:80]
    return ""


def browser_snapshot_author(snapshot: dict) -> str:
    text = str(snapshot.get("text") or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    ignored = {"我的", "我的喜欢", "我的收藏", "观看历史", "稍后再看", "我的作品", "我的预约"}
    for index, line in enumerate(lines):
        if "粉丝" in line and "获赞" in line:
            for previous in reversed(lines[max(0, index - 4) : index]):
                if previous and previous not in ignored and not previous.startswith(("http", "#")):
                    return previous[:40]

    links = snapshot.get("links")
    if isinstance(links, list):
        saw_mention = False
        for raw in links:
            text = str(raw or "").strip()
            if not text or text in ignored or text.startswith("http"):
                continue
            if text.startswith("@"):
                saw_mention = True
                continue
            if saw_mention:
                return text[:40]
        for raw in links:
            text = str(raw or "").strip()
            if text and text not in ignored and not text.startswith("@") and not text.startswith("http"):
                return text[:40]
    return "未知作者"


def first_browser_video_url(videos: list) -> str:
    for item in videos:
        url = snapshot_video_url(item)
        if url and not is_audio_url(url):
            return url
    return ""


def all_browser_video_urls(videos: list) -> list[str]:
    urls: list[str] = []
    for item in videos:
        url = snapshot_video_url(item)
        if url and not is_audio_url(url):
            urls.append(url)
    return unique_nonempty(urls)


def first_browser_audio_url(videos: list) -> str:
    for item in videos:
        if not isinstance(item, dict):
            continue
        url = normalize_url(str(item.get("src") or ""))
        if is_http_url(url) and is_audio_url(url):
            return url
    return ""


def snapshot_video_url(item) -> str:
    if not isinstance(item, dict):
        return ""
    url = normalize_url(str(item.get("src") or ""))
    if is_http_url(url) and is_probable_video_url(url) and not is_site_asset_video_url(url):
        return url
    return ""


def first_browser_video_dimensions(videos: list) -> tuple[int, int]:
    for item in videos:
        if not isinstance(item, dict):
            continue
        width = to_int(item.get("width"))
        height = to_int(item.get("height"))
        if width and height:
            return width, height
    return 0, 0


def extract_aweme_id(url: str) -> str:
    parsed = urlparse(url)
    patterns = (
        r"/video/(\d{8,})",
        r"/note/(\d{8,})",
        r"/share/video/(\d{8,})",
        r"/share/note/(\d{8,})",
    )
    for pattern in patterns:
        match = re.search(pattern, parsed.path)
        if match:
            return match.group(1)
    query = parse_qs(parsed.query)
    for key in ("modal_id", "aweme_id", "awemeId", "item_id", "itemId"):
        if query.get(key):
            return query[key][0]
    return ""


def author_name(aweme: dict) -> str:
    author = aweme.get("author") if isinstance(aweme.get("author"), dict) else {}
    return str(author.get("nickname") or author.get("unique_id") or author.get("short_id") or "未知作者").strip() or "未知作者"


def note_title(aweme: dict, aweme_id: str) -> str:
    desc = str(aweme.get("desc") or aweme.get("caption") or "").strip()
    for line in desc.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned[:48]
    return f"作品-{aweme_id}"


def extract_images(aweme: dict) -> list[ImageItem]:
    raw_images = aweme.get("images") or aweme.get("image_infos") or []
    if not isinstance(raw_images, list):
        return []
    images: list[ImageItem] = []
    for index, raw in enumerate(raw_images, start=1):
        if not isinstance(raw, dict):
            continue
        candidates: list[ImageCandidate] = []
        collect_image_candidates(raw, f"image_{index:03d}", candidates)
        candidates = unique_image_candidates(candidates)
        if candidates:
            images.append(
                ImageItem(
                    index=index,
                    width=first_int(raw, "width", "w"),
                    height=first_int(raw, "height", "h"),
                    candidates=candidates,
                )
            )
    return images


def collect_image_candidates(value, source: str, output: list[ImageCandidate]) -> None:
    decoded = decode_json_container(value)
    if decoded is not value:
        collect_image_candidates(decoded, source, output)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            collect_image_candidates(child, f"{source}[{index}]", output)
        return
    if not isinstance(value, dict):
        return
    width = first_int(value, "width", "w")
    height = first_int(value, "height", "h")
    for key in ("url_list", "urlList", "download_url_list", "downloadUrlList", "urls"):
        urls = value.get(key)
        if isinstance(urls, list):
            for url in urls:
                if isinstance(url, str) and is_http_url(url):
                    output.append(ImageCandidate(url=url, source=f"{source}.{key}", width=width, height=height))
    for key in ("url", "uri", "download_url", "downloadUrl"):
        url = value.get(key)
        if isinstance(url, str) and is_http_url(url):
            output.append(ImageCandidate(url=url, source=f"{source}.{key}", width=width, height=height))
    for key, child in value.items():
        if key in {"url_list", "urlList", "download_url_list", "downloadUrlList", "urls"}:
            continue
        if isinstance(child, (dict, list, str)):
            collect_image_candidates(child, f"{source}.{key}", output)


def unique_image_candidates(candidates: list[ImageCandidate]) -> list[ImageCandidate]:
    seen: set[str] = set()
    result: list[ImageCandidate] = []
    for candidate in candidates:
        url = normalize_url(candidate.url)
        if not url or url in seen:
            continue
        seen.add(url)
        candidate.url = url
        candidate.preview = is_preview_image_url(url)
        candidate.watermark = is_watermark_image_url(url)
        result.append(candidate)
    return result


def extract_videos(aweme: dict) -> list[dict]:
    video = aweme.get("video")
    if isinstance(video, dict):
        return [video]
    return []


def media_candidates(stream: dict, source_prefix: str) -> list[MediaCandidate]:
    candidates: list[MediaCandidate] = []
    collect_media_candidates(stream, source_prefix, candidates)
    seen: set[str] = set()
    result: list[MediaCandidate] = []
    for candidate in candidates:
        for url in expanded_video_urls(candidate.url):
            normalized = normalize_url(url)
            if not normalized or normalized in seen:
                continue
            if is_audio_url(normalized):
                continue
            seen.add(normalized)
            result.append(
                MediaCandidate(
                    url=normalized,
                    source=candidate.source,
                    codec=candidate.codec,
                    width=candidate.width,
                    height=candidate.height,
                    bitrate=candidate.bitrate,
                    declared_size=candidate.declared_size,
                    backup=candidate.backup,
                    watermark=is_watermark_url(normalized) or candidate.watermark,
                    audio_url=candidate.audio_url,
                )
            )
    return result


def collect_media_candidates(value, source: str, output: list[MediaCandidate], context: dict | None = None) -> None:
    decoded = decode_json_container(value)
    if decoded is not value:
        collect_media_candidates(decoded, source, output, context)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            collect_media_candidates(child, f"{source}[{index}]", output, context)
        return
    if not isinstance(value, dict):
        return

    current = {
        "width": first_int(value, "width", "videoWidth", "video_width", "w") or (context or {}).get("width", 0),
        "height": first_int(value, "height", "videoHeight", "video_height", "h") or (context or {}).get("height", 0),
        "bitrate": first_int(value, "bit_rate", "bitrate", "videoBitrate", "video_bitrate") or (context or {}).get("bitrate", 0),
        "size": first_int(value, "data_size", "size", "fileSize", "file_size") or (context or {}).get("size", 0),
        "codec": str(value.get("codec") or value.get("format") or value.get("gear_name") or (context or {}).get("codec", "")),
        "audio_url": str(value.get("audio_url") or value.get("audioUrl") or (context or {}).get("audio_url", "")),
    }

    url_fields = (
        "url_list",
        "urlList",
        "play_addr",
        "playAddr",
        "play_api",
        "playApi",
        "download_addr",
        "downloadAddr",
        "bit_rate_audio",
    )
    has_detailed_bit_rates = isinstance(value.get("bit_rate"), list) and bool(value.get("bit_rate"))
    for key in url_fields:
        if has_detailed_bit_rates and key in {
            "play_addr",
            "playAddr",
            "play_api",
            "playApi",
            "download_addr",
            "downloadAddr",
        }:
            continue
        child = value.get(key)
        if isinstance(child, list):
            for url in child:
                if isinstance(url, str) and is_http_url(url) and should_accept_media_url(url, f"{source}.{key}"):
                    output.append(media_candidate_from_context(url, f"{source}.{key}", current))
        elif isinstance(child, dict):
            collect_media_candidates(child, f"{source}.{key}", output, current)
        elif isinstance(child, str) and is_http_url(child):
            if should_accept_media_url(child, f"{source}.{key}"):
                output.append(media_candidate_from_context(child, f"{source}.{key}", current))

    for key in ("url", "src", "main_url", "mainUrl", "uri"):
        url = value.get(key)
        if isinstance(url, str) and is_http_url(url) and is_probable_video_url(url):
            output.append(media_candidate_from_context(url, f"{source}.{key}", current))

    for key, child in value.items():
        if key in set(url_fields) | {"url", "src", "main_url", "mainUrl", "uri"}:
            continue
        if isinstance(child, (dict, list, str)):
            collect_media_candidates(child, f"{source}.{key}", output, current)


def media_candidate_from_context(url: str, source: str, context: dict) -> MediaCandidate:
    return MediaCandidate(
        url=url,
        source=source,
        codec=str(context.get("codec") or ""),
        width=to_int(context.get("width")),
        height=to_int(context.get("height")),
        bitrate=to_int(context.get("bitrate")),
        declared_size=to_int(context.get("size")),
        backup=is_backup_url(url),
        watermark=is_watermark_url(url) or "wm" in source.lower(),
        audio_url=normalize_url(str(context.get("audio_url") or "")),
    )


def should_accept_media_url(url: str, source: str) -> bool:
    lower_source = source.lower()
    if is_probable_video_url(url):
        return True
    return any(marker in lower_source for marker in ("play_addr", "playaddr", "play_api", "download_addr", "downloadaddr", "bit_rate"))


def expanded_video_urls(url: str) -> list[str]:
    values = [url]
    if "playwm" in url:
        values.insert(0, url.replace("playwm", "play"))
    if "watermark=1" in url:
        values.insert(0, url.replace("watermark=1", "watermark=0"))
    return unique_nonempty(values)


def run_task_group(
    tasks: list[tuple[str, int, object]],
    worker_count: int,
    note_dir: Path,
    final_url: str,
    engine: DownloadEngine,
    report: dict,
    logger: LogFn,
    label: str,
    file_prefix: str = "",
) -> None:
    if not tasks:
        return
    actual_workers = max(1, min(worker_count, len(tasks)))
    if len(tasks) > 1:
        logger(f"{label}任务并发：{actual_workers}")
    completed_tasks = 0
    with ThreadPoolExecutor(max_workers=actual_workers) as executor:
        futures = [
            executor.submit(download_media_task, kind, index, payload, note_dir, final_url, engine, file_prefix)
            for kind, index, payload in tasks
        ]
        for future in as_completed(futures):
            completed_tasks += 1
            result = future.result()
            kind = result.get("kind")
            item = result.get("item", {})
            append_task_result(kind, item, report, logger)
            if len(tasks) > 1:
                logger(f"{label}进度：{completed_tasks}/{len(tasks)}")


def append_task_result(kind: str, item: dict, report: dict, logger: LogFn) -> None:
    if item.get("status") == "ok":
        if kind == "image":
            report["images"].append(item)
            action = "已投递 IDM" if item.get("engine") == "idm" else "成功"
            user_label = f" @{item.get('comment_user')}" if item.get("comment_user") else ""
            logger(f"[{item['index']:03d}] 图片{action}{user_label}：{item['width']}x{item['height']} {item['format']}，{item['elapsed_seconds']:.1f}s")
        else:
            report["videos"].append(item)
            action = "已投递 IDM" if item.get("engine") == "idm" else "成功"
            logger(f"[video {item['index']:03d}] {action}：{format_media_info(item['bytes'], item.get('dimensions', ''), item.get('declared_dimensions', ''))}，{item['elapsed_seconds']:.1f}s")
        return
    report["failures"].append(item)
    if kind == "image":
        report["images"].append(item)
        logger(f"[{item['index']:03d}] 图片失败：{item['error']}")
    else:
        report["videos"].append(item)
        logger(f"[video {item['index']:03d}] 失败：{item['error']}")


def download_media_task(kind: str, index: int, payload, note_dir: Path, final_url: str, engine: DownloadEngine, file_prefix: str = "") -> dict:
    session = make_session()
    start = time.perf_counter()
    try:
        if kind == "image":
            image: ImageItem = payload
            best = choose_best_image(session, image, final_url)
            prefix = f"{file_prefix}_" if file_prefix else ""
            watermark_tag = "wm" if best.candidate.watermark else "nowm_orig"
            if image.comment_user or image.comment_text:
                user_part = safe_filename(image.comment_user or "未知用户", 32)
                filename = f"{prefix}{image.index:03d}_{user_part}_{watermark_tag}_{best.width}x{best.height}.{best.extension}"
            else:
                filename = f"{prefix}{image.index:03d}_{watermark_tag}_{best.width}x{best.height}.{best.extension}"
            target = unique_path(note_dir / filename)
            bytes_count = save_or_enqueue_image(engine, best, target, final_url)
            item = {
                "index": image.index,
                "status": "ok",
                "file": str(target),
                "width": best.width,
                "height": best.height,
                "bytes": bytes_count,
                "format": best.image_format,
                "source": best.candidate.source,
                "url": best.candidate.url,
                "engine": engine.mode,
                "declared_width": image.width,
                "declared_height": image.height,
                "comment_user": image.comment_user,
                "comment_text": image.comment_text,
                "elapsed_seconds": round(time.perf_counter() - start, 3),
            }
            return {"kind": kind, "item": item}

        result = choose_best_media(session, payload, final_url, f"video_{index:03d}")
        prefix = f"{file_prefix}_" if file_prefix else ""
        watermark_tag = "wm" if result.candidate.watermark else "nowm"
        target = unique_path(note_dir / f"{prefix}video_{index:03d}_{watermark_tag}.mp4")
        declared_dimensions = candidate_dimensions(result.candidate)
        used_idm = media_will_use_idm(engine, result)
        bytes_count, dimensions = save_or_enqueue_media(engine, session, result, target, final_url, declared_dimensions)
        item = {
            "index": index,
            "status": "ok",
            "file": str(target),
            "bytes": bytes_count,
            "dimensions": dimensions,
            "declared_dimensions": declared_dimensions,
            "source": result.candidate.source,
            "codec": result.candidate.codec,
            "url": result.candidate.url,
            "engine": "idm" if used_idm else engine.mode,
            "elapsed_seconds": round(time.perf_counter() - start, 3),
        }
        return {"kind": kind, "item": item}
    except Exception as exc:  # noqa: BLE001
        return {
            "kind": kind,
            "item": {
                "index": index,
                "kind": kind,
                "status": "failed",
                "error": str(exc),
                "elapsed_seconds": round(time.perf_counter() - start, 3),
            },
        }


def choose_best_image(session: requests.Session, image: ImageItem, referer: str) -> ImageProbeResult:
    best: ImageProbeResult | None = None
    failures: list[str] = []
    for candidate in image.candidates:
        try:
            result = probe_image(session, candidate, referer)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{candidate.source}: {exc}")
            continue
        if best is None or result.score > best.score:
            best = result
    if best is None:
        raise DouyinDownloadError("所有图片候选都不可用。" + "; ".join(failures[:3]))
    return best


def probe_image(session: requests.Session, candidate: ImageCandidate, referer: str) -> ImageProbeResult:
    headers = dict(BASE_HEADERS)
    headers["Referer"] = referer or "https://www.douyin.com/"
    headers["Accept"] = "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
    try:
        response = session.get(candidate.url, headers=headers, timeout=(5, 25))
    except requests.RequestException as exc:
        raise DouyinDownloadError(f"请求失败：{exc}") from exc
    if response.status_code in {403, 404, 429}:
        raise DouyinDownloadError(f"HTTP {response.status_code}")
    if response.status_code >= 400:
        raise DouyinDownloadError(f"HTTP {response.status_code}")
    content = response.content
    if len(content) < 32:
        raise DouyinDownloadError("图片响应内容过短。")
    extension = extension_from_bytes(content, response.headers.get("Content-Type", ""))
    try:
        from io import BytesIO

        with Image.open(BytesIO(content)) as image:
            width, height = image.size
            image_format = (image.format or extension).lower()
    except UnidentifiedImageError as exc:
        raise DouyinDownloadError("响应不是可识别图片。") from exc
    return ImageProbeResult(
        candidate=candidate,
        content=content,
        extension=extension,
        image_format=image_format,
        width=width,
        height=height,
        content_type=response.headers.get("Content-Type", ""),
    )


def choose_best_media(session: requests.Session, stream: dict, referer: str, source_prefix: str) -> MediaProbeResult:
    candidates = media_candidates(stream, source_prefix)
    if not candidates:
        raise DouyinDownloadError("没有可用视频候选链接。")
    best: MediaProbeResult | None = None
    failures: list[str] = []
    for candidate in candidates:
        try:
            result = probe_media(session, candidate, referer)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{candidate.source}: {exc}")
            continue
        if best is None or result.score > best.score:
            best = result
    if best is None:
        raise DouyinDownloadError("所有视频候选都不可用。" + "; ".join(failures[:4]))
    return best


def probe_media(session: requests.Session, candidate: MediaCandidate, referer: str) -> MediaProbeResult:
    headers = dict(BASE_HEADERS)
    headers["Referer"] = referer or "https://www.douyin.com/"
    headers["Accept"] = "video/mp4,video/*,*/*;q=0.8"
    response = None
    try:
        response = session.head(candidate.url, headers=headers, timeout=(4, 10), allow_redirects=True)
    except requests.RequestException:
        response = None
    if response is None or response.status_code in {403, 405, 501} or response.status_code >= 500:
        try:
            response = session.get(
                candidate.url,
                headers={**headers, "Range": "bytes=0-1023"},
                timeout=(4, 10),
                allow_redirects=True,
                stream=True,
            )
        except requests.RequestException as exc:
            raise DouyinDownloadError(f"请求失败：{exc}") from exc
    if response.status_code in {403, 404, 429}:
        raise DouyinDownloadError(f"HTTP {response.status_code}")
    if response.status_code >= 400 and response.status_code != 416:
        raise DouyinDownloadError(f"HTTP {response.status_code}")
    content_length = content_length_from_headers(response.headers)
    if not content_length and response.status_code == 206:
        match = re.search(r"/(\d+)$", response.headers.get("Content-Range", ""))
        if match:
            content_length = int(match.group(1))
    if response.request.method == "GET":
        response.close()
    return MediaProbeResult(candidate=candidate, content_length=content_length, content_type=response.headers.get("Content-Type", ""))


def save_or_enqueue_image(engine: DownloadEngine, best: ImageProbeResult, target: Path, referer: str) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    if engine.should_use_idm(len(best.content)):
        enqueue_idm(engine, best.candidate.url, target, referer)
        return len(best.content)
    target.write_bytes(best.content)
    return len(best.content)


def save_or_enqueue_media(
    engine: DownloadEngine,
    session: requests.Session,
    result: MediaProbeResult,
    target: Path,
    referer: str,
    declared_dimensions: str = "",
) -> tuple[int, str]:
    if result.candidate.audio_url:
        return download_and_merge_media(session, result.candidate.url, result.candidate.audio_url, target, referer, declared_dimensions)
    size = result.content_length or result.candidate.declared_size
    if engine.should_use_idm_for_video(size):
        enqueue_idm(engine, result.candidate.url, target, referer)
        return size, declared_dimensions
    return download_media(session, result.candidate.url, target, referer, declared_dimensions)


def media_will_use_idm(engine: DownloadEngine, result: MediaProbeResult) -> bool:
    size = result.content_length or result.candidate.declared_size
    return engine.should_use_idm_for_video(size)


def enqueue_idm(engine: DownloadEngine, url: str, target: Path, referer: str) -> None:
    if not engine.idm_path or not engine.proxy:
        raise DouyinDownloadError("IDM 引擎未初始化。")
    target.parent.mkdir(parents=True, exist_ok=True)
    proxy_url = engine.proxy.register(url, referer)
    args = [str(engine.idm_path), "/d", proxy_url, "/p", str(target.parent), "/f", target.name, "/n"]
    try:
        subprocess.run(args, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, **NO_WINDOW_KWARGS)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DouyinDownloadError(f"投递 IDM 失败：{exc}") from exc


def download_media(session: requests.Session, url: str, target: Path, referer: str, declared_dimensions: str = "") -> tuple[int, str]:
    bytes_count = download_binary(session, url, target, referer, accept="video/mp4,video/*,*/*;q=0.8")
    return bytes_count, declared_dimensions or probe_video_dimensions(target)


def download_binary(session: requests.Session, url: str, target: Path, referer: str, accept: str = "*/*") -> int:
    headers = dict(BASE_HEADERS)
    headers["Referer"] = referer or "https://www.douyin.com/"
    headers["Accept"] = accept
    try:
        with session.get(url, headers=headers, timeout=(6, 60), allow_redirects=True, stream=True) as response:
            if response.status_code in {403, 404, 429}:
                raise DouyinDownloadError(f"媒体下载失败：HTTP {response.status_code}")
            if response.status_code >= 400:
                raise DouyinDownloadError(f"媒体下载失败：HTTP {response.status_code}")
            target.parent.mkdir(parents=True, exist_ok=True)
            bytes_count = 0
            with target.open("wb") as file:
                for chunk in response.iter_content(chunk_size=1024 * 512):
                    if chunk:
                        file.write(chunk)
                        bytes_count += len(chunk)
    except requests.RequestException as exc:
        raise DouyinDownloadError(f"媒体下载失败：{exc}") from exc
    if bytes_count < 32:
        raise DouyinDownloadError("媒体响应内容过短。")
    return bytes_count


def download_and_merge_media(
    session: requests.Session,
    video_url: str,
    audio_url: str,
    target: Path,
    referer: str,
    declared_dimensions: str = "",
) -> tuple[int, str]:
    ffmpeg = find_executable("ffmpeg")
    if not ffmpeg:
        raise DouyinDownloadError("该作品最高画质为音视频分轨资源，需要安装 ffmpeg 才能合并为有声音的视频。")

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="douyin_media_") as temp_dir:
        temp_path = Path(temp_dir)
        video_path = temp_path / "video.mp4"
        audio_path = temp_path / "audio.m4a"
        download_binary(session, video_url, video_path, referer, accept="video/mp4,video/*,*/*;q=0.8")
        download_binary(session, audio_url, audio_path, referer, accept="audio/*,video/mp4,*/*;q=0.8")
        merged_path = temp_path / "merged.mp4"
        result = subprocess.run(
            [
                ffmpeg,
                "-y",
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
                "-shortest",
                str(merged_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            **NO_WINDOW_KWARGS,
        )
        if result.returncode != 0 or not merged_path.exists():
            detail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
            raise DouyinDownloadError("音视频合并失败：" + " ".join(detail))
        shutil.move(str(merged_path), str(target))
    return target.stat().st_size, declared_dimensions or probe_video_dimensions(target)


def extract_browser_video_streams(final_url: str, aweme: dict, logger: LogFn) -> list[dict]:
    return []


def read_browser_video_elements(opencli_path: str, session_name: str) -> list[dict]:
    script = (
        "(() => {"
        "const videos = Array.from(document.querySelectorAll('video')).map((v, index) => ({"
        "index, src: v.currentSrc || v.src || v.getAttribute('src') || '',"
        "videoWidth: v.videoWidth || 0, videoHeight: v.videoHeight || 0, kind: 'dom'}));"
        "const resources = performance.getEntriesByType('resource')"
        ".map((entry, index) => ({index, src: entry.name || '', videoWidth: 0, videoHeight: 0, kind: 'resource'}))"
        ".filter(item => /\\.mp4(\\?|$)|douyinvod|amemv/i.test(item.src));"
        "return videos.concat(resources).filter(item => item.src);"
        "})()"
    )
    last_error: Exception | None = None
    for _attempt in range(8):
        try:
            stdout = run_opencli(opencli_path, ["browser", session_name, "eval", script], timeout=20)
            data = parse_json_from_output(stdout)
            if isinstance(data, list):
                items = [item for item in data if isinstance(item, dict)]
                if any(
                    is_http_url(str(item.get("src") or ""))
                    and is_probable_video_url(str(item.get("src") or ""))
                    and not is_audio_url(str(item.get("src") or ""))
                    for item in items
                ):
                    return items
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        time.sleep(1)
    if last_error:
        raise last_error
    return []


def run_opencli(opencli_path: str, args: list[str], timeout: int) -> str:
    command: str | list[str]
    shell = False
    if os.name == "nt" and opencli_path.lower().endswith((".cmd", ".bat")):
        direct_command = direct_opencli_node_command(opencli_path, args)
        if direct_command:
            command = direct_command
        else:
            command = "call " + cmd_quote([opencli_path, *args])
            shell = True
    else:
        command = [opencli_path, *args]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=shell,
        timeout=timeout,
        **NO_WINDOW_KWARGS,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        raise DouyinDownloadError(message or f"opencli exited with {result.returncode}")
    return result.stdout


def direct_opencli_node_command(opencli_path: str, args: list[str]) -> list[str]:
    cli_dir = Path(opencli_path).resolve().parent
    node_path = cli_dir / "node.exe"
    main_js = cli_dir / "node_modules" / "@jackwener" / "opencli" / "dist" / "src" / "main.js"
    if node_path.exists() and main_js.exists():
        return [str(node_path), str(main_js), *args]
    return []


def parse_json_from_output(output: str):
    decoder = json.JSONDecoder()
    for index, char in enumerate(output):
        if char not in "[{":
            continue
        try:
            value, _end = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        return value
    raise DouyinDownloadError("opencli 没有返回 JSON 数据。")


def safe_log_text(value) -> str:
    if isinstance(value, subprocess.TimeoutExpired):
        return f"命令超时 {value.timeout} 秒"
    text = str(value)
    if "Command '" in text and "timed out after" in text:
        match = re.search(r"timed out after ([\d.]+) seconds", text)
        seconds = match.group(1) if match else ""
        return f"命令超时{(' ' + seconds + ' 秒') if seconds else ''}"
    if len(text) > 500:
        text = text[:500] + "..."
    return text.encode("utf-8", "replace").decode("utf-8", "replace")


def cmd_quote(parts: list[str]) -> str:
    return " ".join(f'"{part.replace(chr(34), chr(34) + chr(34))}"' for part in parts)


def best_video_dimensions(aweme: dict) -> tuple[int, int]:
    video = aweme.get("video") if isinstance(aweme.get("video"), dict) else aweme
    return best_dimensions_from_value(video)


def best_dimensions_from_value(value) -> tuple[int, int]:
    decoded = decode_json_container(value)
    if decoded is not value:
        return best_dimensions_from_value(decoded)
    if isinstance(value, list):
        best = (0, 0)
        for child in value:
            child_dimensions = best_dimensions_from_value(child)
            if child_dimensions[0] * child_dimensions[1] > best[0] * best[1]:
                best = child_dimensions
        return best
    if not isinstance(value, dict):
        return (0, 0)
    best = (first_int(value, "width", "videoWidth", "video_width", "w"), first_int(value, "height", "videoHeight", "video_height", "h"))
    for child in value.values():
        if isinstance(child, (dict, list, str)):
            child_dimensions = best_dimensions_from_value(child)
            if child_dimensions[0] * child_dimensions[1] > best[0] * best[1]:
                best = child_dimensions
    return best


def probe_video_dimensions(path: Path) -> str:
    ffprobe = find_executable("ffprobe")
    if not ffprobe:
        return ""
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            **NO_WINDOW_KWARGS,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    try:
        data = json.loads(result.stdout)
        stream = (data.get("streams") or [{}])[0]
        width = to_int(stream.get("width"))
        height = to_int(stream.get("height"))
    except (json.JSONDecodeError, IndexError, AttributeError):
        return ""
    return f"{width}x{height}" if width and height else ""


def find_executable(name: str) -> str | None:
    executable_name = name if name.lower().endswith(".exe") or os.name != "nt" else f"{name}.exe"
    candidates: list[Path] = []
    bundle_dir = getattr(sys, "_MEIPASS", "")
    if bundle_dir:
        candidates.append(Path(bundle_dir) / executable_name)
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / executable_name)
    candidates.append(Path(__file__).resolve().parent / executable_name)
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return shutil.which(executable_name) or shutil.which(name)


def find_idm() -> Path | None:
    candidates = [
        shutil.which("IDMan.exe"),
        r"C:\Program Files (x86)\Internet Download Manager\IDMan.exe",
        r"C:\Program Files\Internet Download Manager\IDMan.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return Path(candidate)
    return None


def extension_from_bytes(content: bytes, content_type: str = "") -> str:
    if content.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "webp"
    content_type = content_type.lower()
    if "png" in content_type:
        return "png"
    if "webp" in content_type:
        return "webp"
    return "jpg"


def content_length_from_headers(headers) -> int:
    value = headers.get("Content-Length") or headers.get("content-length")
    try:
        return int(value) if value else 0
    except (TypeError, ValueError):
        return 0


def first_int(mapping: dict, *keys: str) -> int:
    for key in keys:
        number = to_int(mapping.get(key))
        if number:
            return number
    return 0


def to_int(value) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        match = re.search(r"\d+", value)
        if match:
            return int(match.group(0))
    return 0


def decode_json_container(value):
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if len(stripped) < 2 or stripped[0] not in "[{":
        return value
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        return value
    return decoded if isinstance(decoded, (dict, list)) else value


def normalize_url(url: str) -> str:
    url = html.unescape(url.strip())
    if url.startswith("//"):
        return "https:" + url
    return url


def is_http_url(url: str) -> bool:
    parsed = urlparse(normalize_url(url))
    return parsed.scheme in {"http", "https"}


def is_probable_video_url(url: str) -> bool:
    lower = url.lower()
    return any(marker in lower for marker in (".mp4", "douyinvod", "playwm", "play_addr", "amemv"))


def is_site_asset_video_url(url: str) -> bool:
    lower = url.lower()
    return "douyinstatic.com/obj/douyin-pc-web" in lower or "lf-douyin-pc-web" in lower


def is_watermark_url(url: str) -> bool:
    lower = url.lower()
    return "playwm" in lower or "watermark=1" in lower


def is_watermark_image_url(url: str) -> bool:
    lower = url.lower()
    return (
        "sc=watermark" in lower
        or "watermark=1" in lower
        or "/watermark/" in lower
        or "tplv-dy-water" in lower
        or "~tplv-dy-water" in lower
    )


def is_audio_url(url: str) -> bool:
    lower = url.lower()
    return "media-audio" in lower or "audio-" in lower or "mime_type=audio" in lower


def is_backup_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return "backup" in host or "bak" in host


def is_preview_image_url(url: str) -> bool:
    lower = url.lower()
    return any(marker in lower for marker in ("thumb", "cover", "300x", "resize", "blur"))


def format_media_info(bytes_count: int, dimensions: str, declared_dimensions: str = "") -> str:
    size_mb = bytes_count / 1024 / 1024
    if dimensions:
        return f"{dimensions}，{size_mb:.2f} MB"
    if declared_dimensions:
        return f"页面声明 {declared_dimensions}，{size_mb:.2f} MB"
    return f"{size_mb:.2f} MB"


def candidate_dimensions(candidate: MediaCandidate) -> str:
    return f"{candidate.width}x{candidate.height}" if candidate.width and candidate.height else ""


def unique_nonempty(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = str(value or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def safe_filename(value: str, limit: int = 80) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = "未命名"
    return cleaned[:limit].rstrip(" .") or "未命名"


def has_existing_aweme_nowm_video(output_root: Path, aweme_id: str, media_names: list[str] | None = None) -> bool:
    if not aweme_id:
        return False
    if media_names is not None:
        return any(aweme_id in name and "_nowm" in name.lower() and name.lower().endswith(".mp4") for name in media_names)
    return any(output_root.glob(f"*{aweme_id}*video_*_nowm.mp4"))


def has_existing_aweme_nowm_images(output_root: Path, aweme_id: str, media_names: list[str] | None = None) -> bool:
    if not aweme_id:
        return False
    suffixes = (".jpg", ".jpeg", ".png", ".webp")
    if media_names is not None:
        return any(aweme_id in name and "_nowm_orig_" in name.lower() and name.lower().endswith(suffixes) for name in media_names)
    patterns = [
        f"*{aweme_id}*_*_nowm_orig_*.jpg",
        f"*{aweme_id}*_*_nowm_orig_*.jpeg",
        f"*{aweme_id}*_*_nowm_orig_*.png",
        f"*{aweme_id}*_*_nowm_orig_*.webp",
    ]
    return any(any(output_root.glob(pattern)) for pattern in patterns)


def existing_media_filenames(output_root: Path) -> list[str]:
    suffixes = {".jpg", ".jpeg", ".png", ".webp", ".mp4"}
    try:
        return [path.name for path in output_root.iterdir() if path.is_file() and path.suffix.lower() in suffixes]
    except OSError:
        return []


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    for index in range(1, 1000):
        candidate = parent / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise DouyinDownloadError(f"无法生成不重复文件名：{path}")
