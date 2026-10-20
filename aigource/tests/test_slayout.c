/* Unit test for the structure screen's geometry (slayout.c): the treemap and
 * partition against d3 7.9.0's own numbers on a small tree (the draft's
 * settings), nesting and no overlap on random trees, and the floating nodes'
 * simulation: deterministic, settled apart, inside the box. */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "slayout.h"

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

/* Nodes as metrics.c stores them: breadth-first, children by value. */
typedef struct { StNode *n; int count; } Tree;

static int node(Tree *t, int parent, int own) {
    int i = t->count++;
    t->n[i] = (StNode){.parent = parent, .own = own, .value = own};
    if (parent >= 0) {
        StNode *p = &t->n[parent];
        p->kids = realloc(p->kids, sizeof *p->kids * (size_t)(p->nkids + 1));
        p->kids[p->nkids++] = i;
    }
    return i;
}

static void sum(Tree *t, int i) {
    for (int k = 0; k < t->n[i].nkids; k++) {
        sum(t, t->n[i].kids[k]);
        t->n[i].value += t->n[t->n[i].kids[k]].value;
    }
}

static void free_tree(Tree *t) {
    for (int i = 0; i < t->count; i++) free(t->n[i].kids);
    free(t->n);
}

/* r: A(a1 30, a2 10, a3 5), B 40, C(own 6; c1 8, c2 4), E 2, D 0 */
static void d3_parity(void) {
    Tree t = {calloc(16, sizeof(StNode)), 0};
    int r = node(&t, -1, 0), A = node(&t, r, 0), B = node(&t, r, 40), C = node(&t, r, 6), E = node(&t, r, 2),
        D = node(&t, r, 0);
    int a1 = node(&t, A, 30), a2 = node(&t, A, 10), a3 = node(&t, A, 5), c1 = node(&t, C, 8), c2 = node(&t, C, 4);
    sum(&t, r);
    LRect *q = calloc((size_t)t.count, sizeof *q);
    lay_treemap(t.n, r, 600, 400, LAY_TREEMAP_RATIO, q);
    static const struct { int *i; float x0, y0, x1, y1; } want[] = {
        {0, 0, 0, 600, 400},   {0, 3, 3, 483, 211},     {0, 3, 213, 483, 397},   {0, 485, 3, 597, 357},
        {0, 485, 359, 597, 397}, {0, 598, 359, 598, 397}, {0, 6, 21, 322, 208},   {0, 324, 21, 480, 145},
        {0, 324, 147, 480, 208}, {0, 488, 21, 594, 168},  {0, 488, 170, 594, 243},
    };
    int ids[] = {r, A, B, C, E, D, a1, a2, a3, c1, c2};
    for (int k = 0; k < 11; k++) {
        LRect g = q[ids[k]];
        bool ok = g.x0 == want[k].x0 && g.y0 == want[k].y0 && g.x1 == want[k].x1 && g.y1 == want[k].y1;
        if (!ok) printf("  treemap node %d: %g %g %g %g, d3 %g %g %g %g\n", ids[k], g.x0, g.y0, g.x1, g.y1, want[k].x0,
                        want[k].y0, want[k].x1, want[k].y1);
        CHECK(ok);
    }
    float x0[11], x1[11];
    lay_partition(t.n, r, x0, x1);
    static const float px[][2] = {{0, 1},        {0, 0.428571f},        {0.428571f, 0.809524f}, {0.809524f, 0.980952f},
                                  {0.980952f, 1}, {1, 1},                {0, 0.285714f},         {0.285714f, 0.380952f},
                                  {0.380952f, 0.428571f}, {0.809524f, 0.885714f}, {0.885714f, 0.923810f}};
    for (int k = 0; k < 11; k++) CHECK(fabsf(x0[ids[k]] - px[k][0]) < 1e-5f && fabsf(x1[ids[k]] - px[k][1]) < 1e-5f);
    CHECK(lay_height(t.n, r) == 2 && lay_height(t.n, B) == 0 && lay_height(t.n, C) == 1);
    free(q);
    free_tree(&t);
}

static int cmp_value(const void *a, const void *b, void *nodes) {
    const StNode *n = nodes;
    return n[*(const int *)b].value - n[*(const int *)a].value;
}

/* A random tree, children sorted by value as d3's sort() leaves them. */
static Tree random_tree(unsigned seed, int n) {
    srand(seed);
    Tree t = {calloc((size_t)n, sizeof(StNode)), 0};
    node(&t, -1, 0);
    for (int i = 1; i < n; i++) node(&t, rand() % i, rand() % 4 ? rand() % 300 : 0);
    for (int i = n - 1; i >= 0; i--) t.n[i].value = t.n[i].own;
    sum(&t, 0);
    for (int i = 0; i < n; i++)
        if (t.n[i].nkids) qsort_r(t.n[i].kids, (size_t)t.n[i].nkids, sizeof(int), cmp_value, t.n);
    return t;
}

static bool inside(LRect a, LRect b) { return a.x0 >= b.x0 && a.y0 >= b.y0 && a.x1 <= b.x1 && a.y1 <= b.y1; }
static bool overlap(LRect a, LRect b) { return a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1; }

static void random_trees(void) {
    for (unsigned seed = 1; seed <= 40; seed++) {
        Tree t = random_tree(seed, 20 + (int)seed * 7);
        LRect *q = calloc((size_t)t.count, sizeof *q);
        lay_treemap(t.n, 0, 900, 700, LAY_TREEMAP_RATIO, q);
        float *x0 = calloc((size_t)t.count, sizeof *x0), *x1 = calloc((size_t)t.count, sizeof *x1);
        lay_partition(t.n, 0, x0, x1);
        int bad = 0;
        for (int i = 1; i < t.count; i++) {
            const StNode *d = &t.n[i];
            bad += !inside(q[i], q[d->parent]) || q[i].x1 < q[i].x0 || q[i].y1 < q[i].y0;
            bad += x0[i] < x0[d->parent] - 1e-5f || x1[i] > x1[d->parent] + 1e-5f;
            for (int k = 0; k < d->nkids; k++)
                for (int j = k + 1; j < d->nkids; j++) {
                    bad += overlap(q[d->kids[k]], q[d->kids[j]]);
                    bad += x0[d->kids[j]] < x1[d->kids[k]] - 1e-5f; /* in order, side by side */
                }
        }
        if (bad) printf("  seed %u: %d nesting/overlap problems\n", seed, bad);
        CHECK(!bad);
        free(q);
        free(x0);
        free(x1);
        free_tree(&t);
    }
}

/* nk bodies around a hub: deterministic, apart once settled, inside the box.
 * Past 120 bodies the far ones are grouped on a grid. */
static void sim(int nk, int rmax) {
    float *r = malloc(sizeof *r * (size_t)nk), *pad = malloc(sizeof *pad * (size_t)nk);
    for (int i = 0; i < nk; i++) r[i] = 5 + (float)((i * 37) % rmax), pad[i] = r[i] < 24 ? 15 : 5;
    LSim a, b;
    lay_sim_init(&a, 40, r, pad, nk, 7);
    lay_sim_init(&b, 40, r, pad, nk, 7);
    bool *below = calloc((size_t)nk + 1, 1);
    for (int t = 0; t < 400; t++) {
        lay_sim_tick(&a, t / 60.0, true);
        lay_sim_tick(&b, t / 60.0, true);
        lay_sim_clamp(&a, 1100, 800, below);
        lay_sim_clamp(&b, 1100, 800, below);
    }
    CHECK(!memcmp(a.b, b.b, sizeof *a.b * (size_t)a.n));
    CHECK(a.b[0].x == 0 && a.b[0].y == 0);
    int close = 0, out = 0;
    for (int i = 0; i < a.n; i++) {
        out += fabsf(a.b[i].x) > 550 || fabsf(a.b[i].y) > 400;
        for (int j = i + 1; j < a.n; j++)
            close += hypotf(a.b[i].x - a.b[j].x, a.b[i].y - a.b[j].y) < (a.b[i].r + a.b[j].r) * 0.9f;
    }
    if (close) printf("  sim %d: %d pairs overlap\n", nk, close);
    CHECK(!close);
    CHECK(!out);
    lay_sim_free(&a);
    lay_sim_free(&b);
    free(r), free(pad), free(below);
}

int main(void) {
    d3_parity();
    random_trees();
    sim(30, 40);
    sim(300, 10);
    printf(fails ? "slayout: %d FAILED\n" : "ok   slayout (d3 parity, 40 random trees, simulation of 30 and 300 bodies)\n", fails);
    return fails != 0;
}
