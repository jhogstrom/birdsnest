#!/usr/bin/env bash
# Publish every .mp4 in ./output to YouTube via `make publish`, newest
# first (by mtime), then move successfully-published files into
# ./output/published/ so reruns skip them.
#
# Stops on the first failure (set -e) so we don't keep burning YouTube
# upload quota (1600 units each, 10000/day default) on a broken setup.
set -euo pipefail

readonly SRC_DIR="output"
readonly DEST_DIR="output/published"

if [[ ! -d "${SRC_DIR}" ]]; then
  >&2 echo "Error: ${SRC_DIR} directory not found"
  exit 1
fi
mkdir -p "${DEST_DIR}"

# Collect .mp4s at the top level of SRC_DIR (not recursive - leave
# output/scaled/ and output/published/ alone) and sort by mtime descending
# (newest first). -print0 + read -d '' handles filenames with spaces safely.
files=()
while IFS= read -r -d '' line; do
  # `find -printf '%T@ %p\0'` gives "<epoch.frac> <path>\0"; strip the timestamp.
  files+=("${line#* }")
done < <(find "${SRC_DIR}" -maxdepth 1 -type f -name '*.mp4' -printf '%T@ %p\0' \
         | sort -znr)

if [[ ${#files[@]} -eq 0 ]]; then
  echo "No mp4 files found in ${SRC_DIR}"
  exit 0
fi

echo "Publishing ${#files[@]} file(s), newest first."
for filepath in "${files[@]}"; do
  filename="$(basename "${filepath}")"
  title="${filename%.mp4}"
  echo ">>> Publishing ${filename}"
  make publish file="${filepath}" title="file ${title}"
  mv "${filepath}" "${DEST_DIR}/"
  echo ">>> Done: ${filename}"
done
echo "All ${#files[@]} file(s) published."

