PY      ?= .venv/bin/python
SESSION ?= $(HOME)/.claude/projects/-home-rontero-Documents-Projects-AIProjects-HarnessTools-aiblob-demo-app
OUT     ?= web/timeline.json
ROOT    ?=
PERF_OUT  ?= demo/perf/perf.csv
PERF_ARGS ?=

.PHONY: timeline test perf serve live live-stop live-export live-verify live-serve live-serve-stop
LABELS  ?= demo/labels.json

# analyse, then put back the cached labels (port 1: no llama-server is asked; uncached blobs keep heuristic names)
timeline:
	$(PY) timeline.py $(SESSION) --no-timings -o $(OUT)
	$(PY) label.py $(OUT) --cache $(LABELS) --server http://127.0.0.1:1

test:
	$(PY) -m pytest tests -q

# per-step timings to CSV, e.g. make perf SESSION=x.jsonl ROOT=/repo PERF_ARGS="--seed-from-disk --max-steps 20"
perf:
	$(PY) perf.py $(SESSION) $(if $(ROOT),--root $(ROOT)) -o $(PERF_OUT) $(PERF_ARGS)

serve:
	cd web && python3 -m http.server 8790

# live map (live.py): make live NAME=aigource PROJECT="$$P $$P-aigource" ROOT=../aigource LIVE_ARGS="--seed-git --git-reconcile"
NAME      ?= live
PROJECT   ?= $(SESSION)
LIVE_ARGS ?=
live:
	$(PY) live.py run $(NAME) $(PROJECT) $(if $(ROOT),--root $(ROOT)) $(LIVE_ARGS)

live-stop:
	$(PY) live.py stop $(NAME)

live-export:
	$(PY) live.py export $(NAME)

live-verify:
	$(PY) live.py verify $(NAME)

# serve live/NAME to the page at http://127.0.0.1:$(LIVE_PORT)/; new steps' blobs are labelled by the local model
# (a llama-server at http://127.0.0.1:8080, else one started on demand; LIVE_SERVE_ARGS=--no-ask: cache only),
# e.g. make live-serve NAME=aigource LABELS=demo/labels.aigource.json
LIVE_PORT ?= 8791
LIVE_SERVE_ARGS ?=
live-serve:
	$(PY) liveserve.py run $(NAME) --port $(LIVE_PORT) --labels $(LABELS) $(LIVE_SERVE_ARGS)

live-serve-stop:
	$(PY) liveserve.py stop $(NAME)
