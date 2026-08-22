"""Shared helpers for the video pipeline.

Used by step1_audio_and_captions.py and step2_render_video.py.
"""
import bisect
import json
import random
import re
import struct
import subprocess
import urllib.request
import wave
from pathlib import Path

from PIL import ImageFont

# Includes the Urdu/Arabic full stop (U+06D4) and question mark (U+061F) --
# Urdu scripts don't use "." or "?" at all, so without these every sentence-end
# check silently fails and caption grouping falls back to pause detection.
SENTENCE_END_CHARS = ".!?…۔؟"
TRAILING_QUOTE_CHARS = "\"')]»”’*"
_HAS_WORD_CHAR = re.compile(r"\w", re.UNICODE)


def _tokenize_for_alignment(script_text):
    """Whitespace-split the script, folding any standalone punctuation-only
    token (e.g. a lone "--" or em dash used as a pause, common in these
    scripts) onto the end of the preceding word token.

    edge-tts's WordBoundary events only cover words it actually speaks -- a
    standalone punctuation token gets no event at all, which would otherwise
    shift every subsequent word out of alignment by one. Folding preserves
    the punctuation for sentence-end detection while keeping token count
    matched to spoken-word count.
    """
    tokens = []
    for raw in script_text.split():
        if _HAS_WORD_CHAR.search(raw):
            tokens.append(raw)
        elif tokens:
            tokens[-1] += raw
        # else: leading punctuation with nothing to attach to yet -- drop it
    return tokens


def _trailing_punct(tok):
    """Every trailing non-word character of `tok` (quotes, sentence
    punctuation, any run of them), e.g. "industry.\"" -> ".\"". Used to
    restore a word's real trailing punctuation once compute_sentence_end_*
    has identified it as a sentence end."""
    i = len(tok)
    while i > 0 and not _HAS_WORD_CHAR.match(tok[i - 1]):
        i -= 1
    return tok[i:]


def _compute_sentence_ends(words, script_text, pause_fallback=0.5):
    """Shared alignment pass behind compute_sentence_end_flags and
    compute_sentence_end_punctuation -- returns (flags, punctuation) so
    tokenizing/aligning the script only has to happen once for whichever
    (or both) of them a caller needs. See compute_sentence_end_flags for why
    this alignment is necessary at all."""
    tokens = _tokenize_for_alignment(script_text)
    if len(tokens) == len(words):
        flags, punct = [], []
        for tok in tokens:
            stripped = tok.rstrip(TRAILING_QUOTE_CHARS)
            is_end = bool(stripped) and stripped[-1] in SENTENCE_END_CHARS
            flags.append(is_end)
            punct.append(_trailing_punct(tok) if is_end else "")
        return flags, punct
    flags = []
    for i, w in enumerate(words):
        gap = (words[i + 1]["start"] - w["end"]) if i + 1 < len(words) else 999
        flags.append(gap >= pause_fallback)
    punct = ["." if f else "" for f in flags]
    return flags, punct


def compute_sentence_end_flags(words, script_text, pause_fallback=0.5):
    """Return a list of booleans (one per word) marking whether that word is
    immediately followed by sentence-ending punctuation.

    edge-tts's WordBoundary events strip punctuation from the word text, so
    sentence ends can't be detected from `words` alone. We instead align
    against the original script text by whitespace tokens (which still carry
    punctuation) -- this works whenever the token count matches the word
    count. If it doesn't (unusual tokenization edge-tts didn't speak the way
    the raw text implies), fall back to treating a long pause before the next
    word as a sentence boundary, which is still driven by real TTS timing,
    just less precise.
    """
    return _compute_sentence_ends(words, script_text, pause_fallback)[0]


def compute_sentence_end_punctuation(words, script_text, pause_fallback=0.5):
    """Return a list of strings (one per word) -- the real trailing
    punctuation from the original script (".", "?", "...\"", the Urdu
    equivalents, etc.) for words compute_sentence_end_flags marks as a
    sentence end, "" for every other word.

    Lets scene text (built from edge-tts's WordBoundary events, which strip
    all punctuation -- see compute_sentence_end_flags) get its real
    sentence-ending punctuation back, without altering the timing data
    itself. In the token-count-mismatch fallback path the real character
    isn't recoverable, so a bare "." stands in for it."""
    return _compute_sentence_ends(words, script_text, pause_fallback)[1]


def _finalize_chunk(chunk_words, punctuation=None):
    """`punctuation`, if given (from compute_sentence_end_punctuation, one
    entry per word in chunk_words), is appended to each word's text -- "" for
    every word that isn't a sentence end, so this is a no-op unless passed."""
    if punctuation is not None:
        text = " ".join(w["text"] + p for w, p in zip(chunk_words, punctuation))
    else:
        text = " ".join(w["text"] for w in chunk_words)
    return {
        "text": text,
        "start": chunk_words[0]["start"],
        "end": chunk_words[-1]["end"],
        "duration": chunk_words[-1]["end"] - chunk_words[0]["start"],
    }


def group_captions(words, sentence_end_flags, max_chars=42, max_duration=4.5,
                   pause_break=0.45, max_words=None):
    """Group words into short on-screen caption cards, breaking at sentence
    ends, character limits, duration limits, natural pauses, or a hard word
    count -- whichever comes first. Uses real word timestamps from edge-tts.
    Each card keeps its member words (with their individual timings) under
    "words", needed for per-word highlight rendering.

    `max_words` (e.g. 3) caps how many words show at once for the big
    punchy few-words-on-screen look; None leaves grouping to the other
    limits."""
    cards = []
    current = []
    for i, w in enumerate(words):
        current.append(w)
        text = " ".join(x["text"] for x in current)
        dur = current[-1]["end"] - current[0]["start"]
        next_gap = (words[i + 1]["start"] - w["end"]) if i + 1 < len(words) else None
        should_break = (
            sentence_end_flags[i]
            or len(text) >= max_chars
            or dur >= max_duration
            or (max_words is not None and len(current) >= max_words)
            or (next_gap is not None and next_gap >= pause_break)
        )
        if should_break:
            card = _finalize_chunk(current)
            card["words"] = current
            cards.append(card)
            current = []
    if current:
        card = _finalize_chunk(current)
        card["words"] = current
        cards.append(card)
    return cards


def group_scenes(words, min_duration=4.0, max_duration=8.0, rng=None,
                  long_every=None, long_seconds=None, sentence_punctuation=None):
    """Group words into scenes (one image each), each held for a random
    duration between min_duration and max_duration seconds -- a fast-cut
    pace independent of sentence structure (at this short a duration most
    sentences run well past it, so waiting for a sentence end wouldn't hit
    the target range at all). A fresh random target is drawn after each cut.
    Uses real word timestamps from edge-tts.

    `long_every` (e.g. 3) makes every Nth scene hold for `long_seconds`
    (e.g. 15) of narration instead of the normal random target -- an
    occasional longer, slower clip rather than a fast cut every time.
    Narration and captions play through it exactly as normal; it's still an
    ordinary scene, just held longer.

    `sentence_punctuation` (from compute_sentence_end_punctuation), if given,
    restores real sentence-ending punctuation into each scene's "text" --
    edge-tts's WordBoundary events strip it, so without this every scene
    reads as one long unpunctuated run-on.
    """
    rng = rng or random
    scenes = []
    current = []
    current_punct = []

    def next_target():
        n = len(scenes) + 1  # the scene about to start, 1-indexed
        if long_every and long_seconds and n % long_every == 0:
            return long_seconds
        return rng.uniform(min_duration, max_duration)

    target = next_target()
    for i, w in enumerate(words):
        current.append(w)
        if sentence_punctuation is not None:
            current_punct.append(sentence_punctuation[i])
        dur = current[-1]["end"] - current[0]["start"]
        if dur >= target:
            scenes.append(_finalize_chunk(current, current_punct if sentence_punctuation is not None else None))
            current = []
            current_punct = []
            target = next_target()
    if current:
        scenes.append(_finalize_chunk(current, current_punct if sentence_punctuation is not None else None))
    return scenes


def probe_audio_format(path):
    """(sample_rate, channel_layout) of an audio file's first stream. Used
    both to match a generated silence segment closely enough for the concat
    filter to join it without an implicit resample, and to build a
    loss-free stereo_pan() upmix for --music (see step3_render_video.py)."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate,channels",
            "-of", "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}:\n{result.stderr}")
    rate, channels = result.stdout.split()
    return rate, ("mono" if channels == "1" else "stereo")


def stereo_pan(channel_layout):
    """An explicit ffmpeg `pan` filter expression that converts to stereo
    at unity gain, given a source that's already 'mono' or 'stereo'.

    ffmpeg's own automatic channel-layout conversion (`aformat=
    channel_layouts=stereo`) is a no-op for content that's already stereo,
    but for mono content it applies a lossy "equal power" upmix -- each of
    the two output channels at roughly -3dB rather than a straight
    duplication -- confirmed by direct measurement: it quietly drops a mono
    track's own loudness by ~3dB before it's even mixed with anything else.
    An explicit `pan` duplication avoids that silently-wrong gain change.
    """
    return "pan=stereo|c0=c0|c1=c0" if channel_layout == "mono" else "pan=stereo|c0=c0|c1=c1"


def _splice_silence(audio_path, cuts):
    """Physically insert `duration` seconds of true silence into
    `audio_path` at each `cut_time`, both given in the file's own original
    timeline -- one ffmpeg pass, so cut positions can't drift from being
    re-encoded more than once. `cuts` is a list of (cut_time, duration)
    pairs in ascending time order.
    """
    audio_path = Path(audio_path)
    rate, layout = probe_audio_format(audio_path)

    boundaries = [0.0] + [t for t, _ in cuts]
    inputs = ["-i", str(audio_path)]
    filter_parts = []
    concat_labels = []
    for i, start in enumerate(boundaries):
        label = f"a{i}"
        if i < len(cuts):
            end = cuts[i][0]
            filter_parts.append(f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[{label}]")
        else:
            filter_parts.append(f"[0:a]atrim=start={start:.3f},asetpts=PTS-STARTPTS[{label}]")
        concat_labels.append(f"[{label}]")
        if i < len(cuts):
            dur = cuts[i][1]
            inputs += ["-t", f"{dur:.3f}", "-f", "lavfi", "-i", f"anullsrc=r={rate}:cl={layout}"]
            concat_labels.append(f"[{i + 1}:a]")

    filter_complex = (
        ";".join(filter_parts) + ";" +
        "".join(concat_labels) + f"concat=n={len(concat_labels)}:v=0:a=1[aout]"
    )

    tmp_path = audio_path.with_name(audio_path.stem + ".pause-splice.mp3")
    args = inputs + [
        "-filter_complex", filter_complex,
        "-map", "[aout]",
        "-c:a", "libmp3lame", "-b:a", "192k",
        str(tmp_path),
    ]
    run_ffmpeg(args, description="splicing narration-pause silence into narration audio")
    tmp_path.replace(audio_path)


def _apply_pause_cuts(words, scenes, audio_path, cuts):
    """Shared back half of insert_narration_pauses/insert_sentence_pauses:
    given ascending (cut_time, duration) pairs already chosen by the caller,
    physically splice real silence into audio_path at each and shift every
    word/scene timestamp after a cut forward by that cut's (cumulative)
    length. Returns (new_words, new_scenes, pause_windows) -- pause_windows
    is a list of (start, end) times, in the new shifted timeline, of each
    inserted gap."""
    if not cuts:
        return words, scenes, []
    cuts = sorted(cuts, key=lambda c: c[0])

    cut_times = []  # [(cut time, cumulative shift from here on)]
    running = 0.0
    for cut_t, dur in cuts:
        running += dur
        cut_times.append((cut_t, running))

    def shift_at(t):
        shift = 0.0
        for cut_t, cum in cut_times:
            if t >= cut_t - 1e-6:
                shift = cum
        return shift

    new_words = [
        {**w, "start": w["start"] + shift_at(w["start"]), "end": w["end"] + shift_at(w["start"])}
        for w in words
    ]
    new_scenes = [
        {**s, "start": s["start"] + shift_at(s["start"]), "end": s["end"] + shift_at(s["start"])}
        for s in scenes
    ]

    pause_windows = []
    prior_cum = 0.0
    for cut_t, dur in cuts:
        start = cut_t + prior_cum
        pause_windows.append((start, start + dur))
        prior_cum += dur

    _splice_silence(audio_path, cuts)
    return new_words, new_scenes, pause_windows


def insert_narration_pauses(words, scenes, audio_path, every, min_duration, max_duration,
                             sentence_end_flags, rng=None):
    """Splice a real silence gap into the narration after every `every`
    scenes -- for a pause with no voice at all, just the already-picked clip
    for the scene right before it (and any background music) continuing.

    Each scene boundary is only a rough target for WHERE to cut -- the exact
    cut point is snapped forward to the end of whatever sentence is still
    being spoken there (per `sentence_end_flags`, from
    compute_sentence_end_flags()), so narration always finishes its sentence
    before going silent instead of stopping mid-word. If two scene
    boundaries snap to the same sentence end (possible with a small
    `every`), only one pause is inserted there.

    No new scene is added and scenes.json's shape doesn't change: the scene
    right before a gap simply holds its clip through it, the same way
    scene_segment_durations() already makes any scene hold through an
    ordinary natural pause -- this just manufactures a bigger one on
    purpose. That means step2 needs no separate clip pick for it and step3's
    render needs no changes at all.

    Returns (new_words, new_scenes, pause_windows) with every timestamp
    after each inserted gap shifted forward by that gap's length.
    pause_windows is a list of (start, end) times, in the new shifted
    timeline, of each inserted gap.
    """
    if not every or every < 1 or len(scenes) <= every:
        return words, scenes, []
    rng = rng or random

    sentence_ends = sorted(w["end"] for w, flag in zip(words, sentence_end_flags) if flag)
    if not sentence_ends:
        return words, scenes, []

    cuts = []  # [(cut_time, duration)], ascending, one per distinct sentence end
    seen = set()
    for i in range(every - 1, len(scenes) - 1, every):
        idx = bisect.bisect_left(sentence_ends, scenes[i]["end"])
        if idx >= len(sentence_ends):
            continue  # no sentence finishes at or after this boundary -- nothing safe to cut on
        cut_t = sentence_ends[idx]
        if cut_t in seen:
            continue
        seen.add(cut_t)
        cuts.append((cut_t, rng.uniform(min_duration, max_duration)))
    return _apply_pause_cuts(words, scenes, audio_path, cuts)


def insert_sentence_pauses(words, scenes, audio_path, min_words, min_duration, max_duration,
                            sentence_end_flags, rng=None):
    """Splice a real silence gap into the narration at the end of every
    sentence that lands at least `min_words` words after the previous pause
    (or after the start of the narration, before the first pause) -- pacing
    driven by how much has actually been said rather than a fixed
    --pause-every scene cadence.

    The word count only resets when a pause is actually inserted: a
    sentence end that arrives too soon (fewer than `min_words` words since
    the last pause) is skipped rather than resetting the count, so a run of
    short sentences still gets a pause as soon as their combined length
    clears the bar, at the sentence end that crosses it.

    Same no-new-scene / no step2-3 changes contract as
    insert_narration_pauses -- see its docstring. Returns (new_words,
    new_scenes, pause_windows), same shape as insert_narration_pauses.
    """
    if not min_words or min_words < 1:
        return words, scenes, []
    rng = rng or random

    cuts = []  # [(cut_time, duration)], ascending, one per qualifying sentence end
    since_pause = 0
    for w, flag in zip(words, sentence_end_flags):
        since_pause += 1
        if flag and since_pause > min_words:
            cuts.append((w["end"], rng.uniform(min_duration, max_duration)))
            since_pause = 0
    return _apply_pause_cuts(words, scenes, audio_path, cuts)


def long_gap_starts(words, min_gap=1.0):
    """Start time of every gap of at least `min_gap` seconds between two
    consecutive words -- an inserted --pause-every silence, or any other
    multi-second pause in the narration. Feeds _cap_hold() so a caption
    card's on-screen hold can't bleed across one; ordinary short breaths
    between sentences (well under a second) are untouched."""
    return [w["end"] for w, nxt in zip(words, words[1:]) if nxt["start"] - w["end"] >= min_gap]


def _cap_hold(own_end, natural_hold_end, pause_starts):
    """Clamp a caption's "hold until the next word/card" extension so it
    can't bleed into a long silence (see long_gap_starts()) -- otherwise
    the last line before a several-second narration pause would sit frozen
    on screen for the whole thing.

    Only clamps when this card's own end already precedes an upcoming pause
    start earlier than where the hold would naturally reach -- an ordinary
    short breath/sentence pause is completely unaffected.
    """
    if not pause_starts:
        return natural_hold_end
    candidates = [p for p in pause_starts if own_end <= p < natural_hold_end]
    return min(candidates) if candidates else natural_hold_end


def build_duck_envelope(words, total_duration, gap_min=1.2, duck_level=0.18,
                         attack_ms=150, release_ms=400, lead_ms=80):
    """A background-music gain curve (list of (time, 0..1 gain) breakpoints,
    piecewise-linear) that ducks down to `duck_level` while narration is
    playing and swells back to full (1.0) during real silence -- the intro
    before the first word, the outro after the last, and any narration gap
    of at least `gap_min` seconds (a deliberate pause, not just the breath
    between two ordinary sentences).

    Built from the exact word timestamps rather than detecting narration by
    volume, so it can't misfire on a quiet consonant or a breath, and it's
    fully reproducible. Consecutive words are merged into one "speaking
    segment" whenever the gap between them is under `gap_min` -- a short
    sentence-to-sentence pause stays ducked through it rather than letting
    the music swell back up and immediately duck again, which would sound
    like pumping.

    The duck-down ramp finishes `lead_ms` before a segment's start, not
    exactly at it -- landing the ramp precisely on the word's own timestamp
    left zero margin for error, so word-timing granularity/encoder rounding
    could leave an audible sliver of full-volume music right against the
    first word. `lead_ms` is the safety pad that closes that gap; the
    duck-up ramp starts exactly at a segment's end, taking `release_ms` to
    reach full volume -- a fast attack (with a bit of lead time baked in),
    slower release, same shape as a broadcast ducking compressor.
    """
    if not words:
        return [(0.0, 1.0), (total_duration, 1.0)]

    segments = []
    seg_start, seg_end = words[0]["start"], words[0]["end"]
    for w in words[1:]:
        if w["start"] - seg_end >= gap_min:
            segments.append((seg_start, seg_end))
            seg_start = w["start"]
        seg_end = w["end"]
    segments.append((seg_start, seg_end))

    attack_s, release_s, lead_s = attack_ms / 1000.0, release_ms / 1000.0, lead_ms / 1000.0
    points = [(0.0, 1.0)]
    for seg_start, seg_end in segments:
        duck_at = max(0.0, seg_start - lead_s)
        duck_from = max(0.0, duck_at - attack_s)
        if duck_from > points[-1][0]:
            points.append((duck_from, 1.0))
        points.append((duck_at, duck_level))
        points.append((seg_end, duck_level))
        points.append((seg_end + release_s, 1.0))
    if points[-1][0] < total_duration:
        points.append((total_duration, 1.0))
    return points


def write_envelope_wav(points, path, duration, rate=200):
    """Sample a piecewise-linear (time, gain) envelope (see
    build_duck_envelope()) into a mono 16-bit PCM WAV control signal, at
    `rate` samples/sec -- plenty for a curve whose fastest moves are
    attack_ms/release_ms-scale ramps. Fed into ffmpeg and resampled up to
    the music's real sample rate there; this file is never heard directly,
    only multiplied against the music track as a time-varying gain.
    """
    times = [p[0] for p in points]
    n = max(1, int(round(duration * rate)))
    samples = bytearray()
    for i in range(n):
        t = i / rate
        idx = min(max(bisect.bisect_right(times, t) - 1, 0), len(points) - 2)
        t0, g0 = points[idx]
        t1, g1 = points[idx + 1]
        frac = 0.0 if t1 <= t0 else min(1.0, max(0.0, (t - t0) / (t1 - t0)))
        gain = g0 + (g1 - g0) * frac
        samples += struct.pack("<h", int(min(1.0, max(0.0, gain)) * 32767))
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(bytes(samples))


def format_ass_time(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


ASS_HEADER_TEMPLATE = """[Script Info]
Title: Captions
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes
PlayResX: {width}
PlayResY: {height}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{font_size},{primary_color},&H000000FF,{outline_color},&H64000000,{bold},0,0,0,100,100,0,0,1,{outline_width},1,{alignment},80,80,{margin_v},{encoding}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def rgb_to_ass_color(color):
    """Convert a friendly '#RRGGBB' (or 'RRGGBB') hex string to an opaque ASS
    colour '&HAABBGGRR'. ASS stores colour as BGR with a leading alpha byte
    (00 = fully opaque), the reverse byte order of ordinary web hex -- easy to
    get backwards, so everything user-facing takes plain #RRGGBB and funnels
    through here. A value already in ASS form ('&H...') is passed through
    untouched."""
    if color is None:
        return None
    s = color.strip()
    if s.startswith("&H") or s.startswith("&h"):
        return s
    s = s.lstrip("#")
    if len(s) != 6:
        raise ValueError(f"Expected a #RRGGBB hex colour, got {color!r}")
    r, g, b = s[0:2], s[2:4], s[4:6]
    return f"&H00{b}{g}{r}".upper()


def ass_header(style=None, primary_color=None, outline_color=None, outline_width=3,
                alignment=2, margin_v=90, width=1920, height=1080):
    """Build the .ass [V4+ Styles] header.

    `primary_color`/`outline_color` accept either #RRGGBB or a raw ASS colour
    and default to white text / black outline -- the classic subtitle look.
    `outline_width` is the black border thickness in pixels. `alignment` is
    the standard ASS numpad value (2 = bottom-center, 5 = middle-center);
    `margin_v` is measured from the bottom edge for alignment 1-3 and has no
    effect on vertical centering for 4-9 (libass centers those regardless).
    `width`/`height` set PlayResX/PlayResY -- must match the actual output
    frame size (1920x1080 landscape, or 1080x1920 for vertical Shorts/Reels)
    or every pixel position below (and libass's own auto-scaling) ends up
    stretched to the wrong aspect ratio."""
    style = style or CAPTION_STYLES["default"]
    return ASS_HEADER_TEMPLATE.format(
        font_name=style["font_name"],
        font_size=style["font_size"],
        bold=-1 if style["bold"] else 0,
        encoding=style["encoding"],
        primary_color=rgb_to_ass_color(primary_color) or "&H00FFFFFF",
        outline_color=rgb_to_ass_color(outline_color) or "&H00000000",
        outline_width=outline_width,
        alignment=alignment,
        margin_v=margin_v,
        width=width,
        height=height,
    )


# Kept for any caller still importing the old constant.
ASS_HEADER = None  # set after CAPTION_STYLES is defined, below


def write_ass(captions, path, style=None, pause_starts=None, width=1920, height=1080):
    """Write caption cards as a styled .ass file: bold white text, black
    outline, bottom-center -- the classic static YouTube subtitle look.

    Each card holds until the next one starts rather than ending with its own
    last word, so the caption never blinks out during a pause. `pause_starts`
    (see long_gap_starts()) caps that hold so it can't bleed into a long
    silence -- see _cap_hold(). `width`/`height` must match the actual output
    frame size -- see ass_header().
    """
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(ass_header(style, width=width, height=height))
        for i, c in enumerate(captions):
            start = format_ass_time(c["start"])
            natural_end = captions[i + 1]["start"] if i + 1 < len(captions) else c["end"]
            end = format_ass_time(_cap_hold(c["end"], natural_end, pause_starts))
            text = c["text"].replace("\n", "\\N")
            f.write(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}\n")


def format_srt_time(seconds):
    ms_total = max(0, round(seconds * 1000))
    h, rem = divmod(ms_total, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(captions, path):
    """Write caption cards as a plain-text .srt file -- the format YouTube
    accepts for an uploaded caption track (unlike .ass, which libass can burn
    into a render but YouTube's own uploader doesn't understand).

    Same "hold until the next card starts" timing as write_ass, so a caption
    never blinks out during a pause.
    """
    lines = []
    for i, c in enumerate(captions):
        end = captions[i + 1]["start"] if i + 1 < len(captions) else c["end"]
        lines.append(str(i + 1))
        lines.append(f"{format_srt_time(c['start'])} --> {format_srt_time(end)}")
        lines.append(c["text"].replace("\n", " ").strip())
        lines.append("")
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


DEFAULT_FONT_PATH = "C:/Windows/Fonts/arialbd.ttf"
DEFAULT_FONT_SIZE = 64

# Per-language caption rendering. `font_name` is what goes in the .ass style
# (libass resolves it by family name via fontconfig); `font_path` is the same
# face on disk, used by PIL to measure word widths for the highlight capsule.
# The two MUST be the same face or every pill lands under the wrong word.
#
# `width_scale` corrects PIL's measurement to what libass actually renders --
# it is per-font and empirically calibrated (see LIBASS_WIDTH_SCALE note
# below), never assumed.
CAPTION_STYLES = {
    "default": {
        "font_name": "Arial",
        "font_path": DEFAULT_FONT_PATH,
        "font_size": DEFAULT_FONT_SIZE,
        "bold": True,
        "encoding": 1,
        "rtl": False,
        "width_scale": 0.893,
        # Capsule geometry, all as fractions of font_size so a language can
        # change type size without the padding going out of proportion.
        # These Latin values reproduce the original hardcoded look exactly
        # (pad 16px, height 80px, full-capsule ends) at font_size 64.
        "pad_x_ratio": 0.25,
        "pill_height_ratio": 1.25,
        "pill_top_ratio": 0.0,
        "pill_radius_ratio": 0.5,
        # "fixed" = one capsule height for the whole video, from the ratios
        # above. Latin type sits on a common baseline with predictable
        # ascender/descender extents, so a constant capsule reads as a clean
        # highlighter bar and needs no per-word measurement.
        "pill_fit": "fixed",
        "max_chars": 42,
    },
    # Urdu is right-to-left and written in Nastaliq -- Arial has no Urdu
    # glyphs at all, so it needs its own face.
    #
    # font_size 140 is NOT a typo and is not comparable to the Latin 64:
    # libass renders Jameel Noori Nastaleeq at roughly 0.59x the size PIL
    # does for the same nominal value (this font's internal metrics differ
    # from the usual Latin convention), so a nominal 72 comes out visibly
    # tiny. 140 lands at ~104px of ink height, which Nastaliq needs anyway --
    # its strokes are finer and its letters stack diagonally, so it is far
    # less legible than Latin type at equal height.
    #
    # Calibrated 2026-07-22 by the incremental-advance method: render
    # words[:n] for increasing n and measure how far the line's right edge
    # moves per added word, which isolates the real advance from glyph side
    # bearings (a naive total-ink measurement confounds the two and gave a
    # misleading 0.56). Ratio held at 0.585-0.594 across five word advances.
    "ur": {
        "font_name": "Jameel Noori Nastaleeq",
        "font_path": "C:/Windows/Fonts/Jameel Noori Nastaleeq.ttf",
        "font_size": 140,
        "bold": False,  # Nastaliq has no bold face; faux-bold smears the joins
        # Encoding -1 is what actually turns on right-to-left word order, and
        # it is the ONLY value that does. libass maps -1 to FriBidi's
        # auto-detect base direction; 1 ("default charset"), 0, and 178
        # ("Arabic charset", the intuitive guess) all lay the line out in
        # logical order left-to-right instead.
        #
        # The failure is nasty because it is invisible unless you read Urdu:
        # every letter is still shaped and joined correctly, so the text looks
        # like well-set Nastaliq -- only the *word order* across the line is
        # reversed. Verified by rendering one word in red and checking which
        # end of the line it lands on.
        "encoding": -1,
        "rtl": True,
        "width_scale": 0.589,
        # Ink runs 10-113px below the \an7 anchor at size 140. A capsule at
        # 0.95x (133px) clears the descenders while sitting much closer to the
        # text than the full 1.0x did.
        #
        # The radius is deliberately NOT 0.5 here. A true capsule (r = h/2)
        # collapses into a circle whenever the word is narrower than the pill
        # is tall, which in Urdu is most of them -- Nastaliq words are short
        # and the type is large. 0.3 keeps a rounded rectangle that reads as
        # a highlight rather than a blob.
        "pad_x_ratio": 0.16,
        "pill_height_ratio": 0.95,
        "pill_top_ratio": 0.0,
        "pill_radius_ratio": 0.3,
        # "word" = the capsule is fitted to each word's own measured ink.
        # Nastaliq words vary enormously in vertical extent -- at size 140 a
        # word with no ascender occupies 71px while one with a full ascender
        # occupies 103px -- so a single constant height leaves a large empty
        # yellow gap above the short ones.
        "pill_fit": "word",
        "pad_y_ratio": 0.10,
        "ink_offset_ratio": 0.165,
        # Floor so a one-glyph word still gets a capsule rather than a sliver.
        "pill_min_height_ratio": 0.45,
        "max_chars": 45,
    },
}

ASS_HEADER = ass_header(CAPTION_STYLES["default"])


def caption_style(lang):
    """Caption rendering settings for a language folder, falling back to the
    Latin/LTR defaults for any language without a specific entry."""
    return CAPTION_STYLES.get(lang, CAPTION_STYLES["default"])


_font_cache = {}


def _get_font(font_path=DEFAULT_FONT_PATH, font_size=DEFAULT_FONT_SIZE):
    key = (font_path, font_size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(font_path, font_size)
    return _font_cache[key]



# Empirically calibrated: this ffmpeg/libass build renders "Arial" noticeably
# narrower than PIL's own measurement of the real arialbd.ttf (likely a font
# substitution under the hood) -- verified consistent (~0.893) across both a
# single word and a full 8-word line by rendering real calibration frames and
# measuring rendered pixel width directly, rather than assumed. Now lives in
# CAPTION_STYLES["default"]["width_scale"]; kept here as a named constant
# because it is the documented calibration value.
LIBASS_WIDTH_SCALE = CAPTION_STYLES["default"]["width_scale"]


def measure_text_width(text, font_path=DEFAULT_FONT_PATH, font_size=DEFAULT_FONT_SIZE,
                       width_scale=LIBASS_WIDTH_SCALE):
    """Pixel advance width of text as libass will actually render it -- used
    to position the highlight capsule under the right word."""
    return _get_font(font_path, font_size).getlength(text) * width_scale


def measure_ink_rows(text, style):
    """Where this text's ink actually starts and ends vertically, in pixels
    below an \\an7 anchor, as libass will render it.

    PIL's own box needs two corrections to predict libass: the same overall
    size scaling that `width_scale` applies horizontally, plus a constant
    offset for the difference between PIL's ascender-anchored origin and
    libass's line box top.

    Calibrated 2026-07-22 against real renders of 6 words at 2 sizes: the
    offset held at 0.165 x font_size with a worst case of 3.4px, which the
    capsule's vertical padding absorbs comfortably.
    """
    font = _get_font(style["font_path"], style["font_size"])
    _, y0, _, y1 = font.getbbox(text)
    shift = style["ink_offset_ratio"] * style["font_size"]
    return y0 * style["width_scale"] + shift, y1 * style["width_scale"] + shift


def rounded_rect_drawing(w, h, r):
    """ASS vector drawing (\\p1 path) for a rounded rectangle / capsule of
    size w x h with corner radius r, anchored at its own top-left (0,0)."""
    r = max(0.0, min(r, w / 2, h / 2))
    k = 0.5523  # bezier control-point ratio approximating a quarter circle
    kr = r * k
    return (
        f"m {r:.1f} 0 "
        f"l {w - r:.1f} 0 "
        f"b {w - r + kr:.1f} 0 {w:.1f} {r - kr:.1f} {w:.1f} {r:.1f} "
        f"l {w:.1f} {h - r:.1f} "
        f"b {w:.1f} {h - r + kr:.1f} {w - r + kr:.1f} {h:.1f} {w - r:.1f} {h:.1f} "
        f"l {r:.1f} {h:.1f} "
        f"b {r - kr:.1f} {h:.1f} 0 {h - r + kr:.1f} 0 {h - r:.1f} "
        f"l 0 {r:.1f} "
        f"b 0 {r - kr:.1f} {r - kr:.1f} 0 {r:.1f} 0"
    )


def write_karaoke_ass(
    cards,
    path,
    font_path=None,
    font_size=None,
    pill_color="&H00FFFF&",
    active_text_color="&H000000&",
    text_color=None,
    bottom_margin=90,
    pad_x=None,
    style=None,
    pause_starts=None,
    width=1920,
    height=1080,
):
    """Write caption cards as a two-layer .ass file: as each word is spoken,
    it gets a yellow rounded-pill background with black text, while the rest
    of the line stays the default white/black-outline style (or `text_color`,
    as #RRGGBB, if given -- e.g. yellow for a black-pill-on-yellow-text look).

    Captions are continuous -- each word's pill+line pair runs from that word's
    real edge-tts start until the *next* word starts, not until the word itself
    ends. Timing each pair to the word's own [start, end] instead leaves the
    whole caption blank during every gap between words, so the line flickers
    off on each breath and disappears entirely at sentence pauses. Holding
    instead means the line stays put with its last-spoken word still
    highlighted until the next word (or next card) takes over.

    Word positions are computed from measured font-advance widths (same font
    file/size as the ASS style) so the pill lines up under the right word;
    generous pad_x absorbs the small rendering differences between PIL's
    measurement and libass's actual text shaping.

    For a right-to-left language (`style["rtl"]`), the logically-first word is
    rendered rightmost, so offsets are measured inward from the line's right
    edge instead of outward from its left.
    """
    style = style or CAPTION_STYLES["default"]
    font_path = font_path or style["font_path"]
    font_size = font_size or style["font_size"]
    width_scale = style["width_scale"]
    rtl = style["rtl"]
    if pad_x is None:
        pad_x = font_size * style["pad_x_ratio"]

    def measure(text):
        return measure_text_width(text, font_path, font_size, width_scale)

    pill_height = font_size * style["pill_height_ratio"]
    pill_top = font_size * style["pill_top_ratio"]
    line_y = height - bottom_margin - pill_height
    fit_to_word = style["pill_fit"] == "word"

    def pill_box(word_text):
        """(top offset from line_y, height) of this word's capsule."""
        if not fit_to_word:
            return pill_top, pill_height
        pad_y = font_size * style["pad_y_ratio"]
        y0, y1 = measure_ink_rows(word_text, style)
        h = max(y1 - y0 + pad_y * 2, font_size * style["pill_min_height_ratio"])
        return y0 - pad_y, h

    # One flat pass over every word in the script, so a word's hold can reach
    # across a card boundary into the start of the next line.
    flat = [(ci, wi) for ci, card in enumerate(cards) for wi in range(len(card["words"]))]

    def hold_end(n):
        """When word n gives up the screen: the moment the next word starts,
        or its own end if it is the last word in the script -- capped so the
        hold can't bleed into a long silence (_cap_hold)."""
        ci, wi = flat[n]
        own_end = cards[ci]["words"][wi]["end"]
        if n + 1 < len(flat):
            nci, nwi = flat[n + 1]
            natural = cards[nci]["words"][nwi]["start"]
        else:
            natural = own_end
        return _cap_hold(own_end, natural, pause_starts)

    word_no = 0

    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(ass_header(style, primary_color=text_color, width=width, height=height))
        for card in cards:
            words = card["words"]
            word_texts = [w["text"] for w in words]
            widths = [measure(t) for t in word_texts]
            total_width = measure(" ".join(word_texts)) if words else 0.0
            line_x = (width - total_width) / 2
            # Measure each word's offset as the width of the actual prefix
            # substring (not a sum of individually-measured pieces) so any
            # font kerning between characters is captured exactly as it will
            # be shaped in the real rendered line -- summing separate word
            # and space measurements drifts out of alignment over a line.
            offsets = []
            for i in range(len(words)):
                if rtl:
                    # words[:i+1] render as the rightmost run of the line, so
                    # that run's own left edge is where word i begins.
                    offsets.append(total_width - measure(" ".join(word_texts[: i + 1])))
                elif i == 0:
                    offsets.append(0.0)
                else:
                    offsets.append(measure(" ".join(word_texts[:i]) + " "))

            for i, w in enumerate(words):
                start = format_ass_time(w["start"])
                end = format_ass_time(hold_end(word_no))
                word_no += 1

                pill_w = widths[i] + pad_x * 2
                pill_x = line_x + offsets[i] - pad_x
                top_off, this_h = pill_box(w["text"])
                pill_y = line_y + top_off
                pill_path = rounded_rect_drawing(
                    pill_w, this_h, this_h * style["pill_radius_ratio"]
                )
                pill_text = (
                    f"{{\\an7\\pos({pill_x:.1f},{pill_y:.1f})\\1c{pill_color}\\bord0\\shad0\\p1}}"
                    f"{pill_path}{{\\p0}}"
                )
                f.write(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{pill_text}\n")

                parts = []
                for j, wj in enumerate(words):
                    if j == i:
                        parts.append(f"{{\\c{active_text_color}\\bord0\\shad0}}{wj['text']}{{\\r}}")
                    else:
                        parts.append(wj["text"])
                line_text = " ".join(parts)
                text_dialogue = f"{{\\an7\\pos({line_x:.1f},{line_y:.1f})}}{line_text}"
                f.write(f"Dialogue: 1,{start},{end},Default,,0,0,0,,{text_dialogue}\n")


def write_wordcolor_ass(
    cards,
    path,
    style=None,
    text_color="#FFFFFF",
    highlight_color="#29D6F5",
    outline_color="#000000",
    outline_width=4,
    font_name=None,
    font_size=None,
    alignment=2,
    bottom_margin=90,
    pause_starts=None,
    width=1920,
    height=1080,
):
    """Write caption cards in the punchy news look: the whole line sits in
    `text_color` with a thick `outline_color` border, and as each word is
    spoken it flips to `highlight_color` -- no pill, just a colour change (the
    "THE INCOMING THREATS" style, white words with one cyan word).

    Colours take #RRGGBB (or a raw ASS colour). `text_color` and
    `outline_color` are baked into the style header; the active word is
    recoloured inline per line. Pair with a small `max_words` in
    group_captions (e.g. 3) so only a few big words show at once.

    `font_name`/`font_size` override the language's normal caption font
    (falling back to it when omitted) -- this style has no pill geometry tied
    to font size, so it's free to run much bigger than the highlight style.
    `alignment` is the ASS numpad value; 5 (middle-center) is what makes this
    read as a centered "headline" rather than a bottom-anchored subtitle.

    Timing mirrors write_karaoke_ass: each line holds with its last-spoken
    word still highlighted until the next word starts, so nothing blinks out
    during a breath or pause.
    """
    base_style = style or CAPTION_STYLES["default"]
    render_style = dict(base_style)
    if font_name:
        render_style["font_name"] = font_name
    if font_size:
        render_style["font_size"] = font_size
    hi = rgb_to_ass_color(highlight_color)

    flat = [(ci, wi) for ci, card in enumerate(cards) for wi in range(len(card["words"]))]

    def hold_end(n):
        ci, wi = flat[n]
        own_end = cards[ci]["words"][wi]["end"]
        if n + 1 < len(flat):
            nci, nwi = flat[n + 1]
            natural = cards[nci]["words"][nwi]["start"]
        else:
            natural = own_end
        return _cap_hold(own_end, natural, pause_starts)

    word_no = 0
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(
            ass_header(
                render_style,
                primary_color=text_color,
                outline_color=outline_color,
                outline_width=outline_width,
                alignment=alignment,
                margin_v=bottom_margin,
                width=width,
                height=height,
            )
        )
        for card in cards:
            words = card["words"]
            for i, w in enumerate(words):
                start = format_ass_time(w["start"])
                end = format_ass_time(hold_end(word_no))
                word_no += 1
                parts = []
                for j, wj in enumerate(words):
                    if j == i:
                        parts.append(f"{{\\c{hi}}}{wj['text']}{{\\r}}")
                    else:
                        parts.append(wj["text"])
                line_text = " ".join(parts)
                f.write(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{line_text}\n")


# Pexels/Pixabay/Coverr's CDNs sit behind Cloudflare, which blocks urllib's
# default "Python-urllib/3.x" User-Agent outright -- an ordinary browser UA is
# what makes the request go through. Shared by step2_pick_clips.py's provider
# searches and step3_render_video.py's clip downloads.
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VideoPipeline/1.0"


def download_file(url, dest, timeout=120):
    """Stream `url` to `dest`, writing to a sibling .part file first and
    atomically replacing it -- a crash or network drop mid-download can
    never leave a truncated file sitting where a caller expects a finished
    one."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as f:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    tmp.replace(dest)


def save_json(data, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def run_ffmpeg(args, description=""):
    """Run ffmpeg with the given argument list (excluding the 'ffmpeg'
    binary name itself), raising with captured stderr on failure."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed{' (' + description + ')' if description else ''}:\n"
            + result.stderr
        )
    return result


def concat_audio_files(paths, out_path):
    """Concatenate 2+ audio files, back to back in the given order, into one
    at out_path -- used to sequence a project's bg-1.mp3, bg-2.mp3, ...
    background-music tracks (see step_music_picker.py) into the single
    track step3_render_video.py's ducking/looping pipeline expects.

    Each input is decoded and normalized to the same sample format/rate/
    channel layout before the concat filter joins them, since the tracks
    were downloaded separately (different YouTube sources) and may not
    natively share those -- a raw stream-copy concat would risk a pitch
    shift or glitch at any join where they don't match.
    """
    paths = [Path(p) for p in paths]
    inputs = []
    normalize_parts = []
    for i, p in enumerate(paths):
        inputs += ["-i", str(p)]
        normalize_parts.append(
            f"[{i}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a{i}]"
        )
    concat_inputs = "".join(f"[a{i}]" for i in range(len(paths)))
    filter_complex = ";".join(normalize_parts) + f";{concat_inputs}concat=n={len(paths)}:v=0:a=1[aout]"

    args = inputs + [
        "-filter_complex", filter_complex,
        "-map", "[aout]",
        "-c:a", "libmp3lame", "-b:a", "192k",
        str(out_path),
    ]
    run_ffmpeg(args, description="sequencing background music tracks into one")


def probe_duration(path):
    """Exact duration of a media file in seconds, via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}:\n{result.stderr}")
    return float(result.stdout.strip())


def probe_video_resolution(path):
    """(width, height) of a video file's first video stream, via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=x:p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or "x" not in result.stdout:
        raise RuntimeError(f"ffprobe failed on {path}:\n{result.stderr}")
    # Parse defensively: some ffprobe builds emit a trailing separator
    # ("1920x1080x"), and a file with a second video stream (embedded cover
    # art) yields a second line -- take the first line, drop empty pieces.
    first_line = result.stdout.strip().splitlines()[0]
    parts = [p for p in first_line.split("x") if p.strip()]
    if len(parts) < 2:
        raise RuntimeError(f"ffprobe returned unparseable resolution for {path}: {result.stdout!r}")
    return int(parts[0]), int(parts[1])


def measure_mean_volume(path):
    """Whole-file mean loudness in dBFS, via ffmpeg's volumedetect filter.

    Used to auto-calibrate --music/--music-volume/--duck-db to whatever
    loudness the actual narration and music files happen to be at, instead
    of a fixed dB number that only sounds right by coincidence for a
    particular pair of files -- a music track already mastered loud needs
    far less added gain than a quiet one to land at the same target level.
    """
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    m = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", result.stderr)
    if not m:
        raise RuntimeError(f"Could not measure the volume of {path}:\n{result.stderr}")
    return float(m.group(1))


def scene_segment_durations(scenes, audio_duration):
    """How long each scene's visual must actually be on screen.

    A scene's own "duration" is only the span from its first spoken word to
    its last, so summing them silently drops the leading silence, every pause
    that happens to land on a scene boundary, and the trailing silence -- on a
    40s test that was a 3s shortfall, which ffmpeg's -shortest then chopped
    off the end of the narration. Sizing each segment from one scene's start
    to the next one's instead makes the visuals total exactly the audio
    length, with each scene holding through the pause that follows it.
    """
    starts = [0.0] + [s["start"] for s in scenes[1:]]
    # Guard against a truncated/missing tail: never ask for a negative length.
    end = max(audio_duration, starts[-1] + scenes[-1]["duration"])
    boundaries = starts + [end]
    return [boundaries[i + 1] - boundaries[i] for i in range(len(scenes))]


def natural_sorted_images(images_dir):
    """Return image files in images_dir sorted by filename. Relies only on
    the zero-padded numeric prefix (e.g. "001_...") for correct order --
    whatever follows the prefix (a timestamp stub, a description, nothing)
    doesn't affect sorting."""
    exts = {".png", ".jpg", ".jpeg", ".webp"}
    files = [p for p in Path(images_dir).iterdir() if p.suffix.lower() in exts]
    return sorted(files, key=lambda p: p.name)


def scene_time_stub(seconds):
    """Filesystem-safe H-M-S stub for a scene's start time, e.g. 00-03-45.
    Used as part of the suggested image filename so you can tell at a glance
    when in the video each image belongs, without relying on colons (illegal
    in Windows filenames)."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}-{m:02d}-{s:02d}"


def scene_image_filename(index, start_seconds, ext="png"):
    return f"{index:03d}_{scene_time_stub(start_seconds)}.{ext}"
