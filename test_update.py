from datetime import datetime, timezone
import json
import unittest

import update


NOW = datetime(2026, 9, 18, 8, 30, tzinfo=timezone.utc)
STAMP = int(datetime(2026, 9, 18, 8, 0, tzinfo=timezone.utc).timestamp())


def schedule(finished=0):
    return {"name": "zhongyi", "data": [{"title": "第24轮", "list": [{
        "saishi_id": "2286843", "timestamp": str(STAMP), "内页": "zhibo/zuqiu/2026/match2286843vplayer.htm",
        "主队": "兰州陇原竞技", "客队": "温州", "is_finish": finished}]}]}


def detail():
    return {"match_id": 2286843, "video_live": {"section_info": [{"data": [{
        "name": "直播吧视频直播", "url_list": [
            {"text": "高清720P", "url": "http://freevip.client.com/2286843/rtmp%3A%2F%2Fspl.tiyucdn.com%2F2026%2Fexample_30fps"},
            {"text": "超清1080P", "url": "http://freevip.client.com/2286843/rtmp%3A%2F%2Fspl.tiyucdn.com%2F2026%2Fexample"}]}]}]}}


class UpdateTests(unittest.TestCase):
    def test_normalizes_real_schedule_shape_and_rejects_wrong_match_path(self):
        rows = update.normalize_schedule(schedule(), 2026)
        self.assertEqual(rows[0]["kickoff"], "2026-09-18T16:00:00+08:00")
        self.assertEqual(rows[0]["match_id"], "2286843")
        bad = schedule()
        bad["data"][0]["list"][0]["内页"] = "https://evil.example/stream"
        with self.assertRaises(update.UpdateError):
            update.normalize_schedule(bad, 2026)

    def test_live_end_and_postponement_prevent_detail_fetch(self):
        for code in (8, 9):
            m = update.normalize_schedule(schedule(), 2026)[0]
            row = [0] * 17
            row[2], row[3] = code, STAMP
            update.apply_status(m, {m["match_id"]: row}, NOW)
            self.assertFalse(update.should_check(m, NOW))

    def test_detail_window_excludes_old_matches_and_distant_future(self):
        m = update.normalize_schedule(schedule(), 2026)[0]
        self.assertTrue(update.should_check(m, NOW))
        m["kickoff_timestamp"] = int(NOW.timestamp()) - 4 * 3600 - 1
        self.assertFalse(update.should_check(m, NOW))
        m["kickoff_timestamp"] = int(NOW.timestamp()) + 7 * 86400 + 1
        self.assertFalse(update.should_check(m, NOW))

    def test_probe_checks_first_segment_with_only_512_byte_limit(self):
        calls = []
        def read(url, limit, **kwargs):
            calls.append((url, limit, kwargs))
            if url.endswith(".m3u8"):
                return b"#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\nfirst.ts\n"
            return (b"\x47" + b"\0" * 187) * 2
        self.assertTrue(update.probe_hls("https://spl.tiyucdn.com/2026/test.m3u8", read)["verified"])
        self.assertEqual(calls[-1][1], 512)
        self.assertTrue(calls[-1][2]["sample"])
        self.assertTrue(calls[-1][0].endswith("/first.ts"))

    def test_html_expired_encrypted_and_external_segments_fail_closed(self):
        samples = [b"<html>Please sign in</html>",
                   b"#EXTM3U\n#EXTINF:6,\none.ts\n#EXT-X-ENDLIST\n",
                   b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXTINF:6,\none.ts\n',
                   b"#EXTM3U\n#EXTINF:6,\nhttps://evil.example/a.ts\n"]
        for manifest in samples:
            self.assertFalse(update.probe_hls("https://spl.tiyucdn.com/2026/test.m3u8", lambda *a, **kw: manifest)["verified"])

    def test_bad_segment_is_not_published(self):
        def read(url, limit, **kwargs):
            return b"#EXTM3U\n#EXTINF:6,\none.ts\n" if url.endswith("m3u8") else b"access denied"
        self.assertFalse(update.probe_hls("https://spl.tiyucdn.com/2026/test.m3u8", read)["verified"])

    def test_highest_working_quality_fallback_and_all_playlist(self):
        match = update.normalize_schedule(schedule(), 2026)[0]
        update.inspect_match(match, NOW, lambda u: detail(), lambda u: {"verified": "30fps" in u, "status": "playable" if "30fps" in u else "probe_failed"})
        text = update.playlist([match])
        self.assertIn("高清720P", text)
        self.assertNotIn("超清1080P", text)
        self.assertEqual(text.count("#EXTINF"), 1)
        match["finished"] = True
        self.assertEqual(update.playlist([match]).count("#EXTINF"), 0)

    def test_all_live_sources_failed_aborts_and_preserves_old_publication(self):
        row = [0] * 17
        row[0], row[1], row[2], row[3] = 2286843, 355, 2, STAMP
        def get(url):
            if url == update.LIVE_URL:
                return {"matches": [row]}
            if "stats.qiumibao" in url:
                return schedule()
            return detail()
        with self.assertRaisesRegex(update.UpdateError, "all currently live"):
            update.build(2026, NOW, get, lambda u: {"verified": False, "status": "probe_failed"})

    def test_schedule_failure_is_fatal(self):
        with self.assertRaises(update.UpdateError):
            update.build(2026, NOW, lambda u: {"data": []})

    def test_successful_build_uses_highest_quality_and_records_all_checks(self):
        row = [0] * 17
        row[0], row[1], row[2], row[3] = 2286843, 355, 2, STAMP
        def get(url):
            if url == update.LIVE_URL:
                return {"matches": [row]}
            if "stats.qiumibao" in url:
                return schedule()
            return detail()
        files = update.build(2026, NOW, get, lambda u: {"verified": True, "status": "playable"})
        self.assertEqual(files["zhongyi.m3u"].count("#EXTINF"), 1)
        self.assertIn("超清1080P", files["zhongyi.m3u"])
        self.assertEqual(files["zhongyi-all.m3u"].count("#EXTINF"), 2)
        self.assertEqual(json.loads(files["status.json"])["playable_matches"], 1)
        self.assertIn("2026-09-18T16:30:00+08:00", files["index.html"])


if __name__ == "__main__":
    unittest.main()
