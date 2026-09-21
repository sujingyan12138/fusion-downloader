"""跨平台通用的有界并行 Range 下载核心。

抖音、小红书、微信视频号等平台都需要在单连接速度受限时，用多连接分段
并发拿到更高吞吐。这里只保留一份经过验证的实现：

- 4 MiB 固定分段，逐块校验 HTTP 206、``Content-Range`` 起止与总长度；
- 线程本地 Session 复用连接，块按随机顺序写回精确偏移；
- 主地址失败时按轮换顺序尝试备用地址；
- 任一分块最终失败即整体放弃并删除不完整文件，由调用方决定是否回退
  （例如让上层改走稳定顺序下载或外部下载器）。

并行能力是加速手段而不是正确性前提，因此这里不吞掉失败：失败必须显式
抛出，避免把不完整文件当成完成品。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

import requests
from requests.adapters import HTTPAdapter

from services.cancellation import raise_if_cancelled


LogFn = Callable[[str], None]

RANGE_CHUNK_SIZE = 4 * 1024 * 1024
RANGE_RETRIES = 3
MAX_RANGE_WORKERS = 8

_THREAD_LOCAL = threading.local()


class ParallelDownloadUnavailable(RuntimeError):
    """并行分段下载不可用，调用方应回退到稳定路径。"""


def thread_http_session() -> requests.Session:
    """返回线程本地 Session，避免多线程共享连接池互相阻塞。"""
    session = getattr(_THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=2,
            pool_maxsize=2,
            max_retries=0,
            pool_block=True,
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _THREAD_LOCAL.session = session
    return session


def parse_content_range(value: str) -> tuple[int, int, int]:
    """解析 ``Content-Range: bytes start-end/total``，无法解析时返回 0。"""
    text = str(value or "").strip().lower()
    if not text.startswith("bytes"):
        return 0, 0, 0
    body = text[len("bytes") :].strip()
    range_part, _, total_part = body.partition("/")
    start_text, _, end_text = range_part.partition("-")
    try:
        start = int(start_text.strip())
        end = int(end_text.strip())
    except ValueError:
        return 0, 0, 0
    total_text = total_part.strip()
    total = 0 if total_text in {"", "*"} else int(total_text) if total_text.isdigit() else 0
    return start, end, total


def build_ranges(total: int, chunk_size: int = RANGE_CHUNK_SIZE) -> list[tuple[int, int]]:
    if total <= 0 or chunk_size <= 0:
        return []
    return [
        (start, min(total - 1, start + chunk_size - 1))
        for start in range(0, total, chunk_size)
    ]


def download_range_chunk(
    urls: list[str],
    headers: dict[str, str],
    start: int,
    end: int,
    total: int,
) -> tuple[int, bytes]:
    """下载单个分段并做完整校验；所有地址都失败时抛出异常。"""
    expected = end - start + 1
    last_error: Exception | None = None
    # 至少给每个备用地址一次机会，再额外保留重试次数。
    attempts = max(RANGE_RETRIES + 1, len(urls))
    for attempt in range(attempts):
        raise_if_cancelled()
        url = urls[attempt % len(urls)]
        request_headers = dict(headers)
        request_headers["Range"] = f"bytes={start}-{end}"
        try:
            session = thread_http_session()
            with session.get(url, headers=request_headers, stream=True, timeout=(10, 30)) as response:
                if response.status_code != 206:
                    raise requests.HTTPError(f"HTTP {response.status_code}", response=response)
                range_start, range_end, range_total = parse_content_range(
                    response.headers.get("Content-Range", "")
                )
                if range_start != start or range_end != end:
                    raise requests.ConnectionError(
                        f"Content-Range 不匹配：预期 {start}-{end}，实际 {range_start}-{range_end}"
                    )
                if range_total not in (0, total):
                    raise requests.ConnectionError(
                        f"媒体总大小不一致：预期 {total}，实际 {range_total}"
                    )
                chunks: list[bytes] = []
                for chunk in response.iter_content(256 * 1024):
                    raise_if_cancelled()
                    if chunk:
                        chunks.append(chunk)
                data = b"".join(chunks)
                if len(data) != expected:
                    raise requests.ConnectionError(
                        f"分段长度不完整：预期 {expected}，实际 {len(data)}"
                    )
                return start, data
        except requests.RequestException as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(min(attempt + 1, 3))
    raise ParallelDownloadUnavailable(
        f"Range {start}-{end} 已尝试 {attempts} 次：{last_error}"
    ) from last_error


def download_parallel_stream(
    urls: list[str],
    headers: dict[str, str],
    target: Path,
    label: str,
    total: int,
    logger: LogFn,
    max_workers: int = 4,
) -> None:
    """多连接并发下载 ``total`` 字节到 ``target``。

    任何分块失败都会删除不完整文件并抛出 :class:`ParallelDownloadUnavailable`，
    由调用方回退到稳定路径，绝不留下被误认作完成品的文件。
    """
    if not urls or total <= 0:
        raise ParallelDownloadUnavailable(f"{label}缺少可验证的地址或精确大小。")
    workers = max(1, min(int(max_workers or 1), MAX_RANGE_WORKERS))
    if workers <= 1:
        raise ParallelDownloadUnavailable(f"{label}当前只允许单连接。")

    ranges = build_ranges(total, RANGE_CHUNK_SIZE)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    with target.open("wb") as output:
        output.truncate(total)

    logger(f"开始并行下载{label}：{workers} 路连接，{RANGE_CHUNK_SIZE // (1024 * 1024)} MiB 分段。")
    started = time.monotonic()
    completed = 0
    last_percent = -10
    executor = ThreadPoolExecutor(max_workers=workers)
    futures = {
        executor.submit(download_range_chunk, urls, headers, start, end, total): (start, end)
        for start, end in ranges
    }
    try:
        with target.open("r+b") as output:
            for future in as_completed(futures):
                raise_if_cancelled()
                start, data = future.result()
                output.seek(start)
                output.write(data)
                completed += len(data)
                percent = min(100, int(completed / total * 100))
                if percent >= last_percent + 10 or completed >= total:
                    last_percent = percent
                    elapsed = max(time.monotonic() - started, 0.001)
                    speed = completed / 1024 / 1024 / elapsed
                    logger(f"{label}下载进度：{percent}%（{speed:.2f} MiB/s）")
    except Exception as exc:
        for future in futures:
            future.cancel()
        target.unlink(missing_ok=True)
        if isinstance(exc, ParallelDownloadUnavailable):
            raise
        from services.cancellation import DownloadCancelled

        if isinstance(exc, DownloadCancelled):
            raise
        raise ParallelDownloadUnavailable(f"{label}并行分段失败：{exc}") from exc
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    actual = target.stat().st_size if target.is_file() else 0
    if completed != total or actual != total:
        target.unlink(missing_ok=True)
        raise ParallelDownloadUnavailable(
            f"{label}大小校验失败：预期 {total}，实际写入 {completed}，文件 {actual}。"
        )
    elapsed = max(time.monotonic() - started, 0.001)
    logger(
        f"{label}下载完成：{total / 1024 / 1024:.1f} MiB，"
        f"平均 {total / 1024 / 1024 / elapsed:.2f} MiB/s。"
    )
