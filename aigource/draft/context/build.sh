#!/bin/sh
# Context view draft: a self-contained HTML page from a repo's Claude Code transcripts.
#   draft/context/build.sh [OUT.html] [REPO] [SESSION_ID... | --all | --recent N]
#   (default: out/context.html, this repo, the most recent session, kept under 15 MB)
# The page embeds prompts, source and command output: keep it private (out/ is gitignored).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
out=${1:-$here/out/context.html}
repo=${2:-$(git -C "$here" rev-parse --show-toplevel)}
[ $# -gt 2 ] && shift 2 || set --
mkdir -p "$(dirname "$out")" "$here/out"
python3 -I "$here/build_ctx.py" "$repo" --max-mb "${CTX_MAX_MB:-15}" "$@" > "$here/out/data.json"
python3 -I - "$here/context.tpl.html" "$here/out/data.json" "$out" <<'PY'
import sys
tpl, data, out = sys.argv[1:]
open(out, "w").write(open(tpl).read().replace("__DATA__", open(data).read()))
PY
echo "wrote $out ($(du -h "$out" | cut -f1))"
