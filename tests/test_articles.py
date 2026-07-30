from __future__ import annotations

from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from downloaders import article_common, wechat, zhihu
from downloaders.covers import CoverCandidate, CoverProbe
from services.task_runner import TaskOptions, extract_task_inputs, run_task


def make_image_bytes(size: tuple[int, int], image_format: str = "PNG") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color=(18, 120, 80)).save(buffer, format=image_format)
    return buffer.getvalue()


class ArticleCommonTests(unittest.TestCase):
    def test_localizes_repeated_image_to_typora_assets_and_reuses_result(self) -> None:
        image_bytes = make_image_bytes((640, 360))
        candidate = CoverCandidate("https://cdn.test/photo.png", "test")
        probe = CoverProbe(
            candidate=candidate,
            content=image_bytes,
            extension="png",
            image_format="png",
            width=640,
            height=360,
        )
        article = article_common.WebArticle(
            platform="测试平台",
            source_type="test_article",
            source_url="https://example.test/a",
            canonical_url="https://example.test/a",
            content_id="123",
            title="文章标题",
            author="作者一",
            body_html=(
                "<article><p>这是一段足够长的文章正文，用于测试 Markdown 转换。</p>"
                "<img data-src='https://cdn.test/photo.png'>"
                "<img data-src='https://cdn.test/photo.png'></article>"
            ),
        )

        with tempfile.TemporaryDirectory() as temp_name, patch(
            "downloaders.article_common.choose_best_article_image",
            return_value=probe,
        ):
            root = Path(temp_name)
            kwargs = {
                "organize_by_author": True,
                "image_candidates": lambda image, _url: [
                    CoverCandidate(str(image.get("data-src")), "test")
                ],
            }
            first = article_common.save_web_article(article, root, **kwargs)
            second = article_common.save_web_article(article, root, **kwargs)
            markdown_path = Path(first["output_path"])
            content = markdown_path.read_text(encoding="utf-8")

            self.assertEqual(markdown_path.parent, root / "作者一")
            self.assertEqual(first["markdown"]["status"], "ok")
            self.assertEqual(second["markdown"]["status"], "skipped")
            self.assertEqual(content.count("](./assets/"), 2)
            self.assertEqual(len(first["article_images"]), 1)
            self.assertTrue(Path(first["article_images"][0]["path"]).is_file())
            self.assertEqual(
                len(list((markdown_path.parent / "assets").glob("*"))),
                1,
            )

    def test_small_decoded_decoration_is_removed_without_asset(self) -> None:
        probe = CoverProbe(
            candidate=CoverCandidate("https://cdn.test/icon.png", "test"),
            content=make_image_bytes((64, 64)),
            extension="png",
            image_format="png",
            width=64,
            height=64,
        )
        article = article_common.WebArticle(
            platform="测试平台",
            source_type="test_article",
            source_url="https://example.test/a",
            canonical_url="https://example.test/a",
            content_id="123",
            title="文章标题",
            author="作者",
            body_html="<p>这是一段足够长的正文内容，装饰图不应保存。</p><img src='https://cdn.test/icon.png'>",
        )
        with tempfile.TemporaryDirectory() as temp_name, patch(
            "downloaders.article_common.choose_best_article_image",
            return_value=probe,
        ):
            report = article_common.save_web_article(
                article,
                temp_name,
                organize_by_author=False,
                image_candidates=lambda _image, _url: [probe.candidate],
                min_image_dimension=120,
            )
            content = Path(report["output_path"]).read_text(encoding="utf-8")
            self.assertNotIn("cdn.test/icon.png", content)
            self.assertEqual(report["article_images"], [])
            self.assertFalse((Path(temp_name) / "assets").exists())


class WechatArticleTests(unittest.TestCase):
    def test_extracts_share_urls_without_duplicates(self) -> None:
        text = (
            "微信公众号文章 https://mp.weixin.qq.com/s/abc_DEF-1。"
            "重复：https://mp.weixin.qq.com/s/abc_DEF-1"
        )
        self.assertEqual(
            wechat.extract_urls(text),
            ["https://mp.weixin.qq.com/s/abc_DEF-1"],
        )
        self.assertEqual(
            extract_task_inputs("微信公众号", text, single=False),
            ["https://mp.weixin.qq.com/s/abc_DEF-1"],
        )

    def test_parses_only_js_content_and_prefers_account_name(self) -> None:
        html = """
        <html><head><meta property="og:title" content="测试微信文章"></head>
        <body>
          <div id="js_name">测试公众号</div>
          <div class="other">不应进入正文的页面导航</div>
          <div id="js_content"><p>这是微信公众号文章的完整正文内容，并且长度足够用于可靠识别。</p></div>
        </body></html>
        """
        article = wechat.parse_article_html(html, "https://mp.weixin.qq.com/s/test-id")
        self.assertEqual(article.title, "测试微信文章")
        self.assertEqual(article.author, "测试公众号")
        self.assertEqual(article.content_id, "test-id")
        self.assertIn("完整正文", article.body_html)
        self.assertNotIn("页面导航", article.body_html)

    def test_original_qpic_candidate_precedes_preview(self) -> None:
        soup = article_common.BeautifulSoup(
            "<img data-src='https://mmbiz.qpic.cn/path/640?from=appmsg' data-w='900'>",
            "html.parser",
        )
        candidates = wechat.wechat_image_candidates(
            soup.img,
            "https://mp.weixin.qq.com/s/test",
        )
        self.assertTrue(candidates[0].url.startswith("https://mmbiz.qpic.cn/path/0?"))
        self.assertGreater(candidates[0].preference, candidates[1].preference)


class ZhihuArticleTests(unittest.TestCase):
    def test_extracts_answer_url_and_rejects_question_only(self) -> None:
        share = (
            "怎样才能叫做真正会科研的人？ - 回答 - 知乎 "
            "https://www.zhihu.com/question/1889616763626444553/answer/2019195473999704632"
        )
        self.assertEqual(
            zhihu.extract_urls(share),
            [
                "https://www.zhihu.com/question/1889616763626444553/"
                "answer/2019195473999704632"
            ],
        )
        with self.assertRaises(ValueError):
            zhihu.canonicalize_url("https://www.zhihu.com/question/123")

    def test_html_parser_targets_exact_answer_id(self) -> None:
        html = """
        <h1 class="QuestionHeader-title">问题标题</h1>
        <div class="AnswerItem" name="111">
          <div class="AuthorInfo-name">错误作者</div>
          <div class="RichText">错误回答正文，不能混入目标。</div>
        </div>
        <div class="AnswerItem" name="222">
          <div class="AuthorInfo-name">目标作者</div>
          <meta itemprop="dateCreated" content="2026-01-01">
          <div class="RichContent-inner"><p>目标回答的完整正文内容。</p></div>
        </div>
        """
        article = zhihu.parse_zhihu_html(
            html,
            "https://www.zhihu.com/question/999/answer/222",
        )
        self.assertEqual(article.content_id, "222")
        self.assertEqual(article.author, "目标作者")
        self.assertIn("目标回答", article.body_html)
        self.assertNotIn("错误回答", article.body_html)


class ArticleDispatchTests(unittest.TestCase):
    @patch("services.task_runner.wechat.download_article")
    def test_wechat_dispatch_forwards_author_mode(self, download_article) -> None:
        download_article.return_value = {
            "platform": "微信公众号",
            "author": "作者",
            "output_path": "x.md",
        }
        options = TaskOptions(
            platform="微信公众号",
            feature="文章正文（MD）",
            inputs=["https://mp.weixin.qq.com/s/test"],
            output_root=Path("out"),
            organize_by_author=False,
        )
        report = run_task(options, lambda _message: None)
        self.assertEqual(report["failures"], [])
        self.assertEqual(report["items"][0]["platform"], "微信公众号")
        self.assertFalse(download_article.call_args.kwargs["organize_by_author"])

    @patch("services.task_runner.zhihu.download_article")
    def test_zhihu_dispatch_aggregates_failure(self, download_article) -> None:
        download_article.side_effect = RuntimeError("知乎请求受限")
        options = TaskOptions(
            platform="知乎",
            feature="文章正文（MD）",
            inputs=["https://www.zhihu.com/question/1/answer/2"],
            output_root=Path("out"),
        )
        report = run_task(options, lambda _message: None)
        self.assertEqual(report["items"], [])
        self.assertEqual(report["failures"][0]["error"], "知乎请求受限")


if __name__ == "__main__":
    unittest.main()
