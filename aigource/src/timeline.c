#include "timeline.h"

#include <stdlib.h>
#include <string.h>

#include "util.h"

/* Width a real gap g occupies on the timeline. */
static double squeeze(double g) { return g <= TL_GAP_CAP ? g : TL_GAP_CAP; }

static void recompute_v(History *h) {
    h->v = xrealloc(h->v, (h->cap ? h->cap : 1) * sizeof *h->v);
    for (size_t i = 0; i < h->n; i++)
        h->v[i] = i ? h->v[i - 1] + squeeze(h->ev[i].ts - h->ev[i - 1].ts) : 0;
}

void hist_add(History *h, Event *evs, size_t n, size_t n_applied) {
    if (!n) return;
    if (h->n + n > h->cap) {
        h->cap = (h->n + n) * 2;
        h->ev = xrealloc(h->ev, h->cap * sizeof *h->ev);
    }
    memcpy(h->ev + h->n, evs, n * sizeof *evs);
    h->n += n;
    /* Applied events all have ts <= sim and the rest ts > sim, so after the sort
     * the applied ones are again a prefix. */
    qsort(h->ev, h->n, sizeof *h->ev, ev_cmp);
    h->applied += n_applied;
    recompute_v(h);
}

size_t hist_upper(const History *h, double ts) {
    size_t lo = 0, hi = h->n;
    while (lo < hi) {
        size_t mid = (lo + hi) / 2;
        if (h->ev[mid].ts <= ts) lo = mid + 1;
        else hi = mid;
    }
    return lo;
}

double tl_length(const History *h, double end) {
    if (!h->n) return 0;
    double last = h->ev[h->n - 1].ts;
    return h->v[h->n - 1] + (end > last ? squeeze(end - last) : 0);
}

double tl_to_v(const History *h, double ts, double end) {
    if (!h->n || ts <= h->ev[0].ts) return 0;
    size_t i = hist_upper(h, ts) - 1; /* last event at or before ts */
    double next = i + 1 < h->n ? h->ev[i + 1].ts : end;
    double g = next - h->ev[i].ts;
    if (g <= 0) return h->v[i];
    double f = (ts - h->ev[i].ts) / g;
    if (f > 1) f = 1;
    return h->v[i] + f * squeeze(g);
}

double tl_from_v(const History *h, double v, double end) {
    if (!h->n) return end;
    if (v <= 0) return h->ev[0].ts;
    size_t lo = 0, hi = h->n; /* last i with v[i] <= v */
    while (lo < hi) {
        size_t mid = (lo + hi) / 2;
        if (h->v[mid] <= v) lo = mid + 1;
        else hi = mid;
    }
    size_t i = lo - 1;
    double next = i + 1 < h->n ? h->ev[i + 1].ts : end;
    double g = next - h->ev[i].ts;
    double c = squeeze(g);
    if (c <= 0) return h->ev[i].ts;
    double f = (v - h->v[i]) / c;
    if (f > 1) f = 1;
    return h->ev[i].ts + f * g;
}
