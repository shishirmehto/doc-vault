#!/usr/bin/env bash
# Start an isolated stack (own compose project, ports and volumes) and wait for /health.
set -euo pipefail
unset RUN_ID
source "$(dirname "$0")/env.sh"
if [ -n "$(compose ps -q 2>/dev/null)" ]; then
  echo "project $PROJECT is already up: run doctor.sh to reuse it or cleanup.sh to remove it" >&2; exit 1
fi
for p in "$API_PORT" "$DB_PORT" "$WEB_PORT"; do
  if lsof -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then echo "port $p is taken by another process" >&2; exit 1; fi
done
if grep -Eq '^OPENAI_API_KEY=.+' "$REPO/.env" 2>/dev/null && [ "${VERIFY_LIVE:-}" != 1 ]; then
  echo ".env holds an OpenAI key, so this stack would make paid calls. Set VERIFY_LIVE=1 to accept that." >&2; exit 1
fi
mkdir -p "$RUN_DIR"
echo "$RUN_ID" > "$EVIDENCE_ROOT/current"
touch "$EVIDENCE_ROOT/.started-$PROJECT"
git -C "$REPO" log -1 --format='commit %h %s' > "$RUN_DIR/build.txt"
git -C "$REPO" status --short >> "$RUN_DIR/build.txt"
compose up --build -d "$@"
for _ in $(seq 1 90); do
  if curl -fsS "$API/health" > "$RUN_DIR/00-health.json" 2>/dev/null; then
    echo "ready: $API (web $WEB), evidence in $RUN_DIR"; cat "$RUN_DIR/00-health.json"; echo; exit 0
  fi
  sleep 2
done
echo "api did not answer /health within 180 s; see: $(dirname "$0")/doctor.sh" >&2; exit 1
