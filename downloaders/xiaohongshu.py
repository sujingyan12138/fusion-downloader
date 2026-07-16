from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qs, quote, urlencode, urlparse

import requests
from requests.adapters import HTTPAdapter
from PIL import Image, UnidentifiedImageError

from . import douyin


LogFn = Callable[[str], None]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
        "image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": "https://www.xiaohongshu.com/",
}

ACTIVE_PROXIES: list["HeaderProxyServer"] = []


class XhsDownloadError(RuntimeError):
    """Raised when a Xiaohongshu note cannot be parsed or downloaded."""


@dataclass
class DownloadEngine:
    mode: str = "builtin"
    idm_path: Path | None = None
    proxy: "HeaderProxyServer | None" = None
    idm_threshold_bytes: int = douyin.IDM_LARGE_FILE_THRESHOLD
    idm_video_threshold_bytes: int = douyin.IDM_VIDEO_FILE_THRESHOLD

    @property
    def name(self) -> str:
        if self.mode == "idm" and self.idm_path:
            return f"IDM ({self.idm_path})"
        if self.mode == "smart" and self.idm_path:
            return f"智能模式（视频>{douyin.format_bytes(self.idm_video_threshold_bytes)}或大小未知用 IDM）"
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
class Candidate:
    url: str
    source: str
    preview: bool = False
    transformed: bool = False


@dataclass
class ProbeResult:
    candidate: Candidate
    content: bytes
    extension: str
    image_format: str
    width: int
    height: int
    content_type: str = ""
    bytes_count: int = 0

    @property
    def area(self) -> int:
        return self.width * self.height


@dataclass
class ImageItem:
    index: int
    file_id: str = ""
    trace_id: str = ""
    declared_width: int = 0
    declared_height: int = 0
    url_default: str = ""
    info_urls: list[tuple[str, str]] = field(default_factory=list)
    stream: dict = field(default_factory=dict)
    live_photo: bool = False
    comment_user: str = ""
    comment_text: str = ""
    comment_id: str = ""
    source: str = ""

    @property
    def declared_area(self) -> int:
        return self.declared_width * self.declared_height


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
            self.candidate.declared_area,
            self.candidate.bitrate,
            size,
            0 if self.candidate.backup else 1,
            codec_rank,
        )


def download_note(
    input_text: str,
    output_root: str | Path,
    prefer_format: str = "original",
    log: LogFn | None = None,
    max_workers: int = 4,
    use_idm: bool | str = "smart",
    video_only: bool = False,
) -> dict:
    """Download original-quality images, videos, and Live Photos from a note.

    Args:
        input_text: Share text or a note URL.
        output_root: Root folder, usually the app-adjacent "小红书爬取".
        prefer_format: Currently only "original" is used; kept for the GUI/API.
        log: Optional callback for progress messages.
        max_workers: Concurrent media downloads inside one note.
        use_idm: "smart" uses IDM only for large files when available; "auto" keeps the old IDM-first behavior.
    """

    if prefer_format != "original":
        raise ValueError('Only prefer_format="original" is supported in v1.')

    logger = log or (lambda _message: None)
    source_url = extract_url(input_text)
    logger(f"识别链接：{source_url}")

    session = make_session()

    html, final_url = fetch_note_html(session, source_url)
    state = parse_initial_state(html)
    note_id, note = find_note(state, final_url)
    author = str(note.get("user", {}).get("nickname") or "未知博主").strip() or "未知博主"
    title = note_title(note, note_id)
    images = extract_images(note)
    note_videos = extract_note_videos(note)
    if note_videos:
        runtime_videos = extract_browser_video_streams(final_url, note, logger)
        if runtime_videos:
            note_videos = runtime_videos
    if video_only:
        images = []
    live_photo_count = 0 if video_only else sum(1 for image in images if image.live_photo and image.stream)

    if not images and not note_videos:
        if video_only:
            raise XhsDownloadError("该笔记不是视频笔记。")
        raise XhsDownloadError("没有在该笔记里解析到图片、视频或 Live Photo 动图。")

    note_dir = Path(output_root)
    note_dir.mkdir(parents=True, exist_ok=True)
    file_prefix = safe_filename(f"小红书_{author}_{title}_{note_id}", 120)

    logger(f"作者：{author}")
    logger(f"图片数量：{len(images)}")
    if live_photo_count:
        logger(f"Live Photo 动图数量：{live_photo_count}")
    if note_videos:
        logger(f"视频数量：{len(note_videos)}")
    logger(f"保存目录：{note_dir}")

    report = {
        "input": input_text,
        "source_url": source_url,
        "final_url": final_url,
        "note_id": note_id,
        "author": author,
        "title": title,
        "output_dir": str(note_dir),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "images": [],
        "live_photos": [],
        "videos": [],
        "failures": [],
        "skipped": [],
    }
    existing_media_names = existing_media_filenames(note_dir)
    if has_existing_note_download(note_dir, note_id, existing_media_names):
        logger(f"已存在下载结果，跳过下载：{note_id}")
        report["skipped"].append({"note_id": note_id, "reason": "exists"})
        report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        report["elapsed_seconds"] = 0
        return report

    engine = make_download_engine(use_idm)
    report["download_engine"] = engine.name
    logger(f"下载引擎：{engine.name}")

    started_at = time.perf_counter()
    image_tasks = [("image", image.index, image) for image in images]
    live_tasks = [("live_photo", image.index, image) for image in images if image.live_photo and image.stream]
    video_tasks = [("video", index, stream) for index, stream in enumerate(note_videos, start=1)]

    if engine.mode == "idm":
        image_workers = max(1, min(max_workers, 6))
        media_workers = max(1, min(max_workers, 8))
    elif engine.mode == "smart" and engine.idm_path:
        image_workers = max(1, min(max_workers, 2))
        media_workers = max(1, min(max_workers, 6))
    else:
        image_workers = max(1, min(max_workers, 2))
        media_workers = max(1, min(max_workers, 4))

    run_task_group(image_tasks, image_workers, note_dir, final_url, engine, report, logger, "图片", file_prefix=file_prefix)
    run_task_group(live_tasks + video_tasks, media_workers, note_dir, final_url, engine, report, logger, "视频/动图", file_prefix=file_prefix)

    report["images"].sort(key=lambda item: item.get("index", 0))
    report["live_photos"].sort(key=lambda item: item.get("index", 0))
    report["videos"].sort(key=lambda item: item.get("index", 0))
    report["elapsed_seconds"] = round(time.perf_counter() - started_at, 3)
    report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    ok_count = sum(1 for item in report["images"] if item.get("status") == "ok")
    live_ok_count = sum(1 for item in report["live_photos"] if item.get("status") == "ok")
    video_ok_count = sum(1 for item in report["videos"] if item.get("status") == "ok")
    total_count = len(images) + live_photo_count + len(note_videos)
    total_ok_count = ok_count + live_ok_count + video_ok_count
    logger(
        f"完成：图片 {ok_count}/{len(images)}，Live Photo {live_ok_count}/{live_photo_count}，"
        f"视频 {video_ok_count}/{len(note_videos)}"
    )
    if total_count and total_ok_count == 0:
        raise XhsDownloadError("所有媒体都下载失败，请检查链接是否失效或是否触发访问验证。")

    if engine.mode == "idm":
        logger("已投递到 IDM。请在 IDM 主窗口查看下载进度。")

    if engine.proxy:
        report["idm_proxy"] = engine.proxy.base_url

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
    logger("评论区图片策略：复用小红书浏览器登录态，在页面内分页读取评论接口和可见评论图片。")
    if limit is not None and limit <= 0:
        limit = None

    session = make_session()
    html, final_url = fetch_note_html(session, source_url)
    state = parse_initial_state(html)
    note_id, note = find_note(state, final_url)
    author = str(note.get("user", {}).get("nickname") or "未知博主").strip() or "未知博主"
    title = note_title(note, note_id)
    xsec_token = str(note.get("xsecToken") or query_value(final_url, "xsec_token") or query_value(source_url, "xsec_token") or "")

    snapshot = read_comment_image_snapshot(final_url or source_url, note_id, xsec_token, limit, logger)
    if snapshot.get("loginRequired"):
        logger("检测到小红书浏览器登录态不可用：请先点击“登录小红书”，扫码登录后再爬取评论区图片。")
    browser_final_url = str(snapshot.get("url") or "")
    if browser_final_url:
        final_url = browser_final_url
    raw_images = snapshot.get("commentImages") if isinstance(snapshot.get("commentImages"), list) else []
    images = comment_images_from_snapshot(raw_images, limit)
    if not images:
        raise XhsDownloadError("没有在评论区解析到图片。小红书评论区通常需要先登录，或该笔记评论里没有图片。")

    note_dir = Path(output_root)
    note_dir.mkdir(parents=True, exist_ok=True)
    file_prefix = safe_filename(f"小红书评论_{author}_{title}_{note_id}", 120)

    logger(f"作者：{author}")
    logger(f"评论区图片数量：{len(images)}" + (f"（上限 {limit}）" if limit else ""))
    logger(f"保存目录：{note_dir}")
    logger("评论区图片使用内置并发下载，避免 IDM 队列漏下。")

    report = {
        "input": input_text,
        "source_url": source_url,
        "final_url": final_url,
        "note_id": note_id,
        "author": author,
        "title": title,
        "output_dir": str(note_dir),
        "limit": limit,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "images": [],
        "live_photos": [],
        "videos": [],
        "failures": [],
        "download_engine": "内置下载器",
        "browser_engine": str(snapshot.get("browserEngine") or ""),
        "raw_comment_image_candidates": len(raw_images),
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
        raise XhsDownloadError("评论区图片全部下载失败，请检查登录态或稍后重试。")
    return report


def list_collections(log: LogFn | None = None) -> list[dict]:
    logger = log or (lambda _message: None)
    logger("正在读取小红书收藏作品和专辑入口...")
    favorite_snapshot = read_favorite_video_snapshot(None, logger)
    if favorite_snapshot.get("loginRequired"):
        raise XhsDownloadError("当前小红书登录态不可用。请先点击“登录小红书”，扫码登录后再刷新收藏作品。")
    count = favorite_snapshot.get("visibleCount") or len(favorite_snapshot.get("notes") if isinstance(favorite_snapshot.get("notes"), list) else [])
    collections = [{"id": "__all_favorites__", "name": "全部收藏作品", "count": count}]

    try:
        board_snapshot = read_collection_list_snapshot(logger)
    except Exception as exc:  # noqa: BLE001 - all favorites remains usable.
        logger(f"读取专辑列表失败，仅保留全部收藏作品入口：{exc}")
        board_snapshot = {}
    boards = board_snapshot.get("boards") if isinstance(board_snapshot.get("boards"), list) else []
    seen = {"__all_favorites__"}
    for board in boards:
        if not isinstance(board, dict):
            continue
        board_id = find_collection_note_value(board, ("boardId", "board_id", "id"))
        if not board_id or board_id in seen:
            continue
        seen.add(board_id)
        name = find_collection_note_value(board, ("name", "title", "boardName", "board_name")) or f"专辑 {board_id}"
        board_count = find_collection_note_value(board, ("noteCount", "note_count", "count", "notesCount", "notes_count"))
        collections.append({"id": board_id, "name": f"专辑：{name}", "count": board_count})
    logger(f"检测到全部收藏作品 {count} 个，专辑 {len(collections) - 1} 个；下载时会保存图片、视频和 Live Photo，已存在作品会自动跳过。")
    return collections


def download_collection(
    collection_id: str,
    collection_name: str,
    output_root: str | Path,
    limit: int | None = None,
    log: LogFn | None = None,
    max_workers: int = 4,
    use_idm: bool | str = "smart",
) -> dict:
    if collection_id in {"", "__all__", "__all_videos__", "__all_favorites__"}:
        return download_favorite_videos(output_root, limit=limit, log=log, max_workers=max_workers, use_idm=use_idm)
    return download_xhs_collection_notes(collection_id, collection_name, output_root, limit=limit, log=log, max_workers=max_workers, use_idm=use_idm)


def download_favorite_videos(
    output_root: str | Path,
    limit: int | None = None,
    log: LogFn | None = None,
    max_workers: int = 4,
    use_idm: bool | str = "smart",
) -> dict:
    logger = log or (lambda _message: None)
    if limit is not None and limit <= 0:
        limit = None

    logger("收藏作品：全部收藏作品")
    snapshot = read_favorite_video_snapshot(limit, logger)
    if snapshot.get("loginRequired"):
        raise XhsDownloadError("当前小红书登录态不可用。请先点击“登录小红书”，扫码登录后再下载收藏作品。")
    notes = snapshot.get("notes") if isinstance(snapshot.get("notes"), list) else []
    urls = collection_note_urls(notes)
    if not urls:
        raise XhsDownloadError("没有从收藏页读取到作品。请确认小红书登录窗口能打开“我-收藏-笔记”。")

    collection_dir = Path(output_root)
    collection_dir.mkdir(parents=True, exist_ok=True)
    collection_prefix = safe_filename("小红书收藏作品_全部收藏作品", 120)
    report = {
        "collection_id": "__all_favorites__",
        "collection_name": "全部收藏作品",
        "output_dir": str(collection_dir),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "items": [],
        "failures": [],
        "skipped": [],
    }

    download_xhs_url_collection_items(
        urls,
        "小红书收藏作品",
        collection_dir,
        report,
        logger,
        max_workers=max_workers,
        use_idm=use_idm,
        limit=limit,
    )

    report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    report["ok_count"] = len(report["items"])
    report["failed_count"] = len(report["failures"])
    report["skipped_count"] = len(report["skipped"])
    logger(f"收藏作品完成：成功 {report['ok_count']}，跳过 {report['skipped_count']}，失败 {report['failed_count']}")
    if not report["items"] and report["failures"]:
        raise XhsDownloadError("收藏作品全部下载失败，请检查登录态或稍后重试。")
    return report


def download_xhs_collection_notes(
    collection_id: str,
    collection_name: str,
    output_root: str | Path,
    limit: int | None = None,
    log: LogFn | None = None,
    max_workers: int = 4,
    use_idm: bool | str = "smart",
) -> dict:
    logger = log or (lambda _message: None)
    if limit is not None and limit <= 0:
        limit = None

    display_name = collection_name or collection_id
    logger(f"小红书专辑：{display_name}")
    snapshot = read_collection_notes_snapshot(collection_id, limit, logger)
    if snapshot.get("loginRequired"):
        raise XhsDownloadError("当前小红书登录态不可用。请先点击“登录小红书”，扫码登录后再下载专辑。")
    notes = snapshot.get("notes") if isinstance(snapshot.get("notes"), list) else []
    urls = collection_note_urls(notes)
    if not urls:
        raise XhsDownloadError("没有从专辑页读取到作品。请确认该专辑里有可访问的收藏作品。")

    collection_dir = Path(output_root)
    collection_dir.mkdir(parents=True, exist_ok=True)
    collection_prefix = safe_filename(f"小红书专辑_{display_name}", 120)
    report = {
        "collection_id": collection_id,
        "collection_name": display_name,
        "output_dir": str(collection_dir),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "items": [],
        "failures": [],
        "skipped": [],
    }

    download_xhs_url_collection_items(
        urls,
        "小红书专辑作品",
        collection_dir,
        report,
        logger,
        max_workers=max_workers,
        use_idm=use_idm,
        limit=limit,
    )

    report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    report["ok_count"] = len(report["items"])
    report["failed_count"] = len(report["failures"])
    report["skipped_count"] = len(report["skipped"])
    logger(f"专辑下载完成：成功 {report['ok_count']}，跳过 {report['skipped_count']}，失败 {report['failed_count']}")
    if not report["items"] and report["failures"]:
        raise XhsDownloadError("专辑作品全部下载失败，请检查登录态或稍后重试。")
    return report


def download_xhs_url_collection_items(
    urls: list[str],
    label: str,
    collection_dir: Path,
    report: dict,
    logger: LogFn,
    max_workers: int,
    use_idm: bool | str,
    limit: int | None,
) -> None:
    existing_media_names = existing_media_filenames(collection_dir)
    pending: list[tuple[int, str, str]] = []
    for index, url in enumerate(urls, start=1):
        note_id = extract_note_id_from_url(url)
        if note_id and has_existing_note_download(collection_dir, note_id, existing_media_names):
            logger(f"已存在，跳过{label} {index}/{len(urls)}：{note_id}")
            report["skipped"].append({"index": index, "note_id": note_id, "url": url, "reason": "exists"})
            continue
        pending.append((index, note_id, url))

    if limit:
        pending = pending[:limit]

    collection_workers, per_item_workers = split_collection_workers(len(pending), max_workers)
    if len(pending) > 1:
        logger(f"{label}并发：作品 {collection_workers}，单作品媒体 {per_item_workers}")

    def run_one(item: tuple[int, str, str]) -> dict:
        index, note_id, url = item
        logger(f"\n----- {label} {index}/{len(urls)} -----")
        logger(url)
        try:
            item_report = download_note(url, collection_dir, log=logger, max_workers=per_item_workers, use_idm=use_idm)
            return {"kind": "item", "item": {"index": index, "note_id": note_id, "url": url, "status": "ok", "report": item_report}}
        except Exception as exc:  # noqa: BLE001 - keep collection processing going.
            message = str(exc)
            logger(f"{label}失败：{message}")
            return {"kind": "failure", "item": {"index": index, "note_id": note_id, "url": url, "error": message}}

    if not pending:
        return
    if collection_workers == 1:
        results = [run_one(item) for item in pending]
    else:
        results = []
        with ThreadPoolExecutor(max_workers=collection_workers) as executor:
            futures = [executor.submit(run_one, item) for item in pending]
            for future in as_completed(futures):
                results.append(future.result())

    for result in sorted(results, key=lambda item: item.get("item", {}).get("index", 0)):
        if result.get("kind") == "item":
            report["items"].append(result["item"])
        else:
            report["failures"].append(result["item"])


def split_collection_workers(total_items: int, max_workers: int) -> tuple[int, int]:
    if total_items <= 1:
        return 1, max(1, max_workers)
    if max_workers <= 2:
        return 1, max(1, max_workers)
    collection_workers = min(total_items, 3 if max_workers >= 8 else 2)
    per_item_workers = 3 if max_workers >= 8 else 2
    return collection_workers, per_item_workers


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(BASE_HEADERS)
    adapter = HTTPAdapter(pool_connections=16, pool_maxsize=16)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def make_download_engine(use_idm: bool | str = "smart") -> DownloadEngine:
    mode = douyin.normalize_download_engine_mode(use_idm)
    if mode == "builtin":
        return DownloadEngine()
    idm_path = find_idm()
    if not idm_path:
        return DownloadEngine()
    proxy = HeaderProxyServer()
    ACTIVE_PROXIES.append(proxy)
    return DownloadEngine(mode="idm" if mode == "auto" else "smart", idm_path=idm_path, proxy=proxy)


def xhs_browser_profile_dir() -> Path:
    base_dir = douyin.app_base_dir()
    dist_profile = base_dir / "dist" / "小红书浏览器登录态"
    if dist_profile.exists():
        return dist_profile
    return base_dir / "小红书浏览器登录态"


def read_xhs_login_context() -> dict:
    return evaluate_in_xhs_browser("https://www.xiaohongshu.com/explore", build_login_context_script(), timeout=45)


def read_comment_image_snapshot(url: str, note_id: str, xsec_token: str, limit: int | None, logger: LogFn) -> dict:
    logger("正在使用软件内置浏览器读取小红书评论区图片（复用小红书登录态）...")
    value = evaluate_in_xhs_browser(url, build_comment_image_snapshot_script(note_id, xsec_token, limit), timeout=comment_browser_timeout(limit))
    if isinstance(value, dict):
        value["browserEngine"] = "Built-in CDP"
        value["profileDir"] = str(xhs_browser_profile_dir())
        return value
    return {"browserEngine": "Built-in CDP", "profileDir": str(xhs_browser_profile_dir())}


def read_collection_list_snapshot(logger: LogFn) -> dict:
    logger("正在使用软件内置浏览器读取小红书收藏入口...")
    value = evaluate_in_xhs_browser("https://www.xiaohongshu.com/explore", build_collection_list_script(), timeout=75)
    return value if isinstance(value, dict) else {}


def read_collection_notes_snapshot(collection_id: str, limit: int | None, logger: LogFn) -> dict:
    logger("正在使用软件内置浏览器分页读取小红书收藏作品...")
    value = evaluate_in_xhs_browser("https://www.xiaohongshu.com/explore", build_collection_notes_script(collection_id, limit), timeout=collection_browser_timeout(limit))
    return value if isinstance(value, dict) else {}


def read_favorite_video_snapshot(limit: int | None, logger: LogFn) -> dict:
    logger("正在使用软件内置浏览器读取小红书“收藏-笔记”页...")
    value = evaluate_in_xhs_browser("https://www.xiaohongshu.com/explore", build_favorite_video_script(limit), timeout=collection_browser_timeout(limit))
    return value if isinstance(value, dict) else {}


def evaluate_in_xhs_browser(url: str, expression: str, timeout: int = 60) -> dict:
    try:
        import websocket  # type: ignore[import-not-found]
    except ImportError as exc:
        raise XhsDownloadError("缺少 websocket-client 依赖，请重新安装依赖。") from exc

    browser_path = douyin.find_chromium_browser()
    if not browser_path:
        raise XhsDownloadError("未找到 Chrome 或 Edge。")

    profile_dir = xhs_browser_profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)
    process: subprocess.Popen | None = None
    reuse_existing = False
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
            port, process, reuse_existing = douyin.open_comment_cdp_port(browser_path, profile_dir, force_new=True)
            target = douyin.create_cdp_target(port)
        ws_url = str(target.get("webSocketDebuggerUrl") or "")
        if not ws_url:
            raise XhsDownloadError("Chrome DevTools 没有返回 WebSocket 地址。")
        ws = websocket.create_connection(ws_url, timeout=8)
        try:
            cdp = douyin.CdpClient(ws)
            cdp.call("Page.enable", timeout=8)
            cdp.call("Runtime.enable", timeout=8)
            cdp.call("Network.enable", timeout=8)
            if target.get("id"):
                try:
                    cdp.call("Page.bringToFront", timeout=8)
                except Exception:
                    pass
            cdp.call("Page.navigate", {"url": url}, timeout=8)
            value = douyin.evaluate_after_navigation(cdp, expression, timeout=timeout)
            return value if isinstance(value, dict) else {}
        finally:
            try:
                ws.close()
            except Exception:
                pass
    except douyin.DouyinDownloadError as exc:
        raise XhsDownloadError(str(exc)) from exc
    finally:
        if process is not None and not reuse_existing:
            douyin.terminate_process(process)


def comment_browser_timeout(limit: int | None) -> int:
    if not limit:
        return 420
    return max(180, min(900, int(limit) * 2 + 180))


def collection_browser_timeout(limit: int | None) -> int:
    if not limit:
        return 420
    return max(180, min(900, int(limit) * 2 + 180))


def build_login_context_script() -> str:
    return (
        "(async () => {"
        "const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));"
        "await sleep(1800);"
        "const bodyText = (document.body && document.body.innerText || '').slice(0, 3000);"
        "const loginRequired = /登录后|扫码|手机号登录|获取验证码|登录即代表|请先登录/.test(bodyText);"
        "let me = null;"
        "try {"
        "  const response = await fetch('https://edith.xiaohongshu.com/api/sns/web/v2/user/me', {credentials:'include', headers:{accept:'application/json, text/plain, */*'}});"
        "  me = await response.json();"
        "} catch (error) { me = {error:String(error)}; }"
        "return {url:location.href, title:document.title || '', cookie:document.cookie || '', loginRequired, me, bodyText};"
        "})()"
    )


def build_comment_image_snapshot_script(note_id: str, xsec_token: str, limit: int | None = None) -> str:
    note_json = json.dumps(str(note_id), ensure_ascii=False)
    token_json = json.dumps(str(xsec_token), ensure_ascii=False)
    max_images = max(3, int(limit)) if limit else 0
    max_pages = 220 if not max_images else max(20, min(300, max_images * 3 + 20))
    return (
        "(async () => {"
        "const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));"
        f"const noteId = {note_json};"
        f"const initialXsecToken = {token_json};"
        f"const maxImages = {max_images};"
        f"const maxPages = {max_pages};"
        "const imageFormats = 'jpg,webp,avif';"
        "const allImages = [];"
        "const seen = new Set();"
        "function textOf(el) { return ((el && (el.innerText || el.textContent)) || '').trim(); }"
        "function visibleRect(el) { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth ? r : null; }"
        "function clickElement(el) { const r = visibleRect(el); if (!r) return false; const opts = {bubbles:true,cancelable:true,view:window,clientX:r.left+r.width/2,clientY:r.top+r.height/2}; for (const type of ['pointerdown','mousedown','pointerup','mouseup','click']) el.dispatchEvent(new MouseEvent(type, opts)); if (typeof el.click === 'function') el.click(); return true; }"
        "function activateCommentTab() { const rows = Array.from(document.querySelectorAll('button,div,span')).map(el => ({el,text:textOf(el),rect:visibleRect(el)})).filter(item => item.rect && (/^评论\\s*(\\(\\d+\\))?$/.test(item.text) || /共\\s*\\d+\\s*条评论/.test(item.text) || item.text === '全部评论')).sort((a,b)=>(a.rect.width*a.rect.height)-(b.rect.width*b.rect.height)); for (const item of rows) { let node = item.el; for (let d = 0; node && d < 5; d++, node = node.parentElement) if (clickElement(node)) return item.text; } return ''; }"
        "function imageKey(url) { try { const u = new URL(url, location.href); return u.origin + u.pathname.split('!')[0]; } catch (_) { return String(url || '').split('!')[0]; } }"
        "function keep(item) { if (!item || !item.src || !/^https?:/i.test(item.src)) return; if (/avatar|user|profile|emoji|sticker|icon|logo/i.test(item.src)) return; const key = imageKey(item.src); if (!key || seen.has(key)) return; seen.add(key); allImages.push(item); }"
        "function urlFromValue(value) { if (!value) return []; if (typeof value === 'string') return /^https?:\\/\\//i.test(value) ? [value] : []; if (Array.isArray(value)) return value.flatMap(urlFromValue); if (typeof value !== 'object') return []; const out = []; for (const key of ['url','urlDefault','url_default','src','link']) if (typeof value[key] === 'string') out.push(value[key]); for (const key of ['urlList','url_list','urls']) if (Array.isArray(value[key])) out.push(...value[key].filter(x => typeof x === 'string')); const infoList = Array.isArray(value.infoList) ? value.infoList : Array.isArray(value.info_list) ? value.info_list : []; for (const info of infoList) out.push(...urlFromValue(info)); return out; }"
        "function commentUser(comment) { const user = comment.userInfo || comment.user_info || comment.user || comment.author || {}; return user.nickname || user.nickName || user.name || user.userName || user.user_id || user.userId || ''; }"
        "function commentText(comment) { return comment.content || comment.commentContent || comment.text || comment.desc || ''; }"
        "function pushImagesFromComment(comment, source) { if (!comment || typeof comment !== 'object') return; const user = commentUser(comment); const text = commentText(comment); const containers = []; for (const key of ['pictures','picture','images','imageList','image_list','commentImages','comment_images']) if (comment[key]) containers.push(comment[key]); containers.forEach(container => { const values = Array.isArray(container) ? container : [container]; values.forEach((value, index) => { for (const src of urlFromValue(value)) keep({index: allImages.length + 1, src, width: Number(value && (value.width || value.w)) || 0, height: Number(value && (value.height || value.h)) || 0, commentUser:user, commentText:text, commentId: comment.id || comment.commentId || comment.comment_id || '', source}); }); }); const subs = comment.subComments || comment.sub_comments || comment.subCommentList || comment.sub_comment_list || []; if (Array.isArray(subs)) subs.forEach(child => pushImagesFromComment(child, source + '.sub')); }"
        "async function fetchJson(path, params) { const url = new URL(path, 'https://edith.xiaohongshu.com'); Object.entries(params).forEach(([key, value]) => url.searchParams.set(key, String(value == null ? '' : value))); const response = await fetch(url.toString(), {credentials:'include', headers:{accept:'application/json, text/plain, */*'}}); const text = await response.text(); let data = null; try { data = JSON.parse(text); } catch (_) { data = {text:text.slice(0, 500)}; } return {status:response.status, data, url:url.toString()}; }"
        "function unwrap(data) { return data && data.data && typeof data.data === 'object' ? data.data : data; }"
        "async function fetchSubComments(rootCommentId, topCommentId, xsecToken) { let cursor = ''; for (let page = 0; page < 20; page++) { const res = await fetchJson('/api/sns/web/v2/comment/sub/page', {noteId, rootCommentId, num:10, cursor, imageFormats, topCommentId:topCommentId || '', xsecToken:xsecToken || ''}); const inner = unwrap(res.data); const comments = Array.isArray(inner && inner.comments) ? inner.comments : []; comments.forEach(comment => pushImagesFromComment(comment, 'api.sub')); if (!comments.length || (maxImages && allImages.length >= maxImages)) break; if (!inner || !inner.hasMore) break; cursor = inner.cursor || ''; await sleep(120); } }"
        "async function fetchComments() { let cursor = ''; const xsecToken = initialXsecToken || new URLSearchParams(location.search).get('xsec_token') || ''; const pages = []; for (let page = 0; page < maxPages; page++) { const res = await fetchJson('/api/sns/web/v2/comment/page', {noteId, cursor, topCommentId:'', imageFormats, xsecToken}); const inner = unwrap(res.data); pages.push({page: page + 1, status: res.status, code: res.data && res.data.code, msg: res.data && (res.data.msg || res.data.message), count: Array.isArray(inner && inner.comments) ? inner.comments.length : 0}); const comments = Array.isArray(inner && inner.comments) ? inner.comments : []; for (const comment of comments) { pushImagesFromComment(comment, 'api.page'); const subCount = Number(comment.subCommentCount || comment.sub_comment_count || 0); const existing = Array.isArray(comment.subComments || comment.sub_comments) ? (comment.subComments || comment.sub_comments).length : 0; if (subCount > existing) await fetchSubComments(comment.id || comment.commentId || comment.comment_id || '', inner.topCommentId || '', xsecToken); if (maxImages && allImages.length >= maxImages) break; } if (!comments.length || (maxImages && allImages.length >= maxImages)) break; if (!inner || !inner.hasMore) break; cursor = inner.cursor || ''; await sleep(150); } return pages; }"
        "function collectDomImages() { Array.from(document.images).forEach((img, index) => { const src = img.currentSrc || img.src || ''; if (!src || !/xhscdn|xiaohongshu|ci\\./i.test(src)) return; let text = ''; let el = img; for (let d = 0; el && d < 8; d++, el = el.parentElement) text += ' ' + textOf(el).slice(0, 180); if (!/评论|回复|点赞|分钟前|小时前|天前|周前|月前|年前/.test(text)) return; keep({index: 100000 + index, src, width: img.naturalWidth || 0, height: img.naturalHeight || 0, commentUser:'', commentText:text.slice(0, 180), commentId:'', source:'dom'}); }); }"
        "function scrollComments() { const roots = Array.from(document.querySelectorAll('div,main,section')).filter(el => visibleRect(el) && /评论|回复|点赞|共\\s*\\d+\\s*条评论/.test(textOf(el))); const scrollers = []; for (const root of roots) { for (let el = root; el; el = el.parentElement) { const rect = visibleRect(el); if (!rect) continue; const style = getComputedStyle(el); if (el.clientHeight > 180 && (el.scrollHeight > el.clientHeight + 24 || /auto|scroll/.test(style.overflowY))) scrollers.push(el); } } const targets = Array.from(new Set(scrollers)).slice(0, 6); if (targets.length) { for (const target of targets) { const step = Math.max(420, Math.floor(target.clientHeight * 0.75)); target.scrollTop += step; target.dispatchEvent(new WheelEvent('wheel', {bubbles:true, deltaY:step})); } } else { window.scrollBy(0, 720); document.dispatchEvent(new WheelEvent('wheel', {bubbles:true, deltaY:720})); } }"
        "await sleep(1800);"
        "const activatedCommentTab = activateCommentTab();"
        "if (activatedCommentTab) await sleep(1200);"
        "let pages = [];"
        "try { pages = await fetchComments(); } catch (error) { pages = [{error:String(error)}]; }"
        "if (!maxImages || allImages.length < maxImages) { const domAttempts = maxImages ? 90 : 180; for (let attempt = 0; attempt < domAttempts; attempt++) { if (attempt % 8 === 0) activateCommentTab(); collectDomImages(); if (maxImages && allImages.length >= maxImages && attempt >= 4) break; scrollComments(); await sleep(attempt < 12 ? 500 : 300); } }"
        "const bodyText = (document.body && document.body.innerText || '').slice(0, 3000);"
        "const apiLoginError = pages.some(page => String(page.code) === '-101' || /登录/.test(String(page.msg || page.error || '')));"
        "const loginRequired = allImages.length === 0 && (/登录后|扫码|手机号登录|获取验证码|请先登录|无登录信息/.test(bodyText) || apiLoginError);"
        "return {url:location.href, title:document.title || '', activatedCommentTab, loginRequired, pages, commentImages: maxImages ? allImages.slice(0, maxImages) : allImages};"
        "})()"
    )


def build_collection_list_script() -> str:
    return (
        "(async () => {"
        "const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));"
        "async function fetchJson(path, params) { const url = new URL(path, 'https://edith.xiaohongshu.com'); Object.entries(params || {}).forEach(([key, value]) => url.searchParams.set(key, String(value == null ? '' : value))); const response = await fetch(url.toString(), {credentials:'include', headers:{accept:'application/json, text/plain, */*'}}); const text = await response.text(); let data = null; try { data = JSON.parse(text); } catch (_) { data = {text:text.slice(0, 500)}; } return {status:response.status, data}; }"
        "function unwrap(data) { return data && data.data && typeof data.data === 'object' ? data.data : data; }"
        "function textOf(el) { return ((el && (el.innerText || el.textContent)) || '').trim(); }"
        "function visibleRect(el) { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth ? r : null; }"
        "function clickElement(el) { const r = visibleRect(el); if (!r) return false; const opts = {bubbles:true,cancelable:true,view:window,clientX:r.left+r.width/2,clientY:r.top+r.height/2}; for (const type of ['pointerdown','mousedown','pointerup','mouseup','click']) el.dispatchEvent(new MouseEvent(type, opts)); if (typeof el.click === 'function') el.click(); return true; }"
        "function boardIdFromUrl(url) { const m = String(url || '').match(/\\/board\\/([0-9a-fA-F]{16,32})/); return m ? m[1] : ''; }"
        "function boardCount(text) { const m = String(text || '').match(/笔记\\s*[・·:]?\\s*(\\d+)/); return m ? m[1] : ''; }"
        "const boards = []; const seen = new Set();"
        "function keepBoard(raw) { const id = raw && (raw.id || raw.boardId || raw.board_id || boardIdFromUrl(raw.url || raw.href)); if (!id || seen.has(id)) return; seen.add(id); boards.push({id, boardId:id, name:raw.name || raw.title || '未命名专辑', noteCount:raw.noteCount || raw.note_count || raw.count || '', url:raw.url || raw.href || ('/board/' + id), source:raw.source || 'dom'}); }"
        "function collectDomBoards() { Array.from(document.querySelectorAll('a[href]')).forEach(anchor => { const href = anchor.href || anchor.getAttribute('href') || ''; const id = boardIdFromUrl(href); if (!id) return; let card = anchor; for (let d = 0; card && d < 6; d++, card = card.parentElement) { const txt = textOf(card); if (txt && /笔记|专辑|收藏/.test(txt)) { const parts = txt.split(/\\n+/).map(s => s.trim()).filter(Boolean); keepBoard({id, url:href, name:parts[0] || textOf(anchor) || id, noteCount:boardCount(txt), source:'dom'}); return; } } keepBoard({id, url:href, name:textOf(anchor) || id, source:'dom'}); }); }"
        "function clickFavBoardTabs() { const labels = ['收藏', '专辑']; for (const label of labels) { const candidates = Array.from(document.querySelectorAll('button,div,span,a')).map(el => ({el,text:textOf(el),rect:visibleRect(el)})).filter(x => x.rect && (x.text === label || x.text.startsWith(label + '・') || x.text.startsWith(label + '·'))).sort((a,b)=>a.rect.top-b.rect.top); if (!candidates.length) continue; let node = candidates[0].el; for (let d = 0; node && d < 5; d++, node = node.parentElement) clickElement(node); } }"
        "const meRes = await fetchJson('/api/sns/web/v2/user/me', {});"
        "const me = unwrap(meRes.data) || {};"
        "const user = me.userInfo || me.user_info || me.user || me;"
        "const userId = user.userId || user.user_id || user.id || '';"
        "let noteCount = '';"
        "if (userId) {"
        "  const favUrl = new URL('/user/profile/' + userId, location.origin); favUrl.searchParams.set('tab', 'fav'); favUrl.searchParams.set('subTab', 'board');"
        "  if (!/\\/user\\/profile\\//.test(location.pathname) || !/subTab=board/.test(location.search)) { location.href = favUrl.toString(); await sleep(3500); }"
        "  clickFavBoardTabs(); await sleep(1800); collectDomBoards();"
        "  try {"
        "    const boardRes = await fetchJson('/api/sns/web/v1/board/user', {userId, num:50, page:1, imageFormats:'jpg,webp,avif'});"
        "    const boardData = unwrap(boardRes.data) || {};"
        "    const apiBoards = Array.isArray(boardData.boards) ? boardData.boards : Array.isArray(boardData.boardList) ? boardData.boardList : [];"
        "    apiBoards.forEach(board => keepBoard({...board, source:'api'}));"
        "  } catch (_error) {}"
        "  try {"
        "    const collectRes = await fetchJson('/api/sns/web/v2/note/collect/page', {cursor:'', num:1, userId, imageFormats:'jpg,webp,avif'});"
        "    const collectData = unwrap(collectRes.data) || {};"
        "    noteCount = collectData.total || collectData.count || '';"
        "  } catch (_error) {}"
        "  for (let i = 0; i < 18; i++) { collectDomBoards(); if (boards.length && i > 4) break; window.scrollBy(0, 700); document.dispatchEvent(new WheelEvent('wheel', {bubbles:true, deltaY:700})); await sleep(300); }"
        "}"
        "const bodyText = (document.body && document.body.innerText || '').slice(0, 3000);"
        "const loginRequired = !userId || /登录后|扫码|手机号登录|获取验证码|请先登录/.test(bodyText) || String(meRes.data && meRes.data.code) === '-101';"
        "return {url:location.href, title:document.title || '', loginRequired, userId, noteCount, boards, me: meRes.data};"
        "})()"
    )


def build_favorite_video_script(limit: int | None = None) -> str:
    max_items = max(0, int(limit or 0))
    scroll_rounds = 260 if not max_items else max(30, min(260, max_items * 6 + 30))
    return (
        "(async () => {"
        "const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));"
        f"const maxItems = {max_items};"
        f"const scrollRounds = {scroll_rounds};"
        "async function fetchJson(path, params) { const url = new URL(path, 'https://edith.xiaohongshu.com'); Object.entries(params || {}).forEach(([key, value]) => url.searchParams.set(key, String(value == null ? '' : value))); const response = await fetch(url.toString(), {credentials:'include', headers:{accept:'application/json, text/plain, */*'}}); const text = await response.text(); let data = null; try { data = JSON.parse(text); } catch (_) { data = {text:text.slice(0, 500)}; } return {status:response.status, data, url:url.toString()}; }"
        "function unwrap(data) { return data && data.data && typeof data.data === 'object' ? data.data : data; }"
        "function textOf(el) { return ((el && (el.innerText || el.textContent)) || '').trim(); }"
        "function visibleRect(el) { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth ? r : null; }"
        "function clickElement(el) { const r = visibleRect(el); if (!r) return false; const opts = {bubbles:true,cancelable:true,view:window,clientX:r.left+r.width/2,clientY:r.top+r.height/2}; for (const type of ['pointerdown','mousedown','pointerup','mouseup','click']) el.dispatchEvent(new MouseEvent(type, opts)); if (typeof el.click === 'function') el.click(); return true; }"
        "const seen = new Set();"
        "const byId = new Map();"
        "const notes = [];"
        "function noteIdFromUrl(url) { const text = String(url || ''); let m = text.match(/\\/(?:explore|discovery\\/item)\\/([0-9a-fA-F]{16,32})/); if (m) return m[1]; m = text.match(/\\/user\\/profile\\/[0-9a-fA-F]{16,32}\\/([0-9a-fA-F]{16,32})(?:[?#/]|$)/); return m ? m[1] : ''; }"
        "function normalizeNoteUrl(url, xsecToken) { const id = noteIdFromUrl(url); if (!id) return ''; const u = new URL('/explore/' + id, location.origin); const parsed = new URL(url, location.href); const token = xsecToken || parsed.searchParams.get('xsec_token') || ''; if (token) u.searchParams.set('xsec_token', token); u.searchParams.set('xsec_source', parsed.searchParams.get('xsec_source') || 'pc_collect'); return u.toString(); }"
        "function keep(note) { const rawUrl = note && (note.url || note.href || ''); const id = note && (note.noteId || note.note_id || note.id || note.note_id_str || noteIdFromUrl(rawUrl)); if (!id) return; let token = note.xsecToken || note.xsec_token || ''; try { token = token || new URL(rawUrl || ('/explore/' + id), location.href).searchParams.get('xsec_token') || ''; } catch (_) {} const url = normalizeNoteUrl(rawUrl || ('/explore/' + id), token); const title = note.title || note.displayTitle || note.desc || ''; const existing = byId.get(id); if (existing) { if (token && !existing.xsecToken) { existing.xsecToken = token; existing.url = url; } if (title && (!existing.title || String(title).length > String(existing.title).length)) existing.title = title; if (note.source && !String(existing.source || '').includes(note.source)) existing.source = String(existing.source || 'unknown') + '+' + note.source; return; } seen.add(id); const item = {noteId:id, id, url, xsecToken:token, title, type:note.type || note.noteType || '', source:note.source || 'unknown'}; byId.set(id, item); notes.push(item); }"
        "function collectDomCards() { Array.from(document.querySelectorAll('a[href]')).forEach(anchor => { const href = anchor.href || anchor.getAttribute('href') || ''; const id = noteIdFromUrl(href); if (!id) return; let card = anchor; for (let d = 0; card && d < 6; d++, card = card.parentElement) { const txt = textOf(card); if (txt && (card.querySelector('img') || /点赞|收藏|笔记|视频|播放/.test(txt))) { keep({id, url:href, title:txt.slice(0, 80), source:'dom'}); return; } } keep({id, url:href, title:textOf(anchor).slice(0, 80), source:'dom'}); }); }"
        "function clickFavTabs() { const candidates = Array.from(document.querySelectorAll('button,div,span,a')).map(el => ({el,text:textOf(el),rect:visibleRect(el)})).filter(x => x.rect && /^(收藏|笔记)$/.test(x.text)).sort((a,b)=>a.rect.top-b.rect.top); for (const item of candidates) { let node = item.el; for (let d = 0; node && d < 5; d++, node = node.parentElement) { clickElement(node); } } }"
        "function scrollPage() { const roots = Array.from(document.querySelectorAll('div,main,section')).filter(el => { const r = visibleRect(el); if (!r) return false; const style = getComputedStyle(el); return el.clientHeight > 240 && (el.scrollHeight > el.clientHeight + 30 || /auto|scroll/.test(style.overflowY)); }).sort((a,b)=>(b.clientHeight*b.clientWidth)-(a.clientHeight*a.clientWidth)); const targets = roots.slice(0, 4); if (targets.length) { for (const target of targets) { const step = Math.max(520, Math.floor(target.clientHeight * 0.85)); target.scrollTop += step; target.dispatchEvent(new WheelEvent('wheel', {bubbles:true, deltaY:step})); } } else { window.scrollBy(0, 900); document.dispatchEvent(new WheelEvent('wheel', {bubbles:true, deltaY:900})); } }"
        "const meRes = await fetchJson('/api/sns/web/v2/user/me', {});"
        "const me = unwrap(meRes.data) || {};"
        "const user = me.userInfo || me.user_info || me.user || me;"
        "const userId = user.userId || user.user_id || user.id || '';"
        "if (!userId || user.guest) { const bodyText = (document.body && document.body.innerText || '').slice(0, 3000); return {url:location.href, loginRequired:true, userId, visibleCount:0, notes, me:meRes.data, bodyText}; }"
        "const favUrl = new URL('/user/profile/' + userId, location.origin); favUrl.searchParams.set('tab', 'fav'); favUrl.searchParams.set('subTab', 'note');"
        "if (!/\\/user\\/profile\\//.test(location.pathname) || !/tab=fav/.test(location.search)) { location.href = favUrl.toString(); await sleep(3500); }"
        "clickFavTabs(); await sleep(1200); collectDomCards();"
        "let cursor = ''; const apiPages = [];"
        "for (let page = 0; page < 120; page++) {"
        "  try { const res = await fetchJson('/api/sns/web/v2/note/collect/page', {cursor, num:30, userId, imageFormats:'jpg,webp,avif'}); const inner = unwrap(res.data) || {}; const pageNotes = Array.isArray(inner.notes) ? inner.notes : Array.isArray(inner.noteList) ? inner.noteList : Array.isArray(inner.items) ? inner.items : []; pageNotes.forEach(note => keep({...note, source:'api'})); apiPages.push({page:page+1,status:res.status,code:res.data&&res.data.code,count:pageNotes.length,total:notes.length,hasMore:!!inner.hasMore,cursor:inner.cursor}); if (!pageNotes.length || (maxItems && notes.length >= maxItems)) break; if (!inner.hasMore) break; cursor = inner.cursor || ''; await sleep(120); } catch (error) { apiPages.push({page:page+1,error:String(error)}); break; }"
        "}"
        "let stable = 0; let lastCount = notes.length;"
        "for (let round = 0; round < scrollRounds; round++) { collectDomCards(); if (maxItems && notes.length >= maxItems) break; scrollPage(); await sleep(round < 12 ? 650 : 350); collectDomCards(); if (notes.length === lastCount) stable += 1; else { stable = 0; lastCount = notes.length; } if (!maxItems && stable >= 18 && round > 25) break; }"
        "const bodyText = (document.body && document.body.innerText || '').slice(0, 3000);"
        "const loginRequired = notes.length === 0 && (/登录后|扫码|手机号登录|获取验证码|请先登录/.test(bodyText) || String(meRes.data && meRes.data.code) === '-101');"
        "return {url:location.href, title:document.title || '', loginRequired, userId, visibleCount:notes.length, apiPages, notes:maxItems ? notes.slice(0, maxItems) : notes};"
        "})()"
    )


def build_collection_notes_script(collection_id: str, limit: int | None = None) -> str:
    collection_json = json.dumps(str(collection_id), ensure_ascii=False)
    max_items = max(0, int(limit or 0))
    scroll_rounds = 260 if not max_items else max(30, min(260, max_items * 6 + 30))
    max_pages = 120 if not max_items else max(1, min(120, (max_items // 30) + 8))
    return (
        "(async () => {"
        "const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));"
        f"const collectionId = {collection_json};"
        f"const maxItems = {max_items};"
        f"const scrollRounds = {scroll_rounds};"
        f"const maxPages = {max_pages};"
        "async function fetchJson(path, params) { const url = new URL(path, 'https://edith.xiaohongshu.com'); Object.entries(params || {}).forEach(([key, value]) => url.searchParams.set(key, String(value == null ? '' : value))); const response = await fetch(url.toString(), {credentials:'include', headers:{accept:'application/json, text/plain, */*'}}); const text = await response.text(); let data = null; try { data = JSON.parse(text); } catch (_) { data = {text:text.slice(0, 500)}; } return {status:response.status, data, url:url.toString()}; }"
        "function unwrap(data) { return data && data.data && typeof data.data === 'object' ? data.data : data; }"
        "function textOf(el) { return ((el && (el.innerText || el.textContent)) || '').trim(); }"
        "function visibleRect(el) { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth ? r : null; }"
        "function noteIdFromUrl(url) { const text = String(url || ''); let m = text.match(/\\/(?:explore|discovery\\/item)\\/([0-9a-fA-F]{16,32})/); if (m) return m[1]; m = text.match(/\\/user\\/profile\\/[0-9a-fA-F]{16,32}\\/([0-9a-fA-F]{16,32})(?:[?#/]|$)/); if (m) return m[1]; m = text.match(/\\/board\\/[0-9a-fA-F]{16,32}\\/([0-9a-fA-F]{16,32})(?:[?#/]|$)/); return m ? m[1] : ''; }"
        "function normalizeNoteUrl(url, xsecToken) { const id = noteIdFromUrl(url); if (!id) return ''; const u = new URL('/explore/' + id, location.origin); const parsed = new URL(url, location.href); const token = xsecToken || parsed.searchParams.get('xsec_token') || ''; if (token) u.searchParams.set('xsec_token', token); const source = parsed.searchParams.get('xsec_source') || (String(parsed.pathname).includes('/board/') ? 'pc_board' : 'pc_collect'); if (source) u.searchParams.set('xsec_source', source); return u.toString(); }"
        "const byId = new Map();"
        "const meRes = await fetchJson('/api/sns/web/v2/user/me', {});"
        "const me = unwrap(meRes.data) || {};"
        "const user = me.userInfo || me.user_info || me.user || me;"
        "const userId = user.userId || user.user_id || user.id || '';"
        "const notes = [];"
        "const pages = [];"
        "function keep(note) { const rawUrl = note && (note.url || note.href || ''); const id = note && (note.noteId || note.note_id || note.id || note.note_id_str || noteIdFromUrl(rawUrl)); if (!id) return; let token = note.xsecToken || note.xsec_token || ''; try { token = token || new URL(rawUrl || ('/explore/' + id), location.href).searchParams.get('xsec_token') || ''; } catch (_) {} const url = normalizeNoteUrl(rawUrl || ('/explore/' + id), token); const title = note.title || note.displayTitle || note.desc || ''; const sourceValue = url.includes('pc_board') ? 'pc_board' : 'pc_collect'; const existing = byId.get(id); if (existing) { if (token && !existing.xsecToken) { existing.xsecToken = token; existing.url = url; existing.xsecSource = sourceValue; } if (title && (!existing.title || String(title).length > String(existing.title).length)) existing.title = title; return; } const item = {noteId:id, id, url, href:rawUrl, xsecToken:token, xsecSource:sourceValue, title, source:note.source || 'dom'}; byId.set(id, item); notes.push(item); }"
        "function collectDomCards() { Array.from(document.querySelectorAll('a[href]')).forEach(anchor => { const href = anchor.href || anchor.getAttribute('href') || ''; const id = noteIdFromUrl(href); if (!id) return; let card = anchor; for (let d = 0; card && d < 6; d++, card = card.parentElement) { const txt = textOf(card); if (txt && (card.querySelector('img') || /点赞|收藏|笔记|视频|播放/.test(txt))) { keep({id, url:href, title:txt.slice(0, 80), source:'dom'}); return; } } keep({id, url:href, title:textOf(anchor).slice(0, 80), source:'dom'}); }); }"
        "function scrollPage() { const roots = Array.from(document.querySelectorAll('div,main,section')).filter(el => { const r = visibleRect(el); if (!r) return false; const style = getComputedStyle(el); return el.clientHeight > 240 && (el.scrollHeight > el.clientHeight + 30 || /auto|scroll/.test(style.overflowY)); }).sort((a,b)=>(b.clientHeight*b.clientWidth)-(a.clientHeight*a.clientWidth)); const targets = roots.slice(0, 4); if (targets.length) { for (const target of targets) { const step = Math.max(520, Math.floor(target.clientHeight * 0.85)); target.scrollTop += step; target.dispatchEvent(new WheelEvent('wheel', {bubbles:true, deltaY:step})); } } else { window.scrollBy(0, 900); document.dispatchEvent(new WheelEvent('wheel', {bubbles:true, deltaY:900})); } }"
        "if (collectionId && !collectionId.startsWith('__')) { const boardUrl = new URL('/board/' + collectionId, location.origin); boardUrl.searchParams.set('source', 'web_user_page'); if (!/\\/board\\//.test(location.pathname) || !location.pathname.includes(collectionId)) { location.href = boardUrl.toString(); await sleep(4000); } } else { await sleep(1800); }"
        "collectDomCards();"
        "let cursor = '';"
        "for (let page = 0; userId && page < maxPages; page++) {"
        "  const isAll = collectionId === '__all__' || collectionId === '__all_favorites__' || collectionId === '__all_videos__';"
        "  const path = isAll ? '/api/sns/web/v2/note/collect/page' : '/api/sns/web/v1/board/note';"
        "  const params = isAll ? {cursor, num:30, userId, imageFormats:'jpg,webp,avif'} : {boardId:collectionId, cursor, num:30, imageFormats:'jpg,webp,avif'};"
        "  let res;"
        "  try { res = await fetchJson(path, params); } catch (error) { pages.push({page:page + 1, error:String(error)}); break; }"
        "  const inner = unwrap(res.data) || {};"
        "  const pageNotes = Array.isArray(inner.notes) ? inner.notes : Array.isArray(inner.noteList) ? inner.noteList : Array.isArray(inner.items) ? inner.items : [];"
        "  pageNotes.forEach(note => keep({...note, source:'api'}));"
        "  pages.push({page:page + 1, status:res.status, code:res.data && res.data.code, msg:res.data && (res.data.msg || res.data.message), count:pageNotes.length, total:notes.length, cursor:inner.cursor, hasMore:!!inner.hasMore});"
        "  if (!pageNotes.length || (maxItems && notes.length >= maxItems)) break;"
        "  if (!inner.hasMore) break;"
        "  cursor = inner.cursor || '';"
        "  await sleep(160);"
        "}"
        "let stable = 0; let lastCount = notes.length;"
        "for (let round = 0; round < scrollRounds; round++) { collectDomCards(); if (maxItems && notes.length >= maxItems) break; scrollPage(); await sleep(round < 12 ? 650 : 350); collectDomCards(); if (notes.length === lastCount) stable += 1; else { stable = 0; lastCount = notes.length; } if (!maxItems && stable >= 18 && round > 25) break; }"
        "const bodyText = (document.body && document.body.innerText || '').slice(0, 3000);"
        "const loginRequired = !userId || /登录后|扫码|手机号登录|获取验证码|请先登录/.test(bodyText) || String(meRes.data && meRes.data.code) === '-101';"
        "return {url:location.href, title:document.title || '', loginRequired, userId, collectionId, pages, notes:maxItems ? notes.slice(0, maxItems) : notes};"
        "})()"
    )


def extract_browser_video_streams(final_url: str, note: dict, logger: LogFn) -> list[dict]:
    return []


def read_browser_video_elements(opencli_path: str, session_name: str) -> list[dict]:
    script = (
        "(() => {"
        "const videos = Array.from(document.querySelectorAll('video')).map((v, index) => ({"
        "index,"
        "src: v.currentSrc || v.src || v.getAttribute('src') || '',"
        "videoWidth: v.videoWidth || 0,"
        "videoHeight: v.videoHeight || 0,"
        "duration: Number.isFinite(v.duration) ? v.duration : 0,"
        "kind: 'dom'"
        "}));"
        "const resources = performance.getEntriesByType('resource')"
        ".map((entry, index) => ({"
        "index,"
        "src: entry.name || '',"
        "videoWidth: 0,"
        "videoHeight: 0,"
        "duration: 0,"
        "kind: 'resource'"
        "}))"
        ".filter(item => /\\.mp4(\\?|$)/i.test(item.src));"
        "return videos.concat(resources).filter(item => item.src);"
        "})()"
    )
    last_error: Exception | None = None
    for _attempt in range(5):
        try:
            stdout = run_opencli(opencli_path, ["browser", session_name, "eval", script], timeout=20)
            data = parse_json_from_output(stdout)
            if isinstance(data, list):
                items = [item for item in data if isinstance(item, dict)]
                if any(is_video_url(str(item.get("src") or "")) for item in items):
                    return items
        except Exception as exc:  # noqa: BLE001 - retry while the player initializes.
            last_error = exc
        time.sleep(1)
    if last_error:
        raise last_error
    return []


def run_opencli(opencli_path: str, args: list[str], timeout: int) -> str:
    command: str | list[str]
    shell = False
    if os.name == "nt" and opencli_path.lower().endswith((".cmd", ".bat")):
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
        **douyin.NO_WINDOW_KWARGS,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        raise XhsDownloadError(message or f"opencli exited with {result.returncode}")
    return result.stdout


def cmd_quote(parts: list[str]) -> str:
    return " ".join(f'"{part.replace(chr(34), chr(34) + chr(34))}"' for part in parts)


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
    raise XhsDownloadError("opencli 没有返回 JSON 数据。")


def is_video_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and ".mp4" in parsed.path.lower()


def best_video_dimensions(note: dict) -> tuple[int, int]:
    return best_dimensions_from_value(note.get("video") or note)


def best_dimensions_from_value(value) -> tuple[int, int]:
    decoded = decode_json_container(value)
    if decoded is not value:
        return best_dimensions_from_value(decoded)
    if isinstance(value, list):
        best = (0, 0)
        for child in value:
            child_dimensions = best_dimensions_from_value(child)
            if dimensions_area(child_dimensions) > dimensions_area(best):
                best = child_dimensions
        return best
    if not isinstance(value, dict):
        return (0, 0)

    best = (
        first_int(value, "width", "videoWidth", "video_width", "w"),
        first_int(value, "height", "videoHeight", "video_height", "h"),
    )
    for child in value.values():
        if not isinstance(child, (dict, list, str)):
            continue
        child_dimensions = best_dimensions_from_value(child)
        if dimensions_area(child_dimensions) > dimensions_area(best):
            best = child_dimensions
    return best


def dimensions_area(dimensions: tuple[int, int]) -> int:
    return dimensions[0] * dimensions[1]


def find_idm() -> Path | None:
    candidates = [
        shutil.which("IDMan.exe"),
        r"C:\Program Files (x86)\Internet Download Manager\IDMan.exe",
        r"C:\Program Files\Internet Download Manager\IDMan.exe",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.exists():
            return path
    return None


class HeaderProxyServer:
    def __init__(self) -> None:
        self.token_map: dict[str, tuple[str, str]] = {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def register(self, target_url: str, referer: str) -> str:
        token = f"{int(time.time() * 1000)}_{len(self.token_map)}"
        self.token_map[token] = (target_url, referer)
        return f"{self.base_url}/download?{urlencode({'token': token})}"

    def _handler_class(self):
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_HEAD(self):  # noqa: N802 - stdlib callback name.
                self._proxy_request(send_body=False)

            def do_GET(self):  # noqa: N802 - stdlib callback name.
                self._proxy_request(send_body=True)

            def log_message(self, _format, *args) -> None:
                return

            def _proxy_request(self, send_body: bool) -> None:
                parsed = urlparse(self.path)
                token = parse_qs(parsed.query).get("token", [""])[0]
                target = proxy.token_map.get(token)
                if not target:
                    self.send_error(404, "Unknown token")
                    return

                target_url, referer = target
                headers = dict(BASE_HEADERS)
                headers["Referer"] = referer or "https://www.xiaohongshu.com/"
                headers["Accept"] = "*/*"
                if self.headers.get("Range"):
                    headers["Range"] = self.headers["Range"]

                try:
                    with requests.get(target_url, headers=headers, timeout=(6, 60), stream=True) as response:
                        self.send_response(response.status_code)
                        excluded = {"transfer-encoding", "connection", "content-encoding"}
                        for key, value in response.headers.items():
                            if key.lower() in excluded:
                                continue
                            self.send_header(key, value)
                        self.end_headers()
                        if not send_body:
                            return
                        for chunk in response.iter_content(chunk_size=1024 * 512):
                            if chunk:
                                self.wfile.write(chunk)
                except Exception as exc:  # noqa: BLE001 - proxy must translate to HTTP.
                    try:
                        self.send_error(502, str(exc))
                    except OSError:
                        pass

        return Handler


def save_or_enqueue_image(
    engine: DownloadEngine,
    best: ProbeResult,
    target: Path,
    referer: str,
) -> int:
    if engine.should_use_idm(best.bytes_count):
        enqueue_idm(engine, best.candidate.url, target, referer)
        return best.bytes_count

    target.write_bytes(best.content)
    return best.bytes_count


def save_or_enqueue_media(
    engine: DownloadEngine,
    session: requests.Session,
    result: MediaProbeResult,
    target: Path,
    referer: str,
    declared_dimensions: str = "",
) -> tuple[int, str]:
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
        raise XhsDownloadError("IDM 引擎未初始化。")
    target.parent.mkdir(parents=True, exist_ok=True)
    proxy_url = engine.proxy.register(url, referer)
    args = [
        str(engine.idm_path),
        "/d",
        proxy_url,
        "/p",
        str(target.parent),
        "/f",
        target.name,
        "/n",
    ]
    try:
        subprocess.run(args, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, **douyin.NO_WINDOW_KWARGS)
    except (OSError, subprocess.SubprocessError) as exc:
        raise XhsDownloadError(f"投递 IDM 失败：{exc}") from exc


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
            logger(
                f"[{item['index']:03d}] 图片{action}：{item['width']}x{item['height']} "
                f"{item['format']}，{item['elapsed_seconds']:.1f}s"
            )
        elif kind == "live_photo":
            report["live_photos"].append(item)
            action = "已投递 IDM" if item.get("engine") == "idm" else "成功"
            logger(
                f"[{item['index']:03d}] Live Photo {action}："
                f"{format_media_info(item['bytes'], item.get('dimensions', ''), item.get('declared_dimensions', ''))}，"
                f"{item['elapsed_seconds']:.1f}s"
            )
        else:
            report["videos"].append(item)
            action = "已投递 IDM" if item.get("engine") == "idm" else "成功"
            logger(
                f"[video {item['index']:03d}] {action}："
                f"{format_media_info(item['bytes'], item.get('dimensions', ''), item.get('declared_dimensions', ''))}，"
                f"{item['elapsed_seconds']:.1f}s"
            )
        return

    report["failures"].append(item)
    if kind == "image":
        report["images"].append(item)
        logger(f"[{item['index']:03d}] 图片失败：{item['error']}")
    elif kind == "live_photo":
        report["live_photos"].append(item)
        logger(f"[{item['index']:03d}] Live Photo 失败：{item['error']}")
    else:
        report["videos"].append(item)
        logger(f"[video {item['index']:03d}] 失败：{item['error']}")


def download_media_task(
    kind: str,
    index: int,
    payload,
    note_dir: Path,
    final_url: str,
    engine: DownloadEngine,
    file_prefix: str = "",
) -> dict:
    session = make_session()
    start = time.perf_counter()
    try:
        if kind == "image":
            image: ImageItem = payload
            best = choose_best_image(session, image, final_url)
            prefix = f"{file_prefix}_" if file_prefix else ""
            filename = f"{prefix}{image.index:03d}_{best.width}x{best.height}.{best.extension}"
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
                "declared_width": image.declared_width,
                "declared_height": image.declared_height,
                "elapsed_seconds": round(time.perf_counter() - start, 3),
            }
            if image.comment_user or image.comment_text or image.comment_id:
                item.update(
                    {
                        "comment_user": image.comment_user,
                        "comment_text": image.comment_text,
                        "comment_id": image.comment_id,
                        "comment_source": image.source,
                    }
                )
            return {"kind": kind, "item": item}

        if kind == "live_photo":
            image = payload
            result = choose_best_media(session, image.stream, final_url, f"live_photo_{image.index:03d}")
            prefix = f"{file_prefix}_" if file_prefix else ""
            target = unique_path(note_dir / f"{prefix}{image.index:03d}_live.mp4")
            declared_dimensions = candidate_dimensions(result.candidate)
            used_idm = media_will_use_idm(engine, result)
            bytes_count, dimensions = save_or_enqueue_media(engine, session, result, target, final_url, declared_dimensions)
            item = {
                "index": image.index,
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

        result = choose_best_media(session, payload, final_url, f"video_{index:03d}")
        prefix = f"{file_prefix}_" if file_prefix else ""
        target = unique_path(note_dir / f"{prefix}video_{index:03d}.mp4")
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
    except Exception as exc:  # noqa: BLE001 - convert worker errors to report entries.
        item = {
            "index": index,
            "kind": kind,
            "status": "failed",
            "error": str(exc),
            "elapsed_seconds": round(time.perf_counter() - start, 3),
        }
        return {"kind": kind, "item": item}


def extract_url(text: str) -> str:
    urls = extract_urls(text)
    if not urls:
        raise XhsDownloadError("没有识别到小红书链接，请粘贴完整分享文本或链接。")
    return urls[0]


def query_value(url: str, key: str) -> str:
    values = parse_qs(urlparse(url).query).get(key) or []
    return str(values[0]) if values else ""


def extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    allowed = ("xiaohongshu.com", "xhslink.com")

    for match in re.finditer(r"https?://[^\s，。！!）)\]}>\"']+", text):
        url = match.group(0).strip().rstrip(".,;，。；")
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if not any(host == domain or host.endswith("." + domain) for domain in allowed):
            continue
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def comment_images_from_snapshot(items: list, limit: int | None = None) -> list[ImageItem]:
    images: list[ImageItem] = []
    seen: set[str] = set()
    for raw in items:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("src") or raw.get("url") or "").strip()
        if not url or not url.startswith(("http://", "https://")):
            continue
        key = url.split("!", 1)[0].split("?", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        images.append(
            ImageItem(
                index=len(images) + 1,
                declared_width=to_int(raw.get("width")),
                declared_height=to_int(raw.get("height")),
                url_default=url,
                comment_user=str(raw.get("commentUser") or raw.get("comment_user") or ""),
                comment_text=str(raw.get("commentText") or raw.get("comment_text") or ""),
                comment_id=str(raw.get("commentId") or raw.get("comment_id") or ""),
                source=str(raw.get("source") or "comment"),
            )
        )
        if limit and len(images) >= limit:
            break
    return images


def collection_note_urls(notes: list) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for note in notes:
        if not isinstance(note, dict):
            continue
        note_id = find_collection_note_id(note)
        if not note_id:
            continue
        raw_url = find_collection_note_value(note, ("url", "href", "link"))
        xsec_token = find_collection_note_value(note, ("xsecToken", "xsec_token"))
        if not xsec_token and raw_url:
            xsec_token = query_value(raw_url, "xsec_token")
        xsec_source = find_collection_note_value(note, ("xsecSource", "xsec_source")) or query_value(raw_url, "xsec_source")
        if not xsec_source:
            xsec_source = "pc_board" if "/board/" in raw_url else "pc_collect"
        url = f"https://www.xiaohongshu.com/explore/{quote(note_id, safe='')}"
        params = []
        if xsec_token:
            params.append(f"xsec_token={quote(xsec_token, safe='')}")
        if xsec_source:
            params.append(f"xsec_source={quote(xsec_source, safe='')}")
        if params:
            url += "?" + "&".join(params)
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def has_existing_video_download(output_root: Path, note_id: str, media_names: list[str] | None = None) -> bool:
    if not note_id:
        return False
    if media_names is not None:
        return any(note_id in name and name.lower().endswith(".mp4") for name in media_names)
    return any(output_root.glob(f"*{note_id}*video_*.mp4"))


def has_existing_note_download(output_root: Path, note_id: str, media_names: list[str] | None = None) -> bool:
    if not note_id:
        return False
    if media_names is not None:
        return any(note_id in name for name in media_names)
    patterns = [
        f"*{note_id}*_[0-9][0-9][0-9].jpg",
        f"*{note_id}*_[0-9][0-9][0-9].jpeg",
        f"*{note_id}*_[0-9][0-9][0-9].png",
        f"*{note_id}*_[0-9][0-9][0-9].webp",
        f"*{note_id}*live.mp4",
        f"*{note_id}*video_*.mp4",
    ]
    return any(any(output_root.glob(pattern)) for pattern in patterns)


def existing_media_filenames(output_root: Path) -> list[str]:
    suffixes = {".jpg", ".jpeg", ".png", ".webp", ".mp4"}
    try:
        return [path.name for path in output_root.iterdir() if path.is_file() and path.suffix.lower() in suffixes]
    except OSError:
        return []


def find_collection_note_id(item: dict) -> str:
    for container in collection_note_containers(item):
        for key in ("noteId", "note_id", "note_id_str", "id"):
            value = container.get(key)
            if isinstance(value, (str, int)):
                text = str(value).strip()
                if re.fullmatch(r"[0-9a-fA-F]{16,32}", text):
                    return text
        for key in ("url", "href", "link"):
            value = container.get(key)
            if isinstance(value, str):
                note_id = extract_note_id_from_url(value)
                if note_id:
                    return note_id
    return ""


def find_collection_note_value(item: dict, keys: tuple[str, ...]) -> str:
    for container in collection_note_containers(item):
        for key in keys:
            value = container.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
    return ""


def collection_note_containers(item: dict) -> list[dict]:
    containers = [item]
    for key in ("note", "noteCard", "note_card", "card", "targetNote", "target_note"):
        value = item.get(key)
        if isinstance(value, dict):
            containers.append(value)
    return containers


def find_first_string(value, keys: tuple[str, ...], depth: int = 0) -> str:
    if depth > 5:
        return ""
    if isinstance(value, dict):
        for key in keys:
            raw = value.get(key)
            if isinstance(raw, (str, int)) and str(raw).strip():
                return str(raw).strip()
        for child in value.values():
            found = find_first_string(child, keys, depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for child in value[:20]:
            found = find_first_string(child, keys, depth + 1)
            if found:
                return found
    return ""


def fetch_note_html(session: requests.Session, url: str) -> tuple[str, str]:
    try:
        response = session.get(url, timeout=(8, 25), allow_redirects=True)
    except requests.RequestException as exc:
        raise XhsDownloadError(f"请求笔记页面失败：{exc}") from exc

    if response.status_code in {403, 406, 418, 429}:
        raise XhsDownloadError(f"访问被限制（HTTP {response.status_code}），可能需要稍后重试。")
    if response.status_code >= 400:
        raise XhsDownloadError(f"请求笔记页面失败（HTTP {response.status_code}）。")

    response.encoding = response.apparent_encoding or response.encoding
    html = response.text
    if "window.__INITIAL_STATE__" not in html:
        raise XhsDownloadError("页面里没有找到笔记初始数据，链接可能过期或页面结构已变化。")
    return html, response.url


def parse_initial_state(html: str) -> dict:
    marker = "window.__INITIAL_STATE__="
    start = html.find(marker)
    if start < 0:
        raise XhsDownloadError("没有找到 window.__INITIAL_STATE__。")

    json_start = html.find("{", start + len(marker))
    if json_start < 0:
        raise XhsDownloadError("没有找到初始数据 JSON。")

    json_end = find_matching_brace(html, json_start)
    raw = html[json_start : json_end + 1]

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        # Xiaohongshu normally emits JSON. This fallback handles occasional
        # JS literals if the page changes slightly.
        patched = re.sub(r"\bundefined\b", "null", raw)
        try:
            return json.loads(patched)
        except json.JSONDecodeError as patched_exc:
            raise XhsDownloadError(f"解析页面初始数据失败：{exc}") from patched_exc


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

    raise XhsDownloadError("页面初始数据不完整，无法匹配 JSON 结束位置。")


def find_note(state: dict, final_url: str) -> tuple[str, dict]:
    note_map = (
        state.get("note", {})
        .get("noteDetailMap", {})
    )
    if not isinstance(note_map, dict) or not note_map:
        raise XhsDownloadError("没有在页面数据里找到 noteDetailMap。")

    url_note_id = extract_note_id_from_url(final_url)
    if url_note_id and url_note_id in note_map:
        note_id = url_note_id
    else:
        note_id = str(state.get("note", {}).get("firstNoteId") or next(iter(note_map.keys())))

    wrapper = note_map.get(note_id) or next(iter(note_map.values()))
    note = wrapper.get("note") if isinstance(wrapper, dict) else None
    if not isinstance(note, dict):
        raise XhsDownloadError("页面数据里没有找到笔记详情。")
    return note_id, note


def extract_note_id_from_url(url: str) -> str:
    match = re.search(r"/(?:discovery/item|explore)/([0-9a-fA-F]{16,32})", url)
    if not match:
        match = re.search(r"/user/profile/[0-9a-fA-F]{16,32}/([0-9a-fA-F]{16,32})(?:[/?#]|$)", url)
    if not match:
        match = re.search(r"/board/[0-9a-fA-F]{16,32}/([0-9a-fA-F]{16,32})(?:[/?#]|$)", url)
    return match.group(1) if match else ""


def note_title(note: dict, note_id: str) -> str:
    title = str(note.get("title") or "").strip()
    if title:
        return title[:48]

    desc = str(note.get("desc") or "").strip()
    for line in desc.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned[:48]
    return f"笔记-{note_id}"


def extract_images(note: dict) -> list[ImageItem]:
    image_list = note.get("imageList") or note.get("images") or []
    images: list[ImageItem] = []
    for idx, raw in enumerate(image_list, start=1):
        if not isinstance(raw, dict):
            continue
        info_urls: list[tuple[str, str]] = []
        for info in raw.get("infoList") or []:
            if isinstance(info, dict) and info.get("url"):
                info_urls.append((str(info.get("imageScene") or ""), str(info["url"])))

        images.append(
            ImageItem(
                index=idx,
                file_id=str(raw.get("fileId") or raw.get("file_id") or ""),
                trace_id=str(raw.get("traceId") or raw.get("trace_id") or ""),
                declared_width=to_int(raw.get("width")),
                declared_height=to_int(raw.get("height")),
                url_default=str(raw.get("urlDefault") or raw.get("url") or ""),
                info_urls=info_urls,
                stream=raw.get("stream") if isinstance(raw.get("stream"), dict) else {},
                live_photo=bool(raw.get("livePhoto")),
            )
        )
    return images


def extract_note_videos(note: dict) -> list[dict]:
    streams: list[dict] = []

    for key in ("stream", "video", "videoInfo", "media"):
        value = note.get(key)
        streams.extend(find_stream_objects(value))

    # Some pure video notes keep media info deeper in the note object. Avoid
    # duplicating Live Photo streams already attached to imageList.
    if not streams and str(note.get("type") or "").lower() in {"video", "media"}:
        streams.extend(find_stream_objects(note))

    seen = set()
    result: list[dict] = []
    for stream in streams:
        signature = json.dumps(stream, sort_keys=True, ensure_ascii=False)
        if signature in seen:
            continue
        seen.add(signature)
        result.append(stream)
    return merge_overlapping_streams(result)


def merge_overlapping_streams(streams: list[dict]) -> list[dict]:
    groups: list[list[dict]] = []
    group_urls: list[set[str]] = []

    for stream in streams:
        urls = stream_url_set(stream)
        if not urls:
            groups.append([stream])
            group_urls.append(set())
            continue

        matched_indexes = [index for index, urls_in_group in enumerate(group_urls) if urls & urls_in_group]
        if not matched_indexes:
            groups.append([stream])
            group_urls.append(set(urls))
            continue

        first = matched_indexes[0]
        groups[first].append(stream)
        group_urls[first].update(urls)
        for index in reversed(matched_indexes[1:]):
            groups[first].extend(groups[index])
            group_urls[first].update(group_urls[index])
            del groups[index]
            del group_urls[index]

    return [best_declared_stream(group) for group in groups]


def stream_url_set(stream: dict) -> set[str]:
    urls: set[str] = set()
    for candidate in media_candidates(stream, "dedupe"):
        url = normalize_media_url(candidate.url)
        if url:
            urls.add(url)
    return urls


def best_declared_stream(streams: list[dict]) -> dict:
    return max(streams, key=stream_declared_score)


def stream_declared_score(stream: dict) -> tuple[int, int, int, int]:
    candidates = media_candidates(stream, "dedupe")
    if not candidates:
        return (0, 0, 0, 0)
    best = max(candidates, key=candidate_declared_score)
    return candidate_declared_score(best)


def candidate_declared_score(candidate: MediaCandidate) -> tuple[int, int, int, int]:
    source = candidate.source.lower()
    source_rank = 1 if any(marker in source for marker in ("hd", "uhd", "4k", "origin", "original", "high")) else 0
    return (candidate.declared_area, candidate.bitrate, candidate.declared_size, source_rank)


def find_stream_objects(value) -> list[dict]:
    decoded = decode_json_container(value)
    if decoded is not value:
        return find_stream_objects(decoded)

    if not isinstance(value, (dict, list)):
        return []
    found: list[dict] = []

    if isinstance(value, dict):
        if is_codec_stream_container(value):
            found.append(value)
            return found
        if looks_like_stream(value):
            found.append(value)
            return found
        if has_screencast_stream(value):
            found.append(value)
        for child in value.values():
            found.extend(find_stream_objects(child))
    else:
        for child in value:
            found.extend(find_stream_objects(child))
    return found


def looks_like_stream(value: dict) -> bool:
    return bool(value.get("masterUrl") or value.get("backupUrls") or value.get("url"))


def is_codec_stream_container(value: dict) -> bool:
    codec_keys = {"h264", "h265", "h266", "av1"}
    return any(isinstance(value.get(key), list) and value.get(key) for key in codec_keys)


def has_screencast_stream(value: dict) -> bool:
    for key, child in value.items():
        if is_screencast_key(str(key)) and isinstance(child, str) and child:
            return True
        if isinstance(child, dict) and has_screencast_stream(child):
            return True
    return False


def is_screencast_key(key: str) -> bool:
    lowered = key.lower()
    return lowered.endswith("_screencast_stream") or "screencast_stream" in lowered


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
    if isinstance(decoded, (dict, list)):
        return decoded
    return value


def candidate_urls(image: ImageItem) -> list[Candidate]:
    candidates: list[Candidate] = []
    identifiers = unique_nonempty([image.trace_id, *derived_identifiers(image), image.file_id])
    image_hosts = (
        "ci.xiaohongshu.com",
        "sns-img-qc.xhscdn.com",
    )

    for ident in identifiers:
        ident = ident.strip().lstrip("/")
        if not ident:
            continue
        for host in image_hosts:
            candidates.append(Candidate(f"https://{host}/{ident}", f"{host}:raw"))
        for host in ("ci.xiaohongshu.com", "sns-img-qc.xhscdn.com"):
            candidates.append(
                Candidate(
                    f"https://{host}/{ident}?imageView2/2/format/png",
                    f"{host}:png",
                    transformed=True,
                )
            )

    dft_urls: list[str] = []
    prv_urls: list[str] = []
    for scene, url in image.info_urls:
        if not url:
            continue
        if "PRV" in scene.upper() or "_prv_" in url:
            prv_urls.append(url)
        else:
            dft_urls.append(url)
    if image.url_default:
        dft_urls.insert(0, image.url_default)

    for url in unique_nonempty(dft_urls):
        candidates.append(Candidate(url, "page:default"))
    for url in unique_nonempty(prv_urls):
        candidates.append(Candidate(url, "page:preview", preview=True))

    return dedupe_candidates(candidates)


def choose_best_image(session: requests.Session, image: ImageItem, referer: str) -> ProbeResult:
    candidates = candidate_urls(image)
    if not candidates:
        raise XhsDownloadError("没有可用图片候选链接。")

    best: ProbeResult | None = None
    failures: list[str] = []
    declared_area = image.declared_area

    for candidate in candidates:
        try:
            result = probe_image(session, candidate, referer)
        except Exception as exc:  # noqa: BLE001 - continue candidate probing.
            failures.append(f"{candidate.source}: {exc}")
            continue

        if best is None or probe_sort_key(result) > probe_sort_key(best):
            best = result

        if (
            not candidate.preview
            and not candidate.transformed
            and declared_area
            and result.area >= int(declared_area * 0.98)
        ):
            return result
        if not candidate.preview and not candidate.transformed and candidate.source.startswith("ci."):
            # A valid ci.xiaohongshu.com raw object is normally the highest-quality
            # original encoding. If the page has no declared size, avoid slow
            # probing of lower-priority CDN variants.
            if not declared_area:
                return result

    if best is None:
        detail = "; ".join(failures[:4])
        raise XhsDownloadError(f"所有图片候选都不可用。{detail}")
    return best


def probe_image(session: requests.Session, candidate: Candidate, referer: str) -> ProbeResult:
    headers = dict(BASE_HEADERS)
    headers["Referer"] = referer or "https://www.xiaohongshu.com/"
    headers["Accept"] = "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"

    try:
        response = session.get(candidate.url, headers=headers, timeout=(4, 10), allow_redirects=True)
    except requests.RequestException as exc:
        raise XhsDownloadError(f"请求失败：{exc}") from exc

    if response.status_code in {403, 404, 429}:
        raise XhsDownloadError(f"HTTP {response.status_code}")
    if response.status_code >= 400:
        raise XhsDownloadError(f"HTTP {response.status_code}")

    content = response.content
    if len(content) < 32:
        raise XhsDownloadError("响应内容过短")

    try:
        with Image.open(bytes_to_file(content)) as image:
            width, height = image.size
            image_format = (image.format or "unknown").lower()
    except UnidentifiedImageError as exc:
        raise XhsDownloadError("响应不是可识别图片") from exc

    extension = extension_from_bytes(content, image_format)
    return ProbeResult(
        candidate=candidate,
        content=content,
        extension=extension,
        image_format=image_format,
        width=width,
        height=height,
        content_type=response.headers.get("Content-Type", ""),
        bytes_count=len(content),
    )


def bytes_to_file(content: bytes):
    import io

    return io.BytesIO(content)


def probe_sort_key(result: ProbeResult) -> tuple[int, int, int, int]:
    non_preview = 0 if result.candidate.preview else 1
    original = 0 if result.candidate.transformed else 1
    return (result.area, non_preview, original, result.bytes_count)


def choose_best_media(
    session: requests.Session,
    stream: dict,
    referer: str,
    source_prefix: str,
) -> MediaProbeResult:
    candidates = media_candidates(stream, source_prefix)
    if not candidates:
        raise XhsDownloadError("没有可用视频候选链接。")

    best: MediaProbeResult | None = None
    failures: list[str] = []
    for candidate in candidates:
        try:
            result = probe_media(session, candidate, referer)
        except Exception as exc:  # noqa: BLE001 - continue candidate probing.
            failures.append(f"{candidate.source}: {exc}")
            continue
        if best is None or result.score > best.score:
            best = result

    if best is None:
        detail = "; ".join(failures[:4])
        raise XhsDownloadError(f"所有视频候选都不可用。{detail}")
    return best


def media_candidates(stream: dict, source_prefix: str) -> list[MediaCandidate]:
    candidates: list[MediaCandidate] = []
    collect_media_candidates(stream, source_prefix, candidates)

    seen: set[str] = set()
    result: list[MediaCandidate] = []
    for candidate in candidates:
        url = normalize_media_url(candidate.url)
        if not url or url in seen:
            continue
        seen.add(url)
        candidate.url = url
        result.append(candidate)
    return result


def collect_media_candidates(value, source: str, output: list[MediaCandidate], codec: str = "") -> None:
    decoded = decode_json_container(value)
    if decoded is not value:
        collect_media_candidates(decoded, source, output, codec)
        return

    if isinstance(value, list):
        for index, child in enumerate(value):
            collect_media_candidates(child, f"{source}[{index}]", output, codec)
        return
    if not isinstance(value, dict):
        return

    collect_screencast_candidates(value, source, output, codec)

    for key in ("h266", "h265", "av1", "h264"):
        if isinstance(value.get(key), list):
            collect_media_candidates(value[key], f"{source}:{key}", output, key)

    urls: list[tuple[str, bool]] = []
    for key in ("masterUrl", "master_url", "url", "mainUrl", "main_url"):
        if isinstance(value.get(key), str) and value[key]:
            urls.append((value[key], False))
    for key in ("backupUrls", "backup_urls", "backupUrl", "backup_url"):
        backup_value = value.get(key)
        if isinstance(backup_value, list):
            urls.extend((str(url), True) for url in backup_value if url)
        elif isinstance(backup_value, str) and backup_value:
            urls.append((backup_value, True))

    if urls:
        for url, backup in urls:
            output.append(
                MediaCandidate(
                    url=url,
                    source=source,
                    codec=codec or str(value.get("codec") or value.get("format") or ""),
                    width=first_int(value, "width", "videoWidth", "video_width", "w"),
                    height=first_int(value, "height", "videoHeight", "video_height", "h"),
                    bitrate=first_int(value, "bitrate", "videoBitrate", "video_bitrate", "avgBitrate", "avg_bitrate"),
                    declared_size=first_int(value, "size", "fileSize", "file_size", "contentLength", "content_length"),
                    backup=backup,
                )
            )

    for key, child in value.items():
        if key in {"h266", "h265", "av1", "h264"}:
            continue
        if isinstance(child, (dict, list)):
            collect_media_candidates(child, f"{source}.{key}", output, codec)


def collect_screencast_candidates(
    value: dict,
    source: str,
    output: list[MediaCandidate],
    codec: str = "",
) -> None:
    context_width, context_height = screencast_context_dimensions(value)

    for key, child in value.items():
        child_source = f"{source}.{key}"
        if is_screencast_key(str(key)) and isinstance(child, str) and child:
            width = 0
            height = 0
            if is_high_quality_screencast_key(str(key)):
                width = context_width
                height = context_height
            output.append(
                MediaCandidate(
                    url=child,
                    source=child_source,
                    codec=codec or "h264",
                    width=width,
                    height=height,
                    bitrate=0,
                    declared_size=0,
                    backup=is_backup_url(child),
                )
            )
        elif isinstance(child, dict):
            collect_nested_screencast_candidates(
                child,
                child_source,
                output,
                codec,
                context_width,
                context_height,
            )


def collect_nested_screencast_candidates(
    value: dict,
    source: str,
    output: list[MediaCandidate],
    codec: str,
    context_width: int,
    context_height: int,
) -> None:
    for key, child in value.items():
        child_source = f"{source}.{key}"
        if is_screencast_key(str(key)) and isinstance(child, str) and child:
            width = 0
            height = 0
            if is_high_quality_screencast_key(str(key)):
                width = context_width
                height = context_height
            output.append(
                MediaCandidate(
                    url=child,
                    source=child_source,
                    codec=codec or "h264",
                    width=width,
                    height=height,
                    bitrate=0,
                    declared_size=0,
                    backup=is_backup_url(child),
                )
            )
        elif isinstance(child, dict):
            collect_nested_screencast_candidates(
                child,
                child_source,
                output,
                codec,
                context_width,
                context_height,
            )


def screencast_context_dimensions(value: dict) -> tuple[int, int]:
    width = first_int(value, "width", "videoWidth", "video_width", "w")
    height = first_int(value, "height", "videoHeight", "video_height", "h")
    if width and height:
        return width, height

    for key in ("video", "videoInfo", "video_info", "media", "mediaInfo", "media_info"):
        child = value.get(key)
        if not isinstance(child, dict):
            continue
        width = first_int(child, "width", "videoWidth", "video_width", "w")
        height = first_int(child, "height", "videoHeight", "video_height", "h")
        if width and height:
            return width, height
    return 0, 0


def is_high_quality_screencast_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in ("hd", "uhd", "4k", "origin", "original", "high"))


def is_backup_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return "bak" in host or "backup" in host


def probe_media(session: requests.Session, candidate: MediaCandidate, referer: str) -> MediaProbeResult:
    headers = dict(BASE_HEADERS)
    headers["Referer"] = referer or "https://www.xiaohongshu.com/"
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
            raise XhsDownloadError(f"请求失败：{exc}") from exc

    if response.status_code in {403, 404, 429}:
        raise XhsDownloadError(f"HTTP {response.status_code}")
    if response.status_code >= 400 and response.status_code != 416:
        raise XhsDownloadError(f"HTTP {response.status_code}")

    content_type = response.headers.get("Content-Type", "")
    content_length = content_length_from_headers(response.headers)
    if not content_length and response.status_code == 206:
        content_range = response.headers.get("Content-Range", "")
        match = re.search(r"/(\d+)$", content_range)
        if match:
            content_length = int(match.group(1))

    if response.request.method == "GET":
        response.close()

    return MediaProbeResult(candidate=candidate, content_length=content_length, content_type=content_type)


def download_media(
    session: requests.Session,
    url: str,
    target: Path,
    referer: str,
    declared_dimensions: str = "",
) -> tuple[int, str]:
    headers = dict(BASE_HEADERS)
    headers["Referer"] = referer or "https://www.xiaohongshu.com/"
    headers["Accept"] = "video/mp4,video/*,*/*;q=0.8"

    try:
        with session.get(url, headers=headers, timeout=(6, 60), allow_redirects=True, stream=True) as response:
            if response.status_code in {403, 404, 429}:
                raise XhsDownloadError(f"视频下载失败：HTTP {response.status_code}")
            if response.status_code >= 400:
                raise XhsDownloadError(f"视频下载失败：HTTP {response.status_code}")

            target.parent.mkdir(parents=True, exist_ok=True)
            bytes_count = 0
            with target.open("wb") as file:
                for chunk in response.iter_content(chunk_size=1024 * 512):
                    if not chunk:
                        continue
                    file.write(chunk)
                    bytes_count += len(chunk)
    except requests.RequestException as exc:
        raise XhsDownloadError(f"视频下载失败：{exc}") from exc

    if bytes_count < 32:
        raise XhsDownloadError("视频响应内容过短。")

    return bytes_count, declared_dimensions or probe_video_dimensions(target)


def probe_video_dimensions(path: Path) -> str:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return ""

    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            **douyin.NO_WINDOW_KWARGS,
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
    if width and height:
        return f"{width}x{height}"
    return ""


def content_length_from_headers(headers) -> int:
    value = headers.get("Content-Length")
    if not value:
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


def normalize_media_url(url: str) -> str:
    url = html_unescape_slashes(str(url).strip())
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "http://" + url[len("http://") :]
    if url.startswith("https://"):
        return url
    return ""


def first_int(mapping: dict, *keys: str) -> int:
    for key in keys:
        value = mapping.get(key)
        number = to_int(value)
        if number:
            return number
    return 0


def format_media_info(bytes_count: int, dimensions: str, declared_dimensions: str = "") -> str:
    size_mb = bytes_count / 1024 / 1024
    if dimensions:
        return f"{dimensions}，{size_mb:.2f} MB"
    if declared_dimensions:
        return f"页面声明 {declared_dimensions}，{size_mb:.2f} MB"
    return f"{size_mb:.2f} MB"


def candidate_dimensions(candidate: MediaCandidate) -> str:
    if candidate.width and candidate.height:
        return f"{candidate.width}x{candidate.height}"
    return ""


def derived_identifiers(image: ImageItem) -> list[str]:
    """Extract path-aware object IDs such as notes_pre_post/<fileId> from page URLs."""

    if not image.file_id:
        return []
    if "/" in image.file_id:
        return [image.file_id.strip("/")]

    values: list[str] = []
    urls = [image.url_default, *(url for _scene, url in image.info_urls)]
    file_id = re.escape(image.file_id)
    for url in urls:
        normalized = html_unescape_slashes(url)
        match = re.search(rf"/((?:notes_pre_post|spectrum|image)/{file_id})(?:!|[?#]|$)", normalized)
        if match:
            values.append(match.group(1))
    return values


def extension_from_bytes(content: bytes, image_format: str) -> str:
    if content.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "webp"
    if content[:6] in {b"GIF87a", b"GIF89a"}:
        return "gif"
    mapping = {"jpeg": "jpg", "jpg": "jpg", "png": "png", "webp": "webp", "gif": "gif"}
    return mapping.get(image_format.lower(), "img")


def safe_filename(value: str, max_length: int = 80) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = "未命名"
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if value.upper() in reserved:
        value = "_" + value
    return value[:max_length].rstrip(" .") or "未命名"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for counter in range(1, 1000):
        candidate = path.with_name(f"{stem}_{counter}{suffix}")
        if not candidate.exists():
            return candidate
    raise XhsDownloadError(f"无法生成不重名文件：{path}")


def unique_nonempty(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value:
            continue
        normalized = html_unescape_slashes(str(value).strip())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def dedupe_candidates(candidates: Iterable[Candidate]) -> list[Candidate]:
    seen: set[str] = set()
    result: list[Candidate] = []
    for candidate in candidates:
        url = html_unescape_slashes(candidate.url)
        if not url or url in seen:
            continue
        seen.add(url)
        result.append(Candidate(url, candidate.source, candidate.preview, candidate.transformed))
    return result


def html_unescape_slashes(value: str) -> str:
    return value.replace("\\u002F", "/").replace("\\/", "/")


def to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
