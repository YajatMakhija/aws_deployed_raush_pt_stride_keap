#!/usr/bin/env bash
# Builds the demo server's .env from the old demo settings (~/old-demo.env, copied from the old box).
# Runs ON the demo server so no secret value ever passes through chat or a laptop file.
# Prints setting names only, never values.
#
#   bash scripts/make-demo-env.sh [path/to/old-demo.env]
set -euo pipefail

SRC="${1:-$HOME/old-demo.env}"
DOMAIN="demo.aibolt.ai"
OUT="$(cd "$(dirname "$0")/.." && pwd)/.env"

[ -f "$SRC" ] || { echo "Missing $SRC"; exit 1; }
if [ -f "$OUT" ]; then
  cp -p "$OUT" "$OUT.bak.$(date +%Y%m%d%H%M%S)"
  echo "Backed up the existing .env"
fi

umask 077
cp "$SRC" "$OUT"
chmod 600 "$OUT"

set_kv() {  # set_kv KEY VALUE: replace the line, or append it
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  grep -v "^${key}=" "$OUT" > "$tmp" || true
  printf '%s=%s\n' "$key" "$value" >> "$tmp"
  cat "$tmp" > "$OUT" && rm -f "$tmp"
  echo "set     $key"
}

# Every value that named the old host (API_BASE_URL, PUBLIC_BASE_URL, ...) now names the demo host.
for key in $(grep -E '^[A-Z_]+=.*stride\.aibolt\.ai' "$OUT" | cut -d= -f1); do
  echo "renamed $key (stride.aibolt.ai -> $DOMAIN)"
done
tmp="$(mktemp)"
sed "s/stride\.aibolt\.ai/${DOMAIN}/g" "$OUT" > "$tmp" && cat "$tmp" > "$OUT" && rm -f "$tmp"

set_kv PUBLIC_BASE_URL "https://${DOMAIN}"
set_kv KEAP_REDIRECT_URI "https://${DOMAIN}/api/v1/keap/oauth/callback"

# Safe start: the old settings still carry the clinic phone number, which production owns now.
# Workers stay off (and are not started) until the demo has its test numbers.
set_kv SHEET_SYNC_ENABLED false
set_kv STRIDE_IMPORT_ENABLED false
set_kv KEAP_SYNC_ENABLED false

if grep -q '34\.225\.188\.32' "$OUT"; then
  echo "WARNING: a value still names the production IP 34.225.188.32:"
  grep -E '^[A-Z_]+=.*34\.225\.188\.32' "$OUT" | cut -d= -f1
fi

echo "Done: $OUT (mode 600, $(grep -cE '^[A-Z_]+=' "$OUT") settings)"
