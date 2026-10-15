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
G="git -c user.name=t -c user.email=t@t -c protocol.file.allow=always"
# A submodule with a subdir: git check-ignore refuses its paths.
mkdir -p "$tmp/subsrc/inner" && echo s > "$tmp/subsrc/inner/s" && git -C "$tmp/subsrc" init -q &&
    git -C "$tmp/subsrc" add -A && $G -C "$tmp/subsrc" commit -qm s
for i in $(seq 25); do echo $i > "$tmp/subsrc/inner/f$i"; done
git -C "$tmp/subsrc" add -A && $G -C "$tmp/subsrc" commit -qm more
printf '1\n2\n' > tracked.txt && $G submodule add -q "$tmp/subsrc" sub && $G submodule add -q "$tmp/subsrc" sub2 &&
    git add -A && $G commit -qm init && git submodule deinit -q -f sub2 # registered, empty until checked out

# Fake Claude config: one session for this repo.
slug=$(printf '%s' "$repo" | sed 's/[^A-Za-z0-9]/-/g')
proj=$tmp/cfg/projects/$slug
mkdir -p "$proj"
tx=$proj/s1.jsonl
iso() { date -u -d "${1:-now}" +%Y-%m-%dT%H:%M:%S.000Z; }
rec() { printf '%s\n' "$1" >> "$tx"; }
rec '{"type":"user","timestamp":"'$(iso)'","cwd":"'$repo'","message":{"content":"go"}}'
# A Bash from long ago that never got its result (session killed): ignored.
rec '{"type":"assistant","timestamp":"'$(iso '-700 seconds')'","message":{"content":[{"type":"tool_use","id":"b0","name":"Bash","input":{"command":"sleep 9999"}}]}}'

out=$tmp/out.ndjson
CLAUDE_CONFIG_DIR=$tmp/cfg "$BIN" --dump --all --watch --root "$repo" --follow 48 > "$out" &
pid=$!
sleep 1.5
S=3 # > settle time

# 1) You: create, then edit (+2), atomic save of a tracked file.
printf 'a\nb\nc\n' > mine.txt; sleep $S
printf 'd\ne\n' >> mine.txt; sleep $S
printf '1\n2\n3\n' > .tracked.tmp && mv .tracked.tmp tracked.txt; sleep $S
# Checking out a submodule mid-run: its files aren't this repo's changes, and
# gitignore keeps working after git refused all those paths.
$G submodule update -q --init sub2; echo l > after.log
# A newline in a name must not shift later ignore answers.
printf 'w\n' > "$(printf 'we\nird.txt')"; sleep 0.3; echo a > a.log; sleep 0.3; echo b > b.txt; sleep $S
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
# A Bash that writes early and answers much later (20 s): still the agent's alone.
rec '{"type":"assistant","timestamp":"'$(iso)'","message":{"content":[{"type":"tool_use","id":"b2","name":"Bash","input":{"command":"gen && make test"}}]}}'
sleep 1; printf 'l\n' > long.out; sleep $S
rec '{"type":"user","timestamp":"'$(iso '+20 seconds')'","message":{"content":[{"type":"tool_result","tool_use_id":"b2","content":"ok"}]},"toolUseResult":{"stdout":"","bashEditDiff":{"files":[{"filePath":"'$repo'/long.out","hunks":[{"lines":["+l"]}],"created":true}],"changedFiles":[]}}}'
# 5) A failed command records no changes. Its guess (sed -i tracked.txt), once
# the watcher sees the change, is a real edit; a guess the watcher doesn't see
# (nochange.txt) is dropped; a change nothing guessed (script.out) is still the
# agent's, not yours.
rec '{"type":"assistant","timestamp":"'$(iso)'","message":{"content":[{"type":"tool_use","id":"b3","name":"Bash","input":{"command":"sed -i 1d tracked.txt && false"}}]}}'
sleep 1; printf '2\n3\n' > tracked.txt; sleep 0.5
rec '{"type":"user","timestamp":"'$(iso)'","message":{"content":[{"type":"tool_result","tool_use_id":"b3","is_error":true,"content":"Error: Exit code 1"}]},"toolUseResult":"Error: Exit code 1"}'
sleep $S
rec '{"type":"assistant","timestamp":"'$(iso)'","message":{"content":[{"type":"tool_use","id":"b4","name":"Bash","input":{"command":"sed -i 1d nochange.txt; ./gen.sh"}}]}}'
sleep 1; printf 'q\n' > script.out; sleep 0.5
rec '{"type":"user","timestamp":"'$(iso)'","message":{"content":[{"type":"tool_result","tool_use_id":"b4","is_error":true,"content":"Error: Exit code 1"}]},"toolUseResult":"Error: Exit code 1"}'
wait $pid

# File events from both sources: the agent's (transcript) and yours (watcher).
got=$(python3 - "$out" <<'PY'
import json, sys
for l in open(sys.argv[1]):
    e = json.loads(l)
    if e["kind"] in ("turn", "bash", "failed"): continue
    print(e["actor"], e["kind"], e["path"].replace("\n", "\\n"), "+%d-%d" % (e.get("add", 0), e.get("del", 0)))
PY
)
want="you create mine.txt +3-0
you edit mine.txt +2-0
you edit tracked.txt +1-0
you create we\nird.txt +1-0
you create b.txt +1-0
you create newdir/c.txt +1-0
you delete mine.txt +0-5
s1 edit agent.txt +1-0
s1 create gen.out +2-0
you create notes.txt +1-0
s1 create long.out +1-0
s1 edit tracked.txt +0-1
s1 create script.out +1-0"
if [ "$got" = "$want" ]; then echo "ok   watch"; else
    echo "FAIL watch"; echo "--- want"; echo "$want"; echo "--- got"; echo "$got"; exit 1; fi
