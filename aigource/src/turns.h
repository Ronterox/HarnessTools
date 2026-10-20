#ifndef AIG_TURNS_H
#define AIG_TURNS_H
#include <stdbool.h>
#include <stddef.h>

#include "event.h"

/* The stage's per-turn stats, for the turn histogram on its timeline: a light
 * version of the structure analysis' turns, from the events the stage gets,
 * by the analysis' own rules (analysis.h): a turn is a typed or pasted prompt
 * of a main session until its next one, a subagent's work belongs to its
 * session's turn; fix and corrected by the same patterns; a change is failing
 * or untested by the last test/build run after it in its turn; time is the
 * responses' stamps with gaps capped at AN_IDLE_CAP; slow at ST_SLOW_X the
 * average turn. No replay and no git, so +/- are the events' own counts and a
 * change the analysis would drop (a duplicate, outside today's tree) still
 * counts. Only turns that changed a file exist, in time order. */
typedef struct {
    double ts, end;    /* the prompt; its last response or change */
    long secs;         /* active time */
    int add, del, changes, untested, failing; /* changes: untested, failing as AnChange.verify */
    bool fix, corr, intr, slow;
    int tags;          /* UT_* (ui.h) */
    char *title;       /* the prompt's first line */
    char *actor;       /* its session's id */
} TurnStat;

typedef struct TurnStats TurnStats;

TurnStats *ts_new(const char *root);
void ts_free(TurnStats *t);
/* Events of any kind (what it needs is copied), each once. */
void ts_add(TurnStats *t, const Event *evs, size_t n);
/* The turns, recomputed when events came since the last call: *n of them,
 * *big the most +/- of one (at least 1). Valid until the next ts_get. */
const TurnStat *ts_get(TurnStats *t, int *n, int *big);

#endif
