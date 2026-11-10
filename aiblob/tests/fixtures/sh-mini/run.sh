#!/usr/bin/env bash
# Build, report and serve the project.
set -euo pipefail
cd "$(dirname "$0")"
source lib/common.sh
PORT="${PORT:-8000}"
OUT=build

usage() {
    echo "usage: run.sh [build|report|serve]" >&2
}

report() {
    log "report on $PORT"
    .venv/bin/python tools/report.py --config config.toml > "$OUT/report.txt" || die "report failed"
    python3 -m tools.gen
}

serve() {
    MODE=prod node web/app.js --port "$PORT"
}

trap cleanup EXIT

case "${1:-build}" in
    build) bash scripts/build.sh ;;
    report) report ;;
    serve) serve ;;
    *) usage; exit 2 ;;
esac
cat data/in.txt | head -1
