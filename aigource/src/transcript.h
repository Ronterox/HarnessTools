#ifndef AIG_TRANSCRIPT_H
#define AIG_TRANSCRIPT_H
#include <stddef.h>

#include "event.h"

/* Claude Code transcript JSONL -> Events. The format is internal to Claude Code,
 * so every assumption about it lives in transcript.c. One parser per file. */
#define TR_PROMPT_MAX 140 /* characters of a prompt's first line kept (its title), no "…" */

typedef void (*EmitFn)(void *ud, Event *ev); /* receives ownership of ev's strings */
typedef struct Transcript Transcript;

/* src: the transcript's path, borrowed (must outlive every event), stamped on
 * events for the detail view; NULL for none. */
Transcript *tr_new(const char *root, const char *actor, const char *actor_name, const char *src,
                   EmitFn emit, void *ud);
/* One JSON line, no newline; off = its byte offset in src. */
void tr_feed(Transcript *t, const char *line, size_t len, long off);
void tr_free(Transcript *t);

/* For the filesystem watcher: the earliest Bash call started after `since` that
 * is still running, if any (returns 0 when none). Its result will say which
 * files it changed. Older unanswered calls belong to killed sessions. */
int tr_bash_running(const Transcript *t, double since, double *start);
/* An Edit/Write/MultiEdit/NotebookEdit on this repo-relative path awaits its result. */
int tr_writing(Transcript *t, const char *rel);
/* A failed Bash call running at `ts` (within `slack` seconds of its span): Claude
 * Code records no file changes for those, so the watcher attributes what changed
 * meanwhile to it. Fills the call's identity into *e (src, tool_id copy, notes,
 * turn, t0, ts = its end); returns 0 when none. */
int tr_failed_call(const Transcript *t, double ts, double slack, Event *e);

/* Detail view of one tool call: everything it did, as display lines (a diff,
 * a created file, a command and its output, a tool's input and result).
 * Re-read from the transcript on demand: nothing of it is kept in memory. */
typedef enum { DL_TEXT, DL_HEAD, DL_DIM, DL_HUNK, DL_ADD, DL_DEL, DL_ERR } DlKind;
typedef struct {
    DlKind kind;
    int no; /* line number in the file, 0 = none */
    char *s;
} DetailLine;
typedef struct {
    char *tool; /* "Edit", "Bash", "deskpilot: ui", ... */
    DetailLine *lines;
    int n, cap;
    int state; /* 0 complete, 1 no result recorded yet (running), -1 record unreadable */
} Detail;
/* The call with tool_use `id` on the line at byte `off` of `src`. `path`
 * (repo-relative, may be NULL) picks one file out of a command's changes. */
Detail *tr_detail(const char *src, long off, const char *id, const char *path);
void detail_free(Detail *d);
/* The agent's comments on a call: the text blocks it wrote before it (since the
 * last tool result or prompt), oldest first. Up to `n` of them from the line at
 * `off`, stopping at the tool_use `id`. Re-read on demand like the detail. */
typedef struct {
    char **s;
    int n;
} Notes;
Notes tr_notes(const char *src, long off, int n, const char *id);

/* One change of a file re-read from its record, for replaying the file. Like
 * the detail: tool_use `id` on the line at `off` of `src`; `path` (relative to
 * the repo `root`) picks the file out of a command's changes. */
typedef struct {
    int old_start, old_lines, new_start, new_lines;
    char **lines; /* as recorded: "+added", "-removed", " kept" */
    int n;
} Hunk;
typedef struct {
    int state;      /* 0 found, 1 no result recorded yet, -1 record unreadable */
    int cmd;        /* a command's change (bashEditDiff hunks of path), not an Edit/Write */
    char *tool;     /* the tool's name ("Edit", "Write", "Bash", ...); NULL when unread */
    int patched;    /* an Edit/Write result recorded a structuredPatch (maybe empty) */
    char *original; /* the file before it (originalFile); NULL when not recorded */
    char *created;  /* a Write that created the file: its content; else NULL */
    Hunk *hunks;    /* structuredPatch, or the command's hunks for path */
    int nhunks;
} Change;
Change *tr_change(const char *src, long off, const char *id, const char *root, const char *path);
void change_free(Change *ch);
void notes_free(Notes *ns);
Detail *detail_new(const char *tool);
void detail_line(Detail *d, DlKind kind, const char *s); /* s may hold newlines */
/* The line of the file detail line i stands for, 0 none: the numbered lines
 * of a read, a created file, a diff's kept and added lines. A removed line
 * takes the new side's line where it was: the next kept or added one, else
 * (at a hunk's end) the one before, else the hunk's start. */
int detail_file_line(const Detail *d, int i);

#endif
