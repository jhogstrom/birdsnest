# Birdsnest convenience targets.
#
# Usage:
#   make record [duration=15]
#   make timelapse [duration=3600] [heartbeat=120]
#   make scale-down file=output/foo.mp4 [format=vertical|square|horizontal|phone]
#   make scale-down file='output/session_*.mp4'   # globs work; quote to defer
#                                                  # expansion to make.
#   make publish file=output/foo.mp4 title="..." [privacy=unlisted] \
#                [description="..."] [tags=birds,nest]

PYTHON ?= uv run python
SRC := src

# Defaults (overridable on the command line).
heartbeat ?= 120
format    ?= phone

TIMELAPSE_DURATION ?= 3600

# Social-media presets: <width>x<height> + per-preset CRF.
# `phone` is tuned for WhatsApp/messaging: smaller resolution + slightly
# higher CRF means files stay well under upload caps and look identical on
# a phone screen (and avoid the platform's own re-compression hammering
# them further). Other presets keep CRF 23 for desktop/IG/Reels viewing.
ifeq ($(format),vertical)
  WIDTH  := 1080
  HEIGHT := 1920
  CRF    := 23
else ifeq ($(format),square)
  WIDTH  := 1080
  HEIGHT := 1080
  CRF    := 23
else ifeq ($(format),horizontal)
  WIDTH  := 1920
  HEIGHT := 1080
  CRF    := 23
else ifeq ($(format),phone)
  WIDTH  := 720
  HEIGHT := 1280
  CRF    := 25
else
  $(error Unknown format '$(format)'. Use vertical, square, horizontal, or phone.)
endif

OUTPUT_DIR := output/scaled
SUFFIX     := _$(WIDTH)x$(HEIGHT)

.PHONY: help record timelapse timelapse-loop livestream scale-down publish format lint check

help:
	@echo "Targets:"
	@echo "  make record [duration=15]"
	@echo "      Record an RTSP clip directly to MP4."
	@echo ""
	@echo "  make timelapse [duration=3600] [heartbeat=120]"
	@echo "      Run motion-aware capture with burst on detection,"
	@echo "      then stitch a session timelapse video."
	@echo ""
	@echo "  make timelapse-loop [duration=21600] [heartbeat=120]"
	@echo "      Run back-to-back timelapse sessions forever (Ctrl-C to stop)."
	@echo "      Each session is 'duration' seconds (default 6h) and produces"
	@echo "      its own session_*.mp4 on exit before the next one starts."
	@echo ""
	@echo "  make livestream [bitrate=3500k] [fps=30] [keyint=2] [stream=main|sub] \\"
	@echo "                  [crop=W:H:X:Y] [zoom=WxH]"
	@echo "      Stream the RTSP camera to YouTube Live via RTMPS."
	@echo "      Needs YOUTUBE_STREAM_KEY in .env. Re-encodes to enforce a"
	@echo "      2s keyframe interval. Auto-reconnects on failure."
	@echo "      stream=main forces /stream1 (1080p), stream=sub forces /stream2."
	@echo "      crop+zoom let you focus on a region (e.g. crop=960:540:480:270"
	@echo "      zoom=1920x1080 zooms the centered quarter to full HD)."
	@echo ""
	@echo "  make scale-down file=<path-or-glob> [format=vertical|square|horizontal|phone]"
	@echo "      Re-encode video(s) for social media."
	@echo "      Presets: vertical (1080x1920, default), square (1080x1080),"
	@echo "               horizontal (1920x1080), phone (720x1280, WhatsApp-friendly)."
	@echo "      Globs supported: file='output/session_*.mp4'"
	@echo "      Output: $(OUTPUT_DIR)/<basename>$(SUFFIX).<ext>"
	@echo "      Skips files whose scaled output is already up to date."
	@echo ""
	@echo "  make publish file=<path> title=\"...\" [privacy=unlisted]"
	@echo "                                       [description=\"...\"] [tags=...]"
	@echo "      Upload a video to YouTube. First run opens browser for OAuth."
	@echo "      Privacy defaults to unlisted (safer for automated runs)."

record: duration ?= 15
record:
	$(PYTHON) $(SRC)/record.py --duration $(duration)

timelapse: duration ?= 3600
timelapse:
	$(PYTHON) $(SRC)/capture.py \
		--motion \
		--motion-threshold 3.8 \
		--interval $(heartbeat) \
		--duration $(duration)

# timelapse-loop: run back-to-back capture sessions forever (Ctrl-C to stop).
# Each iteration captures `duration` seconds (default 6h), stitches a session
# video on exit, then immediately starts another session. A Ctrl-C during a
# session stops capture.py, which still builds the partial video; the loop
# trap catches the same signal and exits cleanly without starting another.
timelapse-loop: duration ?= 21600
timelapse-loop:
	@echo "Starting timelapse loop: $(duration)s sessions, Ctrl-C to stop."
	@trap 'echo; echo "Loop aborted."; exit 0' INT TERM; \
	i=1; \
	while :; do \
		echo "==> Session $$i starting at $$(date '+%Y-%m-%d %H:%M:%S')"; \
		$(PYTHON) $(SRC)/capture.py \
			--motion \
			--motion-threshold 3.8 \
			--interval $(heartbeat) \
			--duration $(duration) \
			|| { echo "Session $$i exited non-zero; stopping loop." >&2; exit 1; }; \
		echo "==> Session $$i done at $$(date '+%Y-%m-%d %H:%M:%S')"; \
		i=$$((i + 1)); \
	done

# livestream: RTSP camera -> YouTube Live (RTMPS). Re-encodes to force the
# 2s keyframe interval YouTube wants. Reads YOUTUBE_STREAM_KEY from .env.
# `stream=main` uses /stream1 (high-res); `stream=sub` uses /stream2 (low-res,
# safer when something else is already on the main stream). Default: whatever
# CAMSTREAM_LIVESTREAM (or CAMSTREAM_LOCAL) resolves to in .env.
# `crop=W:H:X:Y` cuts a region out of the input (ffmpeg crop syntax).
# `zoom=WxH` scales the (cropped) frame to that size before encoding.
bitrate ?= 3500k
fps     ?= 30
keyint  ?= 2
stream  ?=
crop    ?=
zoom    ?=

livestream:
	$(PYTHON) $(SRC)/livestream.py \
		--bitrate $(bitrate) \
		--fps $(fps) \
		--keyint-seconds $(keyint) \
		$(if $(stream),--stream $(stream)) \
		$(if $(crop),--crop $(crop)) \
		$(if $(zoom),--zoom-to $(zoom))

# ---------------------------------------------------------------------------
# scale-down: glob-aware, incremental.
#
# `file` may be a single path or a shell glob. We expand it via $(wildcard),
# refuse to scale anything already inside $(OUTPUT_DIR) (so re-globbing the
# parent dir doesn't recurse), then derive an output path per input. Each
# output is a real make target with its input as a prerequisite, so make's
# timestamp logic handles "skip if already up to date" for free.
# ---------------------------------------------------------------------------

# All matching inputs, with anything already under OUTPUT_DIR filtered out.
SCALE_INPUTS := $(filter-out $(OUTPUT_DIR)/%,$(wildcard $(file)))

# stem (no extension) and extension, computed per file at recipe time.
# Output path: output/scaled/<stem>_<WxH>.<ext>
scale_out = $(OUTPUT_DIR)/$(basename $(notdir $(1)))$(SUFFIX)$(suffix $(1))

SCALE_OUTPUTS := $(foreach f,$(SCALE_INPUTS),$(call scale_out,$(f)))

scale-down:
ifndef file
	$(error file=<path-or-glob> is required, e.g. make scale-down file='output/session_*.mp4')
endif
ifeq ($(strip $(SCALE_INPUTS)),)
	@echo "No files matched: $(file)" >&2
	@exit 1
else
	@$(MAKE) --no-print-directory $(SCALE_OUTPUTS)
endif

# Per-file scaling rule. The .mp4 form keeps make happy about the suffix; the
# pattern rule below catches any other extension by matching on stem+suffix.
$(OUTPUT_DIR)/%$(SUFFIX).mp4: output/%.mp4 | $(OUTPUT_DIR)
	@echo "Scaling $< -> $@ ($(WIDTH)x$(HEIGHT), $(format))"
	@ffmpeg -y -loglevel error -stats -i "$<" \
		-vf "scale=w=$(WIDTH):h=$(HEIGHT):force_original_aspect_ratio=decrease,pad=$(WIDTH):$(HEIGHT):(ow-iw)/2:(oh-ih)/2:color=black,setsar=1" \
		-c:v libx264 -crf $(CRF) -preset medium -pix_fmt yuv420p \
		-movflags +faststart \
		-c:a aac -b:a 128k -ac 2 \
		"$@"

$(OUTPUT_DIR):
	@mkdir -p $@

# ---------------------------------------------------------------------------
# publish: YouTube upload via src/upload.py.
# Required: file=<path> title="..."
# Optional: privacy={public,unlisted,private}  (default: unlisted)
#           description="..."
#           tags=tag1,tag2
# ---------------------------------------------------------------------------

privacy     ?= unlisted
description ?=
tags        ?=

publish:
ifndef file
	$(error file=<path> is required, e.g. make publish file=output/foo.mp4 title="Day 3 - feeding")
endif
ifndef title
	$(error title="..." is required)
endif
	@test -f "$(file)" || { echo "File not found: $(file)" >&2; exit 1; }
	$(PYTHON) $(SRC)/upload.py "$(file)" \
		--title "$(title)" \
		--privacy $(privacy) \
		$(if $(description),--description "$(description)") \
		$(if $(tags),--tags "$(tags)")

# ---------------------------------------------------------------------------
# Code quality.
# ---------------------------------------------------------------------------

format:
	uv run black src/

lint:
	uv run ruff check src/

check: format lint
