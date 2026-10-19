/* Unit test for the dir layout in model.c: on random trees, once settled, no
 * two dir edges cross, no two dirs' file clouds overlap, and a big tree is no
 * bigger than it was. AIG_LAYOUT_SPREAD=1 prints each tree's size. */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>

#include "model.h"

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

static float cross(Vector2 o, Vector2 a, Vector2 b) { return (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x); }
static float len(Vector2 a, Vector2 b) { return hypotf(a.x - b.x, a.y - b.y); }

/* Proper crossing of segments ab and cd (touching at a shared end, or running
 * along the same line, doesn't count; the margin scales with the segments, as
 * float noise does). */
static bool crosses(Vector2 a, Vector2 b, Vector2 c, Vector2 d) {
    float eps = 1e-4f * len(a, b) * len(c, d);
    float d1 = cross(c, d, a), d2 = cross(c, d, b), d3 = cross(a, b, c), d4 = cross(a, b, d);
    return ((d1 > eps && d2 < -eps) || (d1 < -eps && d2 > eps)) && ((d3 > eps && d4 < -eps) || (d3 < -eps && d4 > eps));
}

/* nfiles files at random depths up to maxdepth, `fanout` dirs to pick from per
 * level; returns how far the settled tree spreads from the root. */
static float tree(unsigned seed, int nfiles, int maxdepth, int fanout) {
    srand(seed);
    Scene *m = model_new("/nonexistent-aigource-root");
    char path[1024];
    for (int i = 0; i < nfiles; i++) {
        int depth = rand() % (maxdepth + 1), n = 0;
        for (int k = 0; k < depth; k++) n += snprintf(path + n, sizeof path - (size_t)n, "d%d/", rand() % fanout);
        snprintf(path + n, sizeof path - (size_t)n, "f%d.c", i);
        Event e = {.ts = 1000 + i, .kind = EV_READ, .actor = "a", .path = path, .lines = rand() % 800};
        model_apply(m, &e, false);
    }
    for (int i = 0; i < 1500; i++) model_update(m, 1.0f / 60, 0);

    Node *N = m->nodes;
    int ndirs = 0, bad_edges = 0, bad_clouds = 0;
    float spread = 0;
    for (int i = 0; i < m->nnodes; i++) {
        if (!N[i].is_dir) continue;
        ndirs++;
        spread = fmaxf(spread, len(N[i].pos, N[0].pos) + N[i].radius);
        for (int j = i + 1; j < m->nnodes; j++) {
            if (!N[j].is_dir) continue;
            if (i) bad_edges += crosses(N[N[i].parent].pos, N[i].pos, N[N[j].parent].pos, N[j].pos);
            if (!dir_passes(&N[i]) && !dir_passes(&N[j])) /* merged into the next: not drawn */
                bad_clouds += len(N[i].pos, N[j].pos) < N[i].radius + N[j].radius - 0.5f;
        }
    }
    if (bad_edges || bad_clouds)
        printf("seed %u: %d dirs, %d crossing edges, %d overlapping clouds\n", seed, ndirs, bad_edges, bad_clouds);
    if (getenv("AIG_LAYOUT_SPREAD")) printf("seed %u: %d dirs spread %.0f\n", seed, ndirs, spread);
    CHECK(bad_edges == 0);
    CHECK(bad_clouds == 0);
    model_free(m);
    return spread;
}

static void add(Scene *m, const char *path) {
    static double ts = 5000;
    Event e = {.ts = ts++, .kind = EV_READ, .actor = "a", .path = (char *)path, .lines = 100};
    model_apply(m, &e, false);
}

/* A chain of dirs with no files of their own shows as one node; a file in
 * the middle splits it. */
static void merged(void) {
    Scene *m = model_new("/nonexistent-aigource-root");
    add(m, "examples/java/src/Main.java");
    add(m, "examples/java/src/Util.java");
    add(m, "lib/x.c");
    for (int i = 0; i < 600; i++) model_update(m, 1.0f / 60, 0);
    Node *ex = &m->nodes[model_find(m, true, "examples")], *java = &m->nodes[model_find(m, true, "examples/java")];
    Node *src = &m->nodes[model_find(m, true, "examples/java/src")];
    CHECK(dir_passes(ex) && dir_passes(java) && !dir_passes(src));
    CHECK(len(ex->pos, src->pos) < 1e-3f && len(java->pos, src->pos) < 1e-3f);
    add(m, "examples/java/pom.xml"); /* java has a file now: src moves out */
    for (int i = 0; i < 600; i++) model_update(m, 1.0f / 60, 0);
    ex = &m->nodes[model_find(m, true, "examples")], java = &m->nodes[model_find(m, true, "examples/java")];
    src = &m->nodes[model_find(m, true, "examples/java/src")];
    CHECK(dir_passes(ex) && !dir_passes(java));
    CHECK(len(ex->pos, java->pos) < 1e-3f && len(java->pos, src->pos) > java->radius + src->radius);
    /* pom.xml deleted: once it has faded, java merges again, src sliding in. */
    Event del = {.ts = 9000, .kind = EV_DELETE, .actor = "a", .path = "examples/java/pom.xml", .lines = -1};
    model_apply(m, &del, false);
    float step = 0;
    for (int i = 0; i < 600; i++) {
        java = &m->nodes[model_find(m, true, "examples/java")];
        src = &m->nodes[model_find(m, true, "examples/java/src")];
        Vector2 was = src->pos;
        model_update(m, 1.0f / 60, 0);
        step = fmaxf(step, len(was, src->pos));
    }
    CHECK(dir_merged(m, java) && len(java->pos, src->pos) < 1e-3f);
    CHECK(step < 15); /* a slide, not a jump */
    model_free(m);
}

int main(void) {
    merged();
    for (unsigned seed = 1; seed <= 20; seed++) tree(seed, 40 + (int)seed * 15, 5, 4);
    tree(101, 400, 2, 40); /* wide: dozens of dirs side by side */
    tree(102, 120, 40, 1); /* deep: a long chain */
    float big = tree(103, 1500, 6, 8);
    CHECK(big < 30000); /* 1729 dirs: it grows with them, but no more than now */
    if (!fails) printf("ok   layout\n");
    return fails != 0;
}
