from __future__ import annotations

from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from downloaders import article_common, douyin, opencli_browser, wechat, zhihu
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

    def test_extracts_image_detail_url_with_complete_query(self) -> None:
        url = (
            "https://mp.weixin.qq.com/s?t=pages/image_detail&scene=1"
            "&__biz=MzI4MDQ2Nzc0NA==&mid=2247483767&idx=1"
            "&sn=5921455a4fa78de1dbcc2e9d93ca429e#wechat_redirect"
        )
        extracted = wechat.extract_urls(f"微信贴图：{url}")
        self.assertEqual(len(extracted), 1)
        self.assertNotIn("#wechat_redirect", extracted[0])
        self.assertTrue(wechat.is_image_detail_url(extracted[0]))
        self.assertIn("mid=2247483767", extracted[0])
        self.assertNotIn(
            "poc_token",
            wechat.canonicalize_url(extracted[0] + "&poc_token=temporary"),
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

    def test_image_detail_parser_keeps_gallery_and_excludes_page_chrome(self) -> None:
        html = """
        <html><head><title>陪孩子旅行的记录</title></head><body>
          <div id="js_wx_follow_nickname"><span class="nickNameSpan">项琴</span></div>
          <div id="img_swiper_content">
            <div class="swiper_item"><img src="https://mmbiz.qpic.cn/a/0?wx_fmt=jpg"></div>
            <div class="swiper_item"><img src="https://mmbiz.qpic.cn/b/0?watermark=1"></div>
            <div class="swiper_item"><img src="https://mmbiz.qpic.cn/a/0?wx_fmt=jpg"></div>
          </div>
          <div id="js_content"><div id="js_image_content">
            <p id="js_image_desc">这是一段足够长的微信贴图文字内容，用于验证作品正文与画廊图片能够被一起保存。</p>
          </div></div>
          <img class="wx_follow_avatar_pic" src="https://mmbiz.qpic.cn/avatar/0">
        </body></html>
        """
        url = (
            "https://mp.weixin.qq.com/s?t=pages/image_detail"
            "&__biz=test&mid=123&idx=1&sn=abc"
        )
        article = wechat.parse_article_html(html, url)
        parsed_body = article_common.BeautifulSoup(article.body_html, "html.parser")

        self.assertEqual(article.platform, "微信贴图")
        self.assertEqual(article.source_type, "wechat_image_post")
        self.assertEqual(article.author, "项琴")
        self.assertEqual(len(parsed_body.find_all("img")), 2)
        self.assertNotIn("avatar", article.body_html)
        self.assertIn("作品正文", article.body_html)

    def test_image_detail_script_prefers_upload_original_and_keeps_watermark_fallback(
        self,
    ) -> None:
        html = r"""
        <html><head><title>微信公众平台</title></head><body>
          <div id="js_wx_follow_nickname"><span class="nickNameSpan">灰诚 Ashin</span></div>
          <div id="js_content"><div id="js_image_content">
            <h1>坠入凡尘</h1>
            <p id="js_image_desc">这是一段足够长的微信贴图文字内容，用于验证上传原图、无水印图和水印兜底图的优先级。</p>
          </div></div>
          <script>
          window.imageData = [{
            cdn_url: 'https://mmbiz.qpic.cn/mmbiz_jpg/base-image/0?wx_fmt=jpeg',
            width: '2560' * 1,
            height: '3840' * 1,
            show_watermark: true,
            watermark_info: {
              cdn_url: 'http://mmbiz.qpic.cn/sz_mmbiz_jpg/watermarked-image/0?wx_fmt=jpeg',
              is_uploader: true,
            },
            original_info: {
              show_original: true,
              cdn_url: 'http://318.wxapp.tc.qq.com/318/stodownload?m=abc\x26amp;filekey=def',
              file_size: '46308272' * 1,
            },
          }, {
            cdn_url: 'https://mmbiz.qpic.cn/mmbiz_jpg/base-image/0?wx_fmt=jpeg',
            width: '2560' * 1,
            height: '3840' * 1,
            show_watermark: 'true' === 'true',
            watermark_info: {
              cdn_url: 'http://mmbiz.qpic.cn/sz_mmbiz_jpg/watermarked-image/0?wx_fmt=jpeg',
            },
            original_info: {
              show_original: 'true' === 'true',
              cdn_url: 'http://318.wxapp.tc.qq.com/318/stodownload?m=abc\x26amp;filekey=def',
              file_size: '46308272' * 1,
            },
          }];
          </script>
        </body></html>
        """

        article = wechat.parse_article_html(
            html,
            "https://mp.weixin.qq.com/s/short-image-post",
        )
        parsed_body = article_common.BeautifulSoup(article.body_html, "html.parser")
        images = parsed_body.find_all("img")

        self.assertEqual(article.source_type, "wechat_image_post")
        self.assertEqual(article.title, "坠入凡尘")
        self.assertEqual(len(images), 1)
        self.assertEqual(
            images[0]["data-original"],
            "http://318.wxapp.tc.qq.com/318/stodownload?m=abc&filekey=def",
        )
        self.assertEqual(
            images[0]["data-src"],
            "https://mmbiz.qpic.cn/mmbiz_jpg/base-image/0?wx_fmt=jpeg",
        )
        candidates = wechat.wechat_image_candidates(
            images[0],
            article.canonical_url,
        )
        self.assertEqual(
            candidates[0].url,
            "http://318.wxapp.tc.qq.com/318/stodownload?m=abc&filekey=def",
        )
        self.assertFalse(candidates[0].watermark)
        watermark_candidates = [
            candidate
            for candidate in candidates
            if "data-watermark-src" in candidate.source
        ]
        self.assertTrue(watermark_candidates)
        self.assertTrue(all(candidate.watermark for candidate in watermark_candidates))

    def test_raw_image_detail_metadata_can_hydrate_public_page_shell(self) -> None:
        html = r"""
        <html><head>
          <meta property="og:title" content="坠入凡尘">
          <meta name="description" content="第一行\x0a第二行，文字内容足够长，可以验证公开页面脚本直接生成微信贴图正文。">
        </head><body>
          <script>
          window.imageData = [{
            cdn_url: 'https://mmbiz.qpic.cn/mmbiz_jpg/base-image/0?wx_fmt=jpeg',
            width: '2560' * 1,
            height: '3840' * 1,
            show_watermark: false,
            watermark_info: {cdn_url: ''},
            original_info: {
              show_original: true,
              cdn_url: 'http://318.wxapp.tc.qq.com/318/stodownload?m=abc\x26amp;filekey=def',
              file_size: '46308272' * 1,
            },
          }];
          window.field = {nickname: "data-miniprogram-nickname"};
          window.account = {nick_name: '子穫 Ashin'};
          </script>
        </body></html>
        """

        article = wechat.parse_article_html(
            html,
            "https://mp.weixin.qq.com/s/short-image-post",
        )
        body = article_common.BeautifulSoup(article.body_html, "html.parser")

        self.assertEqual(article.title, "坠入凡尘")
        self.assertEqual(article.author, "子穫 Ashin")
        self.assertEqual(article.source_type, "wechat_image_post")
        self.assertIn("第一行 第二行", article.body_html)
        self.assertEqual(len(body.find_all("img")), 1)

    def test_qpic_clean_original_removes_watermark_and_preview_parameters(self) -> None:
        soup = article_common.BeautifulSoup(
            "<img src='https://mmbiz.qpic.cn/path/640?"
            "wxfrom=12&amp;wx_fmt=jpg&amp;tp=webp&amp;watermark=1'>",
            "html.parser",
        )
        candidates = wechat.wechat_image_candidates(
            soup.img,
            "https://mp.weixin.qq.com/s/test",
        )
        self.assertEqual(
            candidates[0].url,
            "https://mmbiz.qpic.cn/path/0?wx_fmt=jpg",
        )
        self.assertFalse(candidates[0].watermark)
        self.assertTrue(candidates[1].watermark)

    @patch("downloaders.wechat.extract_via_opencli")
    @patch("downloaders.wechat.opencli_command", return_value=["opencli"])
    @patch("downloaders.wechat.requests.get")
    def test_captcha_response_falls_back_to_current_chrome(
        self,
        requests_get,
        _opencli_command,
        extract_via_opencli,
    ) -> None:
        response = Mock(
            status_code=200,
            apparent_encoding="utf-8",
            encoding="utf-8",
            text="<html>访问验证</html>",
            url="https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?poc_token=x",
        )
        requests_get.return_value = response
        expected = article_common.WebArticle(
            platform="微信贴图",
            source_type="wechat_image_post",
            source_url="https://mp.weixin.qq.com/s?t=pages/image_detail",
            canonical_url="https://mp.weixin.qq.com/s?t=pages/image_detail",
            content_id="abc",
            title="贴图标题",
            author="作者",
            body_html="<p>这是足够长的贴图正文内容，用来验证浏览器回退。</p>",
        )
        extract_via_opencli.return_value = expected

        article = wechat.fetch_article(
            "https://mp.weixin.qq.com/s?t=pages/image_detail"
        )
        self.assertIs(article, expected)
        extract_via_opencli.assert_called_once()

    @patch("downloaders.wechat.OpenCliWindowGuard")
    @patch("downloaders.wechat.run_opencli_json")
    def test_wechat_opencli_session_releases_owned_window(
        self,
        run_opencli_json,
        window_guard_type,
    ) -> None:
        page_url = (
            "https://mp.weixin.qq.com/s?t=pages/image_detail"
            "&__biz=test&mid=123&idx=1&sn=abc"
        )
        html = """
        <title>贴图标题</title><div id="js_name">作者</div>
        <div id="img_swiper_content"><div class="swiper_item">
          <img src="https://mmbiz.qpic.cn/a/0?wx_fmt=jpg">
        </div></div>
        <div id="js_content"><div id="js_image_content">
          <p id="js_image_desc">这是足够长的贴图正文内容，用来验证浏览器窗口释放流程。</p>
        </div></div>
        """
        run_opencli_json.side_effect = [
            {"url": page_url},
            {"matched": True},
            {
                "url": page_url,
                "title": "贴图标题",
                "author": "作者",
                "bodyHtml": (
                    '<div id="js_content"><div id="js_image_content">'
                    '<p id="js_image_desc">这是足够长的贴图正文内容，'
                    "用来验证浏览器窗口释放流程。</p></div></div>"
                ),
                "galleryHtml": [
                    '<img src="https://mmbiz.qpic.cn/a/0?wx_fmt=jpg">'
                ],
            },
            {},
        ]

        with patch(
            "downloaders.wechat.fetch_wechat_page_html",
            side_effect=wechat.WechatDownloadError("公开页面不可用"),
        ):
            article = wechat.extract_via_opencli(page_url)
        self.assertEqual(article.source_type, "wechat_image_post")
        self.assertEqual(
            run_opencli_json.call_args_list[-1].args[0][-1],
            "close",
        )
        window_guard_type.return_value.close_released_window.assert_called_once_with()


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

    @patch("downloaders.zhihu.OpenCliWindowGuard")
    @patch("downloaders.zhihu.run_opencli")
    def test_opencli_session_always_releases_and_closes_owned_window(
        self,
        run_opencli,
        window_guard_type,
    ) -> None:
        run_opencli.side_effect = [
            {"url": "https://www.zhihu.com/question/999/answer/222"},
            {
                "title": "问题标题",
                "author": "目标作者",
                "bodyHtml": (
                    "<p>目标回答的完整正文内容，长度足够通过正文完整性检查，"
                    "并验证浏览器清理不会改变文章提取结果。</p>"
                ),
                "answerId": "222",
                "questionId": "999",
            },
            {},
        ]
        article = zhihu.extract_via_opencli(
            "https://www.zhihu.com/question/999/answer/222"
        )

        self.assertEqual(article.content_id, "222")
        self.assertEqual(
            run_opencli.call_args_list[-1].args[0][-1],
            "close",
        )
        self.assertGreaterEqual(
            window_guard_type.return_value.observe_owned_window.call_count,
            2,
        )
        window_guard_type.return_value.close_released_window.assert_called_once_with()


class OpenCliWindowGuardTests(unittest.TestCase):
    def window(self, handle: int, title: str) -> opencli_browser.BrowserWindow:
        return opencli_browser.BrowserWindow(handle, title, "chrome.exe")

    @patch("downloaders.opencli_browser._request_window_close", return_value=True)
    @patch("downloaders.opencli_browser._browser_windows")
    def test_closes_reused_blank_window_after_release(
        self,
        browser_windows,
        request_close,
    ) -> None:
        browser_windows.side_effect = [
            {10: self.window(10, "about:blank - Google Chrome")},
            {10: self.window(10, "问题标题 - 知乎 - Google Chrome")},
            {10: self.window(10, "about:blank - Google Chrome")},
            {},
        ]
        guard = opencli_browser.OpenCliWindowGuard()

        self.assertEqual(guard.observe_owned_window(), 10)
        self.assertTrue(guard.close_released_window(timeout_seconds=0.1))
        request_close.assert_called_once_with(10)

    @patch("downloaders.opencli_browser._request_window_close")
    @patch("downloaders.opencli_browser._browser_windows")
    def test_never_closes_preexisting_user_window(
        self,
        browser_windows,
        request_close,
    ) -> None:
        browser_windows.side_effect = [
            {10: self.window(10, "用户正在阅读的页面 - Google Chrome")},
            {10: self.window(10, "知乎文章 - Google Chrome")},
        ]
        guard = opencli_browser.OpenCliWindowGuard()

        self.assertIsNone(guard.observe_owned_window())
        self.assertFalse(guard.close_released_window(timeout_seconds=0))
        request_close.assert_not_called()

    @patch("downloaders.opencli_browser._request_window_close")
    @patch("downloaders.opencli_browser._browser_windows")
    def test_ambiguous_new_windows_fail_closed(
        self,
        browser_windows,
        request_close,
    ) -> None:
        browser_windows.side_effect = [
            {},
            {
                10: self.window(10, "知乎文章 - Google Chrome"),
                20: self.window(20, "用户新开的窗口 - Google Chrome"),
            },
        ]
        guard = opencli_browser.OpenCliWindowGuard()

        self.assertIsNone(guard.observe_owned_window())
        self.assertFalse(guard.close_released_window(timeout_seconds=0))
        request_close.assert_not_called()


class DouyinOpenCliWindowTests(unittest.TestCase):
    @patch("downloaders.douyin.OpenCliWindowGuard")
    @patch("downloaders.douyin.run_opencli")
    def test_comment_snapshot_releases_and_closes_owned_window(
        self,
        run_opencli,
        window_guard_type,
    ) -> None:
        run_opencli.side_effect = ["{}", '{"commentImages": []}', "{}"]

        result = douyin.read_opencli_comment_snapshot(
            "C:/tools/opencli.cmd",
            "https://www.douyin.com/video/123",
        )

        self.assertEqual(result, {"commentImages": []})
        self.assertEqual(run_opencli.call_args_list[-1].args[1][-1], "close")
        window_guard_type.return_value.close_released_window.assert_called_once_with()

    @patch("downloaders.douyin.OpenCliWindowGuard")
    @patch("downloaders.douyin.run_opencli")
    def test_comment_api_snapshot_releases_and_closes_owned_window(
        self,
        run_opencli,
        window_guard_type,
    ) -> None:
        run_opencli.side_effect = ["{}", '{"template": ""}', "{}"]

        result = douyin.read_opencli_comment_api_snapshot(
            "C:/tools/opencli.cmd",
            "https://www.douyin.com/video/123",
            20,
            lambda _message: None,
        )

        self.assertEqual(result, {"template": ""})
        self.assertEqual(run_opencli.call_args_list[-1].args[1][-1], "close")
        window_guard_type.return_value.close_released_window.assert_called_once_with()


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
