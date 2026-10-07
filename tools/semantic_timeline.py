"""Semantic script -> voice -> scene synchronization engine.

Turns a script plus edge-tts word timestamps into a *semantic* scene
timeline: scene boundaries fall where the MEANING changes (subject, action,
location, event), never at arbitrary durations, and every scene knows
exactly which words of the script it represents and exactly when those
words are spoken.

The chain of authority is:

    SCRIPT (exact text, never rewritten here)
        -> TTS WORDS (real word-level timestamps from edge-tts)
        -> SENTENCES (script tokens aligned 1:1 to TTS words)
        -> SEGMENTS  (LLM visual-semantic grouping of whole sentences)
        -> SHOTS     (optional intra-segment visual events, exact word spans)
        -> SCENES    (what step2 picks clips for / step3 renders)

Word indices are the durable coordinate system: every sentence, segment,
shot, and scene stores `word_from`/`word_to` (inclusive, 0-based indices
into word_timings.json), and all start/end times are recomputed from those
indices via refresh_times() -- so anything that shifts timestamps later
(e.g. step1's --pause-every silence splicing) can't desynchronize the map.

The LLM (Gemini, same client/keys as step 0) is asked to do only what code
can't: group consecutive sentences into visual ideas, split multi-event
sentences into shots (by quoting the exact words -- verified here against
the real tokens, never trusted), and produce per-segment semantics: subject
/ action / object / location / time, visual requirements, visual
exclusions, and concrete stock-search queries. Everything structural
(coverage, ordering, word alignment, timing) is validated and repaired in
code. No Gemini key -- or any LLM failure -- falls back to a deterministic
sentence-boundary segmentation that is still strictly better than the old
random-duration cuts.

Run standalone to inspect/debug a project's semantic timeline:

    python semantic_timeline.py en/demo            # plan + write nothing
    python semantic_timeline.py en/demo --write    # rewrite scenes.json/timeline.json
"""
import json
import re
import sys
from pathlib import Path

from common import (
    _tokenize_for_alignment,
    word_token_ranges,
    compute_sentence_end_flags,
    compute_sentence_end_punctuation,
    load_json,
    save_json,
)
from keywords import build_query, extract_keywords

PROJECTS_DIR = Path(__file__).parent.parent / "projects"

# A scene shorter than this reads as a flash cut even in fast-paced edits;
# shorter segments are folded into a neighbor as a SHOT (the visual event
# survives, with its exact timing -- it just doesn't force a full clip pick).
MIN_SCENE_SECONDS = 2.5
# A single visual held longer than this goes dead on screen; longer segments
# are subdivided at sentence bounds (or natural pauses) into continuation
# shots of the same idea.
MAX_SCENE_SECONDS = 15.0

# Cap on sentences per LLM call -- keeps each request well inside the free
# tier's TPM budget and the response inside max_completion_tokens (every
# sentence now carries at least one shot with its own visual description).
BATCH_SENTENCES = 10

# --- Shot planning: the visual timeline is SENTENCE-DRIVEN. ------------------
# A SCENE is a story section (consecutive sentences about one topic); a SHOT
# is one short visual unit. Rules enforced in code, whatever the model returns:
#   * every sentence gets at least one shot of its own -- never shares a clip
#     with another sentence;
#   * a shot longer than MAX_SHOT_SECONDS is split at natural pauses into
#     complementary continuation shots of the same subject;
#   * a shot ON SCREEN for less than MIN_SHOT_SECONDS joins the previous
#     shot rather than flashing. On-screen time runs to the next shot's
#     start, so it includes the breath after the words: "Rain arrives." is
#     ~1.0 s spoken but ~1.4 s on screen -- a valid quick cut that keeps
#     its own visual.
MAX_SHOT_SECONDS = 6.0
MIN_SHOT_SECONDS = 0.8
# Upper bound on sentences per scene, purely so one scene in the clip picker
# stays a manageable unit; longer story sections continue in the next scene
# with the same semantics.
MAX_SCENE_SENTENCES = 8

SEGMENT_SYSTEM = """You are a professional documentary film editor and stock-footage researcher.
You turn a narration script into a VISUAL PLAN: story SCENES, each made of short
SHOTS, where every shot illustrates exactly the words being spoken at that moment.

SCENES (story sections):
- A scene covers one or more CONSECUTIVE whole sentences about the same topic,
  subject, place or event -- typically 2-6 sentences. Start a new scene when the
  story moves to a new topic, subject, place or time.
- Give each scene: meaning, subject, action, object, location, time, entities,
  visual_requirements (2-5 things that must be visible), visual_exclusions (0-4
  wrong-era/wrong-place/cliche items), queries (2-3 stock-search phrases).

SHOTS (the visual timeline) -- this is the important part:
- EVERY sentence gets AT LEAST ONE shot of its own. Never let one shot cover two
  sentences.
- A sentence that contains several visual ideas, or is long (more than ~12
  words), gets 2-3 shots. Each of those shots QUOTES the exact consecutive words
  of the sentence it covers, verbatim and in order.
- Each shot's "visual" is one concrete line of what the CAMERA SEES for those
  words: subject, action, framing. Follow the narration literally: "temperatures
  fall below minus 80" -> "frost-covered thermometer in a blizzard", not another
  landscape.
- Do NOT repeat the same visual on consecutive shots. Within a scene keep the
  theme but change the picture as the narration changes: landscape -> weather ->
  station -> people -> equipment, wide -> medium -> detail.
- Continuity: when the narration stays on the same subject, keep the same
  subject, place, people and era ("continuity":"continue"); when it moves on,
  switch ("continuity":"new"). Never cut to an unrelated place or person the
  narration did not mention.
- "query": 2-5 English words a stock site would tag (English regardless of the
  script language). Concrete nouns and actions only; never abstract words or
  emotions alone.

Return STRICT JSON only, no markdown, no commentary:
{"scenes":[{"first_sentence":N,"last_sentence":N,"meaning":"...","subject":"...",
"action":"...","object":"...","location":"...","time":"...","entities":["..."],
"visual_requirements":["..."],"visual_exclusions":["..."],"queries":["..."],
"shots":[{"sentence":N,"quote":"exact words (only needed when a sentence has 2+ shots)",
"visual":"...","query":"...","subject":"...","action":"...","location":"...",
"continuity":"continue|new"}]}]}
Cover every sentence exactly once, in order, with no gaps and no overlaps, and
give every sentence at least one shot."""


# ---------------------------------------------------------------------------
# Sentence layer: script tokens aligned 1:1 with TTS words


def sentences_from_words(words, script_text):
    """Split the narration into sentences with exact word-index spans.

    Uses the same token<->word alignment as the caption code (see
    compute_sentence_end_flags): script whitespace tokens carry punctuation,
    edge-tts words carry timestamps, and when counts match they are the same
    words in the same order. Returns a list of dicts:
        {"index": 1-based, "word_from": i, "word_to": j, "text": "..."}
    Text is rebuilt from the script tokens when aligned (so it keeps real
    punctuation/casing), else from the TTS words with fallback punctuation.
    """
    flags = compute_sentence_end_flags(words, script_text)
    punct = compute_sentence_end_punctuation(words, script_text)
    tokens, ranges = word_token_ranges(words, script_text)

    sentences = []
    start = 0
    for i, w in enumerate(words):
        if flags[i] or i == len(words) - 1:
            spans = [r for r in ranges[start : i + 1] if r]
            if spans and len(spans) >= (i + 1 - start) * 0.6:
                # script tokens (real punctuation/casing), even where the TTS
                # merged or split words
                text = " ".join(tokens[spans[0][0] : spans[-1][1] + 1])
            else:
                text = " ".join(
                    words[k]["text"] + (punct[k] or "") for k in range(start, i + 1)
                )
            sentences.append(
                {
                    "index": len(sentences) + 1,
                    "word_from": start,
                    "word_to": i,
                    "text": text,
                }
            )
            start = i + 1
    return sentences


# ---------------------------------------------------------------------------
# LLM segmentation


def _extract_json(text):
    """Parse the model's reply into a dict, tolerating markdown fences and
    stray prose around the JSON object."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last <= first:
        raise ValueError("no JSON object in reply")
    blob = text[first : last + 1]
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        # Most common model slip: trailing commas.
        repaired = re.sub(r",\s*([}\]])", r"\1", blob)
        return json.loads(repaired)


def _norm_tokens(text):
    """One normalized token per whitespace token: lowercased with all
    non-word characters stripped INSIDE the token (so a hyphenated
    "well-known" -- one edge-tts WordBoundary event -- normalizes to one
    token "wellknown" whether it came from a TTS word or an LLM quote).
    Punctuation-only tokens vanish. Numbers count as words -- '1969' must
    align."""
    out = []
    for tok in text.lower().split():
        norm = re.sub(r"[^\w]+", "", tok, flags=re.UNICODE)
        if norm:
            out.append(norm)
    return out


def _find_quote(quote, tokens_norm, cursor, slack=6):
    """Locate `quote`'s normalized tokens in the segment starting at or a
    few tokens after `cursor` (models routinely drop a connective like "and"
    between quoted shots -- those gap words get absorbed into the previous
    shot by the caller). Returns (match_start, match_end_exclusive) or None."""
    q = _norm_tokens(quote)
    if not q:
        return None
    limit = min(cursor + slack, len(tokens_norm) - len(q))
    for p in range(cursor, limit + 1):
        if tokens_norm[p : p + len(q)] == q:
            return p, p + len(q)
    return None


def _clean_str(value, limit=200):
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _clean_list(value, limit=8, item_limit=100):
    if not isinstance(value, list):
        return []
    out = []
    for v in value:
        if isinstance(v, str) and v.strip():
            out.append(v.strip()[:item_limit])
        if len(out) >= limit:
            break
    return out


def _llm_segment_batch(batch, lang, keys, model, prev_meaning, verbose=True):
    """One Gemini call turning a batch of numbered sentences into segments.
    Raises LLMError/ValueError upward -- the caller decides how to fall
    back. Returns the raw (validated-shape, uncleaned-coverage) segments."""
    from llm_client import complete, strip_reasoning

    lines = [f'S{s["index"]}: "{s["text"]}"' for s in batch]
    context = (
        f"\nFor continuity, the shot right before this batch showed: {prev_meaning}"
        if prev_meaning
        else ""
    )
    user = (
        f"SCRIPT SENTENCES (numbered, narration language: {lang}):\n"
        + "\n".join(lines)
        + context
        + "\n\nPlan scenes and shots for sentences S"
        + str(batch[0]["index"])
        + " through S"
        + str(batch[-1]["index"])
        + " following the rules -- at least one shot per sentence. STRICT JSON only."
    )
    reply = complete(
        [
            {"role": "system", "content": SEGMENT_SYSTEM},
            {"role": "user", "content": user},
        ],
        keys,
        model=model,
        temperature=0.2,
        max_tokens=7000,
        verbose=verbose,
    )
    data = _extract_json(strip_reasoning(reply))
    segments = data.get("scenes") or data.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("reply had no segments list")
    return segments


def _fallback_segment(sentence):
    """Deterministic single-sentence segment used when the LLM is
    unavailable or skipped a sentence -- keyword semantics only."""
    return {
        "first_sentence": sentence["index"],
        "last_sentence": sentence["index"],
        "meaning": sentence["text"],
        "subject": "",
        "action": "",
        "object": "",
        "location": "",
        "time": "",
        "entities": [],
        "visual_requirements": [],
        "visual_exclusions": [],
        "queries": [],
        "shots": [],
        "llm": False,
    }


def _clean_segment(raw, first, last):
    """Normalize one raw LLM segment into our internal shape (sentence range
    already validated by the caller)."""
    return {
        "first_sentence": first,
        "last_sentence": last,
        "meaning": _clean_str(raw.get("meaning"), 300),
        "subject": _clean_str(raw.get("subject")),
        "action": _clean_str(raw.get("action")),
        "object": _clean_str(raw.get("object")),
        "location": _clean_str(raw.get("location")),
        "time": _clean_str(raw.get("time")),
        "entities": _clean_list(raw.get("entities")),
        "visual_requirements": _clean_list(raw.get("visual_requirements")),
        "visual_exclusions": _clean_list(raw.get("visual_exclusions")),
        "queries": _clean_list(raw.get("queries"), limit=3),
        "shots": raw.get("shots") if isinstance(raw.get("shots"), list) else [],
        "llm": True,
    }


def _repair_coverage(raw_segments, batch):
    """Force the LLM's segments to cover the batch's sentences exactly once,
    in order. Anything malformed, overlapping, or missing becomes fallback
    single-sentence segments -- the timeline never has gaps or overlaps no
    matter what the model returned."""
    by_index = {s["index"]: s for s in batch}
    lo, hi = batch[0]["index"], batch[-1]["index"]
    cleaned = []
    next_needed = lo
    for raw in raw_segments:
        try:
            first = int(raw.get("first_sentence"))
            last = int(raw.get("last_sentence"))
        except (TypeError, ValueError):
            continue
        if last < first:
            first, last = last, first
        first, last = max(first, lo), min(last, hi)
        if first > last or last < next_needed:
            continue
        first = max(first, next_needed)  # clamp overlap with previous segment
        while next_needed < first:  # fill any gap the model left
            cleaned.append(_fallback_segment(by_index[next_needed]))
            next_needed += 1
        cleaned.append(_clean_segment(raw, first, last))
        next_needed = last + 1
    while next_needed <= hi:  # model stopped early
        cleaned.append(_fallback_segment(by_index[next_needed]))
        next_needed += 1
    return cleaned


def _split_span_n(words, w_from, w_to, n):
    """Cut [w_from, w_to] into n consecutive word spans of roughly equal
    DURATION, each cut placed on the widest natural pause near its
    time-balanced target (within +-35% of one share), so pieces are both
    even and breath-aligned. Never returns an empty span; returns fewer than
    n if the span has fewer words."""
    count = w_to - w_from + 1
    n = max(1, min(n, count))
    if n == 1:
        return [(w_from, w_to)]
    t0, t1 = words[w_from]["start"], words[w_to]["end"]
    share = (t1 - t0) / n
    cuts, lo = [], w_from
    for k in range(1, n):
        target = t0 + share * k
        hi = w_to - (n - k)  # leave at least one word per remaining piece
        if lo > hi:
            break
        window = [i for i in range(lo, hi + 1) if abs(words[i]["end"] - target) <= 0.35 * share]
        if window:
            i = max(window, key=lambda i: (words[i + 1]["start"] - words[i]["end"],
                                           -abs(words[i]["end"] - target)))
        else:
            i = min(range(lo, hi + 1), key=lambda i: abs(words[i]["end"] - target))
        cuts.append(i)
        lo = i + 1
    spans, a = [], w_from
    for c in cuts:
        spans.append((a, c))
        a = c + 1
    spans.append((a, w_to))
    return spans


def _shot(words, w_from, w_to, base, sentence_index, part=None):
    """One shot dict over the word span, carrying the visual metadata."""
    shot = {
        "word_from": w_from,
        "word_to": w_to,
        "text": " ".join(words[k]["text"] for k in range(w_from, w_to + 1)),
        "sentence_index": sentence_index,
        "visual": _clean_str(base.get("visual"), 240),
        "query": _clean_str(base.get("query")),
        "subject": _clean_str(base.get("subject")),
        "action": _clean_str(base.get("action")),
        "location": _clean_str(base.get("location")),
        "continuity": "continue" if str(base.get("continuity", "")).lower().startswith("cont") else "new",
    }
    if part is not None:
        shot["part"] = part
    return shot


def _plan_segment_shots(segment, sentences_by_index, words, lang, max_shot=MAX_SHOT_SECONDS):
    """The sentence-driven visual plan for one segment (scene).

    Every sentence of the segment gets its own shot(s):
      - the model's shots for that sentence, in order, each mapped to its
        exact quoted words (verified against the real tokens);
      - if quoting fails, the sentence is cut at its natural pauses into as
        many pieces as the model asked for, keeping the model's visuals;
      - if the model gave the sentence no shot at all (or there is no model),
        one shot over the whole sentence with keyword semantics.
    Then shots longer than MAX_SHOT_SECONDS are split into complementary
    continuation shots, and sub-MIN_SHOT_SECONDS shots join their neighbour.
    Shots always tile the segment's words exactly, in order."""
    first, last = segment["first_sentence"], segment["last_sentence"]
    raw = [r for r in (segment.get("shots") or []) if isinstance(r, dict)]
    seg_w_from = sentences_by_index[first]["word_from"]
    seg_w_to = sentences_by_index[last]["word_to"]
    tokens_all = {
        i: "".join(_norm_tokens(words[i]["text"])) for i in range(seg_w_from, seg_w_to + 1)
    }

    # assign each raw shot to a sentence: explicit number, else by its quote
    per_sentence = {k: [] for k in range(first, last + 1)}
    for r in raw:
        try:
            k = int(r.get("sentence"))
        except (TypeError, ValueError):
            k = None
        if k is None or k not in per_sentence:
            quote = r.get("quote") or ""
            k = None
            for cand in range(first, last + 1):
                sw = sentences_by_index[cand]
                toks = [tokens_all[i] for i in range(sw["word_from"], sw["word_to"] + 1)]
                if quote and _find_quote(quote, toks, 0) is not None:
                    k = cand
                    break
        if k is not None:
            per_sentence[k].append(r)

    seg_queries = [q for q in segment.get("queries") or [] if q]
    shots = []
    for k in range(first, last + 1):
        sent = sentences_by_index[k]
        a, b = sent["word_from"], sent["word_to"]
        mine = per_sentence[k]
        if not mine:
            fallback = {
                "visual": "",
                "query": build_query(sent["text"], lang=lang) or (seg_queries[0] if seg_queries else ""),
                "subject": segment.get("subject") or "",
                "action": segment.get("action") or "",
                "location": segment.get("location") or "",
                "continuity": "continue" if shots else "new",
            }
            shots.append(_shot(words, a, b, fallback, k))
            continue
        if len(mine) == 1:
            shots.append(_shot(words, a, b, mine[0], k))
            continue
        # several shots for this sentence: locate every quote on its own,
        # then put the shots in NARRATION order -- the model sometimes lists
        # them out of order, and a visual must sit on the words it depicts
        toks = [tokens_all[i] for i in range(a, b + 1)]
        located = []
        for r in mine:
            found = _find_quote(r.get("quote") or "", toks, 0) if r.get("quote") else None
            if found is None:
                located = None
                break
            located.append((found[0], r))
        spans = None
        if located:
            located.sort(key=lambda t: t[0])
            starts = [st for st, _r in located]
            if len(set(starts)) == len(starts):
                starts[0] = 0
                spans = [
                    (a + st, a + ((starts[j + 1] - 1) if j + 1 < len(starts) else len(toks) - 1))
                    for j, st in enumerate(starts)
                ]
                mine = [r for _st, r in located]
        if spans is None:
            spans = _split_span_n(words, a, b, len(mine))
        for (x, y), r in zip(spans, mine):
            shots.append(_shot(words, x, y, r, k))

    # complementary continuation shots for anything too long to hold
    def _pieces(x, y):
        """Split until every piece fits max_shot seconds (or is one word)."""
        st, en = _word_span_times(words, x, y)
        if en - st <= max_shot or x == y:
            return [(x, y)]
        n = int(-(-(en - st) // max_shot))  # ceil
        out = []
        for a, b in _split_span_n(words, x, y, n):
            out.extend(_pieces(a, b))
        return out

    split = []
    for sh in shots:
        pieces = _pieces(sh["word_from"], sh["word_to"])
        if len(pieces) == 1:
            split.append(sh)
            continue
        for j, (x, y) in enumerate(pieces):
            base = dict(sh)
            if j > 0:
                # A continuation shows what THESE words say, on the same
                # subject/place as the shot it continues -- never a query
                # borrowed from elsewhere in the scene.
                base["continuity"] = "continue"
                piece_text = " ".join(words[i]["text"] for i in range(x, y + 1))
                own = build_query(piece_text, lang=lang)
                if own:
                    # anchor it on the shot's subject so "bubbles trapped
                    # years" searches as "ice core bubbles trapped"
                    anchor = (sh.get("subject") or "").lower().split()
                    if anchor and anchor[-1] not in own.lower():
                        own = " ".join((anchor[-1:] + own.split())[:4])
                    base["query"] = own
                if sh.get("visual"):
                    base["visual"] = (
                        f"Continuing from: {sh['visual']} -- now showing what is said: {piece_text}"
                    )
            split.append(_shot(words, x, y, base, sh["sentence_index"], part=j + 1))

    # Shots that would only FLASH (on screen < MIN_SHOT_SECONDS, measured to
    # the next shot's start) join the previous shot; only a flash at the
    # very start of the segment is absorbed forward.
    def on_screen(k):
        start = words[split[k]["word_from"]]["start"]
        if k + 1 < len(split):
            return words[split[k + 1]["word_from"]]["start"] - start
        nxt = split[k]["word_to"] + 1
        return (words[nxt]["start"] if nxt < len(words) else words[split[k]["word_to"]]["end"]) - start

    merged = []
    carry = None  # a leading flash waiting to join the next shot
    for k, sh in enumerate(split):
        flash = on_screen(k) < MIN_SHOT_SECONDS
        if carry is not None:
            sh = dict(sh)
            sh["word_from"] = carry["word_from"]
            sh["text"] = carry["text"] + " " + sh["text"]
            carry = None
        if flash and merged:
            prev = merged[-1]
            prev["word_to"] = sh["word_to"]
            prev["text"] = prev["text"] + " " + sh["text"]
            continue
        if flash and k + 1 < len(split):
            carry = sh
            continue
        merged.append(sh)
    if carry is not None:
        merged.append(carry)
    return merged


def analyze_script(sentences, words, lang, llm_keys, model=None, verbose=True,
                   max_shot=MAX_SHOT_SECONDS):
    """LLM pass over all sentences -> list of validated segments with exact
    word spans and (where quoting succeeded) exact word-span shots.
    Never raises on model trouble: failed batches degrade to per-sentence
    fallback segments and the failure is noted in the returned meta."""
    from llm_client import DEFAULT_MODEL, LLMError

    model = model or DEFAULT_MODEL
    sentences_by_index = {s["index"]: s for s in sentences}
    segments = []
    failures = []
    prev_meaning = ""
    for i in range(0, len(sentences), BATCH_SENTENCES):
        batch = sentences[i : i + BATCH_SENTENCES]
        batch_segments = None
        last_err = None
        # A malformed reply (empty content, broken JSON) is usually a one-off
        # model slip, not a real outage -- one retry rescues most of them
        # before degrading this batch to plain sentence segments.
        for attempt in range(2):
            try:
                raw = _llm_segment_batch(batch, lang, llm_keys, model, prev_meaning, verbose=verbose)
                batch_segments = _repair_coverage(raw, batch)
                break
            except (LLMError, ValueError, json.JSONDecodeError) as e:
                last_err = e
        if batch_segments is None and len(batch) > 1 and not isinstance(last_err, LLMError):
            # Broken JSON is almost always a long reply going wrong; two
            # half-size requests usually both come back clean, keeping the
            # planned visuals instead of degrading 10 sentences to keywords.
            # (An LLMError -- quota, auth -- won't be fixed by halving.)
            half = len(batch) // 2
            parts = []
            for sub in (batch[:half], batch[half:]):
                try:
                    raw = _llm_segment_batch(sub, lang, llm_keys, model, prev_meaning, verbose=verbose)
                    parts.extend(_repair_coverage(raw, sub))
                except (LLMError, ValueError, json.JSONDecodeError) as e:
                    last_err = e
                    failures.append(f"sentences {sub[0]['index']}-{sub[-1]['index']}: {e}")
                    parts.extend(_fallback_segment(x) for x in sub)
            batch_segments = parts
        if batch_segments is None:
            failures.append(f"sentences {batch[0]['index']}-{batch[-1]['index']}: {last_err}")
            batch_segments = [_fallback_segment(s) for s in batch]
        for seg in batch_segments:
            seg["planned_shots"] = _plan_segment_shots(seg, sentences_by_index, words, lang, max_shot)
        segments.extend(batch_segments)
        if segments:
            last_shots = segments[-1].get("planned_shots") or []
            prev_meaning = (
                (last_shots[-1].get("visual") if last_shots else "")
                or segments[-1].get("meaning") or ""
            )
    return segments, failures


# ---------------------------------------------------------------------------
# Segments -> scenes (duration shaping + timing)


def _word_span_times(words, w_from, w_to):
    return words[w_from]["start"], words[w_to]["end"]


def _split_span_at_pause(words, w_from, w_to):
    """Best word index to split the span [w_from, w_to] after: the largest
    inter-word gap in the middle half of the span (a natural breath), falling
    back to the midpoint. Returns the LAST word index of the left half."""
    n = w_to - w_from + 1
    if n < 4:
        return w_from + n // 2 - 1
    lo = w_from + n // 4
    hi = w_from + (3 * n) // 4
    best_i, best_gap = None, -1.0
    for i in range(lo, min(hi, w_to)):
        gap = words[i + 1]["start"] - words[i]["end"]
        if gap > best_gap:
            best_gap, best_i = gap, i
    return best_i if best_i is not None else w_from + n // 2 - 1


def _segment_scene(segment, sentences_by_index, words, lang):
    """One segment -> one scene dict (without index/timing polish)."""
    first_s = sentences_by_index[segment["first_sentence"]]
    last_s = sentences_by_index[segment["last_sentence"]]
    w_from, w_to = first_s["word_from"], last_s["word_to"]
    text = " ".join(
        sentences_by_index[k]["text"]
        for k in range(segment["first_sentence"], segment["last_sentence"] + 1)
    )
    queries = [q for q in segment.get("queries") or [] if q]
    primary = queries[0] if queries else build_query(text, lang=lang)
    return {
        "text": text,
        "word_from": w_from,
        "word_to": w_to,
        "query": primary,
        "queries": queries or [primary],
        "keywords": extract_keywords(text, lang=lang),
        "semantic": {
            "meaning": segment.get("meaning") or "",
            "subject": segment.get("subject") or "",
            "action": segment.get("action") or "",
            "object": segment.get("object") or "",
            "location": segment.get("location") or "",
            "time": segment.get("time") or "",
            "entities": segment.get("entities") or [],
        },
        "visual_requirements": segment.get("visual_requirements") or [],
        "visual_exclusions": segment.get("visual_exclusions") or [],
        "shots": segment.get("planned_shots") or [],
        "llm": bool(segment.get("llm")),
        "sentence_from": segment["first_sentence"],
        "sentence_to": segment["last_sentence"],
    }


def _cap_scene_sentences(segments, max_sentences=MAX_SCENE_SENTENCES):
    """Split a story section longer than max_sentences into consecutive
    scenes of the same semantics, each keeping its own sentences' shots.
    Pure bookkeeping for the picker -- the visual plan is unchanged."""
    out = []
    for seg in segments:
        first, last = seg["first_sentence"], seg["last_sentence"]
        if last - first + 1 <= max_sentences:
            out.append(seg)
            continue
        for lo in range(first, last + 1, max_sentences):
            hi = min(lo + max_sentences - 1, last)
            part = dict(seg)
            part["first_sentence"], part["last_sentence"] = lo, hi
            part["planned_shots"] = [
                sh for sh in seg.get("planned_shots") or [] if lo <= sh["sentence_index"] <= hi
            ]
            out.append(part)
    return out


def _merge_short_scenes(scenes, words, min_seconds):
    """A scene shorter than min_seconds joins the previous scene: its shots
    (and their visuals) are kept intact, it just stops being its own scene."""
    out = []
    for scene in scenes:
        start, end = _word_span_times(words, scene["word_from"], scene["word_to"])
        if end - start >= min_seconds or not out:
            out.append(scene)
            continue
        host = out[-1]
        host["shots"] = (host.get("shots") or []) + (scene.get("shots") or [])
        host["word_to"] = scene["word_to"]
        host["text"] = host["text"] + " " + scene["text"]
        host["sentence_to"] = scene["sentence_to"]
        for key in ("visual_requirements", "visual_exclusions"):
            merged = host.get(key, []) + [x for x in scene.get(key, []) if x not in host.get(key, [])]
            host[key] = merged[:8]
        host["queries"] = (host.get("queries") or [])[:2] + [scene["query"]]
    return out


def validate_plan(scenes, sentences, words, max_shot):
    """Check the finished visual plan against the rules and report, so every
    step-1 run proves its own coverage instead of trusting it:
      - scenes tile every narrated word, in order, no gaps or overlaps;
      - each scene's shots tile that scene's words exactly;
      - every sentence starts its own visual (no sentence begins mid-shot,
        except a sub-MIN_SHOT_SECONDS fragment merged on purpose);
      - no visual is longer than max_shot (+ a merged fragment).
    Returns a dict for timeline.json meta; `ok` is False on any hard
    violation (gaps/overlaps), which would desynchronize step 3."""
    units = []  # (word_from, word_to) of every visual, in order
    gaps = []
    expect = 0
    for sc in scenes:
        if sc["word_from"] != expect:
            gaps.append(f"scene {sc.get('index')}: starts at word {sc['word_from']}, expected {expect}")
        expect = sc["word_to"] + 1
        shots = sc.get("shots") or []
        if shots:
            if shots[0]["word_from"] != sc["word_from"] or shots[-1]["word_to"] != sc["word_to"]:
                gaps.append(f"scene {sc.get('index')}: shots don't cover the scene edges")
            for a, b in zip(shots, shots[1:]):
                if b["word_from"] != a["word_to"] + 1:
                    gaps.append(f"scene {sc.get('index')}: gap/overlap between shots")
            units.extend((sh["word_from"], sh["word_to"]) for sh in shots)
        else:
            units.append((sc["word_from"], sc["word_to"]))
    if words and expect != len(words):
        gaps.append(f"narration words {expect}..{len(words) - 1} have no scene")

    starts = {u[0] for u in units}
    shared = []
    for k, snt in enumerate(sentences):
        if snt["word_from"] not in starts:
            nxt = sentences[k + 1]["word_from"] if k + 1 < len(sentences) else None
            end = words[nxt]["start"] if nxt is not None else words[snt["word_to"]]["end"]
            if end - words[snt["word_from"]]["start"] >= MIN_SHOT_SECONDS:
                shared.append(snt["index"])
    durs = [words[b]["end"] - words[a]["start"] for a, b in units]
    tolerance = max_shot + MIN_SHOT_SECONDS
    too_long = sum(1 for d in durs if d > tolerance)
    return {
        "ok": not gaps,
        "visual_units": len(units),
        "sentences": len(sentences),
        "sentences_without_own_shot": shared[:20],
        "sentences_without_own_shot_count": len(shared),
        "longest_seconds": round(max(durs), 2) if durs else 0,
        "median_seconds": round(sorted(durs)[len(durs) // 2], 2) if durs else 0,
        "over_cap": too_long,
        "problems": gaps[:20],
    }


def _finalize_shots(scenes):
    """Continuity context on every shot (previous / next visual across the
    whole video, scene meaning), and the single-shot compatibility rule: a
    scene with exactly one shot stores it on the scene itself (shots == [])
    so step 2/3 treat it as an ordinary single-clip scene."""
    flat = [(scene, sh) for scene in scenes for sh in (scene.get("shots") or [])]
    for i, (scene, sh) in enumerate(flat):
        prev_sh = flat[i - 1][1] if i > 0 else None
        next_sh = flat[i + 1][1] if i + 1 < len(flat) else None
        sh["context"] = {
            "scene_meaning": (scene.get("semantic") or {}).get("meaning") or "",
            "scene_location": (scene.get("semantic") or {}).get("location") or "",
            "previous_visual": (prev_sh.get("visual") or prev_sh.get("text") or "")[:200] if prev_sh else "",
            "next_visual": (next_sh.get("visual") or next_sh.get("text") or "")[:200] if next_sh else "",
        }
    for scene in scenes:
        shots = scene.get("shots") or []
        if len(shots) == 1:
            only = shots[0]
            if only.get("visual"):
                scene.setdefault("semantic", {})["visual"] = only["visual"]
            if only.get("query") and only["query"] not in (scene.get("queries") or []):
                scene["queries"] = [only["query"]] + (scene.get("queries") or [])
                scene["query"] = only["query"]
            scene["shot_context"] = only.get("context")
            scene["shots"] = []
    return scenes


def _merge_short(scenes, words, min_seconds):
    """Fold scenes shorter than min_seconds into a neighbor as an extra SHOT
    so the visual event keeps its own exact word span and timing without
    forcing a full-scene clip pick for a sub-2s flash."""
    if len(scenes) < 2:
        return scenes
    out = []
    for scene in scenes:
        start, end = _word_span_times(words, scene["word_from"], scene["word_to"])
        if end - start >= min_seconds or not out:
            out.append(scene)
            continue
        host = out[-1]
        if not host.get("shots"):
            host["shots"] = [
                {
                    "word_from": host["word_from"],
                    "word_to": host["word_to"],
                    "text": host["text"],
                    "query": host["query"],
                    "subject": host["semantic"]["subject"],
                    "action": host["semantic"]["action"],
                }
            ]
        host["shots"].append(
            {
                "word_from": scene["word_from"],
                "word_to": scene["word_to"],
                "text": scene["text"],
                "query": scene["query"],
                "subject": scene["semantic"]["subject"],
                "action": scene["semantic"]["action"],
            }
        )
        host["word_to"] = scene["word_to"]
        host["text"] = host["text"] + " " + scene["text"]
        host["sentence_to"] = scene["sentence_to"]
        # The host's requirements/queries now describe only part of the scene;
        # keep both sets so the picker sees everything the scene must show.
        for key in ("visual_requirements", "visual_exclusions"):
            merged = host.get(key, []) + [x for x in scene.get(key, []) if x not in host.get(key, [])]
            host[key] = merged[:8]
        host["queries"] = (host.get("queries") or [])[:2] + [scene["query"]]
    return out


def _split_long(scenes, words, max_seconds):
    """Subdivide scenes longer than max_seconds into continuation SHOTS of
    the same idea (never new scenes -- the meaning didn't change, the visual
    just needs relief). Splits at sentence bounds when the scene has several
    sentences, else at the biggest natural pause."""
    for scene in scenes:
        start, end = _word_span_times(words, scene["word_from"], scene["word_to"])
        if end - start <= max_seconds or scene.get("shots"):
            continue
        spans = []
        pending_from = scene["word_from"]
        pending_to = scene["word_to"]
        while True:
            s, e = _word_span_times(words, pending_from, pending_to)
            if e - s <= max_seconds:
                spans.append((pending_from, pending_to))
                break
            cut = _split_span_at_pause(words, pending_from, pending_to)
            spans.append((pending_from, cut))
            pending_from = cut + 1
        if len(spans) < 2:
            continue
        queries = scene.get("queries") or [scene["query"]]
        scene["shots"] = [
            {
                "word_from": a,
                "word_to": b,
                "text": " ".join(words[k]["text"] for k in range(a, b + 1)),
                "query": queries[min(i, len(queries) - 1)],
                "subject": scene["semantic"]["subject"],
                "action": scene["semantic"]["action"],
            }
            for i, (a, b) in enumerate(spans)
        ]
    return scenes


def refresh_times(scenes, words):
    """(Re)compute every scene's and shot's start/end/duration from its word
    indices against the CURRENT word timestamps. Call after anything that
    shifts timestamps (pause splicing) -- indices never change, so this is
    always safe and always exact."""
    for scene in scenes:
        start, end = _word_span_times(words, scene["word_from"], scene["word_to"])
        scene["start"], scene["end"], scene["duration"] = start, end, end - start
        for shot in scene.get("shots") or []:
            s, e = _word_span_times(words, shot["word_from"], shot["word_to"])
            shot["start"], shot["end"], shot["duration"] = s, e, e - s
    return scenes


def fallback_scenes(sentences, words, lang, min_seconds=4.0, max_seconds=MAX_SCENE_SECONDS,
                    max_shot=MAX_SHOT_SECONDS):
    """No-LLM segmentation: whole sentences accumulated to a readable
    duration, never cut mid-sentence. Still exact-word-mapped -- just without
    LLM semantics (keyword queries only)."""
    segments = []
    group = []

    def flush():
        if not group:
            return
        seg = _fallback_segment(group[0])
        seg["last_sentence"] = group[-1]["index"]
        segments.append(seg)
        group.clear()

    for s in sentences:
        group.append(s)
        start = words[group[0]["word_from"]]["start"]
        end = words[group[-1]["word_to"]]["end"]
        if end - start >= min_seconds:
            flush()
    flush()
    by_index = {s["index"]: s for s in sentences}
    for seg in segments:
        seg["planned_shots"] = _plan_segment_shots(seg, by_index, words, lang, max_shot)
    return segments


def build_scenes(
    words,
    script_text,
    lang,
    llm_keys=None,
    model=None,
    min_scene_seconds=MIN_SCENE_SECONDS,
    max_scene_seconds=MAX_SCENE_SECONDS,
    max_shot_seconds=MAX_SHOT_SECONDS,
    verbose=True,
    one_per_sentence=False,
):
    """The full pipeline: words+script -> semantic scenes with exact word
    spans, times, semantics, and shots. Returns (scenes, meta).

    `one_per_sentence` forces a strict 1:1 mapping: every sentence in the
    script becomes exactly one scene, in order, with no LLM call, no merging
    of short sentences into a neighbour, and no shot-splitting of long ones.
    Each scene is still word-index-exact (same coordinate system as the
    semantic timeline), so pause splicing and step 3's frame plan stay exact.
    Use it when you pick or make the visuals yourself and want each line of
    narration to line up with its own clip in step 2."""
    sentences = sentences_from_words(words, script_text)
    sentences_by_index = {s["index"]: s for s in sentences}

    if one_per_sentence:
        segments = [_fallback_segment(s) for s in sentences]
        scenes = [_segment_scene(seg, sentences_by_index, words, lang) for seg in segments]
        # No _merge_short / _split_long -- the sentence IS the scene, always.
        refresh_times(scenes, words)
        for i, scene in enumerate(scenes, start=1):
            scene["index"] = i
            scene["segment_id"] = f"segment_{i:03d}"
        meta = {
            "mode": "sentence",
            "sentences": len(sentences),
            "scenes": len(scenes),
            "llm_scenes": 0,
            "multi_shot_scenes": 0,
            "llm_failures": [],
        }
        return scenes, meta

    mode = "semantic"
    failures = []
    if llm_keys:
        segments, failures = analyze_script(
            sentences, words, lang, llm_keys, model=model, verbose=verbose,
            max_shot=max_shot_seconds,
        )
        if all(not seg.get("llm") for seg in segments):
            mode = "fallback"  # every batch failed -- effectively no LLM
    else:
        mode = "fallback"
        segments = fallback_scenes(sentences, words, lang, max_shot=max_shot_seconds)

    segments = _cap_scene_sentences(segments)
    scenes = [_segment_scene(seg, sentences_by_index, words, lang) for seg in segments]
    scenes = _merge_short_scenes(scenes, words, min_scene_seconds)
    refresh_times(scenes, words)
    total_shots = sum(max(1, len(sc.get("shots") or [])) for sc in scenes)
    scenes = _finalize_shots(scenes)
    for i, scene in enumerate(scenes, start=1):
        scene["index"] = i
        scene["segment_id"] = f"segment_{i:03d}"
        for j, shot in enumerate(scene.get("shots") or [], start=1):
            shot["shot_id"] = f"scene_{i:03d}_shot_{j}"
    shot_secs = [
        sh["duration"] for sc in scenes for sh in (sc.get("shots") or [])
    ] + [sc["duration"] for sc in scenes if not sc.get("shots")]
    check = validate_plan(scenes, sentences, words, max_shot_seconds)
    meta = {
        "mode": mode,
        "planner": "sentence-driven",
        "plan_check": check,
        "sentences": len(sentences),
        "scenes": len(scenes),
        "shots": total_shots,
        "max_shot_seconds": max_shot_seconds,
        "longest_shot_seconds": round(max(shot_secs), 2) if shot_secs else 0,
        "llm_scenes": sum(1 for s in scenes if s.get("llm")),
        "multi_shot_scenes": sum(1 for s in scenes if s.get("shots")),
        "llm_failures": failures,
    }
    return scenes, meta


# ---------------------------------------------------------------------------
# Master timeline artifact (the inspectable word -> time -> scene map)


def write_timeline(project_dir, words, scenes, meta):
    """timeline.json: the single authoritative, inspectable mapping
        script word -> voice timestamp -> segment/scene -> (shot)
    described by the master synchronization model. Every scene/shot span is
    word-index-exact; times come straight from word_timings.json."""
    word_to_scene = {}
    for scene in scenes:
        for w in range(scene["word_from"], scene["word_to"] + 1):
            word_to_scene[w] = scene["index"]
    timeline = {
        "meta": meta,
        "words": [
            {
                "i": i,
                "text": w["text"],
                "start": round(w["start"], 3),
                "end": round(w["end"], 3),
                "scene": word_to_scene.get(i),
            }
            for i, w in enumerate(words)
        ],
        "scenes": [
            {
                "scene_id": f"scene_{s['index']:03d}",
                "segment_id": s.get("segment_id"),
                "index": s["index"],
                "text": s["text"],
                "word_from": s["word_from"],
                "word_to": s["word_to"],
                "start": round(s["start"], 3),
                "end": round(s["end"], 3),
                "duration": round(s["duration"], 3),
                "semantic": s.get("semantic"),
                "visual_requirements": s.get("visual_requirements"),
                "visual_exclusions": s.get("visual_exclusions"),
                "queries": s.get("queries"),
                "shots": [
                    {
                        "shot_id": sh.get("shot_id"),
                        "text": sh["text"],
                        "word_from": sh["word_from"],
                        "word_to": sh["word_to"],
                        "start": round(sh["start"], 3),
                        "end": round(sh["end"], 3),
                        "duration": round(sh["duration"], 3),
                        "sentence_index": sh.get("sentence_index"),
                        "visual": sh.get("visual"),
                        "query": sh.get("query"),
                        "subject": sh.get("subject"),
                        "action": sh.get("action"),
                        "location": sh.get("location"),
                        "continuity": sh.get("continuity"),
                        "context": sh.get("context"),
                    }
                    for sh in s.get("shots") or []
                ],
                "visual": (s.get("semantic") or {}).get("visual"),
                "shot_context": s.get("shot_context"),
            }
            for s in scenes
        ],
    }
    save_json(timeline, project_dir / "timeline.json")
    return timeline


def print_plan(scenes, meta):
    """Human-readable dump of the semantic timeline -- the scene-level debug
    view (script text, voice window, semantics) requested for every scene."""
    print(f"\nSemantic timeline: {meta['scenes']} scenes from {meta['sentences']} sentences "
          f"(mode: {meta['mode']}, {meta['llm_scenes']} LLM-analyzed, "
          f"{meta['multi_shot_scenes']} multi-shot)")
    for f in meta.get("llm_failures") or []:
        print(f"  [warn] LLM batch fell back: {f}")
    for s in scenes:
        sem = s.get("semantic") or {}
        line = " / ".join(x for x in (sem.get("subject"), sem.get("action"), sem.get("location"), sem.get("time")) if x)
        print(f"\nSCENE {s['index']:03d}  {s['start']:.2f} -> {s['end']:.2f}  ({s['duration']:.2f}s)  words {s['word_from']}-{s['word_to']}")
        print(f"  script: {s['text'][:110]}{'...' if len(s['text']) > 110 else ''}")
        if line:
            print(f"  visual: {line}")
        if s.get("visual_requirements"):
            print(f"  must show: {', '.join(s['visual_requirements'])}")
        if s.get("visual_exclusions"):
            print(f"  must NOT show: {', '.join(s['visual_exclusions'])}")
        print(f"  queries: {', '.join(s.get('queries') or [s['query']])}")
        for sh in s.get("shots") or []:
            print(f"    shot {sh['start']:.2f}->{sh['end']:.2f} ({sh['duration']:.2f}s): "
                  f"\"{sh['text'][:60]}{'...' if len(sh['text']) > 60 else ''}\"  [{sh.get('query')}]")


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", help="Path under projects/, e.g. en/demo")
    parser.add_argument("--write", action="store_true",
                        help="Rewrite scenes.json and timeline.json (default: just print the plan). "
                        "Refuses if clips are already picked -- same safety as step 1.")
    parser.add_argument("--model", default=None, help="Gemini model override")
    parser.add_argument("--no-llm", action="store_true", help="Force the deterministic sentence fallback")
    args = parser.parse_args()

    project_dir = PROJECTS_DIR / args.project
    script_text = (project_dir / "script.txt").read_text(encoding="utf-8").strip()
    words = load_json(project_dir / "word_timings.json")
    lang = Path(args.project).parts[0]

    llm_keys = None
    if not args.no_llm:
        try:
            from llm_client import load_keys

            llm_keys = load_keys()
        except Exception as e:
            print(f"No Gemini key ({e}) -- using deterministic sentence segmentation.")

    scenes, meta = build_scenes(words, script_text, lang, llm_keys, model=args.model)
    print_plan(scenes, meta)

    if args.write:
        selections_path = project_dir / "selections.json"
        if selections_path.exists():
            try:
                picked = len(json.loads(selections_path.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                picked = 0
            if picked:
                sys.exit(
                    f"\n{picked} scene(s) already have clips picked -- rewriting scenes.json now "
                    f"would attach them to the wrong narration. Delete selections.json first."
                )
        save_json(scenes, project_dir / "scenes.json")
        write_timeline(project_dir, words, scenes, meta)
        print(f"\nWrote scenes.json ({len(scenes)} scenes) and timeline.json.")


if __name__ == "__main__":
    main()
