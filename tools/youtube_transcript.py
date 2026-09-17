"""Fetch a YouTube video's transcript as one plain-text block, no timestamps.

Used by step0_build_script.py's --yt-link flag: paste a video URL, get back
the spoken words with no [00:12] markers, no speaker labels, no formatting --
just the text, ready to drop into script.txt and hand to the Gemini rewrite
pass.

Two free methods are tried, in order:

1. youtube-transcript-api -- fast, no extra setup, works most of the time.
2. yt-dlp -- pulls the subtitle track directly. YouTube has been blocking a
   growing share of anonymous/cloud/datacenter IPs from method 1 (and even
   from yt-dlp's own anonymous requests) with 429s or "confirm you're not a
   bot" -- yt-dlp gets past that if it can present cookies from a real,
   logged-in browser session, so this is tried with a cookies.txt file if
   one exists (see COOKIES_HELP below), then without.

If both fail, the video simply doesn't have a transcript available from this
network right now -- fetch_transcript_text raises one clear
YouTubeTranscriptError explaining exactly what to do next, instead of a raw
traceback.
"""
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEFAULT_LANGUAGES = ("en", "en-US", "en-GB")

_BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PATH_ID_RE = re.compile(r"^/(?:embed|shorts|live)/([A-Za-z0-9_-]{11})")

# Optional cookies.txt (Netscape format) for the yt-dlp fallback. Export one
# with a browser extension like "Get cookies.txt LOCALLY" while logged into
# YouTube, save it here, and yt-dlp will use it to look like a real browser
# instead of an anonymous IP.
COOKIES_FILE = Path(__file__).with_name("cookies.txt")
# Same content as an env var, for a GitHub Codespace: a Codespaces Secret
# named YT_COOKIES holding the exported cookies.txt text. It's materialised
# into COOKIES_FILE on first use, so it survives the codespace being rebuilt
# (a file dropped into tools/ would not).
COOKIES_ENV = "YT_COOKIES"

COOKIES_HELP = (
    "Free fix: log into YouTube in your normal browser (a secondary Google "
    "account is safer than your channel's), export its cookies with a browser "
    "extension (e.g. \"Get cookies.txt LOCALLY\" for Chrome/Firefox), and save "
    "the file as:\n"
    f"  {COOKIES_FILE}\n"
    f"(or, in a Codespace, paste the file's text into a Codespaces Secret named "
    f"{COOKIES_ENV} and restart the codespace).\n"
    "Then run the command again -- yt-dlp will use those cookies to fetch "
    "the transcript as a logged-in browser instead of an anonymous IP."
)


def normalize_cookies_text(text):
    """Netscape cookie lines must be TAB-separated (7 fields), but a file
    that went through a chat window, an editor or a web form usually comes
    back with the tabs turned into spaces -- which MozillaCookieJar then
    silently rejects. Re-split each cookie line on whitespace into its 7
    fields and re-join with tabs; comments and blank lines pass through."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(stripped)
            continue
        parts = stripped.split(None, 6)
        out.append("\t".join(parts) if len(parts) == 7 else stripped)
    return "\n".join(out).strip() + "\n"


def cookies_file():
    """tools/cookies.txt (normalized in place if needed); else written from
    $YT_COOKIES if that is set; else None."""
    if COOKIES_FILE.exists():
        raw = COOKIES_FILE.read_text(encoding="utf-8", errors="replace")
        fixed = normalize_cookies_text(raw)
        if fixed != raw:
            COOKIES_FILE.write_text(fixed, encoding="utf-8")
        return COOKIES_FILE
    text = os.environ.get(COOKIES_ENV, "")
    if text.strip():
        COOKIES_FILE.write_text(normalize_cookies_text(text), encoding="utf-8")
        return COOKIES_FILE
    return None


class YouTubeTranscriptError(RuntimeError):
    pass


def get_video_id(url_or_id):
    """Pull an 11-char video ID out of any common YouTube URL shape, or pass
    a bare ID straight through."""
    candidate = url_or_id.strip()
    if _BARE_ID_RE.match(candidate):
        return candidate

    parsed = urlparse(candidate)
    host = parsed.netloc.lower()

    if host.endswith("youtu.be"):
        vid = parsed.path.strip("/").split("/")[0]
        if _BARE_ID_RE.match(vid):
            return vid

    if "youtube.com" in host:
        if parsed.path == "/watch":
            vid = parse_qs(parsed.query).get("v", [None])[0]
            if vid and _BARE_ID_RE.match(vid):
                return vid
        m = _PATH_ID_RE.match(parsed.path)
        if m:
            return m.group(1)

    raise YouTubeTranscriptError(f"Could not find a YouTube video ID in: {url_or_id}")


def _clean_text(text):
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise YouTubeTranscriptError("Transcript came back empty.")
    return text


# ---- method 1: youtube-transcript-api ---------------------------------

def _fetch_via_api(video_id, languages):
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api import (
        AgeRestricted,
        CouldNotRetrieveTranscript,
        NoTranscriptFound,
        RequestBlocked,
    )

    api = YouTubeTranscriptApi()
    try:
        fetched = api.fetch(video_id, languages=list(languages))
    except NoTranscriptFound:
        # None of the preferred languages exist -- take whatever the video
        # actually has (manually created tracks sort before auto-generated).
        transcript_list = api.list(video_id)
        transcript = next(iter(transcript_list))
        fetched = transcript.fetch()
    except AgeRestricted as e:
        # Not an IP/blocking problem -- retrying with yt-dlp won't help
        # either without the same cookies, so surface this immediately.
        raise YouTubeTranscriptError(
            f"Video is age-restricted and needs a logged-in session: {e}"
        ) from e
    except (RequestBlocked, CouldNotRetrieveTranscript) as e:
        raise YouTubeTranscriptError(str(e)) from e

    return _clean_text(" ".join(s.text.strip() for s in fetched if s.text and s.text.strip()))


# ---- method 2: yt-dlp (subtitle download, no video) --------------------

_VTT_TAG_RE = re.compile(r"<[^>]+>")
_VTT_TIME_LINE_RE = re.compile(r"-->")


def _vtt_to_text(vtt_path):
    """Turn a .vtt subtitle file into one deduplicated plain-text block.

    Auto-generated captions repeat lines across overlapping cues (rolling
    captions), so consecutive duplicate lines are dropped.
    """
    lines = Path(vtt_path).read_text(encoding="utf-8", errors="ignore").splitlines()
    out = []
    last = None
    for line in lines:
        line = line.strip()
        if not line or line == "WEBVTT":
            continue
        if line.startswith(("NOTE", "STYLE", "Kind:", "Language:")):
            continue
        if _VTT_TIME_LINE_RE.search(line):
            continue
        if line.isdigit():
            continue
        line = _VTT_TAG_RE.sub("", line).strip()
        if not line or line == last:
            continue
        out.append(line)
        last = line
    return _clean_text(" ".join(out))


def _fetch_via_ytdlp(video_id, languages, cookiefile=None):
    try:
        import yt_dlp
    except ImportError as e:
        raise YouTubeTranscriptError(
            "yt-dlp is not installed. Free fix: pip install yt-dlp"
        ) from e

    url = f"https://www.youtube.com/watch?v={video_id}"
    with tempfile.TemporaryDirectory() as tmp:
        outtmpl = str(Path(tmp) / "%(id)s.%(ext)s")
        opts = {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": list(languages) + ["en"],
            "subtitlesformat": "vtt",
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
        }
        if cookiefile:
            opts["cookiefile"] = str(cookiefile)

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except yt_dlp.utils.DownloadError as e:
            raise YouTubeTranscriptError(str(e)) from e

        vtt_files = sorted(Path(tmp).glob(f"{video_id}*.vtt"))
        if not vtt_files:
            raise YouTubeTranscriptError(
                "yt-dlp ran but found no caption track for this video."
            )

        # Prefer an exact language match, in the caller's preference order.
        for lang in list(languages) + ["en"]:
            for f in vtt_files:
                if f".{lang}." in f.name or f.name.endswith(f".{lang}.vtt"):
                    return _vtt_to_text(f)
        return _vtt_to_text(vtt_files[0])


# ---- public entry point -------------------------------------------------

def fetch_transcript_text(url_or_id, languages=DEFAULT_LANGUAGES):
    """Return the video's transcript as one cleaned plain-text string.

    Tries youtube-transcript-api first, then falls back to yt-dlp (with a
    cookies.txt file if one is present) so a single blocked/rate-limited
    request doesn't fail the whole run.
    """
    video_id = get_video_id(url_or_id)

    errors = []
    try:
        return _fetch_via_api(video_id, languages)
    except YouTubeTranscriptError as e:
        errors.append(f"youtube-transcript-api: {e}")

    cookiefile = cookies_file()
    try:
        return _fetch_via_ytdlp(video_id, languages, cookiefile=cookiefile)
    except YouTubeTranscriptError as e:
        errors.append(f"yt-dlp{' (with cookies.txt)' if cookiefile else ''}: {e}")

    detail = "\n".join(f"  - {e}" for e in errors)
    raise YouTubeTranscriptError(
        f"Could not get a transcript for {url_or_id} using either method:\n"
        f"{detail}\n\n"
        f"{COOKIES_HELP}"
    )
