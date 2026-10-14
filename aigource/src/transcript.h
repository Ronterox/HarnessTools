#ifndef AIG_TRANSCRIPT_H
#define AIG_TRANSCRIPT_H
#include <stddef.h>

#include "event.h"

/* Claude Code transcript JSONL -> Events. The format is internal to Claude Code,
 * so every assumption about it lives in transcript.c. One parser per file. */
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
const char *tr_actor(const Transcript *t);

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
Detail *detail_new(const char *tool);
void detail_line(Detail *d, DlKind kind, const char *s); /* s may hold newlines */
const char *tr_actor_name(const Transcript *t);

#endif
