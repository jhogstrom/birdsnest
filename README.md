# Birdsnest

A pair of blackbirds (*Turdus merula*) built a nest in our garden, and we
pointed an IP camera at it. This repo is the small Python toolkit that
turns that camera feed into watchable timelapse videos - capturing frames
only when something actually moves, stitching them into a clip, and
publishing the result.

## Watch the nest

**YouTube channel: [@blackbird-nesting](https://www.youtube.com/@blackbird-nesting)**

That's where the finished videos go. Eggs, feeding runs, fledging - the
whole season, condensed.

---

## What's in here

Four small Python scripts, glued together with a Makefile.

### `src/capture.py` - motion-aware frame capture

Pulls frames from an RTSP stream and saves them only when the scene
changes. The cheap path is a 64×64 grayscale fingerprint compared against
the previous one; if the mean pixel difference crosses
`--motion-threshold`, a full-resolution frame is written.

When motion fires, the script enters an *active window* (default 2 min)
with a lower threshold, so subtle follow-up movements (settling on the
nest, feeding) aren't missed. Optionally it spawns a burst-mode ffmpeg
process holding a warm RTSP connection and dumping JPEGs at several
frames per second for the duration of the window.

A periodic heartbeat frame ensures the timelapse keeps moving even
during quiet stretches. On exit it hands the collected frames to the
stitcher.

### `src/stitch.py` - ffmpeg-based stitcher

Takes a list of timestamped frames and builds an MP4. Two modes:

- **Timelapse**: fixed FPS, every frame becomes a video frame.
- **Realtime**: each frame is held for its real on-clock duration, so
  the video plays at the speed events actually happened.

Adds a short intro card (date range, frame count, source FPS) and
cleans up source images afterward unless `--keep-images` is set.

### `src/record.py` - direct RTSP recording

Plain "save the next N seconds of stream to an MP4". No motion logic,
no stitching. Useful for sanity-checking the camera or grabbing a
specific moment.

### `src/upload.py` - YouTube publisher

Resumable upload via the YouTube Data API v3. First run opens a browser
for OAuth; the refresh token is cached locally
(`.config/birdsnest/youtube_token.json`, chmod 600).

If `--description` is omitted it auto-generates one from the filename:
parses the start/end timestamps embedded in the name and produces a
short blurb like *"Captured on 14 May 2026, 20:54 – 21:11. Source
footage spans 17 min."*

Quota note: each upload costs 1600 units of a 10000/day default quota,
so ~6 uploads/day before Google starts saying no.

---

## Makefile targets

```bash
make record [duration=15]
    # Save N seconds of stream straight to MP4.

make timelapse [duration=3600] [heartbeat=120]
    # One motion-aware capture session, then stitch.

make timelapse-loop [duration=21600] [heartbeat=120]
    # Back-to-back sessions forever (Ctrl-C to stop).
    # Default: 6h sessions, each producing its own video.

make scale-down file=<path-or-glob> [format=vertical|square|horizontal|phone]
    # Re-encode for social media. Globs supported. Incremental
    # (skips files whose scaled output is already up to date).
    # phone preset is tuned for WhatsApp.

make publish file=<path> title="..." [privacy=unlisted] \
                                     [description="..."] [tags=...]
    # Upload to YouTube. Description is auto-generated from the
    # filename if you don't supply one.
```

---

## Setup

```bash
uv sync                                            # install deps
echo 'CAMSTREAM_LOCAL=rtsp://user:pass@cam/...' > .env
mkdir -p .config/birdsnest
# Drop a Google OAuth Desktop client_secrets.json into .config/birdsnest/
```

Tested on WSL2 Ubuntu with `ffmpeg` on `$PATH`. Python >= 3.10.
