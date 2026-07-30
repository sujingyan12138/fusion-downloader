from __future__ import annotations

import re
import unicodedata
from pathlib import Path


INVALID_WINDOWS_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def safe_author_folder_name(author: object, max_length: int = 80) -> str:
    text = unicodedata.normalize("NFKC", str(author or "")).strip()
    text = INVALID_WINDOWS_CHARS_RE.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = "未知作者"
    if text.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        text = f"_{text}"
    text = text[:max_length].rstrip(" .")
    return text or "未知作者"


def author_output_root(
    output_root: str | Path,
    author: object,
    organize_by_author: bool,
) -> Path:
    root = Path(output_root)
    if organize_by_author:
        root = root / safe_author_folder_name(author)
    root.mkdir(parents=True, exist_ok=True)
    return root
