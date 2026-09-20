from datetime import datetime, timezone
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

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

    @patch("update.time.sleep")
    def test_handshake_and_segment_timeout_retry_only_failed_resource(self, sleep):
        calls = []
        attempts = {}
        def read(url, limit, **kwargs):
            calls.append((url, limit, kwargs))
            attempts[url] = attempts.get(url, 0) + 1
            if attempts[url] == 1:
                if url.endswith(".m3u8"):
                    raise URLError(TimeoutError("SSL handshake operation timed out"))
                raise TimeoutError("segment read timed out")
            if url.endswith(".m3u8"):
                return b"#EXTM3U\n#EXTINF:6,\nfirst.ts\n"
            return (b"\x47" + b"\0" * 187) * 2
        result = update.probe_hls("https://spl.tiyucdn.com/2026/test.m3u8", read)
        self.assertTrue(result["verified"])
        self.assertEqual(len(calls), 4)
        self.assertEqual(sleep.call_count, 2)
        for _, limit, kwargs in calls[-2:]:
            self.assertEqual(limit, 512)
            self.assertTrue(kwargs["sample"])

    @patch("update.time.sleep")
    def test_transient_http_status_retries_once(self, sleep):
        for status in (408, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                calls = []
                def read(url, limit, **kwargs):
                    calls.append(url)
                    if len(calls) == 1:
                        raise HTTPError(url, status, "transient", {}, None)
                    if url.endswith(".m3u8"):
                        return b"#EXTM3U\n#EXTINF:6,\nfirst.ts\n"
                    return (b"\x47" + b"\0" * 187) * 2
                self.assertTrue(update.probe_hls("https://spl.tiyucdn.com/2026/test.m3u8", read)["verified"])
                self.assertEqual(len(calls), 3)

    @patch("update.time.sleep")
    def test_denied_missing_or_invalid_tls_never_retry(self, sleep):
        url = "https://spl.tiyucdn.com/2026/test.m3u8"
        errors = [HTTPError(url, status, "not accessible", {}, None) for status in (401, 403, 404)]
        errors.append(URLError(ssl.SSLCertVerificationError("certificate verify failed")))
        for error in errors:
            with self.subTest(error=error):
                calls = []
                def read(*args, **kwargs):
                    calls.append(args)
                    raise error
                self.assertFalse(update.probe_hls(url, read)["verified"])
                self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

    @patch("update.time.sleep")
    def test_persistent_handshake_timeout_stops_after_two_attempts(self, sleep):
        calls = []
        def read(*args, **kwargs):
            calls.append(args)
            raise URLError(TimeoutError("SSL handshake operation timed out"))
        result = update.probe_hls("https://spl.tiyucdn.com/2026/test.m3u8", read)
        self.assertFalse(result["verified"])
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once_with(0.4)

    def test_highest_working_quality_fallback_and_all_playlist(self):
        match = update.normalize_schedule(schedule(), 2026)[0]
        update.inspect_match(match, NOW, lambda u: detail(), lambda u: {"verified": "30fps" in u, "status": "playable" if "30fps" in u else "probe_failed"})
        text = update.playlist([match])
        self.assertIn("高清720P", text)
        self.assertNotIn("超清1080P", text)
        self.assertEqual(text.count("#EXTINF"), 1)
        self.assertNotIn(update.STANDBY_URL, text)
        match["finished"] = True
        self.assertEqual(update.playlist([match], now=NOW).count("#EXTINF"), 1)
        self.assertIn('tvg-id="zhongyi-status"', update.playlist([match], now=NOW))
        self.assertNotIn("example_30fps.m3u8", update.playlist([match], now=NOW))

    def test_empty_default_and_all_playlists_have_one_accurate_next_match_entry(self):
        future = schedule(finished=1)
        future["data"][0]["list"].append({
            "saishi_id": "2287000", "timestamp": str(int(datetime(2026, 10, 6, 7, tzinfo=timezone.utc).timestamp())),
            "内页": "zhibo/zuqiu/2026/match2287000vplayer.htm", "主队": "长春喜都", "客队": "广州蒲公英", "is_finish": 0})
        calls = []
        def get(url):
            calls.append(url)
            if url == update.LIVE_URL:
                return {"matches": []}
            if "stats.qiumibao" in url:
                return future
            self.fail("distant next match should not require a detail fetch")
        files = update.build(2026, NOW, get)
        label = "暂无可用直播 · 下场 10-06 15:00 长春喜都 vs 广州蒲公英"
        for name in ("zhongyi.m3u", "zhongyi-all.m3u"):
            with self.subTest(name=name):
                self.assertEqual(files[name].count("#EXTINF:"), 1)
                self.assertIn('tvg-id="zhongyi-status" group-title="中乙·赛程提示",' + label, files[name])
                self.assertEqual(files[name].splitlines()[-1], update.STANDBY_URL)
        meta = json.loads(files["status.json"])
        self.assertEqual(meta["playable_matches"], 0)
        self.assertEqual(meta["playlist_entries"], 1)
        self.assertEqual(meta["all_playlist_entries"], 1)
        self.assertTrue(meta["standby"])
        self.assertEqual(meta["next_match"]["match_id"], "2287000")
        self.assertEqual(meta["standby_state"], "awaiting_next_match")
        self.assertIn("10-06 15:00（北京时间）长春喜都 vs 广州蒲公英", files["index.html"])
        self.assertIn("播放静态提示视频", files["index.html"])
        self.assertIn('name="robots" content="noindex, nofollow"', files["index.html"])

    def test_empty_status_distinguishes_live_unavailable_and_no_future_schedule(self):
        match = update.normalize_schedule(schedule(), 2026)[0]
        match["is_live"] = True
        text = update.playlist([match], now=NOW)
        self.assertIn("暂无可用直播 · 当前 1 场比赛进行中", text)
        self.assertNotIn("下场", text)
        match["is_live"] = False
        match["finished"] = True
        text = update.playlist([match], now=NOW)
        self.assertIn("暂无已确定的后续赛程", text)
        self.assertEqual(text.count("#EXTINF:"), 1)
        match["finished"] = False
        match["kickoff_timestamp"] = int(NOW.timestamp()) + 3600
        match["live_status_code"] = 9
        self.assertIsNone(update.standby_info([match], NOW)["next_match"])

    def test_main_copies_real_asset_each_run_and_keeps_previous_if_asset_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "source"
            asset = source / "assets" / "standby.mp4"
            asset.parent.mkdir(parents=True)
            destination = directory / "site"
            files = {"zhongyi.m3u": "#EXTM3U\nnew publication\n", "status.json": json.dumps({
                "total_matches": 360, "checked_matches": 0, "playable_matches": 0})}
            first_video = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
            asset.write_bytes(first_video)
            with patch.object(update, "__file__", str(source / "update.py")), \
                    patch.object(update, "build", return_value=files), \
                    patch("update.sys.argv", ["update.py", "--output", str(destination)]), \
                    patch("builtins.print"):
                self.assertEqual(update.main(), 0)
                self.assertEqual((destination / "assets" / "standby.mp4").read_bytes(), first_video)
                asset.write_bytes(first_video + b"updated")
                self.assertEqual(update.main(), 0)
                self.assertEqual((destination / "assets" / "standby.mp4").read_bytes(), first_video + b"updated")
                asset.unlink()
                files["zhongyi.m3u"] = "should not be published"
                self.assertEqual(update.main(), 1)
                self.assertEqual((destination / "zhongyi.m3u").read_text(), "#EXTM3U\nnew publication\n")
                self.assertEqual((destination / "assets" / "standby.mp4").read_bytes(), first_video + b"updated")

    def test_main_staging_failure_preserves_previous_playlist_and_video(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "source"
            asset = source / "assets" / "standby.mp4"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"new video")
            destination = directory / "site"
            (destination / "assets").mkdir(parents=True)
            (destination / "assets" / "standby.mp4").write_bytes(b"previous video")
            (destination / "zhongyi.m3u").write_text("previous playlist")
            # A directory at a staging filename simulates an output write error.
            (destination / "index.html.tmp").mkdir()
            with patch.object(update, "__file__", str(source / "update.py")), \
                    patch.object(update, "build", return_value={"zhongyi.m3u": "new playlist", "index.html": "new page"}), \
                    patch("update.sys.argv", ["update.py", "--output", str(destination)]), \
                    patch("builtins.print"):
                self.assertEqual(update.main(), 1)
            self.assertEqual((destination / "zhongyi.m3u").read_text(), "previous playlist")
            self.assertEqual((destination / "assets" / "standby.mp4").read_bytes(), b"previous video")

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
        meta = json.loads(files["status.json"])
        self.assertEqual(meta["playable_matches"], 1)
        self.assertEqual(meta["playlist_entries"], 1)
        self.assertEqual(meta["all_playlist_entries"], 2)
        self.assertFalse(meta["standby"])
        for name in ("zhongyi.m3u", "zhongyi-all.m3u"):
            self.assertNotIn(update.STANDBY_URL, files[name])
            self.assertNotIn("zhongyi-status", files[name])
        self.assertNotIn("播放静态提示视频", files["index.html"])
        self.assertIn("2026-09-18T16:30:00+08:00", files["index.html"])


if __name__ == "__main__":
    unittest.main()
