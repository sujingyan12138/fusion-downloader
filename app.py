from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

from downloaders import douyin, xiaohongshu
from downloaders.douyin_collection import DouyinCollectionError, list_collections, read_douyin_login_context
from services.task_runner import TaskOptions, extract_task_inputs, run_task


if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent

OUTPUT_ROOT = APP_DIR / "下载结果"
DOUYIN_FEATURES = ("作品媒体", "评论区图片", "作品媒体+评论区图片", "收藏夹")
XHS_FEATURES = ("作品媒体", "评论区图片", "作品媒体+评论区图片", "收藏作品")


class UnifiedDownloaderApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("融合下载器")
        self.geometry("1120x780")
        self.minsize(980, 700)
        self.configure(bg="#f6f8fb")

        self.log_queue: queue.Queue[tuple[str, object | None]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.collections: list[dict] = []
        self.last_output_dir: str | None = None
        self.total_tasks = 0
        self.finished_tasks = 0
        self.success_tasks = 0
        self.failed_tasks = 0

        self.platform_var = tk.StringVar(value="抖音")
        self.feature_var = tk.StringVar(value="作品媒体")
        self.comment_limit_var = tk.StringVar(value="")
        self.collection_limit_var = tk.StringVar(value="")
        self.collection_id_var = tk.StringVar(value="")
        self.collection_var = tk.StringVar(value="")
        self.engine_var = tk.StringVar(value="smart")
        self.speed_var = tk.StringVar(value="balanced")
        self.run_mode_var = tk.StringVar(value="单个")
        self.mode_manually_selected = False
        self.advanced_visible = tk.BooleanVar(value=False)
        self.log_visible = tk.BooleanVar(value=False)
        self.login_douyin_button: ttk.Button | None = None
        self.login_xhs_button: ttk.Button | None = None
        self.check_login_button: ttk.Button | None = None
        self.open_output_button: ttk.Button | None = None
        self.paste_button: ttk.Button | None = None
        self.clear_button: ttk.Button | None = None
        self.copy_failure_button: ttk.Button | None = None
        self.copy_all_button: ttk.Button | None = None
        self.clear_log_button: ttk.Button | None = None

        self._setup_style()
        self._build_ui()
        self._bind_shortcuts()
        self._on_platform_change()
        self.after(100, self._drain_log_queue)

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        base_font = ("Microsoft YaHei UI", 10)
        title_font = ("Microsoft YaHei UI", 28, "bold")
        style.configure(".", font=base_font)
        style.configure("Root.TFrame", background="#f6f8fb")
        style.configure("Topbar.TFrame", background="#f6f8fb")
        style.configure("Panel.TFrame", background="#ffffff", relief="solid", borderwidth=1)
        style.configure("PanelInner.TFrame", background="#ffffff", relief="flat")
        style.configure("Advanced.TFrame", background="#f8fafd", relief="solid", borderwidth=1)
        style.configure("StatCard.TFrame", background="#f8fafd", relief="solid", borderwidth=1)
        style.configure("Title.TLabel", background="#f6f8fb", foreground="#172033", font=title_font)
        style.configure("Subtitle.TLabel", background="#f6f8fb", foreground="#6e7788", font=("Microsoft YaHei UI", 10))
        style.configure("PanelTitle.TLabel", background="#ffffff", foreground="#172033", font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("SectionTitle.TLabel", background="#ffffff", foreground="#172033", font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("Muted.TLabel", background="#ffffff", foreground="#6e7788", font=("Microsoft YaHei UI", 9))
        style.configure("Hint.TLabel", background="#ffffff", foreground="#6e7788", font=("Microsoft YaHei UI", 9))
        style.configure("AdvancedMuted.TLabel", background="#f8fafd", foreground="#6e7788", font=("Microsoft YaHei UI", 9))
        style.configure("AdvancedHint.TLabel", background="#f8fafd", foreground="#6e7788", font=("Microsoft YaHei UI", 9))
        style.configure("Status.TLabel", background="#ffffff", foreground="#303848", font=("Microsoft YaHei UI", 10))
        style.configure("Success.TLabel", background="#ffffff", foreground="#0e9f8a", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Danger.TLabel", background="#ffffff", foreground="#d64545", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("StatValue.TLabel", background="#f8fafd", foreground="#172033", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("SuccessStatValue.TLabel", background="#f8fafd", foreground="#0e9f8a", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("DangerStatValue.TLabel", background="#f8fafd", foreground="#d64545", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("StatLabel.TLabel", background="#f8fafd", foreground="#6e7788", font=("Microsoft YaHei UI", 9))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 11, "bold"), padding=(22, 11), borderwidth=0)
        style.map(
            "Primary.TButton",
            background=[("disabled", "#c8d1e0"), ("active", "#1f4fd1"), ("!disabled", "#2f6bff")],
            foreground=[("disabled", "#ffffff"), ("!disabled", "#ffffff")],
        )
        style.configure("Secondary.TButton", font=("Microsoft YaHei UI", 10), padding=(13, 9), borderwidth=1)
        style.map(
            "Secondary.TButton",
            background=[("disabled", "#f2f5f9"), ("active", "#eef4ff"), ("!disabled", "#ffffff")],
            foreground=[("disabled", "#a2adbd"), ("!disabled", "#303848")],
        )
        style.configure("TCombobox", padding=(10, 7), fieldbackground="#ffffff", background="#ffffff", foreground="#172033", bordercolor="#d8dee8", arrowcolor="#6e7788")
        style.map("TCombobox", fieldbackground=[("readonly", "#ffffff")], bordercolor=[("focus", "#2f6bff")])
        style.configure("TEntry", padding=(10, 8), fieldbackground="#ffffff", foreground="#172033", bordercolor="#d8dee8")
        style.map("TEntry", bordercolor=[("focus", "#2f6bff")])
        style.configure("TRadiobutton", background="#ffffff", foreground="#303848", font=("Microsoft YaHei UI", 10))
        style.map("TRadiobutton", foreground=[("disabled", "#a2adbd"), ("!disabled", "#303848")])
        style.configure("Horizontal.TProgressbar", troughcolor="#e7edf5", background="#2f6bff", bordercolor="#e7edf5", lightcolor="#2f6bff", darkcolor="#2f6bff")

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.scroll_canvas = tk.Canvas(self, bg="#f6f8fb", highlightthickness=0, borderwidth=0)
        self.page_scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.scroll_canvas.yview)
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

        header = ttk.Frame(page, style="Topbar.TFrame", padding=(28, 20, 28, 12))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        header.columnconfigure(1, weight=0)
        ttk.Label(header, text="融合下载器", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="选择来源，粘贴内容，开始下载。抖音与小红书作品、评论图片、收藏夹与专辑统一处理。", style="Subtitle.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.open_output_button = ttk.Button(header, text="打开输出文件夹", style="Secondary.TButton", command=self.open_output_dir)
        self.open_output_button.grid(row=0, column=1, rowspan=2, sticky="e")

        input_panel = ttk.Frame(page, style="Panel.TFrame", padding=(24, 20, 24, 20))
        input_panel.grid(row=1, column=0, sticky="ew", padx=28, pady=(4, 14))
        input_panel.columnconfigure(0, weight=1)

        ttk.Label(input_panel, text="任务设置", style="SectionTitle.TLabel").grid(row=0, column=0, sticky="w")
        selectors = ttk.Frame(input_panel, style="PanelInner.TFrame")
        selectors.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        for column in (1, 3, 5):
            selectors.columnconfigure(column, weight=1)
        ttk.Label(selectors, text="平台", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.platform_combo = ttk.Combobox(selectors, textvariable=self.platform_var, state="readonly", values=("抖音", "小红书"), width=12)
        self.platform_combo.grid(row=0, column=1, padx=(8, 18), sticky="ew")
        self.platform_combo.bind("<<ComboboxSelected>>", lambda _event: self._on_platform_change())

        ttk.Label(selectors, text="功能", style="Muted.TLabel").grid(row=0, column=2, sticky="w")
        self.feature_combo = ttk.Combobox(selectors, textvariable=self.feature_var, state="readonly", values=DOUYIN_FEATURES, width=20)
        self.feature_combo.grid(row=0, column=3, padx=(8, 18), sticky="ew")
        self.feature_combo.bind("<<ComboboxSelected>>", lambda _event: self._on_feature_change())

        ttk.Label(selectors, text="模式", style="Muted.TLabel").grid(row=0, column=4, sticky="w")
        mode_frame = ttk.Frame(selectors, style="PanelInner.TFrame")
        mode_frame.grid(row=0, column=5, sticky="ew", padx=(8, 0))
        self.mode_batch_radio = ttk.Radiobutton(mode_frame, text="批量", variable=self.run_mode_var, value="批量", command=self._mark_mode_manual)
        self.mode_batch_radio.grid(row=0, column=0, sticky="w")
        self.mode_single_radio = ttk.Radiobutton(mode_frame, text="单个", variable=self.run_mode_var, value="单个", command=self._mark_mode_manual)
        self.mode_single_radio.grid(row=0, column=1, sticky="w", padx=(14, 0))

        collection_bar = ttk.Frame(input_panel, style="PanelInner.TFrame")
        collection_bar.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        collection_bar.columnconfigure(1, weight=1)
        self.collection_label_widget = ttk.Label(collection_bar, text="收藏夹", style="Muted.TLabel")
        self.collection_label_widget.grid(row=0, column=0, sticky="w")
        self.collection_combo = ttk.Combobox(collection_bar, textvariable=self.collection_var, state="readonly", values=(), width=32)
        self.collection_combo.grid(row=0, column=1, padx=(8, 10), sticky="ew")
        self.collection_combo.bind("<<ComboboxSelected>>", lambda _event: self._select_collection())
        self.refresh_collections_button = ttk.Button(collection_bar, text="刷新收藏夹列表", style="Secondary.TButton", command=self.refresh_collections)
        self.refresh_collections_button.grid(row=0, column=2, sticky="w")

        advanced_header = ttk.Frame(input_panel, style="PanelInner.TFrame")
        advanced_header.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        advanced_header.columnconfigure(0, weight=1)
        ttk.Label(advanced_header, text="普通任务无需调整高级设置", style="Hint.TLabel").grid(row=0, column=0, sticky="w")
        self.advanced_button = ttk.Button(advanced_header, text="高级设置 ▾", style="Secondary.TButton", command=self.toggle_advanced)
        self.advanced_button.grid(row=0, column=1, sticky="e")

        self.advanced_frame = ttk.Frame(input_panel, style="Advanced.TFrame", padding=(16, 14, 16, 14))
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
        ttk.Label(self.advanced_frame, text="留空表示尽量全部；网络不稳时建议 balanced 或 stable。", style="AdvancedHint.TLabel").grid(row=2, column=2, columnspan=2, sticky="w", pady=(12, 0))

        input_header = ttk.Frame(input_panel, style="PanelInner.TFrame")
        input_header.grid(row=5, column=0, sticky="ew", pady=(18, 0))
        input_header.columnconfigure(1, weight=1)
        ttk.Label(input_header, text="输入链接 / 分享文案", style="PanelTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.detected_label = ttk.Label(input_header, text="已识别：0 条内容", style="Hint.TLabel")
        self.detected_label.grid(row=0, column=1, sticky="e")
        self.input_text = tk.Text(
            input_panel,
            height=7,
            wrap="word",
            font=("Microsoft YaHei UI", 10),
            bg="#fbfcfe",
            fg="#172033",
            insertbackground="#2f6bff",
            relief="flat",
            padx=16,
            pady=14,
            highlightthickness=1,
            highlightbackground="#d8dee8",
            highlightcolor="#2f6bff",
        )
        self.input_text.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        self.input_text.bind("<KeyRelease>", lambda _event: self._update_detected_count())

        action_bar = ttk.Frame(input_panel, style="PanelInner.TFrame")
        action_bar.grid(row=7, column=0, sticky="ew", pady=(16, 0))
        action_bar.columnconfigure(0, weight=1)
        action_bar.columnconfigure(1, weight=0)
        self.login_status_label = ttk.Label(action_bar, text="登录状态：需要下载收藏内容时请先登录", style="Hint.TLabel")
        self.login_status_label.grid(row=0, column=0, sticky="w")
        controls = ttk.Frame(action_bar, style="PanelInner.TFrame")
        controls.grid(row=0, column=1, sticky="e")
        self.paste_button = ttk.Button(controls, text="粘贴", style="Secondary.TButton", command=self.paste_clipboard)
        self.paste_button.grid(row=0, column=0, sticky="w")
        self.clear_button = ttk.Button(controls, text="清空", style="Secondary.TButton", command=self.clear_all)
        self.clear_button.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.login_douyin_button = ttk.Button(controls, text="登录抖音", style="Secondary.TButton", command=self.open_douyin_login)
        self.login_douyin_button.grid(row=0, column=2, sticky="w", padx=(8, 0))
        self.login_xhs_button = ttk.Button(controls, text="登录小红书", style="Secondary.TButton", command=self.open_xhs_login)
        self.login_xhs_button.grid(row=0, column=3, sticky="w", padx=(8, 0))
        self.check_login_button = ttk.Button(controls, text="检查登录状态", style="Secondary.TButton", command=self.check_login_status)
        self.check_login_button.grid(row=0, column=4, sticky="w", padx=(8, 0))
        self.start_button = ttk.Button(controls, text="开始下载", style="Primary.TButton", command=self.start_from_mode)
        self.start_button.grid(row=0, column=5, sticky="e", padx=(16, 0))

        self.status_panel = ttk.Frame(page, style="Panel.TFrame", padding=(24, 18, 24, 18))
        status_panel = self.status_panel
        status_panel.grid(row=2, column=0, sticky="nsew", padx=28, pady=(0, 18))
        status_panel.columnconfigure(0, weight=1)
        status_panel.rowconfigure(4, weight=1, minsize=0)
        feedback_header = ttk.Frame(status_panel, style="PanelInner.TFrame")
        feedback_header.grid(row=0, column=0, sticky="ew")
        feedback_header.columnconfigure(0, weight=1)
        ttk.Label(feedback_header, text="任务反馈", style="SectionTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.empty_state_label = ttk.Label(feedback_header, text="还没有任务，粘贴链接后点击开始下载。", style="Hint.TLabel")
        self.empty_state_label.grid(row=0, column=1, sticky="e")
        stats = ttk.Frame(status_panel, style="PanelInner.TFrame")
        stats.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        for i in range(4):
            stats.columnconfigure(i, weight=1)
        self.total_value = self._stat_card(stats, 0, "总任务", "0")
        self.done_value = self._stat_card(stats, 1, "已完成", "0")
        self.success_value = self._stat_card(stats, 2, "成功", "0", "SuccessStatValue.TLabel")
        self.failed_value = self._stat_card(stats, 3, "失败", "0", "DangerStatValue.TLabel")

        progress_line = ttk.Frame(status_panel, style="PanelInner.TFrame")
        progress_line.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        progress_line.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_line, mode="determinate", maximum=100, value=0)
        self.progress.grid(row=0, column=0, sticky="ew")
        self.status_label = ttk.Label(progress_line, text="等待任务", style="Muted.TLabel")
        self.status_label.grid(row=1, column=0, sticky="w", pady=(8, 0))

        log_header = ttk.Frame(status_panel, style="PanelInner.TFrame")
        log_header.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        log_header.columnconfigure(0, weight=1)
        ttk.Label(log_header, text="运行日志", style="PanelTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.log_toggle_button = ttk.Button(log_header, text="展开日志 ▾", style="Secondary.TButton", command=self.toggle_log_panel)
        self.log_toggle_button.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.copy_failure_button = ttk.Button(log_header, text="复制失败摘要", style="Secondary.TButton", command=self.copy_failure_log)
        self.copy_failure_button.grid(row=0, column=2, sticky="e", padx=(8, 0))
        self.copy_all_button = ttk.Button(log_header, text="复制完整日志", style="Secondary.TButton", command=self.copy_all_log)
        self.copy_all_button.grid(row=0, column=3, sticky="e", padx=(8, 0))
        self.clear_log_button = ttk.Button(log_header, text="清空日志", style="Secondary.TButton", command=self.clear_log)
        self.clear_log_button.grid(row=0, column=4, sticky="e", padx=(8, 0))
        self.log_box = scrolledtext.ScrolledText(
            status_panel,
            wrap="word",
            state="disabled",
            height=12,
            font=("Consolas", 10),
            bg="#20242d",
            fg="#eef2f8",
            insertbackground="#2f6bff",
            relief="flat",
            padx=14,
            pady=12,
            highlightthickness=1,
            highlightbackground="#303848",
            highlightcolor="#435066",
        )
        self.log_box.grid(row=4, column=0, sticky="nsew", pady=(8, 0))
        self.log_box.grid_remove()
        self.log_menu = tk.Menu(self, tearoff=0)
        self.log_menu.add_command(label="复制选中内容", command=lambda: self.log_box.event_generate("<<Copy>>"))
        self.log_menu.add_command(label="复制完整日志", command=self.copy_all_log)
        self.log_menu.add_command(label="复制失败摘要", command=self.copy_failure_log)
        self.log_menu.add_separator()
        self.log_menu.add_command(label="全选", command=self.select_all_log)
        self.log_box.bind("<Button-3>", self.show_log_menu)
        self.log_box.bind("<Control-a>", self.select_all_log)

        footer = ttk.Frame(page, style="Root.TFrame", padding=(28, 0, 28, 14))
        footer.grid(row=3, column=0, sticky="ew")
        ttk.Label(footer, text=f"输出根目录：{OUTPUT_ROOT}", style="Subtitle.TLabel").grid(row=0, column=0, sticky="w")

    def _stat_card(self, parent: ttk.Frame, column: int, label: str, value: str, value_style: str = "StatValue.TLabel") -> ttk.Label:
        card = ttk.Frame(parent, style="StatCard.TFrame", padding=(18, 10, 18, 10))
        card.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 10, 0))
        value_label = ttk.Label(card, text=value, style=value_style)
        value_label.grid(row=0, column=0, sticky="w")
        ttk.Label(card, text=label, style="StatLabel.TLabel").grid(row=1, column=0, sticky="w")
        return value_label

    def _on_page_configure(self, _event: tk.Event | None = None) -> None:
        self.scroll_canvas.configure(scrollregion=self.scroll_canvas.bbox("all"))

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self.scroll_canvas.itemconfigure(self.page_window, width=event.width)

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
            self.advanced_frame.grid(row=4, column=0, sticky="ew", pady=(10, 0))
            self.advanced_button.configure(text="高级设置 ▴")
        else:
            self.advanced_frame.grid_remove()
            self.advanced_button.configure(text="高级设置 ▾")

    def toggle_log_panel(self) -> None:
        visible = not self.log_visible.get()
        self.log_visible.set(visible)
        if visible:
            self.status_panel.rowconfigure(4, minsize=220)
            self.log_box.grid()
            self.log_toggle_button.configure(text="收起日志 ▴")
            self.after_idle(lambda: self.scroll_canvas.yview_moveto(1.0))
        else:
            self.status_panel.rowconfigure(4, minsize=0)
            self.log_box.grid_remove()
            self.log_toggle_button.configure(text="展开日志 ▾")

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
        if self.platform_var.get() == "小红书":
            self.feature_combo.configure(values=XHS_FEATURES)
            if self.feature_var.get() not in XHS_FEATURES:
                self.feature_var.set("作品媒体")
        else:
            self.feature_combo.configure(values=DOUYIN_FEATURES)
            if self.feature_var.get() not in DOUYIN_FEATURES:
                self.feature_var.set("作品媒体")
        self._on_feature_change()

    def _on_feature_change(self) -> None:
        is_xhs = self.platform_var.get() == "小红书"
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
            self.input_text.configure(state="disabled")
        else:
            self.input_text.configure(state="normal")
        self._update_detected_count()

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

    def check_login_status(self) -> None:
        def worker() -> None:
            try:
                if self.platform_var.get() == "抖音":
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
                    cookie_count = len([part for part in str(context.get("cookie") or "").split(";") if part.strip()])
                    me = context.get("me") if isinstance(context.get("me"), dict) else {}
                    data = me.get("data") if isinstance(me.get("data"), dict) else {}
                    is_guest = bool(data.get("guest"))
                    if cookie_count and not is_guest and not context.get("loginRequired"):
                        self.log_queue.put(("log", f"小红书账号登录态可用：Cookie {cookie_count} 个"))
                        self.log_queue.put(("login_status", "小红书账号登录态可用"))
                    elif cookie_count:
                        self.log_queue.put(("log", f"小红书浏览器态可用但当前是游客态：Cookie {cookie_count} 个。公开作品和部分评论可用，收藏作品需要扫码登录账号。"))
                        self.log_queue.put(("login_status", "小红书当前为游客态"))
                    else:
                        self.log_queue.put(("log", "小红书登录态不可用或已失效，请点击“登录小红书”。"))
                        self.log_queue.put(("login_status", "小红书未登录或已失效"))
            except Exception as exc:  # noqa: BLE001
                self.log_queue.put(("log", f"检查登录状态失败：{exc}"))
                self.log_queue.put(("login_status", "登录状态检查失败"))

        threading.Thread(target=worker, daemon=True).start()

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

        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        self._set_buttons_state("disabled")
        self._reset_stats(total=1 if options.feature in {"收藏夹", "收藏视频", "收藏作品"} else len(options.inputs))
        self.empty_state_label.configure(text="任务运行中，请等待下载完成。")
        self._append_log(f"开始任务：平台={options.platform}，功能={options.feature}\n")
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

        return TaskOptions(
            platform=platform,
            feature=feature,
            inputs=inputs,
            output_root=OUTPUT_ROOT,
            download_engine=self.engine_var.get(),
            max_workers=self._selected_workers(),
            comment_limit=parse_positive_int(self.comment_limit_var.get(), "评论图片数量"),
            collection_limit=parse_positive_int(self.collection_limit_var.get(), "收藏作品数量" if platform == "小红书" else "收藏夹作品数量"),
            collection_id=collection_id,
            collection_name=collection_name,
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
        except (DouyinCollectionError, Exception) as exc:  # noqa: BLE001
            self.log_queue.put(("task_failed", str(exc)))
        self.log_queue.put(("all_done", None))

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
                elif event == "task_success":
                    report = payload if isinstance(payload, dict) else {}
                    failures = report.get("failures") if isinstance(report.get("failures"), list) else []
                    self.finished_tasks = self.total_tasks
                    self.success_tasks = max(0, self.total_tasks - len(failures))
                    self.failed_tasks = len(failures)
                    self._refresh_stats()
                elif event == "task_failed":
                    self.finished_tasks = self.total_tasks
                    self.failed_tasks = max(1, self.total_tasks)
                    self._append_log(f"任务失败：{payload}\n")
                    self._refresh_stats()
                elif event == "all_done":
                    self._set_buttons_state("normal")
                    self.status_label.configure(text=f"完成：成功 {self.success_tasks}，失败 {self.failed_tasks}")
                    self.empty_state_label.configure(text="任务完成，可打开输出文件夹或复制日志。")
                    messagebox.showinfo("完成", f"任务完成：成功 {self.success_tasks}，失败 {self.failed_tasks}")
        except queue.Empty:
            pass
        self.after(100, self._drain_log_queue)

    def open_output_dir(self) -> None:
        target = Path(self.last_output_dir) if self.last_output_dir else OUTPUT_ROOT
        target.mkdir(parents=True, exist_ok=True)
        os.startfile(target)  # type: ignore[attr-defined]

    def _reset_stats(self, total: int = 0) -> None:
        self.total_tasks = total
        self.finished_tasks = 0
        self.success_tasks = 0
        self.failed_tasks = 0
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
        self.start_button.configure(state=state)
        self.mode_batch_radio.configure(state=state)
        self.mode_single_radio.configure(state=state)
        self.platform_combo.configure(state=readonly)
        self.feature_combo.configure(state=readonly)
        self.engine_combo.configure(state=readonly)
        self.speed_combo.configure(state=readonly)
        self.advanced_button.configure(state=state)
        self.comment_limit_entry.configure(state=state)
        self.collection_limit_entry.configure(state=state)
        for button in (
            self.paste_button,
            self.clear_button,
            self.login_douyin_button,
            self.login_xhs_button,
            self.check_login_button,
        ):
            if button is not None:
                button.configure(state=state)
        if self.open_output_button is not None:
            self.open_output_button.configure(state="normal")
        for button in (self.copy_failure_button, self.copy_all_button, self.clear_log_button):
            if button is not None:
                button.configure(state="normal")
        self._on_feature_change()

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
