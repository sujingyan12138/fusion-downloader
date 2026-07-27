from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import re
from typing import Callable, Iterable

from PIL import Image, UnidentifiedImageError
import requests


LogFn = Callable[[str], None]
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


class CoverDownloadError(RuntimeError):
    """Raised when no valid cover candidate can be downloaded."""


@dataclass
class CoverCandidate:
    url: str
    source: str
    width: int = 0
    height: int = 0
    preference: int = 0
    preview: bool = False
    watermark: bool = False

    @property
    def declared_area(self) -> int:
        return self.width * self.height


@dataclass
class CoverProbe:
    candidate: CoverCandidate
    content: bytes
    extension: str
    image_format: str
    width: int
    height: int

    @property
    def score(self) -> tuple[int, int, int, int, int, int]:
        return (
            0 if self.candidate.watermark else 1,
            0 if self.candidate.preview else 1,
            self.width * self.height,
            len(self.content),
            self.candidate.preference,
            self.candidate.declared_area,
        )


def info_cover_candidates(
    info: dict,
    *,
    source_prefix: str,
    extra: Iterable[CoverCandidate] = (),
) -> list[CoverCandidate]:
    candidates = list(extra)
    thumbnails = info.get("thumbnails")
    if isinstance(thumbnails, list):
        for index, item in enumerate(thumbnails):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "")
            if not url.startswith(("http://", "https://")):
                continue
            candidates.append(
                CoverCandidate(
                    url=url,
                    source=f"{source_prefix}:thumbnails[{index}]",
                    width=_to_int(item.get("width")),
                    height=_to_int(item.get("height")),
                    preference=_to_int(item.get("preference")),
                )
            )
    thumbnail = str(info.get("thumbnail") or "")
    if thumbnail.startswith(("http://", "https://")):
        candidates.append(
            CoverCandidate(
                url=thumbnail,
                source=f"{source_prefix}:thumbnail",
                width=_to_int(info.get("thumbnail_width")),
                height=_to_int(info.get("thumbnail_height")),
                preference=1,
            )
        )
    return dedupe_candidates(candidates)


def dedupe_candidates(candidates: Iterable[CoverCandidate]) -> list[CoverCandidate]:
    result: list[CoverCandidate] = []
    by_url: dict[str, CoverCandidate] = {}
    for candidate in candidates:
        url = str(candidate.url or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        existing = by_url.get(url)
        if existing is None:
            candidate.url = url
            by_url[url] = candidate
            result.append(candidate)
            continue
        if (candidate.declared_area, candidate.preference) > (
            existing.declared_area,
            existing.preference,
        ):
            existing.width = candidate.width
            existing.height = candidate.height
            existing.preference = candidate.preference
            existing.source = candidate.source
            existing.preview = candidate.preview
            existing.watermark = candidate.watermark
    return result


def download_best_cover(
    candidates: Iterable[CoverCandidate],
    output_root: str | Path,
    stem: str,
    *,
    referer: str = "",
    log: LogFn | None = None,
    max_candidates: int = 16,
) -> dict:
    logger = log or (lambda _message: None)
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    existing = existing_cover_report(output_root, stem)
    if existing:
        logger(f"封面已存在，跳过重复下载：{existing['path']}")
        return existing

    ordered = sorted(
        dedupe_candidates(candidates),
        key=lambda item: (
            0 if item.watermark else 1,
            0 if item.preview else 1,
            item.declared_area,
            item.preference,
        ),
        reverse=True,
    )
    if not ordered:
        raise CoverDownloadError("作品没有提供可用的封面候选。")

    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
    best: CoverProbe | None = None
    failures: list[str] = []
    try:
        for candidate in ordered[: max(1, max_candidates)]:
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
        detail = "；".join(failures[:4])
        raise CoverDownloadError(f"所有封面候选都不可用。{detail}")
    return save_cover_bytes(
        output_root,
        stem,
        best.content,
        width=best.width,
        height=best.height,
        extension=best.extension,
        image_format=best.image_format,
        source=best.candidate.source,
        url=best.candidate.url,
        log=logger,
    )


def probe_cover(
    session: requests.Session,
    candidate: CoverCandidate,
    referer: str,
) -> CoverProbe:
    headers = {
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "User-Agent": DEFAULT_USER_AGENT,
    }
    if referer:
        headers["Referer"] = referer
    try:
        response = session.get(
            candidate.url,
            headers=headers,
            timeout=(5, 25),
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise CoverDownloadError(f"请求失败：{exc}") from exc
    if response.status_code >= 400:
        raise CoverDownloadError(f"HTTP {response.status_code}")
    content = response.content
    if len(content) < 32:
        raise CoverDownloadError("图片响应内容过短。")
    try:
        with Image.open(BytesIO(content)) as image:
            image.load()
            width, height = image.size
            image_format = str(image.format or "").lower()
    except (UnidentifiedImageError, OSError) as exc:
        raise CoverDownloadError("响应不是可解码图片。") from exc
    if width <= 0 or height <= 0:
        raise CoverDownloadError("图片尺寸无效。")
    return CoverProbe(
        candidate=candidate,
        content=content,
        extension=extension_for_format(image_format, response.headers.get("Content-Type", "")),
        image_format=image_format or "unknown",
        width=width,
        height=height,
    )


def save_cover_bytes(
    output_root: str | Path,
    stem: str,
    content: bytes,
    *,
    width: int,
    height: int,
    extension: str,
    image_format: str,
    source: str,
    url: str,
    log: LogFn | None = None,
) -> dict:
    logger = log or (lambda _message: None)
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    existing = existing_cover_report(output_root, stem)
    if existing:
        logger(f"封面已存在，跳过重复下载：{existing['path']}")
        return existing
    safe_extension = re.sub(r"[^a-z0-9]", "", extension.lower()) or "jpg"
    target = unique_path(output_root / f"{stem}_封面_{width}x{height}.{safe_extension}")
    target.write_bytes(content)
    report = {
        "status": "ok",
        "path": str(target),
        "filename": target.name,
        "width": width,
        "height": height,
        "resolution": f"{width}x{height}",
        "bytes": len(content),
        "format": image_format,
        "source": source,
        "url": url,
    }
    logger(f"最高质量封面已保存：{width}x{height}，{target}")
    return report


def existing_cover_report(output_root: str | Path, stem: str) -> dict | None:
    root = Path(output_root)
    if not root.is_dir():
        return None
    prefix = f"{stem}_封面_"
    matches: list[tuple[int, int, Path, str]] = []
    for path in root.iterdir():
        if not path.is_file() or not path.name.startswith(prefix):
            continue
        try:
            with Image.open(path) as image:
                image.load()
                width, height = image.size
                image_format = str(image.format or path.suffix.lstrip(".")).lower()
        except (UnidentifiedImageError, OSError):
            continue
        matches.append((width * height, path.stat().st_size, path, image_format))
    if not matches:
        return None
    _area, size, path, image_format = max(matches, key=lambda item: (item[0], item[1]))
    with Image.open(path) as image:
        width, height = image.size
    return {
        "status": "skipped",
        "path": str(path),
        "filename": path.name,
        "width": width,
        "height": height,
        "resolution": f"{width}x{height}",
        "bytes": size,
        "format": image_format,
        "source": "existing",
        "url": "",
    }


def extension_for_format(image_format: str, content_type: str = "") -> str:
    normalized = image_format.lower()
    mapping = {
        "jpeg": "jpg",
        "jpg": "jpg",
        "png": "png",
        "webp": "webp",
        "avif": "avif",
        "gif": "gif",
        "bmp": "bmp",
    }
    if normalized in mapping:
        return mapping[normalized]
    subtype = content_type.split(";", 1)[0].partition("/")[2].lower()
    return mapping.get(subtype, re.sub(r"[^a-z0-9]", "", subtype) or "jpg")


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 10_000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise CoverDownloadError(f"无法为封面生成唯一文件名：{path.name}")


def _to_int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0
