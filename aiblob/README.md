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

The aigource map (`web/timeline-aigource.json`, 15 MB, gitignored) is built locally from the real sessions,
then labelled from the committed cache `demo/labels.aigource.json` (no GPU needed); open `?src=timeline-aigource.json`:

```
P=~/.claude/projects/-home-rontero-Documents-Projects-AIProjects-HarnessTools
.venv/bin/python timeline.py $P $P-aigource --root ../aigource --seed-git --git-reconcile --no-timings \
    -o web/timeline-aigource.json --sessions-out demo/aigource-sessions.json
.venv/bin/python label.py web/timeline-aigource.json --cache demo/labels.aigource.json --server http://127.0.0.1:1
# new blob versions: label step by step (resumable, timed, GPU-paced; log in demo/label-runs/), then the line above
./serve-llm.sh --runtime b11960 ~/.cache/models/Qwen3.5-4B-Q4_K_M.gguf 8080 -np 1 --cache-ram 0 &
.venv/bin/python label.py web/timeline-aigource.json --per-step --cache demo/labels.aigource.json --cool 80 --report
.venv/bin/python label.py web/timeline-aigource.json --count   # label requests from an empty cache, no GPU
```

A blob keeps its label until the lines changed since that label reach 20% of its lines then (`--relabel-at 0.2`,
the default; an added or removed function counts whole): the new version gets a copy of the label marked
`reused_from` in the cache, without a model call. `--relabel-at 0` asks for every new version, as before
(SCHEMA.md, v5 "Label reuse"). A failed ask leaves its blob with no label to keep, so its next versions are asked
(a `--retry-failed` run can end with a label or so more than a clean run); in the whole-timeline mode, versions
that would keep a label whose ask failed stay unlabelled until a later run labels it.

Live map (live.py): it follows the transcripts as they are written and keeps a store in
live/NAME/: committed steps (steps.ndjson, exactly as a batch build writes them, never changed) and a provisional tail
(provisional.json: what a batch build would add if the transcripts ended now), with a checkpoint for warm restarts:

```
.venv/bin/python live.py run aigource $P $P-aigource --root ../aigource --seed-git --git-reconcile &   # or make live
.venv/bin/python live.py export aigource -o /tmp/t.json   # committed + tail == timeline.py --no-timings
.venv/bin/python live.py verify aigource                  # a batch build compared with the store
.venv/bin/python live.py stop aigource                    # SIGTERM by the pid in live/aigource/live.pid
.venv/bin/python timeline.py ... --no-timings --checkpoint live/aigource   # a batch build live then continues
```

Live server (liveserve.py). With a writer running (`make live NAME=…`), `make live-serve NAME=… [LABELS=…]` serves the
store at http://127.0.0.1:8791/ (`LIVE_PORT=`), which opens the page in live mode (`?live=1`; `?live=0` is the static
page). New steps appear as they commit; steps not committed yet are drawn faded and lock in when committed. On the last
step the page follows new steps (4×, a jump when more than 20 behind); elsewhere a "+N new" pill jumps to the newest.
Blobs never move as steps come (new ones are placed beside the others); Re-layout lays the map out again. A rebuild of
the store reloads the page at the same step. The server runs without a writer too, binds 127.0.0.1 only, refuses
other Host names, and is stopped with `make live-serve-stop NAME=…` (pid in live/NAME/serve.pid).

Labels in live mode (livelabel.py, the server's labelling worker). Only blobs that change in steps made after
labelling was first enabled for the store are asked: the time it began is kept in live/NAME/labelling.json (never
earlier than what the store and its transcripts already hold, so a clock that is behind, a store still in its cold
build or catching up on its transcripts, a rebuild or a restart never asks old steps; edits made while the server was
down count as new; a step without a time is new only after a new step). The whole history stays a manual
`label.py --per-step` run. The 20% rule holds (a blob keeps its label until 20% of it changed), and asks
are coalesced: per blob, its newest version once it is quiet (20 s without change, its turn over, or 120 s at most),
so a burst of edits is one call, and a label that comes back after the blob moved on less than 20% covers the new
version too. The step the page shows goes first (the page posts it), then the newest. Each label is merged into the
cache at once (lock, read again, merge, rename: a label.py run at the same time loses nothing), and reaches every step
showing that blob on the open page, earlier ones included, without a reload; the page then shows exactly what
`label.py T --cache LABELS --server http://127.0.0.1:1` writes with that cache. A chip next to LIVE says how it goes
(🏷 and the blobs waiting; a pulsing green dot while asking; 🌡 and the temperature while the GPU is at 85 °C or more,
paused until below 80 °C; ⏻ while the model cannot be reached or there is no GPU memory for it; ⚠ when the worker
failed, the server restarting it up to 3 times), and titles whose label is on its way are in italics.
The map never waits for labels: with the model away, blobs keep their structural names and the server probes it
again after 15, 30, then every 60 s; a failed ask is tried 3 times, then backed off. Runs are logged in
demo/label-runs/NAME.live.jsonl.

The model: a llama-server already answering at `--llm-url` (default http://127.0.0.1:8080, e.g. `./serve-llm.sh`) is
used as it is and never started or stopped. Otherwise the server starts its own when there is something to ask and
nvidia-smi shows 3,300 MiB of VRAM free (and 2 GiB of RAM available), on a free port, as its child (its own process
group, killed with the server even on SIGKILL), with `-c 4096 -np 1 --cache-ram 0 --sleep-idle-seconds 60` (asleep
after 60 s idle: ~100 MiB of VRAM, woken by the next ask in ~3 s, if the VRAM is free then), and stops it 10 min
after it was last asked (`--idle-stop`), also while asks wait on a hot GPU; its whole process group goes with it. `--no-ask` serves cache hits and kept labels only. Options: liveserve.py's docstring.
API (/live/timeline.json, /live/events, /live/status): liveserve.py's docstring.

Keys on the page: space play/pause, ←/→ step, click a blob to drill down,
click a function for its snippet, Esc back, L legend. A toggles the areas level (also the Areas button,
remembered per browser, default off): blobs grouped into areas above the blob map; click or wheel into an area,
drag to pan, Esc / Backspace / the breadcrumb go back up (Space stays play / pause, also with areas on). The top
view never shows more than 12 areas: past that the 9-blob cap on joins gives way, and a session that grows
module by module puts new modules into existing areas once 12 are in use. F toggles Follow (default off): during replay each step
flies the camera to the area it edits (areas on) or the blob it edits (areas off). `?areas=1`, `?follow=1` and
`?area=<id>` set them from the URL.
Analyzer on any Python tree: `.venv/bin/python analyze.py <dir>`.

web/timeline.json is schema v5 (SCHEMA.md): step 0 holds the whole map, later steps only what changed, and
the page rebuilds every step from those deltas. The blob map folds blobs under 20 lines into a neighbour
(satellites on the page) and cuts blobs of 500+ lines at dominant callees (dashed pieces, a bite out of the
parent), and puts every blob in an area (areas.py); `timeline.py --no-smart` keeps the analyzer's blobs, for
debugging and tests. Analysis is incremental per step (only what an edit can affect
is redone), so big repos stay usable: on TechnicalAnalysis (580 files, 71 steps) a step costs 0.22-0.37 s (p50,
machine load dependent; --full: ~1.07 s) and the timeline is 6 MB (was 357 MB). `timeline.py --full` recomputes everything per step and `--v2` writes every step in full;
both are for debugging and checking (`make test` asserts incremental == full, and expand(v3) == v2).
Timing a session: `make perf SESSION=x.jsonl ROOT=/repo PERF_ARGS="--seed-from-disk --page"` (PERF_ARGS also
takes `--full` / `--v2`); CSVs and summaries land in demo/perf/.
