#!/usr/bin/env bash
# sql.sh "SELECT ...". Runs read-only SQL in this project's database and saves the output as evidence.
set -euo pipefail
source "$(dirname "$0")/env.sh"
mkdir -p "$RUN_DIR"
out=$RUN_DIR/sql-$(date +%H%M%S).txt
{ echo "-- $1"; compose exec -T db psql -U vault -d vault -v ON_ERROR_STOP=1 -c "BEGIN READ ONLY" -c "$1" -c "ROLLBACK"; } | tee "$out"
