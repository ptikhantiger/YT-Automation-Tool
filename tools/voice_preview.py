"""ONE-TIME setup: audition natural male voices from the MS Edge neural voice
library for a given language, so you can pick one to use as the channel's
narrator.

Usage:
    python voice_preview.py [--lang en|es]

Listen to the generated files in tools/voice_samples/<lang>/ and pick a voice
name (e.g. "en-US-AndrewNeural") to pass as --voice to
step1_audio_and_captions.py (or just rely on the default for that language
folder -- see DEFAULT_VOICES in step1_audio_and_captions.py).

Run `python -m edge_tts --list-voices` to see the full list (female voices,
other locales, etc.) if none of these fit.
"""
import argparse
import asyncio
from pathlib import Path

import edge_tts

SAMPLE_TEXT = {
    "es": (
        "A veces, el silencio de una persona dice mucho mas de lo que sus "
        "palabras jamas podrian admitir. Entender esto es el primer paso "
        "hacia una paz verdadera."
    ),
    "en": (
        "Sometimes, a person's silence says far more than their words ever "
        "could admit. Understanding this is the first step toward real "
        "peace."
    ),
    "ur": (
        "کبھی کبھی کسی انسان کی خاموشی اس کے الفاظ سے کہیں زیادہ بولتی ہے۔ "
        "یہ بات سمجھ لینا حقیقی سکون کی طرف پہلا قدم ہے۔"
    ),
}

# A representative spread of natural-sounding male voices per language.
CANDIDATE_VOICES = {
    "es": [
        "es-MX-JorgeNeural",
        "es-US-AlonsoNeural",
        "es-ES-AlvaroNeural",
        "es-CO-GonzaloNeural",
        "es-AR-TomasNeural",
        "es-PE-AlexNeural",
    ],
    "en": [
        "en-US-AndrewNeural",
        "en-US-BrianNeural",
        "en-US-ChristopherNeural",
        "en-US-GuyNeural",
        "en-US-EricNeural",
        "en-GB-RyanNeural",
    ],
    # edge-tts only ships two male Urdu voices -- Pakistani and Indian Urdu.
    # Asad (PK) is the closer match for this channel's audience.
    "ur": [
        "ur-PK-AsadNeural",
        "ur-IN-SalmanNeural",
    ],
}

OUTPUT_DIR = Path(__file__).parent / "voice_samples"


async def synthesize_sample(text, voice, out_path):
    communicate = edge_tts.Communicate(text, voice)
    with open(out_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lang", choices=sorted(CANDIDATE_VOICES), default="es")
    args = parser.parse_args()

    voices = CANDIDATE_VOICES[args.lang]
    text = SAMPLE_TEXT[args.lang]
    out_dir = OUTPUT_DIR / args.lang
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Generating {len(voices)} '{args.lang}' voice samples...\n")
    for voice in voices:
        out_path = out_dir / f"{voice}.mp3"
        await synthesize_sample(text, voice, out_path)
        print(f"  {voice}  ->  {out_path}")
    print(f"\nDone. Listen to the files in {out_dir} and pick your favorite.")
    print(f'Then use it like: python step1_audio_and_captions.py {args.lang}/<slug> --voice "{voices[0]}"')


if __name__ == "__main__":
    asyncio.run(main())
