"""
Pick / arrange background music for a project from YouTube.

Usage:
    python step_music_picker.py <lang>/<slug> [--port 8010] [--no-browser]

e.g. python step_music_picker.py en/crumbs

Serves a local page: paste a YouTube URL and click Add.

- A single video's URL downloads and previews the audio right there, and
  queues it.
- A channel or playlist URL (e.g. the YouTube Audio Library's channel, or
  any "no copyright music" channel's Videos tab) instead lists every track
  in it -- title, duration, an inline filter box to search by name -- with
  its own "Add" button per track, so you can browse hundreds of tracks and
  only download the ones you actually queue.

Add as many tracks as you like and reorder them with the up/down arrows
into the play order you want.

"Save to project" writes the queued tracks into projects/<lang>/<slug>/ as:
  - bg.mp3                    one track queued
  - bg-1.mp3, bg-2.mp3, ...   two or more, in queue order

step3_render_video.py picks these up automatically -- no --music flag
needed (see its own docstring for how it sequences/loops them under the
narration with ducking).

Downloads go through yt-dlp + ffmpeg (same tooling as step0's --yt-link),
cached under the project's .music_cache/ by video ID so re-adding or
reordering the same track never re-downloads it. The queue itself is saved
continuously to music_selections.json, so closing the tab or Ctrl+C-ing the
server never loses your picks -- re-run the same command to resume.
"""
import argparse
import json
import mimetypes
import os
import re
import shutil
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from common import load_json, probe_duration, save_json
from youtube_transcript import COOKIES_FILE, YouTubeTranscriptError, get_video_id

PROJECTS_DIR = Path(__file__).parent.parent / "projects"

# Auto-browsed the moment the page loads (see /api/default-channel below) so
# you land on a filterable, playable list of tracks with nothing to paste --
# override with --channel, or pass --channel '' to start on an empty picker.
DEFAULT_CHANNEL_URL = "https://www.youtube.com/c/audiolibrary-channel"

# Same cookies.txt (and same reason for needing it -- YouTube blocking
# anonymous/datacenter IPs) as youtube_transcript.py's --yt-link fallback,
# just worded for an audio download instead of a transcript fetch.
COOKIES_HELP = (
    "Free fix: log into YouTube in your normal browser, export its cookies "
    "with a browser extension (e.g. \"Get cookies.txt LOCALLY\" for "
    "Chrome/Firefox), and save the file as:\n"
    f"  {COOKIES_FILE}\n"
    "Then run the command again -- yt-dlp will use those cookies to download "
    "as a logged-in browser instead of an anonymous IP."
)

# ThreadingHTTPServer, so shared state lives at module level (same pattern
# as step2_pick_clips.py's STATE).
STATE = {}


class MusicDownloadError(RuntimeError):
    pass


def _canonical_url(video_id):
    return f"https://www.youtube.com/watch?v={video_id}"


# Matches a bare channel/handle root with no tab segment yet, e.g.
# youtube.com/channel/UCxxxx, /c/somename, or /@somehandle -- yt-dlp accepts
# these directly, but a bare root is ambiguous about which tab (Videos,
# Shorts, Live...) to list, so _normalize_channel_url() pins it to /videos.
_CHANNEL_ROOT_RE = re.compile(
    r"^(https?://(?:www\.|music\.)?youtube\.com/(?:channel/[^/?#]+|c/[^/?#]+|@[^/?#]+))/?(?:[?#].*)?$"
)


def _normalize_channel_url(url):
    """music.youtube.com links (e.g. the YouTube Audio Library's own channel
    URL) use the same IDs as youtube.com but aren't reliably supported by
    yt-dlp's extractor -- rewritten to the www.youtube.com equivalent. A
    bare channel/handle root is pinned to its /videos tab so listing always
    returns a flat list of uploads instead of a multi-tab structure."""
    url = url.strip()
    url = re.sub(r"^https?://music\.youtube\.com", "https://www.youtube.com", url, flags=re.IGNORECASE)
    m = _CHANNEL_ROOT_RE.match(url)
    if m:
        return m.group(1) + "/videos"
    return url


def classify_url(url):
    """Figure out whether `url` is a single video or a channel/playlist,
    without downloading anything.

    Returns ("video", video_id) for a single video, or ("list", {"title":
    ..., "entries": [...]}) for a channel/playlist, where each entry is
    {"video_id", "title", "duration", "url"} (duration may be None -- flat
    listing doesn't always have it). Entries are capped at 200 -- plenty for
    browsing a music channel, and keeps a huge channel's listing fast.
    """
    try:
        import yt_dlp
    except ImportError as e:
        raise MusicDownloadError("yt-dlp is not installed. Free fix: pip install yt-dlp") from e

    normalized = _normalize_channel_url(url)
    ydl_opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        "playlistend": 200,
    }
    if COOKIES_FILE.exists():
        ydl_opts["cookiefile"] = str(COOKIES_FILE)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(normalized, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise MusicDownloadError(f"Could not read {url}: {e}\n\n{COOKIES_HELP}") from e

    if info.get("_type") == "playlist" or "entries" in info:
        entries = []
        for e in info.get("entries") or []:
            if not e or not e.get("id"):
                continue
            entries.append({
                "video_id": e["id"],
                "title": e.get("title") or e["id"],
                "duration": e.get("duration"),
                "url": _canonical_url(e["id"]),
            })
        return "list", {"title": info.get("title") or normalized, "entries": entries}

    try:
        video_id = get_video_id(url)
    except YouTubeTranscriptError:
        video_id = info.get("id")
    return "video", video_id


def download_audio(url, cache_dir, on_progress=None):
    """Download `url`'s audio as an mp3 into cache_dir, named <video_id>.mp3
    (plus a <video_id>.json sidecar with its title/source URL). A second
    call for the same video ID is served straight from that cache -- no
    re-download, even across separate runs of this tool.

    `on_progress`, if given, is called with a float 0-100 as yt-dlp reports
    download progress (skipped entirely for a cache hit, since there's
    nothing to wait on).

    Returns {"video_id", "title", "url", "duration"} -- duration is measured
    from the actual downloaded file via ffprobe, not trusted from YouTube's
    own metadata.
    """
    try:
        import yt_dlp
    except ImportError as e:
        raise MusicDownloadError("yt-dlp is not installed. Free fix: pip install yt-dlp") from e

    try:
        video_id = get_video_id(url)
    except YouTubeTranscriptError as e:
        raise MusicDownloadError(str(e)) from e

    mp3_path = cache_dir / f"{video_id}.mp3"
    meta_path = cache_dir / f"{video_id}.json"
    if mp3_path.exists() and meta_path.exists():
        meta = load_json(meta_path)
        return {
            "video_id": video_id, "title": meta["title"], "url": meta["url"],
            "duration": probe_duration(mp3_path),
        }

    def _progress_hook(d):
        if not on_progress or d.get("status") != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        downloaded = d.get("downloaded_bytes")
        if total and downloaded is not None:
            on_progress(round(downloaded / total * 100, 1))

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(cache_dir / "%(id)s.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [_progress_hook],
    }
    if COOKIES_FILE.exists():
        ydl_opts["cookiefile"] = str(COOKIES_FILE)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(_canonical_url(video_id), download=True)
    except yt_dlp.utils.DownloadError as e:
        raise MusicDownloadError(
            f"Could not download audio for {url}: {e}\n\n{COOKIES_HELP}"
        ) from e

    if not mp3_path.exists():
        raise MusicDownloadError(
            f"yt-dlp finished but produced no mp3 for {url} -- this video may have no audio track."
        )

    title = info.get("title") or video_id
    canonical = _canonical_url(video_id)
    save_json({"title": title, "url": canonical}, meta_path)
    return {"video_id": video_id, "title": title, "url": canonical, "duration": probe_duration(mp3_path)}


def queue_path(project_dir):
    return project_dir / "music_selections.json"


def load_queue(project_dir):
    p = queue_path(project_dir)
    return load_json(p) if p.exists() else []


def save_queue(project_dir, queue):
    save_json(queue, queue_path(project_dir))


def existing_bg_files(project_dir):
    """Every bg.mp3 / bg-N.mp3 file currently sitting in the project
    folder, in play order."""
    files = []
    single = project_dir / "bg.mp3"
    if single.exists():
        files.append(single)
    i = 1
    while (project_dir / f"bg-{i}.mp3").exists():
        files.append(project_dir / f"bg-{i}.mp3")
        i += 1
    return files


def materialize_queue(project_dir, cache_dir, queue):
    """Write the queue's cached mp3s into the project folder as bg.mp3 (one
    track) or bg-1.mp3, bg-2.mp3, ... (several, in queue order) -- clearing
    out whatever bg*.mp3 files were already there first, so saving a
    shorter queue than last time doesn't leave stale extra tracks behind
    for step3 to pick up. Returns the list of filenames written."""
    for f in existing_bg_files(project_dir):
        f.unlink()
    written = []
    if len(queue) == 1:
        dest = project_dir / "bg.mp3"
        shutil.copyfile(cache_dir / f"{queue[0]['video_id']}.mp3", dest)
        written.append(dest.name)
    else:
        for i, track in enumerate(queue, start=1):
            dest = project_dir / f"bg-{i}.mp3"
            shutil.copyfile(cache_dir / f"{track['video_id']}.mp3", dest)
            written.append(dest.name)
    return written


def _start_download(url, video_id):
    """Kick off `video_id`'s download on a background thread and return
    immediately -- STATE["progress"][video_id] is updated as it goes so
    /api/progress can be polled for a live percentage instead of the
    browser just hanging on a blocked request until it's done."""
    def worker():
        def on_progress(pct):
            with STATE["lock"]:
                STATE["progress"][video_id] = {"status": "downloading", "percent": pct}

        try:
            track = download_audio(url, STATE["cache_dir"], on_progress=on_progress)
        except MusicDownloadError as e:
            with STATE["lock"]:
                STATE["progress"][video_id] = {"status": "error", "error": str(e)}
            return

        with STATE["lock"]:
            if any(t["video_id"] == track["video_id"] for t in STATE["queue"]):
                STATE["progress"][video_id] = {
                    "status": "error",
                    "error": f"“{track['title']}” is already in the queue.",
                }
                return
            STATE["queue"].append(track)
            save_queue(STATE["project_dir"], STATE["queue"])
            STATE["progress"][video_id] = {"status": "done", "percent": 100, "track": track}

    threading.Thread(target=worker, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):  # quieter console
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Lets the pipeline dashboard (a different local port) read
        # /api/queue to check WHICH project this picker is serving before
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
        route = urllib.parse.urlparse(self.path).path

        if route == "/":
            return self._send_html(PAGE_HTML)

        if route == "/api/queue":
            return self._send_json({
                "project": STATE["project"],
                "queue": STATE["queue"],
                "existing_files": [f.name for f in existing_bg_files(STATE["project_dir"])],
            })

        if route == "/api/default-channel":
            channel_url = STATE.get("channel_url")
            if not channel_url:
                return self._send_json({"kind": None})
            try:
                kind, result = classify_url(channel_url)
            except MusicDownloadError as e:
                return self._send_json({"error": str(e)}, status=502)
            if kind != "list":
                # --channel pointed at a single video, not a channel/playlist --
                # nothing to auto-browse, but not worth failing startup over.
                return self._send_json({"kind": None})
            return self._send_json({"kind": "list", "title": result["title"], "entries": result["entries"]})

        if route == "/api/progress":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            video_id = (qs.get("video_id") or [None])[0]
            with STATE["lock"]:
                p = dict(STATE["progress"].get(video_id) or {"status": "unknown"})
                if p.get("status") == "done":
                    p["queue"] = STATE["queue"]
            return self._send_json(p)

        if route.startswith("/preview/"):
            name = os.path.basename(urllib.parse.unquote(route[len("/preview/"):]))
            path = STATE["cache_dir"] / name
            if not path.exists():
                self.send_error(404)
                return
            data = path.read_bytes()
            content_type = mimetypes.guess_type(name)[0] or "audio/mpeg"
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

        if route == "/api/add":
            url = (body.get("url") or "").strip()
            if not url:
                return self._send_json({"error": "Paste a YouTube URL first."}, status=400)

            # A channel/playlist URL doesn't get downloaded itself -- it's
            # listed (title/duration per video, nothing fetched) so the
            # browser can show a browsable/filterable panel; each entry in
            # it comes back through this same endpoint as an ordinary single
            # video URL when its own "Add" is clicked.
            video_id = None
            if not body.get("skip_classify"):
                # get_video_id is a local URL parse (no network) that
                # succeeds for any ordinary single-video URL shape --
                # skipping straight past classify_url's network round trip
                # for the common case. It only fails for channel/playlist/
                # handle URLs, which classify_url still has to resolve.
                try:
                    video_id = get_video_id(url)
                except YouTubeTranscriptError:
                    try:
                        kind, result = classify_url(url)
                    except MusicDownloadError as e:
                        return self._send_json({"error": str(e)}, status=502)
                    if kind == "list":
                        if not result["entries"]:
                            return self._send_json(
                                {"error": "No videos found at that channel/playlist URL."}, status=400
                            )
                        return self._send_json({"ok": True, "kind": "list", "title": result["title"], "entries": result["entries"]})
                    video_id = result
            else:
                try:
                    video_id = get_video_id(url)
                except YouTubeTranscriptError as e:
                    return self._send_json({"error": str(e)}, status=400)

            with STATE["lock"]:
                if any(t["video_id"] == video_id for t in STATE["queue"]):
                    return self._send_json({"error": "That track is already in the queue."}, status=400)
                cache_dir = STATE["cache_dir"]
                cached = (cache_dir / f"{video_id}.mp3").exists() and (cache_dir / f"{video_id}.json").exists()

            if cached:
                # Already on disk -- instant, so no point making the
                # browser poll a progress bar for it.
                with STATE["lock"]:
                    try:
                        track = download_audio(url, cache_dir)
                    except MusicDownloadError as e:
                        return self._send_json({"error": str(e)}, status=502)
                    if any(t["video_id"] == track["video_id"] for t in STATE["queue"]):
                        return self._send_json(
                            {"error": f"“{track['title']}” is already in the queue."}, status=400
                        )
                    STATE["queue"].append(track)
                    save_queue(STATE["project_dir"], STATE["queue"])
                return self._send_json({"ok": True, "kind": "video", "track": track, "queue": STATE["queue"]})

            with STATE["lock"]:
                STATE["progress"][video_id] = {"status": "downloading", "percent": None}
            _start_download(url, video_id)
            return self._send_json({"ok": True, "kind": "pending", "video_id": video_id})

        if route == "/api/reorder":
            order = body.get("order") or []
            with STATE["lock"]:
                by_id = {t["video_id"]: t for t in STATE["queue"]}
                if set(order) != set(by_id) or len(order) != len(by_id):
                    return self._send_json({"error": "Reorder list doesn't match the current queue."}, status=400)
                STATE["queue"] = [by_id[vid] for vid in order]
                save_queue(STATE["project_dir"], STATE["queue"])
            return self._send_json({"ok": True, "queue": STATE["queue"]})

        if route == "/api/remove":
            video_id = body.get("video_id")
            with STATE["lock"]:
                STATE["queue"] = [t for t in STATE["queue"] if t["video_id"] != video_id]
                save_queue(STATE["project_dir"], STATE["queue"])
            return self._send_json({"ok": True, "queue": STATE["queue"]})

        if route == "/api/save":
            with STATE["lock"]:
                if not STATE["queue"]:
                    return self._send_json({"error": "Add at least one track before saving."}, status=400)
                try:
                    written = materialize_queue(STATE["project_dir"], STATE["cache_dir"], STATE["queue"])
                except OSError as e:
                    return self._send_json({"error": f"Save failed: {e}"}, status=502)
            return self._send_json({"ok": True, "files": written})

        self.send_error(404)


PAGE_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Background Music Picker</title>
<style>
  :root {
    --bg:#12141a; --panel:#1a1d26; --panel2:#222634; --line:#2e3342;
    --text:#e8eaf0; --dim:#9aa1b4; --accent:#f5c542; --ok:#3ecf8e; --err:#ff8f8f;
  }
  /* Light theme -- activated by ?theme=light (the dashboard passes its own
     mode when embedding this picker so both always match). */
  body.light {
    --bg:#f3f5f8; --panel:#ffffff; --panel2:#eef1f6; --line:#dfe4ec;
    --text:#16202c; --dim:#5b6a7e; --accent:#eab308; --ok:#16a34a; --err:#b91c1c;
  }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.5 system-ui,Segoe UI,sans-serif; background:var(--bg); color:var(--text); }
  #wrap { max-width:820px; margin:0 auto; padding:28px 24px 60px; }
  h1 { font-size:20px; margin:0 0 4px; }
  .meta { color:var(--dim); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }
  #addbar { display:flex; gap:10px; margin:20px 0 6px; }
  input[type=text] { flex:1; background:var(--panel); border:1px solid var(--line); color:var(--text);
                      padding:10px 12px; border-radius:8px; font-size:14px; }
  input[type=text]:focus { outline:none; border-color:var(--accent); }
  button { background:var(--panel2); color:var(--text); border:1px solid var(--line);
           padding:9px 16px; border-radius:8px; cursor:pointer; font-size:13px; }
  button:hover:not(:disabled) { border-color:var(--accent); }
  button:disabled { opacity:.4; cursor:default; }
  button.primary { background:var(--accent); color:#1a1200; border-color:var(--accent); font-weight:600; }
  #status { margin:10px 0; font-size:13px; color:var(--dim); min-height:18px; white-space:pre-wrap; }
  #status.err { color:var(--err); }
  #status.ok { color:var(--ok); }
  #queue { list-style:none; margin:18px 0; padding:0; display:flex; flex-direction:column; gap:10px; }
  .track { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px 14px;
           display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
  .track .num { color:var(--dim); font-variant-numeric:tabular-nums; font-size:13px; width:20px; flex:none; }
  .track audio { height:32px; width:220px; flex:none; }
  .track .info { flex:1; min-width:140px; }
  .track .title { font-size:14px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .track .dur { color:var(--dim); font-size:12px; }
  .track .btns { display:flex; gap:4px; flex:none; }
  .track .btns button { padding:5px 9px; font-size:12px; }
  #empty { color:var(--dim); font-size:13px; padding:20px 0; }
  #savebar { display:flex; gap:10px; align-items:center; margin-top:10px; flex-wrap:wrap; }
  #filesnote { color:var(--dim); font-size:12px; }

  #browsepanel { display:none; margin:18px 0; background:var(--panel); border:1px solid var(--line);
                 border-radius:10px; padding:14px 16px; }
  #browsehead { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:10px; }
  #browsetitle { font-size:14px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  #browsecount { color:var(--dim); font-size:12px; flex:none; }
  #browsefilter { width:100%; margin-bottom:10px; }
  #browselist { list-style:none; margin:0; padding:0; max-height:420px; overflow-y:auto;
                display:flex; flex-direction:column; gap:2px; }
  .browse-row { display:flex; align-items:center; gap:10px; padding:7px 4px; border-radius:6px; flex-wrap:wrap; }
  .browse-row:hover { background:var(--panel2); }
  .browse-row .title { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:13px; }
  .browse-row .dur { color:var(--dim); font-size:12px; width:44px; text-align:right; flex:none; }
  .browse-row button { padding:4px 12px; font-size:12px; flex:none; }
  .browse-row .addrowbtn { min-width:52px; }
  .browse-row.added .addrowbtn { color:var(--ok); border-color:var(--ok); }
  .browse-row .playbtn { padding:4px 9px; }
  .browse-row .preview { flex-basis:100%; padding:4px 0 2px 34px; }
  .browse-row .preview iframe { width:100%; max-width:480px; height:80px; border:0; border-radius:6px; display:block; }
  /* ---------- responsive: phones ---------- */
  @media (max-width: 640px) {
    #wrap { padding:16px 12px 50px; }
  }
</style>
</head>
<body>
<div id="wrap">
  <h1>Background music</h1>
  <div class="meta" id="projectname"></div>

  <div id="addbar">
    <input type="text" id="url" placeholder="Paste a YouTube URL -- a single track, or a channel/playlist (e.g. the YouTube Audio Library) to browse...">
    <button id="addbtn" class="primary">Add</button>
  </div>
  <div id="status"></div>

  <div id="browsepanel">
    <div id="browsehead">
      <div id="browsetitle"></div>
      <div id="browsecount"></div>
    </div>
    <input type="text" id="browsefilter" placeholder="Filter by title...">
    <ul id="browselist"></ul>
  </div>

  <ul id="queue"></ul>
  <div id="empty" style="display:none">No tracks queued yet -- paste a YouTube URL above to add one.</div>

  <div id="savebar">
    <button id="savebtn" class="primary">Save to project</button>
    <span id="filesnote"></span>
  </div>
</div>
<script>
// Follow the dashboard's theme when embedded there (?theme=light).
if (new URLSearchParams(location.search).get('theme') === 'light') document.body.classList.add('light');
const $ = id => document.getElementById(id);
let queue = [];
let browseEntries = []; // current channel/playlist listing, unfiltered
let browsePanelVisible = false;
let previewVideoId = null; // browse-row currently showing its inline YouTube preview, if any

const fmtDur = s => s == null ? '--:--' : (s => `${Math.floor(s/60)}:${String(Math.round(s)%60).padStart(2,'0')}`)(Math.round(s));
const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');

async function loadQueue() {
  const r = await fetch('/api/queue');
  const d = await r.json();
  $('projectname').textContent = d.project;
  queue = d.queue;
  render();
}

function render() {
  $('empty').style.display = queue.length ? 'none' : '';
  $('queue').innerHTML = queue.map((t, i) => `
    <li class="track" data-id="${esc(t.video_id)}">
      <span class="num">${i + 1}</span>
      <audio controls preload="none" src="/preview/${encodeURIComponent(t.video_id)}.mp3"></audio>
      <div class="info">
        <div class="title" title="${esc(t.title)}">${esc(t.title)}</div>
        <div class="dur">${fmtDur(t.duration)}</div>
      </div>
      <div class="btns">
        <button data-act="up" ${i === 0 ? 'disabled' : ''} title="Move earlier">&uarr;</button>
        <button data-act="down" ${i === queue.length - 1 ? 'disabled' : ''} title="Move later">&darr;</button>
        <button data-act="remove" title="Remove">&times;</button>
      </div>
    </li>
  `).join('');
  $('queue').querySelectorAll('.track').forEach(el => {
    const id = el.dataset.id;
    el.querySelector('[data-act=up]').onclick = () => move(id, -1);
    el.querySelector('[data-act=down]').onclick = () => move(id, 1);
    el.querySelector('[data-act=remove]').onclick = () => removeTrack(id);
  });
  $('filesnote').textContent = queue.length === 1
    ? 'Will save as: bg.mp3'
    : queue.length > 1
      ? `Will save as: ${queue.map((_, i) => `bg-${i + 1}.mp3`).join(', ')}`
      : '';
  if (browsePanelVisible) renderBrowseList();
}

async function move(id, dir) {
  const i = queue.findIndex(t => t.video_id === id);
  const j = i + dir;
  if (j < 0 || j >= queue.length) return;
  [queue[i], queue[j]] = [queue[j], queue[i]];
  render();
  await fetch('/api/reorder', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({order: queue.map(t => t.video_id)}),
  });
}

async function removeTrack(id) {
  queue = queue.filter(t => t.video_id !== id);
  render();
  await fetch('/api/remove', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({video_id: id}),
  });
}

function renderBrowseList() {
  const filter = $('browsefilter').value.trim().toLowerCase();
  const shown = filter ? browseEntries.filter(e => e.title.toLowerCase().includes(filter)) : browseEntries;
  $('browsecount').textContent = filter ? `${shown.length} of ${browseEntries.length}` : `${browseEntries.length} tracks`;
  $('browselist').innerHTML = shown.map(e => {
    const inQueue = queue.some(t => t.video_id === e.video_id);
    const playing = previewVideoId === e.video_id;
    return `
    <li class="browse-row ${inQueue ? 'added' : ''}" data-id="${esc(e.video_id)}" data-url="${esc(e.url)}">
      <button class="playbtn" title="${playing ? 'Stop preview' : 'Preview before adding'}">${playing ? '&#9632;' : '&#9654;'}</button>
      <span class="title" title="${esc(e.title)}">${esc(e.title)}</span>
      <span class="dur">${fmtDur(e.duration)}</span>
      <button class="addrowbtn" ${inQueue ? 'disabled' : ''}>${inQueue ? 'Added' : 'Add'}</button>
      ${playing ? `<div class="preview"><iframe src="https://www.youtube.com/embed/${esc(e.video_id)}?autoplay=1&rel=0" allow="autoplay" frameborder="0"></iframe></div>` : ''}
    </li>`;
  }).join('');
  $('browselist').querySelectorAll('.browse-row').forEach(el => {
    el.querySelector('.addrowbtn').onclick = () => addFromBrowse(el.dataset.id, el.dataset.url, el);
    el.querySelector('.playbtn').onclick = () => togglePreview(el.dataset.id);
  });
}
$('browsefilter').addEventListener('input', renderBrowseList);

function togglePreview(videoId) {
  previewVideoId = previewVideoId === videoId ? null : videoId;
  renderBrowseList();
}

async function addFromBrowse(videoId, url, rowEl) {
  const btn = rowEl.querySelector('.addrowbtn');
  btn.disabled = true;
  btn.textContent = '...';
  const d = await addTrack(url, true, p => {
    if (p.status === 'downloading') btn.textContent = p.percent != null ? `${Math.round(p.percent)}%` : '...';
  });
  if (d.error) {
    btn.disabled = false;
    btn.textContent = 'Add';
    $('status').className = 'err'; $('status').textContent = d.error;
    return;
  }
  rowEl.classList.add('added');
  btn.textContent = 'Added';
  render();
}

// Polls GET /api/progress until the download the server started for
// videoId finishes (or errors) -- onUpdate is called with each raw poll
// response so the caller can render a live percentage in the meantime.
async function pollProgress(videoId, onUpdate) {
  while (true) {
    const r = await fetch(`/api/progress?video_id=${encodeURIComponent(videoId)}`);
    const d = await r.json();
    onUpdate(d);
    if (d.status === 'done' || d.status === 'error' || d.status === 'unknown') return d;
    await new Promise(resolve => setTimeout(resolve, 400));
  }
}

// Shared POST /api/add -- skipClassify true is used for a single entry
// clicked from an already-listed channel/playlist, so it goes straight to
// download instead of re-classifying a URL we already know is a video. A
// video that isn't already cached comes back as kind:"pending" -- the
// server downloads it on a background thread and onProgress (if given) is
// called with each /api/progress poll while addTrack waits for it.
async function addTrack(url, skipClassify, onProgress) {
  const r = await fetch('/api/add', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({url, skip_classify: skipClassify}),
  });
  const d = await r.json();
  if (d.kind !== 'pending') return d;
  const final = await pollProgress(d.video_id, onProgress || (() => {}));
  if (final.status === 'error') return {error: final.error};
  if (final.status !== 'done') return {error: 'Lost track of the download -- try again.'};
  return {ok: true, kind: 'video', track: final.track, queue: final.queue};
}

function showBrowseList(d, statusMsg) {
  browseEntries = d.entries;
  $('browsetitle').textContent = d.title;
  $('browsefilter').value = '';
  browsePanelVisible = true;
  $('browsepanel').style.display = 'block';
  $('status').className = 'ok';
  $('status').textContent = statusMsg;
  renderBrowseList();
}

$('addbtn').onclick = async () => {
  const url = $('url').value.trim();
  if (!url) return;
  $('addbtn').disabled = true;
  $('status').className = '';
  $('status').textContent = 'Looking that up...';
  browsePanelVisible = false;
  $('browsepanel').style.display = 'none';
  try {
    const d = await addTrack(url, false, p => {
      if (p.status === 'downloading') {
        $('status').textContent = p.percent != null ? `Downloading... ${p.percent}%` : 'Downloading...';
      }
    });
    if (d.error) { $('status').className = 'err'; $('status').textContent = d.error; return; }
    if (d.kind === 'list') {
      showBrowseList(d, `Found ${d.entries.length} track(s) -- filter and click Add on the ones you want.`);
      return;
    }
    queue = d.queue;
    $('url').value = '';
    $('status').className = 'ok';
    $('status').textContent = `Added "${d.track.title}".`;
    render();
  } catch (e) {
    $('status').className = 'err'; $('status').textContent = 'Network error: ' + e;
  } finally {
    $('addbtn').disabled = false;
  }
};
$('url').addEventListener('keydown', e => { if (e.key === 'Enter') $('addbtn').click(); });

async function loadDefaultChannel() {
  try {
    const r = await fetch('/api/default-channel');
    const d = await r.json();
    if (d.error) { $('status').className = 'err'; $('status').textContent = d.error; return; }
    if (d.kind === 'list') {
      showBrowseList(d, `Showing ${d.entries.length} track(s) from "${d.title}" -- filter, preview after Add, or paste your own URL above.`);
    }
  } catch (e) {
    // Auto-browse is a convenience, not a requirement -- a network hiccup
    // here shouldn't block the rest of the page from working.
  }
}

$('savebtn').onclick = async () => {
  if (!queue.length) {
    $('status').className = 'err'; $('status').textContent = 'Add at least one track first.';
    return;
  }
  $('savebtn').disabled = true;
  const r = await fetch('/api/save', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
  const d = await r.json();
  $('savebtn').disabled = false;
  if (d.error) { $('status').className = 'err'; $('status').textContent = d.error; return; }
  $('status').className = 'ok';
  $('status').textContent = `Saved: ${d.files.join(', ')} -- step3_render_video.py will use these automatically, no --music flag needed.`;
};

loadQueue().then(loadDefaultChannel);
</script>
</body></html>
"""


def run(project, port=8010, no_browser=False, channel=DEFAULT_CHANNEL_URL):
    """Serve the music picker for `project` until Ctrl+C. Progress (the
    queue, and any tracks already saved as bg*.mp3) persists across runs --
    re-run the same command any time to keep adding, reordering, or
    re-saving.

    `channel`, if truthy, is auto-browsed the moment the page loads (see
    /api/default-channel) -- defaults to the YouTube Audio Library's own
    channel so the picker opens straight onto a filterable, playable list of
    royalty-free tracks with nothing to paste. Pass '' to start on an empty
    picker instead."""
    project_dir = PROJECTS_DIR / project
    if not project_dir.exists():
        sys.exit(f"No project folder at {project_dir} -- run step1_audio_and_captions.py first.")

    cache_dir = project_dir / ".music_cache"
    cache_dir.mkdir(exist_ok=True)

    STATE.update({
        "project": project,
        "project_dir": project_dir,
        "cache_dir": cache_dir,
        "queue": load_queue(project_dir),
        "lock": threading.Lock(),
        "channel_url": channel,
        "progress": {},
    })

    url = f"http://localhost:{port}/"
    existing = existing_bg_files(project_dir)
    print(f"{len(STATE['queue'])} track(s) queued." if STATE["queue"] else "No tracks queued yet.")
    if existing:
        print(f"Project already has: {', '.join(f.name for f in existing)}")
    print(f"Music picker running at {url}   (Ctrl+C to stop -- your queue is saved as you go)")
    if not no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    class SingleBindHTTPServer(ThreadingHTTPServer):
        # See step2_pick_clips.py: Windows' SO_REUSEADDR default lets a second
        # server silently share the port with a stale one; make it fail loudly.
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
            f"Port {port} is already in use (another music picker still running?): {e}\n"
            f"Stop it, or run again with --port {port + 1}."
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\nStopped. {len(STATE['queue'])} track(s) queued -- resume any time with:")
        print(f"    python step_music_picker.py {project}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("project", help="Path under projects/, e.g. en/crumbs")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open the browser")
    parser.add_argument(
        "--channel", default=DEFAULT_CHANNEL_URL, metavar="URL",
        help="YouTube channel/playlist URL auto-browsed the moment the page loads, so you "
        f"land on a filterable, playable track list with nothing to paste (default: {DEFAULT_CHANNEL_URL}, "
        "the YouTube Audio Library's own channel). Pass --channel '' to start on an empty picker instead.",
    )
    args = parser.parse_args()
    run(args.project, port=args.port, no_browser=args.no_browser, channel=args.channel)


if __name__ == "__main__":
    main()
