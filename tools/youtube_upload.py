"""Upload a rendered video to your YouTube channel as PRIVATE, optionally
scheduled to go public at a given time.

    python tools/youtube_upload.py projects/en/my-video
    python tools/youtube_upload.py projects/en/my-video --publish-at "2026-11-15 18:00"
    python tools/youtube_upload.py projects/en/my-video --publish-at "2026-11-15 18:00" --tz Asia/Karachi

Uses projects/<lang>/<name>/output.mp4. Credentials come from the
environment (YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN -- GitHub
Actions secrets) or tools/youtube_token.json written by youtube_auth.py.

Title / description / tags, first match wins:
  1. projects/.../youtube.json  {"title", "description", "tags", ...}
     (any field you leave out is filled by the next rules)
  2. Gemini writes them from script.txt (needs GEMINI_API_KEY); the
     result is saved to youtube.json so you can see and reuse it
  3. the project name as title, the script's opening as description
credits.txt (stock footage credits from step 3) is always appended to the
description.

Writes projects/.../youtube_upload.json with the video id and link, and
refuses to upload the same project twice (--force to upload again).
Standard library only. Resumable upload: a dropped connection resumes
where it stopped instead of starting over.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

TOKEN_FILE = TOOLS / "youtube_token.json"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
API = "https://www.googleapis.com/youtube/v3"
CHUNK = 32 * 1024 * 1024  # multiple of 256 KiB, as the API requires
DEFAULT_TZ = "Asia/Karachi"
CATEGORY_EDUCATION = "27"
MIN_LEAD_MINUTES = 15  # YouTube rejects a publishAt that's (almost) now


class UploadError(RuntimeError):
    pass


# ---------------------------------------------------------------- auth
def credentials():
    env = {k: os.environ.get(k, "").strip() for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN")}
    if all(env.values()):
        return env["YT_CLIENT_ID"], env["YT_CLIENT_SECRET"], env["YT_REFRESH_TOKEN"]
    if TOKEN_FILE.exists():
        d = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        return d["client_id"], d["client_secret"], d["refresh_token"]
    missing = [k for k, v in env.items() if not v]
    raise UploadError(f"No YouTube credentials: set {', '.join(missing)} (GitHub secrets) "
                      f"or run  python tools/youtube_auth.py  on this computer first.")


def access_token():
    cid, secret, refresh = credentials()
    body = urllib.parse.urlencode({"client_id": cid, "client_secret": secret,
                                   "refresh_token": refresh, "grant_type": "refresh_token"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=body), timeout=30) as r:
            return json.loads(r.read().decode())["access_token"]
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")
        if "invalid_grant" in text:
            raise UploadError(
                "Google rejected the refresh token (invalid_grant). Usually: the app is still in "
                "'Testing' (tokens expire after 7 days -- publish it under Google Auth Platform -> "
                "Audience), or the access was removed at myaccount.google.com/permissions. Run "
                "python tools/youtube_auth.py again and update YT_REFRESH_TOKEN.") from None
        raise UploadError(f"Token refresh failed ({e.code}): {text[:400]}") from None


def _api_error(e):
    text = e.read().decode(errors="replace")
    try:
        err = json.loads(text).get("error", {})
        reason = (err.get("errors") or [{}])[0].get("reason", "")
        msg = err.get("message", text)
    except ValueError:
        reason, msg = "", text
    hints = {
        "quotaExceeded": "Daily API quota used up (about 6 uploads/day). Try again after midnight Pacific time.",
        "uploadLimitExceeded": "YouTube's own daily upload limit for this channel is reached. Try tomorrow.",
        "invalidPublishAt": "The publish time must be in the future (and the video private).",
        "forbidden": "The account has no channel, or the upload permission wasn't granted.",
        "youtubeSignupRequired": "This Google account has no YouTube channel yet -- create one first.",
    }
    return UploadError(f"YouTube API {e.code} {reason}: {msg}" + (f"\n  -> {hints[reason]}" if reason in hints else ""))


# ---------------------------------------------------------------- metadata
def parse_publish_at(text, tz_name):
    """'2026-11-15 18:00' in tz_name (or an ISO string with offset) -> aware UTC datetime."""
    text = text.strip().replace("T", " ")
    text = re.sub(r" (\d):", lambda m: " 0" + m.group(1) + ":", text)  # 9:30 -> 09:30
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise UploadError(f"Can't read the publish time {text!r}; use YYYY-MM-DD HH:MM") from None
    if dt.tzinfo is None:
        try:
            from zoneinfo import ZoneInfo
            dt = dt.replace(tzinfo=ZoneInfo(tz_name))
        except Exception:
            if tz_name != DEFAULT_TZ:
                raise UploadError(f"Unknown timezone {tz_name!r} (pip install tzdata on Windows)") from None
            dt = dt.replace(tzinfo=timezone(timedelta(hours=5)))  # Asia/Karachi, no DST
    return dt.astimezone(timezone.utc)


def clean_title(t):
    t = re.sub(r"[<>]", "", " ".join((t or "").split())).strip(" \"'")
    return t[:100].rstrip()


def clean_description(d):
    return re.sub(r"[<>]", "", d or "")[:4900]


def clean_tags(tags):
    out, total = [], 0
    for t in tags or []:
        t = re.sub(r"[<>,\"]", "", str(t)).strip()
        if not t or t.lower() in (x.lower() for x in out):
            continue
        cost = len(t) + (2 if " " in t else 0) + 1
        if total + cost > 480:
            break
        out.append(t)
        total += cost
    return out


META_PROMPT = """You write YouTube metadata for a documentary-style narrated video.
From the narration script below, return ONLY a JSON object:
{"title": "...", "description": "...", "tags": ["...", ...]}
- title: max 90 characters, specific and curiosity-driven, no clickbait lies,
  no emojis, no ALL CAPS words, no quotes around it.
- description: 2 short paragraphs (what the viewer will learn, why it matters),
  then a blank line and 3-5 hashtags on one line. No links. Max 900 characters.
- tags: 12-20 search phrases, most specific first, each under 30 characters.
Write in the same language as the script.

SCRIPT:
"""


def llm_metadata(script, verbose=True):
    try:
        from llm_client import complete, load_keys
        keys = load_keys()
    except Exception as e:
        if verbose:
            print(f"  (no Gemini for metadata: {e})")
        return {}
    if not keys:
        return {}
    text = script if len(script) < 24000 else script[:16000] + "\n...\n" + script[-6000:]
    try:
        out = complete([{"role": "user", "content": META_PROMPT + text}], keys,
                       temperature=0.6, max_tokens=1500, verbose=False)
    except Exception as e:
        if verbose:
            print(f"  (Gemini metadata failed: {e})")
        return {}
    m = re.search(r"\{.*\}", out, re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
    except ValueError:
        d = {}
    return {k: d[k] for k in ("title", "description", "tags") if d.get(k)}


def build_metadata(project_dir, use_llm=True):
    meta_path = project_dir / "youtube.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    script_path = project_dir / "script.txt"
    script = script_path.read_text(encoding="utf-8", errors="replace") if script_path.exists() else ""
    if use_llm and script and not all(meta.get(k) for k in ("title", "description", "tags")):
        print("Writing title / description / tags with Gemini...")
        gen = llm_metadata(script)
        if gen:
            for k, v in gen.items():
                if not meta.get(k):
                    meta[k] = v
            meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"  saved to {meta_path}")
    if not meta.get("title"):
        meta["title"] = project_dir.name.replace("-", " ").replace("_", " ").title()
    if not meta.get("description"):
        sentences = re.split(r"(?<=[.!?])\s+", " ".join(script.split()))
        meta["description"] = " ".join(sentences[:4])[:700]
    desc = meta["description"].rstrip()
    credits = project_dir / "credits.txt"
    if credits.exists() and credits.read_text(encoding="utf-8").strip():
        desc += "\n\n---\nCredits\n" + credits.read_text(encoding="utf-8").strip()
    return {
        "title": clean_title(meta["title"]),
        "description": clean_description(desc),
        "tags": clean_tags(meta.get("tags")),
        "category": str(meta.get("category") or CATEGORY_EDUCATION),
        "language": meta.get("language") or project_dir.parent.name,
        "made_for_kids": bool(meta.get("made_for_kids", False)),
    }


# ---------------------------------------------------------------- upload
def _request(method, url, token, data=None, headers=None, timeout=120):
    h = {"Authorization": f"Bearer {token}"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    return urllib.request.urlopen(req, timeout=timeout)


def start_session(token, body, size):
    url = UPLOAD_URL + "?" + urllib.parse.urlencode({"uploadType": "resumable", "part": "snippet,status",
                                                     "notifySubscribers": "true"})
    data = json.dumps(body).encode()
    try:
        with _request("POST", url, token, data, {
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4",
        }) as r:
            return r.headers["Location"]
    except urllib.error.HTTPError as e:
        raise _api_error(e) from None


def _uploaded_so_far(session, token, size):
    """Ask the server how many bytes it has (after a dropped connection)."""
    try:
        with _request("PUT", session, token, b"", {"Content-Range": f"bytes */{size}"}) as r:
            return size, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 308:
            rng = e.headers.get("Range")
            return (int(rng.split("-")[1]) + 1 if rng else 0), None
        raise _api_error(e) from None


def upload_file(session, token_fn, path, size):
    token = token_fn()
    pos, t0, last_print, failures = 0, time.time(), 0, 0
    with open(path, "rb") as f:
        while pos < size:
            f.seek(pos)
            chunk = f.read(CHUNK)
            end = pos + len(chunk) - 1
            try:
                with _request("PUT", session, token, chunk, {
                    "Content-Length": str(len(chunk)), "Content-Range": f"bytes {pos}-{end}/{size}",
                }, timeout=600) as r:
                    return json.loads(r.read().decode())  # 200/201: done
            except urllib.error.HTTPError as e:
                if e.code == 308:
                    rng = e.headers.get("Range")
                    pos = int(rng.split("-")[1]) + 1 if rng else 0
                    failures = 0
                elif e.code == 401:
                    token = token_fn()
                    continue
                elif e.code in (500, 502, 503, 504) and failures < 8:
                    failures += 1
                    time.sleep(min(60, 2 ** failures))
                    pos, done = _uploaded_so_far(session, token, size)
                    if done:
                        return done
                    continue
                else:
                    raise _api_error(e) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                failures += 1
                if failures > 8:
                    raise UploadError(f"Upload kept failing: {e}") from None
                print(f"  connection problem ({e}); resuming in {min(60, 2 ** failures)} s...")
                time.sleep(min(60, 2 ** failures))
                pos, done = _uploaded_so_far(session, token, size)
                if done:
                    return done
                continue
            now = time.time()
            if now - last_print > 10 or pos >= size:
                rate = pos / max(1, now - t0) / 1e6
                print(f"  {pos / size:6.1%}  {pos / 1e6:,.0f} / {size / 1e6:,.0f} MB  ({rate:.1f} MB/s)", flush=True)
                last_print = now
    raise UploadError("Upload ended without YouTube confirming the video.")


def verify_private(token, video_id, tries=6):
    """Read the video back from YouTube: it must exist and be private.
    Returns its status dict. Raises UploadError if it isn't private."""
    url = f"{API}/videos?" + urllib.parse.urlencode({"part": "status,processingDetails", "id": video_id})
    for attempt in range(tries):
        try:
            with _request("GET", url, token, timeout=30) as r:
                items = json.loads(r.read().decode()).get("items") or []
        except urllib.error.HTTPError as e:
            raise _api_error(e) from None
        if items:
            st = items[0].get("status", {})
            if st.get("privacyStatus") != "private":
                raise UploadError(f"Video {video_id} is {st.get('privacyStatus')!r}, not private -- "
                                  f"set it to Private in Studio now: https://studio.youtube.com/video/{video_id}/edit")
            return st
        time.sleep(5)  # brand-new video not listed yet
    raise UploadError(f"YouTube accepted the upload but video {video_id} can't be found yet -- check Studio.")


def set_thumbnail(token, video_id, image):
    ctype = "image/png" if image.suffix.lower() == ".png" else "image/jpeg"
    url = UPLOAD_URL.replace("/videos", "/thumbnails/set") + "?" + urllib.parse.urlencode({"videoId": video_id})
    try:
        with _request("POST", url, token, image.read_bytes(), {"Content-Type": ctype}):
            return True
    except urllib.error.HTTPError as e:
        print(f"  thumbnail not set: {_api_error(e)}  (custom thumbnails need a phone-verified channel)")
        return False


def run(project_dir, publish_at=None, tz_name=DEFAULT_TZ, force=False, use_llm=True, dry_run=False,
        video=None):
    project_dir = Path(project_dir).resolve()
    video = Path(video) if video else project_dir / "output.mp4"
    if not video.exists():
        raise UploadError(f"No video at {video} -- render it first (step 3).")
    record_path = project_dir / "youtube_upload.json"
    if record_path.exists() and not force:
        rec = json.loads(record_path.read_text(encoding="utf-8"))
        print(f"Already uploaded: {rec.get('url')}  (use --force to upload again)")
        return rec

    meta = build_metadata(project_dir, use_llm=use_llm)
    status = {"privacyStatus": "private", "selfDeclaredMadeForKids": meta["made_for_kids"],
              "embeddable": True, "license": "youtube"}
    when = None
    if publish_at:
        when = parse_publish_at(publish_at, tz_name)
        if when < datetime.now(timezone.utc) + timedelta(minutes=MIN_LEAD_MINUTES):
            print(f"Warning: publish time {when:%Y-%m-%d %H:%M} UTC is in the past or too soon -- "
                  f"uploading as private WITHOUT a schedule. Set the time in YouTube Studio.")
            when = None
        else:
            status["publishAt"] = when.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    snippet = {"title": meta["title"], "description": meta["description"], "tags": meta["tags"],
               "categoryId": meta["category"], "defaultLanguage": meta["language"],
               "defaultAudioLanguage": meta["language"]}
    body = {"snippet": snippet, "status": status}
    size = video.stat().st_size

    print(f"Title:   {meta['title']}")
    print(f"Tags:    {', '.join(meta['tags'][:8])}{' ...' if len(meta['tags']) > 8 else ''}")
    print(f"Privacy: private" + (f", goes public {when:%Y-%m-%d %H:%M} UTC" if when else ", no schedule"))
    print(f"File:    {video} ({size / 1e6:,.0f} MB)")
    if dry_run:
        print("\n--dry-run: nothing uploaded. Description:\n" + meta["description"])
        return None

    token = access_token()
    session = start_session(token, body, size)
    print("Uploading...")
    result = upload_file(session, access_token, video, size)
    vid = result["id"]
    st = verify_private(access_token(), vid)
    print(f"Verified on YouTube: private, upload {st.get('uploadStatus')}"
          + (f", goes public {st['publishAt']}" if st.get("publishAt") else ", not scheduled"))
    result.setdefault("status", {}).update(st)
    rec = {
        "video_id": vid, "url": f"https://youtu.be/{vid}",
        "studio": f"https://studio.youtube.com/video/{vid}/edit",
        "title": meta["title"], "privacy": result.get("status", {}).get("privacyStatus"),
        "publish_at": result.get("status", {}).get("publishAt"),
        "uploaded": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    for name in ("thumbnail.jpg", "thumbnail.png", "thumbnail.jpeg"):
        if (project_dir / name).exists():
            rec["thumbnail"] = set_thumbnail(access_token(), vid, project_dir / name)
            break
    record_path.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nUploaded: {rec['url']}")
    print(f"Edit in Studio: {rec['studio']}")
    if publish_at and when and not rec["publish_at"]:
        print("Note: YouTube didn't keep the schedule. If your API project isn't audited yet, uploads stay "
              "private (see INFO.md). Set the schedule in Studio.")
    return rec


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", help="project folder, e.g. projects/en/my-video")
    ap.add_argument("--publish-at", help='when it goes public, e.g. "2026-11-15 18:00" (private until then)')
    ap.add_argument("--tz", default=os.environ.get("YT_TIMEZONE") or DEFAULT_TZ,
                    help=f"timezone of --publish-at (default {DEFAULT_TZ})")
    ap.add_argument("--video", help="upload this file instead of <project>/output.mp4")
    ap.add_argument("--force", action="store_true", help="upload even if this project was uploaded before")
    ap.add_argument("--no-llm", dest="use_llm", action="store_false", help="don't ask Gemini for title/tags")
    ap.add_argument("--dry-run", action="store_true", help="show the metadata, upload nothing")
    args = ap.parse_args()
    try:
        run(args.project, args.publish_at, args.tz, args.force, args.use_llm, args.dry_run, args.video)
    except UploadError as e:
        sys.exit(f"ERROR: {e}")


if __name__ == "__main__":
    main()
