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

1. Write the script, run step 1, pick clips in step 2 as usual.
2. When every scene has a clip, click **☁ Render on GitHub (free)** in the
   picker. It commits the project's picks as `render: en/myvideo`, pushes, and
   shows you the link to the Actions run. (By hand, the same thing is
   `git add projects/en/myvideo && git commit -m "render: en/myvideo" && git push`
   — that commit message is the trigger.)
3. 20–40 minutes later (45–90 for a 30-minute video), download `output.mp4`
   from the run's **Artifacts** box (kept 30 days).

The workflow is [`.github/workflows/render.yml`](.github/workflows/render.yml).
It runs `step3_render_video.py --non-interactive --no-4k`. To change quality
(`crf`), `preset`, or allow 4K, start it from **Actions → Render video → Run
workflow** instead, which has those as form fields. If a scene has no clip or
a clip is unplayable, the run fails fast and names the scenes instead of
opening the picker — fix them in the picker, click the button again.

Local uploads and AI illustrations (`clips/*_local*`, `clips/*_ai*`) are
committed with the project because only your disk has them; stock picks are
re-downloaded by the runner.

## Use it from any browser (GitHub Codespaces)

Don't want to run *anything* on your laptop? Open the repo in a Codespace:
a Linux machine on GitHub's side that you drive from a browser tab. Free tier
is 60 hours/month on the 2-core size plus 15 GB of storage — more than enough
for a couple of videos a week. Only your GitHub login can reach its ports.

**One-time**

1. Add your keys as Codespaces Secrets at <https://github.com/settings/codespaces>
   → *New secret*, scoped to this repo: `PEXELS_API_KEY` (required),
   `GEMINI_API_KEY` (recommended), optional `PIXABAY_API_KEY`, `COVERR_API_KEY`,
   `POLLINATIONS_TOKEN`. They arrive as environment variables; no key files.
2. On the repo page: **Code → Codespaces → Create codespace on main**. The
   first build installs ffmpeg, fonts, and the Python packages (~2 min).

**Every video**

```bash
python launch.py --no-browser
```

Open the **Dashboard** port from the *Ports* tab (a toast also pops up). The
dashboard works exactly as it does locally; when a run reaches step 2 the
picker appears as its own forwarded port (8000) — the dashboard embeds it,
or open it in its own tab.

When the last scene is picked, the picker offers **☁ Render on GitHub
(free)** instead of the 60-second local countdown (rendering on the 2-core
codespace is slow and spends your hours; the Actions runner is free and
unlimited). One click pushes the picks and starts the render; the page shows
the link to the run. Download `output.mp4` from its Artifacts when it
finishes. "Render here anyway" still works if you insist.

Notes:
- The codespace **stops itself after 30 idle minutes** and keeps its disk;
  reopen it from **Code → Codespaces**. Unused codespaces are deleted after
  30 days — anything you've pushed is safe, unpushed picks are not, so push.
- Codespaces run on Azure IPs, which YouTube sometimes bot-checks: `--yt-link`
  and the music picker may need the `tools/cookies.txt` workaround described
  in [`tools/README_PIPELINE.md`](tools/README_PIPELINE.md).
- Urdu captions need Jameel Noori Nastaleeq — copy the `.ttf` into `~/.fonts/`
  in the codespace (no package ships it).
- Everything in `.devcontainer/` also works on any Linux VM (e.g. an Oracle
  Cloud Always-Free instance) if you ever outgrow the free hours.

## API keys

Create these files in `tools/` (each holds just the key, nothing else). They are
gitignored and never sent to the browser.

Each can also be given as an environment variable instead of a file
(`PEXELS_API_KEY`, `GEMINI_API_KEY`, `PIXABAY_API_KEY`, `COVERR_API_KEY`,
`POLLINATIONS_TOKEN`) — that's how a Codespace gets them.

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
