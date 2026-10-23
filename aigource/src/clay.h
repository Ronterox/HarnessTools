#ifndef AIG_CLAY_H
#define AIG_CLAY_H
#include <stdbool.h>
#include <stddef.h>

#include "context.h"
#include "slayout.h"

/* The context screen's rules that draw nothing, as the draft has them
 * (draft/context/context.tpl.html): what a call shows and what starts open,
 * window sizes, ranks and target distances for the layout (lay_wins), the
 * calls strip's bands, a window's header facts in the draft's words. Pure: no
 * raylib, so tests/test_clay.c checks them headless. Item and call indices
 * are CtxAgent's. */

#define CL_OPEN_MAX 10 /* default-open windows at most, besides the linked ones and the user's */
#define CL_HUB 170.0f  /* the agent's square in the layout */
#define CL_CHIP_H 24.0f
#define CL_MIN_W 160.0f /* a resized window's least size */
#define CL_MIN_H 60.0f

/* Call c's windows: its order, then what its response made that isn't in it
 * (drawn dashed: this call's response). out holds a->nitems; the count. */
int cl_shown(const CtxAgent *a, int c, const int *by_item, int *out);
/* The selected action sel's likely causes and itself, while c is the call
 * that made it: linked[item] set, the rest cleared (nitems); their count. */
int cl_linked(const CtxAgent *a, int sel, int c, unsigned char *linked);
/* An item's window starts open at call c (the draft's isOpenDefault). */
bool cl_open_default(const CtxItem *it, int c, bool linked, int nlinked);
/* open[k] for shown[k]: ov[item] where the user set it (0 a chip, 1 open, -1
 * not set), else the default, keeping CL_OPEN_MAX of the default-open ones
 * that aren't linked: this call's response first, then the newest. */
void cl_open(const CtxAgent *a, int c, const int *shown, int n, const signed char *ov, const unsigned char *linked,
             int nlinked, bool *open);
/* A window's title: the source, "error · " before a failed result's. */
void cl_title(const CtxItem *it, char *b, size_t n);
/* A window's default size: a chip's from its title, an open one's by kind and tool. */
void cl_size(const CtxItem *it, bool open, float *w, float *h);
/* Ranks and target distances (the draft's targets()): this call's response
 * first, then the linked, then newest entered first, then the call's order;
 * each ring holds the area of everything ranked before it. w[k] is
 * shown[k]'s, sized. */
void cl_targets(const CtxAgent *a, int c, const int *shown, int n, const unsigned char *linked, LWin *w);

/* The calls strip: d3.scaleBand over [0, w], inner padding 0.15 (none past
 * 300 calls). */
typedef struct {
    int n;
    float w, step, bw;
} ClStrip;
ClStrip cl_strip(int n, float w);
float cl_strip_x(const ClStrip *s, int i);
int cl_strip_hit(const ClStrip *s, float x); /* the call at x, clamped to the strip's */

/* A window's header facts at call c (the draft's facts()). */
typedef struct {
    double tok;   /* tokens: scaled at c, or the response's own when not in c's order */
    double share; /* of c's context (in order only) */
    bool in_order;
    int enter;    /* the call it entered, -1 (in order only) */
    int age_calls;
    double age_s;
    int state;    /* CS_*: in order only */
    bool priced;  /* the call's model has prices: cost is what holding it has cost so far */
    double cost;
    int nstale;   /* changes to its file up to c, the first nstale of it->st */
    bool known;   /* all of them its own, still in its context */
    bool dropped; /* thinking the API dropped at c */
} ClFacts;
void cl_facts(const CtxAgent *a, int item, int c, const int *by_item, ClFacts *f);

/* The header's meta row in the draft's words, as runs of one style; a line
 * may break before a run that starts a part (brk). The count. */
enum { CLS_FAINT, CLS_DIM, CLS_READ, CLS_WRITTEN, CLS_NEW, CLS_WARN };
typedef struct {
    char s[80];
    unsigned char style;
    bool brk;
} ClRun;
#define CL_RUNS 16
int cl_meta(const CtxAgent *a, const ClFacts *f, ClRun *r);
/* The stale changes' title: "HH:MM label (who)" a line each. */
void cl_stale_text(const CtxAgent *a, int item, int nstale, char *b, size_t n);

/* Tokens by kind at call c (by[CK_COUNT]). */
void cl_by_kind(const CtxAgent *a, int c, double *by);
/* Items call c added and dropped against the call it is compared with (its base, else the one before). */
void cl_inout(const CtxAgent *a, int c, int *in, int *out);
/* The actions in call order (made, then item): out holds nitems; the count. */
int cl_actions(const CtxAgent *a, int *out);
bool cl_match(const CtxItem *it, const char *q); /* q a substring of its source, any case ("" all) */
/* A reload (ctx_reload) keeps what the user did by item key: map[i], old's
 * item i as an item of nw, -1 when it's gone (old->nitems long). */
void cl_key_map(const CtxAgent *old, const CtxAgent *nw, int *map);
/* Per item: opened (1) or folded (0), -1; where it was dragged (x, y, NAN
 * not); what it was resized to (w, h, 0 not). And the selected action. */
typedef struct {
    signed char *ov;
    float *pin, *size;
    int sel;
} ClUser;
/* from's choices carried to to's items by map (to's arrays initialised, sized for its agent). */
void cl_carry(const int *map, int nold, const ClUser *from, ClUser *to);
/* The last call at or before epoch t, by the latest time up to each call
 * (calls needn't be in time order); CL_NO_CALL when t is before them all;
 * the last call for t <= 0. */
#define CL_NO_CALL (-1)
int cl_call_at(const CtxAgent *a, double t);
/* The call to show: following the stage's clock t (and not playing: play is
 * the view's own clock), cl_call_at's (CL_NO_CALL: none yet at that moment);
 * else cur. */
int cl_follow(const CtxAgent *a, bool follow, bool playing, double t, int cur);

/* The screen's worker runs one job at a time: a load, or a reload of the
 * agent shown. What an open of path does with it (cv_open): */
typedef struct {
    const char *shown;   /* the loaded agent's transcript; NULL none */
    const char *loading; /* the running job's; NULL none */
    bool reload;         /* the running job reloads the agent shown */
    bool cancelled;      /* it was cancelled (another agent was asked for) */
} ClWorker;
enum {
    CL_OPEN_KEEP,       /* it is loading: kept, opened where asked once in */
    CL_OPEN_SHOWN,      /* it is shown: to the call asked for, no load */
    CL_OPEN_SHOWN_DROP, /* the same, and the other agent loading is cancelled */
    CL_OPEN_LOAD,       /* loaded now */
    CL_OPEN_QUEUE,      /* the running job is cancelled; loaded once it's out */
};
int cl_open_job(const ClWorker *w, const char *path);
/* And what a finished job does (swap_in). Closing the screen cancels nothing. */
typedef struct {
    bool reload;    /* it reloaded the agent shown */
    bool cancelled;
    bool newer;     /* another agent was asked for meanwhile */
    bool ok;        /* it gave an agent with API calls */
    bool same;      /* a load of the agent already shown */
} ClDone;
enum {
    CL_JOB_INSTALL,     /* shown, a new agent */
    CL_JOB_KEEP,        /* shown, keeping what the user did (a reload, or the same agent) */
    CL_JOB_DROP,        /* thrown away */
    CL_JOB_RETRY,       /* thrown away, and the transcript still counts as changed: reloaded again */
    CL_JOB_FAIL,        /* "No context to show: why" */
    CL_JOB_RELOAD_FAIL, /* the agent stays, "update failed"; retried once it grows again */
};
int cl_job_done(const ClDone *d);

/* Numbers as the draft writes them: 1.2k, 12k, 3.4M; $0.012; 45 s, 12 min, 1.5 h; 3.2%, 0.05%. */
void cl_fmt_k(char *b, size_t n, double v);
void cl_fmt_usd(char *b, size_t n, double v);
void cl_fmt_min(char *b, size_t n, double s);
void cl_fmt_pct(char *b, size_t n, double share);

#endif
