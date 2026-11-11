#!/usr/bin/env bash
# Remove only the stack launch.sh started: its containers, network and volumes. Evidence stays in $EVIDENCE_ROOT.
set -euo pipefail
source "$(dirname "$0")/env.sh"
if [ ! -e "$EVIDENCE_ROOT/.started-$PROJECT" ]; then
  echo "launch.sh did not start project $PROJECT here; leaving it alone" >&2; exit 1
fi
compose down -v --remove-orphans
rm -f "$EVIDENCE_ROOT/.started-$PROJECT" "$EVIDENCE_ROOT/current"
echo "removed $PROJECT; evidence kept in $RUN_DIR"
ls "$RUN_DIR"
