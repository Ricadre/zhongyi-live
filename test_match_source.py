"""Run: python3 -m unittest discover -s work/zhongyi_builder -p test_match_source.py"""

import json
from pathlib import Path
import unittest
from urllib.parse import quote

from match_source import parse_match_sources


def sample_channel(**overrides):
    channel = {"name": "直播吧视频直播", "channel_id": "kball_16727",
               "url_list": [{"text": "超清1080P", "url": "http://freevip.client.com/2286843/" +
                             quote("rtmp://spl.tiyucdn.com/2026/0917lcam28463", safe="")} ]}
    channel.update(overrides)
    return {"match_id": 2286843, "video_live": {"section_info": [{"data": [channel]}]}}


class MatchSourceTests(unittest.TestCase):
    def test_real_ios_and_android_have_two_unverified_qualities(self):
        fixtures = Path(__file__).resolve().parent / "fixtures"
        parsed = []
        for name in ("app-ios-latest.json", "app-android-latest.json"):
            with self.subTest(platform=name):
                result = parse_match_sources((fixtures / name).read_text())
                self.assertEqual(result["status"], "candidates")
                self.assertEqual(result["match_id"], 2286843)
                self.assertEqual([s["quality_height"] for s in result["sources"]], [1080, 720])
                self.assertEqual([s["candidate_url"] for s in result["sources"]], [
                    "https://spl.tiyucdn.com/2026/0917lcam28463.m3u8",
                    "https://spl.tiyucdn.com/2026/0917lcam28463_30fps.m3u8"])
                self.assertTrue(all(s["rtmp_url"].startswith("rtmp://") for s in result["sources"]))
                self.assertTrue(all(s["status"] == "unverified" and s["verified"] is False for s in result["sources"]))
                self.assertGreater(result["skipped"]["animation"], 0)
                parsed.append(result["sources"])
        self.assertEqual(parsed[0], parsed[1])

    def test_real_hslive_match_has_two_unverified_qualities(self):
        fixture = Path(__file__).resolve().parent / "fixtures" / "schedule-detail-2286896.json"
        result = parse_match_sources(fixture.read_text())
        self.assertEqual(result["status"], "candidates")
        self.assertEqual(result["match_id"], 2286896)
        self.assertEqual([s["quality_height"] for s in result["sources"]], [1080, 720])
        self.assertEqual([s["candidate_url"] for s in result["sources"]], [
            "https://hslive.tiyucdn.com/zbbleft/0917olpk17912.m3u8",
            "https://hslive.tiyucdn.com/zbbleft/0917olpk17912_30fps.m3u8"])
        self.assertTrue(all(s["status"] == "unverified" and s["verified"] is False for s in result["sources"]))

    def test_empty_and_malformed_payloads(self):
        self.assertEqual(parse_match_sources({})["status"], "no_official_source")
        for payload in (None, [], 1, "{broken", b"\xff", {"video_live": [1]}):
            with self.subTest(payload=payload):
                result = parse_match_sources(payload)
                self.assertEqual(result["status"], "invalid_payload")
                self.assertEqual(result["sources"], [])
        result = parse_match_sources(sample_channel(url_list=[None, {"text": "超清1080P", "url": []}]))
        self.assertTrue(result["errors"])
        self.assertIsNone(result["sources"][0]["candidate_url"])

    def test_real_login_error_is_preserved(self):
        result = parse_match_sources({"status": "error", "msg": "请先登录", "data": []})
        self.assertEqual(result["status"], "login_required")
        self.assertEqual(result["sources"], [])

    def test_restricted_entries_never_produce_candidates(self):
        cases = [("need_login", "login_required"), ("need_vip", "membership_required"),
                 ("need_pay", "payment_required"), ("is_paid", "payment_required")]
        for field, status in cases:
            with self.subTest(field=field):
                value = False if field == "is_paid" else True
                result = parse_match_sources(sample_channel(**{field: value}))
                self.assertEqual(result["status"], status)
                self.assertIsNone(result["sources"][0]["candidate_url"])
                self.assertIsNone(result["sources"][0]["rtmp_url"])
        payload = sample_channel()
        payload["video_live"]["section_info"][0]["data"][0]["url_list"][0]["url"] = "http://vip.client.com/2286843/rtmp%3A%2F%2Fspl.tiyucdn.com%2F2026%2F0917lcam28463"
        self.assertEqual(parse_match_sources(payload)["status"], "membership_required")

    def test_missing_url_and_nonofficial_are_not_streams(self):
        result = parse_match_sources(sample_channel(url_list=[{"text": "超清1080P"}]))
        self.assertEqual(result["sources"][0]["status"], "no_url")
        self.assertIsNone(result["sources"][0]["candidate_url"])
        result = parse_match_sources(sample_channel(name="第三方直播"))
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["skipped"]["non_official"], 1)

    def test_unknown_hosts_paths_and_wrong_match_are_not_guessed(self):
        for url, status in [
            ("rtmp://other.example/2026/stream", "unsupported_host"),
            ("rtmp://spl.tiyucdn.com/not-observed.flv", "unsupported_path"),
            ("rtmp://spl.tiyucdn.com.evil.example/2026/stream", "unsupported_host"),
            ("http://freevip.client.com/999/rtmp%3A%2F%2Fspl.tiyucdn.com%2F2026%2Fstream", "invalid_wrapper"),
            ("https://[malformed", "invalid_url"),
        ]:
            with self.subTest(url=url):
                payload = sample_channel(url_list=[{"url": url, "text": "高清720P"}])
                source = parse_match_sources(payload)["sources"][0]
                self.assertEqual(source["status"], status)
                self.assertIsNone(source["candidate_url"])


if __name__ == "__main__":
    unittest.main()
