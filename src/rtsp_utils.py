"""Shared helpers for the RTSP-using scripts (capture, record, livestream).

Right now this is just credential redaction + input size probe + crop
validation. Kept minimal on purpose - we don't need an SDK here, just
the small set of things that were starting to get duplicated.
"""

from __future__ import annotations

import json
import re
import subprocess

# Matches the "user:pass@" segment of any URL so we can scrub credentials
# from anything we log (ffmpeg likes to echo the full URL on failures).
_CRED_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/@\s]+@")
# Stream key segment of a YouTube/RTMPS URL - effectively a bearer token.
_STREAM_KEY_RE = re.compile(r"(rtmps?://[^/\s]+/live2?/)[^\s?]+")


def redact(text: str) -> str:
    """Strip user:pass and RTMPS stream keys from any text before logging."""
    if not text:
        return text
    text = _CRED_RE.sub(r"\1***@", text)
    text = _STREAM_KEY_RE.sub(r"\1***", text)
    return text


def probe_input_size(rtsp_url: str, timeout_s: int = 8) -> tuple[int, int] | None:
    """Return (width, height) of the first video stream, or None on failure.

    Best-effort: used to validate crop arguments before launching the
    encoder. If ffprobe is missing or times out, callers should fall
    back to letting ffmpeg fail at runtime (with a less helpful message).
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


def validate_crop(
    crop: str, input_size: tuple[int, int] | None, label: str = "--crop"
) -> None:
    """Raise ValueError if crop is malformed or wouldn't fit input_size.

    `crop` is an ffmpeg crop expression: W:H:X:Y.
    `label` is included in the error message so callers passing
    --motion-crop get the right name in the diagnostic.
    """
    parts = crop.split(":")
    if len(parts) != 4 or not all(p.lstrip("-").isdigit() for p in parts):
        raise ValueError(f"{label} must be W:H:X:Y (integers), got {crop!r}")
    w, h, x, y = (int(p) for p in parts)
    if w <= 0 or h <= 0:
        raise ValueError(f"{label} W and H must be positive, got {w}x{h}")
    if x < 0 or y < 0:
        raise ValueError(f"{label} X and Y must be non-negative, got x={x} y={y}")
    if input_size is None:
        return
    iw, ih = input_size
    if w + x > iw or h + y > ih:
        raise ValueError(
            f"{label} {crop} doesn't fit input {iw}x{ih} "
            f"(need x+w<={iw}, y+h<={ih}; got x+w={x + w}, y+h={y + h}). "
            "Tip: --stream main is 1080p, --stream sub is typically 640x360."
        )
