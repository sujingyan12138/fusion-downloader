from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from downloaders import douyin
from downloaders import douyin_collection
from PIL import Image


AWEME_ID = "7667575934631744635"
NOTE_URL = f"https://www.douyin.com/note/{AWEME_ID}"


def image_aweme() -> dict:
    return {
        "aweme_id": AWEME_ID,
        "author": {"nickname": "测试作者"},
        "desc": "第一段被截断……版本过低，升级后可展示全部信息",
        "share_info": {
            "share_desc_info": (
                "#在抖音，记录美好生活#第一段完整正文。\n\n"
                "第二段仍然完整。\n#随笔 #原创"
            )
        },
        "images": [
            {
                "width": 1080,
                "height": 1440,
                "url_list": ["https://example.test/original.jpg"],
            }
        ],
    }


def long_article_aweme() -> dict:
    markdown_url = "https://example.test/article-preview.jpg"
    highest_url = "https://example.test/article-highest.jpg"
    return {
        "aweme_id": "7659165382892902650",
        "author": {"nickname": "长文作者"},
        "desc": "这里只是短摘要 #ai",
        "share_info": {"share_desc_info": "#在抖音，记录美好生活#这里只是短摘要 #ai"},
        "article_info": {
            "article_title": "完整长文章标题",
            "article_content": json.dumps(
                {
                    "long_article_abstract": "摘要",
                    "markdown": (
                        "长文章第一段。\n\n"
                        f"![图片描述]({markdown_url} width=800 height=500)\n\n"
                        "# 第一节\n\n长文章结尾。"
                    ),
                },
                ensure_ascii=False,
            ),
            "fe_data": json.dumps(
                {
                    "image_list": [
                        {
                            "same_url": highest_url,
                            "origin_image_url": "https://example.test/article-origin.jpg",
                            "markdown_url": markdown_url,
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        },
        "video": {
            "duration": 0,
            "play_addr": {"url_list": ["https://example.test/background.mp3"]},
        },
    }


def image_bytes(size: tuple[int, int]) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color=(245, 245, 245)).save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


class DouyinMarkdownTests(unittest.TestCase):
    def test_complete_share_text_wins_over_truncated_desc(self) -> None:
        text = douyin.extract_note_text(image_aweme())

        self.assertEqual(
            text,
            "第一段完整正文。\n\n第二段仍然完整。\n#随笔 #原创",
        )
        self.assertNotIn("在抖音，记录美好生活", text)
        self.assertNotIn("版本过低", text)
    def test_truncated_text_is_not_written_as_article(self) -> None:
        aweme = {
            "desc": "只能看到开头……版本过低，升级后可展示全部信息",
            "share_info": {},
        }

        self.assertEqual(douyin.extract_note_text(aweme), "")
        self.assertTrue(douyin.has_truncated_note_text(aweme))

    def test_embedded_long_article_markdown_wins_over_short_share_desc(self) -> None:
        aweme = long_article_aweme()

        text = douyin.extract_note_text(aweme)

        self.assertTrue(douyin.has_long_article(aweme))
        self.assertEqual(douyin.note_title(aweme, aweme["aweme_id"]), "完整长文章标题")
        self.assertIn("长文章第一段。", text)
        self.assertIn("![图片描述](https://example.test/article-preview.jpg", text)
        self.assertIn("# 第一节", text)
        self.assertNotIn("这里只是短摘要", text)

    def test_long_article_image_candidates_prefer_structured_high_quality_urls(self) -> None:
        items = douyin.extract_long_article_image_items(long_article_aweme())

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].candidates[0].url, "https://example.test/article-highest.jpg")
        self.assertFalse(items[0].candidates[0].preview)
        self.assertTrue(items[0].candidates[-1].preview)

    def test_long_article_images_are_saved_to_typora_assets_and_reused(self) -> None:
        aweme = long_article_aweme()
        markdown = douyin.extract_note_text(aweme)
        content = image_bytes((1171, 732))

        def choose_best(_session, item, _referer):
            return SimpleNamespace(
                content=content,
                extension="jpg",
                image_format="jpeg",
                width=1171,
                height=732,
                candidate=item.candidates[0],
            )

        with patch(
            "downloaders.douyin.make_session",
            return_value=MagicMock(),
        ), patch(
            "downloaders.douyin.choose_best_image",
            side_effect=choose_best,
        ), tempfile.TemporaryDirectory() as temp_name:
            first = douyin.localize_long_article_images(
                aweme,
                markdown,
                temp_name,
                f"抖音_作者_标题_{aweme['aweme_id']}",
                NOTE_URL,
            )
            second = douyin.localize_long_article_images(
                aweme,
                markdown,
                temp_name,
                f"抖音_作者_标题_{aweme['aweme_id']}",
                NOTE_URL,
            )
            assets = list(Path(temp_name, "assets").glob("*"))
            target = Path(first["images"][0]["path"])

            self.assertTrue(target.is_file())
            with Image.open(target) as image:
                self.assertEqual(image.size, (1171, 732))

        self.assertEqual(first["images"][0]["status"], "ok")
        self.assertEqual(second["images"][0]["status"], "skipped")
        self.assertEqual(len(assets), 1)
        self.assertIn("](./assets/", first["markdown"])
        self.assertNotIn("article-preview.jpg", first["markdown"])
        self.assertEqual(first["failures"], [])

    def test_article_image_failure_keeps_remote_markdown_reference(self) -> None:
        aweme = long_article_aweme()
        markdown = douyin.extract_note_text(aweme)
        with patch(
            "downloaders.douyin.choose_best_image",
            side_effect=douyin.DouyinDownloadError("HTTP 403"),
        ), tempfile.TemporaryDirectory() as temp_name:
            report = douyin.localize_long_article_images(
                aweme,
                markdown,
                temp_name,
                f"抖音_作者_标题_{aweme['aweme_id']}",
                NOTE_URL,
            )

        self.assertEqual(report["images"], [])
        self.assertEqual(report["failures"][0]["index"], 1)
        self.assertIn("https://example.test/article-preview.jpg", report["markdown"])

    def test_markdown_is_utf8_and_identical_content_is_reused(self) -> None:
        aweme = image_aweme()
        article_text = douyin.extract_note_text(aweme)
        with tempfile.TemporaryDirectory() as temp_name:
            first = douyin.save_note_markdown(
                aweme,
                article_text,
                temp_name,
                f"抖音_测试作者_示例_{AWEME_ID}",
                AWEME_ID,
                "测试作者",
                "示例标题",
                NOTE_URL,
            )
            second = douyin.save_note_markdown(
                aweme,
                article_text,
                temp_name,
                f"抖音_测试作者_示例_{AWEME_ID}",
                AWEME_ID,
                "测试作者",
                "示例标题",
                NOTE_URL,
            )
            target = Path(first["path"])
            content = target.read_text(encoding="utf-8")
            markdown_files = list(Path(temp_name).glob("*.md"))

        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(first["path"], second["path"])
        self.assertEqual(len(markdown_files), 1)
        self.assertIn("# 示例标题", content)
        self.assertIn(f"- 作品 ID：{AWEME_ID}", content)
        self.assertIn(f"- 来源：<{NOTE_URL}>", content)
        self.assertIn("第一段完整正文。\n\n第二段仍然完整。", content)

    def test_audio_only_image_post_video_field_is_not_a_video(self) -> None:
        aweme = image_aweme()
        aweme["video"] = {
            "duration": 0,
            "play_addr": {
                "url_list": ["https://cdn.example.test/music/background.mp3"],
            },
        }

        self.assertEqual(douyin.extract_videos(aweme), [])

    def test_existing_images_still_allow_missing_markdown_to_be_created(self) -> None:
        aweme = image_aweme()
        existing_name = f"抖音_测试作者_示例_{AWEME_ID}_001_nowm_orig_1080x1440.jpg"
        with patch(
            "downloaders.douyin.make_session",
            return_value=MagicMock(),
        ), patch(
            "downloaders.douyin.fetch_page",
            return_value=("<html></html>", NOTE_URL),
        ), patch(
            "downloaders.douyin.find_aweme",
            return_value=aweme,
        ), patch(
            "downloaders.douyin.existing_media_filenames",
            return_value=[existing_name],
        ), patch(
            "downloaders.douyin.make_download_engine",
        ) as make_engine, tempfile.TemporaryDirectory() as temp_name:
            report = douyin.download_note(NOTE_URL, temp_name)
            markdown_path = Path(report["markdown"]["path"])

            self.assertTrue(markdown_path.is_file())
            self.assertEqual(report["skipped"][0]["reason"], "exists_nowm_orig_images")

        make_engine.assert_not_called()

    def test_long_article_without_conventional_media_returns_markdown(self) -> None:
        aweme = long_article_aweme()
        url = f"https://www.douyin.com/user/self?modal_id={aweme['aweme_id']}"
        with patch(
            "downloaders.douyin.make_session",
            return_value=MagicMock(),
        ), patch(
            "downloaders.douyin.fetch_page",
            return_value=("<html></html>", url),
        ), patch(
            "downloaders.douyin.find_aweme",
            return_value=aweme,
        ), patch(
            "downloaders.douyin.localize_long_article_images",
            return_value={
                "markdown": douyin.extract_note_text(aweme),
                "images": [],
                "failures": [],
            },
        ), patch(
            "downloaders.douyin.make_download_engine",
        ) as make_engine, tempfile.TemporaryDirectory() as temp_name:
            report = douyin.download_note(url, temp_name)
            markdown_path = Path(report["markdown"]["path"])
            content = markdown_path.read_text(encoding="utf-8")

        self.assertEqual(report["kind"], "article")
        self.assertEqual(report["output_path"], str(markdown_path))
        self.assertIn("长文章结尾。", content)
        make_engine.assert_not_called()


class DouyinCollectionMarkdownTests(unittest.TestCase):
    def test_existing_images_without_markdown_are_sent_to_single_note_path(self) -> None:
        aweme = image_aweme()
        with tempfile.TemporaryDirectory() as temp_name:
            Path(temp_name, f"抖音_{AWEME_ID}_001_nowm_orig_1080x1440.jpg").write_bytes(b"image")
            with patch(
                "downloaders.douyin_collection.read_douyin_login_context",
                return_value={"cookie": "available"},
            ), patch(
                "downloaders.douyin_collection.fetch_collection_awemes",
                return_value=[aweme],
            ), patch(
                "downloaders.douyin_collection.douyin.download_note",
                return_value={"markdown": {"status": "ok"}},
            ) as download_note:
                report = douyin_collection.download_collection(
                    "collection-id",
                    "测试收藏夹",
                    temp_name,
                    max_workers=1,
                )

        self.assertEqual(report["ok_count"], 1)
        self.assertEqual(report["skipped_count"], 0)
        download_note.assert_called_once()

    def test_existing_images_and_markdown_are_skipped_together(self) -> None:
        aweme = image_aweme()
        with tempfile.TemporaryDirectory() as temp_name:
            Path(temp_name, f"抖音_{AWEME_ID}_001_nowm_orig_1080x1440.jpg").write_bytes(b"image")
            Path(temp_name, f"抖音_{AWEME_ID}_正文.md").write_text("complete", encoding="utf-8")
            with patch(
                "downloaders.douyin_collection.read_douyin_login_context",
                return_value={"cookie": "available"},
            ), patch(
                "downloaders.douyin_collection.fetch_collection_awemes",
                return_value=[aweme],
            ), patch(
                "downloaders.douyin_collection.douyin.download_note",
            ) as download_note:
                report = douyin_collection.download_collection(
                    "collection-id",
                    "测试收藏夹",
                    temp_name,
                    max_workers=1,
                )

        self.assertEqual(report["ok_count"], 0)
        self.assertEqual(report["skipped_count"], 1)
        self.assertEqual(report["skipped"][0]["reason"], "exists_nowm_orig")
        download_note.assert_not_called()

    def test_existing_long_article_without_local_assets_is_upgraded(self) -> None:
        aweme = long_article_aweme()
        aweme_id = aweme["aweme_id"]
        with tempfile.TemporaryDirectory() as temp_name:
            Path(temp_name, f"抖音_{aweme_id}_正文.md").write_text("complete", encoding="utf-8")
            with patch(
                "downloaders.douyin_collection.read_douyin_login_context",
                return_value={"cookie": "available"},
            ), patch(
                "downloaders.douyin_collection.fetch_collection_awemes",
                return_value=[aweme],
            ), patch(
                "downloaders.douyin_collection.douyin.download_note",
            ) as download_note:
                report = douyin_collection.download_collection(
                    "collection-id",
                    "测试收藏夹",
                    temp_name,
                    max_workers=1,
                )

        self.assertEqual(report["skipped_count"], 0)
        self.assertEqual(report["ok_count"], 1)
        download_note.assert_called_once()

    def test_existing_long_article_with_local_assets_is_skipped(self) -> None:
        aweme = long_article_aweme()
        aweme_id = aweme["aweme_id"]
        with tempfile.TemporaryDirectory() as temp_name:
            assets = Path(temp_name, "assets")
            assets.mkdir()
            Path(assets, "article.jpg").write_bytes(image_bytes((1171, 732)))
            Path(temp_name, f"抖音_{aweme_id}_正文.md").write_text(
                "![图片](./assets/article.jpg)\n",
                encoding="utf-8",
            )
            with patch(
                "downloaders.douyin_collection.read_douyin_login_context",
                return_value={"cookie": "available"},
            ), patch(
                "downloaders.douyin_collection.fetch_collection_awemes",
                return_value=[aweme],
            ), patch(
                "downloaders.douyin_collection.douyin.download_note",
            ) as download_note:
                report = douyin_collection.download_collection(
                    "collection-id",
                    "测试收藏夹",
                    temp_name,
                    max_workers=1,
                )

        self.assertEqual(report["skipped_count"], 1)
        self.assertEqual(report["skipped"][0]["reason"], "exists_markdown")
        download_note.assert_not_called()


class DouyinCollectionIdTests(unittest.TestCase):
    def test_string_collection_id_wins_over_rounded_javascript_number(self) -> None:
        collections = douyin_collection.parse_collection_list(
            {
                "collects_list": [
                    {
                        "collects_id": 7655694467143407616,
                        "collects_id_str": "7655694467143407401",
                        "collects_name": "图片",
                    }
                ]
            }
        )

        self.assertEqual(collections, [{"id": "7655694467143407401", "name": "图片", "count": ""}])


if __name__ == "__main__":
    unittest.main()
