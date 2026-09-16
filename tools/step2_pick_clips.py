"""
Step 2: pick a stock clip for every scene, in the browser.

Usage:
    python step2_pick_clips.py <lang>/<slug> [--port 8000] [--no-browser]

e.g. python step2_pick_clips.py en/crumbs

Serves a local page that walks scene by scene through scenes.json: each scene
shows its narration text, an auto-built search query, and 12 stock clips you
can play inline. Clicking "Use this clip" just records the pick in
selections.json -- nothing is downloaded here. step3_render_video.py fetches
every selected clip's real bytes right before rendering, showing a download
percentage as it goes, so this picker stays fast and light no matter how many
scenes you flip through. Edit the search box for a different query, or page
through more results, until every scene has a clip -- then run step3.

Three stock providers are available as source tabs, all free for commercial
use on a monetized YouTube channel with no attribution required (Pexels
License / Pixabay Content License / Coverr License):
  - Pexels  (video + photo) -- api_key: --api-key, PEXELS_API_KEY env var,
    or tools/pexels_key.txt. Required at startup (the default tab).
  - Pixabay (video + photo) -- --pixabay-api-key, PIXABAY_API_KEY env var,
    or tools/pixabay_key.txt. Get a free key at https://pixabay.com/api/docs/
  - Coverr  (video only)    -- --coverr-api-key, COVERR_API_KEY env var,
    or tools/coverr_key.txt. Get a free key at https://coverr.co/developers
Pixabay and Coverr are optional -- their tabs work as soon as a key is added,
no restart needed if you add the key file before opening that tab. The keys
stay on the server side; the browser never sees them.

A fourth "AI illustration" tab generates a still image for a scene with
Pollinations.AI (the free FLUX model) instead of searching stock -- edit the
prompt, generate a few variations, pick one. It needs no key (anonymous tier
~1 image/15s, small corner watermark); a free token from
https://auth.pollinations.ai in tools/pollinations_token.txt (or
POLLINATIONS_TOKEN, or --pollinations-token) unlocks the faster,
watermark-free tier. Generated images are cached under the project's
.ai_cache/ and, once picked, copied into clips/ -- step 3 renders them as a
slow Ken Burns zoom exactly like a Pexels/Pixabay photo. The auto-matcher can
also use this as a last resort: tick "Fill scenes stock can't match with an
AI illustration" in the sidebar and any scene no stock clip fits is filled
with a house-style illustration (flagged for review) rather than left empty.

Search responses are cached under the project's .pexels_cache/ (shared by all
three providers, keyed by provider+query+page), so re-visiting a scene you
already searched costs no API quota. Pexels' measured quota is 25,000
requests/month; Pixabay is 100 requests/60s; Coverr is 50/hour on a free
Demo app (2,000/hour on a paid Coverr+ plan) -- all far more than a video
needs once the cache is warm.
"""
import argparse
import base64
import hashlib
import json
import mimetypes
import os
import random
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from common import USER_AGENT, caption_style, load_json, save_json
from keywords import build_query, extract_keywords

TOOLS_DIR = Path(__file__).parent
PROJECTS_DIR = TOOLS_DIR.parent / "projects"
KEY_FILE = TOOLS_DIR / "pexels_key.txt"
PIXABAY_KEY_FILE = TOOLS_DIR / "pixabay_key.txt"
COVERR_KEY_FILE = TOOLS_DIR / "coverr_key.txt"
POLLINATIONS_TOKEN_FILE = TOOLS_DIR / "pollinations_token.txt"

PEXELS_SEARCH_URL = "https://api.pexels.com/videos/search"
PEXELS_PHOTO_SEARCH_URL = "https://api.pexels.com/v1/search"
PIXABAY_VIDEO_URL = "https://pixabay.com/api/videos/"
PIXABAY_PHOTO_URL = "https://pixabay.com/api/"
COVERR_VIDEO_URL = "https://api.coverr.co/videos"
PER_PAGE = 12
REQUEST_TIMEOUT = 30

# --------------------------------------------------------------------------
# AI illustration provider -- Pollinations.AI (FLUX). Free and key-less: the
# anonymous tier generates unlimited FLUX images at roughly one request every
# 15 seconds. Dropping a free token from https://auth.pollinations.ai into
# tools/pollinations_token.txt lifts that to ~1 per 5s AND removes the small
# corner watermark the anonymous tier adds. Everything the picker generates
# is a still image, rendered by step 3 as a slow Ken Burns zoom exactly like
# a Pexels/Pixabay photo pick -- no render-side changes needed.
# --------------------------------------------------------------------------
POLLINATIONS_IMAGE_URL = "https://image.pollinations.ai/prompt/"
POLLINATIONS_REFERRER = "youtube-automation-tool"
AI_MODEL = "flux"
AI_REQUEST_TIMEOUT = 180
AI_CREDIT_AUTHOR = "Pollinations.AI (FLUX)"
AI_CREDIT_URL = "https://pollinations.ai"
# Appended to every scene's own subject text so the whole video keeps one
# consistent look -- the warm, hand-illustrated storybook style (people shown
# from behind / faceless, which also keeps real individuals unidentifiable).
AI_STYLE_SUFFIX = (
    "2D digital illustration in a warm hand-drawn storybook style, flat cel "
    "shading with gouache-like soft gradients, limited golden-hour palette of "
    "amber, ochre and muted teal, thick clean ink outlines on the foreground, "
    "layered hills and trees fading into atmospheric haze, gentle paper grain "
    "texture, painterly but clearly illustrated and NOT photorealistic, no lens "
    "blur, any people shown from behind or in silhouette with no recognisable "
    "face, no on-image text, no watermark, no logo, wide cinematic 16:9 "
    "composition"
)

# Minimum-quality filter tiers: (long-edge px, short-edge px), checked
# portrait-aware (same long/short-edge logic as the existing HD picker below)
# so a --vertical project's 1080x1920 renditions still count as "1080p".
QUALITY_TIERS = {
    "1080p": (1920, 1080),
    "2k": (2560, 1440),
    "4k": (3840, 2160),
}

# Extensions accepted from a local upload, and the one substituted when the
# browser hands over something else (a local <input type=file accept="image/*">
# can in principle be fooled, but this is a single-user local tool, not a
# hardened upload endpoint).
LOCAL_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp"}

# Populated in main() -- the handler is instantiated per request by
# ThreadingHTTPServer, so shared state lives at module level.
STATE = {}


def auto_render_default():
    """Whether the picker should start step 3 by itself once every scene has
    a clip. On this laptop: yes. Inside a GitHub Codespace (GitHub sets
    CODESPACES=true): no -- the codespace is a 2-core machine that burns the
    free-hours quota, and the repo's Actions workflow renders for free on a
    4-core runner instead. The page then shows the push + render steps in
    place of the countdown. --no-auto-render / --auto-render override."""
    return os.environ.get("CODESPACES", "").lower() != "true"


# --------------------------------------------------------------------------
# Pexels
# --------------------------------------------------------------------------

def resolve_key(explicit, env_name, key_file):
    """Shared lookup for all three providers' API keys: an explicit CLI flag
    wins, then the provider's env var, then its key file under tools/."""
    if explicit:
        return explicit.strip()
    env = os.environ.get(env_name, "").strip()
    if env:
        return env
    if key_file.exists():
        key = key_file.read_text(encoding="utf-8").strip()
        if key:
            return key
    return None


def resolve_api_key(explicit):
    return resolve_key(explicit, "PEXELS_API_KEY", KEY_FILE)


class MissingKeyError(LookupError):
    """Raised when a scene search hits a provider tab whose key hasn't been
    configured yet -- Pixabay/Coverr are optional, so this is discovered at
    request time rather than blocking startup like the required Pexels key."""


def require_key(state_key, key_file, env_name, signup_url):
    key = STATE.get(state_key)
    if not key:
        raise MissingKeyError(
            f"No API key configured for this source yet. Save one to "
            f"tools/{key_file.name}, set the {env_name} environment variable, "
            f"or restart with the matching --api-key flag, then try again. "
            f"Get a free key at {signup_url}"
        )
    return key


def cache_path(query, page, min_duration, kind="video"):
    key = f"{kind}|{query.lower().strip()}|{page}|{min_duration}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return STATE["cache_dir"] / f"{digest}.json"


def pexels_search(query, page, min_duration=None):
    """Search Pexels for video in this project's orientation (landscape by
    default, portrait for a --vertical project -- see STATE["orientation"]),
    serving from the on-disk cache when possible. Returns (payload,
    from_cache)."""
    cached = cache_path(query, page, min_duration)
    if cached.exists():
        return load_json(cached), True

    params = {
        "query": query,
        "per_page": PER_PAGE,
        "page": page,
        "orientation": STATE.get("orientation", "landscape"),
    }
    if min_duration:
        params["min_duration"] = int(min_duration)

    url = PEXELS_SEARCH_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"Authorization": STATE["api_key"], "User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
        remaining = resp.headers.get("X-Ratelimit-Remaining")
        if remaining is not None:
            STATE["rate_remaining"] = remaining

    save_json(payload, cached)
    return payload, False


def pexels_photo_search(query, page):
    """Search Pexels for a photo in this project's orientation, serving from
    the on-disk cache when possible. Returns (payload, from_cache). Mirrors
    pexels_search but hits the separate Photos API, which has no
    duration/orientation-filter parity with the Videos API (no min_duration,
    and orientation is still supported so it's kept for consistent framing)."""
    cached = cache_path(query, page, None, kind="photo")
    if cached.exists():
        return load_json(cached), True

    params = {
        "query": query, "per_page": PER_PAGE, "page": page,
        "orientation": STATE.get("orientation", "landscape"),
    }
    url = PEXELS_PHOTO_SEARCH_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"Authorization": STATE["api_key"], "User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
        remaining = resp.headers.get("X-Ratelimit-Remaining")
        if remaining is not None:
            STATE["rate_remaining"] = remaining

    save_json(payload, cached)
    return payload, False


def pixabay_search(query, page, min_duration=None):
    """Search Pixabay for video, serving from the on-disk cache when
    possible. Returns (payload, from_cache). Pixabay's video endpoint has no
    orientation or min_duration filter (unlike Pexels) -- both are applied
    after the fact by the caller."""
    cached = cache_path(query, page, min_duration, kind="pixabay_video")
    if cached.exists():
        return load_json(cached), True

    api_key = require_key("pixabay_key", PIXABAY_KEY_FILE, "PIXABAY_API_KEY", "https://pixabay.com/api/docs/")
    params = {
        "key": api_key, "q": query, "per_page": PER_PAGE, "page": page,
        "safesearch": "true", "video_type": "film",
    }
    url = PIXABAY_VIDEO_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    save_json(payload, cached)
    return payload, False


def pixabay_photo_search(query, page):
    """Search Pixabay for a photo, serving from the on-disk cache when
    possible. Returns (payload, from_cache). Mirrors pixabay_search but hits
    the image endpoint, which does support `orientation`."""
    cached = cache_path(query, page, None, kind="pixabay_photo")
    if cached.exists():
        return load_json(cached), True

    api_key = require_key("pixabay_key", PIXABAY_KEY_FILE, "PIXABAY_API_KEY", "https://pixabay.com/api/docs/")
    params = {
        "key": api_key, "q": query, "per_page": PER_PAGE, "page": page,
        "safesearch": "true", "image_type": "photo",
        "orientation": "vertical" if STATE.get("orientation") == "portrait" else "horizontal",
    }
    url = PIXABAY_PHOTO_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    save_json(payload, cached)
    return payload, False


def coverr_search(query, page):
    """Search Coverr for video, serving from the on-disk cache when
    possible. Returns (payload, from_cache). Coverr has no orientation or
    min_duration filter, and its `page` is zero-based while ours is
    1-based -- converted here so the rest of the app never has to think
    about it."""
    cached = cache_path(query, page, None, kind="coverr_video")
    if cached.exists():
        return load_json(cached), True

    api_key = require_key("coverr_key", COVERR_KEY_FILE, "COVERR_API_KEY", "https://coverr.co/developers")
    params = {"query": query, "page": max(page - 1, 0), "page_size": PER_PAGE, "urls": "true"}
    url = COVERR_VIDEO_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    save_json(payload, cached)
    return payload, False


def filter_long_enough(videos, min_duration):
    """Post-fetch equivalent of Pexels' native min_duration search param, for
    providers (Pixabay, Coverr) whose API has no such filter."""
    if not min_duration:
        return videos
    return [v for v in videos if not v.get("duration") or v["duration"] >= min_duration]


def meets_quality(item, min_quality):
    """Quality-tier check for photos, which (unlike video) already report
    their true original width/height rather than a picked rendition -- so
    filtering is just a straight comparison, no separate download-rendition
    logic needed. A photo with no reported dimensions can't be verified and
    is excluded when a tier is explicitly requested."""
    if not min_quality:
        return True
    w, h = item.get("width") or 0, item.get("height") or 0
    if not w or not h:
        return False
    long_edge, short_edge = QUALITY_TIERS[min_quality]
    return max(w, h) >= long_edge and min(w, h) >= short_edge


def pexels_error_detail(e):
    """Human-readable message for an HTTPError from either Pexels API
    (videos or photos share the same key, rate limit, and Cloudflare edge)."""
    if e.code == 401:
        return (
            f"Pexels rejected the API key (401). Check the key in {KEY_FILE.name} -- "
            f"copy it from https://www.pexels.com/api/, then restart this script."
        )
    if e.code == 403:
        # Almost always Cloudflare ("error code: 1010"), not the key.
        return (
            "Pexels' CDN blocked the request (403). This is usually Cloudflare rejecting "
            "the client rather than a problem with your API key. Check your internet "
            "connection or any VPN/proxy and try again."
        )
    if e.code == 429:
        return (
            "Pexels rate limit reached (429). Wait a while and try again; scenes you "
            "already searched still work from cache, and picks already made are saved."
        )
    return f"Pexels returned HTTP {e.code}."


def pixabay_error_detail(e):
    if e.code in (400, 401, 403):
        return (
            f"Pixabay rejected the API key ({e.code}). Check the key in {PIXABAY_KEY_FILE.name} -- "
            f"copy it from https://pixabay.com/api/docs/ (you must be signed in), then try again."
        )
    if e.code == 429:
        return (
            "Pixabay rate limit reached (429 -- 100 requests/60s). Wait a while and try again; "
            "scenes you already searched still work from cache, and picks already made are saved."
        )
    return f"Pixabay returned HTTP {e.code}."


def coverr_error_detail(e):
    if e.code in (401, 403):
        return (
            f"Coverr rejected the API key ({e.code}). Check the key in {COVERR_KEY_FILE.name} -- "
            f"create an app and copy its key from https://coverr.co/developers, then try again."
        )
    if e.code == 429:
        return (
            "Coverr rate limit reached (429 -- 50 requests/hour on a free Demo app). Wait a while "
            "and try again; scenes you already searched still work from cache, and picks already "
            "made are saved."
        )
    return f"Coverr returned HTTP {e.code}."


def pick_download_file(video_files, min_quality=None):
    """Best mp4 rendition to actually use in the render: the smallest one at
    or above the requested quality tier (1080p by default), falling back to
    the largest available if the clip simply isn't published that big --
    UNLESS a quality tier was explicitly requested (min_quality set), in
    which case a clip that can't meet it is disqualified (None) rather than
    silently downgraded, so the quality filter actually means something.
    Checked by long/short edge rather than raw width/height so a portrait
    rendition (e.g. 1080x1920, --vertical projects) qualifies as "1080p" the
    same way a landscape 1920x1080 one does."""
    mp4s = [f for f in video_files if (f.get("file_type") or "").endswith("mp4") and f.get("link")]
    if not mp4s:
        return None
    long_edge, short_edge = QUALITY_TIERS.get(min_quality, (1920, 1080))
    hd = [
        f for f in mp4s
        if max(f.get("width") or 0, f.get("height") or 0) >= long_edge
        and min(f.get("width") or 0, f.get("height") or 0) >= short_edge
    ]
    if hd:
        return min(hd, key=lambda f: f.get("width") or 0)
    if min_quality:
        return None
    return max(mp4s, key=lambda f: f.get("width") or 0)


def pick_preview_file(video_files):
    """Small rendition for in-page playback -- previewing 12 clips at full
    1080p would saturate the connection for no benefit."""
    mp4s = [f for f in video_files if (f.get("file_type") or "").endswith("mp4") and f.get("link")]
    if not mp4s:
        return None
    small = [f for f in mp4s if 480 <= (f.get("width") or 0) <= 1280]
    if small:
        return min(small, key=lambda f: f.get("width") or 0)
    return min(mp4s, key=lambda f: f.get("width") or 0)


def simplify_video(v, min_quality=None):
    """Flatten a Pexels video object down to what the UI and selections.json
    actually need. Returns None if min_quality was explicitly requested and
    this clip has no rendition meeting it -- the caller drops those."""
    download = pick_download_file(v.get("video_files") or [], min_quality)
    if min_quality and download is None:
        return None
    preview = pick_preview_file(v.get("video_files") or [])
    user = v.get("user") or {}
    # Pexels' only textual description of a clip is its page URL slug, e.g.
    # ".../video/dynamic-ocean-waves-in-coastal-landscape-35371501/" -- that
    # slug is what the semantic auto-matcher scores against.
    slug = (v.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
    desc = " ".join(w for w in slug.split("-") if not w.isdigit())
    return {
        "id": v.get("id"),
        "desc": desc,
        "duration": v.get("duration"),
        "width": (download or {}).get("width"),
        "height": (download or {}).get("height"),
        "thumb": v.get("image"),
        "page_url": v.get("url"),
        "author": user.get("name"),
        "author_url": user.get("url"),
        "preview_url": (preview or {}).get("link"),
        "download_url": (download or {}).get("link"),
        "kind": "video",
        "source": "pexels",
    }


def simplify_photo(p):
    """Flatten a Pexels photo object the same way simplify_video does. The
    Photos API's `src` dict is already sized renditions rather than a list to
    pick a "best" from -- large2x is the highest resolution Pexels serves
    (up to ~2x the requested display size), plenty for a 1920x1080 render."""
    src = p.get("src") or {}
    # Pexels photos carry real alt-text -- far better matching signal than a
    # URL slug; the auto-matcher judges against it in the photo-fallback round.
    slug = (p.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
    return {
        "id": p.get("id"),
        "desc": (p.get("alt") or "").strip() or " ".join(w for w in slug.split("-") if not w.isdigit()),
        "duration": None,
        "width": p.get("width"),
        "height": p.get("height"),
        "thumb": src.get("medium") or src.get("small"),
        "page_url": p.get("url"),
        "author": p.get("photographer"),
        "author_url": p.get("photographer_url"),
        "preview_url": src.get("large") or src.get("medium"),
        "download_url": src.get("large2x") or src.get("original"),
        "kind": "photo",
        "source": "pexels",
    }


def pixabay_pick_video_renditions(video_obj, min_quality=None):
    """Return (download, preview) rendition dicts from a Pixabay video hit's
    `videos` object (large/medium/small/tiny, each carrying url/width/height).
    Mirrors pick_download_file/pick_preview_file's Pexels logic: smallest
    rendition at or above the requested quality tier (1080p by default) for
    download, a mid-size one for preview. Pixabay's own renditions top out
    around 1920px wide, so a 2K/4K filter will disqualify most/all Pixabay
    hits -- that's correct, not a bug, given what the API actually serves."""
    renditions = [r for r in (video_obj or {}).values() if r.get("url")]
    if not renditions:
        return None, None
    long_edge, short_edge = QUALITY_TIERS.get(min_quality, (1920, 1080))
    hd = [
        r for r in renditions
        if max(r.get("width") or 0, r.get("height") or 0) >= long_edge
        and min(r.get("width") or 0, r.get("height") or 0) >= short_edge
    ]
    if hd:
        download = min(hd, key=lambda r: r.get("width") or 0)
    elif min_quality:
        download = None
    else:
        download = max(renditions, key=lambda r: r.get("width") or 0)
    mid = [r for r in renditions if 480 <= (r.get("width") or 0) <= 1280]
    preview = min(mid, key=lambda r: r.get("width") or 0) if mid else min(renditions, key=lambda r: r.get("width") or 0)
    return download, preview


def simplify_pixabay_video(v, min_quality=None):
    """Flatten a Pixabay video hit down to what the UI and selections.json
    actually need -- same shape as simplify_video. Returns None if
    min_quality was explicitly requested and this clip has no rendition
    meeting it -- the caller drops those."""
    videos = v.get("videos") or {}
    download, preview = pixabay_pick_video_renditions(videos, min_quality)
    if min_quality and download is None:
        return None
    thumb = (videos.get("medium") or videos.get("small") or videos.get("large") or {}).get("thumbnail")
    return {
        "id": v.get("id"),
        "desc": (v.get("tags") or "").replace(",", " "),
        "duration": v.get("duration"),
        "width": (download or {}).get("width"),
        "height": (download or {}).get("height"),
        "thumb": thumb,
        "page_url": v.get("pageURL"),
        "author": v.get("user"),
        "author_url": None,
        "preview_url": (preview or {}).get("url"),
        "download_url": (download or {}).get("url"),
        "kind": "video",
        "source": "pixabay",
    }


def simplify_pixabay_photo(p):
    """Flatten a Pixabay image hit down to what the UI and selections.json
    actually need -- same shape as simplify_photo. `largeImageURL` (max
    1280px on the long edge) is the biggest rendition the default (non
    full-access) API key can fetch."""
    return {
        "id": p.get("id"),
        "duration": None,
        "width": p.get("imageWidth"),
        "height": p.get("imageHeight"),
        "thumb": p.get("webformatURL") or p.get("previewURL"),
        "page_url": p.get("pageURL"),
        "author": p.get("user"),
        "author_url": None,
        "preview_url": p.get("webformatURL") or p.get("previewURL"),
        "download_url": p.get("largeImageURL") or p.get("webformatURL"),
        "kind": "photo",
        "source": "pixabay",
    }


def simplify_coverr_video(v):
    """Flatten a Coverr video hit down to what the UI and selections.json
    actually need -- same shape as simplify_video. Coverr doesn't return
    width/height or a per-video page link in the fields this app requests, and
    its license needs no attribution, so those are left blank rather than
    guessed."""
    urls = v.get("urls") or {}
    return {
        "id": v.get("id"),
        "desc": " ".join(str(x) for x in (v.get("title"), v.get("description")) if x),
        "duration": v.get("duration"),
        "width": None,
        "height": None,
        "thumb": v.get("poster") or v.get("thumbnail"),
        "page_url": None,
        "author": None,
        "author_url": None,
        "preview_url": urls.get("mp4_preview") or urls.get("mp4"),
        "download_url": urls.get("mp4_download") or urls.get("mp4"),
        "kind": "video",
        "source": "coverr",
    }


# --------------------------------------------------------------------------
# AI illustration provider (Pollinations.AI / FLUX)
# --------------------------------------------------------------------------

def _ai_dimensions():
    """Output size to ask Pollinations for -- portrait for a --vertical
    project (render.json), landscape otherwise. Step 3 scales/crops to the
    final frame regardless, but asking for the right aspect avoids a big
    centre-crop."""
    return (1080, 1920) if STATE.get("orientation") == "portrait" else (1920, 1080)


def _ai_scene_subject(scene):
    """A short 'what the camera sees' phrase for a scene, drawn from its
    semantic brief when the timeline provided one, else its search query /
    keywords / raw narration."""
    sem = scene.get("semantic") or {}
    bits = [str(sem[k]) for k in ("subject", "action", "object") if sem.get(k)]
    if sem.get("location"):
        bits.append("in " + str(sem["location"]))
    if sem.get("time"):
        bits.append("(" + str(sem["time"]) + ")")
    if bits:
        return ", ".join(bits)
    for cand in (scene.get("query"),
                 " ".join((scene.get("queries") or [])[:1]),
                 " ".join(scene.get("keywords") or [])):
        if cand and cand.strip():
            return cand.strip()
    return (scene.get("text") or "a quiet establishing shot").strip()[:180]


def _ai_shot_subject(shot, scene):
    """Same idea as _ai_scene_subject but for one shot of a multi-shot scene:
    the shot's own subject/action/words, inheriting the scene's location."""
    sem = scene.get("semantic") or {}
    parts = [p for p in (shot.get("subject") or sem.get("subject"), shot.get("action")) if p]
    if sem.get("location"):
        parts.append("in " + str(sem["location"]))
    if parts:
        return ", ".join(str(p) for p in parts)
    return (shot.get("text") or _ai_scene_subject(scene)).strip()[:180]


def _compose_ai_prompt(subject, raw=False):
    """Final Pollinations prompt: the scene subject followed by the locked
    house-style suffix (unless `raw`, when the caller's text is used verbatim
    so an experiment with a different look is possible)."""
    subject = (subject or "").strip()
    if raw:
        return subject or AI_STYLE_SUFFIX
    subject = subject.rstrip(". ")
    return f"{subject}. {AI_STYLE_SUFFIX}" if subject else AI_STYLE_SUFFIX


def _pollinations_url(prompt, seed, width, height, nologo=False):
    params = {
        "width": width,
        "height": height,
        "seed": int(seed),
        "model": AI_MODEL,
        "referrer": POLLINATIONS_REFERRER,
    }
    if nologo:
        params["nologo"] = "true"
    return (POLLINATIONS_IMAGE_URL + urllib.parse.quote(prompt, safe="")
            + "?" + urllib.parse.urlencode(params))


def _ai_cache_name(prompt, seed, width, height):
    digest = hashlib.sha1(
        f"{prompt}|{seed}|{width}x{height}|{AI_MODEL}".encode("utf-8")
    ).hexdigest()[:20]
    return f"ai_{digest}.jpg"


def _ai_pace():
    """Space successive Pollinations calls out to the free tier's rate limit
    (~1 per 15s anonymous, ~1 per 5s with a token) so a burst of generations
    doesn't just 429."""
    interval = 5.0 if STATE.get("pollinations_token") else 15.0
    last = STATE.get("ai_last_call") or 0.0
    wait = interval - (time.time() - last)
    if wait > 0:
        time.sleep(min(wait, interval))
    STATE["ai_last_call"] = time.time()


def _generate_ai_image(prompt, seed, dest, width, height):
    """Fetch one FLUX image from Pollinations to `dest` (atomic .part write).
    Uses the configured token (faster tier + no watermark) when present.
    Returns the plain, token-free URL for the same image so it can be stored
    as a step-3 re-download fallback."""
    token = STATE.get("pollinations_token")
    fetch_url = _pollinations_url(prompt, seed, width, height, nologo=bool(token))
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(fetch_url, headers=headers)
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(req, timeout=AI_REQUEST_TIMEOUT) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f)
    tmp.replace(dest)
    return _pollinations_url(prompt, seed, width, height, nologo=False)


def _ai_asset(prompt, seed, width, height, fetch_url=None):
    """The common-shape asset dict for a generated illustration -- same keys
    the UI grid and choose()/register_asset expect from a Pexels photo, plus
    the fields needed to re-materialise it (cache_name, prompt, seed)."""
    name = _ai_cache_name(prompt, seed, width, height)
    return {
        "id": seed,
        "kind": "ai",
        "source": "ai",
        "seed": seed,
        "prompt": prompt,
        "model": AI_MODEL,
        "cache_name": name,
        "desc": prompt[:120],
        "duration": None,
        "width": width,
        "height": height,
        "thumb": f"/aicache/{name}",
        "preview_url": f"/aicache/{name}",
        "download_url": fetch_url or _pollinations_url(prompt, seed, width, height),
        "page_url": AI_CREDIT_URL,
        # No named human contributor -- the provider-level credit line in
        # step3's credits.txt (PROVIDER_CREDITS["ai"]) covers Pollinations/FLUX.
        "author": None,
        "author_url": None,
    }


def ai_generate_for_scene(subject, seed, raw=False):
    """Compose the prompt, generate (or reuse a cached) illustration, and
    return its asset dict. Raises urllib errors up to the caller."""
    prompt = _compose_ai_prompt(subject, raw=raw)
    seed = int(seed) if seed else random.randint(1, 9_999_999)
    if seed <= 0:
        seed = random.randint(1, 9_999_999)
    width, height = _ai_dimensions()
    dest = STATE["ai_cache_dir"] / _ai_cache_name(prompt, seed, width, height)
    fetch_url = None
    if not dest.exists():
        _ai_pace()
        fetch_url = _generate_ai_image(prompt, seed, dest, width, height)
    return _ai_asset(prompt, seed, width, height, fetch_url)


def _ai_autofill(target, scene):
    """Generate one house-style illustration for `target` (the scene itself,
    or a shot dict of a multi-shot scene) -- used as the auto-match last
    resort. Returns an asset dict ready for register_asset."""
    subject = _ai_scene_subject(scene) if target is scene else _ai_shot_subject(target, scene)
    return ai_generate_for_scene(subject, seed=random.randint(1, 9_999_999))


# --------------------------------------------------------------------------
# Selections
# --------------------------------------------------------------------------

def load_selections():
    path = STATE["selections_path"]
    if path.exists():
        return {str(k): v for k, v in load_json(path).items()}
    return {}


def save_selections(selections):
    save_json(selections, STATE["selections_path"])


def clip_filename(scene_index, tag, ext, part=None):
    """`part`, if given, distinguishes multiple clips assigned to the same
    scene (a multi-clip pick) -- omitted, this is the original single-clip
    naming so existing files/selections are untouched."""
    base = f"{scene_index:03d}_{tag}"
    if part is not None:
        base += f"_{part}"
    return f"{base}.{ext}"


def asset_tag(asset):
    """Filename tag identifying an asset -- shared between the scene that
    actually downloads/decodes it and any others in a bulk range that just
    copy the resulting file, so both land on the same tag with only the
    scene-index prefix differing. Prefixed with the provider so the same
    numeric id from two different providers never collides on disk; assets
    saved before multi-provider support existed have no "source" field, which
    is always a Pexels pick, so that's the default."""
    if asset.get("kind") == "local":
        return "local"
    if asset.get("kind") == "ai":
        return f"ai{asset.get('seed') or asset.get('id') or 'x'}"
    return f"{asset.get('source') or 'pexels'}{asset['id']}"


def local_ext(filename):
    ext = Path(filename or "").suffix.lstrip(".").lower()
    return ext if ext in LOCAL_IMAGE_EXTS else "jpg"


def resolve_dest_path(scene_index, asset, part=None):
    """Where `asset` will live in clips_dir for `scene_index`, without
    fetching anything -- step2 only needs to know the filename to record in
    selections.json; step3 is the one place that actually downloads a
    remote asset's bytes (see download_selected_clips() there)."""
    kind = asset.get("kind", "video")
    if kind == "local":
        ext = local_ext(asset.get("local_name"))
    elif kind == "video":
        ext = "mp4"
    else:  # "photo" or "ai" -- both land as a still image
        ext = "jpg"
    return STATE["clips_dir"] / clip_filename(scene_index, asset_tag(asset), ext, part)


def materialize_local_asset(scene_index, asset, part=None):
    """Decode a local-upload asset straight to disk. Unlike a Pexels/Pixabay/
    Coverr pick, the browser already handed over the full file (as a data
    URL) when it was chosen, so there's no network fetch to defer to step3 --
    writing it now is just as cheap as recording a path for later."""
    dest = resolve_dest_path(scene_index, asset, part)
    raw = asset.get("local_data") or ""
    if not raw:
        # Happens if a restored multi-clip pick (from selections.json, which
        # only keeps the filename, not the upload bytes) is re-saved without
        # re-choosing the file -- fail loudly rather than truncate the
        # existing clip to zero bytes.
        raise ValueError(f"'{asset.get('local_name') or 'local file'}' needs to be re-uploaded before saving picks again.")
    b64 = raw.split(",", 1)[1] if "," in raw else raw
    dest.write_bytes(base64.b64decode(b64))
    return dest


def materialize_ai_asset(scene_index, asset, part=None):
    """Copy the generated illustration from .ai_cache/ into clips_dir for this
    scene. Like a local upload, an AI pick is written now rather than deferred
    to step3 (the bytes already exist on disk). If the cache file is gone -- a
    picker restart with the cache cleared -- it's regenerated from the
    recorded prompt+seed so a re-save still works. The plain Pollinations URL
    also stays in the selection record as a second-chance re-download for
    step3."""
    dest = resolve_dest_path(scene_index, asset, part)
    prompt = asset.get("prompt")
    seed = int(asset.get("seed") or 0)
    width = asset.get("width") or _ai_dimensions()[0]
    height = asset.get("height") or _ai_dimensions()[1]
    cache_name = asset.get("cache_name")
    if not cache_name and prompt and seed:
        # Restored from selections.json (which doesn't keep cache_name) --
        # re-derive it from the recipe.
        cache_name = _ai_cache_name(prompt, seed, width, height)
    src = STATE["ai_cache_dir"] / cache_name if cache_name else None
    if src is None or not src.is_file():
        if not prompt or not seed:
            raise ValueError("This AI illustration must be re-generated before saving picks again.")
        src = STATE["ai_cache_dir"] / _ai_cache_name(prompt, seed, width, height)
        src.parent.mkdir(exist_ok=True)
        _ai_pace()
        _generate_ai_image(prompt, seed, src, width, height)
    shutil.copyfile(src, dest)
    return dest


def register_asset(scene_index, asset, part=None):
    """Record `asset` as picked for `scene_index`, returning the destination
    path it will occupy in clips_dir. A local upload or an AI illustration is
    written immediately (see materialize_local_asset / materialize_ai_asset);
    a remote stock pick is left unfetched -- its bytes are downloaded later,
    in bulk with a progress percentage, by step3_render_video.py's
    download_selected_clips()."""
    if asset.get("kind") == "local":
        return materialize_local_asset(scene_index, asset, part)
    if asset.get("kind") == "ai":
        return materialize_ai_asset(scene_index, asset, part)
    return resolve_dest_path(scene_index, asset, part)


def selection_record(scene_index, asset, dest, query):
    kind = asset.get("kind", "video")
    record = {
        "scene_index": scene_index,
        "type": "video" if kind == "video" else "image",
        "source": "local" if kind == "local" else (asset.get("source") or "pexels"),
        "pexels_id": asset.get("id") if kind != "local" else None,
        "file": dest.name,
        # Not set for a local upload -- it's already written to disk (see
        # materialize_local_asset), nothing left to fetch. For a remote pick,
        # this is what step3_render_video.py's download_selected_clips()
        # downloads before rendering. An AI illustration keeps its plain
        # Pollinations URL here too -- the file is already written, but the URL
        # lets step3 regenerate it if clips/ was cleared.
        "download_url": None if kind == "local" else asset.get("download_url"),
        # A small streaming-friendly rendition + a still thumbnail, kept so the
        # "Review picked clips" page can preview a remote pick without pulling
        # its full-res file (step3 still downloads download_url for the render).
        # Absent on a local upload / AI illustration -- those already have a
        # file in clips/ the review page serves directly.
        "preview_url": None if kind == "local" else asset.get("preview_url"),
        "thumb": asset.get("thumb"),
        "duration": asset.get("duration"),
        "width": asset.get("width"),
        "height": asset.get("height"),
        "author": asset.get("author"),
        "author_url": asset.get("author_url"),
        "page_url": asset.get("page_url"),
        "query": query,
    }
    if kind == "ai":
        record["ai_prompt"] = asset.get("prompt")
        record["ai_seed"] = asset.get("seed")
        record["ai_model"] = asset.get("model") or AI_MODEL
    return record
# NOTE: "pexels_id" predates multi-provider support and is kept for
# selections.json backward-compatibility -- it now holds the id from
# whichever provider the "source" field names, not only Pexels.


def multi_item_record(asset, dest, share_seconds, shot=None, shot_index=None):
    """One entry in a multi-clip scene's `items` list -- same shape as a
    single-clip selection_record's asset fields, plus `share_seconds`, the
    nominal slice of the scene's narration this item covers (informational;
    step3 recomputes the real split from the scene's actual segment length).

    When `shot` is given (a shot dict from the semantic timeline), this item
    is locked to that shot's OWN narration window: `shot_start`/`shot_end`
    (absolute seconds) and `shot_word_from`/`shot_word_to` (the durable
    word-index coordinate) are recorded so step3 cuts each clip exactly where
    its shot ends -- clip 1 plays for shot 1's duration, then clip 2 begins,
    etc. -- instead of splitting the scene's total time evenly."""
    kind = asset.get("kind", "video")
    item = {
        "type": "video" if kind == "video" else "image",
        "source": "local" if kind == "local" else (asset.get("source") or "pexels"),
        "pexels_id": asset.get("id") if kind != "local" else None,
        "file": dest.name,
        "download_url": None if kind == "local" else asset.get("download_url"),
        "preview_url": None if kind == "local" else asset.get("preview_url"),
        "thumb": asset.get("thumb"),
        "share_seconds": round(share_seconds, 3),
        "width": asset.get("width"),
        "height": asset.get("height"),
        "author": asset.get("author"),
        "author_url": asset.get("author_url"),
        "page_url": asset.get("page_url"),
    }
    if shot is not None:
        item["shot_index"] = shot_index
        if shot.get("start") is not None:
            item["shot_start"] = round(shot["start"], 3)
        if shot.get("end") is not None:
            item["shot_end"] = round(shot["end"], 3)
        for src, dst in (("word_from", "shot_word_from"), ("word_to", "shot_word_to")):
            if shot.get(src) is not None:
                item[dst] = shot[src]
        if shot.get("text"):
            item["shot_text"] = shot["text"]
    if kind == "ai":
        item["ai_prompt"] = asset.get("prompt")
        item["ai_seed"] = asset.get("seed")
        item["ai_model"] = asset.get("model") or AI_MODEL
    return item


def remove_clips_for_scene(scene_index):
    """Delete any previously downloaded clip for this scene so re-picking
    doesn't leave orphaned files behind (step3 matches by filename prefix).
    Matches any extension -- a scene's asset can now be an mp4 clip or a jpg
    image depending on what was picked."""
    for old in STATE["clips_dir"].glob(f"{scene_index:03d}_*"):
        try:
            old.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------
# Semantic auto-match: search -> score candidates against the exact script
# segment -> select or explain the failure. sync_report.json records every
# decision (scene, candidates, scores, reasons, PASS/FAIL) for debugging.
# --------------------------------------------------------------------------

# A candidate must score at least this (0-100) against the scene's semantics
# to be auto-selected outright. Below it -- but at or above AUTOMATCH_SOFT_FLOOR
# -- an "all unpicked" run still fills the scene with the best candidate and
# flags it "soft" for review, rather than leaving the scene empty (the whole
# point of that button is to fill everything). A single-scene "Auto-match this
# scene" keeps the hard threshold unless the caller passes a soft floor.
AUTOMATCH_THRESHOLD = 60
AUTOMATCH_SOFT_FLOOR = 45
# Candidates gathered per scene, and how many of those the LLM actually
# scores per judging batch. The pool is trimmed to the JUDGE best by a
# weighted word-overlap pre-rank first. These were tiny (18/10) under Groq's
# 12k-token/minute free tier; Gemini's is ~250k/min, so a wider net and a
# bigger judge batch cost nothing and catch matches the old sizes missed.
AUTOMATCH_POOL = 32
AUTOMATCH_JUDGE = 14
# If the first JUDGE-sized batch produces nothing at or above the acceptance
# bar and the pool still has candidates, score up to this many batches total
# before giving up on the search results (cheap now, and the right clip is
# often just outside an arbitrary top-14 cut).
AUTOMATCH_MAX_BATCHES = 2
# How many distinct search phrasings to build per scene/shot (see
# _expand_queries) -- one keyword phrase almost never matches stock
# catalogues, several angles on the same visual do.
AUTOMATCH_QUERY_VARIANTS = 7


class AutomatchUnavailable(RuntimeError):
    """The LLM judge can't run right now (Gemini quota exhausted, key rejected,
    or the service is down). Raised instead of quietly failing every scene so
    an 'all unpicked' run stops immediately with one clear message rather than
    grinding through the whole script doing pointless searches it can't score."""


def refresh_llm_keys():
    """Re-read the Gemini keys (tools/gemini_key.txt, GEMINI_API_KEY) into STATE.

    Called before every auto-match and on every /api/scenes poll so a key
    added or changed while the picker is already running takes effect on the
    next run -- STATE["llm_keys"] used to be loaded once at startup, which
    is why dropping a second key into gemini_key.txt mid-session did nothing
    until you restarted the picker."""
    try:
        from llm_client import load_keys
        STATE["llm_keys"] = load_keys()
    except Exception:
        STATE["llm_keys"] = None
    return STATE["llm_keys"]

AUTOMATCH_SYSTEM = """You are a documentary film editor judging stock footage against narration.
You are given one narration segment (its exact script text and semantic
requirements) and a list of candidate stock clips. All you know about each
clip is its short catalog description, the search phrase that surfaced it,
its duration and resolution -- score ONLY what the description actually says;
never assume unmentioned content.

For each candidate return a score 0-100 AND a verdict:
- "strong": the description explicitly shows the required SUBJECT doing the
  required ACTION in the right setting -> score 78-95.
- "ok": right subject in the right kind of setting, action merely UNSTATED
  (short catalog slugs rarely mention the action) -> score 60-74. This is a
  usable match.
- "weak": related theme or mood but the required subject/elements are not
  clearly shown -> score 35-52.
- "wrong": contradicting action (standing vs running, exterior vs entering),
  wrong location or era, or anything in the MUST NOT list -> score 0-30.

Rules:
- ACTION and SUBJECT match matter more than how cinematic or pretty a clip
  sounds. A plain clip that shows the right thing beats a beautiful clip that
  shows something adjacent.
- Reward matching the time period, named objects, and mood on top of a
  correct subject+action.
- Judge every candidate independently and on its own description only.

Return STRICT JSON only, nothing else -- no prose, no extra keys:
{"scores":[{"id":"C1","score":NN,"verdict":"strong|ok|weak|wrong"}, ...]}
Score every candidate exactly once."""


def _automatch_search(query, min_duration, min_quality, page=1):
    """One page of candidates per provider that has a key, flattened to the
    common shape (each carrying `desc`). Provider/API errors are collected,
    not raised -- a dead optional provider must not sink the auto-match."""
    candidates, errors = [], []
    try:
        payload, _ = pexels_search(query, page, min_duration)
        for v in payload.get("videos", []):
            s = simplify_video(v, min_quality)
            if s and s["download_url"]:
                candidates.append(s)
    except Exception as e:
        errors.append(f"Pexels '{query}': {e}")
    if STATE.get("pixabay_key"):
        try:
            payload, _ = pixabay_search(query, page, min_duration)
            for v in payload.get("hits", []):
                s = simplify_pixabay_video(v, min_quality)
                if s and s["download_url"] and (min_duration is None or (s.get("duration") or 0) >= min_duration):
                    candidates.append(s)
        except Exception as e:
            errors.append(f"Pixabay '{query}': {e}")
    # Coverr has no resolution data, so it can't honor a min_quality tier --
    # only draw from it when no tier is set (same rule as its picker tab).
    if STATE.get("coverr_key") and not min_quality:
        try:
            payload, _ = coverr_search(query, page)
            for v in payload.get("hits", []):
                s = simplify_coverr_video(v)
                if not s or not s["download_url"]:
                    continue
                try:
                    dur = float(s.get("duration") or 0)
                except (TypeError, ValueError):
                    dur = 0.0
                if min_duration is None or not dur or dur >= min_duration:
                    candidates.append(s)
        except Exception as e:
            errors.append(f"Coverr '{query}': {e}")
    return candidates, errors


def _photo_pool(queries, errors_out, limit=10):
    """Still-photo candidates (Pexels alt-text is strong matching signal).
    Used as a last-resort round when no video candidate passes: a photo that
    EXACTLY depicts the narration beats a video that vaguely relates to it,
    and step3 renders photos with a slow Ken Burns zoom."""
    pool, seen = [], set()
    for query in queries:
        try:
            payload, _ = pexels_photo_search(query, 1)
            for p in payload.get("photos", []):
                s = simplify_photo(p)
                if s["download_url"] and s.get("desc") and s["id"] not in seen:
                    seen.add(s["id"])
                    s["matched_query"] = query
                    pool.append(s)
        except Exception as e:
            errors_out.append(f"Pexels photos '{query}': {e}")
    return pool[:limit]


_QUERY_STOP = {
    "the", "a", "an", "and", "or", "of", "in", "on", "at", "to", "for", "with",
    "this", "that", "as", "by", "is", "are", "was", "were", "be", "being",
    "into", "onto", "from", "while", "during", "over", "under", "near",
}
_QUERY_WORD = re.compile(r"[A-Za-z][A-Za-z\-]{1,}")


def _core_terms(text, limit=3):
    """The first `limit` content words of a phrase, in reading order (drops
    stopwords and 1-2 letter tokens). A 2-3 word core is what stock
    catalogues actually match; a full 6-word 'what the camera sees' sentence
    usually returns nothing. Reading order keeps the phrase natural
    ('trader watching screen', not 'screen watching trader')."""
    seen, out = set(), []
    for w in _QUERY_WORD.findall((text or "").lower()):
        if w in _QUERY_STOP or len(w) < 3 or w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= limit:
            break
    return out


def _norm_query(q):
    return " ".join((q or "").split()).strip().lower()


def _expand_queries(sem, base_queries, requirements, keywords, fallback_text, lang="en",
                    limit=AUTOMATCH_QUERY_VARIANTS):
    """Build several DISTINCT search phrasings for one scene/shot instead of
    firing a single query at the stock APIs.

    Angles, in priority order:
      1. every LLM-written query verbatim (concrete 'what the camera sees')
      2. subject + action, subject + object, subject + location pairings
      3. each visual requirement (already concrete, camera-visible phrases)
      4. a 2-3 word 'core' of the primary query -- the broad safety net that
         still returns footage when the precise phrase returns nothing
      5. the scene's extracted keywords, and a theme/keyword build_query()
    Deduped (case-insensitively), capped at `limit`, order preserved.
    """
    subj = (sem or {}).get("subject") or ""
    action = (sem or {}).get("action") or ""
    obj = (sem or {}).get("object") or ""
    loc = (sem or {}).get("location") or ""

    ordered = []
    for q in base_queries or []:
        ordered.append(q)
    if subj and action:
        ordered.append(f"{subj} {action}")
    if subj and obj:
        ordered.append(f"{subj} {obj}")
    if subj and loc:
        ordered.append(f"{subj} {loc}")
    for r in (requirements or [])[:3]:
        ordered.append(r)
    primary = (base_queries or [None])[0] or subj or fallback_text
    core = _core_terms(primary, 3)
    if len(core) >= 2:
        ordered.append(" ".join(core))
        ordered.append(" ".join(core[:2]))
    if subj:
        ordered.append(subj)
    if keywords:
        ordered.append(" ".join(keywords[:3]))
    try:
        built = build_query(fallback_text or "", lang=lang)
        if built:
            ordered.append(built)
    except Exception:
        pass

    out, seen = [], set()
    for q in ordered:
        n = _norm_query(q)
        if not n or n in seen:
            continue
        # Trim absurdly long phrases -- keep the first 5 words, no catalogue
        # matches a full sentence.
        words = n.split()
        if len(words) > 5:
            n = " ".join(words[:5])
            if n in seen:
                continue
        seen.add(n)
        out.append(n)
        if len(out) >= limit:
            break
    return out or [_norm_query(fallback_text) or "abstract background"]


def _scene_brief(scene):
    """The exact-script requirements block the judge scores against."""
    sem = scene.get("semantic") or {}
    lines = [f'NARRATION (exact script): "{scene["text"]}"']
    for label, key in (("Subject", "subject"), ("Action", "action"), ("Object", "object"),
                       ("Location", "location"), ("Time/era", "time")):
        if sem.get(key):
            lines.append(f"{label}: {sem[key]}")
    if sem.get("entities"):
        lines.append(f"Entities: {', '.join(sem['entities'])}")
    if scene.get("visual_requirements"):
        lines.append(f"MUST be visible: {', '.join(scene['visual_requirements'])}")
    if scene.get("visual_exclusions"):
        lines.append(f"MUST NOT appear: {', '.join(scene['visual_exclusions'])}")
    lines.append(f"Clip must cover {scene['duration']:.1f}s of narration.")
    return "\n".join(lines)


def _shot_brief(scene, shot):
    """Requirements block for judging ONE shot of a multi-shot scene: the
    shot's own exact words/subject/action, inheriting the scene's location,
    era, and exclusions."""
    sem = scene.get("semantic") or {}
    lines = [f'NARRATION (the exact words this clip covers): "{shot.get("text") or ""}"']
    if shot.get("subject") or sem.get("subject"):
        lines.append(f"Subject: {shot.get('subject') or sem.get('subject')}")
    if shot.get("action"):
        lines.append(f"Action: {shot['action']}")
    if sem.get("location"):
        lines.append(f"Location: {sem['location']}")
    if sem.get("time"):
        lines.append(f"Time/era: {sem['time']}")
    if scene.get("visual_exclusions"):
        lines.append(f"MUST NOT appear: {', '.join(scene['visual_exclusions'])}")
    lines.append(f"Clip must cover {shot.get('duration', 0):.1f}s of narration.")
    return "\n".join(lines)


_PRERANK_STOP = {
    "the", "a", "an", "and", "or", "of", "in", "on", "at", "to", "for", "with",
    "this", "that", "must", "not", "appear", "visible", "clip", "cover", "s",
    "narration", "exact", "script", "subject", "action", "object", "location",
    "time", "era", "entities", "seconds", "footage", "video", "shot", "must",
}
_PRERANK_WORD = re.compile(r"[a-z0-9]{3,}")


def _prerank_signals(scene, shot=None):
    """Weighted terms + exclusion terms for the pre-rank.

    Subject / action / must-be-visible words carry the most matching signal,
    so they're weighted 3x an ordinary narration content word. Exclusion
    words let the pre-rank drop obviously-wrong footage before it ever
    reaches the LLM judge.
    """
    sem = scene.get("semantic") or {}
    src = shot or {}
    high, low, excl = {}, {}, set()

    def add(bucket, text, w):
        for m in _PRERANK_WORD.findall((text or "").lower()):
            if m not in _PRERANK_STOP:
                bucket[m] = max(bucket.get(m, 0), w)

    add(high, src.get("subject") or sem.get("subject"), 3)
    add(high, src.get("action") or sem.get("action"), 3)
    for r in scene.get("visual_requirements") or []:
        add(high, r, 3)
    add(low, sem.get("object"), 2)
    add(low, sem.get("location"), 2)
    add(low, src.get("text") or scene.get("text"), 1)
    for e in scene.get("visual_exclusions") or []:
        for m in _PRERANK_WORD.findall(e.lower()):
            if m not in _PRERANK_STOP:
                excl.add(m)
    weighted = dict(low)
    weighted.update(high)  # high wins on overlap
    return weighted, excl


def _desc_match_score(desc, weighted, exclusions, wset=None):
    """Cheap weighted word-overlap score of a catalogue description against
    the brief's weighted terms:
      + term weight for each brief word present in the description
      + 2 bonus if two brief words appear ADJACENT in the description
      + 3 bonus if a subject/requirement bigram appears verbatim
      - 4 for each exclusion word present
    """
    if wset is None:
        wset = set(weighted)
    words = _PRERANK_WORD.findall((desc or "").lower())
    wjoin = " ".join(words)
    seen = set(words)
    score = sum(weighted[w] for w in seen if w in weighted)
    for a, b in zip(words, words[1:]):
        if a in wset and b in wset:
            score += 2
    score -= 4 * sum(1 for e in exclusions if e in seen)
    for term in weighted:
        if " " in term and term in wjoin:
            score += 3
    return score


def _prerank(pool, weighted, exclusions, keep):
    """Order `pool` by how well each catalogue description matches the brief
    (see _desc_match_score), then keep the top `keep`. Ties broken by having
    a real duration and resolution. Always sorts (even when it doesn't need
    to trim) so the first judged batch is the best of the pool, not just its
    first N in arrival order.
    """
    if not weighted:
        return pool[:keep]
    wset = set(weighted)

    def rank(c):
        return (
            _desc_match_score(c.get("desc"), weighted, exclusions, wset),
            1 if c.get("duration") else 0,
            1 if c.get("width") else 0,
        )

    return sorted(pool, key=rank, reverse=True)[:keep]


def _heuristic_pick(pool, signals):
    """Deterministic fallback for when the Gemini judge is unavailable
    (per-day quota hit, sustained rate-limit) partway through a long
    "auto-match all" run: instead of stopping the whole run, choose the
    candidate whose catalogue description overlaps the brief's weighted terms
    best (the same signal the LLM pre-rank uses) and return it with a
    deliberately modest pseudo-score so it always registers as a soft,
    review-me pick -- never as a confident match. Returns (best, score,
    reason) or (None, -1, "") for an empty pool."""
    weighted, exclusions = signals or ({}, set())
    if not pool:
        return None, -1, ""
    wset = set(weighted)
    scored = sorted(
        pool,
        key=lambda c: (
            _desc_match_score(c.get("desc"), weighted, exclusions, wset),
            1 if c.get("duration") else 0,
            1 if c.get("width") else 0,
        ),
        reverse=True,
    )
    best = scored[0]
    raw = _desc_match_score(best.get("desc"), weighted, exclusions, wset)
    # Map the uncalibrated overlap score into a 30..66 band: comfortably
    # inside "soft" territory for a balanced/strict run so the scene gets
    # filled rather than left empty, but never near a strictness bar -- a
    # keyword-only pick has to be flagged and re-judged later.
    score = int(max(30, min(66, 40 + raw * 3.5)))
    reason = (
        f'keyword-overlap fallback (Gemini judge unavailable): best match by '
        f'term overlap vs "{(best.get("desc") or "")[:80]}"'
    )
    return best, score, reason


# A judged verdict clamps the raw score into a sane band, so a hallucinated
# number can't push obviously-wrong footage over the bar (or bury a clean
# match). "ok" is left as the model gave it.
_VERDICT_CLAMP = {
    "wrong": (0, 28),
    "weak": (30, 55),
    "ok": (0, 100),
    "strong": (72, 96),
}


def _judge_batch(brief, batch, entry_candidates):
    """One Gemini scoring call over `batch` (already the size we want judged).
    Appends every scored candidate to entry_candidates and returns
    (best_asset, best_score, best_reason) for this batch."""
    from llm_client import LLMError, complete, strip_reasoning
    from semantic_timeline import _extract_json

    listing = []
    for i, c in enumerate(batch, start=1):
        desc = ((c.get("desc") or "").strip() or "(no description)")[:140]
        dims = f"{c.get('width')}x{c.get('height')}" if c.get("width") else "res n/a"
        via = c.get("matched_query")
        via_txt = f', via "{via}"' if via else ""
        kind = "photo" if c.get("is_photo") or c.get("kind") == "photo" else f"{c.get('duration') or '?'}s"
        listing.append(f'C{i}: "{desc}" ({kind}, {dims}, {c.get("source")}{via_txt})')
    user = (brief + "\n\nCANDIDATE CLIPS:\n" + "\n".join(listing)
            + "\n\nScore AND give a verdict for every candidate. STRICT JSON only.")
    data, last_err = None, None
    for _ in range(2):  # a truncated/malformed reply is usually a one-off
        try:
            reply = complete(
                [{"role": "system", "content": AUTOMATCH_SYSTEM}, {"role": "user", "content": user}],
                STATE["llm_keys"], temperature=0.1, max_tokens=4000, verbose=False,
            )
        except LLMError as e:
            raise AutomatchUnavailable(str(e))
        try:
            data = _extract_json(strip_reasoning(reply))
            break
        except (ValueError, json.JSONDecodeError) as e:
            last_err = e
    if data is None:
        raise ValueError(f"judge reply unparseable after retry: {last_err}")

    scored = {}
    for s in data.get("scores") or []:
        if not isinstance(s, dict):
            continue
        cid = str(s.get("id") or "").strip().upper()
        try:
            raw = max(0, min(100, int(s.get("score"))))
        except (TypeError, ValueError):
            continue
        verdict = str(s.get("verdict") or "").strip().lower()
        lo, hi = _VERDICT_CLAMP.get(verdict, (0, 100))
        scored[cid] = (max(lo, min(hi, raw)), verdict or "ok", raw)

    best, best_score, best_reason = None, -1, ""
    for i, c in enumerate(batch, start=1):
        score, verdict, raw = scored.get(f"C{i}", (0, "unscored", 0))
        note = f" [{verdict}]" if verdict not in ("ok", "unscored") else ""
        reason = f'judged {score}/100{note} vs "{(c.get("desc") or "")[:80]}"'
        entry_candidates.append({
            "id": c.get("id"), "source": c.get("source"), "desc": c.get("desc"),
            "duration": c.get("duration"), "query": c.get("matched_query"),
            "score": score, "verdict": verdict, "raw_score": raw, "reason": reason,
        })
        if score > best_score:
            best, best_score, best_reason = c, score, reason
    return best, best_score, best_reason


def _pick_best(brief, pool, entry_candidates, signals=None, accept=0):
    """Pre-rank `pool`, then LLM-judge it in batches of AUTOMATCH_JUDGE.

    Judges the strongest batch first; only judges the next batch (up to
    AUTOMATCH_MAX_BATCHES) if nothing in the batches so far reached `accept`
    -- the right clip is often just outside an arbitrary top-N cut, and
    another batch is cheap on Gemini's budget. Returns (best, score, reason)
    across every batch judged."""
    weighted, exclusions = signals or ({}, set())
    ranked = _prerank(pool, weighted, exclusions, AUTOMATCH_POOL)
    best, best_score, best_reason = None, -1, ""
    for b in range(AUTOMATCH_MAX_BATCHES):
        batch = ranked[b * AUTOMATCH_JUDGE:(b + 1) * AUTOMATCH_JUDGE]
        if not batch:
            break
        bbest, bscore, breason = _judge_batch(brief, batch, entry_candidates)
        if bscore > best_score:
            best, best_score, best_reason = bbest, bscore, breason
        if best_score >= accept:
            break
    return best, best_score, best_reason


def _interleave_by_source(pool):
    """Round-robin the pool by provider so trimming to AUTOMATCH_POOL keeps a
    spread of sources instead of whatever one prolific provider returned
    first -- more variety for the pre-rank and judge to choose from."""
    buckets = {}
    for c in pool:
        buckets.setdefault(c.get("source") or "?", []).append(c)
    out = []
    while any(buckets.values()):
        for src in list(buckets):
            if buckets[src]:
                out.append(buckets[src].pop(0))
    return out


def _search_pool(queries, min_duration, min_quality, errors_out):
    """Deduped candidate pool across every query x provider, relaxing the
    duration floor only if it comes up completely dry. Returns (pool,
    relaxed). Page 2 is pulled for the first few queries while the pool is
    thin -- exact matches are found by judging MORE candidates, not by
    settling for the first page."""
    pool, seen = [], set()

    def gather(min_dur, page=1, qs=None):
        targets = list(qs or queries)
        if not targets:
            return
        # Fan the query x provider searches out -- they're independent HTTP
        # calls, so 7 phrasings across 3 providers finish in ~2 waves instead
        # of 21 sequential round-trips. Results are merged in submission order
        # so `matched_query` (used for the "via" hint to the judge) stays the
        # first phrasing that surfaced each clip.
        with ThreadPoolExecutor(max_workers=min(6, len(targets))) as ex:
            results = list(ex.map(
                lambda q: (q, _automatch_search(q, min_dur, min_quality, page=page)),
                targets,
            ))
        for query, (cands, errors) in results:
            errors_out.extend(errors)
            for c in cands:
                key = (c.get("source"), c.get("id"))
                if key not in seen:
                    seen.add(key)
                    c["matched_query"] = query
                    pool.append(c)

    gather(min_duration)
    # Only reach for page 2 while the pool is still thin -- once there are
    # comfortably more than one judge batch's worth, extra pages just cost
    # requests without changing which clip wins.
    if queries and len(pool) < AUTOMATCH_JUDGE * 2:
        gather(min_duration, page=2, qs=queries[:2])
    relaxed = False
    if not pool:
        relaxed = True
        gather(None)
        if queries and len(pool) < AUTOMATCH_JUDGE * 2:
            gather(None, page=2, qs=queries[:2])
    return _interleave_by_source(pool)[:AUTOMATCH_POOL], relaxed


def _revise_queries(brief, tried, result_descs):
    """The automatic-repair half of validate->repair: when nothing scored at
    or above the bar, ask the LLM for different search phrasings given what
    the failed queries actually returned -- one broader, one a synonym, one
    a different concrete framing of the same beat."""
    from llm_client import LLMError, complete, strip_reasoning
    from semantic_timeline import _extract_json

    user = (
        brief
        + f"\n\nStock-video queries already tried: {', '.join(tried)}"
        + ("\nTheir results were about: " + "; ".join(d for d in result_descs if d) if result_descs else "")
        + "\n\nThose queries did not surface matching footage. Suggest 3 DIFFERENT "
        "stock-video search queries, each 2-4 English words of camera-visible things:\n"
        "  1. a BROADER query (drop the least essential word)\n"
        "  2. a SYNONYM query (different words, same visual)\n"
        "  3. a query for a DIFFERENT concrete moment or object from this same beat\n"
        'STRICT JSON only: {"queries":["...","...","..."]}'
    )
    try:
        reply = complete(
            [{"role": "system", "content": "You craft stock-video search queries. STRICT JSON only."},
             {"role": "user", "content": user}],
            STATE["llm_keys"], temperature=0.4, max_tokens=1500, verbose=False,
        )
    except LLMError as e:
        raise AutomatchUnavailable(str(e))
    data = _extract_json(strip_reasoning(reply))
    return [q.strip() for q in data.get("queries") or [] if isinstance(q, str) and q.strip()]


_PHOTO_BRIEF_NOTE = (
    "\nSome candidates are STILL PHOTOS (rendered as a slow Ken Burns zoom) -- "
    "marked (photo). Judge them only on whether the image depicts the exact "
    "narration content; do not penalize for being a still."
)


def _match_with_repair(brief, queries, min_duration, min_quality, candidates_out,
                       errors_out, threshold, signals=None, photo_queries=None):
    """search -> judge -> (while nothing reaches the bar) revise queries and
    retry, up to two repair rounds -> a dedicated still-photo round. Photos
    are also folded into the FIRST judge round when the video pool is thin,
    so a spot-on still isn't missed just because a mediocre video existed.
    Best overall wins. Returns (best, score, reason, relaxed, heuristic) --
    `heuristic` True means the Gemini judge was unavailable and `best` was
    chosen by keyword overlap alone, so it should be saved as a soft pick and
    re-judged on a later run."""
    pool, relaxed = _search_pool(queries, min_duration, min_quality, errors_out)
    photo_qs = photo_queries or queries
    used_brief = brief
    if 0 < len(pool) < 10:
        photos = _photo_pool(photo_qs, errors_out, limit=8)
        for p in photos:
            p["is_photo"] = True
        if photos:
            pool = pool + photos
            used_brief = brief + _PHOTO_BRIEF_NOTE
            errors_out.append(f"Video pool thin ({len(pool) - len(photos)}); mixed in {len(photos)} still photo(s).")

    best, score, reason = None, -1, ""
    heuristic = False
    if pool and STATE.get("llm_down"):
        # An earlier scene in this run already found the Gemini judge out of
        # room -- don't spend this scene's retries rediscovering that, go
        # straight to the keyword-overlap fallback.
        best, score, reason = _heuristic_pick(pool, signals)
        heuristic = best is not None
        if heuristic:
            errors_out.append(
                "Gemini judge still unavailable -- picked by keyword overlap (soft). "
                "Re-run auto-match once quota resets to upgrade this scene."
            )
    elif pool:
        try:
            best, score, reason = _pick_best(used_brief, pool, candidates_out, signals, threshold)
        except AutomatchUnavailable as e:
            # Gemini can't score anything right now. Rather than stop the whole
            # "auto-match all" run (and leave every remaining scene empty),
            # remember it's down and fill this scene with the best
            # keyword-overlap match, flagged soft for a later re-judge.
            STATE["llm_down"] = str(e)
            best, score, reason = _heuristic_pick(pool, signals)
            heuristic = best is not None
            errors_out.append(
                f"Gemini scoring unavailable ({e}). Picked the closest match by "
                f"keyword overlap instead (soft) -- re-run auto-match after the "
                f"quota resets to replace it with a judged pick."
            )
            if not heuristic:
                return None, -1, "", relaxed, heuristic
        except (ValueError, json.JSONDecodeError) as e:
            errors_out.append(f"Scoring failed: {e}")
            return None, -1, "", relaxed, False
        if best is not None and best.get("is_photo"):
            reason = f"[photo] {reason}"

    if heuristic:
        # Repair rounds and the photo round all need the judge -- skip them.
        return best, score, reason, relaxed, True

    tried = {q.lower() for q in queries}
    for repair_round in (1, 2):
        if best is not None and score >= threshold:
            return best, score, reason, relaxed, False
        if STATE.get("llm_down"):
            # Judge is out -- every repair step needs it. Don't burn retries
            # rediscovering that; keep whatever the first round found.
            break
        try:
            revised = _revise_queries(brief, sorted(tried), [c.get("desc") for c in pool[:6]])
        except AutomatchUnavailable as e:
            STATE["llm_down"] = str(e)
            errors_out.append(f"Gemini went unavailable mid-repair ({e}) -- keeping the best pick so far.")
            break
        except (ValueError, json.JSONDecodeError) as e:
            errors_out.append(f"Query revision failed: {e}")
            break
        revised = [q for q in revised if q.lower() not in tried][:3]
        if not revised:
            break
        tried.update(q.lower() for q in revised)
        errors_out.append(f"Repair round {repair_round} tried revised queries: {', '.join(revised)}")
        pool2, relaxed2 = _search_pool(revised, min_duration, min_quality, errors_out)
        pool = pool2 or pool
        if pool2:
            try:
                best2, score2, reason2 = _pick_best(brief, pool2, candidates_out, signals, threshold)
            except AutomatchUnavailable as e:
                STATE["llm_down"] = str(e)
                errors_out.append(f"Gemini went unavailable mid-repair ({e}) -- keeping the best pick so far.")
                break
            except (ValueError, json.JSONDecodeError) as e:
                errors_out.append(f"Scoring failed on repair round: {e}")
                break
            if score2 > score:
                best, score, reason, relaxed = best2, score2, reason2, relaxed or relaxed2

    if (best is None or score < threshold) and not STATE.get("llm_down"):
        # Dedicated photo round: an exact still beats an approximate video.
        photos = _photo_pool(sorted(set(list(tried) + list(photo_qs))), errors_out, limit=12)
        for p in photos:
            p["is_photo"] = True
        if photos:
            errors_out.append(f"Photo round judged {len(photos)} still photo(s).")
            try:
                pbest, pscore, preason = _pick_best(
                    brief + _PHOTO_BRIEF_NOTE, photos, candidates_out, signals, threshold
                )
                if pscore > score:
                    best, score, reason = pbest, pscore, f"[photo] {preason}"
            except AutomatchUnavailable as e:
                STATE["llm_down"] = str(e)
                errors_out.append(f"Gemini went unavailable on the photo round ({e}).")
            except (ValueError, json.JSONDecodeError) as e:
                errors_out.append(f"Scoring failed on photo round: {e}")

    return best, score, reason, relaxed, False


def _finish_with_ai_fallback(scene, entry, best_score):
    """Auto-match last resort: when no stock clip cleared the bar and the
    caller enabled AI fallback (`STATE["ai_fallback"]`, set from the
    /api/automatch request), generate one house-style illustration, save it
    as the scene's pick, and return `entry` as a soft PASS. Returns None if
    AI fallback is off or generation failed -- the caller then returns the
    FAIL entry it already built."""
    if not STATE.get("ai_fallback"):
        return None
    try:
        asset = _ai_autofill(scene, scene)
    except Exception as e:
        entry["failure_reasons"].append(f"AI illustration fallback failed: {e}")
        return None
    with STATE["lock"]:
        remove_clips_for_scene(scene["index"])
        dest = register_asset(scene["index"], asset)
        selections = load_selections()
        record = selection_record(scene["index"], asset, dest, asset["prompt"])
        record["match_score"] = best_score if (best_score is not None and best_score >= 0) else None
        record["match_reason"] = "AI illustration fallback (no stock clip cleared the bar)"
        record["auto_matched"] = True
        record["ai_generated"] = True
        record["soft_match"] = True
        selections[str(scene["index"])] = record
        save_selections(selections)
    entry["status"] = "PASS"
    entry["soft"] = True
    entry["ai_generated"] = True
    entry["chosen"] = {"source": "ai", "desc": asset["desc"], "file": dest.name, "ai": True}
    entry["failure_reasons"].append(
        "No stock clip cleared the bar -- filled with a generated house-style "
        "illustration (flagged for review)."
    )
    return entry


def _automatch_shots(scene, entry, min_quality, threshold, soft_floor=None):
    """Per-shot auto-match for a multi-shot scene: each shot gets its own
    search + judging against ITS exact words, and the scene is saved as a
    multi-clip selection in shot order -- step3 then cuts each clip at the
    shot's exact narration boundary. Every shot must reach the acceptance bar
    or nothing is saved (a half-matched scene would silently misalign the
    later shots). With `soft_floor` set (an "all unpicked" run), the bar drops
    to the floor and the scene is flagged "soft" if any shot came in under the
    strictness threshold."""
    accept = soft_floor if soft_floor is not None else threshold
    shots = scene.get("shots") or []
    entry["shots"] = []
    any_soft = False
    ai_used = False
    # One (shot, asset_or_None, score_or_None, is_ai) per shot, in shot order --
    # kept aligned with `shots` so step3's shot-exact cut boundaries still line
    # up even when some shots were AI-filled.
    outcomes = []
    for shot in shots:
        shot_entry = {"text": shot.get("text"), "query": shot.get("query"),
                      "duration": round(shot.get("duration", 0), 2),
                      "status": "FAIL", "score": None, "chosen": None, "reason": None}
        entry["shots"].append(shot_entry)
        base_qs = [q for q in (shot.get("query"), scene.get("query"),
                               *(scene.get("queries") or [])) if q]
        base_qs = list(dict.fromkeys(base_qs))
        sem = scene.get("semantic") or {}
        shot_sem = {"subject": shot.get("subject") or sem.get("subject"),
                    "action": shot.get("action") or sem.get("action"),
                    "object": sem.get("object"), "location": sem.get("location")}
        queries = _expand_queries(
            shot_sem, base_qs, scene.get("visual_requirements"),
            scene.get("keywords"), shot.get("text") or scene.get("text"),
            lang=STATE.get("lang", "en"),
        )
        signals = _prerank_signals(scene, shot=shot)
        shot_entry["queries_tried"] = queries
        min_duration = max(1, int(shot.get("duration", 0) + 0.999))
        best, score, reason, _relaxed, heuristic = _match_with_repair(
            _shot_brief(scene, shot), queries, min_duration, min_quality,
            entry["candidates"], entry["failure_reasons"], accept, signals=signals,
        )
        shot_entry["score"] = score if score >= 0 else None
        if heuristic:
            shot_entry["heuristic"] = True
        if best is not None and score >= accept:
            shot_entry["status"] = "SOFT" if (score < threshold or heuristic) else "PASS"
            any_soft = any_soft or score < threshold or heuristic
            shot_entry["chosen"] = {"id": best.get("id"), "source": best.get("source"), "desc": best.get("desc")}
            shot_entry["reason"] = reason
            outcomes.append((shot, best, score, False))
            continue
        # No stock clip for this shot -- generate one if AI fallback is on,
        # otherwise leave it unfilled (the scene won't be saved).
        if STATE.get("ai_fallback"):
            try:
                asset = _ai_autofill(shot, scene)
            except Exception as e:
                shot_entry["reason"] = f"No stock clip; AI fallback failed: {e}"
                outcomes.append((shot, None, None, False))
                continue
            ai_used = True
            any_soft = True
            shot_entry["status"] = "AI"
            shot_entry["chosen"] = {"source": "ai", "desc": asset["desc"]}
            shot_entry["reason"] = "No stock clip -- generated a house-style illustration."
            outcomes.append((shot, asset, score if score >= 0 else None, True))
            continue
        shot_entry["reason"] = (
            f"Best candidate scored {score} (needed {accept})." if score >= 0
            else f"No usable search results for: {', '.join(queries[:2])}"
        )
        outcomes.append((shot, None, None, False))

    unfilled = [se for se, (_, a, _, _) in zip(entry["shots"], outcomes) if a is None]
    if unfilled:
        entry["failure_reasons"].append(
            f"{len(unfilled)} of {len(shots)} shots had no usable candidate -- "
            f"nothing saved (a partial multi-pick would misalign the other shots). "
            f"Pick this scene manually (multi mode), enable the AI fallback, or re-run."
        )
        entry["score"] = min((se["score"] for se in entry["shots"] if se["score"] is not None), default=None)
        return entry

    scores_only = [sc for _, _, sc, _ in outcomes if sc is not None]
    any_heuristic = any(se.get("heuristic") for se in entry["shots"])
    with STATE["lock"]:
        remove_clips_for_scene(scene["index"])
        items = []
        for j, (shot, asset, score, is_ai) in enumerate(outcomes):
            dest = register_asset(scene["index"], asset, part=j + 1)
            item = multi_item_record(
                asset, dest, shot.get("duration", 0), shot=shot, shot_index=j + 1,
            )
            if score is not None:
                item["match_score"] = score
            if is_ai:
                item["ai_generated"] = True
            items.append(item)
        selections = load_selections()
        selections[str(scene["index"])] = {
            "scene_index": scene["index"],
            "type": "multi",
            "items": items,
            "shot_aligned": True,
            "query": scene.get("query"),
            "auto_matched": True,
            "soft_match": any_soft,
            "heuristic": any_heuristic,
            "ai_generated": ai_used,
            "match_score": min(scores_only) if scores_only else None,
        }
        save_selections(selections)
    entry["status"] = "PASS"
    entry["soft"] = any_soft
    if any_heuristic:
        entry["heuristic"] = True
    entry["ai_generated"] = ai_used
    entry["score"] = min(scores_only) if scores_only else None
    entry["chosen"] = {
        "multi": True,
        "files": [it["file"] for it in items],
        "descs": [se["chosen"]["desc"] if se["chosen"] else "" for se in entry["shots"]],
    }
    if ai_used:
        entry["failure_reasons"].append(
            "One or more shots had no stock match -- filled with generated "
            "house-style illustration(s), flagged for review."
        )
    return entry


def automatch_scene(scene, min_quality=None, threshold=AUTOMATCH_THRESHOLD, soft_floor=None):
    """Search + score + (maybe) select the best clip for one scene.
    A multi-shot scene (from the semantic timeline) is matched shot by
    shot and saved as a multi-clip selection with shot-exact timing; a
    single-visual scene gets one clip. Returns the report entry; on PASS
    the selection is saved exactly as a human pick would be.

    `soft_floor` (used by the "Auto-match all unpicked" run): when the best
    candidate misses `threshold` but reaches this floor, the scene is filled
    with it anyway and flagged "soft" in the report and the selection, rather
    than left empty. Pass None (the default, used by the per-scene button) to
    keep the strict all-or-nothing behavior."""
    entry = {
        "scene_index": scene["index"],
        "script": scene["text"],
        "voice_start": round(scene["start"], 2),
        "voice_end": round(scene["end"], 2),
        "duration": round(scene["duration"], 2),
        "status": "FAIL",
        "soft": False,
        "score": None,
        "chosen": None,
        "candidates": [],
        "failure_reasons": [],
    }
    if not STATE.get("llm_keys"):
        entry["failure_reasons"].append("No Gemini API key -- semantic scoring unavailable.")
        return entry

    if len(scene.get("shots") or []) >= 2:
        return _automatch_shots(scene, entry, min_quality, threshold, soft_floor)

    base_qs = [q for q in (scene.get("queries") or []) if q] or [scene.get("query") or ""]
    queries = _expand_queries(
        scene.get("semantic") or {}, base_qs, scene.get("visual_requirements"),
        scene.get("keywords"), scene.get("text"), lang=STATE.get("lang", "en"),
    )
    signals = _prerank_signals(scene)
    entry["queries_tried"] = queries
    min_duration = max(1, int(scene["duration"] + 0.999))
    # Let the repair loop stop as soon as it clears the acceptance bar -- the
    # soft floor for an "all" run -- instead of burning extra Gemini calls
    # chasing the full strictness threshold it may never reach.
    accept = soft_floor if soft_floor is not None else threshold
    best, best_score, best_reason, duration_relaxed, heuristic = _match_with_repair(
        _scene_brief(scene), queries, min_duration, min_quality,
        entry["candidates"], entry["failure_reasons"], accept, signals=signals,
    )
    entry["candidates"].sort(key=lambda c: -c["score"])
    entry["score"] = best_score if best_score >= 0 else None
    entry["pool_size"] = len(entry["candidates"])
    if heuristic:
        entry["heuristic"] = True
    if best is None and best_score < 0:
        entry["failure_reasons"].append(f"No usable search results for: {', '.join(queries[:2])}")
        return _finish_with_ai_fallback(scene, entry, None) or entry

    soft = False
    if best is None or best_score < accept:
        entry["failure_reasons"].append(
            f"Best candidate scored {best_score} (needed {accept}) -- "
            f"pick this scene manually; the scored list is in the report."
        )
        if duration_relaxed:
            entry["failure_reasons"].append(
                f"Note: no clip met the {min_duration}s minimum duration; shorter clips were considered."
            )
        return _finish_with_ai_fallback(scene, entry, best_score) or entry
    if heuristic:
        soft = True
        entry["failure_reasons"].append(
            "Gemini judge was unavailable -- this scene was filled by keyword "
            "overlap only (score is an estimate). Re-run auto-match once the "
            "quota resets and it will be re-judged automatically."
        )
    elif best_score < threshold:
        soft = True
        entry["failure_reasons"].append(
            f"Soft match: best candidate scored {best_score}, under the {threshold} strictness "
            f"bar -- filled in anyway so the scene isn't left empty. Review it, or re-run at "
            f"higher strictness / pick manually to replace it."
        )

    with STATE["lock"]:
        remove_clips_for_scene(scene["index"])
        dest = register_asset(scene["index"], best)
        selections = load_selections()
        record = selection_record(scene["index"], best, dest, best.get("matched_query"))
        record["match_score"] = best_score
        record["match_reason"] = best_reason
        record["auto_matched"] = True
        record["soft_match"] = soft
        if heuristic:
            record["heuristic"] = True
        selections[str(scene["index"])] = record
        save_selections(selections)
    entry["status"] = "PASS"
    entry["soft"] = soft
    entry["chosen"] = {
        "id": best.get("id"),
        "source": best.get("source"),
        "desc": best.get("desc"),
        "file": dest.name,
        "duration": best.get("duration"),
    }
    if duration_relaxed and (best.get("duration") or 0) < scene["duration"]:
        entry["failure_reasons"].append(
            f"Clip is {best.get('duration')}s for a {scene['duration']:.1f}s scene -- "
            f"consider adding a second clip (multi mode) instead of looping."
        )
    return entry


def save_sync_report(entries):
    """Merge this run's automatch entries into sync_report.json (keyed by
    scene index) so repeated runs keep the newest decision per scene."""
    path = STATE["selections_path"].parent / "sync_report.json"
    report = {}
    if path.exists():
        try:
            report = load_json(path)
        except Exception:
            report = {}
    scenes = report.get("scenes") or {}
    for e in entries:
        scenes[str(e["scene_index"])] = e
    report["scenes"] = scenes
    report["threshold"] = AUTOMATCH_THRESHOLD
    save_json(report, path)
    return path


def load_sync_report():
    """The scenes map from sync_report.json ({str(index): entry}), or {}.
    Used by the review page to show why each auto-matched scene was picked
    (score, soft/keyword-only/AI flags, failure reasons) after a reload when
    the browser's in-memory matchNotes are gone."""
    path = STATE["selections_path"].parent / "sync_report.json"
    if not path.exists():
        return {}
    try:
        return (load_json(path) or {}).get("scenes") or {}
    except Exception:
        return {}


def _review_clip_url(item):
    """A browser-playable URL for one picked asset on the review page.
    A file already sitting in clips/ (local upload, AI illustration, or a
    clip fetched by an earlier step3 run) is served straight from disk; a
    remote pick that hasn't been downloaded yet streams from its small
    preview rendition, falling back to the full download URL."""
    f = item.get("file")
    if f and (STATE["clips_dir"] / os.path.basename(f)).is_file():
        return f"/clips/{urllib.parse.quote(os.path.basename(f))}", True
    return item.get("preview_url") or item.get("download_url"), False


def build_review_payload():
    """Everything the "Review picked clips" page needs: each scene with its
    narration, the clip picked for it resolved to a playable preview URL, and
    the auto-matcher's own verdict for it from sync_report.json."""
    selections = load_selections()
    report = load_sync_report()
    rows = []
    reviewed = 0
    for s in STATE["scenes"]:
        idx = s["index"]
        sel = selections.get(str(idx))
        preview = None
        if sel is not None:
            if sel.get("type") == "multi":
                items = []
                for it in sel.get("items") or []:
                    url, local = _review_clip_url(it)
                    items.append({
                        "type": it.get("type") or "video",
                        "url": url,
                        "on_disk": local,
                        "thumb": it.get("thumb"),
                        "source": it.get("source") or "pexels",
                        "author": it.get("author"),
                        "page_url": it.get("page_url"),
                        "shot_index": it.get("shot_index"),
                        "shot_text": it.get("shot_text"),
                    })
                preview = {"type": "multi", "items": items,
                           "shot_aligned": bool(sel.get("shot_aligned"))}
            else:
                url, local = _review_clip_url(sel)
                preview = {
                    "type": sel.get("type") or "video",
                    "items": [{
                        "type": sel.get("type") or "video",
                        "url": url,
                        "on_disk": local,
                        "thumb": sel.get("thumb"),
                        "source": sel.get("source") or "pexels",
                        "author": sel.get("author"),
                        "page_url": sel.get("page_url"),
                    }],
                }
        if sel is not None and sel.get("reviewed"):
            reviewed += 1
        rep = report.get(str(idx))
        rep_slim = None
        if rep:
            chosen = rep.get("chosen") or {}
            rep_slim = {
                "status": rep.get("status"),
                "score": rep.get("score"),
                "soft": bool(rep.get("soft")),
                "heuristic": bool(rep.get("heuristic")),
                "ai_generated": bool(rep.get("ai_generated")),
                "chosen": {"desc": chosen.get("desc")} if chosen else None,
                "failure_reasons": rep.get("failure_reasons") or [],
                "queries_tried": rep.get("queries_tried") or [],
                "candidates_n": len(rep.get("candidates") or []),
            }
        rows.append({
            "index": idx,
            "text": s.get("text") or "",
            "start": s.get("start"),
            "end": s.get("end"),
            "duration": s.get("duration"),
            "shots": [
                {"text": sh.get("text"), "start": sh.get("start"),
                 "end": sh.get("end"), "duration": sh.get("duration")}
                for sh in s.get("shots") or []
            ],
            "query": (sel or {}).get("query"),
            "selection": {
                "source": (sel or {}).get("source"),
                "type": (sel or {}).get("type"),
                "author": (sel or {}).get("author"),
                "file": (sel or {}).get("file"),
                "heuristic": bool((sel or {}).get("heuristic")),
                "soft": bool((sel or {}).get("soft")),
            } if sel is not None else None,
            "preview": preview,
            "report": rep_slim,
            "reviewed": bool((sel or {}).get("reviewed")),
        })
    return {
        "project": STATE["project"],
        "rtl": STATE.get("rtl", False),
        "scenes": rows,
        "reviewed": reviewed,
        "total": len(STATE["scenes"]),
        "picked": len(selections),
    }


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):  # quieter console
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Lets the pipeline dashboard (a different local port) read
        # /api/scenes to check WHICH project this picker is serving before
        # embedding it. Local-only server; nothing sensitive in responses.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if route == "/":
            return self._send_html(PAGE_HTML)

        if route == "/api/scenes":
            refresh_llm_keys()  # so a key added after launch enables the button
            selections = load_selections()
            scenes = [
                {
                    "index": s["index"],
                    "text": s["text"],
                    "start": s["start"],
                    "end": s["end"],
                    "duration": s["duration"],
                    "query": s.get("query") or " ".join(s.get("keywords") or []),
                    "keywords": s.get("keywords") or [],
                    # Semantic-timeline fields (absent on legacy scenes.json):
                    "queries": s.get("queries") or [],
                    "semantic": s.get("semantic") or None,
                    "visual_requirements": s.get("visual_requirements") or [],
                    "visual_exclusions": s.get("visual_exclusions") or [],
                    "shots": [
                        {
                            "text": sh.get("text"),
                            "query": sh.get("query"),
                            "start": sh.get("start"),
                            "end": sh.get("end"),
                            "duration": sh.get("duration"),
                        }
                        for sh in s.get("shots") or []
                    ],
                }
                for s in STATE["scenes"]
            ]
            return self._send_json({
                "project": STATE["project"],
                "scenes": scenes,
                "selections": selections,
                "rate_remaining": STATE.get("rate_remaining"),
                "rtl": STATE.get("rtl", False),
                "highlight": sorted(STATE.get("highlight") or []),
                "automatch_available": bool(STATE.get("llm_keys")),
                "auto_render": STATE.get("auto_render", True),
                # AI illustration tab -- always available (Pollinations needs
                # no key); a token just makes it faster and watermark-free.
                "ai_available": True,
                "ai_watermarked": not bool(STATE.get("pollinations_token")),
            })

        if route == "/api/review":
            # Everything the "Review picked clips" page needs -- script text
            # next to a playable preview of the clip picked for each scene,
            # plus the auto-matcher's verdict, so mismatches can be spotted
            # and replaced before the render.
            return self._send_json(build_review_payload())

        if route == "/api/search":
            query = (params.get("q") or [""])[0].strip()
            page = int((params.get("page") or ["1"])[0])
            min_duration = (params.get("min_duration") or [""])[0]
            min_duration = int(min_duration) if min_duration.isdigit() else None
            min_quality = (params.get("min_quality") or [""])[0]
            min_quality = min_quality if min_quality in QUALITY_TIERS else None
            if not query:
                return self._send_json({"error": "Empty search query."}, status=400)
            try:
                payload, from_cache = pexels_search(query, page, min_duration)
            except urllib.error.HTTPError as e:
                return self._send_json({"error": pexels_error_detail(e)}, status=502)
            except urllib.error.URLError as e:
                return self._send_json({"error": f"Could not reach Pexels: {e.reason}"}, status=502)

            videos = [simplify_video(v, min_quality) for v in payload.get("videos", [])]
            videos = [v for v in videos if v and v["download_url"] and v["preview_url"]]
            return self._send_json({
                "videos": videos,
                "page": page,
                "total_results": payload.get("total_results", 0),
                "has_next": bool(payload.get("next_page")),
                "from_cache": from_cache,
                "rate_remaining": STATE.get("rate_remaining"),
            })

        if route == "/api/search-photos":
            query = (params.get("q") or [""])[0].strip()
            page = int((params.get("page") or ["1"])[0])
            min_quality = (params.get("min_quality") or [""])[0]
            min_quality = min_quality if min_quality in QUALITY_TIERS else None
            if not query:
                return self._send_json({"error": "Empty search query."}, status=400)
            try:
                payload, from_cache = pexels_photo_search(query, page)
            except urllib.error.HTTPError as e:
                return self._send_json({"error": pexels_error_detail(e)}, status=502)
            except urllib.error.URLError as e:
                return self._send_json({"error": f"Could not reach Pexels: {e.reason}"}, status=502)

            photos = [simplify_photo(p) for p in payload.get("photos", [])]
            photos = [p for p in photos if p["download_url"] and p["preview_url"] and meets_quality(p, min_quality)]
            return self._send_json({
                "videos": photos,
                "page": page,
                "total_results": payload.get("total_results", 0),
                "has_next": bool(payload.get("next_page")),
                "from_cache": from_cache,
                "rate_remaining": STATE.get("rate_remaining"),
            })

        if route == "/api/search-pixabay":
            query = (params.get("q") or [""])[0].strip()
            page = int((params.get("page") or ["1"])[0])
            min_duration = (params.get("min_duration") or [""])[0]
            min_duration = int(min_duration) if min_duration.isdigit() else None
            min_quality = (params.get("min_quality") or [""])[0]
            min_quality = min_quality if min_quality in QUALITY_TIERS else None
            if not query:
                return self._send_json({"error": "Empty search query."}, status=400)
            try:
                payload, from_cache = pixabay_search(query, page, min_duration)
            except MissingKeyError as e:
                return self._send_json({"error": str(e)}, status=400)
            except urllib.error.HTTPError as e:
                return self._send_json({"error": pixabay_error_detail(e)}, status=502)
            except urllib.error.URLError as e:
                return self._send_json({"error": f"Could not reach Pixabay: {e.reason}"}, status=502)

            videos = [simplify_pixabay_video(v, min_quality) for v in payload.get("hits", [])]
            videos = [v for v in videos if v and v["download_url"] and v["preview_url"]]
            videos = filter_long_enough(videos, min_duration)
            total = payload.get("totalHits", 0)
            return self._send_json({
                "videos": videos,
                "page": page,
                "total_results": total,
                "has_next": page * PER_PAGE < total,
                "from_cache": from_cache,
                "rate_remaining": None,
            })

        if route == "/api/search-pixabay-photos":
            query = (params.get("q") or [""])[0].strip()
            page = int((params.get("page") or ["1"])[0])
            min_quality = (params.get("min_quality") or [""])[0]
            min_quality = min_quality if min_quality in QUALITY_TIERS else None
            if not query:
                return self._send_json({"error": "Empty search query."}, status=400)
            try:
                payload, from_cache = pixabay_photo_search(query, page)
            except MissingKeyError as e:
                return self._send_json({"error": str(e)}, status=400)
            except urllib.error.HTTPError as e:
                return self._send_json({"error": pixabay_error_detail(e)}, status=502)
            except urllib.error.URLError as e:
                return self._send_json({"error": f"Could not reach Pixabay: {e.reason}"}, status=502)

            photos = [simplify_pixabay_photo(p) for p in payload.get("hits", [])]
            photos = [p for p in photos if p["download_url"] and p["preview_url"] and meets_quality(p, min_quality)]
            total = payload.get("totalHits", 0)
            return self._send_json({
                "videos": photos,
                "page": page,
                "total_results": total,
                "has_next": page * PER_PAGE < total,
                "from_cache": from_cache,
                "rate_remaining": None,
            })

        if route == "/api/search-coverr":
            query = (params.get("q") or [""])[0].strip()
            page = int((params.get("page") or ["1"])[0])
            min_duration = (params.get("min_duration") or [""])[0]
            min_duration = int(min_duration) if min_duration.isdigit() else None
            min_quality = (params.get("min_quality") or [""])[0]
            min_quality = min_quality if min_quality in QUALITY_TIERS else None
            if not query:
                return self._send_json({"error": "Empty search query."}, status=400)
            try:
                payload, from_cache = coverr_search(query, page)
            except MissingKeyError as e:
                return self._send_json({"error": str(e)}, status=400)
            except urllib.error.HTTPError as e:
                return self._send_json({"error": coverr_error_detail(e)}, status=502)
            except urllib.error.URLError as e:
                return self._send_json({"error": f"Could not reach Coverr: {e.reason}"}, status=502)

            # Coverr's API never reports a clip's resolution, so a quality
            # tier can't be enforced here -- results are returned unfiltered
            # and quality_unverified tells the UI to say so instead of
            # silently ignoring the filter.
            videos = [simplify_coverr_video(v) for v in payload.get("hits", [])]
            videos = [v for v in videos if v["download_url"] and v["preview_url"]]
            videos = filter_long_enough(videos, min_duration)
            return self._send_json({
                "videos": videos,
                "page": page,
                "total_results": payload.get("total", 0),
                "has_next": page < payload.get("pages", 0),
                "from_cache": from_cache,
                "rate_remaining": None,
                "quality_unverified": bool(min_quality),
            })

        if route.startswith("/clips/") or route.startswith("/aicache/"):
            if route.startswith("/clips/"):
                base, prefix = STATE["clips_dir"], "/clips/"
            else:
                base, prefix = STATE["ai_cache_dir"], "/aicache/"
            name = os.path.basename(urllib.parse.unquote(route[len(prefix):]))
            path = base / name
            if not path.exists():
                self.send_error(404)
                return
            data = path.read_bytes()
            content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_error(404)

    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

        if route == "/api/select":
            scene_index = int(body["index"])
            asset = body["asset"]
            with STATE["lock"]:
                remove_clips_for_scene(scene_index)
                try:
                    dest = register_asset(scene_index, asset)
                except Exception as e:
                    return self._send_json({"error": f"Save failed: {e}"}, status=502)
                selections = load_selections()
                selections[str(scene_index)] = selection_record(scene_index, asset, dest, body.get("query"))
                save_selections(selections)
            return self._send_json({"ok": True, "file": dest.name})

        if route == "/api/select-range":
            scene_from = int(body["from"])
            scene_to = int(body["to"])
            asset = body["asset"]
            if scene_to < scene_from:
                return self._send_json({"error": '"To" must be greater than or equal to "From".'}, status=400)
            targets = sorted(s["index"] for s in STATE["scenes"] if scene_from <= s["index"] <= scene_to)
            if not targets:
                return self._send_json({"error": "No scenes found in that range."}, status=400)
            with STATE["lock"]:
                selections = load_selections()
                first_dest = None
                for idx in targets:
                    remove_clips_for_scene(idx)
                    try:
                        if asset.get("kind") != "local":
                            # Nothing to fetch yet -- every scene in the range
                            # just points at the same not-yet-downloaded asset.
                            dest = resolve_dest_path(idx, asset)
                        elif first_dest is None:
                            dest = materialize_local_asset(idx, asset)
                            first_dest = dest
                        else:
                            # Same local file, every remaining scene -- copy the
                            # one decode instead of repeating it N times.
                            dest = resolve_dest_path(idx, asset)
                            shutil.copyfile(first_dest, dest)
                    except Exception as e:
                        return self._send_json({"error": f"Save failed: {e}"}, status=502)
                    selections[str(idx)] = selection_record(idx, asset, dest, body.get("query"))
                save_selections(selections)
            return self._send_json({"ok": True, "count": len(targets)})

        if route == "/api/select-multi":
            scene_index = int(body["index"])
            assets = body.get("assets") or []
            if not isinstance(assets, list) or not assets:
                return self._send_json({"error": "Pick at least one clip."}, status=400)
            scene = next((s for s in STATE["scenes"] if s["index"] == scene_index), None)
            if scene is None:
                return self._send_json({"error": "Unknown scene."}, status=400)
            # A multi-shot scene: one clip per shot, each locked to that shot's
            # own narration window (see multi_item_record). Anything else
            # (single-visual scene, or a pick count that doesn't match the
            # shots) falls back to an even split of the scene's total time.
            shots = scene.get("shots") or []
            shot_aligned = len(shots) >= 2 and len(assets) == len(shots)
            if not shot_aligned and len(shots) >= 2 and len(assets) != len(shots):
                return self._send_json(
                    {"error": f"This scene has {len(shots)} shots -- pick exactly {len(shots)} "
                              f"clip(s), one per shot, so each clip covers its own shot's "
                              f"narration (you picked {len(assets)})."},
                    status=400,
                )
            with STATE["lock"]:
                remove_clips_for_scene(scene_index)
                share_seconds = scene["duration"] / len(assets)
                items = []
                try:
                    for i, asset in enumerate(assets):
                        dest = register_asset(scene_index, asset, part=i + 1)
                        if shot_aligned:
                            items.append(multi_item_record(
                                asset, dest, shots[i].get("duration", share_seconds),
                                shot=shots[i], shot_index=i + 1,
                            ))
                        else:
                            items.append(multi_item_record(asset, dest, share_seconds))
                except Exception as e:
                    return self._send_json({"error": f"Save failed: {e}"}, status=502)
                selections = load_selections()
                selections[str(scene_index)] = {
                    "scene_index": scene_index,
                    "type": "multi",
                    "items": items,
                    "shot_aligned": shot_aligned,
                    "query": body.get("query"),
                }
                save_selections(selections)
            return self._send_json({"ok": True, "count": len(items), "shot_aligned": shot_aligned})

        if route == "/api/ai-image":
            # Generate ONE house-style illustration for a scene and return it
            # as a grid card (kind:"ai"). The browser calls this once per
            # requested variation so it can show them as they arrive and pace
            # itself against Pollinations' free-tier rate limit. `batch` +
            # `seed_offset` make a Generate click's variations reproducible;
            # nothing is saved until the user actually picks one.
            try:
                idx = int(body["index"])
            except (KeyError, TypeError, ValueError):
                return self._send_json({"error": "Missing scene index."}, status=400)
            scene = next((s for s in STATE["scenes"] if s["index"] == idx), None)
            if scene is None:
                return self._send_json({"error": "Unknown scene."}, status=400)
            raw = bool(body.get("raw"))
            subject = (body.get("prompt") or "").strip() or _ai_scene_subject(scene)
            try:
                seed = int(body.get("batch", 0)) + int(body.get("seed_offset", 0))
            except (TypeError, ValueError):
                seed = 0
            if seed <= 0:
                seed = random.randint(1, 9_999_999)
            try:
                asset = ai_generate_for_scene(subject, seed, raw=raw)
            except urllib.error.HTTPError as e:
                msg = (
                    "Pollinations rate limit hit (the free anonymous tier is about one image "
                    "every 15s). Wait a few seconds and click Generate again, or add a free "
                    "token to tools/pollinations_token.txt for the faster tier."
                    if e.code == 429 else f"Pollinations returned HTTP {e.code}."
                )
                return self._send_json({"error": msg}, status=502)
            except urllib.error.URLError as e:
                return self._send_json({"error": f"Could not reach Pollinations: {e.reason}"}, status=502)
            except Exception as e:
                return self._send_json({"error": f"AI image generation failed: {e}"}, status=502)
            return self._send_json({"ok": True, "image": asset})

        if route == "/api/automatch":
            # Semantic auto-match: one scene ({"index": N}) or the unpicked
            # scenes ({"all": true}, optionally {"limit": N} to do just the
            # next N so the browser can drive it scene-by-scene with live
            # progress instead of one multi-minute request). Search -> LLM
            # pre-rank + score against the exact script segment -> select at
            # or above the bar; every decision written to sync_report.json.
            refresh_llm_keys()  # pick up a key added since the picker launched
            if not STATE.get("llm_keys"):
                return self._send_json(
                    {"error": "Auto-match needs a Gemini API key -- put one in tools/gemini_key.txt "
                              "(one per line for several), then reload this page."},
                    status=400,
                )
            # When on, any scene/shot no stock clip can match is filled with a
            # generated house-style illustration instead of left empty (see
            # _finish_with_ai_fallback / _automatch_shots).
            STATE["ai_fallback"] = bool(body.get("ai_fallback"))
            # `llm_down` latches when Gemini's judge runs out of quota mid-run
            # so the rest of the run fills scenes by keyword overlap instead of
            # stopping (see _match_with_repair). Clear it at the start of a
            # fresh run -- the per-scene button, or the first request of an
            # "all" run (no `exclude` history yet) -- so the judge is retried.
            if not body.get("all") or not body.get("exclude"):
                STATE["llm_down"] = None
            min_quality = body.get("min_quality")
            min_quality = min_quality if min_quality in QUALITY_TIERS else None
            try:
                threshold = max(0, min(100, int(body.get("threshold", AUTOMATCH_THRESHOLD))))
            except (TypeError, ValueError):
                threshold = AUTOMATCH_THRESHOLD
            soft_floor = None
            if body.get("soft_floor") is not None:
                try:
                    soft_floor = max(0, min(threshold, int(body["soft_floor"])))
                except (TypeError, ValueError):
                    soft_floor = None
            try:
                limit = max(0, int(body.get("limit", 0)))
            except (TypeError, ValueError):
                limit = 0
            if body.get("all"):
                # `exclude` is how the scene-by-scene driver skips scenes it
                # already tried this run (a FAIL stays unpicked, so without
                # this the "next unpicked scene" would be the same one again).
                exclude = set()
                if isinstance(body.get("exclude"), list):
                    for x in body["exclude"]:
                        try:
                            exclude.add(int(x))
                        except (TypeError, ValueError):
                            pass
                selections = load_selections()

                def _needs_match(s):
                    if s["index"] in exclude:
                        return False
                    sel = selections.get(str(s["index"]))
                    if sel is None:
                        return True
                    # A keyword-overlap pick made on an earlier run when the
                    # Gemini judge was quota-blocked: re-target it so a later
                    # run (quota reset) upgrades it to a judged pick. Skip this
                    # while the judge is known-down this run -- re-doing it
                    # heuristically would change nothing.
                    return bool(sel.get("heuristic")) and not STATE.get("llm_down")

                targets = [s for s in STATE["scenes"] if _needs_match(s)]
                if limit:
                    targets = targets[:limit]
            else:
                idx = int(body["index"])
                targets = [s for s in STATE["scenes"] if s["index"] == idx]
                if not targets:
                    return self._send_json({"error": "Unknown scene."}, status=400)
            entries = []
            paused = None
            dead_streak = 0  # consecutive scenes nothing could be picked for
            for scene in targets:
                try:
                    e = automatch_scene(
                        scene, min_quality=min_quality, threshold=threshold, soft_floor=soft_floor,
                    )
                except AutomatchUnavailable as ex:
                    # Shouldn't reach here now -- _match_with_repair catches the
                    # judge going down and falls back to keyword overlap. Kept
                    # as a backstop: latch it and let the remaining scenes take
                    # the heuristic path rather than stopping the whole run.
                    STATE["llm_down"] = str(ex)
                    e = {
                        "scene_index": scene["index"], "script": scene["text"],
                        "status": "FAIL", "soft": False, "score": None, "chosen": None,
                        "candidates": [],
                        "failure_reasons": [f"Gemini unavailable: {ex}"],
                    }
                except Exception as ex:
                    e = {
                        "scene_index": scene["index"],
                        "script": scene["text"],
                        "status": "FAIL",
                        "soft": False,
                        "score": None,
                        "chosen": None,
                        "candidates": [],
                        "failure_reasons": [f"Auto-match crashed: {ex}"],
                    }
                entries.append(e)
                # When the judge is down AND scene after scene turns up nothing
                # to even keyword-match (no pool at all -- the network is down,
                # not just Gemini), stop rather than churn through hundreds of
                # empty searches. A single such scene is normal; a run of them
                # isn't.
                if STATE.get("llm_down") and e["status"] != "PASS" and not e.get("candidates"):
                    dead_streak += 1
                    if dead_streak >= 8:
                        paused = ("Gemini scoring is unavailable and the stock providers "
                                  "returned nothing for several scenes in a row -- check the "
                                  "network, then re-run to continue where this left off.")
                        break
                else:
                    dead_streak = 0
            report_path = save_sync_report(entries)
            passed = sum(1 for e in entries if e["status"] == "PASS")
            soft = sum(1 for e in entries if e.get("soft"))
            heuristic_n = sum(1 for e in entries if e.get("heuristic"))
            ai_filled = sum(1 for e in entries if e.get("ai_generated"))
            after = load_selections()

            def _still_pending(s):
                sel = after.get(str(s["index"]))
                if sel is None:
                    return True
                return bool(sel.get("heuristic")) and not STATE.get("llm_down")

            remaining = sum(1 for s in STATE["scenes"] if _still_pending(s))
            return self._send_json({
                "ok": True,
                "paused": paused,
                "llm_down": bool(STATE.get("llm_down")),
                "matched": passed,
                "soft": soft,
                "heuristic": heuristic_n,
                "ai_filled": ai_filled,
                "failed": len(entries) - passed,
                "remaining": remaining,
                "entries": entries,
                "report": report_path.name,
            })

        if route == "/api/clear":
            scene_index = int(body["index"])
            with STATE["lock"]:
                remove_clips_for_scene(scene_index)
                selections = load_selections()
                selections.pop(str(scene_index), None)
                save_selections(selections)
            return self._send_json({"ok": True})

        if route == "/api/review-mark":
            # The review page ticking/unticking a scene as "checked". Stored
            # on the selection itself so a 300-scene review survives a page
            # reload or a picker restart. `index: "*"` clears every tick.
            want = bool(body.get("reviewed"))
            with STATE["lock"]:
                selections = load_selections()
                if body.get("index") == "*":
                    for v in selections.values():
                        v.pop("reviewed", None)
                    save_selections(selections)
                    return self._send_json({"ok": True, "cleared": True})
                key = str(int(body["index"]))
                if key not in selections:
                    return self._send_json({"error": "That scene has no clip picked yet."}, status=400)
                if want:
                    selections[key]["reviewed"] = True
                else:
                    selections[key].pop("reviewed", None)
                save_selections(selections)
                reviewed = sum(1 for v in selections.values() if v.get("reviewed"))
            return self._send_json({"ok": True, "reviewed": reviewed, "total": len(STATE["scenes"])})

        if route == "/api/finish":
            # Every scene has a clip and the browser confirmed it's time to
            # render. Reply first, then shut the server down from a separate
            # thread -- calling shutdown() from inside the request it's
            # answering would deadlock (serve_forever() can't stop while
            # dispatching this very request).
            STATE["finish_requested"] = True
            self._send_json({"ok": True})
            threading.Thread(target=STATE["server"].shutdown, daemon=True).start()
            return

        if route == "/api/clear-all":
            with STATE["lock"]:
                selections = load_selections()
                cleared = len(selections)
                # Delete every scene's downloaded clip, then wipe selections.
                # Iterate the known picks (their keys are scene indices) rather
                # than blanket-deleting the folder, so anything you dropped in
                # clips/ by hand is left alone.
                for key in list(selections.keys()):
                    try:
                        remove_clips_for_scene(int(key))
                    except (ValueError, TypeError):
                        pass
                save_selections({})
            return self._send_json({"ok": True, "cleared": cleared})

        self.send_error(404)


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------

PAGE_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Pick Clips</title>
<style>
  :root {
    --bg:#12141a; --panel:#1a1d26; --panel2:#222634; --line:#2e3342;
    --text:#e8eaf0; --dim:#9aa1b4; --accent:#f5c542; --ok:#3ecf8e;
  }
  /* Light theme -- activated by ?theme=light (the dashboard passes its own
     mode when embedding this picker so both always match). */
  body.light {
    --bg:#f3f5f8; --panel:#ffffff; --panel2:#eef1f6; --line:#dfe4ec;
    --text:#16202c; --dim:#5b6a7e; --accent:#eab308; --ok:#16a34a;
  }
  body.light .scene-item { border-bottom-color:rgba(0,0,0,.05); }
  body.light .scene-item.needs-clip { background:rgba(220,53,69,.10); }
  body.light .scene-item.needs-clip:hover { background:rgba(220,53,69,.18); }
  body.light .scene-item.needs-clip.active { background:rgba(220,53,69,.22); box-shadow:inset 3px 0 0 #d64545; }
  body.light .scene-item.needs-clip .warn { color:#b91c1c; }
  body.light #flagged { background:rgba(220,53,69,.08); color:#b91c1c; }
  body.light .req { background:#ecfdf3; color:#11603a; border-color:#a6e6c3; }
  body.light .excl { background:#fdf0f0; color:#991f1f; border-color:#f2bcbc; }
  body.light .qchip { color:#1d4ed8; }
  body.light .matchnote { color:#11603a; }
  body.light .matchnote.fail { color:#991f1f; }
  body.light .card .info .short { color:#b91c1c; }
  body.light #status.err { color:#b91c1c; }
  body.light #resetall:hover:not(:disabled), body.light #clearall:hover:not(:disabled) { border-color:#d64545; color:#b91c1c; }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.5 system-ui,Segoe UI,sans-serif; background:var(--bg); color:var(--text); }
  #app { display:flex; height:100vh; }

  #sidebar { width:250px; flex:none; background:var(--panel); border-right:1px solid var(--line);
             display:flex; flex-direction:column; }
  #sidebar h1 { font-size:13px; text-transform:uppercase; letter-spacing:.08em; color:var(--dim);
                margin:0; padding:16px; border-bottom:1px solid var(--line); }
  #progress { padding:12px 16px; border-bottom:1px solid var(--line); font-size:13px; }
  #bar { height:5px; background:var(--panel2); border-radius:3px; margin-top:8px; overflow:hidden; }
  #bar > div { height:100%; background:var(--ok); width:0; transition:width .2s; }
  #resetall { margin-top:10px; width:100%; font-size:12px; background:var(--panel2); color:var(--dim); }
  #resetall:hover:not(:disabled) { border-color:#ff8f8f; color:#ffb4b4; }
  #resetall:disabled { opacity:.4; cursor:default; }
  #scenelist { overflow-y:auto; flex:1; }
  .scene-item { padding:8px 16px; cursor:pointer; border-bottom:1px solid rgba(255,255,255,.03);
                display:flex; gap:8px; align-items:baseline; font-size:13px; }
  .scene-item:hover { background:var(--panel2); }
  .scene-item.active { background:var(--panel2); box-shadow:inset 3px 0 0 var(--accent); }
  .scene-item .num { color:var(--dim); font-variant-numeric:tabular-nums; font-size:12px; min-width:30px; }
  .scene-item .snip { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--dim); }
  .scene-item.done .snip { color:var(--text); }
  .scene-item .tick { color:var(--ok); font-weight:700; }
  /* Scenes step 3 flagged as missing a clip (never picked, or the downloaded
     file is gone) -- red background makes them impossible to miss, and wins
     over .active/.done since those don't matter until this is fixed. */
  .scene-item.needs-clip { background:rgba(220,53,69,.35); }
  .scene-item.needs-clip:hover { background:rgba(220,53,69,.5); }
  .scene-item.needs-clip.active { background:rgba(220,53,69,.55); box-shadow:inset 3px 0 0 #ff8f8f; }
  .scene-item.needs-clip .warn { color:#ff8f8f; font-weight:700; }
  #flagged { display:none; padding:10px 16px; border-bottom:1px solid var(--line);
             background:rgba(220,53,69,.2); color:#ff8f8f; font-size:12px; }

  #main { flex:1; overflow-y:auto; padding:24px 28px 60px; }
  .meta { color:var(--dim); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }
  /* Right-to-left narration (Urdu). Nastaliq needs the extra line-height --
     its letters stack diagonally and collide at normal leading. */
  body.rtl-script .scene-item .snip {
    direction:rtl; text-align:right; font-family:"Jameel Noori Nastaleeq",serif;
    font-size:16px; line-height:1.9; }
  body.rtl-script #scenewords, body.rtl-script #tagbox { direction:rtl; }
  body.rtl-script #scenescript .ssbody, body.rtl-script #fullscript .fsrow {
    direction:rtl; text-align:right;
    font-family:"Jameel Noori Nastaleeq",serif; font-size:17px; line-height:2; }
  body.rtl-script .wordchip, body.rtl-script .tag { font-family:"Jameel Noori Nastaleeq",serif; font-size:16px; }

  /* Sentence-as-tags + search-tags UI */
  .wl { margin:16px 0 6px; display:flex; align-items:center; gap:10px; }
  #scenewords { display:flex; flex-wrap:wrap; gap:6px; max-width:900px; }
  .wordchip { background:var(--panel); border:1px solid var(--line); color:var(--dim);
              padding:4px 10px; border-radius:14px; cursor:pointer; font-size:13px; user-select:none; }
  .wordchip:hover { border-color:var(--accent); color:var(--text); }
  .wordchip.added { background:var(--accent); color:#1a1200; border-color:var(--accent); }
  #tagbox { display:flex; flex-wrap:wrap; gap:6px; align-items:center; background:var(--panel);
            border:1px solid var(--line); border-radius:8px; padding:7px 9px; max-width:900px; margin-bottom:12px; }
  #tagbox:focus-within { border-color:var(--accent); }
  #tags { display:contents; }
  .tag { display:inline-flex; align-items:center; gap:6px; background:var(--accent); color:#1a1200;
         border-radius:13px; padding:3px 4px 3px 11px; font-size:13px; font-weight:600; }
  .tag button { background:rgba(0,0,0,.18); color:#1a1200; border:none; border-radius:50%;
                width:18px; height:18px; line-height:16px; padding:0; cursor:pointer; font-size:14px; }
  .tag button:hover { background:rgba(0,0,0,.4); }
  #tagbox input { flex:1; min-width:150px; background:transparent; border:none; color:var(--text);
                  font-size:14px; padding:4px; }
  #tagbox input:focus { outline:none; }
  #clearall { font-size:12px; padding:4px 12px; }
  #clearall:hover:not(:disabled) { border-color:#ff8f8f; color:#ffb4b4; }
  #clearall:disabled { opacity:.4; cursor:default; }

  #sourcetabs { display:flex; gap:6px; margin-bottom:12px; flex-wrap:wrap; }
  .srctab { font-size:12px; padding:7px 12px; }
  .srctab.active { background:var(--accent); color:#1a1200; border-color:var(--accent); font-weight:600; }

  #localpanel { display:none; background:var(--panel); border:1px solid var(--line); border-radius:10px;
                padding:16px; margin-bottom:16px; max-width:520px; }
  #localpanel.on { display:block; }
  #localhint { font-size:12px; color:var(--accent); margin:8px 0 4px; display:none; }
  #localpanel.multi #localhint { display:block; }
  /* Cap the preview so a tall (portrait phone) image can never shove the
     "Use / Add" button below the fold -- that made multi-clip local uploads
     look broken because the button to add the 2nd, 3rd... file was off-screen. */
  #localpreviewwrap { margin:10px 0 0; max-width:360px; }
  #localpreviewwrap img { max-width:100%; max-height:200px; width:auto; height:auto;
                          border-radius:8px; display:block; background:#000; }

  #aipanel { display:none; background:var(--panel); border:1px solid var(--line); border-radius:10px;
             padding:16px; margin-bottom:16px; max-width:680px; }
  #aipanel.on { display:block; }
  #aipanel textarea { width:100%; min-height:66px; margin-top:8px; background:var(--panel2);
                      border:1px solid var(--line); color:var(--text); border-radius:8px;
                      padding:8px 10px; font:13px/1.5 system-ui,Segoe UI,sans-serif; resize:vertical; }
  #aipanel textarea:focus { outline:none; border-color:var(--accent); }
  #aipanel .airow { display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-top:10px; }
  #aipanel label { font-size:12px; display:flex; align-items:center; gap:6px; }
  #aipanel input[type=number] { width:56px; background:var(--panel2); border:1px solid var(--line);
                                color:var(--text); padding:6px 8px; border-radius:6px; font-size:13px; }
  #aifallbackwrap { display:none; margin-top:6px; font-size:11.5px; color:var(--dim);
                    align-items:flex-start; gap:6px; line-height:1.35; cursor:pointer; }

  #controls { display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-bottom:8px; }
  input[type=text] { flex:1; min-width:260px; background:var(--panel); border:1px solid var(--line);
                     color:var(--text); padding:10px 12px; border-radius:8px; font-size:14px; }
  input[type=text]:focus { outline:none; border-color:var(--accent); }
  button { background:var(--panel2); color:var(--text); border:1px solid var(--line);
           padding:9px 14px; border-radius:8px; cursor:pointer; font-size:13px; }
  button:hover:not(:disabled) { border-color:var(--accent); }
  button:disabled { opacity:.4; cursor:default; }
  button.primary { background:var(--accent); color:#1a1200; border-color:var(--accent); font-weight:600; }
  #opts { color:var(--dim); font-size:12px; margin-bottom:16px; display:flex; gap:16px; align-items:center; }
  #opts select { background:var(--panel2); color:var(--text); border:1px solid var(--line);
                 border-radius:6px; padding:3px 6px; font-size:12px; margin-left:4px; }

  #bulkbar { display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom:14px;
             background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px 12px; }
  #bulkbar label { display:flex; align-items:center; gap:6px; font-size:13px; cursor:pointer; }
  #bulkrange { display:none; align-items:center; gap:8px; }
  #bulkrange.on { display:flex; }
  #bulkrange input[type=number] { width:64px; background:var(--panel2); border:1px solid var(--line);
                                   color:var(--text); padding:6px 8px; border-radius:6px; font-size:13px; }
  #bulkrange input[type=number]:focus { outline:none; border-color:var(--accent); }
  #bulkrange button { font-size:12px; padding:6px 10px; }

  #multibar { display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom:14px;
              background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px 12px; }
  #multibar label { display:flex; align-items:center; gap:6px; font-size:13px; cursor:pointer; }
  #multistuff { display:none; align-items:center; gap:10px; flex-wrap:wrap; width:100%; }
  #multistuff.on { display:flex; }
  #multipicks { display:flex; flex-wrap:wrap; gap:6px; }
  .multichip { display:inline-flex; align-items:center; gap:6px; background:var(--accent); color:#1a1200;
               border-radius:13px; padding:3px 4px 3px 11px; font-size:12px; font-weight:600; }
  .multichip button { background:rgba(0,0,0,.18); color:#1a1200; border:none; border-radius:50%;
                       width:18px; height:18px; line-height:16px; padding:0; cursor:pointer; font-size:14px; }
  .multichip button:hover { background:rgba(0,0,0,.4); }
  #multisave { font-size:12px; padding:6px 12px; }
  #multisplit { font-size:12px; color:var(--dim); }
  .card.picked { border-color:var(--accent); box-shadow:0 0 0 1px var(--accent); }

  #grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:16px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:10px; overflow:hidden;
          display:flex; flex-direction:column; }
  .card.chosen { border-color:var(--ok); box-shadow:0 0 0 1px var(--ok); }
  .card video, .card img.thumb { width:100%; aspect-ratio:16/9; object-fit:cover; background:#000; display:block; }
  .card video { cursor:pointer; }
  .card .info { padding:8px 10px; font-size:12px; color:var(--dim); display:flex;
                justify-content:space-between; gap:8px; }
  .card .info .short { color:#ffb4b4; }
  .card button { margin:0 10px 10px; }

  #status { margin:14px 0; color:var(--dim); font-size:13px; min-height:20px; }
  #status.err { color:#ff8f8f; }
  #pager { display:flex; gap:10px; align-items:center; margin:22px 0 10px; }
  #done { margin-top:30px; padding:16px; background:var(--panel); border:1px solid var(--ok);
          border-radius:10px; display:none; }
  code { background:var(--panel2); padding:2px 6px; border-radius:4px; font-size:13px; }
  /* Semantic brief (from timeline-aware scenes.json) */
  #semanticbrief { margin:10px 0 4px; padding:10px 12px; background:var(--panel);
    border:1px solid var(--line); border-radius:10px; font-size:13px; }
  #semanticbrief .briefline { color:var(--dim); margin-bottom:6px; }
  #semanticbrief b { color:var(--text); font-weight:600; }
  .req, .excl { display:inline-block; margin:2px 4px 2px 0; padding:2px 8px; border-radius:999px;
    font-size:12px; border:1px solid transparent; }
  .req { background:rgba(88,214,141,.12); color:#8ce8b0; border-color:rgba(88,214,141,.35); }
  .excl { background:rgba(255,107,107,.10); color:#ff9c9c; border-color:rgba(255,107,107,.30); }
  .qchip { display:inline-block; margin:2px 6px 2px 0; padding:2px 10px; border-radius:999px;
    background:var(--panel2); border:1px solid var(--line); color:var(--accent2, #5aa9ff);
    font-size:12px; cursor:pointer; }
  .qchip:hover { border-color:var(--accent); }
  .shotrow { margin:3px 0 0 8px; color:var(--dim); font-size:12px; }
  .shotrow .t { color:var(--text); }
  #automatchbtn.busy, #automatchall.busy, #aigen.busy { opacity:.5; pointer-events:none; }
  #automatchall { margin-top:8px; width:100%; font-size:12px; background:var(--panel2); color:var(--dim); }
  #automatchall:hover:not(:disabled) { border-color:var(--accent); color:var(--text); }
  .matchnote { margin-top:6px; font-size:12px; color:#8ce8b0; }
  .matchnote.fail { color:#ff9c9c; }

  /* Readable narration for the current scene -- so you can see the exact
     words a clip has to sit under while you're choosing it. */
  #scenescript { margin:12px 0 4px; border:1px solid var(--line); border-radius:10px;
    background:var(--panel); overflow:hidden; }
  #scenescript .sshead { display:flex; align-items:center; justify-content:space-between;
    gap:10px; padding:8px 12px; background:var(--panel2); font-size:12px;
    text-transform:uppercase; letter-spacing:.08em; color:var(--dim); }
  #scenescript .ssbody { padding:12px 14px; font-size:15.5px; line-height:1.75; color:var(--text); }
  #scenescript .now { display:block; margin:2px 0; }
  #scenescript .ctx { display:block; margin:4px 0; color:var(--dim); font-size:13px;
    line-height:1.55; cursor:pointer; }
  #scenescript .ctx:hover { color:var(--text); }
  #scenescript .shotseg { display:block; margin:8px 0; padding-left:10px;
    border-left:2px solid var(--line); }
  #scenescript .shotseg .lbl { display:block; font-size:11px; text-transform:uppercase;
    letter-spacing:.08em; color:var(--accent); margin-bottom:2px; }
  #fulltoggle { font-size:11px; padding:3px 10px; text-transform:none; letter-spacing:0; }
  #fullscript { margin:8px 0 4px; border:1px solid var(--line); border-radius:10px;
    background:var(--panel); max-height:44vh; overflow-y:auto; }
  #fullscript .fsrow { display:flex; gap:10px; padding:8px 12px; border-bottom:1px solid var(--line);
    cursor:pointer; font-size:13.5px; line-height:1.6; }
  #fullscript .fsrow:last-child { border-bottom:none; }
  #fullscript .fsrow:hover { background:var(--panel2); }
  #fullscript .fsrow .n { flex:none; color:var(--dim); font-variant-numeric:tabular-nums; }
  #fullscript .fsrow.cur { background:rgba(245,197,66,.12); }
  #fullscript .fsrow.cur .n { color:var(--accent); font-weight:700; }
  #fullscript .fsrow.done .n::after { content:" \2713"; color:var(--ok); }

  #reviewall { margin-top:8px; width:100%; font-size:12px; background:var(--panel2); color:var(--dim); }
  #reviewall:hover:not(:disabled) { border-color:var(--accent); color:var(--text); }

  /* ---------- Review picked clips (full-screen overlay) ---------- */
  #review { position:fixed; inset:0; z-index:50; background:var(--bg); color:var(--text);
    overflow-y:auto; display:none; }
  #review.on { display:block; }
  #review .rvhead { position:sticky; top:0; z-index:2; display:flex; align-items:center; gap:14px;
    flex-wrap:wrap; padding:12px 24px; background:var(--panel); border-bottom:1px solid var(--line); }
  #review .rvhead h2 { margin:0; font-size:15px; }
  #review .rvhead .sp { flex:1; }
  #rvcount { font-size:12px; color:var(--dim); font-variant-numeric:tabular-nums; }
  #rvbar { height:5px; width:160px; background:var(--panel2); border-radius:3px; overflow:hidden; }
  #rvbar > div { height:100%; background:var(--ok); width:0; transition:width .2s; }
  #reviewlist { max-width:1180px; margin:0 auto; padding:16px 24px 90px; }
  .rev-row { display:grid; grid-template-columns:1fr 440px; gap:22px; padding:18px 4px;
    border-bottom:1px solid var(--line); }
  .rev-row.flag { box-shadow:inset 3px 0 0 var(--accent); padding-left:14px; }
  .rev-row.reviewed { opacity:.5; }
  .rev-row.hide { display:none; }
  .rev-num { font-size:12px; color:var(--dim); text-transform:uppercase; letter-spacing:.06em;
    font-variant-numeric:tabular-nums; }
  .rev-script { font-size:15px; line-height:1.7; margin:8px 0 4px; }
  .rev-row .shotseg { display:block; margin:7px 0; padding-left:10px; border-left:2px solid var(--line); }
  .rev-row .shotseg .lbl { display:block; font-size:11px; text-transform:uppercase; letter-spacing:.06em;
    color:var(--accent); margin-bottom:2px; }
  .rev-chip { display:inline-block; padding:2px 8px; border-radius:999px; font-size:10.5px; font-weight:700;
    text-transform:uppercase; letter-spacing:.05em; margin:3px 6px 3px 0; }
  .rev-chip.ok { background:rgba(88,214,141,.14); color:#8ce8b0; }
  .rev-chip.warn { background:rgba(245,197,66,.16); color:var(--accent); }
  .rev-chip.bad { background:rgba(255,107,107,.14); color:#ff9c9c; }
  .rev-note { font-size:12px; color:var(--dim); margin-top:6px; line-height:1.5; }
  .rev-src { font-size:11.5px; color:var(--dim); margin-top:4px; }
  .rev-src a { color:inherit; }
  .rev-media { width:100%; aspect-ratio:16/9; background:#000; border-radius:10px; display:block;
    object-fit:contain; }
  .rev-media.img { object-fit:cover; }
  .rev-multi { display:flex; gap:8px; flex-wrap:wrap; }
  .rev-multi figure { margin:0; flex:1 1 190px; }
  .rev-multi figcaption { font-size:11px; color:var(--dim); margin-top:3px; overflow:hidden;
    text-overflow:ellipsis; white-space:nowrap; }
  .rev-none { font-size:13px; font-weight:600; color:#ff9c9c; padding:18px; border:1px dashed var(--line);
    border-radius:10px; text-align:center; }
  .rev-actions { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-top:10px; }
  .rev-actions label { font-size:12px; display:flex; align-items:center; gap:5px; cursor:pointer; color:var(--dim); }
  .rev-actions button { font-size:12px; padding:6px 11px; }
  body.light .rev-chip.ok { background:#ecfdf3; color:#11603a; }
  body.light .rev-chip.warn { background:#fef6e0; color:#8a5a00; }
  body.light .rev-chip.bad { background:#fdf0f0; color:#991f1f; }
  body.light .rev-none { color:#991f1f; }
  body.rtl-script .rev-script { direction:rtl; text-align:right;
    font-family:"Jameel Noori Nastaleeq",serif; font-size:17px; line-height:2; }

  /* ---------- responsive: tablets & phones ---------- */
  @media (max-width: 820px) {
    #app { flex-direction:column; height:auto; min-height:100vh; }
    #sidebar { width:100%; max-height:32vh; border-right:none; border-bottom:1px solid var(--line); }
    #main { padding:12px 12px 50px; }
    #grid { grid-template-columns:repeat(auto-fill,minmax(160px,1fr)); gap:10px; }
    #controls { flex-wrap:wrap; }
    #sourcetabs { overflow-x:auto; white-space:nowrap; display:flex; }
    .srctab { flex:none; }
    .rev-row { grid-template-columns:1fr; gap:12px; }
    #reviewlist { padding:12px 12px 80px; }
    #review .rvhead { padding:10px 12px; }
  }
</style></head><body>
<div id="app">
  <div id="sidebar">
    <h1>Scenes</h1>
    <div id="progress"><span id="ptext">-</span><div id="bar"><div></div></div>
      <button id="resetall" title="Delete every downloaded clip and clear all selections">Reset all clips</button>
      <button id="automatchall" style="display:none" title="Go through every scene with no clip yet, one at a time (the sidebar fills in live): search Pexels/Pixabay/Coverr, LLM-score the best candidates against that scene's exact narration, with query-repair rounds and a still-photo fallback. A clip at or above the strictness bar is a clean match; a weaker one that still clears the soft floor fills the scene and is flagged for review so nothing is left empty. Click again to stop -- progress is saved. Every decision is in sync_report.json.">&#10024; Auto-match all unpicked</button>
      <select id="strictness" style="display:none; margin-top:6px; width:100%; font-size:11.5px"
        title="How exact an auto-matched clip must be. Strict/Exact only accept descriptions that explicitly state the subject AND action -- fewer scenes auto-fill, but what fills is right.">
        <option value="60" selected>Match strictness: balanced</option>
        <option value="75">Match strictness: strict (explicit subject+action)</option>
        <option value="85">Match strictness: exact only</option>
        <option value="50">Match strictness: relaxed</option>
      </select>
      <label id="aifallbackwrap" title="After stock search + the still-photo round fail, generate a warm house-style AI illustration (Pollinations / FLUX, free) for that scene or shot instead of leaving it empty. Flagged for review in sync_report.json.">
        <input type="checkbox" id="aifallback"> Fill scenes stock can't match with an AI illustration</label>
      <button id="reviewall" style="display:none" title="Open a side-by-side review: every scene's narration next to the clip picked for it. Catch any mismatch and replace it before the render. Opens automatically once 'Auto-match all' fills every scene.">&#128269; Review picked clips</button>
    </div>
    <div id="flagged"></div>
    <div id="scenelist"></div>
  </div>
  <div id="main">
    <div class="meta" id="scenemeta">Loading...</div>
    <div id="semanticbrief" style="display:none"></div>
    <div id="scenescript"></div>
    <div id="fullscript" style="display:none"></div>

    <div id="sourcetabs">
      <button class="srctab active" data-src="pexels-video">Pexels video</button>
      <button class="srctab" data-src="pexels-photo">Pexels photo</button>
      <button class="srctab" data-src="pixabay-video">Pixabay video</button>
      <button class="srctab" data-src="pixabay-photo">Pixabay photo</button>
      <button class="srctab" data-src="coverr-video">Coverr video</button>
      <button class="srctab" data-src="ai-illustration">AI illustration</button>
      <button class="srctab" data-src="local">Local file</button>
    </div>

    <div id="wlwords" class="wl"><span class="meta">Search helper &mdash; click any word from the narration to add or remove it as a search tag</span></div>
    <div id="scenewords"></div>
    <div id="wltags" class="wl"><span class="meta">Search tags</span><button id="clearall" title="Remove every search tag">Clear all</button></div>
    <div id="tagbox"><span id="tags"></span><input type="text" id="q" placeholder="type a keyword, press Enter to add..."></div>
    <div id="controls">
      <button id="searchbtn" class="primary">Search</button>
      <button id="automatchbtn" style="display:none" title="Search all providers, score every candidate against this scene's exact narration with the LLM, and pick the best automatically">&#10024; Auto-match this scene</button>
      <button id="prevscene">&larr; Prev scene</button>
      <button id="nextscene">Next scene &rarr;</button>
    </div>
    <div id="bulkbar">
      <label><input type="checkbox" id="bulkmode"> Apply the clip/image I pick to a range of scenes</label>
      <span id="bulkrange">
        <span class="meta">from</span> <input type="number" id="bulkfrom" min="1">
        <span class="meta">to</span> <input type="number" id="bulkto" min="1">
        <button id="bulkallremaining" title="Set the range to this scene through the last scene">All remaining</button>
      </span>
    </div>
    <div id="multibar">
      <label><input type="checkbox" id="multimode"> Use more than one clip for this scene (splits its time between them)</label>
      <div id="multistuff">
        <div id="multipicks"></div>
        <span id="multisplit" class="meta"></span>
        <button id="multisave" class="primary" disabled>Save picks</button>
      </div>
    </div>

    <div id="localpanel">
      <div class="meta">Pick an image file from this computer to use for this scene (or the range above).</div>
      <div id="localhint">Multi-clip mode is on &mdash; pick a file and click <b>Add image to picks</b>, repeat for each image you want, then <b>Save picks</b> above.</div>
      <p><input type="file" id="localfile" accept="image/*"></p>
      <button id="uselocalbtn" class="primary" disabled>Use this image</button>
      <div id="localpreviewwrap"><img id="localpreview" style="display:none"></div>
    </div>

    <div id="aipanel">
      <div class="meta">Generate a house-style illustration for this scene with AI &mdash; Pollinations.AI / FLUX,
        free and no key needed. Edit what the scene should show below; the warm hand-drawn
        storybook look is added automatically. Each result is a still image, rendered as a slow
        Ken&nbsp;Burns zoom in step&nbsp;3.</div>
      <textarea id="aiprompt" placeholder="what this scene should show (e.g. a lone traveller walking a mountain trail at sunrise, seen from behind)"></textarea>
      <label style="margin-top:6px"><input type="checkbox" id="airaw"> use my text exactly &mdash; skip the house style</label>
      <div class="airow">
        <label>How many <input type="number" id="aicount" min="1" max="6" value="3"></label>
        <button id="aigen" class="primary">Generate illustrations</button>
        <button id="aistop" style="display:none">Stop</button>
        <span class="meta" id="aihint"></span>
      </div>
    </div>

    <div id="opts">
      <label id="longenoughwrap"><input type="checkbox" id="longenough" checked> Only clips long enough to cover the scene</label>
      <label id="qualitywrap">Minimum quality:
        <select id="minquality">
          <option value="">Any</option>
          <option value="1080p">1080p and up</option>
          <option value="2k">2K and up</option>
          <option value="4k">4K and up</option>
        </select>
      </label>
      <span id="rate"></span>
    </div>
    <div id="status"></div>
    <div id="grid"></div>
    <div id="pager">
      <span id="pageinfo" class="meta"></span>
    </div>
    <div id="loadmore" class="meta" style="display:none; text-align:center; padding:14px 0;">Loading more...</div>
    <div id="autorender" style="display:none; margin-top:30px; padding:16px; background:var(--panel);
         border:1px solid var(--ok); border-radius:10px;">
      <b>All scenes have a clip.</b>
      <span id="arcountdown"> Step 3 (render) starts automatically in <b id="arcount">60</b>s
        with default settings -- you don't need to do anything.</span>
      <span id="arcancelled" style="display:none"> Auto-render is paused. Click
        <b>Render now</b> whenever you're ready.</span>
      <span id="arcloud" style="display:none"> You're in a Codespace, so rendering
        here is off (it's slow and spends your free hours). Push the picks and let
        GitHub Actions render for free -- in the terminal:<br>
        <code id="arcloudcmd" style="display:block; margin:10px 0; white-space:pre-wrap;"></code>
        Then download <b>output.mp4</b> from the run's Artifacts box (Actions tab).
        <b>Render here anyway</b> still works if you really want to.</span>
      <div style="margin-top:12px; display:flex; gap:10px; flex-wrap:wrap;">
        <button id="arnow" class="primary">Render now</button>
        <button id="arwait">Not yet -- keep picking</button>
      </div>
    </div>
    <div id="done">
      <b>All scenes have a clip.</b> Render the final video with:<br><br>
      <code id="rendercmd"></code><br><br>
      Step 3 downloads every picked clip's real footage first (with a progress
      percentage in the terminal), then renders.
    </div>
    <div id="finished" style="display:none; margin-top:30px; padding:16px; background:var(--panel);
         border:1px solid var(--ok); border-radius:10px;">
      <b>Rendering started.</b> Watch the terminal you launched this from -- step 3 is
      downloading the picked clips and rendering there now. You can close this tab.
    </div>
  </div>
</div>

<div id="review">
  <div class="rvhead">
    <h2>Review picked clips</h2>
    <span id="rvcount">0 / 0 checked</span>
    <div id="rvbar"><div></div></div>
    <label style="font-size:12px; color:var(--dim); display:flex; align-items:center; gap:5px; cursor:pointer;">
      <input type="checkbox" id="rvfilter"> Only flagged &amp; unchecked</label>
    <span class="sp"></span>
    <button id="rvback">&larr; Back to picker</button>
    <button id="rvrender" class="primary" title="Every scene checked? Start step 3 (download + render) now.">Looks good &mdash; render</button>
  </div>
  <div id="reviewlist"></div>
</div>
<script>
// Follow the dashboard's theme when embedded there (?theme=light).
if (new URLSearchParams(location.search).get('theme') === 'light') document.body.classList.add('light');
let scenes = [], selections = {}, cur = 0, page = 1, hasNext = false, results = [], searchTags = [];
let loadingMore = false; // guards against two page-fetches firing from one scroll
let highlight = new Set(); // scene indices step 3 flagged as missing a clip
let bulkMode = false;
let multiMode = false; // picking several clips for the current scene, time split between them
let multiPicks = []; // assets queued for the current scene while multiMode is on
let multiUidCounter = 0; // gives each local-file add in multiPicks a distinct identity
// Every source tab maps to a provider (whose "source" tag selections.json
// and the server both use) + a media type ("video"/"photo" -- what the
// scene ends up rendered as) + the search endpoint for that combination.
// 'local' is not a real provider search, just an upload panel.
const SOURCES = {
  'pexels-video':  { provider: 'pexels',  media: 'video', endpoint: '/api/search' },
  'pexels-photo':  { provider: 'pexels',  media: 'photo', endpoint: '/api/search-photos' },
  'pixabay-video': { provider: 'pixabay', media: 'video', endpoint: '/api/search-pixabay' },
  'pixabay-photo': { provider: 'pixabay', media: 'photo', endpoint: '/api/search-pixabay-photos' },
  'coverr-video':  { provider: 'coverr',  media: 'video', endpoint: '/api/search-coverr' },
  'ai-illustration': { provider: 'ai',    media: 'photo', endpoint: '/api/ai-image' },
  'local':         { provider: 'local',   media: 'photo', endpoint: null },
};
const PROVIDER_LABELS = { pexels: 'Pexels', pixabay: 'Pixabay', coverr: 'Coverr', ai: 'AI illustration' };
let source = 'pexels-video'; // key into SOURCES
let localAsset = null;
let renderOffered = false; // ask at most once per page load
let autoRenderEnabled = true, projectPath = '';  // from /api/scenes
let aiStop = false;         // set by the AI panel's Stop button mid-generate
let aiWatermarked = true;   // no Pollinations token -> free tier adds a small watermark

const $ = id => document.getElementById(id);
const fmt = s => `${Math.floor(s/60)}:${String(Math.floor(s%60)).padStart(2,'0')}`;
const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
// A search tag is one word with its edge punctuation stripped (unicode-aware,
// so Urdu words survive). Case is kept for display but ignored when comparing.
const cleanWord = w => w.replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, '');

function sceneWordList() {
  const seen = new Set(), out = [];
  for (const raw of ((scenes[cur] && scenes[cur].text) || '').split(/\s+/)) {
    const w = cleanWord(raw);
    if (!w) continue;
    const k = w.toLowerCase();
    if (!seen.has(k)) { seen.add(k); out.push(w); }
  }
  return out;
}

function renderWords() {
  const active = new Set(searchTags.map(t => t.toLowerCase()));
  $('scenewords').innerHTML = sceneWordList().map(w =>
    `<span class="wordchip ${active.has(w.toLowerCase()) ? 'added' : ''}" data-w="${esc(w)}">${esc(w)}</span>`
  ).join('');
  $('scenewords').querySelectorAll('.wordchip').forEach(el =>
    el.onclick = () => toggleTag(el.dataset.w));
}

function renderTags() {
  $('tags').innerHTML = searchTags.map(t =>
    `<span class="tag">${esc(t)}<button title="Remove tag" data-t="${esc(t)}">&times;</button></span>`
  ).join('');
  $('tags').querySelectorAll('.tag button').forEach(b =>
    b.onclick = () => removeTag(b.dataset.t));
  $('clearall').disabled = searchTags.length === 0;
  renderWords();  // reflect which sentence words are currently in the search
}

// Set the whole tag list at once (deduped, order kept) and re-run the search.
function setTags(arr, doSearch = true) {
  const seen = new Set(), out = [];
  for (const t of arr) {
    const w = (t || '').trim();
    if (!w) continue;
    const k = w.toLowerCase();
    if (!seen.has(k)) { seen.add(k); out.push(w); }
  }
  searchTags = out;
  renderTags();
  if (doSearch) { page = 1; search(); }
}
function addTag(w) {
  w = cleanWord((w || '').trim());
  if (!w || searchTags.some(t => t.toLowerCase() === w.toLowerCase())) return;
  setTags([...searchTags, w]);
}
function removeTag(w) {
  const k = (w || '').toLowerCase();
  setTags(searchTags.filter(t => t.toLowerCase() !== k));
}
function toggleTag(w) {
  const k = cleanWord((w || '').trim()).toLowerCase();
  if (searchTags.some(t => t.toLowerCase() === k)) removeTag(w); else addTag(w);
}

let automatchAvailable = false;

function renderBrief(s) {
  const box = $('semanticbrief');
  const sem = s.semantic || null;
  const hasAny = sem || (s.visual_requirements||[]).length || (s.shots||[]).length || (s.queries||[]).length > 1;
  if (!hasAny) { box.style.display = 'none'; box.innerHTML = ''; return; }
  let html = '';
  if (sem) {
    const bits = [];
    if (sem.subject)  bits.push(`<b>${esc(sem.subject)}</b>`);
    if (sem.action)   bits.push(esc(sem.action));
    if (sem.object)   bits.push(esc(sem.object));
    if (sem.location) bits.push(`in ${esc(sem.location)}`);
    if (sem.time)     bits.push(`(${esc(sem.time)})`);
    if (bits.length) html += `<div class="briefline">Visual: ${bits.join(' &middot; ')}</div>`;
  }
  if ((s.visual_requirements||[]).length)
    html += `<div>${s.visual_requirements.map(r => `<span class="req">${esc(r)}</span>`).join('')}` +
            `${(s.visual_exclusions||[]).map(x => `<span class="excl">not: ${esc(x)}</span>`).join('')}</div>`;
  else if ((s.visual_exclusions||[]).length)
    html += `<div>${s.visual_exclusions.map(x => `<span class="excl">not: ${esc(x)}</span>`).join('')}</div>`;
  if ((s.queries||[]).length)
    html += `<div style="margin-top:5px">${s.queries.map(q => `<span class="qchip" data-q="${esc(q)}">&#128269; ${esc(q)}</span>`).join('')}</div>`;
  if ((s.shots||[]).length) {
    html += `<div class="briefline" style="margin-top:6px">This scene has ${s.shots.length} visual moments -- ` +
            `use "more than one clip" below to give each its own footage:</div>`;
    html += s.shots.map(sh =>
      `<div class="shotrow">${fmt(sh.start)}-${fmt(sh.end)} (${(sh.duration||0).toFixed(1)}s) ` +
      `<span class="t">"${esc(sh.text)}"</span>` +
      (sh.query ? ` <span class="qchip" data-q="${esc(sh.query)}">&#128269; ${esc(sh.query)}</span>` : '')
    ).join('');
  }
  const note = matchNotes[s.index];
  if (note) html += `<div class="matchnote ${note.ok?'':'fail'}">${esc(note.text)}</div>`;
  box.innerHTML = html;
  box.style.display = 'block';
  box.querySelectorAll('.qchip').forEach(el =>
    el.onclick = () => setTags(el.dataset.q.split(/\s+/)));
}

let fullScriptOpen = false;

// The exact narration for the current scene, as readable prose (not the
// deduped word chips below, which are only a search helper). Shows a little
// of the neighbouring scenes for flow, and breaks a multi-shot scene into
// its shots -- each shot is what one clip in a multi-clip pick covers.
function renderScript(s) {
  const box = $('scenescript');
  const prev = scenes[cur - 1], next = scenes[cur + 1];
  const tail = t => { const w = String(t || '').trim().split(/\s+/); return (w.length > 16 ? '… ' : '') + w.slice(-16).join(' '); };
  const head = t => { const w = String(t || '').trim().split(/\s+/); return w.slice(0, 16).join(' ') + (w.length > 16 ? ' …' : ''); };
  const shots = (s.shots || []).filter(sh => sh && sh.text);
  let body = '';
  if (prev) body += `<span class="ctx" data-go="${cur - 1}" title="Go to scene ${String(prev.index).padStart(3,'0')}">← ${esc(tail(prev.text))}</span>`;
  if (shots.length > 1) {
    body += shots.map((sh, k) =>
      `<span class="shotseg"><span class="lbl">Shot ${k + 1} &middot; ${fmt(sh.start)}–${fmt(sh.end)} &middot; ${(sh.duration || 0).toFixed(1)}s</span>${esc(sh.text)}</span>`
    ).join('');
  } else {
    body += `<span class="now">${esc(s.text || '(no narration text)')}</span>`;
  }
  if (next) body += `<span class="ctx" data-go="${cur + 1}" title="Go to scene ${String(next.index).padStart(3,'0')}">${esc(head(next.text))} →</span>`;
  box.innerHTML =
    `<div class="sshead"><span>Narration &middot; scene ${String(s.index).padStart(3,'0')} of ${scenes.length} ` +
    `&middot; ${s.duration.toFixed(1)}s on screen</span>` +
    `<button id="fulltoggle">${fullScriptOpen ? 'Hide full script' : 'Read full script'}</button></div>` +
    `<div class="ssbody">${body}</div>`;
  box.querySelectorAll('.ctx').forEach(el => el.onclick = () => go(+el.dataset.go));
  $('fulltoggle').onclick = toggleFullScript;
  if (fullScriptOpen) {
    $('fullscript').querySelectorAll('.fsrow').forEach((el, i) => {
      el.classList.toggle('cur', i === cur);
      el.classList.toggle('done', !!selections[scenes[i].index]);
    });
  }
}

// The whole script, every scene in order, current one highlighted. Click any
// row to jump straight to that scene.
function toggleFullScript() {
  fullScriptOpen = !fullScriptOpen;
  const box = $('fullscript');
  if (!fullScriptOpen) {
    box.style.display = 'none';
    renderScript(scenes[cur]);
    return;
  }
  box.innerHTML = scenes.map((s, i) =>
    `<div class="fsrow ${i === cur ? 'cur' : ''} ${selections[s.index] ? 'done' : ''}" data-go="${i}">` +
    `<span class="n">${String(s.index).padStart(3,'0')}</span>` +
    `<span class="tx">${esc(s.text || '')}</span></div>`
  ).join('');
  box.querySelectorAll('.fsrow').forEach(el => el.onclick = () => go(+el.dataset.go));
  box.style.display = 'block';
  renderScript(scenes[cur]);
  box.querySelector('.fsrow.cur')?.scrollIntoView({ block: 'center' });
}

let matchNotes = {}; // scene index -> {ok, text} from the last auto-match

async function refreshSelections() {
  const d = await (await fetch('/api/scenes')).json();
  selections = d.selections;
  renderSidebar();
}

function automatchNote(e) {
  if (e.status === 'PASS') {
    if (e.ai_generated) {
      return {ok:false, text:`No stock clip matched -- filled with a generated house-style ` +
        `illustration${e.score ? ` (best stock candidate was ${e.score}/100)` : ''}. ` +
        `Review it above, or open the "AI illustration" tab to regenerate / pick a clip instead.`};
    }
    if (e.heuristic) {
      return {ok:false, text:`Keyword-only match (Gemini judge was out of quota): ` +
        `"${(e.chosen&&e.chosen.desc)||''}" -- picked by tag overlap, score ${e.score||'?'}/100 ` +
        `is an estimate. Re-run "Auto-match all" once the quota resets and this scene is ` +
        `re-judged automatically, or pick a clip now to lock it in.`};
    }
    if (e.soft) {
      return {ok:false, text:`Soft match (score ${e.score}/100, under the strictness bar): ` +
        `"${(e.chosen&&e.chosen.desc)||''}" -- filled so the scene isn't left empty. ` +
        `Review it, or pick another clip to replace it.`};
    }
    const nq = (e.queries_tried||[]).length;
    const nc = (e.candidates_n != null) ? e.candidates_n : (e.candidates||[]).length;
    return {ok:true, text:`Auto-matched (score ${e.score}/100): "${(e.chosen&&e.chosen.desc)||''}" -- ` +
      `${nc} candidates scored across ${nq||'several'} search phrasings. ` +
      `Override it any time by picking another clip.`};
  }
  const why = (e.failure_reasons||[]).join(' ') || 'no candidate scored high enough.';
  const tried = (e.queries_tried||[]).length
    ? ` Tried: ${(e.queries_tried||[]).slice(0,6).map(q=>`"${q}"`).join(', ')}.` : '';
  return {ok:false, text:`Auto-match: no clip accepted -- ${why}${tried}`};
}

// ----------------------------------------------------------------------
// Review picked clips: a full-screen page listing every scene's narration
// next to a playable preview of the clip auto-match (or you) picked for it,
// so a mismatch can be spotted and replaced before the render. Opens
// automatically the moment "Auto-match all" fills the last scene; also
// reachable any time from the sidebar button.
// ----------------------------------------------------------------------
let reviewData = null;
let reviewFilter = false;
let returnToReview = false; // set by "Replace clip" so the next save reopens this

// <video> for hundreds of scenes would hammer the network on open -- give each
// a poster and no source, then attach the real src only as it nears the
// viewport.
const reviewIO = ('IntersectionObserver' in window) ? new IntersectionObserver((ents) => {
  for (const ent of ents) {
    if (!ent.isIntersecting) continue;
    const el = ent.target;
    if (el.dataset.src && !el.src) el.src = el.dataset.src;
    reviewIO.unobserve(el);
  }
}, { rootMargin: '600px 0px' }) : null;

function reviewFlagged(row) {
  if (!row.preview) return true;                     // no clip picked at all
  const sel = row.selection || {};
  if (sel.heuristic || sel.soft) return true;
  const r = row.report;
  if (r && (r.status !== 'PASS' || r.soft || r.heuristic || r.ai_generated)) return true;
  return false;
}

async function openReview() {
  $('status').className = ''; $('status').textContent = '';
  try {
    reviewData = await (await fetch('/api/review')).json();
  } catch (e) {
    $('status').className = 'err';
    $('status').textContent = 'Could not load the review page: ' + e;
    return;
  }
  renderReview();
  $('review').classList.add('on');
  document.body.style.overflow = 'hidden';
  $('review').scrollTop = 0;
}

function closeReview() {
  $('review').classList.remove('on');
  document.body.style.overflow = '';
}

function reviewChips(row) {
  const out = [];
  if (!row.preview) { out.push('<span class="rev-chip bad">no clip</span>'); return out.join(''); }
  const sel = row.selection || {}, r = row.report || null;
  if (r && r.ai_generated) out.push('<span class="rev-chip warn">AI illustration</span>');
  if (sel.heuristic || (r && r.heuristic)) out.push('<span class="rev-chip warn">keyword-only</span>');
  else if (sel.soft || (r && r.soft)) out.push('<span class="rev-chip warn">soft match</span>');
  else if (r && r.status === 'PASS') out.push(`<span class="rev-chip ok">auto-matched${r.score ? ' ' + r.score + '/100' : ''}</span>`);
  else if (r && r.status && r.status !== 'PASS') out.push('<span class="rev-chip bad">auto-match failed</span>');
  if (!r && row.preview) out.push('<span class="rev-chip ok">picked</span>');
  if (row.reviewed) out.push('<span class="rev-chip ok">checked</span>');
  return out.join('');
}

function reviewMediaHTML(row) {
  const p = row.preview;
  if (!p) return `<div class="rev-none">No clip picked for this scene &mdash; use "Replace clip".</div>`;
  const one = (it, cap) => {
    const poster = it.thumb ? ` poster="${esc(it.thumb)}"` : '';
    const media = (it.type === 'video')
      ? `<video class="rev-media" controls preload="none" playsinline${poster} data-src="${esc(it.url || '')}"></video>`
      : `<img class="rev-media img" loading="lazy" src="${esc(it.url || '')}" alt="">`;
    return cap
      ? `<figure>${media}<figcaption>${esc(cap)}</figcaption></figure>`
      : media;
  };
  if (p.type === 'multi') {
    const its = p.items || [];
    return `<div class="rev-multi">` + its.map((it, i) =>
      one(it, it.shot_text ? `Shot ${it.shot_index || i + 1}: ${it.shot_text}` : `Clip ${i + 1}`)
    ).join('') + `</div>`;
  }
  return one((p.items || [])[0] || {}, '');
}

function reviewScriptHTML(row) {
  const shots = (row.shots || []).filter(sh => sh && sh.text);
  if (shots.length > 1) {
    return shots.map((sh, k) =>
      `<span class="shotseg"><span class="lbl">Shot ${k + 1} &middot; ${(sh.duration || 0).toFixed(1)}s</span>${esc(sh.text)}</span>`
    ).join('');
  }
  return esc(row.text || '(no narration text)');
}

function renderReview() {
  const d = reviewData; if (!d) return;
  const total = d.total || (d.scenes || []).length;
  const reviewed = (d.scenes || []).filter(r => r.reviewed).length;
  $('rvcount').textContent = `${reviewed} / ${total} checked`;
  $('rvbar').firstElementChild.style.width = total ? (100 * reviewed / total) + '%' : '0';

  const rows = d.scenes || [];
  $('reviewlist').innerHTML = rows.map(row => {
    const flag = reviewFlagged(row);
    const hidden = reviewFilter && (row.reviewed || !flag);
    const note = row.report ? automatchNote(row.report) : null;
    const src = row.selection && row.selection.source
      ? `Source: ${esc(row.selection.source)}${row.selection.author ? ' &middot; ' + esc(row.selection.author) : ''}` +
        `${row.query ? ' &middot; search: "' + esc(row.query) + '"' : ''}`
      : (row.query ? `Search: "${esc(row.query)}"` : '');
    return `<div class="rev-row ${flag ? 'flag' : ''} ${row.reviewed ? 'reviewed' : ''} ${hidden ? 'hide' : ''}" data-i="${row.index}">
      <div class="rev-l">
        <div class="rev-num">Scene ${String(row.index).padStart(3, '0')} &middot; ${fmt(row.start || 0)}&ndash;${fmt(row.end || 0)} &middot; ${(row.duration || 0).toFixed(1)}s on screen</div>
        <div>${reviewChips(row)}</div>
        <div class="rev-script">${reviewScriptHTML(row)}</div>
        ${note ? `<div class="rev-note ${note.ok ? '' : 'fail'}">${esc(note.text)}</div>` : ''}
        ${src ? `<div class="rev-src">${src}</div>` : ''}
        <div class="rev-actions">
          <button class="rvreplace" data-i="${row.index}">Replace clip</button>
          ${automatchAvailable ? `<button class="rvremat" data-i="${row.index}">Re-auto-match</button>` : ''}
          <label><input type="checkbox" class="rvchk" data-i="${row.index}" ${row.reviewed ? 'checked' : ''} ${row.preview ? '' : 'disabled'}> looks right</label>
        </div>
      </div>
      <div class="rev-r">${reviewMediaHTML(row)}</div>
    </div>`;
  }).join('');

  if (reviewFilter && !$('reviewlist').querySelector('.rev-row:not(.hide)')) {
    $('reviewlist').insertAdjacentHTML('beforeend',
      `<div class="rev-none" style="color:var(--dim)">Nothing flagged and unchecked &mdash; every scene has been reviewed or matched cleanly.</div>`);
  }

  $('reviewlist').querySelectorAll('video[data-src]').forEach(v => { if (reviewIO) reviewIO.observe(v); else v.src = v.dataset.src; });
  $('reviewlist').querySelectorAll('.rvreplace').forEach(b => b.onclick = () => reviewReplace(+b.dataset.i));
  $('reviewlist').querySelectorAll('.rvremat').forEach(b => b.onclick = () => reviewRematch(+b.dataset.i, b));
  $('reviewlist').querySelectorAll('.rvchk').forEach(c => c.onchange = () => reviewMark(+c.dataset.i, c.checked));
}

function reviewReplace(index) {
  returnToReview = true;
  closeReview();
  const i = scenes.findIndex(s => s.index === index);
  if (i === -1) return;
  go(i);
  $('main').scrollTo({ top: 0 });
  $('status').className = '';
  $('status').textContent = `Replacing the clip for scene ${String(index).padStart(3, '0')} -- ` +
    `pick a new one below (search, auto-match, AI, or a local file). You'll return to the review automatically.`;
}

async function reviewRematch(index, btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Matching...'; }
  const threshold = parseInt($('strictness').value, 10) || 60;
  try {
    const d = await postAutomatch({ index, threshold, ai_fallback: $('aifallback').checked });
    if (d.error) { $('status').className = 'err'; $('status').textContent = d.error; closeReview(); return; }
    for (const e of d.entries || []) matchNotes[e.scene_index] = automatchNote(e);
    await refreshSelections();
    await openReview();   // re-fetch + re-render with the new pick
  } catch (e) {
    $('status').className = 'err'; $('status').textContent = 'Re-auto-match failed: ' + e;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Re-auto-match'; }
  }
}

async function reviewMark(index, reviewed) {
  try {
    const r = await fetch('/api/review-mark', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ index, reviewed })
    });
    const d = await r.json();
    if (d.error) { $('status').className = 'err'; $('status').textContent = d.error; return; }
    const row = (reviewData.scenes || []).find(s => s.index === index);
    if (row) row.reviewed = reviewed;
    const rowEl = $('reviewlist').querySelector(`.rev-row[data-i="${index}"]`);
    if (rowEl) {
      rowEl.classList.toggle('reviewed', reviewed);
      if (reviewFilter && reviewed) rowEl.classList.add('hide');
    }
    const total = reviewData.total || 0;
    const n = (reviewData.scenes || []).filter(s => s.reviewed).length;
    $('rvcount').textContent = `${n} / ${total} checked`;
    $('rvbar').firstElementChild.style.width = total ? (100 * n / total) + '%' : '0';
  } catch (e) {
    $('status').className = 'err'; $('status').textContent = 'Could not save that: ' + e;
  }
}

async function reviewRenderClick() {
  const d = reviewData;
  const missing = (d.scenes || []).filter(s => !s.preview).length;
  if (missing) {
    $('rvcount').textContent = `${missing} scene(s) still have no clip -- replace those first`;
    const first = (d.scenes || []).find(s => !s.preview);
    if (first) {
      const el = $('reviewlist').querySelector(`.rev-row[data-i="${first.index}"]`);
      if (el) { el.classList.remove('hide'); el.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
    }
    return;
  }
  closeReview();
  startRender();
}

$('reviewall').onclick = openReview;
$('rvback').onclick = closeReview;
$('rvrender').onclick = reviewRenderClick;
$('rvfilter').onchange = () => { reviewFilter = $('rvfilter').checked; renderReview(); };

let automatchStop = false;
const AUTOMATCH_ALL_LABEL = '✨ Auto-match all unpicked';

async function postAutomatch(body) {
  const r = await fetch('/api/automatch', {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)
  });
  return r.json();
}

async function runAutomatch(all) {
  const btn = all ? $('automatchall') : $('automatchbtn');
  // A second click while "all" is running = stop after the scene in flight.
  if (all && btn.classList.contains('busy')) {
    automatchStop = true;
    $('status').textContent = 'Stopping after the current scene...';
    return;
  }
  btn.classList.add('busy');
  $('status').className = '';
  const mq = $('minquality').value;
  const threshold = parseInt($('strictness').value, 10) || 60;
  const aiFallback = $('aifallback').checked;

  try {
    if (!all) {
      $('status').textContent = 'Auto-matching this scene: searching providers and scoring candidates against its narration...';
      const body = {index: scenes[cur].index, threshold, ai_fallback: aiFallback};
      if (mq) body.min_quality = mq;
      const d = await postAutomatch(body);
      if (d.error) { $('status').className = 'err'; $('status').textContent = d.error; return; }
      if (d.paused) { $('status').className = 'err'; $('status').textContent = 'Auto-match unavailable — ' + d.paused; return; }
      for (const e of d.entries || []) matchNotes[e.scene_index] = automatchNote(e);
      await refreshSelections();
      renderBrief(scenes[cur]);
      const e0 = (d.entries || [])[0] || {};
      $('status').className = (e0.status === 'PASS' && !e0.soft) ? '' : 'err';
      $('status').textContent = e0.status === 'PASS'
        ? `Scene ${e0.scene_index}: ${e0.soft ? 'soft match' : 'matched'} at ${e0.score}/100 -- see the note above.`
        : `No candidate scored high enough -- reasons above; pick manually or adjust the tags.`;
      return;
    }

    // "all": one scene per request so the sidebar fills in live, a stop or a
    // dropped connection never loses the scenes already done, and Gemini's
    // free-tier pacing doesn't hide behind one silent multi-minute request.
    automatchStop = false;
    btn.textContent = '⏹ Stop auto-match';
    const attempted = [];
    // Below the strictness bar but this close to it -> still fill the scene
    // (flagged "soft") rather than leave it empty. Scales with strictness:
    // balanced 60 -> 45, strict 75 -> 60, exact 85 -> 70.
    const softFloor = Math.max(35, threshold - 15);
    let matched = 0, soft = 0, failed = 0, aiFilled = 0, heur = 0;
    let sawLlmDown = false;
    while (!automatchStop) {
      const body = {all:true, limit:1, threshold, soft_floor: softFloor, exclude: attempted, ai_fallback: aiFallback};
      if (mq) body.min_quality = mq;
      let d;
      try { d = await postAutomatch(body); }
      catch (err) {
        $('status').className = 'err';
        $('status').textContent = 'Auto-match request failed: ' + err + ' -- re-run to continue where it stopped.';
        break;
      }
      if (d.error) { $('status').className = 'err'; $('status').textContent = d.error; break; }
      const es = d.entries || [];
      for (const e of es) { attempted.push(e.scene_index); matchNotes[e.scene_index] = automatchNote(e); }
      matched += d.matched || 0; soft += d.soft || 0; failed += d.failed || 0;
      aiFilled += d.ai_filled || 0; heur += d.heuristic || 0;
      if (d.llm_down) sawLlmDown = true;
      const aiTxt = aiFilled ? `, ${aiFilled} AI-filled` : '';
      const heurTxt = heur ? `, ${heur} keyword-only` : '';
      if (d.paused) {
        await refreshSelections();
        $('status').className = 'err';
        $('status').textContent =
          `Auto-match paused after ${matched} matched${soft ? ` (${soft} soft)` : ''}${heurTxt}${aiTxt} — ${d.paused}`;
        break;
      }
      await refreshSelections();
      if (scenes[cur]) renderBrief(scenes[cur]);
      $('status').className = d.llm_down ? 'err' : '';
      $('status').textContent = (d.llm_down
          ? `Gemini quota hit -- still going, matching the rest by keyword overlap (flagged for a later re-judge). `
          : `Auto-matching... `) +
        `${matched} matched` + (soft ? ` (${soft} soft)` : '') + heurTxt + aiTxt +
        `, ${failed} to review, ${d.remaining} scene(s) left. Click the button again to stop -- progress is saved.`;
      if (!es.length || d.remaining === 0) break;
    }
    const aiDone = aiFilled ? `, ${aiFilled} AI-filled` : '';
    const heurDone = heur ? `, ${heur} keyword-only (re-run later to upgrade)` : '';
    $('status').className = (failed || heur || sawLlmDown) ? 'err' : '';
    $('status').textContent = automatchStop
      ? `Stopped. This run: ${matched} matched${soft ? ` (${soft} soft)` : ''}${heurDone}${aiDone}, ${failed} to review. Re-run to do the rest.`
      : (sawLlmDown
          ? `Auto-match finished with Gemini out of quota: ${matched} matched${soft ? ` (${soft} soft)` : ''}${heurDone}${aiDone}, ${failed} to review. Re-run "Auto-match all" after the quota resets (midnight Pacific) to re-judge the keyword-only scenes automatically.`
          : `Auto-match finished: ${matched} matched${soft ? ` (${soft} soft)` : ''}${heurDone}${aiDone}, ${failed} to review. Scores and reasons in sync_report.json.`);
    // Every scene now has a clip -- go straight into the side-by-side review
    // so mismatches get caught before the render, exactly when the whole
    // script is freshly matched and easiest to scan.
    if (!automatchStop) {
      await refreshSelections();
      if (scenes.length && Object.keys(selections).length === scenes.length) {
        setTimeout(openReview, 400);
      }
    }
  } catch (err) {
    $('status').className = 'err'; $('status').textContent = 'Auto-match failed: ' + err;
  } finally {
    btn.classList.remove('busy');
    if (all) btn.textContent = AUTOMATCH_ALL_LABEL;
    automatchStop = false;
  }
}

async function boot() {
  const d = await (await fetch('/api/scenes')).json();
  scenes = d.scenes; selections = d.selections;
  highlight = new Set(d.highlight || []);
  automatchAvailable = !!d.automatch_available;
  autoRenderEnabled = d.auto_render !== false;
  projectPath = d.project || '';
  // Always show the buttons -- with no Gemini key the endpoint returns a clear
  // one-line reason on click, which is more discoverable than a button that
  // silently isn't there. The strictness dropdown only matters when usable.
  $('automatchbtn').style.display = '';
  $('automatchall').style.display = '';
  $('strictness').style.display = automatchAvailable ? '' : 'none';
  // AI illustration is always available (Pollinations needs no key), so the
  // "fill unmatchable scenes with AI" option shows whenever auto-match does.
  aiWatermarked = d.ai_watermarked !== false;
  $('aifallbackwrap').style.display = automatchAvailable ? 'flex' : 'none';
  // Only the narration text flips -- the chrome around it stays LTR so the
  // controls don't move around between language folders.
  if (d.rtl) document.body.classList.add('rtl-script');
  $('rendercmd').textContent = 'python step3_render_video.py ' + d.project;
  if (highlight.size) {
    $('flagged').style.display = 'block';
    $('flagged').textContent = `Step 3 flagged ${highlight.size} scene(s) below in red -- `
      + `they still need a clip (never picked, or the pick couldn't be downloaded). Fix those, then re-render.`;
  }
  renderSidebar();
  // Resume where you left off: prefer a flagged scene, otherwise the first
  // scene that's never been picked at all.
  const firstFlagged = scenes.findIndex(s => highlight.has(s.index));
  const firstOpen = scenes.findIndex(s => !selections[s.index]);
  const start = firstFlagged !== -1 ? firstFlagged : firstOpen;
  // Already complete when the page loads (e.g. reopening a finished project)
  // -- don't pop the render confirm immediately, only on a fresh completion.
  if (start === -1 && scenes.length) renderOffered = true;
  go(start === -1 ? 0 : start);
}

function renderSidebar() {
  $('scenelist').innerHTML = scenes.map((s,i) => {
    const done = !!selections[s.index];
    const flagged = highlight.has(s.index);
    return `<div class="scene-item ${done?'done':''} ${flagged?'needs-clip':''} ${i===cur?'active':''}" data-i="${i}">
      <span class="num">${String(s.index).padStart(3,'0')}</span>
      <span class="snip">${s.text.replace(/</g,'&lt;')}</span>
      ${flagged?'<span class="warn" title="Missing a clip">&#9888;</span>':(done?'<span class="tick">&#10003;</span>':'')}</div>`;
  }).join('');
  $('scenelist').querySelectorAll('.scene-item').forEach(el =>
    el.onclick = () => go(+el.dataset.i));
  const n = Object.keys(selections).length;
  $('ptext').textContent = `${n} / ${scenes.length} scenes have a clip`;
  $('bar').firstElementChild.style.width = (100*n/scenes.length) + '%';
  $('done').style.display = (n === scenes.length && highlight.size === 0) ? 'block' : 'none';
  $('resetall').disabled = n === 0;
  $('reviewall').style.display = n ? '' : 'none';
}

async function resetAll() {
  const n = Object.keys(selections).length;
  if (!n) return;
  if (!confirm(`Delete all ${n} selected clip(s) and start over?\nThis removes the downloaded files and clears every selection. It can't be undone.`)) return;
  const r = await fetch('/api/clear-all', {
    method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'
  });
  const d = await r.json();
  if (d.error) { $('status').className='err'; $('status').textContent = d.error; return; }
  selections = {};
  renderOffered = false;
  $('status').className = ''; $('status').textContent = `Reset ${d.cleared} clip(s). Starting fresh.`;
  go(0);
}

// Rebuild a cart-ready asset from a saved multi-clip item (selections.json's
// `items` shape) so reopening a multi-clip scene can restore its picks.
// Local uploads carry no data here (selections.json only records the
// filename already written to disk) -- restored as a placeholder that
// materialize_local_asset will refuse to re-save without a re-upload,
// rather than silently writing an empty file.
function itemToAsset(it) {
  const kind = it.source === 'local' ? 'local'
             : it.source === 'ai' ? 'ai'
             : (it.type === 'video' ? 'video' : 'photo');
  return {
    kind, id: it.pexels_id, source: it.source || 'pexels',
    download_url: it.download_url, width: it.width, height: it.height,
    author: it.author, author_url: it.author_url, page_url: it.page_url,
    local_name: it.file, __uid: kind === 'local' ? ++multiUidCounter : undefined,
    // AI items carry the recipe so a re-save can regenerate the file if the
    // .ai_cache copy is gone (materialize_ai_asset). The cache_name isn't in
    // selections.json, so re-derive it there from prompt+seed.
    prompt: it.ai_prompt, seed: it.ai_seed, model: it.ai_model,
  };
}

function go(i) {
  if (i < 0 || i >= scenes.length) return;
  cur = i; page = 1;
  const s = scenes[i];
  const sel = selections[s.index];
  // Multi mode is a global toggle that would otherwise leak from whichever
  // scene it was last turned on for -- sync it (and the picks tray) to what
  // THIS scene actually has saved, so a multi-clip scene reopens showing its
  // existing picks instead of an empty tray with no "chosen" highlighting.
  multiMode = !!(sel && sel.type === 'multi');
  multiPicks = multiMode ? (sel.items || []).map(itemToAsset) : [];
  $('multimode').checked = multiMode;
  $('multistuff').classList.toggle('on', multiMode);
  $('bulkmode').disabled = multiMode;
  renderMultiBar();
  syncLocalPanel();
  $('scenemeta').textContent =
    `Scene ${String(s.index).padStart(3,'0')} of ${scenes.length}  .  ${fmt(s.start)}-${fmt(s.end)}  .  needs ${s.duration.toFixed(1)}s`;
  renderBrief(s);
  renderScript(s);
  $('q').value = '';
  prefillAIPrompt(true);  // reset the AI prompt box to this scene's subject
  if (source === 'ai-illustration') { results = []; }
  renderSidebar();
  $('scenelist').querySelector('.active')?.scrollIntoView({block:'nearest'});
  // Default the search to the scene's semantic query when the timeline
  // provided one (a concrete "what the camera sees" phrase), else its few
  // important keywords. All sentence words are still shown as chips below,
  // so you can click any of them to add more. A scene already picked
  // restores exactly the tags it was found with. setTags searches.
  const saved = selections[s.index]?.query;
  const semantic = (s.queries && s.queries.length) ? s.queries[0].split(/\s+/) : null;
  const defaults = semantic || ((s.keywords && s.keywords.length) ? s.keywords : sceneWordList().slice(0, 5));
  setTags(saved ? saved.split(/\s+/) : defaults);
}

async function search() {
  page = 1; results = []; hasNext = false;
  if (source === 'local') return;
  if (source === 'ai-illustration') {
    // Nothing to search -- the AI panel generates on demand. Keep whatever
    // variations are already in the grid for this scene.
    renderGrid();
    $('pageinfo').textContent = ''; $('loadmore').style.display = 'none';
    return;
  }
  const q = searchTags.join(' ').trim();
  if (!q) {
    $('grid').innerHTML = '';
    $('status').className = '';
    $('status').textContent = 'No search tags -- click a word above or type a keyword to search.';
    $('pageinfo').textContent = ''; $('loadmore').style.display = 'none';
    return;
  }
  $('grid').innerHTML = ''; $('status').className = '';
  $('status').textContent = `Searching ${PROVIDER_LABELS[SOURCES[source].provider]}...`;
  await fetchPage();
}

// Fetches the current `page` for the active tags/source and merges it into
// `results` -- called by search() (page 1, replaces the grid) and loadMore()
// (page N+1, appended as the grid is scrolled toward the bottom).
async function fetchPage() {
  const meta = SOURCES[source];
  const s = scenes[cur], q = searchTags.join(' ').trim();
  let url = `${meta.endpoint}?q=${encodeURIComponent(q)}&page=${page}&min_quality=${$('minquality').value}`;
  if (meta.media === 'video') {
    url += `&min_duration=${$('longenough').checked ? Math.ceil(s.duration) : ''}`;
  }
  const r = await fetch(url);
  const d = await r.json();
  if (d.error) {
    $('status').className = 'err'; $('status').textContent = d.error;
    $('loadmore').style.display = 'none';
    return;
  }
  results = results.concat(d.videos);
  hasNext = d.has_next;
  const qualityNote = d.quality_unverified
    ? " -- Coverr doesn't report clip resolution, so the quality filter isn't applied to this tab"
    : '';
  $('status').textContent = (d.total_results
    ? `${results.length} of ${d.total_results} results loaded${d.from_cache ? ' (cached)' : ''}`
    : 'No results -- try different words in the search box.') + qualityNote;
  $('rate').textContent = d.rate_remaining != null
    ? `${PROVIDER_LABELS[meta.provider]} quota left: ${d.rate_remaining}` : '';
  $('pageinfo').textContent = `Page ${d.page}`;
  $('loadmore').style.display = hasNext ? 'block' : 'none';
  renderGrid();
}

// Fires when the results grid is scrolled near its bottom -- fetches the
// next page and appends it, so paging through hundreds of clips never needs
// a manual "next page" click.
async function loadMore() {
  if (loadingMore || !hasNext || source === 'local') return;
  loadingMore = true;
  page++;
  $('loadmore').textContent = 'Loading more...';
  try {
    await fetchPage();
  } finally {
    loadingMore = false;
  }
}

function bulkLabel() {
  const from = String(parseInt($('bulkfrom').value, 10) || 0).padStart(3,'0');
  const to = String(parseInt($('bulkto').value, 10) || 0).padStart(3,'0');
  return `Use for scenes ${from}&ndash;${to}`;
}

// Identity for an asset within multiPicks -- a remote item by provider+kind+
// id (the same numeric id can occur on two different providers), a local
// upload by a counter stamped on it when added (two different files can
// otherwise look identical, and the same file re-added should count as two
// picks anyway).
function assetKey(a) {
  return a.kind === 'local' ? `local:${a.__uid}` : `${a.source || 'pexels'}:${a.kind}:${a.id}`;
}

function renderGrid() {
  const s = scenes[cur], sel = selections[s.index], meta = SOURCES[source], isPhoto = meta.media === 'photo';
  // Selections saved before the image/local-file feature existed have no
  // source/type fields -- they were always a Pexels video pick, so default
  // to that rather than losing the "chosen" ring on every pre-existing pick.
  const selSource = sel ? (sel.source || 'pexels') : undefined;
  const selType = sel ? (sel.type || 'video') : undefined;
  const isMultiSel = selType === 'multi';
  const wantType = isPhoto ? 'image' : 'video';
  const cartKeys = new Set(multiPicks.map(assetKey));
  $('grid').innerHTML = results.map((v,i) => {
    const short = !isPhoto && v.duration < s.duration;
    const isChosen = !bulkMode && !multiMode && !isMultiSel
                     && selSource === meta.provider && sel?.pexels_id === v.id && selType === wantType;
    // Reopening a scene that was saved as a multi-clip pick -- show which of
    // its several clips is which, same as the single-pick "chosen" ring.
    const wasMultiItem = !multiMode && isMultiSel
                     && (sel.items || []).some(it => (it.source || 'pexels') === meta.provider && it.pexels_id === v.id && it.type === wantType);
    const inCart = multiMode && cartKeys.has(assetKey(v));
    const media = isPhoto
      ? `<img class="thumb" src="${v.preview_url}" loading="lazy">`
      : `<video src="${v.preview_url}" muted loop preload="metadata" playsinline></video>`;
    const dims = (v.width && v.height) ? `${v.width}x${v.height}` : ''; // Coverr doesn't report dimensions
    const info = isPhoto
      ? `<span>${dims}</span><span>${esc(v.author || '')}</span>`
      : `<span class="${short?'short':''}">${v.duration}s${short?' (will loop)':''}</span><span>${dims}</span>`;
    const label = multiMode
      ? (inCart ? 'Remove from picks' : 'Add to picks')
      : (bulkMode ? bulkLabel()
        : (isChosen ? 'Selected &#10003;'
          : (wasMultiItem ? 'Used in this scene &#10003;' : (isPhoto ? 'Use this image' : 'Use this clip'))));
    const cardClass = (isChosen || wasMultiItem) ? 'chosen' : (inCart ? 'picked' : '');
    return `<div class="card ${cardClass}" data-i="${i}">
      ${media}
      <div class="info">${info}</div>
      <button>${label}</button>
    </div>`;
  }).join('');
  // Hover-to-play keeps 12 clips from all decoding at once on load.
  $('grid').querySelectorAll('.card').forEach(card => {
    const vid = card.querySelector('video');
    if (vid) {
      card.onmouseenter = () => vid.play().catch(()=>{});
      card.onmouseleave = () => { vid.pause(); vid.currentTime = 0; };
      vid.onclick = () => vid.paused ? vid.play() : vid.pause();
    }
    card.querySelector('button').onclick = () => {
      const v = results[+card.dataset.i];
      if (multiMode) toggleMultiPick(v);
      else if (bulkMode) chooseRange(v);
      else choose(v);
    };
  });
}

function toggleMultiPick(v) {
  const k = assetKey(v);
  const idx = multiPicks.findIndex(p => assetKey(p) === k);
  if (idx !== -1) multiPicks.splice(idx, 1);
  else multiPicks.push(v);
  renderMultiBar();
  renderGrid();
}

function removeMultiPick(k) {
  multiPicks = multiPicks.filter(p => assetKey(p) !== k);
  renderMultiBar();
  renderGrid();
}

// Shots this scene is broken into (semantic timeline). When there are 2+,
// multi-clip mode is "one clip per shot" and each clip is locked to its
// shot's own narration window in step 3 -- NOT an even time split.
function curShots() {
  return ((scenes[cur] && scenes[cur].shots) || []).filter(sh => sh && (sh.text || sh.duration));
}

function renderMultiBar() {
  const s = scenes[cur];
  const shots = curShots();
  const shotMode = shots.length >= 2;
  const n = multiPicks.length;
  $('multipicks').innerHTML = multiPicks.map((p, i) => {
    const base = p.kind === 'local' ? 'Local image'
      : p.kind === 'ai' ? 'AI illustration'
      : `${PROVIDER_LABELS[p.source] || 'Pexels'} ${p.kind === 'photo' ? 'photo' : 'video'} #${p.id}`;
    let label = base;
    if (shotMode) {
      label = i < shots.length
        ? `Shot ${i + 1} (${(shots[i].duration || 0).toFixed(1)}s) ← ${base}`
        : `${base} — extra, will be rejected`;
    }
    return `<span class="multichip">${esc(label)}<button title="Remove" data-k="${esc(assetKey(p))}">&times;</button></span>`;
  }).join('');
  $('multipicks').querySelectorAll('button').forEach(b => b.onclick = () => removeMultiPick(b.dataset.k));
  if (shotMode) {
    const slots = shots.map((sh, i) =>
      `Shot ${i + 1}: ${(sh.duration || 0).toFixed(1)}s ${multiPicks[i] ? '✓' : '—'}`
    ).join('  ·  ');
    $('multisplit').textContent =
      `This scene has ${shots.length} shots — pick exactly one clip per shot, in order. ` +
      `Each clip plays for its own shot's narration (clip 1 ends when shot 1 ends, then clip 2). ` +
      slots;
    $('multisave').disabled = n !== shots.length;
  } else {
    $('multisplit').textContent = n > 0
      ? `${n} clip${n === 1 ? '' : 's'} selected — ${(s.duration / n).toFixed(1)}s each of the ${s.duration.toFixed(1)}s scene`
      : 'Add 2 or more clips below, then save -- the scene’s time splits evenly between them.';
    $('multisave').disabled = n < 1;
  }
}

async function saveMultiPicks() {
  const s = scenes[cur];
  if (multiPicks.length < 1) return;
  const shots = curShots();
  if (shots.length >= 2 && multiPicks.length !== shots.length) {
    $('status').className = 'err';
    $('status').textContent =
      `This scene has ${shots.length} shots -- add exactly ${shots.length} clips (one per shot), ` +
      `in shot order, so each clip covers its own shot. You have ${multiPicks.length}.`;
    return;
  }
  $('status').className = ''; $('status').textContent = 'Saving picks...';
  const query = searchTags.join(' ');
  const r = await fetch('/api/select-multi', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({index: s.index, assets: multiPicks, query})
  });
  const d = await r.json();
  if (d.error) { $('status').className='err'; $('status').textContent = d.error; return; }
  const d2 = await (await fetch('/api/scenes')).json();
  selections = d2.selections;
  highlight.delete(s.index);
  $('status').textContent = d.shot_aligned
    ? `Saved ${d.count} clip(s) -- each locked to its shot's narration window.`
    : `Saved ${d.count} clip(s), time split evenly across the scene.`;
  multiPicks = [];
  renderMultiBar();
  renderSidebar(); renderGrid();
  if (returnToReview) { returnToReview = false; setTimeout(openReview, 300); return; }
  // Auto-advance to the next scene still missing a clip, same as a single pick.
  const nxt = scenes.findIndex((sc,i) => i > cur && !selections[sc.index]);
  if (nxt !== -1) setTimeout(() => go(nxt), 350);
  else setTimeout(maybeOfferRender, 350);
}

async function choose(v) {
  const s = scenes[cur];
  $('status').className = ''; $('status').textContent = v.kind === 'local' ? 'Saving image...' : 'Saving pick...';
  const query = searchTags.join(' ');
  const r = await fetch('/api/select', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({index: s.index, asset: v, query})
  });
  const d = await r.json();
  if (d.error) { $('status').className='err'; $('status').textContent = d.error; return; }
  selections[s.index] = {
    pexels_id: v.kind === 'local' ? null : v.id, file: d.file, query,
    type: v.kind === 'video' ? 'video' : 'image',
    source: v.kind === 'local' ? 'local' : (v.source || 'pexels'),
  };
  highlight.delete(s.index);
  $('status').textContent = `Saved ${d.file}`;
  renderSidebar(); renderGrid();
  // Came here from "Replace clip" in the review page -- go straight back to it
  // so a long review isn't interrupted by scanning forward through the script.
  if (returnToReview) { returnToReview = false; setTimeout(openReview, 300); return; }
  // Auto-advance to the next scene still missing a clip -- the whole job is
  // hundreds of these, so every saved click matters.
  const nxt = scenes.findIndex((sc,i) => i > cur && !selections[sc.index]);
  if (nxt !== -1) setTimeout(() => go(nxt), 350);
  else setTimeout(maybeOfferRender, 350);
}

// Fires once, right when the last scene gets its clip. Shows a 60-second
// countdown and then starts step 3 automatically with default settings --
// no confirm() to click through. "Render now" starts it immediately;
// "Not yet" cancels the auto-start and leaves the picker open.
let autoRenderTimer = null, autoRenderTick = null;

async function startRender() {
  if (autoRenderTimer) clearTimeout(autoRenderTimer);
  if (autoRenderTick) clearInterval(autoRenderTick);
  autoRenderTimer = autoRenderTick = null;
  $('autorender').style.display = 'none';
  $('status').className = ''; $('status').textContent = 'Starting render (step 3)...';
  try { await fetch('/api/finish', { method: 'POST' }); } catch (e) {}
  $('done').style.display = 'none';
  $('finished').style.display = 'block';
  $('finished').scrollIntoView({ behavior: 'smooth' });
}

function cancelAutoRender() {
  if (autoRenderTimer) clearTimeout(autoRenderTimer);
  if (autoRenderTick) clearInterval(autoRenderTick);
  autoRenderTimer = autoRenderTick = null;
  $('arcountdown').style.display = 'none';
  $('arwait').style.display = 'none';
  $('arcancelled').style.display = '';
  // Re-arm: replacing/re-picking the last scene brings the countdown back.
  renderOffered = false;
}

function maybeOfferRender() {
  if (renderOffered) return;
  const n = Object.keys(selections).length;
  if (!scenes.length || n !== scenes.length || highlight.size !== 0) return;
  renderOffered = true;
  const box = $('autorender');
  $('arcancelled').style.display = 'none';
  if (!autoRenderEnabled) {
    // Cloud-render mode (Codespaces): no countdown, show the push + Actions
    // steps instead. "Render now" stays as an explicit opt-in.
    $('arcountdown').style.display = 'none';
    $('arwait').style.display = 'none';
    $('arcloud').style.display = '';
    $('arcloudcmd').textContent =
      `git add projects/${projectPath} && git commit -m "${projectPath}: picks done" && git push\n` +
      `gh workflow run render.yml -f project=${projectPath}`;
    $('arnow').textContent = 'Render here anyway';
    $('arnow').onclick = startRender;
    box.style.display = 'block';
    box.scrollIntoView({ behavior: 'smooth' });
    return;
  }
  $('arcountdown').style.display = '';
  $('arwait').style.display = '';
  box.style.display = 'block';
  box.scrollIntoView({ behavior: 'smooth' });
  let left = 60;
  $('arcount').textContent = left;
  autoRenderTick = setInterval(() => {
    left -= 1;
    $('arcount').textContent = left > 0 ? left : 0;
    if (left <= 0 && autoRenderTick) { clearInterval(autoRenderTick); autoRenderTick = null; }
  }, 1000);
  autoRenderTimer = setTimeout(startRender, 60000);
  $('arnow').onclick = startRender;
  $('arwait').onclick = cancelAutoRender;
}

async function chooseRange(v) {
  const from = parseInt($('bulkfrom').value, 10), to = parseInt($('bulkto').value, 10);
  if (!from || !to || to < from) {
    $('status').className = 'err';
    $('status').textContent = 'Enter a valid "from" and "to" scene number (to must be >= from).';
    return;
  }
  const n = to - from + 1;
  if (!confirm(`Apply this ${v.kind === 'video' ? 'clip' : 'image'} to scenes ${from}-${to} (${n} scene${n===1?'':'s'})?\nThis overwrites any clip already picked for those scenes.`)) return;
  $('status').className = ''; $('status').textContent = `Applying to scenes ${from}-${to}...`;
  const query = searchTags.join(' ');
  const r = await fetch('/api/select-range', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({from, to, asset: v, query})
  });
  const d = await r.json();
  if (d.error) { $('status').className='err'; $('status').textContent = d.error; return; }
  const d2 = await (await fetch('/api/scenes')).json();
  selections = d2.selections;
  for (let idx = from; idx <= to; idx++) highlight.delete(idx);
  $('status').textContent = `Applied to ${d.count} scene(s).`;
  renderSidebar(); renderGrid();
  // Auto-advance to the next scene still missing a clip, same as a single pick.
  const nxt = scenes.findIndex(sc => !selections[sc.index]);
  if (nxt !== -1) setTimeout(() => go(nxt), 350);
  else setTimeout(maybeOfferRender, 350);
}

function updateBulkDefaults() {
  $('bulkfrom').value = scenes[cur].index;
  $('bulkto').value = scenes[scenes.length - 1].index;
}

function localBtnLabel() {
  if (bulkMode) return bulkLabel();
  if (multiMode) return 'Add image to picks';
  return 'Use this image';
}

// Keep the Local-file panel's button label + the multi-mode hint in sync with
// the current bulk/multi state -- even before a file is chosen, so the button
// never misleadingly says "Use this image" while multi mode is on.
function syncLocalPanel() {
  $('localpanel').classList.toggle('multi', multiMode && !bulkMode);
  $('uselocalbtn').textContent = localBtnLabel();
}

function refreshBulkLabels() {
  renderGrid();
  syncLocalPanel();
}

// Bulk mode (one clip -> a range of scenes) and multi mode (several clips ->
// this one scene, time split between them) are mutually exclusive -- picking
// one turns the other off.
$('bulkmode').onchange = () => {
  bulkMode = $('bulkmode').checked;
  $('bulkrange').classList.toggle('on', bulkMode);
  if (bulkMode) {
    updateBulkDefaults();
    if (multiMode) { multiMode = false; $('multimode').checked = false; $('multistuff').classList.remove('on'); multiPicks = []; renderMultiBar(); }
  }
  $('multimode').disabled = bulkMode;
  refreshBulkLabels();
};
$('bulkallremaining').onclick = () => { updateBulkDefaults(); refreshBulkLabels(); };
$('bulkfrom').oninput = refreshBulkLabels;
$('bulkto').oninput = refreshBulkLabels;

$('multimode').onchange = () => {
  multiMode = $('multimode').checked;
  $('multistuff').classList.toggle('on', multiMode);
  if (multiMode && bulkMode) { bulkMode = false; $('bulkmode').checked = false; $('bulkrange').classList.remove('on'); }
  $('bulkmode').disabled = multiMode;
  multiPicks = [];
  renderMultiBar();
  refreshBulkLabels();
};
$('multisave').onclick = saveMultiPicks;

// Source tabs: Pexels video/photo, Pixabay video/photo, Coverr video, the AI
// illustration generator, or a local image file (see SOURCES above). Only one
// is active at a time; switching re-runs the search (local + AI just show
// their own panel -- there's nothing to search).
const SEARCH_UI_IDS = ['wlwords', 'scenewords', 'wltags', 'tagbox', 'opts', 'grid', 'pager'];
function setSource(next) {
  if (source === next) return;
  source = next;
  document.querySelectorAll('.srctab').forEach(b => b.classList.toggle('active', b.dataset.src === source));
  const isLocal = source === 'local';
  const isAI = source === 'ai-illustration';
  // Hide the tag/search chrome for both panel tabs; AI still shows the grid
  // (its generated variations) and pager area, local shows neither.
  ['wlwords', 'scenewords', 'wltags', 'tagbox', 'opts'].forEach(id => {
    $(id).style.display = (isLocal || isAI) ? 'none' : '';
  });
  $('grid').style.display = isLocal ? 'none' : '';
  $('pager').style.display = isLocal ? 'none' : '';
  $('localpanel').classList.toggle('on', isLocal);
  $('aipanel').classList.toggle('on', isAI);
  $('longenoughwrap').style.display = SOURCES[source].media === 'video' ? '' : 'none';
  if (isLocal) {
    syncLocalPanel();
    $('status').className = ''; $('status').textContent = multiMode
      ? 'Multi-clip mode: add each image, then Save picks.' : '';
  } else if (isAI) {
    prefillAIPrompt(false);
    $('aihint').textContent = aiWatermarked
      ? 'Free tier: ~1 image / 15s, small corner watermark. Add tools/pollinations_token.txt (free) for the faster, watermark-free tier.'
      : 'Faster, watermark-free tier (token found).';
    results = []; renderGrid();
    $('status').className = ''; $('status').textContent = 'Edit the prompt, then Generate.';
  } else {
    page = 1; search();
  }
}
document.querySelectorAll('.srctab').forEach(b => b.onclick = () => setSource(b.dataset.src));

// ---- AI illustration panel ----
function aiSubjectFor(s) {
  const sem = s.semantic || {};
  let subj = [sem.subject, sem.action, sem.object].filter(Boolean).join(', ');
  if (sem.location) subj += (subj ? ', in ' : 'in ') + sem.location;
  if (sem.time) subj += ` (${sem.time})`;
  if (!subj) subj = (s.queries && s.queries[0]) || (s.keywords || []).join(' ')
                    || (s.text || '').trim().slice(0, 180);
  return subj;
}
function prefillAIPrompt(force) {
  const s = scenes[cur]; if (!s) return;
  const box = $('aiprompt');
  if (!force && box.dataset.scene === String(s.index)) return; // keep edits
  box.value = aiSubjectFor(s);
  box.dataset.scene = String(s.index);
}
async function generateAI() {
  if ($('aigen').classList.contains('busy')) return;
  const s = scenes[cur]; if (!s) return;
  const n = Math.max(1, Math.min(6, parseInt($('aicount').value, 10) || 3));
  const raw = $('airaw').checked;
  const prompt = $('aiprompt').value.trim();
  if (!prompt) { $('status').className = 'err'; $('status').textContent = 'Type what the scene should show first.'; return; }
  const batch = Math.floor(Math.random() * 9000000) + 1000000;
  aiStop = false;
  $('aigen').classList.add('busy');
  $('aistop').style.display = '';
  results = []; renderGrid();
  try {
    for (let i = 0; i < n && !aiStop; i++) {
      $('status').className = '';
      $('status').textContent = `Generating illustration ${i + 1} of ${n}${aiWatermarked ? ' (free tier is slow -- up to ~20s each)' : ''}...`;
      let d;
      try {
        const r = await fetch('/api/ai-image', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ index: s.index, prompt, raw, batch, seed_offset: i }),
        });
        d = await r.json();
      } catch (err) {
        $('status').className = 'err'; $('status').textContent = 'Generation request failed: ' + err;
        break;
      }
      if (d.error) { $('status').className = 'err'; $('status').textContent = d.error; break; }
      results.push(d.image);
      renderGrid();
    }
    if (!aiStop && results.length) {
      $('status').className = '';
      $('status').textContent = `${results.length} illustration(s) ready -- pick one, or Generate again for more.`;
    } else if (aiStop) {
      $('status').textContent = `Stopped. ${results.length} illustration(s) ready.`;
    }
  } finally {
    $('aigen').classList.remove('busy');
    $('aistop').style.display = 'none';
    aiStop = false;
  }
}
$('aigen').onclick = generateAI;
$('aistop').onclick = () => { aiStop = true; $('status').textContent = 'Stopping after the current image...'; };

// Local file upload: read as a data URL client-side and hand it to the same
// choose()/chooseRange() flow the Pexels grid uses, tagged kind:'local' so
// the server knows to decode base64 instead of downloading a URL.
$('localfile').onchange = () => {
  const file = $('localfile').files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    localAsset = { kind: 'local', source: 'local', local_data: reader.result, local_name: file.name, __uid: ++multiUidCounter };
    $('localpreview').src = reader.result;
    $('localpreview').style.display = 'block';
    $('uselocalbtn').disabled = false;
    $('uselocalbtn').textContent = localBtnLabel();
  };
  reader.readAsDataURL(file);
};
$('uselocalbtn').onclick = () => {
  if (!localAsset) return;
  if (multiMode) {
    multiPicks.push(localAsset);
    renderMultiBar();
    $('status').className = ''; $('status').textContent = 'Added to picks -- choose another file, or Save picks.';
    localAsset = null;
    $('localfile').value = '';
    $('localpreview').style.display = 'none';
    $('uselocalbtn').disabled = true;
    $('uselocalbtn').textContent = localBtnLabel();
    return;
  }
  bulkMode ? chooseRange(localAsset) : choose(localAsset);
};

$('resetall').onclick = resetAll;
$('automatchbtn').onclick = () => runAutomatch(false);
$('automatchall').onclick = () => runAutomatch(true);
$('clearall').onclick = () => setTags([]);
$('searchbtn').onclick = () => {
  // Commit whatever's half-typed in the box, otherwise just re-run the search.
  if ($('q').value.trim()) { addTag($('q').value); $('q').value = ''; }
  else { page = 1; search(); }
};
$('q').onkeydown = e => {
  if (e.key === 'Enter') { e.preventDefault(); addTag($('q').value); $('q').value = ''; }
  else if (e.key === 'Backspace' && !$('q').value && searchTags.length) {
    removeTag(searchTags[searchTags.length - 1]);
  }
};
// Infinite scroll: #main is the scrollable pane (see CSS), so watch its
// scroll position rather than the window's.
$('main').addEventListener('scroll', () => {
  const el = $('main');
  if (el.scrollHeight - el.scrollTop - el.clientHeight < 600) loadMore();
});
$('nextscene').onclick = () => go(cur + 1);
$('prevscene').onclick = () => go(cur - 1);
$('longenough').onchange = () => { page = 1; search(); };
$('minquality').onchange = () => { page = 1; search(); };
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && $('review').classList.contains('on')) { closeReview(); return; }
  if ($('review').classList.contains('on')) return;  // review page has its own scroll
  if (e.target.tagName === 'INPUT') return;
  if (e.key === 'ArrowRight') go(cur + 1);
  if (e.key === 'ArrowLeft') go(cur - 1);
});
boot();
</script></body></html>
"""


def run(project, api_key=None, pixabay_api_key=None, coverr_api_key=None,
        pollinations_token=None, port=8000, no_browser=False, highlight=None,
        auto_render=None):
    """Serve the clip picker for `project` until every scene has a clip and
    the browser confirms it's time to render, or the server is interrupted.

    `highlight`, if given, is an iterable of scene indices to show with a red
    background in the sidebar -- used by step 3 to point out scenes it found
    missing a clip (never picked, or picked but the clip couldn't be
    downloaded).

    Pexels is required (the default tab, checked at startup below). Pixabay
    and Coverr are optional -- their tabs simply error in the browser, with
    instructions, until a key is added; no restart needed once it is.

    Returns True if the browser confirmed "render now" (caller should run
    step 3 next), False if the server just stopped (Ctrl+C, or closed with
    scenes still unpicked). Exits the process on any missing prerequisite,
    same as running this script directly."""
    project_dir = PROJECTS_DIR / project
    scenes_path = project_dir / "scenes.json"
    if not scenes_path.exists():
        sys.exit(f"Missing {scenes_path} -- run step1_audio_and_captions.py first.")

    api_key = resolve_api_key(api_key)
    if not api_key:
        sys.exit(
            "No Pexels API key found.\n"
            f"Save it to {KEY_FILE}, set PEXELS_API_KEY, or pass --api-key.\n"
            "Get a free key at https://www.pexels.com/api/"
        )
    pixabay_key = resolve_key(pixabay_api_key, "PIXABAY_API_KEY", PIXABAY_KEY_FILE)
    coverr_key = resolve_key(coverr_api_key, "COVERR_API_KEY", COVERR_KEY_FILE)
    pollinations_token = resolve_key(pollinations_token, "POLLINATIONS_TOKEN", POLLINATIONS_TOKEN_FILE)

    scenes = load_json(scenes_path)
    if not scenes:
        sys.exit(f"{scenes_path} has no scenes.")

    # scenes.json files written before the Pexels workflow existed have no
    # queries. Backfill them in place rather than making the user re-run step1,
    # which would re-synthesize the whole narration for nothing.
    if any("query" not in s for s in scenes):
        lang = Path(project).parts[0]
        for s in scenes:
            s.setdefault("keywords", extract_keywords(s["text"], lang=lang))
            s.setdefault("query", build_query(s["text"], lang=lang))
        save_json(scenes, scenes_path)
        print(f"Added search queries to {scenes_path.name} for {len(scenes)} scenes.")

    clips_dir = project_dir / "clips"
    clips_dir.mkdir(exist_ok=True)
    cache_dir = project_dir / ".pexels_cache"
    cache_dir.mkdir(exist_ok=True)
    # Generated AI illustrations live here until picked, then get copied into
    # clips/ like a local upload. Kept out of clips/ so "Reset all clips"
    # doesn't force a slow regenerate.
    ai_cache_dir = project_dir / ".ai_cache"
    ai_cache_dir.mkdir(exist_ok=True)

    # render.json (written by step1_audio_and_captions.py --vertical) picks
    # the Pexels search orientation -- a vertical Shorts/Reels project wants
    # portrait source footage, not a landscape clip cropped down to a sliver
    # of its own width.
    render_config_path = project_dir / "render.json"
    orientation = "landscape"
    if render_config_path.exists():
        render_config = load_json(render_config_path)
        if render_config.get("height", 0) > render_config.get("width", 0):
            orientation = "portrait"

    STATE.update({
        "project": project,
        "scenes": scenes,
        "clips_dir": clips_dir,
        "cache_dir": cache_dir,
        "selections_path": project_dir / "selections.json",
        "api_key": api_key,
        "pixabay_key": pixabay_key,
        "coverr_key": coverr_key,
        "pollinations_token": pollinations_token,
        "ai_cache_dir": ai_cache_dir,
        "ai_fallback": False,
        "ai_last_call": 0.0,
        # Latches within one "auto-match all" run when Gemini's judge hits its
        # quota, so the remaining scenes fall back to keyword-overlap picks
        # instead of the run stopping dead. Reset at the start of each run.
        "llm_down": None,
        "lock": threading.Lock(),
        "rate_remaining": None,
        # The narration panel shows the raw script text, so it has to follow
        # the script's own direction -- Urdu reads unusably as LTR.
        "rtl": caption_style(Path(project).parts[0])["rtl"],
        "lang": Path(project).parts[0] if Path(project).parts else "en",
        "finish_requested": False,
        "auto_render": auto_render_default() if auto_render is None else bool(auto_render),
        "highlight": set(highlight or []),
        "orientation": orientation,
    })

    # Gemini keys power the semantic auto-matcher (/api/automatch). Optional:
    # without one the picker works exactly as before, just without the
    # auto-match button. Re-read on every request (see refresh_llm_keys) so
    # adding a key later doesn't need a restart.
    refresh_llm_keys()

    done = len(load_selections())
    url = f"http://localhost:{port}/"
    print(f"{len(scenes)} scenes, {done} already have a clip.")
    if orientation == "portrait":
        print("Vertical project detected (render.json) -- searching Pexels for portrait clips.")
    optional = [name for name, key in (("Pixabay", pixabay_key), ("Coverr", coverr_key)) if not key]
    if optional:
        print(f"No API key configured yet for: {', '.join(optional)} -- those tabs will show a "
              f"setup message until you add one (see the module docstring).")
    if pollinations_token:
        print("Pollinations token found -- AI illustration tab runs on the faster, watermark-free tier.")
    else:
        print("AI illustration tab is on (free, no key). Add tools/pollinations_token.txt "
              "(free at https://auth.pollinations.ai) for faster, watermark-free images.")
    print(f"Picker running at {url}   (Ctrl+C to stop -- your progress is saved as you go)")
    if not no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    class SingleBindHTTPServer(ThreadingHTTPServer):
        # Python's HTTPServer sets SO_REUSEADDR, and on Windows that lets a
        # SECOND picker silently bind a port an older one is still serving --
        # connections then land on whichever process wins, so the browser
        # kept showing a stale project's scenes. SO_EXCLUSIVEADDRUSE makes
        # the second bind fail loudly instead.
        allow_reuse_address = False

        def server_bind(self):
            import socket
            if os.name == "nt":
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

    try:
        server = SingleBindHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        sys.exit(
            f"Port {port} is already in use (another clip picker still running?): {e}\n"
            f"Stop it, or run again with --port {port + 1}."
        )
    STATE["server"] = server
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\nStopped. {len(load_selections())}/{len(scenes)} scenes have clips.")
        print(f"Resume any time with: python step2_pick_clips.py {project}")
        return False

    if STATE.get("finish_requested"):
        print(f"\nAll {len(scenes)} scenes have a clip -- rendering the video now.")
        return True
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", help="Path under projects/, e.g. en/crumbs")
    parser.add_argument("--api-key", default=None, help="Pexels API key (else PEXELS_API_KEY or tools/pexels_key.txt)")
    parser.add_argument("--pixabay-api-key", default=None, help="Pixabay API key (else PIXABAY_API_KEY or tools/pixabay_key.txt) -- optional")
    parser.add_argument("--coverr-api-key", default=None, help="Coverr API key (else COVERR_API_KEY or tools/coverr_key.txt) -- optional")
    parser.add_argument("--pollinations-token", default=None, help="Pollinations token for the AI illustration tab (else POLLINATIONS_TOKEN or tools/pollinations_token.txt) -- optional; the tab works without it")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open the browser")
    ar = parser.add_mutually_exclusive_group()
    ar.add_argument("--no-auto-render", dest="auto_render", action="store_false", default=None,
                    help="Don't start step 3 automatically when the last scene is picked; show the "
                    "push + GitHub Actions render steps instead. Default inside a GitHub Codespace.")
    ar.add_argument("--auto-render", dest="auto_render", action="store_true",
                    help="Start step 3 here automatically (the default outside Codespaces).")
    args = parser.parse_args()

    render_now = run(
        args.project, api_key=args.api_key,
        pixabay_api_key=args.pixabay_api_key, coverr_api_key=args.coverr_api_key,
        pollinations_token=args.pollinations_token,
        port=args.port, no_browser=args.no_browser, auto_render=args.auto_render,
    )
    if render_now:
        import step3_render_video
        step3_render_video.run(args.project)


if __name__ == "__main__":
    main()
