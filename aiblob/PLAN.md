# aiblob — see what the AI did to the *application*, not the code

Sandbox plan, 2026-11-05. Decisions from the interview are fixed; everything
else is a judgment call noted inline.

## The claim to prove

While an agent edits a repo, a single picture of the application's dataflow
updates in realtime: blobs (entry points + everything they reach), lines
(calls on the main path, calls from error branches, shared data), and hot
spots on blobs sized to the change. You spot "login is wrong" from the map,
drill one level into functions, read a snippet at most, and only then open
aigource to ask why.

## Decisions (interview)

| Question | Answer |
|---|---|
| Blob unit | entry point + reachable code; code reached from ≥2 entries is a shared hub |
| Derivation | tree-sitter + heuristics first; qwen pass last (llama-server, `~/.cache/models/Qwen3.5-4B-Q4_K_M.gguf`), cached per blob hash |
| Demo data | a fresh session: `claude -p --model claude-opus-5-5` builds a Python/Flask app with auth into `aiblob/demo/app` |
| Surface | standalone web canvas replaying a JSON timeline |
| Edges | solid = main-path call, dashed red = call from except/error branch, dotted = shared data |
| Spots | fading recency, scrubber + play |
| Drill-down | sub-map: functions as blobs, data edges, click → 1–3 line snippet |
| aigource link | none yet |
| Languages | Python now, heuristics tuned; LSP for dataflow later |

## Pipeline

```
record.sh      claude -p (opus 5.5) builds the app → ~/.claude/projects/…/<sid>.jsonl
replay.py      jsonl → ordered Write/Edit/MultiEdit steps → file snapshot per step
analyze.py     snapshot → tree-sitter → modules, defs, imports, calls, routes,
               except-regions, data symbols → entry points → blobs, edges, metrics
label.py       heuristics (keyword → emoji, kind) then optional qwen pass
timeline.py    per-step blob diff → spots; writes web/timeline.json
web/index.html canvas: blobs, edges, spots, scrubber, drill-down
```

### Entry points (the "one entry the imports flow to")
Module import graph; an entry module is one nobody imports whose transitive
imports cover the most of the repo (`app.py`/`main.py`/`__main__`). Since 2026-11-10 every
module with a `__main__` guard is an entry of its own too (one per program, id `<module>.<module>`,
stable whatever the other modules do), and the biggest-closure pick only looks at modules
without a guard (Flask's app run by `flask run`); SCHEMA.md "Python entries". Inside it,
each route handler (`@app.route`, `@bp.route`, `@router.get`…), CLI command,
or top-level call becomes a blob root. A function reachable from one root
belongs to that blob; from several roots it joins a hub blob named after its
module (`db`, `models`). Unreached code is a grey "orphan" blob so dead code
the AI left behind is visible.

### Data, not ints
Data symbols = classes, module globals, SQL table names in string literals
that read like a statement (verb first, its shape, no docstrings, no English
words as tables; 2026-11-10), request/session/db attribute roots
(`request.form`, `session["user"]`, `db.users`; Flask's only where imported). Two blobs share a dotted edge when they touch the same symbol.
Local scalars are ignored.

### Error paths
A call inside `except`, `if … is None: return/raise/abort`, or whose callee is
`abort`/`raise`/`logger.error`/`flash(error)` is an error-path edge.

### Metrics per blob
size = reachable lines; complexity = branches + handlers; spot = lines added +
removed in this step, radius relative to blob size, hue added/removed/mixed,
fading over the next 6 steps.

## Milestones
1. record.sh + replay.py: snapshots per step from a real session.
2. analyze.py on the final snapshot: blobs/edges look right by hand.
3. timeline.py + web: play the session; spots and growth read at a glance.
4. drill-down sub-map + snippets.
5. label.py qwen pass behind a flag; measure latency per changed blob.

## Out of scope for the sandbox
Redirecting the agent, aigource deep links, non-Python, LSP dataflow.

## Backlog (2026-11-05, order to be decided by Ricardo)
- Stains: spots only for deletions (red) and modifications (yellow), persistent with scrubber age, no spot for pure additions; drill-down shows the stain on the exact function.
- Unreferenced text files (README, LICENSE, docs): one white blob per file, no edges, own region. Referenced non-code files (templates, configs, static) linked by string literals that resolve to a path in the snapshot; pale page blobs attached to the code that references them.
- Callback resolution heuristics: function-as-argument (deferred call edge), imports inside functions, assignment-from-constructor local typing, dunder calls (__call__/__enter__/__iter__). Track the unresolved-call ratio per run; LSP only later, as background enrichment for the residue.
- Fixtures: Flask demo frozen as the middle one-shot app. Add a tiny 3-file script in another language with exactly asserted blobs/edges, plus a golden end state for the Flask app.
- Languages: JS/TS next (test-webapp), Zig or Go for mid-level (recorded sessions exist), C for low-level with aigource and this repo's real sessions as the big-project run.
- Performance harness: per-step timings for parse, analysis, timeline write and page frame cost, logged to CSV; run on the big project.
- argparse subcommands (and equivalents) as roots so CLI apps do not collapse into one blob.
- Model comparison: Qwen3.5-4B vs LFM3-1.7B on the frozen Flask timeline and the small script; cold caches, speed from server logs, fragment/duplicate rates, blind quality rubric; serve-llm.sh takes a model argument.

## Round 2 outcome (2026-11-05, later)
Done: stains, white text blobs + referenced file blobs with ref edges, callback heuristics + unresolved metric
(Flask 2/148), argparse/click subcommand roots (aic → 20 command blobs, 0 orphans), tests (`make test`, 34),
JavaScript via lang_js.py (test-webapp + js-mini fixture), perf harness (`make perf`, demo/perf/), `_unplaced` 20% faster,
page NaN guard. LFM3 comparison blocked: installed llama-server (build 8770) lacks the `lfm3` architecture; needs a
llama.cpp upgrade (root), then `scratchpad run_model.sh` reproduces. 10 non-code blobs unlabelled: GPU was busy.

## Backlog, next
- Big projects: done in round 3 (below).
- JS: regex-matched routes, `app.route().get()` chains, closure-captured state as data symbols.
- Zig/Go (mid) and C (low) languages; C against aigource with real sessions.
- Labels: duplicate titles across blobs (pass the sibling titles in the prompt), the 10 non-code blobs.
- Tooltip covering a file card: done in round 3.

## Round 3 outcome (2026-11-05, evening): big projects
Done: incremental analysis, delta timeline (schema v3), page rebuilds state from deltas, tooltip hides when a card opens.

- **Incremental analysis** (`analyze(files, cache, memo)`, default in timeline.py; `--full` = old per-step recompute).
  Per function, call resolution (+ unresolved counts, registers, subcommands), data symbols, snippet and literal → file
  matches are reused from the previous step. Soundness: every lookup the resolver and `_globals_used` make into a
  cross-module table (function quals, names → quals, class names, class keys, JS aliases, self-attribute types, import
  graph rows, module set / prefixes) is recorded as a key; each step diffs those tables against the previous step's and
  redoes a function when its module was re-parsed (new Func object) or one of its keys changed. This is finer than
  "modules that import a changed module": the unique-method-name fallback and the global class-name table make a
  module depend on modules it never imports (tests/test_incremental.py has that case). Also reused: module parses by
  str identity (no re-hash), import-graph rows, one-hop file literals. Roots, reachability, blobs and edges are still
  recomputed every step (cheap: ~5 ms each phase); no part falls back to a full recompute. GC: the timeline loop
  collects the young generations and `gc.freeze()`s after each step; full collections rescanning the long-lived
  heap cost ~100 ms per step otherwise.
- **Correctness**: incremental == `--full` byte for byte: Flask session, a crafted Python sequence (symbol changes
  reaching non-importing modules, new globals, deletions, a README), the js-mini fixture built and edited step by step
  (`tests/test_incremental.py`), and all 71 TechnicalAnalysis steps (checked in one process, same snapshots).
  `--v2` output is byte-identical to the round-2 writer; `expand(v3) == v2`; web/index.html's `expandSteps()` equals
  `timeline.expand()` on the Flask, sample and TechnicalAnalysis timelines. Old vs new page on Flask and the sample:
  same blobs, deltas, edges (incl. data spokes), stains, ghosts, marks, data nodes and layout positions at every step.
- **Delta timeline v3** (SCHEMA.md "v3"): step 0 full, then `blobs` (new/changed), `removed_blobs`, `edges_add`,
  `edges_remove`, `order` only when the blob order is not derivable. A blob whose state is `same` is re-sent when
  anything else about it changed (lines shifted by an edit above it, a call resolving elsewhere), so rebuilding is
  exact. `timeline.compact()` / `expand()`; web/sample-timeline.json converted (edges sorted, 603 KB → 96 KB).
- **Page**: one path, `expandSteps()` rebuilds each step's full state (unchanged blobs share one zeroed copy);
  data-spoke flags moved from the shared edge objects to a per-step set. Layout: `collide()` checks only pairs a grid
  puts within reach, in the same order (bit-identical positions); maps over 200 blobs relax in proportionally fewer
  passes (Flask/sample unaffected), which took the TechnicalAnalysis layout from ~16 s to ~3.4 s in headless Chrome.

Numbers (demo/perf/*-r3*.csv; before = round-2 code, same machine, same day; TechnicalAnalysis seeded, 71 steps):

| | before | after (incremental) | after `--full` |
|---|---|---|---|
| TA step p50 / mean after step 0 | 976 / 983 ms | 224 / 272 ms quiet, 308–367 / 358–408 ms with the desktop busy | 1065 / 1089 ms |
| TA timeline.json | 356.7 MB | 6.3 MB | 6.3 MB |
| TA load in node (parse + rebuild) | 2.7 s parse, 661 MB heap | 0.05 s parse + 0.2 s expand + 0.14 s maps, 53 MB | |
| TA page in headless Chrome, ready | 21.4 s, 1132 MB heap | 4.7 s (json 0.4, rebuild 0.75, layout 3.4), 50 MB | |
| Flask step p50 / mean | 6.2 / 7.5 ms | 3.2 / 5.3 ms | 6.8 / 7.9 ms |
| Flask timeline.json | 0.56 MB | 0.11 MB | |

The 0.3 s/step target holds on a quiet machine (p50 0.22 s, mean 0.27 s) and is borderline with other load (p50
0.31–0.37 s); incremental is a steady ~3.4x faster than `--full` on the same machine. p95 (0.5–0.8 s) are edits to
workbench/server.py: its re-parse (~150 ms) plus re-resolving its few hundred functions. Step 0 (whole-tree parse)
stays ~6 s. Note the TA tree on disk moved between runs (159.2k → 159.5k lines), so before/after inputs differ slightly.

Known gaps: label.py still reads a timeline as full-state steps; on v3 it labels the blobs each step sends (which is
every blob version, so the page shows the same labels), but its sibling-title context uses the last step's delta
instead of the full last state; it should call `timeline.expand()`. Next speed-ups if needed: reuse unchanged
func dicts between analyze/build_state/compact (~120 ms/step of dict building and comparing), content-keyed reuse
for functions of a re-parsed module whose text did not change.


## Round 4 outcome (2026-11-05)
- C entry split (`CLang.split_entry`): programs whose entry reaches >=20 functions get `command` roots from main's
  direct callees; when one callee owns >50% of the entry, descend one level (depth cap 2; `depth` on the entry blob).
  aigource: 1 blob of 1,293 funcs -> 53 commands + 30 hubs (biggest: hub:src.context 187, sv_frame 182);
  web/timeline-aigource.json 53 MB -> 11.8 MB. Verified incremental == --full on 401 steps.
- Go (`lang_go.py`, tree-sitter-go 0.25): net/http, gin, chi, gorilla, fiber, echo routes; cobra/urfave commands;
  _test.go blobs; error tails; package vars, env, SQL, embed/ParseFiles refs; external = stdlib + imports not in go.mod
  module. go-mini fixture + 17 tests; AgenticExplorer (44 .go files): 519 calls / 0 unresolved, parse 149 ms.
- 115 tests. Nothing committed.

## Sessions outcome (2026-11-07)
- Real transcripts replace the stand-in: `web/timeline-aigource.json` is built from both project dirs with
  `timeline.py ~/.claude/projects/-home-rontero-Documents-Projects-AIProjects-HarnessTools
  ~/.claude/projects/-home-rontero-Documents-Projects-AIProjects-HarnessTools-aigource --root ../aigource --seed-git
  --git-reconcile --no-timings -o web/timeline-aigource.json --sessions-out demo/aigource-sessions.json`.
- 1,270 steps: 1,240 AI + 30 git; 106 `applied: false`; 180 blobs; 44.1 MB; incremental == `--full`; final tree ==
  aigource HEAD (143/143). Sessions: ee491c71 842, 10276891 210, 8e130533 156, a3ff323b 21, f29e0cd7 11 (Oct 27).
  738291ad and d0c6def7 have none: their steps were tests run from aic / undograft that saw ee491c71's changes
  (SCHEMA.md Sessions: observer rule, call rank, blind reports).
- 2026-11-08 rebuild after the rank / merge fixes (`git merge-file` three-way merge for stale-text retries):
  byte-identical to the build above; the real data reaches merge3 6 times (4 clean with nothing to merge, 2
  conflicts that end where they did before).
- 2026-11-08 labels, full history (`label.py --per-step`, Qwen3.5-4B, reasoning off): 711/711 steps ok, 0 failed,
  1,221 blob versions asked, 1,656 calls, 1.40 s per call; 2 h 20 min wall, of which 39 min decoding and 1 h 42 min
  thermal pauses (RTX 2060, pause at 85 °C until < 80 °C). Final step 179/180 blobs labelled (the fixture
  `tests/fixtures/titles/t1.jsonl` is not), 99.93% of all step-blob versions. llama-server needs `--cache-ram 0`:
  its default 8 GB host prompt cache grew to 8.5 GB RSS and starved the machine.

## Backlog, open edges (documented in SCHEMA.md Sessions)
- cds(): `{ cd a; } | cat` keeps the cd; a cd in an if-branch that never runs is tracked.
- Rank: an unrelated cwd whose command mentions root's basename counts as under root; a bare basename after a
  `cd` inside the same command is not resolved.
- diff3 limit: a report that already carried the snapshot's change on repeated lines (`}`) can get it twice.

## Backlog, next
- Go: plain reads of another file's package var need a recorded dependency hook in analyze.py (incremental safety).
- C: a wrapper chain `run(){loop();}` spends both depth levels; consider not counting pass-through wrappers as a level.
- ~~Per-function deltas inside re-sent blobs (aigource timeline 44.1 MB).~~ Done 2026-11-08, schema v4 (SCHEMA.md
  "v4"): a re-sent blob's `funcs` is a patch (new functions whole, changed ones as qual + changed keys,
  `funcs_remove`, `funcs_order` when needed). aigource map, 1270 steps: 44.5 MB → 8.3 MB compact (gzip 6.9 → 1.2 MB),
  76.8 → 13.6 MB on disk as label.py writes it; node load (perf.page_cost) parse+expand+maps 1.06 → 0.58 s, heap
  182 → 122 MB, peak RSS 497 → 314 MB (expand alone 0.19 → 0.22 s). Demo: 114 → 95 KB compact. expand(v4) ==
  expand(v3) on every aigource step (Python and the page's expandSteps() in node); labels 1221 cache hits, 0 calls.
- Canvas clips long titles visually; headless-only perf numbers for the page.

## Areas, merges and cuts outcome (2026-11-08): schema v5
Ported the approved zoom draft into the real pipeline (areas.py, timeline.py, label.py, web/index.html; SCHEMA.md "v5").
- Merges and cuts are the default blob map (`timeline.py --no-smart` keeps the analyzer's, for debugging / tests):
  blobs under 20 lines fold into their module's biggest blob (else the one they share most calls / data with),
  never routes, entries, error handlers, hooks or cut parents; blobs of 500+ lines / 50+ functions give up
  callee pieces owning 10%+ of their lines (dominators, split_entry's depth rule); a piece's module is its root
  function's file's (a method root `app.Svc.run` is in `app`). Edges are recounted over the new blobs as analyze()
  counts them: tests/test_areas.py checks them against a count written from scratch, step by step over c-mini built
  and edited with the gates lowered (folds and cuts at most steps; the scratch count first equals analyze()'s own
  edges on the unreshaped blobs); a bumped, duplicated or dropped edge fails it.
- Areas: modules (header with its .c) of one top directory joined by call sites per line, <= 9 blobs per join,
  down to min(7, ceil(blobs / 9)) + one per further top dir; past 12 areas the cap gives way (never more than 12
  code areas); plus `tests`, `files`. Joins from a heap of pair scores, same joins as the plain scan. Stability rule: a module keeps
  its area id; the grouping is recomputed only when the module set or the cross-module call sites change; new
  modules join at once; existing modules move only when the same fresh grouping has held for the 40 steps after the
  one it came out on (or half the session) and would change some area by more than half its lines. Captions / faces: word frequency over the
  members' label titles and their top 3 emojis by lines, recomputed by label.py when it applies labels.
- Page: Areas toggle (button + A, localStorage, default off) adds the area level above the blob map in the draft's
  drawing (organic area blobs sized by members, 3 faces, caption, summed edges, camera fly on click / wheel,
  neighbours dimmed, breadcrumb, Esc / Backspace up, drag pans); an area edited this step pulses and carries its
  blobs' stains and heat. Follow toggle (button + F, default off) flies to the edited area / blob each step.
  Satellites for folded blobs, dashed outline + bite for cuts, in both modes. Blob -> function drill-down unchanged.
- aigource map (1,270 steps, README build; incremental == --full byte for byte): blobs at the last step 180 -> 167
  (code 127 -> 114: 15 pieces from 10 cuts, 28 folds), 10 areas at the end (8 code + tests + files), 18 area ids
  over the session, 96 blob area reassignments in 6 regroups. Per-step p50 (quiet A/B, same machine, load ~2.5
  from other sessions): HEAD 61.9 / 63.4 ms, v5 70.9-74.1 ms (+~10 ms; areas.apply p50 ~8 ms: reshape 4, recount
  3, areas 2). Size: 7.89 -> 8.68 MB compact before labels, 15.1 MB as label.py writes it (13.6 MB at v4); the
  areas table is sent on 249 of 1,270 steps. Labels: 1,130 cache hits, 0 calls; every non-piece key still hits,
  the 192 versions of the 15 pieces are the only uncached keys (no history labelling run, per the live-only rule).
- Flask demo: 3 folds (hub:auth -> auth.login, hub:api -> api.create_note, hub:notes -> notes.edit), no cuts,
  3 code areas + tests + files, 6 reassignments (one regroup at step 13). tests/golden/flask_final.json updated:
  the three hubs are gone into their targets, 113 -> 102 edges (edges into the hubs now point at the targets, n
  summed; data edges between a hub and its target became internal), blobs carry `area` / `merged`, `areas` table.

- Verifier round (same day): a cut piece rooted at a method took a ghost module (`app.Svc`) and could land in
  another area than its parent (4 of the cuts in TechnicalAnalysis's first 30 steps); now the root function's file
  module. The vacuous identity test of `recount` is replaced by the differential one above. `group()` joins from a
  heap (same joins as the plain scan, checked on random inputs; 200 modules 0.72 -> 0.09 s, 400 5.7 -> 0.4 s) and
  the 9-blob cap gives way past 12 areas. aigource and Flask maps unchanged byte for byte (labelled aigource map
  too; 96 reassignments, 6 regroups). TechnicalAnalysis, first 30 steps (seed-git): 14 areas (12 code + tests +
  files; 31 before the cap gave way), 397 code blobs, 20 pieces all in their parent's area, regroup 33 ms (was
  236-271 ms), per-step p50 276 ms vs 228 ms at HEAD (step 0 4.3 s both).
- Follow-up: the 12-area bound also holds while a session grows (new modules join an existing area at the cap; an
  assignment over the cap adopts the fresh grouping at once): 150 modules added one per step stay at 12 areas, 0
  regroups (was 19 areas). A Flask fixture now drives recount's residual branches (abort edge of a folded hub
  redirected to its new home, a cut parent's ref split to the piece, the extra literal kept by the parent); each
  of four mutations of those branches fails it. aigource and Flask maps unchanged.
- User decision: Space stays play / pause; Esc, Backspace and the breadcrumb go up a level (README).

## Backlog, areas
- On TechnicalAnalysis the areas pass costs ~48 ms per step (p50 276 vs 228 ms): reshape, recount and the areas'
  call-site table walk every function each step; reuse them across steps when blob membership and calls did not
  change.
- Pieces stay unlabelled until a manual `label.py --per-step` run reaches them (live labelling asks only steps made
  after `since`; root function name + parent's emoji meanwhile);
  with `--relabel-at` the 15 pieces need 65 label requests over the aigource history instead of 194.
- The cost is ~10 ms per step on aigource; reshape could reuse the previous step's units when no blob changed
  membership or calls (most steps), if that matters.
- An open area's dashed outline is sized by its farthest member, so a lone outlier leaves empty room; the local
  layout could pull outliers in.
- Captions before labels come from heuristic names (function names for C commands); label.py fixes them.

## Label reuse outcome (2026-11-08): `--relabel-at`
A measurement over the aigource label run (scratchpad labelreuse/report.txt) found that one plain rule saves 53% of
the calls (875 of 1,656) with 1 borderline real miss in 40 audited skips and drift on 1 of 180 live blobs (a
rewording), and removes flicker (129 of 454 paid title changes flipped back to an older title). label.py now
applies it (SCHEMA.md, v5 "Label reuse"):
- Per analyzer-blob identity (blob id, cut parent with its pieces, each `merged` entry, each piece under its own id)
  a label is kept until the lines changed since it was made reach `--relabel-at` (default 0.2) of the identity's
  lines then; an added or removed function counts its whole length. A kept label is a cache entry for the new key
  marked `reused_from`, no model call; the map shows it unmarked. `--relabel-at 0` is the old behaviour.
- State: none stored. Each run replays the rule from step 0 against the cache (a key in the cache is always a hit,
  a reused one counts as the reuse it records), so a `--per-step` resume decides exactly as an uninterrupted run
  (tested, with the resume between two edits that only add up to 20% together). The whole-timeline mode replays
  the same rule assuming its asks succeed, then writes the kept labels whose source got labelled; without a
  server (port 1) it still keeps labels from the cache.
- Pieces get a label of their own on their first version (the parent's label describes the whole blob, and a
  piece's functions already count in the parent's identity, so inheriting would put the parent's title twice on
  the map); later piece versions follow the rule under the piece id. `merged` entries are still never asked, but
  now keep their label through small edits.
- Checks: aigource full cache, port 1, on a copy: 1,130 hits, 0 kept, 0 calls, map and cache byte-identical.
  `--count` from an empty cache: 1,322 -> 533 label requests (59.7% saved; blobs 1,076 -> 416, pieces 194 -> 65,
  files 52 both). In the measurement's own universe (merged entries askable, no piece identities) 1,221 -> 555
  (54.5%) vs the measurement's 576 (52.8%): 49 revisits of a key that kept a label are cache hits here and fresh
  decisions in the measurement; through the anchors they leave they account for the 21 fewer requests (the
  measurement's replay with that one change gives 555 exactly).
- Open: the 20% threshold is fitted to one repo and one labeller; a full relabel can tell kept labels apart by
  `reused_from` but there is no flag to drop them yet. The measurement's audit covers aigource only.

## Live mode, phases 0-1 outcome (2026-11-08)
Plan: .omc/plans/aiblob-live.md (strict exactness: the live map must equal a batch build). Scratch evidence:
scratchpad live/ (measure.py, order.py, sim.py, llm.py and their logs).
- Measured on the aigource map (frozen copy of both project dirs, 160 transcripts, 1,270 steps): `replay.steps()`
  ~20 s and ~170 MB peak (difflib line counts ~55% of it, JSON parsing ~17%); analysis 205 s (loaded machine: 246 s), 630 MB
  peak; `timeline.expand()` 0.4-0.8 s; `analyze(last snapshot, {}, {})` 1.9-2.9 s (143 files).
- Timestamps vs write order: user / assistant records stay in timestamp order within 1.2 s of each other, but their
  writes can lag other records: 7 events (18 tool_use / tool_result records) were written 3-38 s after a record
  with a later timestamp. Copies of earlier sessions (resumes) are written up to 52 min "back in time"; stage 1's
  dedup lets them through. Subagents start 0.12-0.66 s after the parent's Agent call (all 131 here background).
  33 of 9,304 Edit/Write/MultiEdit/Bash calls never got a tool_result (all Bash); no result ever came after a later
  assistant message of the same transcript (10,637 results), which is how the reader ends such calls.
- llama-server b11960 (Qwen3.5-4B, `-np 1 --cache-ram 0`): `--sleep-idle-seconds 60` frees VRAM (3,098 -> 100 MiB,
  RSS 1.05 GB -> 0.52 GB), `/health` answers 200 asleep without waking it, the next request wakes it in 3.2 s.
  `-c 4096`: 2,966 MiB, 4.1 s to /health; `-c 8192` (serve-llm.sh): 3,098 MiB, 5.0 s. `--fit on` does nothing next
  to serve-llm.sh's `-ngl 99` (it only sets unset options); `-ngl auto -c 0` let it pick the model's 262,144
  context: 9.6 GB RSS, 25 s start, slower answers. Use `-c 4096 -np 1 --cache-ram 0 --sleep-idle-seconds 60`.
- Phase 1: replay.steps() is now OpReader (incremental _ops, a call's ops held until its tool_result) -> Engine
  (copies, credit, apply stages with the strict commit rules in its doc) -> Merger (the old apply loop). Batch is
  feed-all + finish(), unchanged: aigource map raw and labelled byte-identical to web/timeline-aigource.json, Flask
  web/timeline.json identical, and the real corpus fed record by record in write order (exact watermark) gives the
  same 1,270 steps with `late` 0. tests/test_chunking.py: every test_replay scenario plus mixed / racing / git
  fixtures in all-at-once, one-record, timestamp-ordered, shuffled and byte-chunked (Lines) feeds.
- Strict stalls (same simulation, hold after max(step time, its call's end)): 1,206 of 1,270 steps 0 s; 59 > 1 s,
  25 > 60 s, 8 > 300 s, max 45 min, nearly all a Bash report waiting for another transcript's running Bash call
  that started before it returned (the 45 min: a subagent's `flock`-waiting build held session 8e130533's reports).
- Open for phase 2: the watermark lag (the 3-38 s late writes would break exactness at now-2.5 s); transcript
  numbers n of files that appear later (ties and copies sort by n); `many` and subagent tags (meta.json) fixed at
  add(); seed_git's untouched() needs the last step; git commits are read once.

## Live mode, phase 2 outcome (2026-11-08): live.py writer
Design in live.py's docstring (store, committed vs provisional, liveness, numbering, memory). In short:
- Committed: replay.Engine drained at w = now - 60 s, analysed (timeline.Analyzer, the old _steps as a class) and
  compacted, appended to live/NAME/steps.ndjson; exactly timeline.py --no-timings' lines, never changed. A broken
  promise (Engine.late), a replaced or cut transcript, a new transcript sorting before a followed one, or a second
  transcript where `many` was off rebuilds the store from scratch.
- Provisional tail (decision 4b): Engine.fork().finish() = the batch build if the transcripts ended now. Fork cost:
  pending ops copied, applied ops / boxes / reader tables shared (the fork's Merger records what it spends instead of
  writing into shared boxes); Merger.fork copies snapshot, history lists, seen, turns; Analyzer.fork shares the parse
  cache and memo tables (analyze() never writes into them) and copies the memo dict and the Zoner's cut memo;
  Compactor is copied shallowly. Redone only when Engine.version moved; the fork's Merger replays the previous run's
  applies while ops match (Merger.memo) and tail analyses are reused while the steps before match; a committed step
  the tail foresaw is adopted with its analysis.
- Liveness: open calls of a transcript close after --idle (600 s) without growth when its session is not running
  (~/.claude/sessions/<pid>.json); an Edit waiting on a permission prompt holds every later step (the tail shows
  them). Late meta.json: tags read when the transcript's first step is made. Numbering: (input, first timestamp,
  place among ties, subagent name) = transcripts() order; a resumed file still writing its copies is followed once it
  has a record of its own. Git commits polled every 10 s; --seed-git's untouched files frozen at the cold build
  (rest.json; verify uses them).
- Memory: Engine.evict (applied ops no report still to come can see), OpReader.evict (finished calls' ops,
  originalFile, structuredPatch), Engine.first keeps (key as read, box) per applied op, HIST_KEEP = 16 texts per file
  (aigource retries fit 1-2 back), parse cache trimmed to the last snapshot, gc unfreeze + collect every 10 min.
- Numbers (aigource, frozen corpus written record by record in write order under a simulated clock, live_sim.py in
  the scratchpad): 1,270 steps committed, 0 rebuilds, late 0, export == batch build of the same files byte for byte;
  10,567 tail recomputes: fork p50 0.5 ms / p99 1.4 ms, whole tail p50 1.1 ms, p90 7 ms, p99 394 ms, max 1.4 s;
  tail length p50 0, p90 2, p99 8, max 18; writer RSS <= 321 MB (batch build: 630 MB); checkpoint 37.8 MB written in
  0.3 s; warm restart (load + analyze of the last snapshot) 2.3 s, 131 MB; two warm restarts mid-run (at 355 and
  1,060 steps: 7.6 / 29.5 MB checkpoints, 0.7 / 2.0 s to start) changed nothing. Live fuzz (the review's random
  sessions, 400 seeds, batch checked at every tick, 2 restarts each): 0 failures. Capped batch rebuild (HIST_KEEP) of the
  aigource map byte-identical, replay 10.8 s (was ~20 s: fewer retries on old texts).
- Tests: tests/test_live.py (Flask, js-mini, c-mini, mixed, racing, git seed + reconcile with a commit made while
  live, late meta, many flip, fixture D numbering, idle close, non-ISO w, timeline.py --checkpoint, resumed copy of a
  moved report, a failing Write the tail showed applied, tracemalloc over 2,000 steps); every tick checks committed
  never changes and committed + tail == batch; restarts at random ticks. tests/test_chunking.py: fixtures killing the
  review's surviving mutants (fresh rank, root wait, Read at a commit's second); late == 0 for all-at-once.
- Open: a resumed copy that sorts before its original (its own first record older than the original's next, not
  seen in real resumes) costs a rebuild; the 60 s lag is a choice (phase 0's worst late write: 38 s).
- Review round (phase 2 verifier, REQUEST_CHANGES, 8 defects), all fixed with a test each: a checkpoint is written
  only after a tick that ended (no `finally` checkpoint past unwritten steps); every git poll checks that each commit
  taken is still in the history (amend inside the lag, reset, rebase -> rebuild); a result for a call that was open
  when its transcript was closed is late (rebuild; also covers eviction changing the tail); transcripts are counted
  as transcripts() counts them (timestamp-less and empty ones turn `many` on, root from its first); live.pid holds
  `pid start-time` (stop / run / timeline.py --checkpoint check it); a transcript can be idle-closed again; the
  checkpoint's JSON header (version, code, flags, steps) is checked before unpickling, any load error means a cold
  build; steps.ndjson is read only up to its last newline, and a half-written last line is cut on restart. Also
  found: the Merger memo's signature read the spent state before a lazily replayed op's state was taken (only lost
  hits); and, once waiting copies counted for `many`, a cold build could number a resumed file mid-copy first and
  keep that order after the copy got a record of its own (late records, rebuild loops). Now a cold build waits for
  a newer file that holds only another's records, tie groups with a member that has nothing of its own are checked
  again whenever one grows (a flip rebuilds), and new members of a tie group go after the numbered ones or rebuild. Mutants: 51 (the review's 42 plus 9 for this round) against test_live + test_chunking; the survivors are
  equivalent (shared memo / cut memo of the analyzer are caches whose content does not change a result; `by` in
  the op signature is implied by the replayed prefix).
- Review round 2 (2 blockers): a writer is unclean from its creation until start() (warm or cold) has ended, so a
  ^C in the first or a --fresh cold build leaves no checkpoint (end to end: SIGINT at 151 of 399 steps, the rerun
  builds cold and verifies); a new transcript waits as a copy in progress only when it holds nothing but records of
  a file older than it on disk (a followed file no longer counts by itself: in a resume chain the middle file was
  left waiting). Tests' Feed now gives each written file its simulated time as mtime (all files written within one
  clock tick had tied mtimes, which hid the chain case), follow() asserts every waiting file copies an older one
  and that none waits at the end, and the resume scenarios (chain, two resumes) run with a cold build at every
  point of their write order. An Analyzer
  fork is also tested directly: run 25 steps ahead (through a regroup and a cut), the parent's Zoner, cut memo, memo
  and output equal an analyzer never forked (kills sharing any of them; only `by` in the op signature stays an
  equivalent mutant).
- Review round 3 (1 blocker): the "waits as a copy" rule depended on mtimes (touch, an untimed cost-state record
  appended at exit, cp, clock skew could leave an original waiting for good). live.py now uses no mtime at all, as
  transcripts() uses none: a cold build numbers every transcript in transcripts() order (a resume found mid-copy is
  then numbered first, and its next records cost one rebuild); afterwards a new transcript holding only records of
  a followed one waits only while this writer sees it grow (--settle 60 s), and a rebuild carries over what it saw
  growing; idle time counts from when the writer found a file; subagent tags are reread by content. Tests: the
  review's touch / untimed / skew / cp / cp -p cases, cold, following and following with `many` on; Feed can write
  random mtimes (resume scenarios at every cold-start point run with both); livefuzz uses random mtimes.

## Live mode, phase 3 outcome (2026-11-09): liveserve.py and the page in live mode
Design in liveserve.py's docstring. In short:
- Server (127.0.0.1:8791, `make live-serve`): /live/timeline.json (the store as `live.py export` writes it, labelled),
  /live/events (SSE: "steps" with id = the committed count, so Last-Event-ID beats `from=` on a reconnect; "tail";
  "rebuild" with the epoch; "labels"; "ask", the state of the labelling chip), /live/status, POST /live/focus. Host check (DNS rebinding), 60 s socket
  timeout, `epoch` / `from` validated (400), no traversal. It only reads the store and runs with or without a writer.
- Served committed steps never contradict the store: `_behind_ok` rebuilds (epoch + 1, the page reloads at the same
  step) when the steps served no longer match what the store says comes next; a store that is a prefix of the steps
  served (a writer cutting back to its checkpoint, or mid-rebuild) is waited on.
- Labels are applied server-side with label.py's own code (step_keys, Keeper, apply_labels, plus Expander / Compactor
  to keep the page's format): cache hits and the 20% keep rule, never a model call.
- Page, `?live=1` (`/` redirects there; `?live=0` is the static page): incremental expansion, provisional steps faded
  until they commit, LIVE badge, follow on the last step at 4x and a jump when more than 20 behind, a "+N new" pill
  elsewhere, blob positions kept (new blobs placed beside the others) plus a Re-layout button. Static mode is
  pixel-identical to before.
- Numbers: page heap ~79 MB at 1,270 steps on aigource.
- Tests: tests/test_liveserve.py (protocol, resume, rebuilds, host check, timeouts), test_live_e2e.py (writer + server +
  simulated transcripts), test_live_page.py (browser driven by tests/cdp_driver.js).
- Open: see the residual risks below (stalled resume, writer dying mid-rebuild).

## Live mode, phase 4 outcome (2026-11-09): livelabel.py, labelling of new steps
Design in livelabel.py's docstring. A worker thread inside liveserve asks the local model for the blobs of NEW steps
only; history is never asked: a restart or rebuild never asks steps older than `since`; steps made while the server was down are asked.
- Coalescing: per blob, its newest version, asked once quiet 20 s, or its turn ended, or 120 s since first wanted;
  both clocks restart after an answer. The 20% rule applies as in label.py. Order: the step the page shows (POST
  /live/focus) first, then the newest.
- Thermal: pause at >= 85 C (nvidia-smi, read at most every 5 s), resume below 80 C.
- Failures: a failed ask is retried after 2 s and 4 s; after 3 attempts the blob backs off 60, 120, 300, 600 s. A
  llama-server that does not answer is probed on /health after 15, 30, then every 60 s, nothing asked meanwhile. An
  error in one step of the worker shows on the chip ('error') and the next step goes on; a dead worker thread is
  restarted by the server up to 3 times (then 'error' stays: the warning chip on the page).
- Log: demo/label-runs/<name>.live.jsonl, one JSON line per ask and per change of state.
- llama-server: one answering at `--llm-url` is used, never started or stopped. Otherwise a child starts on demand,
  only with fresh nvidia-smi VRAM >= 3,300 MiB, RAM >= 2 GiB available and the worker not paused for heat, with
  `-c 4096 -np 1 --cache-ram 0 --sleep-idle-seconds 60`. Own process group plus PR_SET_PDEATHSIG (SIGKILL when the
  starting thread ends); stop = SIGTERM to the group, SIGKILL after 10 s; stopped 10 min after the last answer (or start). Before
  an ask to a sleeping child (/props `is_sleeping`, checked without waking it) free VRAM is read again and the ask
  waits while it is short.
- Boundary: live/NAME/labelling.json {since, steps}, written once. A step is new iff its ts >= since.
  since = max(wall clock at enable, store and transcript mtimes, newest step ts + 1 ms). A transcript mtime more
  than 60 s ahead of max(clock, the store's own files' mtimes) is ignored and logged (`mtime-ahead` in the run log).
- Labels layer: label keys go out as 16-hex kids (`lk` / `mk` per step); SSE "labels" {kid: label or null} changes
  reach every step showing that key without a reload, and the page recaptions areas with a JS port of
  areas.caption_table. Settled labels equal `label.py <export> --cache C --server http://127.0.0.1:1`: on aigource
  with a random half of its cache, 323 steps differ without the layer, 0 with it. label.save_cache now does flock,
  re-read, merge of only the keys this process changed, atomic rename (a concurrent label.py run loses nothing).
- Verification: 432 tests pass; 78 of 78 mutants killed; tests use a fake llama-server (tests/fakellama.py) and a
  fake nvidia-smi (tests/fakesmi.py), so no GPU is needed.
- Real-GPU smoke (2026-11-09, RTX 2060, Qwen3.5-4B Q4 via the b11960 runtime, child started by liveserve): child ready
  6.0 s after start; 4 asks of 1.13-1.34 s each at 70 C gave real titles ("View shared note", "Error page display",
  "API token display", "Login page"). VRAM: 2,972 MiB used by the child (its own per-process use, nvidia-smi compute-apps; phase 0's 3,098 MiB is the total-used delta), 102 MiB after the 60 s sleep. SIGTERM to
  liveserve: child gone and VRAM fully back. SIGKILL to liveserve: child gone within 2 s. Steps with old timestamps
  are (correctly) never asked, so a smoke test must stamp its new steps with the current time.
- Residual risks:
  - a stalled resume (more than --settle between copied records) costs repeated rebuilds;
  - a writer dying in the middle of a rebuild leaves the server serving the old steps until the writer restarts;
  - a label key first seen at step 0 costs ~2.3 s of poll-thread work (fixed: ~0.05 s, see Backlog, live);
  - a theoretical reorder of timeless steps (a step without ts takes the time of the step before it);
  - an Edit waiting on a permission prompt holds every later commit (the provisional tail still shows them);
  - before the writer has written head.json (during the `--seed-git` scan) the server computes `since` from the wall
    clock and the store's mtimes only; this matters only with a clock that runs behind, and no test covers it.

## Backlog, live
- Label the 15 cut pieces (192 versions on aigource) by hand with `label.py --per-step`, only when asked: history is
  never relabelled automatically.
- Per-step cost reuse: done (2026-11-09). A cache change replayed the labeller's plan from the last 32-step snapshot
  before the first step showing a changed key, expanding and keying every step again. The labeller now keeps each
  committed step's units as the Keeper reads them (`rows`: id, key, functions as (qual, hash, lines, delta lines),
  the blob's id; a unit whose key is the step before's is that step's tuple, a function is kept once), so recache()
  replays the Keeper alone, from the same snapshot; snapshots keep their expander and get the replayed plan in place.
  Only an ask replayed at a new step expands that step again (its blob and context), from the snapshot before it.
  label.Keeper.step takes the units (`us`), and its functions are plan_funcs() tuples, as for a step it reads itself.
  Aigource, 1,270 steps, a random half of its cache, same machine, before -> after:
  - recache of a key first shown at step 0: 1.21-1.29 s -> 0.05 s; at step 636: 0.90-0.96 s -> 0.04 s;
  - in the server, a label written to the cache file -> its "labels" event (poll 0.05 s): 1.24-1.38 s -> 0.08-0.13 s;
  - server RSS at 1,270 steps: 129 -> 132 MB when ready, 138 -> 136 MB after two step-0 changes (the old replay forked
    expanders for its snapshots again); what the labeller holds (tracemalloc): 28.7 -> 31.5 MB;
  - a push (step labelled as it comes) costs the same, within noise (1,270 pushes interleaved, 3 runs each);
  - settled labels still equal label.py's with the same cache: 323 steps differ without the layer, 0 with it, and 0
    after a recache of a key first shown late, at step 0, at step 636, and a removal.
  label.py --per-step needed no change. Profiled against the tests' fake model on aigource with
  demo/labels.aigource.json (the 15 cut pieces: 154 steps to label, 64 asks): run 4.2 s, resume 2.1 s on a calm
  machine; under load 7.3 / 3.3 s before, 7.0 / 3.3 s after. The run's time outside the model is save_cache (149 saves
  of the 785 KB cache, 55-66% of the profile) and one pass of expand + file heads + keys + units (~1.5 s), the resume
  only that pass; none of it is per ask, so with the real model (~1.2 s an ask) the run spends its time asking.
  Tests: test_replay_on_kept_units_is_a_full_replay (against the replay that expands every step: random cache changes
  at random points, new steps from a random step on, on flask, the kept fixture, func_patch and 160 aigource steps,
  SNAP 2 / 5 / 32), test_replay_expands_and_keys_no_step_already_seen (counts Expander.push and step_keys calls).
  436 tests pass; 85 of 85 mutants killed (7 new on the rows; the phase-4 mutant on cutting snapshots back is now one
  on the replayed snapshots keeping their old plan).

## Diffs inside blobs, data side outcome (2026-11-09)
Plan: .omc/plans/aiblob-diffs.md (layout B, sink keyed by the existing hashes). The page part is drafted separately.
- diffs.py: BodySink (Analyzer.sink; a fork shares it, pickling drops it) stores every function body, every changed
  file / text blob text and each seeded base once, by sha1[:10], and leaves a file line per step `{f, seed, sq}`.
  `sq` is new against the plan: a seeded file's function bodies in its base, by qual, from the analyzer's own parse
  of the base (the review round: the first version sliced them by a line diff, wrong for 12 of 81 on aigource), so
  a seeded function diffs against its own base body; the line-diff slice stays as a fallback for a base that does
  not parse (`sqa`: approximate quals). replay.Step.before: {rel: base} only when the base
  differs from the snapshot (aigource: 3 of the 7 seeded steps; the other 4 are Read-merges onto the snapshot).
- Static `timeline.py --diffs DIR` / `live.py export --diffs DIR`: index.json.gz + s/<step>.json.gz, gzip mtime 0,
  chunks sorted by hash, so both write the same bytes for the same steps. Live: diffs/bodies.ndjson (append-only, its
  hashes are the seen set at start, a half line cut), diffs/steps.ndjson (truncated / cut back after steps.ndjson,
  checked by the checkpoint's `dlast`), diffs/tail.json (last two tails, keyed by a digest of their lines).
  liveserve: `fv` on served steps (a step waits for its file line; a tail whose lines are not in tail.json is read
  again), `/live/bodies?h=` (<= 256, 400 on bad input), `"diffs"` in "live". Contract and reference reader:
  SCHEMA.md "Diffs", diffs.Reader.range_texts().
- aigource (frozen copy of both project dirs, 1,270 steps, README build line): timeline.json byte-identical with and
  without --diffs (and sessions-out); 3,878 texts (3,495 bodies, 307 file / seed texts, 76 parsed base bodies),
  10.85 MB raw (bodies 5.31, file texts 5.49); every `sq` equals the analysis of its step's snapshot with the base
  (76 of 76, 66 of them `same`); layout B 925 chunks, 3.71 MB gz (max 86 KB, median 1.9 KB), index 34 KB gz;
  live pack 11.3 MB raw, diffs/steps.ndjson 22 KB. Plan: 3,866 texts, 3.67 MB gz, index ~31 KB. Max RSS static
  647.4 -> 648.7 MB, live cold build (`timeline.py --checkpoint`) 890.2 -> 890.7 MB; wall time within noise
  (static 120.8 -> 118.2 s, live 132.1 -> 118.5 s, a loaded machine); timed in-process: BodySink.step 0.36 s and the
  chunk writes (gzip) 0.81 s of a 101 s build. The live export's
  diffs equal the static build's except the file lines of steps 66-67, where the live timeline itself differs from
  batch (--seed-git's untouched files frozen at the cold build, as designed; base and new code alike).
- Tests: tests/test_diffs.py (byte identity Flask / js-mini / c-mini, store completeness, range_texts against replay
  snapshots for random (a, b) on fixtures and random sessions, seeded base, moves / renames / deletes, file-blob
  rules); test_live.py live_diffs_* (complete at every tick with restarts, Flask and a random session, seeded + git
  steps, rebuild, cut-back, a mismatching file-lines file, a crash mid-commit, tail lines; live export == static and
  the reader live == static); test_liveserve.py (fv served, every hash served, unknown / bad / capped / Host, a step
  waiting for its file line, a tail waiting for its lines). 6 new mutants (missed flush before a step / before the
  tail, a body not stored, a stale cut-back, a wrong seed base, a step served before its file line) killed.
- Review round (REQUEST CHANGES): sq from the parsed base (CRLF, form feed, U+2028, JS callbacks / arrows now
  `same`), the fallback slice splits at \n only; Pack checks the file every call and each line read against its hash
  (a recreated, cut or corrupt pack: read again once, else null, never an error); a tail is never served without
  its file lines; a seed counts only up to the step that first shows the qual / path. 10 more mutants killed (tail
  digest ignored, a copy taken as a rename, renames not once, Entry.matches without `before`, a seed equal to the
  snapshot, sq by line diff only, splitlines, Pack trusting its index, a tail without its lines, a seed after the
  key was made). Two of the review's were equivalent and are gone or kept as is: the slice's deleted-run clause
  (a run deleted inside a function is always between mapped lines; removed) and scanning only for unknown hashes
  (the hash check gives the same answers; the per-call scan stays).
- Found on the way, not changed: Analyzer.fork() goes through __getstate__ (copy.copy), so a fork starts with an
  empty parse cache and the memo copy, not the shared cache the docstring says (tail forks re-parse what they touch).
  The sink is set again in fork() for that reason.
- Risks: provisional texts never committed stay in the pack (once each); a store from before diffs is rebuilt cold
  by the new code (CODE now includes diffs.py); a server following it rebuilds (epoch + 1) when diffs/steps.ndjson
  appears and serves the steps again as their file lines are written. liveserve reads the whole diffs/steps.ndjson
  when steps come (22 KB on aigource).

## Diffs inside blobs, page outcome (2026-11-10)
Plan: .omc/plans/aiblob-diffs.md "UI decided" (Skins + Twins); the look follows the approved draft (session scratchpad
diff-draft/), ported into web/index.html's drill-down ("diffs inside blobs" section).
- Data flow: at boot the page reads `<timeline>.diffs/index.json.gz` (fetch + DecompressionStream; gzip checked by
  its magic bytes) for the file lines and which chunk holds which hash; live mode takes `fv` from the served steps
  and `"diffs"` from "live". A drill-down asks for its blob's range (T: a = b = now; S: a = the session's first step;
  L: a = the 👁 step + 1), cached by blob|a|b (DV.ranges): the <diffcore> block computes the entries as
  diffs.Reader.range_texts() does (first appearances of new quals in one scan per range, not per qual), fetches only
  hashes not in DV.texts (static: each chunk once, all its texts kept; live: /live/bodies?h= in batches of 256),
  then line-diffs each entry as difflib does (grouped opcodes, 2 lines of context; texts over 2,000 lines on both
  sides in a Web Worker running the same functions). Live: ranges over the old tail are dropped on new steps, all on
  a reload. A map without the sidecar (or a store from before diffs): a note "diffs unavailable", the drill-down as
  before (pages keep their file card).
- Skins (key 1): the draft's hairPlan / skinFeats / notchedPath / skinBody / drawDendrite as they were; blobPath and
  rimOf share wobPhases / wobK (the same arithmetic, so blobPath draws the same pixels); per hunk one faint chomp,
  its notch edge in the scar colour; hairs, every 5 bundled; a changed function sized by kept + removed lines (also
  in an opened cluster's layout). Twins (key 2): ghost + new, side-by-side lens, M morphs, gone drift off, new bud
  from their caller. Pages (file / text blobs): a page drill-down with the marks on the left edge (Twins: before |
  after pages joined by ribbons). Hover a hunk = its lines; click = the hunk bubble; click a body / ghost / page = the
  whole diff (lens, capped at 400 rows), the circle eases smaller and left to make room.
- Against the draft (the app's frame, not the look): no shelf; the look and scope switches sit in the header and the
  summary (+a −r, changed / new / gone / moved out), the 👁 strip (session start, the blob's last 10 changes in the
  session, now) and the legend are a DOM panel beside the circle, so the canvas of an unchanged drill-down stays
  pixel-identical; the drill-down keeps relaxing a few passes a frame, so a scope / look change glides; "you looked"
  defaults to the step at which that blob's drill-down was last closed (else the draft's third-latest change), and
  that step and the eye's always sit on the strip, in order; the range keys are 7 / 8 / 9 (user's choice: keys that
  clash with nothing; the page binds Space, arrows, Home, End, Esc, Backspace, Enter and / in the cluster search,
  a, f, g, s, l, 1, 2, m), T / S / L keep their meaning everywhere; clustered blobs (user's choice "hairs + bites on
  clusters"): in the overview each cluster wears its changed functions' marks on its own rim (functions in code
  order, then hunks; past 60 marks the hunks merge per function, then per run of functions, and a hair stands for
  10, 25, ... lines, said in the tip), hover = function and hunk, click = that cluster opened with the function
  pinned; one scale for the whole overview (5, else 10, 25, ... said in the panel's legend), hunks merged per function
  before it goes up, and every hair holds what it stands for (a part left over is a thinner hair of its own lines,
  never rounded up); each cluster's skin is drawn once to an offscreen canvas (at rest, no sway) and blitted; Twins
  there = a dashed ghost of the cluster's size before (cheap), never past the blob's rim; an opened cluster wears the looks; a
  page's drill-down keeps the file card's facts in the panel; a changed binary says so; a satellite badge only on
  changed functions; moved-out functions are counted, not drawn; moved / renamed / seeded notes in the tips and the
  lens; ranges kept for the last 64 (an error asked again after 4 s); the panel on a plate, the circle clear of it
  in narrow windows, and smaller (30%) when a lens opens; both themes. Note: in windows narrower than about 1,150 px
  a map with diffs shifts every drill-down right (and smaller) to clear the panel, unchanged blobs too: there the
  picture is not the one a map without diffs draws (at 1,400 px it is, pixel for pixel).
- Tests: tests/test_diffs_page.py. Parity: the page's <diffcore> in node vs Reader.range_texts() + difflib hunks on
  1,249 ranges (c-mini, Flask, the fixture; every state). Chrome on a three-step fixture (an entry blob, an untouched
  orphan, a text file): Skins counts (bites = hunks with removals, hairs = n // 5 + n % 5 per hunk) in four scopes;
  hair roots and notch mouths within 1 px of blobPath's outline at several t; notch pixels the scar's colour; hover
  / click / lens / closing; Twins side by side, morph; T / S / L keys and ←/→ on the strip; the page's edge marks and
  its Twins; an opened cluster (engine.c) and the overview arcs; live mode (texts from /live/bodies, a provisional
  step); the headless page's own fetch / cache path equal to the reference on every range of the fixture, each chunk
  read once; HEAD's page vs this one with a frozen clock, pixel-identical for unchanged drill-downs and for a map
  without the sidecar. Mutants killed (6/6): the old side from step a, hairs not bundled, the notch stroked in the
  border colour, the scope ignored, the cache key without a, marks ignoring the wobble's time. Review round
  (REQUEST CHANGES): the eye on a step that is no change, the page's facts, binary changes, the page's legend, the
  range cap, the panel; tests added for the eye, the range keys, a tail redone under a range (V5), the border off
  the notch mouths (V6), hunks for every run of unchanged lines between edits (V2 without the worker test). Round 2:
  honest hairs in the cluster overview (aigource "blocks of" at step 1253 drew 350 lines for 167, now 167; "stale
  events" 155 for 78, now 78; tested: each cluster's hairs hold its added lines exactly at scale 10), the overview's
  frame on Context data builder 32 ms -> 7.1 ms median (plain 6.0; skinBody 138 ms -> 0 a frame, cached); the
  ~0.5 s stalls every ~20 frames in headless are there with diffs off too (not the diff's).
- Screenshots (aigource, dark, both looks, next to the draft's): session scratchpad shots/aigource/; same totals as the
  draft at all five samples (draw_legend +5 −5; model_update +16 −39, 2 changed 1 new 2 gone; README +6 −1 since the
  session start; context_dump +179 −336; nvim +201 −123, 7 changed 5 new 1 gone).
- Risks: headless Chrome draws the aigource map at about a frame a second, so a click's texts take seconds there (the
  promises wait on frames), not in a real browser; difflib's worst case is quadratic in repeated lines (650 ms for a
  3,000-line file whose lines repeat every 97: hence the worker); a range over a provisional step is made again
  when the tail changes.

## Clusters inside blobs outcome (2026-11-09, rule changed 2026-11-10): schema v6
Plan: .omc/plans/aiblob-clusters.md; the user picked draft variant A "Clusters" as it was, with the big emojis.
- clusters.py, run by areas.Zoner after the areas (off with --no-smart): a code blob (not tests) gets `clusters`
  [{id, cap, emoji, funcs}]. First rule: blobs at the cut size (500+ lines or 50+ functions). The user's rule since
  2026-11-10, "any size, 3+ groups": 12+ functions (clusters.MIN_FUNCS: under that the plain drill-down names every
  function readably) and 3+ fresh groups besides the loose ends (MIN_GROUPS); fewer and the plain drill-down stays.
  The cap's floor went from 6 to 3 (it only bound under 30 functions: 15 functions were forced into up to 6). The
  verdict holds like the groupings: HOLD steps before a blob switches view, counted over the blob's age (every code
  blob's steps are counted, clustered or not), so it applies at once only on the step a blob first appears and a blob
  crossing the line back and forth does not flicker, nor shows clusters for a step after a long plain life (review
  follow-up; the first version counted from the step the blob first reached the floor). A held view is never a
  single cluster: under 2 besides the loose ends the blob draws plain that step. Measured with HEAD's and the new
  clusters.apply over the same expanded steps (clustering runs last, so this equals a rebuild; a scratch rebuild
  agreed exactly before the age hold): aigource 8 -> 31 blobs clustered at the last step, 5,143 -> 20,627
  blob-steps, 28 -> 69 distinct blobs, 12 -> 14 view switches of a blob present on both steps; file ~8.93 -> ~9.08
  MB; Scene rendering engine (hub:src.render, 39 functions, 308 lines) 7 clusters. aiblob map
  (web/timeline-aiblob.json): 5 -> 18 at the last step, 3,881 -> 9,963 blob-steps, 11 -> 34 distinct, 9 -> 10
  switches; orphan:analyze>analyze.analyze (11 functions) is plain now instead of one cluster. The dials keep the
  cut size as their limits. Tests: the 12 / 11 function boundary, loose ends no group, a blob turned plain comes
  back with new ids, a long-lived blob waits the hold, live == batch at the default floors (the verifier's 14
  mutants: 12 killed, 'due counts tests' was equivalent and its clause is gone, 'shrinking blob loses state' no
  longer applies; its equivalent, an untracked blob under the floor, is killed). The fresh grouping is the draft's Louvain and
  post-steps ported bit for bit (rng(), visit order, tie order): on the draft's three viz_run snapshots (86 / 146 / 324
  functions) the same 8 / 11 / 14 clusters, captions and emoji. Stability is areas.py's rule (HOLD, REGROUP, the cap;
  the loose ends always id 0); the state is replaced each step, never written to, so a forked Zoner shares nothing.
  label.py may later put `label: {title, emoji}` on a cluster; the page already shows it over the guess.
- Encoding: a re-sent blob's `clusters` is an entry patch on the base's table (`clusters` changed entries, `[]` when
  none; `clusters_remove`), in _func_patch / _apply_func_patch, so timeline.expand(), liveserve's Expander and the
  page's patchFuncs all read it; a table a patch cannot carry sends the step in full.
- Page: the drill-down of a blob with `clusters` is variant A (clDraw and friends in web/index.html), with the
  limits as constants (CL_LINES 500, CL_FUNCS 50, CL_TANGLE 25%, CL_BRANCHY 5 of CL_BRANCHES 15+, CL_COUPLING 8);
  others draw exactly as before (the plain drill-down of every blob of the sample, Flask and aigource timelines at
  a few steps, HEAD page vs this one with a frozen clock: 177 of 177 pixel-identical). The step highlight reaches in:
  a changed function pulses and so does its cluster. Against the draft: colours by cluster id (they hold across
  steps), layouts warm-start from the places remembered per cluster and function, captions also split camelCase,
  light-theme colours, a blob pill opens that blob, a hint line in the panel (the app has no keys footer), the
  search box under the header; variants B and C, the notes and the snapshot buttons are not ported.
- aigource (README build, --diffs): 1,270 steps, 28 blobs clustered on 5,143 blob-steps; 8.67 -> 8.93 MB (+2.9%;
  whole tables 10.22 MB); build 2:57, max RSS 651 MB; expand(compact()) gives the steps back; the --diffs store
  complete (3,878 hashes) and the timeline the same size as without it (byte identity is tested on c-mini and
  Flask); every label still a cache hit (1,322). viz_run at steps 137 / 475 / 962: 8 / 11 / 14 clusters, no label
  overlap open or closed.
- Tests: tests/test_clusters.py (rng bit for bit with the page's, planted communities, the gates, captions, the same
  output under 4 hash seeds, ids held while functions come and go, adoption only after HOLD, the cap, fork isolation,
  entry patches and round trips through v3-v6 and the page expander, a table a patch cannot carry, c-mini with
  lowered gates: incremental == full, live == batch with restarts, --diffs byte identity and a complete store);
  tests/test_clusters_page.py (drill in, pulses, no label overlaps, open, pill hop, / search, Esc / Backspace, no
  console errors). Flask has no blob at the cut size: its golden summary is unchanged, its timeline too but for
  `schema`.
- Risks: membership lags the fresh grouping between regroups (at viz_run step 137: handle input 21 functions where
  a fresh grouping has 20); past ~500 functions an opened cluster holds 40-90 functions and names start hiding (the
  draft's own note: a second level would be needed); the caption guess is English-keyword based until label.py
  names clusters.
