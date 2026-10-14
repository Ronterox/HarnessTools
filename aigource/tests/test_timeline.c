/* Unit tests for timeline.c: gap compression, time <-> timeline mapping, and the
 * applied-prefix invariant of hist_add. */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>

#include "timeline.h"
#include "util.h"

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)
#define NEAR(a, b) (fabs((a) - (b)) < 1e-6)

static Event ev(double ts) {
    static unsigned long seq;
    Event e = {.ts = ts, .seq = seq++, .kind = EV_EDIT, .actor = xstrdup("a"), .lines = -1};
    return e;
}

int main(void) {
    History h = {0};
    /* Events at 0, 10, 20, then a 1h idle gap, then 3620, 3630. */
    double ts[] = {1000, 1010, 1020, 4620, 4630};
    Event in[5];
    for (int i = 0; i < 5; i++) in[i] = ev(ts[i]);
    hist_add(&h, in, 5, 0);
    double end = 4640;

    CHECK(NEAR(h.v[2], 20));
    CHECK(NEAR(h.v[3], 20 + TL_GAP_CAP));                 /* 1h gap squeezed */
    CHECK(NEAR(tl_length(&h, end), 20 + TL_GAP_CAP + 20)); /* + 10s tail to end */
    CHECK(NEAR(tl_to_v(&h, 1005, end), 5));                 /* inside a short gap: linear */
    CHECK(NEAR(tl_to_v(&h, 1020 + 1800, end), 20 + TL_GAP_CAP / 2)); /* mid long gap */
    CHECK(NEAR(tl_to_v(&h, 500, end), 0));                  /* before first event */

    /* Round trips across the whole bar. */
    double len = tl_length(&h, end);
    for (double v = 0; v <= len; v += len / 37) CHECK(NEAR(tl_to_v(&h, tl_from_v(&h, v, end), end), v));
    for (int i = 0; i < 5; i++) CHECK(NEAR(tl_from_v(&h, h.v[i], end), ts[i]));
    CHECK(NEAR(tl_from_v(&h, len, end), end));

    /* Applied prefix: 3 applied (ts <= sim = 1025); a late batch with one past event. */
    h.applied = 3;
    Event late[2] = {ev(1015), ev(5000)};
    hist_add(&h, late, 2, 1);
    CHECK(h.applied == 4);
    for (size_t i = 0; i < h.applied; i++) CHECK(h.ev[i].ts <= 1025);
    for (size_t i = h.applied; i < h.n; i++) CHECK(h.ev[i].ts > 1025);
    CHECK(hist_upper(&h, 1020) == 4 && hist_upper(&h, 0) == 0 && hist_upper(&h, 1e9) == h.n);

    if (!fails) puts("ok   timeline");
    return fails != 0;
}
