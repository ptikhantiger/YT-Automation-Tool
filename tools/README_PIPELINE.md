# Video Pipeline

Narration (MS Edge neural voices, any language edge-tts supports), synced
burned-in captions, and a video assembled from stock clips (Pexels, Pixabay,
Coverr, or a local upload) you pick yourself, one per scene. For the news
format it also writes the script, from three source reports of the same
story.

Works for the English channel and its Spanish and Urdu versions — projects
live under `projects/<lang>/<slug>/`, e.g. `projects/en/crumbs`,
`projects/es/test-crumbs` or `projects/ur/test-crumbs`. `<lang>` is a folder
name for your own organization, but it also selects:

- the default narrator voice — `DEFAULT_VOICES` in
  `tools/step1_audio_and_captions.py` (`en`, `es`, `ur` are set up)
- how captions are typeset — `CAPTION_STYLES` in `tools/common.py` (font,
  size, text direction)

Add an entry to both if you branch into another language.

### Urdu specifics

Urdu works end to end, but it needed its own caption setup:

- **Voice:** `ur-PK-AsadNeural` (Pakistani Urdu). `ur-IN-SalmanNeural` is the
  only other male option edge-tts ships.
- **Font:** Jameel Noori Nastaleeq (install it if you use Urdu). Arial
  has no Urdu glyphs at all.
- **Font size 140, not 64.** libass renders this font at roughly 0.59× the
  size the number implies, and Nastaliq needs to be physically larger than
  Latin type to stay legible. Don't "fix" this to match the English value.
- **Captions render right-to-left**, including the yellow highlight capsule.
  This depends entirely on `Encoding: -1` in the .ass style — it is the only
  value that works, and `178` ("Arabic charset") does *not*. If Urdu ever
  starts reading backwards, that's the first thing to check: the letters stay
  perfectly shaped either way, so only the word order gives it away.
- **Stock search queries come from a separate Urdu theme table** (`LANG_THEME_QUERIES`
  in `tools/keywords.py`). Urdu words return nothing on any of the three
  providers (Pexels/Pixabay/Coverr all index English-language tags), so a
  scene matching no theme falls back to the generic query — expect to lean on
  the picker's search box more than you do in English.

Translate the English script into `script.txt` the same way you do for
Spanish, keeping the Master Prompt tone and content rules.

---

## Semantic script → voice → scene synchronization

Scenes are no longer cut at random durations. By default step 1 builds a
**semantic timeline** (`semantic_timeline.py`): the exact script is aligned
1:1 with edge-tts's word-level timestamps, whole sentences are grouped/split
into *visual segments* by the same Groq LLM step 0 uses (deterministic
sentence grouping if no key), and every scene records the exact words
(`word_from`/`word_to`), voice window, subject/action/object/location/time,
visual requirements + exclusions, and concrete search queries it represents.
A sentence with several visual events ("the rocket launched … and climbed …")
becomes one scene with multiple **shots**, each with its own exact word span,
timing, and query.

Artifacts per project:
- `timeline.json` — the inspectable master map: word → timestamp → scene → shot.
- `scenes.json` — same scenes, consumed by steps 2/3 (backward compatible).
- `sync_report.json` — step 2 auto-match decisions: every candidate scored
  0–100 against the scene's exact narration, chosen clip or failure reasons.
- `render_report.json` — step 3: per scene, script text, voice + screen
  windows, clip used, fit strategy (trim/exact/loop), warnings.

In the step 2 picker, each scene shows its semantic brief (requirements,
exclusions, shot list, alternate queries) and — with a Groq key — an
**✨ Auto-match** button (per scene, or "all unpicked" in the sidebar):
searches Pexels/Pixabay, LLM-scores candidates against the exact segment
(action mismatches and excluded content fail), auto-selects above the
threshold (60 by default), revises queries and retries once when nothing
passes, and matches multi-shot scenes shot-by-shot into a multi-clip pick.
Anything below threshold is left for you with reasons. Step 3 then cuts
multi-shot scenes at the exact shot boundaries (not an even split), trims
long clips toward their middle instead of always using the head, and never
loops silently — loops are reported.

Flags: `--no-semantic` restores the old random-duration grouping;
`--semantic-min-seconds`/`--semantic-max-seconds` tune scene length bounds
(short scenes fold into a neighbor as a shot; long ones subdivide at
sentence bounds/pauses). Debug any project's timeline without re-running
audio: `python semantic_timeline.py en/slug` (add `--write` to regenerate
scenes.json/timeline.json — refuses if clips are already picked).

---

## Quick reference

Everything runs from `tools/`. Replace `en/crumbs` with your project.

**Dashboard.** `cd tools && python webui/server.py` opens a local browser page
covering steps 0/1/2/music/step3 -- every flag documented with its type,
default, and help text, presets you can click to fill the form, a live
command preview, and a Run button that streams real stdout in and lets you
Stop long-running steps (the clip picker, the music picker). Stdlib only, no
install needed. It's a convenience layer over the same scripts below, not a
replacement for them -- the CLI still works exactly as documented here.

```
cd tools
python step0_build_script.py en/crumbs          # script.txt from script-1/2/3.txt (news format only)
python step1_audio_and_captions.py en/crumbs    # audio.mp3 + captions.ass + scenes.json
```

Step 0 is only for the [news format](#news-videos-the-50-format) — everywhere
else you write `script.txt` yourself and start at step 1.

**Steps chain automatically.** Step 1 launches step 2 (the browser clip
picker at `:8000`) as soon as it finishes. Step 2 shows a confirm popup —
"Render the final video now?" — the moment the last scene gets a clip; click
OK and it runs step 3 for you in the same terminal. You never have to type
the step 2 or step 3 commands unless you're resuming/redoing one on its own:

```
python step1_audio_and_captions.py en/crumbs --no-chain  # stop after step 1 (e.g. to check audio.mp3 first)
python step2_pick_clips.py en/crumbs                     # resume picking, or run standalone
python step3_render_video.py en/crumbs                   # re-render without re-picking
```

Flags you'll actually reach for:

| Flag | Step | What it's for |
|---|---|---|
| `--rate -5%` | 1 | Narration too fast. `+5%` speeds up. |
| `--scene-seconds-min 15 --scene-seconds-max 25` | 1 | Fewer clips to pick. Default 4–8s means 250+ clips on a 30-min script; 15–25s cuts that to ~80. |
| `--voice en-US-BrianNeural` | 1 | Different narrator. |
| `--caption-style classic` | 1 | Plain white line instead of the yellow capsule highlight. |
| `--caption-style wordcolor-bottom` | 1 | This channel's fixed branded look — white words, locked bright-cyan (`#00E6FF`) current word, black outline, 3 words/card, bottom-anchored like `highlight`/`classic` (not centered like `wordcolor`). |
| `--captions-only` | 1 | Rewrite captions.ass alone, reusing the existing timings. Safe *after* you've picked clips — see below. |
| `--seed 42` | 1 | Reproducible scene breakdown across re-runs. |
| `--no-chain` | 1 | Don't auto-launch step 2 when step 1 finishes. |
| `--port 8001` | 2 | Port 8000 already in use. |
| `--crf 18` | 3 | Higher quality, bigger file. `--crf 23` for smaller. |
| `--preset slow` | 3 | Better compression, slower render. |

Rules of thumb:

- **Re-running step 1 wipes the scene breakdown**, which invalidates the clips
  you've already picked (`selections.json` is keyed by scene number). Get the
  voice and pacing right *before* you start picking.
- **Want to change how captions look after picking clips?** That's what
  `--captions-only` is for — it rewrites `captions.ass` from the existing
  `word_timings.json` and touches nothing else, so your picks stay valid:
  ```
  python step1_audio_and_captions.py ur/crumbs --captions-only
  python step3_render_video.py ur/crumbs
  ```
- **Steps 2 and 3 are safe to re-run** any time. Step 2 resumes where you left
  off; step 3 just re-renders.
- **Step 2 is resumable** — Ctrl+C whenever, re-run the same command to
  continue. Picking 130+ clips is the long part of making a video; you don't
  have to do it in one sitting.

---

## Background music

Optional, run any time before step 3:

```
python step_music_picker.py en/crumbs
```

Opens a browser page: paste a YouTube URL and click Add.

- **A single video's URL** downloads and previews the audio right there via
  yt-dlp + ffmpeg, and queues it.
- **A channel or playlist URL** — e.g. the [YouTube Audio
  Library](https://www.youtube.com/channel/UCht8qITGkBvXKsR1Byln-wA)'s own
  channel, or any "no copyright music" channel's Videos tab — instead
  *lists* every track in it (title, duration, up to 200) with a filter box
  to search by name and its own Add button per track, so you can browse and
  only download the ones you actually want instead of opening each video
  individually to copy its URL.

Add as many tracks as you like and reorder them with the ↑/↓ buttons into
the play order you want, then **Save to project** writes them into
`projects/en/crumbs/` as:

- `bg.mp3` — one track queued
- `bg-1.mp3`, `bg-2.mp3`, ... — two or more, in queue order

`step3_render_video.py` picks these up **automatically, no `--music` flag
needed** — a sequence is concatenated in order into one track first (see
`concat_audio_files()` in `common.py`), then mixed under the narration with
the same auto-ducking as an explicit `--music PATH` always had. Passing
`--music` explicitly still overrides auto-detection, e.g. for a track that
isn't from this picker.

Downloads are cached under the project's `.music_cache/` by video ID, so
re-adding or reordering the same track never re-downloads it, and the queue
itself is saved continuously to `music_selections.json` — closing the tab or
Ctrl+C-ing the server never loses your picks, re-run the same command to
resume.

---

## News videos (the 50+ format)

A second format that shares the whole pipeline but starts one step earlier:
instead of writing `script.txt` yourself, you drop in **three news sources
covering the same story** and the script is written from them.

### 0. Drop in the three sources

```
projects/en/<slug>/script-1.txt
projects/en/<slug>/script-2.txt
projects/en/<slug>/script-3.txt
```

Three outlets, one story. Paste the article text into each — headline and body
is plenty, no formatting needed.

### 0b. Build the script

```
cd tools
python step0_build_script.py en/<slug>
```

Writes `script.txt`: one continuous story that cross-checks the three reports,
in plain spoken language pitched at viewers aged 50+. Where the sources
disagree it says so out loud rather than quietly picking one, and it never adds
a fact the sources don't contain. `script-1/2/3.txt` are left untouched, so you
can re-run for a fresh take any time.

It runs three passes over the Groq API:

1. **Fact brief** — cross-checks the sources into `brief.json`: agreed facts,
   disagreements, timeline, people, numbers, quotes. Everything downstream is
   written from this brief and nothing else, which is what stops the model
   inventing detail.
2. **Sections** — writes the eight sections of the format one at a time against
   the brief. Asking for 2,500 words in one request does not work: the models
   compress the back half, drift off the sources, and repeat paragraphs.
3. **Naturalness** — a rhythm pass, then a targeted pass that splits only the
   over-long sentences and leaves everything else alone.

| Flag | Why |
|---|---|
| `--brief-only` | Stop after `brief.json`. One cheap request — worth it to check the sources were understood before spending the full run. |
| `--words 2500` | Target length. Default 2500 ≈ 16.7 min at `--rate -10%`. |
| `--polish` | Skip generation, re-run the naturalness pass over an existing `script.txt`. Keeps a backup at `script.pre-polish.txt`. |
| `--documentary` | Swaps the rewrite rulebook to the humanized documentary tone and skips the news-only sentence-length/banned-phrase passes, which would otherwise fight that tone. Implies `--polish` — used alone it just rewrites the existing `script.txt` in place, no `script-N.txt` sources needed (documentary mode never does multi-source generation). |
| `--yt-link <url>` | Fetches that video's transcript (no timestamps), writes it straight to `script.txt`, then runs the same rewrite pass as `--polish` — one command from a YouTube link to a rewritten `script.txt`. Creates the project folder if it doesn't exist yet. Needs `youtube-transcript-api` and `yt-dlp` (see `requirements.txt`). |
| `--no-polish` | Generate sections only. Faster, reads stiffer. Cheapest way to get a complete draft. |
| `--fresh` | Ignore a partial build left by an interrupted run and start over. |
| `--model` | Default `openai/gpt-oss-20b` (llama-3.3-70b-versatile was decommissioned by Groq 2026-08-18). See the note in `groq_client.py` before changing it — the other reasoning models were tried and lost. |

**Multiple Groq keys.** `groq_key.txt` can hold more than one key, one per line. When a request is too large for the active key's TPM limit (`HTTP 413`) or a key hits a quota block too long to wait out, `complete()` rolls over to the next key and retries immediately — no code change needed, just add a second line to `groq_key.txt`. Each key gets its own local pacing budget, since they're separate Groq accounts.

**If `--yt-link` fails with a "blocking requests from your IP" or "sign in to confirm you're not a bot" error:** YouTube has been blocking a growing share of anonymous IPs from transcript-fetching tools. `--yt-link` already tries two free methods in order (`youtube-transcript-api`, then `yt-dlp`) before giving up, so this only happens when both are blocked from your network. The free fix: log into YouTube in your normal browser, export its cookies with a browser extension (e.g. "Get cookies.txt LOCALLY" for Chrome/Firefox), and save the file as `tools/cookies.txt`. The `yt-dlp` fallback picks it up automatically and authenticates as that logged-in browser instead of an anonymous IP — no code change needed.

**Why a `--yt-link` transcript used to trigger 413s on its own:** a raw transcript has no blank lines at all, so `--polish`'s batching (which splits on blank-line paragraphs) saw the whole transcript as a single "paragraph" and sent it as one oversized request. It now splits any paragraph over ~480 words by sentence first, so a transcript is always chunked into ~320-word batches regardless of its formatting.

**The `--polish` rulebook is external, not hardcoded.** By default it reads
`tools/groq-rewrite-instructions.txt` — the news rewrite instructions — and
falls back to the built-in `POLISH_SYSTEM` prompt only if that file is ever
missing. Edit `groq-rewrite-instructions.txt` any time to change how news
scripts get polished, no code change needed. Pass `--documentary` to
ignore that file entirely and use the documentary tone rules instead.

### The daily token budget is the real limit

Measured on a free key, 2026-07-22: **100,000 tokens per day**, and one full
2,500-word build costs roughly **40,000** of them. That is about two builds a
day, and the API says so plainly when you run out:

```
Rate limit reached ... on tokens per day (TPD): Limit 100000, Used 99448
```

Most of that cost is the fact brief being resent with all eight section
requests, which is what keeps the sections factually anchored. It buys accuracy
and it is not free.

**The budget refills gradually, it does not reset at midnight.** It trickles
back at about 1.16 tokens a second — a hundred thousand spread over
twenty-four hours. Drain it completely and you are roughly ten hours from
affording another full build, no matter what time of day it is. The
`retry-after` you get back is only the wait for *that one request*, not for a
whole run, so a drained key will stop again a section later. This is the case
the resume cache exists for: leave it, come back, re-run the same command.

If you need builds back to back, that is what the Dev tier is for.

What this means in practice:

- **A blocked run resumes.** The brief and every finished section are cached in
  `.build_cache.json`, so re-running the same command after the quota resets
  picks up where it stopped instead of paying twice. The cache is deleted on
  success, so a normal re-run still gives you a genuinely fresh take. Editing a
  source or changing `--words`/`--model` discards it rather than mixing old
  sections into a new script.
- **`script.txt` is written before the polish passes**, so a block during
  polish still leaves a usable script. Finish it later with `--polish`.
- **Use `--brief-only` first** on a new story. It costs a fraction of a build,
  and a wrong brief means a wrong script.
- **A long block stops rather than waits.** Groq answers an exhausted daily
  quota with a `retry-after` of up to 90 minutes; sleeping through that is
  indistinguishable from a hang, so the run exits and tells you how long to
  wait. Short per-minute throttles are still waited out normally.

**Read `script.txt` before going further.** It is machine-written from your
sources, and the script prints warnings (banned phrases, digits TTS would
misread, repeated sentences) that are worth acting on.

`News Master Prompt.txt` is the rulebook — edit it to change the format
permanently.

**API key.** Put a Groq key in `tools/groq_key.txt` (just the key, nothing
else), or set `GROQ_API_KEY`, or pass `--api-key`. Free keys from
<https://console.groq.com/keys>. The free tier rate-limits readily on a
section-by-section build; the client backs off and retries on its own, so a
run that pauses for a while is working, not stuck.

### 1-3. Same as always, with two flags

```
cd tools
python step1_audio_and_captions.py en/<slug> --font-size 88 --rate -10% \
    --scene-seconds-min 12 --scene-seconds-max 20 --no-chain
python step2_pick_clips.py en/<slug>
python step3_render_video.py en/<slug>
```

`--no-chain` matters more here than elsewhere — see the runtime checkpoint
below. Once you've confirmed the duration is in range, drop `--no-chain` (or
just run `step2_pick_clips.py` yourself) and step 2/3 chain automatically as
usual.

| Flag | Why |
|---|---|
| `--font-size 88` | Captions ~37% larger than the 64 default. **The one flag you must not forget** — it's the whole reason this format exists. Line length rescales itself from 42 to 30 characters so nothing runs off screen. |
| `--rate -10%` | Drops narration from 166 to ~150 wpm. Noticeably easier to follow, and it's what the script's length targets assume. |
| `--scene-seconds-min 12 --scene-seconds-max 20` | Optional. Default 4–8s means ~170 clips to pick on a 17-minute script; this cuts it to ~65. Slower cuts also suit the audience. |

### Length

The script targets **2,300–2,800 words**, which is 15–19 minutes at
`--rate -10%` — comfortably inside your 10–25 minute window. Word counts in
`News Master Prompt.txt` are calibrated against this voice's **real measured
pace of 166 wpm**, not the 140 wpm figure quoted in the older
`Master Prompt.txt`, which is wrong for this voice and produces short scripts.

**Checkpoint:** after step 1, the printed `audio.mp3` duration is the finished
video's exact runtime. If it's outside 10–25 minutes, fix it there — ask for a
longer or shorter script, or adjust `--rate` — before you start picking clips.

---

## One-time setup

- `pip install -r tools/requirements.txt` (installs `edge-tts` and `Pillow`)
- ffmpeg + ffprobe are already installed, with the `libass` filter needed for
  styled captions.
- **Pexels API key** (required) — get a free one at https://www.pexels.com/api/
  and save it to `tools/pexels_key.txt` (just the key, nothing else). You can
  instead set the `PEXELS_API_KEY` environment variable or pass `--api-key`.
  The key stays server-side; the picker page in your browser never sees it.
- **Pixabay / Coverr API keys** (optional) — step 2 also offers Pixabay
  (video + photo) and Coverr (video) as extra source tabs, both free for
  commercial/monetized use with no attribution required, same as Pexels.
  Add each key to enable its tab (see below); without one, that tab shows a
  short setup message.
  - Pixabay: free key at https://pixabay.com/api/docs/ → `tools/pixabay_key.txt`
    or `PIXABAY_API_KEY`.
  - Coverr: create an app for a free key at https://coverr.co/developers →
    `tools/coverr_key.txt` or `COVERR_API_KEY`.
  To swap in a different key later, just overwrite the matching file — no
  restart needed, the tab picks it up next time you click it. Without a key,
  that tab shows a short setup message instead of results.

## One-time: pick your narrator voice

```
cd tools
python voice_preview.py --lang en
python voice_preview.py --lang es
python voice_preview.py --lang ur
```

Listen to the files in `tools/voice_samples/<lang>/` and pick your favorite
(defaults: `en-US-AndrewNeural` for English, `es-MX-JorgeNeural` for Spanish,
`ur-PK-AsadNeural` for Urdu — all calm natural male voices). Run
`python -m edge_tts --list-voices` to
browse the full list (other locales, female voices, etc.) if none of these fit.

## Per-video workflow

### 0. Create the project folder

```
projects/<lang>/<slug>/script.txt
```

e.g. `projects/en/crumbs/script.txt`. Paste your final script into
`script.txt` (Spanish scripts translated from the English Master Prompt
script, keeping the same tone/content rules).

### 1. Generate audio + captions + scene breakdown

```
cd tools
python step1_audio_and_captions.py <lang>/<slug>
```

Optional flags:
- `--voice "en-US-AndrewNeural"` — override the language's default voice
- `--rate -5%` — slow narration down slightly (or `+5%` to speed up)
- `--scene-seconds-min 4` / `--scene-seconds-max 8` — each scene/clip is held
  for a random duration in this range (default 4–8s, a fast-cut pace; raise
  both for fewer, longer-held clips and less picking work)
- `--seed 42` — fix the random scene-duration sequence
- `--caption-style classic` — plain static bold-white line instead of the
  default per-word yellow-capsule highlight
- `--caption-style wordcolor-bottom` — this channel's fixed branded look:
  big bold text, bottom-anchored like ordinary subtitles (same position as
  `highlight`/`classic`, not vertically centered), white words, a locked
  bright-cyan (`#00E6FF`) current word, black outline, 3 words/card —
  matched to the channel's reference caption image so it doesn't drift
  video to video. Override even its locked colours with `--text-color` /
  `--highlight-color` / `--outline-color` if you ever need to.
- `--image-prompts` — also write `image_prompts.txt` for the old Google Flow
  still-image workflow (off by default now)

This writes into `projects/<lang>/<slug>/`:
- `audio.mp3` — the narration
- `word_timings.json` — raw word-level timestamps from edge-tts
- `scenes.json` — the script broken into scenes, each held for a random 4–8s
  by default with an exact start/end/duration, plus the `keywords` and the
  starting `query` used to search Pexels for that scene
- `captions.ass` — styled captions, with the currently spoken word in a yellow
  rounded-pill capsule timed to that exact word

**Checkpoint:** listen to `audio.mp3`. If the pacing or voice isn't right,
tweak `--voice`/`--rate` and re-run step 1 (cheap, just re-synthesizes). Step
1 auto-launches step 2 as soon as it finishes, so if you want to listen first,
pass `--no-chain` and start step 2 yourself once you're happy.

### 2. Pick a stock clip for every scene

```
python step2_pick_clips.py <lang>/<slug>
```

(Skip this — step 1 opens it for you automatically, unless you passed
`--no-chain`.)

Opens a local page at `http://localhost:8000/` that walks scene by scene:

- The scene's narration text, its timestamp, and how many seconds of footage
  it needs are shown at the top.
- **Six source tabs** above the search box: Pexels video/photo, Pixabay
  video/photo, Coverr video, and a local file upload. All three stock
  providers are free for commercial use on a monetized channel with no
  attribution required. Pixabay and Coverr need their own key (see
  [One-time setup](#one-time-setup)) — without one, that tab shows a short
  setup message instead of results. Switching tabs re-runs the current search
  tags against the new provider.
- 12 clips (or photos) appear as cards. **Hover a card to play it**, or click
  the video to pause/resume. Only 12 load at a time so previews stay
  responsive.
- **"Use this clip"** downloads it into `clips/` and records it in
  `selections.json`, then auto-advances to the next scene still missing a clip.
- **The search box** is the real control — the auto-generated query is only a
  starting point. Edit it and press Enter to search anything you want; the
  same tags carry over when you switch source tabs.
- **"Next 12"** pages through more results; "Previous 12" goes back.
- **"Only clips long enough to cover the scene"** (on by default, video tabs
  only) filters out clips shorter than the scene. Uncheck it to see more
  options — short clips still work, they just loop.
- **"Minimum quality"** dropdown (Any / 1080p+ / 2K+ / 4K+) filters Pexels and
  Pixabay results to clips/photos with a rendition at or above the chosen
  tier, and makes step 2 download that higher-resolution rendition instead of
  the ~1080p one it settles for by default. Pixabay's own renditions rarely
  exceed 1080p, so a 2K/4K tier will mostly empty that tab — expected, not a
  bug. Coverr never reports a clip's resolution, so its tab ignores this
  filter and returns all results regardless, with a note in the status line.
- The sidebar lists every scene with a ✓ once it has a clip. Click any scene
  to jump to it, or use ← / → arrow keys. Re-picking a scene replaces its
  clip cleanly.

Progress is saved continuously. Close the page or Ctrl+C the server any time
and re-run the same command to resume where you left off.

**The moment the last scene gets a clip**, a confirm popup asks whether to
render now. Click OK and step 3 runs automatically in the same terminal —
no need to run it by hand. Click Cancel to keep tweaking picks; re-open the
picker (or run step 3 yourself) whenever you're ready.

**Rate limits:** Pexels' measured quota is **25,000 requests/month** — far
more than you'll need; a 133-scene video costs 133 searches plus however many
times you re-search or page. The remaining Pexels quota is shown under the
search box. Pixabay is 100 requests/60s; Coverr is 50/hour on a free key
(2,000/hour on a paid Coverr+ plan). Every search on every provider is cached
under the project's `.pexels_cache/` folder, so revisiting scenes and
re-picking is instant and free regardless of quota.

### 3. Render the final video

```
python step3_render_video.py <lang>/<slug>
```

Optional flags:
- `--fps 30` (default), `--crf 20` (lower = higher quality/bigger file),
  `--preset medium` (libx264 speed/quality tradeoff)
- `--keep-tmp` — keep the per-scene segment files in `tmp/` for debugging

Produces `projects/<lang>/<slug>/output.mp4` — 1920x1080, H.264 + AAC, ready
to upload. Each clip is cut to its scene's exact duration, cropped to fill
1920x1080 (vertical and 4K sources are handled), stripped of its own audio,
hard-cut to the next, captions burned in, narration muxed.

Also writes `credits.txt` naming every stock provider used (Pexels, Pixabay,
and/or Coverr) plus every contributor credited by name — none of these
licenses require attribution, but it's worth pasting into the video
description on a monetized channel.

## Notes / gotchas

- **Segment lengths are not scene `duration`s.** A scene's `duration` only
  spans its first spoken word to its last, so summing them drops the leading
  silence, every pause landing on a scene boundary, and the trailing silence.
  On a 40s test that was a 3s shortfall that `-shortest` chopped off the end
  of the narration. `scene_segment_durations()` in `common.py` sizes each
  segment from one scene's start to the next one's instead, with the last
  running to the real audio duration. **The older image-based
  `step2_render_video.py` still has this bug** — it's superseded by step3, but
  don't reuse it without porting the fix.
- **Pexels 403s have nothing to do with your key.** Pexels sits behind
  Cloudflare, which blocks urllib's default `Python-urllib/3.x` User-Agent and
  answers `403` with body `error code: 1010` no matter how valid the key is.
  `USER_AGENT` in `step2_pick_clips.py` is what makes requests go through — if
  you ever port this code elsewhere, carry that header with it. A genuinely
  bad key returns `401`, not `403`.
- **Pixabay/Coverr have no width/height or orientation filter parity with
  Pexels.** Pixabay's video endpoint has no `orientation` param (its photo
  endpoint does), so a `--vertical` project's portrait filtering only really
  narrows Pexels results — check Pixabay/Coverr picks visually. Coverr also
  doesn't return width/height at all in the fields this app requests, so its
  cards show no dimensions.
- **Repeated queries.** The auto-query uses a theme table in `keywords.py`, so
  a word that recurs through the script (e.g. "crumbs") maps every one of
  those scenes to the same search and would give you repetitive footage. Vary
  it in the search box as you go — you're reviewing every scene anyway.
- **Clip variety.** Nothing stops you picking the same clip for two scenes;
  the picker won't warn you. Worth watching for on long scripts.
- **Crossfade transitions**: still hard cuts between clips. `xfade` is the
  natural v2 addition once you've shipped a few videos.
- **The old Google Flow still-image path** (`--image-prompts` +
  `step2_render_video.py`) is left intact but is no longer the main workflow.
