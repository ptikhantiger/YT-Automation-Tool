"""One-click launcher for the YouTube Automation Tool.

Runs a short preflight (Python version, ffmpeg, dependencies, API keys),
picks a free port, then starts the web dashboard, which opens your browser
on its own.

    python launch.py               # normal start
    python launch.py --port 9000   # force a specific port
    python launch.py --no-browser  # start the server but don't open a tab
    python launch.py --skip-checks # go straight to the dashboard

Windows users can just double-click run.bat, which calls this.

Output is deliberately plain ASCII: the Windows console defaults to cp1252
and raises UnicodeEncodeError on fancy characters.
"""
import argparse
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOLS = ROOT / "tools"
SERVER = TOOLS / "webui" / "server.py"
REQUIREMENTS = TOOLS / "requirements.txt"

DEFAULT_PORT = 8765
PORT_SEARCH_RANGE = 20

# import name -> pip name. Only what steps 1-3 actually need to start.
CORE_PACKAGES = [("edge_tts", "edge-tts"), ("PIL", "Pillow")]
# Needed only for step 0 --yt-link and the music picker.
OPTIONAL_PACKAGES = [("youtube_transcript_api", "youtube-transcript-api"), ("yt_dlp", "yt-dlp")]

# key file -> (label, required, signup url)
KEYS = [
    ("pexels_key.txt", "Pexels (stock clips)", True, "https://www.pexels.com/api/"),
    ("gemini_key.txt", "Gemini (script + auto-match)", False, "https://aistudio.google.com/apikey"),
    ("pixabay_key.txt", "Pixabay (extra source tab)", False, "https://pixabay.com/api/docs/"),
    ("coverr_key.txt", "Coverr (extra source tab)", False, "https://coverr.co/developers"),
]


def line(char="-", width=64):
    print(char * width)


def check_python():
    if sys.version_info < (3, 9):
        print(f"  [FAIL] Python {sys.version_info.major}.{sys.version_info.minor} is too old (need 3.9+).")
        return False
    print(f"  [ OK ] Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    return True


def check_ffmpeg():
    ok = True
    for exe in ("ffmpeg", "ffprobe"):
        path = shutil.which(exe)
        if path:
            print(f"  [ OK ] {exe}")
        else:
            print(f"  [FAIL] {exe} not found on PATH")
            ok = False
    if not ok:
        print()
        print("         ffmpeg renders the video and burns in the captions -- the")
        print("         pipeline cannot run without it. Install it and make sure")
        print("         it is on your PATH:")
        print("           winget install Gyan.FFmpeg      (Windows)")
        print("           https://ffmpeg.org/download.html")
    return ok


def missing_packages(packages):
    import importlib.util
    return [(mod, pip) for mod, pip in packages if importlib.util.find_spec(mod) is None]


def check_dependencies(auto_yes=False):
    missing = missing_packages(CORE_PACKAGES)
    if not missing:
        print("  [ OK ] Python packages (edge-tts, Pillow)")
    else:
        names = ", ".join(pip for _, pip in missing)
        print(f"  [FAIL] Missing required package(s): {names}")
        if not REQUIREMENTS.exists():
            print(f"         Expected {REQUIREMENTS} -- install manually: pip install {names}")
            return False
        if auto_yes or not sys.stdin.isatty():
            answer = "y"
        else:
            print()
            answer = input(f"         Install now from {REQUIREMENTS.name}? [Y/n] ").strip().lower()
        if answer in ("", "y", "yes"):
            print("         Installing...")
            rc = subprocess.call([sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)])
            if rc != 0:
                print("  [FAIL] pip install failed. Install manually and re-run.")
                return False
            if missing_packages(CORE_PACKAGES):
                print("  [FAIL] Packages still missing after install.")
                return False
            print("  [ OK ] Packages installed")
        else:
            print("         Skipped -- the pipeline will not run without them.")
            return False

    optional_missing = missing_packages(OPTIONAL_PACKAGES)
    if optional_missing:
        names = ", ".join(pip for _, pip in optional_missing)
        print(f"  [WARN] Optional packages absent: {names}")
        print("         Steps 1-3 work fine. These are only needed for step 0's")
        print("         --yt-link and the background music picker.")
    return True


def check_keys():
    """Keys are not fatal -- the dashboard should still open so the user can
    read the setup text. Only report what is and isn't there."""
    blocking = False
    for filename, label, required, url in KEYS:
        path = TOOLS / filename
        present = path.exists() and bool(path.read_text(encoding="utf-8", errors="replace").strip())
        if present:
            print(f"  [ OK ] {label}")
        elif required:
            print(f"  [FAIL] {label} -- missing tools/{filename}")
            print(f"         Required. Get a free key: {url}")
            blocking = True
        else:
            print(f"  [WARN] {label} -- no tools/{filename} (optional)")
    return not blocking


def find_free_port(preferred):
    for port in range(preferred, preferred + PORT_SEARCH_RANGE):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return None


def main():
    parser = argparse.ArgumentParser(description="Launch the video pipeline dashboard.")
    parser.add_argument("--port", type=int, default=None, help=f"Port to serve on (default: {DEFAULT_PORT}, or the next free one)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser tab")
    parser.add_argument("--skip-checks", action="store_true", help="Skip the preflight checks")
    parser.add_argument("--yes", action="store_true", help="Answer yes to the dependency install prompt")
    args = parser.parse_args()

    # Keep our own output in step with the server subprocess, which runs
    # unbuffered. Without this, redirecting to a file block-buffers this
    # process and the banner lands after the child's first lines.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    print()
    line("=")
    print("  YouTube Automation Tool")
    line("=")

    if not SERVER.exists():
        print(f"\n  [FAIL] Cannot find {SERVER}")
        print("         Run this from the project folder (next to tools/).")
        return 1

    if not args.skip_checks:
        print("\nChecking your setup:\n")
        ok_python = check_python()
        ok_ffmpeg = check_ffmpeg()
        ok_deps = check_dependencies(auto_yes=args.yes) if ok_python else False
        print()
        ok_keys = check_keys()

        if not (ok_python and ok_ffmpeg and ok_deps):
            print()
            line()
            print("  Setup incomplete -- fix the [FAIL] items above and re-run.")
            line()
            return 1
        if not ok_keys:
            print()
            print("  A Pexels key is required before you can pick clips, but the")
            print("  dashboard will still open so you can read the setup notes.")

    if args.port is not None:
        port = args.port
    else:
        port = find_free_port(DEFAULT_PORT)
        if port is None:
            print(f"\n  [FAIL] No free port between {DEFAULT_PORT} and {DEFAULT_PORT + PORT_SEARCH_RANGE - 1}.")
            return 1
        if port != DEFAULT_PORT:
            print(f"\n  Note: port {DEFAULT_PORT} is busy, using {port} instead.")

    print()
    line()
    print(f"  Dashboard:  http://127.0.0.1:{port}/")
    print("  Stop:       press Ctrl+C in this window")
    line()
    print()

    cmd = [sys.executable, "-u", str(SERVER), "--port", str(port)]
    if args.no_browser:
        cmd.append("--no-browser")

    try:
        return subprocess.call(cmd, cwd=str(TOOLS))
    except KeyboardInterrupt:
        print("\nShutting down.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
