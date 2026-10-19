/* Unit tests for the change rings in model.c: a change is sized next to its
 * file (or the average file when that's bigger) and gets the matching tier;
 * touched files keep their label for LABEL_SECS of real time. */
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "model.h"

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

static Scene *m;

static void apply(EvKind k, const char *path, int add, int del, int lines) {
    static double ts = 1000;
    Event e = {.ts = ts++, .kind = k, .actor = "a", .path = (char *)path, .add = add, .del = del, .lines = lines};
    model_apply(m, &e, true);
}

/* The pulses the last change made, newest last: their count and tiers. */
static int pulses(RingTier *t, Color *c) {
    int n = 0;
    for (int k = 0; k < m->fx_count; k++) {
        Fx *f = &m->fx[(m->fx_head + k) % MAX_FX];
        if (f->kind != FX_PULSE) continue;
        t[n] = f->tier;
        c[n++] = f->color;
    }
    m->fx_count = 0; /* the next change starts clean */
    return n;
}

static bool same(Color a, Color b) { return !memcmp(&a, &b, sizeof a); }

int main(void) {
    m = model_new("/nonexistent-aigource-root");
    RingTier t[8];
    Color c[8];
    char path[32];
    for (int i = 0; i < 7; i++) {
        snprintf(path, sizeof path, "src/f%d.c", i);
        apply(EV_READ, path, 0, 0, 200);
    }
    apply(EV_READ, "src/big.c", 0, 0, 1000);
    apply(EV_READ, "lib/mid.py", 0, 0, 200);
    apply(EV_READ, "src/tiny.c", 0, 0, 20);
    CHECK(pulses(t, c) == 0); /* reads don't ring */
    CHECK(m->nsized == 10);

    apply(EV_EDIT, "src/big.c", 10, 10, 1000); /* 10 lines of 1000: minor, both sides */
    CHECK(pulses(t, c) == 2 && t[0] == RING_MINOR && t[1] == RING_MINOR);
    CHECK(same(c[0], kind_color(EV_CREATE)) && same(c[1], kind_color(EV_DELETE)));

    apply(EV_EDIT, "lib/mid.py", 60, 20, 240); /* 0.3 and 0.1 of a 200-line file */
    CHECK(pulses(t, c) == 2 && t[0] == RING_NOTABLE && t[1] == RING_MINOR);

    apply(EV_EDIT, "src/tiny.c", 20, 20, 20); /* whole tiny file, but next to the average: minor */
    CHECK(pulses(t, c) == 2 && t[0] == RING_MINOR && t[1] == RING_MINOR);

    apply(EV_CREATE, "src/new.c", 500, 0, 500); /* 2.5x the usual file */
    CHECK(pulses(t, c) == 1 && t[0] == RING_MAJOR);

    apply(EV_CREATE, "src/avg.c", 200, 0, 200); /* about one usual file */
    CHECK(pulses(t, c) == 1 && t[0] == RING_NOTABLE);

    /* First seen in an edit: its length before comes from the length after
     * (1000), so +300 is notable, not the major it'd be next to the average. */
    apply(EV_EDIT, "src/fresh.c", 300, 0, 1300);
    CHECK(pulses(t, c) == 1 && t[0] == RING_NOTABLE);

    /* An `rm` guessed from a command has no count: it removed the whole file. */
    apply(EV_READ, "src/gone.c", 0, 0, 800);
    apply(EV_DELETE, "src/gone.c", 0, 0, -1);
    CHECK(pulses(t, c) == 1 && t[0] == RING_MAJOR && same(c[0], kind_color(EV_DELETE)));
    CHECK(m->nodes[model_find(m, false, "src/gone.c")].last_del == 800);

    apply(EV_EDIT, "src/big.c", 1000, 1000, 1000); /* a full rewrite */
    CHECK(pulses(t, c) == 2 && t[0] == RING_MAJOR && t[1] == RING_MAJOR);

    apply(EV_TOUCH, "src/f0.c", 0, 0, -1); /* changed by a command, size unknown */
    CHECK(pulses(t, c) == 1 && t[0] == RING_MINOR && same(c[0], kind_color(EV_TOUCH)));

    /* Deleted files leave the average; recreated ones come back. */
    int n0 = m->nsized;
    apply(EV_DELETE, "src/new.c", 0, 500, -1);
    CHECK(pulses(t, c) == 1 && t[0] == RING_MAJOR);
    CHECK(m->nsized == n0 - 1);
    apply(EV_CREATE, "src/new.c", 10, 0, 10);
    CHECK(pulses(t, c) == 1 && t[0] == RING_MINOR);
    CHECK(m->nsized == n0);
    apply(EV_READ, "gen/huge.json", 0, 0, 50000); /* counts as 4000 in the average */
    double sum = 0;
    for (int i = 0; i < m->nnodes; i++)
        if (!m->nodes[i].is_dir && !m->nodes[i].deleted && m->nodes[i].lines >= 0)
            sum += fmin(m->nodes[i].lines, 4000); /* SIZE_CAP */
    CHECK(fabs(sum - m->sum_lines) < 1e-9);

    /* Labels: real seconds, frozen while paused (dt 0), gone after LABEL_SECS. */
    int ni = model_find(m, false, "src/new.c");
    CHECK(m->nodes[ni].label == LABEL_SECS);
    model_update(m, 0, 0);
    CHECK(m->nodes[ni].label == LABEL_SECS);
    for (int i = 0; i < (int)(LABEL_SECS * 20) + 1; i++) model_update(m, 1.0f / 20, 0);
    CHECK(m->nodes[ni].label == 0);

    model_free(m);
    if (!fails) printf("ok   rings\n");
    return fails != 0;
}
