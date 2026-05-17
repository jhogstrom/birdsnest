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

Five small Python scripts, glued together with a Makefile.

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

`--crop W:H:X:Y` restricts saved frames (and burst frames) to a region
of the input; useful when the camera shows more than just the nest.
`--motion-crop W:H:X:Y` restricts the motion-detection fingerprint to
a region only, so background activity (wind, shadows, neighbouring
branches) doesn't trigger false positives. `--motion-crop` defaults
to `--crop` if only `--crop` is given. Example: `--crop 800:800:600:200`
on the 1920×1080 main stream keeps an 800×800 window around the nest
for both saved frames and motion detection.

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

### `src/livestream.py` - RTSP -> YouTube Live

Streams the camera straight to YouTube Live via RTMPS. A single ffmpeg
process; no OBS/NDI/VLC in the chain. Re-encodes video to force the
2-second keyframe interval YouTube wants (most cameras default to 5-10s
GOPs, which makes YouTube's transcoder unhappy and causes laggy
playback). Mixes in a silent AAC track if the camera has no usable
microphone.

Reconnects automatically with exponential backoff (2s -> 60s, cap) if
either side drops. Reads `YOUTUBE_STREAM_KEY` from `.env` next to the
RTSP URL.

`--stream main|sub` switches between the camera's main and substream
(handy on cameras like the TP-Link Tapo that limit `/stream1` to one
concurrent client - so timelapse capture can keep using the main feed
while livestream uses the substream).

`--crop W:H:X:Y` cuts a region out of the input; `--zoom-to WxH`
scales it back to any size. Encode cost is set by the output size, not
the input, so cropping to focus on the nest and zooming back to 1080p
costs the same as a full-frame 1080p stream - but the viewer sees the
bird filling the screen.

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

make livestream [bitrate=3500k] [fps=30] [keyint=2] [stream=main|sub] \
                [crop=W:H:X:Y] [zoom=WxH]
    # Stream RTSP camera to YouTube Live (RTMPS). Auto-reconnects.
    # Needs YOUTUBE_STREAM_KEY in .env.
    # Example: focus on a 960x540 region in the center, zoom to full HD:
    #   make livestream stream=main crop=960:540:480:270 zoom=1920x1080

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
cat > .env <<'EOF'
CAMSTREAM_LOCAL=rtsp://user:pass@cam/stream1
CAMSTREAM_LIVESTREAM=rtsp://user:pass@cam/stream2  # optional, livestream-only
YOUTUBE_STREAM_KEY=xxxx-xxxx-xxxx-xxxx-xxxx        # only for `make livestream`
EOF
chmod 600 .env
mkdir -p .config/birdsnest
# Drop a Google OAuth Desktop client_secrets.json into .config/birdsnest/
# (only needed for `make publish`)
```

Tested on WSL2 Ubuntu with `ffmpeg` on `$PATH`. Python >= 3.10.

---

## Running livestream as a system service

For a dedicated always-on box (the natural home for `make livestream`),
install the systemd unit:

```bash
# Optional: create a dedicated, unprivileged user.
sudo useradd -r -m -d /home/birdsnest -s /bin/bash birdsnest
sudo -u birdsnest curl -LsSf https://astral.sh/uv/install.sh | sh
# (clone the repo, populate .env, etc., as the service user)

# Install the unit (uses sensible defaults; override via env vars).
sudo -E ./scripts/install-systemd.sh

# Or, for a fully custom setup:
BIRDSNEST_USER=birdsnest \
BIRDSNEST_DIR=/opt/birdsnest \
STREAM=main \
EXTRA_ARGS="--crop 960:540:480:270 --zoom-to 1920x1080" \
  sudo -E ./scripts/install-systemd.sh

# Start it and watch logs.
sudo systemctl enable --now birdsnest-livestream
sudo journalctl -u birdsnest-livestream -f
```

The unit runs as the chosen user, restarts on hard failure (the script
has its own ffmpeg-reconnect backoff for normal blips), and goes through
`journalctl` for logs. `scripts/install-systemd.sh` is idempotent - run
it again to update args without uninstalling.
