#!/bin/bash
# Install (or update) the birdsnest-livestream systemd service.
#
# Defaults match a single-user dev setup. Override for a dedicated box:
#
#   BIRDSNEST_USER=birdsnest \
#   BIRDSNEST_DIR=/opt/birdsnest \
#   STREAM=main \
#   EXTRA_ARGS="--crop 960:540:480:270 --zoom-to 1920x1080" \
#     sudo -E ./scripts/install-systemd.sh
#
# After install:
#   sudo systemctl enable --now birdsnest-livestream
#   sudo journalctl -u birdsnest-livestream -f

set -euo pipefail

# Defaults --------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

: "${BIRDSNEST_USER:=$(stat -c '%U' "${REPO_DIR}")}"
: "${BIRDSNEST_DIR:=${REPO_DIR}}"
: "${STREAM:=main}"             # main | sub
: "${EXTRA_ARGS:=}"             # additional flags to append (e.g. --crop ...)
: "${UNIT_NAME:=birdsnest-livestream}"
: "${SERVICE_DIR:=/etc/systemd/system}"

TEMPLATE="${REPO_DIR}/systemd/${UNIT_NAME}.service.template"
TARGET="${SERVICE_DIR}/${UNIT_NAME}.service"

# Sanity ----------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    >&2 echo "Error: must run as root (sudo). Re-exec with: sudo -E $0 $*"
    exit 1
fi

if [[ ! -f "${TEMPLATE}" ]]; then
    >&2 echo "Error: template not found at ${TEMPLATE}"
    exit 1
fi

if ! id -u "${BIRDSNEST_USER}" >/dev/null 2>&1; then
    >&2 echo "Error: user '${BIRDSNEST_USER}' does not exist."
    >&2 echo "       Create it with:  sudo useradd -r -m -d /home/${BIRDSNEST_USER} -s /bin/bash ${BIRDSNEST_USER}"
    exit 1
fi

if [[ ! -d "${BIRDSNEST_DIR}" ]]; then
    >&2 echo "Error: BIRDSNEST_DIR does not exist: ${BIRDSNEST_DIR}"
    exit 1
fi

if [[ ! -f "${BIRDSNEST_DIR}/.env" ]]; then
    >&2 echo "Warning: ${BIRDSNEST_DIR}/.env not found."
    >&2 echo "         Service will fail until you create it with CAMSTREAM_LOCAL"
    >&2 echo "         (or CAMSTREAM_LIVESTREAM) and YOUTUBE_STREAM_KEY."
fi

# Locate uv: try the service user's PATH first, fall back to common spots.
UV_PATH="$(sudo -u "${BIRDSNEST_USER}" -H bash -lc 'command -v uv' 2>/dev/null || true)"
if [[ -z "${UV_PATH}" ]]; then
    for candidate in \
        "/home/${BIRDSNEST_USER}/.local/bin/uv" \
        "/usr/local/bin/uv" \
        "/usr/bin/uv"; do
        if [[ -x "${candidate}" ]]; then
            UV_PATH="${candidate}"
            break
        fi
    done
fi
if [[ -z "${UV_PATH}" ]]; then
    >&2 echo "Error: uv not found for user '${BIRDSNEST_USER}'."
    >&2 echo "       Install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
    >&2 echo "       (run as ${BIRDSNEST_USER}, then re-run this script)"
    exit 1
fi

case "${STREAM}" in
    main|sub) ;;
    *) >&2 echo "Error: STREAM must be 'main' or 'sub' (got '${STREAM}')."; exit 1 ;;
esac

# Compose ExecStart args.
COMPOSED_ARGS="--stream ${STREAM}"
if [[ -n "${EXTRA_ARGS}" ]]; then
    COMPOSED_ARGS="${COMPOSED_ARGS} ${EXTRA_ARGS}"
fi

# Render ----------------------------------------------------------------------
echo "Installing ${UNIT_NAME} at ${TARGET}"
echo "  User:       ${BIRDSNEST_USER}"
echo "  Dir:        ${BIRDSNEST_DIR}"
echo "  uv:         ${UV_PATH}"
echo "  Args:       ${COMPOSED_ARGS}"
echo

# Use Python for substitution -- sed gets messy if any value contains slashes.
python3 - "${TEMPLATE}" "${TARGET}" \
    "${BIRDSNEST_USER}" "${BIRDSNEST_DIR}" "${UV_PATH}" "${COMPOSED_ARGS}" <<'PY'
import sys
src, dst, user, directory, uv, args = sys.argv[1:7]
content = open(src).read()
content = (content
    .replace("@@USER@@", user)
    .replace("@@DIR@@", directory)
    .replace("@@UV@@", uv)
    .replace("@@EXTRA_ARGS@@", args))
open(dst, "w").write(content)
PY

chmod 644 "${TARGET}"

# Reload and report -----------------------------------------------------------
systemctl daemon-reload
echo "Reloaded systemd."
echo

systemctl status "${UNIT_NAME}" --no-pager 2>&1 | head -3 || true
echo
echo "Next steps:"
echo "  sudo systemctl enable --now ${UNIT_NAME}"
echo "  sudo journalctl -u ${UNIT_NAME} -f"
echo
echo "To uninstall:"
echo "  sudo systemctl disable --now ${UNIT_NAME}"
echo "  sudo rm ${TARGET}"
echo "  sudo systemctl daemon-reload"
