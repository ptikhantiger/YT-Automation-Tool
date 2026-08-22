"""
Step 3: clips/ + selections.json + audio.mp3 + captions.ass -> output.mp4

Usage:
    python step3_render_video.py <lang>/<slug> [--fps 30] [--crf 20] [--keep-tmp]
        [--music PATH]

--music-volume/--duck-db auto-calibrate from the actual measured loudness of
the music file and the narration (see measure_mean_volume() in common.py) --
pass either explicitly (in dB) to override auto-calibration for it.

e.g. python step3_render_video.py en/crumbs
     python step3_render_video.py en/crumbs --music bg.mp3

--music PATH is looked for inside projects/<project>/ first (so a bare
filename like "bg.mp3" just means "sitting next to this project's
audio.mp3"), falling back to PATH as given (relative to wherever you're
running this from, or absolute) -- handy for one shared track reused
across projects.

Omit --music entirely and this auto-detects whatever
step_music_picker.py saved: a single projects/<project>/bg.mp3, or a
bg-1.mp3, bg-2.mp3, ... sequence, concatenated in order into one track
before mixing -- no flag needed for the common case.

step2_pick_clips.py only records picks -- it never downloads anything -- so
the first thing this script does is fetch every selected clip's real footage
that isn't already sitting in clips/ (already-downloaded clips from a
previous run, and local uploads, are left alone), printing a running
download percentage as it goes.

Each scene's chosen stock clip (Pexels, Pixabay, Coverr, or a local upload) is
cut to that scene's exact narration duration, cropped to fill the output
frame, and stripped of its own audio.
The segments are concatenated, muxed with the narration, and burned with the
styled captions in one final pass.

Output frame is 1920x1080 landscape by default, or 1080x1920 vertical
(YouTube Shorts / TikTok / Reels) if step1_audio_and_captions.py was run
with --vertical -- that choice is read back from render.json automatically,
no flag needed here.

That default doubles to full 4K (3840x2160 / 2160x3840) automatically
whenever the FIRST scene's own picked clip is itself natively 4K+ (checked
with ffprobe on the real downloaded file, not API metadata) -- see
maybe_upscale_to_4k(). It's a one-clip signal, not a per-scene requirement:
later scenes render at whatever resolution they were picked at and simply
get scaled to fill the 4K frame like any other render, so a handful of
carefully-picked 4K clips is enough to trigger it without every clip in the
project needing to match. 4K encodes substantially slower than 1080p, so
this is worth it mainly for short/few-clip projects. --no-4k forces the
1080p-class default regardless.

Clips are looped when they're shorter than the scene they cover (rare -- the
picker defaults to filtering for clips long enough) and simply cut short when
they're longer, which is the normal case.

--music mixes a background track under the narration with automatic
ducking -- quiet while narration plays, back up during real silence -- built
from word_timings.json's exact timestamps (see build_duck_envelope() in
common.py), not detected from the audio.

Also writes credits.txt listing every stock provider used (Pexels, Pixabay,
and/or Coverr) and every contributor credited by name. None of these
licenses require attribution, but crediting creators is good practice for a
monetized channel.
"""
import argparse
import shutil
import sys
from pathlib import Path

from common import (
    build_duck_envelope,
    concat_audio_files,
    download_file,
    load_json,
    measure_mean_volume,
    probe_audio_format,
    probe_duration,
    probe_video_resolution,
    run_ffmpeg,
    scene_segment_durations,
    stereo_pan,
    write_envelope_wav,
)

PROJECTS_DIR = Path(__file__).parent.parent / "projects"
TOOLS_DIR = Path(__file__).parent

# Landscape default -- overridden per-project by render.json (written by
# step1_audio_and_captions.py's --vertical) so a Shorts/Reels project renders
# at 1080x1920 without needing a matching flag repeated here. See
# output_size() below.
OUTPUT_W, OUTPUT_H = 1920, 1080


def output_size(project_dir):
    """(width, height) for this project's render -- read from render.json if
    step 1 wrote one (i.e. --vertical was used), else the 1920x1080 default.

    captions.ass is authored against a specific PlayResX/PlayResY at step 1
    time and can't be changed after the fact, so the render frame here MUST
    match it or every caption position comes out stretched to the wrong
    aspect ratio -- this is why that choice is read back rather than exposed
    as a separate step 3 flag."""
    render_config_path = project_dir / "render.json"
    if render_config_path.exists():
        config = load_json(render_config_path)
        return config["width"], config["height"]
    return OUTPUT_W, OUTPUT_H


# What counts as "the source is actually 4K" when deciding whether to render
# at 4K instead of the 1080p-class default -- matches the "4k and up" tier
# step2_pick_clips.py's own quality filter uses, so a clip that passed that
# filter also clears this bar.
FOUR_K_LONG_EDGE, FOUR_K_SHORT_EDGE = 3840, 2160


def starting_clip_path(selections, scenes, clips_dir):
    """Path to the real video file behind the FIRST scene's clip pick (the
    first item, for a multi-clip scene) -- the single signal used to decide
    whether to render the whole video at 4K (see maybe_upscale_to_4k()).

    Returns None for a still image (a Ken-Burns pan over a photo doesn't
    carry a meaningful "source resolution" the same way stock footage does)
    or if the first scene somehow has nothing pickable."""
    sel = selections[str(scenes[0]["index"])]
    items = selection_items(sel) or []
    if not items or not items[0] or items[0].get("type") == "image":
        return None
    return clips_dir / items[0]["file"]


def maybe_upscale_to_4k(width, height, clip_path):
    """Double an orientation-correct 1080p-class frame (1920x1080 or, for a
    --vertical project, 1080x1920) to its exact 4K equivalent
    (3840x2160 / 2160x3840) if -- and only if -- `clip_path` (the first
    scene's own clip, see starting_clip_path()) is itself natively 4K+.

    Doubling both dimensions keeps the exact aspect ratio captions.ass was
    authored against at step 1 time, so the burned-in captions still land
    correctly without needing their own separate 4K authoring pass (see
    output_size()'s docstring on why that coupling exists at all).

    Leaves width/height untouched if clip_path is missing/unprobeable, or
    the base frame isn't one of the two known 1080p-class sizes (an
    explicit non-default render.json size is left alone rather than
    guessed at)."""
    if not clip_path or not clip_path.exists():
        return width, height
    if (width, height) not in ((OUTPUT_W, OUTPUT_H), (OUTPUT_H, OUTPUT_W)):
        return width, height
    try:
        clip_w, clip_h = probe_video_resolution(clip_path)
    except RuntimeError:
        return width, height
    long_edge, short_edge = max(clip_w, clip_h), min(clip_w, clip_h)
    if long_edge >= FOUR_K_LONG_EDGE and short_edge >= FOUR_K_SHORT_EDGE:
        return width * 2, height * 2
    return width, height


def to_ffmpeg_path(path):
    return str(Path(path).resolve()).replace("\\", "/")


def escaped_filter_path(path):
    # ffmpeg filter option values need ':' escaped and the whole path quoted
    # so spaces (e.g. a name with spaces) don't break filter parsing.
    p = to_ffmpeg_path(path).replace(":", "\\:")
    return f"'{p}'"


def fit_strategy(source_duration, needed):
    """How to fit a clip of source_duration seconds into a needed-seconds
    slot: (trim_start_seconds, label). Never speed-adjusts, never blindly
    stretches:

    - source comfortably longer -> trim: skip a little of the clip's head
      (stock footage fronts fades/slates/establishing wobble), biased toward
      the middle but never past it, so the used range is the meatiest part.
    - source ~= needed -> use as-is from the start.
    - source shorter -> loop (the old behavior), but the caller reports it
      loudly with the loop factor so it's a visible editorial decision --
      the picker's duration filter and multi-shot scenes exist to avoid it.
    """
    if source_duration is None or source_duration <= 0:
        return 0.0, "unknown-source"
    if source_duration >= needed * 1.15 and source_duration - needed >= 1.0:
        trim = min((source_duration - needed) * 0.5, source_duration * 0.2)
        return trim, f"trim (use {trim:.1f}s-{trim + needed:.1f}s of {source_duration:.1f}s)"
    if source_duration >= needed:
        return 0.0, "exact"
    return 0.0, f"loop x{needed / source_duration:.1f} ({source_duration:.1f}s source)"


def render_segment(clip_path, duration, fps, out_path, width=OUTPUT_W, height=OUTPUT_H,
                   trim_start=0.0):
    """Cut clip_path down to exactly `duration` seconds at width x height.

    `trim_start` skips that many seconds of the source first (see
    fit_strategy()). -stream_loop -1 with a hard -t handles both remaining
    cases in one command: a long clip is simply truncated, a short one
    repeats until the scene is filled.
    setsar=1 matters because stock footage occasionally carries a non-square
    pixel aspect ratio, which would make the concat demuxer reject the stream.
    Scale-to-cover + center-crop works for any target aspect, including a
    1080x1920 vertical frame cut from 16:9 landscape footage.
    """
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},"
        f"fps={fps},setsar=1,format=yuv420p"
    )
    # A trimmed source is by definition longer than the slot, so looping can
    # never be needed there -- and -stream_loop plus input-side -ss interact
    # unreliably, so they're mutually exclusive here.
    if trim_start > 0.01:
        args = ["-ss", f"{trim_start:.3f}"]
    else:
        args = ["-stream_loop", "-1"]
    args += [
        "-i", str(clip_path),
        "-t", f"{duration:.3f}",
        "-an",
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        str(out_path),
    ]
    run_ffmpeg(args, description=f"rendering segment for {clip_path.name}")


def render_image_segment(image_path, duration, fps, out_path, width=OUTPUT_W, height=OUTPUT_H):
    """Turn a still image (picked in step2 as an "image" selection, from a
    Pexels/Pixabay photo or a local upload) into a `duration`-second clip at
    width x height.

    A dead-static frame for several seconds reads poorly next to cut video
    clips, so this applies a slow, constant Ken Burns zoom-in via zoompan.
    Upscaling 2x before the zoom keeps the crop from ever sampling above the
    source's native resolution, which is what zoompan does if fed the output
    size directly. `d` (zoompan's internal frame count) is set to match the
    exact number of output frames so the zoom neither stalls nor overruns
    before ffmpeg's own `-t` cuts the stream.
    """
    zoom_frames = max(round(duration * fps), 1)
    vf = (
        f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
        f"crop={width * 2}:{height * 2},"
        f"zoompan=z='min(zoom+0.0008,1.15)':d={zoom_frames}:s={width}x{height}:fps={fps},"
        f"setsar=1,format=yuv420p"
    )
    args = [
        "-loop", "1",
        "-i", str(image_path),
        "-t", f"{duration:.3f}",
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        str(out_path),
    ]
    run_ffmpeg(args, description=f"rendering image segment for {image_path.name}")


def selection_authors(sel):
    """Yield (author, credit_url) for every clip/image behind a selection --
    one for a normal pick, one per item for a multi-clip scene."""
    if sel.get("type") == "multi":
        for item in sel.get("items") or []:
            if item.get("author"):
                yield item["author"], item.get("page_url") or item.get("author_url") or ""
    elif sel.get("author"):
        yield sel["author"], sel.get("page_url") or sel.get("author_url") or ""


PROVIDER_CREDITS = {
    "pexels": "Pexels (https://www.pexels.com)",
    "pixabay": "Pixabay (https://pixabay.com)",
    "coverr": "Coverr (https://coverr.co)",
}


def selection_sources(sel):
    """Provider tag(s) behind a selection -- one for a normal pick, one per
    item for a multi-clip scene. Selections saved before multi-provider
    support existed have no "source" field, which was always a Pexels pick."""
    if sel.get("type") == "multi":
        for item in sel.get("items") or []:
            yield item.get("source") or "pexels"
    else:
        yield sel.get("source") or "pexels"


def write_credits(selections, scenes, path):
    seen = {}
    sources_used = set()
    for s in scenes:
        sel = selections.get(str(s["index"]))
        if not sel:
            continue
        for author, url in selection_authors(sel):
            seen.setdefault(author, url)
        sources_used.update(selection_sources(sel))
    sources_used.discard("local")
    with open(path, "w", encoding="utf-8") as f:
        if sources_used:
            names = [PROVIDER_CREDITS.get(s, s) for s in sorted(sources_used)]
            f.write(f"Stock footage via {', '.join(names)}\n\n")
        for author, url in sorted(seen.items()):
            f.write(f"- {author}{'  ' + url if url else ''}\n")


def selection_items(sel):
    """Every clip/image dict behind a selection -- the single dict itself for
    an ordinary pick, or each entry of `items` for a multi-clip scene. Shared
    by download_selected_clips() and the missing-file check in run()."""
    return sel.get("items") if sel.get("type") == "multi" else [sel]


def download_selected_clips(selections, clips_dir):
    """Fetch every selected clip's real bytes that aren't already sitting in
    clips_dir. step2_pick_clips.py only records picks now -- a local upload
    is written to disk immediately there (no network fetch needed), but a
    Pexels/Pixabay/Coverr pick is left unfetched until this runs, right
    before rendering.

    Already-present files are skipped, so re-running this after a partial
    failure (a dropped connection, a dead URL) only retries what's still
    missing instead of re-downloading everything.
    """
    pending = []  # (dest_path, download_url)
    for sel in selections.values():
        for item in selection_items(sel) or []:
            if not item or not item.get("download_url"):
                continue
            dest = clips_dir / item["file"]
            if not dest.exists():
                pending.append((dest, item["download_url"]))

    if not pending:
        return

    total = len(pending)
    print(f"Downloading {total} clip(s) picked in step 2...")
    for i, (dest, url) in enumerate(pending, start=1):
        try:
            download_file(url, dest)
        except Exception as e:
            print()
            sys.exit(
                f"Failed to download {dest.name}: {e}\n"
                f"Re-run step3_render_video.py -- clips already downloaded are kept, "
                f"only the missing one(s) are retried."
            )
        pct = i * 100 // total
        print(f"\r  {i}/{total} clips downloaded ({pct}%)", end="", flush=True)
    print()


def resolve_music(project_dir, music, tmp_dir):
    """Resolve the --music input.

    An explicit `music` path is honored as before: a bare filename is
    looked for inside the project folder first (so "bg.mp3" just means
    "sitting next to this project's audio.mp3"), falling back to the path
    as given.

    Without one, auto-detect what step_music_picker.py saved:
    projects/<project>/bg.mp3 for a single track, or a bg-1.mp3, bg-2.mp3,
    ... sequence for several -- concatenated in order into one track (see
    concat_audio_files() in common.py) so the rest of the pipeline sees a
    single music input either way. Returns None (render with narration
    only) if nothing was passed and nothing was found.
    """
    if music:
        in_project = project_dir / music
        if in_project.exists():
            return in_project
        if Path(music).exists():
            return Path(music)
        sys.exit(f"--music file not found -- looked for {in_project} and {Path(music).resolve()}")

    sequence = []
    i = 1
    while (project_dir / f"bg-{i}.mp3").exists():
        sequence.append(project_dir / f"bg-{i}.mp3")
        i += 1
    if len(sequence) == 1:
        return sequence[0]
    if sequence:
        merged = tmp_dir / "bg_sequence.mp3"
        concat_audio_files(sequence, merged)
        print(f"  sequenced {len(sequence)} background track(s) ({', '.join(p.name for p in sequence)}) into one")
        return merged

    single = project_dir / "bg.mp3"
    return single if single.exists() else None


def run(project, fps=30, crf=20, preset="medium", keep_tmp=False, music=None,
        music_volume=None, duck_db=None, duck_gap_min=1.2,
        duck_attack_ms=150.0, duck_release_ms=400.0, duck_lead_ms=80.0,
        music_target_db=-20.0, music_below_voice_db=12.0, no_4k=False):
    """Render projects/<project>/output.mp4 from picked clips + captions.
    Returns the output path. Exits the process on any missing prerequisite,
    same as running this script directly.

    `music`, if given (or auto-detected -- see resolve_music()), is mixed
    in under the narration with automatic ducking -- quiet while narration
    plays, back up to `music_volume` during real silence -- built from the
    exact word timestamps in word_timings.json rather than detected from
    the audio (see build_duck_envelope() in common.py).

    `music_volume`/`duck_db` default to None, meaning "figure it out" --
    both narration and music are measured (see measure_mean_volume()) and
    the gains needed to land music at `music_target_db` when at full volume
    and `music_below_voice_db` below narration's own loudness while ducked
    are computed automatically, since a fixed dB number only sounds right
    by coincidence for whatever a specific music file's native loudness
    happens to be. Pass either explicitly to skip auto-calibration for it.

    The render frame is bumped from the 1080p-class default (1920x1080, or
    1080x1920 for a --vertical project) to its exact 4K equivalent whenever
    the FIRST scene's own picked clip is itself natively 4K+ -- see
    maybe_upscale_to_4k(). This is a one-clip signal, not a per-scene check:
    later scenes stay whatever resolution they were picked at and just get
    scaled up to fill the 4K frame like any other render. `--no-4k` forces
    the 1080p-class default regardless."""
    project_dir = PROJECTS_DIR / project
    scenes_path = project_dir / "scenes.json"
    selections_path = project_dir / "selections.json"
    clips_dir = project_dir / "clips"
    audio_path = project_dir / "audio.mp3"
    captions_path = project_dir / "captions.ass"
    output_path = project_dir / "output.mp4"
    tmp_dir = project_dir / "tmp"
    tmp_dir.mkdir(exist_ok=True)

    music = resolve_music(project_dir, music, tmp_dir)

    for required in (scenes_path, audio_path, captions_path):
        if not required.exists():
            sys.exit(f"Missing {required} -- run step1_audio_and_captions.py first.")
    if not selections_path.exists():
        sys.exit(
            f"Missing {selections_path} -- pick clips first:\n"
            f"    python step2_pick_clips.py {project}"
        )

    scenes = load_json(scenes_path)
    selections = load_json(selections_path)

    def selection_files(sel):
        return [item["file"] for item in selection_items(sel) or [] if item]

    def reopen_step2_for(problem_scenes, reason):
        preview = ", ".join(str(i) for i in problem_scenes[:12])
        more = f" (and {len(problem_scenes) - 12} more)" if len(problem_scenes) > 12 else ""
        print(f"{len(problem_scenes)} of {len(scenes)} scenes {reason}: {preview}{more}")
        print("Opening step 2 so you can fix them -- they'll show with a red background.")
        import step2_pick_clips
        render_now = step2_pick_clips.run(project, highlight=problem_scenes)
        if not render_now:
            sys.exit(
                f"Still missing clips -- resume any time with:\n"
                f"    python step2_pick_clips.py {project}"
            )
        # Every flagged scene got fixed and the browser confirmed render --
        # reload everything from disk and start over.
        return run(
            project, fps=fps, crf=crf, preset=preset, keep_tmp=keep_tmp, music=music,
            music_volume=music_volume, duck_db=duck_db, duck_gap_min=duck_gap_min,
            duck_attack_ms=duck_attack_ms, duck_release_ms=duck_release_ms, duck_lead_ms=duck_lead_ms,
            music_target_db=music_target_db, music_below_voice_db=music_below_voice_db,
            no_4k=no_4k,
        )

    unpicked = [s["index"] for s in scenes if str(s["index"]) not in selections]
    if unpicked:
        return reopen_step2_for(unpicked, "have no clip picked yet")

    download_selected_clips(selections, clips_dir)

    missing_file = [
        s["index"] for s in scenes
        if any(not (clips_dir / f).exists() for f in selection_files(selections[str(s["index"])]))
    ]
    if missing_file:
        return reopen_step2_for(missing_file, "have a clip picked but it couldn't be downloaded")

    width, height = output_size(project_dir)
    if not no_4k:
        base_width, base_height = width, height
        clip_path = starting_clip_path(selections, scenes, clips_dir)
        width, height = maybe_upscale_to_4k(width, height, clip_path)
        if (width, height) != (base_width, base_height):
            print(
                f"  first scene's clip ({clip_path.name}) is 4K+ -- rendering the whole "
                f"video at {width}x{height} instead of the {base_width}x{base_height} default"
            )

    narration_seconds = probe_duration(audio_path)
    segment_seconds = scene_segment_durations(scenes, narration_seconds)

    tmp_dir.mkdir(exist_ok=True)
    print(
        f"Rendering {len(scenes)} scenes' clip segments ({narration_seconds:.1f}s of "
        f"narration) at {width}x{height}..."
    )
    def multi_shares(scene, items, screen_start, total_duration):
        """Per-item on-screen durations for a multi-clip scene.

        When the scene's semantic timeline defined shots and the pick count
        matches, each clip covers exactly its shot's narration window (shot
        boundaries within the scene's on-screen span, so cuts land where the
        MEANING changes). Otherwise the old even split.
        """
        shots = scene.get("shots") or []
        if len(shots) == len(items) and all("start" in sh for sh in shots):
            starts = [screen_start] + [sh["start"] for sh in shots[1:]]
            bounds = starts + [screen_start + total_duration]
            shares = [bounds[k + 1] - bounds[k] for k in range(len(items))]
            if all(s > 0.05 for s in shares):
                return shares, "shot-exact"
        return [total_duration / len(items)] * len(items), "even"

    def clip_source_duration(path):
        try:
            return probe_duration(path)
        except Exception:
            return None

    segment_paths = []
    report_scenes = []
    screen_start = 0.0
    for i, scene in enumerate(scenes):
        sel = selections[str(scene["index"])]
        total_duration = segment_seconds[i]
        entry = {
            "scene_id": f"scene_{scene['index']:03d}",
            "script": scene.get("text", ""),
            "voice_start": round(scene.get("start", 0.0), 2),
            "voice_end": round(scene.get("end", 0.0), 2),
            "screen_start": round(screen_start, 2),
            "screen_end": round(screen_start + total_duration, 2),
            "duration": round(total_duration, 2),
            "match_score": sel.get("match_score"),
            "auto_matched": bool(sel.get("auto_matched")),
            "clips": [],
            "warnings": [],
        }
        if sel.get("type") == "multi":
            # Several clips for one scene: shot-exact timing when the semantic
            # timeline defined the shots, even split otherwise.
            items = sel.get("items") or []
            shares, share_mode = multi_shares(scene, items, screen_start, total_duration)
            for j, (item, share) in enumerate(zip(items, shares)):
                clip_path = clips_dir / item["file"]
                seg_path = tmp_dir / f"seg_{i + 1:03d}_{j + 1:02d}.mp4"
                if item.get("type") == "image":
                    render_image_segment(clip_path, share, fps, seg_path, width, height)
                    entry["clips"].append({"file": item["file"], "seconds": round(share, 2), "strategy": "image ken-burns"})
                else:
                    src = clip_source_duration(clip_path)
                    trim, label = fit_strategy(src, share)
                    render_segment(clip_path, share, fps, seg_path, width, height, trim_start=trim)
                    entry["clips"].append({"file": item["file"], "seconds": round(share, 2),
                                           "source_seconds": src, "strategy": label})
                    if label.startswith("loop"):
                        entry["warnings"].append(f"{item['file']} loops ({label}) -- pick a longer clip to avoid the repeat.")
                segment_paths.append(seg_path)
            shares_text = ", ".join(f"{s:.1f}s" for s in shares)
            print(f"  [{i + 1}/{len(scenes)}] {len(items)} clips -> {total_duration:.1f}s ({share_mode} split: {shares_text})")
        else:
            clip_path = clips_dir / sel["file"]
            seg_path = tmp_dir / f"seg_{i + 1:03d}.mp4"
            if sel.get("type") == "image":
                render_image_segment(clip_path, total_duration, fps, seg_path, width, height)
                entry["clips"].append({"file": sel["file"], "seconds": round(total_duration, 2), "strategy": "image ken-burns"})
                print(f"  [{i + 1}/{len(scenes)}] {clip_path.name} -> {total_duration:.1f}s")
            else:
                src = clip_source_duration(clip_path)
                trim, label = fit_strategy(src, total_duration)
                render_segment(clip_path, total_duration, fps, seg_path, width, height, trim_start=trim)
                entry["clips"].append({"file": sel["file"], "seconds": round(total_duration, 2),
                                       "source_seconds": src, "strategy": label})
                if label.startswith("loop"):
                    entry["warnings"].append(f"{sel['file']} loops ({label}) -- pick a longer clip or use multi-clip for this scene.")
                print(f"  [{i + 1}/{len(scenes)}] {clip_path.name} -> {total_duration:.1f}s [{label}]"
                      + (f" (match {sel.get('match_score')}/100)" if sel.get("match_score") is not None else ""))
            segment_paths.append(seg_path)
        report_scenes.append(entry)
        screen_start += total_duration

    concat_list_path = tmp_dir / "concat_list.txt"
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for seg_path in segment_paths:
            f.write(f"file '{to_ffmpeg_path(seg_path)}'\n")

    print("Concatenating segments, muxing audio, and burning captions...")
    ass_arg = f"ass={escaped_filter_path(captions_path)}"
    args_final = [
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_list_path),
        "-i", str(audio_path),
    ]
    audio_filter = None
    audio_map = "1:a:0"

    if music:
        timings_path = project_dir / "word_timings.json"
        if not timings_path.exists():
            sys.exit(f"--music needs {timings_path} -- run step1_audio_and_captions.py first.")
        words = load_json(timings_path)

        if music_volume is None or duck_db is None:
            narration_mean = measure_mean_volume(audio_path)
            music_mean = measure_mean_volume(music)
            auto_music_volume = music_target_db - music_mean
            auto_duck_db = (narration_mean - music_below_voice_db) - music_target_db
            if music_volume is None:
                music_volume = auto_music_volume
            if duck_db is None:
                duck_db = auto_duck_db
            print(
                f"  measured narration {narration_mean:.1f}dB, music {music_mean:.1f}dB -- "
                f"auto-calibrated to --music-volume {music_volume:.1f} --duck-db {duck_db:.1f}"
            )

        envelope_path = tmp_dir / "duck_envelope.wav"
        envelope_duration = narration_seconds + 1.0
        duck_level = 10 ** (duck_db / 20)
        points = build_duck_envelope(
            words, envelope_duration, gap_min=duck_gap_min, duck_level=duck_level,
            attack_ms=duck_attack_ms, release_ms=duck_release_ms, lead_ms=duck_lead_ms,
        )
        write_envelope_wav(points, envelope_path, envelope_duration)
        print(
            f"  ducking music under narration (base {music_volume:.0f}dB, "
            f"{duck_db:.0f}dB while speaking, back up after {duck_gap_min:.1f}s+ of silence)"
        )
        # Explicit pan-based upmix, not aformat's automatic one -- narration
        # and the envelope are always mono, and aformat's automatic mono ->
        # stereo conversion quietly drops a mono source by ~3dB (confirmed
        # by measurement) rather than a straight duplication. See
        # stereo_pan() in common.py.
        _, narr_layout = probe_audio_format(audio_path)
        _, music_layout = probe_audio_format(music)
        narr_pan = stereo_pan(narr_layout)
        music_pan = stereo_pan(music_layout)
        env_pan = stereo_pan("mono")
        args_final += ["-stream_loop", "-1", "-i", str(music), "-i", str(envelope_path)]
        audio_filter = (
            f"[1:a]aformat=sample_fmts=fltp,{narr_pan},aresample=44100[narr];"
            f"[2:a]aformat=sample_fmts=fltp,{music_pan},aresample=44100,volume={music_volume}dB[musicg];"
            f"[3:a]aformat=sample_fmts=fltp,{env_pan},aresample=44100[env];"
            "[musicg][env]amultiply[ducked];"
            "[narr][ducked]amix=inputs=2:duration=first:dropout_transition=0,volume=2[aout]"
        )
        audio_map = "[aout]"

    video_filter_parts = [f"[0:v]{ass_arg}[vout]"]

    filter_complex_parts = ([audio_filter] if audio_filter else []) + video_filter_parts
    args_final += [
        "-filter_complex", ";".join(filter_complex_parts),
        "-map", "[vout]",
        "-map", audio_map,
    ]
    args_final += [
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
    ]
    # -shortest exists to stop a stray over-long stream from padding the
    # container. In the music path it's redundant -- the mix's own
    # `amix=duration=first` already ends the audio exactly at the narration's
    # end and the video length is fixed by the concat segments -- and pairing
    # -shortest with an infinitely-looped music input leaves the cut point to
    # ffmpeg's stream heuristics rather than the filtergraph, so it's only
    # applied on the plain narration path where it can't misfire.
    if not music:
        args_final += ["-shortest"]
    args_final += [
        # moov atom up front so the file streams/scrubs immediately in
        # browsers and players without a full download first.
        "-movflags", "+faststart",
        str(output_path),
    ]
    run_ffmpeg(args_final, description="final concat + captions + audio mux")

    write_credits(selections, scenes, project_dir / "credits.txt")

    # Scene-level render report: for every scene, which exact script text it
    # covers, its voice and on-screen windows, which clip filled it, the fit
    # strategy chosen, and any warnings (loops, missing match scores).
    from common import save_json as _save_json

    _save_json(
        {
            "output": output_path.name,
            "narration_seconds": round(narration_seconds, 2),
            "frame": f"{width}x{height}",
            "scenes": report_scenes,
        },
        project_dir / "render_report.json",
    )
    warned = [e for e in report_scenes if e["warnings"]]
    if warned:
        print(f"\n{len(warned)} scene(s) have timing warnings (see render_report.json):")
        for e in warned:
            for w in e["warnings"]:
                print(f"  {e['scene_id']}: {w}")

    if not keep_tmp:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print(f"\nDone: {output_path}")
    print(f"Credits for the description: {project_dir / 'credits.txt'}")
    return output_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", help="Path under projects/, e.g. en/crumbs")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--crf", type=int, default=20, help="Final encode quality (lower = better/larger)")
    parser.add_argument("--preset", default="medium", help="libx264 preset")
    parser.add_argument("--keep-tmp", action="store_true", help="Don't delete tmp/ segments after rendering")
    parser.add_argument(
        "--music", default=None, metavar="PATH",
        help="Background music mp3/wav to mix under the narration, with automatic ducking: "
        "quiet while narration is speaking, back up to --music-volume during real silence "
        "(intro, outro, any pause of --duck-gap-min seconds or longer). Loops if shorter "
        "than the video. Omit this and a project's bg.mp3 (or bg-1.mp3, bg-2.mp3, ... "
        "sequence, as saved by step_music_picker.py) is used automatically if present, "
        "narration-only if not.",
    )
    parser.add_argument(
        "--music-volume", type=float, default=None,
        help="Music level in dB when not ducked, i.e. during silence (with --music). Default: "
        "auto-calibrated from the music file's own measured loudness so it lands at "
        "--music-target-db regardless of how loud/quiet the source file was mastered. Pass "
        "a number to override auto-calibration with a fixed level instead.",
    )
    parser.add_argument(
        "--duck-db", type=float, default=None,
        help="Additional dB reduction applied to music while narration is playing (with "
        "--music). Default: auto-calibrated from narration's measured loudness so music "
        "lands --music-below-voice-db under it. Pass a number to override.",
    )
    parser.add_argument(
        "--music-target-db", type=float, default=-20.0,
        help="Target mean loudness (dBFS) for music at full volume, i.e. during silence, "
        "when auto-calibrating --music-volume (with --music, no explicit --music-volume)",
    )
    parser.add_argument(
        "--music-below-voice-db", type=float, default=12.0,
        help="How far below narration's own measured loudness music should sit while ducked, "
        "when auto-calibrating --duck-db (with --music, no explicit --duck-db)",
    )
    parser.add_argument(
        "--duck-gap-min", type=float, default=1.2,
        help="Minimum silence gap, in seconds, before music swells back to full volume -- "
        "shorter pauses (an ordinary breath between sentences) stay ducked through them "
        "rather than pumping back up and down (with --music)",
    )
    parser.add_argument(
        "--duck-attack-ms", type=float, default=150.0,
        help="How fast music ducks down when narration starts, in ms (with --music)",
    )
    parser.add_argument(
        "--duck-release-ms", type=float, default=400.0,
        help="How fast music swells back up once narration stops, in ms (with --music)",
    )
    parser.add_argument(
        "--duck-lead-ms", type=float, default=80.0,
        help="Safety margin, in ms, by which the duck-down ramp finishes BEFORE a word's own "
        "start timestamp rather than landing exactly on it -- closes the gap that otherwise "
        "leaves a barely-audible sliver of full-volume music right up against the first word "
        "(word-timing granularity, encoder rounding). Increase if you can still hear music "
        "overlapping the very start of speech; 0 reverts to the old exactly-on-time behavior "
        "(with --music)",
    )
    parser.add_argument(
        "--no-4k", action="store_true",
        help="Render at the 1080p-class default (1920x1080, or 1080x1920 for --vertical) "
        "even if the first scene's own picked clip is natively 4K+. Without this flag, that "
        "one clip's resolution is the signal: if it's 4K+, the whole video renders at the "
        "matching 4K frame (3840x2160 / 2160x3840) instead -- keep your clip picks few and "
        "genuinely 4K-sourced if you want this, since a 4K render takes substantially longer "
        "to encode than 1080p.",
    )
    args = parser.parse_args()

    run(
        args.project, fps=args.fps, crf=args.crf, preset=args.preset, keep_tmp=args.keep_tmp,
        music=args.music, music_volume=args.music_volume, duck_db=args.duck_db,
        duck_gap_min=args.duck_gap_min, duck_attack_ms=args.duck_attack_ms,
        duck_release_ms=args.duck_release_ms, duck_lead_ms=args.duck_lead_ms,
        music_target_db=args.music_target_db,
        music_below_voice_db=args.music_below_voice_db,
        no_4k=args.no_4k,
    )


if __name__ == "__main__":
    main()
