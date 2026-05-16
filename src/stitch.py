#!/usr/bin/env python3
"""Stitch captured frames between two timestamps into a video using ffmpeg.

Exposes importable helpers used by capture.py to build a session timelapse:

  - build_timelapse(frames, output, fps, crf, intro=True, ...)
  - build_realtime(frames, output, crf, intro=True, ...)
  - make_intro_image(path, title, timeframe_line, info_line, size)
"""

from __future__ import annotations

import argparse
import logging
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

LOG = logging.getLogger("stitch")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT_DIR = REPO_ROOT / "output"
DEFAULT_VIDEO_DIR = REPO_ROOT / "output"

FRAME_RE = re.compile(r"frame_(\d{8}_\d{6})(?:_(\d{3}))?\.jpg$")
TS_FMT = "%Y%m%d_%H%M%S"

INTRO_DURATION_DEFAULT = 2.0
COPYRIGHT_TEXT = "(c) Jesper Hogstrom"  # ASCII-safe for drawtext


# ---------------------------------------------------------------------------
# Filename / timestamp helpers
# ---------------------------------------------------------------------------


def parse_timestamp_from_name(name: str) -> datetime:
    m = FRAME_RE.search(name)
    if not m:
        raise ValueError(
            f"Filename does not match frame_YYYYMMDD_HHMMSS[_mmm].jpg: {name}"
        )
    ts = datetime.strptime(m.group(1), TS_FMT)
    if m.group(2):
        ts = ts.replace(microsecond=int(m.group(2)) * 1000)
    return ts


def collect_frames(
    input_dir: Path, start: datetime, end: datetime
) -> list[tuple[datetime, Path]]:
    frames: list[tuple[datetime, Path]] = []
    for path in input_dir.glob("frame_*.jpg"):
        try:
            ts = parse_timestamp_from_name(path.name)
        except ValueError:
            continue
        if start <= ts <= end:
            frames.append((ts, path))
    frames.sort(key=lambda x: x[0])
    return frames


def resolve_frame(arg: str, input_dir: Path) -> Path:
    p = Path(arg)
    if not p.is_absolute() and not p.exists():
        p = input_dir / p.name
    if not p.exists():
        raise FileNotFoundError(f"Frame not found: {arg}")
    return p


# ---------------------------------------------------------------------------
# Intro card
# ---------------------------------------------------------------------------


def _drawtext_escape(text: str) -> str:
    """Escape a string for use inside a drawtext filter `text='...'` value."""
    text = text.replace("\\", "\\\\")
    text = text.replace(":", r"\:")
    text = text.replace("'", r"\'")
    text = text.replace("%", r"\%")
    return text


def _probe_image_size(path: Path) -> tuple[int, int]:
    """Return (width, height) of an image using ffprobe."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=p=0:s=x",
        str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
    w, h = out.split("x")
    return int(w), int(h)


def make_intro_image(
    out_path: Path,
    title: str,
    timeframe_line: str,
    info_line: str,
    size: tuple[int, int] = (1920, 1080),
) -> Path:
    """Render a single PNG intro card via ffmpeg's drawtext filter."""
    w, h = size

    title_size = max(36, h // 14)
    line_size = max(24, h // 26)
    copy_size = max(18, h // 50)

    filters = [
        f"drawtext=font=Sans:fontcolor=white:fontsize={title_size}:"
        f"text='{_drawtext_escape(title)}':"
        f"x=(w-text_w)/2:y=h*0.30",
        f"drawtext=font=Sans:fontcolor=white:fontsize={line_size}:"
        f"text='{_drawtext_escape(timeframe_line)}':"
        f"x=(w-text_w)/2:y=h*0.48",
        f"drawtext=font=Sans:fontcolor=white:fontsize={line_size}:"
        f"text='{_drawtext_escape(info_line)}':"
        f"x=(w-text_w)/2:y=h*0.58",
        f"drawtext=font=Sans:fontcolor=white:fontsize={copy_size}:"
        f"text='{_drawtext_escape(COPYRIGHT_TEXT)}':"
        f"x=w-text_w-30:y=h-text_h-25",
    ]

    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={w}x{h}:d=1",
        "-vf",
        ",".join(filters),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(out_path),
    ]
    LOG.debug("intro ffmpeg: %s", " ".join(shlex.quote(c) for c in cmd))
    subprocess.run(cmd, check=True)
    return out_path


def _format_seconds(secs: float) -> str:
    if secs < 60:
        return f"{secs:.1f}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{int(m)}m {int(s)}s"
    h, m = divmod(m, 60)
    return f"{int(h)}h {int(m)}m"


def _build_intro_for(
    frames: list[tuple[datetime, Path]],
    workdir: Path,
    title: str = "Birdsnest Timelapse",
) -> Path:
    """Generate an intro card sized to the first frame."""
    first_ts, first_path = frames[0]
    last_ts = frames[-1][0]
    span_s = (last_ts - first_ts).total_seconds()
    if len(frames) > 1 and span_s > 0:
        avg_interval = span_s / (len(frames) - 1)
        interval_text = f"avg {_format_seconds(avg_interval)} between captures"
    else:
        interval_text = "single frame"

    timeframe_line = (
        f"{first_ts.strftime('%Y-%m-%d %H:%M:%S')}  ->  "
        f"{last_ts.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    info_line = f"{len(frames)} frames | {interval_text}"

    try:
        size = _probe_image_size(first_path)
    except Exception:
        size = (1920, 1080)

    intro_path = workdir / "intro.jpg"
    return make_intro_image(intro_path, title, timeframe_line, info_line, size=size)


# ---------------------------------------------------------------------------
# Concat builders
# ---------------------------------------------------------------------------


def _write_concat_list(
    list_path: Path,
    entries: Iterable[tuple[Path, float | None]],
) -> None:
    """Write a concat-demuxer file. entries: (path, duration_or_None)."""
    with list_path.open("w") as f:
        for path, duration in entries:
            f.write(f"file '{path.resolve().as_posix()}'\n")
            if duration is not None:
                f.write(f"duration {duration:.6f}\n")


def _normalize_frames(
    frames: list[tuple[datetime, Path]] | list[Path],
) -> list[tuple[datetime | None, Path]]:
    out: list[tuple[datetime | None, Path]] = []
    for item in frames:
        if isinstance(item, tuple):
            out.append(item)
        else:
            try:
                ts = parse_timestamp_from_name(item.name)
            except ValueError:
                ts = None
            out.append((ts, item))
    return out


def build_timelapse(
    frames: list[tuple[datetime, Path]] | list[Path],
    output: Path,
    fps: float = 24.0,
    crf: int = 20,
    intro: bool = True,
    intro_seconds: float = INTRO_DURATION_DEFAULT,
    title: str = "Birdsnest Timelapse",
) -> Path:
    """Constant-FPS timelapse: each captured image becomes 1 video frame."""
    normalized = _normalize_frames(frames)
    if not normalized:
        raise ValueError("No frames provided to build_timelapse.")

    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="stitch-") as tmpdir:
        tmp = Path(tmpdir)
        entries: list[tuple[Path, float | None]] = []

        if intro and intro_seconds > 0 and all(ts is not None for ts, _ in normalized):
            intro_path = _build_intro_for(
                [(ts, p) for ts, p in normalized],  # type: ignore[misc]
                workdir=tmp,
                title=title,
            )
            entries.append((intro_path, intro_seconds))

        per_frame = 1.0 / fps
        for _, img in normalized:
            entries.append((img, per_frame))
        entries.append((normalized[-1][1], None))

        list_path = tmp / "list.txt"
        _write_concat_list(list_path, entries)

        cmd = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-stats",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-fps_mode",
            "cfr",
            "-r",
            str(fps),
            "-pix_fmt",
            "yuv420p",
            "-vf",
            "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v",
            "libx264",
            "-crf",
            str(crf),
            "-preset",
            "medium",
            str(output),
        ]
        LOG.info("Encoding timelapse @ %sfps -> %s", fps, output)
        subprocess.run(cmd, check=True)
    return output


def build_realtime(
    frames: list[tuple[datetime, Path]],
    output: Path,
    crf: int = 20,
    intro: bool = True,
    intro_seconds: float = INTRO_DURATION_DEFAULT,
    title: str = "Birdsnest Timelapse",
) -> Path:
    """Real-time: each frame held for the actual capture gap to the next."""
    if not frames:
        raise ValueError("No frames provided to build_realtime.")
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="stitch-") as tmpdir:
        tmp = Path(tmpdir)
        entries: list[tuple[Path, float | None]] = []

        if intro and intro_seconds > 0:
            intro_path = _build_intro_for(frames, workdir=tmp, title=title)
            entries.append((intro_path, intro_seconds))

        for i, (ts, img) in enumerate(frames):
            if i < len(frames) - 1:
                gap = (frames[i + 1][0] - ts).total_seconds()
                gap = max(gap, 1.0 / 60.0)
                entries.append((img, gap))
            else:
                entries.append((img, 1.0))
        entries.append((frames[-1][1], None))

        list_path = tmp / "list.txt"
        _write_concat_list(list_path, entries)

        cmd = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-stats",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-pix_fmt",
            "yuv420p",
            "-vf",
            "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v",
            "libx264",
            "-crf",
            str(crf),
            "-preset",
            "medium",
            str(output),
        ]
        LOG.info("Encoding real-time video -> %s", output)
        subprocess.run(cmd, check=True)
    return output


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cleanup_frames(
    frames: list[tuple[datetime, Path]] | list[Path],
) -> int:
    """Delete the source image files used to build a video.

    Accepts the same shapes as the build_* functions. Missing files are
    ignored. Returns the number of files actually removed.
    """
    removed = 0
    for item in frames:
        path = item[1] if isinstance(item, tuple) else item
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            pass
        except OSError as exc:
            LOG.warning("Could not delete %s: %s", path, exc)
    LOG.info("Removed %d source image(s).", removed)
    return removed


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stitch frames into a video.")
    p.add_argument("image1", help="First frame (filename or path).")
    p.add_argument("image2", help="Last frame (filename or path).")
    p.add_argument(
        "--mode",
        choices=["timelapse", "realtime"],
        default="timelapse",
        help="timelapse (default): constant FPS, each image = 1 frame. "
        "realtime: each frame held for its real capture gap.",
    )
    p.add_argument(
        "--fps",
        type=float,
        default=24.0,
        help="Output FPS for timelapse mode (default: 24).",
    )
    p.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"Where to look up frames (default: {DEFAULT_INPUT_DIR}).",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output video path (default: ./output/<mode>_<start>_<end>.mp4).",
    )
    p.add_argument(
        "--crf", type=int, default=20, help="x264 CRF quality (default: 20)."
    )
    p.add_argument("--no-intro", action="store_true", help="Skip the title card.")
    p.add_argument(
        "--intro-seconds",
        type=float,
        default=INTRO_DURATION_DEFAULT,
        help=f"Intro card duration (default: {INTRO_DURATION_DEFAULT}s).",
    )
    p.add_argument("--title", default="Birdsnest Timelapse", help="Intro card title.")
    p.add_argument(
        "--keep-images",
        action="store_true",
        help="Keep source frame images after stitching (default: delete them).",
    )
    return p.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = parse_args()

    img1 = resolve_frame(args.image1, args.input_dir)
    img2 = resolve_frame(args.image2, args.input_dir)
    ts1 = parse_timestamp_from_name(img1.name)
    ts2 = parse_timestamp_from_name(img2.name)
    if ts2 < ts1:
        ts1, ts2 = ts2, ts1

    frames = collect_frames(args.input_dir, ts1, ts2)
    if not frames:
        LOG.error("No frames found between %s and %s in %s", ts1, ts2, args.input_dir)
        return 1
    LOG.info("Found %d frames from %s to %s", len(frames), ts1, ts2)

    if args.output is None:
        DEFAULT_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
        tag = "rt" if args.mode == "realtime" else f"{int(args.fps)}fps"
        args.output = DEFAULT_VIDEO_DIR / (
            f"{args.mode}_{ts1.strftime(TS_FMT)}_{ts2.strftime(TS_FMT)}_{tag}.mp4"
        )

    intro = not args.no_intro
    if args.mode == "timelapse":
        build_timelapse(
            frames,
            args.output,
            fps=args.fps,
            crf=args.crf,
            intro=intro,
            intro_seconds=args.intro_seconds,
            title=args.title,
        )
    else:
        build_realtime(
            frames,
            args.output,
            crf=args.crf,
            intro=intro,
            intro_seconds=args.intro_seconds,
            title=args.title,
        )

    if not args.keep_images:
        cleanup_frames(frames)

    LOG.info("Done: %s", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
