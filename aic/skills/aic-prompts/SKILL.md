---
name: aic-prompts
description: Find the requests the human keeps typing to you (and the questions they keep asking the aic compiler) in this project's history, and turn them into aic prompt templates in .aic/prompts/. Only when the human runs /aic-prompts.
disable-model-invocation: true
---

# aic prompt templates from this project's history

The human reviews your work with aic, from Neovim. A **prompt template** turns
a request they type again and again into one pick from a list: they choose it,
aic renders it with their doc, diff or selection, they read it and send it.
Your job: find those repeated requests in this project's history, propose
templates for them, and write the ones the human approves.

`aic` here means `{AIC}` (it may not be on PATH). Run it from the project root.

## 1. Read the history

```sh
{AIC} history | head -300        # read on with sed -n '301,600p' etc.
{AIC} history --json             # longer texts (up to 2000 characters each), when a line was cut (…)
```

- `prompt` lines, newest session first: what the human typed to a worker in
  this project (every session on disk; a prompt they re-sent counts once).
  aic's own sends start with `[aic] `; the text after the header is the
  pasted data.
- `question` lines, after the prompts: what they asked the compiler (aic's
  reviewer). `whatif` lines are what-if questions: ask templates never apply
  to those, so don't count them.

The templates that already exist, so you don't duplicate them:

```sh
{AIC} prompts --json             # submit templates (built-in: spec, selection, message, review)
{AIC} prompts --compiler --json  # compiler templates (built-in: compile, ask, whatif, fix)
```

## 2. Find the patterns

A pattern is one intent asked **at least 3 times, in at least 2 sessions**,
however it was worded ("run the tests and fix what fails", "make the tests
pass", "tests are red, fix them"). For each one, split:

- the fixed part, the instruction: it becomes the template's header (submit)
  or its instructions (ask);
- the varying part, the data: it becomes a variable (`{{doc}}`,
  `{{selection}}`, `{{diff}}`…) or, when the human types it each time,
  `{{input}}`.

Skip one-off tasks, slash commands, and anything a built-in already does.
`prompt` patterns become **submit templates**, `question` patterns become
**ask templates**.

## 3. Propose before writing

Show the human a short list. For each candidate give:

- the name and kind (submit or ask);
- the header (submit) or a one-line summary of the instructions (ask);
- the evidence: how many times, in how many sessions, and one or two short
  quotes.

Ask which ones to write. Write only those. Never copy secrets, tokens or
private data from the history into a template: generalize.

## 4. Submit templates: `.aic/prompts/<name>.md`

```markdown
---
description: Run the tests and fix what fails, keeping my notes in mind
header: "Run the test suite, fix every failure, then tell me what you changed and why:"
requires: [doc]
---
My notes ({{doc.name}}):

{{content}}
```

Front matter keys (no others):

| key | meaning |
|---|---|
| `description` | one line for the picker |
| `header` | **required**. Typed into the worker as the human's own words |
| `requires` | variables that must not be empty, or nothing is sent, e.g. `[changed]`, `[selection]` |
| `marks_sent` | `true` only for a template that sends the doc as work, like `spec` (the next `{{diff}}` starts from it) |
| `input` | a label; aic asks the human for text and puts it in `{{input}}` |

Quote a value that contains ` #` (a space, then `#`, starts a comment), and
write `\"` for a `"` inside double quotes.

**The header is typed as the human**, so aic refuses it, and nothing is sent,
if it has any of these:

- digits (a dialog could take them as menu choices);
- `@` or a backslash;
- invisible characters;
- more than 294 characters (aic puts `[aic] ` in front, and the whole line
  must stay under 300).

It is one line. Variables in the header are filled in before it is checked,
so keep `{{selection.lines}}`, `{{doc}}`, `{{doc.name}}` and `{{input}}` out
of it (they can hold digits); `{{#var}}…{{/var}}` sections are fine. Keep
numbers, paths and `@anchors` in the body. The header must say what to do with the body: the body
is pasted, and the worker reads pasted text as data, not as instructions.

The body is pasted under the header. Variables:

| variable | value |
|---|---|
| `{{doc}}` | the doc's text |
| `{{doc.name}}` | its name |
| `{{selection}}` `{{selection.lines}}` | the selected lines (empty without a selection), and their range `a-b` |
| `{{content}}` | the selection if there is one, else the whole doc |
| `{{previous}}` | what this worker was last sent of the doc |
| `{{changed}}` | `yes` when the doc changed since then, else empty |
| `{{diff}}` | a unified diff of the doc, from `{{previous}}` to now (not the code: that is the `@diff` anchor) |
| `{{diagnostics}}` | the doc's last compile results, one per line |
| `{{hand_edits}}` | where the human edited code by hand since their last send: `src/a.py:10-14, …` |
| `{{anchors}}` | what the `@anchors` in what is sent point at (see below) |
| `{{input}}` | the text the human typed (see `input`) |

How variables work:

- `{{#var}}…{{/var}}` keeps its text only when `var` is non-empty, and
  `{{^var}}…{{/var}}` only when it is empty. Write `\{{` for a literal `{{`.
- `{{anchors}}` and `{{hand_edits}}` are added at the end anyway when you
  leave them out.
- `@anchors` written in the body or the doc are resolved for the worker:
  `@diff` (git diff HEAD), `@file:42`, `@file:10-20`, `@file:symbol`, `@log`
  and the like are inlined; a plain `@file` or `@dir/` stays a path the worker
  opens itself.

A name starts with a letter or `_`, then letters, digits, `_ . -`. **A file
with any other name is silently ignored** (no error, not listed). Don't reuse
a built-in name: a project file replaces the built-in (and a same-named user
template in `~/.config/aic/prompts/`).

## 5. Ask templates: `.aic/prompts/compiler/<name>.md`

These are instructions to the compiler, a read-only reviewer that answers the
human's questions about your work.

```markdown
---
description: Explain with a call diagram before any prose
task: ask
---
Answer the question below. Start with an ASCII call diagram of the code
involved, then at most five sentences.

{{question}}
```

Keys: `description`, `task: ask`. A `model:` key is ignored for project
templates.

| variable | value |
|---|---|
| `{{question}}` | the human's question |
| `{{doc}}` | the current doc |
| `{{diagnostics}}` | its last compile results |
| `{{activity}}` | what the worker did recently |
| `{{pointing}}` | what the cursor was on |
| `{{anchors}}` | the content of the `@anchors` in the question |
| `{{doc.name}}` | the doc's name |

Every one you leave out is added after your text anyway, so place only the
ones where the order matters. aic also adds a closing block of its own after
every project template; that's expected.

Never name an ask template `compile`, `ask`, `whatif` or `fix`: those files
replace aic's own instructions to the compiler.

## 6. Check

```sh
{AIC} prompts --json             # each new submit template is listed, with no "error"
{AIC} prompts --compiler --json  # each new ask template: task "ask", no "error"
{AIC} render --prompt <name> --doc <doc> --json    # every submit template: this is what checks the header
{AIC} ask --print --template <name> "a test question"   # every ask template: shows the prompt, asks nothing
```

- `prompts --json` only checks the syntax. The header rules are checked when
  a template is rendered, so render **each** new submit template.
- `render` options: `--input TEXT` when the template asks for input,
  `--from A --to B` (doc lines) when it uses `{{selection}}`.
- `render` needs an existing doc (`{AIC} docs` lists them). If there is none,
  say so and let the human try the template; don't create docs.

Fix every error, then check again.

## 7. Tell the human how to use them

- **Submit templates:** in a doc, `<leader>zs` (`:AicSubmit`; in visual mode,
  only the selection), then pick the template. The rendered prompt opens; `:w`
  sends it.
- **Ask templates:** `:AicAskTemplate <name>` (or `<leader>zA` to pick one).
  Questions (`<leader>z?` and the questions view) then use it until Neovim
  restarts. `:AicAskTemplate ask` goes back to the default.

Write only under `.aic/prompts/`. `.aic/docs/` is the human's and the hook
refuses your writes there.
