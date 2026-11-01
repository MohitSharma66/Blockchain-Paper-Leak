#!/bin/sh
set -eu

# Find the center id from the printer-agent CLI arguments.
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

DEVICE_DIR="/app/printer-agent/devices/$CENTER"
SEED_DIR="/seed/$CENTER"
mkdir -p "$DEVICE_DIR" /exports

# Import an existing identity into this center's private Docker volume on first startup.
# Seed folders are read-only and are not mounted into the running process after this copy.
if [ ! -s "$DEVICE_DIR/chain_key.hex" ] || [ ! -s "$DEVICE_DIR/wrap_private.pem" ]; then
  if [ -s "$SEED_DIR/chain_key.hex" ] && [ -s "$SEED_DIR/wrap_private.pem" ]; then
    cp "$SEED_DIR/chain_key.hex" "$DEVICE_DIR/chain_key.hex"
    cp "$SEED_DIR/wrap_private.pem" "$DEVICE_DIR/wrap_private.pem"
    if [ -f "$SEED_DIR/enrollment_$CENTER.json" ]; then
      cp "$SEED_DIR/enrollment_$CENTER.json" "$DEVICE_DIR/enrollment_$CENTER.json"
    fi
    chmod 600 "$DEVICE_DIR/chain_key.hex" "$DEVICE_DIR/wrap_private.pem"
    echo "Imported the existing $CENTER identity into its dedicated Docker volume."
  else
    python printer_agent.py init --center "$CENTER" --home /app/printer-agent/devices
  fi
fi

# Enrollment JSON contains public keys/address only, not the private keys.
if [ -f "$DEVICE_DIR/enrollment_$CENTER.json" ]; then
  cp "$DEVICE_DIR/enrollment_$CENTER.json" "/exports/enrollment_$CENTER.json"
fi

exec python printer_agent.py "$@"
