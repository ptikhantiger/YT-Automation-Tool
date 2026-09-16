"""
Step 1: script.txt -> audio.mp3, word_timings.json, scenes.json, captions.ass,
image_prompts.txt

Usage:
    python step1_audio_and_captions.py <lang>/<slug> [--voice VOICE] [--rate +0%]
        [--scene-seconds-min 4] [--scene-seconds-max 8] [--vertical]

--vertical switches captions (and, via render.json, step 3's output frame)
to 1080x1920 for YouTube Shorts / TikTok / Reels instead of the default
1920x1080 landscape.

e.g. python step1_audio_and_captions.py en/crumbs
     python step1_audio_and_captions.py es/test-crumbs --voice es-US-AlonsoNeural
     python step1_audio_and_captions.py en/pakistan --pause-every 3

--pause-every splices a real silent gap into the narration after every N
scenes -- no voice, just the already-picked clip for the scene right before
it (and background music, with step3's --music) continuing to play. No
separate clip pick needed and step2/step3 need no changes for it.
--pause-after-words does the same but triggers on content instead of scene
count: a pause after every sentence that lands N+ words past the previous
one (e.g. --pause-after-words 15).

--sentence-scenes makes every sentence its own scene, 1:1, with no LLM
grouping and no shot-splitting -- so each line of narration gets its own
clip pick in step 2. Use it when you pick or make the visuals yourself
(Local file / AI illustration) and want them to line up sentence for
sentence with no matching guesswork.

Reads projects/<lang>/<slug>/script.txt and writes all outputs into that same
project folder. Works for any language edge-tts has voices for -- <lang> is
just a folder name for your own organization (en/, es/, ...), but it also
picks a sensible default narrator voice when it matches a known language
below. Run voice_preview.py first if you haven't picked a narrator voice yet.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

import edge_tts

import random

from common import (
    caption_style,
    compute_sentence_end_flags,
    compute_sentence_end_punctuation,
    group_captions,
    group_scenes,
    insert_narration_pauses,
    insert_sentence_pauses,
    load_json,
    long_gap_starts,
    save_json,
    scene_image_filename,
    strip_speaker_labels,
    write_ass,
    write_karaoke_ass,
    write_wordcolor_ass,
)
from keywords import build_query, extract_keywords
from semantic_timeline import (
    MAX_SCENE_SECONDS,
    MIN_SCENE_SECONDS,
    build_scenes,
    print_plan,
    refresh_times,
    write_timeline,
)

PROJECTS_DIR = Path(__file__).parent.parent / "projects"

# Sensible default narrator voice per language folder -- override any time
# with --voice. Add more entries here as you add language folders.
DEFAULT_VOICES = {
    "es": "es-MX-JorgeNeural",
    "en": "en-US-AndrewNeural",
    "ur": "ur-PK-AsadNeural",
}


async def synthesize(text, voice, rate, out_mp3):
    communicate = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    words = []
    with open(out_mp3, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                words.append(
                    {
                        "text": chunk["text"],
                        "start": chunk["offset"] / 1e7,
                        "end": (chunk["offset"] + chunk["duration"]) / 1e7,
                    }
                )
    return words


def make_image_prompt(scene_text):
    snippet = " ".join(scene_text.split())
    if len(snippet) > 220:
        snippet = snippet[:220].rsplit(" ", 1)[0] + "..."
    return (
        f'A cinematic, warm, softly-lit photograph evoking this idea: "{snippet}" '
        "Style: calm, reflective, metaphorical -- soft natural light, muted warm tones, "
        "shallow depth of field, no visible text or logos, no literal close-up faces "
        "(silhouettes, distant figures, or pure nature/still-life symbolism only). "
        "Wide 16:9 cinematic composition."
    )


def format_timestamp(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", help="Path under projects/, e.g. en/crumbs or es/test-crumbs")
    parser.add_argument("--voice", default=None, help="edge-tts voice name (default depends on language folder)")
    parser.add_argument("--rate", default="-10%", help="edge-tts speech rate adjustment, e.g. -5%%")
    parser.add_argument(
        "--scene-seconds-min", type=float, default=12.0, help="Minimum seconds of narration per image/scene"
    )
    parser.add_argument(
        "--scene-seconds-max", type=float, default=25.0, help="Maximum seconds of narration per image/scene"
    )
    parser.add_argument(
        "--seed", type=int, default=None, help="Random seed for reproducible scene-duration picks"
    )
    parser.add_argument(
        "--sentence-scenes",
        action="store_true",
        help="Split the script into one scene per sentence -- a strict 1:1 mapping. "
        "Every sentence in script.txt becomes its own scene and its own clip pick in "
        "step 2, in order, with NO LLM merging of consecutive sentences and NO "
        "shot-splitting of long ones. Needs no Gemini key. Use this when you pick or make "
        "the visuals yourself (Local file / AI illustration) and want each line of "
        "narration to line up exactly with its own image or clip -- it removes the "
        "guesswork of matching separately-made visuals to grouped scenes. Overrides "
        "--no-semantic and the --semantic-*/--scene-seconds-* options.",
    )
    parser.add_argument(
        "--no-semantic",
        action="store_true",
        help="Use the legacy random-duration scene grouping (--scene-seconds-min/max) "
        "instead of the semantic timeline. By default scenes are cut where the MEANING "
        "changes -- whole sentences grouped/split into visual segments by the same Gemini "
        "LLM step 0 uses (falling back to exact sentence-boundary grouping if no Gemini "
        "key is configured), each scene mapped to the exact words and voice timestamps "
        "it represents, with per-scene visual requirements/exclusions and search "
        "queries, plus a timeline.json master map (word -> timestamp -> scene).",
    )
    parser.add_argument(
        "--semantic-min-seconds", type=float, default=MIN_SCENE_SECONDS,
        help="Semantic mode: scenes shorter than this are folded into a neighbor as an "
        "extra shot (the visual event keeps its exact timing; it just doesn't force a "
        "separate full-scene clip pick).",
    )
    parser.add_argument(
        "--semantic-max-seconds", type=float, default=MAX_SCENE_SECONDS,
        help="Semantic mode: scenes longer than this are subdivided into continuation "
        "shots at sentence bounds / natural pauses so one visual never runs stale.",
    )
    parser.add_argument(
        "--long-scene-every", type=int, default=0, metavar="N",
        help="Make every Nth scene hold for --long-scene-seconds of narration instead of "
        "the normal random --scene-seconds-min/max target -- an occasional slower, longer "
        "clip rather than a fast cut every time. 0 (default) disables this. "
        "e.g. --long-scene-every 3 --long-scene-seconds 15 for every third clip to run 15s.",
    )
    parser.add_argument(
        "--long-scene-seconds", type=float, default=15.0,
        help="Narration length of each long scene (with --long-scene-every)",
    )
    parser.add_argument(
        "--pause-every", type=int, default=0, metavar="N",
        help="Splice a real silent gap into the narration after every N scenes -- no voice, "
        "just the already-picked clip for the scene right before it (and background music, "
        "with --music on step3) continuing to play. 0 (default) disables this. No separate "
        "clip pick needed -- step2/step3 need no changes, the scene simply holds its clip "
        "through the extra gap the same way it already holds through any natural pause.",
    )
    parser.add_argument(
        "--pause-after-words", type=int, default=0, metavar="N",
        help="Splice a real silent gap into the narration at the end of every sentence "
        "that lands at least N words after the previous pause (or the start of the "
        "narration) -- content-driven pacing instead of --pause-every's fixed scene "
        "cadence. A sentence ending too soon is skipped and its words keep accumulating "
        "toward the next one that clears the bar. 0 (default) disables this. Combine "
        "with --pause-every if you want both triggers active at once. e.g. "
        "--pause-after-words 15 for a pause after every sentence that runs the "
        "narration 15+ words past the last one.",
    )
    parser.add_argument(
        "--pause-seconds-min", type=float, default=5.0,
        help="Minimum length of each silent pause (with --pause-every / --pause-after-words)",
    )
    parser.add_argument(
        "--pause-seconds-max", type=float, default=8.0,
        help="Maximum length of each silent pause (with --pause-every / --pause-after-words)",
    )
    parser.add_argument(
        "--image-prompts",
        action="store_true",
        help="Also write image_prompts.txt for the old Google Flow still-image workflow",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run even though clips have already been picked (discards that work)",
    )
    parser.add_argument(
        "--captions-only",
        action="store_true",
        help="Rewrite captions.ass from the existing word_timings.json and leave everything "
        "else alone. Safe after you've picked clips -- it doesn't re-synthesize audio or "
        "redraw scene boundaries, so selections.json stays valid.",
    )
    parser.add_argument(
        "--font-size",
        type=int,
        default=None,
        help="Override the caption font size for this language (English default 64, Urdu 140). "
        "Line length is rescaled automatically so the wider type still fits on screen. "
        "Use 88 for the 50+ news format -- see 'News videos' in README_PIPELINE.md.",
    )
    parser.add_argument(
        "--caption-style",
        choices=["wordcolor", "wordcolor-bottom", "highlight", "std-yellow-black", "classic"],
        default="classic",
        help="'wordcolor' = big bold centered headline look, white words with the "
        "current word coloured, no capsule (default; defaults --max-words to 3 "
        "unless you pass your own). Colours set by --text-color / --highlight-color "
        "/ --outline-color. "
        "'wordcolor-bottom' = a bottom-anchored branded look -- same big bold "
        "text and per-word colouring as 'wordcolor', but bottom-anchored (same "
        "position as 'highlight'/'classic', not vertically centered) with its own "
        "locked-in colours (white words, bright cyan #00E6FF current word, black "
        "outline) so the look stays consistent video to video. --text-color / "
        "--highlight-color / --outline-color still override it if you explicitly "
        "pass them. Always 3 words/card unless you pass --max-words. "
        "'highlight' = small bottom-anchored line, current word gets a yellow "
        "capsule + black text. "
        "'std-yellow-black' = classic yellow subtitle line (all words yellow, "
        "black outline), current word gets a black capsule behind it instead "
        "of a colour change -- yellow text stays yellow throughout, only the "
        "capsule marks which word is playing. "
        "'classic' = static bold white line with black outline, no per-word "
        "highlighting at all.",
    )
    parser.add_argument(
        "--max-words",
        type=int,
        default=None,
        help="Hard cap on words shown per caption card. Default: 3 for "
        "--caption-style wordcolor/wordcolor-bottom (the punchy few-words-on-screen "
        "look they're designed for), otherwise no cap -- grouping follows line length "
        "and pauses.",
    )
    parser.add_argument(
        "--text-color",
        default=None,
        help="Colour of the normal (not-yet-spoken) caption words, as #RRGGBB. "
        "Applies to the 'wordcolor'/'wordcolor-bottom' styles. Default white for both.",
    )
    parser.add_argument(
        "--highlight-color",
        default=None,
        help="Colour of the currently-spoken (highlighted) word, as #RRGGBB. "
        "Applies to the 'wordcolor'/'wordcolor-bottom' styles. Default #29D6F5 "
        "(cyan) for 'wordcolor', #00E6FF (brighter cyan, matched to the channel's "
        "reference caption image) for 'wordcolor-bottom'.",
    )
    parser.add_argument(
        "--outline-color",
        default=None,
        help="Colour of the caption outline/border, as #RRGGBB. "
        "Applies to the 'wordcolor'/'wordcolor-bottom' styles. Default black for both.",
    )
    parser.add_argument(
        "--vertical",
        action="store_true",
        help="Render for vertical 9:16 formats (YouTube Shorts / TikTok / Reels) at "
        "1080x1920 instead of the default 1920x1080 landscape. Captions are laid out "
        "for the narrower frame (line length rescales automatically, same as "
        "--font-size does) and this choice is saved to render.json so step 3 picks up "
        "the matching output size automatically -- no need to pass --vertical again. "
        "Step 2's clip picker also switches to portrait Pexels results so footage "
        "isn't cropped down from a much wider landscape source.",
    )
    parser.add_argument(
        "--no-chain",
        action="store_true",
        help="Don't automatically continue into step 2 (clip picking) when this finishes. "
        "By default step 1 launches the picker for you; pass this to just write the "
        "files and stop, e.g. if you want to listen to audio.mp3 before picking clips.",
    )
    args = parser.parse_args()

    project_dir = PROJECTS_DIR / args.project
    script_path = project_dir / "script.txt"
    if not script_path.exists():
        sys.exit(
            f"No script found at {script_path}\n"
            f"Create the folder and put your script in script.txt first."
        )

    lang = Path(args.project).parts[0] if Path(args.project).parts else None
    voice = args.voice or DEFAULT_VOICES.get(lang)
    if not voice:
        sys.exit(
            f"No default voice known for language folder '{lang}'. Pass one explicitly, e.g. "
            f"--voice en-US-AndrewNeural (run 'python -m edge_tts --list-voices' to browse)."
        )

    # Scene boundaries are drawn randomly, so a re-run renumbers and re-times
    # every scene -- but selections.json is keyed by scene number, so already
    # picked clips would silently end up under the wrong narration.
    selections_path = project_dir / "selections.json"
    if selections_path.exists() and not args.force and not args.captions_only:
        try:
            picked = len(json.loads(selections_path.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            picked = 0
        if picked:
            sys.exit(
                f"{picked} scene(s) in this project already have a Pexels clip picked.\n"
                f"Re-running step 1 redraws the scene breakdown, which would leave those clips\n"
                f"attached to the wrong lines of narration.\n\n"
                f"  - Just changing the voice or rate? Re-run with --force, then re-pick clips.\n"
                f"  - Want to keep your picks? Don't re-run step 1.\n"
                f"  - Starting over? Delete {selections_path.name} and the clips/ folder first."
            )

    text = script_path.read_text(encoding="utf-8").strip()
    if not text:
        sys.exit(f"{script_path} is empty.")
    # Last line of defense before TTS: drop any "Narrator:" / "[Voice-over]:"
    # speaker labels sitting at the start of a line, so edge-tts doesn't read
    # them aloud. Done here (not only in step 0) so a hand-pasted script.txt
    # or one built before this check is cleaned too. Applied to `text` itself
    # so synthesis, sentence alignment and captions all see the same words.
    cleaned = strip_speaker_labels(text)
    if cleaned != text:
        removed = len(text.splitlines()) - len(cleaned.splitlines())
        print(f"  stripped speaker label(s) from script.txt before synthesis"
              + (f" ({removed} label line(s) removed)" if removed > 0 else ""))
        text = cleaned

    timings_path = project_dir / "word_timings.json"
    audio_path = project_dir / "audio.mp3"
    if args.captions_only:
        if not timings_path.exists():
            sys.exit(f"--captions-only needs {timings_path.name}, which doesn't exist yet. Run step 1 normally first.")
        words = load_json(timings_path)
        print(f"Reusing existing timings ({len(words)} words) -- audio and scenes untouched.")
    else:
        print(f"Synthesizing with voice '{voice}' (rate {args.rate})...")
        words = asyncio.run(synthesize(text, voice, args.rate, audio_path))
        if not words:
            sys.exit("No word timing data returned by edge-tts -- check your internet connection and voice name.")
        print(f"  audio.mp3 written ({words[-1]['end']:.1f}s), {len(words)} words timed")

    # Computed once here (rather than after any pause splicing below) since
    # they only depend on word identity/order/count, which pause insertion
    # never changes -- only timestamps shift. insert_narration_pauses() /
    # insert_sentence_pauses() need sentence_end_flags to snap a cut to the
    # end of a sentence rather than an arbitrary mid-sentence point;
    # group_scenes() needs sentence_punct to restore real punctuation into
    # scene text (edge-tts's WordBoundary events strip it).
    sentence_end_flags = compute_sentence_end_flags(words, text)
    sentence_punct = compute_sentence_end_punctuation(words, text)

    if not args.captions_only:
        rng = random.Random(args.seed)
        timeline_meta = None
        if args.sentence_scenes:
            # Strict one-scene-per-sentence: the script's own sentence
            # boundaries are the scene boundaries, 1:1, no LLM, no merging or
            # shot-splitting -- so separately-made visuals line up line for line.
            scenes, timeline_meta = build_scenes(
                words, text, lang, llm_keys=None, one_per_sentence=True,
            )
            print(
                f"  one scene per sentence -- {timeline_meta['sentences']} sentence(s) "
                f"-> {len(scenes)} scene(s), 1:1 (no LLM, no merging)"
            )
        elif args.no_semantic:
            scenes = group_scenes(
                words, min_duration=args.scene_seconds_min, max_duration=args.scene_seconds_max, rng=rng,
                long_every=args.long_scene_every, long_seconds=args.long_scene_seconds,
                sentence_punctuation=sentence_punct,
            )
        else:
            llm_keys = None
            try:
                from llm_client import load_keys
                llm_keys = load_keys()
            except Exception:
                llm_keys = None
            if llm_keys:
                print("  analyzing script semantics with Gemini (one scene per visual idea)...")
            else:
                print(
                    "  no Gemini key found -- semantic scenes fall back to exact sentence "
                    "grouping (add a key to tools/gemini_key.txt for full semantic analysis)"
                )
            scenes, timeline_meta = build_scenes(
                words, text, lang, llm_keys,
                min_scene_seconds=args.semantic_min_seconds,
                max_scene_seconds=args.semantic_max_seconds,
            )
        if args.pause_every:
            before_end = words[-1]["end"]
            words, scenes, pause_windows = insert_narration_pauses(
                words, scenes, audio_path,
                every=args.pause_every,
                min_duration=args.pause_seconds_min,
                max_duration=args.pause_seconds_max,
                sentence_end_flags=sentence_end_flags,
                rng=rng,
            )
            print(
                f"  spliced {len(pause_windows)} silent pause(s) ({args.pause_seconds_min:.0f}-"
                f"{args.pause_seconds_max:.0f}s each, every {args.pause_every} scenes) into "
                f"audio.mp3 -- now {words[-1]['end']:.1f}s (was {before_end:.1f}s)"
            )
        if args.pause_after_words:
            before_end = words[-1]["end"]
            words, scenes, pause_windows = insert_sentence_pauses(
                words, scenes, audio_path,
                min_words=args.pause_after_words,
                min_duration=args.pause_seconds_min,
                max_duration=args.pause_seconds_max,
                sentence_end_flags=sentence_end_flags,
                rng=rng,
            )
            print(
                f"  spliced {len(pause_windows)} silent pause(s) ({args.pause_seconds_min:.0f}-"
                f"{args.pause_seconds_max:.0f}s each, after every sentence landing "
                f"{args.pause_after_words}+ words past the previous pause) into audio.mp3 -- "
                f"now {words[-1]['end']:.1f}s (was {before_end:.1f}s)"
            )
        # timeline_meta is set for both the semantic pipeline and the strict
        # --sentence-scenes 1:1 mapping (both go through build_scenes and are
        # word-index exact); it's None only for the legacy group_scenes path.
        if timeline_meta is None:
            for i, s in enumerate(scenes, start=1):
                s["index"] = i
                s["keywords"] = extract_keywords(s["text"], lang=lang)
                s["query"] = build_query(s["text"], lang=lang)
        else:
            # Pause splicing above shifted word timestamps; scene/shot spans
            # are word-INDEX based, so re-deriving times from the words keeps
            # the whole timeline exact no matter what moved.
            refresh_times(scenes, words)
        save_json(scenes, project_dir / "scenes.json")
        if timeline_meta is not None:
            write_timeline(project_dir, words, scenes, timeline_meta)
            n_multi = timeline_meta["multi_shot_scenes"]
            kind = "one-per-sentence" if timeline_meta["mode"] == "sentence" else "semantic"
            print(
                f"  scenes.json written ({len(scenes)} {kind} scenes from "
                f"{timeline_meta['sentences']} sentences, mode: {timeline_meta['mode']}"
                + (f", {n_multi} multi-shot" if n_multi else "")
                + f" -- that's {len(scenes)} clip picks in step 2)"
            )
            print("  timeline.json written (word -> timestamp -> scene master map)")
            for f in timeline_meta.get("llm_failures") or []:
                print(f"  [warn] LLM batch fell back to sentence grouping: {f}")
        elif args.long_scene_every:
            n_long = sum(1 for i in range(args.long_scene_every, len(scenes) + 1, args.long_scene_every))
            print(
                f"  scenes.json written ({len(scenes)} scenes, random "
                f"{args.scene_seconds_min:.0f}-{args.scene_seconds_max:.0f}s each, except every "
                f"{args.long_scene_every} scenes ({n_long} of them) which runs "
                f"{args.long_scene_seconds:.0f}s -- that's {len(scenes)} Pexels clips to pick in step 2)"
            )
        else:
            print(
                f"  scenes.json written ({len(scenes)} scenes, random "
                f"{args.scene_seconds_min:.0f}-{args.scene_seconds_max:.0f}s each -- "
                f"that's {len(scenes)} Pexels clips to pick in step 2)"
            )

    save_json(words, timings_path)
    # Any long gap in the narration -- an inserted --pause-every silence, or
    # any other multi-second pause -- so a caption card's on-screen hold
    # can't freeze through it. Derived straight from word_timings.json, so
    # this works identically whether scenes were just built or (--captions-
    # only) this is rewriting captions for pauses spliced in an earlier run.
    pause_starts = long_gap_starts(words)

    width, height = (1080, 1920) if args.vertical else (1920, 1080)
    width_ratio = width / 1920

    style = caption_style(lang)
    if args.font_size or args.vertical:
        # Copy first -- caption_style() hands back the shared module-level dict.
        # Every capsule dimension is already a ratio of font_size, so the
        # highlight geometry follows the new size on its own. Only max_chars is
        # an absolute character count, so scale it inversely to font size (to
        # keep the rendered line roughly the same pixel width as the calibrated
        # default, e.g. English 42 chars at 64 -> 30 chars at 88) and directly
        # to frame width (a 1080-wide vertical frame fits proportionally fewer
        # characters than the 1920-wide landscape default it was calibrated on).
        style = dict(style)
        scale = (args.font_size / style["font_size"]) if args.font_size else 1.0
        style["max_chars"] = max(12, round(style["max_chars"] * width_ratio / scale))
        if args.font_size:
            style["font_size"] = args.font_size
        print(
            f"  caption font size {style['font_size']}"
            + (f", frame {width}x{height}" if args.vertical else "")
            + f" (line length {style['max_chars']} chars)"
        )
    # wordcolor/wordcolor-bottom are a punchy few-words-at-a-time look -- a
    # full-sentence line at their large centered font size would run off the
    # sides of the frame. Only apply the default when the user hasn't set
    # their own cap.
    max_words = args.max_words
    if max_words is None and args.caption_style in ("wordcolor", "wordcolor-bottom"):
        max_words = 3
    captions = group_captions(
        words, sentence_end_flags, max_chars=style["max_chars"], max_words=max_words
    )
    if args.caption_style == "highlight":
        write_karaoke_ass(
            captions, project_dir / "captions.ass", style=style, pause_starts=pause_starts,
            width=width, height=height,
        )
    elif args.caption_style == "std-yellow-black":
        # Locked preset: yellow line throughout, current word gets a black
        # capsule behind it (pill_color) with its text kept yellow
        # (active_text_color) so the word stays readable against the black
        # box -- the inverse of 'highlight', which is a yellow capsule on a
        # white line.
        write_karaoke_ass(
            captions, project_dir / "captions.ass", style=style, pause_starts=pause_starts,
            width=width, height=height,
            text_color="#FFFF00", pill_color="&H000000&", active_text_color="&H00FFFF&",
        )
    elif args.caption_style == "classic":
        write_ass(
            captions, project_dir / "captions.ass", style=style, pause_starts=pause_starts,
            width=width, height=height,
        )
    else:  # "wordcolor" / "wordcolor-bottom" -- big bold centered headline look
        # wordcolor-bottom is the bottom-anchored preset -- its colours
        # are locked to match the reference caption image (white words, bright
        # cyan #00E6FF active word, black outline) rather than the generic
        # wordcolor defaults, but --text-color/--highlight-color/--outline-color
        # still win if the user explicitly passes them.
        if args.caption_style == "wordcolor-bottom":
            default_text, default_highlight, default_outline = "#FFFFFF", "#00E6FF", "#000000"
            # Bottom-anchored, same as 'highlight' and 'classic' (alignment=2,
            # margin_v=90 is their shared default via ass_header) -- this is
            # the position viewers are used to seeing captions sit at, not
            # the vertically-centered 'wordcolor' headline placement.
            alignment, bottom_margin = 2, 90
        else:
            default_text, default_highlight, default_outline = "#FFFFFF", "#29D6F5", "#000000"
            alignment, bottom_margin = 5, 40
        write_wordcolor_ass(
            captions,
            project_dir / "captions.ass",
            style=style,
            text_color=args.text_color or default_text,
            highlight_color=args.highlight_color or default_highlight,
            outline_color=args.outline_color or default_outline,
            outline_width=6,
            font_name="Arial Black",
            font_size=args.font_size or 100,
            alignment=alignment,
            bottom_margin=bottom_margin,
            pause_starts=pause_starts,
            width=width,
            height=height,
        )
    words_note = f", max {max_words} words/card" if max_words else ""
    print(f"  captions.ass written ({len(captions)} caption cards, {args.caption_style} style{words_note})")

    # Step 3 (and step 2's clip picker) read this back so a project rendered
    # --vertical stays vertical automatically -- captions.ass above is already
    # laid out for this exact frame size, so the render step must match it or
    # every caption position comes out stretched to the wrong aspect ratio.
    save_json({"width": width, "height": height}, project_dir / "render.json")

    if args.image_prompts and not args.captions_only:
        images_dir = project_dir / "images"
        images_dir.mkdir(exist_ok=True)
        prompts_path = project_dir / "image_prompts.txt"
        with open(prompts_path, "w", encoding="utf-8") as f:
            f.write(
                "Google Flow image prompts -- one per scene.\n"
                "Generate each in Flow, download, and save into images/ using the exact filename\n"
                "given for each scene below -- the timestamp in the name is just so you can tell\n"
                "at a glance where in the video it belongs; the numeric prefix is what actually\n"
                "controls playback order.\n"
                "These are starting-point prompts (simple template) -- feel free to hand-edit them,\n"
                "or ask Claude directly to rewrite them with more specific, varied imagery per scene.\n\n"
            )
            for s in scenes:
                filename = scene_image_filename(s["index"], s["start"])
                f.write(
                    f"=== Scene {s['index']:03d} "
                    f"({format_timestamp(s['start'])} - {format_timestamp(s['end'])}, {s['duration']:.0f}s) ===\n"
                )
                f.write(f"Save as: images/{filename}\n")
                f.write(make_image_prompt(s["text"]) + "\n\n")
        print(f"  image_prompts.txt written -- {len(scenes)} prompts, save Flow images into {images_dir}")

    if args.captions_only:
        print("\nNext: re-render with your existing clips:")
        print(f"    python step3_render_video.py {args.project}")
        return

    if args.no_chain:
        print("\nNext: review audio.mp3, then pick a stock clip for each scene:")
        print(f"    python step2_pick_clips.py {args.project}")
        return

    print("\nStep 1 done. Continuing straight into step 2 (pick a clip per scene)...")
    print("(pass --no-chain next time to stop here instead, e.g. to listen to audio.mp3 first)")
    import step2_pick_clips
    render_now = step2_pick_clips.run(args.project)
    if render_now:
        import step3_render_video
        step3_render_video.run(args.project)


if __name__ == "__main__":
    main()
