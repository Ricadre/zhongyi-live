"""Parse official Zhibo8 match sources without network access or authentication.

Observed schema (both app-ios-latest.json and app-android-latest.json):
  match_id, match_title, start_time
  video_live.section_info[].data[]:
    name='直播吧视频直播', channel_id='kball_16727', start_time, end_time,
    live_status_url, url_list[].{text, s_text, url}, url (default quality)
  url is http://freevip.client.com/<match_id>/<percent-encoded RTMP URL>.
  The actual sample advertises 高清720P and 超清1080P; it contains n_l=true
  on 1080P. n_l has undocumented meaning and is NOT treated as access denial.

Only the observed official provider name and CDNs are accepted. The RTMP ->
HTTPS mapping was verified for sample matches; every produced candidate still needs
a fresh HLS/segment probe. Label height is advertised, not measured resolution.
No URL variants, stream identifiers, credentials, or authentication are guessed.

Return value: {match_id, title, status, sources, skipped, errors}.
sources is ordered by descending advertised quality and includes unavailable
official entries, which have candidate_url=None and an explanatory status.
All records always have verified=False; this module cannot validate a stream.
"""

import json
import re
from urllib.parse import unquote, urlsplit, urlunsplit


OFFICIAL_NAMES = frozenset({"直播吧视频直播"})
KNOWN_CDN_PATHS = {
    "spl.tiyucdn.com": r"/\d{4}/[A-Za-z0-9_-]+",
    # Observed in official match 2286896, same freevip wrapper and qualities.
    "hslive.tiyucdn.com": r"/zbbleft/[A-Za-z0-9_-]+",
}
RESTRICTED_STATES = frozenset({"login_required", "membership_required", "payment_required"})


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _true(value):
    return value is True or value == 1 or (isinstance(value, str) and value.lower() in {"1", "true", "yes"})


def _restriction(obj):
    """Fail closed on explicit access markers; never derive a hidden URL."""
    for field in ("need_login", "login_required", "require_login"):
        if _true(obj.get(field)):
            return "login_required"
    for field in ("need_vip", "is_vip", "vip_required", "membership_required"):
        if _true(obj.get(field)):
            return "membership_required"
    for field in ("need_pay", "is_pay", "pay_required", "payment_required"):
        if _true(obj.get(field)):
            return "payment_required"
    if "is_paid" in obj and obj["is_paid"] in (False, 0, "0"):
        return "payment_required"
    message = " ".join(_text(obj.get(k)) for k in ("msg", "message", "error", "status", "text"))
    lowered = message.lower()
    if any(s in message for s in ("请先登录", "需要登录", "登录后")) or "login_required" in lowered:
        return "login_required"
    if any(s in message for s in ("会员专享", "开通会员", "会员观看")) or "membership_required" in lowered:
        return "membership_required"
    if any(s in message for s in ("未支付", "请支付", "付费观看", "购买后")) or "payment_required" in lowered:
        return "payment_required"
    if str(obj.get("code", "")) in {"401", "403"}:
        return "login_required"
    return None


def _url_details(raw, match_id):
    """Return (rtmp_url, candidate_url, status), retaining only explicit input."""
    if not raw:
        return None, None, "no_url"
    if any(ord(char) < 32 for char in raw):
        return None, None, "invalid_url"
    try:
        parsed = urlsplit(raw)
        if parsed.username or parsed.password or parsed.port:
            return None, None, "invalid_url"
        host = parsed.hostname
        if host == "freevip.client.com" and parsed.scheme in {"http", "https"}:
            parts = parsed.path.split("/", 2)
            if len(parts) != 3 or not parts[1].isdigit() or (match_id is not None and parts[1] != str(match_id)):
                return None, None, "invalid_wrapper"
            if parsed.query or parsed.fragment:
                return None, None, "unsupported_wrapper"
            decoded = unquote(parts[2])
            if not decoded.startswith("rtmp://"):
                return None, None, "unsupported_wrapper"
            return _url_details(decoded, match_id)
        if host and host.endswith(".client.com"):
            if "vip" in host:
                return None, None, "membership_required"
            if "pay" in host:
                return None, None, "payment_required"
            if "login" in host:
                return None, None, "login_required"
            return None, None, "unsupported_wrapper"
        rtmp = raw if parsed.scheme == "rtmp" else None
        if host not in KNOWN_CDN_PATHS:
            return rtmp, None, "unsupported_host"
        if parsed.fragment:
            return rtmp, None, "invalid_url"
        if parsed.scheme == "rtmp":
            if not re.fullmatch(KNOWN_CDN_PATHS[host], parsed.path):
                return rtmp, None, "unsupported_path"
            return rtmp, urlunsplit(("https", parsed.netloc, parsed.path + ".m3u8", parsed.query, "")), "unverified"
        if parsed.scheme in {"http", "https"} and parsed.path.endswith(".m3u8"):
            return None, raw, "unverified"
        return None, None, "unsupported_url"
    except (ValueError, UnicodeError):
        return None, None, "invalid_url"


def parse_match_sources(payload):
    """Parse a decoded dict or JSON text/bytes; malformed input returns status.

    Sources contain provider, channel_id, quality_label, short_label,
    quality_height, source_url, rtmp_url, candidate_url, status, verified,
    start_time, end_time, live_status_url, and schema_path. Consumers should
    probe only candidate_url values with status='unverified', then select the
    first that actually validates. Never emit unavailable records into M3U.
    """
    result = {"match_id": None, "title": "", "status": "no_official_source", "sources": [],
              "skipped": {"animation": 0, "non_official": 0}, "errors": []}
    if isinstance(payload, (str, bytes, bytearray)):
        try:
            payload = json.loads(payload)
        except (ValueError, UnicodeError):
            result.update(status="invalid_payload", errors=["payload is not valid JSON"])
            return result
    if not isinstance(payload, dict):
        result.update(status="invalid_payload", errors=["payload must be a JSON object"])
        return result
    result["match_id"] = payload.get("match_id") if isinstance(payload.get("match_id"), (str, int)) else None
    result["title"] = _text(payload.get("match_title")) or _text(payload.get("title"))
    top_restriction = _restriction(payload)
    if top_restriction:
        result["status"] = top_restriction
        return result

    channels = []
    video = payload.get("video_live", {})
    if isinstance(video, dict):
        sections = video.get("section_info", [])
        if not isinstance(sections, list):
            result["errors"].append("video_live.section_info must be an array")
        else:
            for i, section in enumerate(sections):
                data = section.get("data", []) if isinstance(section, dict) else None
                if not isinstance(data, list):
                    result["errors"].append("section data must be an array")
                    continue
                channels.extend((item, f"video_live.section_info[{i}].data[{j}]") for j, item in enumerate(data))
    else:
        result["errors"].append("video_live must be an object")
    if isinstance(payload.get("channel"), list):
        channels.extend((item, f"channel[{j}]") for j, item in enumerate(payload["channel"]))

    seen = set()
    for channel, path in channels:
        if not isinstance(channel, dict):
            result["errors"].append(path + " must be an object")
            continue
        name = _text(channel.get("name"))
        channel_id = _text(channel.get("channel_id"))
        if "动画" in name or channel_id.startswith("animation_"):
            result["skipped"]["animation"] += 1
            continue
        if name not in OFFICIAL_NAMES:
            result["skipped"]["non_official"] += 1
            continue
        entries = channel.get("url_list")
        if entries is not None and not isinstance(entries, list):
            result["errors"].append(path + ".url_list must be an array")
        entries = entries if isinstance(entries, list) else []
        entries = [(entry, f"{path}.url_list[{i}]") for i, entry in enumerate(entries)]
        # A default can be a separate official source, but do not duplicate a
        # labelled quality already present in url_list.
        default = _text(channel.get("url"))
        listed = {_text(entry.get("url")) for entry, _ in entries if isinstance(entry, dict)}
        if (default and default not in listed) or not entries:
            entries.append(({"url": default, "text": "默认"}, path + ".url"))
        for entry, entry_path in entries:
            if not isinstance(entry, dict):
                result["errors"].append(entry_path + " must be an object")
                continue
            label = _text(entry.get("text")) or _text(entry.get("s_text")) or "默认"
            raw = _text(entry.get("url"))
            restricted = _restriction(channel) or _restriction(entry)
            rtmp, candidate, status = (None, None, restricted) if restricted else _url_details(raw, result["match_id"])
            quality = re.search(r"(?<!\d)(\d{3,4})\s*[pP](?!\d)", label)
            height = int(quality.group(1)) if quality else None
            key = (name, channel_id, raw, label, status)
            if key in seen:
                continue
            seen.add(key)
            result["sources"].append({
                "provider": name, "channel_id": channel_id, "quality_label": label,
                "short_label": _text(entry.get("s_text")), "quality_height": height,
                "source_url": raw or None, "rtmp_url": rtmp, "candidate_url": candidate,
                "status": status, "verified": False, "schema_path": entry_path,
                "start_time": channel.get("start_time"), "end_time": channel.get("end_time"),
                "live_status_url": _text(channel.get("live_status_url")) or None,
            })
    result["sources"].sort(key=lambda item: -(item["quality_height"] or 0))
    if any(item["candidate_url"] for item in result["sources"]):
        result["status"] = "candidates"
    elif result["sources"]:
        result["status"] = next((item["status"] for item in result["sources"] if item["status"] in RESTRICTED_STATES), "no_playable_source")
    elif result["errors"]:
        result["status"] = "invalid_payload"
    return result
