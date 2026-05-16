#!/usr/bin/env python3
"""Record the RTSP stream directly to a video file using ffmpeg."""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

LOG = logging.getLogger("record")

# Strip user:pass@ from URLs in anything we log (ffmpeg likes to echo the
# full URL on failures).
_CRED_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/@\s]+@")


def redact(text: str) -> str:
    if not text:
        return text
    return _CRED_RE.sub(r"\1***@", text)


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = REPO_ROOT / "output"
ENV_FILE = REPO_ROOT / ".env"
ENV_VAR = "CAMSTREAM_LOCAL"
DEFAULT_DURATION = 15


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--duration",
        type=int,
        default=DEFAULT_DURATION,
        help=f"Capture duration in seconds (default: {DEFAULT_DURATION}).",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output video path (default: ./output/clip_<timestamp>.mp4).",
    )
    p.add_argument(
        "--rtsp-url", default=None, help=f"RTSP URL (defaults to ${ENV_VAR} from .env)."
    )
    p.add_argument(
        "--reencode",
        action="store_true",
        help="Re-encode with libx264 instead of stream copy.",
    )
    p.add_argument(
        "--crf",
        type=int,
        default=20,
        help="x264 CRF when --reencode is used (default: 20).",
    )
    p.add_argument(
        "--no-audio",
        action="store_true",
        help="Drop audio (default: transcode audio to AAC for MP4 compatibility).",
    )
    return p.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = parse_args()

    if args.duration <= 0:
        LOG.error("--duration must be > 0")
        return 2

    load_env_file(ENV_FILE)
    rtsp_url = args.rtsp_url or os.environ.get(ENV_VAR)
    if not rtsp_url:
        LOG.error("No RTSP URL supplied (--rtsp-url or %s in .env).", ENV_VAR)
        return 2

    if args.output is None:
        DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.output = DEFAULT_OUTPUT_DIR / f"clip_{ts}_{args.duration}s.mp4"
    args.output.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-stats",
        "-rtsp_transport",
        "tcp",
        "-i",
        rtsp_url,
        "-t",
        str(args.duration),
    ]
    if args.reencode:
        cmd += [
            "-c:v",
            "libx264",
            "-crf",
            str(args.crf),
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
        ]
    else:
        # Video stream copy: no re-encoding, fastest, preserves quality.
        cmd += ["-c:v", "copy"]

    if args.no_audio:
        cmd += ["-an"]
    else:
        # Camera often emits PCM A-law which MP4 can't carry; transcode to AAC.
        cmd += ["-c:a", "aac", "-b:a", "128k"]

    cmd.append(str(args.output))

    LOG.info("Recording %ds -> %s", args.duration, args.output)
    # Capture stderr so we can redact RTSP credentials before logging it.
    # Generous wall-clock timeout: duration + 30s slack for connect/finalize.
    try:
        result = subprocess.run(
            cmd,
            timeout=args.duration + 30,
            check=False,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        LOG.error("ffmpeg timed out.")
        return 1

    if result.returncode != 0:
        LOG.error(
            "ffmpeg failed (rc=%s): %s",
            result.returncode,
            redact(result.stderr.strip()),
        )
        return result.returncode

    LOG.info("Done: %s", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
