from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import unescape
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


@dataclass
class WechatImageRecord:
    cdn_url: str
    width: int
    height: int
    original_url: str = ""
    original_size: int = 0
    watermark_url: str = ""


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
    html, final_url = fetch_wechat_page_html(url)
    return parse_article_html(html, final_url)


def fetch_wechat_page_html(url: str) -> tuple[str, str]:
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
    return response.text, final_url


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
        run_opencli_json(
            [
                "browser",
                session,
                "wait",
                "selector",
                "#js_content",
                "--timeout",
                "45000",
            ],
            cwd=douyin.app_base_dir(),
            timeout_seconds=55,
        )
        payload = run_opencli_json(
            ["browser", session, "eval", build_wechat_extract_script()],
            cwd=douyin.app_base_dir(),
            timeout_seconds=30,
        )
        window_guard.observe_owned_window()
        if not isinstance(payload, dict):
            raise WechatDownloadError("Chrome 没有返回微信页面数据。")
        html = browser_payload_html(payload)
        page_url = str(payload.get("url") or current_url)
        if not html:
            page_text = clean_text(payload.get("pageText"))
            raise WechatDownloadError(
                f"Chrome 中没有找到微信正文。{page_text[:160]}"
            )
        image_data_html = ""
        try:
            image_data_html, _final_url = fetch_wechat_page_html(page_url)
        except WechatDownloadError:
            pass
        return parse_article_html(
            html,
            page_url,
            image_data_html=image_data_html,
        )
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
(() => {
  const body = document.querySelector('#js_content');
  const title = (
    document.querySelector('#activity-name')?.textContent
    || document.querySelector('#js_image_content h1')?.textContent
    || document.querySelector('meta[property="og:title"]')?.content
    || document.title
    || ''
  ).trim();
  const author = (
    document.querySelector('#js_name')?.textContent
    || document.querySelector('#js_wx_follow_nickname')?.textContent
    || ''
  ).trim();
  const galleryHtml = [...document.querySelectorAll(
    '#img_swiper_content .swiper_item img, .share_media_swiper_content .swiper_item img'
  )].map(image => image.outerHTML);
  return {
    url: location.href,
    title,
    author,
    bodyHtml: body?.outerHTML || '',
    galleryHtml,
    pageText: (document.body?.innerText || '').slice(0, 500),
  };
})()
""".strip()


def browser_payload_html(payload: dict) -> str:
    body_html = str(payload.get("bodyHtml") or "")
    if not body_html:
        return ""
    soup = BeautifulSoup("<html><head></head><body></body></html>", "html.parser")
    title = soup.new_tag("title")
    title.string = clean_text(payload.get("title")) or "微信公众平台"
    soup.head.append(title)
    author = clean_text(payload.get("author"))
    if author:
        author_element = soup.new_tag("div", id="js_name")
        author_element.string = author
        soup.body.append(author_element)
    body_fragment = BeautifulSoup(body_html, "html.parser").find()
    if body_fragment is not None:
        soup.body.append(body_fragment)
    gallery_html = payload.get("galleryHtml")
    if isinstance(gallery_html, list) and gallery_html:
        gallery = soup.new_tag("div", id="img_swiper_content")
        for value in gallery_html:
            image = BeautifulSoup(str(value), "html.parser").find("img")
            if image is None:
                continue
            item = soup.new_tag("div")
            item["class"] = ["swiper_item"]
            item.append(image)
            gallery.append(item)
        soup.body.append(gallery)
    return str(soup)


def parse_article_html(
    html: str,
    source_url: str,
    *,
    image_data_html: str = "",
) -> WebArticle:
    soup = BeautifulSoup(html, "html.parser")
    record_html = image_data_html or html
    image_records = extract_image_detail_records(record_html)
    body = soup.select_one("#js_content")
    if (body is None or len(body.get_text("", strip=True)) < 20) and image_records:
        hydrate_image_detail_shell(soup, record_html)
        body = soup.select_one("#js_content")
    if body is None or len(body.get_text("", strip=True)) < 20:
        page_text = soup.get_text(" ", strip=True)
        if "环境异常" in page_text or "访问过于频繁" in page_text:
            raise WechatDownloadError("微信提示访问环境异常或请求过于频繁，请稍后重试。")
        raise WechatDownloadError("没有在页面中找到微信公众号正文。")

    canonical = canonicalize_url(source_url)
    image_detail = (
        is_image_detail_url(canonical)
        or soup.select_one("#js_image_content") is not None
        or bool(image_records)
    )
    if image_detail:
        body = build_image_detail_body(soup, image_records=image_records)

    title = first_text(
        soup,
        "#activity-name",
        "#js_image_content h1",
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
    ) or wechat_script_author(record_html)
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


def hydrate_image_detail_shell(soup: BeautifulSoup, html: str) -> None:
    title = decode_wechat_js_text(
        first_text(
            soup,
            "meta[property='og:title']",
            "meta[property='twitter:title']",
            "title",
        )
    )
    description = decode_wechat_js_text(
        first_text(
            soup,
            "meta[name='description']",
            "meta[property='og:description']",
            "meta[property='twitter:description']",
        )
    )
    if not title or len(clean_text(BeautifulSoup(description, "html.parser").get_text(" "))) < 20:
        return
    container = soup.body or soup
    body = soup.new_tag("div", id="js_content")
    image_content = soup.new_tag("div", id="js_image_content")
    heading = soup.new_tag("h1")
    heading.string = title
    image_content.append(heading)
    paragraph = soup.new_tag("p", id="js_image_desc")
    paragraph.string = clean_text(BeautifulSoup(description, "html.parser").get_text(" "))
    image_content.append(paragraph)
    body.append(image_content)
    container.append(body)
    author = wechat_script_author(html)
    if author and soup.select_one("#js_name") is None:
        author_element = soup.new_tag("div", id="js_name")
        author_element.string = author
        container.append(author_element)


def wechat_script_author(html: str) -> str:
    for pattern in (
        r"\bnick_name\s*:\s*'(?P<value>[^']+)'",
        r'\bnick_name\s*:\s*"(?P<value>[^"]+)"',
        r"\bnickname\s*:\s*'(?P<value>[^']+)'",
        r'\bnickname\s*:\s*"(?P<value>[^"]+)"',
    ):
        for match in re.finditer(pattern, html, re.IGNORECASE):
            value = clean_text(decode_wechat_js_text(match.group("value")))
            if (
                value
                and not value.startswith(("gh_", "data-"))
                and "nickname" not in value.lower()
            ):
                return value
    return ""


def decode_wechat_js_text(value: str) -> str:
    decoded = str(value or "").replace(r"\/", "/")
    decoded = re.sub(
        r"\\x(?P<hex>[0-9a-fA-F]{2})",
        lambda match: chr(int(match.group("hex"), 16)),
        decoded,
    )
    decoded = re.sub(
        r"\\u(?P<hex>[0-9a-fA-F]{4})",
        lambda match: chr(int(match.group("hex"), 16)),
        decoded,
    )
    return unescape(decoded)


def is_image_detail_url(url: str) -> bool:
    query = parse_qs(urlsplit(url).query)
    return (query.get("t") or [""])[0] == "pages/image_detail"


def extract_image_detail_records(html: str) -> list[WechatImageRecord]:
    records: list[WechatImageRecord] = []
    by_source: dict[tuple[str, str], int] = {}
    object_start = re.compile(
        r"\{\s*cdn_url\s*:\s*'(?P<url>https?:[^']*qpic\.cn[^']*)'\s*,",
        re.IGNORECASE,
    )
    for match in object_start.finditer(html):
        block = balanced_js_object(html, match.start())
        if not block or "show_watermark" not in block:
            continue
        width_match = re.search(
            r"\bwidth\s*:\s*'?(?P<width>\d+)'?\s*(?:\*\s*1)?\s*,"
            r"\s*height\s*:\s*'?(?P<height>\d+)'?",
            block,
            re.IGNORECASE,
        )
        if width_match is None:
            continue
        cdn_url = decode_wechat_js_url(match.group("url"))
        parts = urlsplit(cdn_url)
        if not parts.hostname or not parts.hostname.endswith("qpic.cn"):
            continue

        original_url = ""
        original_size = 0
        original_match = re.search(
            r"\boriginal_info\s*:\s*\{(?P<body>.*?)\}",
            block,
            re.IGNORECASE | re.DOTALL,
        )
        if original_match is not None:
            original_body = original_match.group("body")
            show_original = re.search(
                r"\bshow_original\s*:\s*(?:'true'\s*===\s*'true'|true)",
                original_body,
                re.IGNORECASE,
            )
            original_url_match = re.search(
                r"\bcdn_url\s*:\s*'(?P<url>[^']+)'",
                original_body,
                re.IGNORECASE,
            )
            original_size_match = re.search(
                r"\bfile_size\s*:\s*'?(?P<size>\d+)'?",
                original_body,
                re.IGNORECASE,
            )
            if show_original is not None and original_url_match is not None:
                original_url = decode_wechat_js_url(original_url_match.group("url"))
                if original_size_match is not None:
                    original_size = int(original_size_match.group("size"))

        watermark_url = ""
        watermark_match = re.search(
            r"\bwatermark_info\s*:\s*\{(?P<body>.*?)\}",
            block,
            re.IGNORECASE | re.DOTALL,
        )
        if watermark_match is not None:
            watermark_url_match = re.search(
                r"\bcdn_url\s*:\s*'(?P<url>[^']+)'",
                watermark_match.group("body"),
                re.IGNORECASE,
            )
            if watermark_url_match is not None:
                watermark_url = decode_wechat_js_url(watermark_url_match.group("url"))

        record = WechatImageRecord(
            cdn_url=cdn_url,
            width=int(width_match.group("width")),
            height=int(width_match.group("height")),
            original_url=original_url,
            original_size=original_size,
            watermark_url=watermark_url,
        )
        key = ((parts.hostname or "").lower(), parts.path)
        existing_index = by_source.get(key)
        if existing_index is None:
            by_source[key] = len(records)
            records.append(record)
            continue
        existing = records[existing_index]
        if (
            bool(record.original_url),
            record.original_size,
            bool(record.watermark_url),
        ) > (
            bool(existing.original_url),
            existing.original_size,
            bool(existing.watermark_url),
        ):
            records[existing_index] = record
    return records


def balanced_js_object(text: str, start: int) -> str:
    depth = 0
    quote = ""
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            continue
        if character in {"'", '"'}:
            quote = character
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""


def decode_wechat_js_url(value: str) -> str:
    return clean_text(decode_wechat_js_text(value))


def build_image_detail_body(
    soup: BeautifulSoup,
    *,
    image_records: list[WechatImageRecord] | None = None,
) -> Tag:
    description = soup.select_one("#js_image_desc")
    unique_images: list[Tag] = []
    if image_records:
        for record in image_records:
            image = soup.new_tag("img")
            image["data-src"] = record.cdn_url
            image["data-w"] = str(record.width)
            image["width"] = str(record.width)
            image["height"] = str(record.height)
            if record.original_url:
                image["data-original"] = record.original_url
                image["data-original-size"] = str(record.original_size)
            if record.watermark_url:
                image["data-watermark-src"] = record.watermark_url
            unique_images.append(image)
    else:
        images = soup.select(
            "#img_swiper_content .swiper_item img, "
            ".share_media_swiper_content .swiper_item img"
        )
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
    for attribute in (
        "data-original",
        "data-src",
        "data-backsrc",
        "data-watermark-src",
        "src",
    ):
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
    try:
        height = int(float(str(image.get("height") or 0)))
    except (TypeError, ValueError):
        height = 0
    for raw, source in values:
        parts = urlsplit(raw)
        if parts.scheme not in {"http", "https"}:
            continue
        watermark_source = source == "data-watermark-src"
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
                    height=height,
                    preference=12,
                    watermark=watermark_source,
                )
            )
            candidates.append(
                CoverCandidate(
                    url=original,
                    source=f"wechat:{source}:original",
                    width=width,
                    height=height,
                    preference=10,
                    watermark=watermark_source or "watermark" in dict(query_items),
                )
            )
        candidates.append(
            CoverCandidate(
                url=urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, "")),
                source=f"wechat:{source}",
                width=width,
                height=height,
                preference=5 if source != "src" else 1,
                preview=source == "src",
                watermark=watermark_source or "watermark" in dict(parse_qsl(parts.query)),
            )
        )
    return candidates
