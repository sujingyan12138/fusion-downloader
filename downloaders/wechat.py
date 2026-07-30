from __future__ import annotations

from datetime import datetime
import hashlib
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag
import requests

from downloaders.article_common import (
    ArticleDownloadError,
    DEFAULT_USER_AGENT,
    WebArticle,
    clean_text,
    save_web_article,
)
from downloaders.covers import CoverCandidate


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
    return urlunsplit(("https", "mp.weixin.qq.com", parts.path or "/s", parts.query, ""))


def download_article(
    url: str,
    output_root: str | Path,
    *,
    max_workers: int = 4,
    organize_by_author: bool = True,
    log=None,
) -> dict:
    logger = log or (lambda _message: None)
    article = fetch_article(url)
    logger(f"已提取微信公众号文章：{article.author}《{article.title}》")
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


def fetch_article(url: str) -> WebArticle:
    canonical = canonicalize_url(url)
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        response = requests.get(canonical, headers=headers, timeout=(8, 45), allow_redirects=True)
    except requests.RequestException as exc:
        raise WechatDownloadError(f"读取微信公众号文章失败：{exc}") from exc
    if response.status_code >= 400:
        raise WechatDownloadError(f"读取微信公众号文章失败：HTTP {response.status_code}")
    response.encoding = response.apparent_encoding or "utf-8"
    return parse_article_html(response.text, str(response.url or canonical))


def parse_article_html(html: str, source_url: str) -> WebArticle:
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one("#js_content")
    if body is None or len(body.get_text("", strip=True)) < 20:
        page_text = soup.get_text(" ", strip=True)
        if "环境异常" in page_text or "访问过于频繁" in page_text:
            raise WechatDownloadError("微信提示访问环境异常或请求过于频繁，请稍后重试。")
        raise WechatDownloadError("没有在页面中找到微信公众号正文。")

    title = first_text(
        soup,
        "#activity-name",
        "meta[property='og:title']",
        "title",
    )
    author = first_text(
        soup,
        "#js_name",
        "meta[name='author']",
        ".rich_media_meta_nickname",
    )
    if not title:
        raise WechatDownloadError("已找到正文，但文章标题为空。")
    canonical = canonicalize_url(source_url)
    return WebArticle(
        platform="微信公众号",
        source_type="wechat_article",
        source_url=source_url,
        canonical_url=canonical,
        content_id=wechat_content_id(canonical),
        title=title,
        author=author or "未知公众号",
        body_html=str(body),
        published=wechat_published(soup, html),
    )


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
            original = urlunsplit((parts.scheme, parts.netloc, original_path, parts.query, ""))
            candidates.append(
                CoverCandidate(
                    url=original,
                    source=f"wechat:{source}:original",
                    width=width,
                    preference=10,
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
