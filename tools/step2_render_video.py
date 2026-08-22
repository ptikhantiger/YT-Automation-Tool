"""
Step 2: scenes.json + images/ + audio.mp3 + captions.ass -> output.mp4

Usage:
    python step2_render_video.py <lang>/<slug> [--fps 30] [--crf 20] [--keep-tmp]

e.g. python step2_render_video.py en/crumbs

Reads projects/<lang>/<slug>/{scenes.json, images/, audio.mp3, captions.ass}
and writes projects/<lang>/<slug>/output.mp4. Each image gets a slow Ken
Burns zoom (alternating zoom-in / zoom-out) matched exactly to its scene's
real narration duration, then all segments are concatenated, muxed with the
narration audio, and burned with the styled captions in one final pass.
"""
import argparse
import shutil
import sys
from pathlib import Path

from common import load_json, natural_sorted_images, run_ffmpeg

PROJECTS_DIR = Path(__file__).parent.parent / "projects"

# High-res working canvas the zoompan filter animates within before
# downscaling to the final 1920x1080 output -- gives the zoom room to move
# without ever upscaling past source quality on typical Flow image sizes.
ZOOMPAN_CANVAS = "3840:2160"
OUTPUT_SIZE = "1920x1080"


def to_ffmpeg_path(path):
    return str(Path(path).resolve()).replace("\\", "/")


def escaped_filter_path(path):
    # ffmpeg filter option values need ':' escaped and the whole path quoted
    # so spaces (e.g. a name with spaces) don't break filter parsing.
    p = to_ffmpeg_path(path).replace(":", "\\:")
    return f"'{p}'"


def render_segment(image_path, duration, fps, out_path, zoom_in):
    if zoom_in:
        zoom_expr = "min(zoom+0.0015,1.3)"
    else:
        zoom_expr = "if(eq(on,0),1.3,max(zoom-0.0015,1.0))"

    vf = (
        f"scale={ZOOMPAN_CANVAS}:force_original_aspect_ratio=increase,"
        f"crop={ZOOMPAN_CANVAS},"
        f"zoompan=z='{zoom_expr}':d=1:"
        "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"s={OUTPUT_SIZE}:fps={fps},format=yuv420p"
    )
    args = [
        "-loop", "1",
        "-framerate", str(fps),
        "-i", str(image_path),
        "-t", f"{duration:.3f}",
        "-vf", vf,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        str(out_path),
    ]
    run_ffmpeg(args, description=f"rendering segment for {image_path.name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", help="Path under projects/, e.g. en/crumbs or es/test-crumbs")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--crf", type=int, default=20, help="Final encode quality (lower = better/larger)")
    parser.add_argument("--preset", default="medium", help="libx264 preset")
    parser.add_argument("--keep-tmp", action="store_true", help="Don't delete tmp/ segments after rendering")
    args = parser.parse_args()

    project_dir = PROJECTS_DIR / args.project
    scenes_path = project_dir / "scenes.json"
    images_dir = project_dir / "images"
    audio_path = project_dir / "audio.mp3"
    captions_path = project_dir / "captions.ass"
    output_path = project_dir / "output.mp4"
    tmp_dir = project_dir / "tmp"

    for required in (scenes_path, audio_path, captions_path):
        if not required.exists():
            sys.exit(f"Missing {required} -- run step1_audio_and_captions.py first.")
    if not images_dir.exists():
        sys.exit(f"Missing {images_dir} -- add your Google Flow images there first.")

    scenes = load_json(scenes_path)
    images = natural_sorted_images(images_dir)

    if len(images) != len(scenes):
        sys.exit(
            f"Scene/image count mismatch: scenes.json has {len(scenes)} scenes but "
            f"{images_dir} has {len(images)} images.\n"
            f"Check image_prompts.txt -- you need exactly one image per scene. Images are "
            f"matched to scenes purely by sorted filename order, so as long as each starts "
            f"with its zero-padded scene number (e.g. 001_..., 002_...) the rest of the name "
            f"doesn't matter -- but the suggested \"Save as:\" filename in image_prompts.txt "
            f"also encodes the scene's timestamp for your own reference."
        )

    tmp_dir.mkdir(exist_ok=True)
    print(f"Rendering {len(scenes)} Ken Burns segments...")
    segment_paths = []
    for i, (scene, image) in enumerate(zip(scenes, images)):
        seg_path = tmp_dir / f"seg_{i + 1:03d}.mp4"
        render_segment(image, scene["duration"], args.fps, seg_path, zoom_in=(i % 2 == 0))
        segment_paths.append(seg_path)
        print(f"  [{i + 1}/{len(scenes)}] {image.name} ({scene['duration']:.1f}s) -> {seg_path.name}")

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
        "-vf", ass_arg,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "libx264",
        "-preset", args.preset,
        "-crf", str(args.crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        str(output_path),
    ]
    run_ffmpeg(args_final, description="final concat + captions + audio mux")

    if not args.keep_tmp:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print(f"\nDone: {output_path}")


if __name__ == "__main__":
    main()
