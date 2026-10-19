---
description: Compile a doc against the code and the worker's activity (the diagnostics)
task: compile
---
Compile the doc named below ("the spec") against the repository and the worker's recent activity.
Emit diagnostics of these kinds:
- mismatch: the spec says X but the worker did Y, or has not done it yet
- incoherent: the spec contradicts itself, is ambiguous, or has a hole the worker would have to guess
- model: the spec assumes something about the existing code that is not true
- unspecified: the worker changed things the spec does not mention (scope creep, side effects)
For each item set file="doc" and line=<doc line> when it is about a line of the doc being compiled, otherwise file=<repo-relative path> and line=<code line> (and spec_line=<doc line> if a doc line is related).
severity: error = will produce wrong behaviour or contradicts the spec; warning = likely problem or unclear; info = worth knowing.
Report every item that still holds after the new activity: your list replaces this doc's previous list (shown below); items of other docs are not affected.
An empty or nearly empty spec is fine: then only report 'unspecified' items that the human should know about.
A heading containing "[done]" marks a finished requirement: verify it still holds (report it if
the code broke it), but never report it as pending work or ask the human to remove it.
Pseudocode is intent: do not report syntax, naming or missing-detail nits in it; report only where its
logic is wrong, contradicts itself, or disagrees with the code. For box diagrams, check that every box
exists in the code and every arrow is a real call/data path (and flag real paths the diagram omits).
If the code has an obvious logic bug (off-by-one, inverted condition, wrong variable, missing await,
state not persisted...) report it even when the spec does not mention that area, as kind=mismatch
when it contradicts the spec's intent, otherwise kind=unspecified, severity=error.
summary: 1-3 sentences on the state of things.

{{doc}}

{{previous_items}}

{{#anchors}}
{{anchors}}

{{/anchors}}
{{activity}}
