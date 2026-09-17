# INFO — how to run this from the cloud, and what to do when something is stuck

This is the day-to-day operator guide for the cloud setup:

- **GitHub Codespaces** runs the app (dashboard, script writing, voice, clip picking).
- **GitHub Actions** renders the video (`.github/workflows/render.yml`).
- Your laptop (or phone) is only a browser.

Repo: <https://github.com/ptikhantiger/YT-Automation-Tool>
Codespaces: <https://github.com/codespaces>
Actions (renders): <https://github.com/ptikhantiger/YT-Automation-Tool/actions>
Secrets: <https://github.com/settings/codespaces>

Every command below is typed in the **codespace terminal** (the panel at the
bottom of the codespace window, prompt `@ptikhantiger ➜ /workspaces/YT-Automation-Tool (main) $`)
unless it says otherwise.

---

## 1. The normal workflow (nothing stuck)

1. Open <https://github.com/codespaces> → click **YouTube Automation Tool**.
   It wakes up in 30–60 s. The dashboard starts by itself.
2. Open the dashboard: **PORTS** tab → row **Dashboard (8765)** → globe icon.
   Or paste the URL: `https://youtube-automation-tool-x5g9p46grgp9fp94p-8765.app.github.dev/`
3. **Script.** Either:
   - Explorer (left) → `projects/en` → right-click → New Folder → `myvideo` →
     right-click it → New File → `script.txt` → paste → Ctrl+S; or
   - Dashboard → **Step 0** → project `en/myvideo` → `--yt-link <youtube url>`
     (writes the script from that video's transcript, then rewrites it).
4. Dashboard → **Step 1** → project `en/myvideo` → **Run**. Wait for
   `captions.ass written`. The clip picker (Step 2) opens by itself.
5. **Picker.** Sidebar → **✨ Auto-match all unpicked** (or pick by hand).
   The bar at the top shows `N / M scenes have a clip — X to go`.
6. When it says **All M scenes have a clip** → click **☁ Render on GitHub (free)**
   in that top bar. It commits, pulls, pushes, and shows a link to the run.
7. Open the link (Actions page). The run is called `render: en/myvideo`.
   Rough time: 2–3 minutes per minute of video (a 30-min video ≈ 60–75 min).
8. Green ✓ → click the run → **Artifacts** (bottom) → `video-en-myvideo`.
   The zip has `output.mp4`, `credits.txt` (paste into the description),
   `render_report.json`.
9. Close the tab. The codespace stops itself after 30 idle minutes.

**You never push manually.** The Render button is the only "push".

---

## 2. Dashboard problems

### The Dashboard URL shows "HTTP ERROR 502" / "isn't working"

The dashboard process isn't running (or the codespace is still booting).

```bash
bash .devcontainer/start.sh
```

Expected: `Dashboard running on port 8765`. Then reload the dashboard tab.

If it says something else, read the log:

```bash
cat /tmp/ytat/dashboard.log
```

Look for a `[FAIL]` line. Typical one: `Pexels (stock clips) -- missing` →
the `PEXELS_API_KEY` secret isn't visible → see section 6.

### The Ports tab is empty / I deleted the ports

Restart the codespace (section 7) — the ports come back automatically.
Or, without a restart: **PORTS** tab → **Forward a Port** → type `8765` → Enter.

### Port 8000 (Clip picker) shows 502

Normal when no Step 2 is running. Start Step 2 from the dashboard first.

### The picker is embedded in the dashboard but the box is blank

GitHub needs to set a cookie for that port once. **PORTS** tab → row
**Clip picker (8000)** → globe icon → it opens in its own tab. Use it there,
or go back to the dashboard and reload.

### Restart the dashboard (after a code update, or if it misbehaves)

```bash
pkill -f webui/server.py; pkill -f launch.py; bash .devcontainer/start.sh
```

Then reload the dashboard tab. Running renders on GitHub are not affected.

---

## 3. "Render on GitHub" problems

### The button shows a red error

Copy the whole red text — it contains git's own message. The common cases:

**`git push failed`** (rejected / non-fast-forward / "fetch first")
→ The codespace is behind GitHub. The button now pulls first, but if you see
this anyway:

```bash
git pull --rebase origin main && git push
```

The commit the button already made gets pushed and the render starts.

**`git commit failed ... tell me who you are`**

```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

Then click the button again.

**`git pull --rebase failed`**

```bash
git status
```

If it shows `rebase in progress`:

```bash
git rebase --abort
git pull --rebase origin main
git push
```

### I clicked Render but no run appears on the Actions page

Check whether the commit left the codespace:

```bash
git status
```

- `Your branch is ahead of 'origin/main' by 1 commit` → it did **not** push. Run
  `git pull --rebase origin main && git push`.
- `Your branch is up to date with 'origin/main'` → it pushed. Reload the Actions
  page; the run is named `render: en/<slug>` and may take ~20 s to appear.
  If a run exists but was **skipped**, the commit message didn't start with
  `render:` — click the button again (it makes a correct commit).

### Start a render by hand (without the button)

```bash
git add projects/en/myvideo
git commit -m "render: en/myvideo"
git push
```

The commit message `render: en/<slug>` is the trigger. Re-render with nothing
changed:

```bash
git commit --allow-empty -m "render: en/myvideo" && git push
```

### Start a render from the GitHub website (change quality / preset / 4K)

Actions → **Render video** (left) → **Run workflow** → type `en/myvideo`,
choose `crf` (18 high quality, 20 default, 23 smaller), `preset`, `allow_4k`.

---

## 4. The render run failed (red ✗ on the Actions page)

Click the run. At the top there is an **annotation** with the last lines of the
render log — that is the reason. Common ones:

**`N of M scenes have no clip picked yet`** → pick them in Step 2, click Render again.

**`have a clip picked but it's missing or unplayable`** → a clip could not be
downloaded on GitHub's side (dead link / provider block). Open Step 2, the
scene is shown; pick a different clip, click Render again.

**`is not in the repository -- run 'git add ...'`** → the project files were
never pushed. Click Render again, or push by hand (section 3).

**Timeout after 6 hours** → the job limit. Only happens on very long 4K renders;
use the website form with `allow_4k` off and `preset` = `veryfast`.

The full log needs you to be signed in to GitHub (click the failed step to
expand it). The tail is also uploaded as an artifact named `render-log-...`.

---

## 5. Code updates ("Sync code")

When the code in the repo is updated (from the laptop, or a fix), the codespace
does not pick it up until it pulls.

- Dashboard header → **⟳ Sync code**, or picker top bar → **⟳ Sync code**, or:

```bash
git pull --rebase origin main
```

If it reports that `.py` / `.html` files changed, restart the dashboard
(section 2, last item) and re-run Step 2 to load the new code. Your picks are
saved in `selections.json` and survive.

The codespace also pulls automatically every time it boots.

---

## 6. Keys and secrets

All keys live as **Codespaces Secrets** (<https://github.com/settings/codespaces>),
scoped to the repo `YT-Automation-Tool`:

| Secret | Used for | Required |
|---|---|---|
| `PEXELS_API_KEY` | stock clips | yes |
| `GEMINI_API_KEY` | script writing, semantic scenes, auto-match | recommended |
| `PIXABAY_API_KEY` | extra stock source | optional |
| `COVERR_API_KEY` | extra stock source | optional |
| `POLLINATIONS_TOKEN` | faster, watermark-free AI illustrations | optional |
| `YT_COOKIES` | YouTube transcript / music download from the codespace | needed for `--yt-link` and the music picker |

**Rules:**

- A new or changed secret is only visible after a **full codespace restart**
  (section 7). `git pull`, reloading the tab, or reopening the browser is not a restart.
- Check whether a secret is visible:
  ```bash
  echo "PEXELS: ${#PEXELS_API_KEY}  GEMINI: ${#GEMINI_API_KEY}  YT_COOKIES: ${#YT_COOKIES}"
  ```
  A `0` means not visible → restart the codespace; if still `0`, open the secret
  on the settings page and make sure **Repository access** includes this repo.
- After a restart, also restart the dashboard (section 2) so its child processes
  inherit the new values.

### `--yt-link` says "Sign in to confirm you're not a bot" / "blocking requests from your IP"

YouTube blocks anonymous requests from cloud IPs; the codespace is on Azure.
The fix is a logged-in cookie file:

1. On your own computer, in Chrome, install the extension **Get cookies.txt LOCALLY**.
2. Log into YouTube with a **secondary Google account** (not the channel's).
3. On youtube.com click the extension → **Export** → a `.txt` file downloads.
4. Open it in Notepad → Ctrl+A → Ctrl+C.
5. <https://github.com/settings/codespaces> → secret `YT_COOKIES` → **Update** → paste → save.
6. Restart the codespace (section 7). The app writes it to `tools/cookies.txt`
   on first use (tabs/spaces are normalised automatically).

Quick test without a restart (only works if the secret is already visible):

```bash
printf '%s\n' "$YT_COOKIES" > tools/cookies.txt && wc -l tools/cookies.txt
```

Cookies expire when that account signs out or after a few months — repeat 3–6.

If the error line says `yt-dlp (with cookies.txt)` and still fails, the cookies
themselves were rejected → re-export fresh ones.

---

## 7. Restarting / recreating the codespace

### Restart (stop + start) — needed after adding/changing secrets

In the codespace: press **F1** → type `Codespaces: Stop Current Codespace` → Enter.
Wait for "stopped", then reopen it from <https://github.com/codespaces>.
Everything on its disk is kept. The dashboard auto-starts.

### Rebuild the container — if the setup itself is broken (ffmpeg missing, pip errors)

**F1** → `Codespaces: Rebuild Container`. Takes ~2 minutes; files are kept.
Your projects are also safe on GitHub if they were ever rendered (the Render
button pushes them).

### The codespace was deleted (unused for 30 days)

Repo page → **Code** → **Codespaces** → **Create codespace on main**. Secrets are
already in place. The new codespace has a **different name**, so the URLs
change: read them from the PORTS tab, and update the bookmark.

### Check free-tier usage

<https://github.com/settings/billing/summary> → Codespaces. Free: 60 hours/month
on the 2-core machine, 15 GB storage. Renders on Actions are unlimited (public repo).

---

## 8. Projects: files, safety, cleanup

- A project is `projects/<lang>/<slug>/`. The repo tracks only the small files
  (`script.txt`, `audio.mp3`, `scenes.json`, `selections.json`, `captions.ass`,
  `timeline.json`, `word_timings.json`, `render.json`, `bg*.mp3`, local uploads
  and AI images in `clips/`). Stock clips, caches, `tmp/` and `output.mp4` are
  never committed — the render downloads clips itself.
- Anything **not yet rendered** exists only in the codespace. To back it up
  without rendering:
  ```bash
  git add projects/en/myvideo && git commit -m "myvideo: work in progress" && git push
  ```
  (a message that does not start with `render:` does not trigger a render).
- Re-running Step 1 invalidates the picks (scenes change). Get voice and pacing
  right before picking.
- Remove a finished project from the repo:
  ```bash
  git rm -r projects/en/myvideo && git commit -m "remove myvideo" && git push
  ```
- Restore a project deleted earlier (find the commit that still had it):
  ```bash
  git log --oneline -- projects/en/myvideo | head
  git checkout <commit> -- projects/en/myvideo
  ```

---

## 9. Phone / tablet

Everything is a web page behind your GitHub login, so it works in a mobile
browser. Turn on **Desktop site**. Waking the codespace, Step 0 with a
`--yt-link`, Step 1, Auto-match, Render on GitHub, and downloading the artifact
are all fine on a phone. Picking clips by hand is cramped; use Auto-match plus
the **Review picked clips** page.

---

## 10. Quick command reference

| Situation | Command (codespace terminal) |
|---|---|
| Dashboard not running / 502 | `bash .devcontainer/start.sh` |
| Restart the dashboard | `pkill -f webui/server.py; pkill -f launch.py; bash .devcontainer/start.sh` |
| Dashboard log | `cat /tmp/ytat/dashboard.log` |
| Get latest code | `git pull --rebase origin main` |
| Render commit stuck in codespace | `git pull --rebase origin main && git push` |
| Render by hand | `git add projects/en/X && git commit -m "render: en/X" && git push` |
| Re-render, nothing changed | `git commit --allow-empty -m "render: en/X" && git push` |
| Are secrets visible? | `echo "${#PEXELS_API_KEY} ${#GEMINI_API_KEY} ${#YT_COOKIES}"` |
| Write cookies from the secret | `printf '%s\n' "$YT_COOKIES" > tools/cookies.txt` |
| Git state | `git status` |
| Undo a broken rebase | `git rebase --abort` |
| Stop the codespace | F1 → `Codespaces: Stop Current Codespace` |
| Rebuild the container | F1 → `Codespaces: Rebuild Container` |
| Run Step 0 by hand | `cd tools && python step0_build_script.py en/X --yt-link <url>` |
| Run Step 1 by hand | `cd tools && python step1_audio_and_captions.py en/X --no-chain` |
| Run Step 2 by hand | `cd tools && python step2_pick_clips.py en/X --no-browser` |
| Check ffmpeg | `ffmpeg -version \| head -1` |
