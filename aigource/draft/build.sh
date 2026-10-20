#!/bin/sh
# Structure view draft: a self-contained HTML page from a repo's Claude Code transcripts.
#   draft/build.sh [OUT.html] [REPO] [FOLDER...]   (default: out/structure.html, this repo, aigource/src aic/aic)
# The page embeds the folders' source code and your prompts: keep it private (out/ is gitignored).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
out=${1:-$here/out/structure.html}
repo=${2:-$(git -C "$here" rev-parse --show-toplevel)}
[ $# -gt 2 ] && shift 2 || set --
mkdir -p "$(dirname "$out")"
python3 -I "$here/build_data.py" "$repo" "$@" > "$here/out/data.json"
python3 -I - "$here/structure.tpl.html" "$here/out/data.json" "$out" <<'PY'
import sys
tpl, data, out = sys.argv[1:]
open(out, "w").write(open(tpl).read().replace("__DATA__", open(data).read()))
PY
echo "wrote $out"
