#include "engine.h"

#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "util.h"

struct Engine {
    Analysis *an;
    EngOpts o;
    pthread_t th;
    bool running, restart;
    pthread_mutex_t mu;
    /* under mu: the worker's results */
    AnSnapshot *new_s;
    void *new_x[ENG_PARTS];
    bool done;
    char stats[256];
    /* the owner's */
    AnSnapshot *snap; /* the latest, its parts beside it */
    void *x[ENG_PARTS];
    long rev;
    /* per cadence (0 changes, 1 any): events counted; added when the last build
     * started (-1: none to stand on); the last one's time, the first waiting's */
    struct { long added, built; double last, first; } c[2];
    bool any_on;
    double delay;      /* AIG_ENG_DELAY_FIRST: the first build waits this long (a test's window) */
    atomic_bool stop;  /* eng_free: the delay ends */
};

/* ---- builds: one at a time on a worker thread ------------------------------ */

static void *worker(void *arg) {
    Engine *e = arg;
    for (double until = now_mono() + e->delay; e->delay > 0 && now_mono() < until && !atomic_load(&e->stop);)
        usleep(20000);
    e->delay = 0;
    AnSnapshot *snap = an_build(e->an);
    void *x[ENG_PARTS] = {0};
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
        for (int k = 0; k < ENG_PARTS; k++)
            if (e->o.part[k].derive) x[k] = e->o.part[k].derive(snap);
    }
    pthread_mutex_lock(&e->mu);
    e->new_s = snap;
    memcpy(e->new_x, x, sizeof x);
    e->done = true;
    pthread_mutex_unlock(&e->mu);
    return NULL;
}

static void start_build(Engine *e) {
    if (e->running) return;
    for (int k = 0; k < 2; k++) e->c[k].built = e->c[k].added;
    e->done = false;
    e->restart = false;
    if (pthread_create(&e->th, NULL, worker, e)) {
        perror("aigource: structure build");
        return;
    }
    e->running = true;
}

void eng_request(Engine *e, bool now) {
    if (e->c[1].added == e->c[1].built && e->snap) return;
    if (!e->running) start_build(e);
    else if (now) an_cancel(e->an, true), e->restart = true;
}

Engine *eng_new(const char *root, const EngOpts *o) {
    Engine *e = xcalloc(1, sizeof *e);
    e->an = an_new(root);
    e->o = *o;
    pthread_mutex_init(&e->mu, NULL);
    e->c[0].built = e->c[1].built = -1;
    e->any_on = o->any.quiet > 0;
    const char *d = getenv("AIG_ENG_DELAY_FIRST");
    e->delay = d ? atof(d) : 0;
    return e;
}

static bool counts(EvKind k) {
    return k == EV_CREATE || k == EV_EDIT || k == EV_DELETE || k == EV_TURN || k == EV_TURN_COPY;
}

/* n events that count for cadence k just arrived */
static void pending(Engine *e, int k, long n) {
    if (!n) return;
    if (e->c[k].added == e->c[k].built) e->c[k].first = now_mono();
    e->c[k].added += n;
    e->c[k].last = now_mono();
}

void eng_add(Engine *e, const Event *evs, size_t n) {
    if (!n) return;
    an_add(e->an, evs, n);
    long k = 0;
    for (size_t i = 0; i < n; i++) k += counts(evs[i].kind);
    pending(e, 0, k);
    pending(e, 1, (long)n);
}

void eng_touch(Engine *e) { pending(e, 0, 1), pending(e, 1, 1); }

void eng_any(Engine *e, bool on) { e->any_on = on && e->o.any.quiet > 0; }

void eng_tick(Engine *e) {
    if (!e->snap || e->running) return;
    double now = now_mono();
    for (int k = 0; k < 2; k++) {
        const EngCadence *cd = k ? &e->o.any : &e->o.changes;
        if (cd->quiet <= 0 || (k && !e->any_on) || e->c[k].added == e->c[k].built) continue;
        if (now - e->c[k].last >= cd->quiet || now - e->c[k].first >= cd->cap) {
            start_build(e);
            return;
        }
    }
}

void eng_drop(Engine *e, AnSnapshot *s, void **x) {
    for (int k = 0; k < ENG_PARTS; k++)
        if (x[k] && e->o.part[k].drop) e->o.part[k].drop(x[k]);
    an_snapshot_free(s);
}

bool eng_swap(Engine *e, AnSnapshot **old, void **old_x) {
    *old = NULL;
    memset(old_x, 0, sizeof(void *) * ENG_PARTS);
    pthread_mutex_lock(&e->mu);
    bool done = e->done;
    AnSnapshot *ns = e->new_s;
    void *nx[ENG_PARTS];
    memcpy(nx, e->new_x, sizeof nx);
    if (done) e->done = false, e->new_s = NULL, memset(e->new_x, 0, sizeof e->new_x);
    pthread_mutex_unlock(&e->mu);
    if (!done) return false;
    pthread_join(e->th, NULL);
    e->running = false;
    an_cancel(e->an, false);
    bool whole = ns != NULL;
    for (int k = 0; k < ENG_PARTS; k++) whole &= !e->o.part[k].derive || nx[k];
    if (!whole) { /* cancelled */
        eng_drop(e, ns, nx);
        e->c[0].built = e->c[1].built = -1;
        if (e->restart) start_build(e);
        return false;
    }
    *old = e->snap;
    memcpy(old_x, e->x, sizeof e->x);
    e->snap = ns;
    memcpy(e->x, nx, sizeof nx);
    e->rev++;
    if (e->restart) start_build(e);
    return true;
}

void eng_free(Engine *e) {
    if (!e) return;
    atomic_store(&e->stop, true);
    if (e->running) an_cancel(e->an, true), pthread_join(e->th, NULL);
    eng_drop(e, e->new_s, e->new_x);
    eng_drop(e, e->snap, e->x);
    an_free(e->an);
    pthread_mutex_destroy(&e->mu);
    free(e);
}

AnSnapshot *eng_snap(const Engine *e) { return e->snap; }
void *eng_part(const Engine *e, int k) { return e->x[k]; }

long eng_rev(const Engine *e) { return e->rev; }
bool eng_running(const Engine *e) { return e->running; }
long eng_added(const Engine *e) { return e->c[1].added; }

void eng_stats(Engine *e, char *b, size_t n) {
    pthread_mutex_lock(&e->mu);
    snprintf(b, n, "%s", e->stats);
    pthread_mutex_unlock(&e->mu);
}
