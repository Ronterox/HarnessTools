#!/usr/bin/env bash
# Record a demo session: Claude Code (opus 5.5) builds the app in demo/app headless.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
app=$here/demo/app
mkdir -p "$app"
cd "$app"
claude -p --model claude-opus-5-5 --permission-mode bypassPermissions \
  --output-format json "$(cat "$here/demo/SPEC.md")" > "$here/demo/record.out.json"
echo "done: $(ls "$app" | wc -l) entries in $app"
