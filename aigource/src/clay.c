#include "clay.h"

#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "util.h"

/* ---- what a call shows -------------------------------------------------------- */

int cl_shown(const CtxAgent *a, int c, const int *by_item, int *out) {
    const CtxCall *C = &a->calls[c];
    int n = 0;
    for (int i = 0; i < C->norder; i++)
        if (by_item[C->order[i]] == i) out[n++] = C->order[i]; /* once, at its first place */
    for (int i = 0; i < C->nmade; i++)
        if (by_item[C->made[i]] < 0) out[n++] = C->made[i];
    return n;
}

int cl_linked(const CtxAgent *a, int sel, int c, unsigned char *linked) {
    memset(linked, 0, (size_t)a->nitems);
    if (sel < 0 || sel >= a->nitems || a->items[sel].made != c) return 0;
    const CtxItem *it = &a->items[sel];
    int n = 0;
    for (int k = 0; k < it->nlk; k++) n += !linked[it->lk[k].item], linked[it->lk[k].item] = 1;
    n += !linked[sel], linked[sel] = 1;
    return n;
}

bool cl_open_default(const CtxItem *it, int c, bool linked, int nlinked) {
    if (linked) return true;
    if (it->kind == CK_SYSTEM || it->kind == CK_TOOLS) return false;
    if (it->made == c) return true; /* this call's response */
    if (nlinked) return false;      /* an action is selected: its causes are what opens */
    return it->enter >= 0 && it->enter >= c - 1 && it->enter <= c;
}

typedef struct {
    const CtxAgent *a;
    const int *shown;
    const unsigned char *linked;
    int c;
} RankCmp;

/* this call's response first, then the linked, then the newest entered, then the order shown */
static int cmp_rank(const void *x, const void *y, void *ud) {
    const RankCmp *o = ud;
    int i = *(const int *)x, j = *(const int *)y, p = o->shown[i], q = o->shown[j];
    int rp = o->a->items[p].made == o->c, rq = o->a->items[q].made == o->c;
    if (rp != rq) return rq - rp;
    if (o->linked[p] != o->linked[q]) return o->linked[q] - o->linked[p];
    if (o->a->items[p].enter != o->a->items[q].enter) return o->a->items[q].enter - o->a->items[p].enter;
    return i - j;
}

void cl_open(const CtxAgent *a, int c, const int *shown, int n, const signed char *ov, const unsigned char *linked,
             int nlinked, bool *open) {
    int *def = xmalloc(sizeof *def * (size_t)n), nd = 0;
    for (int k = 0; k < n; k++) {
        int i = shown[k];
        open[k] = ov[i] >= 0 ? ov[i] == 1 : cl_open_default(&a->items[i], c, linked[i], nlinked);
        if (open[k] && ov[i] < 0 && !linked[i]) def[nd++] = k;
    }
    if (nd > CL_OPEN_MAX) { /* a busy call stays readable: the newest stay open (none is linked) */
        RankCmp o = {a, shown, linked, c};
        qsort_r(def, (size_t)nd, sizeof *def, cmp_rank, &o);
        for (int k = CL_OPEN_MAX; k < nd; k++) open[def[k]] = false;
    }
    free(def);
}

void cl_title(const CtxItem *it, char *b, size_t n) {
    snprintf(b, n, "%s%s", it->kind == CK_RESULT && it->err ? "error \xC2\xB7 " : "", it->src ? it->src : "");
}

static bool tool_is(const CtxItem *it, const char *t) { return it->tool && !strcmp(it->tool, t); }

void cl_size(const CtxItem *it, bool open, float *w, float *h) {
    if (!open) {
        char t[1024];
        cl_title(it, t, sizeof t);
        *w = fminf(300, 64 + (float)utf8_count(t, sizeof t) * 6.6f + 44), *h = CL_CHIP_H;
    } else if (it->kind == CK_RESULT && (tool_is(it, "Read") || tool_is(it, "Bash") || tool_is(it, "Grep")))
        *w = 460, *h = 270;
    else if (it->kind == CK_ACTION && (tool_is(it, "Edit") || tool_is(it, "Write")))
        *w = 440, *h = 240;
    else if (it->kind == CK_PROMPT || it->kind == CK_SUMMARY || it->kind == CK_TEXT)
        *w = 380, *h = 210;
    else if (it->kind == CK_THINKING)
        *w = 340, *h = 150;
    else
        *w = 360, *h = 180;
}

void cl_targets(const CtxAgent *a, int c, const int *shown, int n, const unsigned char *linked, LWin *w) {
    int *ix = xmalloc(sizeof *ix * (size_t)n);
    for (int k = 0; k < n; k++) ix[k] = k;
    RankCmp o = {a, shown, linked, c};
    qsort_r(ix, (size_t)n, sizeof *ix, cmp_rank, &o);
    const float r0 = 92;
    float area = 0;
    for (int r = 0; r < n; r++) {
        LWin *m = &w[ix[r]];
        float ar = (m->w + 14) * (m->h + 14);
        m->rank = r;
        m->tr = sqrtf(r0 * r0 + (area + ar / 2) / ((float)M_PI * 0.4f)) + m->h / 4;
        area += ar;
    }
    free(ix);
}

/* ---- the calls strip ------------------------------------------------------------- */

ClStrip cl_strip(int n, float w) {
    float pi = n > 300 ? 0 : 0.15f;
    ClStrip s = {n, w, 0, 0};
    s.step = w / fmaxf(1, (float)n - pi);
    s.bw = s.step * (1 - pi);
    return s;
}

/* d3's band: the bands centred in the range (it matters only for one call) */
float cl_strip_x(const ClStrip *s, int i) {
    float pi = s->n > 300 ? 0 : 0.15f;
    return (s->w - s->step * ((float)s->n - pi)) * 0.5f + s->step * (float)i;
}

/* the draft's scrubAt: by step from the strip's left */
int cl_strip_hit(const ClStrip *s, float x) {
    if (s->n < 1) return -1;
    int i = (int)floorf(x / s->step);
    return i < 0 ? 0 : i >= s->n ? s->n - 1 : i;
}

/* ---- a window's header ------------------------------------------------------------ */

void cl_facts(const CtxAgent *a, int item, int c, const int *by_item, ClFacts *f) {
    const CtxItem *it = &a->items[item];
    const CtxCall *C = &a->calls[c];
    int pos = by_item[item];
    memset(f, 0, sizeof *f);
    f->enter = -1;
    f->state = CS_NONE;
    f->in_order = pos >= 0;
    f->tok = pos >= 0 ? ctx_tokens(a, c, pos) : it->w;
    for (int k = 0; k < C->ndrop; k++) f->dropped |= C->drop[k] == item;
    if (pos < 0) return;
    f->share = C->total > 0 ? f->tok / C->total : 0;
    if (it->enter >= 0) {
        f->enter = it->enter;
        f->age_calls = c - it->enter;
        f->age_s = C->t - a->calls[it->enter].t;
    }
    f->state = C->state[pos];
    f->priced = ctx_price(C->model) != NULL;
    f->cost = C->cum[pos];
    int known = 0;
    if (it->nst) f->nstale = ctx_stale_at(a, item, c, by_item, &known);
    f->known = known;
}

void cl_fmt_k(char *b, size_t n, double v) {
    if (v >= 1e6) snprintf(b, n, "%.*fM", v >= 1e7 ? 0 : 1, jfix(v / 1e6, v >= 1e7 ? 0 : 1));
    else if (v >= 1e3) snprintf(b, n, "%.*fk", v >= 1e4 ? 0 : 1, jfix(v / 1e3, v >= 1e4 ? 0 : 1));
    else snprintf(b, n, "%.0f", floor(v + 0.5));
}

void cl_fmt_usd(char *b, size_t n, double v) {
    if (v >= 1) snprintf(b, n, "$%.2f", jfix(v, 2));
    else if (v >= 0.01) snprintf(b, n, "$%.3f", jfix(v, 3));
    else if (v > 0) snprintf(b, n, "$%.4f", jfix(v, 4));
    else snprintf(b, n, "$0");
}

void cl_fmt_min(char *b, size_t n, double s) {
    if (s < 90) snprintf(b, n, "%.0f s", floor(s + 0.5));
    else if (s < 5400) snprintf(b, n, "%.0f min", floor(s / 60 + 0.5));
    else snprintf(b, n, "%.1f h", jfix(s / 3600, 1));
}

void cl_fmt_pct(char *b, size_t n, double share) {
    int d = share < 0.01 ? 2 : 1;
    snprintf(b, n, "%.*f%%", d, jfix(share * 100, d));
}

static void run(ClRun *r, int *n, bool part, unsigned char style, const char *fmt, ...)
    __attribute__((format(printf, 5, 6)));
static void run(ClRun *r, int *n, bool part, unsigned char style, const char *fmt, ...) {
    if (*n >= CL_RUNS - 1) return;
    if (part && *n) r[(*n)++] = (ClRun){" \xC2\xB7 ", CLS_FAINT, false}; /* parts joined by " · " */
    ClRun *x = &r[(*n)++];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(x->s, sizeof x->s, fmt, ap);
    va_end(ap);
    x->style = style, x->brk = part;
}

int cl_meta(const CtxAgent *a, const ClFacts *f, ClRun *r) {
    static const char *const state[] = {"read", "written", "new", "?"};
    static const unsigned char sstyle[] = {CLS_READ, CLS_WRITTEN, CLS_NEW, CLS_FAINT};
    char k[32], x[32];
    int n = 0;
    cl_fmt_k(k, sizeof k, f->tok);
    run(r, &n, true, CLS_DIM, "%s tok", k);
    if (f->in_order) cl_fmt_pct(x, sizeof x, f->share), run(r, &n, false, CLS_FAINT, " \xC2\xB7 %s", x);
    else run(r, &n, false, CLS_FAINT, " out \xC2\xB7 this call's response");
    if (f->enter >= 0) {
        fmt_clock(x, sizeof x, a->calls[f->enter].t, "%H:%M");
        run(r, &n, true, CLS_FAINT, "in #%d %s", f->enter, x);
        cl_fmt_min(x, sizeof x, f->age_s);
        run(r, &n, true, CLS_FAINT, "age %d calls \xC2\xB7 %s", f->age_calls, x);
    }
    if (f->in_order) {
        int s = f->state >= CS_READ && f->state <= CS_NONE ? f->state : CS_NONE;
        run(r, &n, true, CLS_FAINT, "cache ");
        run(r, &n, false, sstyle[s], "%s", state[s]);
        if (f->priced) cl_fmt_usd(x, sizeof x, f->cost), run(r, &n, true, CLS_FAINT, "%s carried", x);
    }
    if (f->nstale) run(r, &n, true, f->known ? CLS_DIM : CLS_WARN, f->known ? "stale \xC2\xB7 known" : "stale \xE2\x9A\xA0");
    if (f->dropped) run(r, &n, true, CLS_WARN, "dropped by the API at this call");
    return n;
}

void cl_stale_text(const CtxAgent *a, int item, int nstale, char *b, size_t n) {
    static const char *const by[] = {"its own edit", "its own command", "another agent of this session",
                                     "another session", "the user or a program (Claude Code noticed)"};
    const CtxItem *it = &a->items[item];
    size_t o = 0;
    b[0] = 0;
    for (int k = 0; k < nstale && k < it->nst && o + 1 < n; k++) {
        char t[16];
        int kind = it->st[k].kind >= 0 && it->st[k].kind <= CST_NOTICED ? it->st[k].kind : CST_NOTICED;
        fmt_clock(t, sizeof t, it->st[k].t, "%H:%M");
        int w = snprintf(b + o, n - o, "%s%s %s (%s)", k ? "\n" : "", t, it->st[k].label ? it->st[k].label : "", by[kind]);
        if (w < 0) break;
        o += (size_t)w < n - o ? (size_t)w : n - o - 1;
    }
}

/* ---- the side panel and the strip's words ----------------------------------------- */

void cl_by_kind(const CtxAgent *a, int c, double *by) {
    const CtxCall *C = &a->calls[c];
    for (int k = 0; k < CK_COUNT; k++) by[k] = 0;
    for (int p = 0; p < C->norder; p++) by[a->items[C->order[p]].kind] += ctx_tokens(a, c, p);
}

void cl_inout(const CtxAgent *a, int c, int *in, int *out) {
    const CtxCall *C = &a->calls[c];
    int b = C->base >= 0 ? C->base : c - 1; /* no base: against the call before */
    unsigned char *mark = xcalloc((size_t)a->nitems, 1);
    if (b >= 0)
        for (int p = 0; p < a->calls[b].norder; p++) mark[a->calls[b].order[p]] = 1;
    *in = *out = 0;
    for (int p = 0; p < C->norder; p++) {
        int i = C->order[p];
        if (!(mark[i] & 2)) *in += !(mark[i] & 1), mark[i] |= 2;
    }
    if (b >= 0)
        for (int p = 0; p < a->calls[b].norder; p++) {
            int i = a->calls[b].order[p];
            if (mark[i] == 1) ++*out, mark[i] = 4;
        }
    free(mark);
}

static int cmp_action(const void *x, const void *y, void *ud) {
    const CtxAgent *a = ud;
    int i = *(const int *)x, j = *(const int *)y;
    return a->items[i].made != a->items[j].made ? a->items[i].made - a->items[j].made : i - j;
}

int cl_actions(const CtxAgent *a, int *out) {
    int n = 0;
    for (int i = 0; i < a->nitems; i++)
        if (a->items[i].kind == CK_ACTION && a->items[i].made >= 0) out[n++] = i;
    qsort_r(out, (size_t)n, sizeof *out, cmp_action, (void *)a);
    return n;
}

bool cl_match(const CtxItem *it, const char *q) { return !*q || (it->src && strcasestr(it->src, q)); }

static int cmp_key(const void *x, const void *y, void *ud) {
    const CtxItem *it = ud;
    uint64_t a = it[*(const int *)x].key, b = it[*(const int *)y].key;
    return (a > b) - (a < b);
}

/* nw's items sorted by key, each of old's looked up in them */
void cl_key_map(const CtxAgent *old, const CtxAgent *nw, int *map) {
    int *ix = xmalloc(sizeof *ix * (size_t)nw->nitems);
    for (int j = 0; j < nw->nitems; j++) ix[j] = j;
    qsort_r(ix, (size_t)nw->nitems, sizeof *ix, cmp_key, nw->items);
    for (int i = 0; i < old->nitems; i++) {
        uint64_t k = old->items[i].key;
        int lo = 0, hi = nw->nitems - 1;
        map[i] = -1;
        while (lo <= hi) {
            int mid = (lo + hi) / 2;
            uint64_t m = nw->items[ix[mid]].key;
            if (m == k) { map[i] = ix[mid]; break; }
            if (m < k) lo = mid + 1;
            else hi = mid - 1;
        }
    }
    free(ix);
}

void cl_carry(const int *map, int nold, const ClUser *from, ClUser *to) {
    for (int i = 0; i < nold; i++) {
        int j = map[i];
        if (j < 0) continue;
        to->ov[j] = from->ov[i];
        to->pin[2 * j] = from->pin[2 * i], to->pin[2 * j + 1] = from->pin[2 * i + 1];
        to->size[2 * j] = from->size[2 * i], to->size[2 * j + 1] = from->size[2 * i + 1];
    }
    to->sel = from->sel >= 0 && from->sel < nold ? map[from->sel] : -1;
}

/* by the latest time up to each call: a call recorded out of time order doesn't end the walk early */
int cl_call_at(const CtxAgent *a, double t) {
    if (a->ncalls < 1) return CL_NO_CALL;
    if (t <= 0) return a->ncalls - 1;
    double m = -INFINITY;
    int k = CL_NO_CALL;
    for (int i = 0; i < a->ncalls; i++) {
        m = fmax(m, a->calls[i].t);
        if (m > t) break;
        k = i;
    }
    return k;
}

int cl_follow(const CtxAgent *a, bool follow, bool playing, double t, int cur) {
    return follow && !playing ? cl_call_at(a, t) : cur;
}

/* ---- the worker's jobs ------------------------------------------------------------- */

static bool same(const char *a, const char *b) { return a && b && !strcmp(a, b); }

int cl_open_job(const ClWorker *w, const char *path) {
    bool busy = w->loading != NULL;
    if (busy && !w->reload && !w->cancelled && same(w->loading, path)) return CL_OPEN_KEEP;
    if (same(w->shown, path)) return busy && !w->reload ? CL_OPEN_SHOWN_DROP : CL_OPEN_SHOWN;
    return busy ? CL_OPEN_QUEUE : CL_OPEN_LOAD;
}

int cl_job_done(const ClDone *d) {
    if (d->newer) return CL_JOB_DROP;                        /* another agent asked for: it loads next */
    if (d->cancelled) return d->reload ? CL_JOB_RETRY : CL_JOB_DROP;
    if (!d->ok) return d->reload ? CL_JOB_RELOAD_FAIL : CL_JOB_FAIL;
    return d->reload || d->same ? CL_JOB_KEEP : CL_JOB_INSTALL;
}
