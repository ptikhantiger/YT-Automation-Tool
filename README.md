# YouTube Automation Tool

A local, end-to-end pipeline that turns a script into a finished 1080p YouTube video:
neural narration, word-synced burned-in captions, and stock footage picked per scene.

Pure Python + ffmpeg. No cloud services, no web framework.

## Pipeline

| Step | Script | Produces |
|---|---|---|
| 0 *(optional)* | `step0_build_script.py` | `script.txt` from 3 news sources, or from a YouTube transcript (`--yt-link`) |
| 1 | `step1_audio_and_captions.py` | `audio.mp3`, word timings, semantic scene split, `captions.ass` |
| 2 | `step2_pick_clips.py` | Browser picker: Pexels / Pixabay / Coverr / local upload, plus LLM auto-match |
| *music* | `step_music_picker.py` | Background tracks pulled from YouTube via yt-dlp |
| 3 | `step3_render_video.py` | `output.mp4` + `credits.txt` |

Steps chain automatically: 1 launches 2, and 2 launches 3 once every scene has a clip.

## Quick start

Setting up on a new machine? See **[SETUP.md](SETUP.md)** for the full
step-by-step (Python, ffmpeg, API keys, troubleshooting).

**Windows: just double-click `run.bat`.** It checks Python, ffmpeg, the
required packages and your API keys, then starts the dashboard and opens
your browser. If a check fails it tells you exactly what to install.

Any platform:

```bash
python launch.py            # same preflight + dashboard
python launch.py --port 9000    # force a port
python launch.py --no-browser   # don't open a tab
```

Or drive it manually:

```bash
pip install -r tools/requirements.txt   # needs ffmpeg + ffprobe on PATH too
cd tools

# the dashboard (every flag documented, live output, Stop button)
python webui/server.py

# or the CLI -- put your narration in projects/en/myvideo/script.txt, then:
python step1_audio_and_captions.py en/myvideo
```

Projects live in `projects/<lang>/<slug>/` and are gitignored — they hold your
generated audio, clips, and rendered video.

## API keys

Create these files in `tools/` (each holds just the key, nothing else). They are
gitignored and never sent to the browser.

| File | Service | Required | Free key |
|---|---|---|---|
| `pexels_key.txt` | Pexels stock video/photo | **yes** | https://www.pexels.com/api/ |
| `groq_key.txt` | Groq LLM (step 0, semantic scenes, auto-match) | recommended | https://console.groq.com/keys |
| `pixabay_key.txt` | Pixabay source tab | optional | https://pixabay.com/api/docs/ |
| `coverr_key.txt` | Coverr source tab | optional | https://coverr.co/developers |

`groq_key.txt` accepts multiple keys, one per line — the client rotates to the
next when one hits its quota.

## Documentation

- **[SETUP.md](SETUP.md)** — fresh-laptop install, API keys, troubleshooting.
- [`tools/README_PIPELINE.md`](tools/README_PIPELINE.md) — full flag reference,
  the news-article workflow, caption styles, and gotchas.
- [`tools/HOW_TO.txt`](tools/HOW_TO.txt) — the short version.

## Notes

- `ffmpeg` must be built with `libass` (for burned-in styled captions).
- Stock footage from Pexels, Pixabay, and Coverr is free for commercial use with
  no attribution required; `credits.txt` is still written for each render.
