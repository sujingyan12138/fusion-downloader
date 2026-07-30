from __future__ import annotations

import json
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from PIL import Image


class OutputLocationTests(unittest.TestCase):
    def test_output_root_is_below_selected_parent(self) -> None:
        parent = Path(r"D:\Media")

        self.assertEqual(
            app.output_root_for_parent(parent),
            parent / "下载结果",
        )

    def test_output_parent_setting_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp_root = Path(temp_name)
            settings_path = temp_root / "下载器设置.json"
            selected_parent = temp_root / "自定义位置"

            app.save_output_parent(selected_parent, settings_path)

            self.assertEqual(
                app.load_output_parent(settings_path, temp_root),
                selected_parent,
            )
            saved = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["output_parent"], str(selected_parent))

    def test_output_preferences_preserve_each_other_and_author_mode_defaults_on(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp_root = Path(temp_name)
            settings_path = temp_root / "下载器设置.json"
            selected_parent = temp_root / "自定义位置"

            self.assertTrue(app.load_organize_by_author(settings_path))
            app.save_organize_by_author(False, settings_path)
            app.save_output_parent(selected_parent, settings_path)

            self.assertFalse(app.load_organize_by_author(settings_path))
            self.assertEqual(app.load_output_parent(settings_path, temp_root), selected_parent)
            saved = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(
                saved,
                {
                    "organize_by_author": False,
                    "output_parent": str(selected_parent),
                },
            )

    def test_invalid_setting_falls_back_to_default_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp_root = Path(temp_name)
            settings_path = temp_root / "下载器设置.json"
            settings_path.write_text('{"output_parent": "relative"}', encoding="utf-8")

            self.assertEqual(
                app.load_output_parent(settings_path, temp_root),
                temp_root,
            )

    def test_malformed_setting_falls_back_to_default_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp_root = Path(temp_name)
            settings_path = temp_root / "下载器设置.json"
            settings_path.write_text("{not-json", encoding="utf-8")

            self.assertEqual(
                app.load_output_parent(settings_path, temp_root),
                temp_root,
            )


class GuiThemeTests(unittest.TestCase):
    def test_checkmark_toggle_draws_a_tick_and_respects_disabled_state(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            selected = tk.BooleanVar(root, value=False)
            widget = app.CheckmarkToggle(
                root,
                text="测试封面",
                variable=selected,
                canvas_bg=app.COLORS["surface"],
            )

            self.assertEqual(widget.itemcget(widget._check, "state"), "hidden")
            widget._toggle()
            self.assertTrue(selected.get())
            self.assertEqual(widget.itemcget(widget._check, "state"), "normal")
            self.assertEqual(len(widget.coords(widget._check)), 6)

            widget.configure(state="disabled")
            widget._toggle()
            self.assertTrue(selected.get())
        finally:
            root.destroy()

    def test_gui_uses_the_green_bento_design_system(self) -> None:
        self.assertEqual(app.COLORS["selection"], "#00B176")
        self.assertEqual(app.COLORS["accent_dark"], "#007E54")
        self.assertEqual(app.COLORS["accent_deep"], "#0E3B2E")
        self.assertEqual(app.COLORS["log"], "#F6FAF8")
        self.assertNotIn("blue", app.COLORS)
        self.assertNotIn("blue_active", app.COLORS)
        self.assertNotIn(app.COLORS["selection"].lower(), {"#000000", "#0071e3"})

    def test_application_icon_is_available_to_source_runtime(self) -> None:
        self.assertEqual(app.ICON_PATH.name, "favicon.ico")
        self.assertTrue(app.ICON_PATH.is_file())
        with Image.open(app.ICON_PATH) as icon:
            self.assertEqual(
                sorted(icon.info.get("sizes", [])),
                [
                    (16, 16),
                    (20, 20),
                    (24, 24),
                    (32, 32),
                    (40, 40),
                    (48, 48),
                    (64, 64),
                    (128, 128),
                    (256, 256),
                ],
            )

    def test_decorative_step_numbers_and_brand_letter_are_not_rendered(self) -> None:
        root = app.UnifiedDownloaderApp()
        root.withdraw()
        try:
            root.update_idletasks()
            rendered_text: list[str] = []
            pending = [root]
            while pending:
                widget = pending.pop()
                pending.extend(widget.winfo_children())
                try:
                    text = widget.cget("text")
                except tk.TclError:
                    text = ""
                if text:
                    rendered_text.append(str(text))
                if isinstance(widget, tk.Canvas):
                    for item in widget.find_all():
                        if widget.type(item) == "text":
                            rendered_text.append(str(widget.itemcget(item, "text")))

            self.assertNotIn("STEP 01", rendered_text)
            self.assertNotIn("STEP 02", rendered_text)
            self.assertNotIn("05", rendered_text)
            self.assertNotIn("F", rendered_text)
            self.assertEqual(
                sum(
                    root.hero_banner.type(item) == "oval"
                    for item in root.hero_banner.find_all()
                ),
                2,
            )
        finally:
            root.destroy()

    def test_output_controls_are_in_footer_and_author_grouping_is_default(self) -> None:
        with patch("app.load_organize_by_author", return_value=True):
            root = app.UnifiedDownloaderApp()
        root.withdraw()
        try:
            root.update_idletasks()
            self.assertTrue(root.organize_by_author_var.get())
            self.assertIsNotNone(root.choose_output_button)
            self.assertIsNotNone(root.open_output_button)
            self.assertIsNotNone(root.author_folder_radio)
            self.assertIsNotNone(root.flat_output_radio)

            def is_descendant(widget: tk.Widget, ancestor: tk.Widget) -> bool:
                current: tk.Widget | None = widget
                while current is not None:
                    if current is ancestor:
                        return True
                    parent_name = current.winfo_parent()
                    current = current._nametowidget(parent_name) if parent_name else None
                return False

            self.assertTrue(is_descendant(root.choose_output_button, root.footer))
            self.assertTrue(is_descendant(root.open_output_button, root.footer))
            self.assertEqual(root.author_folder_radio.cget("text"), "按博主分类")
            self.assertEqual(root.flat_output_radio.cget("text"), "全部平铺")
        finally:
            root.destroy()

    def test_article_platforms_are_available_and_disable_cover_controls(self) -> None:
        root = app.UnifiedDownloaderApp()
        root.withdraw()
        try:
            self.assertEqual(len(root.platform_buttons), 7)
            self.assertIn("微信公众号", root.platform_combo.cget("values"))
            self.assertIn("知乎", root.platform_combo.cget("values"))

            root.platform_var.set("微信公众号")
            root._on_platform_change()
            self.assertEqual(root.feature_var.get(), "文章正文（MD）")
            self.assertFalse(root.download_cover_var.get())
            self.assertFalse(root.cover_only_var.get())
            self.assertEqual(root.login_zhihu_button.winfo_manager(), "")

            root.platform_var.set("知乎")
            root._on_platform_change()
            self.assertEqual(root.login_zhihu_button.winfo_manager(), "grid")
            self.assertEqual(root.check_login_button.winfo_manager(), "")
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
