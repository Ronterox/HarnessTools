#!/bin/sh
# Parser + discovery tests on hand-built fixtures (no display needed).
# Regenerate fixtures: python3 tests/make_fixtures.py; accept output: UPDATE=1 tests/run.sh
set -eu
cd "$(dirname "$0")/.."
BIN=./build/aigource
OUT=tests/out
RAYLIB_INC=vendor/raylib-5.5_linux_amd64/include
TS_INC=vendor/tree-sitter-0.25.10/lib/include
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
      tests/fixtures/proj/s1/subagents/agent-abc123def.jsonl tests/fixtures/proj/s2.jsonl

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
bash_call() { # id (two digits: its second), command, is_error, result
    printf '{"type":"assistant","timestamp":"2026-10-14T10:00:%sZ","cwd":"%s","message":{"content":[{"type":"tool_use","id":"%s","name":"Bash","input":{"command":"%s"}}]}}\n' "$1" "$repo" "$1" "$2"
    printf '{"type":"user","timestamp":"2026-10-14T10:00:%sZ","cwd":"%s","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"%s","is_error":%s,"content":"%s"}]}}\n' "$1" "$repo" "$1" "$3" "$4"
}
{
    bash_call 01 'cat a.txt | head -5; cd sub && sed -n 1p b.txt; cat ../missing.txt 2>/dev/null' false ok
    bash_call 02 'cat c.txt && false' true 'Exit code 1'
    # Line ranges: one read per file and range, a repeated one once; tail counts from the end.
    bash_call 03 "sed -n '1,2p;4p' a.txt c.txt; head -3 a.txt; tail -n 2 c.txt; sed -n 1,2p a.txt; tail -n +2 a.txt" false ok
} > "$cfg/cmdread.jsonl"
check cmdread sh -c "$BIN --dump --root '$repo' '$cfg/cmdread.jsonl' | sed 's|$repo|REPO|g'"

# A test run piped into another command has the pipe's exit status: failed_out
# when its output shows a failure. Passing and failing piped runs (the output in
# the result block, or in toolUseResult's stdout), a trusted status (pipefail),
# FAIL in the output of a piped command that isn't the test run, a run in another
# tree (not checked), LeakSanitizer's exit-time report, "make | tail" quoted in a
# python string (no run), "pipefail" inside a here-document (not set), a
# traceback from <stdin> (an inline script), Neovim's E5113, "1 failure(s)";
# calls that never ran (ran:false).
bash_out() { # id, command, stdout: the output in toolUseResult as well
    printf '{"type":"assistant","timestamp":"2026-10-14T10:00:%sZ","cwd":"%s","message":{"content":[{"type":"tool_use","id":"%s","name":"Bash","input":{"command":"%s"}}]}}\n' "$1" "$repo" "$1" "$2"
    printf '{"type":"user","timestamp":"2026-10-14T10:00:%sZ","cwd":"%s","toolUseResult":{"stdout":"%s","stderr":"","interrupted":false},"message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"%s","is_error":false,"content":"see stdout"}]}}\n' "$1" "$repo" "$3" "$1"
}
{
    bash_call 01 'make test 2>&1 | tail -3' false 'ok   parse\n== 5 passed, 0 failed in 0.1s ==\nno errors; error_count=0'
    bash_call 02 'make 2>&1 | tail -5' false 'src/a.c:3:1: error: expected ;\nmake: *** [Makefile:9: build/a.o] Error 1'
    bash_out 03 'uv run pytest -q | tail -2' '..F.F..\n2 failed, 5 passed in 0.12s'
    bash_call 04 'set -o pipefail; make 2>&1 | tail -3' false 'FAIL parse'
    bash_call 05 'make && grep -r FAIL logs | head' false 'logs/x.log: FAIL y\nFAIL z'
    bash_out 06 'make check | grep -v ^ok' 'make: *** [Makefile:3: check] Error 2'
    bash_call 07 'cd /tmp/wt && make test 2>&1 | tail -3' false 'FAIL parse'
    bash_out 08 'make asan 2>&1 | tail -3' '==9==ERROR: LeakSanitizer: detected memory leaks\nSUMMARY: AddressSanitizer: 64 byte(s) leaked in 1 allocation(s).'
    bash_call 09 "python3 -c \\\"print('(cd x && make) | tail')\\\" | tail -1" false 'FAIL x'
    bash_call 10 'cat <<EOF > s.sh\nset -o pipefail\nEOF\nmake test 2>&1 | tail -3' false 'FAIL parse'
    bash_out 11 'make test 2>&1 | tail -4' 'Traceback (most recent call last):\n  File \"<stdin>\", line 4, in <module>\nAssertionError'
    bash_out 12 'nvim --headless -u NONE -l tests/smoke.lua 2>&1 | tail -2' 'E5113: Lua chunk: tests/smoke.lua:9: boom'
    bash_out 13 'nvim --headless -u NONE -l tests/smoke.lua 2>&1 | tail -2' 'ok   - opens\n\n1 failure(s)'
    # Never ran (ran:false, no verdict): rejected by the user, blocked by a hook, interrupted before any output.
    bash_call 14 'make test' true "The user doesn't want to proceed with this tool use. The tool use was rejected (eg. if it was a file edit, the new_string was NOT written to the file). STOP what you are doing and wait for the user to tell you how to proceed."
    bash_call 15 'make test' true 'PreToolUse:Bash hook error: blocked'
    printf '{"type":"assistant","timestamp":"2026-10-14T10:00:16Z","cwd":"%s","message":{"content":[{"type":"tool_use","id":"16","name":"Bash","input":{"command":"make test"}}]}}\n' "$repo"
    printf '{"type":"user","timestamp":"2026-10-14T10:00:16Z","cwd":"%s","toolUseResult":{"stdout":"","stderr":"","interrupted":true},"message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"16","is_error":false,"content":""}]}}\n' "$repo"
} > "$cfg/verify.jsonl"
check verify sh -c "$BIN --dump --root '$repo' '$cfg/verify.jsonl' | grep bash_end"

# Structure analysis on a throwaway repo: its first state committed (git
# baselines), today's files on disk; a session, its subagent and a resumed copy.
# A leak-checked run too when the asan build is there (make asan).
srepo="$cfg/st/repo"
mkdir -p "$srepo" "$cfg/st/st1/subagents"
cp -R tests/fixtures/structure/repo/before/. "$srepo/"
printf 'bin\000ary' > "$srepo/img.bin"
git -C "$srepo" init -q
git -C "$srepo" add -A
GIT_AUTHOR_DATE=2026-10-14T09:00:00Z GIT_COMMITTER_DATE=2026-10-14T09:00:00Z git -C "$srepo" -c user.name=t \
    -c user.email=t@t -c commit.gpgsign=false -c core.hooksPath=/dev/null commit -qm base
cp -R tests/fixtures/structure/repo/after/. "$srepo/"
for f in st1.jsonl st2.jsonl st1/subagents/agent-sub1.jsonl; do
    sed "s|/REPO|$srepo|g" "tests/fixtures/structure/$f" > "$cfg/st/$f"
done
cp tests/fixtures/structure/st1/subagents/agent-sub1.meta.json "$cfg/st/st1/subagents/"
structure() { # binary
    "$1" --structure-dump --root "$srepo" "$cfg/st/st1.jsonl" "$cfg/st/st1/subagents/agent-sub1.jsonl" \
        "$cfg/st/st2.jsonl" 2>/dev/null | python3 -c 'import json, re, sys
print(re.sub(r"\[\s+([^][{}]*?)\s+\]", lambda m: "[" + " ".join(m.group(1).split()) + "]",
             json.dumps(json.load(sys.stdin), indent=1, ensure_ascii=False)))'
}
# Functions need Neovim's C and Python grammars and textobjects queries (found
# where syntax.c looks); the replay itself is checked without them too.
has_lang() { # language: a parser and a textobjects query installed
    d=${XDG_DATA_HOME:-$HOME/.local/share}
    p=; q=
    for x in "$d/nvim/site/parser" "$d/nvim/lazy/nvim-treesitter/parser" /usr/local/lib/nvim/parser \
             /usr/lib/x86_64-linux-gnu/nvim/parser /usr/lib/nvim/parser; do
        [ -f "$x/$1.so" ] && p=1
    done
    for x in "$d/nvim/lazy/nvim-treesitter-textobjects/queries" "$d/nvim/site/pack/packer/start/nvim-treesitter-textobjects/queries" \
             "$d/nvim/site/queries" "$d/nvim/lazy/nvim-treesitter/queries" /usr/local/share/nvim/runtime/queries \
             /usr/share/nvim/runtime/queries; do
        [ -f "$x/$1/textobjects.scm" ] && q=1
    done
    [ -n "$p" ] && [ -n "$q" ]
}
if has_lang c && has_lang python; then
    check structure structure $BIN
    if [ -x build/asan/aigource ]; then
        if ASAN_OPTIONS=detect_leaks=1 structure build/asan/aigource > "$OUT/structure-asan.ndjson" &&
           diff -q tests/expected/structure.ndjson "$OUT/structure-asan.ndjson" > /dev/null; then echo "ok   structure (asan, leaks checked)"
        else echo "FAIL structure (asan)"; fail=1; fi
    fi
else
    echo "skip structure (no C and Python grammars with textobjects queries installed)"
fi

# What the structure view derives from that (metrics.c): expected JSON
# (tests/expected/structure-metrics.json), leak-checked under asan; compared with
# the draft page's own JavaScript below.
rm -f "$OUT/st-data.json" "$OUT/st-metrics.json"
st_metrics() { # binary: the metrics as pretty JSON; the snapshot left in $OUT/st-data.json
    "$1" --structure-dump --metrics "$OUT/st-metrics.json" --root "$srepo" "$cfg/st/st1.jsonl" \
        "$cfg/st/st1/subagents/agent-sub1.jsonl" "$cfg/st/st2.jsonl" > "$OUT/st-data.json" 2>/dev/null &&
    python3 -c 'import json, re, sys
print(re.sub(r"\[\s+([^][{}]*?)\s+\]", lambda m: "[" + " ".join(m.group(1).split()) + "]",
             json.dumps(json.load(sys.stdin), indent=1, ensure_ascii=False)))' < "$OUT/st-metrics.json"
}
check_json() { # name, binary: its st_metrics against tests/expected/NAME.json
    if st_metrics "$2" > "$OUT/$1.json"; then
        [ "${UPDATE:-}" = 1 ] && cp "$OUT/$1.json" tests/expected/structure-metrics.json
        if diff -u tests/expected/structure-metrics.json "$OUT/$1.json"; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi
    else echo "FAIL $1 (exit $?)"; fail=1; fi
}
if has_lang c && has_lang python; then
    check_json structure-metrics $BIN
    if [ -x build/asan/aigource ]; then
        export ASAN_OPTIONS=detect_leaks=1
        check_json "structure-metrics-asan" build/asan/aigource
        unset ASAN_OPTIONS
    else echo "skip structure-metrics (asan): no build/asan (make asan)"; fi
else
    echo "skip structure-metrics (no C and Python grammars with textobjects queries installed)"
fi
export AIG_NO_DEFS=1 # no functions: the same replay, whatever is installed
check structure-nodefs structure $BIN
if [ -x build/asan/aigource ]; then
    if ASAN_OPTIONS=detect_leaks=1 structure build/asan/aigource > "$OUT/structure-nodefs-asan.ndjson" &&
       diff -q tests/expected/structure-nodefs.ndjson "$OUT/structure-nodefs-asan.ndjson" > /dev/null; then
        echo "ok   structure-nodefs (asan, leaks checked)"
    else echo "FAIL structure-nodefs (asan)"; fail=1; fi
fi
unset AIG_NO_DEFS

# The structure screen's search end to end: a --shot of the fixture repo with
# the search box on a query and its first result gone to, as Enter does. It
# opens a window, so only on a display given for it (AIG_TEST_DISPLAY, e.g. a
# private Xephyr or Xvfb; never the inherited DISPLAY), software GL, nvim
# sockets in an empty directory.
if [ -n "${AIG_TEST_DISPLAY:-}" ]; then
    mkdir -p "$cfg/nvim"
    rm -f "$OUT/search-shot.png"
    if DISPLAY=$AIG_TEST_DISPLAY LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe AIG_NVIM_DIR="$cfg/nvim" \
       timeout 120 $BIN --root "$srepo" "$cfg/st/st1.jsonl" "$cfg/st/st2.jsonl" --seek 1 --frames 5 --mouse 5,5 \
       --structure floating --structure-search helper --structure-pick 1 --shot "$OUT/search-shot.png" \
       > "$OUT/search-shot.txt" 2>&1 && [ -s "$OUT/search-shot.png" ]
    then echo "ok   search shot ($OUT/search-shot.png)"
    else cat "$OUT/search-shot.txt"; echo "FAIL search shot"; fail=1; fi
else
    echo "skip search shot (set AIG_TEST_DISPLAY to a private X display)"
fi

# Line diff, hunks, relocation.
cc -std=c11 -D_GNU_SOURCE -Isrc -Ivendor/yyjson-0.10.0/src -I$TS_INC -o "$OUT/test_analysis" tests/test_analysis.c \
   src/analysis.c src/transcript.c src/cmdguess.c src/syntax.c src/util.c build/yyjson.o build/tree-sitter.o -ldl -lm -lpthread
"$OUT/test_analysis" || fail=1

# Rebuilds with warm caches agree with fresh builds (new events, a file changed
# on disk, a new commit, cancelled builds). Under ASan/UBSan too when the asan
# objects are there (make asan).
cc -std=c11 -D_GNU_SOURCE -Isrc -Ivendor/yyjson-0.10.0/src -I$TS_INC -o "$OUT/test_rebuild" tests/test_rebuild.c \
   src/analysis.c src/transcript.c src/cmdguess.c src/syntax.c src/event.c src/util.c build/yyjson.o build/tree-sitter.o \
   -ldl -lm -lpthread
"$OUT/test_rebuild" tests/fixtures/structure "$cfg/rebuild" || fail=1
if [ -f build/asan/yyjson.o ] && [ -f build/asan/tree-sitter.o ]; then
    cc -std=c11 -O1 -g -fno-omit-frame-pointer -fsanitize=address,undefined -D_GNU_SOURCE -Isrc -Ivendor/yyjson-0.10.0/src \
       -I$TS_INC -o "$OUT/test_rebuild_asan" tests/test_rebuild.c src/analysis.c src/transcript.c src/cmdguess.c src/syntax.c \
       src/event.c src/util.c build/asan/yyjson.o build/asan/tree-sitter.o -ldl -lm -lpthread
    if ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 "$OUT/test_rebuild_asan" tests/fixtures/structure \
       "$cfg/rebuild-asan" > "$OUT/test_rebuild_asan.txt" 2>&1
    then echo "ok   rebuild (asan, leaks checked)"
    else cat "$OUT/test_rebuild_asan.txt"; echo "FAIL rebuild (asan)"; fail=1; fi
else
    echo "skip rebuild (asan): no build/asan (make asan)"
fi

# The stage's per-turn stats (turns.c), by the analysis' rules.
cc -std=c11 -D_GNU_SOURCE -Isrc -I$RAYLIB_INC -Ivendor/yyjson-0.10.0/src -I$TS_INC -o "$OUT/test_turns" tests/test_turns.c \
   src/turns.c src/analysis.c src/transcript.c src/cmdguess.c src/syntax.c src/util.c build/yyjson.o build/tree-sitter.o \
   -ldl -lm -lpthread
"$OUT/test_turns" || fail=1

# Derived metrics on hand-built and generated snapshots (edge cases, >= boundaries,
# a seeded random corpus); each written as JSON for the parity check. Under
# ASan/UBSan too when the asan objects are there (make asan).
mkdir -p "$OUT/metrics" "$OUT/metrics-asan"
cc -std=c11 -D_GNU_SOURCE -Isrc -Ivendor/yyjson-0.10.0/src -I$TS_INC -o "$OUT/test_metrics" tests/test_metrics.c \
   src/metrics.c src/analysis.c src/transcript.c src/cmdguess.c src/syntax.c src/util.c build/yyjson.o build/tree-sitter.o \
   -ldl -lm -lpthread
"$OUT/test_metrics" "$OUT/metrics" || fail=1
if [ -f build/asan/yyjson.o ] && [ -f build/asan/tree-sitter.o ]; then
    cc -std=c11 -O1 -g -fno-omit-frame-pointer -fsanitize=address,undefined -D_GNU_SOURCE -Isrc -Ivendor/yyjson-0.10.0/src \
       -I$TS_INC -o "$OUT/test_metrics_asan" tests/test_metrics.c src/metrics.c src/analysis.c src/transcript.c src/cmdguess.c \
       src/syntax.c src/util.c build/asan/yyjson.o build/asan/tree-sitter.o -ldl -lm -lpthread
    if ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 "$OUT/test_metrics_asan" "$OUT/metrics-asan" > "$OUT/test_metrics_asan.txt" 2>&1
    then echo "ok   metrics (asan, leaks checked)"
    else cat "$OUT/test_metrics_asan.txt"; echo "FAIL metrics (asan)"; fail=1; fi
else
    echo "skip metrics (asan): no build/asan (make asan)"
fi

# metrics.c against the draft page's own JavaScript (tests/metrics_parity.js) on
# the fixture's snapshot and the unit tests' ones. Needs node and d3 7.9.0
# (vendor/, from scripts/fetch-deps.sh; or D3_JS=path); each skip is printed,
# and is a failure with REQUIRE_PARITY=1.
skip_parity() {
    if [ "${REQUIRE_PARITY:-}" = 1 ]; then echo "FAIL metrics parity $1 (REQUIRE_PARITY=1): $2"; fail=1
    else echo "skip metrics parity $1: $2"; fi
}
d3js=${D3_JS:-vendor/d3-7.9.0.min.js}
for p in st metrics/A metrics/B metrics/C metrics/R1 metrics/R2 metrics/R3; do
    if ! command -v node > /dev/null; then skip_parity "$p" "no node"
    elif [ ! -s "$d3js" ]; then skip_parity "$p" "no $d3js (scripts/fetch-deps.sh)"
    elif [ ! -s "$OUT/$p-data.json" ]; then skip_parity "$p" "no snapshot (see above)"
    elif node tests/metrics_parity.js "$OUT/$p-data.json" "$OUT/$p-metrics.json" "$d3js" > "$OUT/$p-parity.txt"; then
        echo "ok   metrics parity $p ($(tail -1 "$OUT/$p-parity.txt" | sed 's/^metrics parity: //'))"
    else cat "$OUT/$p-parity.txt"; echo "FAIL metrics parity $p"; fail=1; fi
done

# Guessing the files a failed command touched.
cc -std=c11 -D_GNU_SOURCE -Isrc -o "$OUT/test_cmdguess" tests/test_cmdguess.c src/cmdguess.c src/util.c -lm -lpthread
"$OUT/test_cmdguess" || fail=1

# A change re-read from its record (tr_change).
cc -std=c11 -D_GNU_SOURCE -Isrc -Ivendor/yyjson-0.10.0/src -o "$OUT/test_change" tests/test_change.c src/transcript.c \
   src/cmdguess.c src/util.c build/yyjson.o -lm -lpthread
"$OUT/test_change" || fail=1

# The file line a detail window row opens in nvim (detail_file_line).
cc -std=c11 -D_GNU_SOURCE -Isrc -Ivendor/yyjson-0.10.0/src -o "$OUT/test_detail" tests/test_detail.c src/transcript.c \
   src/cmdguess.c src/util.c build/yyjson.o -lm -lpthread
"$OUT/test_detail" || fail=1

# Timeline mapping unit test.
cc -std=c11 -D_GNU_SOURCE -Isrc -o "$OUT/test_timeline" tests/test_timeline.c src/timeline.c src/event.c src/util.c -lm -lpthread
"$OUT/test_timeline" || fail=1

# Change rings: sizing and tiers, label timing.
cc -std=c11 -D_GNU_SOURCE -Isrc -I$RAYLIB_INC -o "$OUT/test_rings" tests/test_rings.c src/model.c src/util.c -lm -lpthread
"$OUT/test_rings" || fail=1

# Dir layout: no crossing edges, no overlapping folders.
cc -std=c11 -D_GNU_SOURCE -Isrc -I$RAYLIB_INC -o "$OUT/test_layout" tests/test_layout.c src/model.c src/util.c -lm -lpthread
"$OUT/test_layout" || fail=1

# Structure screen geometry: treemap and partition against d3's numbers, the
# floating nodes' simulation.
cc -std=c11 -D_GNU_SOURCE -Isrc -Ivendor/yyjson-0.10.0/src -I$TS_INC -o "$OUT/test_slayout" tests/test_slayout.c \
   src/slayout.c src/util.c -lm -lpthread
"$OUT/test_slayout" || fail=1

# Search on the structure screen: names ranked like a file finder (and the
# names index against them), content hits (smart case, lines and columns, CRLF,
# UTF-8 and bytes that aren't), the worker (binary texts skipped, the cap,
# cancelled searches). Under ASan/UBSan too.
cc -std=c11 -D_GNU_SOURCE -Isrc -o "$OUT/test_ssearch" tests/test_ssearch.c src/ssearch.c src/util.c -lm -lpthread
"$OUT/test_ssearch" || fail=1
cc -std=c11 -O1 -g -fno-omit-frame-pointer -fsanitize=address,undefined -D_GNU_SOURCE -Isrc -o "$OUT/test_ssearch_asan" \
   tests/test_ssearch.c src/ssearch.c src/util.c -lm -lpthread
if ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 "$OUT/test_ssearch_asan" > "$OUT/test_ssearch_asan.txt" 2>&1
then echo "ok   ssearch (asan, leaks checked)"
else cat "$OUT/test_ssearch_asan.txt"; echo "FAIL ssearch (asan)"; fail=1; fi

# Opening code in Neovim: discovery and msgpack-RPC against headless nvims
# started in a throwaway socket dir (skipped without nvim).
if command -v nvim > /dev/null; then
    cc -std=c11 -D_GNU_SOURCE -Isrc -o "$OUT/test_nvim" tests/test_nvim.c src/nvim.c src/util.c -lm -lpthread
    "$OUT/test_nvim" || fail=1
else
    echo "skip nvim (no nvim installed)"
fi

# Syntax highlighting with Neovim's grammars (skipped where none is installed).
TS=vendor/tree-sitter-0.25.10/lib
cc -std=c11 -D_GNU_SOURCE -Isrc -I$TS/include -o "$OUT/test_syntax" tests/test_syntax.c src/syntax.c src/util.c \
   build/tree-sitter.o -ldl -lm -lpthread
"$OUT/test_syntax" || fail=1

exit $fail
