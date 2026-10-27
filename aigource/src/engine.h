#ifndef AIG_ENGINE_H
#define AIG_ENGINE_H
#include <stdbool.h>
#include <stddef.h>

#include "analysis.h"
#include "event.h"

/* One repo's analysis kept warm: it owns the Analysis fed every event given
 * to it, builds (an_build, then the owner's derive) on a worker thread, one at
 * a time, the rebuild cadence and the latest snapshot. No window: the clock is
 * now_mono. The structure screen (sview.c) and `--serve` (serve.c) consume
 * it. Every call but the worker's own is from one thread (the owner's); a
 * finished build becomes the latest only in eng_swap, so what the owner reads
 * stays put between its calls. */
typedef struct Engine Engine;

typedef struct {
    double quiet, cap; /* live: rebuild this long after the last event that counts, or once one waited cap */
    bool changes_only; /* only creates, edits, deletes and turns count (else every event) */
    /* On the worker, after an_build: what the owner keeps beside the snapshot
     * (NULL: none; a NULL result counts the build as cancelled). */
    void *(*derive)(const AnSnapshot *s);
    /* Frees a pair the engine made: derive's result (NULL when none), then its snapshot. */
    void (*drop)(AnSnapshot *s, void *extra);
} EngOpts;

Engine *eng_new(const char *root, const EngOpts *o);
/* Cancels and joins a running build, frees the latest pair and the Analysis. */
void eng_free(Engine *e);
/* Events for the analysis (copied), each once; any kind. */
void eng_add(Engine *e, const Event *evs, size_t n);
/* Something on disk changed with no event to say so (a save): counts as one. */
void eng_touch(Engine *e);
/* Build when something counted since the last build, or none is in yet: now,
 * or after the running one (cancelled when `now`). */
void eng_request(Engine *e, bool now);
/* Live: once a first build is in, start the next when the cadence says so. */
void eng_tick(Engine *e);
/* A build finished since the last call: true when it is the latest now, the
 * pair before it in *old and *old_extra (NULL before the first), for the owner to
 * free with eng_drop once nothing reads it; false when none finished or it
 * was cancelled (a cancelled build asked again by eng_request starts here). */
bool eng_swap(Engine *e, AnSnapshot **old, void **old_extra);
void eng_drop(Engine *e, AnSnapshot *s, void *extra);
/* The latest snapshot (NULL before the first build), derive's result in *extra. */
AnSnapshot *eng_snap(const Engine *e, void **extra);
long eng_rev(const Engine *e);      /* builds that became the latest */
bool eng_running(const Engine *e);
long eng_added(const Engine *e);    /* events that counted */
/* The last finished build's counts (an_snapshot_stats' line), "" before one. */
void eng_stats(Engine *e, char *b, size_t n);

#endif
