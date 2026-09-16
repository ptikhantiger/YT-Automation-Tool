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

Projects live in `projects/<lang>/<slug>/`. Only their small metadata files
(script, audio, scenes, picks, captions — a few MB) are tracked; clips, caches,
and the rendered video stay on your disk.

## Render in the cloud (free)

The only CPU-heavy step is step 3 (ffmpeg encoding). Steps 0–2 are just API
calls and a browser page, so do those on your laptop and let GitHub Actions do
the encode — free, unlimited minutes on a public repo, no API keys needed:

1. Write the script, run step 1, pick clips in step 2 as usual. When the
   picker asks to render, click **Not yet**.
2. Push the project:
   ```bash
   git add projects/en/myvideo
   git commit -m "myvideo: picks done"
   git push
   ```
3. On GitHub: **Actions → Render video → Run workflow**, type `en/myvideo`.
4. 20–40 minutes later, download `output.mp4` from the run's **Artifacts** box
   (kept 30 days).

The workflow is [`.github/workflows/render.yml`](.github/workflows/render.yml).
It runs `step3_render_video.py --non-interactive --no-4k`; quality (`crf`),
`preset`, and 4K are inputs on the Run workflow form. If a scene has no clip
or a clip is unplayable, the run fails fast and names the scenes instead of
opening the picker — fix them locally, push, re-run.

Local uploads and AI illustrations (`clips/*_local*`, `clips/*_ai*`) are
committed with the project because only your disk has them; stock picks are
re-downloaded by the runner.

## API keys

Create these files in `tools/` (each holds just the key, nothing else). They are
gitignored and never sent to the browser.

| File | Service | Required | Free key |
|---|---|---|---|
| `pexels_key.txt` | Pexels stock video/photo | **yes** | https://www.pexels.com/api/ |
| `gemini_key.txt` | Gemini LLM (step 0, semantic scenes, auto-match) | recommended | https://aistudio.google.com/apikey |
| `pixabay_key.txt` | Pixabay source tab | optional | https://pixabay.com/api/docs/ |
| `coverr_key.txt` | Coverr source tab | optional | https://coverr.co/developers |

`gemini_key.txt` accepts multiple keys, one per line — the client rotates to the
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
