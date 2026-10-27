---
description: Suggest edits for a file's warnings (aic fix --warnings FILE)
task: fixfile
---
The human asked for fixes to the warnings below, from your last compile of `{{file.name}}`. They will review your
edits hunk by hunk before any is applied.
- For each warning, give the smallest edit of this file that resolves it and keeps everything else behaving as it
  does. Follow the file's language, idioms and error-handling style; keep its indentation.
- When resolving it is not a local edit of this file (it needs other files, a design decision, or intent you can't
  infer), list it under skipped with a one-line reason instead of guessing.
- item = the warning's number below. help: one line saying what your edit does.
- Each edit replaces `old` with `new`: copy `old` verbatim from the file as it was in your last compile, WITHOUT the
  "  12| " prefix, prefer whole lines; line = where `old` starts. Never touch the lines of another warning's edit or
  of the suggestions you already gave in that compile.
Verify what you rely on in other code with Read/Grep/Glob first.

{{items}}
