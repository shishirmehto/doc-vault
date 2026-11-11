#!/usr/bin/env bash
# Read-only: is this instance worth driving? Exit 0 only when every check passes.
set -uo pipefail
source "$(dirname "$0")/env.sh"
ok=0
say() { printf '%-6s %s\n' "$1" "$2"; if [ "$1" = FAIL ]; then ok=1; fi; }
for s in db redis api worker beat; do
  state=$(compose ps --format '{{.Service}} {{.State}}' 2>/dev/null | awk -v s="$s" '$1==s{print $2}')
  [ "$state" = running ] && say OK "$s running" || say FAIL "$s is '${state:-absent}' in project $PROJECT"
done
web=$(compose ps --format '{{.Service}} {{.State}}' 2>/dev/null | awk '$1=="web"{print $2}')
[ -n "$web" ] && say INFO "web $web on $WEB" || say INFO "web not started (API-only run)"
bound=$(compose port api 8000 2>/dev/null)
[ "${bound##*:}" = "$API_PORT" ] && say OK "port $API_PORT belongs to $PROJECT/api" || say FAIL "port $API_PORT is not this project's api (got '${bound:-none}')"
h=$(curl -sS -m 5 -w ' %{http_code}' "$API/health" 2>/dev/null)
case "$h" in
  *'"db":"ok"'*'"redis":"ok"'*' 200') say OK "health $h";;
  *) say FAIL "health ${h:-no answer}";;
esac
compose logs worker 2>/dev/null | grep -q 'ready\.' && say OK "worker consuming" || say FAIL "worker never logged 'ready.'"
img=$(docker image inspect -f '{{.Created}}' "$PROJECT-api" 2>/dev/null | cut -c1-19)
src=$(TZ=UTC git -C "$REPO" log -1 --format=%cd --date=iso-strict-local -- docvault | cut -c1-19)
dirty=$(git -C "$REPO" status --short -- docvault | wc -l | tr -d ' ')
if [[ "$img" < "$src" ]]; then say FAIL "api image ($img UTC) is older than the last docvault commit ($src UTC): cleanup.sh, then launch.sh"; else say OK "api image ($img UTC) is newer than the last docvault commit ($src UTC)"; fi
[ "$dirty" = 0 ] || say INFO "$dirty uncommitted files in docvault/ are not in the image unless launch.sh ran after them"
exit $ok
