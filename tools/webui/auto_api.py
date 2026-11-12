"""Backend of the dashboard's "New auto video" page (auto.html).

create(): writes projects/<lang>/<name>/script.txt (or source.txt with a
YouTube link), commits it as "auto: <lang>/<name> [YYYY-MM-DD HH:MM]" and
pushes -- that commit is what starts .github/workflows/auto.yml.

status(): the recent Auto video runs from the GitHub API (the repo is
public, so no login is needed; inside a Codespace its GITHUB_TOKEN is used
for a higher rate limit), each with its current step, Release link and --
once uploaded -- the YouTube/Studio links from youtube_upload.json.
Answers are cached briefly: anonymous GitHub API calls are limited to 60
an hour per address.
"""

import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECTS_DIR = REPO_ROOT / "projects"
WORKFLOW = "auto.yml"
LANGS = {"en": "English", "es": "Spanish", "ur": "Urdu"}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")
CACHE_TTL = 45 if os.environ.get("GITHUB_TOKEN") else 120
_cache = {"at": 0.0, "data": None}
_lock = threading.Lock()


def _git(*args, timeout=180):
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def repo_slug():
    url = _git("remote", "get-url", "origin", timeout=15).stdout.strip()
    m = re.search(r"github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else None


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)[:60]


def estimate_minutes(text):
    """(narration minutes, cloud processing minutes) for a script."""
    words = len((text or "").split())
    narration = words / 140.0  # edge-tts at -10%
    processing = 8 + narration * 10  # measured: 4 sentences ~8 min, 27 min ~4-5 h
    return round(narration, 1), round(processing)


# ------------------------------------------------------------------ create
def create(body):
    lang = (body.get("lang") or "en").strip()
    if lang not in LANGS:
        return {"ok": False, "error": f"Unknown language {lang!r}."}
    name = slugify(body.get("name"))
    if not NAME_RE.match(name or ""):
        return {"ok": False, "error": "Give the video a name (letters and numbers, at least 2)."}
    mode = body.get("mode") or "script"
    text = (body.get("text") or "").replace("\r\n", "\n").strip()
    if mode == "script":
        if len(text.split()) < 8:
            return {"ok": False, "error": "The script is empty or too short."}
    elif mode == "link":
        if not re.match(r"^https?://(www\.|m\.)?(youtube\.com|youtu\.be)/", text):
            return {"ok": False, "error": "Paste a YouTube video link (https://www.youtube.com/... or https://youtu.be/...)."}
    else:
        return {"ok": False, "error": "bad mode"}

    when = (body.get("publish_at") or "").strip().replace("T", " ")
    if when:
        if not re.match(r"^\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}$", when):
            return {"ok": False, "error": "Publish time must look like 2026-11-20 18:00."}

    # Latest state first, so the name check sees projects made elsewhere.
    pull = _git("pull", "--rebase", "--autostash", "origin", "main")
    if pull.returncode != 0:
        _git("rebase", "--abort", timeout=30)
        return {"ok": False, "error": "Couldn't get the latest version from GitHub (git pull failed).",
                "log": (pull.stdout + pull.stderr)[-1500:]}
    project = f"{lang}/{name}"
    pdir = PROJECTS_DIR / lang / name
    if pdir.exists() and any(pdir.iterdir()):
        return {"ok": False, "error": f"A project called {project} already exists. Choose another name."}
    pdir.mkdir(parents=True, exist_ok=True)
    if mode == "script":
        (pdir / "script.txt").write_text(text + "\n", encoding="utf-8")
        added = pdir / "script.txt"
    else:
        lines = [text.split()[0]]
        if body.get("documentary"):
            lines.append("documentary")
        (pdir / "source.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        added = pdir / "source.txt"

    msg = f"auto: {project}" + (f" {when}" if when else "")
    steps = [
        ("add", ["add", str(added.relative_to(REPO_ROOT))]),
        ("commit", ["commit", "-m", msg, "--", str(added.relative_to(REPO_ROOT))]),
    ]
    for label, args in steps:
        r = _git(*args)
        if r.returncode != 0:
            return {"ok": False, "error": f"git {label} failed.", "log": (r.stdout + r.stderr)[-1500:]}
    for attempt in range(4):
        r = _git("push", "origin", "HEAD:main")
        if r.returncode == 0:
            break
        _git("pull", "--rebase", "--autostash", "origin", "main")
        time.sleep(2 + attempt * 3)
    else:
        return {"ok": False, "error": "Saved on this machine, but pushing to GitHub failed. "
                "Check the internet / GitHub login, then click Sync code or run 'git push'.",
                "log": (r.stdout + r.stderr)[-1500:]}
    with _lock:
        _cache["at"] = 0.0  # show the new run on the next poll
    slug = repo_slug()
    return {"ok": True, "project": project, "commit": msg,
            "actions": f"https://github.com/{slug}/actions/workflows/{WORKFLOW}" if slug else None}


# ------------------------------------------------------------------ status
def _get(url, raw=False, timeout=15):
    headers = {"User-Agent": "yt-automation-dashboard", "Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read().decode("utf-8", "replace")
    return data if raw else json.loads(data)


TITLE_RE = re.compile(r"^auto:\s*(\S+)\s*(.*)$")


def _raw_json(slug, sha, path):
    try:
        return json.loads(_get(f"https://raw.githubusercontent.com/{slug}/{sha}/{path}", raw=True))
    except Exception:
        return None


def _fetch_status():
    slug = repo_slug()
    if not slug:
        return {"ok": False, "error": "This folder isn't linked to a GitHub repo."}
    runs = _get(f"https://api.github.com/repos/{slug}/actions/workflows/{WORKFLOW}/runs?per_page=30")
    out = []
    for run in runs.get("workflow_runs") or []:
        first = (run.get("display_title") or "").splitlines()[0:1]
        m = TITLE_RE.match(first[0].strip() if first else "")
        if not m:
            continue  # pushes that weren't auto requests (skipped runs)
        if run.get("conclusion") == "skipped":
            continue
        project, when = m.group(1), m.group(2).strip()
        item = {
            "project": project, "publish_at": when or None,
            "status": run.get("status"), "conclusion": run.get("conclusion"),
            "started": run.get("run_started_at") or run.get("created_at"),
            "updated": run.get("updated_at"), "run_url": run.get("html_url"),
            "run_number": run.get("run_number"), "step": None,
        }
        if run.get("status") != "completed":
            try:
                jobs = _get(run["jobs_url"]).get("jobs") or []
                for j in jobs:
                    if j.get("status") == "in_progress":
                        cur = next((s["name"] for s in j.get("steps") or [] if s.get("status") == "in_progress"), None)
                        item["step"] = cur or j.get("name")
                        break
                else:
                    item["step"] = "waiting for a GitHub machine"
            except Exception:
                pass
        if run.get("conclusion") == "success":
            item["release"] = (f"https://github.com/{slug}/releases/tag/"
                               f"video-{project.replace('/', '-')}-{run.get('run_number')}")
        out.append(item)

    # One record per project: its latest run. Add upload + review info.
    seen, latest = set(), []
    for it in out:
        if it["project"] in seen:
            continue
        seen.add(it["project"])
        latest.append(it)
    for it in latest[:12]:
        if it["conclusion"] == "success" or it["status"] != "completed":
            up = _raw_json(slug, "main", f"projects/{it['project']}/youtube_upload.json")
            if up:
                it["youtube"] = {k: up.get(k) for k in ("url", "studio", "title", "privacy", "publish_at")}
            rep = _raw_json(slug, "main", f"projects/{it['project']}/auto_pick_report.json")
            if rep and rep.get("needs_review"):
                it["review"] = rep.get("reasons") or ["needs a look"]
    return {"ok": True, "repo": slug, "runs": latest[:12],
            "actions": f"https://github.com/{slug}/actions/workflows/{WORKFLOW}",
            "checked": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def status(force=False):
    with _lock:
        age = time.time() - _cache["at"]
        # a forced refresh still waits 15 s between real checks (rate limit)
        if _cache["data"] and age < (15 if force else CACHE_TTL):
            return _cache["data"]
    try:
        data = _fetch_status()
    except urllib.error.HTTPError as e:
        msg = "GitHub's hourly limit for status checks is used up -- it refreshes within the hour." \
            if e.code in (403, 429) else f"GitHub answered {e.code}."
        data = {"ok": False, "error": msg}
        with _lock:
            if _cache["data"]:
                stale = dict(_cache["data"], stale=msg)
                _cache["at"] = time.time()
                return stale
    except Exception as e:
        data = {"ok": False, "error": f"Couldn't reach GitHub: {e}"}
    with _lock:
        _cache.update(at=time.time(), data=data)
    return data


def defaults():
    tomorrow = (datetime.now() + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
    return {"langs": LANGS, "default_publish": tomorrow.strftime("%Y-%m-%dT%H:%M"),
            "timezone": os.environ.get("YT_TIMEZONE") or "Asia/Karachi",
            "names": sorted(f"{p.parent.name}/{p.name}" for p in PROJECTS_DIR.glob("*/*") if p.is_dir())}
