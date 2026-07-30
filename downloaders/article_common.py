from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
import json
import re
from typing import Callable
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup, Tag
from markdownify import markdownify
from PIL import Image, UnidentifiedImageError
import requests

from downloaders.covers import CoverCandidate, CoverDownloadError, CoverProbe, probe_cover
from downloaders.paths import author_output_root


LogFn = Callable[[str], None]
ImageCandidatesFn = Callable[[Tag, str], list[CoverCandidate]]
ImageFilterFn = Callable[[Tag], bool]
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)
INVALID_WINDOWS_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ArticleDownloadError(RuntimeError):
    """Raised when a web article cannot be converted into a complete local artifact."""


@dataclass
class WebArticle:
    platform: str
    source_type: str
    source_url: str
    canonical_url: str
    content_id: str
    title: str
    author: str
    body_html: str
    published: str = ""
    modified: str = ""


def clean_text(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"[\u200b\u200c\u200d\u2060\ufeff]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def safe_filename(value: object, max_length: int = 150) -> str:
    text = clean_text(value)
    text = INVALID_WINDOWS_CHARS_RE.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    text = text[:max_length].rstrip(" .")
    return text or "未命名"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 10000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise ArticleDownloadError(f"无法为文件生成不重复名称：{path}")


def yaml_string(value: object) -> str:
    return json.dumps(str(value or ""), ensure_ascii=False)


def save_web_article(
    article: WebArticle,
    output_root: str | Path,
    *,
    organize_by_author: bool,
    image_candidates: ImageCandidatesFn,
    image_filter: ImageFilterFn | None = None,
    min_image_dimension: int = 0,
    max_workers: int = 4,
    log: LogFn | None = None,
) -> dict:
    logger = log or (lambda _message: None)
    if not clean_text(article.title):
        raise ArticleDownloadError("文章标题为空。")
    if len(BeautifulSoup(article.body_html, "html.parser").get_text("", strip=True)) < 20:
        raise ArticleDownloadError("文章正文为空或过短。")

    root = author_output_root(output_root, article.author, organize_by_author)
    file_prefix = safe_filename(
        f"{article.platform}_{article.author}_{article.title}_{article.content_id}",
        145,
    )
    normalized = normalize_article_body(
        article,
        root,
        file_prefix=file_prefix,
        image_candidates=image_candidates,
        image_filter=image_filter,
        min_image_dimension=min_image_dimension,
        max_workers=max_workers,
        logger=logger,
    )
    body_markdown = str(normalized["markdown"])
    content = render_article_markdown(
        article,
        body_markdown,
        localized_images=len(normalized["images"]),
    )

    desired = root / f"{file_prefix}_正文.md"
    target = desired
    status = "ok"
    if desired.is_file():
        try:
            if desired.read_text(encoding="utf-8") == content:
                status = "skipped"
            else:
                target = unique_path(desired)
        except (OSError, UnicodeError):
            target = unique_path(desired)
    if status == "ok":
        try:
            target.write_text(content, encoding="utf-8", newline="\n")
        except OSError as exc:
            raise ArticleDownloadError(f"保存 Markdown 失败：{exc}") from exc
        logger(f"正文 Markdown：{target}")
    else:
        logger(f"正文 Markdown 已存在，跳过重复保存：{target}")

    return {
        "platform": article.platform,
        "kind": "article",
        "content_id": article.content_id,
        "title": article.title,
        "author": article.author,
        "source_url": article.canonical_url,
        "webpage_url": article.canonical_url,
        "output_dir": str(root),
        "output_path": str(target),
        "filename": target.name,
        "markdown": {
            "status": status,
            "path": str(target),
            "filename": target.name,
            "characters": len(body_markdown),
        },
        "article_images": normalized["images"],
        "article_image_failures": normalized["failures"],
    }


def normalize_article_body(
    article: WebArticle,
    output_root: Path,
    *,
    file_prefix: str,
    image_candidates: ImageCandidatesFn,
    image_filter: ImageFilterFn | None,
    min_image_dimension: int,
    max_workers: int,
    logger: LogFn,
) -> dict:
    soup = BeautifulSoup(article.body_html, "html.parser")
    for unwanted in soup.find_all(["script", "style", "noscript", "button", "form"]):
        unwanted.decompose()
    for unwanted in soup.select(
        ".Catalog, .RichContent-actions, .ContentItem-actions, .Reward, "
        ".qr_code_pc_outer, .js_pc_qr_code, svg"
    ):
        unwanted.decompose()
    for link in soup.find_all("a", href=True):
        link["href"] = urljoin(article.canonical_url, str(link["href"]))

    groups: list[dict] = []
    by_candidate_key: dict[tuple[str, ...], dict] = {}
    for image in list(soup.find_all("img")):
        if image_filter is not None and not image_filter(image):
            image.decompose()
            continue
        candidates = dedupe_image_candidates(image_candidates(image, article.canonical_url))
        if not candidates:
            image.decompose()
            continue
        key = tuple(candidate.url for candidate in candidates)
        group = by_candidate_key.get(key)
        if group is None:
            group = {
                "index": len(groups) + 1,
                "images": [],
                "candidates": candidates,
                "remote": candidates[0].url,
            }
            groups.append(group)
            by_candidate_key[key] = group
        group["images"].append(image)

    reports: list[dict] = []
    failures: list[dict] = []

    def download_one(group: dict) -> dict:
        index = int(group["index"])
        best = choose_best_article_image(
            group["candidates"],
            article.canonical_url,
        )
        if min_image_dimension and max(best.width, best.height) < min_image_dimension:
            return {
                "index": index,
                "status": "ignored",
                "width": best.width,
                "height": best.height,
            }
        assets_dir = output_root / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        stem = safe_filename(
            f"{article.platform}_{article.content_id}_正文图片_{index:03d}_{best.width}x{best.height}",
            110,
        )
        target = assets_dir / f"{stem}.{best.extension}"
        status = "ok"
        if target.is_file() and is_decodable_image_size(target, best.width, best.height):
            status = "skipped"
        else:
            if target.exists():
                target = unique_path(target)
            try:
                target.write_bytes(best.content)
            except OSError as exc:
                raise ArticleDownloadError(f"保存正文图片失败：{exc}") from exc
        return {
            "index": index,
            "status": status,
            "path": str(target),
            "filename": target.name,
            "relative_path": f"./assets/{quote(target.name, safe='._-')}",
            "width": best.width,
            "height": best.height,
            "bytes": len(best.content),
            "source": best.candidate.source,
            "url": best.candidate.url,
        }

    worker_count = max(1, min(len(groups), max(1, min(max_workers, 4))))
    if groups:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_groups = {
                executor.submit(download_one, group): group
                for group in groups
            }
            for future in as_completed(future_groups):
                group = future_groups[future]
                try:
                    report = future.result()
                    if report["status"] == "ignored":
                        for image in group["images"]:
                            image.decompose()
                        logger(
                            f"正文图片 {report['index']:03d}：忽略装饰小图 "
                            f"{report['width']}x{report['height']}"
                        )
                        continue
                    reports.append(report)
                    for image in group["images"]:
                        image["src"] = report["relative_path"]
                        if not clean_text(image.get("alt")):
                            image["alt"] = f"正文配图 {report['index']}"
                        clean_image_attributes(image)
                except Exception as exc:  # noqa: BLE001 - retain a working remote image.
                    failure = {"index": group["index"], "url": group["remote"], "error": str(exc)}
                    failures.append(failure)
                    for image in group["images"]:
                        image["src"] = group["remote"]
                        clean_image_attributes(image)

    reports.sort(key=lambda item: item["index"])
    failures.sort(key=lambda item: item["index"])
    for item in reports:
        action = "复用" if item["status"] == "skipped" else "保存"
        logger(
            f"正文图片 {item['index']:03d}：{action} "
            f"{item['width']}x{item['height']} -> {item['relative_path']}"
        )
    for failure in failures:
        logger(f"正文图片 {failure['index']:03d} 下载失败，保留远程引用：{failure['error']}")

    markdown = markdownify(str(soup), heading_style="ATX", bullets="-")
    markdown = markdown.replace("\u200b", "").replace("\xa0", " ")
    markdown = re.sub(r"[ \t]+\n", "\n", markdown)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
    if not markdown:
        raise ArticleDownloadError("正文转换为 Markdown 后为空。")
    logger(f"正文图片完成：本地 {len(reports)}/{len(groups)}，失败 {len(failures)}")
    return {"markdown": markdown, "images": reports, "failures": failures}


def dedupe_image_candidates(candidates: list[CoverCandidate]) -> list[CoverCandidate]:
    result: list[CoverCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        url = str(candidate.url or "").strip()
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        candidate.url = url
        seen.add(url)
        result.append(candidate)
    return result


def choose_best_article_image(
    candidates: list[CoverCandidate],
    referer: str,
) -> CoverProbe:
    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
    best: CoverProbe | None = None
    failures: list[str] = []
    try:
        for candidate in candidates[:8]:
            try:
                result = probe_cover(session, candidate, referer)
            except CoverDownloadError as exc:
                failures.append(f"{candidate.source}: {exc}")
                continue
            if best is None or result.score > best.score:
                best = result
    finally:
        session.close()
    if best is None:
        raise ArticleDownloadError("所有正文图片候选都不可用。" + "；".join(failures[:3]))
    return best


def clean_image_attributes(image: Tag) -> None:
    for attribute in (
        "data-original",
        "data-actualsrc",
        "data-src",
        "data-backsrc",
        "data-type",
        "data-w",
        "data-ratio",
        "srcset",
        "style",
        "class",
        "width",
        "height",
    ):
        image.attrs.pop(attribute, None)


def is_decodable_image_size(path: Path, width: int, height: int) -> bool:
    try:
        with Image.open(path) as image:
            image.load()
            return image.size == (width, height)
    except (UnidentifiedImageError, OSError):
        return False


def render_article_markdown(
    article: WebArticle,
    body_markdown: str,
    *,
    localized_images: int,
) -> str:
    lines = [
        "---",
        f"title: {yaml_string(article.title)}",
        f"author: {yaml_string(article.author)}",
        f"source: {yaml_string(article.canonical_url)}",
        f"source_type: {article.source_type}",
        f"content_id: {yaml_string(article.content_id)}",
        f"published: {yaml_string(article.published)}",
        f"modified: {yaml_string(article.modified)}",
        f"localized_images: {localized_images}",
        "---",
        "",
        f"# {article.title}",
        "",
        f"> 作者：{article.author}  ",
        f"> 来源：[{article.platform}原文]({article.canonical_url})",
    ]
    if article.published:
        lines.append(f"> 发布：{article.published}")
    if article.modified and article.modified != article.published:
        lines.append(f"> 修改：{article.modified}")
    lines.extend(["", body_markdown.strip(), ""])
    return "\n".join(lines)
