/* Unit test for the context screen's rules that draw nothing (clay.c) and its
 * window layout (slayout.c's lay_wins): which windows start open (the
 * draft's isOpenDefault, the cap, the user's choice), ranks, the calls
 * strip's bands and hit test, the header's words, on the context fixture
 * (tests/fixtures/context/proj) and on made-up agents; the layout settled
 * apart, pinned windows kept, newest nearest, the same run twice. */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "clay.h"

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

static unsigned seed = 7;
static float rnd(void) {
    seed = seed * 1664525u + 1013904223u;
    return (float)(seed >> 8) / 16777216.0f;
}

static void words(void) {
    char b[64];
    struct { double v; const char *k; } ks[] = {{0.4, "0"}, {999.4, "999"}, {999.6, "1000"}, {1234, "1.2k"}, {1250, "1.3k"},
                                                {12345, "12k"}, {1.25e6, "1.3M"}, {12.5e6, "13M"}};
    for (size_t i = 0; i < sizeof ks / sizeof *ks; i++) {
        cl_fmt_k(b, sizeof b, ks[i].v);
        if (strcmp(b, ks[i].k)) printf("  fmt_k(%g) = %s, want %s\n", ks[i].v, b, ks[i].k);
        CHECK(!strcmp(b, ks[i].k));
    }
    cl_fmt_usd(b, sizeof b, 1.234); CHECK(!strcmp(b, "$1.23"));
    cl_fmt_usd(b, sizeof b, 0.0123); CHECK(!strcmp(b, "$0.012"));
    cl_fmt_usd(b, sizeof b, 0.00123); CHECK(!strcmp(b, "$0.0012"));
    cl_fmt_usd(b, sizeof b, 0); CHECK(!strcmp(b, "$0"));
    cl_fmt_min(b, sizeof b, 45); CHECK(!strcmp(b, "45 s"));
    cl_fmt_min(b, sizeof b, 89.6); CHECK(!strcmp(b, "90 s"));
    cl_fmt_min(b, sizeof b, 600); CHECK(!strcmp(b, "10 min"));
    cl_fmt_min(b, sizeof b, 7200); CHECK(!strcmp(b, "2.0 h"));
    cl_fmt_pct(b, sizeof b, 0.0333); CHECK(!strcmp(b, "3.3%"));
    cl_fmt_pct(b, sizeof b, 0.0005); CHECK(!strcmp(b, "0.05%"));
    cl_fmt_pct(b, sizeof b, 0.005); CHECK(!strcmp(b, "0.50%")); /* two decimals under 1% */
    cl_fmt_pct(b, sizeof b, 0.0099); CHECK(!strcmp(b, "0.99%"));
    cl_fmt_pct(b, sizeof b, 0.01); CHECK(!strcmp(b, "1.0%"));
    cl_fmt_min(b, sizeof b, 75); CHECK(!strcmp(b, "75 s"));
}

/* d3.scaleBand: every band's start hits its call; outside clamps */
static void strip(void) {
    for (int n = 1; n <= 700; n += 33) {
        ClStrip s = cl_strip(n, 1000);
        CHECK(fabsf(s.bw - s.step * (n > 300 ? 1 : 0.85f)) < 1e-3f);
        for (int i = 0; i < n; i++) {
            int h = cl_strip_hit(&s, cl_strip_x(&s, i) + s.bw * 0.5f);
            if (h != i) printf("  strip n %d: band %d hit as %d\n", n, i, h);
            CHECK(h == i);
        }
        for (int i = 0; i < n && n <= 300; i++) { /* the gap after a band is its call's (the draft's floor(x / step)) */
            float gx = cl_strip_x(&s, i) + s.bw + (s.step - s.bw) * 0.9f;
            CHECK(cl_strip_hit(&s, gx) == (i < n - 1 ? i : n - 1));
            CHECK(cl_strip_hit(&s, cl_strip_x(&s, i) + s.step * 0.95f) == i);
        }
        CHECK(cl_strip_hit(&s, -20) == 0);
        CHECK(cl_strip_hit(&s, 1500) == n - 1);
        if (n > 1) CHECK(fabsf(cl_strip_x(&s, n - 1) + s.bw - 1000) < 0.01f); /* the last band ends at the strip's end */
    }
    ClStrip s = cl_strip(0, 1000);
    CHECK(cl_strip_hit(&s, 10) == -1);
}

/* n windows of random sizes, the first ranked nearest */
static LWin *wins(int n) {
    LWin *w = calloc((size_t)n, sizeof *w);
    float area = 0;
    for (int k = 0; k < n; k++) {
        bool open = rnd() < 0.1f;
        w[k] = (LWin){.w = open ? 340 + rnd() * 120 : 80 + rnd() * 200, .h = open ? 150 + rnd() * 120 : 24, .rank = k,
                      .id = (unsigned)k * 7 + 3, .fresh = true};
        float a = (w[k].w + 14) * (w[k].h + 14);
        w[k].tr = sqrtf(92 * 92 + (area + a / 2) / ((float)M_PI * 0.4f)) + w[k].h / 4;
        area += a;
    }
    return w;
}

static void layout(int n) {
    LWin *a = wins(n), *b = malloc(sizeof *b * (size_t)n);
    a[n / 3].pinned = true, a[n / 3].x = 400, a[n / 3].y = -300, a[n / 3].fresh = false;
    memcpy(b, a, sizeof *b * (size_t)n);
    lay_wins(a, n, CL_HUB, 120, 120);
    lay_wins(b, n, CL_HUB, 120, 120);
    CHECK(!memcmp(a, b, sizeof *a * (size_t)n)); /* deterministic */
    int over = lay_wins_overlaps(a, n, CL_HUB);
    if (over) printf("  layout of %d: %d overlapping pairs\n", n, over);
    CHECK(over == 0);
    CHECK(a[n / 3].x == 400 && a[n / 3].y == -300); /* pinned: where it was put */
    double d0 = 0, d1 = 0;
    int m = n / 5;
    for (int k = 0; k < m; k++) d0 += hypotf(a[k].x, a[k].y), d1 += hypotf(a[n - 1 - k].x, a[n - 1 - k].y);
    CHECK(d0 < d1); /* distance follows rank: newest nearest */
    for (int k = 0; k < n; k++) CHECK(!a[k].fresh && isfinite(a[k].x) && isfinite(a[k].y));
    /* a step on: the windows stay near where they were (only new ones are seeded) */
    memcpy(b, a, sizeof *b * (size_t)n);
    lay_wins(b, n, CL_HUB, 24, 120);
    double moved = 0;
    for (int k = 0; k < n; k++) moved += hypotf(b[k].x - a[k].x, b[k].y - a[k].y);
    if (moved / n > 60) printf("  layout of %d: a second pass moved them %.0f px on average\n", n, moved / n);
    CHECK(moved / n <= 60);
    CHECK(lay_wins_overlaps(b, n, CL_HUB) == 0);
    free(a), free(b);
}

static CtxAgent *fixture(const char *proj, const char *file) {
    char p[4096], *err = NULL;
    snprintf(p, sizeof p, "%s/%s", proj, file);
    CtxAgent *a = ctx_load("/REPO", p, NULL, NULL, &err);
    if (!a) printf("FAIL load %s: %s\n", p, err ? err : "?"), fails++;
    free(err);
    return a;
}

static void join(const ClRun *r, int n, char *b, size_t cap) {
    size_t o = 0;
    b[0] = 0;
    for (int i = 0; i < n; i++) o += (size_t)snprintf(b + o, cap - o, "%s", r[i].s);
}

/* The fixture's main agent s1 (8 calls): its items' enter calls and links are in tests/expected/context.ndjson. */
static void on_fixture(const char *proj) {
    CtxAgent *a = fixture(proj, "s1.jsonl");
    if (!a) return;
    int n = a->nitems, *by = malloc(sizeof *by * (size_t)n), *shown = malloc(sizeof *shown * (size_t)n);
    unsigned char *linked = calloc((size_t)n, 1);
    signed char *ov = malloc((size_t)n);
    bool *open = malloc((size_t)n);
    memset(ov, -1, (size_t)n);
    /* call 1, nothing selected: what entered at 0 and 1 and the call's response open, system and tools never */
    ctx_positions(a, 1, by);
    int ns = cl_shown(a, 1, by, shown), nl = cl_linked(a, -1, 1, linked);
    CHECK(nl == 0);
    CHECK(ns == a->calls[1].norder + 2); /* its response's text and Edit (made at 1, in no order yet) */
    cl_open(a, 1, shown, ns, ov, linked, nl, open);
    for (int k = 0; k < ns; k++) {
        const CtxItem *it = &a->items[shown[k]];
        bool want = it->kind != CK_SYSTEM && it->kind != CK_TOOLS && (it->made == 1 || (it->enter >= 0 && it->enter <= 1));
        if (open[k] != want) printf("  call 1: item %d open %d, want %d\n", shown[k], open[k], want);
        CHECK(open[k] == want);
    }
    /* call 4: only what entered at 3 and 4 */
    ctx_positions(a, 4, by);
    ns = cl_shown(a, 4, by, shown);
    cl_open(a, 4, shown, ns, ov, linked, 0, open);
    for (int k = 0; k < ns; k++) {
        const CtxItem *it = &a->items[shown[k]];
        CHECK(open[k] == (it->kind != CK_SYSTEM && it->kind != CK_TOOLS && (it->made == 4 || it->enter >= 3)));
    }
    /* the user's choice wins: item 1 (the prompt, entered at 0) open, item 19 (a prompt entered at 4) folded */
    ov[1] = 1, ov[19] = 0;
    cl_open(a, 4, shown, ns, ov, linked, 0, open);
    for (int k = 0; k < ns; k++) {
        if (shown[k] == 1) CHECK(open[k]);
        if (shown[k] == 19) CHECK(!open[k]);
    }
    memset(ov, -1, (size_t)n);
    /* an action selected (item 12, Edit made by call 1): its causes and itself open, the rest only the response */
    ctx_positions(a, 1, by);
    ns = cl_shown(a, 1, by, shown);
    nl = cl_linked(a, 12, 1, linked);
    CHECK(nl == a->items[12].nlk + 1 && linked[12]);
    cl_open(a, 1, shown, ns, ov, linked, nl, open);
    for (int k = 0; k < ns; k++) CHECK(open[k] == (linked[shown[k]] || a->items[shown[k]].made == 1));
    CHECK(cl_linked(a, 12, 2, linked) == 0); /* another call: nothing linked */
    /* ranks: the response first, then the linked, then the newest */
    nl = cl_linked(a, 12, 1, linked);
    LWin *w = calloc((size_t)(ns > 0 ? ns : 1), sizeof *w);
    for (int k = 0; k < ns; k++) cl_size(&a->items[shown[k]], true, &w[k].w, &w[k].h);
    cl_targets(a, 1, shown, ns, linked, w);
    for (int k = 0; k < ns; k++)
        for (int j = 0; j < ns; j++) {
            const CtxItem *p = &a->items[shown[k]], *q = &a->items[shown[j]];
            if (p->made == 1 && q->made != 1) CHECK(w[k].rank < w[j].rank);
            if (p->made != 1 && q->made != 1 && linked[shown[k]] && !linked[shown[j]]) CHECK(w[k].rank < w[j].rank);
            if (w[k].rank < w[j].rank) CHECK(w[k].tr - w[k].h / 4 <= w[j].tr - w[j].h / 4); /* rings grow with rank */
            if (p->made != 1 && q->made != 1 && linked[shown[k]] == linked[shown[j]] && p->enter > q->enter)
                CHECK(w[k].rank < w[j].rank); /* newest first */
        }
    /* each ring holds the area of everything ranked before it, plus a quarter of its height */
    float area = 0;
    for (int r = 0; r < ns; r++)
        for (int k = 0; k < ns; k++) {
            if (w[k].rank != r) continue;
            float ar = (w[k].w + 14) * (w[k].h + 14), want = sqrtf(92 * 92 + (area + ar / 2) / ((float)M_PI * 0.4f)) + w[k].h / 4;
            CHECK(fabsf(w[k].tr - want) < 0.01f);
            area += ar;
        }
    free(w);
    /* the header's words: a Read that went stale, at call 4; this call's response at call 1 */
    char b[1024];
    ClFacts f;
    ClRun r[CL_RUNS];
    ctx_positions(a, 4, by);
    cl_facts(a, 8, 4, by, &f);
    CHECK(f.in_order && f.enter == 1 && f.age_calls == 3 && f.priced);
    int p8 = by[8];
    CHECK(f.age_s > 0 && fabs(f.age_s - (a->calls[4].t - a->calls[1].t)) < 1e-6);
    CHECK(fabs(f.share - ctx_tokens(a, 4, p8) / a->calls[4].total) < 1e-12 && f.share > 0);
    CHECK(f.cost == a->calls[4].cum[p8] && f.cost > 0);
    CHECK(f.state == a->calls[4].state[p8]);
    CHECK(f.nstale == 2 && f.known); /* its own Edit and command, both still in the context at call 4 */
    join(r, cl_meta(a, &f, r), b, sizeof b);
    CHECK(strstr(b, " tok \xC2\xB7 ") && strstr(b, " \xC2\xB7 in #1 ") && strstr(b, " \xC2\xB7 age 3 calls \xC2\xB7 ") &&
          strstr(b, " \xC2\xB7 cache ") && strstr(b, " carried"));
    CHECK(!f.nstale || strstr(b, f.known ? "stale \xC2\xB7 known" : "stale \xE2\x9A\xA0"));
    static const char *const sw[] = {"cache read", "cache written", "cache new"};
    for (int c = 0; c < a->ncalls; c++) { /* each state's word, at every call */
        ctx_positions(a, c, by);
        for (int i = 0; i < n; i++) {
            if (by[i] < 0) continue;
            cl_facts(a, i, c, by, &f);
            join(r, cl_meta(a, &f, r), b, sizeof b);
            if (f.state <= CS_NEW) CHECK(strstr(b, sw[f.state]) != NULL);
            CHECK(f.dropped == (c == 6 && i == 23));
        }
    }
    ctx_positions(a, 6, by); /* call 6 drops thinking item 23 (thinking_drop) */
    cl_facts(a, 23, 6, by, &f);
    join(r, cl_meta(a, &f, r), b, sizeof b);
    CHECK(f.dropped && f.tok == 0 && strstr(b, " \xC2\xB7 dropped by the API at this call"));
    /* the stale changes' title, a line each */
    cl_stale_text(a, 8, 2, b, sizeof b);
    char t1[16], t2[16], want[512];
    fmt_clock(t1, sizeof t1, a->items[8].st[0].t, "%H:%M"), fmt_clock(t2, sizeof t2, a->items[8].st[1].t, "%H:%M");
    snprintf(want, sizeof want, "%s %s (its own edit)\n%s %s (its own command)", t1, a->items[8].st[0].label, t2,
             a->items[8].st[1].label);
    CHECK(!strcmp(b, want));
    cl_stale_text(a, 8, 1, b, sizeof b);
    CHECK(!strchr(b, '\n'));
    CtxStale st[3] = {{a->items[8].st[0].t, CST_SESSION, -1, "x"}, {a->items[8].st[0].t, CST_PROJECT, -1, "y"},
                      {a->items[8].st[0].t, CST_NOTICED, -1, "z"}};
    CtxStale *keep = a->items[8].st;
    a->items[8].st = st, a->items[8].nst = 3;
    cl_stale_text(a, 8, 3, b, sizeof b);
    snprintf(want, sizeof want, "%s x (another agent of this session)\n%s y (another session)\n%s z (the user or a program "
             "(Claude Code noticed))", t1, t1, t1);
    CHECK(!strcmp(b, want));
    a->items[8].st = keep, a->items[8].nst = 2;
    ctx_positions(a, 1, by);
    cl_facts(a, 11, 1, by, &f);
    CHECK(!f.in_order && f.enter == -1 && f.tok == a->items[11].w);
    join(r, cl_meta(a, &f, r), b, sizeof b);
    CHECK(strstr(b, " tok out \xC2\xB7 this call's response") && !strstr(b, "cache"));
    /* the compaction's call: it starts over against the call before */
    int in, out;
    cl_inout(a, 5, &in, &out);
    CHECK(in > 0 && out > 0);
    for (int c = 1; c < a->ncalls; c++) { /* against sets, as the draft counts them */
        int b0 = a->calls[c].base >= 0 ? a->calls[c].base : c - 1, wi = 0, wo = 0;
        memset(linked, 0, (size_t)n);
        for (int p = 0; p < a->calls[b0].norder; p++) linked[a->calls[b0].order[p]] |= 1;
        for (int p = 0; p < a->calls[c].norder; p++) linked[a->calls[c].order[p]] |= 2;
        for (int i = 0; i < n; i++) wi += linked[i] == 2, wo += linked[i] == 1;
        cl_inout(a, c, &in, &out);
        CHECK(in == wi && out == wo);
    }
    double kinds[CK_COUNT], sum = 0;
    cl_by_kind(a, 4, kinds);
    for (int k = 0; k < CK_COUNT; k++) sum += kinds[k];
    CHECK(fabs(sum - a->calls[4].total) < 1e-6 * a->calls[4].total + 1e-6);
    /* actions in call order, the filter any case */
    int *acts = malloc(sizeof *acts * (size_t)n), na = cl_actions(a, acts);
    for (int k = 1; k < na; k++) CHECK(a->items[acts[k - 1]].made <= a->items[acts[k]].made);
    CHECK(na == 9); /* items 7, 12, 14, 24, 25, 26, 32, 34, 35 */
    CHECK(cl_match(&a->items[12], "edit SRC") && !cl_match(&a->items[12], "Grep") && cl_match(&a->items[12], ""));
    /* the call at a time */
    CHECK(cl_call_at(a, 0) == a->ncalls - 1);
    CHECK(cl_call_at(a, a->calls[3].t) == 3 && cl_call_at(a, a->calls[3].t + 0.001) == 3);
    CHECK(cl_call_at(a, a->calls[0].t - 100) == CL_NO_CALL); /* before its first call: none yet */
    free(acts), free(by), free(shown), free(linked), free(ov), free(open);
    ctx_free(a);
}

/* +in −out against the base call when there is one, not the call before. */
static void inout_base(void) {
    int o0[] = {0, 1, 2}, o1[] = {0, 3}, o2[] = {0, 1, 4};
    CtxCall calls[3] = {{.order = o0, .norder = 3, .base = -1}, {.order = o1, .norder = 2, .base = -1},
                        {.order = o2, .norder = 3, .base = 0}};
    CtxItem it[5];
    memset(it, 0, sizeof it);
    CtxAgent a = {.calls = calls, .ncalls = 3, .items = it, .nitems = 5};
    int in, out;
    cl_inout(&a, 2, &in, &out); /* against call 0: +4 −2 */
    CHECK(in == 1 && out == 1);
    cl_inout(&a, 1, &in, &out); /* no base: against call 0, +3 −1 −2 */
    CHECK(in == 1 && out == 2);
    cl_inout(&a, 0, &in, &out);
    CHECK(in == 3 && out == 0);
}

/* An item twice in a call's order (it shouldn't be, but a window is one per item): shown once, at its first place. */
static void dups(void) {
    int o[] = {3, 1, 3, 0}, made[] = {2, 4};
    CtxCall calls[1] = {{.order = o, .norder = 4, .made = made, .nmade = 2}};
    CtxItem it[5];
    memset(it, 0, sizeof it);
    CtxAgent a = {.calls = calls, .ncalls = 1, .items = it, .nitems = 5};
    int by[5], out[5];
    ctx_positions(&a, 0, by);
    int n = cl_shown(&a, 0, by, out);
    CHECK(n == 5 && out[0] == 3 && out[1] == 1 && out[2] == 0 && out[3] == 2 && out[4] == 4);
}

/* A reload's key map on made-up agents whose items moved and went: the
 * user's choices follow the keys, never the indices; a selection that went is
 * gone. */
static void keys(void) {
    CtxItem o[5], n[6];
    memset(o, 0, sizeof o), memset(n, 0, sizeof n);
    uint64_t ok[5] = {10, 20, 30, 40, 50}, nk[6] = {30, 99, 10, 50, 77, 20};
    for (int i = 0; i < 5; i++) o[i].key = ok[i];
    for (int j = 0; j < 6; j++) n[j].key = nk[j];
    CtxAgent A = {.items = o, .nitems = 5}, B = {.items = n, .nitems = 6};
    int map[5];
    cl_key_map(&A, &B, map);
    int want[5] = {2, 5, 0, -1, 3}, moved = 0;
    for (int i = 0; i < 5; i++) { CHECK(map[i] == want[i]); moved += map[i] != i; }
    CHECK(moved == 5);
    signed char ov0[5] = {1, 0, -1, 1, -1}, ov1[6];
    float pin0[10], size0[10], pin1[12], size1[12];
    for (int i = 0; i < 10; i++) pin0[i] = (float)(100 + i), size0[i] = (float)(200 + i);
    memset(ov1, -1, sizeof ov1);
    for (int j = 0; j < 12; j++) pin1[j] = NAN, size1[j] = 0;
    ClUser from = {ov0, pin0, size0, 3}, to = {ov1, pin1, size1, 7};
    cl_carry(map, 5, &from, &to);
    CHECK(to.sel == -1); /* item 3 (key 40) went */
    CHECK(ov1[2] == 1 && ov1[5] == 0 && ov1[0] == -1 && ov1[3] == -1 && ov1[1] == -1 && ov1[4] == -1);
    CHECK(pin1[4] == 100 && pin1[5] == 101 && pin1[10] == 102 && pin1[11] == 103 && pin1[6] == 108 && pin1[7] == 109 &&
          pin1[0] == 104 && pin1[1] == 105);
    CHECK(size1[4] == 200 && size1[5] == 201 && size1[0] == 204 && size1[1] == 205 && size1[6] == 208 && size1[7] == 209);
    CHECK(isnan(pin1[2]) && isnan(pin1[3]) && isnan(pin1[8]) && isnan(pin1[9]) && size1[2] == 0 && size1[8] == 0);
    from.sel = 1;
    cl_carry(map, 5, &from, &to);
    CHECK(to.sel == 5);
    from.sel = 5;
    cl_carry(map, 5, &from, &to);
    CHECK(to.sel == -1); /* not one of old's */
    from.sel = -1;
    cl_carry(map, 5, &from, &to);
    CHECK(to.sel == -1);
}

/* The stage's clock: followed (not while playing), else the call stays; calls out of time order. */
static void follow(void) {
    CtxCall calls[5] = {{.t = 100}, {.t = 110}, {.t = 105}, {.t = 130}, {.t = 140}}; /* call 2 recorded out of order */
    CtxAgent a = {.calls = calls, .ncalls = 5};
    CHECK(cl_call_at(&a, 99) == CL_NO_CALL);
    CHECK(cl_call_at(&a, 100) == 0 && cl_call_at(&a, 107) == 0); /* 105 is after 110 in the run: not yet */
    CHECK(cl_call_at(&a, 110) == 2 && cl_call_at(&a, 129) == 2 && cl_call_at(&a, 130) == 3);
    CHECK(cl_call_at(&a, 1e12) == 4 && cl_call_at(&a, 0) == 4);
    CHECK(cl_follow(&a, true, false, 131, 0) == 3);
    CHECK(cl_follow(&a, true, false, 50, 4) == CL_NO_CALL);
    CHECK(cl_follow(&a, false, false, 131, 1) == 1); /* detached: its own call */
    CHECK(cl_follow(&a, true, true, 131, 1) == 1);  /* playing: its own clock */
    CtxAgent none = {0};
    CHECK(cl_call_at(&none, 10) == CL_NO_CALL);
}

/* The worker's jobs: opens and finished jobs as the screen meets them. */
static void jobs(void) {
    const char *A = "/p/a.jsonl", *B = "/p/b.jsonl";
    /* nothing loaded, nothing running: load */
    CHECK(cl_open_job(&(ClWorker){0}, A) == CL_OPEN_LOAD);
    /* A loading: opening A again (a reopen) keeps it; B queues behind it */
    CHECK(cl_open_job(&(ClWorker){NULL, A, false, false}, A) == CL_OPEN_KEEP);
    CHECK(cl_open_job(&(ClWorker){NULL, A, false, false}, B) == CL_OPEN_QUEUE);
    /* A loading but cancelled (B was asked for): opening A again loads it once the job is out */
    CHECK(cl_open_job(&(ClWorker){NULL, A, false, true}, A) == CL_OPEN_QUEUE);
    /* A shown: reopening it loads nothing, reloading or not; while B loads, B is dropped */
    CHECK(cl_open_job(&(ClWorker){A, NULL, false, false}, A) == CL_OPEN_SHOWN);
    CHECK(cl_open_job(&(ClWorker){A, A, true, false}, A) == CL_OPEN_SHOWN);
    CHECK(cl_open_job(&(ClWorker){A, B, false, false}, A) == CL_OPEN_SHOWN_DROP);
    /* a switch during a reload: the reload is cancelled, B queued, the reload dropped when it's out */
    CHECK(cl_open_job(&(ClWorker){A, A, true, false}, B) == CL_OPEN_QUEUE);
    CHECK(cl_job_done(&(ClDone){.reload = true, .cancelled = true, .newer = true, .ok = true}) == CL_JOB_DROP);
    /* a close during a reload cancels nothing: on reopen the reload is in, kept */
    CHECK(cl_job_done(&(ClDone){.reload = true, .ok = true}) == CL_JOB_KEEP);
    /* a reload cancelled with nothing newer (the screen freed): retried, not lost */
    CHECK(cl_job_done(&(ClDone){.reload = true, .cancelled = true}) == CL_JOB_RETRY);
    CHECK(cl_job_done(&(ClDone){.reload = true, .cancelled = true, .ok = true}) == CL_JOB_RETRY);
    /* a cancelled load: dropped */
    CHECK(cl_job_done(&(ClDone){.cancelled = true, .ok = true}) == CL_JOB_DROP);
    /* loads: a new agent, the same one again (kept), failures */
    CHECK(cl_job_done(&(ClDone){.ok = true}) == CL_JOB_INSTALL);
    CHECK(cl_job_done(&(ClDone){.ok = true, .same = true}) == CL_JOB_KEEP);
    CHECK(cl_job_done(&(ClDone){0}) == CL_JOB_FAIL);
    CHECK(cl_job_done(&(ClDone){.reload = true}) == CL_JOB_RELOAD_FAIL);
    CHECK(cl_job_done(&(ClDone){.newer = true, .ok = true}) == CL_JOB_DROP);
}

/* A busy call: 15 items enter at once, ten of them stay open, the newest. */
static void cap(void) {
    CtxAgent a = {0};
    CtxItem it[16];
    memset(it, 0, sizeof it);
    for (int i = 0; i < 16; i++) it[i] = (CtxItem){.kind = CK_RESULT, .enter = i < 3 ? 1 : 2, .made = -1, .src = "x"};
    it[15].made = 2, it[15].kind = CK_TEXT, it[15].enter = -1; /* the call's response */
    a.items = it, a.nitems = 16;
    int shown[16];
    unsigned char linked[16] = {0};
    signed char ov[16];
    bool open[16];
    memset(ov, -1, sizeof ov);
    for (int i = 0; i < 16; i++) shown[i] = i;
    cl_open(&a, 2, shown, 16, ov, linked, 0, open);
    int n = 0;
    for (int i = 0; i < 16; i++) n += open[i];
    CHECK(n == CL_OPEN_MAX);
    CHECK(open[15]);                            /* the response first */
    for (int i = 0; i < 3; i++) CHECK(!open[i]); /* the older ones folded */
    ov[0] = 1;                                  /* opened by hand: on top of the ten */
    linked[1] = 1;                              /* linked: too */
    cl_open(&a, 2, shown, 16, ov, linked, 1, open);
    CHECK(open[0] && open[1]);
    n = 0;
    for (int i = 0; i < 16; i++) n += open[i];
    CHECK(n == 3); /* an action selected: only its causes, the response and the user's */
    /* exactly ten default-open: all stay; eleven: one folds, the oldest; the linked and the user's don't count */
    for (int extra = 0; extra < 2; extra++) {
        for (int i = 0; i < 16; i++) it[i] = (CtxItem){.kind = CK_RESULT, .enter = i < 5 ? 1 : 2, .made = -1, .src = "x"};
        int m = 10 + extra;
        memset(ov, -1, sizeof ov), memset(linked, 0, sizeof linked);
        for (int i = 0; i < m; i++) shown[i] = i + 5 - extra; /* items 5-extra .. 14 */
        cl_open(&a, 2, shown, m, ov, linked, 0, open);
        n = 0;
        for (int i = 0; i < m; i++) n += open[i];
        CHECK(n == 10);
        if (extra) CHECK(!open[0]); /* item 4: entered at 1, the oldest */
        /* plus a user-opened and a linked one: still ten of the others */
        shown[m] = 0, shown[m + 1] = 1, ov[0] = 1, linked[1] = 1;
        cl_open(&a, 2, shown, m + 2, ov, linked, 1, open);
        n = 0;
        for (int i = 0; i < m + 2; i++) n += open[i];
        CHECK(open[m] && open[m + 1]);
        CHECK(n == 2); /* an action selected: the default ones close */
        cl_open(&a, 2, shown, m + 1, ov, linked, 0, open); /* the user's one, nothing selected */
        n = 0;
        for (int i = 0; i < m; i++) n += open[i];
        CHECK(n == 10 && open[m]);
    }
    /* an action selected: ten windows of the call's response and a linked one, all open (the linked doesn't count) */
    for (int i = 0; i < 16; i++) it[i] = (CtxItem){.kind = CK_TEXT, .enter = -1, .made = 2, .src = "x"};
    it[10] = (CtxItem){.kind = CK_RESULT, .enter = 1, .made = -1, .src = "x"};
    memset(ov, -1, sizeof ov), memset(linked, 0, sizeof linked);
    linked[10] = 1;
    for (int i = 0; i < 11; i++) shown[i] = i;
    cl_open(&a, 2, shown, 11, ov, linked, 1, open);
    n = 0;
    for (int i = 0; i < 11; i++) n += open[i];
    CHECK(n == 11);
    float w, h;
    it[3] = (CtxItem){.kind = CK_RESULT, .enter = 2, .made = -1, .src = "x"};
    cl_size(&it[3], false, &w, &h);
    CHECK(h == CL_CHIP_H && w == 64 + 6.6f + 44);
    it[3].tool = "Read";
    cl_size(&it[3], true, &w, &h);
    CHECK(w == 460 && h == 270);
    it[3].kind = CK_THINKING, cl_size(&it[3], true, &w, &h);
    CHECK(w == 340 && h == 150);
    it[3].kind = CK_PROMPT, cl_size(&it[3], true, &w, &h);
    CHECK(w == 380 && h == 210);
    it[3].kind = CK_INJECTED, cl_size(&it[3], true, &w, &h);
    CHECK(w == 360 && h == 180);
    char t[64];
    it[3] = (CtxItem){.kind = CK_RESULT, .err = 1, .src = "Bash make"};
    cl_title(&it[3], t, sizeof t);
    CHECK(!strcmp(t, "error \xC2\xB7 Bash make"));
    it[3].kind = CK_ACTION, cl_title(&it[3], t, sizeof t);
    CHECK(!strcmp(t, "Bash make"));
}

/* Live (C3): the fixture's s1 grows (s1-append.jsonl) and is reloaded; a
 * window dragged somewhere, one resized, one folded and the selected action
 * carry over to the same items (by key); the new items have none of it. */
static void reload(const char *proj, const char *tmp) {
    char cmd[4096], s1[2048], extra[2048];
    snprintf(cmd, sizeof cmd, "rm -rf '%s/live' && mkdir -p '%s/live' && cp -R '%s/.' '%s/live/'", tmp, tmp, proj, tmp);
    CHECK(!system(cmd));
    snprintf(s1, sizeof s1, "%s/live/s1.jsonl", tmp);
    snprintf(extra, sizeof extra, "%s/../s1-append.jsonl", proj);
    char *err = NULL;
    CtxProject *pr = ctx_project_new();
    CtxAgent *a = ctx_load("/REPO", s1, pr, NULL, &err);
    CHECK(a != NULL);
    if (!a) { free(err); ctx_project_free(pr); return; }
    int n = a->nitems;
    ClUser u = {malloc((size_t)n), malloc(sizeof(float) * 2 * (size_t)n), calloc(2 * (size_t)n, sizeof(float)), 12};
    memset(u.ov, -1, (size_t)n);
    for (int i = 0; i < 2 * n; i++) u.pin[i] = NAN;
    u.pin[16] = 123, u.pin[17] = -45;  /* item 8 (a Read's result) dragged */
    u.size[14] = 500, u.size[15] = 300; /* item 7 resized */
    u.ov[1] = 0;                        /* the prompt folded */
    FILE *in = fopen(extra, "r"), *out = fopen(s1, "a");
    for (int ch; in && out && (ch = fgetc(in)) != EOF;) fputc(ch, out);
    if (in) fclose(in);
    if (out) fclose(out);
    CtxAgent *b = ctx_reload(a, pr, NULL, &err);
    CHECK(b && b->ncalls > a->ncalls && b->nitems > a->nitems);
    if (b) {
        int *map = malloc(sizeof *map * (size_t)n), m = b->nitems;
        cl_key_map(a, b, map);
        ClUser v = {malloc((size_t)m), malloc(sizeof(float) * 2 * (size_t)m), calloc(2 * (size_t)m, sizeof(float)), -1};
        memset(v.ov, -1, (size_t)m);
        for (int i = 0; i < 2 * m; i++) v.pin[i] = NAN;
        cl_carry(map, n, &u, &v);
        for (int i = 0; i < n; i++) /* the same items, by key */
            if (map[i] >= 0) CHECK(a->items[i].kind == b->items[map[i]].kind && !strcmp(a->items[i].src, b->items[map[i]].src));
        CHECK(map[8] >= 0 && map[7] >= 0 && map[1] >= 0 && map[12] >= 0);
        if (map[8] >= 0) CHECK(v.pin[2 * map[8]] == 123 && v.pin[2 * map[8] + 1] == -45);
        if (map[7] >= 0) CHECK(v.size[2 * map[7]] == 500 && v.size[2 * map[7] + 1] == 300);
        if (map[1] >= 0) CHECK(v.ov[map[1]] == 0);
        CHECK(v.sel == map[12] && b->items[v.sel].kind == CK_ACTION && b->items[v.sel].made == a->items[12].made);
        int carried = 0, fresh = 0;
        for (int j = 0; j < m; j++) {
            carried += !isnan(v.pin[2 * j]) || v.size[2 * j] > 0 || v.ov[j] >= 0;
            bool old = false;
            for (int i = 0; i < n; i++) old |= map[i] == j;
            fresh += !old;
        }
        CHECK(carried == 3 && fresh > 0);
        u.sel = -1;
        cl_carry(map, n, &u, &v);
        CHECK(v.sel == -1);
        free(map), free(v.ov), free(v.pin), free(v.size);
    }
    free(err);
    free(u.ov), free(u.pin), free(u.size);
    ctx_free(a), ctx_free(b);
    ctx_project_free(pr);
}

int main(int argc, char **argv) {
    words();
    strip();
    cap();
    inout_base();
    dups();
    keys();
    follow();
    jobs();
    layout(40);
    layout(400);
    layout(1100);
    if (argc > 1) on_fixture(argv[1]);
    if (argc > 2) reload(argv[1], argv[2]);
    printf(fails ? "clay: %d FAILED\n" : "ok   clay (open rule and cap, ranks, strip, words, the clock, the worker's jobs, a reload keeping the user's "
                   "choices by key, layout of 40/400/1100 windows)\n", fails);
    return fails != 0;
}
