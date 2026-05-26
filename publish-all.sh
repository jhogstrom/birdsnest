#!/usr/bin/env bash
set -euo pipefail
readonly SRC_DIR="output"
readonly DEST_DIR="output/published"
if [[ ! -d "${SRC_DIR}" ]]; then
  >&2 echo "Error: ${SRC_DIR} directory not found"
  exit 1
fi
mkdir -p "${DEST_DIR}"
shopt -s nullglob
files=("${SRC_DIR}"/*.mp4)
shopt -u nullglob
if [[ ${#files[@]} -eq 0 ]]; then
  echo "No mp4 files found in ${SRC_DIR}"
  exit 0
fi
for filepath in "${files[@]}"; do
  filename="$(basename "${filepath}")"
  title="${filename%.mp4}"
  echo ">>> Publishing ${filename}"
  make publish file="${filepath}" title="file ${title}"
  mv "${filepath}" "${DEST_DIR}/"
  echo ">>> Done: ${filename}"
done
echo "All ${#files[@]} file(s) published."

