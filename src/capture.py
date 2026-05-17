#!/usr/bin/env python3
"""Capture still images from an RTSP camera stream at a fixed interval."""

from __future__ import annotations

import argparse
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import stitch
from rtsp_utils import probe_input_size, redact, validate_crop

LOG = logging.getLogger("capture")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = REPO_ROOT / "output"
ENV_FILE = REPO_ROOT / ".env"
ENV_VAR = "CAMSTREAM_LOCAL"


def load_env_file(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, no quotes/expansion required."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def capture_frame(
    rtsp_url: str, output_path: Path, timeout: int, crop: str | None = None
) -> bool:
    """Grab a single frame from the RTSP stream using ffmpeg.

    `crop` is an ffmpeg crop expression "W:H:X:Y" applied before the JPEG
    is written. None = full frame.
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-i",
        rtsp_url,
        "-frames:v",
        "1",
        "-q:v",
        "2",
    ]
    if crop:
        cmd += ["-vf", f"crop={crop}"]
    cmd.append(str(output_path))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        LOG.error("ffmpeg timed out after %ss", timeout)
        return False

    if result.returncode != 0:
        LOG.error(
            "ffmpeg failed (rc=%s): %s",
            result.returncode,
            redact(result.stderr.strip()),
        )
        return False
    return True


def capture_fingerprint(
    rtsp_url: str, size: int, timeout: int, crop: str | None = None
) -> bytes | None:
    """Grab one frame, downscaled to `size`x`size` grayscale raw bytes.

    Used for cheap motion detection. Returns None on failure. If `crop`
    is given, motion is measured on the cropped region only - useful for
    ignoring background activity outside the nest.
    """
    vf = f"scale={size}:{size},format=gray"
    if crop:
        vf = f"crop={crop},{vf}"
    cmd = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-i",
        rtsp_url,
        "-frames:v",
        "1",
        "-vf",
        vf,
        "-f",
        "rawvideo",
        "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        LOG.warning("fingerprint ffmpeg timed out after %ss", timeout)
        return None
    if result.returncode != 0 or len(result.stdout) != size * size:
        LOG.warning(
            "fingerprint failed (rc=%s, bytes=%d): %s",
            result.returncode,
            len(result.stdout),
            redact(result.stderr.decode("utf-8", errors="replace").strip()[:200]),
        )
        return None
    return result.stdout


def mean_abs_diff(a: bytes, b: bytes) -> float:
    """Mean absolute pixel difference of two equal-length grayscale buffers."""
    if len(a) != len(b) or not a:
        return 0.0
    total = 0
    for x, y in zip(a, b, strict=True):
        total += x - y if x >= y else y - x
    return total / len(a)


class BurstCapture:
    """Persistent RTSP -> JPEG extractor for high-rate capture windows.

    Runs a single long-lived ffmpeg child that writes sequential JPEGs into
    a scratch dir at a fixed fps. A poller thread harvests new files,
    renames them to `frame_YYYYMMDD_HHMMSS_mmm.jpg` in the output dir, and
    appends (timestamp, path) tuples to the shared session list.

    Designed to be started on motion and stopped when the active window
    closes. Cheaper than respawning ffmpeg per frame because the RTSP
    connection stays warm.
    """

    POLL_INTERVAL = 0.2
    HARVEST_RE = re.compile(r"^burst_(\d+)\.jpg$")

    def __init__(
        self,
        rtsp_url: str,
        output_dir: Path,
        fps: float,
        session_frames: list[tuple[datetime, Path]],
        on_frame: Callable[[datetime, Path], None] | None = None,
        crop: str | None = None,
    ) -> None:
        self.rtsp_url = rtsp_url
        self.output_dir = output_dir
        self.fps = fps
        self.session_frames = session_frames
        self.on_frame = on_frame
        self.crop = crop

        self._proc: subprocess.Popen | None = None
        self._scratch: Path | None = None
        self._poller: threading.Thread | None = None
        self._stop = threading.Event()
        self._harvested = 0

    @property
    def active(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self.active:
            return
        self._stop.clear()
        self._harvested = 0
        self._scratch = self.output_dir / ".burst"
        if self._scratch.exists():
            for p in self._scratch.glob("burst_*.jpg"):
                try:
                    p.unlink()
                except OSError:
                    pass
        self._scratch.mkdir(parents=True, exist_ok=True)

        pattern = str(self._scratch / "burst_%06d.jpg")
        vf = f"fps={self.fps}"
        if self.crop:
            vf = f"crop={self.crop},{vf}"
        cmd = [
            "ffmpeg",
            "-loglevel",
            "error",
            "-rtsp_transport",
            "tcp",
            "-i",
            self.rtsp_url,
            "-vf",
            vf,
            "-q:v",
            "2",
            "-f",
            "image2",
            pattern,
        ]
        LOG.info("Burst capture starting @ %.1f fps", self.fps)
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self._poller = threading.Thread(target=self._poll_loop, daemon=True)
        self._poller.start()

    def stop(self) -> None:
        if not self._proc:
            return
        self._stop.set()
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=2)
        if self._poller:
            self._poller.join(timeout=2)
        # Final sweep in case frames landed between last poll and exit.
        self._harvest_once(final=True)
        if self._scratch and self._scratch.exists():
            try:
                self._scratch.rmdir()
            except OSError:
                # Non-empty (unexpected) - leave it for the user to inspect.
                pass
        LOG.info("Burst capture stopped (%d frame(s) harvested).", self._harvested)
        self._proc = None
        self._poller = None
        self._scratch = None

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            self._harvest_once()
            self._stop.wait(self.POLL_INTERVAL)

    def _harvest_once(self, final: bool = False) -> None:
        if not self._scratch:
            return
        # Sort by sequence number so timestamps stay monotonic.
        candidates: list[tuple[int, Path]] = []
        for p in self._scratch.glob("burst_*.jpg"):
            m = self.HARVEST_RE.match(p.name)
            if not m:
                continue
            candidates.append((int(m.group(1)), p))
        candidates.sort(key=lambda x: x[0])
        # Skip the most recent file unless final - ffmpeg may still be writing it.
        if not final and candidates:
            candidates = candidates[:-1]
        for _, src in candidates:
            now = datetime.now()
            name = (
                f"frame_{now.strftime('%Y%m%d_%H%M%S')}"
                f"_{now.microsecond // 1000:03d}.jpg"
            )
            dst = self.output_dir / name
            try:
                src.rename(dst)
            except OSError as exc:
                LOG.warning("Could not harvest %s: %s", src.name, exc)
                continue
            self._harvested += 1
            self.session_frames.append((now, dst))
            if self.on_frame is not None:
                try:
                    self.on_frame(now, dst)
                except Exception as exc:  # noqa: BLE001
                    LOG.debug("on_frame callback raised: %s", exc)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--interval",
        type=float,
        required=True,
        help="Seconds between captures.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Where to write images (default: {DEFAULT_OUTPUT_DIR}).",
    )
    parser.add_argument(
        "--rtsp-url",
        type=str,
        default=None,
        help=f"RTSP URL (defaults to ${ENV_VAR} from .env).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=15,
        help="Per-capture ffmpeg timeout in seconds (default: 15).",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=0,
        help="Stop after N interval/heartbeat captures. Motion, cooldown, "
        "and burst frames do not count. 0 = unlimited (default).",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="Stop after this many wall-clock seconds (any save type). "
        "0 = run forever (default).",
    )
    parser.add_argument(
        "--no-video",
        action="store_true",
        help="Skip building a session timelapse video on exit.",
    )
    parser.add_argument(
        "--video-fps",
        type=float,
        default=24.0,
        help="FPS for the session timelapse video (default: 24).",
    )
    parser.add_argument(
        "--video-title",
        default="Birdsnest Timelapse",
        help="Title shown on the intro card.",
    )
    parser.add_argument(
        "--keep-images",
        action="store_true",
        help="Keep captured frame images after the session video is built "
        "(default: delete them).",
    )
    parser.add_argument(
        "--motion",
        action="store_true",
        help="Enable motion-aware capture: peek every --peek-interval, save a frame "
        "either on detected motion or as a heartbeat every --interval seconds.",
    )
    parser.add_argument(
        "--peek-interval",
        type=float,
        default=1.0,
        help="Motion mode: seconds between motion checks (default: 1.0).",
    )
    parser.add_argument(
        "--motion-threshold",
        type=float,
        default=3.0,
        help="Motion mode: mean abs pixel diff (0-255) to trigger a save "
        "from the idle baseline (default: 3.0). Lower = more sensitive.",
    )
    parser.add_argument(
        "--motion-threshold-active",
        type=float,
        default=1.5,
        help="Motion mode: lower threshold used during the active window "
        "after recent motion, so small follow-up movements (e.g. a bird "
        "settling on eggs) still trigger saves (default: 1.5).",
    )
    parser.add_argument(
        "--motion-active-window",
        type=float,
        default=120.0,
        help="Motion mode: seconds after the last detected motion during "
        "which --motion-threshold-active applies. Each new motion resets "
        "the window (default: 120).",
    )
    parser.add_argument(
        "--motion-cooldown",
        type=int,
        default=3,
        help="Motion mode: keep saving every peek for N peeks after last motion, "
        "regardless of score (default: 3).",
    )
    parser.add_argument(
        "--fingerprint-size",
        type=int,
        default=64,
        help="Motion mode: downscale dimension for fingerprint (default: 64).",
    )
    parser.add_argument(
        "--log-motion-scores",
        action="store_true",
        help="Motion mode: log the diff score on every peek (for tuning).",
    )
    parser.add_argument(
        "--burst",
        action="store_true",
        help="Motion mode: when motion is detected, open a persistent RTSP "
        "connection and harvest frames at --burst-fps for the duration of "
        "the active window. Captures sub-second nest activity.",
    )
    parser.add_argument(
        "--burst-fps",
        type=float,
        default=5.0,
        help="Frames per second for --burst mode (default: 5.0).",
    )
    parser.add_argument(
        "--crop",
        default=None,
        help=(
            "Crop saved frames (and burst frames) to this region. Format: "
            "W:H:X:Y (ffmpeg crop syntax). Example for /stream1 (1920x1080): "
            "'800:800:600:200' keeps an 800x800 window starting at x=600,y=200. "
            "Useful for focusing the output on the nest and trimming "
            "uninteresting surroundings."
        ),
    )
    parser.add_argument(
        "--motion-crop",
        default=None,
        help=(
            "Restrict motion detection to this region only (W:H:X:Y). "
            "Independent of --crop, so you can detect motion in a tight "
            "window around the nest while saving a wider frame. Defaults "
            "to --crop if --crop is set."
        ),
    )
    return parser.parse_args()


def run_motion_loop(
    args: argparse.Namespace,
    rtsp_url: str,
    stop_flag,
) -> tuple[int, list[tuple[datetime, Path]]]:
    """Motion-aware capture loop.

    Ticks every --peek-interval seconds. On each tick:
      - grab a downscaled grayscale fingerprint
      - compare to the previous fingerprint via mean abs pixel diff
      - if score >= threshold OR within --motion-cooldown peeks of last motion
        OR --interval seconds have passed since the last saved frame:
        save a full-resolution frame.

    Returns (count_saved, session_frames).
    """
    captured = 0
    interval_saves = 0
    session_start = time.monotonic()
    session_frames: list[tuple[datetime, Path]] = []
    prev_fp: bytes | None = None
    last_save_monotonic: float | None = None
    last_heartbeat_monotonic: float | None = None
    last_motion_monotonic: float | None = None
    cooldown_left = 0

    fp_timeout = max(5, int(args.peek_interval * 4))

    def _duration_elapsed() -> bool:
        return args.duration > 0 and (time.monotonic() - session_start) >= args.duration

    burst: BurstCapture | None = None

    def _on_burst_frame(ts: datetime, path: Path) -> None:
        nonlocal captured, last_save_monotonic
        captured += 1
        last_save_monotonic = time.monotonic()
        LOG.info("Saved %s [burst] (%d total)", path.name, captured)

    if args.burst:
        burst = BurstCapture(
            rtsp_url=rtsp_url,
            output_dir=args.output_dir,
            fps=args.burst_fps,
            session_frames=session_frames,
            on_frame=_on_burst_frame,
            crop=args.crop,
        )

    try:
        while not stop_flag():
            cycle_start = time.monotonic()
            now = datetime.now()

            fp = capture_fingerprint(
                rtsp_url,
                size=args.fingerprint_size,
                timeout=fp_timeout,
                crop=args.motion_crop,
            )
            score = mean_abs_diff(prev_fp, fp) if (prev_fp and fp) else 0.0

            in_active_window = (
                last_motion_monotonic is not None
                and (time.monotonic() - last_motion_monotonic)
                < args.motion_active_window
            )
            effective_threshold = (
                args.motion_threshold_active
                if in_active_window
                else args.motion_threshold
            )
            motion = (
                fp is not None and prev_fp is not None and score >= effective_threshold
            )

            if args.log_motion_scores:
                LOG.info(
                    "peek score=%.2f thr=%.2f%s%s%s",
                    score,
                    effective_threshold,
                    " [ACTIVE]" if in_active_window else "",
                    " [BURST]" if burst and burst.active else "",
                    " [MOTION]" if motion else "",
                )

            save_reason: str | None = None
            if prev_fp is None:
                # Always save the very first frame: baseline + heartbeat.
                save_reason = "initial"
            elif motion:
                save_reason = "motion-active" if in_active_window else "motion"
                cooldown_left = args.motion_cooldown
                last_motion_monotonic = time.monotonic()
            elif cooldown_left > 0:
                save_reason = "cooldown"
                cooldown_left -= 1
            elif (
                last_heartbeat_monotonic is None
                or (time.monotonic() - last_heartbeat_monotonic) >= args.interval
            ):
                save_reason = "heartbeat"

            # Burst lifecycle: start on motion, stop when active window closes.
            if burst is not None:
                if motion and not burst.active:
                    burst.start()
                elif burst.active and not in_active_window:
                    burst.stop()

            # While burst is harvesting we skip per-peek saves (it provides
            # frames at a much higher rate). Always honor the very first save
            # so we have a baseline even if burst hasn't produced anything yet.
            skip_save = burst is not None and burst.active and save_reason != "initial"

            if save_reason is not None and not skip_save:
                timestamp = now.strftime("%Y%m%d_%H%M%S")
                out_path = args.output_dir / f"frame_{timestamp}.jpg"
                if capture_frame(
                    rtsp_url, out_path, timeout=args.timeout, crop=args.crop
                ):
                    captured += 1
                    if save_reason in ("initial", "heartbeat"):
                        interval_saves += 1
                        last_heartbeat_monotonic = time.monotonic()
                    session_frames.append((now, out_path))
                    last_save_monotonic = time.monotonic()
                    LOG.info(
                        "Saved %s [%s, score=%.2f] (%d total)",
                        out_path.name,
                        save_reason,
                        score,
                        captured,
                    )
                else:
                    LOG.warning("Capture failed for %s", out_path.name)

            if fp is not None:
                prev_fp = fp

            if args.count and interval_saves >= args.count:
                break
            if _duration_elapsed():
                LOG.info("Reached --duration %.0fs, stopping.", args.duration)
                break

            if stop_flag():
                break

            elapsed = time.monotonic() - cycle_start
            sleep_for = args.peek_interval - elapsed
            if sleep_for > 0:
                end = time.monotonic() + sleep_for
                while not stop_flag() and time.monotonic() < end:
                    time.sleep(min(0.2, end - time.monotonic()))
    finally:
        if burst is not None and burst.active:
            burst.stop()
        # Ensure session_frames is sorted: burst harvests interleave with
        # peek saves, and the harvest poller can deliver in small bursts.
        session_frames.sort(key=lambda item: item[0])

    return captured, session_frames


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    args = parse_args()

    if args.interval <= 0:
        LOG.error("--interval must be > 0")
        return 2

    load_env_file(ENV_FILE)
    rtsp_url = args.rtsp_url or os.environ.get(ENV_VAR)
    if not rtsp_url:
        LOG.error("No RTSP URL supplied (--rtsp-url or %s in .env).", ENV_VAR)
        return 2

    args.output_dir.mkdir(parents=True, exist_ok=True)

    # --motion-crop defaults to --crop so the common case ("just look at
    # the nest") is a single flag.
    if args.motion_crop is None and args.crop is not None:
        args.motion_crop = args.crop

    if args.crop or args.motion_crop:
        input_size = probe_input_size(rtsp_url)
        if input_size:
            LOG.info("Input video size: %dx%d.", *input_size)
        else:
            LOG.warning("Could not probe input size; skipping crop pre-check.")
        try:
            if args.crop:
                validate_crop(args.crop, input_size, label="--crop")
            if args.motion_crop and args.motion_crop != args.crop:
                validate_crop(args.motion_crop, input_size, label="--motion-crop")
        except ValueError as e:
            LOG.error("%s", e)
            return 2

    if args.motion:
        LOG.info(
            "Motion mode: peek every %.2fs, heartbeat every %.2fs, "
            "threshold=%.2f (active=%.2f for %.0fs after motion), "
            "cooldown=%d%s -> %s",
            args.peek_interval,
            args.interval,
            args.motion_threshold,
            args.motion_threshold_active,
            args.motion_active_window,
            args.motion_cooldown,
            f", burst @ {args.burst_fps:.1f}fps" if args.burst else "",
            args.output_dir,
        )
    else:
        LOG.info("Capturing every %.2fs into %s", args.interval, args.output_dir)

    stop = False

    def _handle_signal(signum, _frame):
        nonlocal stop
        LOG.info("Received signal %s, stopping after current capture.", signum)
        stop = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    captured = 0
    session_frames: list[tuple[datetime, Path]] = []

    if args.motion:
        captured, session_frames = run_motion_loop(
            args=args,
            rtsp_url=rtsp_url,
            stop_flag=lambda: stop,
        )
    else:
        session_start = time.monotonic()
        while not stop:
            cycle_start = time.monotonic()
            now = datetime.now()
            timestamp = now.strftime("%Y%m%d_%H%M%S")
            out_path = args.output_dir / f"frame_{timestamp}.jpg"

            if capture_frame(rtsp_url, out_path, timeout=args.timeout, crop=args.crop):
                captured += 1
                session_frames.append((now, out_path))
                LOG.info("Saved %s (%d total)", out_path.name, captured)
            else:
                LOG.warning("Capture failed for %s", out_path.name)

            if args.count and captured >= args.count:
                break
            if (
                args.duration > 0
                and (time.monotonic() - session_start) >= args.duration
            ):
                LOG.info("Reached --duration %.0fs, stopping.", args.duration)
                break

            if stop:
                break

            elapsed = time.monotonic() - cycle_start
            sleep_for = args.interval - elapsed
            if sleep_for > 0:
                # Sleep in small chunks so signals are responsive.
                end = time.monotonic() + sleep_for
                while not stop and time.monotonic() < end:
                    time.sleep(min(0.5, end - time.monotonic()))

    LOG.info("Done. %d image(s) captured.", captured)

    if not args.no_video and len(session_frames) >= 2:
        first_ts = session_frames[0][0]
        last_ts = session_frames[-1][0]
        video_name = (
            f"session_{first_ts.strftime('%Y%m%d_%H%M%S')}"
            f"_{last_ts.strftime('%Y%m%d_%H%M%S')}"
            f"_{int(args.video_fps)}fps.mp4"
        )
        video_path = args.output_dir / video_name
        LOG.info(
            "Building session timelapse from %d frames -> %s",
            len(session_frames),
            video_path,
        )
        try:
            stitch.build_timelapse(
                session_frames,
                video_path,
                fps=args.video_fps,
                intro=True,
                title=args.video_title,
            )
            LOG.info("Session video: %s", video_path)
            if not args.keep_images:
                stitch.cleanup_frames(session_frames)
        except Exception as exc:  # noqa: BLE001 - report and continue
            LOG.error("Failed to build session video: %s", exc)
    elif not args.no_video:
        LOG.info(
            "Not enough frames (%d) to build a session video.", len(session_frames)
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
