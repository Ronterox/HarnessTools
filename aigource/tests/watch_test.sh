#!/bin/sh
# Filesystem watcher + attribution, end to end in a throwaway repo (~25s: each
# change has to settle). Usage: tests/watch_test.sh
set -eu
cd "$(dirname "$0")/.."
BIN=$PWD/build/aigource
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
repo=$tmp/repo
mkdir -p "$repo" && cd "$repo"
git init -q && printf 'ignored/\n*.log\n' > .gitignore
printf '1\n2\n' > tracked.txt && git add -A && git -c user.name=t -c user.email=t@t commit -qm init

# Fake Claude config: one session for this repo.
slug=$(printf '%s' "$repo" | sed 's/[^A-Za-z0-9]/-/g')
proj=$tmp/cfg/projects/$slug
mkdir -p "$proj"
tx=$proj/s1.jsonl
iso() { date -u +%Y-%m-%dT%H:%M:%S.000Z; }
rec() { printf '%s\n' "$1" >> "$tx"; }
rec '{"type":"user","timestamp":"'$(iso)'","cwd":"'$repo'","message":{"content":"go"}}'

out=$tmp/out.ndjson
CLAUDE_CONFIG_DIR=$tmp/cfg "$BIN" --dump --all --watch --root "$repo" --follow 28 > "$out" &
pid=$!
sleep 1.5
S=3 # > settle time

# 1) You: create, then edit (+2), atomic save of a tracked file.
printf 'a\nb\nc\n' > mine.txt; sleep $S
printf 'd\ne\n' >> mine.txt; sleep $S
printf '1\n2\n3\n' > .tracked.tmp && mv .tracked.tmp tracked.txt; sleep $S
# 2) Noise: ignored dir, ignored pattern, editor swap, born-and-died temp file.
mkdir -p ignored && echo x > ignored/x; echo y > debug.log; echo z > .mine.txt.swp
echo t > tmpfile && rm tmpfile; sleep $S
# 3) New folder with a file in it right away; then a delete.
mkdir -p newdir && printf 'n\n' > newdir/c.txt; sleep $S
rm mine.txt; sleep $S
# 4) Agent: an Edit it records itself (dup), and a file written while its Bash runs.
rec '{"type":"assistant","timestamp":"'$(iso)'","message":{"content":[{"type":"tool_use","id":"e1","name":"Edit","input":{"file_path":"'$repo'/agent.txt"}}]}}'
printf 'x\n' > agent.txt
rec '{"type":"user","timestamp":"'$(iso)'","message":{"content":[{"type":"tool_result","tool_use_id":"e1","content":"ok"}]},"toolUseResult":{"originalFile":"","structuredPatch":[{"lines":["+x"]}]}}'
# While its Bash runs, it writes gen.out and you save notes.txt: both must wait
# for the result, whose bashEditDiff claims gen.out only.
rec '{"type":"assistant","timestamp":"'$(iso)'","message":{"content":[{"type":"tool_use","id":"b1","name":"Bash","input":{"command":"make gen"}}]}}'
sleep 1; printf 'g\ng\n' > gen.out; printf 'n\n' > notes.txt; sleep $S
rec '{"type":"user","timestamp":"'$(iso)'","message":{"content":[{"type":"tool_result","tool_use_id":"b1","content":"ok"}]},"toolUseResult":{"stdout":"","bashEditDiff":{"files":[{"filePath":"'$repo'/gen.out","hunks":[{"lines":["+g","+g"]}],"created":true}],"changedFiles":["'$repo'/gen.out"]}}}'
wait $pid

# File events from both sources: the agent's (transcript) and yours (watcher).
got=$(python3 - "$out" <<'PY'
import json, sys
for l in open(sys.argv[1]):
    e = json.loads(l)
    if e["kind"] in ("turn", "bash"): continue
    print(e["actor"], e["kind"], e["path"], "+%d-%d" % (e.get("add", 0), e.get("del", 0)))
PY
)
want="you create mine.txt +3-0
you edit mine.txt +2-0
you edit tracked.txt +1-0
you create newdir/c.txt +1-0
you delete mine.txt +0-5
s1 edit agent.txt +1-0
s1 create gen.out +2-0
you create notes.txt +1-0"
if [ "$got" = "$want" ]; then echo "ok   watch"; else
    echo "FAIL watch"; echo "--- want"; echo "$want"; echo "--- got"; echo "$got"; exit 1; fi
