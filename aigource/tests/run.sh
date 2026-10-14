#!/bin/sh
# Parser + discovery tests on hand-built fixtures (no display needed).
# Regenerate fixtures: python3 tests/make_fixtures.py; accept output: UPDATE=1 tests/run.sh
set -eu
cd "$(dirname "$0")/.."
BIN=./build/aigource
OUT=tests/out
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

# Discovery: project dir found from the root's slug, subagents picked up.
mkdir -p "$cfg/projects"
ln -s "$PWD/tests/fixtures/proj" "$cfg/projects/-proj"
check discover env CLAUDE_CONFIG_DIR="$cfg" $BIN --dump --all --root /proj

# Timeline mapping unit test.
cc -std=c11 -D_GNU_SOURCE -Isrc -o "$OUT/test_timeline" tests/test_timeline.c src/timeline.c src/event.c src/util.c -lm -lpthread
"$OUT/test_timeline" || fail=1

exit $fail
