"""
Combine a two-person conversation into a one-person script.

Reads a single dialogue file where two people take alternating, labeled turns
(for example "Alice:" ... "Bob:" ... "Alice:" ...) and rewrites the whole thing
into ONE continuous script that reads as if a single person is speaking, in the
first person. Writes the result to script.txt in the same project folder, ready
for step1.

Usage:
    python combine_speakers.py <lang>/<slug>
    python combine_speakers.py <lang>/<slug> --input interview.txt
    python combine_speakers.py <lang>/<slug> --perspective third

e.g. python combine_speakers.py en/rate-chat

Input (default projects/<lang>/<slug>/dialogue.txt), labeled turns:

    Alice: So what actually happened here?
    Bob: The rate went up on Tuesday.
    Alice: And why does that matter to me?
    Bob: It changes what your loan costs.

The speaker labels are only hints -- they are stripped from the output, which
reads as one person's continuous narration with nothing left to show it began
as a conversation.

WHY IT WORKS IN BATCHES
Same reason step0 does: asking any of these models to rewrite a long transcript
in one request makes them compress the back half and drop detail. Converting a
few turns at a time against a running tail keeps every request small enough to
preserve all the content while still reading as one unbroken voice.
"""
import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

from llm_client import DEFAULT_MODEL, LLMError, complete, load_key

# Reuse the pipeline's narration cleaner, word counter, runtime estimate and
# validator so the output obeys the same TTS-safe rules as step0's script.txt.
from step0_build_script import clean_narration, runtime_line, validate, word_count

REPO_ROOT = Path(__file__).parent.parent
PROJECTS_DIR = REPO_ROOT / "projects"

# Turns to gather per Gemini request. ~320 spoken words keeps each request well
# inside the 250k-token/minute free-tier window (see llm_client note 5) while
# still giving the model enough context to blend the turns smoothly.
BATCH_WORDS = 320


# --------------------------------------------------------------------------
# parsing the transcript
# --------------------------------------------------------------------------

# A turn label is a short prefix before the first colon on a line, e.g.
# "Alice:", "Bob:", "Q:", "Interviewer:". Capped at four words and rejected if
# it ends like a sentence, so an ordinary mid-line colon ("Here's the thing:")
# doesn't get mistaken for a speaker.
_LABEL = re.compile(r"^\s*([^:\n]{1,40}?)\s*:\s+(.*)$")

# Transcripts often prefix each turn with a timestamp -- "00:00:34 Bob: ...",
# "[01:22] Sara: ...", "1:06 - ...". The colons inside it would otherwise be
# read as the speaker's label (and the timestamp would be read aloud), so strip
# a leading one before looking for the real label. Never matches money or
# ordinary text because it must be anchored at the very start of the line.
_TIMESTAMP = re.compile(
    r"^\s*[\[(]?\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?[\])]?\s*[-–]?\s*"
)


def _strip_timestamp(line):
    return _TIMESTAMP.sub("", line, count=1).strip()


def _candidate_label(line):
    m = _LABEL.match(line)
    if not m:
        return None
    label = m.group(1).strip()
    if not label or len(label.split()) > 4 or label[-1] in ".!?":
        return None
    return label


def parse_turns(text):
    """Split a labeled transcript into [(speaker, words), ...] in order.

    Two passes so a stray one-off "Note:" line can't be mistaken for a speaker:
    a prefix only counts as a real speaker label if it recurs (a dialogue
    alternates, so each speaker's label appears more than once). A line that
    isn't a new label is folded onto the current turn, so a turn can run over
    several lines.
    """
    lines = [
        _strip_timestamp(l) for l in (raw.strip() for raw in text.splitlines()) if l
    ]

    counts = Counter(lab for lab in map(_candidate_label, lines) if lab)
    real = {lab for lab, c in counts.items() if c >= 2}

    turns = []
    speaker, buf = None, []
    for line in lines:
        m = _LABEL.match(line)
        lab = m.group(1).strip() if m else None
        # A recurring label is a real turn boundary. If nothing recurred at all
        # (e.g. a two-turn snippet), fall back to treating any candidate label
        # as a boundary so we still split the speakers.
        is_boundary = m is not None and (lab in real or (not real and _candidate_label(line)))
        if is_boundary:
            if buf:
                turns.append((speaker or "Speaker", " ".join(buf).strip()))
                buf = []
            speaker = lab
            rest = m.group(2).strip()
            if rest:
                buf.append(rest)
        else:
            buf.append(line)
    if buf:
        turns.append((speaker or "Speaker", " ".join(buf).strip()))

    # No labels found anywhere: treat blank-line-separated paragraphs as turns
    # so a plain alternating transcript still batches instead of going in as one
    # giant block.
    if len(turns) <= 1:
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        if len(paras) > 1:
            turns = [("Speaker", _strip_timestamp(p)) for p in paras]

    return turns


def group_turns(turns, max_words=BATCH_WORDS):
    """Batch consecutive turns up to a word budget, never splitting a turn."""
    batches, cur, cur_words = [], [], 0
    for speaker, words in turns:
        w = word_count(words)
        if cur and cur_words + w > max_words:
            batches.append(cur)
            cur, cur_words = [], 0
        cur.append((speaker, words))
        cur_words += w
    if cur:
        batches.append(cur)
    return batches


# --------------------------------------------------------------------------
# the merge prompt
# --------------------------------------------------------------------------

_PERSPECTIVE = {
    "first": (
        "in the FIRST PERSON (\"I\", \"me\", \"my\"), as if one person is telling "
        "the whole thing themselves"
    ),
    "third": (
        "as a NEUTRAL NARRATOR recounting it (no \"I\"), the way a newsreader would"
    ),
}


def merge_system(perspective):
    return (
        "You are given part of a conversation between two people, written as "
        "alternating labeled turns (for example \"Alice:\" and \"Bob:\"). Rewrite "
        f"it into ONE continuous piece of narration that reads {_PERSPECTIVE[perspective]}.\n\n"
        "RULES (these matter more than anything else):\n"
        "- Collapse both speakers into ONE single voice. Delete every speaker "
        "label and every \"he said\" / \"she asked\" attribution. The result must "
        "never reveal that it began as a conversation between two people.\n"
        "- Keep ALL the substance from BOTH speakers -- every fact, point, "
        "example, number, opinion and detail must survive. You are changing the "
        "FORM from dialogue to monologue, NOT summarising. Do not shorten, skip "
        "or compress anything.\n"
        "- Turn questions and answers into flowing statements. A question one "
        "speaker asks becomes a natural lead-in the single voice raises and then "
        "addresses (\"Now, you might wonder why this matters -- here's why...\").\n"
        "- Where the two agree, say it once. Where they add different things, keep "
        "both. If they disagree, hold both views in the single voice (\"part of me "
        "sees it this way, but there's also...\") -- never invent a resolution or "
        "any new fact that neither speaker gave.\n"
        "- One consistent voice and tone throughout. Natural spoken English, "
        "contractions welcome (we're, isn't, that's, here's).\n"
        "- This is read aloud by a text-to-speech machine: write ALL numbers, "
        "dates and symbols as spoken WORDS (\"twenty twenty-six\", \"forty-seven "
        "dollars\", \"five percent\"), never digits or the % $ signs.\n"
        "- Do NOT open with a greeting or close with a sign-off unless the "
        "speakers themselves did. Just convert what you are given.\n\n"
        "OUTPUT: only the rewritten narration -- no preamble, no speaker labels, "
        "no markdown, no notes, no \"Here is the rewrite\"."
    )


def merge_batch(block, tail, perspective, key, model, verbose=True):
    tail_note = (
        "THE NARRATION SO FAR ENDS LIKE THIS (continue on smoothly from it, do "
        f"NOT repeat it):\n...{tail}\n\n"
        if tail
        else ""
    )
    user = (
        f"{tail_note}CONVERT THIS PART OF THE CONVERSATION:\n{block}"
    )
    before = word_count(block)
    text = complete(
        [
            {"role": "system", "content": merge_system(perspective)},
            {"role": "user", "content": user},
        ],
        key,
        model=model,
        temperature=0.7,
        max_tokens=max(1200, before * 3),
        verbose=verbose,
    )
    return clean_narration(text)


# --------------------------------------------------------------------------
# resume cache
# --------------------------------------------------------------------------
#
# A 7,000-word transcript is 25+ requests, and the Gemini free tier caps the day
# at 100,000 tokens -- a block partway through is normal, not exceptional. Like
# step0, we cache every finished batch (keyed by a hash of the transcript and
# the settings that change the output) so a re-run after the quota window picks
# up where it stopped instead of re-paying for -- and re-burning the daily quota
# on -- batches already done. Change the transcript, perspective, model or batch
# size and the cache is discarded rather than mixing old output into new.

CACHE_NAME = ".combine_cache.json"


def _signature(text, perspective, model, batch_words):
    h = hashlib.sha256()
    h.update(text.encode("utf-8", "replace"))
    h.update(f"|{perspective}|{model}|{batch_words}".encode("utf-8"))
    return h.hexdigest()


def load_cache(project_dir, signature, fresh=False):
    path = project_dir / CACHE_NAME
    if fresh or not path.exists():
        return {"signature": signature, "batches": {}}
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {"signature": signature, "batches": {}}
    if cache.get("signature") != signature:
        print("  transcript or settings changed -- ignoring the previous partial run")
        return {"signature": signature, "batches": {}}
    return cache


def save_cache(project_dir, cache):
    (project_dir / CACHE_NAME).write_text(
        json.dumps(cache, ensure_ascii=False), encoding="utf-8"
    )


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("project", help="Path under projects/, e.g. en/rate-chat")
    parser.add_argument(
        "--input",
        default="dialogue.txt",
        help="Transcript file inside the project folder (default dialogue.txt)",
    )
    parser.add_argument(
        "--perspective",
        choices=["first", "third"],
        default="first",
        help="Voice of the single speaker: 'first' = I/me/my (default), "
        "'third' = neutral narrator",
    )
    parser.add_argument(
        "--batch-words",
        type=int,
        default=BATCH_WORDS,
        help=f"Turns of transcript to convert per request (default {BATCH_WORDS})",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Gemini model")
    parser.add_argument(
        "--api-key", default=None, help="Gemini key (else gemini_key.txt or $GEMINI_API_KEY)"
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore the cached batches from an interrupted run and start over",
    )
    args = parser.parse_args()

    project_dir = PROJECTS_DIR / args.project
    if not project_dir.exists():
        sys.exit(f"No such project folder: {project_dir}")

    dialogue_path = project_dir / args.input
    if not dialogue_path.exists():
        sys.exit(
            f"No transcript at {dialogue_path}\n"
            f"Put the two-person conversation in {args.input} first, with each turn\n"
            f"labeled, e.g.:\n\n"
            f"    Alice: So what actually happened here?\n"
            f"    Bob: The rate went up on Tuesday.\n"
        )

    text = dialogue_path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        sys.exit(f"{dialogue_path} is empty.")

    turns = parse_turns(text)
    if not turns:
        sys.exit(f"Couldn't find any turns in {dialogue_path}.")

    speakers = sorted({sp for sp, _ in turns})
    batches = group_turns(turns, max_words=args.batch_words)
    in_words = word_count(text)
    print(
        f"Input:       {dialogue_path.name} "
        f"({len(turns)} turns from {len(speakers)} speaker(s): "
        f"{', '.join(speakers[:4])}{'...' if len(speakers) > 4 else ''}, "
        f"{in_words:,} words)"
    )
    print(f"Perspective: {args.perspective} person")
    print(f"Model:       {args.model}")
    print(f"Converting in {len(batches)} batch(es)...\n")

    try:
        key = load_key(args.api_key)
    except LLMError as e:
        sys.exit(str(e))

    signature = _signature(text, args.perspective, args.model, args.batch_words)
    cache = load_cache(project_dir, signature, fresh=args.fresh)
    done = cache.setdefault("batches", {})
    if done:
        print(f"  resuming: {len(done)}/{len(batches)} batch(es) already done\n")

    script_path = project_dir / "script.txt"
    parts = []
    blocked = None
    for i, batch in enumerate(batches):
        block = "\n".join(f"{sp}: {words}" for sp, words in batch)
        if str(i) in done:
            out = done[str(i)]
            print(f"  batch {i + 1}/{len(batches)}: {word_count(out):>4}w (cached)")
            parts.append(out)
            continue
        tail = " ".join(" ".join(parts).split()[-80:]) if parts else ""
        try:
            out = merge_batch(block, tail, args.perspective, key, args.model)
        except LLMError as e:
            blocked = e
            break
        print(
            f"  batch {i + 1}/{len(batches)}: "
            f"{word_count(block):>4}w in -> {word_count(out):>4}w out"
        )
        parts.append(out)
        done[str(i)] = out
        save_cache(project_dir, cache)

    # Always write what we have, even on a block -- a partial script.txt from
    # the first N batches is far more useful than nothing, and the cache lets a
    # re-run finish the rest.
    script = "\n\n".join(p for p in parts if p)
    if script:
        script_path.write_text(script + "\n", encoding="utf-8")

    if blocked is not None:
        partial = (
            f"  A partial script.txt ({word_count(script):,} words) was written in "
            f"the meantime.\n"
            if script
            else "  No batches finished yet, so script.txt was left untouched.\n"
        )
        sys.exit(
            f"\nGemini error: {blocked}\n\n"
            f"  {len(done)}/{len(batches)} batches are saved -- re-run the SAME "
            f"command after the quota resets and it finishes the rest.\n"
            f"{partial}"
        )

    out_words = word_count(script)

    # Guard against the summarising failure mode: a genuine dialogue->monologue
    # rewrite stays about the same length or grows a little. A big shrink means
    # the model compressed instead of converting.
    if out_words < in_words * 0.55:
        print(
            f"\n  ** Warning: output ({out_words:,}w) is much shorter than the "
            f"transcript ({in_words:,}w).\n     The model may have summarised "
            f"rather than converted. Review script.txt, and if so re-run with a "
            f"smaller --batch-words. **"
        )

    # All batches succeeded: script.txt is already written above. Drop the cache
    # so the next run of the same command is a genuinely fresh take rather than a
    # replay of the cached batches.
    (project_dir / CACHE_NAME).unlink(missing_ok=True)

    print(f"\n  script.txt written -- {runtime_line(out_words)}")
    validate(script)

    print(
        f"\nRead script.txt before going further -- it is machine-written from "
        f"your transcript.\nThen:"
    )
    print(
        f"    python step1_audio_and_captions.py {args.project} "
        f"--caption-style wordcolor --max-words 3 --font-size 110"
    )


if __name__ == "__main__":
    main()
