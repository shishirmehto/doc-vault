# Sourced by every helper. Override any value from the shell before calling a helper.
REPO=$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)
PROJECT=${VERIFY_PROJECT:-docvault-verify}
API_PORT=${VERIFY_API_PORT:-18000}
DB_PORT=${VERIFY_DB_PORT:-54339}
WEB_PORT=${VERIFY_WEB_PORT:-13000}
API=http://localhost:$API_PORT
WEB=http://localhost:$WEB_PORT
EVIDENCE_ROOT=${EVIDENCE_ROOT:-/tmp/doc-vault-verify}
RUN_ID=${RUN_ID:-$(cat "$EVIDENCE_ROOT/current" 2>/dev/null || date +%Y%m%d-%H%M%S)}
RUN_DIR=$EVIDENCE_ROOT/$RUN_ID
compose() { API_PORT=$API_PORT DB_PORT=$DB_PORT WEB_PORT=$WEB_PORT docker compose -p "$PROJECT" --project-directory "$REPO" "$@"; }
