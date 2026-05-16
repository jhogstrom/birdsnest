#!/usr/bin/env python3
"""Upload a video to YouTube via the Data API v3.

First run is interactive: opens a browser for OAuth approval. Subsequent
runs use the cached refresh token at .config/birdsnest/youtube_token.json
(project-local preferred, ~/.config/birdsnest/ as fallback).

Setup (one-time):
  1. Google Cloud Console -> create project, enable "YouTube Data API v3"
  2. OAuth consent screen: External, scope youtube.upload, add yourself as
     a test user.
  3. Create OAuth client ID (Desktop app), download JSON to
     .config/birdsnest/client_secrets.json (project-local, gitignored).
  4. uv sync   (adds google-api-python-client + google-auth-oauthlib).

Quota:
  Each upload costs 1600 units of a 10000/day default quota (~6 uploads/day).
  Request a quota increase from Google if you need more.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

LOG = logging.getLogger("upload")

# Default preamble for auto-generated descriptions. Kept short and factual;
# the per-video timing detail is appended below.
DEFAULT_PREAMBLE = (
    "Timelapse from a blackbird nest in our garden, captured automatically "
    "by a motion-aware camera."
)

# Filename patterns produced by capture.py / record.py / stitch.py.
# Each regex captures named groups consumed by _parse_filename().
_TS = r"(?P<{name}>\d{{8}}_\d{{6}})"
_FILENAME_PATTERNS = (
    # session_<start>_<end>_<fps>fps.mp4  (capture.py session output)
    re.compile(
        r"^session_"
        + _TS.format(name="start")
        + r"_"
        + _TS.format(name="end")
        + r"_(?P<fps>\d+)fps$"
    ),
    # timelapse_<start>_<end>_<fps>fps.mp4  or  ..._rt.mp4  (manual stitch)
    re.compile(
        r"^timelapse_"
        + _TS.format(name="start")
        + r"_"
        + _TS.format(name="end")
        + r"_(?:(?P<fps>\d+)fps|rt)$"
    ),
    # clip_<start>_<seconds>s.mp4  (record.py direct recording)
    re.compile(r"^clip_" + _TS.format(name="start") + r"_(?P<seconds>\d+)s$"),
)

# Trailing _WxH suffix added by `make scale-down` (e.g. `_720x1280`).
_SCALED_SUFFIX_RE = re.compile(r"_\d+x\d+$")


def _parse_filename(path: Path) -> dict | None:
    """Best-effort extract of capture metadata from a video filename.

    Returns a dict with at least {"start": datetime, "kind": str} and
    optionally {"end": datetime, "fps": int, "seconds": int}. Returns
    None if no known pattern matches.
    """
    stem = path.stem
    # Strip the scale-down suffix if present so the underlying pattern matches.
    stem = _SCALED_SUFFIX_RE.sub("", stem)

    for pattern in _FILENAME_PATTERNS:
        m = pattern.match(stem)
        if not m:
            continue
        groups = m.groupdict()
        try:
            start = datetime.strptime(groups["start"], "%Y%m%d_%H%M%S")
        except ValueError:
            return None
        info: dict = {"start": start, "kind": stem.split("_", 1)[0]}
        if groups.get("end"):
            try:
                info["end"] = datetime.strptime(groups["end"], "%Y%m%d_%H%M%S")
            except ValueError:
                pass
        if groups.get("fps"):
            info["fps"] = int(groups["fps"])
        if groups.get("seconds"):
            info["seconds"] = int(groups["seconds"])
            info["end"] = start + timedelta(seconds=int(groups["seconds"]))
        return info
    return None


def _format_duration(delta: timedelta) -> str:
    """Human-friendly duration like '2 h 14 min' or '47 min' or '38 s'."""
    total = int(delta.total_seconds())
    if total <= 0:
        return "0 s"
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours} h {minutes:02d} min" if minutes else f"{hours} h"
    if minutes:
        return (
            f"{minutes} min"
            if not seconds or minutes >= 5
            else f"{minutes} min {seconds:02d} s"
        )
    return f"{seconds} s"


def auto_description(path: Path, preamble: str = DEFAULT_PREAMBLE) -> str:
    """Build a YouTube description from a video filename.

    Falls back to just the preamble if the filename doesn't parse.
    """
    info = _parse_filename(path)
    if not info:
        return preamble

    start: datetime = info["start"]
    end: datetime | None = info.get("end")

    # Date line: '12 May 2026'. Time range if we know both ends.
    date_str = start.strftime("%d %B %Y").lstrip("0")
    if end and end != start:
        # Same-day: '20:54 - 21:11'. Cross-day: include both dates.
        if end.date() == start.date():
            time_str = f"{start.strftime('%H:%M')} - {end.strftime('%H:%M')}"
            shot_line = f"Captured on {date_str}, {time_str}."
        else:
            shot_line = (
                f"Captured from {start.strftime('%d %B %Y %H:%M').lstrip('0')} "
                f"to {end.strftime('%d %B %Y %H:%M').lstrip('0')}."
            )
        duration_line = f"Source footage spans {_format_duration(end - start)}."
    else:
        shot_line = f"Captured on {date_str} at {start.strftime('%H:%M')}."
        duration_line = (
            f"Clip length: {info['seconds']} s." if info.get("seconds") else ""
        )

    parts = [preamble, "", shot_line]
    if duration_line:
        parts.append(duration_line)
    return "\n".join(parts)


CONFIG_DIR_HOME = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "birdsnest"
)
# Project-local config dir, relative to the repo root (this file is in src/).
CONFIG_DIR_PROJECT = Path(__file__).resolve().parent.parent / ".config" / "birdsnest"


def _resolve_config(filename: str) -> Path:
    """Prefer project-local .config/birdsnest/, fall back to ~/.config/birdsnest/.

    For files that may not exist yet (token), we return the project-local path
    if its parent dir exists, otherwise the home-dir path. This keeps the
    OAuth token next to the client_secrets.json the user dropped in.
    """
    project_path = CONFIG_DIR_PROJECT / filename
    home_path = CONFIG_DIR_HOME / filename
    if project_path.exists():
        return project_path
    if home_path.exists():
        return home_path
    if CONFIG_DIR_PROJECT.exists():
        return project_path
    return home_path


CLIENT_SECRETS_DEFAULT = _resolve_config("client_secrets.json")
TOKEN_FILE_DEFAULT = _resolve_config("youtube_token.json")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
API_SERVICE = "youtube"
API_VERSION = "v3"

PRIVACY_CHOICES = ("public", "unlisted", "private")
CATEGORY_PEOPLE_BLOGS = "22"  # YouTube category id for "People & Blogs"


REPO_ROOT = Path(__file__).resolve().parent.parent

# Paths/names that should never be slurped via --description @file:.
# These are operator-owned but easy to fat-finger; refuse loudly rather
# than uploading credentials or SSH keys to YouTube.
_DESC_DENY_NAMES = {".env", "client_secrets.json", "youtube_token.json"}
_DESC_DENY_DIRS = (
    REPO_ROOT / ".config",
    Path.home() / ".config" / "birdsnest",
    Path.home() / ".ssh",
    Path("/etc"),
)


def _is_safe_description_path(path: Path) -> tuple[bool, str]:
    """Return (ok, reason). Reject obvious secret files."""
    try:
        resolved = path.resolve()
    except OSError as exc:
        return False, f"could not resolve path: {exc}"
    if resolved.name in _DESC_DENY_NAMES:
        return False, f"refusing to read sensitive file: {resolved.name}"
    for deny in _DESC_DENY_DIRS:
        try:
            resolved.relative_to(deny.resolve())
        except (ValueError, OSError):
            continue
        return False, f"refusing to read from sensitive directory: {deny}"
    return True, ""


def _check_deps() -> None:
    """Fail fast with a useful message if Google libs aren't installed."""
    missing: list[str] = []
    try:
        import google.auth  # noqa: F401
    except ImportError:
        missing.append("google-auth")
    try:
        import google_auth_oauthlib  # noqa: F401
    except ImportError:
        missing.append("google-auth-oauthlib")
    try:
        import googleapiclient  # noqa: F401
    except ImportError:
        missing.append("google-api-python-client")
    if missing:
        sys.stderr.write(
            "Missing dependencies: " + ", ".join(missing) + "\n"
            "Install with: pip install google-api-python-client google-auth-oauthlib\n"
        )
        sys.exit(2)


def get_authenticated_service(client_secrets: Path, token_file: Path):
    """Return an authorized YouTube API client. Triggers browser flow if needed."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds: Credentials | None = None

    if token_file.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
        except (ValueError, json.JSONDecodeError) as exc:
            LOG.warning("Token file invalid (%s); re-authorizing.", exc)
            creds = None

    if creds and creds.valid:
        return build(API_SERVICE, API_VERSION, credentials=creds)

    if creds and creds.expired and creds.refresh_token:
        LOG.info("Refreshing access token.")
        try:
            creds.refresh(Request())
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Refresh failed (%s); re-authorizing.", exc)
            creds = None

    if not creds or not creds.valid:
        if not client_secrets.exists():
            sys.stderr.write(
                f"Client secrets not found at {client_secrets}\n"
                "See header of this file for setup instructions.\n"
            )
            sys.exit(2)
        LOG.info("Starting OAuth flow (browser will open).")
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES)
        # run_local_server spins up a tiny web server on localhost to catch
        # the OAuth redirect; works on a desktop with a browser available.
        creds = flow.run_local_server(port=0, open_browser=True)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        # Atomically create the token file with 0600 from the start, so the
        # refresh token never exists on disk with world-readable perms.
        # O_EXCL + replace dance: write to a sibling tempfile, then rename.
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=token_file.name + ".",
            dir=str(token_file.parent),
        )
        try:
            os.chmod(tmp_name, 0o600)
            with os.fdopen(tmp_fd, "w") as fh:
                fh.write(creds.to_json())
            os.replace(tmp_name, token_file)
        except Exception:
            # Best-effort cleanup of the tempfile on failure.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        LOG.info("Saved token to %s", token_file)

    return build(API_SERVICE, API_VERSION, credentials=creds)


def upload_video(
    youtube,
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    privacy: str,
    category_id: str = CATEGORY_PEOPLE_BLOGS,
    made_for_kids: bool = False,
) -> dict:
    """Resumably upload a video. Returns the API response (includes id)."""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": made_for_kids,
        },
    }

    media = MediaFileUpload(
        str(video_path),
        chunksize=8 * 1024 * 1024,  # 8 MiB chunks - good for flaky links.
        resumable=True,
        mimetype="video/*",
    )

    request = youtube.videos().insert(
        part=",".join(body.keys()),
        body=body,
        media_body=media,
    )

    response = None
    last_pct = -1
    while response is None:
        try:
            status, response = request.next_chunk()
        except HttpError as exc:
            # 5xx and 503 quota-exceeded are retryable; surface anything else.
            if exc.resp.status in (500, 502, 503, 504):
                LOG.warning("Transient upload error (%s), retrying.", exc.resp.status)
                continue
            raise
        if status:
            pct = int(status.progress() * 100)
            if pct != last_pct:
                LOG.info("Upload progress: %d%%", pct)
                last_pct = pct
    return response


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("video", type=Path, help="Path to the video file to upload.")
    p.add_argument("--title", required=True, help="Video title (max 100 chars).")
    p.add_argument(
        "--description",
        default="",
        help=(
            "Video description. Use '@file:path' to read from a file. "
            "If omitted, a description is auto-generated from the filename "
            "(start/end timestamps and a short blackbird-nest preamble)."
        ),
    )
    p.add_argument(
        "--tags",
        default="",
        help="Comma-separated tags (e.g. 'birds,timelapse,nest').",
    )
    p.add_argument(
        "--privacy",
        choices=PRIVACY_CHOICES,
        default="unlisted",
        help="Privacy status (default: unlisted - safer for automated runs).",
    )
    p.add_argument(
        "--category",
        default=CATEGORY_PEOPLE_BLOGS,
        help=f"YouTube category id (default: {CATEGORY_PEOPLE_BLOGS} = People & Blogs).",
    )
    p.add_argument(
        "--made-for-kids",
        action="store_true",
        help="Mark as made for kids (COPPA). Default: not made for kids.",
    )
    p.add_argument(
        "--client-secrets",
        type=Path,
        default=CLIENT_SECRETS_DEFAULT,
        help=f"OAuth client secrets JSON (default: {CLIENT_SECRETS_DEFAULT}).",
    )
    p.add_argument(
        "--token-file",
        type=Path,
        default=TOKEN_FILE_DEFAULT,
        help=f"Where to cache the OAuth token (default: {TOKEN_FILE_DEFAULT}).",
    )
    return p.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = parse_args()
    _check_deps()

    if not args.video.exists():
        LOG.error("Video not found: %s", args.video)
        return 2
    if len(args.title) > 100:
        LOG.error("Title is %d chars; YouTube limit is 100.", len(args.title))
        return 2

    description = args.description
    if description.startswith("@file:"):
        desc_path = Path(description[len("@file:") :])
        if not desc_path.exists():
            LOG.error("Description file not found: %s", desc_path)
            return 2
        ok, reason = _is_safe_description_path(desc_path)
        if not ok:
            LOG.error("Refusing --description @file:%s -- %s", desc_path, reason)
            return 2
        description = desc_path.read_text()
        LOG.info(
            "Description loaded from %s (%d chars). First line: %s",
            desc_path,
            len(description),
            description.splitlines()[0][:120] if description.strip() else "(empty)",
        )
    elif not description:
        # No description supplied: synthesize one from the filename.
        description = auto_description(args.video)
        LOG.info("Auto-generated description:\n%s", description)

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]

    LOG.info("Authenticating...")
    youtube = get_authenticated_service(args.client_secrets, args.token_file)

    LOG.info(
        "Uploading %s (%.1f MB) as '%s' [%s]",
        args.video.name,
        args.video.stat().st_size / (1024 * 1024),
        args.title,
        args.privacy,
    )
    response = upload_video(
        youtube,
        args.video,
        title=args.title,
        description=description,
        tags=tags,
        privacy=args.privacy,
        category_id=args.category,
        made_for_kids=args.made_for_kids,
    )

    video_id = response.get("id")
    LOG.info("Done. Video id: %s", video_id)
    LOG.info("URL: https://www.youtube.com/watch?v=%s", video_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
