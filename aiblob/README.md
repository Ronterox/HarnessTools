# aiblob

See what an AI did to the *application*, not the code: a live map of blobs
(entry point + reachable code), lines (main calls, error-path calls, shared
data) and hot spots sized to each edit, replayed from a Claude Code transcript.
Design and decisions: PLAN.md. Data contract: SCHEMA.md.

```
./record.sh                      # claude -p (opus 5.5) builds demo/app from demo/SPEC.md
make timeline                    # transcript -> web/timeline.json (replay.py, analyze.py, timeline.py), then the
                                 # cached labels from demo/labels.json (label.py, no llama-server asked)
./serve-llm.sh &                 # optional: local Qwen3.5-4B on llama-server
.venv/bin/python label.py web/timeline.json   # optional: label new blobs with it, cached in demo/labels.json
cd web && python3 -m http.server 8790         # open http://localhost:8790/?step=18
```

The aigource map (`web/timeline-aigource.json`, 44 MB, gitignored) is built locally from the real sessions,
then labelled from the committed cache `demo/labels.aigource.json` (no GPU needed); open `?src=timeline-aigource.json`:

```
P=~/.claude/projects/-home-rontero-Documents-Projects-AIProjects-HarnessTools
.venv/bin/python timeline.py $P $P-aigource --root ../aigource --seed-git --git-reconcile --no-timings \
    -o web/timeline-aigource.json --sessions-out demo/aigource-sessions.json
.venv/bin/python label.py web/timeline-aigource.json --cache demo/labels.aigource.json --server http://127.0.0.1:1
# new blob versions: label step by step (resumable, timed, GPU-paced; log in demo/label-runs/), then the line above
./serve-llm.sh --runtime b11960 ~/.cache/models/Qwen3.5-4B-Q4_K_M.gguf 8080 -np 1 --cache-ram 0 &
.venv/bin/python label.py web/timeline-aigource.json --per-step --cache demo/labels.aigource.json --cool 80 --report
```

Keys on the page: space play/pause, ←/→ step, click a blob to drill down,
click a function for its snippet, Esc back, L legend.
Analyzer on any Python tree: `.venv/bin/python analyze.py <dir>`.

web/timeline.json is schema v3 (SCHEMA.md): step 0 holds the whole map, later steps only what changed, and
the page rebuilds every step from those deltas. Analysis is incremental per step (only what an edit can affect
is redone), so big repos stay usable: on TechnicalAnalysis (580 files, 71 steps) a step costs 0.22-0.37 s (p50,
machine load dependent; --full: ~1.07 s) and the timeline is 6 MB (was 357 MB). `timeline.py --full` recomputes everything per step and `--v2` writes every step in full;
both are for debugging and checking (`make test` asserts incremental == full, and expand(v3) == v2).
Timing a session: `make perf SESSION=x.jsonl ROOT=/repo PERF_ARGS="--seed-from-disk --page"` (PERF_ARGS also
takes `--full` / `--v2`); CSVs and summaries land in demo/perf/.
