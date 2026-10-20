/* The structure view's derived values: a port of what
 * aigource/draft/structure.tpl.html computes from data.json in JavaScript
 * (prep, "10-12. Per node", METRIC/MAX/heat, SLOW_X, partners, TINFO,
 * turnBar). Names follow the page's. tests/metrics_parity.js runs the page's
 * own code on the same snapshot and compares.
 *
 * Orders matter where the page breaks ties by insertion order (its sorts are
 * stable): a node's changes, a folder's (its children's in snapshot order),
 * its turns in first-seen order, nodes breadth-first as d3's descendants().
 * So every sort here is stable (ssort) and walks the same order. */
#include "metrics.h"

#include <limits.h>
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "util.h"

#define PUSH(a, n, cap, v)                                                   \
    do {                                                                     \
        if ((n) == (cap)) {                                                  \
            (cap) = (cap) ? (cap) * 2 : 8;                                   \
            (a) = xrealloc((a), (size_t)(cap) * sizeof *(a));                \
        }                                                                    \
        (a)[(n)++] = (v);                                                    \
    } while (0)

#define DESC(a, b) (((b) > (a)) - ((b) < (a))) /* larger first */

static double jround(double x) { return floor(x + 0.5); } /* Math.round */

typedef int (*Cmp)(const void *, const void *, void *);

/* Stable merge sort (the page's Array.prototype.sort is stable). */
static void ssort(void *base, size_t n, size_t sz, Cmp cmp, void *ctx) {
    if (n < 2) return;
    char *a = base, *tmp = xmalloc(n * sz);
    for (size_t w = 1; w < n; w *= 2) {
        for (size_t lo = 0; lo < n; lo += 2 * w) {
            size_t mid = lo + w < n ? lo + w : n, hi = lo + 2 * w < n ? lo + 2 * w : n, i = lo, j = mid, k = lo;
            while (i < mid && j < hi) {
                if (cmp(a + j * sz, a + i * sz, ctx) < 0) memcpy(tmp + k++ * sz, a + j++ * sz, sz);
                else memcpy(tmp + k++ * sz, a + i++ * sz, sz);
            }
            memcpy(tmp + k * sz, a + i * sz, (mid - i) * sz);
            k += mid - i;
            memcpy(tmp + k * sz, a + j * sz, (hi - j) * sz);
        }
        memcpy(a, tmp, n * sz);
    }
    free(tmp);
}

/* ---- the page's tree: chains merged, folders' changes gathered, sized ---- */

typedef struct {
    int an, parent;
    char *name, *id;
    int *kids, nkids, ckids;
    AnCh *ch;          /* a folder's: its children's, concatenated (owned) */
    int nch, cch;
    AnRw *rw;
    int nrw, crw;
    int lines, cx, own, value;
} Tmp;

typedef struct {
    const AnSnapshot *s;
    Tmp *t;
    int nt, ct;
    int *of_an;        /* snapshot node -> tmp node */
} Build;

static char *join(const char *a, const char *b) {
    size_t na = strlen(a), nb = strlen(b);
    char *r = xmalloc(na + nb + 2);
    memcpy(r, a, na);
    r[na] = '/';
    memcpy(r + na + 1, b, nb + 1);
    return r;
}

static int cmp_value(const void *pa, const void *pb, void *ctx) {
    const Tmp *t = ctx;
    return DESC(t[*(const int *)pa].value, t[*(const int *)pb].value);
}

/* prep(): a folder whose only child is a folder becomes one node with it
 * (not the root); a folder's changes, rework, lines and complexity are its
 * children's. Then d3.hierarchy's sum(own) and sort by value. */
static int tmp_build(Build *b, int sn, int parent, const char *ppath) {
    const AnSnapshot *s = b->s;
    int an = sn;
    char *name = xstrdup(s->nodes[an].name);
    b->of_an[an] = b->nt;
    while (s->nodes[an].kind == AN_DIR && *ppath && s->nodes[an].nkids == 1 &&
           s->nodes[s->nodes[an].kids[0]].kind == AN_DIR) {
        an = s->nodes[an].kids[0];
        char *nm = join(name, s->nodes[an].name);
        free(name);
        name = nm;
        b->of_an[an] = b->nt;
    }
    Tmp z = {.an = an, .parent = parent, .name = name, .id = *ppath ? join(ppath, name) : xstrdup(name)};
    PUSH(b->t, b->nt, b->ct, z);
    int me = b->nt - 1;
    const AnNode *n = &s->nodes[an];
    for (int k = 0; k < n->nkids; k++) {
        int kid = tmp_build(b, n->kids[k], me, b->t[me].id);
        Tmp *t = &b->t[me];
        PUSH(t->kids, t->nkids, t->ckids, kid);
    }
    Tmp *t = &b->t[me];
    if (n->kind == AN_DIR) {
        for (int k = 0; k < t->nkids; k++) {
            const Tmp *c = &b->t[t->kids[k]];
            for (int i = 0; i < c->nch; i++) PUSH(t->ch, t->nch, t->cch, c->ch[i]);
            for (int i = 0; i < c->nrw; i++) PUSH(t->rw, t->nrw, t->crw, c->rw[i]);
            t->lines += c->lines;
            t->cx += c->cx;
        }
    } else {
        t->ch = n->ch, t->nch = n->nch, t->rw = n->rw, t->nrw = n->nrw;
        t->lines = n->lines, t->cx = n->cx;
    }
    int kl = 0, kv = 0;
    for (int k = 0; k < t->nkids; k++) kl += b->t[t->kids[k]].lines, kv += b->t[t->kids[k]].value;
    t->own = t->nkids ? (t->lines - kl > 0 ? t->lines - kl : 0) : t->lines;
    t->value = t->own + kv;
    ssort(t->kids, (size_t)t->nkids, sizeof *t->kids, cmp_value, b->t);
    return me;
}

/* ---- sorts ---- */

typedef struct {
    const long long *tms; /* turn -> prompt time in ms, as the page's ISO strings compare */
} Ctx;

static int cmp_turn_time(const void *pa, const void *pb, void *ctx) {
    const long long *tms = ((const Ctx *)ctx)->tms;
    long long a = tms[*(const int *)pa], b = tms[*(const int *)pb];
    return (a > b) - (a < b);
}

static int cmp_rows_newest(const void *pa, const void *pb, void *ctx) {
    const long long *tms = ((const Ctx *)ctx)->tms;
    long long a = tms[((const StNodeTurn *)pa)->turn], b = tms[((const StNodeTurn *)pb)->turn];
    return (b > a) - (b < a);
}

static int cmp_blame(const void *pa, const void *pb, void *ctx) {
    const long long *tms = ((const Ctx *)ctx)->tms;
    int ta = ((const StBlame *)pa)->turn, tb = ((const StBlame *)pb)->turn;
    if (ta < 0 || tb < 0) return (ta < 0) - (tb < 0);
    return DESC(tms[ta], tms[tb]);
}

static int cmp_reads(const void *pa, const void *pb, void *ctx) {
    (void)ctx;
    return DESC(((const StRead *)pa)->n, ((const StRead *)pb)->n);
}

static int cmp_pairs(const void *pa, const void *pb, void *ctx) {
    (void)ctx;
    return DESC(((const StRwPair *)pa)->n, ((const StRwPair *)pb)->n);
}

static int cmp_who(const void *pa, const void *pb, void *ctx) {
    (void)ctx;
    const StWho *a = pa, *b = pb;
    if ((a->actor < 0) != (b->actor < 0)) return (a->actor < 0) - (b->actor < 0);
    return DESC(a->lines, b->lines);
}

static int cmp_lines_desc(const void *pa, const void *pb, void *ctx) {
    (void)ctx;
    return DESC(((const StWho *)pa)->lines, ((const StWho *)pb)->lines);
}

static int cmp_partner(const void *pa, const void *pb, void *ctx) {
    (void)ctx;
    const StPartner *a = pa, *b = pb;
    return a->shared != b->shared ? DESC(a->shared, b->shared) : DESC(a->degree, b->degree);
}

static int cmp_touch(const void *pa, const void *pb, void *ctx) {
    (void)ctx;
    const StTouch *a = pa, *b = pb;
    return DESC(a->add + a->del, b->add + b->del);
}

static int cmp_int(const void *pa, const void *pb) {
    int a = *(const int *)pa, b = *(const int *)pb;
    return (a > b) - (a < b);
}

typedef struct { long long age; int n; } AgeRun; /* n lines of this age */

static int cmp_agerun(const void *pa, const void *pb) {
    long long a = ((const AgeRun *)pa)->age, b = ((const AgeRun *)pb)->age;
    return (a > b) - (a < b);
}

/* Sorted by age, equal ages merged: the new count. */
static int age_runs_merge(AgeRun *r, int n) {
    if (n < 2) return n;
    qsort(r, (size_t)n, sizeof *r, cmp_agerun);
    int u = 0;
    for (int k = 1; k < n; k++)
        if (r[k].age == r[u].age) r[u].n += r[k].n;
        else r[++u] = r[k];
    return u + 1;
}

typedef struct {       /* per node, while building */
    AgeRun *ages;      /* known ages of its lines (age_runs_merge'd) */
    int nages, unknown;
    int *cid_lines;    /* file: cid, lines changed, cid, ... by cid (lineShare's denominator) */
    int ncid_lines;
} Scratch;

typedef struct {       /* first-seen lookups: key -> position, valid while stamp[key] == gen */
    int *stamp, *pos, gen;
} Seen;

static Seen seen_new(int n) {
    return (Seen){xcalloc((size_t)(n > 0 ? n : 1), sizeof(int)), xcalloc((size_t)(n > 0 ? n : 1), sizeof(int)), 0};
}
static void seen_free(Seen *s) { free(s->stamp), free(s->pos); }
/* key's position, or -1 the first time (then it's at *next, which advances) */
static int seen(Seen *s, int key, int *next) {
    if (s->stamp[key] == s->gen) return s->pos[key];
    s->stamp[key] = s->gen;
    s->pos[key] = (*next)++;
    return -1;
}

/* readsOf(cids): per path its highest read count among the changes, most
 * read first; the top ST_TOP_ROWS, with the file node of each path. */
static StRead *reads_of(const AnSnapshot *s, const int *cids, int n, const StrMap *by_rel, Seen *ps, int *out_n) {
    StRead *r = NULL;
    int nr = 0, cr = 0;
    ps->gen++;
    for (int i = 0; i < n; i++) {
        const AnChange *c = &s->changes[cids[i]];
        for (int k = 0; k < c->ntop; k++) {
            int p = c->top[k].path, at = nr;
            if (p < 0 || p >= s->npaths) continue;
            int j = seen(ps, p, &at);
            if (j < 0) PUSH(r, nr, cr, ((StRead){p, c->top[k].n, -1}));
            else if (c->top[k].n > r[j].n) r[j].n = c->top[k].n;
        }
    }
    ssort(r, (size_t)nr, sizeof *r, cmp_reads, NULL);
    if (nr > ST_TOP_ROWS) nr = ST_TOP_ROWS;
    for (int j = 0; j < nr; j++) r[j].node = strmap_get(by_rel, s->paths[r[j].path]);
    *out_n = nr;
    return r;
}

static bool has_note(const AnSnapshot *s, int c) {
    int ix = c >= 0 ? c : -1 - c;
    return ix < s->ncomments && s->comments[ix] && *s->comments[ix];
}

static bool same_note(int a, int b) { return (a >= 0 ? a : -1 - a) == (b >= 0 ? b : -1 - b); }

/* Lines by agent, in first-seen order (the owner's tie-break); actor -1 at key 0. */
static void add_who(StNode *d, int *cap, Seen *ws, int actor, int lines) {
    int at = d->nwho, j = seen(ws, actor + 1, &at);
    if (j < 0) { PUSH(d->who, d->nwho, *cap, ((StWho){actor, 0})); j = at - 1; }
    d->who[j].lines += lines;
}

static int file_of(const StMetrics *m, int i) {
    while (i >= 0 && m->nodes[i].kind != AN_FILE) i = m->nodes[i].parent;
    return i;
}

/* The blame runs of f over its lines a..b (1-based), clipped, into out
 * (room for f->nblame): the originsOf(f).slice(a - 1, b) of the page, as runs. */
static int clip_runs(const AnNode *f, int a, int b, AnRun *out) {
    int n = 0, line = 1;
    for (int k = 0; k < f->nblame && line <= b; k++) {
        int lo = line, hi = line + f->blame[k].n - 1;
        line = hi + 1;
        lo = lo > a ? lo : a, hi = hi < b ? hi : b;
        if (hi >= lo) out[n++] = (AnRun){hi - lo + 1, f->blame[k].origin};
    }
    return n;
}

/* A node's lines as clip_runs gives them: a file's all, a def's a..b. */
static int node_runs(const StMetrics *m, int i, AnRun *out) {
    const StNode *d = &m->nodes[i];
    const AnNode *f = &m->snap->nodes[m->nodes[d->file].an];
    return d->kind == AN_FILE ? clip_runs(f, 1, INT_MAX, out) : clip_runs(f, d->a > 1 ? d->a : 1, d->b, out);
}

static int cmp_cid_key(const void *k, const void *e) {
    int a = *(const int *)k, b = *(const int *)e;
    return (a > b) - (a < b);
}

StMetrics *st_build(const AnSnapshot *s) {
    StMetrics *m = xcalloc(1, sizeof *m);
    m->snap = s;
    m->nturns = s->nturns;
    m->turns = xcalloc((size_t)(s->nturns ? s->nturns : 1), sizeof *m->turns);
    m->order = xcalloc((size_t)(s->nturns ? s->nturns : 1), sizeof *m->order);
    long long *tms = xcalloc((size_t)(s->nturns ? s->nturns : 1), sizeof *tms);
    for (int t = 0; t < s->nturns; t++) tms[t] = llround(s->turns[t].ts * 1e6) / 1000, m->order[t] = t;
    Ctx ctx = {tms};
    ssort(m->order, (size_t)s->nturns, sizeof *m->order, cmp_turn_time, &ctx);
    int *rank = xcalloc((size_t)(s->nturns ? s->nturns : 1), sizeof *rank);
    for (int r = 0; r < s->nturns; r++) rank[m->order[r]] = r, m->turns[m->order[r]].rank = r;
    int maxactor = -1;
    for (int c = 0; c < s->nchanges; c++) {
        long long e = s->changes[c].epoch;
        if (!c || e > m->now) m->now = e;
        if (!c || e < m->first) m->first = e;
        if (s->changes[c].actor > maxactor) maxactor = s->changes[c].actor;
    }
    m->age_span = m->now - m->first > 3600 ? m->now - m->first : 3600;
    for (int lv = 0; lv < 3; lv++)
        for (int k = 0; k < ST_NMODES; k++) m->max[lv][k] = 1;
    m->bar_big = 1;
    if (!s->nnodes) {
        free(tms);
        free(rank);
        return m;
    }

    /* The tree, breadth-first. */
    Build b = {.s = s, .of_an = xmalloc((size_t)s->nnodes * sizeof(int))};
    for (int i = 0; i < s->nnodes; i++) b.of_an[i] = -1;
    tmp_build(&b, 0, -1, "");
    int nn = b.nt, *fin = xmalloc((size_t)nn * sizeof *fin), *bfs = xmalloc((size_t)nn * sizeof *bfs);
    int qh = 0, qt = 0;
    bfs[qt++] = 0;
    while (qh < qt) {
        int i = bfs[qh++];
        for (int k = 0; k < b.t[i].nkids; k++) bfs[qt++] = b.t[i].kids[k];
    }
    for (int r = 0; r < nn; r++) fin[bfs[r]] = r;
    m->nnodes = nn;
    m->nodes = xcalloc((size_t)nn, sizeof *m->nodes);
    const Tmp **tp = xmalloc((size_t)nn * sizeof *tp); /* display node -> its tmp */
    for (int r = 0; r < nn; r++) {
        Tmp *t = &b.t[bfs[r]];
        StNode *d = &m->nodes[r];
        const AnNode *an = &s->nodes[t->an];
        tp[r] = t;
        d->kind = an->kind;
        d->level = an->kind == AN_DIR ? ST_LV_DIR : an->kind == AN_FILE ? ST_LV_FILE : ST_LV_DEF;
        d->name = t->name, d->id = t->id, t->name = t->id = NULL;
        d->an = t->an;
        d->parent = t->parent < 0 ? -1 : fin[t->parent];
        d->depth = d->parent < 0 ? 0 : m->nodes[d->parent].depth + 1;
        d->nkids = t->nkids;
        d->kids = xmalloc((size_t)(t->nkids ? t->nkids : 1) * sizeof *d->kids);
        for (int k = 0; k < t->nkids; k++) d->kids[k] = fin[t->kids[k]];
        d->own = t->own, d->value = t->value;
        d->lines = t->lines, d->cx = t->cx, d->nest = an->kind >= AN_FUNC ? an->depth : 0;
    }
    m->nan = s->nnodes;
    m->of_an = b.of_an;
    for (int i = 0; i < s->nnodes; i++)
        if (m->of_an[i] >= 0) m->of_an[i] = fin[m->of_an[i]];

    StrMap by_rel = {0}; /* BY_REL: a file's path below the root -> node */
    int maxblame = 1;
    for (int i = 0; i < nn; i++) {
        const char *sl = strchr(m->nodes[i].id, '/');
        if (m->nodes[i].kind == AN_FILE) strmap_put(&by_rel, sl ? sl + 1 : "", i);
        if (strmap_get(&m->by_id, m->nodes[i].id) < 0) strmap_put(&m->by_id, m->nodes[i].id, i);
        if (sl && strmap_get(&m->by_rel, sl + 1) < 0) strmap_put(&m->by_rel, sl + 1, i);
        StNode *d = &m->nodes[i];
        d->file = d->kind == AN_DIR ? -1 : file_of(m, i);
        d->lang = d->file >= 0 ? s->nodes[m->nodes[d->file].an].lang : NULL;
        if (d->kind >= AN_FUNC) d->a = s->nodes[d->an].a, d->b = s->nodes[d->an].b;
        if (d->kind == AN_FILE && s->nodes[d->an].nblame > maxblame) maxblame = s->nodes[d->an].nblame;
    }

    int nt = s->nturns, nc = s->nchanges;
    Seen ts = seen_new(nt + 1), cs = seen_new(nc), ps = seen_new(s->npaths), ws = seen_new(maxactor + 2);
    Seen rws = seen_new(nt * (ST_REWORK_TURNS + 1)), ns = seen_new(nn);
    int *first = xcalloc((size_t)(nt ? nt : 1), sizeof *first);
    int *cids = NULL, ncids = 0, ccids = 0;
    long *acc = xcalloc((size_t)(nc ? nc : 1), sizeof *acc); /* lines a node changed per change */
    double *share = NULL;
    AnRun *runs = xmalloc((size_t)maxblame * sizeof *runs);
    Scratch *sc = xcalloc((size_t)nn, sizeof *sc);

    /* prep(): turns, +/-, rework, fixes, context, flags; the turn rows. */
    for (int i = 0; i < nn; i++) {
        StNode *d = &m->nodes[i];
        const Tmp *t = tp[i];
        bool notes = d->kind != AN_DIR;
        int *ctx_first = NULL, nctx = 0, cctx = 0, crows = 0;
        ts.gen++;
        for (int k = 0; k < t->nch; k++) {
            const AnCh *e = &t->ch[k];
            const AnChange *c = &s->changes[e->cid];
            int tu = c->turn, at = d->nrows, row = seen(&ts, tu, &at);
            if (row < 0) {
                PUSH(d->rows, d->nrows, crows, ((StNodeTurn){.turn = tu}));
                PUSH(ctx_first, nctx, cctx, e->cid);
                first[tu] = nctx - 1;
                row = d->nrows - 1;
            } else if (e->cid < ctx_first[first[tu]]) {
                ctx_first[first[tu]] = e->cid;
            }
            StNodeTurn *r = &d->rows[row];
            r->changes++, r->add += e->add, r->del += e->del;
            if (c->verify == AN_UNTESTED) r->flags |= STT_UNTESTED, d->flags.untested++;
            if (c->verify == AN_FAILING) r->flags |= STT_FAILING, d->flags.failing++;
            if (e->blind) r->flags |= STT_BLIND, d->flags.blind++;
            d->add += e->add, d->del += e->del;
            if (notes && has_note(s, c->comment)) {
                int j = 0;
                while (j < r->nnotes && !same_note(r->notes[j], c->comment)) j++;
                if (j == r->nnotes) {
                    r->notes = xrealloc(r->notes, (size_t)(r->nnotes + 1) * sizeof *r->notes);
                    r->notes[r->nnotes++] = c->comment;
                }
            }
            int j = 0;
            while (j < r->ntools && strcmp(r->tools[j], c->tool)) j++;
            if (j == r->ntools) {
                r->tools = xrealloc(r->tools, (size_t)(r->ntools + 1) * sizeof *r->tools);
                r->tools[r->ntools++] = c->tool;
            }
        }
        d->edits = t->nch;
        d->turns = d->nrows;
        for (int k = 0; k < d->nrows; k++) {
            const AnTurn *tu = &s->turns[d->rows[k].turn];
            d->fix_turns += tu->fix;
            d->flags.corrected += tu->corr != NULL;
            d->flags.interrupted += tu->intr;
            if (tu->corr && d->ncorr_turns < 2) d->corr_turns[d->ncorr_turns++] = d->rows[k].turn;
        }
        ssort(d->rows, (size_t)d->nrows, sizeof *d->rows, cmp_rows_newest, &ctx);

        /* rework: lines written, then rewritten within ST_REWORK_TURNS turns */
        StRwPair *pairs = NULL;
        int np = 0, cp = 0;
        rws.gen++;
        for (int k = 0; k < t->nrw; k++) {
            const AnRw *w = &t->rw[k];
            if (w->origin < 0 || w->cid < 0) continue;
            int to = s->changes[w->origin].turn, tc = s->changes[w->cid].turn, gap = rank[tc] - rank[to];
            if (gap < 0 || gap > ST_REWORK_TURNS) continue;
            d->rework += w->n;
            if (!gap) d->rework_same += w->n;
            int at = np, j = seen(&rws, to * (ST_REWORK_TURNS + 1) + gap, &at);
            if (j < 0) { PUSH(pairs, np, cp, ((StRwPair){to, tc, 0})); j = np - 1; }
            pairs[j].n += w->n;
        }
        ssort(pairs, (size_t)np, sizeof *pairs, cmp_pairs, NULL);
        d->nrw_pairs = np < ST_RW_PAIRS ? np : ST_RW_PAIRS;
        if (np) memcpy(d->rw_pairs, pairs, (size_t)d->nrw_pairs * sizeof *pairs);
        free(pairs);
        d->rework_pct = d->add ? (int)jround(100 * (d->rework < d->add ? (double)d->rework / d->add : 1)) : 0;

        /* context cost: per turn, what the agent had read before its first change here */
        d->ctx_turns = nctx;
        if (nctx) {
            double rs = 0, r = 0, se = 0;
            for (int k = 0; k < nctx; k++) {
                const AnChange *c = &s->changes[ctx_first[k]];
                rs += c->reads + c->searches, r += c->reads, se += c->searches;
            }
            d->ctx_avg = rs / nctx, d->ctx_reads = r / nctx, d->ctx_searches = se / nctx;
            d->ctx_rows = reads_of(s, ctx_first, nctx, &by_rel, &ps, &d->nctx_rows);
        }
        free(ctx_first);

        /* distinct changes, in order, with the lines changed in each: tokens and time,
         * a def's by its share of the file's lines (lineShare) */
        ncids = 0;
        cs.gen++;
        for (int k = 0; k < t->nch; k++) {
            int c = t->ch[k].cid, at = ncids;
            if (seen(&cs, c, &at) < 0) { PUSH(cids, ncids, ccids, c); acc[c] = 0; }
            acc[c] += t->ch[k].add + t->ch[k].del;
        }
        d->ncids = ncids;
        share = xrealloc(share, (size_t)(ncids ? ncids : 1) * sizeof *share);
        const Scratch *fx = d->level == ST_LV_DEF ? &sc[d->file] : NULL;
        for (int k = 0; k < ncids; k++) {
            share[k] = 1;
            const int *all = fx && fx->ncid_lines ? bsearch(&cids[k], fx->cid_lines, (size_t)fx->ncid_lines, 2 * sizeof(int),
                                                            cmp_cid_key) : NULL;
            if (all && all[1]) share[k] = acc[cids[k]] < all[1] ? (double)acc[cids[k]] / all[1] : 1;
        }
        for (int q = 0; q < 4; q++) {
            double sum = 0;
            for (int k = 0; k < ncids; k++) {
                const AnChange *c = &s->changes[cids[k]];
                sum += (q < 3 ? (double)c->tok[q] : (double)c->secs) * share[k];
            }
            d->tok[q] = (long long)jround(sum);
        }
        d->per_turn = d->turns ? (double)d->tok[3] / d->turns : 0;
        if (d->kind == AN_FILE && ncids) { /* its defs come later (breadth-first) */
            Scratch *x = &sc[i];
            x->cid_lines = xmalloc((size_t)ncids * 2 * sizeof(int));
            for (int k = 0; k < ncids; k++) x->cid_lines[2 * k] = cids[k], x->cid_lines[2 * k + 1] = (int)acc[cids[k]];
            x->ncid_lines = ncids;
            qsort(x->cid_lines, (size_t)ncids, 2 * sizeof(int), cmp_cid_key);
        }

        /* trend: today's point when it differs from the last change's */
        if (d->kind != AN_DIR) {
            const AnNode *an = &s->nodes[d->an];
            d->trend = xmalloc((size_t)(an->ntr + 1) * sizeof *d->trend);
            for (int k = 0; k < an->ntr; k++) d->trend[k] = (StPoint){an->tr[k].cid, an->tr[k].lines, an->tr[k].cx};
            d->ntrend = an->ntr;
            if (d->ntrend) {
                StPoint *l = &d->trend[d->ntrend - 1];
                if (l->lines != d->lines || (d->level == ST_LV_DEF && l->cx != d->cx))
                    d->trend[d->ntrend++] = (StPoint){-1, d->lines, d->cx};
            }
            if (d->ntrend >= 2) {
                StPoint f = d->trend[0], l = d->trend[d->ntrend - 1];
                d->growing = (l.lines >= f.lines * 1.5 && l.lines - f.lines >= 20) ||
                             (d->level == ST_LV_DEF && l.cx - f.cx >= 8);
            }
        }
    }
    for (int i = 0; i < nn; i++) free(sc[i].cid_lines), sc[i].cid_lines = NULL;

    /* Today's lines: who wrote them, how old (as weighted runs); children
     * first, each child's runs freed once its folder took them. Then the blame
     * legend: lines per turn, newest first. */
    for (int i = nn - 1; i >= 0; i--) {
        StNode *d = &m->nodes[i];
        Scratch *x = &sc[i];
        int cw = 0, ca = 0;
        ws.gen++;
        if (d->kind == AN_DIR) {
            for (int k = 0; k < d->nkids; k++) {
                const StNode *kd = &m->nodes[d->kids[k]];
                Scratch *kx = &sc[d->kids[k]];
                for (int j = 0; j < kd->nwho; j++) add_who(d, &cw, &ws, kd->who[j].actor, kd->who[j].lines);
                for (int j = 0; j < kx->nages; j++) PUSH(x->ages, x->nages, ca, kx->ages[j]);
                x->unknown += kx->unknown;
                free(kx->ages), kx->ages = NULL, kx->nages = 0;
            }
        } else {
            int nr = node_runs(m, i, runs), cb = 0;
            ts.gen++;
            for (int k = 0; k < nr; k++) {
                int c = runs[k].origin, tu = c >= 0 ? s->changes[c].turn : -1, at = d->nblame;
                add_who(d, &cw, &ws, c >= 0 ? s->changes[c].actor : -1, runs[k].n);
                if (c >= 0) PUSH(x->ages, x->nages, ca, ((AgeRun){m->now - s->changes[c].epoch, runs[k].n}));
                else x->unknown += runs[k].n;
                int j = seen(&ts, tu < 0 ? nt : tu, &at);
                if (j < 0) { PUSH(d->blame, d->nblame, cb, ((StBlame){tu, 0})); j = d->nblame - 1; }
                d->blame[j].n += runs[k].n;
            }
            ssort(d->blame, (size_t)d->nblame, sizeof *d->blame, cmp_blame, &ctx);
        }
        x->nages = age_runs_merge(x->ages, x->nages);
        d->owner = -1;
        for (int j = 0, best = 0; j < d->nwho; j++)
            if (d->who[j].actor >= 0 && d->who[j].lines > best) best = d->who[j].lines, d->owner = d->who[j].actor;
        long known = 0;
        for (int j = 0; j < x->nages; j++) {
            long long g = x->ages[j].age;
            known += x->ages[j].n;
            d->ages[g < 3600 ? ST_AGE_HOUR : g < 86400 ? ST_AGE_DAY : g < 7 * 86400 ? ST_AGE_WEEK : ST_AGE_OLDER] += x->ages[j].n;
        }
        d->ages[ST_AGE_UNKNOWN] = x->unknown;
        d->age_median = -1;
        for (long j = 0, cum = 0; j < x->nages && known; j++) /* the upper median: sorted[known / 2] */
            if ((cum += x->ages[j].n) > known / 2) {
                d->age_median = x->ages[j].age;
                break;
            }
        if (d->kind >= AN_FUNC) free(x->ages), x->ages = NULL, x->nages = 0;
    }
    /* owner colours: agents by lines in the whole tree */
    {
        StWho *w = xmalloc((size_t)(m->nodes[0].nwho ? m->nodes[0].nwho : 1) * sizeof *w);
        int n = 0;
        for (int j = 0; j < m->nodes[0].nwho; j++)
            if (m->nodes[0].who[j].actor >= 0) w[n++] = m->nodes[0].who[j];
        ssort(w, (size_t)n, sizeof *w, cmp_lines_desc, NULL);
        m->actors = xmalloc((size_t)(n ? n : 1) * sizeof *m->actors);
        for (int j = 0; j < n; j++) m->actors[j] = w[j].actor;
        m->nactors = n;
        free(w);
    }
    for (int i = 0; i < nn; i++) {
        ssort(m->nodes[i].who, (size_t)m->nodes[i].nwho, sizeof(StWho), cmp_who, NULL);
        m->nodes[i].owner_slot = st_actor_color(m, m->nodes[i].owner);
    }

    /* METRIC, MAX per level, slow, heat */
    for (int i = 0; i < nn; i++) {
        StNode *d = &m->nodes[i];
        double *v = d->metric;
        v[ST_TURNS] = d->turns;
        v[ST_LINES] = d->add + d->del;
        v[ST_RISK] = (double)d->turns * d->cx;
        v[ST_REWORK] = d->rework;
        v[ST_FIXES] = d->fix_turns;
        v[ST_CONTEXT] = d->ctx_avg;
        v[ST_COST] = (double)d->tok[0];
        v[ST_TIME] = (double)d->tok[3];
        v[ST_AGE] = d->age_median < 0 ? 0 : 1 - log1p(d->age_median / 60.0) / log1p(m->age_span / 60.0);
        v[ST_OWNER] = d->turns;
        for (int k = 0; k < ST_NMODES; k++)
            if (v[k] > m->max[d->level][k]) m->max[d->level][k] = v[k];
    }
    for (int lv = ST_LV_FILE; lv <= ST_LV_DEF; lv++) {
        double sum = 0;
        int n = 0;
        for (int i = 0; i < nn; i++)
            if (m->nodes[i].turns && m->nodes[i].level == lv) sum += m->nodes[i].per_turn, n++;
        m->avg_time[lv] = n ? sum / n : 0;
    }
    for (int i = nn - 1; i >= 0; i--) {
        StNode *d = &m->nodes[i];
        if (d->kind == AN_DIR)
            for (int k = 0; k < d->nkids; k++) d->flags.slow += m->nodes[d->kids[k]].flags.slow;
        else if (d->kind == AN_FILE)
            d->flags.slow = d->turns > 0 && m->avg_time[ST_LV_FILE] > 0 && d->per_turn >= ST_SLOW_X * m->avg_time[ST_LV_FILE];
        for (int k = 0; k < ST_NMODES; k++)
            d->heat[k] = k == ST_AGE ? d->metric[k] : log1p(d->metric[k]) / log1p(m->max[d->level][k]);
        d->kinds = (d->flags.corrected + d->flags.interrupted ? STK_CORR : 0) |
                   (d->flags.untested + d->flags.failing ? STK_TEST : 0) | (d->flags.blind ? STK_BLIND : 0) |
                   (d->flags.slow ? STK_SLOW : 0);
    }
    if (nt) {
        double sum = 0;
        for (int t = 0; t < nt; t++) sum += (double)s->turns[t].secs;
        m->turn_avg = sum / nt;
    }

    /* Coupling: same-level nodes changed in 2+ of the same turns, counted
     * through each turn's list of nodes (an inverted index), not pairwise. */
    {
        int *tin = xmalloc((size_t)nn * sizeof *tin), *tout = xmalloc((size_t)nn * sizeof *tout);
        int *stack = xmalloc((size_t)nn * 2 * sizeof *stack), sp = 0, clock = 0;
        stack[sp++] = 0;
        while (sp) { /* pre/post order: ancestors without walking up */
            int v = stack[--sp];
            if (v < 0) {
                tout[-1 - v] = clock++;
                continue;
            }
            tin[v] = clock++;
            stack[sp++] = -1 - v;
            for (int k = m->nodes[v].nkids - 1; k >= 0; k--) stack[sp++] = m->nodes[v].kids[k];
        }
        int *off[3] = {0}, *at[3] = {0}, *list[3] = {0}; /* per level: turn -> nodes, breadth-first */
        for (int lv = ST_LV_FILE; lv <= ST_LV_DEF; lv++) {
            off[lv] = xcalloc((size_t)nt + 1, sizeof(int));
            at[lv] = xcalloc((size_t)nt + 1, sizeof(int));
        }
        for (int i = 0; i < nn; i++)
            if (m->nodes[i].level != ST_LV_DIR)
                for (int k = 0; k < m->nodes[i].nrows; k++) off[m->nodes[i].level][m->nodes[i].rows[k].turn + 1]++;
        for (int lv = ST_LV_FILE; lv <= ST_LV_DEF; lv++) {
            for (int t = 0; t < nt; t++) off[lv][t + 1] += off[lv][t];
            memcpy(at[lv], off[lv], (size_t)(nt + 1) * sizeof(int));
            list[lv] = xmalloc((size_t)(off[lv][nt] ? off[lv][nt] : 1) * sizeof(int));
        }
        for (int i = 0; i < nn; i++)
            if (m->nodes[i].level != ST_LV_DIR)
                for (int k = 0; k < m->nodes[i].nrows; k++) {
                    int lv = m->nodes[i].level;
                    list[lv][at[lv][m->nodes[i].rows[k].turn]++] = i;
                }
        int *cnt = xcalloc((size_t)nn, sizeof *cnt), *hit = NULL, nhit = 0, chit = 0;
        StPartner *out = NULL;
        int no = 0, co = 0;
        for (int i = 0; i < nn; i++) {
            StNode *d = &m->nodes[i];
            if (d->level == ST_LV_DIR || d->turns < 2) continue;
            int lv = d->level;
            nhit = no = 0;
            ns.gen++;
            for (int k = 0; k < d->nrows; k++) {
                int tu = d->rows[k].turn;
                for (int q = off[lv][tu]; q < off[lv][tu + 1]; q++) {
                    int p = list[lv][q], ph = nhit;
                    if (seen(&ns, p, &ph) < 0) { PUSH(hit, nhit, chit, p); cnt[p] = 0; }
                    cnt[p]++;
                }
            }
            if (nhit) qsort(hit, (size_t)nhit, sizeof *hit, cmp_int); /* PEERS order: breadth-first */
            for (int q = 0; q < nhit; q++) {
                int p = hit[q], shared = cnt[p];
                if (shared < 2 || p == i || (tin[p] > tin[i] && tout[p] < tout[i]) || (tin[i] > tin[p] && tout[i] < tout[p]))
                    continue;
                const StNode *pd = &m->nodes[p];
                int lo = d->turns < pd->turns ? d->turns : pd->turns;
                bool cross = lv == ST_LV_FILE ? pd->parent != d->parent : pd->file != d->file;
                PUSH(out, no, co, ((StPartner){p, shared, (int)jround(100.0 * shared / d->turns), (double)shared / lo, cross}));
            }
            ssort(out, (size_t)no, sizeof *out, cmp_partner, NULL);
            d->npartners = no < ST_TOP_ROWS ? no : ST_TOP_ROWS;
            if (d->npartners) {
                d->partners = xmalloc((size_t)d->npartners * sizeof *d->partners);
                memcpy(d->partners, out, (size_t)d->npartners * sizeof *out);
            }
        }
        free(out);
        free(hit);
        free(cnt);
        for (int lv = 0; lv < 3; lv++) free(off[lv]), free(at[lv]), free(list[lv]);
        free(tin);
        free(tout);
        free(stack);
    }

    /* TINFO: per turn, what it changed. */
    {
        int *cf = xcalloc((size_t)(nt ? nt : 1), sizeof *cf), *cd = xcalloc((size_t)(nt ? nt : 1), sizeof *cd);
        int *cc = xcalloc((size_t)(nt ? nt : 1), sizeof *cc), *cn = xcalloc((size_t)(nt ? nt : 1), sizeof *cn);
        for (int i = 0; i < nn; i++) {
            StNode *d = &m->nodes[i];
            if (d->kind == AN_DIR) continue;
            const Tmp *t = tp[i];
            bool file = d->kind == AN_FILE;
            for (int k = 0; k < t->nch; k++) {
                const AnCh *e = &t->ch[k];
                int tu = s->changes[e->cid].turn;
                StTurn *T = &m->turns[tu];
                StTouch **l = file ? &T->files : &T->defs;
                int *nl = file ? &T->nfiles : &T->ndefs, *cl = file ? &cf[tu] : &cd[tu];
                if (!*nl || (*l)[*nl - 1].node != i) {
                    PUSH(*l, *nl, *cl, ((StTouch){i, 0, 0}));
                    PUSH(T->nodes, T->nnodes, cn[tu], i);
                }
                (*l)[*nl - 1].add += e->add, (*l)[*nl - 1].del += e->del;
                PUSH(T->cids, T->ncids, cc[tu], e->cid);
                if (file) T->add += e->add, T->del += e->del;
                else if (e->blind) T->blind = true;
            }
            if (file) {
                const AnNode *an = &s->nodes[d->an];
                for (int k = 0; k < an->nblame; k++)
                    if (an->blame[k].origin >= 0) m->turns[s->changes[an->blame[k].origin].turn].kept += an->blame[k].n;
            }
        }
        free(cf);
        free(cd);
        free(cc);
        free(cn);
        for (int tu = 0; tu < nt; tu++) {
            StTurn *T = &m->turns[tu];
            const AnTurn *at = &s->turns[tu];
            if (T->ncids) qsort(T->cids, (size_t)T->ncids, sizeof *T->cids, cmp_int);
            int u = 0;
            for (int k = 0; k < T->ncids; k++)
                if (!u || T->cids[u - 1] != T->cids[k]) T->cids[u++] = T->cids[k];
            T->ncids = u;
            ssort(T->files, (size_t)T->nfiles, sizeof *T->files, cmp_touch, NULL);
            ssort(T->defs, (size_t)T->ndefs, sizeof *T->defs, cmp_touch, NULL);
            /* the nodes it touched and their ancestors */
            int *ids = NULL, nids = 0, cids_ = 0;
            ns.gen++;
            for (int k = 0; k < T->nnodes; k++)
                for (int v = T->nodes[k], ph = nids; v >= 0 && seen(&ns, v, &ph) < 0; v = m->nodes[v].parent)
                    PUSH(ids, nids, cids_, v);
            free(T->nodes);
            if (nids) qsort(ids, (size_t)nids, sizeof *ids, cmp_int);
            T->nodes = ids, T->nnodes = nids;
            T->survival_pct = T->add ? (int)jround(100 * (T->kept < T->add ? (double)T->kept / T->add : 1)) : 0;
            int na = 0, ca = 0, *agents = NULL;
            for (int k = 0; k < T->ncids; k++) {
                const AnChange *c = &s->changes[T->cids[k]];
                T->untested += c->verify == AN_UNTESTED;
                T->failing += c->verify == AN_FAILING;
                T->on_changes += c->tok[0];
                int j = 0;
                while (j < na && agents[j] != c->actor) j++;
                if (j == na) PUSH(agents, na, ca, c->actor);
                if (c->actor >= 0 && c->actor < s->nactors && s->actors[c->actor].sub) T->subagents = true;
                if (has_note(s, c->comment)) {
                    int q = 0;
                    while (q < T->nnotes && !same_note(T->notes[q], c->comment)) q++;
                    if (q == T->nnotes) {
                        T->notes = xrealloc(T->notes, (size_t)(T->nnotes + 1) * sizeof *T->notes);
                        T->notes[T->nnotes++] = c->comment;
                    }
                }
            }
            free(agents);
            T->nagents = na;
            T->tested = !T->untested && !T->failing;
            T->slow = m->turn_avg > 0 && at->secs >= ST_SLOW_X * m->turn_avg;
            T->slow_x = m->turn_avg > 0 ? at->secs / m->turn_avg : 0;
            if (at->has_tok) memcpy(T->tok, at->tok, sizeof T->tok);
            /* the page's Math.round(100 * on / max(1, out)), kept to 0..100 (on can exceed a turn's own count) */
            double led = jround(100.0 * (double)T->on_changes / (double)(T->tok[0] > 1 ? T->tok[0] : 1));
            T->led_pct = led > 100 ? 100 : led < 0 ? 0 : (int)led;
            T->reads = reads_of(s, T->cids, T->ncids, &by_rel, &ps, &T->nreads);
            if (T->add + T->del > m->bar_big) m->bar_big = T->add + T->del;
        }
        for (int tu = 0; tu < nt; tu++) {
            StTurn *T = &m->turns[tu];
            T->bar_h = 5 + 17 * sqrt((double)(T->add + T->del) / m->bar_big);
            T->bar_w = 0.4 + sqrt(s->turns[tu].secs / (m->turn_avg ? m->turn_avg : 1));
        }
    }

    for (int i = 0; i < nn; i++) free(sc[i].ages);
    free(sc);
    for (int i = 0; i < b.nt; i++) {
        Tmp *t = &b.t[i];
        free(t->kids);
        if (s->nodes[t->an].kind == AN_DIR) free(t->ch), free(t->rw);
    }
    free(b.t);
    free(tp);
    free(fin);
    free(bfs);
    free(cids);
    free(acc);
    free(share);
    free(runs);
    free(first);
    seen_free(&ts), seen_free(&cs), seen_free(&ps), seen_free(&ws), seen_free(&rws), seen_free(&ns);
    free(rank);
    free(tms);
    strmap_free(&by_rel);
    return m;
}

void st_free(StMetrics *m) {
    if (!m) return;
    for (int i = 0; i < m->nnodes; i++) {
        StNode *d = &m->nodes[i];
        free(d->name);
        free(d->id);
        free(d->kids);
        free(d->ctx_rows);
        free(d->who);
        free(d->partners);
        free(d->trend);
        free(d->blame);
        for (int k = 0; k < d->nrows; k++) free(d->rows[k].notes), free(d->rows[k].tools);
        free(d->rows);
    }
    for (int t = 0; t < m->nturns; t++) {
        StTurn *T = &m->turns[t];
        free(T->cids);
        free(T->files);
        free(T->defs);
        free(T->nodes);
        free(T->reads);
        free(T->notes);
    }
    free(m->nodes);
    free(m->of_an);
    free(m->turns);
    free(m->order);
    free(m->actors);
    strmap_free(&m->by_id);
    strmap_free(&m->by_rel);
    free(m);
}

int st_actor_color(const StMetrics *m, int actor) {
    for (int i = 0; actor >= 0 && i < m->nactors; i++)
        if (m->actors[i] == actor) return i % ST_ACTOR_COLORS;
    return -1;
}

/* ---- JSON (tests/metrics_parity.js reads it) ---- */

static void jd(FILE *f, double v) { fprintf(f, "%.12g", v); } /* stable across libms; parity allows 1e-9 */

static void j_ints(FILE *f, const int *a, int n) {
    fputc('[', f);
    for (int i = 0; i < n; i++) fprintf(f, "%s%d", i ? "," : "", a[i]);
    fputc(']', f);
}

static void j_reads(FILE *f, const StRead *r, int n) {
    fputc('[', f);
    for (int i = 0; i < n; i++) fprintf(f, "%s[%d,%d,%d]", i ? "," : "", r[i].path, r[i].n, r[i].node);
    fputc(']', f);
}

static void j_touch(FILE *f, const StTouch *t, int n) {
    fputc('[', f);
    for (int i = 0; i < n; i++) fprintf(f, "%s[%d,%d,%d]", i ? "," : "", t[i].node, t[i].add, t[i].del);
    fputc(']', f);
}

static void j_mnode(FILE *f, const StNode *d) {
    static const char *kinds[] = {"dir", "file", "function", "class"};
    fputs("{\"id\":", f);
    json_str(f, d->id);
    fputs(",\"name\":", f);
    json_str(f, d->name);
    fprintf(f, ",\"kind\":\"%s\",\"an\":%d,\"parent\":%d,\"kids\":", kinds[d->kind], d->an, d->parent);
    j_ints(f, d->kids, d->nkids);
    fprintf(f, ",\"depth\":%d,\"own\":%d,\"value\":%d,\"lines\":%d,\"cx\":%d,\"nest\":%d", d->depth, d->own, d->value,
            d->lines, d->cx, d->nest);
    fprintf(f, ",\"a\":%d,\"b\":%d,\"file\":%d,\"lang\":", d->a, d->b, d->file);
    if (d->lang) json_str(f, d->lang);
    else fputs("null", f);
    fprintf(f, ",\"ownerSlot\":%d,\"corrTurns\":", d->owner_slot);
    j_ints(f, d->corr_turns, d->ncorr_turns);
    fprintf(f, ",\"turns\":%d,\"edits\":%d,\"add\":%d,\"dels\":%d,\"changes\":%d", d->turns, d->edits, d->add, d->del,
            d->ncids);
    fprintf(f, ",\"rework\":%d,\"reworkSame\":%d,\"reworkPct\":%d,\"rwPairs\":[", d->rework, d->rework_same,
            d->rework_pct);
    for (int k = 0; k < d->nrw_pairs; k++)
        fprintf(f, "%s[%d,%d,%d]", k ? "," : "", d->rw_pairs[k].to, d->rw_pairs[k].by, d->rw_pairs[k].n);
    fprintf(f, "],\"fixTurns\":%d,\"ctxTurns\":%d,\"ctxAvg\":", d->fix_turns, d->ctx_turns);
    jd(f, d->ctx_avg);
    fputs(",\"ctxReads\":", f);
    jd(f, d->ctx_reads);
    fputs(",\"ctxSearches\":", f);
    jd(f, d->ctx_searches);
    fputs(",\"ctxRows\":", f);
    j_reads(f, d->ctx_rows, d->nctx_rows);
    fprintf(f, ",\"tok\":[%lld,%lld,%lld,%lld],\"perTurn\":", d->tok[0], d->tok[1], d->tok[2], d->tok[3]);
    jd(f, d->per_turn);
    fputs(",\"who\":[", f);
    for (int k = 0; k < d->nwho; k++) fprintf(f, "%s[%d,%d]", k ? "," : "", d->who[k].actor, d->who[k].lines);
    fprintf(f, "],\"owner\":%d,\"ageMedian\":", d->owner);
    if (d->age_median < 0) fputs("null", f);
    else fprintf(f, "%lld", d->age_median);
    fputs(",\"ages\":", f);
    j_ints(f, d->ages, ST_NAGES);
    fprintf(f, ",\"flags\":[%d,%d,%d,%d,%d,%d],\"kinds\":%d,\"metric\":[", d->flags.corrected, d->flags.interrupted,
            d->flags.untested, d->flags.failing, d->flags.blind, d->flags.slow, d->kinds);
    for (int k = 0; k < ST_NMODES; k++) {
        if (k) fputc(',', f);
        jd(f, d->metric[k]);
    }
    fputs("],\"heat\":[", f);
    for (int k = 0; k < ST_NMODES; k++) {
        if (k) fputc(',', f);
        jd(f, d->heat[k]);
    }
    fputs("],\"partners\":[", f);
    for (int k = 0; k < d->npartners; k++) {
        const StPartner *p = &d->partners[k];
        fprintf(f, "%s[%d,%d,", k ? "," : "", p->node, p->shared);
        jd(f, p->degree);
        fprintf(f, ",%s,%d]", p->cross ? "true" : "false", p->pct);
    }
    fputs("],\"trend\":[", f);
    for (int k = 0; k < d->ntrend; k++)
        fprintf(f, "%s[%d,%d,%d]", k ? "," : "", d->trend[k].cid, d->trend[k].lines, d->trend[k].cx);
    fprintf(f, "],\"growing\":%s,\"blame\":[", d->growing ? "true" : "false");
    for (int k = 0; k < d->nblame; k++) fprintf(f, "%s[%d,%d]", k ? "," : "", d->blame[k].turn, d->blame[k].n);
    fputs("],\"rows\":[", f);
    for (int k = 0; k < d->nrows; k++) {
        const StNodeTurn *r = &d->rows[k];
        fprintf(f, "%s[%d,%d,%d,%d,%d,", k ? "," : "", r->turn, r->changes, r->add, r->del, r->flags);
        j_ints(f, r->notes, r->nnotes);
        fputs(",[", f);
        for (int j = 0; j < r->ntools; j++) {
            if (j) fputc(',', f);
            json_str(f, r->tools[j]);
        }
        fputs("]]", f);
    }
    fputs("]}", f);
}

void st_json(FILE *f, const StMetrics *m) {
    fputs("{\"order\":", f);
    j_ints(f, m->order, m->nturns);
    fprintf(f, ",\"now\":%lld,\"first\":%lld,\"ageSpan\":%lld,\"avgTime\":[", m->now, m->first, m->age_span);
    for (int k = 0; k < 3; k++) {
        if (k) fputc(',', f);
        jd(f, m->avg_time[k]);
    }
    fputs("],\"turnAvg\":", f);
    jd(f, m->turn_avg);
    fputs(",\"max\":[", f);
    for (int lv = 0; lv < 3; lv++) {
        fputs(lv ? ",[" : "[", f);
        for (int k = 0; k < ST_NMODES; k++) {
            if (k) fputc(',', f);
            jd(f, m->max[lv][k]);
        }
        fputc(']', f);
    }
    fputs("],\"actors\":", f);
    j_ints(f, m->actors, m->nactors);
    fprintf(f, ",\"barBig\":%d,\"ofAn\":", m->bar_big);
    j_ints(f, m->of_an, m->nan);
    fputs(",\"nodes\":[", f);
    for (int i = 0; i < m->nnodes; i++) {
        if (i) fputs(",\n", f);
        j_mnode(f, &m->nodes[i]);
    }
    fputs("],\"turns\":[", f);
    for (int t = 0; t < m->nturns; t++) {
        const StTurn *T = &m->turns[t];
        fprintf(f, "%s{\"rank\":%d,\"cids\":", t ? ",\n" : "", T->rank);
        j_ints(f, T->cids, T->ncids);
        fputs(",\"files\":", f);
        j_touch(f, T->files, T->nfiles);
        fputs(",\"defs\":", f);
        j_touch(f, T->defs, T->ndefs);
        fprintf(f, ",\"add\":%d,\"del\":%d,\"kept\":%d,\"survival\":%d,\"nodes\":", T->add, T->del, T->kept,
                T->survival_pct);
        j_ints(f, T->nodes, T->nnodes);
        fputs(",\"reads\":", f);
        j_reads(f, T->reads, T->nreads);
        fputs(",\"notes\":", f);
        j_ints(f, T->notes, T->nnotes);
        fprintf(f, ",\"untested\":%d,\"failing\":%d,\"tested\":%s,\"blind\":%s,\"slow\":%s,\"slowX\":", T->untested,
                T->failing, T->tested ? "true" : "false", T->blind ? "true" : "false", T->slow ? "true" : "false");
        jd(f, T->slow_x);
        fprintf(f, ",\"tok\":[%lld,%lld,%lld],\"onChanges\":%lld,\"ledPct\":%d,\"agents\":%d,\"subagents\":%s,\"barH\":",
                T->tok[0], T->tok[1], T->tok[2], T->on_changes, T->led_pct, T->nagents, T->subagents ? "true" : "false");
        jd(f, T->bar_h);
        fputs(",\"barW\":", f);
        jd(f, T->bar_w);
        fputc('}', f);
    }
    fputs("]}\n", f);
}

void st_free_pair(StMetrics *m, AnSnapshot *s) {
    st_free(m);
    if (s) an_snapshot_free(s);
}

int st_find(const StMetrics *m, const char *id_or_rel) {
    int i = strmap_get(&m->by_id, id_or_rel);
    return i >= 0 ? i : strmap_get(&m->by_rel, id_or_rel);
}

int *st_line_origins(const StMetrics *m, int node, int *n) {
    *n = 0;
    if (node < 0 || node >= m->nnodes || m->nodes[node].file < 0) return NULL;
    const StNode *d = &m->nodes[node];
    const AnNode *f = &m->snap->nodes[m->nodes[d->file].an];
    int total = 0;
    for (int k = 0; k < f->nblame; k++) total += f->blame[k].n;
    int a = d->kind == AN_FILE ? 1 : d->a < 1 ? 1 : d->a, b = d->kind == AN_FILE || d->b > total ? total : d->b;
    if (b < a) return NULL;
    int *o = xmalloc((size_t)(b - a + 1) * sizeof *o), line = 1;
    for (int k = 0; k < f->nblame && line <= b; k++)
        for (int j = 0; j < f->blame[k].n && line <= b; j++, line++)
            if (line >= a) o[line - a] = f->blame[k].origin;
    *n = b - a + 1;
    return o;
}

int st_blame_slot(const StNode *d, int turn) {
    for (int k = 0; turn >= 0 && k < d->nblame; k++)
        if (d->blame[k].turn == turn) return k % ST_BLAME_COLORS;
    return -1;
}

static int cmp_int_key(const void *k, const void *e) {
    int a = *(const int *)k, b = *(const int *)e;
    return (a > b) - (a < b);
}

bool st_turn_touches(const StTurn *T, int node) {
    return T->nnodes && bsearch(&node, T->nodes, (size_t)T->nnodes, sizeof *T->nodes, cmp_int_key);
}
