from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from downloaders import wechat_channels
from services.task_runner import TaskOptions, extract_task_inputs, run_task


H264_URL = (
    "https://finder.video.qq.com/251/20302/stodownload"
    "?encfilekey=encrypted-key&token=h264-token"
    "&X-snsvideoflag=xWT113&sign=temporary-sign"
)
H265_URL = (
    "https://finder.video.qq.com/251/20302/stodownload"
    "?encfilekey=encrypted-key&token=h265-token"
    "&X-snsvideoflag=xWT258&sign=temporary-sign"
)


def sample_profile() -> wechat_channels.ChannelsProfile:
    return wechat_channels.profile_from_payload(
        {
            "data": {
                "feedInfo": {
                    "description": "测试视频号标题 #话题",
                    "videoUrl": H264_URL,
                    "h264VideoInfo": {"videoUrl": H264_URL},
                    "h265VideoInfo": {"videoUrl": H265_URL},
                },
                "authorInfo": {"nickname": "测试作者"},
            }
        },
        "https://weixin.qq.com/sph/AUJhrS4bvy",
    )


class WechatChannelsUrlTests(unittest.TestCase):
    def test_extracts_short_and_preview_urls_without_duplicates(self) -> None:
        text = (
            "短链 https://weixin.qq.com/sph/AUJhrS4bvy，"
            "预览 https://channels.weixin.qq.com/finder-preview/pages/sph"
            "?id=AUJhrS4bvy&scene=1"
        )
        self.assertEqual(
            wechat_channels.extract_urls(text),
            ["https://weixin.qq.com/sph/AUJhrS4bvy"],
        )
        self.assertEqual(
            extract_task_inputs("微信", text, single=False),
            ["https://weixin.qq.com/sph/AUJhrS4bvy"],
        )

    def test_rejects_non_channels_url(self) -> None:
        with self.assertRaises(ValueError):
            wechat_channels.canonicalize_url("https://example.com/video/1")


class WechatChannelsProfileTests(unittest.TestCase):
    def test_builds_original_candidate_without_transient_quality_parameters(self) -> None:
        profile = sample_profile()
        original = profile.candidates[0]

        self.assertEqual(profile.content_id, "AUJhrS4bvy")
        self.assertEqual(profile.author, "测试作者")
        self.assertEqual(original.label, "原始质量")
        self.assertEqual(
            original.url,
            "https://finder.video.qq.com/251/20302/stodownload"
            "?encfilekey=encrypted-key&token=h264-token",
        )
        self.assertNotIn("X-snsvideoflag", original.url)
        self.assertNotIn("sign=", original.url)

    @patch("downloaders.wechat_channels.probe_candidate")
    def test_original_candidate_wins_when_accessible(self, probe_candidate) -> None:
        profile = sample_profile()

        def fake_probe(candidate, **_kwargs):
            size = {
                "原始质量": 212_000_000,
                "H.264 在线版": 22_000_000,
                "H.265 在线版": 8_000_000,
            }[candidate.label]
            return wechat_channels.ChannelsProbe(candidate, size, "video/mp4")

        probe_candidate.side_effect = fake_probe
        selected = wechat_channels.select_best_candidate(profile)

        self.assertEqual(selected.candidate.label, "原始质量")
        self.assertEqual(selected.content_length, 212_000_000)

    @patch("downloaders.wechat_channels.probe_candidate")
    def test_falls_back_to_largest_working_online_candidate(
        self,
        probe_candidate,
    ) -> None:
        profile = sample_profile()

        def fake_probe(candidate, **_kwargs):
            if candidate.label == "原始质量":
                raise wechat_channels.WechatChannelsDownloadError("已失效")
            size = 22_000_000 if candidate.label == "H.264 在线版" else 8_000_000
            return wechat_channels.ChannelsProbe(candidate, size, "video/mp4")

        probe_candidate.side_effect = fake_probe
        selected = wechat_channels.select_best_candidate(profile)

        self.assertEqual(selected.candidate.label, "H.264 在线版")


class WechatChannelsDownloadTests(unittest.TestCase):
    @patch("downloaders.wechat_channels.youtube.probe_media")
    @patch("downloaders.wechat_channels.youtube.download_parallel_stream")
    @patch("downloaders.wechat_channels.youtube.find_executable", return_value="ffprobe")
    @patch("downloaders.wechat_channels.select_best_candidate")
    @patch("downloaders.wechat_channels.fetch_profile")
    def test_downloads_verified_original_and_reuses_existing_file(
        self,
        fetch_profile,
        select_best_candidate,
        _find_executable,
        download_parallel_stream,
        probe_media,
    ) -> None:
        profile = sample_profile()
        selected = wechat_channels.ChannelsProbe(
            profile.candidates[0],
            4,
            "video/mp4",
        )
        fetch_profile.return_value = profile
        select_best_candidate.return_value = selected
        probe_media.return_value = {
            "has_video": True,
            "has_audio": True,
            "resolution": "1920x1080",
            "fps": 30.0,
            "video_codec": "h264",
            "audio_codec": "aac",
        }

        def fake_download(_info, target, _label, _logger, max_workers):
            self.assertEqual(max_workers, 4)
            Path(target).write_bytes(b"mp4!")

        download_parallel_stream.side_effect = fake_download

        with tempfile.TemporaryDirectory() as temp_name:
            first = wechat_channels.download_video(
                "https://weixin.qq.com/sph/AUJhrS4bvy",
                temp_name,
                max_workers=4,
                organize_by_author=True,
            )
            second = wechat_channels.download_video(
                "https://weixin.qq.com/sph/AUJhrS4bvy",
                temp_name,
                max_workers=4,
                organize_by_author=True,
            )

            output = Path(first["output"])
            self.assertTrue(output.is_file())
            self.assertEqual(output.parent.name, "测试作者")
            self.assertEqual(first["selected_format"], "原始质量")
            self.assertEqual(first["resolution"], "1920x1080")
            self.assertEqual(second["status"], "skipped")
            self.assertEqual(second["output"], first["output"])
            self.assertEqual(download_parallel_stream.call_count, 1)
            self.assertEqual(select_best_candidate.call_count, 1)


class WechatChannelsDispatchTests(unittest.TestCase):
    @patch("services.task_runner.wechat_channels.download_video")
    def test_wechat_dispatches_channels_video_and_forwards_options(
        self,
        download_video,
    ) -> None:
        download_video.return_value = {
            "platform": "微信视频号",
            "author": "测试作者",
            "output": "video.mp4",
        }
        options = TaskOptions(
            platform="微信",
            feature="自动识别（视频/图文）",
            inputs=["https://weixin.qq.com/sph/AUJhrS4bvy"],
            output_root=Path("out"),
            max_workers=8,
            organize_by_author=False,
        )

        report = run_task(options, lambda _message: None)

        self.assertEqual(report["failures"], [])
        self.assertEqual(report["items"][0]["platform"], "微信视频号")
        self.assertFalse(download_video.call_args.kwargs["organize_by_author"])
        self.assertEqual(download_video.call_args.kwargs["max_workers"], 8)


if __name__ == "__main__":
    unittest.main()
