#!/usr/bin/env bash
# call.sh METHOD PATH [curl args]. Saves the response body as evidence and prints "<status>" then the body.
# Example: call.sh POST /documents -F file=@samples/sample-cgl-policy.pdf
set -euo pipefail
source "$(dirname "$0")/env.sh"
mkdir -p "$RUN_DIR"
method=$1; path=$2; shift 2
n=$(find "$RUN_DIR" -name '[0-9][0-9]-*' ! -name '00-*' | wc -l | tr -d ' ')
out=$RUN_DIR/$(printf '%02d' $((n + 1)))-$method-$(printf '%s' "${path#/}" | tr -c 'A-Za-z0-9.-' '_' | cut -c1-60).json
code=$(cd "$REPO" && curl -sS -o "$out" -w '%{http_code}' -X "$method" "$API$path" "$@")
printf '%s %s %s -> %s %s\n' "$(date +%H:%M:%S)" "$method" "$path" "$code" "$(basename "$out")" >> "$RUN_DIR/transcript.log"
echo "$code"; cat "$out"; echo
