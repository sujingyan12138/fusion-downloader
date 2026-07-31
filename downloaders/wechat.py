from __future__ import annotations

from datetime import datetime
import hashlib
import re
from pathlib import Path
import subprocess
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit
import uuid

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
from downloaders.opencli_browser import (
    OpenCliWindowGuard,
    opencli_command,
    run_opencli_json,
)


URL_RE = re.compile(
    r"https?://mp\.weixin\.qq\.com/[^\s<>'\"。，、；！）\]】]+",
    re.IGNORECASE,
)


class WechatDownloadError(ArticleDownloadError):
    """Raised when a WeChat Official Account article cannot be downloaded."""


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
    if host != "mp.weixin.qq.com":
        raise ValueError("这不是微信公众号文章链接。")
    stable_query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key != "poc_token"
        ]
    )
    return urlunsplit(("https", "mp.weixin.qq.com", parts.path or "/s", stable_query, ""))


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
    content_label = "微信贴图" if article.source_type == "wechat_image_post" else "微信公众号文章"
    logger(f"已提取{content_label}：{article.author}《{article.title}》")
    return save_web_article(
        article,
        output_root,
        organize_by_author=organize_by_author,
        image_candidates=wechat_image_candidates,
        image_filter=wechat_image_filter,
        min_image_dimension=120,
        max_workers=max_workers,
        log=logger,
    )


def fetch_article(url: str, logger=None) -> WebArticle:
    logger = logger or (lambda _message: None)
    canonical = canonicalize_url(url)
    failures: list[str] = []
    try:
        article = extract_via_http(canonical)
        logger("微信正文读取方式：公开页面")
        return article
    except WechatDownloadError as exc:
        failures.append(f"公开页面：{exc}")

    if opencli_command():
        try:
            logger("普通请求受限，正在复用当前 Chrome 会话读取微信正文与原图...")
            article = extract_via_opencli(canonical)
            logger("微信正文读取方式：当前 Chrome/OpenCLI 会话")
            return article
        except (WechatDownloadError, RuntimeError, subprocess.TimeoutExpired) as exc:
            failures.append(f"当前 Chrome：{exc}")

    detail = "；".join(failures[-2:])
    raise WechatDownloadError(
        "微信未返回可下载的正文。请确认 Chrome 已启动且 OpenCLI 扩展可用后重试。"
        + (f" 详情：{detail}" if detail else "")
    )


def extract_via_http(url: str) -> WebArticle:
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        response = requests.get(url, headers=headers, timeout=(8, 45), allow_redirects=True)
    except requests.RequestException as exc:
        raise WechatDownloadError(f"读取微信公众号文章失败：{exc}") from exc
    if response.status_code >= 400:
        raise WechatDownloadError(f"读取微信公众号文章失败：HTTP {response.status_code}")
    response.encoding = response.apparent_encoding or "utf-8"
    final_url = str(response.url or url)
    if "/mp/wappoc_appmsgcaptcha" in urlsplit(final_url).path:
        raise WechatDownloadError("微信要求完成访问验证。")
    return parse_article_html(response.text, final_url)


def extract_via_opencli(url: str) -> WebArticle:
    session = f"fusion-wechat-{uuid.uuid4().hex[:10]}"
    window_guard = OpenCliWindowGuard()
    try:
        result = run_opencli_json(
            ["browser", session, "--window", "background", "open", url],
            cwd=douyin.app_base_dir(),
            timeout_seconds=60,
        )
        window_guard.observe_owned_window()
        current_url = str(result.get("url") or url) if isinstance(result, dict) else url
        payload = run_opencli_json(
            ["browser", session, "eval", build_wechat_extract_script()],
            cwd=douyin.app_base_dir(),
            timeout_seconds=90,
        )
        window_guard.observe_owned_window()
        if not isinstance(payload, dict):
            raise WechatDownloadError("Chrome 没有返回微信页面数据。")
        html = str(payload.get("html") or "")
        page_url = str(payload.get("url") or current_url)
        if not html:
            page_text = clean_text(payload.get("pageText"))
            raise WechatDownloadError(
                f"Chrome 中没有找到微信正文。{page_text[:160]}"
            )
        return parse_article_html(html, page_url)
    except subprocess.TimeoutExpired as exc:
        raise WechatDownloadError("OpenCLI 等待微信页面响应超时。") from exc
    finally:
        window_guard.observe_owned_window()
        try:
            run_opencli_json(
                ["browser", session, "close"],
                cwd=douyin.app_base_dir(),
                timeout_seconds=15,
            )
        except (RuntimeError, subprocess.TimeoutExpired):
            pass
        window_guard.close_released_window()


def build_wechat_extract_script() -> str:
    return r"""
(async () => {
  const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));
  let stableSignature = '';
  let stableTicks = 0;
  for (let attempt = 0; attempt < 40; attempt += 1) {
    const body = document.querySelector('#js_content');
    const bodyText = (body?.innerText || body?.textContent || '').trim();
    const imageDetail = new URL(location.href).searchParams.get('t') === 'pages/image_detail';
    const gallerySources = [...document.querySelectorAll(
      '#img_swiper_content .swiper_item img, .share_media_swiper_content .swiper_item img'
    )].map(image => image.currentSrc || image.src || image.getAttribute('data-src') || '')
      .filter(Boolean);
    const author = (
      document.querySelector('#js_name')?.textContent
      || document.querySelector('#js_wx_follow_nickname')?.textContent
      || ''
    ).trim();
    const ready = body && bodyText.length >= 20
      && author.length > 0
      && (!imageDetail || gallerySources.length > 0);
    const signature = `${bodyText.length}|${author}|${[...new Set(gallerySources)].length}`;
    if (ready && signature === stableSignature) {
      stableTicks += 1;
    } else {
      stableSignature = signature;
      stableTicks = 0;
    }
    if (ready && stableTicks >= 6) {
      return {
        url: location.href,
        html: document.documentElement.outerHTML,
      };
    }
    await sleep(500);
  }
  return {
    error: 'body_not_found',
    url: location.href,
    pageText: (document.body?.innerText || '').slice(0, 500),
  };
})()
""".strip()


def parse_article_html(html: str, source_url: str) -> WebArticle:
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one("#js_content")
    if body is None or len(body.get_text("", strip=True)) < 20:
        page_text = soup.get_text(" ", strip=True)
        if "环境异常" in page_text or "访问过于频繁" in page_text:
            raise WechatDownloadError("微信提示访问环境异常或请求过于频繁，请稍后重试。")
        raise WechatDownloadError("没有在页面中找到微信公众号正文。")

    canonical = canonicalize_url(source_url)
    image_detail = is_image_detail_url(canonical) or soup.select_one("#js_image_content") is not None
    if image_detail:
        body = build_image_detail_body(soup)

    title = first_text(
        soup,
        "#activity-name",
        "meta[property='og:title']",
        "title",
    )
    author = first_text(
        soup,
        "#js_name",
        "#js_wx_follow_nickname .nickNameSpan",
        "#js_wx_follow_nickname",
        "meta[name='author']",
        ".rich_media_meta_nickname",
        ".account_nickname",
    )
    if not title:
        raise WechatDownloadError("已找到正文，但文章标题为空。")
    return WebArticle(
        platform="微信贴图" if image_detail else "微信公众号",
        source_type="wechat_image_post" if image_detail else "wechat_article",
        source_url=source_url,
        canonical_url=canonical,
        content_id=wechat_content_id(canonical),
        title=title,
        author=author or "未知公众号",
        body_html=str(body),
        published=wechat_published(soup, html),
    )


def is_image_detail_url(url: str) -> bool:
    query = parse_qs(urlsplit(url).query)
    return (query.get("t") or [""])[0] == "pages/image_detail"


def build_image_detail_body(soup: BeautifulSoup) -> Tag:
    description = soup.select_one("#js_image_desc")
    images = soup.select(
        "#img_swiper_content .swiper_item img, "
        ".share_media_swiper_content .swiper_item img"
    )
    unique_images: list[Tag] = []
    seen: set[str] = set()
    for image in images:
        source = clean_text(
            image.get("data-original")
            or image.get("data-src")
            or image.get("src")
        )
        if not source or source in seen:
            continue
        seen.add(source)
        unique_images.append(image)
    if description is None or len(description.get_text("", strip=True)) < 20:
        raise WechatDownloadError("微信贴图页面缺少文字内容。")
    if not unique_images:
        raise WechatDownloadError("微信贴图页面没有找到作品图片。")

    wrapper = BeautifulSoup("<div></div>", "html.parser").div
    description_copy = BeautifulSoup(str(description), "html.parser").find()
    if description_copy is not None:
        wrapper.append(description_copy)
    for image in unique_images:
        paragraph = soup.new_tag("p")
        image_copy = BeautifulSoup(str(image), "html.parser").find("img")
        if image_copy is None:
            continue
        paragraph.append(image_copy)
        wrapper.append(paragraph)
    return wrapper


def first_text(soup: BeautifulSoup, *selectors: str) -> str:
    for selector in selectors:
        element = soup.select_one(selector)
        if element is None:
            continue
        value = element.get("content") if element.name == "meta" else element.get_text(" ", strip=True)
        value = clean_text(value)
        if value:
            return value
    return ""


def wechat_published(soup: BeautifulSoup, html: str) -> str:
    value = first_text(
        soup,
        "meta[property='article:published_time']",
        "#publish_time",
    )
    if value:
        return value
    match = re.search(r"\b(?:var\s+)?ct\s*=\s*[\"'](\d{9,12})[\"']", html)
    if match:
        try:
            return datetime.fromtimestamp(int(match.group(1))).strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            pass
    return ""


def wechat_content_id(url: str) -> str:
    parts = urlsplit(url)
    path_match = re.search(r"/s/([A-Za-z0-9_-]+)", parts.path)
    if path_match:
        return path_match.group(1)
    query = parse_qs(parts.query)
    fields = "-".join(
        str((query.get(key) or [""])[0])
        for key in ("__biz", "mid", "idx", "sn")
    ).strip("-")
    if fields:
        return hashlib.sha256(fields.encode("utf-8")).hexdigest()[:16]
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def wechat_image_filter(image: Tag) -> bool:
    classes = " ".join(str(item) for item in image.get("class") or [])
    if re.search(r"\b(?:emoji|icon|avatar|qr_code)\b", classes, re.IGNORECASE):
        return False
    try:
        declared_width = int(float(str(image.get("data-w") or image.get("width") or 0)))
    except (TypeError, ValueError):
        declared_width = 0
    alt = clean_text(image.get("alt"))
    return not (0 < declared_width < 120 and not alt)


def wechat_image_candidates(image: Tag, source_url: str) -> list[CoverCandidate]:
    values: list[tuple[str, str]] = []
    for attribute in ("data-original", "data-src", "data-backsrc", "src"):
        value = clean_text(image.get(attribute))
        if value and not value.startswith("data:"):
            values.append((value, attribute))
    srcset = clean_text(image.get("srcset"))
    if srcset:
        for item in srcset.split(","):
            value = item.strip().split()[0]
            if value:
                values.append((value, "srcset"))

    candidates: list[CoverCandidate] = []
    try:
        width = int(float(str(image.get("data-w") or image.get("width") or 0)))
    except (TypeError, ValueError):
        width = 0
    for raw, source in values:
        parts = urlsplit(raw)
        if parts.scheme not in {"http", "https"}:
            continue
        if parts.hostname and parts.hostname.endswith("qpic.cn"):
            original_path = re.sub(r"/(?:\d+|0)$", "/0", parts.path)
            query_items = parse_qsl(parts.query, keep_blank_values=True)
            clean_query = urlencode(
                [
                    (key, value)
                    for key, value in query_items
                    if key not in {"watermark", "tp", "usePicPrefetch", "wxfrom"}
                ]
            )
            clean_original = urlunsplit(
                (parts.scheme, parts.netloc, original_path, clean_query, "")
            )
            original = urlunsplit(
                (parts.scheme, parts.netloc, original_path, parts.query, "")
            )
            candidates.append(
                CoverCandidate(
                    url=clean_original,
                    source=f"wechat:{source}:original-clean",
                    width=width,
                    preference=12,
                )
            )
            candidates.append(
                CoverCandidate(
                    url=original,
                    source=f"wechat:{source}:original",
                    width=width,
                    preference=10,
                    watermark="watermark" in dict(query_items),
                )
            )
        candidates.append(
            CoverCandidate(
                url=urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, "")),
                source=f"wechat:{source}",
                width=width,
                preference=5 if source != "src" else 1,
                preview=source == "src",
            )
        )
    return candidates
