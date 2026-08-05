from __future__ import annotations

import json
import os
import queue
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from app_version import APP_VERSION
from downloaders import bilibili, douyin, xiaohongshu, youtube, zhihu
from downloaders.douyin_collection import DouyinCollectionError, list_collections, read_douyin_login_context
from services.cancellation import DownloadCancelled, request_cancellation, reset_cancellation
from services.task_runner import TaskOptions, extract_task_inputs, run_task
from services import updater


if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent

RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ICON_PATH = RESOURCE_DIR / "favicon.ico"
UPDATE_HELPER_PATH = RESOURCE_DIR / "fusion_update_helper.ps1"
OUTPUT_DIR_NAME = "下载结果"
DEFAULT_OUTPUT_PARENT = APP_DIR
SETTINGS_PATH = APP_DIR / "下载器设置.json"
DOUYIN_FEATURES = ("作品媒体", "评论区图片", "作品媒体+评论区图片", "收藏夹")
XHS_FEATURES = ("作品媒体", "评论区图片", "作品媒体+评论区图片", "收藏作品")
BILIBILI_FEATURES = ("视频媒体",)
YOUTUBE_FEATURES = ("视频媒体",)
TIKTOK_FEATURES = ("视频媒体",)


def xhs_login_state(context: dict) -> tuple[str, str]:
    """Return the verified Xiaohongshu account state for the GUI.

    A browser always has several visitor cookies.  They must not be presented as
    a logged-in account merely because the page itself does not show a login
    dialog.  The browser-side probe supplies ``accountDetected`` only after it
    finds an account identifier from the signed-in page or the account API.
    """
    cookie_count = len([part for part in str(context.get("cookie") or "").split(";") if part.strip()])
    if context.get("authenticated") or context.get("accountDetected"):
        return "account", f"小红书账号登录态可用：浏览器 Cookie {cookie_count} 个"
    if cookie_count:
        return (
            "guest",
            f"小红书浏览器态可用但未验证账号登录：Cookie {cookie_count} 个。"
            "公开作品和部分评论可用，收藏作品需要在软件打开的小红书窗口中完成登录。",
        )
    return "unavailable", "小红书登录态不可用或已失效，请点击“登录小红书”。"
ARTICLE_FEATURES = ("文章正文（MD）",)
WECHAT_FEATURES = ("自动识别（视频/图文）",)


COLORS = {
    "background": "#F2F6F4",
    "surface": "#FFFFFF",
    "surface_subtle": "#F6F9F7",
    "surface_hover": "#EAF2EE",
    "text": "#14231C",
    "text_secondary": "#64736C",
    "text_tertiary": "#87948E",
    "selection": "#00B176",
    "selection_active": "#009F6A",
    "selection_pressed": "#00875A",
    "selection_hover_soft": "#E2F8F0",
    "selection_text": "#063A2B",
    "selection_focus": "#007E54",
    "accent_dark": "#007E54",
    "accent_deep": "#0E3B2E",
    "accent_soft": "#DFF8EF",
    "accent_pale": "#F0FBF7",
    "border": "#DCE6E1",
    "divider": "#E8EFEB",
    "success": "#009865",
    "danger": "#D94D5C",
    "danger_soft": "#FFF0F2",
    "log": "#F6FAF8",
    "shadow": "#DCE6E1",
}


def output_root_for_parent(parent: Path) -> Path:
    return parent / OUTPUT_DIR_NAME


def load_app_settings(settings_path: Path = SETTINGS_PATH) -> dict:
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError, TypeError):
        return {}


def save_app_settings(data: dict, settings_path: Path = SETTINGS_PATH) -> None:
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_output_parent(
    settings_path: Path = SETTINGS_PATH,
    default_parent: Path = DEFAULT_OUTPUT_PARENT,
) -> Path:
    data = load_app_settings(settings_path)
    saved_parent = str(data.get("output_parent") or "").strip()
    candidate = Path(saved_parent).expanduser()
    if candidate.is_absolute():
        return candidate
    return default_parent


def save_output_parent(parent: Path, settings_path: Path = SETTINGS_PATH) -> None:
    data = load_app_settings(settings_path)
    data["output_parent"] = str(parent)
    save_app_settings(data, settings_path)


def load_organize_by_author(settings_path: Path = SETTINGS_PATH) -> bool:
    value = load_app_settings(settings_path).get("organize_by_author")
    return value if isinstance(value, bool) else True


def save_organize_by_author(
    enabled: bool,
    settings_path: Path = SETTINGS_PATH,
) -> None:
    data = load_app_settings(settings_path)
    data["organize_by_author"] = bool(enabled)
    save_app_settings(data, settings_path)


def _rounded_points(width: int, height: int, radius: int, inset: int = 1) -> list[int]:
    left = inset
    top = inset
    right = max(left + 1, width - inset)
    bottom = max(top + 1, height - inset)
    radius = max(2, min(radius, (right - left) // 2, (bottom - top) // 2))
    return [
        left + radius,
        top,
        right - radius,
        top,
        right,
        top,
        right,
        top + radius,
        right,
        bottom - radius,
        right,
        bottom,
        right - radius,
        bottom,
        left + radius,
        bottom,
        left,
        bottom,
        left,
        bottom - radius,
        left,
        top + radius,
        left,
        top,
    ]


class RoundedCard(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        *,
        canvas_bg: str,
        fill: str,
        radius: int = 24,
        outline: str | None = None,
        inset: int = 22,
        shadow: bool = True,
        shadow_fill: str | None = None,
    ) -> None:
        super().__init__(
            master,
            bg=canvas_bg,
            borderwidth=0,
            highlightthickness=0,
            height=120,
        )
        self._fill = fill
        self._outline = outline or fill
        self._radius = radius
        self._inset = inset
        self._shadow_offset = 3 if shadow else 0
        self._requested_height = 120
        self._shadow = (
            self.create_polygon(
                _rounded_points(100, 100, radius),
                fill=shadow_fill or COLORS["shadow"],
                outline="",
                smooth=True,
                splinesteps=48,
            )
            if shadow
            else None
        )
        self._surface = self.create_polygon(
            _rounded_points(100, 100, radius),
            fill=fill,
            outline=self._outline,
            width=1,
            smooth=True,
            splinesteps=48,
        )
        self.body = tk.Frame(self, bg=fill, borderwidth=0, highlightthickness=0)
        self._body_window = self.create_window(
            (inset, inset),
            window=self.body,
            anchor="nw",
        )
        self.bind("<Configure>", self._redraw)
        self.body.bind("<Configure>", lambda _event: self.after_idle(self._sync_height))

    def _sync_height(self) -> None:
        if not self.winfo_exists():
            return
        wanted = max(80, self.body.winfo_reqheight() + self._inset * 2 + self._shadow_offset)
        if wanted != self._requested_height:
            self._requested_height = wanted
            super().configure(height=wanted)

    def _redraw(self, event: tk.Event) -> None:
        width = max(20, int(event.width))
        height = max(20, int(event.height))
        if self._shadow is not None:
            shadow_points = _rounded_points(
                width,
                max(10, height - self._shadow_offset),
                self._radius,
                inset=1,
            )
            shadow_points = [
                coordinate + (self._shadow_offset if index % 2 else 0)
                for index, coordinate in enumerate(shadow_points)
            ]
            self.coords(self._shadow, *shadow_points)
        surface_height = max(10, height - self._shadow_offset)
        self.coords(
            self._surface,
            *_rounded_points(width, surface_height, self._radius, inset=1),
        )
        self.itemconfigure(
            self._body_window,
            width=max(1, width - self._inset * 2),
        )


class HeroBanner(tk.Canvas):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(
            master,
            height=142,
            bg=COLORS["background"],
            borderwidth=0,
            highlightthickness=0,
        )
        self.bind("<Configure>", self._draw)

    def _draw(self, event: tk.Event | None = None) -> None:
        self.delete("all")
        width = max(320, int(event.width) if event is not None else self.winfo_width())
        height = max(120, int(event.height) if event is not None else self.winfo_height())
        self.create_polygon(
            _rounded_points(width, height, 28, inset=1),
            fill=COLORS["accent_deep"],
            outline=COLORS["accent_deep"],
            smooth=True,
            splinesteps=48,
        )
        self.create_oval(
            width - 360,
            -170,
            width + 70,
            230,
            fill="#105D45",
            outline="",
        )
        self.create_oval(
            width - 235,
            -85,
            width + 55,
            205,
            fill="#087A52",
            outline="",
        )
        self.create_text(
            36,
            25,
            text="DOWNLOAD STUDIO",
            anchor="nw",
            fill="#6FE1B9",
            font=("Segoe UI Variable Text Semibold", 9),
        )
        title_width = max(260, width - 72)
        self.create_text(
            36,
            50,
            text="把复杂下载，变成一个动作。",
            anchor="nw",
            width=title_width,
            fill="#FFFFFF",
            font=("Microsoft YaHei UI", 21, "bold"),
        )
        self.create_text(
            36,
            93,
            text="一个入口，保存七个平台的最高质量媒体、文章、封面与收藏内容。",
            anchor="nw",
            width=title_width,
            fill="#C5DED4",
            font=("Microsoft YaHei UI", 9),
        )
class PillButton(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        *,
        text: str,
        command: Callable[[], object] | None,
        variant: str = "secondary",
        canvas_bg: str,
        min_width: int = 88,
        height: int = 40,
        font: tuple[str, int] | tuple[str, int, str] = ("Microsoft YaHei UI", 10),
    ) -> None:
        self._text = text
        self._command = command
        self._variant = variant
        self._state = "normal"
        self._hovered = False
        self._pressed = False
        self._focused = False
        self._height = height
        self._font_spec = font
        measure_font = tkfont.Font(root=master, font=font)
        width = max(min_width, measure_font.measure(text) + 30)
        super().__init__(
            master,
            width=width,
            height=height,
            bg=canvas_bg,
            borderwidth=0,
            highlightthickness=0,
            takefocus=1,
            cursor="hand2",
        )
        self._shape = self.create_polygon(
            _rounded_points(width, height, height // 2, inset=1),
            smooth=True,
            splinesteps=48,
        )
        self._label = self.create_text(
            width // 2,
            height // 2,
            text=text,
            font=font,
        )
        self.bind("<Configure>", lambda _event: self._draw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)
        self.bind("<Key-space>", self._on_keyboard_invoke)
        self.bind("<Key-Return>", self._on_keyboard_invoke)
        self._draw()

    def _palette(self) -> tuple[str, str]:
        if self._state == "disabled":
            if self._variant == "primary":
                return "#AAB8B1", "#FFFFFF"
            return "#EEF2F0", "#A1ADA7"
        if self._variant == "primary":
            if self._pressed:
                return "#006A47", "#FFFFFF"
            if self._hovered:
                return "#00734D", "#FFFFFF"
            return COLORS["accent_dark"], "#FFFFFF"
        if self._pressed:
            return "#DDE8E2", COLORS["text"]
        if self._hovered:
            return COLORS["surface_hover"], COLORS["text"]
        return "#EDF3F0", COLORS["text"]

    def _draw(self) -> None:
        width = max(4, self.winfo_width())
        height = max(4, self.winfo_height())
        fill, foreground = self._palette()
        if self._focused and self._state != "disabled":
            outline = COLORS["selection"]
        else:
            outline = fill
        outline_width = 2 if self._focused and self._state != "disabled" else 1
        self.coords(self._shape, *_rounded_points(width, height, height // 2, inset=2))
        self.itemconfigure(
            self._shape,
            fill=fill,
            outline=outline,
            width=outline_width,
        )
        self.coords(self._label, width // 2, height // 2)
        self.itemconfigure(self._label, text=self._text, fill=foreground, font=self._font_spec)
        super().configure(cursor="arrow" if self._state == "disabled" else "hand2")

    def _on_enter(self, _event: tk.Event) -> None:
        self._hovered = True
        self._draw()

    def _on_leave(self, _event: tk.Event) -> None:
        self._hovered = False
        self._pressed = False
        self._draw()

    def _on_press(self, _event: tk.Event) -> None:
        if self._state == "disabled":
            return
        self.focus_set()
        self._pressed = True
        self._draw()

    def _on_release(self, event: tk.Event) -> None:
        if self._state == "disabled":
            return
        should_invoke = (
            self._pressed
            and 0 <= int(event.x) <= self.winfo_width()
            and 0 <= int(event.y) <= self.winfo_height()
        )
        self._pressed = False
        self._draw()
        if should_invoke and self._command is not None:
            self._command()

    def _on_focus_in(self, _event: tk.Event) -> None:
        self._focused = True
        self._draw()

    def _on_focus_out(self, _event: tk.Event) -> None:
        self._focused = False
        self._pressed = False
        self._draw()

    def _on_keyboard_invoke(self, _event: tk.Event) -> str:
        if self._state != "disabled" and self._command is not None:
            self._command()
        return "break"

    def configure(self, cnf: dict | None = None, **kwargs: object) -> object:
        if cnf is not None:
            return super().configure(cnf, **kwargs)
        text = kwargs.pop("text", None)
        state = kwargs.pop("state", None)
        command = kwargs.pop("command", None)
        if text is not None:
            self._text = str(text)
        if state is not None:
            self._state = str(state)
        if command is not None:
            self._command = command if callable(command) else None
        result = super().configure(**kwargs) if kwargs else None
        self._draw()
        return result

    config = configure

    def cget(self, key: str) -> object:
        if key == "text":
            return self._text
        if key == "state":
            return self._state
        return super().cget(key)


class CheckmarkToggle(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        *,
        text: str,
        variable: tk.BooleanVar,
        command: Callable[[], object] | None = None,
        canvas_bg: str,
        font: tuple[str, int] | tuple[str, int, str] = ("Microsoft YaHei UI", 9),
    ) -> None:
        self._text = text
        self._variable = variable
        self._command = command
        self._state = "normal"
        self._hovered = False
        self._pressed = False
        self._focused = False
        self._font_spec = font
        self._indicator_size = 18
        measure_font = tkfont.Font(root=master, font=font)
        width = self._indicator_size + 9 + measure_font.measure(text) + 4
        height = 32
        super().__init__(
            master,
            width=width,
            height=height,
            bg=canvas_bg,
            borderwidth=0,
            highlightthickness=0,
            takefocus=1,
            cursor="hand2",
        )
        top = (height - self._indicator_size) // 2
        box_points = _rounded_points(
            self._indicator_size,
            self._indicator_size,
            5,
            inset=1,
        )
        box_points = [
            coordinate + (top if index % 2 else 1)
            for index, coordinate in enumerate(box_points)
        ]
        self._box = self.create_polygon(
            box_points,
            smooth=True,
            splinesteps=24,
        )
        self._check = self.create_line(
            6,
            top + 9,
            9,
            top + 12,
            14,
            top + 6,
            width=2.4,
            capstyle=tk.ROUND,
            joinstyle=tk.ROUND,
        )
        self._label = self.create_text(
            self._indicator_size + 9,
            height // 2,
            text=text,
            anchor="w",
            font=font,
        )
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)
        self.bind("<Key-space>", self._on_keyboard_invoke)
        self.bind("<Key-Return>", self._on_keyboard_invoke)
        self._variable.trace_add("write", self._on_variable_change)
        self._draw()

    def _draw(self) -> None:
        selected = bool(self._variable.get())
        if self._state == "disabled":
            fill = "#B7C2BB" if selected else "#F1F2F1"
            outline = fill if selected else "#D3D7D4"
            foreground = "#AEAEB2"
        elif selected:
            fill = (
                COLORS["selection_pressed"]
                if self._pressed
                else COLORS["selection_active"]
                if self._hovered
                else COLORS["selection"]
            )
            outline = COLORS["selection_focus"] if self._focused else fill
            foreground = COLORS["text"]
        else:
            fill = COLORS["selection_hover_soft"] if self._hovered else COLORS["surface"]
            outline = COLORS["selection_focus"] if self._focused else "#AAB6AF"
            foreground = COLORS["text"]
        self.itemconfigure(
            self._box,
            fill=fill,
            outline=outline,
            width=2 if self._focused and self._state != "disabled" else 1,
        )
        self.itemconfigure(
            self._check,
            fill="#FFFFFF",
            state="normal" if selected else "hidden",
        )
        self.itemconfigure(self._label, fill=foreground, text=self._text, font=self._font_spec)
        super().configure(cursor="arrow" if self._state == "disabled" else "hand2")

    def _toggle(self) -> None:
        if self._state == "disabled":
            return
        self._variable.set(not bool(self._variable.get()))
        if self._command is not None:
            self._command()

    def _on_variable_change(self, *_args: object) -> None:
        if self.winfo_exists():
            self._draw()

    def _on_enter(self, _event: tk.Event) -> None:
        self._hovered = True
        self._draw()

    def _on_leave(self, _event: tk.Event) -> None:
        self._hovered = False
        self._pressed = False
        self._draw()

    def _on_press(self, _event: tk.Event) -> None:
        if self._state == "disabled":
            return
        self.focus_set()
        self._pressed = True
        self._draw()

    def _on_release(self, event: tk.Event) -> None:
        if self._state == "disabled":
            return
        should_toggle = (
            self._pressed
            and 0 <= int(event.x) <= self.winfo_width()
            and 0 <= int(event.y) <= self.winfo_height()
        )
        self._pressed = False
        self._draw()
        if should_toggle:
            self._toggle()

    def _on_focus_in(self, _event: tk.Event) -> None:
        self._focused = True
        self._draw()

    def _on_focus_out(self, _event: tk.Event) -> None:
        self._focused = False
        self._pressed = False
        self._draw()

    def _on_keyboard_invoke(self, _event: tk.Event) -> str:
        self._toggle()
        return "break"

    def configure(self, cnf: dict | None = None, **kwargs: object) -> object:
        if cnf is not None:
            return super().configure(cnf, **kwargs)
        state = kwargs.pop("state", None)
        text = kwargs.pop("text", None)
        command = kwargs.pop("command", None)
        if state is not None:
            self._state = str(state)
        if text is not None:
            self._text = str(text)
        if command is not None:
            self._command = command if callable(command) else None
        result = super().configure(**kwargs) if kwargs else None
        self._draw()
        return result

    config = configure

    def cget(self, key: str) -> object:
        if key == "text":
            return self._text
        if key == "state":
            return self._state
        return super().cget(key)


class UnifiedDownloaderApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("融合下载器")
        if ICON_PATH.is_file():
            try:
                self.iconbitmap(default=str(ICON_PATH))
            except tk.TclError:
                pass
        self.geometry("1280x860")
        self.minsize(920, 700)
        self.configure(bg=COLORS["background"])

        self.log_queue: queue.Queue[tuple[str, object | None]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.update_worker: threading.Thread | None = None
        self.collections: list[dict] = []
        self.output_parent = load_output_parent()
        self.output_root = output_root_for_parent(self.output_parent)
        self.last_output_dir: str | None = None
        self.total_tasks = 0
        self.finished_tasks = 0
        self.success_tasks = 0
        self.failed_tasks = 0
        self.cover_failed_tasks = 0

        self.platform_var = tk.StringVar(value="抖音")
        self.feature_var = tk.StringVar(value="作品媒体")
        self.comment_limit_var = tk.StringVar(value="")
        self.collection_limit_var = tk.StringVar(value="")
        self.collection_id_var = tk.StringVar(value="")
        self.collection_var = tk.StringVar(value="")
        self.engine_var = tk.StringVar(value="smart")
        self.speed_var = tk.StringVar(value="balanced")
        self.run_mode_var = tk.StringVar(value="单个")
        self.download_cover_var = tk.BooleanVar(value=False)
        self.cover_only_var = tk.BooleanVar(value=False)
        self.organize_by_author_var = tk.BooleanVar(value=load_organize_by_author())
        self.mode_manually_selected = False
        self.advanced_visible = tk.BooleanVar(value=False)
        self.log_visible = tk.BooleanVar(value=True)
        self.login_douyin_button: PillButton | None = None
        self.login_xhs_button: PillButton | None = None
        self.login_bilibili_button: PillButton | None = None
        self.login_youtube_button: PillButton | None = None
        self.login_zhihu_button: PillButton | None = None
        self.check_login_button: PillButton | None = None
        self.open_output_button: PillButton | None = None
        self.choose_output_button: PillButton | None = None
        self.paste_button: PillButton | None = None
        self.clear_button: PillButton | None = None
        self.copy_failure_button: ttk.Button | None = None
        self.copy_all_button: ttk.Button | None = None
        self.clear_log_button: ttk.Button | None = None
        self.update_button: ttk.Button | None = None
        self.download_cover_check: CheckmarkToggle | None = None
        self.cover_only_check: CheckmarkToggle | None = None
        self.author_folder_radio: ttk.Radiobutton | None = None
        self.flat_output_radio: ttk.Radiobutton | None = None
        self.platform_buttons: list[ttk.Radiobutton] = []
        self._wide_layout: bool | None = None

        self._setup_style()
        self._build_ui()
        self._bind_shortcuts()
        self._on_platform_change()
        self.after(100, self._drain_log_queue)

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        base_font = ("Microsoft YaHei UI", 10)
        style.configure(".", font=base_font)
        style.configure("Root.TFrame", background=COLORS["background"])
        style.configure("Surface.TFrame", background=COLORS["surface"])
        style.configure("PanelInner.TFrame", background=COLORS["surface"])
        style.configure("Soft.TFrame", background=COLORS["surface_subtle"])
        style.configure(
            "Bento.TFrame",
            background=COLORS["surface_subtle"],
            borderwidth=1,
            relief="solid",
            bordercolor=COLORS["border"],
            lightcolor=COLORS["border"],
            darkcolor=COLORS["border"],
        )
        style.configure(
            "AccentSoft.TFrame",
            background=COLORS["accent_pale"],
        )
        style.configure(
            "NavTitle.TLabel",
            background=COLORS["background"],
            foreground=COLORS["text"],
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        style.configure(
            "NavSubtitle.TLabel",
            background=COLORS["background"],
            foreground=COLORS["text_tertiary"],
            font=("Microsoft YaHei UI", 8),
        )
        style.configure(
            "Eyebrow.TLabel",
            background=COLORS["background"],
            foreground=COLORS["accent_dark"],
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.configure(
            "Title.TLabel",
            background=COLORS["background"],
            foreground=COLORS["text"],
            font=("Microsoft YaHei UI", 26, "bold"),
        )
        style.configure(
            "Subtitle.TLabel",
            background=COLORS["background"],
            foreground=COLORS["text_secondary"],
            font=("Microsoft YaHei UI", 10),
        )
        style.configure(
            "SectionTitle.TLabel",
            background=COLORS["surface"],
            foreground=COLORS["text"],
            font=("Microsoft YaHei UI", 15, "bold"),
        )
        style.configure(
            "PanelTitle.TLabel",
            background=COLORS["surface"],
            foreground=COLORS["text"],
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        style.configure(
            "Muted.TLabel",
            background=COLORS["surface"],
            foreground=COLORS["text_secondary"],
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "Hint.TLabel",
            background=COLORS["surface"],
            foreground=COLORS["text_tertiary"],
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "SoftMuted.TLabel",
            background=COLORS["surface_subtle"],
            foreground=COLORS["text_secondary"],
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "BentoTitle.TLabel",
            background=COLORS["surface_subtle"],
            foreground=COLORS["text"],
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.configure(
            "BentoMuted.TLabel",
            background=COLORS["surface_subtle"],
            foreground=COLORS["text_secondary"],
            font=("Microsoft YaHei UI", 8),
        )
        style.configure(
            "FooterEyebrow.TLabel",
            background=COLORS["surface"],
            foreground=COLORS["text_tertiary"],
            font=("Microsoft YaHei UI", 8, "bold"),
        )
        style.configure(
            "FooterPath.TLabel",
            background=COLORS["surface"],
            foreground=COLORS["text"],
            font=("Microsoft YaHei UI", 10),
        )
        style.configure(
            "FooterTitle.TLabel",
            background=COLORS["surface"],
            foreground=COLORS["text"],
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.configure(
            "FooterHint.TLabel",
            background=COLORS["surface"],
            foreground=COLORS["text_tertiary"],
            font=("Microsoft YaHei UI", 8),
        )
        style.configure(
            "FooterSegment.TFrame",
            background=COLORS["surface_subtle"],
        )
        style.configure(
            "AccentHint.TLabel",
            background=COLORS["accent_pale"],
            foreground=COLORS["accent_dark"],
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "AdvancedMuted.TLabel",
            background=COLORS["surface_subtle"],
            foreground=COLORS["text_secondary"],
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "AdvancedHint.TLabel",
            background=COLORS["surface_subtle"],
            foreground=COLORS["text_tertiary"],
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "StatusPill.TLabel",
            background=COLORS["accent_pale"],
            foreground=COLORS["accent_dark"],
            padding=(12, 8),
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "StatValue.TLabel",
            background=COLORS["surface_subtle"],
            foreground=COLORS["text"],
            font=("Segoe UI Variable Display Semib", 22),
        )
        style.configure(
            "SuccessStatValue.TLabel",
            background=COLORS["surface_subtle"],
            foreground=COLORS["success"],
            font=("Segoe UI Variable Display Semib", 22),
        )
        style.configure(
            "DangerStatValue.TLabel",
            background=COLORS["surface_subtle"],
            foreground=COLORS["danger"],
            font=("Segoe UI Variable Display Semib", 22),
        )
        style.configure(
            "StatLabel.TLabel",
            background=COLORS["surface_subtle"],
            foreground=COLORS["text_secondary"],
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "Link.TButton",
            background=COLORS["surface"],
            foreground=COLORS["accent_dark"],
            borderwidth=0,
            focuscolor=COLORS["surface"],
            padding=(3, 4),
            font=("Microsoft YaHei UI", 9),
        )
        style.map(
            "Link.TButton",
            background=[("active", COLORS["surface"])],
            foreground=[
                ("disabled", "#AEAEB2"),
                ("active", COLORS["selection_pressed"]),
                ("!disabled", COLORS["accent_dark"]),
            ],
        )
        style.configure(
            "SoftLink.TButton",
            background=COLORS["surface_subtle"],
            foreground=COLORS["accent_dark"],
            borderwidth=0,
            focuscolor=COLORS["surface_subtle"],
            padding=(3, 4),
            font=("Microsoft YaHei UI", 9),
        )
        style.map(
            "SoftLink.TButton",
            background=[("active", COLORS["surface_subtle"])],
            foreground=[
                ("disabled", "#A1ADA7"),
                ("active", COLORS["selection_pressed"]),
                ("!disabled", COLORS["accent_dark"]),
            ],
        )
        style.configure(
            "Primary.TButton",
            background=COLORS["accent_dark"],
            foreground="#FFFFFF",
            borderwidth=0,
            padding=(16, 10),
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.map(
            "Primary.TButton",
            background=[
                ("disabled", "#AAB8B1"),
                ("active", "#00734D"),
                ("!disabled", COLORS["accent_dark"]),
            ],
            foreground=[("disabled", "#FFFFFF"), ("!disabled", "#FFFFFF")],
        )
        style.configure(
            "Secondary.TButton",
            background="#ECECF0",
            foreground=COLORS["text"],
            borderwidth=0,
            padding=(13, 9),
            font=("Microsoft YaHei UI", 9),
        )
        style.map(
            "Secondary.TButton",
            background=[
                ("disabled", "#EEF2F0"),
                ("active", COLORS["surface_hover"]),
                ("!disabled", "#EDF3F0"),
            ],
            foreground=[("disabled", "#A1ADA7"), ("!disabled", COLORS["text"])],
        )
        style.configure(
            "TCombobox",
            padding=(11, 8),
            fieldbackground=COLORS["surface"],
            background=COLORS["surface"],
            foreground=COLORS["text"],
            bordercolor=COLORS["border"],
            arrowcolor=COLORS["text_secondary"],
            lightcolor=COLORS["border"],
            darkcolor=COLORS["border"],
        )
        style.map(
            "TCombobox",
            fieldbackground=[
                ("readonly", COLORS["surface"]),
                ("disabled", "#EEF2F0"),
            ],
            bordercolor=[("focus", COLORS["selection"])],
            foreground=[("disabled", "#A1ADA7")],
        )
        style.configure(
            "TEntry",
            padding=(11, 8),
            fieldbackground=COLORS["surface"],
            foreground=COLORS["text"],
            bordercolor=COLORS["border"],
            lightcolor=COLORS["border"],
            darkcolor=COLORS["border"],
        )
        style.map(
            "TEntry",
            bordercolor=[("focus", COLORS["selection"])],
            fieldbackground=[("disabled", "#EEF2F0")],
        )
        style.layout(
            "Platform.TRadiobutton",
            [
                (
                    "Radiobutton.padding",
                    {
                        "sticky": "nswe",
                        "children": [
                            (
                                "Radiobutton.focus",
                                {
                                    "sticky": "nswe",
                                    "children": [
                                        ("Radiobutton.label", {"sticky": "nswe"})
                                    ],
                                },
                            )
                        ],
                    },
                )
            ],
        )
        style.configure(
            "Platform.TRadiobutton",
            background=COLORS["surface_subtle"],
            foreground=COLORS["text_secondary"],
            borderwidth=0,
            padding=(11, 10),
            anchor="center",
            focuscolor=COLORS["selection_focus"],
            focusthickness=2,
            focussolid=True,
            font=("Microsoft YaHei UI", 9),
        )
        style.map(
            "Platform.TRadiobutton",
            background=[
                ("selected", COLORS["selection"]),
                ("focus", COLORS["selection_hover_soft"]),
                ("active", COLORS["selection_hover_soft"]),
                ("!selected", COLORS["surface_subtle"]),
            ],
            foreground=[
                ("disabled", "#AEAEB2"),
                ("selected", COLORS["selection_text"]),
                ("!selected", COLORS["text_secondary"]),
            ],
            focuscolor=[
                ("selected", COLORS["selection"]),
                ("!selected", COLORS["selection_hover_soft"]),
            ],
        )
        style.layout(
            "Mode.TRadiobutton",
            [
                (
                    "Radiobutton.padding",
                    {
                        "sticky": "nswe",
                        "children": [
                            (
                                "Radiobutton.focus",
                                {
                                    "sticky": "nswe",
                                    "children": [
                                        ("Radiobutton.label", {"sticky": "nswe"})
                                    ],
                                },
                            )
                        ],
                    },
                )
            ],
        )
        style.configure(
            "Mode.TRadiobutton",
            background=COLORS["surface_subtle"],
            foreground=COLORS["text_secondary"],
            padding=(12, 8),
            anchor="center",
            focuscolor=COLORS["selection"],
            focusthickness=2,
            focussolid=True,
            font=("Microsoft YaHei UI", 9),
        )
        style.map(
            "Mode.TRadiobutton",
            background=[
                ("selected", COLORS["accent_soft"]),
                ("focus", COLORS["surface_hover"]),
                ("active", COLORS["surface_hover"]),
                ("!selected", COLORS["surface_subtle"]),
            ],
            foreground=[
                ("disabled", "#A1ADA7"),
                ("selected", COLORS["accent_dark"]),
                ("!selected", COLORS["text_secondary"]),
            ],
            focuscolor=[
                ("selected", COLORS["accent_soft"]),
                ("!selected", COLORS["surface_hover"]),
            ],
        )
        style.layout(
            "FooterMode.TRadiobutton",
            [
                (
                    "Radiobutton.padding",
                    {
                        "sticky": "nswe",
                        "children": [
                            ("Radiobutton.label", {"sticky": "nswe"}),
                        ],
                    },
                )
            ],
        )
        style.configure(
            "FooterMode.TRadiobutton",
            background=COLORS["surface_subtle"],
            foreground=COLORS["text_secondary"],
            padding=(15, 9),
            anchor="center",
            borderwidth=0,
            focusthickness=0,
            focussolid=False,
            font=("Microsoft YaHei UI", 9),
        )
        style.map(
            "FooterMode.TRadiobutton",
            background=[
                ("selected", COLORS["accent_soft"]),
                ("active", COLORS["surface_hover"]),
                ("!selected", COLORS["surface_subtle"]),
            ],
            foreground=[
                ("disabled", "#A1ADA7"),
                ("selected", COLORS["accent_dark"]),
                ("!selected", COLORS["text_secondary"]),
            ],
        )
        style.configure(
            "Horizontal.TProgressbar",
            troughcolor=COLORS["divider"],
            background=COLORS["selection"],
            bordercolor=COLORS["divider"],
            lightcolor=COLORS["selection"],
            darkcolor=COLORS["selection"],
            thickness=7,
        )
        style.configure(
            "Minimal.Vertical.TScrollbar",
            gripcount=0,
            background="#B7C8BF",
            troughcolor=COLORS["background"],
            bordercolor=COLORS["background"],
            lightcolor="#B7C8BF",
            darkcolor="#B7C8BF",
            arrowcolor=COLORS["background"],
            width=9,
        )

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.scroll_canvas = tk.Canvas(
            self,
            bg=COLORS["background"],
            highlightthickness=0,
            borderwidth=0,
        )
        self.page_scrollbar = ttk.Scrollbar(
            self,
            orient="vertical",
            command=self.scroll_canvas.yview,
            style="Minimal.Vertical.TScrollbar",
        )
        self.scroll_canvas.configure(yscrollcommand=self.page_scrollbar.set)
        self.scroll_canvas.grid(row=0, column=0, sticky="nsew")
        self.page_scrollbar.grid(row=0, column=1, sticky="ns")

        page = ttk.Frame(self.scroll_canvas, style="Root.TFrame")
        self.page_frame = page
        self.page_window = self.scroll_canvas.create_window((0, 0), window=page, anchor="nw")
        page.bind("<Configure>", self._on_page_configure)
        self.scroll_canvas.bind("<Configure>", self._on_canvas_configure)
        self.bind_all("<MouseWheel>", self._on_mousewheel)
        page.columnconfigure(0, weight=1)
        page.columnconfigure(1, weight=1)

        nav = ttk.Frame(page, style="Root.TFrame", padding=(32, 18, 32, 14))
        nav.grid(row=0, column=0, columnspan=2, sticky="ew")
        nav.columnconfigure(0, weight=1)
        brand = ttk.Frame(nav, style="Root.TFrame")
        brand.grid(row=0, column=0, sticky="w")
        ttk.Label(brand, text="融合下载器", style="NavTitle.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
        )
        ttk.Label(brand, text="多平台内容下载工作台", style="NavSubtitle.TLabel").grid(
            row=1,
            column=0,
            sticky="w",
            pady=(2, 0),
        )
        self.update_button = ttk.Button(
            nav,
            text=f"检查更新  v{APP_VERSION}",
            style="Link.TButton",
            command=self.check_for_updates,
        )
        self.update_button.grid(row=0, column=1, sticky="e")
        self.hero_banner = HeroBanner(page)
        self.hero_banner.grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="ew",
            padx=32,
            pady=(0, 18),
        )

        self.input_card = RoundedCard(
            page,
            canvas_bg=COLORS["background"],
            fill=COLORS["surface"],
            radius=24,
            outline=COLORS["border"],
            inset=24,
        )
        input_panel = self.input_card.body
        input_panel.columnconfigure(0, weight=1)

        task_header = ttk.Frame(input_panel, style="Surface.TFrame")
        task_header.grid(row=0, column=0, sticky="ew")
        task_header.columnconfigure(0, weight=1)
        ttk.Label(task_header, text="创建下载任务", style="SectionTitle.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
        )

        ttk.Label(input_panel, text="选择平台", style="PanelTitle.TLabel").grid(
            row=1,
            column=0,
            sticky="w",
            pady=(18, 0),
        )
        platform_strip = ttk.Frame(
            input_panel,
            style="Bento.TFrame",
            padding=(5, 5, 5, 5),
        )
        platform_strip.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        platforms = ("抖音", "小红书", "Bilibili", "YouTube", "TikTok", "微信", "知乎")
        for index, platform in enumerate(platforms):
            platform_strip.columnconfigure(index, weight=1, uniform="platform")
            button = ttk.Radiobutton(
                platform_strip,
                text=platform,
                variable=self.platform_var,
                value=platform,
                style="Platform.TRadiobutton",
                command=self._on_platform_change,
                takefocus=True,
            )
            button.grid(
                row=0,
                column=index,
                sticky="ew",
                padx=(0 if index == 0 else 2, 0),
            )
            self.platform_buttons.append(button)
        self.platform_combo = ttk.Combobox(
            input_panel,
            textvariable=self.platform_var,
            state="readonly",
            values=platforms,
        )

        options = ttk.Frame(input_panel, style="Surface.TFrame")
        options.grid(row=3, column=0, sticky="ew", pady=(18, 0))
        options.rowconfigure(0, weight=1)
        for column in range(3):
            options.columnconfigure(column, weight=1, uniform="option")
        feature_group = ttk.Frame(
            options,
            style="Bento.TFrame",
            padding=(14, 12, 14, 12),
        )
        feature_group.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        feature_group.columnconfigure(0, weight=1)
        ttk.Label(feature_group, text="下载内容", style="BentoMuted.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
        )
        self.feature_combo = ttk.Combobox(
            feature_group,
            textvariable=self.feature_var,
            state="readonly",
            values=DOUYIN_FEATURES,
            width=18,
        )
        self.feature_combo.grid(row=1, column=0, sticky="ew", pady=(7, 0))
        self.feature_combo.bind("<<ComboboxSelected>>", lambda _event: self._on_feature_change())

        mode_group = ttk.Frame(
            options,
            style="Bento.TFrame",
            padding=(14, 12, 14, 12),
        )
        mode_group.grid(row=0, column=1, sticky="nsew", padx=(0, 10))
        mode_group.columnconfigure(0, weight=1)
        ttk.Label(mode_group, text="任务模式", style="BentoMuted.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
        )
        mode_frame = ttk.Frame(mode_group, style="Soft.TFrame", padding=(3, 3, 3, 3))
        mode_frame.grid(row=1, column=0, sticky="ew", pady=(7, 0))
        mode_frame.columnconfigure(0, weight=1, uniform="mode")
        mode_frame.columnconfigure(1, weight=1, uniform="mode")
        self.mode_single_radio = ttk.Radiobutton(
            mode_frame,
            text="单个",
            variable=self.run_mode_var,
            value="单个",
            command=self._mark_mode_manual,
            style="Mode.TRadiobutton",
        )
        self.mode_single_radio.grid(row=0, column=0, sticky="ew")
        self.mode_batch_radio = ttk.Radiobutton(
            mode_frame,
            text="批量",
            variable=self.run_mode_var,
            value="批量",
            command=self._mark_mode_manual,
            style="Mode.TRadiobutton",
        )
        self.mode_batch_radio.grid(row=0, column=1, sticky="ew", padx=(2, 0))

        cover_group = ttk.Frame(
            options,
            style="Bento.TFrame",
            padding=(14, 12, 14, 12),
        )
        cover_group.grid(row=0, column=2, sticky="nsew")
        ttk.Label(cover_group, text="封面设置", style="BentoMuted.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
        )
        self.download_cover_check = CheckmarkToggle(
            cover_group,
            text="同时保存最高质量作品封面",
            variable=self.download_cover_var,
            canvas_bg=COLORS["surface_subtle"],
        )
        self.download_cover_check.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.cover_only_check = CheckmarkToggle(
            cover_group,
            text="仅下载最高质量作品封面",
            variable=self.cover_only_var,
            command=self._on_cover_only_change,
            canvas_bg=COLORS["surface_subtle"],
        )
        self.cover_only_check.grid(row=2, column=0, sticky="w", pady=(4, 0))

        self.collection_bar = ttk.Frame(
            input_panel,
            style="Bento.TFrame",
            padding=(14, 12, 14, 12),
        )
        self.collection_bar.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        self.collection_bar.columnconfigure(1, weight=1)
        self.collection_label_widget = ttk.Label(
            self.collection_bar,
            text="收藏夹",
            style="BentoMuted.TLabel",
        )
        self.collection_label_widget.grid(row=0, column=0, sticky="w")
        self.collection_combo = ttk.Combobox(
            self.collection_bar,
            textvariable=self.collection_var,
            state="readonly",
            values=(),
            width=26,
        )
        self.collection_combo.grid(row=0, column=1, padx=(8, 10), sticky="ew")
        self.collection_combo.bind("<<ComboboxSelected>>", lambda _event: self._select_collection())
        self.refresh_collections_button = ttk.Button(
            self.collection_bar,
            text="刷新收藏夹列表",
            style="SoftLink.TButton",
            command=self.refresh_collections,
        )
        self.refresh_collections_button.grid(row=0, column=2, sticky="w")

        advanced_header = ttk.Frame(input_panel, style="Surface.TFrame")
        advanced_header.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        advanced_header.columnconfigure(0, weight=1)
        ttk.Label(
            advanced_header,
            text="保留默认设置即可获得推荐下载体验。",
            style="Hint.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self.advanced_button = ttk.Button(
            advanced_header,
            text="显示高级设置",
            style="Link.TButton",
            command=self.toggle_advanced,
        )
        self.advanced_button.grid(row=0, column=1, sticky="e")

        self.advanced_frame = ttk.Frame(
            input_panel,
            style="Bento.TFrame",
            padding=(16, 14, 16, 14),
        )
        self.advanced_frame.columnconfigure(1, weight=1)
        self.advanced_frame.columnconfigure(3, weight=1)
        ttk.Label(self.advanced_frame, text="下载引擎", style="AdvancedMuted.TLabel").grid(row=0, column=0, sticky="w")
        self.engine_combo = ttk.Combobox(self.advanced_frame, textvariable=self.engine_var, state="readonly", values=("smart", "builtin", "auto"), width=14)
        self.engine_combo.grid(row=0, column=1, sticky="ew", padx=(8, 20))
        ttk.Label(self.advanced_frame, text="速度", style="AdvancedMuted.TLabel").grid(row=0, column=2, sticky="w")
        self.speed_combo = ttk.Combobox(self.advanced_frame, textvariable=self.speed_var, state="readonly", values=("stable", "balanced", "fast"), width=14)
        self.speed_combo.grid(row=0, column=3, sticky="ew", padx=(8, 0))
        ttk.Label(self.advanced_frame, text="评论图片上限", style="AdvancedMuted.TLabel").grid(row=1, column=0, sticky="w", pady=(12, 0))
        self.comment_limit_entry = ttk.Entry(self.advanced_frame, textvariable=self.comment_limit_var, width=12)
        self.comment_limit_entry.grid(row=1, column=1, sticky="ew", padx=(8, 20), pady=(12, 0))
        self.collection_limit_label = ttk.Label(self.advanced_frame, text="收藏夹作品上限", style="AdvancedMuted.TLabel")
        self.collection_limit_label.grid(row=1, column=2, sticky="w", pady=(12, 0))
        self.collection_limit_entry = ttk.Entry(self.advanced_frame, textvariable=self.collection_limit_var, width=12)
        self.collection_limit_entry.grid(row=1, column=3, sticky="ew", padx=(8, 0), pady=(12, 0))
        ttk.Label(self.advanced_frame, text="手动 ID", style="AdvancedMuted.TLabel").grid(row=2, column=0, sticky="w", pady=(12, 0))
        self.collection_id_entry = ttk.Entry(self.advanced_frame, textvariable=self.collection_id_var, width=24)
        self.collection_id_entry.grid(row=2, column=1, sticky="ew", padx=(8, 20), pady=(12, 0))
        ttk.Label(
            self.advanced_frame,
            text="留空表示全部；网络不稳建议选 balanced 或 stable。",
            style="AdvancedHint.TLabel",
            justify="left",
            wraplength=250,
        ).grid(
            row=2,
            column=2,
            columnspan=2,
            sticky="w",
            pady=(12, 0),
        )

        input_header = ttk.Frame(input_panel, style="Surface.TFrame")
        input_header.grid(row=7, column=0, sticky="ew", pady=(20, 0))
        input_header.columnconfigure(1, weight=1)
        ttk.Label(input_header, text="输入链接 / 分享文案", style="PanelTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.detected_label = ttk.Label(input_header, text="已识别：0 条内容", style="Hint.TLabel")
        self.detected_label.grid(row=0, column=1, sticky="e")
        self.input_text = tk.Text(
            input_panel,
            height=4,
            wrap="word",
            font=("Microsoft YaHei UI", 10),
            bg=COLORS["accent_pale"],
            fg=COLORS["text"],
            insertbackground=COLORS["accent_dark"],
            selectbackground=COLORS["accent_soft"],
            selectforeground=COLORS["text"],
            relief="flat",
            padx=16,
            pady=14,
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            highlightcolor=COLORS["selection"],
            undo=True,
        )
        self.input_text.grid(row=8, column=0, sticky="ew", pady=(8, 0))
        self.input_text.bind("<KeyRelease>", lambda _event: self._update_detected_count())

        input_tools = ttk.Frame(input_panel, style="Surface.TFrame")
        input_tools.grid(row=9, column=0, sticky="ew", pady=(10, 0))
        input_tools.columnconfigure(0, weight=1)
        ttk.Label(
            input_tools,
            text="快捷键：Ctrl+L 定位输入框 · Ctrl+Enter 开始",
            style="Hint.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self.paste_button = PillButton(
            input_tools,
            text="粘贴",
            command=self.paste_clipboard,
            canvas_bg=COLORS["surface"],
            min_width=72,
            height=36,
            font=("Microsoft YaHei UI", 9),
        )
        self.paste_button.grid(row=0, column=1, sticky="e")
        self.clear_button = PillButton(
            input_tools,
            text="清空",
            command=self.clear_all,
            canvas_bg=COLORS["surface"],
            min_width=72,
            height=36,
            font=("Microsoft YaHei UI", 9),
        )
        self.clear_button.grid(row=0, column=2, sticky="e", padx=(8, 0))

        self.login_status_label = ttk.Label(
            input_panel,
            text="登录状态：需要下载收藏内容时请先登录",
            style="StatusPill.TLabel",
            anchor="w",
            justify="left",
            wraplength=650,
        )
        self.login_status_label.grid(row=10, column=0, sticky="ew", pady=(14, 0))

        action_bar = ttk.Frame(
            input_panel,
            style="Bento.TFrame",
            padding=(12, 10, 12, 10),
        )
        action_bar.grid(row=11, column=0, sticky="ew", pady=(14, 0))
        action_bar.columnconfigure(0, weight=1)
        account_controls = ttk.Frame(action_bar, style="Soft.TFrame")
        account_controls.grid(row=0, column=0, sticky="w")
        self.login_douyin_button = PillButton(
            account_controls,
            text="登录抖音",
            command=self.open_douyin_login,
            canvas_bg=COLORS["surface_subtle"],
            min_width=94,
            height=38,
            font=("Microsoft YaHei UI", 9),
        )
        self.login_douyin_button.grid(row=0, column=0, sticky="w")
        self.login_xhs_button = PillButton(
            account_controls,
            text="登录小红书",
            command=self.open_xhs_login,
            canvas_bg=COLORS["surface_subtle"],
            min_width=104,
            height=38,
            font=("Microsoft YaHei UI", 9),
        )
        self.login_xhs_button.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.login_bilibili_button = PillButton(
            account_controls,
            text="登录 Bilibili",
            command=self.open_bilibili_login,
            canvas_bg=COLORS["surface_subtle"],
            min_width=112,
            height=38,
            font=("Microsoft YaHei UI", 9),
        )
        self.login_bilibili_button.grid(row=0, column=2, sticky="w", padx=(8, 0))
        self.login_youtube_button = PillButton(
            account_controls,
            text="YouTube 登录设置",
            command=self.open_youtube_auth_settings,
            canvas_bg=COLORS["surface_subtle"],
            min_width=136,
            height=38,
            font=("Microsoft YaHei UI", 9),
        )
        self.login_youtube_button.grid(row=0, column=3, sticky="w", padx=(8, 0))
        self.login_zhihu_button = PillButton(
            account_controls,
            text="登录知乎",
            command=self.open_zhihu_login,
            canvas_bg=COLORS["surface_subtle"],
            min_width=94,
            height=38,
            font=("Microsoft YaHei UI", 9),
        )
        self.login_zhihu_button.grid(row=0, column=4, sticky="w", padx=(8, 0))
        self.check_login_button = PillButton(
            account_controls,
            text="检查登录状态",
            command=self.check_login_status,
            canvas_bg=COLORS["surface_subtle"],
            min_width=112,
            height=38,
            font=("Microsoft YaHei UI", 9),
        )
        self.check_login_button.grid(row=0, column=5, sticky="w", padx=(8, 0))
        self.start_button = PillButton(
            action_bar,
            text="开始下载",
            command=self.start_from_mode,
            variant="primary",
            canvas_bg=COLORS["surface_subtle"],
            min_width=148,
            height=46,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        self.start_button.grid(row=0, column=1, sticky="e", padx=(18, 0))

        self.status_card = RoundedCard(
            page,
            canvas_bg=COLORS["background"],
            fill=COLORS["surface"],
            radius=24,
            outline=COLORS["border"],
            inset=22,
        )
        self.status_panel = self.status_card.body
        status_panel = self.status_panel
        status_panel.columnconfigure(0, weight=1)
        status_panel.rowconfigure(5, weight=1, minsize=220)
        feedback_header = ttk.Frame(status_panel, style="Surface.TFrame")
        feedback_header.grid(row=0, column=0, sticky="ew")
        feedback_header.columnconfigure(0, weight=1)
        ttk.Label(feedback_header, text="任务动态", style="SectionTitle.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
        )
        self.empty_state_label = ttk.Label(
            status_panel,
            text="还没有任务，粘贴链接后点击开始下载。",
            style="StatusPill.TLabel",
            wraplength=280,
            justify="left",
        )
        self.empty_state_label.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        stats = ttk.Frame(status_panel, style="Surface.TFrame")
        stats.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        stats.grid_configure(row=2)
        for i in range(2):
            stats.columnconfigure(i, weight=1, uniform="stat")
        self.total_value = self._stat_card(stats, 0, "总任务", "0")
        self.done_value = self._stat_card(stats, 1, "已完成", "0")
        self.success_value = self._stat_card(stats, 2, "成功", "0", "SuccessStatValue.TLabel")
        self.failed_value = self._stat_card(stats, 3, "失败", "0", "DangerStatValue.TLabel")

        progress_line = ttk.Frame(status_panel, style="Surface.TFrame")
        progress_line.grid(row=3, column=0, sticky="ew", pady=(16, 0))
        progress_line.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_line, mode="determinate", maximum=100, value=0)
        self.progress.grid(row=0, column=0, sticky="ew")
        self.status_label = ttk.Label(progress_line, text="等待任务", style="Muted.TLabel")
        self.status_label.grid(row=1, column=0, sticky="w", pady=(8, 0))

        log_header = ttk.Frame(status_panel, style="Surface.TFrame")
        log_header.grid(row=4, column=0, sticky="ew", pady=(18, 0))
        log_header.columnconfigure(0, weight=1)
        ttk.Label(log_header, text="运行日志", style="PanelTitle.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
        )
        self.log_toggle_button = ttk.Button(
            log_header,
            text="隐藏运行日志",
            style="Link.TButton",
            command=self.toggle_log_panel,
        )
        self.log_toggle_button.grid(row=0, column=1, sticky="e", padx=(8, 0))
        log_actions = ttk.Frame(log_header, style="Surface.TFrame")
        log_actions.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.copy_failure_button = ttk.Button(
            log_actions,
            text="复制失败摘要",
            style="Link.TButton",
            command=self.copy_failure_log,
        )
        self.copy_failure_button.grid(row=0, column=0, sticky="w")
        self.copy_all_button = ttk.Button(
            log_actions,
            text="复制完整日志",
            style="Link.TButton",
            command=self.copy_all_log,
        )
        self.copy_all_button.grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.clear_log_button = ttk.Button(
            log_actions,
            text="清空日志",
            style="Link.TButton",
            command=self.clear_log,
        )
        self.clear_log_button.grid(row=0, column=2, sticky="w", padx=(10, 0))
        self.log_box = scrolledtext.ScrolledText(
            status_panel,
            wrap="word",
            state="disabled",
            height=12,
            font=("Consolas", 10),
            bg=COLORS["log"],
            fg="#24352D",
            insertbackground=COLORS["accent_dark"],
            selectbackground=COLORS["accent_soft"],
            selectforeground=COLORS["text"],
            relief="flat",
            padx=14,
            pady=12,
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            highlightcolor=COLORS["selection"],
        )
        self.log_box.grid(row=5, column=0, sticky="nsew", pady=(10, 0))
        self.log_menu = tk.Menu(self, tearoff=0)
        self.log_menu.add_command(label="复制选中内容", command=lambda: self.log_box.event_generate("<<Copy>>"))
        self.log_menu.add_command(label="复制完整日志", command=self.copy_all_log)
        self.log_menu.add_command(label="复制失败摘要", command=self.copy_failure_log)
        self.log_menu.add_separator()
        self.log_menu.add_command(label="全选", command=self.select_all_log)
        self.log_box.bind("<Button-3>", self.show_log_menu)
        self.log_box.bind("<Control-a>", self.select_all_log)

        self.footer = ttk.Frame(page, style="Root.TFrame", padding=(34, 6, 34, 22))
        self.footer.columnconfigure(0, weight=1)
        self.footer_card = RoundedCard(
            self.footer,
            canvas_bg=COLORS["background"],
            fill=COLORS["surface"],
            radius=20,
            outline=COLORS["border"],
            inset=18,
            shadow=False,
        )
        self.footer_card.grid(row=0, column=0, sticky="ew")
        footer_content = self.footer_card.body
        footer_content.columnconfigure(0, weight=1)

        location_row = tk.Frame(
            footer_content,
            bg=COLORS["surface"],
            borderwidth=0,
            highlightthickness=0,
        )
        location_row.grid(row=0, column=0, sticky="ew")
        location_row.columnconfigure(0, weight=1)
        location_info = tk.Frame(
            location_row,
            bg=COLORS["surface"],
            borderwidth=0,
            highlightthickness=0,
        )
        location_info.grid(row=0, column=0, sticky="w")
        ttk.Label(
            location_info,
            text="下载位置",
            style="FooterEyebrow.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self.output_path_label = ttk.Label(
            location_info,
            text=str(self.output_root),
            style="FooterPath.TLabel",
            wraplength=720,
        )
        self.output_path_label.grid(row=1, column=0, sticky="w", pady=(3, 0))

        footer_actions = tk.Frame(
            location_row,
            bg=COLORS["surface"],
            borderwidth=0,
            highlightthickness=0,
        )
        footer_actions.grid(row=0, column=1, sticky="e", padx=(20, 0))
        self.choose_output_button = PillButton(
            footer_actions,
            text="更改位置",
            command=self.choose_output_parent,
            canvas_bg=COLORS["surface"],
            min_width=100,
            height=40,
            font=("Microsoft YaHei UI", 9),
        )
        self.choose_output_button.grid(row=0, column=0, sticky="e", padx=(0, 8))
        self.open_output_button = PillButton(
            footer_actions,
            text="打开下载结果",
            command=self.open_output_dir,
            variant="primary",
            canvas_bg=COLORS["surface"],
            min_width=128,
            height=40,
            font=("Microsoft YaHei UI", 9),
        )
        self.open_output_button.grid(row=0, column=1, sticky="e")

        divider = tk.Frame(
            footer_content,
            height=1,
            bg=COLORS["divider"],
            borderwidth=0,
            highlightthickness=0,
        )
        divider.grid(row=1, column=0, sticky="ew", pady=(14, 12))

        organization_row = tk.Frame(
            footer_content,
            bg=COLORS["surface"],
            borderwidth=0,
            highlightthickness=0,
        )
        organization_row.grid(row=2, column=0, sticky="ew")
        organization_row.columnconfigure(0, weight=1)
        organization_info = tk.Frame(
            organization_row,
            bg=COLORS["surface"],
            borderwidth=0,
            highlightthickness=0,
        )
        organization_info.grid(row=0, column=0, sticky="w")
        ttk.Label(
            organization_info,
            text="文件归类",
            style="FooterTitle.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self.organization_hint_label = ttk.Label(
            organization_info,
            text=self._organization_hint_text(),
            style="FooterHint.TLabel",
        )
        self.organization_hint_label.grid(row=1, column=0, sticky="w", pady=(3, 0))

        organization_strip = ttk.Frame(
            organization_row,
            style="FooterSegment.TFrame",
            padding=(4, 4, 4, 4),
        )
        organization_strip.grid(row=0, column=1, sticky="e", padx=(20, 0))
        self.author_folder_radio = ttk.Radiobutton(
            organization_strip,
            text="按博主分类",
            variable=self.organize_by_author_var,
            value=True,
            command=self._on_output_organization_change,
            style="FooterMode.TRadiobutton",
        )
        self.author_folder_radio.grid(row=0, column=0, sticky="ew")
        self.flat_output_radio = ttk.Radiobutton(
            organization_strip,
            text="全部平铺",
            variable=self.organize_by_author_var,
            value=False,
            command=self._on_output_organization_change,
            style="FooterMode.TRadiobutton",
        )
        self.flat_output_radio.grid(row=0, column=1, sticky="ew", padx=(2, 0))
        self._apply_responsive_layout(1180)

    def _stat_card(
        self,
        parent: ttk.Frame,
        index: int,
        label: str,
        value: str,
        value_style: str = "StatValue.TLabel",
    ) -> ttk.Label:
        row = index // 2
        column = index % 2
        card = ttk.Frame(parent, style="Bento.TFrame", padding=(14, 12, 14, 12))
        card.grid(
            row=row,
            column=column,
            sticky="nsew",
            padx=(0 if column == 0 else 8, 0),
            pady=(0 if row == 0 else 8, 0),
        )
        value_label = ttk.Label(card, text=value, style=value_style)
        value_label.grid(row=0, column=0, sticky="w")
        ttk.Label(card, text=label, style="StatLabel.TLabel").grid(row=1, column=0, sticky="w")
        return value_label

    def _apply_responsive_layout(self, page_width: int) -> None:
        wide = page_width >= 1120
        if self._wide_layout == wide:
            return
        self._wide_layout = wide
        if wide:
            self.page_frame.columnconfigure(0, weight=5)
            self.page_frame.columnconfigure(1, weight=3)
            self.input_card.grid(
                row=2,
                column=0,
                sticky="nsew",
                padx=(32, 10),
                pady=(0, 18),
            )
            self.status_card.grid(
                row=2,
                column=1,
                sticky="nsew",
                padx=(10, 32),
                pady=(0, 18),
            )
            self.footer.grid(row=3, column=0, columnspan=2, sticky="ew")
        else:
            self.page_frame.columnconfigure(0, weight=1)
            self.page_frame.columnconfigure(1, weight=0)
            self.input_card.grid(
                row=2,
                column=0,
                columnspan=2,
                sticky="ew",
                padx=24,
                pady=(0, 16),
            )
            self.status_card.grid(
                row=3,
                column=0,
                columnspan=2,
                sticky="ew",
                padx=24,
                pady=(0, 16),
            )
            self.footer.grid(row=4, column=0, columnspan=2, sticky="ew")

    def _on_page_configure(self, _event: tk.Event | None = None) -> None:
        self.scroll_canvas.configure(scrollregion=self.scroll_canvas.bbox("all"))

    def _on_canvas_configure(self, event: tk.Event) -> None:
        viewport_width = max(1, int(event.width))
        page_width = min(viewport_width, 1320)
        page_x = max(0, (viewport_width - page_width) // 2)
        self.scroll_canvas.coords(self.page_window, page_x, 0)
        self.scroll_canvas.itemconfigure(self.page_window, width=page_width)
        self._apply_responsive_layout(page_width)

    def _on_mousewheel(self, event: tk.Event) -> str | None:
        if event.widget in (self.input_text, self.log_box):
            return None
        self.scroll_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"

    @property
    def is_running(self) -> bool:
        return bool(self.worker and self.worker.is_alive())

    def _bind_shortcuts(self) -> None:
        self.bind("<Control-Return>", lambda _event: self.start_from_mode())
        self.bind("<Control-l>", lambda _event: self.focus_input())
        self.bind("<Control-L>", lambda _event: self.focus_input())

    def focus_input(self) -> str:
        if str(self.input_text.cget("state")) != "disabled":
            self.input_text.focus_set()
        return "break"

    def start_from_mode(self) -> None:
        self.start(single=self.run_mode_var.get() == "单个")

    def _mark_mode_manual(self) -> None:
        self.mode_manually_selected = True

    def toggle_advanced(self) -> None:
        visible = not self.advanced_visible.get()
        self.advanced_visible.set(visible)
        if visible:
            self.advanced_frame.grid(row=6, column=0, sticky="ew", pady=(10, 0))
            self.advanced_button.configure(text="隐藏高级设置")
        else:
            self.advanced_frame.grid_remove()
            self.advanced_button.configure(text="显示高级设置")

    def toggle_log_panel(self) -> None:
        visible = not self.log_visible.get()
        self.log_visible.set(visible)
        if visible:
            self.status_panel.rowconfigure(5, minsize=220)
            self.log_box.grid()
            self.log_toggle_button.configure(text="隐藏运行日志")
            self.after_idle(lambda: self.scroll_canvas.yview_moveto(1.0))
        else:
            self.status_panel.rowconfigure(5, minsize=0)
            self.log_box.grid_remove()
            self.log_toggle_button.configure(text="显示运行日志")

    def _update_detected_count(self) -> None:
        if self.feature_var.get() in {"收藏夹", "收藏视频", "收藏作品"}:
            self.detected_label.configure(text="收藏内容将按所选入口下载")
            return
        text = self.input_text.get("1.0", "end").strip()
        count = len(extract_task_inputs(self.platform_var.get(), text, single=False)) if text else 0
        if not self.mode_manually_selected:
            self.run_mode_var.set("批量" if count > 1 else "单个")
        self.detected_label.configure(text=f"已识别：{count} 条内容")

    def _on_platform_change(self) -> None:
        platform = self.platform_var.get()
        is_wechat = platform in {"微信", "微信公众号"}
        if is_wechat or platform == "知乎":
            self.feature_combo.configure(
                values=WECHAT_FEATURES if is_wechat else ARTICLE_FEATURES
            )
            self.feature_var.set(
                "自动识别（视频/图文）" if is_wechat else "文章正文（MD）"
            )
            self.download_cover_var.set(False)
            self.cover_only_var.set(False)
            self.login_douyin_button.grid_remove()
            self.login_xhs_button.grid_remove()
            self.login_bilibili_button.grid_remove()
            self.login_youtube_button.grid_remove()
            self.check_login_button.grid_remove()
            if platform == "知乎":
                self.login_zhihu_button.grid()
                self.login_status_label.configure(
                    text="知乎：优先复用当前 Chrome 会话；请求受限时可点击“登录知乎”"
                )
            else:
                self.login_zhihu_button.grid_remove()
                self.login_status_label.configure(
                    text="微信：自动识别视频号、公开文章和贴图；视频号选择原始最高质量"
                )
        elif platform == "TikTok":
            self.feature_combo.configure(values=TIKTOK_FEATURES)
            self.feature_var.set("视频媒体")
            self.run_mode_var.set("单个")
            self.login_douyin_button.grid_remove()
            self.login_xhs_button.grid_remove()
            self.login_bilibili_button.grid_remove()
            self.login_youtube_button.grid_remove()
            self.login_zhihu_button.grid_remove()
            self.check_login_button.grid_remove()
            self.login_status_label.configure(text="TikTok：下载公开单作品最高质量；私密或受限内容暂不支持")
        elif platform == "YouTube":
            self.feature_combo.configure(values=YOUTUBE_FEATURES)
            self.feature_var.set("视频媒体")
            self.login_douyin_button.grid_remove()
            self.login_xhs_button.grid_remove()
            self.login_bilibili_button.grid_remove()
            self.login_zhihu_button.grid_remove()
            self.login_youtube_button.grid()
            self.check_login_button.grid()
            auth_context = youtube.read_youtube_auth_context()
            if auth_context.get("error"):
                status = "YouTube：登录配置异常，请打开登录设置重新配置"
            elif auth_context.get("mode") == "firefox":
                status = "YouTube：已启用 Firefox 登录状态；按当前账号权限选择最高画质"
            elif auth_context.get("mode") == "chrome":
                status = "YouTube：尝试读取现有 Chrome 登录状态；失败时请导入 Cookie"
            elif auth_context.get("mode") == "edge":
                status = "YouTube：尝试读取现有 Edge 登录状态；失败时请导入 Cookie"
            elif auth_context.get("mode") == "cookie_file":
                status = "YouTube：已启用导入的 Cookie；按当前账号权限选择最高画质"
            else:
                status = "YouTube：匿名下载公开最高画质；遇到验证时可配置登录状态"
            self.login_status_label.configure(text=status)
        elif platform == "Bilibili":
            self.feature_combo.configure(values=BILIBILI_FEATURES)
            self.feature_var.set("视频媒体")
            self.login_douyin_button.grid_remove()
            self.login_xhs_button.grid_remove()
            self.login_youtube_button.grid_remove()
            self.login_zhihu_button.grid_remove()
            self.login_bilibili_button.grid()
            self.check_login_button.grid()
            self.login_status_label.configure(text="Bilibili：未登录下载公开最高画质；登录后按账号权限选择最高画质")
        elif platform == "小红书":
            self.feature_combo.configure(values=XHS_FEATURES)
            if self.feature_var.get() not in XHS_FEATURES:
                self.feature_var.set("作品媒体")
            self.login_douyin_button.grid_remove()
            self.login_xhs_button.grid()
            self.login_bilibili_button.grid_remove()
            self.login_youtube_button.grid_remove()
            self.login_zhihu_button.grid_remove()
            self.check_login_button.grid()
            self.login_status_label.configure(text="登录状态：需要下载收藏内容时请先登录小红书")
        else:
            self.feature_combo.configure(values=DOUYIN_FEATURES)
            if self.feature_var.get() not in DOUYIN_FEATURES:
                self.feature_var.set("作品媒体")
            self.login_douyin_button.grid()
            self.login_xhs_button.grid_remove()
            self.login_bilibili_button.grid_remove()
            self.login_youtube_button.grid_remove()
            self.login_zhihu_button.grid_remove()
            self.check_login_button.grid()
            self.login_status_label.configure(text="登录状态：需要下载收藏内容时请先登录抖音")
        self._on_feature_change()

    def _on_feature_change(self) -> None:
        is_xhs = self.platform_var.get() == "小红书"
        is_tiktok = self.platform_var.get() == "TikTok"
        is_article = self.platform_var.get() in {"微信", "微信公众号", "知乎"}
        is_media_only = self.platform_var.get() in {
            "Bilibili",
            "YouTube",
            "TikTok",
            "微信",
            "微信公众号",
            "知乎",
        }
        is_collection = self.feature_var.get() in {"收藏夹", "收藏视频", "收藏作品"}
        state = "readonly" if is_collection and not self.is_running else "disabled"
        entry_state = "normal" if is_collection and not is_xhs and not self.is_running else "disabled"
        if hasattr(self, "collection_limit_label"):
            self.collection_limit_label.configure(text="收藏作品上限" if is_xhs else "收藏夹作品上限")
        if hasattr(self, "collection_label_widget"):
            self.collection_label_widget.configure(text="收藏作品" if is_xhs else "收藏夹")
        self.refresh_collections_button.configure(text="刷新收藏作品" if is_xhs else "刷新收藏夹列表")
        self.collection_combo.configure(state=state)
        self.collection_id_entry.configure(state=entry_state)
        self.refresh_collections_button.configure(state="normal" if is_collection and not self.is_running else "disabled")
        if is_collection:
            self.collection_bar.grid()
            self.input_text.configure(state="disabled")
        else:
            self.collection_bar.grid_remove()
            self.input_text.configure(state="normal")
        regular_state = "disabled" if is_media_only or self.is_running else "normal"
        combo_state = "disabled" if is_media_only or self.is_running else "readonly"
        self.engine_combo.configure(state=combo_state)
        self.comment_limit_entry.configure(state=regular_state)
        self.collection_limit_entry.configure(state=regular_state)
        if is_article:
            self.download_cover_var.set(False)
            self.cover_only_var.set(False)
            self.download_cover_check.configure(state="disabled")
            self.cover_only_check.configure(state="disabled")
        elif not self.is_running:
            self.cover_only_check.configure(state="normal")
        if is_tiktok:
            self.run_mode_var.set("单个")
            self.mode_batch_radio.configure(state="disabled")
        elif not self.is_running:
            self.mode_batch_radio.configure(state="normal")
        self._update_detected_count()

    def _on_cover_only_change(self) -> None:
        cover_only = self.cover_only_var.get()
        is_article = self.platform_var.get() in {"微信", "微信公众号", "知乎"}
        if self.download_cover_check is not None:
            self.download_cover_check.configure(
                state="disabled" if cover_only or self.is_running or is_article else "normal"
            )
        if self.cover_only_check is not None:
            self.cover_only_check.configure(
                state="disabled" if self.is_running or is_article else "normal"
            )
        if not self.is_running:
            self.start_button.configure(text="下载封面" if cover_only else "开始下载")

    def paste_clipboard(self) -> None:
        try:
            text = self.clipboard_get()
        except tk.TclError:
            messagebox.showwarning("提示", "剪贴板里没有可粘贴的文本。")
            return
        self.input_text.configure(state="normal")
        self.input_text.delete("1.0", "end")
        self.input_text.insert("1.0", text)
        self._on_feature_change()
        self._update_detected_count()

    def copy_all_log(self) -> None:
        text = self.log_box.get("1.0", "end-1c")
        self._copy_text(text)
        self.status_label.configure(text="已复制完整日志")

    def copy_failure_log(self) -> None:
        text = self.log_box.get("1.0", "end-1c")
        keywords = ("失败", "错误", "异常", "Traceback", "Error", "Exception", "HTTP ", "验证", "登录")
        lines = [line for line in text.splitlines() if any(keyword in line for keyword in keywords)]
        summary = "\n".join(lines[-120:]) if lines else text[-4000:]
        self._copy_text(summary)
        self.status_label.configure(text="已复制失败摘要")

    def clear_log(self) -> None:
        self._set_log("")
        self.status_label.configure(text="日志已清空")

    def _copy_text(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)

    def select_all_log(self, _event: tk.Event | None = None) -> str:
        self.log_box.tag_add("sel", "1.0", "end-1c")
        self.log_box.mark_set("insert", "1.0")
        self.log_box.see("insert")
        return "break"

    def show_log_menu(self, event: tk.Event) -> str:
        self.log_menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def clear_all(self) -> None:
        if self.is_running:
            return
        self.input_text.configure(state="normal")
        self.input_text.delete("1.0", "end")
        self.collection_id_var.set("")
        self.collection_var.set("")
        self.mode_manually_selected = False
        self.run_mode_var.set("单个")
        self._set_log("")
        self._reset_stats()
        self._on_feature_change()
        self._update_detected_count()

    def open_douyin_login(self) -> None:
        try:
            profile_dir = douyin.open_douyin_login_browser()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打开失败", str(exc))
            return
        self._append_log(f"已打开抖音登录窗口。扫码登录一次后将复用：{profile_dir}\n")
        self.login_status_label.configure(text="登录状态：抖音登录窗口已打开")

    def open_xhs_login(self) -> None:
        try:
            browser_path = douyin.find_chromium_browser()
            if not browser_path:
                raise RuntimeError("未找到 Chrome 或 Edge。")
            profile_dir = xiaohongshu.xhs_browser_profile_dir()
            profile_dir.mkdir(parents=True, exist_ok=True)
            douyin.launch_chromium_cdp_browser(browser_path, profile_dir, visible=True, url="https://www.xiaohongshu.com/")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打开失败", str(exc))
            return
        self._append_log(f"已打开小红书登录窗口。登录态目录：{profile_dir}\n")
        self.login_status_label.configure(text="登录状态：小红书登录窗口已打开")

    def open_bilibili_login(self) -> None:
        try:
            profile_dir = bilibili.open_bilibili_login_browser()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打开失败", str(exc))
            return
        self._append_log(f"已打开 Bilibili 登录窗口。登录成功后将复用：{profile_dir}\n")
        self.login_status_label.configure(text="登录状态：Bilibili 登录窗口已打开")

    def open_zhihu_login(self) -> None:
        try:
            profile_dir = zhihu.open_zhihu_login_browser()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打开失败", str(exc))
            return
        self._append_log(f"已打开知乎登录窗口。登录一次后将复用：{profile_dir}\n")
        self.login_status_label.configure(text="知乎登录窗口已打开，登录完成后可直接重试下载")

    def open_youtube_auth_settings(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("YouTube 登录设置")
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.configure(bg="#ffffff")
        panel = ttk.Frame(dialog, style="PanelInner.TFrame", padding=(24, 22))
        panel.grid(row=0, column=0, sticky="nsew")
        panel.columnconfigure(0, weight=1)
        panel.columnconfigure(1, weight=1)
        ttk.Label(panel, text="YouTube 登录状态", style="PanelTitle.TLabel").grid(
            row=0,
            column=0,
            columnspan=2,
            sticky="w",
        )
        ttk.Label(
            panel,
            text=(
                "默认匿名下载公开内容。软件不会收集 Google 密码。\n"
                "遇到机器人验证时，推荐按教程导出 Cookie，再让软件自动查找并导入。"
            ),
            style="Muted.TLabel",
            justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 18))

        def open_external_url(url: str, label: str, parent: tk.Misc) -> None:
            try:
                opened = webbrowser.open(url, new=2)
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("无法打开链接", f"无法打开{label}：{exc}", parent=parent)
                return
            if not opened:
                messagebox.showwarning(
                    "未能自动打开",
                    f"系统没有成功打开{label}。\n请复制以下地址到浏览器：\n{url}",
                    parent=parent,
                )

        def finish_cookie_import(context: dict, windows: tuple[tk.Misc, ...]) -> None:
            for window in windows:
                try:
                    if window.winfo_exists():
                        window.destroy()
                except tk.TclError:
                    pass
            count = int(context.get("cookie_count") or 0)
            source_name = str(context.get("source_name") or "所选文件")
            self._append_log(
                f"已从 {source_name} 导入 YouTube/Google 相关 Cookie {count} 个；"
                "其他站点 Cookie 未保存。\n"
            )
            self.login_status_label.configure(text="YouTube：已导入 Cookie，建议检查登录状态")
            messagebox.showinfo(
                "导入成功",
                f"已导入 {count} 个 YouTube/Google 相关 Cookie。\n"
                "下一步请点击主界面的“检查登录状态”。",
                parent=self,
            )

        def use_firefox() -> None:
            try:
                youtube.open_youtube_login_in_firefox()
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("无法打开 Firefox", str(exc), parent=dialog)
                return
            dialog.destroy()
            self._append_log(
                "已用正常 Firefox 打开 YouTube。请在 Firefox 中完成登录并关闭窗口，"
                "然后回到软件点击“检查登录状态”。\n"
            )
            self.login_status_label.configure(text="YouTube：等待 Firefox 登录完成")
            messagebox.showinfo(
                "下一步",
                "请在 Firefox 中正常登录 YouTube。\n"
                "完成后关闭 Firefox，再回到软件点击“检查登录状态”。",
                parent=self,
            )

        def import_cookie_file() -> None:
            selected = filedialog.askopenfilename(
                parent=dialog,
                title="选择 YouTube cookies.txt",
                filetypes=(("Cookie 文本文件", "*.txt"), ("所有文件", "*.*")),
            )
            if not selected:
                return
            try:
                context = youtube.import_youtube_cookie_file(Path(selected))
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("导入失败", str(exc), parent=dialog)
                return
            finish_cookie_import({**context, "source_name": Path(selected).name}, (dialog,))

        def auto_import_cookie_file(parent: tk.Misc, windows: tuple[tk.Misc, ...]) -> None:
            try:
                context = youtube.auto_import_latest_youtube_cookie()
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror(
                    "自动导入失败",
                    f"{exc}\n\n软件只会检查桌面和下载目录顶层，不会扫描整台电脑。",
                    parent=parent,
                )
                return
            finish_cookie_import(context, windows)

        def open_cookie_guide() -> None:
            guide = tk.Toplevel(dialog)
            guide.title("YouTube Cookie 导出教程")
            guide.transient(dialog)
            guide.resizable(False, False)
            guide.configure(bg="#ffffff")
            guide_panel = ttk.Frame(guide, style="PanelInner.TFrame", padding=(26, 22))
            guide_panel.grid(row=0, column=0, sticky="nsew")
            guide_panel.columnconfigure(0, weight=1)
            guide_panel.columnconfigure(1, weight=1)

            ttk.Label(
                guide_panel,
                text="按这 5 步导出 YouTube Cookie",
                style="PanelTitle.TLabel",
            ).grid(row=0, column=0, columnspan=2, sticky="w")
            ttk.Label(
                guide_panel,
                text=(
                    "Cookie 相当于临时登录钥匙：只保存在自己的电脑，不要发给别人、"
                    "不要上传网盘或聊天窗口。账号下载存在触发限流的风险，非必要时优先匿名下载。"
                ),
                style="Muted.TLabel",
                justify="left",
                wraplength=760,
            ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 18))

            def add_step(grid_row: int, number: int, title: str, body: str) -> None:
                ttk.Label(
                    guide_panel,
                    text=f"{number}. {title}",
                    style="SectionTitle.TLabel",
                ).grid(row=grid_row, column=0, columnspan=2, sticky="w", pady=(8, 0))
                ttk.Label(
                    guide_panel,
                    text=body,
                    style="Muted.TLabel",
                    justify="left",
                    wraplength=760,
                ).grid(row=grid_row + 1, column=0, columnspan=2, sticky="w", pady=(4, 0))

            add_step(
                2,
                1,
                "安装正确的扩展",
                "点击下方按钮安装“Get cookies.txt LOCALLY”。名称必须带 LOCALLY；"
                "不要安装旧的同名扩展。安装后在扩展详情中开启“允许在无痕模式下运行”。",
            )
            link_row = ttk.Frame(guide_panel, style="PanelInner.TFrame")
            link_row.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 2))
            ttk.Button(
                link_row,
                text="打开官方扩展下载页",
                style="Primary.TButton",
                command=lambda: open_external_url(
                    youtube.YOUTUBE_COOKIE_EXTENSION_URL,
                    "Chrome 扩展商店",
                    guide,
                ),
            ).grid(row=0, column=0, sticky="w")
            ttk.Button(
                link_row,
                text="查看 yt-dlp 官方说明",
                style="Secondary.TButton",
                command=lambda: open_external_url(
                    youtube.YOUTUBE_COOKIE_EXPORT_GUIDE_URL,
                    "yt-dlp 官方教程",
                    guide,
                ),
            ).grid(row=0, column=1, sticky="w", padx=(10, 0))

            add_step(
                5,
                2,
                "打开无痕窗口并只保留一个标签页",
                "Chrome/Edge 按 Ctrl+Shift+N，新窗口中只留一个标签页。"
                "如果看不到扩展图标，请先到扩展管理页允许它在无痕模式运行。",
            )
            add_step(
                7,
                3,
                "在这个无痕标签页登录 YouTube",
                "打开 youtube.com 并正常登录。不要在这个无痕窗口继续浏览其他页面或打开第二个标签页。",
            )
            add_step(
                9,
                4,
                "同一标签页打开 robots.txt 并导出",
                "把下方地址粘贴到刚才同一个标签页。打开扩展后确认 Export Format 为 Netscape，"
                "点击“Export”，不要点“Export All Cookies”。文件可保存到桌面或下载目录。",
            )
            ttk.Button(
                guide_panel,
                text="复制 robots.txt 地址",
                style="Secondary.TButton",
                command=lambda: (
                    self._copy_text(youtube.YOUTUBE_ROBOTS_URL),
                    messagebox.showinfo(
                        "已复制",
                        "已复制 robots.txt 地址，请粘贴到刚才同一个无痕标签页。",
                        parent=guide,
                    ),
                ),
            ).grid(row=11, column=0, columnspan=2, sticky="w", pady=(8, 2))
            add_step(
                12,
                5,
                "关闭无痕窗口，再回软件自动导入",
                "导出后立即关闭整个无痕窗口。然后点击下方按钮；软件只检查桌面和下载目录中"
                "最近 48 小时的 .txt 文件，并只保存 YouTube/Google 相关 Cookie。",
            )

            action_row = ttk.Frame(guide_panel, style="PanelInner.TFrame")
            action_row.grid(row=14, column=0, columnspan=2, sticky="ew", pady=(18, 0))
            action_row.columnconfigure(0, weight=1)
            ttk.Button(
                action_row,
                text="自动查找并导入刚导出的 Cookie",
                style="Primary.TButton",
                command=lambda: auto_import_cookie_file(guide, (guide, dialog)),
            ).grid(row=0, column=0, sticky="ew")

            def close_guide() -> None:
                try:
                    guide.grab_release()
                except tk.TclError:
                    pass
                guide.destroy()
                if dialog.winfo_exists():
                    dialog.grab_set()
                    dialog.focus_set()

            ttk.Button(
                action_row,
                text="暂时关闭教程",
                style="Secondary.TButton",
                command=close_guide,
            ).grid(row=0, column=1, sticky="e", padx=(10, 0))
            guide.protocol("WM_DELETE_WINDOW", close_guide)
            dialog.grab_release()
            guide.grab_set()
            guide.focus_set()

        def use_existing_browser(browser: str, label: str) -> None:
            try:
                youtube.configure_browser_auth(browser)
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("设置失败", str(exc), parent=dialog)
                return
            dialog.destroy()
            self._append_log(
                f"已选择读取现有 {label} 登录状态。请确认该浏览器已登录 YouTube，"
                "完全关闭浏览器后点击“检查登录状态”。\n"
            )
            self.login_status_label.configure(text=f"YouTube：等待读取 {label} 登录状态")
            messagebox.showinfo(
                "下一步",
                f"请先确认日常使用的 {label} 已登录 YouTube，然后完全关闭 {label}。\n"
                "回到软件点击“检查登录状态”。\n\n"
                "如果提示无法复制或解密 Cookie，请改用 Firefox 或导入 cookies.txt；"
                "软件不会强制关闭浏览器，也不会要求管理员权限。",
                parent=self,
            )

        def use_anonymous() -> None:
            if not messagebox.askyesno(
                "清除 YouTube 登录状态",
                "将删除软件本地保存的 YouTube Cookie 和登录配置，继续使用匿名模式。是否继续？",
                parent=dialog,
            ):
                return
            youtube.clear_youtube_auth()
            dialog.destroy()
            self._append_log("已清除软件本地 YouTube 登录状态，切换为匿名模式。\n")
            self.login_status_label.configure(text="YouTube：匿名下载公开最高画质")

        ttk.Button(
            panel,
            text="打开 Cookie 导出教程（推荐）",
            style="Primary.TButton",
            command=open_cookie_guide,
        ).grid(row=2, column=0, columnspan=2, sticky="ew")
        ttk.Button(
            panel,
            text="自动查找并导入刚导出的 Cookie",
            style="Secondary.TButton",
            command=lambda: auto_import_cookie_file(dialog, (dialog,)),
        ).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(
            panel,
            text="手动选择 cookies.txt",
            style="Secondary.TButton",
            command=import_cookie_file,
        ).grid(row=4, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(
            panel,
            text="清除并使用匿名模式",
            style="Secondary.TButton",
            command=use_anonymous,
        ).grid(row=4, column=1, sticky="ew", padx=(10, 0), pady=(10, 0))
        ttk.Separator(panel, orient="horizontal").grid(
            row=5,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(18, 12),
        )
        ttk.Label(
            panel,
            text="其他方式（高级，可能受浏览器锁定或 Windows 加密影响）",
            style="Muted.TLabel",
        ).grid(row=6, column=0, columnspan=2, sticky="w")
        ttk.Button(
            panel,
            text="使用 Firefox 登录状态",
            style="Secondary.TButton",
            command=use_firefox,
        ).grid(row=7, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(
            panel,
            text="读取现有 Chrome（实验）",
            style="Secondary.TButton",
            command=lambda: use_existing_browser("chrome", "Chrome"),
        ).grid(row=8, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(
            panel,
            text="读取现有 Edge（实验）",
            style="Secondary.TButton",
            command=lambda: use_existing_browser("edge", "Edge"),
        ).grid(row=8, column=1, sticky="ew", padx=(10, 0), pady=(10, 0))
        ttk.Button(
            panel,
            text="关闭",
            style="Secondary.TButton",
            command=dialog.destroy,
        ).grid(row=9, column=0, columnspan=2, sticky="e", pady=(18, 0))
        dialog.grab_set()
        dialog.focus_set()

    def check_login_status(self) -> None:
        platform = self.platform_var.get()

        def worker() -> None:
            try:
                if platform == "YouTube":
                    context = youtube.inspect_youtube_auth_context()
                    if context.get("error"):
                        self.log_queue.put(("log", str(context["error"])))
                        self.log_queue.put(("login_status", "YouTube 登录状态不可用"))
                    elif context.get("mode") == "anonymous":
                        self.log_queue.put(("log", "YouTube 当前使用匿名模式，不读取任何账号 Cookie。"))
                        self.log_queue.put(("login_status", "YouTube 匿名模式"))
                    elif context.get("account_cookie_found"):
                        count = int(context.get("cookie_count") or 0)
                        self.log_queue.put(("log", f"YouTube 登录 Cookie 可读取：相关 Cookie {count} 个。"))
                        self.log_queue.put(("login_status", "YouTube 登录状态已就绪"))
                    else:
                        count = int(context.get("cookie_count") or 0)
                        self.log_queue.put(
                            (
                                "log",
                                f"YouTube Cookie 可读取 {count} 个，但未检测到账号登录凭据；"
                                "请确认浏览器已登录或重新导入。",
                            )
                        )
                        self.log_queue.put(("login_status", "YouTube 可能仍是游客状态"))
                elif platform == "Bilibili":
                    context = bilibili.read_bilibili_login_context()
                    if context.get("vip"):
                        self.log_queue.put(("log", "Bilibili 大会员登录态可用，可自动选择会员最高画质。"))
                        self.log_queue.put(("login_status", "Bilibili 大会员登录态可用"))
                    elif context.get("logged_in"):
                        self.log_queue.put(("log", "Bilibili 普通账号登录态可用，将选择账号可用最高画质。"))
                        self.log_queue.put(("login_status", "Bilibili 普通账号登录态可用"))
                    else:
                        self.log_queue.put(("log", "Bilibili 当前未登录，只能获取公开最高画质。"))
                        self.log_queue.put(("login_status", "Bilibili 未登录"))
                elif platform == "抖音":
                    context = read_douyin_login_context()
                    cookie_count = len([part for part in str(context.get("cookie") or "").split(";") if part.strip()])
                    if cookie_count and not context.get("loginRequired"):
                        self.log_queue.put(("log", f"抖音登录态可用：Cookie {cookie_count} 个"))
                        self.log_queue.put(("login_status", "抖音登录态可用"))
                    else:
                        self.log_queue.put(("log", "抖音登录态不可用或已失效，请点击“登录抖音”。"))
                        self.log_queue.put(("login_status", "抖音未登录或已失效"))
                else:
                    context = xiaohongshu.read_xhs_login_context()
                    state, message = xhs_login_state(context)
                    if state == "account":
                        self.log_queue.put(("log", message))
                        self.log_queue.put(("login_status", "小红书账号登录态可用"))
                    elif state == "guest":
                        self.log_queue.put(("log", message))
                        self.log_queue.put(("login_status", "小红书未验证账号登录"))
                    else:
                        self.log_queue.put(("log", message))
                        self.log_queue.put(("login_status", "小红书未登录或已失效"))
            except Exception as exc:  # noqa: BLE001
                self.log_queue.put(("log", f"检查登录状态失败：{exc}"))
                self.log_queue.put(("login_status", "登录状态检查失败"))

        threading.Thread(target=worker, daemon=True).start()

    def check_for_updates(self) -> None:
        if self.is_running or (self.update_worker and self.update_worker.is_alive()):
            return
        if self.update_button is not None:
            self.update_button.configure(state="disabled", text="正在检查更新…")
        self._append_log(f"正在检查更新：当前版本 v{APP_VERSION}\n")

        def worker() -> None:
            try:
                info = updater.check_for_update(APP_VERSION)
                self.log_queue.put(("update_checked", info))
            except Exception as exc:  # noqa: BLE001 - GUI needs a readable update failure.
                self.log_queue.put(("update_failed", str(exc)))

        self.update_worker = threading.Thread(target=worker, daemon=True)
        self.update_worker.start()

    def _show_update_result(self, info: updater.UpdateInfo) -> None:
        if self.update_button is not None:
            self.update_button.configure(state="normal", text=f"检查更新  v{APP_VERSION}")
        if not info.update_available:
            self._append_log(f"更新检查完成：当前已是最新版本 v{APP_VERSION}。\n")
            messagebox.showinfo(
                "已是最新版本",
                f"当前版本：v{APP_VERSION}\nGitHub 最新版本：v{info.latest_version}",
                parent=self,
            )
            return

        notes = info.notes.strip() or "这个版本没有填写更新说明。"
        if len(notes) > 1200:
            notes = notes[:1200].rstrip() + "…"
        asset = info.asset
        if asset is None or not asset.sha256:
            reason = "没有找到 Windows EXE" if asset is None else "缺少 SHA-256 校验值"
            self._append_log(f"发现 v{info.latest_version}，但无法安全自动安装：{reason}。\n")
            if messagebox.askyesno(
                "发现新版本",
                f"当前版本：v{APP_VERSION}\n最新版本：v{info.latest_version}\n\n"
                f"{notes}\n\n自动安装暂不可用（{reason}）。是否打开 GitHub 下载页面？",
                parent=self,
            ):
                webbrowser.open(info.release_url or updater.RELEASES_PAGE)
            return

        size_mb = asset.size / 1024 / 1024
        if not messagebox.askyesno(
            "发现新版本",
            f"当前版本：v{APP_VERSION}\n最新版本：v{info.latest_version}\n"
            f"下载大小：{size_mb:.1f} MB\n\n{notes}\n\n"
            "是否下载新版？下载完成并校验无误后，软件会再次询问是否安装。",
            parent=self,
        ):
            self._append_log("用户暂不下载新版。\n")
            return
        self._download_update(info)

    def _download_update(self, info: updater.UpdateInfo) -> None:
        if self.update_button is not None:
            self.update_button.configure(state="disabled", text="正在下载更新…")
        self._append_log(f"开始下载 v{info.latest_version}；完成后会核对文件大小和 SHA-256。\n")

        def progress(downloaded: int, total: int) -> None:
            self.log_queue.put(("update_progress", (downloaded, total)))

        def worker() -> None:
            try:
                staged = updater.download_update(info, APP_DIR, progress=progress)
                self.log_queue.put(("update_downloaded", (info, staged)))
            except Exception as exc:  # noqa: BLE001 - GUI needs a readable update failure.
                self.log_queue.put(("update_failed", str(exc)))

        self.update_worker = threading.Thread(target=worker, daemon=True)
        self.update_worker.start()

    def _confirm_update_install(self, info: updater.UpdateInfo, staged: Path) -> None:
        if self.update_button is not None:
            self.update_button.configure(state="normal", text=f"检查更新  v{APP_VERSION}")
        self._append_log(f"新版 v{info.latest_version} 下载并校验通过：{staged}\n")
        if not messagebox.askyesno(
            "安装更新",
            f"v{info.latest_version} 已下载并通过 SHA-256 校验。\n\n"
            "点击“是”后软件将关闭、替换 EXE 并重新打开。登录态、设置和下载结果不会被删除。\n"
            "旧版 EXE 会保留在“更新备份”中，启动失败时会自动恢复。",
            parent=self,
        ):
            self._append_log("新版已保存在更新临时文件中，用户暂未安装。\n")
            return
        try:
            updater.launch_update_helper(
                staged,
                UPDATE_HELPER_PATH,
                info.latest_version,
                current_version=APP_VERSION,
            )
        except Exception as exc:  # noqa: BLE001 - keep the running version intact.
            messagebox.showerror("无法安装更新", str(exc), parent=self)
            self._append_log(f"无法启动更新安装：{exc}\n")
            return
        self._append_log("更新程序已启动，正在关闭当前版本。\n")
        self.after(200, self.destroy)

    def refresh_collections(self) -> None:
        if self.is_running:
            return
        self.refresh_collections_button.configure(state="disabled")

        def worker() -> None:
            try:
                if self.platform_var.get() == "小红书":
                    collections = xiaohongshu.list_collections(log=lambda msg: self.log_queue.put(("log", msg)))
                else:
                    collections = list_collections(log=lambda msg: self.log_queue.put(("log", msg)))
                self.log_queue.put(("collections", collections))
            except Exception as exc:  # noqa: BLE001
                label = "收藏作品" if self.platform_var.get() == "小红书" else "收藏夹"
                self.log_queue.put(("log", f"刷新{label}失败：{exc}"))
                self.log_queue.put(("collections_done", None))

        threading.Thread(target=worker, daemon=True).start()

    def _select_collection(self) -> None:
        value = self.collection_var.get()
        for item in self.collections:
            label = collection_label(item)
            if label == value:
                self.collection_id_var.set(str(item["id"]))
                return

    def start(self, single: bool) -> None:
        if self.is_running:
            return
        try:
            options = self._build_options(single)
        except ValueError as exc:
            messagebox.showwarning("提示", str(exc))
            return

        try:
            options.output_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(
                "无法创建下载目录",
                f"无法在所选位置创建“{OUTPUT_DIR_NAME}”文件夹：\n{options.output_root}\n\n{exc}",
            )
            return
        self._set_buttons_state("disabled")
        reset_cancellation()
        self._reset_stats(total=1 if options.feature in {"收藏夹", "收藏视频", "收藏作品"} else len(options.inputs))
        self.empty_state_label.configure(text="任务运行中，请等待下载完成。")
        if options.platform in {"微信", "微信公众号", "知乎"}:
            cover_mode = "不适用"
        else:
            cover_mode = "仅下载封面" if options.cover_only else ("同时保存" if options.download_cover else "不保存")
        organization_mode = "按博主分类" if options.organize_by_author else "全部平铺"
        self._append_log(
            f"开始任务：平台={options.platform}，功能={options.feature}，"
            f"封面模式={cover_mode}，保存方式={organization_mode}\n"
            f"输出根目录：{options.output_root}\n"
        )
        if options.cover_only:
            self._append_log("仅封面模式已启用：本次不会下载视频、作品图片或评论内容。\n")
        self.worker = threading.Thread(target=self._run_worker, args=(options,), daemon=True)
        self.worker.start()

    def _build_options(self, single: bool) -> TaskOptions:
        platform = self.platform_var.get()
        feature = self.feature_var.get()
        inputs: list[str] = []
        collection_id = self.collection_id_var.get().strip()
        collection_name = self.collection_var.get().strip().split("  ID:", 1)[0]
        is_collection = feature in {"收藏夹", "收藏视频", "收藏作品"}
        if not is_collection:
            text = self.input_text.get("1.0", "end").strip()
            if not text:
                raise ValueError("请先粘贴分享文本或链接。")
            inputs = extract_task_inputs(platform, text, single=single)
            if not inputs:
                raise ValueError(f"没有识别到{platform}链接。")
        elif platform == "小红书":
            collection_id = collection_id or "__all_favorites__"
            collection_name = collection_name or "全部收藏作品"
        elif not collection_id:
            raise ValueError("请先刷新并选择收藏夹，或手动输入收藏夹 ID。")

        article_platform = platform in {"微信", "微信公众号", "知乎"}
        cover_only = False if article_platform else self.cover_only_var.get()
        return TaskOptions(
            platform=platform,
            feature=feature,
            inputs=inputs,
            output_root=self.output_root,
            download_engine=self.engine_var.get(),
            max_workers=self._selected_workers(),
            comment_limit=parse_positive_int(self.comment_limit_var.get(), "评论图片数量"),
            collection_limit=parse_positive_int(self.collection_limit_var.get(), "收藏作品数量" if platform == "小红书" else "收藏夹作品数量"),
            collection_id=collection_id,
            collection_name=collection_name,
            download_cover=False if article_platform else self.download_cover_var.get() or cover_only,
            cover_only=cover_only,
            organize_by_author=self.organize_by_author_var.get(),
        )

    def _selected_workers(self) -> int:
        speed = self.speed_var.get()
        if speed == "stable":
            return 2
        if speed == "fast":
            return 8
        return 4

    def _run_worker(self, options: TaskOptions) -> None:
        try:
            report = run_task(options, log=lambda msg: self.log_queue.put(("log", msg)))
            self.last_output_dir = report.get("output_dir")
            self.log_queue.put(("task_success", report))
        except DownloadCancelled as exc:
            self.log_queue.put(("task_cancelled", str(exc)))
        except (DouyinCollectionError, Exception) as exc:  # noqa: BLE001
            self.log_queue.put(("task_failed", str(exc)))
        self.log_queue.put(("all_done", None))

    def cancel_current_task(self) -> None:
        if not self.is_running:
            return
        if request_cancellation():
            self._append_log(
                "已请求终止：软件会停止后续解析和下载；正在等待的网络请求返回后退出。\n"
            )
        self.start_button.configure(state="disabled", text="正在终止…")

    def _drain_log_queue(self) -> None:
        try:
            while True:
                event, payload = self.log_queue.get_nowait()
                if event == "log":
                    self._append_log(str(payload) + "\n")
                elif event == "collections":
                    self.collections = list(payload or [])  # type: ignore[arg-type]
                    values = [collection_label(item) for item in self.collections]
                    self.collection_combo.configure(values=values)
                    if values:
                        self.collection_var.set(values[0])
                        self._select_collection()
                    label = "收藏作品入口" if self.platform_var.get() == "小红书" else "收藏夹列表"
                    self._append_log(f"{label}已刷新：{len(values)} 个\n")
                    self._on_feature_change()
                elif event == "collections_done":
                    self._on_feature_change()
                elif event == "login_status":
                    self.login_status_label.configure(text=f"登录状态：{payload}")
                elif event == "update_checked" and isinstance(payload, updater.UpdateInfo):
                    self._show_update_result(payload)
                elif event == "update_progress" and isinstance(payload, tuple) and len(payload) == 2:
                    downloaded, total = (int(payload[0]), int(payload[1]))
                    percent = 0 if total <= 0 else min(100, int(downloaded / total * 100))
                    if self.update_button is not None:
                        self.update_button.configure(text=f"正在下载更新 {percent}%")
                elif event == "update_downloaded" and isinstance(payload, tuple) and len(payload) == 2:
                    info, staged = payload
                    if isinstance(info, updater.UpdateInfo):
                        self._confirm_update_install(info, Path(staged))
                elif event == "update_failed":
                    if self.update_button is not None:
                        self.update_button.configure(state="normal", text=f"检查更新  v{APP_VERSION}")
                    self._append_log(f"更新失败：{payload}\n")
                    messagebox.showerror("更新失败", str(payload), parent=self)
                elif event == "task_success":
                    report = payload if isinstance(payload, dict) else {}
                    failures = report.get("failures") if isinstance(report.get("failures"), list) else []
                    cover_failures = (
                        report.get("cover_failures")
                        if isinstance(report.get("cover_failures"), list)
                        else []
                    )
                    self.finished_tasks = self.total_tasks
                    self.success_tasks = max(0, self.total_tasks - len(failures))
                    self.failed_tasks = len(failures)
                    self.cover_failed_tasks = len(cover_failures)
                    if cover_failures:
                        self._append_log(
                            f"封面获取警告：{len(cover_failures)} 个作品未能保存封面；"
                            "媒体任务结果不受影响，请查看上方具体错误。\n"
                        )
                    self._refresh_stats()
                elif event == "task_failed":
                    self.finished_tasks = self.total_tasks
                    self.failed_tasks = max(1, self.total_tasks)
                    self._append_log(f"任务失败：{payload}\n")
                    self._refresh_stats()
                elif event == "task_cancelled":
                    self._append_log(f"{payload}\n")
                    self.status_label.configure(text="任务已终止")
                    self.empty_state_label.configure(text="下载已终止，可以修改设置后重新开始。")
                elif event == "all_done":
                    self._set_buttons_state("normal")
                    if self.status_label.cget("text") == "任务已终止":
                        continue
                    cover_suffix = (
                        f"，封面警告 {self.cover_failed_tasks}"
                        if self.cover_failed_tasks
                        else ""
                    )
                    self.status_label.configure(
                        text=f"完成：成功 {self.success_tasks}，失败 {self.failed_tasks}{cover_suffix}"
                    )
                    self.empty_state_label.configure(text="任务完成，可打开输出文件夹或复制日志。")
                    messagebox.showinfo(
                        "完成",
                        f"任务完成：成功 {self.success_tasks}，失败 {self.failed_tasks}{cover_suffix}",
                    )
        except queue.Empty:
            pass
        self.after(100, self._drain_log_queue)

    def open_output_dir(self) -> None:
        target = Path(self.last_output_dir) if self.last_output_dir else self.output_root
        try:
            target.mkdir(parents=True, exist_ok=True)
            os.startfile(target)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("无法打开下载目录", f"无法打开：\n{target}\n\n{exc}")

    def choose_output_parent(self) -> None:
        if self.is_running:
            return
        selected = filedialog.askdirectory(
            parent=self,
            title=f"选择保存位置（将在其中创建“{OUTPUT_DIR_NAME}”文件夹）",
            initialdir=str(self.output_parent),
            mustexist=True,
        )
        if not selected:
            return

        parent = Path(selected).expanduser().resolve()
        output_root = output_root_for_parent(parent)
        try:
            output_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(
                "无法使用所选位置",
                f"无法在所选位置创建“{OUTPUT_DIR_NAME}”文件夹：\n{output_root}\n\n{exc}",
            )
            return

        self.output_parent = parent
        self.output_root = output_root
        self.last_output_dir = None
        self.output_path_label.configure(text=str(self.output_root))
        self._append_log(f"输出位置已更改：{self.output_root}\n")
        try:
            save_output_parent(parent)
        except OSError as exc:
            messagebox.showwarning(
                "位置已更改",
                f"本次会话将使用所选位置，但无法保存该设置：\n{exc}",
            )

    def _on_output_organization_change(self) -> None:
        if self.is_running:
            return
        enabled = self.organize_by_author_var.get()
        label = "按博主分类" if enabled else "全部放在下载结果"
        self.organization_hint_label.configure(text=self._organization_hint_text())
        self._append_log(f"保存方式已切换：{label}\n")
        try:
            save_organize_by_author(enabled)
        except OSError as exc:
            messagebox.showwarning(
                "保存方式已切换",
                f"本次会话将使用“{label}”，但无法保存该设置：\n{exc}",
            )

    def _organization_hint_text(self) -> str:
        if self.organize_by_author_var.get():
            return "同一博主的作品会自动收纳到同名文件夹"
        return "所有作品直接保存在下载结果根目录"

    def _reset_stats(self, total: int = 0) -> None:
        self.total_tasks = total
        self.finished_tasks = 0
        self.success_tasks = 0
        self.failed_tasks = 0
        self.cover_failed_tasks = 0
        self._refresh_stats()
        self.status_label.configure(text="等待任务" if total == 0 else "准备开始")
        self.empty_state_label.configure(text="还没有任务，粘贴链接后点击开始下载。" if total == 0 else "任务已创建，等待执行。")

    def _refresh_stats(self) -> None:
        self.total_value.configure(text=str(self.total_tasks))
        self.done_value.configure(text=str(self.finished_tasks))
        self.success_value.configure(text=str(self.success_tasks))
        self.failed_value.configure(text=str(self.failed_tasks))
        value = 0 if self.total_tasks == 0 else int(self.finished_tasks / self.total_tasks * 100)
        self.progress.configure(value=value)

    def _set_buttons_state(self, state: str) -> None:
        readonly = "readonly" if state == "normal" else "disabled"
        self.start_button.configure(
            state="normal",
            command=self.start_from_mode if state == "normal" else self.cancel_current_task,
            text=(
                "下载封面"
                if state == "normal" and self.cover_only_var.get()
                else "开始下载"
                if state == "normal"
                else "终止下载"
            ),
        )
        self.mode_batch_radio.configure(state=state)
        self.mode_single_radio.configure(state=state)
        for button in self.platform_buttons:
            button.configure(state=state)
        self.platform_combo.configure(state=readonly)
        self.feature_combo.configure(state=readonly)
        self.engine_combo.configure(state=readonly)
        self.speed_combo.configure(state=readonly)
        self.advanced_button.configure(state=state)
        if self.download_cover_check is not None:
            self.download_cover_check.configure(state=state)
        if self.cover_only_check is not None:
            self.cover_only_check.configure(state=state)
        if self.author_folder_radio is not None:
            self.author_folder_radio.configure(state=state)
        if self.flat_output_radio is not None:
            self.flat_output_radio.configure(state=state)
        self.comment_limit_entry.configure(state=state)
        self.collection_limit_entry.configure(state=state)
        for button in (
            self.paste_button,
            self.clear_button,
            self.login_douyin_button,
            self.login_xhs_button,
            self.login_bilibili_button,
            self.login_youtube_button,
            self.login_zhihu_button,
            self.check_login_button,
            self.update_button,
        ):
            if button is not None:
                button.configure(state=state)
        if self.open_output_button is not None:
            self.open_output_button.configure(state="normal")
        if self.choose_output_button is not None:
            self.choose_output_button.configure(state=state)
        for button in (self.copy_failure_button, self.copy_all_button, self.clear_log_button):
            if button is not None:
                button.configure(state="normal")
        self._on_feature_change()
        if state == "normal":
            self._on_cover_only_change()
        else:
            if self.download_cover_check is not None:
                self.download_cover_check.configure(state="disabled")
            if self.cover_only_check is not None:
                self.cover_only_check.configure(state="disabled")

    def _append_log(self, text: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _set_log(self, text: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        if text:
            self.log_box.insert("end", text)
        self.log_box.configure(state="disabled")


def parse_positive_int(value: str, label: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{label}必须是正整数，或留空表示尽量全部。") from exc
    if parsed <= 0:
        raise ValueError(f"{label}必须是正整数，或留空表示尽量全部。")
    return parsed


def collection_label(item: dict) -> str:
    count = item.get("count")
    suffix = f"  {count}个作品" if count not in (None, "") else ""
    return f"{item.get('name', '未命名收藏夹')}{suffix}  ID:{item.get('id', '')}"


if __name__ == "__main__":
    UnifiedDownloaderApp().mainloop()
