#ifndef AIG_ENGINE_H
#define AIG_ENGINE_H
#include <stdbool.h>
#include <stddef.h>

#include "analysis.h"
#include "event.h"

/* One repo's analysis kept warm: it owns the Analysis fed every event given
 * to it, builds (an_build, then the owner's derive) on a worker thread, one at
 * a time, the rebuild cadence and the latest snapshot. No window: the clock is
 * now_mono. The structure screen (sview.c) and the server (serve.c) consume
 * it, each with its own part derived from every snapshot; a GUI started with
 * -a runs one for both (render.c). Every call but the worker's own is from one thread (the owner's); a
 * finished build becomes the latest only in eng_swap, so what the owner reads
 * stays put between its calls. */
typedef struct Engine Engine;

/* What a consumer keeps beside each snapshot: derive makes it on the worker,
 * after an_build (a NULL result counts the build as cancelled); drop frees
 * it, before its snapshot goes. A part with no derive is always NULL. */
typedef struct {
    void *(*derive)(const AnSnapshot *s);
    void (*drop)(void *x);
} EngPart;

enum { ENG_STRUCTURE, ENG_SERVE, ENG_PARTS }; /* the parts: sview.c's, serve.c's */

/* Live rebuilds: quiet seconds after the last event that counts, or once the
 * first one waiting has waited cap; quiet 0: off. */
typedef struct { double quiet, cap; } EngCadence;

typedef struct {
    EngCadence changes; /* counting creates, edits, deletes, turns (and eng_touch) */
    EngCadence any;     /* counting every event, while eng_any is on (from the start when set) */
    EngPart part[ENG_PARTS];
} EngOpts;

Engine *eng_new(const char *root, const EngOpts *o);
/* Cancels and joins a running build, frees the latest pair and the Analysis. */
void eng_free(Engine *e);
/* Events for the analysis (copied), each once; any kind. */
void eng_add(Engine *e, const Event *evs, size_t n);
/* Something on disk changed with no event to say so (a save): counts as one. */
void eng_touch(Engine *e);
/* Build when any event arrived since the last build, or none is in yet: now,
 * or after the running one (cancelled when `now`). */
void eng_request(Engine *e, bool now);
/* Live: once a first build is in, start the next when a cadence says so. */
void eng_tick(Engine *e);
/* The `any` cadence on or off (a host's structure screen, open or closed). */
void eng_any(Engine *e, bool on);
/* A build finished since the last call: true when it is the latest now, the
 * snapshot before it in *old and its parts in old_x[ENG_PARTS] (NULL before
 * the first), for the owner to free with eng_drop once nothing reads them;
 * false when none finished or it was cancelled (a cancelled build asked
 * again by eng_request starts here). */
bool eng_swap(Engine *e, AnSnapshot **old, void **old_x);
void eng_drop(Engine *e, AnSnapshot *s, void **x);
/* The latest snapshot (NULL before the first build), and its part k. */
AnSnapshot *eng_snap(const Engine *e);
void *eng_part(const Engine *e, int k);
long eng_rev(const Engine *e);      /* builds that became the latest */
bool eng_running(const Engine *e);
long eng_added(const Engine *e);    /* events added */
/* The last finished build's counts (an_snapshot_stats' line), "" before one. */
void eng_stats(Engine *e, char *b, size_t n);

#endif
