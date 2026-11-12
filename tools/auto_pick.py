"""Step 2 without a browser: "Auto-match all" for a whole project.

    python tools/auto_pick.py en/my-video
    python tools/auto_pick.py en/my-video --threshold 60 --no-ai-fallback

Starts the step 2 picker in the background (no browser), drives
/api/automatch exactly like the "Auto-match all unpicked" button does --
one scene per request, same strictness / soft floor / AI fallback -- then
runs "Replace repeated clips" once, and stops the picker.

Writes projects/<project>/auto_pick_report.json with the counts and a
`needs_review` verdict the cloud workflow uses: when too many shots are
weak (soft / keyword-only / AI-filled / still empty), the upload is not
scheduled, so a video nobody looked at can't go public by itself.

Exit codes: 0 done (every scene has a clip), 2 some scenes are still empty,
1 error.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
PROJECTS = TOOLS.parent / "projects"
# Share of weak shots above which the video is flagged for a human look.
REVIEW_SHARE = 0.25


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(port, path, body=None, timeout=1800):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")


def wait_ready(port, proc, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            raise RuntimeError(f"the picker exited early (code {proc.returncode}) -- see the log above")
        try:
            return call(port, "/api/scenes", timeout=10)
        except (urllib.error.URLError, ConnectionError, OSError, ValueError):
            time.sleep(1)
    raise RuntimeError("the picker didn't start within 3 minutes")


def drive(port, threshold, ai_fallback, min_quality=None, repeats=False, log=print):
    soft_floor = max(35, threshold - 15)
    attempted, totals = [], {"matched": 0, "soft": 0, "heuristic": 0, "ai_filled": 0, "failed": 0}
    llm_down, t0 = False, time.time()
    while True:
        body = {"all": True, "limit": 1, "threshold": threshold, "soft_floor": soft_floor,
                "exclude": attempted, "ai_fallback": ai_fallback}
        if repeats:
            body["repeats"] = True
        if min_quality:
            body["min_quality"] = min_quality
        d = None
        for attempt in range(3):
            try:
                d = call(port, "/api/automatch", body)
                break
            except (urllib.error.URLError, ConnectionError, OSError, TimeoutError) as e:
                log(f"  request failed ({e}); retrying...")
                time.sleep(10)
        if d is None:
            raise RuntimeError("the picker stopped answering")
        if d.get("error"):
            raise RuntimeError(d["error"])
        es = d.get("entries") or []
        for e in es:
            attempted.append(e["scene_index"])
            tag = ("AI" if e.get("ai_generated") else "keyword" if e.get("heuristic")
                   else "soft" if e.get("soft") else e.get("status"))
            log(f"  scene {e['scene_index']:>4}: {tag:<7} {e.get('score') if e.get('score') is not None else '-':>3}"
                f"   {(e.get('script') or '')[:70]}")
        for k in totals:
            totals[k] += d.get(k) or 0
        llm_down = llm_down or bool(d.get("llm_down"))
        if d.get("paused"):
            log(f"Paused: {d['paused']}")
            break
        if len(attempted) % 10 == 0 and es:
            mins = (time.time() - t0) / 60
            log(f"-- {len(attempted)} done, {d.get('remaining')} left, {mins:.0f} min so far")
        if not es or d.get("remaining") == 0:
            break
    totals["llm_down"] = llm_down
    return totals


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", help="path under projects/, e.g. en/my-video")
    ap.add_argument("--threshold", type=int, default=60, help="strictness 0-100 (picker default 60 = balanced)")
    ap.add_argument("--no-ai-fallback", dest="ai_fallback", action="store_false",
                    help="leave unmatchable shots empty instead of generating an illustration")
    ap.add_argument("--min-quality", default=None, help="e.g. hd / fullhd (default: picker's default)")
    ap.add_argument("--no-repeats-pass", dest="repeats_pass", action="store_false",
                    help="skip the 'Replace repeated clips' pass")
    args = ap.parse_args()

    project_dir = PROJECTS / args.project
    if not (project_dir / "scenes.json").exists():
        sys.exit(f"{project_dir}/scenes.json not found -- run step 1 first.")

    port = free_port()
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        [sys.executable, str(TOOLS / "step2_pick_clips.py"), args.project,
         "--port", str(port), "--no-browser", "--no-auto-render"],
        cwd=str(TOOLS), env=env, stdout=subprocess.DEVNULL, stderr=None,
    )
    t0 = time.time()
    try:
        info = wait_ready(port, proc)
        n = len(info.get("scenes") or [])
        if not info.get("automatch_available"):
            raise RuntimeError("auto-match unavailable: no Gemini key (GEMINI_API_KEY)")
        print(f"Picker up on port {port}: {n} scenes, {len(info.get('selections') or {})} already picked.")
        print(f"Auto-matching (strictness {args.threshold}, AI fallback {'on' if args.ai_fallback else 'off'})...")
        totals = drive(port, args.threshold, args.ai_fallback, args.min_quality)
        if args.repeats_pass:
            print("Replacing repeated clips...")
            drive(port, args.threshold, args.ai_fallback, args.min_quality, repeats=True)
        final = call(port, "/api/scenes", timeout=30)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()

    scenes = final.get("scenes") or []
    sels = final.get("selections") or {}
    empty = [s["index"] for s in scenes if str(s["index"]) not in sels]
    weak_ids = set(empty)
    # Scene-level flags written by the matcher. An AI illustration is drawn
    # for the exact sentence, so it isn't counted as weak by itself; a
    # below-the-bar stock clip (soft) or a keyword-only pick (Gemini down) is.
    kinds = {"soft": 0, "heuristic": 0, "ai": 0}
    for k, sel in sels.items():
        if sel.get("ai_generated"):
            kinds["ai"] += 1
        if sel.get("heuristic"):
            kinds["heuristic"] += 1
            weak_ids.add(int(k))
        elif sel.get("soft_match") and not sel.get("ai_generated"):
            kinds["soft"] += 1
            weak_ids.add(int(k))
    share = len(weak_ids) / max(1, len(scenes))
    reasons = []
    if empty:
        reasons.append(f"{len(empty)} scene(s) have no clip")
    if totals.get("llm_down"):
        reasons.append("Gemini quota ran out; some scenes matched by keywords only")
    if share > REVIEW_SHARE:
        reasons.append(f"{share:.0%} of scenes are weak matches (over {REVIEW_SHARE:.0%})")
    report = {
        "scenes": len(scenes), "picked": len(sels), "empty": empty,
        "soft_scenes": kinds["soft"], "keyword_only_scenes": kinds["heuristic"], "ai_scenes": kinds["ai"],
        "weak_scene_share": round(share, 3), "llm_down": totals.get("llm_down"),
        "minutes": round((time.time() - t0) / 60, 1),
        "needs_review": bool(reasons), "reasons": reasons,
    }
    (project_dir / "auto_pick_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("\n" + json.dumps(report, indent=2))
    sys.exit(2 if empty else 0)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        sys.exit(f"ERROR: {e}")
