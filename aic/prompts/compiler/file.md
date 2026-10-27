---
description: Compile one source file, answered like rustc (aic compile FILE)
task: file
---
Compile the file `{{file.name}}` below the way a compiler with an excellent linter would, but for meaning: report what
is wrong with it, not how you would have written it.
- error: it will misbehave or cannot work: a logic bug (off-by-one, inverted condition, wrong variable, missing await,
  unhandled error path, leaked resource, race), a call that does not match its definition, code that cannot run.
- warning: likely a problem, or a trap for the next reader: dead code, an ignored result, shadowing that hides a bug.
- note: worth knowing; no change needed.
No style, formatting or naming nits; nothing a formatter or the real compiler would already say. Verify every claim
about other code (definitions, callers, imports) with Read/Grep/Glob before making it.
For each item:
- code: a short kebab-case lint name (off-by-one, unused-result, missing-await, wrong-arity...).
- message: one line, lowercase, no final period, as rustc writes them ("mismatched types").
- line: the line it is about. snippet: the exact text on that line it points at, copied verbatim (the shortest
  that identifies it). label: what is wrong there, in a few words ("expected `int`, found `str`").
- secondary: other lines of this file that explain it, each with line, snippet and label ("defined here").
- note: optional extra explanation, one or two sentences.
- help: how to fix it, one line. Add `edits` only when the fix is a local edit of this file you are sure of: aic fix
  applies them as written. Each edit replaces `old` with `new`: copy `old` verbatim from the file WITHOUT the "  12| "
  prefix, prefer whole lines, keep the file's indentation; line = where `old` starts. Edits of different items never
  touch the same lines.
Report every item that still holds: your list replaces your previous list for this file (shown below).
summary: one sentence on the file's state.

{{file}}

{{previous_items}}

{{#changes}}
{{changes}}
{{/changes}}
