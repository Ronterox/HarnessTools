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
imports cover the most of the repo (`app.py`/`main.py`/`__main__`). Inside it,
each route handler (`@app.route`, `@bp.route`, `@router.get`…), CLI command,
or top-level call becomes a blob root. A function reachable from one root
belongs to that blob; from several roots it joins a hub blob named after its
module (`db`, `models`). Unreached code is a grey "orphan" blob so dead code
the AI left behind is visible.

### Data, not ints
Data symbols = classes, module globals, SQL table names in string literals,
request/session/db attribute roots (`request.form`, `session["user"]`,
`db.users`). Two blobs share a dotted edge when they touch the same symbol.
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
- Pieces stay unlabelled until a live labelling run reaches them (root function name + parent's emoji meanwhile);
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
