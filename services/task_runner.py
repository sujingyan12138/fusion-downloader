from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from downloaders import bilibili, douyin, xiaohongshu, youtube
from downloaders.douyin_collection import download_collection


LogFn = Callable[[str], None]


@dataclass
class TaskOptions:
    platform: str
    feature: str
    inputs: list[str]
    output_root: Path
    download_engine: str = "smart"
    max_workers: int = 4
    comment_limit: int | None = None
    collection_limit: int | None = None
    collection_id: str = ""
    collection_name: str = ""


def extract_task_inputs(platform: str, text: str, single: bool) -> list[str]:
    if platform == "YouTube":
        urls = youtube.extract_urls(text)
    elif platform == "Bilibili":
        urls = bilibili.extract_urls(text)
    elif platform == "小红书":
        urls = xiaohongshu.extract_urls(text)
    else:
        urls = douyin.extract_urls(text)
    if single:
        return urls[:1]
    return urls


def run_task(options: TaskOptions, log: LogFn) -> dict:
    if options.platform == "YouTube":
        return run_youtube_urls(options, log)
    if options.platform == "Bilibili":
        return run_bilibili_urls(options, log)
    if options.platform == "小红书":
        if options.feature in {"收藏作品", "收藏视频", "收藏夹"}:
            return xiaohongshu.download_collection(
                options.collection_id,
                options.collection_name or options.collection_id,
                options.output_root,
                limit=options.collection_limit,
                log=log,
                max_workers=options.max_workers,
                use_idm=options.download_engine,
            )
        return run_xhs_urls(options, log)

    if options.feature == "收藏夹":
        return download_collection(
            options.collection_id,
            options.collection_name or options.collection_id,
            options.output_root,
            limit=options.collection_limit,
            log=log,
            max_workers=options.max_workers,
            use_idm=options.download_engine,
        )
    return run_douyin_urls(options, log)


def run_youtube_urls(options: TaskOptions, log: LogFn) -> dict:
    reports = []
    failures = []
    root = options.output_root
    log("YouTube 将下载公开可获取的最高画质；需要登录、年龄验证或私享的视频暂不支持。")

    def run_one(index: int, url: str, per_item_workers: int) -> dict:
        log(f"\n===== YouTube 任务 {index}/{len(options.inputs)} =====")
        log(url)
        try:
            report = youtube.download_video(url, root, log=log, max_workers=per_item_workers)
            return {"index": index, "url": url, "status": "ok", "report": report}
        except Exception as exc:  # noqa: BLE001 - aggregate task failures for GUI.
            message = str(exc)
            log(f"任务失败：{message}")
            return {"index": index, "url": url, "status": "failed", "error": message}

    outer_workers, per_item_workers = split_url_workers(len(options.inputs), options.max_workers)
    if len(options.inputs) > 1:
        log(f"批量任务并发：视频 {outer_workers}，单视频分片 {per_item_workers}")
    results = run_url_batch(options.inputs, outer_workers, per_item_workers, run_one)
    for result in results:
        if result.get("status") == "ok":
            reports.append(result["report"])
        else:
            failures.append({"url": result.get("url", ""), "error": result.get("error", "")})
    return {"output_dir": str(root), "items": reports, "failures": failures}


def run_bilibili_urls(options: TaskOptions, log: LogFn) -> dict:
    reports = []
    failures = []
    root = options.output_root
    try:
        login_context = bilibili.read_bilibili_login_context()
    except Exception as exc:  # noqa: BLE001 - public formats remain available without login.
        login_context = {"cookie": "", "logged_in": False, "vip": False}
        log(f"读取 Bilibili 登录态失败，将按未登录下载：{exc}")
    cookie_header = str(login_context.get("cookie") or "")
    if login_context.get("vip"):
        log("Bilibili 大会员登录态可用：将自动选择账号可用的最高画质。")
    elif login_context.get("logged_in"):
        log("Bilibili 普通账号登录态可用：将自动选择账号可用的最高画质。")
    else:
        log("Bilibili 当前未登录：将下载公开可用的最高画质；大会员 4K/高帧率不会出现在候选中。")

    def run_one(index: int, url: str, per_item_workers: int) -> dict:
        log(f"\n===== Bilibili 任务 {index}/{len(options.inputs)} =====")
        log(url)
        try:
            report = bilibili.download_video(
                url, root, log=log, max_workers=per_item_workers, cookie_header=cookie_header
            )
            return {"index": index, "url": url, "status": "ok", "report": report}
        except Exception as exc:  # noqa: BLE001 - aggregate task failures for GUI.
            message = str(exc)
            log(f"任务失败：{message}")
            return {"index": index, "url": url, "status": "failed", "error": message}

    outer_workers, per_item_workers = split_url_workers(len(options.inputs), options.max_workers)
    if len(options.inputs) > 1:
        log(f"批量任务并发：视频 {outer_workers}，单视频分片 {per_item_workers}")
    results = run_url_batch(options.inputs, outer_workers, per_item_workers, run_one)
    for result in results:
        if result.get("status") == "ok":
            reports.append(result["report"])
        else:
            failures.append({"url": result.get("url", ""), "error": result.get("error", "")})
    return {"output_dir": str(root), "items": reports, "failures": failures}


def run_douyin_urls(options: TaskOptions, log: LogFn) -> dict:
    reports = []
    failures = []
    root = options.output_root
    media_root = root
    comment_root = root

    def run_one(index: int, url: str, per_item_workers: int) -> dict:
        log(f"\n===== 抖音任务 {index}/{len(options.inputs)} =====")
        log(url)
        try:
            if options.feature == "评论区图片":
                report = douyin.download_comment_images(url, comment_root, limit=options.comment_limit, log=log, max_workers=per_item_workers)
            elif options.feature == "作品媒体+评论区图片":
                media_report = douyin.download_note(url, media_root, log=log, max_workers=per_item_workers, use_idm=options.download_engine)
                comment_report = douyin.download_comment_images(url, comment_root, limit=options.comment_limit, log=log, max_workers=per_item_workers)
                comment_report["media_report"] = media_report
                report = comment_report
            else:
                report = douyin.download_note(url, media_root, log=log, max_workers=per_item_workers, use_idm=options.download_engine)
            return {"index": index, "url": url, "status": "ok", "report": report}
        except Exception as exc:  # noqa: BLE001 - aggregate task failures for GUI.
            message = str(exc)
            log(f"任务失败：{message}")
            return {"index": index, "url": url, "status": "failed", "error": message}

    outer_workers, per_item_workers = split_url_workers(len(options.inputs), options.max_workers)
    if len(options.inputs) > 1:
        log(f"批量任务并发：作品 {outer_workers}，单作品媒体/评论 {per_item_workers}")
    results = run_url_batch(options.inputs, outer_workers, per_item_workers, run_one)
    for result in results:
        if result.get("status") == "ok":
            reports.append(result["report"])
        else:
            failures.append({"url": result.get("url", ""), "error": result.get("error", "")})
    return {"output_dir": str(root), "items": reports, "failures": failures}


def run_xhs_urls(options: TaskOptions, log: LogFn) -> dict:
    reports = []
    failures = []
    root = options.output_root

    def run_one(index: int, url: str, per_item_workers: int) -> dict:
        log(f"\n===== 小红书任务 {index}/{len(options.inputs)} =====")
        log(url)
        try:
            if options.feature == "评论区图片":
                report = xiaohongshu.download_comment_images(url, root, limit=options.comment_limit, log=log, max_workers=per_item_workers)
            elif options.feature == "作品媒体+评论区图片":
                media_report = xiaohongshu.download_note(url, root, log=log, max_workers=per_item_workers, use_idm=options.download_engine)
                comment_report = xiaohongshu.download_comment_images(url, root, limit=options.comment_limit, log=log, max_workers=per_item_workers)
                comment_report["media_report"] = media_report
                report = comment_report
            else:
                report = xiaohongshu.download_note(url, root, log=log, max_workers=per_item_workers, use_idm=options.download_engine)
            return {"index": index, "url": url, "status": "ok", "report": report}
        except Exception as exc:  # noqa: BLE001 - aggregate task failures for GUI.
            message = str(exc)
            log(f"任务失败：{message}")
            return {"index": index, "url": url, "status": "failed", "error": message}

    outer_workers, per_item_workers = split_url_workers(len(options.inputs), options.max_workers)
    if len(options.inputs) > 1:
        log(f"批量任务并发：作品 {outer_workers}，单作品媒体/评论 {per_item_workers}")
    results = run_url_batch(options.inputs, outer_workers, per_item_workers, run_one)
    for result in results:
        if result.get("status") == "ok":
            reports.append(result["report"])
        else:
            failures.append({"url": result.get("url", ""), "error": result.get("error", "")})
    return {"output_dir": str(root), "items": reports, "failures": failures}


def split_url_workers(total_inputs: int, max_workers: int) -> tuple[int, int]:
    if total_inputs <= 1:
        return 1, max(1, max_workers)
    if max_workers <= 2:
        return 1, max(1, max_workers)
    outer_workers = min(total_inputs, 4 if max_workers >= 8 else 2)
    per_item_workers = 3 if max_workers >= 8 else 2
    return outer_workers, per_item_workers


def run_url_batch(
    inputs: list[str],
    outer_workers: int,
    per_item_workers: int,
    worker: Callable[[int, str, int], dict],
) -> list[dict]:
    if outer_workers <= 1:
        return [worker(index, url, per_item_workers) for index, url in enumerate(inputs, start=1)]
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=outer_workers) as executor:
        futures = [executor.submit(worker, index, url, per_item_workers) for index, url in enumerate(inputs, start=1)]
        for future in as_completed(futures):
            results.append(future.result())
    return sorted(results, key=lambda item: item.get("index", 0))
