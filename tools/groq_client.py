"""Minimal Groq chat-completions client for the news script builder.

Deliberately dependency-free (urllib only), matching the rest of this
pipeline -- no `openai` or `groq` package to install.

Two things here are not optional and are the result of real failures, not
caution:

1. **The User-Agent header.** Groq sits behind Cloudflare, which blocks
   urllib's default `Python-urllib/3.x` and answers `403` with the body
   `error code: 1010` no matter how valid your key is. This is the exact same
   trap `step2_pick_clips.py` hits with Pexels, with a byte-identical error
   body. Verified 2026-07-22: the same request fails with the default UA and
   succeeds with a browser one. A genuinely bad key returns `401`, not `403`.
   Note `GET /models` is NOT behind this block, so a key can look fine while
   every completion 403s.

2. **Reasoning-model output handling.** Several models here don't put their
   answer where you'd expect:
   - `openai/gpt-oss-*` return prose in `message.content` but spend tokens on
     a separate reasoning channel first. With a low `max_completion_tokens`
     the budget is consumed by reasoning and `content` comes back EMPTY.
   - `qwen/qwen3.6-*` emit a `<think>` block inline at the start of `content`,
     and it is not always closed -- an unterminated `<think>` swallows the
     whole script if you only strip well-formed pairs.
   `strip_reasoning()` handles both shapes.

3. **The tokens-per-minute budget is the real constraint, not requests.**
   Measured on this key 2026-07-22: `x-ratelimit-limit-requests: 1000` but
   `x-ratelimit-limit-tokens: 12000`, and tokens are per MINUTE. A
   section-by-section script build resends the whole fact brief with every
   request, so it exhausts 12k tokens long before it runs out of requests.
   `_BUDGET` paces requests against that window so the 429 never happens.
   Reserving is based on `max_completion_tokens`, because that is what the API
   counts against you up front -- not what the model actually returns.

4. **A `retry-after` can be far longer than you want to wait.** When this key
   hit the limit, Groq answered 429 with `retry-after: 5412` -- ninety minutes.
   Sleeping that long is indistinguishable from a hang: the first build to hit
   it sat silent until it was killed. `MAX_RETRY_WAIT` caps what we will sit
   through and turns anything longer into an error that says when to come back.
"""
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.groq.com/openai/v1/chat/completions"

# Free-tier tokens per minute, from x-ratelimit-limit-tokens. See note 3.
TPM_LIMIT = 12000

# Longest retry-after we will actually wait out. See note 4. Anything beyond
# this is a daily-quota style block, not a blip, and the caller deserves to be
# told rather than left watching a silent process.
MAX_RETRY_WAIT = 180.0

# See note 1 in the module docstring -- do not remove.
USER_AGENT = "Mozilla/5.0 (compatible; VideoPipeline/1.0)"

KEY_FILE = Path(__file__).parent / "groq_key.txt"

# llama-3.3-70b-versatile (the previous default) was removed from Groq
# entirely -- confirmed 2026-08-18 via GET /models, it's no longer in the
# list for this key and every completion 404s with "model_not_found". Groq
# decommissions models on its own schedule with no warning to callers.
#
# Re-ran the same head-to-head (2026-08-18, opening 250 words of a 3-source
# rate-rise story) against what's left:
#   openai/gpt-oss-20b   at the full 4000-token budget: ~2,600 tokens go to
#                        hidden reasoning before any content, but the content
#                        that follows is clean -- avg sentence ~15w, in the
#                        12-18w target, no markdown, no stray digits. At the
#                        1200-token budget this project used to test with,
#                        it returns EMPTY (all budget spent reasoning) -- see
#                        note 2, this is the failure mode it warns about.
#   qwen/qwen3.6-27b     still fails outright: even given the full 4000-token
#                        budget it never closes its <think> block, so 100% of
#                        the response is unterminated reasoning and
#                        strip_reasoning() has nothing left to return.
#   openai/gpt-oss-120b  not re-tested; already rejected 2026-07-22 for
#                        21.2w avg sentences, too long-winded for this slot.
#   groq/compound(-mini) not tried -- these are agentic/tool-calling compound
#                        systems, not a plain chat model, and a poor fit for
#                        fixed-output script generation.
#
# gpt-oss-20b's reasoning tax means every request costs ~2,600+ tokens before
# it writes a word, which eats a big chunk of the 12,000 TPM budget per call
# -- expect _TokenBudget to pace harder than it did under llama. Don't drop
# max_tokens below ~4000 for this model or you'll hit the empty-content path.
DEFAULT_MODEL = "openai/gpt-oss-20b"


class GroqError(RuntimeError):
    pass


def load_keys(explicit=None):
    """All available Groq API keys, in priority order.

    --api-key wins outright (one key). Otherwise GROQ_API_KEY (comma- or
    newline-separated for more than one), otherwise every non-blank,
    non-comment line in tools/groq_key.txt -- one key per line. Keeping more
    than one key there lets complete() fall over to the next account when one
    key's TPM/TPD budget is exhausted; see the key-rotation notes on
    complete().
    """
    import os

    if explicit:
        return [explicit.strip()]
    env = os.environ.get("GROQ_API_KEY")
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
    raise GroqError(
        f"No Groq API key. Save one (or more, one per line) to {KEY_FILE.name}, "
        f"set GROQ_API_KEY, or pass --api-key. Get one free at "
        f"https://console.groq.com/keys"
    )


def load_key(explicit=None):
    """A single API key -- the first one load_keys() finds. Kept for callers
    that only ever use one key at a time."""
    return load_keys(explicit)[0]


_THINK_OPEN = re.compile(r"<think>", re.I)


def strip_reasoning(text):
    """Remove a reasoning model's <think> block, closed or not.

    An unterminated <think> is the dangerous case: qwen opened one and never
    closed it, so a naive `<think>.*?</think>` strip left the entire internal
    monologue sitting in the script. If we see an opening tag with no closing
    tag, everything from it onward is monologue, so drop the tail.
    """
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    m = _THINK_OPEN.search(text)
    if m:
        text = text[: m.start()]
    return text.strip()


class _TokenBudget:
    """Rolling 60-second token budget, so we pace instead of getting 429'd.

    Groq charges a request against the minute window at the moment it is made,
    counting prompt tokens plus whatever `max_completion_tokens` reserves. We
    mirror that: reserve the estimate before sending, then correct it to the
    real usage the response reports, which is usually far lower and hands the
    budget back to the next request.
    """

    def __init__(self, limit=TPM_LIMIT):
        self.limit = limit
        self.events = []  # [[timestamp, tokens], ...]

    def _used(self, now):
        self.events = [e for e in self.events if e[0] > now - 60]
        return sum(e[1] for e in self.events)

    def reserve(self, tokens, verbose=True):
        """Block until `tokens` fit in the window. Returns the event to settle."""
        while True:
            now = time.time()
            used = self._used(now)
            if used + tokens <= self.limit or not self.events:
                break
            # Wait for the oldest reservation to age out of the window.
            wait = 60 - (now - self.events[0][0]) + 0.5
            if verbose:
                print(f"    pacing: {used:,}/{self.limit:,} tokens used this "
                      f"minute, waiting {wait:.0f}s")
            time.sleep(max(wait, 1.0))
        event = [time.time(), tokens]
        self.events.append(event)
        return event

    def settle(self, event, actual):
        """Replace a reservation with the tokens the request really cost."""
        if actual:
            event[1] = actual


_BUDGETS = {}


def _budget_for(key):
    """Each key gets its own rolling window -- they're separate accounts on
    Groq's side, so one key's usage must not throttle the other."""
    if key not in _BUDGETS:
        _BUDGETS[key] = _TokenBudget()
    return _BUDGETS[key]


def _estimate_prompt_tokens(messages):
    """Rough char/4 estimate. Only needs to be close enough to pace on."""
    chars = sum(len(m.get("content") or "") for m in messages)
    return chars // 4 + 8 * len(messages)


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

    Groq's free tier rate-limits readily -- a section-by-section script build
    fires a dozen or more requests in a row and WILL hit 429. The API sends a
    `retry-after` header when it does; honour it rather than guessing.

    `key` may be a single key string or a list/tuple of keys. With more than
    one, a key that's out of room -- a 413 (this one request needs more
    tokens than that account's TPM limit allows) or a 429 whose retry-after
    is a quota block rather than a blip -- rotates to the next key and
    retries immediately, instead of failing or sleeping through a wait that
    another account doesn't need to take. Each key gets its own token-pacing
    budget (see _budget_for) since they're separate accounts on Groq's side.
    """
    keys = list(key) if isinstance(key, (list, tuple)) else [key]
    key_idx = 0

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_completion_tokens": max_tokens,
    }

    delay = 4.0
    last_err = None
    for attempt in range(retries * len(keys)):
        active_key = keys[key_idx]
        headers = {
            "Authorization": f"Bearer {active_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        req = urllib.request.Request(
            API_URL, data=json.dumps(payload).encode("utf-8"), headers=headers
        )
        budget = _budget_for(active_key)
        reservation = budget.reserve(
            _estimate_prompt_tokens(messages) + payload["max_completion_tokens"],
            verbose=verbose,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.load(resp)
            budget.settle(reservation, (data.get("usage") or {}).get("total_tokens"))
            content = (data["choices"][0]["message"].get("content") or "").strip()
            content = strip_reasoning(content)
            if not content:
                # Almost always a reasoning model that spent its whole token
                # budget thinking. Retrying with more room usually fixes it.
                last_err = GroqError(
                    f"{model} returned empty content "
                    f"(usage: {data.get('usage')}). If this is a gpt-oss model, "
                    f"raise --max-tokens or use a non-reasoning model."
                )
                # Capped at the minute budget: doubling past it cannot succeed,
                # it just buys a guaranteed 429.
                payload["max_completion_tokens"] = min(
                    int(payload["max_completion_tokens"] * 2), TPM_LIMIT - 2000
                )
                continue
            return content
        except urllib.error.HTTPError as e:
            # Kept whole: the quota handler below parses this as JSON to report
            # which limit Groq actually hit, and a 300-char clip cuts mid-object.
            body = e.read().decode("utf-8", "replace")[:2000]
            more_keys = key_idx + 1 < len(keys)
            if e.code == 401:
                if more_keys:
                    if verbose:
                        print(f"    key {key_idx + 1} rejected (401) -- "
                              f"trying key {key_idx + 2}")
                    key_idx += 1
                    continue
                raise GroqError(f"Groq rejected the key (401). Check {KEY_FILE.name}.")
            if e.code == 403 and "1010" in body:
                raise GroqError(
                    "Groq returned 403 error code 1010 -- Cloudflare blocked the "
                    "request's User-Agent. Something stripped USER_AGENT from "
                    "this module's headers; it is required."
                )
            if e.code == 413:
                # This one request needs more tokens than the key's TPM limit
                # allows -- waiting never helps, only a bigger-budget key does.
                if more_keys:
                    if verbose:
                        print(f"    key {key_idx + 1} got 413 (request too "
                              f"large for its TPM limit) -- trying key "
                              f"{key_idx + 2}")
                    key_idx += 1
                    continue
                raise GroqError(
                    f"HTTP 413 from Groq on all {len(keys)} key(s) tried -- "
                    f"the request is too large for every key's TPM limit: {body}"
                )
            if e.code == 429 or e.code >= 500:
                wait = float(e.headers.get("retry-after") or delay)
                if wait > MAX_RETRY_WAIT:
                    # See note 4: a long retry-after is a quota block, and
                    # silently sleeping through it looks like a hung process.
                    if more_keys:
                        if verbose:
                            print(f"    key {key_idx + 1} quota-blocked for "
                                  f"{wait / 60:.0f}m -- trying key {key_idx + 2}")
                        key_idx += 1
                        continue
                    # Groq's own body names which bucket ran out (TPM vs TPD
                    # vs RPD) -- always pass it through rather than guessing,
                    # since the fix differs: TPM means slow down, TPD means
                    # come back tomorrow or add another key.
                    try:
                        detail = json.loads(body)["error"]["message"]
                    except (ValueError, KeyError, TypeError):
                        detail = body.strip() or "(no detail returned)"
                    raise GroqError(
                        f"Groq quota block on all {len(keys)} key(s), needs "
                        f"{wait / 60:.0f} more minutes (retry-after: {wait:.0f}s) "
                        f"-- stopped instead of sleeping through it.\n"
                        f"  Groq says: {detail}\n"
                        f"  Wait it out and re-run, add another key to "
                        f"{KEY_FILE.name} (one per line), or use a key with a "
                        f"higher limit. Anything already written to disk is kept."
                    )
                if verbose:
                    print(f"    {e.code} from Groq on key {key_idx + 1}, "
                          f"waiting {wait:.0f}s (attempt {attempt + 1})")
                time.sleep(wait)
                delay = min(delay * 2, 60)
                last_err = GroqError(f"HTTP {e.code}: {body}")
                continue
            raise GroqError(f"HTTP {e.code} from Groq: {body}")
        except (urllib.error.URLError, TimeoutError) as e:
            if verbose:
                print(f"    network error ({e}), retrying in {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2, 60)
            last_err = e
            continue

    raise GroqError(
        f"Groq failed after {retries * len(keys)} attempts across "
        f"{len(keys)} key(s). Last error: {last_err}"
    )
