# Setup on a Fresh Laptop

Everything needed to get this project running from nothing. Written for
**Windows** — see [Mac / Linux](#mac--linux) at the bottom, which needs a code
change before it will run.

Budget about 20 minutes, most of it downloads.

---

## Checklist

| # | What | Required? |
|---|---|---|
| 1 | Python 3.9 or newer | **yes** |
| 2 | ffmpeg + ffprobe (with libass, libx264, aac) | **yes** |
| 3 | The project files | **yes** |
| 4 | Python packages (`edge-tts`, `Pillow`) | **yes** |
| 5 | Pexels API key | **yes** |
| 6 | Gemini API key | strongly recommended |
| 7 | Pixabay / Coverr keys, `yt-dlp` | optional |

You also need an internet connection every time you run this — narration is
synthesized by Microsoft's servers and every clip is searched and downloaded
live.

---

## 1. Python

Download from <https://www.python.org/downloads/> and run the installer.

> **Tick "Add python.exe to PATH" on the first screen.** It is off by default.
> If you miss it, `run.bat` cannot find Python and you will have to reinstall
> or add it to PATH by hand.

Verify in a **new** terminal:

```
python --version
```

Anything `3.9.0` or higher is fine. The code uses no 3.10+ syntax.

---

## 2. ffmpeg

This does the actual video rendering and burns the captions in. The build must
include **libass** (captions), **libx264** (the only video encoder the project
uses), and **aac** (audio). Any standard Windows build has all three.

```
winget install Gyan.FFmpeg
```

> **Close the terminal and open a new one afterwards.** PATH changes do not
> apply to terminals that are already open — this is the single most common
> "I installed it but it says not found" cause.

Verify:

```
ffmpeg -version
ffprobe -version
```

No winget? Download from <https://www.gyan.dev/ffmpeg/builds/> (get
`ffmpeg-release-essentials.zip`), unzip to e.g. `C:\ffmpeg`, then add
`C:\ffmpeg\bin` to your PATH via *Settings → System → About → Advanced system
settings → Environment Variables*.

---

## 3. Get the project

```
git clone https://github.com/ptikhantiger/YouTube-Automation-Tool.git
cd YouTube-Automation-Tool
```

No git? Download the ZIP from the repo page (*Code → Download ZIP*) and
extract it.

---

## 4. Python packages

The launcher offers to do this for you, but you can run it directly:

```
pip install -r tools/requirements.txt
```

That installs `edge-tts` (narration) and `Pillow` (measures text width so the
caption highlight lands on the right word). It also installs
`youtube-transcript-api` and `yt-dlp`, which are only needed for step 0's
`--yt-link` and the background music picker — steps 1–3 work fine without them.

---

## 5. API keys

**The keys are deliberately not in the repository** (they are gitignored), so a
fresh clone has none. Create each file inside the `tools/` folder containing
**just the key and nothing else** — no quotes, no `KEY=` prefix, no blank
lines.

### Pexels — required

Without this you cannot pick clips at all.

1. Go to <https://www.pexels.com/api/> and sign up (free).
2. Copy your key.
3. Save it as `tools/pexels_key.txt`.

Free quota is 25,000 requests/month, which is far more than you will use — one
search per scene, plus re-searches. Every search is cached on disk, so
revisiting a scene costs nothing.

### Gemini — strongly recommended

Powers the semantic scene splitting, the ✨ auto-match button, and all of
step 0. Everything still runs without it, but scene splitting falls back to
simpler sentence grouping and you lose auto-match entirely.

1. Go to <https://aistudio.google.com/apikey> and sign up (free).
2. Save the key as `tools/gemini_key.txt`.

> **This file accepts multiple keys, one per line.** When one hits its quota
> the client automatically rolls over to the next. The free tier gives
> `gemini-flash-lite-latest` about 1,000 requests/day and 250,000 tokens/minute
> — plenty for normal use — so a second key (a separate Google project) is
> only needed if two people share this tool or you do many builds a day.

### Pixabay and Coverr — optional

Extra source tabs in the clip picker. Without them, those tabs show a short
setup message instead of results. Pexels alone is enough to make videos.

- Pixabay: <https://pixabay.com/api/docs/> → `tools/pixabay_key.txt`
- Coverr: <https://coverr.co/developers> → `tools/coverr_key.txt`

For Coverr, save only the **API Key** value — not the App name or ID.

---

## 6. First run

Double-click **`run.bat`**.

It checks everything above before starting anything and prints a report:

```
================================================================
  YouTube Automation Tool
================================================================

Checking your setup:

  [ OK ] Python 3.14.5
  [ OK ] ffmpeg
  [ OK ] ffprobe
  [ OK ] Python packages (edge-tts, Pillow)
  [ OK ] Pexels (stock clips)
  [ OK ] Gemini (script + auto-match)

----------------------------------------------------------------
  Dashboard:  http://127.0.0.1:8765/
  Stop:       press Ctrl+C in this window
----------------------------------------------------------------
```

Your browser opens automatically. Any `[FAIL]` line tells you exactly what to
install. If port 8765 is busy the launcher moves to the next free port on its
own.

Not on Windows, or prefer the terminal:

```
python launch.py
```

Useful flags: `--port 9000`, `--no-browser`, `--skip-checks`, `--yes`.

---

## 7. Confirm it actually works

Do not trust the checkmarks — make a real 10-second video.

```
mkdir projects\en\test
```

Put one or two sentences in `projects\en\test\script.txt`, then:

```
cd tools
python step1_audio_and_captions.py en/test
```

That writes `audio.mp3`, `scenes.json` and `captions.ass`, then opens the clip
picker in your browser. Pick a clip for each scene (or click **✨ Auto-match
all unpicked** if you added a Gemini key). When the last scene is filled it asks
whether to render — click OK.

You should end up with `projects/en/test/output.mp4`: 1920x1080, H.264 + AAC,
captions burned in.

If that works, everything works.

---

## Disk space

Roughly **100 MB per video** — downloaded clips are the bulk of it, not the
final file. A real example from this project: a 24-second video used 97 MB
(70 MB of source clips, 26 MB of rendered output).

`projects/` is gitignored, so none of this is ever committed. Delete old
project folders freely; they are just generated output.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `'python' is not recognized` | PATH box was not ticked during install. Reinstall and tick it, or add Python to PATH manually. |
| `ffmpeg not found` but you installed it | You are in a terminal opened *before* the install. Close it, open a new one. |
| `run.bat` window flashes and vanishes | It should hold open on error. If not, open a terminal and run `python launch.py` to see the message. |
| Captions missing from the output video | Your ffmpeg build lacks libass. Check with `ffmpeg -filters` and look for `ass`. Reinstall a full build. |
| `OSError: cannot open resource` during step 1 | A required font is missing. On Windows this should not happen; on Mac/Linux see below. |
| Clip thumbnails are black | Normal. Cards use `preload="metadata"` and only load on hover, to save bandwidth. Give them a few seconds. |
| `403` with body `error code: 1010` from Pexels | Cloudflare is rejecting the request's User-Agent. The `USER_AGENT` constant in `common.py` is what gets requests through — do not remove it. A genuinely bad key returns `401`, not `403`. |
| Gemini stops partway with a rate-limit message | The per-minute or per-day free-tier quota is spent (per-day resets at midnight Pacific). Re-run the same command later — step 0 caches finished sections and resumes. Adding a second key to `tools/gemini_key.txt` doubles the budget. |
| Gemini says the model is "no longer available to new users" | Google retired that version. The default `gemini-flash-lite-latest` is an alias that avoids this; if you pinned `--model`, switch to `gemini-3.5-flash` or drop the flag. |
| `--yt-link` says "sign in to confirm you're not a bot" | YouTube is blocking your IP. Export your browser cookies with a "Get cookies.txt LOCALLY" extension and save as `tools/cookies.txt`. It is gitignored. |
| Port 8765 already in use | The launcher falls back automatically. To force one: `run.bat --port 9000`. |
| Picked clips look wrong after re-running step 1 | Re-running step 1 rebuilds the scene list, and `selections.json` is keyed by scene number. Get voice and pacing right *before* picking clips. To restyle captions afterwards use `--captions-only`, which is safe. |

---

## Mac / Linux

**This will not run as-is.** Two font paths are hardcoded to Windows in
`tools/common.py`:

```python
DEFAULT_FONT_PATH = "C:/Windows/Fonts/arialbd.ttf"        # line 640
"font_path": "C:/Windows/Fonts/Jameel Noori Nastaleeq.ttf" # line 693
```

Pillow's `ImageFont.truetype()` is called with no error handling, so a missing
font raises `OSError` and step 1 dies while generating captions.

To port it, point `DEFAULT_FONT_PATH` at a real bold font and make sure libass
can resolve the family name in `CAPTION_STYLES`:

- **macOS:** `/System/Library/Fonts/Supplemental/Arial Bold.ttf`
- **Linux:** install `fonts-liberation`, then
  `/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf`

Everything else (Python, ffmpeg, the servers) is already cross-platform.

---

## Security notes

- **Never commit the key files.** They are in `.gitignore` — keep it that way.
  A key committed once stays in git history even after you delete the file.
- **The servers bind to `127.0.0.1` only and have no authentication.** That is
  deliberate; the code assumes only your own browser can reach it. Do not
  expose these ports to the internet or your local network without putting a
  login in front of them.
- If you ever paste a key into a chat, email, or screenshot, **rotate it** —
  all four providers let you revoke and reissue for free.
