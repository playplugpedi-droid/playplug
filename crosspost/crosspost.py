#!/usr/bin/env python3
"""
crosspost.py - watch your social accounts for new videos and repost them.

How it works
  1. DETECT  new videos on YouTube (public RSS feed) and on TikTok / Facebook /
             Instagram (through the Windsor.ai API you already connected).
  2. DRAFT   keep the original title + description when they exist; when one is
             missing, ask Claude (optional) or a simple template to write it.
  3. QUEUE   every new video becomes a pending job. Nothing is posted yet.
  4. REVIEW  you approve / edit / schedule each job, then it is published to the
             other platforms through the Ayrshare API.

Safe by default: `scan` never posts, the first scan only records what already
exists (so your back catalogue is NOT reposted), `review --dry-run` prints the
exact requests without sending them, and posts you make yourself are never
echoed back (loop protection). Only repost videos you own.

Python 3.9+, standard library only. See README.md.
"""
from __future__ import annotations

import argparse
import copy
import json
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "1.0"
PLATFORMS = ("youtube", "tiktok", "facebook", "instagram")
AYR_OPTIONS_KEY = {
    "youtube": "youTubeOptions",
    "facebook": "faceBookOptions",
    "instagram": "instagramOptions",
    "tiktok": "tikTokOptions",
}
TEXT_LIMIT = {"youtube": 5000, "tiktok": 2200, "instagram": 2200, "facebook": 10000}

DEFAULT_CONFIG = {
    "sources": {
        "youtube": {"type": "youtube_rss", "channel_id": "UCNdaqe9hoXwOZrkYT3uO2xw"},
        "tiktok": {"type": "windsor", "connector": "tiktok_organic"},
        "facebook": {"type": "windsor", "connector": "facebook_organic"},
        "instagram": {"type": "windsor", "connector": "instagram"},
    },
    # Where each source's videos may be reposted. Anything not listed here
    # defaults to "every other platform". Remove a platform to stop posting there.
    "destinations": {
        "youtube": ["tiktok", "facebook", "instagram"],
        "tiktok": ["youtube", "facebook", "instagram"],
        "facebook": ["youtube", "tiktok", "instagram"],
        "instagram": ["youtube", "tiktok", "facebook"],
    },
    "lookback_days": 7,
    "max_jobs_per_scan": 5,
    "max_posts_per_day": 10,
    "brand_notes": "Play Plug Music - music content. Natural tone, no misleading claims.",
    "ai": {"enabled": True, "model": "claude-sonnet-5-5"},
    "download_with_ytdlp": True,
    # Which service posts to each platform: "ayrshare" (default) or "direct"
    # (your social-publisher tool, using each platform's own API).
    "backend": {"default": "ayrshare", "per_platform": {}},
    # Folder where social-publisher is installed/unzipped (used by the "direct" backend)
    "direct_cli_dir": "..",
    # Extra Ayrshare options per platform, merged over the defaults, e.g.
    # {"youtube": {"visibility": "unlisted"}}
    "post_options": {},
}

ENV_EXAMPLE = """\
# Copy this file to .env (same folder as crosspost.py) and fill in your keys.
# Never share or upload this file.
WINDSOR_API_KEY=
AYRSHARE_API_KEY=
# Optional: lets Claude write missing titles/descriptions
ANTHROPIC_API_KEY=
"""


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
class HttpError(Exception):
    pass


class Fatal(Exception):
    pass


def base_dir() -> Path:
    return Path(os.environ.get("CROSSPOST_HOME") or Path(__file__).resolve().parent)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def redact(s: str) -> str:
    return re.sub(r"(api_key=)[^&\s]+", r"\1***", s)


def load_env():
    p = base_dir() / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        if v:
            os.environ.setdefault(k.strip(), v)


def http_real(method, url, headers=None, body=None, timeout=60, raw=False):
    h = {"User-Agent": "crosspost/" + VERSION}
    h.update(headers or {})
    data = body
    if body is not None and not isinstance(body, (bytes, bytearray)):
        data = json.dumps(body).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:500].decode("utf-8", "replace")
        raise HttpError(f"{method} {redact(url)} -> HTTP {e.code}: {detail}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise HttpError(f"{method} {redact(url)} -> {e}")
    if raw:
        return payload
    return json.loads(payload) if payload.strip() else {}


HTTP = http_real  # the demo swaps this for a fake
INPUT = input  # the demo swaps this for scripted answers


def load_config() -> dict:
    p = base_dir() / "config.json"
    if not p.exists():
        raise Fatal("No config.json yet - run:  python crosspost.py init")
    user = json.loads(p.read_text(encoding="utf-8"))
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict) and k not in ("sources", "destinations"):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def load_state() -> dict:
    p = base_dir() / "state.json"
    st = {"baselined": [], "seen": {}, "sent": {p_: [] for p_ in PLATFORMS}, "queue": [], "history": []}
    if p.exists():
        st.update(json.loads(p.read_text(encoding="utf-8")))
    return st


def save_state(st: dict):
    p = base_dir() / "state.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


# --------------------------------------------------------------------------- #
# text fingerprints (used to recognise our own reposts and avoid loops)
# --------------------------------------------------------------------------- #
def strip_tags(s):
    return re.sub(r"#\w+", "", s or "")


def norm(s):
    return re.sub(r"[\W_]+", " ", strip_tags(s).lower()).strip()


def fp(s):
    return norm(s)[:60].strip()


def same(a, b):
    return len(a) >= 8 and len(b) >= 8 and (a.startswith(b) or b.startswith(a))


def video_fp(v):
    return fp(v.get("title") or v.get("description") or "")


# --------------------------------------------------------------------------- #
# sources
# --------------------------------------------------------------------------- #
WINDSOR_FIELDS = {
    "tiktok_organic": ["video_id", "video_caption", "video_share_url", "video_create_datetime", "video_duration"],
    "facebook_organic": ["id", "title", "description", "source", "video_permalink_url", "created_time",
                         "length", "is_crosspost_video", "video_post_type"],
    "instagram": ["media_id", "media_caption", "media_permalink", "media_type", "media_url", "timestamp"],
}


def adapt_row(platform, r):
    """Turn one Windsor row into our common video dict (or None to ignore it)."""
    if platform == "tiktok":
        if not r.get("video_id"):
            return None
        return dict(platform="tiktok", id=str(r["video_id"]), title="", description=r.get("video_caption") or "",
                    url=r.get("video_share_url") or "", media_url="",
                    published=str(r.get("video_create_datetime") or r.get("date") or ""),
                    duration=num(r.get("video_duration")))
    if platform == "facebook":
        if not r.get("id"):
            return None
        v = dict(platform="facebook", id=str(r["id"]), title=r.get("title") or "",
                 description=r.get("description") or "", url=r.get("video_permalink_url") or "",
                 media_url=r.get("source") or "", published=str(r.get("created_time") or ""),
                 duration=num(r.get("length")))
        if str(r.get("is_crosspost_video")).lower() == "true":
            v["skip_reason"] = "Facebook marks it as a crosspost from another page"
        return v
    if platform == "instagram":
        if not r.get("media_id") or str(r.get("media_type", "")).upper() not in ("VIDEO", "REEL"):
            return None
        return dict(platform="instagram", id=str(r["media_id"]), title="", description=r.get("media_caption") or "",
                    url=r.get("media_permalink") or "", media_url=r.get("media_url") or "",
                    published=str(r.get("timestamp") or ""), duration=None)
    return None


def fetch_windsor(name, src, cfg):
    key = os.environ.get("WINDSOR_API_KEY")
    if not key:
        raise Fatal("WINDSOR_API_KEY is not set (put it in .env)")
    connector = src["connector"]
    q = {"api_key": key, "date_preset": f"last_{int(cfg['lookback_days'])}d",
         "fields": ",".join(WINDSOR_FIELDS[connector])}
    res = HTTP("GET", f"https://connectors.windsor.ai/{connector}?" + urllib.parse.urlencode(q, safe=","))
    rows = res.get("data", []) if isinstance(res, dict) else res
    out, seen = [], set()
    for r in rows or []:
        v = adapt_row(name, r)
        if v and v["id"] not in seen:
            seen.add(v["id"])
            out.append(v)
    return out


def fetch_youtube_rss(name, src, cfg):
    url = "https://www.youtube.com/feeds/videos.xml?channel_id=" + urllib.parse.quote(src["channel_id"])
    xml = HTTP("GET", url, raw=True)
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
          "m": "http://search.yahoo.com/mrss/"}
    root = ET.fromstring(xml)
    out = []
    for e in root.findall("a:entry", ns):
        vid = e.findtext("yt:videoId", namespaces=ns)
        if not vid:
            continue
        out.append(dict(platform="youtube", id=vid, title=(e.findtext("a:title", namespaces=ns) or "").strip(),
                        description=(e.findtext("m:group/m:description", namespaces=ns) or "").strip(),
                        url="https://www.youtube.com/watch?v=" + vid, media_url="",
                        published=e.findtext("a:published", namespaces=ns) or "", duration=None))
    return out


def fetch_source(name, src, cfg):
    t = src.get("type")
    if t == "windsor":
        return fetch_windsor(name, src, cfg)
    if t == "youtube_rss":
        return fetch_youtube_rss(name, src, cfg)
    raise Fatal(f"Unknown source type for {name}: {t}")


# --------------------------------------------------------------------------- #
# drafting titles / descriptions
# --------------------------------------------------------------------------- #
def template_title(text, brand):
    t = re.sub(r"\s+", " ", strip_tags(text)).strip()
    t = re.split(r"(?<=[.!?])\s|\n", t)[0] if t else ""
    if len(t) > 90:
        t = t[:90].rsplit(" ", 1)[0] + "..."
    return t or "New video from " + (brand.split(" - ")[0] or "us")


def ai_draft(v, cfg):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not cfg["ai"].get("enabled", True):
        return None
    known = {k: v.get(k) for k in ("title", "description", "duration") if v.get(k)}
    prompt = (
        "Write a title and description for a short social video that is being reposted to other platforms.\n"
        f"Brand notes: {cfg.get('brand_notes', '')}\n"
        f"Original platform: {v['platform']}\n"
        f"What is known about the video (you cannot see the video itself): {json.dumps(known, ensure_ascii=False)}\n\n"
        "Rules: title max 90 characters; description 1-3 short sentences; 3-6 hashtags (no # sign). "
        "Reuse the language of the existing text. Do NOT invent facts, names, dates or links that are not "
        "in the information above. No misleading clickbait.\n"
        'Reply with ONLY JSON: {"title": "...", "description": "...", "hashtags": ["..."]}'
    )
    body = {"model": cfg["ai"].get("model", "claude-sonnet-5-5"), "max_tokens": 600,
            "messages": [{"role": "user", "content": prompt}]}
    try:
        res = HTTP("POST", "https://api.anthropic.com/v1/messages",
                   headers={"x-api-key": key, "anthropic-version": "2023-06-01"}, body=body)
        text = "".join(b.get("text", "") for b in res.get("content", []) if b.get("type") == "text")
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        d = json.loads(text)
        return {"title": str(d.get("title", ""))[:100], "description": str(d.get("description", "")),
                "hashtags": [str(h).lstrip("#") for h in d.get("hashtags", [])][:6]}
    except (HttpError, ValueError, KeyError, TypeError) as e:
        print(f"    (Claude draft failed: {e}; using template)")
        return None


def build_draft(v, cfg):
    title, desc = (v.get("title") or "").strip(), (v.get("description") or "").strip()
    if title and desc:
        return {"title": title, "description": desc, "hashtags": [], "by": "original", "note": ""}
    has_text = bool(title or desc)
    ai = ai_draft(v, cfg) if has_text else None
    if ai:
        return {"title": title or ai["title"], "description": desc or ai["description"],
                "hashtags": ai["hashtags"], "by": "claude", "note": "Missing text was drafted by Claude - check it."}
    brand = cfg.get("brand_notes", "")
    t = title or template_title(desc, brand)
    note = "Missing text was filled with a simple template - edit it." if has_text else \
        "The video has no text at all and Claude cannot see the video - write the title/description yourself."
    return {"title": t, "description": desc or t, "hashtags": [], "by": "template", "note": note}


# --------------------------------------------------------------------------- #
# scanning
# --------------------------------------------------------------------------- #
def mark_seen(state, v):
    state["seen"][f"{v['platform']}:{v['id']}"] = {
        "platform": v["platform"], "fp": video_fp(v), "title": (v.get("title") or v.get("description") or "")[:80],
        "ts": now_iso()}


def is_echo(state, v):
    me = video_fp(v)
    return any(same(me, e["fp"]) for e in state["sent"].get(v["platform"], []))


def pick_destinations(cfg, state, v):
    dests = cfg["destinations"].get(v["platform"]) or [p for p in PLATFORMS if p != v["platform"]]
    dests = [d for d in dests if d in PLATFORMS and d != v["platform"]]
    me = video_fp(v)
    for info in state["seen"].values():  # already on that platform (e.g. you posted it by hand)?
        if info["platform"] in dests and same(me, info["fp"]):
            dests.remove(info["platform"])
    return dests


def scan(cfg, state):
    new_jobs = []
    for name, src in cfg["sources"].items():
        if src.get("enabled", True) is False:
            continue
        try:
            vids = fetch_source(name, src, cfg)
        except (HttpError, Fatal, ET.ParseError, ValueError) as e:
            print(f"  [{name}] could not read: {e}")
            continue
        if name not in state["baselined"]:
            for v in vids:
                mark_seen(state, v)
            state["baselined"].append(name)
            print(f"  [{name}] first run: recorded {len(vids)} existing video(s); only NEW uploads will be picked up.")
            continue
        fresh = 0
        for v in sorted(vids, key=lambda x: x["published"]):
            key = f"{v['platform']}:{v['id']}"
            if key in state["seen"]:
                continue
            if len(new_jobs) >= cfg["max_jobs_per_scan"]:
                print(f"  [{name}] job limit reached; the rest will be picked up next scan.")
                break
            mark_seen(state, v)
            label = (v["title"] or v["description"])[:50].replace("\n", " ")
            if v.get("skip_reason"):
                print(f"  [{name}] skipped '{label}': {v['skip_reason']}")
                continue
            if is_echo(state, v):
                print(f"  [{name}] '{label}' is one of your own reposts - skipped (loop protection).")
                continue
            dests = pick_destinations(cfg, state, v)
            if not dests:
                print(f"  [{name}] '{label}' already exists on the other platforms - nothing to do.")
                continue
            draft = build_draft(v, cfg)
            job = {"id": uuid.uuid4().hex[:8], "status": "pending", "created": now_iso(),
                   "source": v["platform"], "source_id": v["id"], "url": v["url"], "media_url": v["media_url"],
                   "media_file": None, "duration": v["duration"], "orig_title": v["title"],
                   "orig_description": v["description"], "draft": draft, "destinations": dests, "done": []}
            state["queue"].append(job)
            new_jobs.append(job)
            fresh += 1
            print(f"  [{name}] NEW: '{label}'  -> queued for {', '.join(dests)}  (text: {draft['by']})")
        if not fresh:
            print(f"  [{name}] nothing new.")
    return new_jobs


# --------------------------------------------------------------------------- #
# publishing
# --------------------------------------------------------------------------- #
def ayr(method, path, body=None):
    key = os.environ.get("AYRSHARE_API_KEY")
    if not key:
        raise Fatal("AYRSHARE_API_KEY is not set (put it in .env)")
    return HTTP(method, "https://api.ayrshare.com/api" + path, headers={"Authorization": "Bearer " + key}, body=body)


def compose_text(plat, d):
    title, desc = d["title"].strip(), d["description"].strip()
    if plat == "youtube":
        parts = [desc or title]
    else:
        parts = ([title] if title and norm(title) not in norm(desc) else []) + ([desc] if desc else [])
    text = "\n\n".join(parts)
    tags = []
    for t in d.get("hashtags", []):
        tag = "#" + str(t).lstrip("#")
        if tag.lower() not in text.lower():
            tags.append(tag)
    if tags:
        text += "\n\n" + " ".join(tags)
    return text[:TEXT_LIMIT[plat]]


def build_payload(job, plat, cfg, media_url, schedule=None):
    d = job["draft"]
    payload = {"post": compose_text(plat, d), "platforms": [plat], "mediaUrls": [media_url], "isVideo": True}
    if schedule:
        payload["scheduleDate"] = schedule
    dur = job.get("duration")
    opts = {}
    if plat == "youtube":
        opts = {"title": (d["title"] or "New video")[:100], "visibility": "public"}
        if dur and dur <= 60:
            opts["shorts"] = True
    elif plat in ("facebook", "instagram") and dur and dur <= 90:
        opts = {"reels": True}
    opts.update(cfg.get("post_options", {}).get(plat, {}))
    if opts:
        payload[AYR_OPTIONS_KEY[plat]] = opts
    return payload


def ayrshare_upload(path):
    name = Path(path).name
    ctype = mimetypes.guess_type(name)[0] or "video/mp4"
    info = ayr("GET", "/media/uploadUrl?" + urllib.parse.urlencode({"fileName": name, "contentType": ctype}))
    data = Path(path).read_bytes()
    HTTP("PUT", info["uploadUrl"], headers={"Content-Type": ctype}, body=data, raw=True, timeout=600)
    return info["accessUrl"]


def download_with_ytdlp(job):
    url = job.get("url") or ""
    if not url.startswith("https://") or not shutil.which("yt-dlp"):
        return None
    out_dir = base_dir() / "downloads"
    out_dir.mkdir(exist_ok=True)
    print("    downloading the video with yt-dlp ...")
    try:
        subprocess.run(["yt-dlp", "-f", "mp4/best", "-o", str(out_dir / (job["id"] + ".%(ext)s")), "--", url],
                       check=True, timeout=900)
    except (subprocess.SubprocessError, OSError) as e:
        print(f"    download failed: {e}")
        return None
    files = sorted(out_dir.glob(job["id"] + ".*"))
    return str(files[0]) if files else None


def resolve_media(job, cfg):
    if job.get("media_file"):
        return ayrshare_upload(job["media_file"])
    if job.get("media_url"):
        return job["media_url"]
    if cfg.get("download_with_ytdlp", True):
        path = download_with_ytdlp(job)
        if path:
            job["media_file"] = path
            return ayrshare_upload(path)
    return None


def backend_for(plat, cfg):
    b = cfg.get("backend") or {}
    return b.get("per_platform", {}).get(plat) or b.get("default") or "ayrshare"


def direct_cmd(job, plat, cfg, path, ig_url=None):
    d = job["draft"]
    cmd = [sys.executable, "-m", "social_publisher.cli", "publish", path, "--platforms", plat,
           "--title", (d["title"] or "New video")[:100], "--description", compose_text(plat, d),
           "--privacy", "public"]
    tags = ",".join(str(t).lstrip("#") for t in d.get("hashtags", []))
    if tags:
        cmd += ["--tags", tags]
    if ig_url:
        cmd += ["--ig-url", ig_url]
    return cmd


def direct_cwd(cfg):
    return str((base_dir() / cfg.get("direct_cli_dir", "../social-publisher")).resolve())


def resolve_local(job, cfg):
    if job.get("media_file") and Path(job["media_file"]).exists():
        return job["media_file"]
    if cfg.get("download_with_ytdlp", True):
        path = download_with_ytdlp(job)
        if path:
            job["media_file"] = path
            return path
    return None


def publish_direct(job, plat, cfg, dry_run):
    """Post one platform through the social-publisher CLI. Returns (ok, link)."""
    path = job.get("media_file") if dry_run else resolve_local(job, cfg)
    if not path:
        print(f"    {plat}: the direct backend needs the video as a local file (use [m] to give a path, or install yt-dlp).")
        return False, ""
    ig_url = None
    if plat == "instagram":
        ig_url = job.get("media_url")
        if not ig_url and not dry_run and os.environ.get("AYRSHARE_API_KEY"):
            try:
                ig_url = ayrshare_upload(path)
            except (HttpError, Fatal, OSError) as e:
                print(f"    instagram: could not host the video for Instagram: {e}")
        if not ig_url and not dry_run:
            print("    instagram: Instagram needs a public video URL; set one with [m] or add AYRSHARE_API_KEY to host it.")
            return False, ""
    cmd = direct_cmd(job, plat, cfg, path, ig_url)
    if dry_run:
        print(f"    [dry run] {' '.join(shlex.quote(c) for c in cmd)}  (in {direct_cwd(cfg)})")
        return False, ""
    try:
        r = subprocess.run(cmd, cwd=direct_cwd(cfg), capture_output=True, text=True, timeout=1800)
    except (subprocess.SubprocessError, OSError) as e:
        print(f"    {plat}: FAILED - could not run social-publisher: {e}")
        return False, ""
    out = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"^\s*ok:\s*(\S*)", out, re.M)
    if r.returncode == 0 and m:
        return True, m.group(1)
    print(f"    {plat}: FAILED - {out.strip()[-300:]}")
    return False, ""


def posts_last_24h(state):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    n = 0
    for h in state["history"]:
        try:
            if datetime.strptime(h["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) > cutoff:
                n += 1
        except (KeyError, ValueError):
            pass
    return n


def publish(job, cfg, state, dry_run=False, schedule=None):
    """Post the job to its remaining destinations. Returns True if everything went out."""
    media = None
    if any(backend_for(p, cfg) == "ayrshare" for p in job["destinations"]):
        if dry_run:
            media = job.get("media_url") or job.get("media_file") or "https://example.invalid/video.mp4 (placeholder)"
        else:
            try:
                media = resolve_media(job, cfg)
            except (HttpError, Fatal, KeyError, OSError) as e:
                print(f"    could not prepare the video: {e}")
                return False
        if not media:
            print("    No video file available for this job. Choose [m] to give a file path or a public video URL.")
            return False
    for plat in list(job["destinations"]):
        if not dry_run and posts_last_24h(state) >= cfg["max_posts_per_day"]:
            print(f"    daily safety limit ({cfg['max_posts_per_day']} posts) reached - stopping.")
            break
        if backend_for(plat, cfg) == "direct":
            if schedule:
                print(f"    {plat}: the direct backend cannot schedule posts - choose publish-now, or use Ayrshare for this platform.")
                continue
            ok, link = publish_direct(job, plat, cfg, dry_run)
            if dry_run or not ok:
                continue
            links = [link] if link else []
            payload = {"post": compose_text(plat, job["draft"])}
        else:
            payload = build_payload(job, plat, cfg, media, schedule)
            if dry_run:
                print(f"    [dry run] POST https://api.ayrshare.com/api/post  ({plat})")
                print("    " + json.dumps(payload, indent=2, ensure_ascii=False).replace("\n", "\n    "))
                continue
            try:
                res = ayr("POST", "/post", payload)
            except (HttpError, Fatal) as e:
                print(f"    {plat}: FAILED - {e}")
                continue
            bad = res.get("status") == "error" or res.get("errors")
            if bad:
                print(f"    {plat}: FAILED - {json.dumps(res)[:300]}")
                continue
            links = [p.get("postUrl") for p in res.get("postIds", []) if p.get("postUrl")]
        print(f"    {plat}: {'scheduled' if schedule else 'posted'} {' '.join(links)}")
        for text in {fp(job["draft"]["title"]), fp(payload["post"])}:
            if text:
                state["sent"].setdefault(plat, []).append({"fp": text, "from": f"{job['source']}:{job['source_id']}",
                                                           "ts": now_iso()})
        state["history"].append({"ts": now_iso(), "job": job["id"], "from": job["source"], "to": plat,
                                 "scheduled": schedule or "", "links": links})
        job["destinations"].remove(plat)
        job["done"].append(plat)
    if dry_run:
        return False
    job["status"] = "done" if not job["destinations"] else "partial"
    return not job["destinations"]


def parse_when(s):
    s = s.strip()
    if s.isdigit():
        dt = datetime.now(timezone.utc) + timedelta(minutes=int(s))
    else:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def show_job(job, i, n):
    d = job["draft"]
    media = "file " + job["media_file"] if job.get("media_file") else (
        "direct link from the source" if job.get("media_url") else "none yet (needs a file/URL, or yt-dlp)")
    print("\n" + "=" * 70)
    print(f"[{i}/{n}] from {job['source']}  ->  {', '.join(job['destinations'])}")
    print(f"  Source:      {job['url']}")
    print(f"  Title:       {d['title']}")
    print(f"  Description: {d['description'][:300]}")
    print(f"  Hashtags:    {' '.join('#' + h for h in d['hashtags']) or '-'}")
    print(f"  Text by:     {d['by']}" + (f"   ** {d['note']}" if d["note"] else ""))
    print(f"  Video:       {media}")


def review(cfg, state, dry_run=False):
    q = [j for j in state["queue"] if j["status"] in ("pending", "partial")]
    if not q:
        print("Nothing waiting for review.")
        return
    for i, job in enumerate(q, 1):
        while True:
            show_job(job, i, len(q))
            a = INPUT("  [p]ost now  [s]chedule  [e]dit text  [d]estinations  [m]edia  [x] skip for good  "
                      "[l]ater  [q]uit > ").strip().lower()
            if a == "p" or a == "s":
                when = None
                if a == "s":
                    try:
                        when = parse_when(INPUT("  Minutes from now, or a date/time like 2026-11-15T18:00 > "))
                    except ValueError:
                        print("  Could not read that time.")
                        continue
                if publish(job, cfg, state, dry_run, when) or dry_run:
                    break
                save_state(state)
            elif a == "e":
                d = job["draft"]
                for field in ("title", "description"):
                    new = INPUT(f"  {field} (Enter keeps current) > ").strip()
                    if new:
                        d[field] = new
                tags = INPUT("  hashtags, comma separated (Enter keeps) > ").strip()
                if tags:
                    d["hashtags"] = [t.strip().lstrip("#") for t in tags.split(",") if t.strip()]
                d["by"], d["note"] = "you", ""
            elif a == "d":
                raw = INPUT(f"  destinations, comma separated from {', '.join(PLATFORMS)} > ").strip()
                chosen = [x.strip() for x in raw.split(",") if x.strip() in PLATFORMS and x.strip() != job["source"]]
                if chosen:
                    job["destinations"] = chosen
            elif a == "m":
                m = INPUT("  public video URL (https://...) or path to a video file > ").strip().strip('"')
                if m.startswith("https://"):
                    job["media_url"], job["media_file"] = m, None
                elif m and Path(m).is_file():
                    job["media_file"] = m
                else:
                    print("  Not a https:// URL or an existing file.")
            elif a == "x":
                job["status"] = "skipped"
                break
            elif a == "l":
                break
            elif a == "q":
                save_state(state)
                return
        save_state(state)
    save_state(state)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_init(_args):
    d = base_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / "config.json"
    if p.exists():
        print("config.json already exists - left unchanged.")
    else:
        p.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        print("Wrote config.json")
    e = d / ".env.example"
    e.write_text(ENV_EXAMPLE, encoding="utf-8")
    print("Wrote .env.example  -> copy it to .env and add your keys, then run:  python crosspost.py check")


def cmd_check(_args):
    cfg = load_config()
    ok_all = True

    def line(name, good, msg):
        nonlocal ok_all
        ok_all = ok_all and good
        print(f"  [{'OK' if good else 'FAIL'}] {name}: {msg}")

    need_ayr = any(backend_for(pl, cfg) == "ayrshare" for pl in PLATFORMS)
    for k, req in (("WINDSOR_API_KEY", True), ("AYRSHARE_API_KEY", need_ayr), ("ANTHROPIC_API_KEY", False)):
        have = bool(os.environ.get(k))
        line(k, have or not req, "set" if have else ("missing" if req else "not set (optional - needed only for AI-written titles)"))
    if any(backend_for(pl, cfg) == "direct" for pl in PLATFORMS):
        d = Path(direct_cwd(cfg))
        line("direct backend", (d / "social_publisher").is_dir(),
             f"social-publisher found in {d}" if (d / "social_publisher").is_dir()
             else f"folder not found: {d} (set direct_cli_dir in config.json)")
    for name, src in cfg["sources"].items():
        try:
            vids = fetch_source(name, src, cfg)
            line(f"source {name}", True, f"{len(vids)} video(s) in the last {cfg['lookback_days']} days")
        except (HttpError, Fatal, ET.ParseError, ValueError) as e:
            line(f"source {name}", False, str(e)[:200])
    try:
        res = ayr("GET", "/user")
        linked = res.get("activeSocialAccounts")
        if linked is None:
            line("Ayrshare", True, "key accepted (could not read the list of linked accounts)")
        else:
            line("Ayrshare", True, "linked accounts: " + (", ".join(linked) or "none"))
            wanted = {d for ds in cfg["destinations"].values() for d in ds}
            missing = sorted(wanted - set(linked))
            if missing:
                line("Ayrshare destinations", False, "not linked in Ayrshare: " + ", ".join(missing))
    except (HttpError, Fatal) as e:
        line("Ayrshare", False, str(e)[:200])
    line("yt-dlp", True, "found" if shutil.which("yt-dlp") else
         "not installed (optional; needed to repost YouTube/TikTok videos automatically)")
    print("\nAll good." if ok_all else "\nFix the FAIL lines above, then run check again.")


def cmd_scan(_args):
    cfg, st = load_config(), load_state()
    print("Scanning ...")
    jobs = scan(cfg, st)
    save_state(st)
    waiting = len([j for j in st["queue"] if j["status"] in ("pending", "partial")])
    print(f"\n{len(jobs)} new job(s). {waiting} waiting in total. Run:  python crosspost.py review")


def cmd_review(args):
    cfg, st = load_config(), load_state()
    review(cfg, st, dry_run=args.dry_run)


def cmd_status(_args):
    st = load_state()
    print(f"Sources already baselined: {', '.join(st['baselined']) or 'none'}")
    for j in st["queue"]:
        if j["status"] in ("pending", "partial"):
            print(f"  waiting: {j['id']}  {j['source']} -> {', '.join(j['destinations'])}  {j['draft']['title'][:50]}")
    print(f"Posts in the last 24h: {posts_last_24h(st)}")
    for h in st["history"][-5:]:
        print(f"  {h['ts']}  {h['from']} -> {h['to']}  {' '.join(h['links'])}")


def cmd_watch(args):
    cfg = load_config()
    print(f"Watching every {args.interval} minute(s). Press Ctrl+C to stop.")
    while True:
        st = load_state()
        print(f"\n[{now_iso()}] scanning ...")
        scan(cfg, st)
        save_state(st)
        if args.auto_publish:
            when = parse_when(str(args.delay_minutes)) if args.delay_minutes else None
            for job in [j for j in st["queue"] if j["status"] in ("pending", "partial")]:
                if job["draft"]["by"] != "original" and not args.include_ai_drafts:
                    print(f"  job {job['id']}: text was not written by you - left for manual review.")
                    continue
                print(f"  auto-publishing job {job['id']} ...")
                publish(job, cfg, st, False, when)
                save_state(st)
        else:
            n = len([j for j in st["queue"] if j["status"] in ("pending", "partial")])
            if n:
                print(f"  {n} job(s) waiting - run  python crosspost.py review")
        try:
            time.sleep(args.interval * 60)
        except KeyboardInterrupt:
            print("\nStopped.")
            return


# --------------------------------------------------------------------------- #
# offline demo (also serves as the self-test)
# --------------------------------------------------------------------------- #
def cmd_demo(_args):
    global HTTP, INPUT
    tmp = tempfile.mkdtemp(prefix="crosspost_demo_")
    os.environ["CROSSPOST_HOME"] = tmp
    for k in ("WINDSOR_API_KEY", "AYRSHARE_API_KEY", "ANTHROPIC_API_KEY"):
        os.environ[k] = "demo-key"
    ph = {"n": 1}
    posted = []

    def feed():
        entries = ['<entry><yt:videoId>YT_A</yt:videoId><title>Studio session vlog</title>'
                   '<published>2026-11-01T10:00:00Z</published><media:group><media:description>'
                   'Behind the scenes</media:description></media:group></entry>']
        if ph["n"] >= 3:
            p = [x for x in posted if x["platforms"] == ["youtube"]][0]
            entries.append('<entry><yt:videoId>YT_ECHO</yt:videoId><title>%s</title><published>2026-11-14T12:00:00Z'
                           '</published><media:group><media:description>x</media:description></media:group></entry>'
                           % p["youTubeOptions"]["title"])
        return ('<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
                'xmlns:media="http://search.yahoo.com/mrss/">' + "".join(entries) + "</feed>").replace(
            "media:", "m:").replace('xmlns:media', 'xmlns:m').encode()

    def fake(method, url, headers=None, body=None, timeout=60, raw=False):
        if "youtube.com/feeds" in url:
            return feed()
        if "connectors.windsor.ai/tiktok_organic" in url:
            rows = [{"video_id": "T1", "video_caption": "Old teaser #music", "video_share_url": "https://t/T1",
                     "video_create_datetime": "2026-11-01T09:00:00Z", "video_duration": 20}]
            if ph["n"] >= 2:
                rows.append({"video_id": "T2", "video_caption": "New beat drop - out now on all platforms #newmusic",
                             "video_share_url": "https://t/T2", "video_create_datetime": "2026-11-14T09:00:00Z",
                             "video_duration": 25})
            return {"data": rows}
        if "connectors.windsor.ai/facebook_organic" in url:
            rows = [{"id": "F1", "title": "Old fb", "description": "old", "source": "https://cdn/F1.mp4",
                     "created_time": "2026-11-01T09:00:00Z", "length": 30, "is_crosspost_video": "false"}]
            if ph["n"] >= 2:
                rows.append({"id": "F9", "title": "Shared from another page", "description": "x",
                             "source": "https://cdn/F9.mp4", "created_time": "2026-11-14T09:30:00Z",
                             "length": 30, "is_crosspost_video": "true"})
            return {"data": rows}
        if "connectors.windsor.ai/instagram" in url:
            return {"data": [{"media_id": "I1", "media_caption": "photo", "media_type": "IMAGE",
                              "timestamp": "2026-11-01T09:00:00Z"}]}
        if "api.anthropic.com" in url:
            return {"content": [{"type": "text", "text": json.dumps(
                {"title": "New Beat Drop: Out Now on All Platforms",
                 "description": "Fresh beat, out now. Tell us your favourite part.",
                 "hashtags": ["newmusic", "beats", "musicproducer"]})}]}
        if url.endswith("/api/user"):
            return {"activeSocialAccounts": ["youtube", "tiktok", "facebook", "instagram"]}
        if url.endswith("/api/post"):
            posted.append(body)
            plat = body["platforms"][0]
            return {"status": "success", "errors": [], "postIds": [
                {"status": "success", "platform": plat, "id": "p" + plat, "postUrl": f"https://{plat}.example/post"}]}
        raise HttpError("demo: unexpected request " + url)

    HTTP = fake
    print(f"(demo data in {tmp}; no network, no real accounts)\n")
    cmd_init(None)
    cfg = load_config()
    st = load_state()
    print("\n--- Run 1: first scan only records what already exists")
    assert scan(cfg, st) == []
    save_state(st)
    ph["n"] = 2
    print("\n--- Run 2: you posted a new TikTok (caption only, no title); Facebook shows a crosspost")
    jobs = scan(cfg, st)
    save_state(st)
    assert len(jobs) == 1 and jobs[0]["source"] == "tiktok", jobs
    assert jobs[0]["draft"]["by"] == "claude" and jobs[0]["draft"]["title"].startswith("New Beat Drop")
    print("\n--- Review: attach a video URL, then post")
    answers = iter(["m", "https://example.com/video.mp4", "p"])
    INPUT = lambda prompt: next(answers)
    review(cfg, st)
    assert [p["platforms"][0] for p in posted] == ["youtube", "facebook", "instagram"], posted
    yt = posted[0]
    assert yt["youTubeOptions"]["title"].startswith("New Beat Drop") and yt["youTubeOptions"]["shorts"] is True
    ph["n"] = 3
    print("\n--- Run 3: the repost now shows up on YouTube; it must NOT be reposted again")
    jobs = scan(cfg, load_state())
    assert jobs == [], jobs
    print("\nDEMO PASSED: detect -> draft -> approve -> repost -> loop protection all worked.")
    shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_env()
    ap = argparse.ArgumentParser(description="Detect new videos on your social accounts and repost them.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create config.json and .env.example").set_defaults(fn=cmd_init)
    sub.add_parser("check", help="test keys, sources and linked accounts").set_defaults(fn=cmd_check)
    sub.add_parser("scan", help="look for new videos and queue them (never posts)").set_defaults(fn=cmd_scan)
    r = sub.add_parser("review", help="approve, edit, schedule or skip queued reposts")
    r.add_argument("--dry-run", action="store_true", help="show the requests without sending them")
    r.set_defaults(fn=cmd_review)
    sub.add_parser("status", help="show the queue and recent posts").set_defaults(fn=cmd_status)
    w = sub.add_parser("watch", help="scan repeatedly (Ctrl+C to stop)")
    w.add_argument("--interval", type=int, default=15, help="minutes between scans (default 15)")
    w.add_argument("--auto-publish", action="store_true",
                   help="post without asking (only videos whose text YOU wrote, unless --include-ai-drafts)")
    w.add_argument("--include-ai-drafts", action="store_true", help="with --auto-publish: also post AI/template drafts")
    w.add_argument("--delay-minutes", type=int, default=15,
                   help="with --auto-publish: schedule this many minutes ahead so you can cancel in Ayrshare (default 15)")
    w.set_defaults(fn=cmd_watch)
    sub.add_parser("demo", help="offline self-test with fake data").set_defaults(fn=cmd_demo)
    args = ap.parse_args(argv)
    try:
        args.fn(args)
    except Fatal as e:
        print("Error:", e)
        return 2
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
