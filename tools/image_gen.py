"""AI image generation backends for the picker's "AI illustration" tab.

This module is deliberately standalone: it owns the network calls, the rate
limiting and the retry policy for every free/cheap image provider, and knows
nothing about scenes, selections or rendering. `step2_pick_clips.py` imports
it and keeps doing what it already did -- build a prompt from the semantic
brief, cache the file, record it as an AI pick -- so nothing about the
existing workflow, cache names or selection format changes.

Why it exists (measured 2026-10-22): Pollinations' free tier is now *one
image every 45 seconds per IP*, and answers 402 {"code":"FREE_LIMIT"} when
you ask faster, plus an occasional 503 {"code":"SERVICE_UNAVAILABLE"} when
the shared queue is full. Older code paced itself at 15 s and treated both
as hard failures, so a second image in a row broke. Here:

  * pacing is per provider and read from PROVIDERS (45 s anonymous, 6 s with
    a token), with a small safety margin,
  * 402 / 429 / 503 are retried with the wait the server itself asks for
    (`Retry-After`, or "Retry after 28s" in the JSON body),
  * only a non-retryable error (bad prompt, auth) escapes to the caller,
    as a GenerationError with a message a human can act on.

Providers
---------
pollinations   free, no key. FLUX. Serves at most 1024x576 on the free tier
               (it silently downsizes a 1920x1080 request and watermarks the
               image unless a token is supplied). Verified working here.
cloudflare     Workers AI, FLUX.1-schnell. Free daily allowance, needs a
               free Cloudflare account: tools/cloudflare_key.txt with the
               account id on line 1 and the API token on line 2 (or
               "account_id:token" on one line), or CLOUDFLARE_ACCOUNT_ID +
               CLOUDFLARE_API_TOKEN. Fast (seconds), 1024px output. The
               request shape is implemented but NOT verified against a live
               account from this checkout -- check it once before a long run.
huggingface    Inference Providers, FLUX.1-schnell. Small monthly free
               credit, needs tools/huggingface_key.txt or HF_TOKEN. Also
               fast. Same caveat: implemented, not verified here.

Everything writes atomically through a .part file and is validated before
being kept (real JPEG/PNG/WebP signature, sane size), so a rate-limit HTML
page can never end up saved as a .jpg that ffmpeg chokes on later.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent

DEFAULT_TIMEOUT = 180
USER_AGENT = "Mozilla/5.0 (compatible; YouTubeAutomationTool/1.0; +https://github.com/)"

POLLINATIONS_IMAGE_URL = "https://image.pollinations.ai/prompt/"
POLLINATIONS_REFERRER = "youtube-automation-tool"
POLLINATIONS_MODEL = "flux"

CLOUDFLARE_KEY_FILE = TOOLS_DIR / "cloudflare_key.txt"
HUGGINGFACE_KEY_FILE = TOOLS_DIR / "huggingface_key.txt"
CLOUDFLARE_MODEL = "@cf/black-forest-labs/flux-1-schnell"
HUGGINGFACE_MODEL = "black-forest-labs/FLUX.1-schnell"

# interval:          seconds between calls without a token/key
# interval_with_token: seconds between calls with one
# max_attempts:      how many times to retry a retryable failure
PROVIDERS = {
    "pollinations": {
        "label": "Pollinations.AI (FLUX)",
        "credit_url": "https://pollinations.ai",
        "interval": 45.0,
        "interval_with_token": 6.0,
        "max_attempts": 4,
        # The free tier serves 1024x576 whatever you ask for; recorded so the
        # caller can say so in the log instead of leaving a mystery.
        "max_free_size": (1024, 576),
        "needs_key": False,
    },
    "cloudflare": {
        "label": "Cloudflare Workers AI (FLUX.1-schnell)",
        "credit_url": "https://developers.cloudflare.com/workers-ai/",
        "interval": 2.0,
        "interval_with_token": 2.0,
        "max_attempts": 3,
        "max_free_size": (1024, 1024),
        "needs_key": True,
    },
    "huggingface": {
        "label": "Hugging Face Inference (FLUX.1-schnell)",
        "credit_url": "https://huggingface.co/docs/inference-providers",
        "interval": 2.0,
        "interval_with_token": 2.0,
        "max_attempts": 3,
        "max_free_size": (1024, 1024),
        "needs_key": True,
    },
}

DEFAULT_PROVIDER = "pollinations"

# Look presets. "storybook" is the original locked house style, byte for byte,
# so existing projects keep their look and their cache keys.
STYLES = {
    "storybook": (
        "2D digital illustration in a warm hand-drawn storybook style, flat cel "
        "shading with gouache-like soft gradients, limited golden-hour palette of "
        "amber, ochre and muted teal, thick clean ink outlines on the foreground, "
        "layered hills and trees fading into atmospheric haze, gentle paper grain "
        "texture, painterly but clearly illustrated and NOT photorealistic, no lens "
        "blur, any people shown from behind or in silhouette with no recognisable "
        "face, no on-image text, no watermark, no logo, wide cinematic 16:9 "
        "composition"
    ),
    "cinematic-doc": (
        "photorealistic cinematic documentary photograph, shot on 35mm film with a "
        "natural available light, shallow depth of field, realistic skin and "
        "materials, muted teal and rust colour grade, fine film grain, believable "
        "everyday detail, no on-image text, no watermark, no logo, wide 16:9 "
        "composition. Avoid: distorted hands, extra fingers, malformed faces, "
        "plastic-looking surfaces, oversaturated colours"
    ),
    "retro-print": (
        "vintage 1970s print advertisement illustration, halftone dot texture, "
        "limited four-colour ink palette of warm cream, burnt orange, deep teal and "
        "charcoal, bold simplified shapes, slightly off-register colour, aged paper "
        "grain, no on-image text, no watermark, no logo, wide cinematic 16:9 "
        "composition"
    ),
    "blueprint": (
        "clean technical blueprint drawing, thin white line work on deep blue paper, "
        "isometric cutaway view, precise measured detail, subtle grid, no on-image "
        "text or labels, no watermark, no logo, wide cinematic 16:9 composition"
    ),
}

DEFAULT_STYLE = "storybook"

# provider -> monotonic timestamp of the last call, so pacing survives across
# scenes and across every caller in the process.
_LAST_CALL: dict[str, float] = {}
_PACE_MARGIN = 1.06


class GenerationError(RuntimeError):
    """A generation that did not produce a usable image.

    `retry_after` is set when the provider told us how long to wait -- callers
    that would rather not block (a web request thread) can show it to the user
    instead of sleeping."""

    def __init__(self, message, *, provider=None, retry_after=None, status=None):
        super().__init__(message)
        self.provider = provider
        self.retry_after = retry_after
        self.status = status


def provider_info(provider=None):
    return PROVIDERS.get(provider or DEFAULT_PROVIDER) or PROVIDERS[DEFAULT_PROVIDER]


def available_providers():
    return list(PROVIDERS)


def resolve_key(provider, token=None):
    """A token/key for `provider`: the explicit argument, then the documented
    env var, then the key file. Returns None when nothing is configured."""
    provider = provider or DEFAULT_PROVIDER
    if provider == "pollinations":
        if token:
            return token
        token = os.environ.get("POLLINATIONS_TOKEN")
        if token:
            return token.strip()
        if (TOOLS_DIR / "pollinations_token.txt").is_file():
            return (TOOLS_DIR / "pollinations_token.txt").read_text(encoding="utf-8").strip() or None
        return None
    if provider == "cloudflare":
        if token:
            return token
        acct = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        tok = os.environ.get("CLOUDFLARE_API_TOKEN")
        if acct and tok:
            return f"{acct}:{tok}"
        if CLOUDFLARE_KEY_FILE.is_file():
            return CLOUDFLARE_KEY_FILE.read_text(encoding="utf-8").strip() or None
        return None
    if provider == "huggingface":
        if token:
            return token
        tok = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_API_KEY")
        if tok:
            return tok.strip()
        if HUGGINGFACE_KEY_FILE.is_file():
            return HUGGINGFACE_KEY_FILE.read_text(encoding="utf-8").strip() or None
        return None
    return token


def pace(provider=None, token=None):
    """Sleep just long enough that this call respects the provider's minimum
    interval. Safe to call more often than needed: a call already spaced out
    returns immediately."""
    provider = provider or DEFAULT_PROVIDER
    cfg = provider_info(provider)
    interval = cfg["interval_with_token"] if token else cfg["interval"]
    if interval <= 0:
        return
    last = _LAST_CALL.get(provider)
    if last is not None:
        wait = interval * _PACE_MARGIN - (time.monotonic() - last)
        if wait > 0:
            time.sleep(min(wait, interval * _PACE_MARGIN))
    _LAST_CALL[provider] = time.monotonic()


def compose_prompt(subject, style=DEFAULT_STYLE, raw=False):
    """The subject from the scene's brief plus the chosen look. `raw` uses the
    caller's text verbatim (the 'use my text exactly' tick in the picker)."""
    subject = (subject or "").strip()
    if raw:
        return subject or STYLES.get(style, STYLES[DEFAULT_STYLE])
    style_text = STYLES.get(style, STYLES[DEFAULT_STYLE])
    subject = subject.rstrip(". ")
    return f"{subject}. {style_text}" if subject else style_text


def pollinations_url(prompt, seed, width, height, *, token=None):
    """The plain URL for an image. Stored on a pick so step 3 can re-download
    it if the cached file is ever lost."""
    params = {
        "width": int(width),
        "height": int(height),
        "seed": int(seed),
        "model": POLLINATIONS_MODEL,
        "referrer": POLLINATIONS_REFERRER,
    }
    if token:
        params["nologo"] = "true"
    return (POLLINATIONS_IMAGE_URL + urllib.parse.quote(prompt, safe="")
            + "?" + urllib.parse.urlencode(params))


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _request(url, *, data=None, headers=None, timeout=DEFAULT_TIMEOUT):
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), dict(resp.headers)


def _body_snippet(body, limit=200):
    """A short, printable description of an error response. Pollinations can
    answer 503 with a *JPEG placeholder* body, so a naive decode dumps binary
    noise into the console and the picker's status line."""
    if not body:
        return ""
    try:
        text = body.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        return f"(binary body, {len(body)} bytes)"
    printable = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in text)
    printable = " ".join(printable.split())
    if len(printable) < 8:
        return f"(binary body, {len(body)} bytes)"
    return printable[:limit]


def _retry_after_from(status, headers, body):
    """How long the provider asked us to wait, in seconds, or None. Handles
    both a Retry-After header and the JSON bodies these APIs actually send
    ("Retry after 28s", "retry after 10s")."""
    for key in ("Retry-After", "retry-after", "X-RateLimit-Reset-After"):
        raw = (headers or {}).get(key)
        if raw:
            try:
                return max(1.0, float(str(raw).strip()))
            except ValueError:
                pass
    if body:
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
        m = re.search(r"retry after\s+(\d+(?:\.\d+)?)\s*s", text, re.I)
        if m:
            return max(1.0, float(m.group(1)))
        m = re.search(r"try again in\s+(\d+(?:\.\d+)?)\s*s", text, re.I)
        if m:
            return max(1.0, float(m.group(1)))
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            err = payload.get("error") or {}
            if isinstance(err, dict):
                m = re.search(r"(\d+(?:\.\d+)?)\s*s", str(err.get("message", "")), re.I)
                if m:
                    return max(1.0, float(m.group(1)))
    return None


def _is_retryable(status):
    # 402 is the free-tier "slow down", 429 the classic rate limit, 5xx the
    # shared queue. 400/401/403 mean the request or the key is wrong.
    return status in (402, 408, 425, 429, 500, 502, 503, 504)


# --------------------------------------------------------------------------
# Backends -- each returns (image_bytes, plain_url_or_empty)
# --------------------------------------------------------------------------

def _pollinations(prompt, seed, width, height, token, timeout):
    url = pollinations_url(prompt, seed, width, height, token=token)
    headers = {"Accept": "image/*"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data, _ = _request(url, headers=headers, timeout=timeout)
    return data, pollinations_url(prompt, seed, width, height, token=None)


def _cloudflare_creds(token):
    token = (token or resolve_key("cloudflare") or "").strip()
    if not token:
        raise GenerationError(
            "Cloudflare Workers AI needs a free account key.\n"
            f"Save 'account_id' on line 1 and the API token on line 2 of {CLOUDFLARE_KEY_FILE.name}\n"
            "(tools/), or set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN.\n"
            "Docs: https://developers.cloudflare.com/workers-ai/",
            provider="cloudflare",
        )
    if "\n" in token or ":" in token:
        parts = [p.strip() for p in re.split(r"[\r\n:]+", token) if p.strip()]
        if len(parts) >= 2:
            return parts[0], parts[1]
    raise GenerationError(
        "Cloudflare key file must contain the account id and the API token "
        "(account id on line 1, token on line 2, or 'account_id:token').",
        provider="cloudflare",
    )


def _cloudflare(prompt, seed, width, height, token, timeout):
    account, api_token = _cloudflare_creds(token)
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{CLOUDFLARE_MODEL}"
    body = {"prompt": prompt, "steps": 4}
    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
    data = json.dumps(body).encode("utf-8")
    raw, _ = _request(url, data=data, headers=headers, timeout=timeout)
    payload = json.loads(raw.decode("utf-8", "replace"))
    if isinstance(payload.get("result"), dict):
        b64 = payload["result"].get("image")
    else:
        b64 = payload.get("result") if isinstance(payload.get("result"), str) else None
    if not b64:
        raise GenerationError(f"Cloudflare returned no image: {str(payload)[:300]}",
                              provider="cloudflare")
    return base64.b64decode(b64), ""


def _huggingface(prompt, seed, width, height, token, timeout):
    token = (token or resolve_key("huggingface") or "").strip()
    if not token:
        raise GenerationError(
            "Hugging Face Inference needs a free access token.\n"
            f"Save it to tools/{HUGGINGFACE_KEY_FILE.name} or set HF_TOKEN.\n"
            "Docs: https://huggingface.co/docs/inference-providers",
            provider="huggingface",
        )
    url = f"https://router.huggingface.co/hf-inference/models/{HUGGINGFACE_MODEL}"
    body = json.dumps({
        "inputs": prompt,
        "parameters": {"width": int(width), "height": int(height), "seed": int(seed)},
    }).encode("utf-8")
    raw, headers = _request(url, data=body, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "Accept": "image/png"}, timeout=timeout)
    if raw[:1] == b"{":
        raise GenerationError(f"Hugging Face returned an error: {raw[:300].decode('utf-8', 'replace')}",
                              provider="huggingface")
    return raw, ""


_BACKENDS = {
    "pollinations": _pollinations,
    "cloudflare": _cloudflare,
    "huggingface": _huggingface,
}


# --------------------------------------------------------------------------
# Validation / public entry point
# --------------------------------------------------------------------------

def looks_like_image(data):
    if not data or len(data) < 4096:
        return False
    if data[:3] == b"\xff\xd8\xff":
        return True
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


def image_size(path):
    """(width, height) of a saved image, or None. Uses Pillow when present
    (it is a project requirement) and stays quiet when it isn't."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def generate(prompt, dest, *, seed, width, height, provider=None, token=None,
             timeout=DEFAULT_TIMEOUT):
    """Generate one image and write it to `dest`.

    Returns the plain Pollinations URL for the same image (or "" for providers
    that don't expose one). Pacing is handled here, so callers don't need to
    sleep first. Raises GenerationError after the provider's attempts are
    exhausted, carrying `retry_after` when the server named a wait.
    """
    provider = provider or DEFAULT_PROVIDER
    if provider not in _BACKENDS:
        raise GenerationError(f"Unknown image provider {provider!r}. "
                              f"Choose from: {', '.join(_BACKENDS)}.")
    cfg = provider_info(provider)
    backend = _BACKENDS[provider]
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    attempts = max(1, int(cfg.get("max_attempts", 3)))
    last_error = None
    for attempt in range(1, attempts + 1):
        pace(provider, token=token)
        try:
            data, plain_url = backend(prompt, seed, width, height, token, timeout)
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = e.read()
            except Exception:
                pass
            wait = _retry_after_from(e.code, dict(e.headers or {}), body)
            detail = ""
            if body:
                try:
                    payload = json.loads(body.decode("utf-8", "replace"))
                    detail = str((payload.get("error") or {}).get("message")
                                 or payload.get("message") or "")[:200]
                except Exception:
                    detail = _body_snippet(body)
            last_error = GenerationError(
                f"{cfg['label']} returned HTTP {e.code}"
                + (f": {detail}" if detail else "") + f" (attempt {attempt}/{attempts})",
                provider=provider, retry_after=wait, status=e.code)
            if not _is_retryable(e.code) or attempt == attempts:
                raise last_error from None
            # 5xx means the shared queue is full rather than "you asked too
            # fast", so give it progressively more room than a 402's hint.
            backoff = max(wait or 0.0, 10.0 * attempt)
            time.sleep(min(backoff, 120.0))
            continue
        except urllib.error.URLError as e:
            last_error = GenerationError(
                f"{cfg['label']} could not be reached: {e.reason} "
                f"(attempt {attempt}/{attempts})", provider=provider)
            if attempt == attempts:
                raise last_error from None
            time.sleep(min(5.0 * attempt, 60.0))
            continue

        if not looks_like_image(data):
            snippet = data[:160].decode("utf-8", "replace") if data else "(empty)"
            last_error = GenerationError(
                f"{cfg['label']} did not return an image (attempt {attempt}/{attempts}): {snippet}",
                provider=provider)
            if attempt == attempts:
                raise last_error
            time.sleep(min(5.0 * attempt, 60.0))
            continue

        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
        return plain_url

    raise last_error or GenerationError(f"{cfg['label']} failed.", provider=provider)
