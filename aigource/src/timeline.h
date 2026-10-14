#ifndef AIG_TIMELINE_H
#define AIG_TIMELINE_H
#include <stddef.h>

#include "event.h"

/* Every event seen, time-ordered, kept so the scene can be rebuilt at any point
 * of the timeline. ev[0, applied) are reflected in the current scene. */
typedef struct {
    Event *ev;
    size_t n, cap, applied;
    double *v; /* gap-compressed "timeline time" of each event */
} History;

/* Idle stretches longer than this are drawn this long, so a session spread over
 * days doesn't make the bar mostly empty. */
#define TL_GAP_CAP 120.0

/* Merges events in (takes ownership) and keeps ev sorted. The caller must have
 * applied any incoming event with ts <= the current sim time already and pass
 * how many of `evs` (a ts-sorted prefix) those are, so ev[0, applied) stays the
 * applied prefix. */
void hist_add(History *h, Event *evs, size_t n, size_t n_applied);
size_t hist_upper(const History *h, double ts); /* first index with ev.ts > ts */

/* Real time <-> timeline time over [first event, end]. */
double tl_to_v(const History *h, double ts, double end);
double tl_from_v(const History *h, double v, double end);
double tl_length(const History *h, double end);

#endif
