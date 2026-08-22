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
import re
import shutil
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from common import USER_AGENT, caption_style, load_json, save_json
from keywords import build_query, extract_keywords

TOOLS_DIR = Path(__file__).parent
PROJECTS_DIR = TOOLS_DIR.parent / "projects"
KEY_FILE = TOOLS_DIR / "pexels_key.txt"
PIXABAY_KEY_FILE = TOOLS_DIR / "pixabay_key.txt"
COVERR_KEY_FILE = TOOLS_DIR / "coverr_key.txt"

PEXELS_SEARCH_URL = "https://api.pexels.com/videos/search"
PEXELS_PHOTO_SEARCH_URL = "https://api.pexels.com/v1/search"
PIXABAY_VIDEO_URL = "https://pixabay.com/api/videos/"
PIXABAY_PHOTO_URL = "https://pixabay.com/api/"
COVERR_VIDEO_URL = "https://api.coverr.co/videos"
PER_PAGE = 12
REQUEST_TIMEOUT = 30

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
    ext = local_ext(asset.get("local_name")) if kind == "local" else ("mp4" if kind == "video" else "jpg")
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


def register_asset(scene_index, asset, part=None):
    """Record `asset` as picked for `scene_index`, returning the destination
    path it will occupy in clips_dir. A local upload is written immediately
    (see materialize_local_asset); a remote pick is left unfetched -- its
    bytes are downloaded later, in bulk with a progress percentage, by
    step3_render_video.py's download_selected_clips()."""
    if asset.get("kind") == "local":
        return materialize_local_asset(scene_index, asset, part)
    return resolve_dest_path(scene_index, asset, part)


def selection_record(scene_index, asset, dest, query):
    kind = asset.get("kind", "video")
    return {
        "scene_index": scene_index,
        "type": "video" if kind == "video" else "image",
        "source": "local" if kind == "local" else (asset.get("source") or "pexels"),
        "pexels_id": asset.get("id") if kind != "local" else None,
        "file": dest.name,
        # Not set for a local upload -- it's already written to disk (see
        # materialize_local_asset), nothing left to fetch. For a remote pick,
        # this is what step3_render_video.py's download_selected_clips()
        # downloads before rendering.
        "download_url": None if kind == "local" else asset.get("download_url"),
        "duration": asset.get("duration"),
        "width": asset.get("width"),
        "height": asset.get("height"),
        "author": asset.get("author"),
        "author_url": asset.get("author_url"),
        "page_url": asset.get("page_url"),
        "query": query,
    }
# NOTE: "pexels_id" predates multi-provider support and is kept for
# selections.json backward-compatibility -- it now holds the id from
# whichever provider the "source" field names, not only Pexels.


def multi_item_record(asset, dest, share_seconds):
    """One entry in a multi-clip scene's `items` list -- same shape as a
    single-clip selection_record's asset fields, plus `share_seconds`, the
    nominal slice of the scene's narration this item covers (informational;
    step3 recomputes the real split from the scene's actual segment length)."""
    kind = asset.get("kind", "video")
    return {
        "type": "video" if kind == "video" else "image",
        "source": "local" if kind == "local" else (asset.get("source") or "pexels"),
        "pexels_id": asset.get("id") if kind != "local" else None,
        "file": dest.name,
        "download_url": None if kind == "local" else asset.get("download_url"),
        "share_seconds": round(share_seconds, 3),
        "width": asset.get("width"),
        "height": asset.get("height"),
        "author": asset.get("author"),
        "author_url": asset.get("author_url"),
        "page_url": asset.get("page_url"),
    }


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
# to be auto-selected; below it the scene is left for the human, with the
# scored candidates and failure reason shown in the report.
AUTOMATCH_THRESHOLD = 60
AUTOMATCH_POOL = 24  # max candidates scored per scene (LLM context budget)

AUTOMATCH_SYSTEM = """You are a documentary film editor judging stock footage against narration.
You are given one narration segment (its exact script text and semantic
requirements) and a list of candidate stock clips. All you know about each
clip is its short catalog description, duration, and resolution -- score
ONLY what the description actually says; never assume unmentioned content.

Score each candidate 0-100 for how well it visually represents THIS exact
narration:
- subject match and ACTION match matter most: footage whose description
  states a CONTRADICTING action (standing vs running, exterior vs entering)
  scores below 50.
- right subject in the right setting with the action simply UNSTATED (short
  catalog slugs rarely mention actions) scores 55-65 -- plausible, not
  proven.
- wrong location/era, or anything in the exclusion list, scores below 35.
- generic thematic B-roll that doesn't show the required subject/elements
  scores below 45, even if pleasant.
- a description explicitly matching subject+action+setting scores 75+; add
  points for matching time period, objects, and mood.
Return STRICT JSON only:
{"scores":[{"id":"C1","score":NN,"reason":"one short sentence"}, ...]}
Include every candidate exactly once."""


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


def _pick_best(scene_or_shot_brief, pool, entry_candidates):
    """Score `pool` against a brief; append scored candidates to
    entry_candidates and return (best_asset, best_score, best_reason)."""
    from groq_client import complete, strip_reasoning
    from semantic_timeline import _extract_json

    listing = []
    for i, c in enumerate(pool, start=1):
        desc = (c.get("desc") or "").strip() or "(no description)"
        dims = f"{c.get('width')}x{c.get('height')}" if c.get("width") else "unknown res"
        listing.append(f'C{i}: "{desc}" ({c.get("duration") or "?"}s, {dims}, {c.get("source")})')
    user = scene_or_shot_brief + "\n\nCANDIDATE CLIPS:\n" + "\n".join(listing) + "\n\nScore every candidate. STRICT JSON only."
    data = None
    last_err = None
    for attempt in range(2):  # a truncated/malformed reply is usually a one-off
        reply = complete(
            [{"role": "system", "content": AUTOMATCH_SYSTEM}, {"role": "user", "content": user}],
            STATE["groq_keys"], temperature=0.1, max_tokens=2000, verbose=False,
        )
        try:
            data = _extract_json(strip_reasoning(reply))
            break
        except (ValueError, json.JSONDecodeError) as e:
            last_err = e
    if data is None:
        raise ValueError(f"judge reply unparseable after retry: {last_err}")
    scores = {}
    for s in data.get("scores") or []:
        cid = str(s.get("id") or "").strip().upper()
        try:
            scores[cid] = (max(0, min(100, int(s.get("score")))), str(s.get("reason") or "")[:200])
        except (TypeError, ValueError):
            pass
    best, best_score, best_reason = None, -1, ""
    for i, c in enumerate(pool, start=1):
        score, reason = scores.get(f"C{i}", (0, "not scored by judge"))
        entry_candidates.append({
            "id": c.get("id"), "source": c.get("source"), "desc": c.get("desc"),
            "duration": c.get("duration"), "query": c.get("matched_query"),
            "score": score, "reason": reason,
        })
        if score > best_score:
            best, best_score, best_reason = c, score, reason
    return best, best_score, best_reason


def _search_pool(queries, min_duration, min_quality, errors_out):
    """Deduped candidate pool across queries/providers, relaxing the
    duration floor if it comes up completely dry. Returns (pool, relaxed).
    A thin first page pulls page 2 of the primary query as well -- exact
    matches are found by judging MORE candidates, not by settling."""
    pool, seen = [], set()

    def gather(min_dur, page=1, qs=None):
        for query in qs or queries:
            cands, errors = _automatch_search(query, min_dur, min_quality, page=page)
            errors_out.extend(errors)
            for c in cands:
                key = (c.get("source"), c.get("id"))
                if key not in seen:
                    seen.add(key)
                    c["matched_query"] = query
                    pool.append(c)

    gather(min_duration)
    if queries and len(pool) < AUTOMATCH_POOL // 2:
        gather(min_duration, page=2, qs=queries[:1])
    relaxed = False
    if not pool:
        relaxed = True
        gather(None)
    return pool[:AUTOMATCH_POOL], relaxed


def _revise_queries(brief, tried, result_descs):
    """The automatic-repair half of validate->repair: when every candidate
    scored below threshold, ask the LLM for different search phrasings given
    what the failed queries actually returned."""
    from groq_client import complete, strip_reasoning
    from semantic_timeline import _extract_json

    user = (
        brief
        + f"\n\nStock-video queries already tried: {', '.join(tried)}"
        + ("\nTheir results were about: " + "; ".join(d for d in result_descs if d) if result_descs else "")
        + "\n\nThose queries did not surface matching footage. Suggest 2 DIFFERENT concrete "
        "stock-video search queries (2-4 English words each, camera-visible things, "
        "synonyms or adjacent framings of the same event) more likely to find footage "
        'matching the narration. STRICT JSON only: {"queries":["...","..."]}'
    )
    reply = complete(
        [{"role": "system", "content": "You craft stock-video search queries. STRICT JSON only."},
         {"role": "user", "content": user}],
        STATE["groq_keys"], temperature=0.4, max_tokens=300, verbose=False,
    )
    data = _extract_json(strip_reasoning(reply))
    return [q.strip() for q in data.get("queries") or [] if isinstance(q, str) and q.strip()]


def _match_with_repair(brief, queries, min_duration, min_quality, candidates_out, errors_out, threshold):
    """search -> judge -> (while nothing passes) revise queries and try
    again, up to two repair rounds -> finally a still-photo round (photos
    carry real alt-text and often depict the exact narration nouns; step3
    renders them with a Ken Burns zoom). Best overall wins.
    Returns (best, score, reason, relaxed)."""
    pool, relaxed = _search_pool(queries, min_duration, min_quality, errors_out)
    best, score, reason = None, -1, ""
    if pool:
        try:
            best, score, reason = _pick_best(brief, pool, candidates_out)
        except Exception as e:
            errors_out.append(f"Scoring failed: {e}")
            return None, -1, "", relaxed

    tried = {q.lower() for q in queries}
    round_queries = list(queries)
    for repair_round in (1, 2):
        if best is not None and score >= threshold:
            return best, score, reason, relaxed
        try:
            revised = _revise_queries(brief, sorted(tried), [c.get("desc") for c in pool[:6]])
        except Exception as e:
            errors_out.append(f"Query revision failed: {e}")
            break
        revised = [q for q in revised if q.lower() not in tried][:2]
        if not revised:
            break
        tried.update(q.lower() for q in revised)
        round_queries = revised
        errors_out.append(f"Repair round {repair_round} tried revised queries: {', '.join(revised)}")
        pool2, relaxed2 = _search_pool(revised, min_duration, min_quality, errors_out)
        pool = pool2 or pool
        if pool2:
            try:
                best2, score2, reason2 = _pick_best(brief, pool2, candidates_out)
            except Exception as e:
                errors_out.append(f"Scoring failed on repair round: {e}")
                break
            if score2 > score:
                best, score, reason, relaxed = best2, score2, reason2, relaxed or relaxed2

    if best is None or score < threshold:
        # Photo fallback: an exact still beats an approximate video.
        photos = _photo_pool(sorted(tried), errors_out)
        if photos:
            errors_out.append(f"Photo round judged {len(photos)} still photo(s).")
            photo_brief = brief + (
                "\nNote: these candidates are STILL PHOTOS (rendered as a slow "
                "Ken Burns zoom). Judge only whether the image depicts the exact "
                "narration content; do not penalize for being a photo."
            )
            try:
                pbest, pscore, preason = _pick_best(photo_brief, photos, candidates_out)
                if pscore > score:
                    best, score, reason = pbest, pscore, f"[photo] {preason}"
            except Exception as e:
                errors_out.append(f"Scoring failed on photo round: {e}")

    return best, score, reason, relaxed


def _automatch_shots(scene, entry, min_quality, threshold):
    """Per-shot auto-match for a multi-shot scene: each shot gets its own
    search + judging against ITS exact words, and the scene is saved as a
    multi-clip selection in shot order -- step3 then cuts each clip at the
    shot's exact narration boundary. All shots must pass or nothing is
    saved (a half-matched scene would silently misalign the later shots)."""
    shots = scene.get("shots") or []
    picks = []
    entry["shots"] = []
    for shot in shots:
        shot_entry = {"text": shot.get("text"), "query": shot.get("query"),
                      "duration": round(shot.get("duration", 0), 2),
                      "status": "FAIL", "score": None, "chosen": None, "reason": None}
        entry["shots"].append(shot_entry)
        queries = [q for q in (shot.get("query"), scene.get("query"), (scene.get("queries") or [None])[0]) if q]
        queries = list(dict.fromkeys(queries))  # dedupe, keep order
        min_duration = max(1, int(shot.get("duration", 0) + 0.999))
        best, score, reason, _relaxed = _match_with_repair(
            _shot_brief(scene, shot), queries[:3], min_duration, min_quality,
            entry["candidates"], entry["failure_reasons"], threshold,
        )
        shot_entry["score"] = score if score >= 0 else None
        if best is None or score < threshold:
            shot_entry["reason"] = (
                f"Best candidate scored {score} (threshold {threshold})." if score >= 0
                else f"No usable search results for: {', '.join(queries[:2])}"
            )
            continue
        shot_entry["status"] = "PASS"
        shot_entry["chosen"] = {"id": best.get("id"), "source": best.get("source"), "desc": best.get("desc")}
        shot_entry["reason"] = reason
        picks.append((shot, best, score))

    failed = [se for se in entry["shots"] if se["status"] != "PASS"]
    if failed:
        entry["failure_reasons"].append(
            f"{len(failed)} of {len(shots)} shots had no candidate above threshold -- "
            f"nothing saved (a partial multi-pick would misalign the other shots). "
            f"Pick this scene manually (multi mode) or re-run."
        )
        entry["score"] = min((se["score"] for se in entry["shots"] if se["score"] is not None), default=None)
        return entry

    with STATE["lock"]:
        remove_clips_for_scene(scene["index"])
        items = []
        for j, (shot, best, score) in enumerate(picks):
            dest = register_asset(scene["index"], best, part=j + 1)
            item = multi_item_record(best, dest, shot.get("duration", 0))
            item["match_score"] = score
            items.append(item)
        selections = load_selections()
        selections[str(scene["index"])] = {
            "scene_index": scene["index"],
            "type": "multi",
            "items": items,
            "query": scene.get("query"),
            "auto_matched": True,
            "match_score": min(score for _, _, score in picks),
        }
        save_selections(selections)
    entry["status"] = "PASS"
    entry["score"] = min(score for _, _, score in picks)
    entry["chosen"] = {
        "multi": True,
        "files": [it["file"] for it in items],
        "descs": [se["chosen"]["desc"] for se in entry["shots"]],
    }
    return entry


def automatch_scene(scene, min_quality=None, threshold=AUTOMATCH_THRESHOLD):
    """Search + score + (maybe) select the best clip for one scene.
    A multi-shot scene (from the semantic timeline) is matched shot by
    shot and saved as a multi-clip selection with shot-exact timing; a
    single-visual scene gets one clip. Returns the report entry; on PASS
    the selection is saved exactly as a human pick would be."""
    entry = {
        "scene_index": scene["index"],
        "script": scene["text"],
        "voice_start": round(scene["start"], 2),
        "voice_end": round(scene["end"], 2),
        "duration": round(scene["duration"], 2),
        "status": "FAIL",
        "score": None,
        "chosen": None,
        "candidates": [],
        "failure_reasons": [],
    }
    if not STATE.get("groq_keys"):
        entry["failure_reasons"].append("No Groq API key -- semantic scoring unavailable.")
        return entry

    if len(scene.get("shots") or []) >= 2:
        return _automatch_shots(scene, entry, min_quality, threshold)

    queries = [q for q in (scene.get("queries") or []) if q] or [scene.get("query") or ""]
    min_duration = max(1, int(scene["duration"] + 0.999))
    best, best_score, best_reason, duration_relaxed = _match_with_repair(
        _scene_brief(scene), queries[:3], min_duration, min_quality,
        entry["candidates"], entry["failure_reasons"], threshold,
    )
    entry["candidates"].sort(key=lambda c: -c["score"])
    entry["score"] = best_score if best_score >= 0 else None
    if best is None and best_score < 0:
        entry["failure_reasons"].append(f"No usable search results for: {', '.join(queries[:2])}")
        return entry

    if best is None or best_score < threshold:
        entry["failure_reasons"].append(
            f"Best candidate scored {best_score} (threshold {threshold}) -- "
            f"pick this scene manually; the scored list is in the report."
        )
        if duration_relaxed:
            entry["failure_reasons"].append(
                f"Note: no clip met the {min_duration}s minimum duration; shorter clips were considered."
            )
        return entry

    with STATE["lock"]:
        remove_clips_for_scene(scene["index"])
        dest = register_asset(scene["index"], best)
        selections = load_selections()
        record = selection_record(scene["index"], best, dest, best.get("matched_query"))
        record["match_score"] = best_score
        record["match_reason"] = best_reason
        record["auto_matched"] = True
        selections[str(scene["index"])] = record
        save_selections(selections)
    entry["status"] = "PASS"
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
                "automatch_available": bool(STATE.get("groq_keys")),
            })

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

        if route.startswith("/clips/"):
            name = os.path.basename(urllib.parse.unquote(route[len("/clips/"):]))
            path = STATE["clips_dir"] / name
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
            with STATE["lock"]:
                remove_clips_for_scene(scene_index)
                share_seconds = scene["duration"] / len(assets)
                items = []
                try:
                    for i, asset in enumerate(assets):
                        dest = register_asset(scene_index, asset, part=i + 1)
                        items.append(multi_item_record(asset, dest, share_seconds))
                except Exception as e:
                    return self._send_json({"error": f"Save failed: {e}"}, status=502)
                selections = load_selections()
                selections[str(scene_index)] = {
                    "scene_index": scene_index,
                    "type": "multi",
                    "items": items,
                    "query": body.get("query"),
                }
                save_selections(selections)
            return self._send_json({"ok": True, "count": len(items)})

        if route == "/api/automatch":
            # Semantic auto-match: one scene ({"index": N}) or every scene
            # without a pick yet ({"all": true}). Search -> LLM-score against
            # the exact script segment -> select above threshold; always
            # written to sync_report.json, PASS or FAIL.
            if not STATE.get("groq_keys"):
                return self._send_json(
                    {"error": "Auto-match needs a Groq API key (tools/groq_key.txt) for semantic scoring."},
                    status=400,
                )
            min_quality = body.get("min_quality")
            min_quality = min_quality if min_quality in QUALITY_TIERS else None
            try:
                threshold = max(0, min(100, int(body.get("threshold", AUTOMATCH_THRESHOLD))))
            except (TypeError, ValueError):
                threshold = AUTOMATCH_THRESHOLD
            if body.get("all"):
                selections = load_selections()
                targets = [s for s in STATE["scenes"] if str(s["index"]) not in selections]
            else:
                idx = int(body["index"])
                targets = [s for s in STATE["scenes"] if s["index"] == idx]
                if not targets:
                    return self._send_json({"error": "Unknown scene."}, status=400)
            entries = []
            for scene in targets:
                try:
                    entries.append(automatch_scene(scene, min_quality=min_quality, threshold=threshold))
                except Exception as e:
                    entries.append({
                        "scene_index": scene["index"],
                        "script": scene["text"],
                        "status": "FAIL",
                        "score": None,
                        "chosen": None,
                        "candidates": [],
                        "failure_reasons": [f"Auto-match crashed: {e}"],
                    })
            report_path = save_sync_report(entries)
            passed = sum(1 for e in entries if e["status"] == "PASS")
            return self._send_json({
                "ok": True,
                "matched": passed,
                "failed": len(entries) - passed,
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
  #localpreviewwrap { margin:12px 0; max-width:360px; }
  #localpreviewwrap img { width:100%; border-radius:8px; display:block; background:#000; }

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
  #automatchbtn.busy, #automatchall.busy { opacity:.5; pointer-events:none; }
  #automatchall { margin-top:8px; width:100%; font-size:12px; background:var(--panel2); color:var(--dim); }
  #automatchall:hover:not(:disabled) { border-color:var(--accent); color:var(--text); }
  .matchnote { margin-top:6px; font-size:12px; color:#8ce8b0; }
  .matchnote.fail { color:#ff9c9c; }
  /* ---------- responsive: tablets & phones ---------- */
  @media (max-width: 820px) {
    #app { flex-direction:column; height:auto; min-height:100vh; }
    #sidebar { width:100%; max-height:32vh; border-right:none; border-bottom:1px solid var(--line); }
    #main { padding:12px 12px 50px; }
    #grid { grid-template-columns:repeat(auto-fill,minmax(160px,1fr)); gap:10px; }
    #controls { flex-wrap:wrap; }
    #sourcetabs { overflow-x:auto; white-space:nowrap; display:flex; }
    .srctab { flex:none; }
  }
</style></head><body>
<div id="app">
  <div id="sidebar">
    <h1>Scenes</h1>
    <div id="progress"><span id="ptext">-</span><div id="bar"><div></div></div>
      <button id="resetall" title="Delete every downloaded clip and clear all selections">Reset all clips</button>
      <button id="automatchall" style="display:none" title="Auto-match every scene that has no clip yet: search widely, LLM-score every candidate against each scene's exact narration (with query-repair rounds and a still-photo fallback), select the best above the strictness bar. Scenes that fail are left for you, with reasons in sync_report.json">&#10024; Auto-match all unpicked</button>
      <select id="strictness" style="display:none; margin-top:6px; width:100%; font-size:11.5px"
        title="How exact an auto-matched clip must be. Strict/Exact only accept descriptions that explicitly state the subject AND action -- fewer scenes auto-fill, but what fills is right.">
        <option value="60" selected>Match strictness: balanced</option>
        <option value="75">Match strictness: strict (explicit subject+action)</option>
        <option value="85">Match strictness: exact only</option>
        <option value="50">Match strictness: relaxed</option>
      </select></div>
    <div id="flagged"></div>
    <div id="scenelist"></div>
  </div>
  <div id="main">
    <div class="meta" id="scenemeta">Loading...</div>
    <div id="semanticbrief" style="display:none"></div>

    <div id="sourcetabs">
      <button class="srctab active" data-src="pexels-video">Pexels video</button>
      <button class="srctab" data-src="pexels-photo">Pexels photo</button>
      <button class="srctab" data-src="pixabay-video">Pixabay video</button>
      <button class="srctab" data-src="pixabay-photo">Pixabay photo</button>
      <button class="srctab" data-src="coverr-video">Coverr video</button>
      <button class="srctab" data-src="local">Local file</button>
    </div>

    <div id="wlwords" class="wl"><span class="meta">Sentence &mdash; click a word to add it to or remove it from the search</span></div>
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
      <p><input type="file" id="localfile" accept="image/*"></p>
      <div id="localpreviewwrap"><img id="localpreview" style="display:none"></div>
      <button id="uselocalbtn" class="primary" disabled>Use this image</button>
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
  'local':         { provider: 'local',   media: 'photo', endpoint: null },
};
const PROVIDER_LABELS = { pexels: 'Pexels', pixabay: 'Pixabay', coverr: 'Coverr' };
let source = 'pexels-video'; // key into SOURCES
let localAsset = null;
let renderOffered = false; // ask at most once per page load

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

let matchNotes = {}; // scene index -> {ok, text} from the last auto-match

async function refreshSelections() {
  const d = await (await fetch('/api/scenes')).json();
  selections = d.selections;
  renderSidebar();
}

function automatchNote(e) {
  if (e.status === 'PASS') {
    return {ok:true, text:`Auto-matched (score ${e.score}/100): "${(e.chosen&&e.chosen.desc)||''}" -- ` +
      `${(e.candidates||[]).length} candidates scored. Override it any time by picking another clip.`};
  }
  const why = (e.failure_reasons||[]).join(' ') || 'no candidate scored above the threshold.';
  return {ok:false, text:`Auto-match: no clip accepted -- ${why}`};
}

async function runAutomatch(all) {
  const btn = all ? $('automatchall') : $('automatchbtn');
  btn.classList.add('busy');
  $('status').className = '';
  $('status').textContent = all
    ? 'Auto-matching every unpicked scene (search + LLM scoring, a few seconds per scene)...'
    : 'Auto-matching: searching providers and scoring candidates against this scene\'s narration...';
  try {
    const body = all ? {all:true} : {index: scenes[cur].index};
    const mq = $('minquality').value;
    if (mq) body.min_quality = mq;
    body.threshold = parseInt($('strictness').value, 10) || 60;
    const r = await fetch('/api/automatch', {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)
    });
    const d = await r.json();
    if (d.error) { $('status').className = 'err'; $('status').textContent = d.error; return; }
    for (const e of d.entries || []) matchNotes[e.scene_index] = automatchNote(e);
    await refreshSelections();
    renderBrief(scenes[cur]);
    $('status').className = d.failed ? 'err' : '';
    $('status').textContent = all
      ? `Auto-match done: ${d.matched} scene(s) matched, ${d.failed} left for manual picking ` +
        `(scores and reasons in ${d.report}).`
      : (d.entries[0].status === 'PASS'
          ? `Scene ${d.entries[0].scene_index} matched with score ${d.entries[0].score}/100 -- see the note above.`
          : `No candidate scored above the threshold -- reasons shown above; pick manually or adjust the tags.`);
  } catch (err) {
    $('status').className = 'err'; $('status').textContent = 'Auto-match failed: ' + err;
  } finally {
    btn.classList.remove('busy');
  }
}

async function boot() {
  const d = await (await fetch('/api/scenes')).json();
  scenes = d.scenes; selections = d.selections;
  highlight = new Set(d.highlight || []);
  automatchAvailable = !!d.automatch_available;
  if (automatchAvailable) {
    $('automatchbtn').style.display = '';
    $('automatchall').style.display = '';
    $('strictness').style.display = '';
  }
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
  const kind = it.source === 'local' ? 'local' : (it.type === 'video' ? 'video' : 'photo');
  return {
    kind, id: it.pexels_id, source: it.source || 'pexels',
    download_url: it.download_url, width: it.width, height: it.height,
    author: it.author, author_url: it.author_url, page_url: it.page_url,
    local_name: it.file, __uid: kind === 'local' ? ++multiUidCounter : undefined,
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
  $('scenemeta').textContent =
    `Scene ${String(s.index).padStart(3,'0')} of ${scenes.length}  .  ${fmt(s.start)}-${fmt(s.end)}  .  needs ${s.duration.toFixed(1)}s`;
  renderBrief(s);
  $('q').value = '';
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

function renderMultiBar() {
  const s = scenes[cur];
  $('multipicks').innerHTML = multiPicks.map(p => {
    const label = p.kind === 'local' ? 'Local image'
      : `${PROVIDER_LABELS[p.source] || 'Pexels'} ${p.kind === 'photo' ? 'photo' : 'video'} #${p.id}`;
    return `<span class="multichip">${esc(label)}<button title="Remove" data-k="${esc(assetKey(p))}">&times;</button></span>`;
  }).join('');
  $('multipicks').querySelectorAll('button').forEach(b => b.onclick = () => removeMultiPick(b.dataset.k));
  const n = multiPicks.length;
  $('multisplit').textContent = n > 0
    ? `${n} clip${n === 1 ? '' : 's'} selected — ${(s.duration / n).toFixed(1)}s each of the ${s.duration.toFixed(1)}s scene`
    : 'Add 2 or more clips below, then save -- the scene’s time splits evenly between them.';
  $('multisave').disabled = n < 1;
}

async function saveMultiPicks() {
  const s = scenes[cur];
  if (multiPicks.length < 1) return;
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
  $('status').textContent = `Saved ${d.count} clip(s), time split across the scene.`;
  multiPicks = [];
  renderMultiBar();
  renderSidebar(); renderGrid();
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
  // Auto-advance to the next scene still missing a clip -- the whole job is
  // hundreds of these, so every saved click matters.
  const nxt = scenes.findIndex((sc,i) => i > cur && !selections[sc.index]);
  if (nxt !== -1) setTimeout(() => go(nxt), 350);
  else setTimeout(maybeOfferRender, 350);
}

// Fires once, right when the last scene gets its clip. Confirms with the
// user before kicking off step 3 automatically -- rendering isn't cheap, so
// this shouldn't happen silently or repeat on every subsequent visit.
async function maybeOfferRender() {
  if (renderOffered) return;
  const n = Object.keys(selections).length;
  if (n !== scenes.length || highlight.size !== 0) return;
  renderOffered = true;
  const go3 = confirm(`All ${scenes.length} scenes have a clip.\n\nRender the final video now (step 3)? This can take a while.`);
  if (!go3) return;
  $('status').className = ''; $('status').textContent = 'Starting render...';
  await fetch('/api/finish', { method: 'POST' });
  $('done').style.display = 'none';
  $('finished').style.display = 'block';
  $('finished').scrollIntoView({ behavior: 'smooth' });
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

function refreshBulkLabels() {
  renderGrid();
  if (localAsset) $('uselocalbtn').textContent = localBtnLabel();
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

// Source tabs: Pexels video/photo, Pixabay video/photo, Coverr video, or a
// local image file (see SOURCES above). Only one is active at a time;
// switching re-runs the search (or, for local, just shows the upload panel --
// there's nothing to search).
const SEARCH_UI_IDS = ['wlwords', 'scenewords', 'wltags', 'tagbox', 'opts', 'grid', 'pager'];
function setSource(next) {
  if (source === next) return;
  source = next;
  document.querySelectorAll('.srctab').forEach(b => b.classList.toggle('active', b.dataset.src === source));
  const isLocal = source === 'local';
  SEARCH_UI_IDS.forEach(id => { $(id).style.display = isLocal ? 'none' : ''; });
  $('localpanel').classList.toggle('on', isLocal);
  $('longenoughwrap').style.display = SOURCES[source].media === 'video' ? '' : 'none';
  if (isLocal) {
    $('status').className = ''; $('status').textContent = '';
  } else {
    page = 1; search();
  }
}
document.querySelectorAll('.srctab').forEach(b => b.onclick = () => setSource(b.dataset.src));

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
  if (e.target.tagName === 'INPUT') return;
  if (e.key === 'ArrowRight') go(cur + 1);
  if (e.key === 'ArrowLeft') go(cur - 1);
});
boot();
</script></body></html>
"""


def run(project, api_key=None, pixabay_api_key=None, coverr_api_key=None,
        port=8000, no_browser=False, highlight=None):
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
        "lock": threading.Lock(),
        "rate_remaining": None,
        # The narration panel shows the raw script text, so it has to follow
        # the script's own direction -- Urdu reads unusably as LTR.
        "rtl": caption_style(Path(project).parts[0])["rtl"],
        "finish_requested": False,
        "highlight": set(highlight or []),
        "orientation": orientation,
    })

    # Groq keys power the semantic auto-matcher (/api/automatch). Optional:
    # without one the picker works exactly as before, just without the
    # auto-match button.
    try:
        from groq_client import load_keys as _load_groq_keys

        STATE["groq_keys"] = _load_groq_keys()
    except Exception:
        STATE["groq_keys"] = None

    done = len(load_selections())
    url = f"http://localhost:{port}/"
    print(f"{len(scenes)} scenes, {done} already have a clip.")
    if orientation == "portrait":
        print("Vertical project detected (render.json) -- searching Pexels for portrait clips.")
    optional = [name for name, key in (("Pixabay", pixabay_key), ("Coverr", coverr_key)) if not key]
    if optional:
        print(f"No API key configured yet for: {', '.join(optional)} -- those tabs will show a "
              f"setup message until you add one (see the module docstring).")
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
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open the browser")
    args = parser.parse_args()

    render_now = run(
        args.project, api_key=args.api_key,
        pixabay_api_key=args.pixabay_api_key, coverr_api_key=args.coverr_api_key,
        port=args.port, no_browser=args.no_browser,
    )
    if render_now:
        import step3_render_video
        step3_render_video.run(args.project)


if __name__ == "__main__":
    main()
