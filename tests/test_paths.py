from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from downloaders.paths import author_output_root, safe_author_folder_name


class AuthorOutputPathTests(unittest.TestCase):
    def test_author_mode_creates_and_reuses_windows_safe_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            first = author_output_root(root, ' 博主<一>：测试/频道? ', True)
            second = author_output_root(root, ' 博主<一>：测试/频道? ', True)

            self.assertEqual(first, root / "博主_一__测试_频道_")
            self.assertEqual(second, first)
            self.assertTrue(first.is_dir())

    def test_flat_mode_keeps_files_in_selected_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)

            self.assertEqual(author_output_root(root, "博主一", False), root)
            self.assertFalse((root / "博主一").exists())

    def test_reserved_and_empty_author_names_are_safe(self) -> None:
        self.assertEqual(safe_author_folder_name("CON"), "_CON")
        self.assertEqual(safe_author_folder_name("  ...  "), "未知作者")


if __name__ == "__main__":
    unittest.main()
