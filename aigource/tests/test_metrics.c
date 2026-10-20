/* metrics.c on hand-built snapshots: folder chains, sizes and order, coupling
 * (2+ shared turns, never ancestors), rework window, line shares, slow files
 * and turns, turn info; and the edges: zero averages, nodes with no turns,
 * single-turn nodes, an empty tree. With a directory argument, each
 * snapshot and its metrics are written there as JSON (A-data.json,
 * A-metrics.json, ...) for tests/metrics_parity.js. */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "metrics.h"

static int fails, checks;

#define CHECK(cond, ...)                          \
    do {                                          \
        checks++;                                 \
        if (!(cond)) {                            \
            printf("FAIL metrics: " __VA_ARGS__); \
            printf(" (line %d)\n", __LINE__);     \
            fails++;                              \
        }                                         \
    } while (0)

#define BASE 1792000000.0
#define N(a) (int)(sizeof(a) / sizeof *(a))

static AnNode nodes[1024];
static int nnodes;

static int node(int parent, int kind, const char *name, int lines) {
    AnNode *n = &nodes[nnodes];
    *n = (AnNode){.name = (char *)name, .kind = kind, .parent = parent, .lines = lines, .lang = "c"};
    if (parent >= 0) {
        AnNode *p = &nodes[parent];
        p->kids = realloc(p->kids, (size_t)(p->nkids + 1) * sizeof *p->kids);
        p->kids[p->nkids++] = nnodes;
    }
    return nnodes++;
}

static void def(int i, int a, int b, int cx, int depth) {
    nodes[i].a = a, nodes[i].b = b, nodes[i].cx = cx, nodes[i].depth = depth;
}

static void reset(void) {
    for (int i = 0; i < nnodes; i++) free(nodes[i].kids);
    nnodes = 0;
}

/* reset() for a generated snapshot: its arrays are on the heap. */
static void reset_owned(void) {
    for (int i = 0; i < nnodes; i++) free(nodes[i].ch), free(nodes[i].rw), free(nodes[i].tr), free(nodes[i].blame);
    reset();
}

static int find(const StMetrics *m, const char *id) {
    for (int i = 0; i < m->nnodes; i++)
        if (!strcmp(m->nodes[i].id, id)) return i;
    return -1;
}

static bool near(double a, double b) { return fabs(a - b) < 1e-9; }

static void dump(const char *dir, const char *name, const AnSnapshot *s, const StMetrics *m) {
    if (!dir) return;
    char p[4096];
    snprintf(p, sizeof p, "%s/%s-data.json", dir, name);
    FILE *f = fopen(p, "w");
    if (f) an_snapshot_json(f, s), fclose(f);
    snprintf(p, sizeof p, "%s/%s-metrics.json", dir, name);
    if ((f = fopen(p, "w"))) st_json(f, m), fclose(f);
}

/* Two sessions' worth of turns over a/b/x.c (functions f1, class K with
 * method m), a/b/y.c, c/d/w.c and an untouched z.py. Turn indices are out of
 * time order on purpose. */
static void snapshot_a(const char *dir) {
    reset();
    int r = node(-1, AN_DIR, "r", 0);
    int a = node(r, AN_DIR, "a", 0), b = node(a, AN_DIR, "b", 0);
    int x = node(b, AN_FILE, "x.c", 10), y = node(b, AN_FILE, "y.c", 5);
    int f1 = node(x, AN_FUNC, "f1", 4), k = node(x, AN_CLASS, "K", 6), mm = node(k, AN_FUNC, "m", 4);
    int c = node(r, AN_DIR, "c", 0), d = node(c, AN_DIR, "d", 0), w = node(d, AN_FILE, "w.c", 3);
    int z = node(r, AN_FILE, "z.py", 4);
    def(f1, 1, 4, 2, 0), def(k, 5, 10, 10, 0), def(mm, 6, 9, 9, 1);
    nodes[x].cx = 12, nodes[w].cx = 1;
    AnCh xch[] = {{0, 5, 0, 0}, {2, 3, 2, 1}, {4, 2, 1, 0}}, ych[] = {{1, 4, 1, 0}, {3, 1, 1, 0}, {5, 2, 0, 0}, {7, 1, 1, 0}};
    AnCh wch[] = {{6, 3, 0, 0}}, f1ch[] = {{0, 3, 0, 0}, {4, 1, 1, 0}}, kch[] = {{0, 2, 0, 0}, {2, 3, 2, 1}}, mch[] = {{2, 2, 1, 1}};
    AnRw xrw[] = {{0, 2, 2}, {0, 4, 1}}, yrw[] = {{1, 7, 1}, {3, 3, 1}};
    AnTr xtr[] = {{0, 5, 0}, {2, 8, 0}, {4, 10, 0}}, f1tr[] = {{0, 3, 1}, {4, 4, 2}}, ktr[] = {{0, 2, 1}}, mtr[] = {{2, 4, 1}};
    AnRun xbl[] = {{3, 0}, {2, 4}, {3, 2}, {2, -1}}, ybl[] = {{2, 1}, {1, 3}, {1, 5}, {1, 7}}, wbl[] = {{3, 6}}, zbl[] = {{4, -1}};
#define SET(i, f, arr) nodes[i].f = arr, nodes[i].n##f = N(arr)
    SET(x, ch, xch), SET(y, ch, ych), SET(w, ch, wch), SET(f1, ch, f1ch), SET(k, ch, kch), SET(mm, ch, mch);
    SET(x, rw, xrw), SET(y, rw, yrw), SET(x, tr, xtr), SET(f1, tr, f1tr), SET(k, tr, ktr), SET(mm, tr, mtr);
    SET(x, blame, xbl), SET(y, blame, ybl), SET(w, blame, wbl), SET(z, blame, zbl);
    AnTurn turns[] = {
        {.title = "third", .ts = BASE + 3000, .actor = 0, .secs = 100},
        {.title = "first", .ts = BASE + 1000, .actor = 0, .secs = 100, .has_tok = true, .tok = {200, 20, 2}},
        {.title = "second", .ts = BASE + 2000, .actor = 0, .secs = 100, .fix = true, .corr = "no, wrong", .intr = true},
        {.title = "fourth", .ts = BASE + 4000, .actor = 0, .secs = 1000},
    };
    AnRead top0[] = {{0, 2}}, top1[] = {{0, 3}, {2, 1}}, top3[] = {{1, 1}};
    AnChange ch[] = {
        {.turn = 1, .comment = 0, .tool = "Edit", .verify = AN_PASSED, .reads = 2, .searches = 1, .top = top0, .ntop = 1,
         .epoch = (long long)BASE + 1000, .actor = 0, .tok = {100, 10, 1}, .secs = 60},
        {.turn = 1, .comment = 0, .tool = "Edit", .verify = AN_UNTESTED, .reads = 3, .top = top1, .ntop = 2,
         .epoch = (long long)BASE + 1010, .actor = 0, .tok = {40}, .secs = 20},
        {.turn = 2, .comment = -1, .tool = "Write", .verify = AN_FAILING, .epoch = (long long)BASE + 2000, .actor = 1,
         .tok = {10}, .secs = 10},
        {.turn = 2, .comment = 2, .tool = "Edit", .verify = AN_PASSED, .reads = 1, .searches = 1, .top = top3, .ntop = 1,
         .epoch = (long long)BASE + 2005, .actor = 1, .tok = {5}, .secs = 5},
        {.turn = 0, .comment = 1, .tool = "Bash", .verify = AN_PASSED, .epoch = (long long)BASE + 3000, .actor = 0,
         .tok = {7}, .secs = 400},
        {.turn = 0, .comment = 2, .tool = "Edit", .verify = AN_PASSED, .epoch = (long long)BASE + 3001, .actor = 0, .secs = 1},
        {.turn = 0, .comment = 0, .tool = "Edit", .verify = AN_UNTESTED, .epoch = (long long)BASE + 3002, .actor = 0, .secs = 3},
        {.turn = 3, .comment = 2, .tool = "Edit", .verify = AN_PASSED, .epoch = (long long)BASE + 4000, .actor = 0,
         .tok = {1}, .secs = 2},
    };
    char *comments[] = {"first note", "", "second"}, *paths[] = {"a/b/x.c", "a/b/y.c", "other.txt"};
    AnActor actors[] = {{.id = "s0", .name = "session s0"}, {.sub = true, .id = "sub1", .name = "explore", .task = "look"}};
    AnSnapshot s = {.nodes = nodes, .nnodes = nnodes, .turns = turns, .nturns = N(turns), .comments = comments,
                    .ncomments = N(comments), .paths = paths, .npaths = N(paths), .actors = actors, .nactors = N(actors),
                    .changes = ch, .nchanges = N(ch)};
    StMetrics *m = st_build(&s);
    dump(dir, "A", &s, m);

    CHECK(m->order[0] == 1 && m->order[1] == 2 && m->order[2] == 0 && m->order[3] == 3, "ORDER");
    /* chains merged below the root; children by size, breadth-first */
    static const char *ids[] = {"r", "r/a/b", "r/z.py", "r/c/d", "r/a/b/x.c", "r/a/b/y.c", "r/c/d/w.c",
                                "r/a/b/x.c/K", "r/a/b/x.c/f1", "r/a/b/x.c/K/m"};
    CHECK(m->nnodes == N(ids), "nodes: %d", m->nnodes);
    for (int i = 0; i < m->nnodes && i < N(ids); i++) CHECK(!strcmp(m->nodes[i].id, ids[i]), "node %d: %s", i, m->nodes[i].id);
    int X = find(m, "r/a/b/x.c"), Y = find(m, "r/a/b/y.c"), W = find(m, "r/c/d/w.c"), Z = find(m, "r/z.py");
    int F1 = find(m, "r/a/b/x.c/f1"), K = find(m, "r/a/b/x.c/K"), M = find(m, "r/a/b/x.c/K/m"), AB = find(m, "r/a/b");
    CHECK(m->of_an[a] == AB && m->of_an[b] == AB && m->of_an[x] == X, "of_an");
    CHECK(m->nodes[AB].value == 15 && m->nodes[X].own == 0 && m->nodes[K].own == 2 && m->nodes[K].value == 6, "sizes");
    CHECK(m->nodes[AB].lines == 15 && m->nodes[AB].cx == 12, "folder lines %d cx %d", m->nodes[AB].lines, m->nodes[AB].cx);

    /* coupling: x and y share 3 turns; f1 and K only 1; w changed once */
    const StNode *nx = &m->nodes[X], *ny = &m->nodes[Y];
    CHECK(nx->npartners == 1 && nx->partners[0].node == Y && nx->partners[0].shared == 3 && nx->partners[0].pct == 100 &&
              near(nx->partners[0].degree, 1) && !nx->partners[0].cross, "x partners");
    CHECK(ny->npartners == 1 && ny->partners[0].pct == 75, "y partners: pct %d", ny->npartners ? ny->partners[0].pct : -1);
    CHECK(!m->nodes[F1].npartners && !m->nodes[K].npartners, "partners need 2 shared turns");
    CHECK(m->nodes[W].turns == 1 && !m->nodes[W].npartners && !m->nodes[M].npartners, "single-turn nodes have none");

    /* rework: gaps by time order, not turn index */
    CHECK(nx->rework == 3 && nx->rework_same == 0 && nx->rework_pct == 30, "x rework %d", nx->rework);
    CHECK(ny->rework == 1 && ny->rework_same == 1, "y rework: out of the window, same turn");
    CHECK(nx->nrw_pairs == 2 && nx->rw_pairs[0].to == 1 && nx->rw_pairs[0].by == 2 && nx->rw_pairs[0].n == 2, "x pairs");

    /* rows newest first, fixes, flags */
    CHECK(nx->nrows == 3 && nx->rows[0].turn == 0 && nx->rows[1].turn == 2 && nx->rows[2].turn == 1, "x rows");
    CHECK(nx->rows[1].flags == (STT_FAILING | STT_BLIND) && nx->rows[1].nnotes == 1 && nx->rows[1].notes[0] == -1, "x row t2");
    CHECK(nx->rows[0].nnotes == 0 && nx->rows[0].ntools == 1 && !strcmp(nx->rows[0].tools[0], "Bash"), "empty comment");
    CHECK(nx->fix_turns == 1 && nx->flags.corrected == 1 && nx->flags.interrupted == 1 && nx->flags.blind == 1, "x flags");
    CHECK(nx->kinds == (STK_CORR | STK_TEST | STK_BLIND | STK_SLOW), "x kinds %d", nx->kinds);

    /* context: first change per turn */
    CHECK(near(nx->ctx_avg, 1) && nx->nctx_rows == 1 && nx->ctx_rows[0].node == X, "x context");
    CHECK(near(ny->ctx_avg, 1.25) && ny->nctx_rows == 3 && ny->ctx_rows[0].n == 3 && ny->ctx_rows[1].node == -1 &&
              ny->ctx_rows[2].node == Y, "y context");

    /* cost by line share; slow by time per turn */
    const StNode *nf = &m->nodes[F1];
    CHECK(nf->tok[0] == 65 && nf->tok[3] == 303, "f1 shares: %lld %lld", nf->tok[0], nf->tok[3]);
    CHECK(nx->tok[3] == 470 && near(m->avg_time[ST_LV_FILE], (470 / 3.0 + 7 + 3) / 3), "file avg %g", m->avg_time[1]);
    CHECK(nx->flags.slow == 1 && !ny->flags.slow && m->nodes[0].flags.slow == 1 && !nf->flags.slow, "slow");

    /* no turns: z.py */
    const StNode *nz = &m->nodes[Z];
    CHECK(!nz->turns && !nz->nrows && nz->per_turn == 0 && nz->owner == -1 && nz->age_median == -1 &&
              nz->ages[ST_AGE_UNKNOWN] == 4 && nz->heat[ST_TURNS] == 0 && nz->heat[ST_AGE] == 0, "untouched node");

    /* trend and growing */
    CHECK(nx->ntrend == 3 && !nx->growing, "x trend");
    CHECK(m->nodes[K].ntrend == 2 && m->nodes[K].trend[1].cid == -1 && m->nodes[K].trend[1].lines == 6, "K now point");
    CHECK(m->nodes[M].growing, "m growing by complexity");

    /* ownership, colours, blame legend */
    CHECK(m->nactors == 2 && m->actors[0] == 0 && st_actor_color(m, 1) == 1 && st_actor_color(m, -1) == -1, "actors");
    CHECK(nx->owner == 0 && nx->nwho == 3 && nx->who[2].actor == -1, "x who");
    CHECK(nx->nblame == 4 && nx->blame[0].turn == 0 && nx->blame[3].turn == -1, "x legend");

    /* turns */
    /* the API P4 draws from */
    CHECK(st_find(m, "r/a/b/x.c/K") == K && st_find(m, "a/b/x.c") == X && st_find(m, "nope") == -1, "st_find");
    CHECK(m->nodes[M].a == 6 && m->nodes[M].b == 9 && m->nodes[M].file == X && !strcmp(m->nodes[M].lang, "c") &&
              m->nodes[AB].file == -1 && !m->nodes[AB].lang, "def range, file, lang");
    int no, *o = st_line_origins(m, K, &no);
    CHECK(no == 6 && o && o[0] == 4 && o[1] == 2 && o[3] == 2 && o[4] == -1 && o[5] == -1, "K origins");
    free(o);
    o = st_line_origins(m, AB, &no);
    CHECK(!o && !no, "no origins for a folder");
    CHECK(st_blame_slot(nx, 0) == 0 && st_blame_slot(nx, 1) == 2 && st_blame_slot(nx, -1) == -1 && st_blame_slot(nx, 3) == -1,
          "blame slots");
    CHECK(nx->owner_slot == 0 && m->nodes[Z].owner_slot == -1, "owner slots");
    CHECK(nx->ncorr_turns == 1 && nx->corr_turns[0] == 2 && !m->nodes[W].ncorr_turns, "corrected turns");
    CHECK(st_turn_touches(&m->turns[1], K) && st_turn_touches(&m->turns[1], 0) && !st_turn_touches(&m->turns[1], W) &&
              !st_turn_touches(&m->turns[3], X), "turn touches");
    const StTurn *t1 = &m->turns[1], *t2 = &m->turns[2], *t0 = &m->turns[0], *t3 = &m->turns[3];
    CHECK(t1->ncids == 2 && t1->nfiles == 2 && t1->files[0].node == X && t1->ndefs == 2 && t1->defs[0].node == F1, "t1 touched");
    CHECK(t1->add == 9 && t1->del == 1 && t1->kept == 5 && t1->survival_pct == 56, "t1 survival %d", t1->survival_pct);
    CHECK(t1->nnotes == 1 && t1->untested == 1 && !t1->tested && t1->on_changes == 140 && t1->led_pct == 70, "t1 info");
    CHECK(t2->blind && t2->failing == 1 && t2->nnotes == 2 && t2->notes[0] == -1 && t2->subagents && t2->nagents == 1, "t2");
    CHECK(t0->nnotes == 2 && t0->notes[0] == 2 && t0->notes[1] == 0, "t0 notes");
    CHECK(t3->slow && near(t3->slow_x, 1000 / 325.0) && !t0->slow && near(m->turn_avg, 325), "slow turn");
    CHECK(near(t1->bar_h, 5 + 17 * sqrt(10 / 10.0)) && m->bar_big == 10, "bar height");
    st_free(m);
}

/* One change, no time, no tokens: every average is zero; a root over a folder
 * chain doesn't merge, the chain does; a turn whose changes aren't in the tree
 * (the file is gone). */
static void snapshot_b(const char *dir) {
    reset();
    int r = node(-1, AN_DIR, "r", 0), p = node(r, AN_DIR, "p", 0), q = node(p, AN_DIR, "q", 0);
    int f = node(q, AN_FILE, "f.c", 2);
    AnCh fch[] = {{0, 2, 0, 0}};
    AnRun fbl[] = {{2, 0}};
    AnTr ftr[] = {{0, 2, 0}};
    SET(f, ch, fch), SET(f, blame, fbl), SET(f, tr, ftr);
    AnTurn turns[] = {{.title = "only", .ts = BASE, .actor = 0}, {.title = "gone", .ts = BASE + 10, .actor = 0}};
    AnChange ch[] = {{.turn = 0, .comment = 0, .tool = "Write", .epoch = (long long)BASE, .actor = 0}};
    char *comments[] = {""};
    AnActor actors[] = {{.id = "s0", .name = "session s0"}};
    AnSnapshot s = {.nodes = nodes, .nnodes = nnodes, .turns = turns, .nturns = 2, .comments = comments, .ncomments = 1,
                    .actors = actors, .nactors = 1, .changes = ch, .nchanges = 1};
    StMetrics *m = st_build(&s);
    dump(dir, "B", &s, m);
    CHECK(m->nnodes == 3 && !strcmp(m->nodes[0].name, "r") && !strcmp(m->nodes[1].id, "r/p/q"), "root stays, chain merges");
    CHECK(m->turn_avg == 0 && m->avg_time[ST_LV_FILE] == 0 && !m->nodes[2].flags.slow && !m->turns[0].slow &&
              m->turns[0].slow_x == 0, "zero averages: nothing slow");
    CHECK(near(m->turns[0].bar_w, 0.4) && near(m->turns[0].bar_h, 22) && m->turns[0].led_pct == 0, "zero bar");
    CHECK(m->age_span == 3600 && m->nodes[2].age_median == 0 && near(m->nodes[2].heat[ST_AGE], 1), "age span floor");
    CHECK(m->nodes[2].heat[ST_COST] == 0 && m->max[ST_LV_FILE][ST_COST] == 1, "zero cost heat");
    CHECK(!m->turns[1].ncids && !m->turns[1].nnodes && !m->turns[1].nfiles && m->turns[1].tested, "a turn with nothing in the tree");
    CHECK(m->nodes[2].ntrend == 1 && !m->nodes[2].growing && !m->nodes[2].npartners, "one point, single turn");
    st_free(m);
}

/* The >= boundaries: a file at exactly 2x the average time per turn, a turn
 * at exactly 2x the average turn, growth of exactly 1.5x and +20 lines or +8
 * complexity, ages of exactly 1 hour, 1 day, 1 week. */
static void snapshot_c(const char *dir) {
    reset();
    long long E = (long long)BASE + 10 * 86400;
    int r = node(-1, AN_DIR, "r", 0), f1 = node(r, AN_FILE, "f1.c", 60), f2 = node(r, AN_FILE, "f2.c", 59);
    int f3 = node(r, AN_FILE, "f3.c", 20), g = node(f3, AN_FUNC, "g", 10), h = node(f3, AN_FUNC, "h", 10);
    def(g, 1, 10, 10, 0), def(h, 11, 20, 9, 0);
    nodes[f3].cx = 19;
    AnCh f1ch[] = {{0, 1, 0, 0}}, f2ch[] = {{1, 1, 0, 0}}, f3ch[] = {{2, 1, 0, 0}, {3, 1, 0, 0}, {4, 1, 0, 0}};
    AnCh gch[] = {{2, 1, 0, 0}}, hch[] = {{2, 1, 0, 0}};
    AnTr f1tr[] = {{0, 40, 0}}, f2tr[] = {{1, 40, 0}}, gtr[] = {{2, 10, 2}}, htr[] = {{2, 10, 2}};
    AnRun f3bl[] = {{4, 2}, {4, 0}, {4, 1}, {4, 3}, {4, 4}};
    SET(f1, ch, f1ch), SET(f2, ch, f2ch), SET(f3, ch, f3ch), SET(g, ch, gch), SET(h, ch, hch);
    SET(f1, tr, f1tr), SET(f2, tr, f2tr), SET(g, tr, gtr), SET(h, tr, htr), SET(f3, blame, f3bl);
    AnTurn turns[] = {{.title = "a", .ts = (double)E - 3600, .secs = 10}, {.title = "b", .ts = (double)E - 86400, .secs = 10},
                      {.title = "c", .ts = (double)E, .secs = 40}};
    AnChange ch[] = {{.turn = 0, .tool = "Edit", .epoch = E - 3600, .secs = 10}, {.turn = 1, .tool = "Edit", .epoch = E - 86400, .secs = 10},
                     {.turn = 2, .tool = "Edit", .epoch = E, .secs = 40}, {.turn = 2, .tool = "Edit", .epoch = E - 7 * 86400},
                     {.turn = 2, .tool = "Edit", .epoch = E - 3599}};
    char *comments[] = {""};
    AnActor actors[] = {{.id = "s0", .name = "session s0"}};
    AnSnapshot s = {.nodes = nodes, .nnodes = nnodes, .turns = turns, .nturns = N(turns), .comments = comments, .ncomments = 1,
                    .actors = actors, .nactors = 1, .changes = ch, .nchanges = N(ch)};
    StMetrics *m = st_build(&s);
    dump(dir, "C", &s, m);
    int F1 = find(m, "r/f1.c"), F2 = find(m, "r/f2.c"), F3 = find(m, "r/f3.c"), G = find(m, "r/f3.c/g"), H = find(m, "r/f3.c/h");
    CHECK(near(m->avg_time[ST_LV_FILE], 20) && m->nodes[F3].flags.slow && !m->nodes[F1].flags.slow, "slow at exactly 2x");
    CHECK(near(m->turn_avg, 20) && m->turns[2].slow && !m->turns[0].slow, "slow turn at exactly 2x");
    CHECK(m->nodes[F1].growing && !m->nodes[F2].growing, "growing at exactly 1.5x and +20");
    CHECK(m->nodes[G].growing && !m->nodes[H].growing, "growing at exactly +8 complexity");
    const int *a = m->nodes[F3].ages;
    CHECK(a[ST_AGE_HOUR] == 8 && a[ST_AGE_DAY] == 4 && a[ST_AGE_WEEK] == 4 && a[ST_AGE_OLDER] == 4, "age buckets: %d %d %d %d",
          a[0], a[1], a[2], a[3]);
    CHECK(m->nodes[F3].age_median == 3600, "upper median %lld", m->nodes[F3].age_median);
    st_free(m);
}

/* ---- a seeded random corpus, for parity ---- */

static unsigned rs;
static int rnd(int n) {
    rs = rs * 1103515245u + 12345u;
    return (int)((rs >> 8) % (unsigned)n);
}

#define RPUSH(arr, n, v)                                                       \
    do {                                                                       \
        (arr) = realloc((arr), (size_t)((n) + 1) * sizeof *(arr));             \
        (arr)[(n)++] = (v);                                                    \
    } while (0)

/* Folder chains (src/core, tools/a/b), 24 files with functions, classes,
 * methods and nested functions, 90 turns over three weeks out of index order,
 * hot files sharing many turns, rework, trends, blame runs from any change. */
static void snapshot_r(const char *dir, unsigned seed, const char *name) {
    reset();
    rs = seed;
    static char names[1024][16];
    static char paths_buf[32][40];
    enum { NF = 24, NT = 90, NC = 700 };
    int r = node(-1, AN_DIR, "r", 0), src = node(r, AN_DIR, "src", 0), core = node(src, AN_DIR, "core", 0);
    int lib = node(r, AN_DIR, "lib", 0), util = node(lib, AN_DIR, "util", 0), tools = node(r, AN_DIR, "tools", 0);
    int ta = node(tools, AN_DIR, "a", 0), tb = node(ta, AN_DIR, "b", 0);
    int holders[] = {core, lib, util, tb}, files[NF], nd[NF], defs[NF][24], np = 0;
    const char *hpath[] = {"src/core", "lib", "lib/util", "tools/a/b"};
    char *paths[NF + 2];
    for (int f = 0; f < NF; f++) {
        int hi = rnd(4);
        snprintf(names[nnodes], 16, "f%d.%s", f, f % 3 ? "c" : "py");
        files[f] = node(holders[hi], AN_FILE, names[nnodes], 0);
        snprintf(paths_buf[np], 40, "%s/%s", hpath[hi], names[nnodes - 1]);
        paths[np] = paths_buf[np], np++;
        nd[f] = 0;
        int line = 1 + rnd(5), cx = 0;
        for (int k = 0, top = rnd(5); k < top; k++) {
            bool cls = rnd(3) == 0;
            int len = 10 + rnd(30), d;
            snprintf(names[nnodes], 16, "%s%d", cls ? "K" : "fn", k);
            d = node(files[f], cls ? AN_CLASS : AN_FUNC, names[nnodes], len);
            def(d, line, line + len - 1, 1 + rnd(12), 0);
            defs[f][nd[f]++] = d;
            for (int j = 0, sub = cls ? 1 + rnd(3) : rnd(2); j < sub; j++) { /* methods, or a nested function */
                int sl = 1 + rnd(len / (sub + 1) > 1 ? len / (sub + 1) : 1), sa = line + 1 + j * (len / (sub + 1)), m;
                snprintf(names[nnodes], 16, "%s%d", cls ? "m" : "in", j);
                m = node(d, AN_FUNC, names[nnodes], sl);
                def(m, sa, sa + sl - 1, 1 + rnd(nodes[d].cx), 1);
                defs[f][nd[f]++] = m;
            }
            cx += nodes[d].cx;
            line += len + rnd(4);
        }
        nodes[files[f]].lines = line + rnd(10), nodes[files[f]].cx = cx;
    }
    paths[np++] = "other.txt", paths[np++] = "README.md";
    /* turns: time order is a shuffle of the indices; gaps up to 3 days */
    static AnTurn turns[NT];
    int tord[NT];
    for (int t = 0; t < NT; t++) tord[t] = t;
    for (int t = NT - 1; t > 0; t--) {
        int j = rnd(t + 1), x = tord[t];
        tord[t] = tord[j], tord[j] = x;
    }
    double clock = BASE;
    double tts[NT];
    for (int k = 0; k < NT; k++) {
        int t = tord[k];
        clock += 60 + rnd(k % 7 ? 7200 : 3 * 86400);
        tts[t] = clock;
        turns[t] = (AnTurn){.title = "t", .ts = clock, .actor = rnd(3), .secs = rnd(4) ? 10 + rnd(3000) : 0,
                            .has_tok = rnd(3) != 0, .tok = {rnd(20000), rnd(9000), rnd(90000)}, .fix = rnd(5) == 0,
                            .corr = rnd(6) ? NULL : "that's wrong", .intr = rnd(8) == 0};
    }
    static AnChange ch[NC];
    static AnRead tops[NC][8];
    static char *tool[] = {"Edit", "Write", "Bash", "MultiEdit"};
    int *fch[NF] = {0}, nfch[NF] = {0};
    for (int c = 0; c < NC; c++) {
        int t = tord[c * NT / NC], f = rnd(10) < 7 ? rnd(8) : rnd(NF), add = rnd(30), del = rnd(15);
        ch[c] = (AnChange){.turn = t, .comment = rnd(2) ? rnd(4) : -1 - rnd(4), .tool = tool[rnd(4)], .verify = rnd(3),
                           .reads = rnd(6), .searches = rnd(4), .top = tops[c], .ntop = rnd(9),
                           .epoch = (long long)tts[t] + c % 50, .actor = rnd(5) ? turns[t].actor : 3,
                           .tok = {rnd(3000), rnd(500), rnd(40000)}, .secs = rnd(300)};
        for (int k = 0; k < ch[c].ntop; k++) tops[c][k] = (AnRead){rnd(np), 1 + rnd(4)};
        AnNode *F = &nodes[files[f]];
        RPUSH(F->ch, F->nch, ((AnCh){c, add, del, rnd(9) == 0}));
        if (nfch[f] && rnd(2)) RPUSH(F->rw, F->nrw, ((AnRw){fch[f][rnd(nfch[f])], c, 1 + rnd(5)}));
        RPUSH(F->tr, F->ntr, ((AnTr){c, F->lines - rnd(20), 0}));
        for (int k = 0, nk = nd[f] ? rnd(4) : 0; k < nk; k++) {
            int di = defs[f][rnd(nd[f])];
            AnCh e = {c, rnd(add + 3), rnd(del + 2), rnd(7) == 0};
            for (int x = di; x != files[f]; x = nodes[x].parent) { /* nested ones count inside their class too */
                AnNode *D = &nodes[x];
                RPUSH(D->ch, D->nch, e);
                if (rnd(2)) RPUSH(D->tr, D->ntr, ((AnTr){c, D->lines - rnd(8) + rnd(30), D->cx - rnd(3) + rnd(10)}));
                if (nfch[f] && rnd(3) == 0) RPUSH(D->rw, D->nrw, ((AnRw){fch[f][rnd(nfch[f])], c, 1 + rnd(3)}));
            }
        }
        RPUSH(fch[f], nfch[f], c);
    }
    for (int f = 0; f < NF; f++) { /* today's lines: runs from its changes, others' or none */
        AnNode *F = &nodes[files[f]];
        for (int l = 0; l < F->lines;) {
            int n = 1 + rnd(12), o = rnd(5) == 0 ? -1 : nfch[f] && rnd(6) ? fch[f][rnd(nfch[f])] : rnd(NC);
            n = n < F->lines - l ? n : F->lines - l;
            RPUSH(F->blame, F->nblame, ((AnRun){n, o}));
            l += n;
        }
        free(fch[f]);
    }
    char *comments[] = {"first", "", "third note", "fourth"};
    AnActor actors[] = {{.id = "s0", .name = "s0"}, {.id = "s1", .name = "s1"}, {.id = "s2", .name = "s2"},
                        {.sub = true, .id = "x", .name = "explore", .task = "look"}};
    AnSnapshot s = {.nodes = nodes, .nnodes = nnodes, .turns = turns, .nturns = NT, .comments = comments, .ncomments = 4,
                    .paths = paths, .npaths = np, .actors = actors, .nactors = 4, .changes = ch, .nchanges = NC};
    StMetrics *m = st_build(&s);
    dump(dir, name, &s, m);
    int many = 0, two = 0, nested = 0;
    for (int i = 0; i < m->nnodes; i++) {
        const StNode *d = &m->nodes[i];
        many += d->turns >= 2 && d->level == ST_LV_FILE && d->npartners == ST_TOP_ROWS;
        for (int k = 0; k < d->npartners; k++) two += d->partners[k].shared == 2;
        nested += d->kind >= AN_FUNC && m->nodes[d->parent].kind >= AN_FUNC && d->turns >= 2;
    }
    CHECK(m->nturns > 64 && many && two && nested && find(m, "r/src/core") >= 0 && find(m, "r/tools/a/b") >= 0,
          "corpus %s covers: %d full partner lists, %d 2-turn partners, %d nested", name, many, two, nested);
    st_free(m);
    reset_owned();
}

int main(int argc, char **argv) {
    const char *dir = argc > 1 ? argv[1] : NULL;
    snapshot_a(dir);
    snapshot_b(dir);
    snapshot_c(dir);
    snapshot_r(dir, 1, "R1");
    snapshot_r(dir, 2, "R2");
    snapshot_r(dir, 3, "R3");
    reset();
    AnSnapshot empty = {0};
    StMetrics *m = st_build(&empty);
    CHECK(m && !m->nnodes && !m->nturns && m->bar_big == 1 && st_actor_color(m, 0) == -1, "empty snapshot");
    st_free(m);
    printf("%s metrics (%d checks)\n", fails ? "FAIL" : "ok  ", checks);
    return fails != 0;
}
