#!/usr/bin/env bash
# Download the retrieval eval's public documents into DIR (default evals/retrieval/docs) and check each sha256
# against evals/retrieval/documents.yaml. A file already there with the right sha256 is kept; any mismatch fails.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
dir=${1:-$root/evals/retrieval/docs}
mkdir -p "$dir"

if command -v sha256sum > /dev/null; then
  sha256() { sha256sum "$1" | cut -d' ' -f1; }
else
  sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }
fi

manifest=$(uv run --project "$root" python -c '
import sys, yaml
for d in yaml.safe_load(open(sys.argv[1]))["documents"]:
    print(d["file"], d["sha256"], d["url"])
' "$root/evals/retrieval/documents.yaml")

failed=0
while read -r file sha url; do
  path=$dir/$file
  if [[ -f $path && $(sha256 "$path") == "$sha" ]]; then
    echo "ok        $file"
    continue
  fi
  if ! curl --fail --location --silent --show-error --retry 3 --output "$path.part" "$url"; then
    echo "FAILED    $file: could not download $url" >&2
    rm -f "$path.part"
    failed=1
    continue
  fi
  got=$(sha256 "$path.part")
  if [[ $got != "$sha" ]]; then
    echo "MISMATCH  $file: got sha256 $got, the manifest says $sha ($url)" >&2
    rm -f "$path.part"
    failed=1
    continue
  fi
  mv "$path.part" "$path"
  echo "fetched   $file"
done <<< "$manifest"

exit $failed
