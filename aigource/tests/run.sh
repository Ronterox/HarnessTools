#!/bin/sh
# Parser + discovery tests on hand-built fixtures (no display needed).
# Regenerate fixtures: python3 tests/make_fixtures.py; accept output: UPDATE=1 tests/run.sh
set -eu
cd "$(dirname "$0")/.."
BIN=./build/aigource
OUT=tests/out
RAYLIB_INC=vendor/raylib-5.5_linux_amd64/include
mkdir -p "$OUT"
fail=0
cfg=$(mktemp -d)
trap 'rm -rf "$cfg"' EXIT

check() { # name, then command producing NDJSON
    name=$1; shift
    if ! "$@" > "$OUT/$name.ndjson"; then
        echo "FAIL $name (exit $?)"; fail=1; return 0
    fi
    if [ "${UPDATE:-}" = 1 ]; then cp "$OUT/$name.ndjson" "tests/expected/$name.ndjson"; fi
    if diff -u "tests/expected/$name.ndjson" "$OUT/$name.ndjson"; then echo "ok   $name"
    else echo "FAIL $name"; fail=1; fi
}

# Explicit files: every parser rule.
check parse $BIN --dump --root /proj tests/fixtures/proj/s1.jsonl \
      tests/fixtures/proj/s1/subagents/agent-abc123def.jsonl

# Detail view: every kind of step in full (diffs, files, commands, tools).
check detail $BIN --dump --detail --root /proj tests/fixtures/detail/d1.jsonl

# Discovery: project dir found from the root's slug, subagents picked up.
mkdir -p "$cfg/projects"
ln -s "$PWD/tests/fixtures/proj" "$cfg/projects/-proj"
check discover env CLAUDE_CONFIG_DIR="$cfg" $BIN --dump --all --root /proj

# Files commands read, in a throwaway repo (reads count only for files on
# disk): a.txt and sub/b.txt read, a missing probe dropped, a failed command's
# reads ignored.
repo="$cfg/repo"
mkdir -p "$repo/sub"
echo a > "$repo/a.txt"; echo b > "$repo/sub/b.txt"; echo c > "$repo/c.txt"
bash_call() { # id, command, is_error, result
    printf '{"type":"assistant","timestamp":"2026-10-14T10:00:0%sZ","cwd":"%s","message":{"content":[{"type":"tool_use","id":"%s","name":"Bash","input":{"command":"%s"}}]}}\n' "$1" "$repo" "$1" "$2"
    printf '{"type":"user","timestamp":"2026-10-14T10:00:0%sZ","cwd":"%s","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"%s","is_error":%s,"content":"%s"}]}}\n' "$1" "$repo" "$1" "$3" "$4"
}
{
    bash_call 1 'cat a.txt | head -5; cd sub && sed -n 1p b.txt; cat ../missing.txt 2>/dev/null' false ok
    bash_call 2 'cat c.txt && false' true 'Exit code 1'
} > "$cfg/cmdread.jsonl"
check cmdread sh -c "$BIN --dump --root '$repo' '$cfg/cmdread.jsonl' | sed 's|$repo|REPO|g'"

# Guessing the files a failed command touched.
cc -std=c11 -D_GNU_SOURCE -Isrc -o "$OUT/test_cmdguess" tests/test_cmdguess.c src/cmdguess.c src/util.c -lm -lpthread
"$OUT/test_cmdguess" || fail=1

# Timeline mapping unit test.
cc -std=c11 -D_GNU_SOURCE -Isrc -o "$OUT/test_timeline" tests/test_timeline.c src/timeline.c src/event.c src/util.c -lm -lpthread
"$OUT/test_timeline" || fail=1

# Change rings: sizing and tiers, label timing.
cc -std=c11 -D_GNU_SOURCE -Isrc -I$RAYLIB_INC -o "$OUT/test_rings" tests/test_rings.c src/model.c src/util.c -lm -lpthread
"$OUT/test_rings" || fail=1

# Dir layout: no crossing edges, no overlapping folders.
cc -std=c11 -D_GNU_SOURCE -Isrc -I$RAYLIB_INC -o "$OUT/test_layout" tests/test_layout.c src/model.c src/util.c -lm -lpthread
"$OUT/test_layout" || fail=1

# Syntax highlighting with Neovim's grammars (skipped where none is installed).
TS=vendor/tree-sitter-0.25.10/lib
cc -std=c11 -D_GNU_SOURCE -Isrc -I$TS/include -o "$OUT/test_syntax" tests/test_syntax.c src/syntax.c src/util.c \
   build/tree-sitter.o -ldl -lm -lpthread
"$OUT/test_syntax" || fail=1

exit $fail
