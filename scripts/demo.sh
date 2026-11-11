#!/usr/bin/env bash
# The demo, end to end over the HTTP API: upload the three samples, wait until they are ready and
# their insights exist, print the insights, then chat with them and print every answer with its citations.
#
#   docker compose up --build -d && scripts/demo.sh
#   API=http://localhost:18000 scripts/demo.sh
#
# Needs curl and jq. Safe to rerun: a sample uploaded before comes back as the same document (HTTP 200).
set -euo pipefail

API=${API:-http://localhost:${API_PORT:-8000}}
SAMPLES="$(cd "$(dirname "$0")/../samples" && pwd)"
TIMEOUT_S=${TIMEOUT_S:-180}

section() { printf '\n== %s\n' "$*"; }
fail() { echo "demo: $*" >&2; exit 1; }

# GET or POST, fail on any status but the ones listed in OK (default 2xx). Prints the body.
call() {
  local out code
  out=$(curl -s -w '\n%{http_code}' "$@") || fail "curl $* did not connect"
  code=${out##*$'\n'}
  [[ $code == 2* ]] || fail "$* answered HTTP $code: ${out%$'\n'*}"
  printf '%s' "${out%$'\n'*}"
}

wait_for() {   # wait_for <description> <command that succeeds when done>
  local waited=0
  until "${@:2}"; do
    (( waited++ < TIMEOUT_S )) || fail "timed out after ${TIMEOUT_S}s waiting for $1"
    sleep 1
  done
}

section "Health"
wait_for "the API" curl -sf "$API/health" -o /dev/null
call "$API/health" | jq -c .

section "Upload"
upload() {
  local body
  body=$(curl -s -w '\n%{http_code}' -F "file=@$SAMPLES/$1" "$API/documents")
  case ${body##*$'\n'} in
    202) echo "  $1: new, queued" >&2 ;;
    200) echo "  $1: uploaded before, same document" >&2 ;;
    *) fail "upload of $1 answered HTTP ${body##*$'\n'}: ${body%$'\n'*}" ;;
  esac
  printf '%s' "${body%$'\n'*}" | jq -r .id
}
POLICY=$(upload sample-cgl-policy.pdf)
QUOTE=$(upload sample-renewal-quote.pdf)
ENDORSEMENT=$(upload sample-endorsement-additional-insured.docx)

section "Indexing"
ready() {
  local doc status
  doc=$(call "$API/documents/$1")
  status=$(jq -r .status <<<"$doc")
  [[ $status != failed ]] || fail "document $1 failed: $(jq -c '{failed_stage, error}' <<<"$doc")"
  [[ $status == ready ]]
}
for id in "$POLICY" "$QUOTE" "$ENDORSEMENT"; do
  wait_for "document $id to be ready" ready "$id"
  call "$API/documents/$id" | jq -r '"  \(.filename): ready, \(.pages) \(if .page_basis == "synthetic" then "sections" else "pages" end), \(.processing_ms) ms"'
done

section "Insights"
insights_done() { [[ $(call "$API/documents/$1/insights" | jq -r .status) != pending ]]; }
for id in "$POLICY" "$QUOTE" "$ENDORSEMENT"; do
  wait_for "insights for $id" insights_done "$id"
  call "$API/documents/$id/insights" | jq -r '
    if .status == "ready" then
      "  \(.title) (\(.document_type)): \(.key_facts | length) verified key facts, read \(.coverage.pages_read) of \(.coverage.page_count)",
      (.key_facts[:3][] | "      \(.label): \(.value) (\(.page_label))")
    else "  insights \(.status): \(.error.message // .reason)" end'
done

start_chat() {   # start_chat <document id>...
  call "$API/chats" -H 'content-type: application/json' \
    -d "$(jq -cn '{document_ids: $ARGS.positional}' --args "$@")" | jq -r .id
}

ask() {   # ask <chat id> <question>
  call "$API/chats/$1/messages" -H 'content-type: application/json' -d "$(jq -cn --arg q "$2" '{question: $q}')" |
    jq -r '"", "Q: \(.question)",
      (if .interpreted_as != .question then "   (interpreted as: \(.interpreted_as))" else empty end),
      "A: \(.answer)" + (if .status == "not_found" then "   [not found: \(.reason)]" else "" end),
      (.citations[] | "   [\(.ref)] \(.filename), \(.page_label): \"\(.quote)\""),
      "   (\(.usage.calls) model calls, \(.latency_ms) ms)"'
}

section "Chat with the policy"
CHAT=$(start_chat "$POLICY")
ask "$CHAT" "What is the property damage deductible?"
ask "$CHAT" "What is the each occurrence limit?"
ask "$CHAT" "Do defence costs reduce it?"
ask "$CHAT" "Is flood damage covered?"

section "Compare the policy with the renewal quote"
CHAT=$(start_chat "$POLICY" "$QUOTE")
ask "$CHAT" "Compare the general aggregate limit in the two documents."

section "Metrics"
call "$API/metrics/documents" | jq -c '{total, by_status, insights}'
call "$API/metrics/processing?window=24h" | jq -c '{time_to_ready_ms, chat, ai: {calls: .ai.calls, cost_usd: .ai.cost_usd}}'
