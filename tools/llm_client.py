"""Minimal Google Gemini client for the video pipeline.

Dependency-free (urllib only), matching the rest of this pipeline -- no
`google-generativeai` package to install. Drop-in replacement for the old
`groq_client`: the public surface is identical
(`complete`, `load_keys`, `load_key`, `strip_reasoning`, `DEFAULT_MODEL`,
plus the error class), so callers were repointed with a one-line import
change.

Why Gemini, and the gotchas that shaped this file:

1. **The old model was decommissioned without warning.** Groq removed
   `llama-3.3-70b-versatile`, then this project's Groq key started getting
   `gemini-2.5-*` style redirects. Google does the same thing -- a pinned
   `gemini-3.6-flash` will 404 "no longer available to new users" once it's a
   few versions old. So DEFAULT_MODEL is the moving alias
   `gemini-flash-lite-latest`, which Google keeps pointed at the current
   flash-lite. Pass `--model gemini-3.5-flash` for a heavier model when
   script quality matters more than speed.

2. **Thinking tokens can eat the whole output budget.** Gemini 3.x flash
   ("thinking") models spend hundreds to >1000 tokens reasoning before the
   first word of the answer; with a tight `maxOutputTokens` the response
   comes back with `finishReason: MAX_TOKENS` and NO text. The lite models
   don't think, which is the other reason they're the default. `complete()`
   still handles the empty-text case the same way the Groq client did:
   double the output budget and retry. Do NOT send `thinkingConfig` to a
   lite model -- it makes the request fail outright (measured 2026-09).

3. **Response parts aren't all text.** A thinking model returns extra parts
   carrying only a `thoughtSignature` (no `text` key). Join only the parts
   that actually have `text`.

4. **429 carries its own retry hint.** Gemini answers `RESOURCE_EXHAUSTED`
   with `error.details[].retryDelay` like `"37s"`. Honour it, cap the wait
   at MAX_RETRY_WAIT (a per-day quota block returns a wait far longer than
   anyone wants to sit through -- turn that into an error that says when to
   come back), and rotate to the next key first if there is one.

5. **Free-tier pacing.** ~250k tokens/minute shared across models, ~15
   requests/minute for flash-lite, ~1000 requests/day. `_TokenBudget` paces
   tokens; a small per-key request spacing paces RPM. Each key gets its own
   windows -- keys are separate projects on Google's side.
"""
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Free-tier tokens per minute (shared across models on a project). See note 5.
TPM_LIMIT = 250_000

# Minimum seconds between requests on one key -- ~15 requests/minute for
# flash-lite on the free tier (note 5). Cheap insurance against a burst of
# per-scene auto-match calls tripping RESOURCE_EXHAUSTED on request count.
MIN_REQUEST_SPACING = 4.0

# Longest retry we'll actually wait out. See note 4 -- anything past this is a
# daily-quota block, not a blip, and silently sleeping through it is
# indistinguishable from a hang.
MAX_RETRY_WAIT = 180.0

# Gemini isn't behind the Cloudflare UA block Groq/Pexels are, but a real UA
# is harmless and keeps every outbound request in this pipeline consistent.
USER_AGENT = "Mozilla/5.0 (compatible; VideoPipeline/1.0)"

KEY_FILE = Path(__file__).parent / "gemini_key.txt"

# A moving alias, on purpose -- see note 1. Cleanest output of everything
# tested (returns bare JSON, not fenced), zero thinking-token overhead.
DEFAULT_MODEL = "gemini-flash-lite-latest"

# Heavier model for prose-quality work (step 0's news script). Still a
# concrete id rather than an alias because step 0 is run rarely and
# deliberately; swap it here if it 404s.
QUALITY_MODEL = "gemini-3.5-flash"


class LLMError(RuntimeError):
    pass


def load_keys(explicit=None):
    """All available Gemini API keys, in priority order.

    --api-key wins outright (one key). Otherwise GEMINI_API_KEY /
    GOOGLE_API_KEY (comma- or newline-separated for more than one), otherwise
    every non-blank, non-comment line in tools/gemini_key.txt -- one key per
    line. More than one key lets complete() roll over to the next project
    when one hits its per-minute or per-day quota.
    """
    import os

    if explicit:
        return [explicit.strip()]
    for env_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        env = os.environ.get(env_name)
        if env:
            keys = [k.strip() for k in env.replace(",", "\n").splitlines() if k.strip()]
            if keys:
                return keys
    if KEY_FILE.exists():
        keys = [
            line.strip() for line in KEY_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if keys:
            return keys
    raise LLMError(
        f"No Gemini API key. Save one (or more, one per line) to {KEY_FILE.name}, "
        f"set GEMINI_API_KEY, or pass --api-key. Get one free at "
        f"https://aistudio.google.com/apikey"
    )


def load_key(explicit=None):
    """A single API key -- the first one load_keys() finds. Kept for callers
    that only ever use one key at a time."""
    return load_keys(explicit)[0]


_THINK_OPEN = re.compile(r"<think>", re.I)


def strip_reasoning(text):
    """Remove a `<think>` block, closed or not.

    Gemini keeps its reasoning in a separate response part (filtered out in
    complete(), not here), so on the Gemini path this is almost always a
    no-op. Kept because callers import it and a model prompted to "think step
    by step" can still emit an inline block; an unterminated `<think>` would
    otherwise leave the whole monologue in the output.
    """
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    m = _THINK_OPEN.search(text)
    if m:
        text = text[: m.start()]
    return text.strip()


class _TokenBudget:
    """Rolling 60-second token budget, so we pace instead of getting 429'd.

    Reserve an estimate (prompt + max output) before sending, then correct it
    to the real usage the response reports -- usually far lower, which hands
    the budget straight back to the next request.
    """

    def __init__(self, limit=TPM_LIMIT):
        self.limit = limit
        self.events = []  # [[timestamp, tokens], ...]
        self.last_request = 0.0

    def _used(self, now):
        self.events = [e for e in self.events if e[0] > now - 60]
        return sum(e[1] for e in self.events)

    def reserve(self, tokens, verbose=True):
        """Block until `tokens` fit in the window AND the per-key request
        spacing has elapsed. Returns the event to settle."""
        while True:
            now = time.time()
            used = self._used(now)
            if used + tokens <= self.limit or not self.events:
                break
            wait = 60 - (now - self.events[0][0]) + 0.5
            if verbose:
                print(f"    pacing: {used:,}/{self.limit:,} tokens used this "
                      f"minute, waiting {wait:.0f}s")
            time.sleep(max(wait, 1.0))
        gap = time.time() - self.last_request
        if 0 < gap < MIN_REQUEST_SPACING:
            time.sleep(MIN_REQUEST_SPACING - gap)
        self.last_request = time.time()
        event = [time.time(), tokens]
        self.events.append(event)
        return event

    def settle(self, event, actual):
        if actual:
            event[1] = actual


_BUDGETS = {}


def _budget_for(key):
    """Each key gets its own rolling window -- separate projects on Google's
    side, so one key's usage must not throttle another."""
    if key not in _BUDGETS:
        _BUDGETS[key] = _TokenBudget()
    return _BUDGETS[key]


def _estimate_prompt_tokens(messages):
    """Rough char/4 estimate. Only needs to be close enough to pace on."""
    chars = sum(len(m.get("content") or "") for m in messages)
    return chars // 4 + 8 * len(messages)


def _to_gemini_payload(messages, temperature, max_tokens):
    """Convert OpenAI-style chat `messages` (the shape every caller in this
    pipeline already builds) into a Gemini generateContent body.

    - every `system` message is concatenated into `system_instruction`
    - `user` -> role "user", `assistant` -> role "model"
    - consecutive same-role turns are kept as separate entries; Gemini is
      lenient about that and none of this pipeline's prompts alternate
      strictly anyway.
    """
    system_bits, contents = [], []
    for m in messages:
        role = m.get("role") or "user"
        text = m.get("content") or ""
        if role == "system":
            if text:
                system_bits.append(text)
            continue
        contents.append({
            "role": "model" if role == "assistant" else "user",
            "parts": [{"text": text}],
        })
    if not contents:
        # A system-only prompt -- fold it into a single user turn so Gemini
        # has something to respond to.
        contents = [{"role": "user", "parts": [{"text": "\n\n".join(system_bits)}]}]
        system_bits = []
    body = {
        "contents": contents,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
    }
    if system_bits:
        body["system_instruction"] = {"parts": [{"text": "\n\n".join(system_bits)}]}
    return body


def _extract_text(data):
    """Join the text of every response part that actually carries text.

    Skips thought-signature-only parts (note 3). Returns "" if the model
    produced no text part at all (safety block, or MAX_TOKENS consumed by
    thinking -- note 2)."""
    for cand in data.get("candidates") or []:
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p["text"] for p in parts if isinstance(p, dict) and "text" in p)
        if text.strip():
            return text.strip(), cand.get("finishReason")
        return "", cand.get("finishReason")
    return "", None


def _retry_after_from_body(body):
    """Gemini puts the wait in error.details[].retryDelay as e.g. "37s"."""
    try:
        data = json.loads(body)
        for d in data.get("error", {}).get("details", []) or []:
            rd = d.get("retryDelay")
            if isinstance(rd, str) and rd.endswith("s"):
                return float(rd[:-1])
    except (ValueError, KeyError, TypeError):
        pass
    return None


def _error_message(body):
    try:
        return json.loads(body)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return body.strip() or "(no detail returned)"


def complete(
    messages,
    key,
    model=DEFAULT_MODEL,
    temperature=0.85,
    max_tokens=4000,
    retries=5,
    timeout=300,
    verbose=True,
):
    """One chat completion, with backoff on rate limits and transient errors.

    `messages` is the OpenAI chat shape ([{role, content}, ...]); it's
    converted to Gemini's contents/system_instruction here so callers didn't
    have to change. Returns the model's text.

    `key` may be a single key string or a list/tuple of keys. With more than
    one, a key that's out of room -- a 429 whose retry-after is a quota block
    rather than a blip -- rotates to the next key and retries immediately
    instead of sleeping through a wait another project doesn't need to take.
    Each key gets its own token + request pacing (see _budget_for).
    """
    keys = list(key) if isinstance(key, (list, tuple)) else [key]
    key_idx = 0
    out_budget = max_tokens

    delay = 4.0
    last_err = None
    for attempt in range(retries * len(keys)):
        active_key = keys[key_idx]
        url = f"{API_BASE}/{model}:generateContent"
        headers = {
            "x-goog-api-key": active_key,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        payload = _to_gemini_payload(messages, temperature, out_budget)
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), headers=headers
        )
        budget = _budget_for(active_key)
        reservation = budget.reserve(
            _estimate_prompt_tokens(messages) + out_budget, verbose=verbose
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.load(resp)
            budget.settle(reservation, (data.get("usageMetadata") or {}).get("totalTokenCount"))

            block = (data.get("promptFeedback") or {}).get("blockReason")
            if block:
                raise LLMError(f"Gemini blocked the prompt ({block}). Rephrase and retry.")

            content, finish = _extract_text(data)
            content = strip_reasoning(content)
            if not content:
                if finish == "MAX_TOKENS":
                    # A thinking model spent the whole output budget reasoning
                    # (note 2). More room usually fixes it; capped so we don't
                    # just buy a guaranteed token 429.
                    last_err = LLMError(
                        f"{model} returned no text (finishReason MAX_TOKENS, "
                        f"usage {data.get('usageMetadata')}). Retrying with a "
                        f"bigger output budget; use a non-thinking model "
                        f"(e.g. {DEFAULT_MODEL}) to avoid this."
                    )
                    out_budget = min(int(out_budget * 2), TPM_LIMIT - 5000)
                    continue
                last_err = LLMError(
                    f"{model} returned no text (finishReason {finish}, "
                    f"usage {data.get('usageMetadata')})."
                )
                if finish in ("SAFETY", "PROHIBITED_CONTENT", "RECITATION"):
                    raise last_err
                continue
            return content
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:2000]
            more_keys = key_idx + 1 < len(keys)
            if e.code in (400, 401) and ("API_KEY_INVALID" in body or "API key not valid" in body):
                if more_keys:
                    if verbose:
                        print(f"    key {key_idx + 1} invalid -- trying key {key_idx + 2}")
                    key_idx += 1
                    continue
                raise LLMError(f"Gemini rejected the key. Check {KEY_FILE.name}: {_error_message(body)}")
            if e.code == 403:
                raise LLMError(
                    f"Gemini returned 403 (permission denied) -- the key may not have the "
                    f"Generative Language API enabled, or the model is restricted. "
                    f"{_error_message(body)}"
                )
            if e.code == 404:
                # Model retired / not available to this key (note 1). The
                # body names the replacement -- pass it straight through.
                raise LLMError(
                    f"Gemini model '{model}' is unavailable: {_error_message(body)}\n"
                    f"  Set a current model with --model (e.g. gemini-3.5-flash), "
                    f"or update DEFAULT_MODEL in llm_client.py."
                )
            if e.code == 429 or e.code >= 500:
                wait = _retry_after_from_body(body)
                if wait is None:
                    wait = float(e.headers.get("retry-after") or delay)
                if wait > MAX_RETRY_WAIT:
                    if more_keys:
                        if verbose:
                            print(f"    key {key_idx + 1} quota-blocked for "
                                  f"{wait / 60:.0f}m -- trying key {key_idx + 2}")
                        key_idx += 1
                        continue
                    raise LLMError(
                        f"Gemini quota block on all {len(keys)} key(s), needs "
                        f"{wait / 60:.0f} more minutes (retry-after {wait:.0f}s) -- "
                        f"stopped instead of sleeping through it.\n"
                        f"  Gemini says: {_error_message(body)}\n"
                        f"  Wait it out and re-run, add another key to {KEY_FILE.name} "
                        f"(one per line), or use --model gemini-3.5-flash-lite for a "
                        f"higher daily request cap. Anything already written is kept."
                    )
                if verbose:
                    print(f"    {e.code} from Gemini on key {key_idx + 1}, "
                          f"waiting {wait:.0f}s (attempt {attempt + 1})")
                time.sleep(wait)
                delay = min(delay * 2, 60)
                last_err = LLMError(f"HTTP {e.code}: {body}")
                continue
            raise LLMError(f"HTTP {e.code} from Gemini: {body}")
        except (urllib.error.URLError, TimeoutError) as e:
            if verbose:
                print(f"    network error ({e}), retrying in {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2, 60)
            last_err = e
            continue

    raise LLMError(
        f"Gemini failed after {retries * len(keys)} attempts across "
        f"{len(keys)} key(s). Last error: {last_err}"
    )
