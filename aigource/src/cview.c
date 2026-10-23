#include "cview.h"

#include <limits.h>
#include <math.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#include "cview_int.h"
#include "nvim.h"
#include "util.h"

/* Bands of the screen, as the draft's window: the bar (agent, stats), the
 * stage beside the side panel, the calls strip, the hint row. */
#define BAR_H 44.0f
#define STRIP_H 66.0f
#define BARS_H 54.0f /* the strip's tallest bar */
#define HINT_H 30.0f
#define SIDE_W 330.0f
#define ACT_ROW 20.0f
#define WHY_ROW 19.0f
#define PICK_ROW 26.0f
#define FAST 0.15      /* calls shown closer together than this: no animation (a scrub) */
#define REST 0.18      /* ...and the full layout once the scrub rests this long */
#define PLAY_STEP 0.65 /* playing: a call this often */
#define POLL 0.5          /* live: the transcript's size looked at this often; */
#define DEBOUNCE 3.0      /* reloaded this long after it last grew, */
#define DEBOUNCE_MAX 15.0 /* or once growth has waited this long (as the structure screen's rebuilds) */
#define K_MIN 0.05f
#define K_MAX 2.5f

enum { D_NONE, D_EAT, D_PAN, D_MOVE, D_RESIZE, D_SCRUB };
enum { FIT_RECENT, FIT_ALL, FIT_LINKS };

/* An open's ask (cv_open's): the transcript, where to open it. */
typedef struct {
    char *path;
    int call, sel;
    double t;
} Ask;

/* A job's result: handed over under mu. */
typedef struct {
    CtxAgent *a;
    CtxRef *refs;
    int nrefs;
    char *err;
    double ms; /* how long it took */
} Result;

struct CView {
    char *root;
    CtxProject *proj;
    /* the worker: one job at a time, a load or a reload of the agent shown */
    pthread_t th;
    bool running;
    atomic_int cancel;
    pthread_mutex_t mu;
    Ask load;                /* the transcript it reads, where to open it */
    const CtxAgent *reloading; /* a reload: of this agent (the one shown, kept until it's in); NULL a load */
    long long load_size;     /* the transcript's size and time when the job started */
    double load_mtime;
    bool done;               /* under mu: its result is in */
    Result res;
    /* the latest ask, loaded once the worker is free */
    bool want_due;
    Ask want;
    /* the agent shown */
    CtxAgent *a;
    CtxRef *refs;
    int nrefs;
    char *err;             /* the last load failed (failed): why */
    bool failed;
    char *reload_err;      /* the last live reload failed: why (the agent stays) */
    bool open;
    /* live (C3): the transcript as last loaded, and growth seen since */
    long long src_size, seen_size;
    double src_mtime, seen_mtime, polled, changed, first_change, reload_ms;
    /* the stage's clock: followed (the last call at or before it) until a scrub, a step or a pick */
    bool follow;
    bool none_yet;         /* following, and the stage's clock is before the agent's first call */
    double stage_t;
    int C, sel;            /* the call shown; the selected action, -1 */
    int *by_item;          /* C's positions (ctx_positions) */
    unsigned char *linked; /* the selected action's likely causes and itself, at C */
    int nlinked;
    double by[CK_COUNT];   /* C's tokens by kind */
    int inn, outn;         /* items C added and dropped */
    /* per item of this agent, what the user did: kept while it is loaded (and by key across a reload) */
    signed char *ov;       /* opened (1), folded (0), -1 */
    float *pin;            /* x, y where it was dragged (NAN: not) */
    float *size;           /* w, h it was resized to (0: not) */
    /* the windows */
    CNode **node;          /* per item: its window while C shows it */
    int *shown, nshown;    /* the items C shows */
    bool *open_tmp;
    unsigned char *mark;
    CNode **zs;            /* shown windows in drawing order (nshown) */
    CNode **gone;          /* leaving: fading out */
    int ngone, capgone;
    unsigned zseq;
    /* the actions */
    int *acts, nacts, *fil, nfil;
    char q[128];
    bool typing, all;
    float act_scroll;
    bool act_follow;
    /* camera: screen = stage's corner + t + k * world */
    float tx, ty, k, sw, sh;
    struct { bool on; double t0, d; float cx0, cy0, lk0, cx1, cy1, lk1; } cam;
    double clock, last_show, rest_at, fit_at, play_at;
    bool playing;
    /* input */
    int drag, dn;          /* what a press started; its window's item */
    Vector2 press;
    float x0, y0, w0, h0;
    bool moved;
    bool picker;           /* the agent list is open */
    float pick_scroll;
    int hov, hov_zone;     /* the window under the mouse and its part, -1 */
    struct { char key[96]; float t; } tip;
    struct { int c, sel, lines; float w, sw; bool on; } cache; /* the stats' width at call c; the selected action's lines */
    /* AIG_CTX_BENCH=N: scrub the last N calls, a call a frame, and print the times */
    struct { int n, from, at, all; double *show, *frame; } bench;
};

typedef struct {
    Rectangle bar, stage, side, strip, hint;
    Rectangle pick, est, recent, all, follow;
    float stats_y;
    Rectangle why, filter, list, picklist;
    float sec1_h, why_h, why_rows, acts_y; /* why_rows: the y its rows start at */
    int why_lines;                       /* the action's source: 1 or 2 lines */
    Rectangle lbl, prev, play, next, bars, cur;
} Geo;

static void show(CView *c, int call, bool instant);

/* ---- loading ------------------------------------------------------------------- */

static void *worker(void *arg) {
    CView *c = arg;
    char *err = NULL;
    double t0 = now_wall();
    CtxAgent *a = c->reloading ? ctx_reload(c->reloading, c->proj, &c->cancel, &err)
                               : ctx_load(c->root, c->load.path, c->proj, &c->cancel, &err);
    CtxRef *refs = NULL;
    int nrefs = a ? ctx_session_agents(c->load.path, &refs) : 0;
    pthread_mutex_lock(&c->mu);
    c->res = (Result){a, refs, nrefs, err, (now_wall() - t0) * 1e3};
    c->done = true;
    pthread_mutex_unlock(&c->mu);
    return NULL;
}

static void file_stat(const char *path, long long *size, double *mtime) {
    struct stat st;
    if (stat(path, &st)) { *size = -1, *mtime = 0; return; }
    *size = (long long)st.st_size;
    *mtime = (double)st.st_mtim.tv_sec + st.st_mtim.tv_nsec * 1e-9;
}

static void start_job(CView *c) {
    file_stat(c->load.path, &c->load_size, &c->load_mtime);
    atomic_store(&c->cancel, 0);
    c->done = false;
    if (pthread_create(&c->th, NULL, worker, c)) {
        perror("aigource: context load");
        if (!c->reloading) free(c->err), c->err = xstrdup("could not start the load"), c->failed = true;
        c->reloading = NULL;
        return;
    }
    c->running = true;
}

/* The latest ask. */
static void start_load(CView *c) {
    free(c->load.path);
    c->load = c->want, c->want.path = NULL, c->want_due = false;
    c->reloading = NULL;
    start_job(c);
}

/* The agent shown again, its transcript having grown (live). */
static void start_reload(CView *c) {
    free(c->load.path);
    c->load.path = xstrdup(c->a->path);
    c->reloading = c->a;
    start_job(c);
}

/* A load of another agent (or the first) is on: the screen says so. A reload isn't: the agent stays. */
static bool loading(const CView *c) { return (c->running && !c->reloading) || c->want_due; }
/* An agent to show: loaded, and no other on its way. */
static bool shown(const CView *c) { return c->a && !c->failed && !loading(c); }

CView *cv_new(const char *root) {
    CView *c = xcalloc(1, sizeof *c);
    c->root = xstrdup(root);
    c->proj = ctx_project_new();
    pthread_mutex_init(&c->mu, NULL);
    c->sel = c->hov = -1;
    c->k = 1;
    c->sw = 1000, c->sh = 700;
    const char *b = getenv("AIG_CTX_BENCH");
    c->bench.n = b ? atoi(b) : 0;
    return c;
}

static void free_node(CNode *n) {
    if (!n) return;
    cw_body_free(n);
    free(n);
}

/* The agent shown and every window, as it goes (install sets them all again). */
static void free_agent(CView *c) {
    for (int k = 0; k < c->nshown; k++) free_node(c->zs[k]);
    for (int k = 0; k < c->ngone; k++) free_node(c->gone[k]);
    c->ngone = c->nshown = 0;
    c->drag = D_NONE, c->dn = c->hov = -1;
    free(c->by_item), free(c->linked), free(c->ov), free(c->pin), free(c->size), free(c->node), free(c->shown);
    free(c->open_tmp), free(c->mark), free(c->zs), free(c->acts), free(c->fil);
    ctx_free(c->a);
    ctx_refs_free(c->refs, c->nrefs);
}

void cv_free(CView *c) {
    if (!c) return;
    if (c->running) atomic_store(&c->cancel, 1), pthread_join(c->th, NULL);
    ctx_free(c->res.a), ctx_refs_free(c->res.refs, c->res.nrefs), free(c->res.err);
    free_agent(c);
    free(c->gone);
    ctx_project_free(c->proj);
    pthread_mutex_destroy(&c->mu);
    free(c->bench.show), free(c->bench.frame);
    free(c->load.path), free(c->want.path), free(c->err), free(c->reload_err), free(c->root);
    free(c);
}

static void install(CView *c, CtxAgent *a, CtxRef *refs, int nrefs, bool keep);
static bool select_action(CView *c, int id, bool instant);
static void fit(CView *c, int what, bool animate);
static void stop(CView *c);

/* A finished job in, at the start of a frame (cl_job_done): a load replaces
 * the agent shown, a reload updates it; a dropped one goes. */
static void swap_in(CView *c) {
    if (!c->running) {
        if (c->want_due) start_load(c);
        return;
    }
    pthread_mutex_lock(&c->mu);
    bool done = c->done;
    Result r = c->res;
    if (done) c->res = (Result){0}, c->done = false;
    pthread_mutex_unlock(&c->mu);
    if (!done) return;
    CtxAgent *a = r.a;
    char *err = r.err;
    pthread_join(c->th, NULL);
    c->running = false;
    ClDone d = {.reload = c->reloading != NULL, .cancelled = atomic_load(&c->cancel), .newer = c->want_due,
                .ok = a && a->ncalls > 0, .same = a && c->a && !strcmp(c->a->path, a->path)};
    c->reloading = NULL;
    int what = cl_job_done(&d);
    if (what != CL_JOB_INSTALL && what != CL_JOB_KEEP) {
        if (a && !a->ncalls && !err) err = xstrdup("this transcript has no API calls yet");
        ctx_free(a);
        ctx_refs_free(r.refs, r.nrefs);
        /* CL_JOB_RETRY: the transcript still counts as changed (src_* untouched), live() reloads it again */
        if (what == CL_JOB_RELOAD_FAIL) { /* the agent stays; retried once it grows again */
            c->src_size = c->load_size, c->src_mtime = c->load_mtime;
            free(c->reload_err), c->reload_err = err ? err : xstrdup("unreadable");
        } else if (what == CL_JOB_FAIL) {
            free(c->err), c->err = err ? err : xstrdup("unreadable"), c->failed = true, stop(c);
        } else free(err);
        if (c->want_due) start_load(c);
        return;
    }
    free(err);
    free(c->reload_err), c->reload_err = NULL;
    if (d.reload) {
        c->reload_ms = r.ms;
        if (getenv("AIG_DEBUG")) fprintf(stderr, "context: reloaded %s in %.0f ms, %d calls, %d items\n", a->id, r.ms, a->ncalls, a->nitems);
    }
    double t0 = GetTime();
    install(c, a, r.refs, r.nrefs, what == CL_JOB_KEEP);
    if (d.reload && getenv("AIG_DEBUG")) fprintf(stderr, "context: swapped in at a frame's start in %.1f ms\n", (GetTime() - t0) * 1e3);
    c->src_size = c->seen_size = c->load_size, c->src_mtime = c->seen_mtime = c->load_mtime;
    c->changed = c->first_change = 0;
}

/* An open on the agent shown: to the action asked for, else the call (from the end when < 0, by time when CV_NONE). */
static void go_to(CView *c, Ask w, bool instant) {
    if (select_action(c, w.sel, instant)) return;
    if (w.sel >= 0) fprintf(stderr, "aigource: context: item %d is not an action\n", w.sel);
    show(c, w.call == CV_NONE ? cl_call_at(c->a, w.t) : w.call < 0 ? c->a->ncalls + w.call : w.call, instant);
}

void cv_open(CView *c, const char *path, int call, int sel, double t) {
    swap_in(c); /* a job that finished while closed is taken in, not thrown away */
    c->open = true;
    c->picker = c->typing = c->playing = false;
    c->follow = call == CV_NONE && sel < 0; /* a call or an action asked for: not the stage's */
    if (t > 0) c->stage_t = t;
    ClWorker w = {c->a ? c->a->path : NULL, c->running ? c->load.path : NULL, c->reloading != NULL, atomic_load(&c->cancel)};
    free(c->want.path), c->want.path = NULL, c->want_due = false;
    switch (cl_open_job(&w, path)) {
    case CL_OPEN_KEEP: /* on its way: opened where asked once in */
        c->load.call = call, c->load.sel = sel, c->load.t = t;
        return;
    case CL_OPEN_SHOWN_DROP: atomic_store(&c->cancel, 1); /* the other agent's load: dropped */
        /* fall through */
    case CL_OPEN_SHOWN: /* loaded (shown, or behind another's failed load): live reloads keep it fresh */
        c->failed = false;
        go_to(c, (Ask){NULL, call, sel, t}, false);
        return;
    }
    c->want = (Ask){xstrdup(path), call, sel, t};
    c->want_due = true;
    c->failed = false;
    if (c->running) atomic_store(&c->cancel, 1); /* CL_OPEN_QUEUE: the newer one loads when it's out */
    else start_load(c);
}

/* Nothing is cancelled: a load or reload in flight is taken in at the next open. */
void cv_close(CView *c) {
    c->open = c->picker = c->typing = c->playing = false;
    c->drag = D_NONE;
}

bool cv_is_open(const CView *c) { return c && c->open; }
/* a --shot waits for this: an agent shown, or why there is none */
bool cv_ready(const CView *c) { return c->open && !c->running && !c->want_due && (c->a || c->failed); }
bool cv_typing(const CView *c) { return c && c->open && c->typing; }

void cv_clock(CView *c, double t) { c->stage_t = t; }

static void filter(CView *c) {
    c->nfil = 0;
    for (int k = 0; k < c->nacts; k++)
        if (cl_match(&c->a->items[c->acts[k]], c->q)) c->fil[c->nfil++] = c->acts[k];
}

/* The windows on screen, as the reloaded agent's items (map: old item ->
 * new, cl_key_map); the gone ones freed. *dn: the window in hand, -1. */
static CNode **carry_windows(CView *c, const CtxAgent *a, const int *map, int *n, int *dn) {
    CNode **carried = xmalloc(sizeof *carried * (size_t)c->nshown);
    *n = 0, *dn = -1;
    for (int k = 0; k < c->nshown; k++) {
        CNode *w = c->zs[k];
        int j = map[w->item];
        if (j < 0) { free_node(w); continue; }
        if (w->item == c->dn) *dn = j;
        w->item = j, w->kind = a->items[j].kind, w->hdr_c = -1, w->fit_w = w->meta_w = -1;
        cl_title(&a->items[j], w->title, sizeof w->title);
        carried[(*n)++] = w;
    }
    c->nshown = 0;
    return carried;
}

/* A job is in: its agent replaces the shown one. keep (a reload, or the same
 * agent loaded again): what the user did carries over by item key, the
 * windows stay where they are (new ones fly in from the agent), and so do the
 * selection, the filter, the view, a pan or drag in progress, and the call
 * (the stage's when following). */
static void install(CView *c, CtxAgent *a, CtxRef *refs, int nrefs, bool keep) {
    int n = a->nitems;
    size_t N = (size_t)n;
    ClUser u = {xmalloc(N), xmalloc(sizeof(float) * 2 * N), xcalloc(2 * N, sizeof(float)), -1};
    memset(u.ov, -1, N);
    for (int i = 0; i < 2 * n; i++) u.pin[i] = NAN;
    CNode **carried = NULL;
    int ncarried = 0, dn = -1, old_c = c->C, old_n = c->nshown;
    keep = keep && c->a;
    if (keep) {
        int *map = xmalloc(sizeof *map * (size_t)c->a->nitems);
        cl_key_map(c->a, a, map);
        cl_carry(map, c->a->nitems, &(ClUser){c->ov, c->pin, c->size, c->sel}, &u);
        carried = carry_windows(c, a, map, &ncarried, &dn);
        free(map);
    }
    int drag = c->drag;
    float scroll = c->act_scroll;
    free_agent(c);
    c->a = a, c->refs = refs, c->nrefs = nrefs;
    c->ov = u.ov, c->pin = u.pin, c->size = u.size;
    c->by_item = xmalloc(sizeof *c->by_item * N);
    c->linked = xcalloc(N, 1);
    c->mark = xcalloc(N, 1);
    c->node = xcalloc(N, sizeof *c->node);
    c->shown = xmalloc(sizeof *c->shown * N);
    c->zs = xmalloc(sizeof *c->zs * N);
    c->open_tmp = xmalloc(sizeof *c->open_tmp * N);
    c->acts = xmalloc(sizeof *c->acts * N);
    c->fil = xmalloc(sizeof *c->fil * N);
    c->nacts = cl_actions(a, c->acts);
    filter(c);
    c->rest_at = c->fit_at = 0;
    c->cache.on = false, c->cache.sel = -2;
    c->last_show = -1e9;
    c->failed = false;
    if (keep) {
        for (int k = 0; k < ncarried; k++) c->node[carried[k]->item] = carried[k], c->zs[k] = carried[k];
        c->nshown = ncarried; /* show() takes them from here */
        free(carried);
        c->sel = u.sel, c->act_scroll = scroll;
        if (drag == D_PAN || drag == D_SCRUB || drag == D_EAT) c->drag = drag; /* goes on */
        else if (dn >= 0) c->drag = drag, c->dn = dn;                         /* a window in hand too */
        int call = c->follow ? cl_call_at(a, c->stage_t) : old_c < a->ncalls ? old_c : a->ncalls - 1;
        c->C = -1;
        show(c, call < 0 ? 0 : call, false);
        if (getenv("AIG_DEBUG")) { /* what was kept: windows, dragged ones where they were put, the selection */
            int pinned = 0, put = 0;
            for (int k = 0; k < c->nshown; k++) {
                const CNode *w = c->zs[k];
                if (isnan(c->pin[2 * w->item])) continue;
                pinned++, put += w->x == c->pin[2 * w->item] && w->y == c->pin[2 * w->item + 1];
            }
            fprintf(stderr, "context: kept %d of the %d windows on screen (%d now), %d dragged ones shown (%d where they "
                    "were put), selection %s, call %d%s\n", ncarried, old_n, c->nshown, pinned, put,
                    c->sel >= 0 ? c->a->items[c->sel].src : "none", c->C, c->follow ? " (following)" : "");
        }
        return;
    }
    c->sel = -1, c->C = -1;
    c->playing = false;
    c->act_scroll = 0;
    go_to(c, c->load, true);
    fit(c, FIT_RECENT, false);
    if (c->bench.n > 0) /* its own steps: not the stage's clock */
        c->bench.from = a->ncalls - (c->bench.n < a->ncalls ? c->bench.n : a->ncalls), c->bench.at = -1, c->follow = false;
}

/* ---- one call on the stage -------------------------------------------------------- */

static void set_open(CView *c, CNode *n, bool open) {
    n->open = open;
    if (!open) cw_body_free(n), n->scroll = n->hscroll = 0;
    const float *s = &c->size[2 * n->item];
    if (open && s[0] > 0) n->w = s[0], n->h = s[1];
    else cl_size(&c->a->items[n->item], open, &n->w, &n->h);
    n->fit_w = n->meta_w = -1;
    n->hdr_c = -1;
}

static CNode *new_node(CView *c, int item) {
    CNode *n = xcalloc(1, sizeof *n);
    n->item = item, n->kind = c->a->items[item].kind;
    n->fresh = true;
    n->z = c->zseq++;
    n->t0 = -1, n->hdr_c = -1, n->fit_w = n->meta_w = -1;
    cl_title(&c->a->items[item], n->title, sizeof n->title);
    if (!isnan(c->pin[2 * item])) n->x = c->pin[2 * item], n->y = c->pin[2 * item + 1];
    c->node[item] = n;
    return n;
}

static int cmp_z(const void *x, const void *y) {
    unsigned a = (*(CNode *const *)x)->z, b = (*(CNode *const *)y)->z;
    return (a > b) - (a < b);
}

/* The layout of the windows shown (the draft's simulate()): ranks and target
 * distances, then lay_wins. */
static void simulate(CView *c, int ticks, int passes) {
    int n = c->nshown;
    LWin *w = xmalloc(sizeof *w * (size_t)n);
    for (int k = 0; k < n; k++) {
        CNode *d = c->node[c->shown[k]];
        const float *p = &c->pin[2 * d->item];
        w[k] = (LWin){.x = d->x, .y = d->y, .w = d->w, .h = d->h, .id = (unsigned)d->item, .fresh = d->fresh};
        if (!isnan(p[0])) w[k].pinned = true, w[k].x = p[0], w[k].y = p[1];
        if (d->item == c->dn && (c->drag == D_MOVE || c->drag == D_RESIZE)) w[k].pinned = true; /* in hand: stays */
    }
    cl_targets(c->a, c->C, c->shown, n, c->linked, w);
    lay_wins(w, n, CL_HUB, ticks, passes);
    for (int k = 0; k < n; k++) {
        CNode *d = c->node[c->shown[k]];
        d->x = w[k].x, d->y = w[k].y;
    }
    free(w);
}

/* Windows move from where they are drawn to their new places. */
static void snapshot(CView *c) {
    for (int k = 0; k < c->nshown; k++) {
        CNode *n = c->zs[k];
        if (!n->fresh) cw_pos(n, c->clock, &n->ax, &n->ay, &n->aa);
    }
}

static void animate(CView *c, bool instant) {
    for (int k = 0; k < c->nshown; k++) {
        CNode *n = c->zs[k];
        if (instant) n->t0 = -1;
        else {
            if (n->fresh) n->ax = n->ay = 0, n->aa = 0; /* new ones fly out of the agent */
            n->t0 = c->clock;
        }
        n->fresh = false;
    }
}

static void relayout(CView *c, int ticks) {
    snapshot(c);
    simulate(c, ticks, 120);
    animate(c, false);
}

static void show(CView *c, int call, bool instant) {
    const CtxAgent *a = c->a;
    if (c->clock - c->last_show < FAST) instant = true; /* scrubbing fast: no animation */
    call = call < 0 ? 0 : call >= a->ncalls ? a->ncalls - 1 : call;
    bool forward = call > c->C;
    c->C = call;
    ctx_positions(a, call, c->by_item);
    c->nlinked = cl_linked(a, c->sel, call, c->linked);
    int prev = c->nshown;
    c->nshown = cl_shown(a, call, c->by_item, c->shown);
    cl_by_kind(a, call, c->by);
    cl_inout(a, call, &c->inn, &c->outn);
    int summary = -1; /* a compaction folds the windows into its summary */
    if (call > 0 && a->calls[call].comp)
        for (int k = 0; k < c->nshown && summary < 0; k++)
            if (a->items[c->shown[k]].kind == CK_SUMMARY) summary = c->shown[k];
    memset(c->mark, 0, (size_t)a->nitems);
    for (int k = 0; k < c->nshown; k++) c->mark[c->shown[k]] = 1;
    int fold0 = c->ngone;
    for (int k = 0; k < prev; k++) { /* the windows that leave */
        CNode *n = c->zs[k];
        if (c->mark[n->item]) continue;
        c->node[n->item] = NULL;
        if (c->dn == n->item) c->drag = D_EAT, c->dn = -1;
        if (instant) { free_node(n); continue; }
        cw_pos(n, c->clock, &n->ax, &n->ay, &n->aa);
        n->t0 = n->gone = c->clock;
        n->gx = n->ax, n->gy = n->ay;
        n->leave = forward && summary >= 0 && a->items[n->item].leave == call ? LV_FOLD /* into the summary */
                 : n->kind == CK_THINKING                                         ? LV_THINK
                                                                                  : LV_FADE;
        PUSH(c->gone, c->ngone, c->capgone, n);
    }
    cl_open(a, call, c->shown, c->nshown, c->ov, c->linked, c->nlinked, c->open_tmp);
    int fresh = 0;
    for (int k = 0; k < c->nshown; k++) {
        int i = c->shown[k];
        CNode *n = c->node[i];
        if (!n) n = new_node(c, i), fresh++;
        if (!n->w || (c->open_tmp[k] != n->open && i != c->dn)) set_open(c, n, c->open_tmp[k]); /* not the one in hand */
        n->resp = c->by_item[i] < 0;
        n->hdr_c = -1;
        c->zs[k] = n;
    }
    qsort(c->zs, (size_t)c->nshown, sizeof *c->zs, cmp_z);
    snapshot(c);
    if (instant && fresh != c->nshown) { /* scrubbing: a short pass now, the full one when it rests */
        simulate(c, 6, 12);
        c->rest_at = c->clock + REST;
    } else {
        simulate(c, fresh == c->nshown ? 120 : 24, 120);
        c->rest_at = 0;
    }
    animate(c, instant);
    if (summary >= 0)
        for (int k = fold0; k < c->ngone; k++)
            if (c->gone[k]->leave == LV_FOLD) c->gone[k]->gx = c->node[summary]->x, c->gone[k]->gy = c->node[summary]->y;
    c->last_show = c->clock;
}

/* False when id isn't an action. */
static bool select_action(CView *c, int id, bool instant) {
    const CtxAgent *a = c->a;
    if (id < 0 || id >= a->nitems || a->items[id].kind != CK_ACTION || a->items[id].made < 0) return false;
    c->sel = id;
    c->follow = false; /* the action's call, not the stage's */
    for (int k = 0; k < a->items[id].nlk; k++) c->ov[a->items[id].lk[k].item] = -1; /* its causes open */
    show(c, a->items[id].made, instant);
    if (!instant) c->fit_at = c->clock + 0.05;
    c->act_follow = true;
    return true;
}

static void stop(CView *c) { c->playing = false; }

static void play(CView *c) {
    if (c->playing) { stop(c); return; }
    if (c->C >= c->a->ncalls - 1) show(c, 0, false);
    c->playing = true;
    c->follow = false; /* the view's own clock */
    c->play_at = c->clock + PLAY_STEP;
}

/* ---- camera ----------------------------------------------------------------------- */

static void cam_to(CView *c, float tx, float ty, float k, double d) {
    if (d <= 0) {
        c->cam.on = false;
        c->tx = tx, c->ty = ty, c->k = k;
        return;
    }
    /* the view's centre and log zoom move, so it doesn't swing */
    c->cam.on = true, c->cam.t0 = c->clock, c->cam.d = d;
    c->cam.cx0 = (c->sw / 2 - c->tx) / c->k, c->cam.cy0 = (c->sh / 2 - c->ty) / c->k, c->cam.lk0 = logf(c->k);
    c->cam.cx1 = (c->sw / 2 - tx) / k, c->cam.cy1 = (c->sh / 2 - ty) / k, c->cam.lk1 = logf(k);
}

static void cam_step(CView *c) {
    if (!c->cam.on) return;
    double p = (c->clock - c->cam.t0) / c->cam.d;
    if (p >= 1) p = 1, c->cam.on = false;
    float e = p < 0.5 ? 4 * (float)(p * p * p) : 1 - powf(-2 * (float)p + 2, 3) / 2;
    float cx = c->cam.cx0 + (c->cam.cx1 - c->cam.cx0) * e, cy = c->cam.cy0 + (c->cam.cy1 - c->cam.cy0) * e;
    c->k = expf(c->cam.lk0 + (c->cam.lk1 - c->cam.lk0) * e);
    c->tx = c->sw / 2 - c->k * cx, c->ty = c->sh / 2 - c->k * cy;
}

static void fit(CView *c, int what, bool animate) {
    float x0 = -90, y0 = -90, x1 = 90, y1 = 90;
    for (int pass = 0; pass < 2; pass++) {
        int got = 0;
        for (int k = 0; k < c->nshown; k++) {
            const CNode *n = c->node[c->shown[k]];
            if (!pass && !(what == FIT_ALL || (what == FIT_RECENT ? n->open : c->linked[n->item]))) continue;
            x0 = fminf(x0, n->x - n->w / 2), x1 = fmaxf(x1, n->x + n->w / 2);
            y0 = fminf(y0, n->y - n->h / 2), y1 = fmaxf(y1, n->y + n->h / 2);
            got++;
        }
        if (got || !c->nshown) break; /* none of those: all of them */
    }
    float k = fminf(1.1f, fmaxf(what == FIT_ALL ? 0.05f : 0.5f, 0.92f * fminf(c->sw / (x1 - x0), c->sh / (y1 - y0))));
    cam_to(c, c->sw / 2 - k * (x0 + x1) / 2, c->sh / 2 - k * (y0 + y1) / 2, k, animate ? 0.45 : 0);
}

static void center_on(CView *c, const CNode *n) {
    float k = fmaxf(c->k, 0.8f);
    cam_to(c, c->sw / 2 - k * n->x, c->sh / 2 - k * n->y, k, 0.4);
}

static Camera2D camera(const CView *c, const Geo *g) {
    return (Camera2D){.offset = {g->stage.x + c->tx, g->stage.y + c->ty}, .zoom = c->k};
}

static Vector2 to_world(const CView *c, const Geo *g, Vector2 p) {
    return (Vector2){(p.x - g->stage.x - c->tx) / c->k, (p.y - g->stage.y - c->ty) / c->k};
}

/* ---- geometry ----------------------------------------------------------------------- */

static void ref_label(const CView *c, int k, char *b, size_t n) {
    const CtxRef *r = &c->refs[k];
    bool cur = !strcmp(r->id, c->a->id);
    char calls[48] = "";
    if (cur) snprintf(calls, sizeof calls, " \xC2\xB7 %d calls", c->a->ncalls);
    if (k == 0) snprintf(b, n, "main agent%s", calls);
    else {
        const char *t = strrchr(r->name, ':');
        snprintf(b, n, "%s \xC2\xB7 %s%s", t ? t + 1 : r->name, r->desc ? r->desc : "", calls);
    }
}

static int cur_ref(const CView *c) {
    for (int k = 0; k < c->nrefs; k++)
        if (!strcmp(c->refs[k].id, c->a->id)) return k;
    return -1;
}

static void agent_label(const CView *c, char *b, size_t n) {
    int k = cur_ref(c);
    if (k >= 0) ref_label(c, k, b, n);
    else if (!strcmp(c->a->kind, "main")) snprintf(b, n, "main agent \xC2\xB7 %d calls", c->a->ncalls);
    else snprintf(b, n, "%s \xC2\xB7 %d calls", c->a->name, c->a->ncalls);
}

static int nkinds(const CView *c) {
    int n = 0;
    for (int k = 0; k < CK_COUNT; k++) n += c->by[k] > 0;
    return n;
}

static float stats(CView *c, float x, float cy, bool d);

/* C's context window (1M when unknown), and how full C is. */
static double call_window(const CtxCall *C, double *share) {
    int win = ctx_window(C->model);
    double W = win ? win : 1e6;
    *share = fmin(1, C->total / W);
    return W;
}
#define FOLLOWING "following the stage"
#define DETACHED "detached \xC2\xB7 L follows"

static Geo geometry(CView *c) {
    Geo g;
    memset(&g, 0, sizeof g);
    float W = (float)GetScreenWidth(), H = (float)GetScreenHeight(), bar_h = BAR_H;
    float x = sv_brand("context", 0, false) + 16;
    if (shown(c)) {
        char lb[600];
        agent_label(c, lb, sizeof lb);
        g.pick = (Rectangle){x, (BAR_H - 26) / 2, fminf(380, fmaxf(160, sv_tw(F_MONO, lb, 12.5f) + 34)), 26};
        x += g.pick.width + 12;
        if (c->a->est) {
            g.est = (Rectangle){x, (BAR_H - 18) / 2, sv_tws(F_MONO_B, "ESTIMATED", 10.5f, 0.5f) + 12, 18};
            x += g.est.width + 12;
        }
        g.recent = (Rectangle){x, (BAR_H - 22) / 2, sv_tw(F_MONO, "recent", 12) + 18, 22};
        g.all = (Rectangle){g.recent.x + g.recent.width + 8, g.recent.y, sv_tw(F_MONO, "all", 12) + 18, 22};
        g.follow = (Rectangle){g.all.x + g.all.width + 8, g.recent.y, sv_tw(F_MONO, c->follow ? FOLLOWING : DETACHED, 12) + 26, 22};
        x = g.follow.x + g.follow.width + 16;
        g.stats_y = BAR_H / 2;
        if (!c->cache.on || c->cache.c != c->C) c->cache.sw = stats(c, 0, 0, false), c->cache.c = c->C, c->cache.on = true;
        if (x + c->cache.sw + ((c->running && c->reloading) || c->reload_err ? 110 : 0) > W - 12) g.stats_y = BAR_H + 13, bar_h = BAR_H + 28; /* a row of their own */
    }
    g.bar = (Rectangle){0, 0, W, bar_h};
    float body_h = fmaxf(60, H - bar_h - STRIP_H - HINT_H), sw = fminf(SIDE_W, W * 0.4f);
    g.stage = (Rectangle){0, bar_h, W - sw, body_h};
    g.side = (Rectangle){W - sw, bar_h, sw, body_h};
    g.strip = (Rectangle){0, bar_h + body_h, W, STRIP_H};
    g.hint = (Rectangle){0, H - HINT_H, W, HINT_H};
    /* side: the context's make-up, why (an action selected), the actions */
    float y = g.side.y;
    g.sec1_h = 10 + 16 + 6 + 10 + 6 + ceilf((float)nkinds(c) / 2) * 17 + 10;
    y += g.sec1_h;
    if (shown(c) && c->sel >= 0) {
        const CtxItem *it = &c->a->items[c->sel];
        int n = it->nlk, cap = 0;
        if (c->cache.sel != c->sel || c->cache.w != sw) { /* wrapped once per selection */
            UiSpan *rows = NULL;
            const char *src = it->src ? it->src : "";
            c->cache.lines = ui_wrap(ui_mono, ui_mono_bold, src, NULL, (int)strlen(src), PX(12), sw - 24, 0, &rows, &cap) > 1 ? 2 : 1;
            c->cache.sel = c->sel, c->cache.w = sw;
            free(rows);
        }
        g.why_lines = c->cache.lines;
        g.why_rows = y + 10 + 16 + 6 + (float)g.why_lines * 17 + 4 + (it->made != c->C || c->none_yet ? 17 : 0);
        g.why_h = g.why_rows - y + (float)(n ? n : 1) * WHY_ROW + 10;
        g.why = (Rectangle){g.side.x, y, sw, g.why_h};
        y += g.why_h;
    }
    g.filter = (Rectangle){g.side.x + 12, y + 10 + 16 + 6, sw - 24, 28};
    g.acts_y = y;
    y = g.filter.y + g.filter.height + 10;
    g.list = (Rectangle){g.side.x, y, sw, fmaxf(0, g.side.y + body_h - y)};
    /* strip */
    float sy = g.strip.y + (STRIP_H - 22) / 2;
    g.lbl = (Rectangle){12, sy, sv_tws(F_SANS_B, "CALLS", 11, 0.88f), 22};
    g.prev = (Rectangle){g.lbl.x + g.lbl.width + 10, sy, 26, 22};
    g.play = (Rectangle){g.prev.x + 26 + 6, sy, 30, 22};
    g.next = (Rectangle){g.play.x + 30 + 6, sy, 26, 22};
    float cw = fminf(290, W * 0.25f);
    g.cur = (Rectangle){W - 12 - cw, sy, cw, 22};
    float bx = g.next.x + 26 + 10;
    g.bars = (Rectangle){bx, g.strip.y + (STRIP_H - BARS_H) / 2, fmaxf(0, g.cur.x - 10 - bx), BARS_H};
    if (c->picker && c->nrefs) {
        float h = fminf((float)c->nrefs * PICK_ROW + 8, g.stage.height - 20);
        float w = fmaxf(g.pick.width, 420);
        g.picklist = (Rectangle){g.pick.x, g.pick.y + g.pick.height + 4, fminf(w, W - g.pick.x - 12), h};
    }
    return g;
}

/* ---- the bar ------------------------------------------------------------------------ */

static float tx_run(int f, const char *s, float x, float cy, float css, Color c, bool draw) {
    if (draw) sv_tx(f, s, x, sv_mid(cy, css), css, c);
    return sv_tw(f, s, css);
}

static void badge(Rectangle r, const char *s) {
    DrawRectangleRoundedLinesEx(r, 6 / fminf(r.width, r.height), 4, 1, C_HOT);
    sv_txs(F_MONO_B, s, r.x + 6, sv_mid(r.y + r.height / 2, 10.5f), 10.5f, 0.5f, C_HOT);
}

/* The draft's stats, from x (drawn when d; geometry measures them): their width. */
static float stats(CView *c, float x, float cy, bool d) {
    const CtxAgent *a = c->a;
    const CtxCall *C = &a->calls[c->C];
    double share, W = call_window(C, &share);
    double rd = 0, tot = 0;
    for (int k = 0; k <= c->C; k++)
        if (a->calls[k].has_u) rd += (double)a->calls[k].u[1], tot += a->calls[k].total;
    char t1[32], t2[32], b[160];
    float gap = 14, s = x;
    cl_fmt_k(t1, sizeof t1, C->total);
    cl_fmt_k(t2, sizeof t2, W);
    x += tx_run(F_MONO, "context ", x, cy, 12.5f, C_DIM, d);
    x += tx_run(F_MONO_B, t1, x, cy, 12.5f, C_FG, d);
    snprintf(b, sizeof b, " / %s ", t2);
    x += tx_run(F_MONO, b, x, cy, 12.5f, C_DIM, d);
    if (d) {
        Rectangle m = {x, cy - 3.5f, 90, 7};
        DrawRectangleRec(m, C_BG);
        DrawRectangleRec((Rectangle){x + 1, cy - 2.5f, (float)share * 88, 5}, C_HOT);
        DrawRectangleLinesEx(m, 1, C_LINE);
    }
    x += 90;
    snprintf(b, sizeof b, " %.1f%%", fmax(0, floor(share * 1000 + 0.5) / 10));
    x += tx_run(F_MONO, b, x, cy, 12.5f, C_DIM, d) + gap;
    x += tx_run(F_MONO, "cache hit ", x, cy, 12.5f, C_DIM, d);
    if (C->has_u && C->total > 0) snprintf(b, sizeof b, "%.0f%%", floor((double)C->u[1] / C->total * 100 + 0.5));
    else snprintf(b, sizeof b, "\xE2\x80\x94");
    x += tx_run(F_MONO_B, b, x, cy, 12.5f, C_FG, d);
    if (tot > 0) snprintf(b, sizeof b, " \xC2\xB7 %.0f%% so far", floor(rd / tot * 100 + 0.5));
    else snprintf(b, sizeof b, " \xC2\xB7 \xE2\x80\x94%% so far");
    x += tx_run(F_MONO, b, x, cy, 12.5f, C_DIM, d) + gap;
    snprintf(b, sizeof b, "%d", C->norder);
    x += tx_run(F_MONO_B, b, x, cy, 12.5f, C_FG, d);
    x += tx_run(F_MONO, " items", x, cy, 12.5f, C_DIM, d) + gap;
    x += tx_run(F_MONO, "call ", x, cy, 12.5f, C_DIM, d);
    snprintf(b, sizeof b, "%d", c->C);
    x += tx_run(F_MONO_B, b, x, cy, 12.5f, C_FG, d);
    snprintf(b, sizeof b, " / %d", a->ncalls - 1);
    x += tx_run(F_MONO, b, x, cy, 12.5f, C_DIM, d);
    if (C->noresp) {
        float bw = sv_tws(F_MONO_B, "NO RESPONSE", 10.5f, 0.5f) + 12;
        if (d) badge((Rectangle){x + gap, cy - 9, bw, 18}, "NO RESPONSE");
        x += gap + bw;
    }
    return x - s;
}

static void chevron(float x, float y, Color c) {
    DrawTriangle((Vector2){x - 4, y - 2}, (Vector2){x, y + 2.5f}, (Vector2){x + 4, y - 2}, c);
}

static void draw_bar(CView *c, const Geo *g, Vector2 mp) {
    DrawRectangleRec(g->bar, C_PANEL);
    sv_hline(0, g->bar.width, g->bar.y + g->bar.height - 1, C_LINE);
    float cy = BAR_H / 2;
    sv_brand("context", cy, true);
    if (!shown(c)) return; /* loading another agent, or none: not the old one's */
    Rectangle p = g->pick;
    bool hot = CheckCollisionPointRec(mp, p) || c->picker;
    ui_box(p, 10 / p.height, hot ? sv_brighter(C_RAISED, 0.4f) : C_RAISED, 1, c->picker ? C_FOCUS : C_LINE);
    char lb[600], fitb[600];
    agent_label(c, lb, sizeof lb);
    sv_fit(F_MONO, fitb, sizeof fitb, lb, 12.5f, p.width - 30);
    sv_tx(F_MONO, fitb, p.x + 8, sv_mid(cy, 12.5f), 12.5f, C_FG);
    chevron(p.x + p.width - 13, cy, C_DIM);
    if (g->est.width > 0) badge(g->est, "ESTIMATED");
    for (int b = 0; b < 2; b++) {
        Rectangle r = b ? g->all : g->recent;
        bool h = CheckCollisionPointRec(mp, r);
        ui_box(r, 8 / r.height, C_RAISED, 1, C_LINE);
        sv_tx(F_MONO, b ? "all" : "recent", r.x + 9, sv_mid(r.y + r.height / 2, 12), 12, h ? C_FG : C_DIM);
    }
    /* the stage's clock: followed, or not (L, or a click, follows it again) */
    Rectangle f = g->follow;
    bool fh = CheckCollisionPointRec(mp, f);
    DrawRectangleRounded(f, 1, 8, c->follow ? ui_fade(C_ADD, 0.12f) : fh ? C_RAISED : C_PANEL);
    DrawRectangleRoundedLinesEx(f, 1, 8, 1, c->follow ? ui_fade(C_ADD, 0.5f) : C_LINE);
    DrawCircleV((Vector2){f.x + 10, f.y + f.height / 2}, 3.5f, c->follow ? C_ADD : C_FAINT);
    sv_tx(F_MONO, c->follow ? FOLLOWING : DETACHED, f.x + 19, sv_mid(f.y + f.height / 2, 12), 12,
          c->follow ? C_ADD : fh ? C_FG : C_DIM);
    float sx;
    if (c->none_yet) {
        static const char ny[] = "no call yet at this moment";
        sx = sv_tw(F_MONO, ny, 12.5f);
        sv_tx(F_MONO, ny, g->bar.width - 12 - sx, sv_mid(g->stats_y, 12.5f), 12.5f, C_DIM);
    } else sx = stats(c, g->bar.width - 12 - c->cache.sw, g->stats_y, true); /* geometry's width, this call's */
    const char *live = c->running && c->reloading ? "updating\xE2\x80\xA6" : c->reload_err ? "update failed" : NULL;
    if (live) { /* left of the stats */
        float lw = sv_tw(F_SANS, live, 12);
        sv_tx(F_SANS, live, g->bar.width - 12 - sx - 14 - lw, sv_mid(g->stats_y, 12), 12, c->reload_err && !c->running ? C_WARN : C_FAINT);
    }
}

/* ---- the side panel ---------------------------------------------------------------- */

static void h3(const char *s, float x, float y) { sv_txs(F_SANS_B, s, x, sv_mid(y + 8, 11), 11, 0.88f, C_FAINT); }

static int why_row(const CView *c, const Geo *g, Vector2 mp) {
    if (c->sel < 0 || !CheckCollisionPointRec(mp, g->why)) return -1;
    float y0 = g->why_rows;
    int k = (int)floorf((mp.y - y0) / WHY_ROW);
    return mp.y >= y0 && k >= 0 && k < c->a->items[c->sel].nlk ? k : -1;
}

static int act_row(const CView *c, const Geo *g, Vector2 mp) {
    if (!CheckCollisionPointRec(mp, g->list)) return -1;
    int k = (int)floorf((mp.y - g->list.y - 4 + c->act_scroll) / ACT_ROW);
    return mp.y >= g->list.y + 4 && k >= 0 && k < c->nfil ? k : -1;
}

static void draw_side(CView *c, const Geo *g, Vector2 mp) {
    const CtxAgent *a = c->a;
    Rectangle s = g->side;
    DrawRectangleRec(s, C_PANEL);
    DrawRectangle((int)s.x, (int)s.y, 1, (int)s.height, C_LINE);
    float x = s.x + 12, w = s.width - 24, y = s.y + 10;
    char b[256], v[32];
    /* what the context is made of */
    sv_when(v, sizeof v, a->calls[c->none_yet ? 0 : c->C].t);
    for (char *p = v; *p; p++) *p = *p >= 'a' && *p <= 'z' ? (char)(*p - 32) : *p; /* a heading's: upper case */
    if (c->none_yet) snprintf(b, sizeof b, "NO CALL YET \xC2\xB7 THE FIRST AT %s", v);
    else snprintf(b, sizeof b, "CONTEXT AT CALL %d \xC2\xB7 %s", c->C, v);
    h3(b, x, y);
    y += 16 + 6;
    double total = c->none_yet ? 0 : a->calls[c->C].total;
    if (!c->none_yet) DrawRectangleRounded((Rectangle){x, y, w, 10}, 0.6f, 4, C_BG);
    float sx = x;
    for (int k = 0; k < CK_COUNT; k++) {
        if (c->by[k] <= 0 || total <= 0) continue;
        float sw = (float)(c->by[k] / total) * w;
        DrawRectangleRec((Rectangle){sx, y, fminf(sw, x + w - sx), 10}, cv_kind_col(k));
        sx += sw;
    }
    y += 10 + 6;
    int j = 0;
    for (int k = 0; k < CK_COUNT && !c->none_yet; k++) {
        if (c->by[k] <= 0) continue;
        float cx = x + (float)(j % 2) * (w / 2 + 5), cy = y + (float)(j / 2) * 17 + 8.5f, cwid = w / 2 - 5;
        DrawRectangleRounded((Rectangle){cx, cy - 4, 8, 8}, 0.5f, 4, cv_kind_col(k));
        sv_tx(F_MONO, ctx_kind_names[k], cx + 14, sv_mid(cy, 11.5f), 11.5f, C_DIM);
        cl_fmt_k(v, sizeof v, c->by[k]);
        sv_tx(F_MONO, v, cx + cwid - sv_tw(F_MONO, v, 11.5f), sv_mid(cy, 11.5f), 11.5f, C_FAINT);
        j++;
    }
    sv_hline(s.x, s.x + s.width, s.y + g->sec1_h - 1, C_LINE);
    /* why it decided */
    if (c->sel >= 0) {
        const CtxItem *it = &a->items[c->sel];
        y = g->why.y + 10;
        h3("WHY IT DECIDED \xC2\xB7 LIKELY", x, y);
        y += 16 + 6;
        /* its source in up to two lines */
        int cap = 0;
        UiSpan *rows = NULL;
        const char *src = it->src ? it->src : "";
        int nr = ui_wrap(ui_mono, ui_mono_bold, src, NULL, (int)strlen(src), PX(12), w, 0, &rows, &cap);
        for (int r = 0; r < nr && r < 2; r++) {
            char line[512];
            int n = rows[r].len < 500 ? rows[r].len : 500;
            memcpy(line, src + rows[r].start, (size_t)n), line[n] = 0;
            if (r == 1 && nr > 2 && n < 500) { /* more: the second line ends in "…", within the width */
                char fitb[512];
                memcpy(line + n, "\xE2\x80\xA6", 4);
                sv_fit(F_MONO, fitb, sizeof fitb, line, 12, w);
                memcpy(line, fitb, sizeof fitb);
            }
            sv_tx(F_MONO, line, x, sv_mid(y + 8.5f + (float)r * 17, 12), 12, C_FG);
        }
        free(rows);
        if (it->made != c->C || c->none_yet) { /* L, or a step, moved off its call: it stays selected */
            snprintf(b, sizeof b, "selected action is from call %d", it->made);
            sv_tx(F_SANS, b, x, sv_mid(g->why_rows - 4 - 8.5f, 12), 12, C_FAINT);
        }
        y = g->why_rows;
        int hot = why_row(c, g, mp);
        if (!it->nlk) sv_tx(F_SANS, "no likely causes found in its context", x, sv_mid(y + WHY_ROW / 2, 12), 12, C_DIM);
        for (int k = 0; k < it->nlk; k++) {
            float cy = y + (float)k * WHY_ROW + WHY_ROW / 2;
            const char *why = it->lk[k].why ? it->lk[k].why : "";
            char fitb[300], t[512];
            sv_fit(F_MONO, fitb, sizeof fitb, why, 11, w * 0.6f);
            float rw = sv_tw(F_MONO, fitb, 11);
            sv_tx(F_MONO, fitb, x, sv_mid(cy, 11), 11, C_HOT);
            cl_title(&a->items[it->lk[k].item], t, sizeof t);
            sv_fit(F_MONO, b, sizeof b, t, 11.5f, w - rw - 6);
            sv_tx(F_MONO, b, x + rw + 6, sv_mid(cy, 11.5f), 11.5f, k == hot ? C_FG : C_DIM);
        }
        sv_hline(s.x, s.x + s.width, g->why.y + g->why.height - 1, C_LINE);
    }
    /* the actions */
    h3("ACTIONS", x, g->acts_y + 10);
    Rectangle f = g->filter;
    ui_box(f, 10 / f.height, C_RAISED, c->typing ? 2 : 1, c->typing ? C_FOCUS : C_LINE);
    float fy = f.y + f.height / 2;
    if (!c->q[0]) {
        sv_tx(F_MONO, "filter: Edit, make, path\xE2\x80\xA6", f.x + 7, sv_mid(fy, 12.5f), 12.5f, C_FAINT);
        if (c->typing) DrawRectangle((int)f.x + 6, (int)(fy - 8), 1, 16, C_FOCUS);
    } else {
        char fitb[256];
        sv_fit(F_MONO, fitb, sizeof fitb, c->q, 12.5f, f.width - 16);
        float qw = sv_tw(F_MONO, fitb, 12.5f);
        if (c->typing && c->all) DrawRectangle((int)f.x + 6, (int)(fy - 9), (int)qw + 2, 18, ui_fade(C_FOCUS, 0.35f));
        sv_tx(F_MONO, fitb, f.x + 7, sv_mid(fy, 12.5f), 12.5f, C_FG);
        if (c->typing && !c->all) DrawRectangle((int)(f.x + 8 + qw), (int)(fy - 8), 1, 16, C_FOCUS);
    }
    sv_hline(s.x, s.x + s.width, g->list.y - 1, C_LINE);
    Rectangle L = g->list;
    float full = (float)c->nfil * ACT_ROW + 14, maxs = fmaxf(0, full - L.height);
    if (c->act_follow && c->sel >= 0) { /* the selected action in view */
        for (int k = 0; k < c->nfil; k++)
            if (c->fil[k] == c->sel) {
                float ry = 4 + (float)k * ACT_ROW;
                if (ry < c->act_scroll || ry + ACT_ROW > c->act_scroll + L.height) c->act_scroll = ry - L.height / 2;
            }
        c->act_follow = false;
    }
    c->act_scroll = fmaxf(0, fminf(maxs, c->act_scroll));
    BeginScissorMode((int)L.x, (int)L.y, (int)L.width, (int)L.height);
    int hot = act_row(c, g, mp), k0 = (int)(c->act_scroll / ACT_ROW);
    for (int k = k0 > 0 ? k0 - 1 : 0; k < c->nfil; k++) {
        float ry = L.y + 4 + (float)k * ACT_ROW - c->act_scroll;
        if (ry > L.y + L.height) break;
        const CtxItem *it = &a->items[c->fil[k]];
        Rectangle r = {L.x + 6, ry, L.width - 12 - 6, ACT_ROW};
        bool sel = c->fil[k] == c->sel;
        if (sel || k == hot) DrawRectangleRounded(r, 8 / ACT_ROW, 6, C_RAISED);
        if (sel) DrawRectangleRoundedLinesEx(r, 8 / ACT_ROW, 6, 1, C_HOT);
        snprintf(v, sizeof v, "#%d", it->made);
        float cy = ry + ACT_ROW / 2;
        sv_tx(F_MONO, v, r.x + 6 + 38 - sv_tw(F_MONO, v, 12), sv_mid(cy, 12), 12, C_FAINT);
        char fitb[300];
        sv_fit(F_MONO, fitb, sizeof fitb, it->src ? it->src : "", 12, r.width - 6 - 38 - 6 - 6);
        sv_tx(F_MONO, fitb, r.x + 6 + 38 + 6, sv_mid(cy, 12), 12, it->made == c->C ? C_ADD : C_FG);
    }
    EndScissorMode();
    if (maxs > 0)
        ui_thumb(L.x + L.width - 8, L.y + 4, 4, L.height - 8, L.height, full, c->act_scroll, maxs, 20, ui_fade(C_DIM, 0.45f));
}

/* ---- the strip ------------------------------------------------------------------------ */

static double max_total(const CtxAgent *a) {
    double m = 0;
    for (int k = 0; k < a->ncalls; k++) m = fmax(m, a->calls[k].total);
    return m > 0 ? m : 1;
}

static int strip_at(const CView *c, const Geo *g, Vector2 mp) {
    if (!CheckCollisionPointRec(mp, g->bars)) return -1;
    ClStrip s = cl_strip(c->a->ncalls, g->bars.width);
    return cl_strip_hit(&s, mp.x - g->bars.x);
}

static void small_button(Rectangle q, Vector2 mp) {
    ui_box(q, 8 / q.height, CheckCollisionPointRec(mp, q) ? sv_brighter(C_RAISED, 0.5f) : C_RAISED, 1, C_LINE);
}

static void draw_strip(CView *c, const Geo *g, Vector2 mp) {
    const CtxAgent *a = c->a;
    Rectangle r = g->strip;
    DrawRectangleRec(r, C_PANEL);
    sv_hline(0, r.width, r.y, C_LINE);
    float cy = r.y + r.height / 2;
    sv_txs(F_SANS_B, "CALLS", g->lbl.x, sv_mid(cy, 11), 11, 0.88f, C_FAINT);
    small_button(g->prev, mp);
    small_button(g->play, mp);
    small_button(g->next, mp);
    sv_tx(F_MONO, "\xE2\x80\xB9", g->prev.x + 9, sv_mid(cy, 13), 13, C_DIM);
    sv_tx(F_MONO, "\xE2\x80\xBA", g->next.x + 9, sv_mid(cy, 13), 13, C_DIM);
    float px = g->play.x + g->play.width / 2;
    if (c->playing) DrawRectangle((int)px - 4, (int)cy - 5, 3, 10, C_DIM), DrawRectangle((int)px + 1, (int)cy - 5, 3, 10, C_DIM);
    else DrawTriangle((Vector2){px - 3.5f, cy - 5}, (Vector2){px - 3.5f, cy + 5}, (Vector2){px + 5, cy}, C_DIM);
    Rectangle B = g->bars;
    if (B.width > 0) {
        ClStrip s = cl_strip(a->ncalls, B.width);
        double mx = max_total(a);
        float bw = fmaxf(1, s.bw), base = B.y + B.height - 4;
        for (int i = 0; i < a->ncalls; i++) {
            float x = B.x + cl_strip_x(&s, i), h = (B.height - 12) * (float)(a->calls[i].total / mx);
            if (a->calls[i].noresp) DrawRectangleLinesEx((Rectangle){x, base - h, bw, h}, 0.8f, C_FAINT);
            else DrawRectangleRec((Rectangle){x, base - h, bw, h}, UT_PLAIN);
        }
        for (int i = 0; i < a->ncalls; i++)
            if (a->calls[i].comp) DrawRectangleRec((Rectangle){B.x + cl_strip_x(&s, i) - 1, B.y + 2, 2, B.height - 4}, C_DEL);
        float cw = fmaxf(2, s.bw);
        if (c->sel >= 0) DrawRectangleRec((Rectangle){B.x + cl_strip_x(&s, a->items[c->sel].made), B.y + B.height - 3, cw, 3}, C_HOT);
        Rectangle cur = {B.x + cl_strip_x(&s, c->C) - (s.bw < 3 ? 1 : 0), B.y, cw, B.height};
        if (!c->none_yet) DrawRectangleRec(cur, (Color){242, 165, 65, 51}), DrawRectangleLinesEx(cur, 1, C_HOT);
    }
    if (c->none_yet) {
        char t[32], b[96];
        fmt_clock(t, sizeof t, a->calls[0].t, "%H:%M");
        snprintf(b, sizeof b, "no call yet \xC2\xB7 the first at %s", t);
        tx_run(F_MONO, b, g->cur.x, g->cur.y + g->cur.height / 2, 12, C_DIM, true);
        return;
    }
    /* #C time · tokens · +in −out · compaction */
    const CtxCall *C = &a->calls[c->C];
    char t[32], k[32], b[160];
    float x = g->cur.x, ty = g->cur.y + g->cur.height / 2;
    snprintf(b, sizeof b, "#%d", c->C);
    x += tx_run(F_MONO_B, b, x, ty, 12, C_FG, true);
    fmt_clock(t, sizeof t, C->t, "%H:%M");
    cl_fmt_k(k, sizeof k, C->total);
    snprintf(b, sizeof b, " %s \xC2\xB7 %s tok \xC2\xB7 +%d \xE2\x88\x92%d%s", t, k, c->inn, c->outn, C->comp ? " \xC2\xB7 " : "");
    char fitb[200];
    sv_fit(F_MONO, fitb, sizeof fitb, b, 12, g->cur.x + g->cur.width - x);
    x += tx_run(F_MONO, fitb, x, ty, 12, C_DIM, true);
    if (C->comp && !strcmp(fitb, b) && x + sv_tw(F_MONO_B, "compaction", 12) <= g->cur.x + g->cur.width)
        tx_run(F_MONO_B, "compaction", x, ty, 12, C_FG, true);
}

static void draw_hint(const Geo *g) {
    Rectangle r = g->hint;
    DrawRectangleRec(r, C_WIN);
    sv_hline(0, r.width, r.y, C_LINE);
    static const char *const keys[][2] = {
        {"drag", "background pans, a title moves a window"}, {"wheel", "zoom"}, {"corner", "resize"}, {"chip", "opens"},
        {"\xE2\x86\x90 \xE2\x86\x92 space", "calls, play"}, {"L", "follow the stage"}, {"/", "filter"}, {"f a", "fit"},
        {"X / Esc", "back"}};
    sv_hints(keys, 9, 12, r.y + r.height / 2, r.width - 12, 16);
}

/* ---- the stage ----------------------------------------------------------------------- */

/* A dashed quadratic from the agent to (x, y), as the draft's links. */
static void dashed_link(float x, float y, Color col, const char *label) {
    Vector2 p0 = {0, 0}, q = {x / 2 - y * 0.15f, y / 2 + x * 0.15f}, p2 = {x, y}, prev = p0;
    float run = 0;
    for (int j = 1; j <= 48; j++) {
        float t = (float)j / 48, u = 1 - t;
        Vector2 cur = {u * u * p0.x + 2 * u * t * q.x + t * t * p2.x, u * u * p0.y + 2 * u * t * q.y + t * t * p2.y};
        float seg = hypotf(cur.x - prev.x, cur.y - prev.y);
        for (float s = 0; s < seg;) { /* 5 on, 4 off */
            float ph = fmodf(run + s, 9), step = fminf(seg - s, ph < 5 ? 5 - ph : 9 - ph);
            if (ph < 5) {
                Vector2 a = {prev.x + (cur.x - prev.x) * s / seg, prev.y + (cur.y - prev.y) * s / seg};
                Vector2 b = {prev.x + (cur.x - prev.x) * (s + step) / seg, prev.y + (cur.y - prev.y) * (s + step) / seg};
                DrawLineEx(a, b, 1.6f, col);
            }
            s += fmaxf(step, 0.01f);
        }
        run += seg;
        prev = cur;
    }
    sv_halo(F_MONO, label, x * 0.55f - y * 0.08f, y * 0.55f + x * 0.08f - PX(11) * 0.8f, 11, col, C_BG);
}

static void draw_links(CView *c) {
    const CtxAgent *a = c->a;
    if (c->sel < 0 || a->items[c->sel].made != c->C) return;
    const CtxItem *it = &a->items[c->sel];
    char b[300];
    float x, y, al;
    for (int k = 0; k < it->nlk; k++) {
        CNode *n = c->node[it->lk[k].item];
        if (!n) continue;
        cw_pos(n, c->clock, &x, &y, &al);
        snprintf(b, sizeof b, "likely \xC2\xB7 %s", it->lk[k].why ? it->lk[k].why : "");
        dashed_link(x, y, ui_fade(C_HOT, 0.9f * al), b);
    }
    CNode *s = c->node[c->sel];
    if (s) cw_pos(s, c->clock, &x, &y, &al), dashed_link(x, y, ui_fade(C_FG, 0.9f * al), "action");
}

static void draw_agent(CView *c) {
    const CtxAgent *a = c->a;
    const CtxCall *C = &a->calls[c->C];
    double share, W = call_window(C, &share);
    DrawCircleGradient(0, 0, 100, ui_fade(BLACK, 0.5f), ui_fade(BLACK, 0));
    DrawCircleGradient(0, 0, 70, HEX(0x1d2a3a), HEX(0x121a24));
    DrawRing((Vector2){0, 0}, 69.5f, 70.5f, 0, 360, 72, C_LINE);
    DrawRing((Vector2){0, 0}, 72, 76, 0, 360, 72, C_LINE);
    if (share > 0) DrawRing((Vector2){0, 0}, 72, 76, -90, -90 + (float)share * 360, 72, C_HOT);
    const char *nm = "agent";
    if (strcmp(a->kind, "main")) nm = strrchr(a->name, ':') ? strrchr(a->name, ':') + 1 : a->name;
    char fitb[128], m[128], t1[32], t2[32];
    sv_fit(F_MONO_B, fitb, sizeof fitb, nm, 13, 120);
    sv_txc(F_MONO_B, fitb, 0, -15, 13, C_FG);
    const char *model = C->model ? C->model : "";
    if (!strncmp(model, "claude-", 7)) model += 7;
    sv_fit(F_MONO, m, sizeof m, model, 11, 120);
    sv_txc(F_MONO, m, 0, 4, 11, C_DIM);
    cl_fmt_k(t1, sizeof t1, C->total);
    cl_fmt_k(t2, sizeof t2, W);
    snprintf(m, sizeof m, "%s / %s", t1, t2);
    sv_txc(F_MONO, m, 0, 20, 11, C_DIM);
}

/* The window under world point p: the top one drawn there, -1. */
static int window_at(CView *c, Vector2 p, int *zone, int *line) {
    float x, y, al;
    CNode *top = c->dn >= 0 ? c->node[c->dn] : NULL;
    if (top) { /* the dragged one is on top */
        cw_pos(top, c->clock, &x, &y, &al);
        if ((*zone = cw_hit(top, x, y, p, line))) return top->item;
    }
    for (int k = c->nshown - 1; k >= 0; k--) {
        CNode *n = c->zs[k];
        if (!n->vis) continue;
        cw_pos(n, c->clock, &x, &y, &al);
        if ((*zone = cw_hit(n, x, y, p, line))) return n->item;
    }
    *zone = CW_NONE;
    return -1;
}

/* ---- input ---------------------------------------------------------------------------- */

static void scrub(CView *c, const Geo *g, float x) {
    ClStrip s = cl_strip(c->a->ncalls, g->bars.width);
    int i = cl_strip_hit(&s, x - g->bars.x);
    if (i >= 0 && i != c->C) show(c, i, false);
}

static void switch_agent(CView *c, int k) {
    if (k < 0 || k >= c->nrefs || k == cur_ref(c)) return;
    /* the call at the current position: the last one when following the end */
    double t = c->C >= c->a->ncalls - 1 ? 0 : c->a->calls[c->C].t;
    bool follow = c->follow;
    char *path = xstrdup(c->refs[k].path); /* cv_open's swap_in can free refs */
    cv_open(c, path, CV_NONE, -1, follow ? c->stage_t : t);
    free(path);
    c->follow = follow; /* following or not, as it was */
}

static int pick_row(const CView *c, const Geo *g, Vector2 mp) {
    if (!c->picker || !CheckCollisionPointRec(mp, g->picklist)) return -1;
    int k = (int)floorf((mp.y - g->picklist.y - 4 + c->pick_scroll) / PICK_ROW);
    return k >= 0 && k < c->nrefs ? k : -1;
}

/* The call before (d -1) or after (+1): the view's own. */
static void step(CView *c, int d) { stop(c), c->follow = false, show(c, c->C + d, false); }

static void open_window(CView *c, CNode *n, int ticks) {
    c->ov[n->item] = 1;
    set_open(c, n, true);
    relayout(c, ticks);
}

static void press(CView *c, const Geo *g, Vector2 mp) {
    c->press = mp, c->moved = false, c->drag = D_EAT, c->dn = -1;
    if (c->picker) { /* a pick, or a click away: either closes it */
        int k = pick_row(c, g, mp);
        c->picker = false;
        if (k >= 0) switch_agent(c, k);
        return;
    }
    bool in_filter = CheckCollisionPointRec(mp, g->filter);
    if (c->typing && !in_filter) c->typing = false;
    if (in_filter) {
        c->typing = true, c->all = false;
        return;
    }
    if (CheckCollisionPointRec(mp, g->pick)) { c->picker = c->nrefs > 0, c->pick_scroll = 0; return; }
    if (CheckCollisionPointRec(mp, g->recent)) { fit(c, FIT_RECENT, true); return; }
    if (CheckCollisionPointRec(mp, g->all)) { fit(c, FIT_ALL, true); return; }
    if (CheckCollisionPointRec(mp, g->follow)) { stop(c), c->follow = true; return; }
    if (CheckCollisionPointRec(mp, g->prev)) { step(c, -1); return; }
    if (CheckCollisionPointRec(mp, g->play)) { play(c); return; }
    if (CheckCollisionPointRec(mp, g->next)) { step(c, 1); return; }
    if (CheckCollisionPointRec(mp, g->bars)) {
        stop(c);
        c->follow = false; /* a scrub: the view's own call */
        c->drag = D_SCRUB;
        scrub(c, g, mp.x);
        return;
    }
    int k = why_row(c, g, mp);
    if (k >= 0) { /* its window open, in view */
        CNode *n = c->node[c->a->items[c->sel].lk[k].item];
        if (n && !n->open) open_window(c, n, 40);
        if (n) center_on(c, n);
        return;
    }
    if ((k = act_row(c, g, mp)) >= 0) { select_action(c, c->fil[k], false); return; }
    if (!CheckCollisionPointRec(mp, g->stage)) return;
    int zone, line = 0, it = window_at(c, to_world(c, g, mp), &zone, &line);
    CNode *n = it >= 0 ? c->node[it] : NULL;
    if (!n) { /* the background: pan */
        c->drag = D_PAN, c->x0 = c->tx, c->y0 = c->ty, c->cam.on = false;
        return;
    }
    float x, y, al;
    cw_pos(n, c->clock, &x, &y, &al);
    if (zone == CW_TITLE || zone == CW_GRIP) { /* to the front, and it stays there */
        n->z = c->zseq++;
        qsort(c->zs, (size_t)c->nshown, sizeof *c->zs, cmp_z);
    }
    switch (zone) {
    case CW_WHY: select_action(c, it, false); break;
    case CW_FOLD:
        c->ov[it] = 0;
        set_open(c, n, false);
        relayout(c, 40);
        break;
    case CW_GRIP: /* the top-left corner stays */
        n->x = x, n->y = y, n->t0 = -1;
        c->drag = D_RESIZE, c->dn = it;
        c->x0 = x - n->w / 2, c->y0 = y - n->h / 2, c->w0 = n->w, c->h0 = n->h;
        break;
    case CW_TITLE:
        n->x = x, n->y = y, n->t0 = -1;
        c->drag = D_MOVE, c->dn = it, c->x0 = x, c->y0 = y;
        break;
    case CW_CODE: {
        char path[PATH_MAX];
        if (file_at(c->root, c->a->items[it].path, path, sizeof path)) nv_open(c->root, path, line);
        break;
    }
    case CW_TOOL: cw_toggle_tool(n, line); break;
    default: break;
    }
}

static void drag(CView *c, const Geo *g, Vector2 mp) {
    float dx = (mp.x - c->press.x) / c->k, dy = (mp.y - c->press.y) / c->k;
    if (fabsf(dx) + fabsf(dy) > 3) c->moved = true;
    CNode *n = c->dn >= 0 ? c->node[c->dn] : NULL;
    switch (c->drag) {
    case D_PAN: c->tx = c->x0 + (mp.x - c->press.x), c->ty = c->y0 + (mp.y - c->press.y); break;
    case D_SCRUB: scrub(c, g, mp.x); break;
    case D_MOVE:
        if (n) n->x = c->x0 + dx, n->y = c->y0 + dy, n->t0 = -1;
        break;
    case D_RESIZE:
        if (n) {
            n->w = fmaxf(CL_MIN_W, c->w0 + dx), n->h = fmaxf(CL_MIN_H, c->h0 + dy);
            n->x = c->x0 + n->w / 2, n->y = c->y0 + n->h / 2, n->t0 = -1;
        }
        break;
    }
}

static void release(CView *c) {
    CNode *n = c->dn >= 0 ? c->node[c->dn] : NULL;
    if (n && c->drag == D_MOVE) {
        if (c->moved) c->pin[2 * n->item] = n->x, c->pin[2 * n->item + 1] = n->y; /* stays where it is put */
        else if (!n->open) open_window(c, n, 60);                                 /* a click on a chip */
    } else if (n && c->drag == D_RESIZE) {
        c->size[2 * n->item] = n->w, c->size[2 * n->item + 1] = n->h;
        c->pin[2 * n->item] = n->x, c->pin[2 * n->item + 1] = n->y;
    }
    c->drag = D_NONE, c->dn = -1;
}

static void wheel(CView *c, const Geo *g, Vector2 mp, float w) {
    if (c->picker && CheckCollisionPointRec(mp, g->picklist)) {
        c->pick_scroll = fmaxf(0, fminf((float)c->nrefs * PICK_ROW + 8 - g->picklist.height, c->pick_scroll - w * 3 * PICK_ROW));
        return;
    }
    if (CheckCollisionPointRec(mp, g->list)) { c->act_scroll -= w * 3 * ACT_ROW; return; }
    if (!CheckCollisionPointRec(mp, g->stage)) return;
    Vector2 p = to_world(c, g, mp);
    int zone, line, it = window_at(c, p, &zone, &line);
    if (it >= 0) {
        CNode *n = c->node[it];
        float x, y, al;
        cw_pos(n, c->clock, &x, &y, &al);
        if (cw_wheel(n, x, y, p, w, IsKeyDown(KEY_LEFT_SHIFT) || IsKeyDown(KEY_RIGHT_SHIFT))) return;
    }
    float k = fmaxf(K_MIN, fminf(K_MAX, c->k * powf(1.15f, w))); /* zoom about the mouse */
    c->cam.on = false;
    c->tx = mp.x - g->stage.x - p.x * k, c->ty = mp.y - g->stage.y - p.y * k, c->k = k;
}

bool cv_keys(CView *c) {
    if (!c->open) return false;
    bool ctrl = IsKeyDown(KEY_LEFT_CONTROL) || IsKeyDown(KEY_RIGHT_CONTROL), esc = IsKeyPressed(KEY_ESCAPE);
    if (c->typing) { /* the filter's: Esc leaves it first */
        if (ui_edit_line(c->q, sizeof c->q, INT_MAX, &c->all, ctrl)) filter(c), c->act_scroll = 0;
        if (IsKeyPressed(KEY_ENTER) || IsKeyPressed(KEY_KP_ENTER) || esc) c->typing = false;
        return esc;
    }
    if (!shown(c)) {
        if (esc) cv_close(c);
        return esc;
    }
    if (c->picker) {
        if (esc) c->picker = false;
        return esc;
    }
    for (int ch; (ch = GetCharPressed());)
        if (ch == '/') c->typing = true, c->all = c->q[0] != 0;
    if (ctrl && IsKeyPressed(KEY_F)) c->typing = true, c->all = c->q[0] != 0;
    if (c->typing) return false;
    if (IsKeyPressed(KEY_LEFT) || IsKeyPressedRepeat(KEY_LEFT)) step(c, -1);
    if (IsKeyPressed(KEY_RIGHT) || IsKeyPressedRepeat(KEY_RIGHT)) step(c, 1);
    if (IsKeyPressed(KEY_L)) stop(c), c->follow = true; /* the stage's clock again */
    if (IsKeyPressed(KEY_SPACE)) play(c);
    if (!ctrl && IsKeyPressed(KEY_F)) fit(c, FIT_RECENT, true);
    if (!ctrl && IsKeyPressed(KEY_A)) fit(c, FIT_ALL, true);
    if (esc) {
        if (c->sel >= 0) c->sel = -1, show(c, c->C, false);
        else if (c->q[0]) c->q[0] = 0, filter(c), c->act_scroll = 0;
        else cv_close(c);
    }
    return esc;
}

/* ---- tooltips ---------------------------------------------------------------------- */

/* A box of lines at `at`: a bold first one, then text's (either may be ""). */
static void tip_lines(const char *bold, const char *text, Vector2 at) {
    float W = (float)GetScreenWidth(), H = (float)GetScreenHeight(), css = 12, w = 0, lh = 17, maxw = fminf(520, W - 24);
    char lines[12][300];
    int n = 0;
    if (*bold) sv_fit(F_MONO_B, lines[n], sizeof lines[n], bold, css, maxw), n++;
    for (const char *p = text; *p && n < 12;) {
        const char *e = strchr(p, '\n');
        size_t l = e ? (size_t)(e - p) : strlen(p);
        char raw[600];
        snprintf(raw, sizeof raw, "%.*s", (int)(l < 599 ? l : 599), p);
        sv_fit(F_MONO, lines[n], sizeof lines[n], raw, css, maxw);
        n++;
        if (!e) break;
        p = e + 1;
    }
    for (int i = 0; i < n; i++) w = fmaxf(w, sv_tw(i || !*bold ? F_MONO : F_MONO_B, lines[i], css));
    float bw = w + 16, bh = (float)n * lh + 8, x = fminf(at.x, W - bw - 8), y = at.y;
    if (y + bh > H - 4) y = at.y - bh - 30;
    if (x < 4) x = 4;
    ui_box((Rectangle){x, y, bw, bh}, 10 / fminf(bw, bh), HEX(0x0b0f15), 1, C_LINE);
    for (int i = 0; i < n; i++)
        sv_tx(i || !*bold ? F_MONO : F_MONO_B, lines[i], x + 8, sv_mid(y + 4 + lh * ((float)i + 0.5f), css), css,
              i || !*bold ? C_DIM : C_FG);
}

static void draw_tips(CView *c, const Geo *g, Vector2 mp, float dt) {
    const CtxAgent *a = c->a;
    char b[2048];
    if (c->drag != D_NONE && c->drag != D_EAT) return;
    if (c->hov >= 0 && c->hov_zone == CW_TITLE && c->node[c->hov] && !c->node[c->hov]->open) { /* a chip: its header */
        CNode *n = c->node[c->hov];
        size_t o = 0;
        b[0] = 0;
        for (int i = 0; i < n->nmeta && o + 1 < sizeof b; i++) o += (size_t)snprintf(b + o, sizeof b - o, "%s", n->meta[i].s);
        float x, y, al;
        cw_pos(n, c->clock, &x, &y, &al);
        Vector2 s = GetWorldToScreen2D((Vector2){x - n->w / 2, y + n->h / 2}, camera(c, g));
        tip_lines(n->title, b, (Vector2){s.x, s.y + 6});
        c->tip.key[0] = 0;
        return;
    }
    int bar = strip_at(c, g, mp);
    if (bar >= 0) { /* at once, as a chip's */
        char t[32], k[32], head[64];
        fmt_clock(t, sizeof t, a->calls[bar].t, "%H:%M");
        cl_fmt_k(k, sizeof k, a->calls[bar].total);
        snprintf(head, sizeof head, "#%d %s", bar, t);
        snprintf(b, sizeof b, " \xC2\xB7 %s tok%s%s", k, a->calls[bar].comp ? " \xC2\xB7 a compaction" : "",
                 a->calls[bar].noresp ? " \xC2\xB7 no response recorded" : "");
        sv_tooltip(head, b, mp);
        c->tip.key[0] = 0;
        return;
    }
    const char *title = NULL;
    if (c->hov >= 0 && c->hov_zone == CW_STALE && c->node[c->hov]) {
        cl_stale_text(a, c->hov, c->node[c->hov]->f.nstale, b, sizeof b);
        title = b;
    }
    struct { Rectangle r; const char *t; } ts[] = {
        {g->lbl, "One bar per API call; height: context size; red: a compaction; hollow: no response recorded"},
        {g->prev, "Previous call (\xE2\x86\x90)"}, {g->play, "Play (space)"}, {g->next, "Next call (\xE2\x86\x92)"},
        {g->recent, "Zoom to the open windows (f)"}, {g->all, "Zoom to every window (a)"},
        {g->est, "This transcript has no request records: the messages come from the conversation records, the "
                 "system prompt and tools from the first call's usage"}};
    for (size_t i = 0; !title && i < sizeof ts / sizeof *ts; i++)
        if (ts[i].r.width > 0 && CheckCollisionPointRec(mp, ts[i].r)) title = ts[i].t;
    if (!title) { c->tip.key[0] = 0, c->tip.t = 0; return; }
    if (strncmp(c->tip.key, title, sizeof c->tip.key - 1)) snprintf(c->tip.key, sizeof c->tip.key, "%.95s", title), c->tip.t = 0;
    else c->tip.t += dt;
    if (c->tip.t > TIP_DELAY) tip_lines("", title, (Vector2){mp.x + 12, mp.y + 18});
}

static void draw_picker(CView *c, const Geo *g, Vector2 mp) {
    Rectangle r = g->picklist;
    DrawRectangleRounded((Rectangle){r.x + 2, r.y + 3, r.width, r.height}, 12 / fminf(r.width, r.height), 6, ui_fade(BLACK, 0.35f));
    ui_box(r, 12 / fminf(r.width, r.height), HEX(0x0b0f15), 1, C_LINE);
    BeginScissorMode((int)r.x + 1, (int)r.y + 1, (int)r.width - 2, (int)r.height - 2);
    int hot = pick_row(c, g, mp), cur = cur_ref(c);
    for (int k = 0; k < c->nrefs; k++) {
        float y = r.y + 4 + (float)k * PICK_ROW - c->pick_scroll;
        if (y + PICK_ROW < r.y || y > r.y + r.height) continue;
        Rectangle row = {r.x + 4, y, r.width - 8, PICK_ROW};
        if (k == cur) DrawRectangleRounded(row, 8 / PICK_ROW, 6, sv_brighter(C_RAISED, 0.4f)),
                      DrawRectangle((int)row.x, (int)row.y + 4, 2, (int)PICK_ROW - 8, C_FOCUS);
        else if (k == hot) DrawRectangleRounded(row, 8 / PICK_ROW, 6, C_RAISED);
        char lb[600], fitb[600];
        ref_label(c, k, lb, sizeof lb);
        sv_fit(F_MONO, fitb, sizeof fitb, lb, 12.5f, row.width - 20);
        sv_tx(F_MONO, fitb, row.x + 10, sv_mid(y + PICK_ROW / 2, 12.5f), 12.5f, k == cur || k == hot ? C_FG : C_SOFT);
    }
    EndScissorMode();
}

/* ---- the frame ------------------------------------------------------------------------ */

/* Live: the transcript grew; reloaded once it stops for a moment (or has waited long enough). */
static void live(CView *c) {
    if (c->running || c->clock - c->polled < POLL) return;
    c->polled = c->clock;
    long long size;
    double mt;
    file_stat(c->a->path, &size, &mt);
    if (size < 0) return;
    bool pending = c->seen_size != c->src_size || c->seen_mtime != c->src_mtime;
    if (size != c->seen_size || mt != c->seen_mtime) {
        if (!pending) c->first_change = c->clock;
        c->seen_size = size, c->seen_mtime = mt, c->changed = c->clock;
        pending = size != c->src_size || mt != c->src_mtime;
    }
    /* stopped growing for a moment, waited long enough, or last written long ago (a reopen: no wait) */
    if (pending && (c->clock - c->changed >= DEBOUNCE || c->clock - c->first_change >= DEBOUNCE_MAX ||
                    now_wall() - c->seen_mtime >= DEBOUNCE))
        start_reload(c);
}

static void draw_loading(CView *c, const Geo *g) {
    draw_bar(c, g, (Vector2){-1, -1});
    DrawRectangleRec(g->stage, C_BG);
    DrawRectangleRec(g->side, C_PANEL);
    DrawRectangleRec(g->strip, C_PANEL);
    draw_hint(g);
    float cx = g->stage.x + g->stage.width / 2, cy = g->stage.y + g->stage.height / 2;
    char line[600], fitb[600];
    const char *path = c->want.path ? c->want.path : c->load.path ? c->load.path : "";
    const char *name = strrchr(path, '/') ? strrchr(path, '/') + 1 : path;
    if (loading(c)) {
        int dots = (int)(c->clock * 3) % 4;
        snprintf(line, sizeof line, "Loading the context%.*s", dots, "...");
        float w = sv_tw(F_SANS, "Loading the context...", 15);
        sv_tx(F_SANS, line, cx - w / 2, sv_mid(cy - 12, 15), 15, C_DIM);
    } else {
        snprintf(line, sizeof line, "No context to show: %s", c->err ? c->err : "nothing loaded");
        sv_fit(F_SANS, fitb, sizeof fitb, line, 15, g->stage.width - 40);
        sv_txc(F_SANS, fitb, cx, cy - 12, 15, C_DIM);
        sv_txc(F_MONO, "Esc: back", cx, cy + 36, 12, C_FAINT);
    }
    sv_fit(F_MONO, fitb, sizeof fitb, name, 12, g->stage.width - 40);
    sv_txc(F_MONO, fitb, cx, cy + 14, 12, C_FAINT);
}

/* AIG_CTX_BENCH: one call a frame, then the times to stderr. */
static int cmp_d(const void *x, const void *y) {
    double a = *(const double *)x, b = *(const double *)y;
    return (a > b) - (a < b);
}

static void bench_step(CView *c, double frame_ms) {
    int n = c->a->ncalls - c->bench.from;
    if (c->bench.all) { /* then everything in view, zoomed out: 30 frames of the last call */
        c->bench.frame[--c->bench.all] = frame_ms;
        if (c->bench.all) return;
        int m = n < 30 ? n : 30, vis = 0;
        for (int k = 0; k < c->nshown; k++) vis += c->zs[k]->vis;
        qsort(c->bench.frame, (size_t)m, sizeof(double), cmp_d);
        fprintf(stderr, "context bench: zoomed out (fit all, k %.2f), %d of %d windows in view: frame (cv_frame) ms median "
                "%.1f p90 %.1f max %.1f\n", c->k, vis, c->nshown, c->bench.frame[m / 2], c->bench.frame[(int)(0.9 * (m - 1))],
                c->bench.frame[m - 1]);
        c->bench.n = 0;
        return;
    }
    if (!c->bench.show) c->bench.show = xcalloc((size_t)n + 1, sizeof(double)), c->bench.frame = xcalloc((size_t)n + 1, sizeof(double));
    if (c->bench.at >= 0) c->bench.frame[c->bench.at] = frame_ms;
    if (++c->bench.at >= n) {
        double t0 = GetTime();
        relayout(c, 24);
        double rest = (GetTime() - t0) * 1e3;
        LWin *w = xmalloc(sizeof *w * (size_t)c->nshown);
        int open = 0;
        for (int k = 0; k < c->nshown; k++) {
            CNode *d = c->node[c->shown[k]];
            w[k] = (LWin){.x = d->x, .y = d->y, .w = d->w, .h = d->h};
            open += d->open;
        }
        int over = lay_wins_overlaps(w, c->nshown, CL_HUB);
        free(w);
        qsort(c->bench.show, (size_t)n, sizeof(double), cmp_d);
        qsort(c->bench.frame, (size_t)n, sizeof(double), cmp_d);
#define Q(v, p) (v)[(int)((p) * (n - 1))]
        fprintf(stderr, "context bench: %d steps over calls %d-%d, %d windows at the end (%d open, overlapping pairs %d); "
                "show() ms median %.1f p90 %.1f max %.1f; frame (cv_frame) ms median %.1f p90 %.1f max %.1f; "
                "the full layout at rest %.0f ms\n", n, c->bench.from, c->a->ncalls - 1, c->nshown, open, over,
                Q(c->bench.show, 0.5), Q(c->bench.show, 0.9), c->bench.show[n - 1], Q(c->bench.frame, 0.5),
                Q(c->bench.frame, 0.9), c->bench.frame[n - 1], rest);
#undef Q
        fit(c, FIT_ALL, false);
        c->bench.all = n < 30 ? n : 30;
        return;
    }
    double t0 = GetTime();
    show(c, c->bench.from + c->bench.at, false);
    c->bench.show[c->bench.at] = (GetTime() - t0) * 1e3;
}

void cv_frame(CView *c, Vector2 mp, float dt) {
    double f0 = GetTime();
    c->clock += dt;
    if (!c->open) return;
    Geo g = geometry(c);
    c->sw = g.stage.width, c->sh = g.stage.height; /* before a load is in: it fits the view to it */
    swap_in(c);
    g = geometry(c);
    c->sw = g.stage.width, c->sh = g.stage.height;
    if (!shown(c)) {
        ui_hand(false);
        draw_loading(c, &g);
        return;
    }
    if (c->bench.n > 0 && c->bench.at < 0) bench_step(c, 0);
    /* input: the side and the strip, then the stage */
    if (IsMouseButtonPressed(MOUSE_BUTTON_LEFT)) press(c, &g, mp), g = geometry(c);
    if (c->drag != D_NONE && IsMouseButtonDown(MOUSE_BUTTON_LEFT)) drag(c, &g, mp);
    if (c->drag != D_NONE && !IsMouseButtonDown(MOUSE_BUTTON_LEFT)) release(c);
    float w = GetMouseWheelMove();
    if (w != 0) wheel(c, &g, mp, w);
    if (!shown(c)) return; /* an agent switch asked: it loads */
    /* time: the stage's clock when following it, the view's own when playing */
    int f = cl_follow(c->a, c->follow, c->playing, c->stage_t, c->C);
    c->none_yet = f == CL_NO_CALL; /* drawn as such, the windows of the call before kept for when it comes */
    if (f >= 0 && f != c->C) show(c, f, false);
    live(c);
    if (c->playing && c->clock >= c->play_at) {
        if (c->C >= c->a->ncalls - 1) stop(c);
        else show(c, c->C + 1, false), c->play_at = c->clock + PLAY_STEP; /* no burst after a stall */
    }
    if (c->rest_at > 0 && c->clock >= c->rest_at) c->rest_at = 0, relayout(c, 24);
    if (c->fit_at > 0 && c->clock >= c->fit_at) c->fit_at = 0, fit(c, FIT_LINKS, true);
    for (int k = 0; k < c->ngone;) { /* gone once faded */
        if (c->clock - c->gone[k]->gone > cw_life(c->gone[k]) + 0.05) free_node(c->gone[k]), c->gone[k] = c->gone[--c->ngone];
        else k++;
    }
    cam_step(c);
    g = geometry(c);
    Camera2D cam = camera(c, &g);
    /* what is in view: laid out and drawn; its header the call's */
    float vx0 = -c->tx / c->k, vy0 = -c->ty / c->k, vx1 = (g.stage.width - c->tx) / c->k, vy1 = (g.stage.height - c->ty) / c->k;
    for (int k = 0; k < c->nshown; k++) {
        CNode *n = c->zs[k];
        float x, y, al;
        cw_pos(n, c->clock, &x, &y, &al);
        n->vis = !c->none_yet && x + n->w / 2 > vx0 && x - n->w / 2 < vx1 && y + n->h / 2 > vy0 && y - n->h / 2 < vy1;
        if (n->vis && n->hdr_c != c->C) cw_header(n, c->a, c->C, c->by_item);
    }
    bool over_stage = CheckCollisionPointRec(mp, g.stage) && !(c->picker && CheckCollisionPointRec(mp, g.picklist));
    Vector2 mw = over_stage ? to_world(c, &g, mp) : (Vector2){-1e9f, -1e9f};
    int line;
    c->hov = over_stage && (c->drag == D_NONE || c->drag == D_EAT) ? window_at(c, mw, &c->hov_zone, &line) : -1;
    char path[PATH_MAX];
    bool nvim = c->hov >= 0 && c->hov_zone == CW_CODE && file_at(c->root, c->a->items[c->hov].path, path, sizeof path) &&
                nv_find(c->root, NULL, 0);
    ui_hand(nvim);

    /* drawing */
    DrawRectangle(0, 0, GetScreenWidth(), GetScreenHeight(), C_WIN);
    Rectangle st = g.stage;
    DrawRectangleRec(st, C_BG);
    DrawCircleGradient((int)(st.x + st.width / 2), (int)(st.y + st.height / 2), 0.7f * hypotf(st.width, st.height) / 2,
                       HEX(0x121a25), C_BG);
    if (c->none_yet) { /* the stage's moment is before the agent's first call */
        char w[32], line[160];
        sv_when(w, sizeof w, c->a->calls[0].t);
        snprintf(line, sizeof line, "No call yet at this moment: the agent's first is at %s", w);
        sv_txc(F_SANS, line, st.x + st.width / 2, st.y + st.height / 2 - 12, 15, C_DIM);
        static const char hint[] = "it shows up as the stage's clock gets there; \xE2\x86\x90 \xE2\x86\x92 or the strip look at its calls";
        sv_txc(F_SANS, hint, st.x + st.width / 2, st.y + st.height / 2 + 14, 12, C_FAINT);
    }
    BeginMode2D(cam);
    if (!c->none_yet) draw_links(c), draw_agent(c);
    CwCtx x = {c->a, cam, st, mw, nvim, c->drag != D_NONE && c->drag != D_EAT};
    float px, py, al;
    for (int k = 0; k < c->ngone && !c->none_yet; k++) {
        CNode *n = c->gone[k];
        float sc = cw_scale(n, c->clock);
        cw_pos(n, c->clock, &px, &py, &al);
        if (sc > 0.999f) { cw_draw(n, &x, px, py, al, false, false); continue; }
        CwCtx xs = x; /* folding into the summary: drawn shrunk about its centre */
        xs.cam = (Camera2D){.offset = GetWorldToScreen2D((Vector2){px, py}, cam), .target = {px, py}, .zoom = c->k * sc};
        EndMode2D();
        BeginMode2D(xs.cam);
        cw_draw(n, &xs, px, py, al, false, false);
        EndMode2D();
        BeginMode2D(cam);
    }
    CNode *top = c->dn >= 0 ? c->node[c->dn] : NULL;
    for (int k = 0; k < c->nshown; k++) {
        CNode *n = c->zs[k];
        if (!n->vis || n == top) continue;
        cw_pos(n, c->clock, &px, &py, &al);
        cw_draw(n, &x, px, py, al, c->linked[n->item] && n->item != c->sel, n->item == c->sel);
    }
    if (top) {
        if (top->hdr_c != c->C) cw_header(top, c->a, c->C, c->by_item);
        cw_draw(top, &x, top->x, top->y, 1, c->linked[top->item] && top->item != c->sel, top->item == c->sel);
    }
    EndMode2D();
    draw_bar(c, &g, mp);
    draw_side(c, &g, mp);
    draw_strip(c, &g, mp);
    draw_hint(&g);
    if (c->picker) draw_picker(c, &g, mp);
    else draw_tips(c, &g, mp, dt);
    if (c->bench.n > 0) bench_step(c, (GetTime() - f0) * 1e3);
}
