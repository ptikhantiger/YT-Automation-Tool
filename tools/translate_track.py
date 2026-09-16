"""
Translate an existing project's script into another spoken-language audio
track: script.txt -> tracks/<lang>/audio.mp3 + tracks/<lang>/captions.srt

Usage:
    python translate_track.py <lang>/<slug> --lang <target-code>
        [--voice VOICE] [--rate +0%] [--retranslate] [--model MODEL]

e.g. python translate_track.py en/norway --lang es
     python translate_track.py en/norway --lang fr --voice fr-FR-HenriNeural
     python translate_track.py en/norway --lang hi --rate -10%

Built for YouTube's multi-audio-track / multi-caption-track upload feature:
the video itself doesn't change, only the narration and captions do. Each
run creates projects/<lang>/<slug>/tracks/<target-code>/ (made automatically
if it doesn't exist yet) holding exactly the two files YouTube needs:

    audio.mp3      -- narration re-voiced by edge-tts in the target language
    captions.srt    -- YouTube-uploadable caption track, timed to that audio

Re-run any time with the same --lang to regenerate -- e.g. to try a
different --voice or --rate without spending a fresh Gemini translation, since
the translation itself is cached in tracks/.cache/<lang>.txt. Pass
--retranslate to force a new translation (e.g. after editing script.txt).
"""
import argparse
import asyncio
import re
import sys
from pathlib import Path

from common import caption_style, compute_sentence_end_flags, group_captions, write_srt
from llm_client import DEFAULT_MODEL, LLMError, complete, load_keys
from step1_audio_and_captions import DEFAULT_VOICES, synthesize

PROJECTS_DIR = Path(__file__).parent.parent / "projects"

# Extends step1's DEFAULT_VOICES (en/es/ur) with more edge-tts languages.
# Override any of these any time with --voice; run
# `python -m edge_tts --list-voices` to browse the full catalogue.
TRACK_VOICES = {
    **DEFAULT_VOICES,
    "fr": "fr-FR-HenriNeural",
    "de": "de-DE-ConradNeural",
    "it": "it-IT-DiegoNeural",
    "pt": "pt-BR-AntonioNeural",
    "ar": "ar-SA-HamedNeural",
    "hi": "hi-IN-MadhurNeural",
    "zh": "zh-CN-YunyangNeural",
    "ja": "ja-JP-KeitaNeural",
    "ko": "ko-KR-InJoonNeural",
    "ru": "ru-RU-DmitryNeural",
    "tr": "tr-TR-AhmetNeural",
    "id": "id-ID-ArdiNeural",
    "vi": "vi-VN-NamMinhNeural",
    "bn": "bn-IN-BashkarNeural",
    "fa": "fa-IR-FaridNeural",
    "pl": "pl-PL-MarekNeural",
    "nl": "nl-NL-MaartenNeural",
    "sw": "sw-KE-RafikiNeural",
    "th": "th-TH-NiwatNeural",
    "ta": "ta-IN-ValluvarNeural",
    "te": "te-IN-MohanNeural",
}

# English names, used only to tell Gemini what language to translate into. An
# unlisted --lang code still works (falls back to the code itself in the
# prompt), just less fluently phrased.
LANGUAGE_NAMES = {
    "en": "English", "es": "Spanish", "ur": "Urdu", "fr": "French",
    "de": "German", "it": "Italian", "pt": "Portuguese", "ar": "Arabic",
    "hi": "Hindi", "zh": "Chinese (Mandarin)", "ja": "Japanese",
    "ko": "Korean", "ru": "Russian", "tr": "Turkish", "id": "Indonesian",
    "vi": "Vietnamese", "bn": "Bengali", "fa": "Persian", "pl": "Polish",
    "nl": "Dutch", "sw": "Swahili", "th": "Thai", "ta": "Tamil",
    "te": "Telugu",
}


def word_count(text):
    return len(text.split())


# Models like to open a translation with "Here is the translation:" or wrap
# the whole thing in quotes -- both would otherwise get read aloud.
_PREAMBLE = re.compile(
    r"^\s*(?:here(?:'s| is| are)[^.\n]*[:.]|sure[,!][^.\n]*[:.]|"
    r"translation\s*[:\-][^\n]*)\s*",
    re.I,
)
_MD_PATTERNS = [
    (re.compile(r"^\s{0,3}#{1,6}\s+.*$", re.M), ""),          # headings
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),              # bold
    (re.compile(r"^\s*[-*+•]\s+", re.M), ""),            # bullets
]


def clean_translation(text):
    text = _PREAMBLE.sub("", text)
    for pat, repl in _MD_PATTERNS:
        text = pat.sub(repl, text)
    text = text.strip()
    if len(text) > 2 and text[0] in "\"'" and text[-1] in "\"'":
        text = text[1:-1].strip()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def translate_system(language_name):
    return (
        f"You are a professional translator preparing a narration script for text-to-speech "
        f"voice-over. Translate the given news narration into natural, spoken {language_name} -- "
        f"the way a native {language_name} narrator would actually say it out loud, not a stiff "
        f"word-for-word translation.\n\n"
        f"Rules:\n"
        f"- Keep every fact, name, date and number exactly as given -- translate names the way "
        f"{language_name} speakers commonly render them, but never drop, add, or change a fact.\n"
        f"- Numbers, dates and figures: write them the way a {language_name} narrator would "
        f"naturally read them aloud.\n"
        f"- Keep quoted speech as a faithful translation of what was said, still marked as a quote.\n"
        f"- Keep the same paragraph breaks as the source, one-for-one -- do not merge or split "
        f"paragraphs.\n"
        f"- Natural spoken {language_name}, not written/formal register. Use contractions and "
        f"everyday phrasing wherever {language_name} normally would.\n"
        f"- Return ONLY the translated narration. No preamble, no notes, no markdown, no quotation "
        f"marks around the whole thing."
    )


def translate(text, language_name, key, model, verbose=True):
    """Paragraph-batched translation -- a single request over a whole script
    risks the model summarising instead of translating in full, the same
    failure mode step0's polish() batches around."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    system = translate_system(language_name)
    out = []
    for i, para in enumerate(paras, 1):
        if verbose:
            print(f"  translating paragraph {i}/{len(paras)} ({word_count(para)} words)...")
        translated = clean_translation(
            complete(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": para},
                ],
                key,
                model=model,
                temperature=0.5,
                max_tokens=max(1500, word_count(para) * 4),
                verbose=verbose,
            )
        )
        out.append(translated)
    return "\n\n".join(out)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("project", help="Path under projects/, e.g. en/norway")
    parser.add_argument(
        "--lang", required=True,
        help="Target language code for this track, e.g. es, fr, hi -- used as both the "
        "tracks/ subfolder name and (unless --voice is given) to pick a default edge-tts "
        "voice and tell Gemini what to translate into.",
    )
    parser.add_argument("--voice", default=None, help="edge-tts voice name, overriding the --lang default")
    parser.add_argument("--rate", default="+0%", help="edge-tts speech rate adjustment, e.g. -10%%")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Gemini model for translation")
    parser.add_argument("--api-key", default=None, help="Gemini API key, overriding gemini_key.txt/GEMINI_API_KEY")
    parser.add_argument(
        "--retranslate", action="store_true",
        help="Ignore any cached translation for --lang and translate script.txt again with "
        "Gemini (use after editing script.txt).",
    )
    parser.add_argument(
        "--max-chars", type=int, default=None,
        help="Caption line length cap, in characters. Default depends on --lang (see "
        "caption_style() in common.py).",
    )
    args = parser.parse_args()

    project_dir = PROJECTS_DIR / args.project
    script_path = project_dir / "script.txt"
    if not script_path.exists():
        sys.exit(f"No script found at {script_path}")

    voice = args.voice or TRACK_VOICES.get(args.lang)
    if not voice:
        sys.exit(
            f"No default voice known for --lang '{args.lang}'. Pass one explicitly, e.g. "
            f"--voice fr-FR-HenriNeural (run 'python -m edge_tts --list-voices' to browse)."
        )

    tracks_dir = project_dir / "tracks" / args.lang
    tracks_dir.mkdir(parents=True, exist_ok=True)
    cache_path = project_dir / "tracks" / ".cache" / f"{args.lang}.txt"

    if cache_path.exists() and not args.retranslate:
        translated = cache_path.read_text(encoding="utf-8").strip()
        print(
            f"Reusing cached {args.lang} translation ({word_count(translated)} words) -- "
            f"pass --retranslate to redo it."
        )
    else:
        language_name = LANGUAGE_NAMES.get(args.lang, args.lang)
        try:
            key = load_keys(args.api_key)
        except LLMError as e:
            sys.exit(str(e))
        text = script_path.read_text(encoding="utf-8").strip()
        if not text:
            sys.exit(f"{script_path} is empty.")
        print(
            f"Translating {script_path.name} ({word_count(text)} words) into "
            f"{language_name} with {args.model}..."
        )
        translated = translate(text, language_name, key, args.model)
        if word_count(translated) < 5:
            sys.exit("Translation came back empty or near-empty -- Gemini likely refused or errored; try again.")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(translated + "\n", encoding="utf-8")
        print(f"  translated to {word_count(translated)} words")

    audio_path = tracks_dir / "audio.mp3"
    print(f"Synthesizing with voice '{voice}' (rate {args.rate})...")
    words = asyncio.run(synthesize(translated, voice, args.rate, audio_path))
    if not words:
        sys.exit("No word timing data returned by edge-tts -- check your internet connection and voice name.")
    print(f"  {audio_path} written ({words[-1]['end']:.1f}s), {len(words)} words timed")

    sentence_end_flags = compute_sentence_end_flags(words, translated)
    max_chars = args.max_chars or caption_style(args.lang)["max_chars"]
    captions = group_captions(words, sentence_end_flags, max_chars=max_chars)
    captions_path = tracks_dir / "captions.srt"
    write_srt(captions, captions_path)
    print(f"  {captions_path} written ({len(captions)} cues)")

    print(
        f"\nDone -- {tracks_dir} now has audio.mp3 and captions.srt for '{args.lang}'.\n"
        f"On the YouTube video built from this same project: Subtitles > Add language "
        f"> upload captions.srt, and Audio track > Add language > upload audio.mp3."
    )


if __name__ == "__main__":
    main()
