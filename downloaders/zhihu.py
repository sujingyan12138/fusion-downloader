from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag
import requests

from downloaders import douyin
from downloaders.article_common import (
    ArticleDownloadError,
    DEFAULT_USER_AGENT,
    WebArticle,
    clean_text,
    save_web_article,
)
from downloaders.covers import CoverCandidate


URL_RE = re.compile(
    r"https?://(?:(?:www\.)?zhihu\.com|zhuanlan\.zhihu\.com)/"
    r"[^\s<>'\"。，、；！）\]】]+",
    re.IGNORECASE,
)
ANSWER_RE = re.compile(r"/question/(\d+)/answer/(\d+)")
ARTICLE_RE = re.compile(r"/p/(\d+)")


class ZhihuDownloadError(ArticleDownloadError):
    """Raised when a Zhihu answer or column article cannot be extracted."""


def extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in URL_RE.finditer(text or ""):
        url = match.group(0).rstrip("。，、；;！!）)]}】")
        normalized = canonicalize_url(url)
        if normalized not in seen:
            seen.add(normalized)
            urls.append(normalized)
    return urls


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host not in {"zhihu.com", "www.zhihu.com", "zhuanlan.zhihu.com"}:
        raise ValueError("这不是知乎链接。")
    path = parts.path.rstrip("/")
    if not (ANSWER_RE.search(path) or ARTICLE_RE.search(path)):
        raise ValueError("目前支持知乎回答链接和知乎专栏文章链接。")
    preferred_host = "zhuanlan.zhihu.com" if ARTICLE_RE.search(path) else "www.zhihu.com"
    return urlunsplit(("https", preferred_host, path, "", ""))


def zhihu_browser_profile_dir() -> Path:
    return douyin.app_base_dir() / "知乎浏览器登录态"


def open_zhihu_login_browser() -> Path:
    browser_path = douyin.find_chromium_browser()
    if not browser_path:
        raise ZhihuDownloadError("未找到 Chrome 或 Edge，无法打开知乎登录窗口。")
    profile_dir = zhihu_browser_profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)
    douyin.launch_chromium_cdp_browser(
        browser_path,
        profile_dir,
        visible=True,
        url="https://www.zhihu.com/signin",
    )
    return profile_dir


def download_article(
    url: str,
    output_root: str | Path,
    *,
    max_workers: int = 4,
    organize_by_author: bool = True,
    log=None,
) -> dict:
    logger = log or (lambda _message: None)
    article = fetch_article(url, logger)
    logger(f"已提取知乎文章：{article.author}《{article.title}》")
    return save_web_article(
        article,
        output_root,
        organize_by_author=organize_by_author,
        image_candidates=zhihu_image_candidates,
        max_workers=max_workers,
        log=logger,
    )


def fetch_article(url: str, logger=None) -> WebArticle:
    logger = logger or (lambda _message: None)
    canonical = canonicalize_url(url)
    failures: list[str] = []
    try:
        article = extract_via_http(canonical)
        logger("知乎正文读取方式：公开页面")
        return article
    except ZhihuDownloadError as exc:
        failures.append(f"公开页面：{exc}")

    if opencli_command():
        try:
            logger("普通请求受限，正在复用当前 Chrome 会话读取知乎正文...")
            article = extract_via_opencli(canonical)
            logger("知乎正文读取方式：当前 Chrome/OpenCLI 会话")
            return article
        except (ZhihuDownloadError, RuntimeError, subprocess.TimeoutExpired) as exc:
            failures.append(f"当前 Chrome：{exc}")

    try:
        logger("正在尝试软件专属知乎登录态...")
        article = extract_via_builtin_browser(canonical)
        logger("知乎正文读取方式：软件专属登录态")
        return article
    except ZhihuDownloadError as exc:
        failures.append(f"软件登录态：{exc}")

    detail = "；".join(failures[-3:])
    raise ZhihuDownloadError(
        "知乎拒绝了当前请求，未能取得目标正文。请点击“登录知乎”，"
        "在打开的窗口完成登录后重试；若仍提示请求异常，请在正常 Chrome 中登录知乎并保持 OpenCLI 扩展连接。"
        f" 详情：{detail}"
    )


def extract_via_http(url: str) -> WebArticle:
    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            timeout=(8, 35),
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise ZhihuDownloadError(f"请求失败：{exc}") from exc
    if response.status_code == 403:
        raise ZhihuDownloadError("HTTP 403 异常请求限制")
    if response.status_code >= 400:
        raise ZhihuDownloadError(f"HTTP {response.status_code}")
    return parse_zhihu_html(response.text, url)


def parse_zhihu_html(html: str, url: str) -> WebArticle:
    soup = BeautifulSoup(html, "html.parser")
    answer_match = ANSWER_RE.search(url)
    if answer_match:
        question_id, answer_id = answer_match.groups()
        target = None
        for candidate in soup.select(".AnswerItem"):
            zop = str(candidate.get("data-zop") or "")
            if candidate.get("name") == answer_id or answer_id in zop:
                target = candidate
                break
        if target is None:
            raise ZhihuDownloadError("页面中没有找到目标回答。")
        body = target.select_one(".RichContent-inner, .RichText")
        if body is None:
            raise ZhihuDownloadError("目标回答正文为空。")
        title_node = soup.select_one("h1.QuestionHeader-title")
        author_node = target.select_one(".AuthorInfo-name")
        return WebArticle(
            platform="知乎",
            source_type="zhihu_answer",
            source_url=url,
            canonical_url=f"https://www.zhihu.com/question/{question_id}/answer/{answer_id}",
            content_id=answer_id,
            title=clean_text(title_node.get_text(" ", strip=True) if title_node else ""),
            author=clean_text(author_node.get_text(" ", strip=True) if author_node else "") or "未知作者",
            body_html=str(body),
            published=meta_content(target, "meta[itemprop='dateCreated']"),
            modified=meta_content(target, "meta[itemprop='dateModified']"),
        )

    article_match = ARTICLE_RE.search(url)
    body = soup.select_one(
        ".Post-RichTextContainer .RichText, article .Post-RichText, .Post-RichText"
    )
    if not article_match or body is None:
        raise ZhihuDownloadError("页面中没有找到知乎专栏正文。")
    title_node = soup.select_one("h1.Post-Title")
    author_node = soup.select_one(".AuthorInfo-name")
    article_id = article_match.group(1)
    return WebArticle(
        platform="知乎",
        source_type="zhihu_zhuanlan",
        source_url=url,
        canonical_url=f"https://zhuanlan.zhihu.com/p/{article_id}",
        content_id=article_id,
        title=clean_text(title_node.get_text(" ", strip=True) if title_node else ""),
        author=clean_text(author_node.get_text(" ", strip=True) if author_node else "") or "未知作者",
        body_html=str(body),
        published=meta_content(soup, "meta[itemprop='datePublished']"),
        modified=meta_content(soup, "meta[itemprop='dateModified']"),
    )


def meta_content(node, selector: str) -> str:
    element = node.select_one(selector)
    return clean_text(element.get("content") if element else "")


def extract_via_opencli(url: str) -> WebArticle:
    session = f"fusion-zhihu-{uuid.uuid4().hex[:10]}"
    opened = False
    try:
        result = run_opencli(
            ["browser", session, "--window", "background", "open", url],
            timeout_seconds=60,
        )
        opened = True
        current_url = str(result.get("url") or url)
        payload = run_opencli(
            ["browser", session, "eval", build_extract_script(current_url)],
            timeout_seconds=110,
        )
        return article_from_payload(payload, url)
    except subprocess.TimeoutExpired as exc:
        raise ZhihuDownloadError("OpenCLI 等待浏览器响应超时。") from exc
    finally:
        if opened:
            try:
                run_opencli(["browser", session, "close"], timeout_seconds=15)
            except (RuntimeError, subprocess.TimeoutExpired):
                pass


def extract_via_builtin_browser(url: str) -> WebArticle:
    try:
        import websocket  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ZhihuDownloadError("缺少 websocket-client 依赖。") from exc
    browser_path = douyin.find_chromium_browser()
    if not browser_path:
        raise ZhihuDownloadError("未找到 Chrome 或 Edge。")
    profile_dir = zhihu_browser_profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)
    process: subprocess.Popen | None = None
    reuse_existing = False
    try:
        port, process, reuse_existing = douyin.open_comment_cdp_port(browser_path, profile_dir)
        target = douyin.create_cdp_target(port)
        ws_url = str(target.get("webSocketDebuggerUrl") or "")
        if not ws_url:
            raise ZhihuDownloadError("Chrome DevTools 没有返回 WebSocket 地址。")
        ws = websocket.create_connection(ws_url, timeout=8)
        try:
            cdp = douyin.CdpClient(ws)
            cdp.call("Page.enable", timeout=8)
            cdp.call("Runtime.enable", timeout=8)
            cdp.call("Page.navigate", {"url": url}, timeout=8)
            payload = douyin.evaluate_after_navigation(
                cdp,
                build_extract_script(url),
                timeout=90,
            )
            return article_from_payload(payload, url)
        finally:
            try:
                ws.close()
            except Exception:
                pass
    except ZhihuDownloadError:
        raise
    except Exception as exc:
        raise ZhihuDownloadError(str(exc)) from exc
    finally:
        if process is not None and not reuse_existing:
            douyin.terminate_process(process)


def article_from_payload(payload: object, source_url: str) -> WebArticle:
    if not isinstance(payload, dict):
        raise ZhihuDownloadError("浏览器没有返回结构化正文。")
    if payload.get("error"):
        page = clean_text(payload.get("pageText"))[:180]
        raise ZhihuDownloadError(
            f"浏览器未找到正文（{payload.get('error')}）"
            + (f"；页面提示：{page}" if page else "")
        )
    body_html = str(payload.get("bodyHtml") or "").strip()
    if len(BeautifulSoup(body_html, "html.parser").get_text("", strip=True)) < 20:
        raise ZhihuDownloadError("浏览器返回的正文过短。")
    answer_id = clean_text(payload.get("answerId"))
    question_id = clean_text(payload.get("questionId"))
    article_id = clean_text(payload.get("articleId"))
    if answer_id:
        canonical = f"https://www.zhihu.com/question/{question_id}/answer/{answer_id}"
        source_type = "zhihu_answer"
        content_id = answer_id
    elif article_id:
        canonical = f"https://zhuanlan.zhihu.com/p/{article_id}"
        source_type = "zhihu_zhuanlan"
        content_id = article_id
    else:
        raise ZhihuDownloadError("浏览器正文缺少回答或文章 ID。")
    title = clean_text(payload.get("title"))
    if not title:
        raise ZhihuDownloadError("浏览器已提取正文，但标题为空。")
    return WebArticle(
        platform="知乎",
        source_type=source_type,
        source_url=source_url,
        canonical_url=canonical,
        content_id=content_id,
        title=title,
        author=clean_text(payload.get("author")) or "未知作者",
        body_html=body_html,
        published=clean_text(payload.get("published")),
        modified=clean_text(payload.get("modified")),
    )


def build_extract_script(url: str) -> str:
    answer_match = ANSWER_RE.search(url)
    if answer_match:
        question_id, answer_id = answer_match.groups()
        return f"""
(async () => {{
  const answerId = {json.dumps(answer_id)};
  const questionId = {json.dumps(question_id)};
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const deadline = Date.now() + 70000;
  const parseZop = element => {{
    const raw = element?.getAttribute('data-zop')
      || element?.querySelector('[data-zop]')?.getAttribute('data-zop') || '';
    try {{ return JSON.parse(raw); }} catch {{ return {{}}; }}
  }};
  while (Date.now() < deadline) {{
    const pageText = document.body?.innerText || '';
    if (pageText.includes('请求存在异常') || pageText.includes('"code":40362')) {{
      return {{error: 'request_blocked', pageText: pageText.slice(0, 500)}};
    }}
    const target = [...document.querySelectorAll('.AnswerItem')].find(element =>
      element.getAttribute('name') === answerId
      || String(parseZop(element).itemId || '') === answerId
    );
    const body = target?.querySelector('.RichContent-inner, .RichText');
    if (target && body) {{
      const zop = parseZop(target);
      return {{
        title: (document.querySelector('h1.QuestionHeader-title')?.textContent
          || zop.title || '').trim(),
        author: (target.querySelector('.AuthorInfo-name')?.textContent
          || zop.authorName || '').trim(),
        published: target.querySelector('meta[itemprop="dateCreated"]')?.content || '',
        modified: target.querySelector('meta[itemprop="dateModified"]')?.content || '',
        bodyHtml: body.innerHTML,
        answerId,
        questionId
      }};
    }}
    await sleep(500);
  }}
  return {{error: 'target_not_found', pageText: (document.body?.innerText || '').slice(0, 500)}};
}})()
""".strip()

    article_id = ARTICLE_RE.search(url).group(1) if ARTICLE_RE.search(url) else ""
    return f"""
(async () => {{
  const articleId = {json.dumps(article_id)};
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const deadline = Date.now() + 70000;
  while (Date.now() < deadline) {{
    const pageText = document.body?.innerText || '';
    if (pageText.includes('请求存在异常') || pageText.includes('"code":40362')) {{
      return {{error: 'request_blocked', pageText: pageText.slice(0, 500)}};
    }}
    const body = document.querySelector(
      '.Post-RichTextContainer .RichText, article .Post-RichText, .Post-RichText'
    );
    if (body) {{
      return {{
        title: (document.querySelector('h1.Post-Title')?.textContent || '').trim(),
        author: (document.querySelector('.AuthorInfo-name')?.textContent || '').trim(),
        published: document.querySelector('meta[itemprop="datePublished"]')?.content || '',
        modified: document.querySelector('meta[itemprop="dateModified"]')?.content || '',
        bodyHtml: body.innerHTML,
        articleId
      }};
    }}
    await sleep(500);
  }}
  return {{error: 'body_not_found', pageText: (document.body?.innerText || '').slice(0, 500)}};
}})()
""".strip()


def parse_first_json(value: str):
    stripped = value.lstrip()
    try:
        result, _ = json.JSONDecoder().raw_decode(stripped)
        return result
    except json.JSONDecodeError as exc:
        preview = re.sub(r"\s+", " ", value).strip()[:300]
        raise RuntimeError(f"无法解析 OpenCLI 返回值：{preview}") from exc


def opencli_command() -> list[str] | None:
    launcher = shutil.which("opencli.cmd") or shutil.which("opencli")
    if not launcher:
        return None
    launcher_path = Path(launcher)
    if launcher_path.suffix.lower() in {".cmd", ".ps1"}:
        node = launcher_path.parent / "node.exe"
        main = (
            launcher_path.parent
            / "node_modules"
            / "@jackwener"
            / "opencli"
            / "dist"
            / "src"
            / "main.js"
        )
        if node.is_file() and main.is_file():
            return [str(node), str(main)]
    return [launcher]


def run_opencli(args: list[str], timeout_seconds: int = 60):
    command = opencli_command()
    if not command:
        raise RuntimeError("未找到 OpenCLI。")
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    completed = subprocess.run(
        [*command, *args],
        cwd=douyin.app_base_dir(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
        **kwargs,
    )
    if completed.returncode != 0:
        detail = clean_text(completed.stderr or completed.stdout)
        raise RuntimeError(f"OpenCLI 执行失败：{detail or f'退出码 {completed.returncode}'}")
    return parse_first_json(completed.stdout)


def zhihu_image_candidates(image: Tag, source_url: str) -> list[CoverCandidate]:
    candidates: list[CoverCandidate] = []
    for preference, attribute in enumerate(
        ("data-original", "data-actualsrc", "data-src", "src"),
        start=8,
    ):
        value = clean_text(image.get(attribute))
        if not value or value.startswith("data:"):
            continue
        candidates.append(
            CoverCandidate(
                url=urljoin(source_url, value),
                source=f"zhihu:{attribute}",
                preference=20 - preference,
                preview=attribute == "src",
            )
        )
    srcset = clean_text(image.get("srcset"))
    if srcset:
        for index, item in enumerate(srcset.split(","), start=1):
            value = item.strip().split()[0]
            if value:
                candidates.append(
                    CoverCandidate(
                        url=urljoin(source_url, value),
                        source=f"zhihu:srcset[{index}]",
                        preference=index,
                    )
                )
    return candidates
