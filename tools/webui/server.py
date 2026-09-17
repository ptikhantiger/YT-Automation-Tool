"""Local dashboard for the video pipeline: pick flags in a browser, click Run,
watch the real stdout stream in.

Deliberately dependency-free (stdlib only), matching the rest of this
pipeline (see llm_client.py's docstring) -- no Flask/FastAPI to install.

Usage:
    python tools/webui/server.py [--port 8765] [--no-browser]

This is a thin, generic script runner, not a reimplementation of the
pipeline: index.html owns all the flag documentation and presets, and only
ever POSTs a script key + a project path + a list of already-built
"--flag value" strings. This file's job is just:

  1. Serve index.html.
  2. GET  /api/projects        -> list existing projects/<lang>/<slug> paths.
  3. POST /api/run             -> spawn one of the five allow-listed scripts
                                   as `python -u <script> <project> <flags...>`,
                                   return a run_id.
  4. GET  /api/stream?run_id=  -> stream that process's stdout/stderr live,
                                   line by line, until it exits.
  5. GET  /api/status?run_id=  -> whether it's finished, and its exit code.
  6. POST /api/stop            -> kill it (and, on Windows, its whole process
                                   tree -- see _stop_process, this matters
                                   for step3's ffmpeg children).

`script` is always looked up in the SCRIPTS allow-list below, never taken
as a raw path -- the whole point of the allow-list is that this server
will never exec anything the user didn't already have on disk as one of
these five files.
"""
import json
import os
import subprocess
import sys
import threading
import queue
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

WEBUI_DIR = Path(__file__).resolve().parent
TOOLS_DIR = WEBUI_DIR.parent
REPO_ROOT = TOOLS_DIR.parent
PROJECTS_DIR = REPO_ROOT / "projects"
INDEX_HTML = WEBUI_DIR / "index.html"

# The only scripts this server will ever launch. Keys match what
# index.html sends as "script"; edit both places together.
SCRIPTS = {
    "step0": TOOLS_DIR / "step0_build_script.py",
    "step1": TOOLS_DIR / "step1_audio_and_captions.py",
    "step2": TOOLS_DIR / "step2_pick_clips.py",
    "music": TOOLS_DIR / "step_music_picker.py",
    "step3": TOOLS_DIR / "step3_render_video.py",
}

def _git(*args, timeout=180):
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def _git_sync():
    """git pull --rebase from origin; report whether code files changed (a
    running server keeps the old Python until restarted)."""
    try:
        before = _git("rev-parse", "HEAD", timeout=15).stdout.strip()
        branch = _git("rev-parse", "--abbrev-ref", "HEAD", timeout=15).stdout.strip() or "main"
        r = _git("pull", "--rebase", "--autostash", "origin", branch)
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0:
            _git("rebase", "--abort", timeout=30)
            return {"ok": False, "error": "git pull --rebase failed.", "log": out}
        after = _git("rev-parse", "HEAD", timeout=15).stdout.strip()
        changed = []
        if before != after:
            changed = [l for l in _git("diff", "--name-only", before, after, timeout=30).stdout.splitlines() if l.strip()]
        return {"ok": True, "updated": before != after, "changed": changed,
                "code_changed": any(f.endswith((".py", ".html")) for f in changed), "log": out}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "git took too long (network?).", "log": ""}
    except OSError as e:
        return {"ok": False, "error": f"git isn't available here: {e}", "log": ""}


# run_id -> {"proc": Popen, "queue": Queue, "returncode": int|None}
RUNS = {}
RUNS_LOCK = threading.Lock()


def _reader_thread(run_id, proc):
    """Pump the child's stdout into its queue line by line until it exits."""
    q = RUNS[run_id]["queue"]
    try:
        for line in proc.stdout:
            q.put(line)
    except Exception as e:
        q.put(f"\n[webui] lost the output stream: {e}\n")
    proc.wait()
    RUNS[run_id]["returncode"] = proc.returncode
    q.put(None)  # sentinel: no more output


def _start_process(script_key, project, flags):
    script_path = SCRIPTS[script_key]
    argv = [sys.executable, "-u", str(script_path), project, *flags]
    kwargs = dict(
        cwd=str(TOOLS_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
    )
    if os.name == "nt":
        # New process group so _stop_process can taskkill the whole tree
        # (ffmpeg children step3 spawns would otherwise survive a plain
        # terminate() of just the python.exe parent).
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(argv, **kwargs)
    run_id = uuid.uuid4().hex
    with RUNS_LOCK:
        RUNS[run_id] = {"proc": proc, "queue": queue.Queue(), "returncode": None}
    t = threading.Thread(target=_reader_thread, args=(run_id, proc), daemon=True)
    t.start()
    return run_id, argv


def _stop_process(run_id):
    with RUNS_LOCK:
        entry = RUNS.get(run_id)
    if not entry:
        return False
    proc = entry["proc"]
    if proc.poll() is not None:
        return False
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
            )
        else:
            proc.terminate()
    except Exception:
        pass
    return True


def _list_projects():
    projects = []
    if PROJECTS_DIR.is_dir():
        for lang_dir in sorted(PROJECTS_DIR.iterdir()):
            if not lang_dir.is_dir():
                continue
            for slug_dir in sorted(lang_dir.iterdir()):
                if slug_dir.is_dir():
                    projects.append(f"{lang_dir.name}/{slug_dir.name}")
    return projects


VOICES_CACHE = WEBUI_DIR / ".voices_cache.json"


def _list_edge_voices():
    """Full edge-tts voice catalog as [{name, gender, locale}], cached for a
    week -- the list rarely changes and fetching it needs a network call."""
    import time

    try:
        if VOICES_CACHE.is_file() and time.time() - VOICES_CACHE.stat().st_mtime < 7 * 86400:
            return json.loads(VOICES_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    import asyncio

    import edge_tts

    raw = asyncio.run(edge_tts.list_voices())
    voices = sorted(
        (
            {
                "name": v.get("ShortName", ""),
                "gender": v.get("Gender", ""),
                "locale": v.get("Locale", ""),
            }
            for v in raw
            if v.get("ShortName")
        ),
        key=lambda v: (v["locale"], v["name"]),
    )
    try:
        VOICES_CACHE.write_text(json.dumps(voices), encoding="utf-8")
    except OSError:
        pass
    return voices


def _project_state(project):
    """Everything the wizard UI needs to know about where a project stands:
    which pipeline artifacts exist, scene/selection counts, and the output
    file's mtime/size (so the UI can tell a fresh render from an old one).
    All cheap stat()/small-JSON reads -- safe to poll every couple seconds."""
    d = PROJECTS_DIR / project
    state = {
        "exists": d.is_dir(),
        "script": False, "audio": False, "scenes": 0, "selections": 0,
        "music": False, "timeline": False, "output": False,
        "output_mtime": None, "output_size": None,
    }
    if not state["exists"]:
        return state
    state["script"] = (d / "script.txt").is_file()
    state["audio"] = (d / "audio.mp3").is_file()
    state["timeline"] = (d / "timeline.json").is_file()
    for name, key in (("scenes.json", "scenes"), ("selections.json", "selections")):
        try:
            state[key] = len(json.loads((d / name).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    state["music"] = (d / "bg.mp3").is_file() or (d / "bg-1.mp3").is_file()
    out = d / "output.mp4"
    if out.is_file():
        st = out.stat()
        state["output"] = True
        state["output_mtime"] = st.st_mtime
        state["output_size"] = st.st_size
    return state


def _safe_project(project):
    """Reject anything that could escape projects/ -- this only needs to
    stop fat-fingering, not a hostile actor, since the server only binds to
    127.0.0.1 and the caller is the same machine's browser."""
    if not project or project.startswith(("/", "\\")) or ".." in Path(project).parts:
        return False
    return True


class Handler(BaseHTTPRequestHandler):
    server_version = "PipelineDashboard/1.0"
    # HTTP/1.1 so the <video> element's Range requests behave; every response
    # here carries an exact Content-Length except _stream, which opts out of
    # keep-alive explicitly (it has no length until the child process exits).
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # keep the console clean; errors still raise/print via send_error

    def handle(self):
        # A browser aborting its own request (closing a tab mid-stream, or
        # dropping the connection before a 404 body for /favicon.ico lands)
        # is routine, not an error -- without this, ThreadingHTTPServer
        # prints a full WinError 10053/10054 traceback for every one.
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/":
            self._serve_index()
        elif path == "/api/projects":
            self._json({"projects": _list_projects()})
        elif path == "/api/stream":
            self._stream(qs.get("run_id", [None])[0])
        elif path == "/api/status":
            self._status(qs.get("run_id", [None])[0])
        elif path == "/api/voices":
            try:
                self._json({"voices": _list_edge_voices()})
            except Exception as e:
                # No network / edge-tts hiccup: the UI falls back to a plain
                # text field, so surface the reason instead of a 500 page.
                self._json({"voices": [], "error": str(e)})
        elif path == "/api/project-state":
            project = (qs.get("project", [""])[0] or "").strip()
            if not _safe_project(project):
                self._json({"error": "invalid project"}, 400)
            else:
                self._json(_project_state(project))
        elif path.startswith("/video/"):
            # /video/<lang>/<slug>.mp4 -> projects/<lang>/<slug>/output.mp4.
            # A plain .mp4 path (no query string) -- Chrome's media stack and
            # privacy/extension layers treat that far more predictably than a
            # video behind an /api/...?project=... URL.
            project = path[len("/video/"):].removesuffix(".mp4")
            if not _safe_project(project):
                self.send_error(400, "invalid project")
            else:
                self._serve_video(PROJECTS_DIR / project / "output.mp4")
        elif path == "/favicon.ico":
            # Browsers request this on every load; an empty 204 keeps them
            # satisfied without a 404 round-trip.
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404, "Not found")

    def do_POST(self):
        if self.path == "/api/run":
            self._run()
        elif self.path == "/api/stop":
            body = self._read_json()
            stopped = _stop_process(body.get("run_id", ""))
            self._json({"stopped": stopped})
        elif self.path == "/api/git-sync":
            # Pull the latest code from GitHub (same as the picker's Sync
            # button). Plain git; nothing is executed from the pulled files.
            self._json(_git_sync())
        else:
            self.send_error(404, "Not found")

    def _serve_index(self):
        try:
            body = INDEX_HTML.read_bytes()
        except OSError:
            self.send_error(500, "index.html missing")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Always revalidate -- otherwise the browser keeps serving a cached
        # copy of the UI long after index.html changed on disk.
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        # Chrome's media stack probes video URLs with HEAD before streaming;
        # answering 501 (the BaseHTTPRequestHandler default for unimplemented
        # methods) makes the <video> element stall forever at 0:00.
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/video/"):
            project = parsed.path[len("/video/"):].removesuffix(".mp4")
            if not _safe_project(project):
                self.send_error(400, "invalid project")
            else:
                self._serve_video(PROJECTS_DIR / project / "output.mp4", head=True)
        else:
            self.send_error(404, "Not found")

    def _serve_video(self, path, head=False):
        """Stream a project's output.mp4 with Range support so the wizard's
        Done screen can embed a <video> player that starts and scrubs
        instantly. (The render writes +faststart, so metadata is up front.)"""
        import re as _re

        if not path.is_file():
            self.send_error(404, "no output.mp4 yet")
            return
        size = path.stat().st_size
        start, end = 0, size - 1
        m = _re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        partial = bool(m and (m.group(1) or m.group(2)))
        if partial:
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            else:
                start = max(0, size - int(m.group(2)))
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Cache-Control", "no-cache")  # re-renders replace the file in place
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head:
            return
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _run(self):
        try:
            body = self._read_json()
        except (ValueError, UnicodeDecodeError):
            self._json({"error": "bad JSON body"}, 400)
            return

        script_key = body.get("script")
        project = (body.get("project") or "").strip()
        flags = body.get("flags") or []

        if script_key not in SCRIPTS:
            self._json({"error": f"unknown script '{script_key}'"}, 400)
            return
        if not _safe_project(project):
            self._json({"error": f"invalid project path '{project}'"}, 400)
            return
        if not isinstance(flags, list) or not all(isinstance(f, str) for f in flags):
            self._json({"error": "flags must be a list of strings"}, 400)
            return

        run_id, argv = _start_process(script_key, project, flags)
        self._json({"run_id": run_id, "argv": argv})

    def _status(self, run_id):
        with RUNS_LOCK:
            entry = RUNS.get(run_id)
        if not entry:
            self._json({"error": "unknown run_id"}, 404)
            return
        running = entry["proc"].poll() is None
        self._json({"running": running, "returncode": entry["returncode"]})

    def _stream(self, run_id):
        with RUNS_LOCK:
            entry = RUNS.get(run_id)
        if not entry:
            self.send_error(404, "unknown run_id")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        # No Content-Length (the stream ends when the child exits), so under
        # HTTP/1.1 the connection itself must signal the end of the body.
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        q = entry["queue"]
        try:
            while True:
                item = q.get()
                if item is None:
                    break
                self.wfile.write(item.encode("utf-8", "replace"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass  # client navigated away or closed the tab mid-stream


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open the browser")
    args = parser.parse_args()

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
        server = SingleBindHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        sys.exit(
            f"Port {args.port} is already in use (another dashboard still running?): {e}\n"
            f"Close it, or run again with --port {args.port + 1}."
        )
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Pipeline dashboard running at {url}")
    print("Ctrl+C to stop. Scripts you launch from the page keep running "
          "until they finish or you hit their Stop button.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    main()
