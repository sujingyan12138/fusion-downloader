from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from downloaders import douyin, xiaohongshu
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
    if platform == "小红书":
        urls = xiaohongshu.extract_urls(text)
    else:
        urls = douyin.extract_urls(text)
    if single:
        return urls[:1]
    return urls


def run_task(options: TaskOptions, log: LogFn) -> dict:
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


def run_douyin_urls(options: TaskOptions, log: LogFn) -> dict:
    reports = []
    failures = []
    root = options.output_root
    media_root = root
    comment_root = root
    for index, url in enumerate(options.inputs, start=1):
        log(f"\n===== 抖音任务 {index}/{len(options.inputs)} =====")
        log(url)
        try:
            if options.feature == "评论区图片":
                report = douyin.download_comment_images(url, comment_root, limit=options.comment_limit, log=log, max_workers=options.max_workers)
            elif options.feature == "作品媒体+评论区图片":
                media_report = douyin.download_note(url, media_root, log=log, max_workers=options.max_workers, use_idm=options.download_engine)
                comment_report = douyin.download_comment_images(url, comment_root, limit=options.comment_limit, log=log, max_workers=options.max_workers)
                comment_report["media_report"] = media_report
                report = comment_report
            else:
                report = douyin.download_note(url, media_root, log=log, max_workers=options.max_workers, use_idm=options.download_engine)
            reports.append(report)
        except Exception as exc:  # noqa: BLE001 - aggregate task failures for GUI.
            message = str(exc)
            log(f"任务失败：{message}")
            failures.append({"url": url, "error": message})
    return {"output_dir": str(root), "items": reports, "failures": failures}


def run_xhs_urls(options: TaskOptions, log: LogFn) -> dict:
    reports = []
    failures = []
    root = options.output_root
    for index, url in enumerate(options.inputs, start=1):
        log(f"\n===== 小红书任务 {index}/{len(options.inputs)} =====")
        log(url)
        try:
            if options.feature == "评论区图片":
                report = xiaohongshu.download_comment_images(url, root, limit=options.comment_limit, log=log, max_workers=options.max_workers)
            elif options.feature == "作品媒体+评论区图片":
                media_report = xiaohongshu.download_note(url, root, log=log, max_workers=options.max_workers, use_idm=options.download_engine)
                comment_report = xiaohongshu.download_comment_images(url, root, limit=options.comment_limit, log=log, max_workers=options.max_workers)
                comment_report["media_report"] = media_report
                report = comment_report
            else:
                report = xiaohongshu.download_note(url, root, log=log, max_workers=options.max_workers, use_idm=options.download_engine)
            reports.append(report)
        except Exception as exc:  # noqa: BLE001 - aggregate task failures for GUI.
            message = str(exc)
            log(f"任务失败：{message}")
            failures.append({"url": url, "error": message})
    return {"output_dir": str(root), "items": reports, "failures": failures}
