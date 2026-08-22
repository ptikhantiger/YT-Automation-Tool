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

The LLM (Groq, same client/keys as step 0) is asked to do only what code
can't: group consecutive sentences into visual ideas, split multi-event
sentences into shots (by quoting the exact words -- verified here against
the real tokens, never trusted), and produce per-segment semantics: subject
/ action / object / location / time, visual requirements, visual
exclusions, and concrete stock-search queries. Everything structural
(coverage, ordering, word alignment, timing) is validated and repaired in
code. No Groq key -- or any LLM failure -- falls back to a deterministic
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
# tier's TPM budget and the response inside max_completion_tokens.
BATCH_SENTENCES = 12

SEGMENT_SYSTEM = """You are a professional documentary film editor and stock-footage researcher.
You break narration scripts into VISUAL SEGMENTS for editing: each segment is one
continuous visual idea, and each gets precise semantics and concrete stock-video
search queries.

Rules:
- Work ONLY with the sentences given. Never rewrite, summarize, or reorder them.
- A segment covers one or more CONSECUTIVE whole sentences. Use more than one
  sentence ONLY when they describe the same continuous visual (same subject doing
  the same thing in the same place). When the subject, action, location, or event
  changes, start a new segment.
- If one sentence contains SEVERAL distinct visual events (e.g. "the rocket
  launched from Florida and climbed into the atmosphere"), add "shots": each shot
  QUOTES the exact consecutive words of the sentence(s) it covers, verbatim, in
  order, together covering the whole segment text with no words skipped or added.
- Queries describe what a CAMERA SEES: concrete nouns and actions, 2-5 English
  words (English regardless of script language -- stock sites index in English).
  Never abstract words (freedom, success), never emotions alone.
- visual_requirements: 2-5 short items that MUST be visible for the segment to be
  correct. visual_exclusions: 0-4 items that must NOT appear (wrong era, wrong
  place, cliches to avoid). Base both on the sentence meaning only.
- "action" is what the subject is DOING, as a present participle ("landing",
  "running toward car"), or "" if none.
Return STRICT JSON only, no markdown, no commentary:
{"segments":[{"first_sentence":N,"last_sentence":N,"meaning":"...","subject":"...",
"action":"...","object":"...","location":"...","time":"...","entities":["..."],
"visual_requirements":["..."],"visual_exclusions":["..."],"queries":["..."],
"shots":[{"quote":"...","query":"...","subject":"...","action":"..."}]}]}
"shots" may be [] or omitted when the segment is a single visual.
Cover every sentence exactly once, in order, with no gaps and no overlaps."""


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
    tokens = _tokenize_for_alignment(script_text)
    aligned = len(tokens) == len(words)

    sentences = []
    start = 0
    for i, w in enumerate(words):
        if flags[i] or i == len(words) - 1:
            if aligned:
                text = " ".join(tokens[start : i + 1])
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
    """One Groq call turning a batch of numbered sentences into segments.
    Raises GroqError/ValueError upward -- the caller decides how to fall
    back. Returns the raw (validated-shape, uncleaned-coverage) segments."""
    from groq_client import complete, strip_reasoning

    lines = [f'S{s["index"]}: "{s["text"]}"' for s in batch]
    context = (
        f"\nFor continuity, the segment right before this batch showed: {prev_meaning}"
        if prev_meaning
        else ""
    )
    user = (
        f"SCRIPT SENTENCES (numbered, narration language: {lang}):\n"
        + "\n".join(lines)
        + context
        + "\n\nSegment sentences S"
        + str(batch[0]["index"])
        + " through S"
        + str(batch[-1]["index"])
        + " following the rules. STRICT JSON only."
    )
    reply = complete(
        [
            {"role": "system", "content": SEGMENT_SYSTEM},
            {"role": "user", "content": user},
        ],
        keys,
        model=model,
        temperature=0.2,
        max_tokens=3000,
        verbose=verbose,
    )
    data = _extract_json(strip_reasoning(reply))
    segments = data.get("segments")
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


def _resolve_shots(segment, sentences_by_index, words):
    """Turn the LLM's quoted shots into exact word spans, verified against
    the real script tokens. Shots must tile the segment's words in order;
    any mismatch discards ALL shots for the segment (the segment itself is
    still fine -- it just plays as a single visual)."""
    first_s = sentences_by_index[segment["first_sentence"]]
    last_s = sentences_by_index[segment["last_sentence"]]
    w_from, w_to = first_s["word_from"], last_s["word_to"]

    raw_shots = segment.get("shots") or []
    if len(raw_shots) < 2:
        return []

    tokens_norm = ["".join(_norm_tokens(words[i]["text"])) for i in range(w_from, w_to + 1)]
    n = len(tokens_norm)

    # Locate each quote in order; a small gap before a match (a dropped
    # connective) is fine -- those words are absorbed into the previous shot.
    matches = []
    cursor = 0
    for raw in raw_shots:
        quote = raw.get("quote") if isinstance(raw, dict) else None
        if not quote:
            return []
        found = _find_quote(quote, tokens_norm, cursor)
        if found is None:
            return []
        matches.append((found, raw))
        cursor = found[1]

    # Shot k runs from its match start (shot 0: the segment start) to just
    # before shot k+1's match start; the last shot runs to the segment end.
    # This tiles the segment exactly no matter what the model skipped.
    resolved = []
    for k, ((m_start, _m_end), raw) in enumerate(matches):
        rel_from = 0 if k == 0 else matches[k][0][0]
        rel_to = (matches[k + 1][0][0] - 1) if k + 1 < len(matches) else n - 1
        if rel_to < rel_from:
            return []
        resolved.append(
            {
                "word_from": w_from + rel_from,
                "word_to": w_from + rel_to,
                "text": " ".join(words[i]["text"] for i in range(w_from + rel_from, w_from + rel_to + 1)),
                "query": _clean_str(raw.get("query")),
                "subject": _clean_str(raw.get("subject")),
                "action": _clean_str(raw.get("action")),
            }
        )
    return resolved


def analyze_script(sentences, words, lang, groq_keys, model=None, verbose=True):
    """LLM pass over all sentences -> list of validated segments with exact
    word spans and (where quoting succeeded) exact word-span shots.
    Never raises on model trouble: failed batches degrade to per-sentence
    fallback segments and the failure is noted in the returned meta."""
    from groq_client import DEFAULT_MODEL, GroqError

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
                raw = _llm_segment_batch(batch, lang, groq_keys, model, prev_meaning, verbose=verbose)
                batch_segments = _repair_coverage(raw, batch)
                break
            except (GroqError, ValueError, json.JSONDecodeError) as e:
                last_err = e
        if batch_segments is None:
            failures.append(f"sentences {batch[0]['index']}-{batch[-1]['index']}: {last_err}")
            batch_segments = [_fallback_segment(s) for s in batch]
        for seg in batch_segments:
            seg["shots"] = _resolve_shots(seg, sentences_by_index, words) if seg.get("llm") else []
        segments.extend(batch_segments)
        if segments:
            prev_meaning = segments[-1].get("meaning") or ""
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
        "shots": segment.get("shots") or [],
        "llm": bool(segment.get("llm")),
        "sentence_from": segment["first_sentence"],
        "sentence_to": segment["last_sentence"],
    }


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


def fallback_scenes(sentences, words, lang, min_seconds=4.0, max_seconds=MAX_SCENE_SECONDS):
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
    return segments


def build_scenes(
    words,
    script_text,
    lang,
    groq_keys=None,
    model=None,
    min_scene_seconds=MIN_SCENE_SECONDS,
    max_scene_seconds=MAX_SCENE_SECONDS,
    verbose=True,
):
    """The full pipeline: words+script -> semantic scenes with exact word
    spans, times, semantics, and shots. Returns (scenes, meta)."""
    sentences = sentences_from_words(words, script_text)
    sentences_by_index = {s["index"]: s for s in sentences}

    mode = "semantic"
    failures = []
    if groq_keys:
        segments, failures = analyze_script(
            sentences, words, lang, groq_keys, model=model, verbose=verbose
        )
        if all(not seg.get("llm") for seg in segments):
            mode = "fallback"  # every batch failed -- effectively no LLM
    else:
        mode = "fallback"
        segments = fallback_scenes(sentences, words, lang)

    scenes = [_segment_scene(seg, sentences_by_index, words, lang) for seg in segments]
    scenes = _merge_short(scenes, words, min_scene_seconds)
    scenes = _split_long(scenes, words, max_scene_seconds)
    refresh_times(scenes, words)
    for i, scene in enumerate(scenes, start=1):
        scene["index"] = i
        scene["segment_id"] = f"segment_{i:03d}"
        for j, shot in enumerate(scene.get("shots") or [], start=1):
            shot["shot_id"] = f"scene_{i:03d}_shot_{j}"
    meta = {
        "mode": mode,
        "sentences": len(sentences),
        "scenes": len(scenes),
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
                        "query": sh.get("query"),
                    }
                    for sh in s.get("shots") or []
                ],
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
    parser.add_argument("--model", default=None, help="Groq model override")
    parser.add_argument("--no-llm", action="store_true", help="Force the deterministic sentence fallback")
    args = parser.parse_args()

    project_dir = PROJECTS_DIR / args.project
    script_text = (project_dir / "script.txt").read_text(encoding="utf-8").strip()
    words = load_json(project_dir / "word_timings.json")
    lang = Path(args.project).parts[0]

    groq_keys = None
    if not args.no_llm:
        try:
            from groq_client import load_keys

            groq_keys = load_keys()
        except Exception as e:
            print(f"No Groq key ({e}) -- using deterministic sentence segmentation.")

    scenes, meta = build_scenes(words, script_text, lang, groq_keys, model=args.model)
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
