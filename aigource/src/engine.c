#include "engine.h"

#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "util.h"

struct Engine {
    Analysis *an;
    EngOpts o;
    pthread_t th;
    bool running, restart;
    pthread_mutex_t mu;
    /* under mu: the worker's results */
    AnSnapshot *new_s;
    void *new_x;
    bool done;
    char stats[256];
    /* the owner's */
    AnSnapshot *snap; /* the latest, derive's result beside it */
    void *x;
    long rev;
    long added, built; /* events counted; added when the last build started (-1: none to stand on) */
    double last_add, first_pending;
};

/* ---- builds: one at a time on a worker thread ------------------------------ */

static void *worker(void *arg) {
    Engine *e = arg;
    AnSnapshot *snap = an_build(e->an);
    void *x = NULL;
    if (snap) {
        char *buf = NULL;
        size_t len = 0;
        FILE *f = open_memstream(&buf, &len);
        if (f) an_snapshot_stats(f, snap), fclose(f);
        pthread_mutex_lock(&e->mu);
        snprintf(e->stats, sizeof e->stats, "%s", buf ? buf : "");
        char *nl = strchr(e->stats, '\n');
        if (nl) *nl = 0;
        pthread_mutex_unlock(&e->mu);
        free(buf);
        if (e->o.derive) x = e->o.derive(snap);
    }
    pthread_mutex_lock(&e->mu);
    e->new_s = snap;
    e->new_x = x;
    e->done = true;
    pthread_mutex_unlock(&e->mu);
    return NULL;
}

static void start_build(Engine *e) {
    if (e->running) return;
    e->built = e->added;
    e->done = false;
    e->restart = false;
    if (pthread_create(&e->th, NULL, worker, e)) {
        perror("aigource: structure build");
        return;
    }
    e->running = true;
}

void eng_request(Engine *e, bool now) {
    if (e->added == e->built && e->snap) return;
    if (!e->running) start_build(e);
    else if (now) an_cancel(e->an, true), e->restart = true;
}

Engine *eng_new(const char *root, const EngOpts *o) {
    Engine *e = xcalloc(1, sizeof *e);
    e->an = an_new(root);
    e->o = *o;
    pthread_mutex_init(&e->mu, NULL);
    e->built = -1;
    return e;
}

static bool counts(EvKind k) {
    return k == EV_CREATE || k == EV_EDIT || k == EV_DELETE || k == EV_TURN || k == EV_TURN_COPY;
}

/* n events that count just arrived */
static void pending(Engine *e, long n) {
    if (!n) return;
    if (e->added == e->built) e->first_pending = now_mono();
    e->added += n;
    e->last_add = now_mono();
}

void eng_add(Engine *e, const Event *evs, size_t n) {
    if (!n) return;
    an_add(e->an, evs, n);
    long k = (long)n;
    if (e->o.changes_only) {
        k = 0;
        for (size_t i = 0; i < n; i++) k += counts(evs[i].kind);
    }
    pending(e, k);
}

void eng_touch(Engine *e) { pending(e, 1); }

void eng_tick(Engine *e) {
    if (!e->snap || e->running || e->added == e->built) return;
    double now = now_mono();
    if (now - e->last_add >= e->o.quiet || now - e->first_pending >= e->o.cap) start_build(e);
}

void eng_drop(Engine *e, AnSnapshot *s, void *x) {
    if (!s && !x) return;
    if (e->o.drop) e->o.drop(s, x);
    else an_snapshot_free(s);
}

bool eng_swap(Engine *e, AnSnapshot **old, void **old_x) {
    *old = NULL, *old_x = NULL;
    pthread_mutex_lock(&e->mu);
    bool done = e->done;
    AnSnapshot *ns = e->new_s;
    void *nx = e->new_x;
    if (done) e->done = false, e->new_s = NULL, e->new_x = NULL;
    pthread_mutex_unlock(&e->mu);
    if (!done) return false;
    pthread_join(e->th, NULL);
    e->running = false;
    an_cancel(e->an, false);
    if (!ns || (e->o.derive && !nx)) { /* cancelled */
        eng_drop(e, ns, nx);
        e->built = -1;
        if (e->restart) start_build(e);
        return false;
    }
    *old = e->snap, *old_x = e->x;
    e->snap = ns, e->x = nx;
    e->rev++;
    if (e->restart) start_build(e);
    return true;
}

void eng_free(Engine *e) {
    if (!e) return;
    if (e->running) an_cancel(e->an, true), pthread_join(e->th, NULL);
    eng_drop(e, e->new_s, e->new_x);
    eng_drop(e, e->snap, e->x);
    an_free(e->an);
    pthread_mutex_destroy(&e->mu);
    free(e);
}

AnSnapshot *eng_snap(const Engine *e, void **x) {
    if (x) *x = e->x;
    return e->snap;
}

long eng_rev(const Engine *e) { return e->rev; }
bool eng_running(const Engine *e) { return e->running; }
long eng_added(const Engine *e) { return e->added; }

void eng_stats(Engine *e, char *b, size_t n) {
    pthread_mutex_lock(&e->mu);
    snprintf(b, n, "%s", e->stats);
    pthread_mutex_unlock(&e->mu);
}
