# How the structure draft gets its numbers

Moved: the spec for every number is now `../docs/STRUCTURE.md`, written for the
app (`src/analysis.c`, `src/metrics.c`) and updated where the port differs from
this draft (tree-sitter definitions, the whole-repo tree, command reads,
turn kinds).

The draft (`build_data.py` + `structure.tpl.html`) stays as the test oracle for
the app: `tests/structure_parity.py` compares `aigource --structure-dump` with
`build_data.py` on this repo's transcripts, and `tests/metrics_parity.js` runs
the page's own JavaScript on the app's snapshot and compares it with
`aigource --metrics`. Its names are mapped to the C functions at the end of
`docs/STRUCTURE.md`.
