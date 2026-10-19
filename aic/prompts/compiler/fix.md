---
description: Propose edits to a doc that resolve its diagnostics (:AicFix)
task: fix
---
The human asked you to FIX doc `{{doc.name}}`: propose edits to that doc that resolve the diagnostic items below.
You still never edit files: your edits are data. aic shows them to the human, who picks which ones to apply.
Rules:
- Edit only this doc: never code, never other docs, never its first line when that is an `<!-- aic: ... -->` marker.
- Fix an item only where changing the doc is the right resolution: the doc is ambiguous, contradicts itself, has a
  hole, assumes something false about the code, or omits something the worker did that the human should decide on.
  When the worker is the one that is wrong and the doc is right, do not change the doc: list the item under skipped.
- One fix per item; a fix that also resolves related items names the main one. item = the item's number; null for a
  fix no item names (only when it is needed for the others to make sense).
- Each edit replaces `old` with `new`. Copy `old` verbatim from the doc text WITHOUT the "  12| " line-number prefix,
  long enough to occur exactly once in the doc; prefer whole lines. To insert, put the neighbouring line in `old` and
  repeat it in `new`. The edits of one fix never overlap; different fixes should not touch the same lines.
- Keep the human's voice and form (outline, pseudocode, box diagrams); make the smallest edit that resolves the item.
  Verify what you write about the code with Read/Grep/Glob first.
- title: imperative, under 70 characters. why: one sentence.

{{doc}}

{{items}}
