PY      ?= .venv/bin/python
SESSION ?= $(HOME)/.claude/projects/-home-rontero-Documents-Projects-AIProjects-HarnessTools-aiblob-demo-app
OUT     ?= web/timeline.json
ROOT    ?=
PERF_OUT  ?= demo/perf/perf.csv
PERF_ARGS ?=

.PHONY: timeline test perf serve
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
