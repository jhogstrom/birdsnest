#!/usr/bin/env python3
"""Stream an RTSP camera to YouTube Live via RTMPS.

Pipeline: RTSP (from camera) -> ffmpeg -> RTMPS (to YouTube).

Re-encodes video with libx264 to force a 2s keyframe interval (YouTube
Live requirement). If your camera already emits keyframes every 2s, you
can pass --copy-video for zero-CPU stream copy.

Audio: most nest cams have no usable audio. By default we mux a silent
AAC track so YouTube is happy. Pass --copy-audio if your camera emits
clean AAC and you want to keep it.

Reconnects with bounded exponential backoff on failure. SIGINT/SIGTERM
shut down cleanly.

Setup:
  Put YOUTUBE_STREAM_KEY=... in .env (alongside CAMSTREAM_LOCAL).
  Grab the key from YouTube Studio -> Go Live -> Stream settings.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

LOG = logging.getLogger("livestream")

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"
# Prefer a livestream-specific RTSP URL (e.g. camera substream) so we
# don't fight the timelapse capture for the single concurrent client
# slot some cameras allow on the main stream.
RTSP_ENV_VARS = ("CAMSTREAM_LIVESTREAM", "CAMSTREAM_LOCAL")
KEY_ENV_VAR = "YOUTUBE_STREAM_KEY"

YT_INGEST_PRIMARY = "rtmps://a.rtmp.youtube.com/live2"
YT_INGEST_BACKUP = "rtmps://b.rtmp.youtube.com/live2"

# Backoff: start at 2s, double on each failure, cap at 60s. Reset to base
# after the stream has been up for `BACKOFF_RESET_AFTER` seconds.
BACKOFF_BASE = 2.0
BACKOFF_MAX = 60.0
BACKOFF_RESET_AFTER = 120.0

# Strip user:pass@ from URLs in anything we log (ffmpeg may echo them on
# failure). Same helper as capture.py/record.py.
_CRED_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/@\s]+@")
# Redact the stream key segment of an RTMPS URL too -- it's effectively a
# bearer token for your live channel.
_STREAM_KEY_RE = re.compile(r"(rtmps?://[^/\s]+/live2?/)[^\s?]+")


def redact(text: str) -> str:
    if not text:
        return text
    text = _CRED_RE.sub(r"\1***@", text)
    text = _STREAM_KEY_RE.sub(r"\1***", text)
    return text


def probe_input_size(rtsp_url: str, timeout_s: int = 8) -> tuple[int, int] | None:
    """Return (width, height) of the first video stream, or None on failure.

    Used to sanity-check --crop before launching the encoder, so we can
    fail fast with a helpful message instead of letting ffmpeg crash mid-
    stream-attempt.
    """
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-loglevel",
                "error",
                "-rtsp_transport",
                "tcp",
                "-timeout",
                "5000000",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                rtsp_url,
            ],
            stderr=subprocess.PIPE,
            timeout=timeout_s,
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
        return None
    try:
        data = json.loads(out)
        s = data["streams"][0]
        return int(s["width"]), int(s["height"])
    except (KeyError, IndexError, ValueError, json.JSONDecodeError):
        return None


def validate_crop(crop: str, input_size: tuple[int, int] | None) -> None:
    """Raise ValueError if crop is malformed or wouldn't fit input_size."""
    parts = crop.split(":")
    if len(parts) != 4 or not all(p.lstrip("-").isdigit() for p in parts):
        raise ValueError(f"--crop must be W:H:X:Y (integers), got {crop!r}")
    w, h, x, y = (int(p) for p in parts)
    if w <= 0 or h <= 0:
        raise ValueError(f"--crop W and H must be positive, got {w}x{h}")
    if x < 0 or y < 0:
        raise ValueError(f"--crop X and Y must be non-negative, got x={x} y={y}")
    if input_size is None:
        # Couldn't probe; let ffmpeg be the final judge.
        return
    iw, ih = input_size
    if w + x > iw or h + y > ih:
        raise ValueError(
            f"--crop {crop} doesn't fit input {iw}x{ih} "
            f"(need x+w<={iw}, y+h<={ih}; got x+w={x + w}, y+h={y + h}). "
            "Tip: --stream main is 1080p, --stream sub is typically 640x360."
        )


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_ffmpeg_cmd(
    rtsp_url: str,
    ingest_url: str,
    copy_video: bool,
    copy_audio: bool,
    video_bitrate: str,
    keyint_seconds: int,
    fps: int,
    crop: str | None = None,
    zoom_to: str | None = None,
) -> list[str]:
    """Assemble the ffmpeg argv for one streaming attempt.

    `crop` is an ffmpeg crop expression: "W:H:X:Y" (e.g. "960:540:480:270"
    cuts a centered 960x540 window out of a 1920x1080 frame).

    `zoom_to` is a "WxH" target after the crop (e.g. "1920x1080" scales
    the cropped region back up to full HD). Cheap relative to the encode.
    Stream-copy modes (`copy_video=True`) can't crop/zoom.
    """
    cmd: list[str] = [
        "ffmpeg",
        "-loglevel",
        "warning",
        "-stats",
        # --- Input-side resilience. NOTE: ffmpeg's -reconnect family is
        # HTTP-only and refuses to open an RTSP input ("Operation not
        # permitted") if set. RTSP reconnection is handled by our outer
        # wrapper loop instead. ---
        "-rtsp_transport",
        "tcp",
        # ffmpeg >=5 renamed -stimeout to -timeout on the RTSP demuxer.
        "-timeout",
        "10000000",  # microseconds: 10s socket timeout
        "-use_wallclock_as_timestamps",
        "1",
        "-fflags",
        "+genpts",
        "-i",
        rtsp_url,
    ]

    if not copy_audio:
        # Synth silent stereo so YouTube has an audio track to mux.
        cmd += [
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
        ]

    # --- Video ---
    if copy_video:
        if crop or zoom_to:
            raise ValueError("--crop / --zoom-to require re-encode; drop --copy-video")
        cmd += ["-c:v", "copy"]
    else:
        gop = max(1, fps * keyint_seconds)
        vf_parts: list[str] = []
        if crop:
            vf_parts.append(f"crop={crop}")
        if zoom_to:
            w, _, h = zoom_to.lower().partition("x")
            if not (w.isdigit() and h.isdigit()):
                raise ValueError(f"--zoom-to must be WxH, got {zoom_to!r}")
            # Force even dimensions; yuv420p requires it.
            vf_parts.append(f"scale={w}:{h}:flags=lanczos,setsar=1")
        cmd += [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-g",
            str(gop),
            "-keyint_min",
            str(gop),
            "-sc_threshold",
            "0",  # disable scene-cut keyframes; we want a strict GOP
            "-force_key_frames",
            f"expr:gte(t,n_forced*{keyint_seconds})",
            "-b:v",
            video_bitrate,
            "-maxrate",
            video_bitrate,
            "-bufsize",
            video_bitrate,  # tight bufsize keeps RTMP happy
        ]
        if vf_parts:
            cmd += ["-vf", ",".join(vf_parts)]

    # --- Audio ---
    if copy_audio:
        cmd += ["-c:a", "copy"]
    else:
        cmd += [
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-shortest",  # bounded by the RTSP input, not the silent source
        ]

    cmd += [
        "-f",
        "flv",
        ingest_url,
    ]
    return cmd


def stream_once(cmd: list[str], stop_flag) -> int:
    """Run ffmpeg once, streaming stderr through the logger with redaction.

    Returns ffmpeg's exit code, or -1 if we killed it via stop_flag.
    """
    LOG.info("Starting ffmpeg: %s", redact(" ".join(cmd)))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    assert proc.stderr is not None
    try:
        while True:
            if stop_flag():
                LOG.info("Stop requested; terminating ffmpeg.")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
                return -1
            line = proc.stderr.readline()
            if not line:
                # EOF on stderr -> ffmpeg has (or is about to) exit.
                break
            LOG.info("ffmpeg: %s", redact(line.rstrip()))
        return proc.wait()
    finally:
        # Make sure we never leak a child process.
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--rtsp-url",
        default=None,
        help=(
            "RTSP URL. Overrides --stream / env. Defaults: first set of $"
            + ", $".join(RTSP_ENV_VARS)
            + " from .env."
        ),
    )
    p.add_argument(
        "--stream",
        choices=("main", "sub"),
        default=None,
        help=(
            "Convenience: pick the camera's main (high-res) or sub (low-res) "
            "stream. Rewrites the trailing /stream1 or /stream2 on the "
            "resolved RTSP URL. Useful when something else is already holding "
            "the main stream's single client slot."
        ),
    )
    p.add_argument(
        "--stream-key",
        default=None,
        help=f"YouTube stream key (defaults to ${KEY_ENV_VAR} from .env).",
    )
    p.add_argument(
        "--ingest-url",
        default=YT_INGEST_PRIMARY,
        help=(
            f"Base ingest URL, key is appended (default: {YT_INGEST_PRIMARY}). "
            f"Backup: {YT_INGEST_BACKUP}"
        ),
    )
    p.add_argument(
        "--copy-video",
        action="store_true",
        help="Stream-copy video (zero CPU). Only safe if the camera emits "
        "keyframes every ~2s; otherwise YouTube playback will be janky.",
    )
    p.add_argument(
        "--copy-audio",
        action="store_true",
        help="Stream-copy camera audio instead of muxing silence. Use only "
        "if the camera emits AAC.",
    )
    p.add_argument(
        "--bitrate",
        default="3500k",
        help="Video bitrate when re-encoding (default: 3500k - good for 1080p30).",
    )
    p.add_argument(
        "--fps",
        type=int,
        default=30,
        help="Output FPS when re-encoding (default: 30).",
    )
    p.add_argument(
        "--keyint-seconds",
        type=int,
        default=2,
        help="Keyframe interval in seconds (default: 2 - YouTube's sweet spot).",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Run a single ffmpeg attempt and exit; do not auto-reconnect.",
    )
    p.add_argument(
        "--crop",
        default=None,
        help=(
            "Crop the input before encoding. Format: W:H:X:Y (ffmpeg crop "
            "filter syntax). Example: '960:540:480:270' takes a centered "
            "960x540 window from a 1920x1080 stream. Cheap; pairs well with "
            "--zoom-to to fill the output frame with the cropped region."
        ),
    )
    p.add_argument(
        "--zoom-to",
        default=None,
        help=(
            "After cropping (or instead of), scale the video to this WxH. "
            "Example: --crop 960:540:480:270 --zoom-to 1920x1080 zooms the "
            "centered region back to full HD. Encode cost is set by the "
            "output size + bitrate, not the input."
        ),
    )
    return p.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = parse_args()

    load_env_file(ENV_FILE)
    rtsp_url = args.rtsp_url
    if not rtsp_url:
        for var in RTSP_ENV_VARS:
            if os.environ.get(var):
                rtsp_url = os.environ[var]
                LOG.info("Using RTSP URL from $%s.", var)
                break
    if not rtsp_url:
        LOG.error(
            "No RTSP URL (--rtsp-url or one of %s in .env).",
            ", ".join(RTSP_ENV_VARS),
        )
        return 2

    if args.stream:
        # Swap the trailing /streamN segment. Most RTSP cams (TP-Link, Reolink,
        # Hikvision substreams) follow this convention; if yours doesn't, pass
        # --rtsp-url explicitly.
        target = "/stream1" if args.stream == "main" else "/stream2"
        new_url = re.sub(r"/stream[12]\b", target, rtsp_url)
        if new_url == rtsp_url:
            LOG.warning(
                "--stream %s requested but URL has no /stream1|/stream2 "
                "segment to rewrite; using as-is.",
                args.stream,
            )
        else:
            LOG.info("Rewrote RTSP URL for --stream %s.", args.stream)
            rtsp_url = new_url
    stream_key = args.stream_key or os.environ.get(KEY_ENV_VAR)
    if not stream_key:
        LOG.error("No YouTube stream key (--stream-key or %s in .env).", KEY_ENV_VAR)
        return 2

    ingest_url = f"{args.ingest_url.rstrip('/')}/{stream_key}"

    if args.crop:
        # Probe input dimensions so we can give a clear error if the user's
        # crop region doesn't fit. Probing is best-effort; if ffprobe is
        # missing or times out, we still let ffmpeg attempt the stream.
        input_size = probe_input_size(rtsp_url)
        if input_size:
            LOG.info("Input video size: %dx%d.", *input_size)
        else:
            LOG.warning("Could not probe input size; skipping crop pre-check.")
        try:
            validate_crop(args.crop, input_size)
        except ValueError as e:
            LOG.error("%s", e)
            return 2

    cmd = build_ffmpeg_cmd(
        rtsp_url=rtsp_url,
        ingest_url=ingest_url,
        copy_video=args.copy_video,
        copy_audio=args.copy_audio,
        video_bitrate=args.bitrate,
        keyint_seconds=args.keyint_seconds,
        fps=args.fps,
        crop=args.crop,
        zoom_to=args.zoom_to,
    )

    stop = False

    def _handle_signal(signum, _frame):
        nonlocal stop
        LOG.info("Received signal %s, shutting down.", signum)
        stop = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    LOG.info(
        "Streaming %s -> YouTube Live (%s mode, key redacted).",
        redact(rtsp_url),
        "stream-copy" if args.copy_video else "re-encode",
    )

    backoff = BACKOFF_BASE
    attempt = 0
    while not stop:
        attempt += 1
        started = time.monotonic()
        rc = stream_once(cmd, stop_flag=lambda: stop)
        ran_for = time.monotonic() - started

        if stop or rc == -1:
            LOG.info("Clean shutdown after attempt %d.", attempt)
            return 0
        if args.once:
            LOG.info("--once set; exiting with rc=%s after %.1fs.", rc, ran_for)
            return rc if rc is not None else 1

        # Reset backoff if the stream was up long enough to count as stable.
        if ran_for >= BACKOFF_RESET_AFTER:
            backoff = BACKOFF_BASE
            LOG.info(
                "Stream ran %.0fs before failing (rc=%s). Resetting backoff.",
                ran_for,
                rc,
            )
        else:
            LOG.warning(
                "Stream failed after %.1fs (rc=%s). Reconnecting in %.1fs.",
                ran_for,
                rc,
                backoff,
            )

        # Sleep in small chunks so signals are responsive.
        end = time.monotonic() + backoff
        while not stop and time.monotonic() < end:
            time.sleep(min(0.5, end - time.monotonic()))
        backoff = min(BACKOFF_MAX, backoff * 2)

    return 0


if __name__ == "__main__":
    sys.exit(main())
