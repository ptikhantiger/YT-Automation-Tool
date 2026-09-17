"""
Step 0: script-1.txt + script-2.txt + script-3.txt -> script.txt

Merges several news reports of THE SAME story into one continuous spoken
script, written in plain language for viewers aged 50 and over, using the
Gemini API.

Usage:
    python step0_build_script.py <lang>/<slug> [--words 2500] [--model ...]
    python step0_build_script.py <lang>/<slug> --polish     # humanise an
                                                            # existing script
    python step0_build_script.py <lang>/<slug> --documentary
                                                            # rewrite the
                                                            # existing
                                                            # script.txt with
                                                            # the documentary
                                                            # tone -- --polish
                                                            # is implied, no
                                                            # script-N.txt
                                                            # sources needed
    python step0_build_script.py <lang>/<slug> --yt-link <youtube-url>
                                                            # transcript ->
                                                            # script.txt ->
                                                            # Gemini rewrite,
                                                            # one command
    python step0_build_script.py <lang>/<slug> --yt-link <url> --documentary
                                                            # same,
                                                            # documentary tone

e.g. python step0_build_script.py en/rate-rise

Reads every projects/<lang>/<slug>/script-N.txt (any number, two or more) plus
the rulebook at "News Master Prompt.txt" in the repo root, and writes
script.txt into the same project folder. The source files are never modified.

--polish's rewrite rulebook: by default it reads tools/llm-rewrite-
instructions.txt (news purpose, falling back to the built-in POLISH_SYSTEM if
that file is missing). Pass --documentary to use the
humanized documentary tone instead, and skip the news-only sentence-length /
banned-phrase passes that would otherwise fight that tone.

--yt-link <url> fetches that video's transcript with no timestamps (via
youtube_transcript.py), writes it straight into script.txt, and then runs it
through the same rewrite pass --polish uses -- one command from a video link
to a rewritten script.txt. Creates the project folder if it doesn't exist yet.

WHY IT BUILDS THE SCRIPT IN SECTIONS
Asking any of these models for 2,500 words in one request does not work: they
compress the back half, drift off the sources, and repeat whole paragraphs.
Building section by section against a shared fact brief keeps each request
small enough to stay accurate, hits the word target reliably, and lets a
single weak section be regenerated without redoing the whole script.
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from common import strip_speaker_labels
from llm_client import DEFAULT_MODEL, LLMError, complete, load_keys
from youtube_transcript import YouTubeTranscriptError, fetch_transcript_text

REPO_ROOT = Path(__file__).parent.parent
TOOLS_DIR = Path(__file__).parent
PROJECTS_DIR = REPO_ROOT / "projects"
# The rulebook lives in tools/ (where it's tracked); the repo root is still
# checked so a copy placed there keeps working.
RULEBOOK = next(
    (p for p in (TOOLS_DIR / "News Master Prompt.txt", REPO_ROOT / "News Master Prompt.txt") if p.exists()),
    TOOLS_DIR / "News Master Prompt.txt",
)

# --polish rulebook. Default (news purpose) is read from this file; missing
# it falls back to the built-in POLISH_SYSTEM below. --documentary
# switches to DOCUMENTARY_SYSTEM_PROMPT instead.
NEWS_REWRITE_INSTRUCTIONS = TOOLS_DIR / "llm-rewrite-instructions.txt"

# Humanized documentary tone rules, used by --documentary instead of the
# news rewrite rulebook in llm-rewrite-instructions.txt.
DOCUMENTARY_SYSTEM_PROMPT = """You rewrite documentary narration scripts for a YouTube channel.

Voice: warm, natural, human -- like a person telling a friend a story, not a
textbook or a press release. Simple English: short-to-medium sentences,
everyday words, contractions where they'd naturally be spoken (it's, don't,
you'll). Documentary narrator pacing: some short punchy sentences for
emphasis, mixed with longer flowing ones -- never a wall of uniform length.

Rules:
- Keep every fact, number, date, place name, and the overall structure/order
  exactly as given. This is a rewrite of tone, not a rewrite of content.
- Names of ordinary people the story is about (one, two, or three
  individuals whose personal story or example the video is built around): do
  NOT keep their real names. Replace each with a plain, relatable stand-in
  ("a man", "a woman", "your friend", "a friend of yours", "your mate",
  "a classmate", "a colleague", "a coworker", "your neighbor", "an old
  schoolmate"), matching that person's gender, and use the SAME stand-in for
  that person every time. Give two or three such people DIFFERENT stand-ins
  so they stay distinct, and keep the relationships between them intact
  without names ("his sister", "her husband", "their boss"). This is not a
  content change -- the person and everything they do stay the same, only
  the name goes. EXCEPTION: keep the real names of genuinely well-known
  public figures central to the facts (heads of state, government officials,
  world-famous business or historical figures).
- Remove any duplicated or stuttered phrases (leftover transcription
  artifacts like "for more than two straight for more than two straight
  months") -- keep one clean copy of the sentence.
- Cut stiff/formal phrasing ("there exists", "it is the case that") in favor
  of how someone would actually say it out loud.
- No markdown, no headers, no bullet points, no stage directions, and no
  speaker labels -- never begin a line with "Narrator:", "NARRATOR",
  "Voice-over:", or similar. Output plain narration paragraphs only, same as
  the input.
- Keep it roughly the same length as the input. This is a polish, not a
  summary.
- Output ONLY the rewritten script text, nothing else -- no preamble, no
  notes, no "Here's the rewrite:"."""


def load_polish_system(documentary):
    """Pick the --polish rulebook.

    --documentary always uses that channel's own tone rules. Otherwise
    this reads llm-rewrite-instructions.txt (the news rewrite rulebook) and
    falls back to the built-in POLISH_SYSTEM if that file is missing or empty.
    """
    if documentary:
        return DOCUMENTARY_SYSTEM_PROMPT
    if NEWS_REWRITE_INSTRUCTIONS.exists():
        text = NEWS_REWRITE_INSTRUCTIONS.read_text(encoding="utf-8").strip()
        if text:
            return text
    return POLISH_SYSTEM

# Measured pace of this pipeline's narrator voice (en-US-AndrewNeural), from
# 2,302 real words in 829.8s of audio. The other Master Prompt's 140 wpm is
# wrong for this voice and produces scripts that come out short.
WPM_DEFAULT_RATE = 166.4
WPM_SLOWED = 150.0  # at the --rate -10% recommended for this audience

MIN_WORDS = 1800   # floor that keeps the video over 10 minutes at either rate
MAX_WORDS = 3400   # ceiling that keeps it under 25 minutes

# Section plan. Fractions of the total word target, following the structure in
# News Master Prompt.txt. "what happened" is the spine and gets the most room.
SECTIONS = [
    ("open", 0.06,
     "The story itself in two or three plain sentences, so a viewer who watches "
     "only this much still knows what happened. Start with the news. No greeting, "
     "no 'welcome back', no teasing what is coming later."),
    ("why it matters", 0.08,
     "Why this touches an ordinary life -- money, health, safety, family, the "
     "price of something. Make it concrete and personal to one viewer."),
    ("what happened", 0.26,
     "The events in time order, slowly, with dates spoken as words. This is the "
     "spine of the script. Walk through it step by step. Do not rush it and do "
     "not summarise it."),
    ("background", 0.16,
     "What led to this. Assume the viewer has not followed the story at all and "
     "did not see any earlier coverage. Explain the mechanism of how this works "
     "in plain terms."),
    ("where reports differ", 0.12,
     "Where the source reports disagree, stated openly. Give each version, then "
     "say plainly what is NOT in dispute. If the sources barely disagree, say "
     "that they broadly agree and spend the words on what all of them confirm."),
    ("what it means", 0.16,
     "Interpretation, clearly marked as interpretation with phrases like 'the "
     "argument being made is' or 'what supporters say'. Where there are sides, "
     "give each side its fair case in its own terms."),
    ("what happens next", 0.10,
     "What is expected next, what is still uncertain, and what to watch for. Be "
     "honest about the limits of what is known."),
    ("close", 0.06,
     "Restate the central fact in fresh words. One calm reflective line. A short "
     "warm invitation to comment or subscribe. Never a hard sell."),
]

# Style rules repeated on every section request. Kept tight on purpose -- the
# full rulebook is too long to resend per section without crowding out the
# facts, and these are the rules the models actually break.
STYLE_RULES = """HOW TO WRITE (these matter more than anything else):
- Sentences of TWELVE TO EIGHTEEN words. Not shorter. A string of six-word
  sentences sounds like a robot reading a list -- vary the length, and let some
  run to eighteen words before you stop.
- One idea per sentence. Never join two complete thoughts with a comma.
- Plain words only. Explain any term the moment it appears, inside the sentence.
- Contractions throughout: we're, isn't, that's, here's.
- Speak to ONE person as "you". Never "viewers" or "our audience".
- Round every number and give it a size a person can picture. Never give a
  percentage without saying what it is a percentage of.
- Write ALL numbers, dates and symbols as spoken words: "four and three
  quarters percent", "March the fourth, twenty twenty-six", "forty seven
  dollars". Never use digits or the % $ signs. This is read aloud by a machine.
- NEVER name or number the sources. Do not write "Outlet A", "the first
  report", "source two", or any outlet's brand name. Say "one report",
  "another account", "the reporting from the scene".
- Never invent a fact, name, number or quote. Use ONLY the brief below. If the
  brief does not say it, it does not go in the script.
- Tone: a warm, trusted newsreader with time to explain properly. Calm about
  serious things. Never dramatic, never scolding, never telling the viewer
  what to feel.
- NEVER use these phrases: dive in, delve, buckle up, shockwaves, game-changer,
  in conclusion, it's important to note, at the end of the day, slammed,
  blasted, in today's fast-paced world, stay tuned, that being said, moreover,
  furthermore, needless to say, testament to, landscape, navigate.

OUTPUT: plain spoken paragraphs only. No heading, no title, no label, no
markdown, no bullets, no stage directions, no word count, no preamble such as
"Here is the section", and NO speaker label -- never start a line with
"Narrator:", "NARRATOR", or "Voice-over:". Output nothing but the words the
narrator will say."""

BANNED_PHRASES = [
    "dive in", "diving in", "delve", "buckle up", "shockwave", "game-chang",
    "in conclusion", "important to note", "at the end of the day", "slammed",
    "blasted", "fast-paced", "stay tuned", "that being said", "moreover",
    "furthermore", "needless to say", "testament to", "in today's world",
]

OUTLET_NAMING = re.compile(
    r"\b(?:outlet|source|report|article)\s+(?:[a-c]|one|two|three|1|2|3)\b"
    r"|\bthe (?:first|second|third) (?:outlet|source)\b",
    re.I,
)


# --------------------------------------------------------------------------
# cleaning
# --------------------------------------------------------------------------

_MD_PATTERNS = [
    (re.compile(r"^\s{0,3}#{1,6}\s+.*$", re.M), ""),        # headings
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),            # bold
    (re.compile(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", re.S), r"\1"),  # italics
    (re.compile(r"^\s*[-*+•]\s+", re.M), ""),               # bullets
    (re.compile(r"^\s*\d+[.)]\s+", re.M), ""),              # numbered lists
    (re.compile(r"^\s*[-=_]{3,}\s*$", re.M), ""),           # rules
    (re.compile(r"^\s*\[[^\]]*\]\s*$", re.M), ""),          # [stage directions]
]

# Models like to open with "Here is the section:" or label the part they were
# asked for. Both would be read aloud by the narrator.
_PREAMBLE = re.compile(
    r"^\s*(?:here(?:'s| is| are)[^.\n]*[:.]|sure[,!][^.\n]*[:.]|"
    r"(?:opening|section|part|narration|script)\s*[:\-][^\n]*)\s*",
    re.I,
)
_LABEL_LINE = re.compile(
    r"^\s*(?:open(?:ing)?|why it matters|what happened|background|"
    r"where reports differ|what it means|what happens next|close|closing)\s*:?\s*$",
    re.I | re.M,
)


def clean_narration(text):
    """Strip everything a narrator must not read aloud."""
    text = _PREAMBLE.sub("", text)
    text = _LABEL_LINE.sub("", text)
    for pat, repl in _MD_PATTERNS:
        text = pat.sub(repl, text)
    # "Narrator:" / "[Voice-over]:" speaker labels the models prepend to a
    # section or paragraph -- edge-tts would read them aloud. Run after the
    # markdown pass so a bold "**Narrator:**" is already unwrapped.
    text = strip_speaker_labels(text)
    # Source-naming the models slip in despite being told not to.
    text = OUTLET_NAMING.sub("one report", text)
    text = re.sub(r"\bAccording to one report, one report\b", "According to one report", text, flags=re.I)
    # A whole response wrapped in quotes.
    text = text.strip()
    if len(text) > 2 and text[0] in "\"'" and text[-1] in "\"'":
        text = text[1:-1].strip()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def word_count(text):
    return len(text.split())


def runtime_line(words):
    return (
        f"{words:,} words -> {words / WPM_SLOWED:.1f} min at --rate -10%, "
        f"{words / WPM_DEFAULT_RATE:.1f} min at default rate"
    )


# --------------------------------------------------------------------------
# passes
# --------------------------------------------------------------------------

BRIEF_SYSTEM = """You are a news desk fact-checker. You are given several reports of the same
story from different outlets. Extract a factual brief.

Rules:
- Use ONLY what the reports actually say. Never add outside knowledge.
- A fact goes in "agreed" only if it is not contradicted anywhere.
- Any figure, date or detail the reports give DIFFERENTLY goes in "disagreements".
- Never invent a quote. Only quote words a report attributes to someone.

Return ONLY a JSON object, no other text, with these keys:
{
  "headline": "one plain sentence saying what happened",
  "agreed": ["facts every report supports"],
  "disagreements": [{"topic": "...", "versions": ["what one says", "what another says"], "undisputed": "what is agreed despite the difference"}],
  "timeline": [{"when": "...", "what": "..."}],
  "people": [{"name": "...", "role": "...", "why_they_matter": "..."}],
  "numbers": [{"value": "...", "means": "what it represents in plain words"}],
  "quotes": [{"speaker": "...", "role": "...", "words": "exact quoted words"}],
  "why_it_matters": ["concrete effects on an ordinary person"],
  "unknowns": ["what is not yet known or not yet decided"],
  "background": ["context the reports give about how this came about"]
}"""


def build_brief(sources, key, model, verbose=True):
    """Cross-check the sources into a structured fact brief."""
    joined = "\n\n".join(
        f"===== REPORT {i} =====\n{s}" for i, s in enumerate(sources, 1)
    )
    if verbose:
        print("  [1/3] cross-checking sources into a fact brief...")
    raw = complete(
        [
            {"role": "system", "content": BRIEF_SYSTEM},
            {"role": "user", "content": joined},
        ],
        key,
        model=model,
        temperature=0.2,          # extraction, not creativity
        # 4000 is ~2.5x the largest brief this has produced. Kept deliberately
        # tight: the reservation counts against a 12,000 token/minute budget,
        # and an 8000 request eats most of the window on its own.
        max_tokens=4000,
        verbose=verbose,
    )
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise LLMError("Fact-brief pass did not return JSON. Try re-running.")
    try:
        brief = json.loads(m.group(0))
    except ValueError as e:
        raise LLMError(f"Fact brief was not valid JSON ({e}). Try re-running.")
    if verbose:
        print(
            f"        {len(brief.get('agreed', []))} agreed facts, "
            f"{len(brief.get('disagreements', []))} disagreements, "
            f"{len(brief.get('quotes', []))} quotes, "
            f"{len(brief.get('unknowns', []))} open questions"
        )
    return brief


def write_section(name, target_words, guidance, brief, written_tail, covered,
                  key, model, verbose=True):
    system = (
        "You write narration for a television news channel whose viewers are "
        "aged fifty and over. You are writing ONE section of a longer script.\n\n"
        + STYLE_RULES
    )
    prev = (
        f"\nTHE SCRIPT SO FAR ENDS LIKE THIS (continue naturally from it, never "
        f"repeat it):\n...{written_tail}\n"
        if written_tail
        else ""
    )
    already = (
        f"\nALREADY COVERED, do not repeat: {', '.join(covered)}.\n" if covered else ""
    )
    user = (
        f"FACT BRIEF (the only facts you may use):\n"
        f"{json.dumps(brief, ensure_ascii=False, indent=1)}\n"
        f"{prev}{already}\n"
        f"NOW WRITE THIS SECTION: {name}\n"
        f"{guidance}\n\n"
        f"Write {target_words} words. Do NOT exceed {int(target_words * 1.15)} "
        f"words -- going long here pushes the finished video past its time "
        f"limit. Do not label the section. Output only the narration."
    )
    text = complete(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        key,
        model=model,
        temperature=0.85,
        max_tokens=max(1200, int(target_words * 3)),
        verbose=verbose,
    )
    return clean_narration(text)


POLISH_SYSTEM = """You are a script doctor for a news channel whose viewers are aged fifty and
over. You are given narration that is factually correct but reads stiffly.

Rewrite it so it sounds like a warm, trusted newsreader speaking, NOT like text
being read out.

ABSOLUTE RULES:
- Change NOT ONE FACT. Every name, number, date, quote and claim stays exactly
  as it is. You are changing rhythm and wording only.
- Add nothing. Remove nothing. Keep the same length, give or take a little.
- Keep every paragraph break exactly where it is.

HARD LIMIT: no sentence may exceed TWENTY words. Count as you write. This
matters more than any other instruction here -- a long sentence is the single
worst thing you can do to a listener of this age. When merging short sentences,
stop at eighteen words.

WHAT TO FIX:
- Sentences under about ten words strung together: merge SOME of them into
  twelve to eighteen word sentences so it stops sounding clipped. Never merge
  more than two, and never past twenty words.
- Sentences over twenty words: split them into two or three.
- Two complete thoughts joined by a comma: make them two sentences.
- Missing contractions: use we're, isn't, that's, here's.
- Three sentences in a row starting the same way: vary the openings.
- Flat transitions: use natural spoken signposts sparingly, like "now, here's
  the part that matters" or "let me back up for a moment".
- Any digits or symbols: write them as spoken words.

NEVER use: dive in, delve, buckle up, shockwaves, game-changer, in conclusion,
it's important to note, at the end of the day, slammed, stay tuned, that being
said, moreover, furthermore, needless to say.

Output ONLY the rewritten narration. No preamble, no notes, no markdown, and
no speaker labels -- never begin a line with "Narrator:", "NARRATOR", or
"Voice-over:"."""


def polish(text, key, model, system=POLISH_SYSTEM, verbose=True):
    """Rhythm/naturalness pass over an existing script, in paragraph batches.

    Batched rather than whole-script because a single request over 2,500 words
    reliably comes back shortened -- these models summarise when asked to
    rewrite something long. `system` picks the rulebook -- see
    load_polish_system() for how it's chosen (news vs. --documentary).
    """
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    # A raw --yt-link transcript is one giant blob with no blank lines at
    # all, so it comes in here as a single "paragraph". Split any paragraph
    # over ~1.5x the batch target by sentence, so it can't produce one
    # oversized request on its own (that's what caused the 413s).
    units = []
    for p in paras:
        if word_count(p) <= 480:
            units.append(p)
            continue
        sentences = [s.strip() for s in SENTENCE_SPLIT.split(p) if s.strip()]
        cur, cur_words = [], 0
        for s in sentences:
            cur.append(s)
            cur_words += word_count(s)
            if cur_words >= 320:
                units.append(" ".join(cur))
                cur, cur_words = [], 0
        if cur:
            units.append(" ".join(cur))

    batches = []
    cur, cur_words = [], 0
    for u in units:
        cur.append(u)
        cur_words += word_count(u)
        if cur_words >= 320:
            batches.append(cur)
            cur, cur_words = [], 0
    if cur:
        batches.append(cur)

    out = []
    for i, batch in enumerate(batches, 1):
        chunk = "\n\n".join(batch)
        before = word_count(chunk)
        if verbose:
            print(f"  polishing batch {i}/{len(batches)} ({before} words)...")
        new = clean_narration(
            complete(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": chunk},
                ],
                key,
                model=model,
                temperature=0.7,
                max_tokens=max(1500, before * 3),
                verbose=verbose,
            )
        )
        after = word_count(new)
        # Guard against the summarising failure mode: if the model gave back
        # noticeably less than it was given, keep the original batch.
        if after < before * 0.75:
            if verbose:
                print(f"      rejected (came back {after}w vs {before}w) -- keeping original")
            out.append(chunk)
        else:
            out.append(new)
    return "\n\n".join(out)


SPLIT_SYSTEM = """You split over-long sentences for a news script read aloud to viewers aged
fifty and over.

You are given numbered sentences. Rewrite EACH one as two or three sentences of
no more than eighteen words each.

THE ONE MISTAKE TO AVOID: do not shorten the text. You are SPLITTING a long
sentence into shorter ones, not summarising it. Your rewrite must be about the
SAME TOTAL LENGTH as what you were given, or slightly longer -- splitting
usually adds words, because the new sentence needs its own subject. Every
detail in the original must survive. If your version is shorter than the
original, you have done it wrong.

Example.
Given (34 words):
  "The bank raised rates to four and three quarters percent on Tuesday, the
   highest level since two thousand eight, after a seven to two vote that most
   economists had not expected."
Correct answer (39 words):
  "The bank raised rates to four and three quarters percent on Tuesday. That's
   the highest level since two thousand eight. The vote was seven to two. Most
   economists hadn't expected it at all."
Note that nothing was dropped and the total got slightly longer.

- Change no facts. Keep every name, number, date and quoted phrase identical.
- Keep numbers written as spoken words. Never introduce digits.
- Natural spoken English. Contractions are good.

Return ONLY a JSON object mapping each number to its rewritten text, like:
{"1": "First rewritten version. Second sentence.", "2": "..."}"""

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
MAX_SENTENCE_WORDS = 22


def fix_long_sentences(text, key, model, rounds=3, verbose=True):
    """Split only the sentences that are too long, leaving the rest untouched.

    A whole-script rewrite to fix sentence length loses far more than it gains
    -- it re-rolls prose that was already fine and tends to shorten the script.
    Sending only the offenders and substituting them back in place is exact,
    cheap, and cannot disturb anything else.
    """
    for rnd in range(rounds):
        sents = [s.strip() for s in SENTENCE_SPLIT.split(text) if s.strip()]
        longs = [s for s in sents if len(s.split()) > MAX_SENTENCE_WORDS]
        if not longs:
            break
        if verbose:
            print(f"  splitting {len(longs)} over-long sentence(s) "
                  f"(round {rnd + 1})...")
        fixed_any = False
        # Batch so one request handles a dozen sentences.
        for i in range(0, len(longs), 12):
            batch = longs[i:i + 12]
            numbered = "\n".join(f"{n + 1}. {s}" for n, s in enumerate(batch))
            try:
                raw = complete(
                    [{"role": "system", "content": SPLIT_SYSTEM},
                     {"role": "user", "content": numbered}],
                    key, model=model, temperature=0.4, max_tokens=4000,
                    verbose=verbose,
                )
                m = re.search(r"\{.*\}", raw, re.S)
                if not m:
                    continue
                mapping = json.loads(m.group(0))
            except (LLMError, ValueError):
                continue
            for n, original in enumerate(batch, 1):
                new = clean_narration(str(mapping.get(str(n), "")).strip())
                # Only accept a genuine split that kept the substance.
                if not new or new == original:
                    continue
                if len(new.split()) < len(original.split()) * 0.7:
                    continue  # summarised rather than split
                if max((len(s.split()) for s in SENTENCE_SPLIT.split(new)), default=99) > MAX_SENTENCE_WORDS:
                    continue  # still too long
                text = text.replace(original, new, 1)
                fixed_any = True
        if not fixed_any:
            break
    return text


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def validate(text, verbose=True):
    """Report anything that would hurt the narration. Warnings, not errors."""
    problems = []
    low = text.lower()

    hits = sorted({b for b in BANNED_PHRASES if b in low})
    if hits:
        problems.append(f"banned phrases present: {', '.join(hits)}")

    digits = re.findall(r"\b\d[\d,.]*\b", text)
    if digits:
        problems.append(
            f"{len(digits)} number(s) still in digits (TTS may misread): "
            f"{', '.join(digits[:8])}"
        )

    symbols = [s for s in ("%", "$", "£", "€", "&") if s in text]
    if symbols:
        problems.append(f"symbols left in text: {' '.join(symbols)}")

    named = OUTLET_NAMING.findall(text)
    if named:
        problems.append(f"{len(named)} source-naming phrase(s) survived cleaning")

    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    longs = [s for s in sents if len(s.split()) > 25]
    if longs:
        problems.append(
            f"{len(longs)} sentence(s) over 25 words (hard to read aloud): "
            f'"{longs[0][:70]}..."'
        )

    # Whole repeated sentences are the classic long-generation failure.
    seen, dupes = set(), []
    for s in sents:
        k = re.sub(r"[^a-z ]", "", s.lower()).strip()
        if len(k.split()) >= 6:
            if k in seen:
                dupes.append(s)
            seen.add(k)
    if dupes:
        problems.append(f'{len(dupes)} repeated sentence(s), e.g. "{dupes[0][:70]}..."')

    if verbose:
        if problems:
            print("\n  Warnings:")
            for p in problems:
                print(f"    - {p}")
        else:
            print("\n  No problems found.")
    return problems


# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# resume cache
# --------------------------------------------------------------------------
#
# A full build costs roughly forty thousand tokens and the free tier allows a
# hundred thousand a DAY, so a quota block partway through is normal rather
# than exceptional -- and without this, every block threw away the brief and
# every finished section, then charged full price to redo them. The cache makes
# a blocked run resumable: re-run the same command after the quota resets and
# it picks up at the first section it never got to.
#
# Guarded by a hash of the sources plus the settings that change what gets
# written. Edit a source file, change --words or --model, and the cache is
# discarded rather than silently mixing old sections into a new script.

CACHE_NAME = ".build_cache.json"


def _cache_signature(sources, words, model):
    h = hashlib.sha256()
    for s in sources:
        h.update(s.encode("utf-8", "replace"))
        h.update(b"\0")
    h.update(f"{words}|{model}".encode("utf-8"))
    return h.hexdigest()


def load_cache(project_dir, signature, fresh=False, verbose=True):
    path = project_dir / CACHE_NAME
    if fresh or not path.exists():
        return {"signature": signature, "brief": None, "sections": {}}
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {"signature": signature, "brief": None, "sections": {}}
    if cache.get("signature") != signature:
        if verbose:
            print("  sources or settings changed -- ignoring the previous "
                  "partial build")
        return {"signature": signature, "brief": None, "sections": {}}
    return cache


def save_cache(project_dir, cache):
    (project_dir / CACHE_NAME).write_text(
        json.dumps(cache, ensure_ascii=False), encoding="utf-8"
    )


def load_sources(project_dir):
    files = sorted(
        project_dir.glob("script-*.txt"),
        key=lambda p: int(re.search(r"(\d+)", p.stem).group(1))
        if re.search(r"(\d+)", p.stem)
        else 0,
    )
    sources = []
    for f in files:
        t = f.read_text(encoding="utf-8", errors="replace").strip()
        if t:
            sources.append(t)
    return files, sources


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("project", help="Path under projects/, e.g. en/rate-rise")
    parser.add_argument("--words", type=int, default=2500,
                        help="Target word count (default 2500 ~ 17 min at --rate -10%%)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Gemini model")
    parser.add_argument("--api-key", default=None,
                        help="Gemini key (else every line in gemini_key.txt, or "
                             "$GEMINI_API_KEY). Multiple keys in gemini_key.txt "
                             "(one per line) are used as fallbacks when a "
                             "request is too large or a key's quota runs out.")
    parser.add_argument("--polish", action="store_true",
                        help="Skip generation: run the naturalness pass over the existing script.txt")
    parser.add_argument("--documentary", action="store_true",
                        help="Rewrite using the humanized "
                             "documentary tone instead of the news rewrite "
                             "rulebook (llm-rewrite-instructions.txt), and "
                             "skip the news-only sentence-length/banned-phrase "
                             "passes. Implies --polish (rewrites the existing "
                             "script.txt in place) unless combined with "
                             "--yt-link -- no script-N.txt sources needed.")
    parser.add_argument("--no-polish", action="store_true",
                        help="Skip the naturalness pass after generating")
    parser.add_argument("--brief-only", action="store_true",
                        help="Write the fact brief to brief.json and stop (cheap sanity check)")
    parser.add_argument("--fresh", action="store_true",
                        help="Ignore the cached brief and sections from an "
                             "interrupted run and rebuild from scratch")
    parser.add_argument("--yt-link", default=None, metavar="URL",
                        help="Fetch this YouTube video's transcript (no "
                             "timestamps), write it to script.txt, and run "
                             "the same rewrite pass as --polish. Creates the "
                             "project folder if it doesn't exist yet.")
    args = parser.parse_args()

    # documentary mode never does multi-source generation -- it's always one
    # script.txt rewritten in place, same as its own standalone tool. So
    # --documentary alone (no --polish, no --yt-link) still means
    # "rewrite the existing script.txt", not "merge script-1/2/3.txt".
    if args.documentary and not args.yt_link:
        args.polish = True

    project_dir = PROJECTS_DIR / args.project
    if args.yt_link:
        project_dir.mkdir(parents=True, exist_ok=True)
    elif not project_dir.exists():
        sys.exit(f"No such project folder: {project_dir}")
    if not RULEBOOK.exists():
        print(f"warning: {RULEBOOK.name} not found -- using built-in rules only")

    try:
        key = load_keys(args.api_key)
    except LLMError as e:
        sys.exit(str(e))
    if len(key) > 1:
        print(f"  {len(key)} Gemini keys loaded -- will roll over to the next "
              f"one if a key runs out of room\n")

    script_path = project_dir / "script.txt"

    # ---- fetch a YouTube transcript, then fall through to the rewrite -----
    if args.yt_link:
        print(f"Fetching transcript: {args.yt_link}")
        try:
            transcript = fetch_transcript_text(args.yt_link)
        except YouTubeTranscriptError as e:
            sys.exit(str(e))
        script_path.write_text(transcript.strip() + "\n", encoding="utf-8")
        print(f"  saved raw transcript -> {script_path.name} "
              f"({word_count(transcript)} words)\n")
        args.polish = True

    # ---- polish-only mode (also the --yt-link rewrite step) ---------------
    if args.polish:
        if not script_path.exists():
            sys.exit(f"--polish needs an existing {script_path}")
        original = script_path.read_text(encoding="utf-8").strip()
        system = load_polish_system(args.documentary)
        rulebook = (
            "documentary tone rules" if args.documentary
            else NEWS_REWRITE_INSTRUCTIONS.name if NEWS_REWRITE_INSTRUCTIONS.exists()
            else "built-in POLISH_SYSTEM (fallback)"
        )
        print(f"Polishing {script_path.name} ({word_count(original)} words) "
              f"with {args.model} [{rulebook}]\n")
        backup = project_dir / "script.pre-polish.txt"
        backup.write_text(original, encoding="utf-8")
        result = polish(original, key, args.model, system=system)
        if args.documentary:
            # News-only passes: spoken-word numbers and 20-word sentence caps
            # would fight the documentary tone's intentionally varied pacing.
            script_path.write_text(result + "\n", encoding="utf-8")
            print(f"\n  original kept at {backup.name}")
            print(f"  {runtime_line(word_count(result))}")
        else:
            result = fix_long_sentences(result, key, args.model)
            script_path.write_text(result + "\n", encoding="utf-8")
            print(f"\n  original kept at {backup.name}")
            print(f"  {runtime_line(word_count(result))}")
            validate(result)
        return

    # ---- generate ---------------------------------------------------------
    files, sources = load_sources(project_dir)
    if len(sources) < 2:
        sys.exit(
            f"Need at least two source files. Found {len(sources)} in {project_dir}.\n"
            f"Create script-1.txt, script-2.txt and script-3.txt with the reports "
            f"you want merged."
        )
    print(f"Sources: {', '.join(f.name for f in files)} "
          f"({sum(word_count(s) for s in sources):,} words in)")
    print(f"Model:   {args.model}")
    print(f"Target:  {runtime_line(args.words)}\n")

    if args.words < MIN_WORDS:
        print(f"warning: {args.words} words is under the {MIN_WORDS}-word floor "
              f"for a 10-minute video\n")
    if args.words > MAX_WORDS:
        print(f"warning: {args.words} words may run past 25 minutes\n")

    signature = _cache_signature(sources, args.words, args.model)
    cache = load_cache(project_dir, signature, fresh=args.fresh)

    try:
        if cache.get("brief"):
            brief = cache["brief"]
            print("  [1/3] reusing the fact brief from the previous run "
                  "(--fresh to rebuild)")
        else:
            brief = build_brief(sources, key, args.model)
            cache["brief"] = brief
            save_cache(project_dir, cache)
        (project_dir / "brief.json").write_text(
            json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if args.brief_only:
            print(f"\n  brief.json written. Review it, then re-run without --brief-only.")
            return

        done = cache.setdefault("sections", {})
        if done:
            print(f"  [2/3] writing {len(SECTIONS)} sections "
                  f"({len(done)} already done, resuming)...")
        else:
            print(f"  [2/3] writing {len(SECTIONS)} sections...")
        parts, covered = [], []
        for name, weight, guidance in SECTIONS:
            target = max(60, int(args.words * weight))
            if name in done:
                text = done[name]
                print(f"        {name:<22} {word_count(text):>4}w (cached)")
            else:
                tail = " ".join(" ".join(parts).split()[-90:]) if parts else ""
                text = write_section(name, target, guidance, brief, tail, covered,
                                     key, args.model)
                print(f"        {name:<22} {word_count(text):>4}w (asked {target})")
                done[name] = text
                save_cache(project_dir, cache)
            parts.append(text)
            covered.append(name)

        script = "\n\n".join(p for p in parts if p)

        # Write the unpolished script before spending anything on polish, so a
        # quota block during the rhythm passes still leaves a usable script.txt
        # rather than nothing at all.
        script_path.write_text(script + "\n", encoding="utf-8")

        if not args.no_polish:
            print(f"  [3/3] naturalness pass...")
            script = polish(script, key, args.model)
        else:
            print("  [3/3] naturalness pass skipped (--no-polish)")

        # Always run, polished or not: the sections themselves produce
        # over-long sentences, and the polish pass makes it worse by merging.
        script = fix_long_sentences(script, key, args.model)

    except LLMError as e:
        sys.exit(
            f"\nGemini error: {e}\n\n"
            f"Re-run the same command to resume -- the fact brief and every "
            f"finished section are cached in {CACHE_NAME}, so nothing already "
            f"paid for is spent twice."
        )

    words = word_count(script)
    script_path.write_text(script + "\n", encoding="utf-8")

    # Crash recovery only. Dropping it on success keeps re-running the same
    # command a genuinely fresh take rather than a replay of cached sections.
    (project_dir / CACHE_NAME).unlink(missing_ok=True)

    print(f"\n  script.txt written -- {runtime_line(words)}")
    if words < MIN_WORDS:
        print(f"  ** UNDER the {MIN_WORDS}-word floor: video would come in under "
              f"10 minutes. Re-run with a higher --words, or add more source "
              f"material. **")
    elif words > MAX_WORDS:
        print(f"  ** OVER the {MAX_WORDS}-word ceiling: video may exceed 25 "
              f"minutes. Re-run with a lower --words. **")

    validate(script)

    print(f"\nRead script.txt before going further -- it is machine-written from "
          f"your sources.\nThen:")
    print(f"    python step1_audio_and_captions.py {args.project} "
          f"--font-size 88 --rate -10%")


if __name__ == "__main__":
    main()
