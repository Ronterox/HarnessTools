#ifndef AIG_TRANSCRIPT_H
#define AIG_TRANSCRIPT_H
#include <stddef.h>

#include "event.h"

/* Claude Code transcript JSONL -> Events. The format is internal to Claude Code,
 * so every assumption about it lives in transcript.c. One parser per file. */
typedef void (*EmitFn)(void *ud, Event *ev); /* receives ownership of ev's strings */
typedef struct Transcript Transcript;

Transcript *tr_new(const char *root, const char *actor, const char *actor_name,
                   EmitFn emit, void *ud);
void tr_feed(Transcript *t, const char *line, size_t len); /* one JSON line, no newline */
void tr_free(Transcript *t);

/* For the filesystem watcher: the earliest Bash call still running, if any
 * (returns 0 when none). Its result will say which files it changed. */
int tr_bash_running(const Transcript *t, double *start);
/* An Edit/Write/MultiEdit/NotebookEdit on this absolute path awaits its result. */
int tr_writing(const Transcript *t, const char *abs_path);
const char *tr_actor(const Transcript *t);
const char *tr_actor_name(const Transcript *t);

#endif
