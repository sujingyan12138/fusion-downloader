from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import requests

from app_version import APP_VERSION
from services.network_policy import bypass_unreachable_loopback_proxy


GITHUB_OWNER = "sujingyan12138"
GITHUB_REPOSITORY = "fusion-downloader"
LATEST_RELEASE_API = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases/latest"
)
RELEASES_PAGE = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases"
SUPPORTED_ASSET_NAMES = (
    "Fusion.Downloader.exe",
    "Fusion Downloader.exe",
    "融合下载器.exe",
)
GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": f"Fusion-Downloader/{APP_VERSION}",
}
ProgressFn = Callable[[int, int], None]


class UpdateError(RuntimeError):
    """Raised when an update cannot be checked, verified, or installed safely."""


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    download_url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class UpdateInfo:
    current_version: str
    latest_version: str
    title: str
    notes: str
    release_url: str
    published_at: str
    asset: ReleaseAsset | None

    @property
    def update_available(self) -> bool:
        return compare_versions(self.latest_version, self.current_version) > 0


def normalized_version(value: str) -> str:
    text = str(value or "").strip()
    match = re.search(r"\d+(?:\.\d+){0,3}", text)
    if not match:
        raise UpdateError(f"无法识别版本号：{value or '空'}")
    return match.group(0)


def version_parts(value: str) -> tuple[int, ...]:
    parts = tuple(int(part) for part in normalized_version(value).split("."))
    return parts + (0,) * (4 - len(parts))


def compare_versions(left: str, right: str) -> int:
    left_parts = version_parts(left)
    right_parts = version_parts(right)
    return (left_parts > right_parts) - (left_parts < right_parts)


def _asset_sha256(asset: dict) -> str:
    digest = str(asset.get("digest") or "").strip().lower()
    if digest.startswith("sha256:"):
        value = digest.removeprefix("sha256:")
        if re.fullmatch(r"[0-9a-f]{64}", value):
            return value
    return ""


def select_release_asset(assets: object) -> ReleaseAsset | None:
    if not isinstance(assets, list):
        return None
    candidates = [asset for asset in assets if isinstance(asset, dict)]
    by_name = {str(asset.get("name") or ""): asset for asset in candidates}
    selected = next((by_name[name] for name in SUPPORTED_ASSET_NAMES if name in by_name), None)
    if selected is None:
        selected = next(
            (
                asset
                for asset in candidates
                if str(asset.get("name") or "").lower().endswith(".exe")
                and "downloader" in str(asset.get("name") or "").lower()
            ),
            None,
        )
    if selected is None:
        return None
    name = str(selected.get("name") or "")
    download_url = str(selected.get("browser_download_url") or "")
    size = int(selected.get("size") or 0)
    sha256 = _asset_sha256(selected)
    if not name or not download_url.startswith("https://github.com/") or size <= 0:
        return None
    return ReleaseAsset(name=name, download_url=download_url, size=size, sha256=sha256)


def check_for_update(current_version: str = APP_VERSION) -> UpdateInfo:
    session = requests.Session()
    session.headers.update(GITHUB_HEADERS)
    try:
        with bypass_unreachable_loopback_proxy(probe_url=LATEST_RELEASE_API):
            response = session.get(LATEST_RELEASE_API, timeout=(8, 25))
    except requests.RequestException as exc:
        raise UpdateError(f"无法连接 GitHub 检查更新：{exc}") from exc
    if response.status_code == 404:
        raise UpdateError("GitHub 仓库还没有可用的正式 Release。")
    if response.status_code >= 400:
        raise UpdateError(f"GitHub 更新接口返回 HTTP {response.status_code}。")
    try:
        release = response.json()
    except requests.JSONDecodeError as exc:
        raise UpdateError("GitHub 更新接口没有返回有效 JSON。") from exc
    if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
        raise UpdateError("GitHub 没有返回可用的正式版本。")
    latest_version = normalized_version(str(release.get("tag_name") or release.get("name") or ""))
    return UpdateInfo(
        current_version=normalized_version(current_version),
        latest_version=latest_version,
        title=str(release.get("name") or f"v{latest_version}"),
        notes=str(release.get("body") or "").strip(),
        release_url=str(release.get("html_url") or RELEASES_PAGE),
        published_at=str(release.get("published_at") or ""),
        asset=select_release_asset(release.get("assets")),
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_update(
    info: UpdateInfo,
    app_dir: Path,
    progress: ProgressFn | None = None,
) -> Path:
    asset = info.asset
    if asset is None:
        raise UpdateError("这个 Release 没有找到可用的 Windows EXE。")
    if not asset.sha256:
        raise UpdateError("这个 Release 没有 SHA-256 校验值，为安全起见不会自动安装。")
    update_dir = app_dir.resolve() / "更新临时文件"
    update_dir.mkdir(parents=True, exist_ok=True)
    final_path = update_dir / f"Fusion.Downloader-v{info.latest_version}.exe"
    part_path = final_path.with_suffix(".exe.part")
    if final_path.is_file():
        if final_path.stat().st_size == asset.size and sha256_file(final_path) == asset.sha256:
            if progress:
                progress(asset.size, asset.size)
            return final_path
        final_path.unlink(missing_ok=True)
    part_path.unlink(missing_ok=True)
    session = requests.Session()
    session.headers.update(GITHUB_HEADERS)
    downloaded = 0
    try:
        with bypass_unreachable_loopback_proxy(probe_url=asset.download_url):
            with session.get(asset.download_url, stream=True, timeout=(10, 60)) as response:
                response.raise_for_status()
                with part_path.open("wb") as stream:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        stream.write(chunk)
                        downloaded += len(chunk)
                        if progress:
                            progress(downloaded, asset.size)
        if downloaded != asset.size:
            raise UpdateError(f"新版文件大小不一致：预期 {asset.size}，实际 {downloaded}。")
        actual_sha256 = sha256_file(part_path)
        if actual_sha256 != asset.sha256:
            raise UpdateError("新版文件 SHA-256 校验失败，已拒绝安装。")
        part_path.replace(final_path)
        return final_path
    except (OSError, requests.RequestException, UpdateError) as exc:
        part_path.unlink(missing_ok=True)
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError(f"下载新版失败：{exc}") from exc


def application_executable() -> Path | None:
    if not getattr(sys, "frozen", False):
        return None
    executable = Path(sys.executable).resolve()
    return executable if executable.suffix.lower() == ".exe" else None


def launch_update_helper(
    staged_executable: Path,
    helper_script: Path,
    latest_version: str,
    current_version: str = APP_VERSION,
    current_executable: Path | None = None,
) -> None:
    current = (current_executable or application_executable())
    if current is None:
        raise UpdateError("源码运行模式不能替换 Python；请使用打包后的 EXE 验证安装更新。")
    current = current.resolve()
    staged = staged_executable.resolve()
    helper_source = helper_script.resolve()
    if not current.is_file() or not staged.is_file() or not helper_source.is_file():
        raise UpdateError("更新所需文件不完整，无法开始安装。")
    try:
        staged.relative_to(current.parent)
    except ValueError as exc:
        raise UpdateError("新版文件不在当前软件目录中，拒绝替换。") from exc
    owned_update_dir = current.parent / "更新临时文件"
    owned_update_dir.mkdir(parents=True, exist_ok=True)
    helper = owned_update_dir / "fusion_update_helper.ps1"
    try:
        if helper_source != helper.resolve():
            shutil.copy2(helper_source, helper)
    except OSError as exc:
        raise UpdateError(f"无法准备更新程序：{exc}") from exc
    args = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-File",
        str(helper),
        "-CurrentPath",
        str(current),
        "-StagedPath",
        str(staged),
        "-ParentPid",
        str(os.getpid()),
        "-CurrentVersion",
        normalized_version(current_version),
        "-LatestVersion",
        normalized_version(latest_version),
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        subprocess.Popen(args, creationflags=creationflags, close_fds=True)
    except OSError as exc:
        raise UpdateError(f"无法启动更新程序：{exc}") from exc
