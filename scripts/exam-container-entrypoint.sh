#!/bin/sh
set -eu

# Exam-server container bootstrap. Only does work for `run`; other sub-commands (roster ...) pass straight through.
# First start of a center: create its device identity in the center's Docker volume and publish the public
# enrollment file. Later starts reuse it.
if [ "${1:-}" != "run" ]; then
  exec python /app/exam-server/exam_server.py "$@"
fi

CENTER=""
PREV=""
for ARG in "$@"; do
  if [ "$PREV" = "--center" ]; then CENTER="$ARG"; break; fi
  PREV="$ARG"
done
if [ -z "$CENTER" ]; then
  echo "Missing --center <center_id> argument." >&2
  exit 2
fi

HOME_DIR="/app/printer-agent/devices"
DEVICE_DIR="$HOME_DIR/$CENTER"
mkdir -p "$DEVICE_DIR" /exports /data

if [ ! -s "$DEVICE_DIR/chain_key.hex" ] || [ ! -s "$DEVICE_DIR/wrap_private.pem" ]; then
  echo "Creating the device identity for $CENTER (kept in this center's Docker volume)."
  # --force only because the folder may already hold other files; the identity itself is missing.
  python /app/printer-agent/printer_agent.py init --center "$CENTER" --home "$HOME_DIR" --force
fi

# Public enrollment data only (address + public key), for the board's "Enroll printers" step.
if [ -f "$DEVICE_DIR/enrollment_$CENTER.json" ]; then
  cp "$DEVICE_DIR/enrollment_$CENTER.json" "/exports/enrollment_$CENTER.json"
fi

# The candidate list is created by the board; the exam server downloads it and verifies it against the chain.

exec python /app/exam-server/exam_server.py "$@"
