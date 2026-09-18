#!/usr/bin/env python3
"""Build a public China League Two playlist from official, unauthenticated data.

No credentials, guessed stream IDs, or authentication workarounds are used.
An advertised RTMP-to-HLS candidate is never published before a live manifest
and a small media sample both validate. Run: python update.py --output site
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import html
import json
from pathlib import Path
import re
import ssl
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener
from zoneinfo import ZoneInfo

from match_source import parse_match_sources

SHANGHAI = ZoneInfo("Asia/Shanghai")
DATA_HOSTS = frozenset({"stats.qiumibao.com", "matchs.qiumibao.com", "s.qiumibao.com"})
STREAM_HOSTS = frozenset({"spl.tiyucdn.com", "hslive.tiyucdn.com"})
LIVE_URL = "https://matchs.qiumibao.com/live/all.htm"
LIVE_STATES = {2, 3, 4, 5, 6, 7}
STATE_NAMES = {0: "比赛异常", 1: "未开始", 2: "上半场", 3: "中场", 4: "下半场",
               5: "加时", 6: "加时", 7: "点球", 8: "已结束", 9: "推迟"}


class UpdateError(RuntimeError):
    pass


def allow_url(url, hosts):
    p = urlsplit(url)
    if (p.scheme not in {"https", "http"} or p.hostname not in hosts
            or p.username or p.password or p.port not in (None, 80, 443)
            or p.fragment or any(ord(c) < 32 for c in url)):
        raise UpdateError("URL is outside the official allowlist")
    return url


class CheckedRedirect(HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        allow_url(newurl, self.hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_bytes(url, limit, *, hosts=DATA_HOSTS, sample=False):
    allow_url(url, hosts)
    headers = {"User-Agent": "ZhongyiSchedulePlaylist/1.0", "Accept-Encoding": "identity"}
    if sample:
        headers["Range"] = "bytes=0-511"
    opener = build_opener(CheckedRedirect(hosts))
    with opener.open(Request(url, headers=headers), timeout=12) as response:
        allow_url(response.url, hosts)
        # Even if Range is ignored, close the response after at most 512 bytes.
        data = response.read(limit if sample else limit + 1)
    if not sample and len(data) > limit:
        raise UpdateError("upstream response exceeded size limit")
    return data


def fetch_json(url):
    for attempt in range(2):
        try:
            return json.loads(fetch_bytes(url, 5_000_000).decode("utf-8-sig"))
        except HTTPError as exc:
            exc.close()
            if attempt or exc.code not in {408, 429, 500, 502, 503, 504}:
                raise
        except (URLError, TimeoutError, ConnectionError, json.JSONDecodeError):
            if attempt:
                raise
        time.sleep(0.4)


def schedule_url(year):
    return "https://stats.qiumibao.com/shuju/public/index.php?" + urlencode({
        "_url": "/data/index", "year": year, "league": "中乙", "league_id": 355,
        "type": "赛程", "tab": "赛程"})


def normalize_schedule(payload, year):
    if not isinstance(payload, dict) or payload.get("name") != "zhongyi" or not isinstance(payload.get("data"), list):
        raise UpdateError("official full-season schedule schema was not recognized")
    result, seen = [], set()
    for group in payload["data"]:
        if not isinstance(group, dict) or not isinstance(group.get("list"), list):
            raise UpdateError("invalid schedule round")
        for row in group["list"]:
            try:
                mid = str(row["saishi_id"])
                stamp = int(row["timestamp"])
                path = "/" + row["内页"].lstrip("/")
                date = datetime.fromtimestamp(stamp, SHANGHAI)
                if not mid.isdigit() or not re.fullmatch(r"/zhibo/zuqiu/\d{4}/match" + mid + r"v(?:player)?\.htm", path):
                    raise ValueError("invalid match path")
                if date.year != year or mid in seen:
                    raise ValueError("duplicate match or incorrect season")
                seen.add(mid)
                result.append({"match_id": mid, "round": group.get("title", ""),
                    "kickoff_timestamp": stamp, "kickoff": date.isoformat(),
                    "home": row["主队"], "away": row["客队"],
                    "home_logo": row.get("主队图标", ""), "away_logo": row.get("客队图标", ""),
                    "path": path, "page_url": "https://www.zhibo8.com" + path,
                    "finished": str(row.get("is_finish")) == "1", "score": row.get("比分", ""),
                    "sources": [], "source_status": "not_checked"})
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise UpdateError("invalid match in full-season schedule: " + str(exc)) from exc
    if not result:
        raise UpdateError("official full-season schedule is empty; preserve the previous publication")
    return sorted(result, key=lambda m: (m["kickoff_timestamp"], m["match_id"]))


def live_map(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("matches"), list):
        raise UpdateError("live status schema was not recognized")
    return {str(row[0]): row for row in payload["matches"]
            if isinstance(row, list) and len(row) > 16 and str(row[1]) == "355"}


def apply_status(match, current, now):
    row = current.get(match["match_id"])
    code = int(row[2]) if row is not None else None
    if row is not None:
        match["live_status_code"] = code
        match["finished"] = code == 8 or match["finished"]
        if code not in {0, 9} and int(row[3]) > 0:
            match["kickoff_timestamp"] = int(row[3])
            match["kickoff"] = datetime.fromtimestamp(int(row[3]), SHANGHAI).isoformat()
    match["match_status"] = ("已结束" if match["finished"] else STATE_NAMES.get(code)
        or ("未开始" if match["kickoff_timestamp"] > now.timestamp() else "待更新"))
    match["is_live"] = not match["finished"] and code in LIVE_STATES


def should_check(match, now, days=7):
    delta = match["kickoff_timestamp"] - now.timestamp()
    return (not match["finished"] and match.get("live_status_code") not in {0, 9}
            and -4 * 3600 <= delta <= days * 86400)


def read_stream_with_retry(read, url, limit, **kwargs):
    """Retry one transient transport failure, never access denial or bad media.

    Each underlying fetch retains its 12-second timeout, original size limit,
    TLS validation and redirect allowlist. Retry only the failed resource, not
    an entire probe or every previously validated segment.
    """
    for attempt in range(2):
        try:
            return read(url, limit, **kwargs)
        except HTTPError as exc:
            exc.close()
            retryable = exc.code in {408, 429} or 500 <= exc.code <= 599
            if attempt or not retryable:
                raise
        except URLError as exc:
            if attempt or isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise
        except (TimeoutError, ConnectionError):
            if attempt:
                raise
        time.sleep(0.4)


def probe_hls(url, read=None, depth=0):
    """Check a bounded live manifest and the first segment; download no full media."""
    read = read or fetch_bytes
    try:
        allow_url(url, STREAM_HOSTS)
        if depth > 2:
            raise UpdateError("manifest nesting limit")
        raw = read_stream_with_retry(read, url, 131_072, hosts=STREAM_HOSTS)
        text = raw.decode("utf-8-sig")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines or lines[0] != "#EXTM3U":
            raise UpdateError("not an HLS manifest")
        if "#EXT-X-ENDLIST" in lines:
            raise UpdateError("ended HLS stream")
        if any(line.startswith("#EXT-X-KEY:") and "METHOD=NONE" not in line for line in lines):
            raise UpdateError("encrypted stream is not published")
        if any(line.startswith("#EXT-X-STREAM-INF:") for line in lines):
            variants = []
            for index, line in enumerate(lines[:-1]):
                if line.startswith("#EXT-X-STREAM-INF:") and not lines[index + 1].startswith("#"):
                    bandwidth = re.search(r"(?:^|,)BANDWIDTH=(\d+)", line.split(":", 1)[1])
                    variants.append((int(bandwidth[1]) if bandwidth else 0, urljoin(url, lines[index + 1])))
            for _, variant in sorted(variants, reverse=True):
                check = probe_hls(variant, read, depth + 1)
                if check["verified"]:
                    return {**check, "checked_url": url}
            raise UpdateError("no readable official HLS variant")
        if not any(line.startswith("#EXTINF:") for line in lines):
            raise UpdateError("manifest has no media durations")
        segments = [line for line in lines if not line.startswith("#")]
        if not segments:
            raise UpdateError("manifest has no media segments")
        segment = urljoin(url, segments[0])
        allow_url(segment, STREAM_HOSTS)
        data = read_stream_with_retry(read, segment, 512, hosts=STREAM_HOSTS, sample=True)
        # MPEG-TS has a synchronization byte every 188 bytes. Accept an ID3
        # prefix only if two correctly spaced synchronization bytes are read.
        is_ts = any(data[i] == 0x47 and data[i + 188] == 0x47 for i in range(min(188, max(0, len(data) - 188))))
        is_mp4 = len(data) >= 12 and data[4:8] in {b"styp", b"moof", b"ftyp"}
        if not (is_ts or is_mp4):
            raise UpdateError("first segment is unreadable or not recognized media")
        return {"verified": True, "status": "playable", "checked_url": url,
                "segment_sample_bytes": len(data)}
    except Exception as exc:
        return {"verified": False, "status": "probe_failed", "error": str(exc)[:240]}


def inspect_match(match, now, get_json, probe):
    try:
        payload = get_json("https://s.qiumibao.com/m/ios/json" + match["path"])
        parsed = parse_match_sources(payload)
        if str(parsed.get("match_id")) != match["match_id"]:
            raise UpdateError("match detail ID mismatch")
        match["source_status"] = parsed["status"]
        for original in parsed["sources"]:
            source = dict(original)
            candidate = source.get("candidate_url")
            end = source.get("end_time")
            if end and str(end).isdigit() and int(end) <= now.timestamp():
                source.update(verified=False, status="broadcast_ended")
            elif candidate and source["status"] == "unverified":
                source.update(probe(candidate))
            match["sources"].append(source)
        if any(s.get("verified") for s in match["sources"]):
            match["source_status"] = "playable"
        elif any(s.get("status") == "probe_failed" for s in match["sources"]):
            match["source_status"] = "not_playable_yet" if match["kickoff_timestamp"] > now.timestamp() else "probe_failed"
        if parsed.get("errors"):
            match["source_errors"] = parsed["errors"]
    except Exception as exc:
        match["source_status"] = "detail_error"
        match["source_errors"] = [str(exc)[:240]]
    return match


def safe_label(text):
    return re.sub(r'[\r\n\x00-\x1f"]', " ", str(text))


def playlist(matches, all_qualities=False):
    output = ["#EXTM3U", "# Generated from official published match sources; only verified streams are included."]
    for match in matches:
        if match["finished"] or match.get("live_status_code") in {0, 8, 9}:
            continue
        playable = sorted((s for s in match["sources"] if s.get("verified") and s.get("candidate_url")),
                          key=lambda s: -(s.get("quality_height") or 0))
        if not all_qualities:
            playable = playable[:1]
        seen = set()
        for source in playable:
            url = source["candidate_url"]
            if url in seen:
                continue
            allow_url(url, STREAM_HOSTS)
            seen.add(url)
            label = safe_label(f'{match["kickoff"][5:16].replace("T", " ")} {match["home"]} vs {match["away"]} · {source["quality_label"]}')
            output.extend([f'#EXTINF:-1 tvg-id="zhongyi-{match["match_id"]}-{source.get("quality_height") or "default"}" group-title="中乙",{label}', url])
    return "\n".join(output) + "\n"


def render_index(matches, meta):
    esc = html.escape
    rows = []
    ordered = [m for m in matches if not m["finished"]] + list(reversed([m for m in matches if m["finished"]]))
    labels = {"playable": "已验证可播放", "not_checked": "待临近比赛检查", "no_official_source": "官方暂未公布直播源",
              "not_playable_yet": "直播源已公布，尚未通过播放检查", "detail_error": "详情获取失败",
              "probe_failed": "播放检查失败", "login_required": "需登录，未收录", "membership_required": "需会员，未收录",
              "payment_required": "需付费，未收录", "no_playable_source": "暂无可发布源", "candidates": "暂无可播放源"}
    for m in ordered:
        qualities = " / ".join(s["quality_label"] for s in m["sources"] if s.get("verified"))
        source_text = "已结束" if m["finished"] else labels.get(m["source_status"], m["source_status"])
        rows.append(f'<tr><td>{esc(m["kickoff"][5:16].replace("T", " "))}</td><td>{esc(m["round"])}</td>'
                    f'<td><a href="{esc(m["page_url"], quote=True)}" rel="noreferrer">{esc(m["home"])} · {esc(m["away"])}</a></td>'
                    f'<td>{esc(m["match_status"])}</td><td>{esc(qualities or source_text)}</td></tr>')
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>中乙直播订阅与赛程</title><style>body{{font:16px/1.7 system-ui,sans-serif;background:#f5f7fa;color:#172334;margin:0}}main{{max-width:1100px;margin:auto;padding:40px 22px}}h1{{font-size:32px;margin-bottom:4px}}p{{color:#526176}}.actions{{display:flex;gap:12px;flex-wrap:wrap;margin:25px 0}}.button{{background:#125ecf;color:white;border:0;border-radius:8px;padding:11px 16px;text-decoration:none;cursor:pointer;font:inherit}}.secondary{{background:#e1eaf7;color:#174577}}.table{{overflow:auto;background:white;border-radius:12px}}table{{border-collapse:collapse;width:100%;white-space:nowrap}}td,th{{padding:12px 15px;text-align:left;border-bottom:1px solid #edf0f4}}a{{color:#125ecf}}small{{color:#657184}}#copied{{min-height:26px}}</style><main>
<h1>中乙直播订阅</h1><p>{meta["year"]} 赛季 · 已验证可播放 {meta["playable_matches"]} 场 · 全赛季 {len(matches)} 场</p>
<p>默认订阅每场选择当前验证通过的最高官方画质。画质名称由官方标注；检查仅确认播放流可读取。未开播、尚未公布或验证失败的源不会进入订阅。</p>
<div class="actions"><button class="button" onclick="copyUrl('zhongyi.m3u')">复制订阅地址</button><a class="button secondary" href="zhongyi.m3u">下载默认 M3U</a><button class="button secondary" onclick="copyUrl('zhongyi-all.m3u')">复制全部画质订阅</button></div><div id="copied" role="status"></div>
<small>最近成功更新：{esc(meta["updated_shanghai"])}（北京时间）。订阅文件随任务更新，播放器需刷新订阅。<a href="status.json">运行状态</a> · <a href="schedule.json">完整赛程 JSON</a></small>
<h2>全赛季赛程</h2><div class="table"><table><thead><tr><th>北京时间</th><th>轮次</th><th>对阵</th><th>比赛状态</th><th>已验证画质 / 直播源状态</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p>数据来自直播吧公开官方赛程与官方比赛直播入口，仅收录无需登录即可验证的公开流。比赛改期与新直播源将在后续成功更新时反映。</p></main>
<script>async function copyUrl(file){{const url=new URL(file,location.href).href;document.getElementById('copied').textContent=url;try{{await navigator.clipboard.writeText(url);document.getElementById('copied').textContent='已复制：'+url}}catch(e){{document.getElementById('copied').textContent=url}}}}</script></html>'''


def build(year, now, get_json=fetch_json, probe=probe_hls, days=7):
    # The full schedule must succeed before any artifact can be replaced.
    matches = normalize_schedule(get_json(schedule_url(year)), year)
    warnings = []
    try:
        current = live_map(get_json(LIVE_URL))
    except Exception as exc:
        # Without current status, do not risk publishing an already-ended or
        # postponed match from stale season data.
        raise UpdateError("live status unavailable; preserving publication: " + str(exc)) from exc
    for match in matches:
        apply_status(match, current, now)
    selected = [m for m in matches if should_check(m, now, days)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda m: inspect_match(m, now, get_json, probe), selected))
    in_progress = [m for m in selected if m["is_live"]]
    if in_progress and not any(m["source_status"] == "playable" for m in in_progress):
        raise UpdateError("all currently live match sources failed validation; preserving previous publication")
    playable = [m for m in selected if m["source_status"] == "playable"]
    for m in selected:
        if m.get("source_errors"):
            warnings.append({"match_id": m["match_id"], "errors": m["source_errors"]})
    meta = {"year": year, "updated_utc": now.astimezone(timezone.utc).isoformat(),
            "updated_shanghai": now.astimezone(SHANGHAI).isoformat(), "lookahead_days": days,
            "total_matches": len(matches), "checked_matches": len(selected),
            "playable_matches": len(playable), "live_matches": len(in_progress),
            "quality_labels": "official_advertised_not_measured", "source": schedule_url(year)}
    status = {"ok": True, **meta, "warnings": warnings,
              "matches": [{"match_id": m["match_id"], "match_status": m["match_status"],
                           "source_status": m["source_status"], "sources": m["sources"],
                           "errors": m.get("source_errors", [])} for m in selected]}
    return {"schedule.json": json.dumps({"meta": meta, "matches": matches}, ensure_ascii=False, indent=2) + "\n",
            "status.json": json.dumps(status, ensure_ascii=False, indent=2) + "\n",
            "zhongyi.m3u": playlist(matches), "zhongyi-all.m3u": playlist(matches, True),
            "index.html": render_index(matches, meta)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="site")
    parser.add_argument("--year", type=int, default=datetime.now(SHANGHAI).year)
    parser.add_argument("--days", type=int, default=7, choices=range(1, 8))
    args = parser.parse_args()
    try:
        files = build(args.year, datetime.now(timezone.utc), days=args.days)
        destination = Path(args.output)
        destination.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            temporary = destination / (name + ".tmp")
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(destination / name)
        meta = json.loads(files["status.json"])
        print(f'Updated: {meta["total_matches"]} scheduled, {meta["checked_matches"]} checked, {meta["playable_matches"]} playable')
        return 0
    except Exception as exc:
        print("Update failed; existing output was not replaced: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
