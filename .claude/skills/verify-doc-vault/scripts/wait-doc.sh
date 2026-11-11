#!/usr/bin/env bash
# wait-doc.sh DOC_ID [status|insights] [timeout_s]. Polls until the field settles; saves the final JSON.
# Exit 0 on ready, 1 on failed or unavailable, 2 on timeout.
set -euo pipefail
source "$(dirname "$0")/env.sh"
id=$1; field=${2:-status}; timeout=${3:-180}
[ "$field" = insights ] && key=insights_status || key=status
for _ in $(seq 1 $((timeout / 2))); do
  body=$(curl -fsS "$API/documents/$id")
  v=$(printf '%s' "$body" | python3 -c "import sys,json; print(json.load(sys.stdin)['$key'])")
  case "$v" in
    ready) printf '%s\n' "$body" > "$RUN_DIR/doc-$id-$field-ready.json"; echo "$key=ready"; exit 0;;
    failed|unavailable) printf '%s\n' "$body" > "$RUN_DIR/doc-$id-$field-$v.json"; echo "$key=$v"; printf '%s\n' "$body"; exit 1;;
  esac
  sleep 2
done
echo "$key still '$v' after ${timeout}s" >&2; exit 2
