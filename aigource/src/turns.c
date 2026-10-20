#include "turns.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "analysis.h"
#include "metrics.h"
#include "ui.h"
#include "util.h"

enum { R_PROMPT, R_CHANGE, R_USAGE, R_INTR, R_RUN };

typedef struct {
    double ts;
    unsigned long seq;
    int kind, actor, add, del;
    bool err;   /* a run: failed */
    char *text; /* a prompt: its first line */
} Rec;

struct TurnStats {
    char *root;
    Rec *r;
    size_t n, cap;
    StrMap ix;  /* actor id -> index */
    char **id;
    int *parent; /* a subagent's session, -1 a session */
    int nid, capid;
    bool dirty;
    TurnStat *t;
    int nt, big;
};

typedef struct { /* a prompt while computing */
    const Rec *p;
    double end, run_ts;
    bool run, run_err, intr;
    int add, del;
    double *stamps, *chg; /* usage stamps; its changes' times */
    int nstamps, capstamps, nchg, capchg;
} Pr;

#define GROW(a, n, cap)                                                   \
    do {                                                                  \
        if ((n) == (cap)) {                                               \
            (cap) = (cap) ? (cap)*2 : 16;                                 \
            (a) = xrealloc((a), (size_t)(cap) * sizeof *(a));             \
        }                                                                 \
    } while (0)

TurnStats *ts_new(const char *root) {
    TurnStats *t = xcalloc(1, sizeof *t);
    t->root = xstrdup(root ? root : ".");
    return t;
}

static void drop_turns(TurnStats *t) {
    for (int i = 0; i < t->nt; i++) free(t->t[i].title), free(t->t[i].actor);
    free(t->t);
    t->t = NULL, t->nt = 0;
}

void ts_free(TurnStats *t) {
    if (!t) return;
    drop_turns(t);
    for (size_t i = 0; i < t->n; i++) free(t->r[i].text);
    free(t->r);
    for (int i = 0; i < t->nid; i++) free(t->id[i]);
    free(t->id), free(t->parent);
    strmap_free(&t->ix);
    free(t->root);
    free(t);
}

static int intern(TurnStats *t, const char *id) {
    int k = strmap_get(&t->ix, id);
    if (k >= 0) return k;
    if (t->nid == t->capid) {
        t->capid = t->capid ? t->capid * 2 : 16;
        t->id = xrealloc(t->id, (size_t)t->capid * sizeof *t->id);
        t->parent = xrealloc(t->parent, (size_t)t->capid * sizeof *t->parent);
    }
    t->id[t->nid] = xstrdup(id);
    t->parent[t->nid] = -1;
    strmap_put(&t->ix, id, t->nid);
    return t->nid++;
}

void ts_add(TurnStats *t, const Event *evs, size_t n) {
    for (size_t i = 0; i < n; i++) {
        const Event *e = &evs[i];
        if (!e->actor) continue;
        Rec r = {.ts = e->ts, .seq = e->seq, .kind = -1};
        switch (e->kind) {
        case EV_TURN:
        case EV_TURN_COPY:
            if (e->text && (e->prompt == PROMPT_HUMAN || e->prompt == PROMPT_PASTED)) r.kind = R_PROMPT;
            break;
        case EV_CREATE:
        case EV_EDIT:
        case EV_DELETE:
            if (e->via == VIA_TRANSCRIPT && e->path) r.kind = R_CHANGE, r.add = e->add, r.del = e->del;
            break;
        case EV_USAGE: r.kind = R_USAGE; break;
        case EV_INTERRUPT: r.kind = R_INTR; break;
        case EV_BASH_END: {
            if (e->not_run || !e->text) break; /* never ran: no verdict */
            char *start = NULL; /* where it began, as the analysis takes it */
            if (e->path && *e->path != '/') {
                start = xmalloc(strlen(t->root) + strlen(e->path) + 2);
                sprintf(start, "%s/%s", t->root, e->path);
            }
            if (an_verify_cmd(e->text, t->root, start ? start : e->path)) r.kind = R_RUN, r.err = e->err || e->failed_out;
            free(start);
            break;
        }
        case EV_TITLE:
            if (e->who.session && *e->who.session) {
                int sub = intern(t, e->actor), s = intern(t, e->who.session); /* both before parent moves */
                if (s != sub) t->parent[sub] = s;
            }
            break;
        default: break;
        }
        if (r.kind < 0) continue;
        r.actor = intern(t, e->actor);
        if (r.kind == R_PROMPT) r.text = xstrdup(e->text);
        GROW(t->r, t->n, t->cap);
        t->r[t->n++] = r;
        t->dirty = true;
    }
}

static int cmp_rec(const void *pa, const void *pb) {
    const Rec *a = pa, *b = pb;
    if (a->ts != b->ts) return a->ts < b->ts ? -1 : 1;
    return a->seq < b->seq ? -1 : a->seq > b->seq;
}

static int cmp_turn(const void *pa, const void *pb) {
    const TurnStat *a = pa, *b = pb;
    return a->ts < b->ts ? -1 : a->ts > b->ts;
}

static void compute(TurnStats *t) {
    drop_turns(t);
    t->big = 1;
    qsort(t->r, t->n, sizeof *t->r, cmp_rec);
    int ns = t->nid;
    int *sess = xmalloc((size_t)(ns ? ns : 1) * sizeof *sess);
    for (int a = 0; a < ns; a++) sess[a] = t->parent[a] >= 0 ? t->parent[a] : a;
    /* prompts per session, in time order (records are), a repeat (same time and text) once */
    int *cnt = xcalloc((size_t)ns + 1, sizeof *cnt), *at = xcalloc((size_t)ns + 1, sizeof *at);
    for (size_t i = 0; i < t->n; i++)
        if (t->r[i].kind == R_PROMPT && t->parent[t->r[i].actor] < 0) cnt[t->r[i].actor + 1]++;
    for (int a = 0; a < ns; a++) cnt[a + 1] += cnt[a];
    int np = cnt[ns];
    Pr *pr = xcalloc((size_t)(np ? np : 1), sizeof *pr);
    for (size_t i = 0; i < t->n; i++) {
        const Rec *r = &t->r[i];
        if (r->kind != R_PROMPT || t->parent[r->actor] >= 0) continue;
        int a = r->actor, k = cnt[a] + at[a];
        if (at[a] && pr[k - 1].p->ts == r->ts && !strcmp(pr[k - 1].p->text, r->text)) continue;
        pr[k].p = r, pr[k].end = r->ts, at[a]++;
    }
    for (size_t i = 0; i < t->n; i++) {
        const Rec *r = &t->r[i];
        if (r->kind == R_PROMPT) continue;
        int s = sess[r->actor], lo = cnt[s], hi = cnt[s] + at[s]; /* the latest prompt at or before it */
        while (lo < hi) {
            int mid = (lo + hi) / 2;
            if (pr[mid].p->ts <= r->ts) lo = mid + 1;
            else hi = mid;
        }
        if (lo == cnt[s]) continue; /* before the session's first prompt */
        Pr *p = &pr[lo - 1];
        switch (r->kind) {
        case R_CHANGE:
            GROW(p->chg, p->nchg, p->capchg);
            p->chg[p->nchg++] = r->ts;
            p->add += r->add, p->del += r->del;
            p->end = fmax(p->end, r->ts);
            break;
        case R_USAGE:
            GROW(p->stamps, p->nstamps, p->capstamps);
            p->stamps[p->nstamps++] = r->ts;
            p->end = fmax(p->end, r->ts);
            break;
        case R_INTR: p->intr = true; break;
        case R_RUN: p->run = true, p->run_ts = r->ts, p->run_err = r->err; break; /* the last one counts */
        }
    }
    int cap = 0;
    for (int s = 0; s < ns; s++)
        for (int k = cnt[s]; k < cnt[s] + at[s]; k++) {
            Pr *p = &pr[k];
            if (!p->nchg) continue;
            TurnStat u = {.ts = p->p->ts, .end = p->end, .add = p->add, .del = p->del, .changes = p->nchg, .intr = p->intr};
            u.fix = an_fix_prompt(p->p->text);
            u.corr = k + 1 < cnt[s] + at[s] && an_corr_prompt(pr[k + 1].p->text);
            for (int c = 0; c < p->nchg; c++) { /* the last run after the change */
                if (!p->run || p->run_ts <= p->chg[c]) u.untested++;
                else if (p->run_err) u.failing++;
            }
            GROW(p->stamps, p->nstamps, p->capstamps);
            p->stamps[p->nstamps++] = p->p->ts;
            u.secs = an_active(p->stamps, p->nstamps);
            u.title = xstrdup(p->p->text);
            u.actor = xstrdup(t->id[s]);
            if (u.add + u.del > t->big) t->big = u.add + u.del;
            GROW(t->t, t->nt, cap);
            t->t[t->nt++] = u;
        }
    if (t->nt) qsort(t->t, (size_t)t->nt, sizeof *t->t, cmp_turn);
    double sum = 0;
    for (int i = 0; i < t->nt; i++) sum += (double)t->t[i].secs;
    double avg = t->nt ? sum / t->nt : 0;
    for (int i = 0; i < t->nt; i++) {
        TurnStat *u = &t->t[i];
        u->slow = avg > 0 && u->secs >= ST_SLOW_X * avg;
        u->tags = (u->fix ? UT_FIX : 0) | (u->corr || u->intr ? UT_CORR : 0) | (u->failing ? UT_FAILING : 0) |
                  (u->untested ? UT_UNTESTED : 0) | (u->slow ? UT_SLOW : 0);
    }
    for (int k = 0; k < np; k++) free(pr[k].stamps), free(pr[k].chg);
    free(pr), free(cnt), free(at), free(sess);
}

const TurnStat *ts_get(TurnStats *t, int *n, int *big) {
    if (t->dirty) compute(t), t->dirty = false;
    *n = t->nt;
    *big = t->big;
    return t->t;
}
