# Structure view draft

The design draft of aigource's structure view, as one HTML page built from real
Claude Code transcripts: Icicle, Treemap and Floating nodes over the repo's
folders → files → classes → functions, with an inspector (code with AI blame,
the turns that changed it and the agent's comments, rework, complexity trend,
fixes, change coupling, friction flags).

The view now lives in the app (`S` in aigource; spec in `../docs/STRUCTURE.md`,
plan and what landed in `PLAN.md`). The draft is kept as the **test oracle**:
`../tests/structure_parity.py` runs `build_data.py` against `aigource
--structure-dump`, and `../tests/metrics_parity.js` runs the page's script
(`structure.tpl.html`, DOM stubbed) against `aigource --metrics`. Change the
draft only to fix the oracle; the app's rules are in `docs/STRUCTURE.md`.

```sh
draft/build.sh                          # out/structure.html for aigource/src and aic/aic of this repo
draft/build.sh /tmp/x.html ~/code/app src lib
```

`build_data.py` replays every change on the file as it was then (Edit/Write
results; command changes lined up with git), so each line of today's code knows
the change that wrote it. `structure.tpl.html` is the page (d3 and
highlight.js from cdnjs); `__DATA__` is replaced by the data. Output goes to
`out/` (gitignored): it embeds source code and prompts.

The draft finds C definitions by regex and Python ones by indentation, and maps
only `.c` and `.py` files under the folders given; the app uses tree-sitter
textobjects and the whole repo.
