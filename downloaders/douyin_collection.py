from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import time
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, quote, urlparse

import requests

from . import douyin


LogFn = Callable[[str], None]


class DouyinCollectionError(RuntimeError):
    """Raised when Douyin collections cannot be listed or downloaded."""


COLLECTION_LIST_ENDPOINTS = (
    "https://www.douyin.com/aweme/v1/web/collects/list/",
    "https://www.douyin.com/aweme/v1/web/collects/user/all/",
    "https://www.douyin.com/aweme/v1/web/collects/query/",
)


def list_collections(log: LogFn | None = None) -> list[dict]:
    logger = log or (lambda _message: None)
    context = read_douyin_login_context("https://www.douyin.com/user/self?showTab=favorite_collection")
    if not context.get("cookie"):
        raise DouyinCollectionError("没有读取到抖音登录态。请先点击“登录抖音”，扫码登录后再刷新收藏夹。")
    if context.get("loginRequired"):
        raise DouyinCollectionError("当前抖音登录态看起来已失效。请重新点击“登录抖音”扫码登录。")

    logger("正在读取抖音收藏夹列表...")
    session = requests.Session()
    session.headers.update(collection_headers(context))
    collections: list[dict] = []
    failures: list[str] = []
    for endpoint in COLLECTION_LIST_ENDPOINTS:
        url = with_query(endpoint, {"device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web", "count": "50", "cursor": "0"})
        try:
            response = session.get(url, timeout=(8, 25))
            if response.status_code >= 400:
                failures.append(f"{endpoint} -> HTTP {response.status_code}")
                continue
            data = response.json()
        except (requests.RequestException, json.JSONDecodeError):
            failures.append(f"{endpoint} -> 请求或 JSON 解析失败")
            continue
        status_code = douyin.to_int(data.get("status_code"))
        status_msg = str(data.get("status_msg") or "")
        if status_code and status_code != 0:
            failures.append(f"{endpoint} -> status_code={status_code} {status_msg}")
        collections = parse_collection_list(data)
        if collections:
            break

    if not collections:
        logger("普通接口没有读到收藏夹，改用浏览器内 fetch 读取完整登录态...")
        collections = fetch_collections_in_browser(log=logger)

    if not collections:
        detail = "；".join(failures[-3:])
        suffix = f"接口结果：{detail}。" if detail else ""
        raise DouyinCollectionError(f"没有自动读取到收藏夹列表。{suffix}请确认软件内抖音登录窗口能看到收藏夹，或手动输入收藏夹 ID。")
    logger(f"读取到 {len(collections)} 个收藏夹。")
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
    logger = log or (lambda _message: None)
    clean_id = str(collection_id or "").strip()
    if not clean_id:
        raise DouyinCollectionError("收藏夹 ID 不能为空。")
    if limit is not None and limit <= 0:
        limit = None

    context = read_douyin_login_context("https://www.douyin.com/user/self?showTab=favorite_collection")
    if not context.get("cookie"):
        raise DouyinCollectionError("没有读取到抖音登录态。请先点击“登录抖音”，扫码登录后再下载收藏夹。")

    logger(f"收藏夹：{collection_name or clean_id}")
    awemes = fetch_collection_awemes(clean_id, context, limit=limit, log=logger)
    if not awemes:
        raise DouyinCollectionError("没有从该收藏夹读取到作品，可能收藏夹为空、无权限或登录态失效。")

    collection_dir = Path(output_root)
    collection_dir.mkdir(parents=True, exist_ok=True)
    collection_prefix = douyin.safe_filename(f"抖音收藏夹_{collection_name or clean_id}_{clean_id}", 120)
    report = {
        "collection_id": clean_id,
        "collection_name": collection_name or clean_id,
        "output_dir": str(collection_dir),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "items": [],
        "failures": [],
        "skipped": [],
    }

    existing_media_names = existing_media_filenames(collection_dir)
    pending: list[tuple[int, dict, str, str]] = []
    for index, aweme in enumerate(awemes, start=1):
        aweme_id = str(aweme.get("aweme_id") or aweme.get("awemeId") or "")
        if not aweme_id:
            report["failures"].append({"index": index, "error": "作品缺少 aweme_id"})
            continue
        url = collection_aweme_url(aweme)
        has_video = bool(douyin.extract_videos(aweme))
        if has_video and has_existing_aweme_nowm_video(collection_dir, aweme_id, existing_media_names):
            logger(f"已存在无水印视频，跳过收藏夹作品 {index}/{len(awemes)}：{aweme_id}")
            report["skipped"].append({"index": index, "aweme_id": aweme_id, "url": url, "reason": "exists_nowm"})
            continue
        if not has_video and has_existing_aweme_nowm_images(collection_dir, aweme_id, existing_media_names):
            logger(f"已存在无水印原图下载结果，跳过收藏夹作品 {index}/{len(awemes)}：{aweme_id}")
            report["skipped"].append({"index": index, "aweme_id": aweme_id, "url": url, "reason": "exists_nowm_orig"})
            continue
        pending.append((index, aweme, aweme_id, url))

    collection_workers = max(1, min(len(pending), max(1, min(max_workers, 4))))
    per_item_workers = max(1, min(max_workers, 3))
    if len(pending) > 1:
        logger(f"收藏夹作品并发：{collection_workers}，单作品媒体并发：{per_item_workers}")

    def run_one(item: tuple[int, dict, str, str]) -> dict:
        index, aweme, aweme_id, url = item
        logger(f"\n----- 收藏夹作品 {index}/{len(awemes)} -----")
        logger(url)
        try:
            logger("收藏夹作品使用单作品详情链路，优先无水印视频和原图质量。")
            item_report = douyin.download_note(
                url,
                collection_dir,
                log=logger,
                max_workers=per_item_workers,
                use_idm=use_idm,
                cookie_header=str(context.get("cookie") or ""),
                fallback_aweme=aweme,
            )
            return {"kind": "item", "item": {"index": index, "aweme_id": aweme_id, "status": "ok", "report": item_report, "source": "download_note"}}
        except Exception as exc:  # noqa: BLE001 - keep collection processing going.
            message = str(exc)
            logger(f"收藏夹作品失败：{message}")
            return {"kind": "failure", "item": {"index": index, "aweme_id": aweme_id, "url": url, "error": message}}

    if pending:
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

    report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    report["ok_count"] = len(report["items"])
    report["failed_count"] = len(report["failures"])
    report["skipped_count"] = len(report["skipped"])
    logger(f"收藏夹完成：成功 {report['ok_count']}，跳过 {report['skipped_count']}，失败 {report['failed_count']}")
    if not report["items"] and report["failures"]:
        raise DouyinCollectionError("收藏夹作品全部下载失败，请检查登录态或稍后重试。")
    return report


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


def read_douyin_login_context(url: str = "https://www.douyin.com/") -> dict:
    try:
        import websocket  # type: ignore[import-not-found]
    except ImportError as exc:
        raise DouyinCollectionError("缺少 websocket-client 依赖，请重新安装依赖。") from exc

    browser_path = douyin.find_chromium_browser()
    if not browser_path:
        raise DouyinCollectionError("未找到 Chrome 或 Edge，无法读取抖音登录态。")

    profile_dir = douyin.douyin_browser_profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)
    process = None
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
            raise DouyinCollectionError("Chrome DevTools 没有返回 WebSocket 地址。")
        ws = websocket.create_connection(ws_url, timeout=8)
        try:
            cdp = douyin.CdpClient(ws)
            cdp.call("Page.enable", timeout=8)
            cdp.call("Runtime.enable", timeout=8)
            cdp.call("Network.enable", timeout=8)
            cdp.call("Page.navigate", {"url": url}, timeout=8)
            value = douyin.evaluate_after_navigation(cdp, build_login_context_script(), timeout=45)
            if not isinstance(value, dict):
                value = {}
            network_cookie = get_douyin_cookie_header(cdp)
            if network_cookie:
                value["documentCookie"] = value.get("cookie") or ""
                value["cookie"] = network_cookie
                value["cookieSource"] = "cdp_network"
            return value
        finally:
            try:
                ws.close()
            except Exception:
                pass
    finally:
        if process is not None and not reuse_existing:
            douyin.terminate_process(process)


def build_login_context_script() -> str:
    return (
        "(async () => {"
        "const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));"
        "await sleep(2500);"
        "const bodyText = (document.body && document.body.innerText || '').slice(0, 3000);"
        "const loginRequired = /扫码登录|验证码登录|密码登录|登录后|请先登录|登录即代表同意/.test(bodyText);"
        "return {url: location.href, title: document.title || '', cookie: document.cookie || '', userAgent: navigator.userAgent || '', loginRequired, bodyText};"
        "})()"
    )


def collection_headers(context: dict) -> dict:
    headers = dict(douyin.BASE_HEADERS)
    headers["Accept"] = "application/json, text/plain, */*"
    headers["Referer"] = "https://www.douyin.com/"
    if context.get("userAgent"):
        headers["User-Agent"] = str(context["userAgent"])
    if context.get("cookie"):
        headers["Cookie"] = str(context["cookie"])
    return headers


def fetch_collection_awemes(collection_id: str, context: dict, limit: int | None, log: LogFn) -> list[dict]:
    try:
        awemes = fetch_collection_awemes_with_requests(collection_id, context, limit=limit, log=log)
        if awemes:
            return awemes
        log("普通接口没有读到收藏夹作品，改用浏览器内 fetch 分页读取...")
    except DouyinCollectionError as exc:
        log(f"普通接口读取收藏夹作品失败，改用浏览器内 fetch：{exc}")
    return fetch_collection_awemes_in_browser(collection_id, limit=limit, log=log)


def fetch_collection_awemes_with_requests(collection_id: str, context: dict, limit: int | None, log: LogFn) -> list[dict]:
    session = requests.Session()
    session.headers.update(collection_headers(context))
    awemes: list[dict] = []
    cursor = 0
    empty_rounds = 0
    max_pages = 200 if not limit else max(1, min(200, (limit // 20) + 5))
    for page in range(max_pages):
        params = {
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "collects_id": collection_id,
            "cursor": str(cursor),
            "count": "20",
        }
        url = with_query("https://www.douyin.com/aweme/v1/web/collects/video/list/", params)
        try:
            response = session.get(url, timeout=(8, 30))
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            raise DouyinCollectionError(f"读取收藏夹内容失败：{exc}") from exc
        status_code = douyin.to_int(data.get("status_code"))
        status_msg = str(data.get("status_msg") or "")
        if status_code and status_code != 0:
            raise DouyinCollectionError(f"收藏夹内容接口返回 status_code={status_code} {status_msg}")
        page_items = data.get("aweme_list") if isinstance(data.get("aweme_list"), list) else []
        if not page_items:
            empty_rounds += 1
            if empty_rounds >= 2:
                break
        else:
            empty_rounds = 0
            awemes.extend(item for item in page_items if isinstance(item, dict))
            log(f"收藏夹第 {page + 1} 页：读取 {len(page_items)} 个作品，累计 {len(awemes)}")
        if limit and len(awemes) >= limit:
            awemes = awemes[:limit]
            break
        has_more = bool(data.get("has_more"))
        next_cursor = douyin.to_int(data.get("cursor"))
        if not has_more and next_cursor <= cursor:
            break
        cursor = next_cursor if next_cursor > cursor else cursor + 20
        time.sleep(0.2)
    return awemes


def get_douyin_cookie_header(cdp: "douyin.CdpClient") -> str:
    try:
        result = cdp.call("Network.getAllCookies", timeout=8)
    except Exception:
        return ""
    raw_cookies = result.get("cookies") if isinstance(result.get("cookies"), list) else []
    parts: list[str] = []
    seen: set[str] = set()
    for cookie in raw_cookies:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "")
        if "douyin.com" not in domain and "iesdouyin.com" not in domain:
            continue
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def fetch_collections_in_browser(log: LogFn | None = None) -> list[dict]:
    logger = log or (lambda _message: None)
    value = evaluate_in_douyin_browser(
        "https://www.douyin.com/user/self?showTab=favorite_collection",
        build_collection_list_fetch_script(),
        timeout=60,
    )
    attempts = value.get("attempts") if isinstance(value.get("attempts"), list) else []
    collections: list[dict] = []
    failures: list[str] = []
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        data = attempt.get("data") if isinstance(attempt.get("data"), dict) else {}
        parsed = parse_collection_list(data)
        if parsed:
            collections = parsed
            break
        status = attempt.get("status")
        status_code = data.get("status_code")
        status_msg = data.get("status_msg")
        failures.append(f"HTTP {status} status_code={status_code} {status_msg}")
    if failures:
        logger("浏览器内收藏夹接口结果：" + "；".join(failures[-3:]))
    return collections


def fetch_collection_awemes_in_browser(collection_id: str, limit: int | None, log: LogFn) -> list[dict]:
    value = evaluate_in_douyin_browser(
        "https://www.douyin.com/user/self?showTab=favorite_collection",
        build_collection_aweme_fetch_script(collection_id, limit),
        timeout=120,
    )
    pages = value.get("pages") if isinstance(value.get("pages"), list) else []
    for page in pages:
        if isinstance(page, dict):
            log(
                f"浏览器内收藏夹第 {page.get('page')} 页："
                f"HTTP {page.get('status')}，读取 {page.get('count')} 个作品，累计 {page.get('total')}"
            )
    awemes = value.get("awemes") if isinstance(value.get("awemes"), list) else []
    return [item for item in awemes if isinstance(item, dict)]


def evaluate_in_douyin_browser(url: str, expression: str, timeout: int = 60) -> dict:
    try:
        import websocket  # type: ignore[import-not-found]
    except ImportError as exc:
        raise DouyinCollectionError("缺少 websocket-client 依赖，请重新安装依赖。") from exc

    browser_path = douyin.find_chromium_browser()
    if not browser_path:
        raise DouyinCollectionError("未找到 Chrome 或 Edge。")

    profile_dir = douyin.douyin_browser_profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)
    process = None
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
            raise DouyinCollectionError("Chrome DevTools 没有返回 WebSocket 地址。")
        ws = websocket.create_connection(ws_url, timeout=8)
        try:
            cdp = douyin.CdpClient(ws)
            cdp.call("Page.enable", timeout=8)
            cdp.call("Runtime.enable", timeout=8)
            cdp.call("Page.navigate", {"url": url}, timeout=8)
            value = douyin.evaluate_after_navigation(cdp, expression, timeout=timeout)
            return value if isinstance(value, dict) else {}
        finally:
            try:
                ws.close()
            except Exception:
                pass
    finally:
        if process is not None and not reuse_existing:
            douyin.terminate_process(process)


def build_collection_list_fetch_script() -> str:
    endpoints_json = json.dumps(COLLECTION_LIST_ENDPOINTS, ensure_ascii=False)
    return (
        "(async () => {"
        "const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));"
        "await sleep(1200);"
        f"const endpoints = {endpoints_json};"
        "const attempts = [];"
        "for (const endpoint of endpoints) {"
        "  const url = new URL(endpoint, location.origin);"
        "  const params = {device_platform:'webapp', aid:'6383', channel:'channel_pc_web', count:'50', cursor:'0'};"
        "  for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value);"
        "  try {"
        "    const response = await fetch(url.toString(), {credentials:'include', headers:{accept:'application/json, text/plain, */*'}});"
        "    const text = await response.text();"
        "    let data = null;"
        "    try { data = JSON.parse(text); } catch (_error) {}"
        "    attempts.push({url:url.toString(), status:response.status, data, text:text.slice(0, 500)});"
        "  } catch (error) {"
        "    attempts.push({url:url.toString(), status:0, error:String(error)});"
        "  }"
        "}"
        "const bodyText = (document.body && document.body.innerText || '').slice(0, 3000);"
        "const loginRequired = /扫码登录|验证码登录|密码登录|登录后|请先登录|登录即代表同意/.test(bodyText);"
        "return {url:location.href, title:document.title || '', loginRequired, attempts};"
        "})()"
    )


def build_collection_aweme_fetch_script(collection_id: str, limit: int | None) -> str:
    collection_json = json.dumps(str(collection_id), ensure_ascii=False)
    max_items = max(0, int(limit or 0))
    max_pages = 200 if not max_items else max(1, min(200, (max_items // 20) + 5))
    return (
        "(async () => {"
        "const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));"
        f"const collectionId = {collection_json};"
        f"const maxItems = {max_items};"
        f"const maxPages = {max_pages};"
        "const pages = [];"
        "const awemes = [];"
        "let cursor = 0;"
        "let emptyRounds = 0;"
        "for (let page = 0; page < maxPages; page++) {"
        "  const url = new URL('/aweme/v1/web/collects/video/list/', location.origin);"
        "  const params = {device_platform:'webapp', aid:'6383', channel:'channel_pc_web', collects_id:collectionId, cursor:String(cursor), count:'20'};"
        "  for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value);"
        "  let data = null;"
        "  let status = 0;"
        "  try {"
        "    const response = await fetch(url.toString(), {credentials:'include', headers:{accept:'application/json, text/plain, */*'}});"
        "    status = response.status;"
        "    const text = await response.text();"
        "    try { data = JSON.parse(text); } catch (_error) { data = {status_msg:text.slice(0, 300)}; }"
        "  } catch (error) {"
        "    pages.push({page:page + 1, status, error:String(error), count:0, total:awemes.length});"
        "    break;"
        "  }"
        "  const list = Array.isArray(data && data.aweme_list) ? data.aweme_list : [];"
        "  if (!list.length) emptyRounds += 1; else emptyRounds = 0;"
        "  for (const item of list) awemes.push(item);"
        "  pages.push({page:page + 1, status, status_code:data && data.status_code, status_msg:data && data.status_msg, count:list.length, total:awemes.length, cursor:data && data.cursor, has_more:!!(data && data.has_more)});"
        "  if (maxItems && awemes.length >= maxItems) { awemes.length = maxItems; break; }"
        "  if (emptyRounds >= 2) break;"
        "  const nextCursor = Number(data && data.cursor) || 0;"
        "  if (!(data && data.has_more) && nextCursor <= cursor) break;"
        "  cursor = nextCursor > cursor ? nextCursor : cursor + 20;"
        "  await sleep(150);"
        "}"
        "return {url:location.href, collectionId, pages, awemes};"
        "})()"
    )


def parse_collection_list(data: dict) -> list[dict]:
    candidates: list = []
    if isinstance(data.get("collects_list"), list):
        candidates.extend(data["collects_list"])
    if isinstance(data.get("collects"), list):
        candidates.extend(data["collects"])
    inner = data.get("data")
    if isinstance(inner, dict):
        if isinstance(inner.get("collects_list"), list):
            candidates.extend(inner["collects_list"])
        if isinstance(inner.get("collects"), list):
            candidates.extend(inner["collects"])

    result: list[dict] = []
    seen: set[str] = set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        coll_id = item.get("collects_id") or item.get("collects_id_str") or item.get("id") or item.get("coll_id")
        name = item.get("collects_name") or item.get("name") or item.get("title") or item.get("desc")
        if not coll_id or not name:
            continue
        coll_id = str(coll_id)
        if coll_id in seen:
            continue
        seen.add(coll_id)
        result.append({"id": coll_id, "name": str(name), "count": item.get("total") or item.get("count") or item.get("aweme_count") or ""})
    return result


def collection_aweme_url(aweme: dict) -> str:
    aweme_id = str(aweme.get("aweme_id") or aweme.get("awemeId") or "")
    if aweme.get("images"):
        return f"https://www.douyin.com/note/{aweme_id}"
    return f"https://www.douyin.com/video/{aweme_id}"


def with_query(base_url: str, params: dict[str, str]) -> str:
    parsed = urlparse(base_url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    for key, value in params.items():
        query[key] = [value]
    parts: list[str] = []
    for key, values in query.items():
        for value in values:
            parts.append(f"{quote(key, safe='')}={quote(str(value), safe='')}")
    return parsed._replace(query="&".join(parts)).geturl()
