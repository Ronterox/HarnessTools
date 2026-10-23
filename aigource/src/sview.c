#include "sview.h"

#include <limits.h>
#include <math.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#include "nvim.h"
#include "slayout.h"
#include "ssearch.h"
#include "sview_int.h"
#include "util.h"

/* Bands of the screen, top to bottom, as the draft's window: the bar (tabs,
 * heat modes), the path, the stage beside the inspector, the turn strip, the
 * hint row. */
#define BAR_ROW 36.0f
#define CRUMBS_H 40.0f
#define STRIP_H 64.0f
#define BARS_H 48.0f      /* the turn strip's tallest bar */
#define HINT_H 30.0f
#define INSP_W 420.0f
#define DIM_A 0.14f       /* nodes the selected turn didn't touch */
#define CW_CLIP 7.2f      /* the draft's clip(): px per character, whatever the size */
#define DEBOUNCE 3.0      /* live: rebuild this long after the last event... */
#define DEBOUNCE_MAX 15.0 /* ...or once events have waited this long */
#define SEARCH_WAIT 0.12  /* a content search starts this long after the last key */
#define NAMES_MAX 100     /* name results listed (all of them are emphasised) */
#define SHEAD_H 24.0f     /* the result list: a section's heading, */
#define SNAME_H 24.0f     /* a name, */
#define SHIT_H 40.0f      /* a line of a file */

static const char *const VIEW_KEY[] = {"icicle", "treemap", "floating"};
static const char *const VIEW_NAME[] = {"Icicle", "Treemap", "Floating nodes"};
static const char *const MODE_NAME[ST_NMODES] = {"turns", "lines", "risk", "rework", "fixes",
                                                 "context", "cost", "time", "age", "owner"};
static const char *const MODE_TITLE[ST_NMODES] = {
    "How many turns changed it",
    "Lines added and removed",
    "Turns \xC3\x97 complexity: changed often and hard to follow",
    "Lines rewritten within 2 turns of being written",
    "Turns that were fixes: the prompt reports a problem",
    "Reads and searches the agent needed before touching it, per turn",
    "Output tokens the agent spent on the changes to it",
    "Agent time spent on the changes to it",
    "How recently today's lines were written: newest is hottest",
    "Which session or subagent wrote most of today's lines",
};
static const char *const MODE_LO[ST_NMODES] = {"no turn", "no lines", "calm",  "kept",  "no fix",
                                               "self-evident", "cheap", "quick", "oldest", ""};
static const char *const MODE_HI[ST_NMODES] = {
    "most turns",     "most +/\xE2\x88\x92",      "most turns \xC3\x97 complexity", "most lines rewritten soon",
    "most fix turns", "most reading to change", "most output tokens",           "most agent time",
    "newest",         "",
};

/* Search: what matched by name, by content, or holds such a node (per node). */
enum { QH_NAME = 1, QH_TEXT = 2, QH_IN = 4 };

typedef struct { int node; SsName m; } NameHit;

typedef struct {
    SsEngine *eng;        /* content searches, on its own thread; made with the first build */
    bool open;            /* the box has focus: keys go only to it */
    bool all;             /* reopened: the query is selected, typing replaces it */
    char q[SS_QBYTES];
    double edited;        /* when the query last changed */
    bool names_due, content_due;
    unsigned gen;         /* the content search asked last, 0 none */
    const StMetrics *m;   /* the build the files, names and marks are of */
    SsNames *ix;          /* its node paths, folded (made with it, on the build's thread) */
    int *found;           /* scratch: ss_names_find's, one per node */
    int *file_node, nfiles; /* the engine's files: their nodes */
    NameHit *names;       /* ranked, at most NAMES_MAX */
    int nnames, nall;     /* listed, matched */
    SsResult res;         /* content hits: the last search's that came back */
    int *hnode, *hdef;    /* per hit: the node to inspect (its def, else the file), its def (-1) */
    int hfiles;           /* files with hits */
    unsigned char *hit;   /* per node: QH_* */
    int sel, pick;        /* the selected result; test hook: the result to go to (1-based, 0 none) */
    bool went;            /* went to sel since the query or the build changed */
    bool follow;          /* the selection moved by key: scroll it into view */
    float scroll;
    bool hold;            /* after a jump: hovering doesn't inspect until the mouse moves */
    Vector2 hold_at;
} Search;

/* A bar of the turn strip: turns order[a, b) (one, or a bin of them when
 * there are too many), drawn at x, w px wide; the gap after it is its own. */
typedef struct { float x, w; int a, b; } Slot;

typedef struct {
    int view, focus;
    float w, h;
    const StMetrics *m;
    int *drawn, ndrawn; /* nodes in draw order */
    bool *on;           /* per node: drawn */
    LRect *r;           /* per node: icicle/treemap rectangles, stage-relative */
    float *x0, *x1;     /* partition */
    int height;
    LSim sim;
    int *body_node;     /* sim body -> node */
    int *node_body;     /* node -> sim body, -1 */
    bool *below;        /* per body: label under it */
    unsigned gen;       /* bumped on every rebuild of the above */
} Lay;

struct SView {
    char *root;
    Analysis *an;
    pthread_t th;
    bool running, restart;
    pthread_mutex_t mu;
    /* under mu: the worker's results */
    AnSnapshot *new_s;
    StMetrics *new_m;
    SsNames *new_ix;
    bool done;
    char stats[256];
    /* the render thread's */
    AnSnapshot *snap;
    StMetrics *m;
    long added, built;     /* events added; added when the last build started */
    double last_add, first_pending;
    bool open;
    int view, mode;
    int focus;             /* the folder (or file) in view */
    int shown, shown_turn; /* what the inspector shows: a node, or a turn when >= 0 */
    int hov;               /* the node under the mouse this frame */
    int turn;              /* the selected turn (dims the rest), -1 */
    const char *hook;
    int hook_turn;
    bool hook_due;
    Color *fill;
    const StMetrics *fill_m;
    int fill_mode;
    Lay lay;
    Insp insp;
    int press_node, drag_body;
    Vector2 press_at;
    bool pressed;
    double clock;
    struct { const char *key; float t; } tip;               /* a title waiting to show */
    struct { const StMetrics *m; float x0, w, gap; Slot *slot; int n; } bars;
    struct { unsigned gen; int from; const StMetrics *m; int n, *vis, *pi; bool *exact, *mark; } arcs;
    Search sr;
};

/* ---- text ---------------------------------------------------------------- */

Font sv_font(int f) {
    switch (f) {
    case F_SANS_B: return ui_font_bold;
    case F_MONO: return ui_mono;
    case F_MONO_B: return ui_mono_bold;
    default: return ui_font;
    }
}

Font sv_bold(int f) { return f <= F_SANS_B ? ui_font_bold : ui_mono_bold; }

float sv_tws(int f, const char *s, float css, float sp) {
    return ui_measure_text(sv_font(f), s, PX(css), ui_spacing(sv_font(f), PX(css)) + sp);
}
float sv_tw(int f, const char *s, float css) { return sv_tws(f, s, css, 0); }

void sv_txs(int f, const char *s, float x, float y, float css, float sp, Color c) {
    ui_draw_text(sv_font(f), s, (Vector2){floorf(x + 0.5f), floorf(y + 0.5f)}, PX(css),
                 ui_spacing(sv_font(f), PX(css)) + sp, c);
}
void sv_tx(int f, const char *s, float x, float y, float css, Color c) { sv_txs(f, s, x, y, css, 0, c); }

void sv_fit(int f, char *out, size_t n, const char *s, float css, float maxw) {
    ui_fit_font(sv_font(f), out, n, s, PX(css), ui_spacing(sv_font(f), PX(css)), maxw);
}

/* The draft's clip(): cut by character count at 7.2 px each, "…" last. */
static void clip_chars(char *out, size_t n, const char *s, float px) {
    int fit = (int)floorf((px - 8) / CW_CLIP), len = (int)utf8_count(s, SIZE_MAX);
    if (fit >= len) { snprintf(out, n, "%s", s); return; }
    if (fit < 3) { out[0] = 0; return; }
    size_t o = 0;
    int k = 0;
    for (const char *p = s; *p && o + 5 < n; p++) {
        if (!UTF8_CONT(*p) && ++k > fit - 1) break;
        out[o++] = *p;
    }
    memcpy(out + o, "\xE2\x80\xA6", 4);
}

void sv_txc(int f, const char *s, float cx, float cy, float css, Color c) {
    sv_tx(f, s, cx - sv_tw(f, s, css) / 2, sv_mid(cy, css), css, c);
}

void sv_halo(int f, const char *s, float x, float y, float css, Color c, Color halo) {
    for (int dy = -1; dy <= 1; dy++)
        for (int dx = -1; dx <= 1; dx++)
            if (dx || dy) sv_tx(f, s, x + (float)dx * 1.5f, y + (float)dy * 1.5f, css, halo);
    sv_tx(f, s, x, y, css, c);
}

void sv_hline(float x0, float x1, float y, Color c) { DrawRectangle((int)x0, (int)y, (int)(x1 - x0), 1, c); }

float sv_brand(const char *screen, float cy, bool draw) {
    char b[64];
    snprintf(b, sizeof b, " \xC2\xB7 %s", screen);
    float x = 12 + sv_tw(F_MONO_B, "aigource", 14);
    if (draw) sv_tx(F_MONO_B, "aigource", 12, sv_mid(cy, 14), 14, C_FG), sv_tx(F_MONO, b, x, sv_mid(cy, 14), 14, C_DIM);
    return x + sv_tw(F_MONO, b, 14);
}

float sv_hints(const char *const (*keys)[2], int n, float x, float cy, float right, float gap) {
    for (int i = 0; i < n; i++) {
        float kw = sv_tw(F_MONO_B, keys[i][0], 12), w = kw + 6 + sv_tw(F_MONO, keys[i][1], 12);
        if (x + w > right) break;
        sv_tx(F_MONO_B, keys[i][0], x, sv_mid(cy, 12), 12, C_DIM);
        sv_tx(F_MONO, keys[i][1], x + kw + 6, sv_mid(cy, 12), 12, C_FAINT);
        x += w + gap;
    }
    return x;
}

void sv_nvim_hint(float right, float inset, float top, float bottom, bool at_top, float al) {
    static const char hint[] = "click: open in nvim";
    float hw = sv_tw(F_SANS, hint, 11) + 12, hx = right - hw - inset, hy = at_top ? top + 6 : bottom - 18 - 6;
    DrawRectangleRounded((Rectangle){hx, hy, hw, 18}, 0.4f, 6, ui_fade(C_RAISED, 0.95f * al));
    sv_tx(F_SANS, hint, hx + 6, sv_mid(hy + 9, 11), 11, ui_fade(C_DIM, al));
}

void sv_tooltip(const char *bold, const char *rest, Vector2 at) {
    float css = 12, wb = bold && *bold ? sv_tw(F_MONO_B, bold, css) : 0;
    char fitb[600];
    float W = (float)GetScreenWidth(), H = (float)GetScreenHeight();
    sv_fit(F_MONO, fitb, sizeof fitb, rest ? rest : "", css, fmaxf(40, W * 0.6f - wb));
    float w = wb + (*fitb ? sv_tw(F_MONO, fitb, css) : 0) + 16, h = 24;
    float x = at.x + 14, y = at.y + 16;
    if (x + w > W - 8) x = at.x - w - 10;
    if (x < 4) x = 4;
    if (y + h > H - 4) y = at.y - h - 8;
    ui_box((Rectangle){x, y, w, h}, 5 / 12.0f, HEX(0x0b0f15), 1, C_LINE);
    if (wb) sv_tx(F_MONO_B, bold, x + 8, sv_mid(y + h / 2, css), css, C_FG);
    sv_tx(F_MONO, fitb, x + 8 + wb, sv_mid(y + h / 2, css), css, C_DIM);
}

/* ---- colours: d3's Lab interpolation, darker/brighter, hsl lightness ----- */

static float rgb2l(float x) { return (x /= 255) <= 0.04045f ? x / 12.92f : powf((x + 0.055f) / 1.055f, 2.4f); }
static float l2rgb(float x) { return 255 * (x <= 0.0031308f ? 12.92f * x : 1.055f * powf(x, 1 / 2.4f) - 0.055f); }
static float xyz2lab(float t) { return t > 0.008856452f ? cbrtf(t) : t / 0.12841855f + 0.137931034f; }
static float lab2xyz(float t) { return t > 0.206896552f ? t * t * t : 0.12841855f * (t - 0.137931034f); }
static unsigned char clamp8(float v) { return (unsigned char)fmaxf(0, fminf(255, floorf(v + 0.5f))); }

static void to_lab(Color c, float *L, float *A, float *B) {
    float r = rgb2l(c.r), g = rgb2l(c.g), b = rgb2l(c.b);
    float y = xyz2lab(0.2225045f * r + 0.7168786f * g + 0.0606169f * b), x = y, z = y;
    if (c.r != c.g || c.g != c.b) {
        x = xyz2lab((0.4360747f * r + 0.3850649f * g + 0.1430804f * b) / 0.96422f);
        z = xyz2lab((0.0139322f * r + 0.0971045f * g + 0.7141733f * b) / 0.82521f);
    }
    *L = 116 * y - 16, *A = 500 * (x - y), *B = 200 * (y - z);
}

static Color from_lab(float L, float A, float B) {
    float y = (L + 16) / 116, x = y + A / 500, z = y - B / 200;
    x = 0.96422f * lab2xyz(x), y = lab2xyz(y), z = 0.82521f * lab2xyz(z);
    return (Color){clamp8(l2rgb(3.1338561f * x - 1.6168667f * y - 0.4906146f * z)),
                   clamp8(l2rgb(-0.9787684f * x + 1.9161415f * y + 0.0334540f * z)),
                   clamp8(l2rgb(0.0719453f * x - 0.2289914f * y + 1.4052427f * z)), 255};
}

Color sv_mix_lab(Color a, Color b, float t) {
    float l0, a0, b0, l1, a1, b1;
    to_lab(a, &l0, &a0, &b0);
    to_lab(b, &l1, &a1, &b1);
    return from_lab(l0 + (l1 - l0) * t, a0 + (a1 - a0) * t, b0 + (b1 - b0) * t);
}

Color sv_ramp(double t) {
    static const float at[] = {0, 0.3f, 0.7f, 1};
    static const unsigned col[] = {0x1e2b40, 0x3b5a7a, 0xc98a3d, 0xf2a541};
    float v = (float)fmax(0, fmin(1, t));
    int i = v < at[1] ? 0 : v < at[2] ? 1 : 2;
    return sv_mix_lab(HEX(col[i]), HEX(col[i + 1]), (v - at[i]) / (at[i + 1] - at[i]));
}

Color sv_darker(Color c, float k) {
    float f = powf(0.7f, k);
    return (Color){clamp8(c.r * f), clamp8(c.g * f), clamp8(c.b * f), c.a};
}
Color sv_brighter(Color c, float k) {
    float f = powf(1 / 0.7f, k);
    return (Color){clamp8(c.r * f), clamp8(c.g * f), clamp8(c.b * f), c.a};
}

Color sv_ink(Color c) {
    int mx = c.r > c.g ? (c.r > c.b ? c.r : c.b) : (c.g > c.b ? c.g : c.b);
    int mn = c.r < c.g ? (c.r < c.b ? c.r : c.b) : (c.g < c.b ? c.g : c.b);
    return (float)(mx + mn) / 510.0f > 0.55f ? C_BG : C_FG;
}

Color sv_actor_col(int slot) {
    static const unsigned pal[ST_ACTOR_COLORS] = {0x4e9be6, 0xf28e2b, 0x59c27a, 0xe15759,
                                                  0xb07aa1, 0xedc948, 0x76b7b2, 0xff9da7};
    return slot < 0 ? C_UNKNOWN : HEX(pal[slot % ST_ACTOR_COLORS]);
}

Color sv_blame_col(int slot) {
    static const unsigned pal[ST_BLAME_COLORS] = {0xf2a541, 0x56d364, 0x79c0ff, 0xd2a8ff,
                                                  0xff7b72, 0x39c5cf, 0xe3b341, 0xffa198};
    return slot < 0 ? C_UNKNOWN : HEX(pal[slot % ST_BLAME_COLORS]);
}

Color sv_flag_col(int k) {
    return k == STK_CORR ? C_CORR : k == STK_TEST ? C_TEST : k == STK_BLIND ? C_BLIND : C_SLOW;
}

/* ---- words ---------------------------------------------------------------- */

void sv_fmt_k(char *b, size_t n, long long v) {
    if (v >= 1000000) snprintf(b, n, "%.1fM", jfix((double)v / 1e6, 1));
    else if (v >= 1000) snprintf(b, n, v >= 10000 ? "%.0fk" : "%.1fk", jfix((double)v / 1e3, v >= 10000 ? 0 : 1));
    else snprintf(b, n, "%lld", v);
}

void sv_fmt_age(char *b, size_t n, double s) {
    if (s < 3600) snprintf(b, n, "%.0f min", fmax(1, jfix(s / 60, 0)));
    else if (s < 86400) snprintf(b, n, "%.0f h", jfix(s / 3600, 0));
    else snprintf(b, n, "%.0f d", jfix(s / 86400, 0));
}

void sv_fmt_dur(char *b, size_t n, double s) {
    if (s < 60) snprintf(b, n, "%.0f s", s);
    else if (s < 3600) snprintf(b, n, "%.0f min", jfix(s / 60, 0));
    else snprintf(b, n, "%.1f h", jfix(s / 3600, 1));
}

void sv_when(char *b, size_t n, double ts) { fmt_clock(b, n, ts, "%-d %b, %H:%M"); }

void sv_plural(char *b, size_t n, long v, const char *w) { snprintf(b, n, "%ld %s%s", v, w, v == 1 ? "" : "s"); }

void sv_actor_name(const AnSnapshot *S, int a, bool first, char *b, size_t n) {
    if (a < 0 || a >= S->nactors) { snprintf(b, n, "before these transcripts"); return; }
    const AnActor *ac = &S->actors[a];
    if (!ac->sub) { snprintf(b, n, "%s", ac->name ? ac->name : ""); return; }
    const char *t = ac->name ? strrchr(ac->name, ':') : NULL;
    t = t ? t + 1 : ac->name ? ac->name : "";
    if (first) snprintf(b, n, "%s", t);
    else snprintf(b, n, "%s \xC2\xB7 %s", t, ac->task ? ac->task : "");
}

const char *sv_kind_word(int kind) {
    return kind == AN_DIR ? "folder" : kind == AN_FILE ? "file" : kind == AN_CLASS ? "class" : "function";
}

bool sv_is_def(const StNode *d) { return d->kind == AN_FUNC || d->kind == AN_CLASS; }

/* s cut to its first n characters (JavaScript's slice), no "…". */
static void slice_chars(char *s, int n) {
    int k = 0, b = 0;
    while (s[b] && k < n) {
        b++;
        while (UTF8_CONT(s[b])) b++;
        k++;
    }
    s[b] = 0;
}

void sv_metric_word(const StMetrics *m, const StNode *d, int mode, char *b, size_t n) {
    char x[64], y[64];
    switch (mode) {
    case ST_TURNS: sv_plural(b, n, d->turns, "turn"); break;
    case ST_LINES: snprintf(b, n, "+%d \xE2\x88\x92%d", d->add, d->del); break;
    case ST_RISK: sv_plural(x, sizeof x, d->turns, "turn"); snprintf(b, n, "%s \xC2\xB7 cx %d", x, d->cx); break;
    case ST_REWORK: snprintf(b, n, "%d ln reworked", d->rework); break;
    case ST_FIXES: sv_plural(b, n, d->fix_turns, "fix turn"); break;
    case ST_CONTEXT: snprintf(b, n, "%.1f reads/turn", jfix(d->ctx_avg, 1)); break;
    case ST_COST:
        sv_fmt_k(x, sizeof x, d->tok[0]);
        sv_fmt_dur(y, sizeof y, (double)d->tok[3]);
        snprintf(b, n, "%s out \xC2\xB7 %s", x, y);
        break;
    case ST_TIME:
        sv_fmt_dur(x, sizeof x, (double)d->tok[3]);
        if (d->turns) {
            sv_fmt_dur(y, sizeof y, jfix(d->per_turn, 0));
            snprintf(b, n, "%s \xC2\xB7 %s/turn", x, y);
        } else snprintf(b, n, "%s", x);
        break;
    case ST_AGE:
        if (d->age_median < 0) snprintf(b, n, "older");
        else sv_fmt_age(x, sizeof x, (double)d->age_median), snprintf(b, n, "median %s", x);
        break;
    default: {
        char nm[256];
        sv_actor_name(m->snap, d->owner, true, nm, sizeof nm);
        slice_chars(nm, 24);
        snprintf(b, n, "%s", nm);
    }
    }
}

/* ---- builds: one at a time on a worker thread ------------------------------ */

/* Every node's path below the root (the root's ""), folded once for the search's names. */
static SsNames *names_index(const StMetrics *m) {
    const char **paths = xmalloc(sizeof *paths * (size_t)m->nnodes);
    for (int i = 0; i < m->nnodes; i++) paths[i] = i ? sv_rel(m->nodes[i].id) : "";
    SsNames *x = ss_names_new(paths, m->nnodes);
    free(paths);
    return x;
}

static void *worker(void *arg) {
    SView *s = arg;
    AnSnapshot *snap = an_build(s->an);
    StMetrics *m = NULL;
    if (snap) {
        char *buf = NULL;
        size_t len = 0;
        FILE *f = open_memstream(&buf, &len);
        if (f) an_snapshot_stats(f, snap), fclose(f);
        pthread_mutex_lock(&s->mu);
        snprintf(s->stats, sizeof s->stats, "%s", buf ? buf : "");
        char *nl = strchr(s->stats, '\n');
        if (nl) *nl = 0;
        pthread_mutex_unlock(&s->mu);
        free(buf);
        m = st_build(snap);
    }
    SsNames *ix = m ? names_index(m) : NULL;
    pthread_mutex_lock(&s->mu);
    s->new_s = snap;
    s->new_m = m;
    s->new_ix = ix;
    s->done = true;
    pthread_mutex_unlock(&s->mu);
    return NULL;
}

static void start_build(SView *s) {
    if (s->running) return;
    s->built = s->added;
    s->done = false;
    s->restart = false;
    if (pthread_create(&s->th, NULL, worker, s)) {
        perror("aigource: structure build");
        return;
    }
    s->running = true;
}

/* Events arrived since the last build: build now, or after the running one
 * (cancelled when `now`: an explicit R). */
static void request_build(SView *s, bool now) {
    if (s->added == s->built && s->m) return;
    if (!s->running) start_build(s);
    else if (now) an_cancel(s->an, true), s->restart = true;
}

SView *sv_new(const char *root) {
    SView *s = xcalloc(1, sizeof *s);
    s->root = xstrdup(root);
    s->an = an_new(root);
    pthread_mutex_init(&s->mu, NULL);
    s->view = 2;
    s->focus = s->shown = s->shown_turn = s->hov = s->turn = -1;
    s->insp.legend_turn = INT_MIN;
    s->insp.strip_line = s->insp.card_line = -1;
    s->insp.doc.card = s->insp.card.card = -1;
    s->insp.mark.node = -1;
    s->press_node = s->drag_body = -1;
    s->built = -1;
    return s;
}

void sv_add(SView *s, const Event *evs, size_t n) {
    if (!n) return;
    an_add(s->an, evs, n);
    if (s->added == s->built) s->first_pending = GetTime();
    s->added += (long)n;
    s->last_add = GetTime();
}

static bool search_busy(const SView *s);

bool sv_is_open(const SView *s) { return s && s->open; }
bool sv_ready(const SView *s) { return s->open && s->m && !s->running && !s->hook_due && !search_busy(s); }
bool sv_typing(const SView *s) { return s && s->open && s->sr.open; }

void sv_toggle(SView *s) {
    s->open = !s->open;
    if (s->open) request_build(s, false);
}

void sv_hooks(SView *s, const char *spec, int turn) {
    s->hook = spec;
    s->hook_turn = turn;
    s->hook_due = true;
    if (!s->open) sv_toggle(s);
}

static void lay_free(Lay *l) {
    unsigned gen = l->gen;
    free(l->drawn);
    free(l->on);
    free(l->r);
    free(l->x0);
    free(l->x1);
    free(l->body_node);
    free(l->node_body);
    free(l->below);
    lay_sim_free(&l->sim);
    memset(l, 0, sizeof *l);
    l->gen = gen + 1;
}

static void search_drop_hits(SView *s) {
    Search *q = &s->sr;
    ss_result_free(&q->res);
    free(q->hnode), free(q->hdef);
    q->hnode = q->hdef = NULL;
    q->hfiles = 0;
    if (q->hit && q->m)
        for (int i = 0; i < q->m->nnodes; i++) q->hit[i] &= (unsigned char)~QH_TEXT;
}

static void search_free(SView *s) {
    Search *q = &s->sr;
    ss_engine_free(q->eng); /* first: it reads the snapshot's texts */
    q->eng = NULL;
    search_drop_hits(s);
    free(q->file_node), free(q->names), free(q->hit), free(q->found);
    ss_names_free(q->ix);
}

static void arcs_free(SView *s) {
    free(s->arcs.vis), free(s->arcs.pi), free(s->arcs.exact), free(s->arcs.mark);
    memset(&s->arcs, 0, sizeof s->arcs);
}

/* A drag or press holds sim bodies and nodes: gone with them. Every layout
 * rebuild calls it, so a drag_body > 0 is a body of the current sim, press_node's. */
static void drop_press(SView *s) {
    s->drag_body = s->press_node = -1;
    s->pressed = false;
}

void sv_free(SView *s) {
    if (!s) return;
    if (s->running) an_cancel(s->an, true), pthread_join(s->th, NULL);
    search_free(s);
    ss_names_free(s->new_ix);
    st_free_pair(s->new_m, s->new_s);
    st_free_pair(s->m, s->snap);
    an_free(s->an);
    lay_free(&s->lay);
    si_free(&s->insp);
    arcs_free(s);
    free(s->bars.slot);
    free(s->fill);
    pthread_mutex_destroy(&s->mu);
    free(s->root);
    free(s);
}

/* ---- what is shown -------------------------------------------------------- */

static bool is_anc(const StMetrics *m, int a, int n) { /* a is n or holds it */
    for (; n >= 0; n = m->nodes[n].parent)
        if (n == a) return true;
    return false;
}

static void inspect(SView *s, int n) {
    s->shown = n;
    s->shown_turn = -1;
}

static void set_focus(SView *s, int n) {
    s->focus = n;
    inspect(s, n);
}

/* A node from a list: its folder or file in view, it inspected. */
static void go(SView *s, int n) {
    if (!s->m || n < 0 || n >= s->m->nnodes) return;
    int holder = s->m->nodes[n].nkids ? n : s->m->nodes[n].parent;
    if (holder >= 0) s->focus = holder;
    inspect(s, n);
}

/* A click on the stage: a folder or a file opens, the centre goes up, anything else is inspected. */
static void open_node(SView *s, int n) {
    const StNode *d = &s->m->nodes[n];
    if (d->nkids && n != s->focus) set_focus(s, n);
    else if (n == s->focus && d->parent >= 0) set_focus(s, d->parent);
    else inspect(s, n);
}

/* Selecting shows the turn; clearing it only undims (the inspector stays, as the draft's). */
static void select_turn(SView *s, int t) {
    if (!s->m || t >= s->m->nturns) return;
    s->turn = t;
    if (t >= 0) s->shown_turn = t;
}

static void step_turn(SView *s, int dir) {
    const StMetrics *m = s->m;
    if (!m || !m->nturns) return;
    int i = s->turn < 0 ? (dir > 0 ? -1 : m->nturns) : m->turns[s->turn].rank;
    i += dir;
    if (i < 0) i = 0;
    if (i >= m->nturns) i = m->nturns - 1;
    select_turn(s, m->order[i]);
}

/* Who a node was, to find it again in another build. */
typedef struct { char *id; int a, b; } NodeRef;

static NodeRef ref_of(const StMetrics *m, int n) {
    return m && n >= 0 ? (NodeRef){xstrdup(m->nodes[n].id), m->nodes[n].a, m->nodes[n].b} : (NodeRef){0};
}

/* The node with that id; of same-named functions in a file, the one at the same lines. */
static int find_ref(const StMetrics *m, NodeRef r) {
    if (!r.id) return -1;
    int first = st_find(m, r.id);
    if (first < 0 || !sv_is_def(&m->nodes[first])) return first;
    for (int i = 0; i < m->nnodes; i++)
        if (m->nodes[i].a == r.a && m->nodes[i].b == r.b && !strcmp(m->nodes[i].id, r.id)) return i;
    return first;
}

static int find_turn(const StMetrics *m, const StMetrics *old, int t) {
    if (!old || t < 0) return -1;
    const AnTurn *o = &old->snap->turns[t];
    for (int i = 0; i < m->nturns; i++) {
        const AnTurn *T = &m->snap->turns[i];
        if (T->ts == o->ts && !strcmp(T->title ? T->title : "", o->title ? o->title : "")) return i;
    }
    return -1;
}

/* ---- search: names and file contents ------------------------------------------ */

/* The query to run again: names at once, content (2 characters and up) after edited + SEARCH_WAIT. */
static void search_due(Search *q, double edited) {
    q->names_due = true;
    q->content_due = utf8_count(q->q, SIZE_MAX) >= 2, q->edited = edited;
}

static bool search_on(const SView *s) { return s->sr.q[0] && s->sr.hit && s->sr.m == s->m; }

static int search_total(const Search *q) { return q->nnames + q->res.n; }

static int cmp_rel(const void *a, const void *b, void *ud) {
    const StMetrics *m = ud;
    return strcmp(m->nodes[*(const int *)a].id, m->nodes[*(const int *)b].id);
}

/* A new build: the worker gets its files (path order, the snapshot's texts),
 * and the query runs again on it. */
static void search_files(SView *s, const StMetrics *m, SsNames *ix) {
    Search *q = &s->sr;
    if (!q->eng) q->eng = ss_engine_new();
    int nf = 0, *fn = xmalloc(sizeof *fn * (size_t)m->nnodes);
    for (int i = 0; i < m->nnodes; i++)
        if (m->nodes[i].kind == AN_FILE) fn[nf++] = i;
    qsort_r(fn, (size_t)nf, sizeof *fn, cmp_rel, (void *)m);
    SsFile *f = xmalloc(sizeof *f * (size_t)nf);
    for (int k = 0; k < nf; k++) {
        const AnNode *a = &m->snap->nodes[m->nodes[fn[k]].an];
        f[k] = (SsFile){a->src, a->srclen};
    }
    ss_engine_files(q->eng, f, nf);
    free(f);
    search_drop_hits(s);
    free(q->file_node);
    q->file_node = fn, q->nfiles = nf;
    q->nnames = q->nall = 0;
    free(q->hit);
    q->hit = xcalloc((size_t)m->nnodes, 1);
    q->found = xrealloc(q->found, sizeof *q->found * (size_t)m->nnodes);
    ss_names_free(q->ix);
    q->ix = ix;
    q->m = m;
    q->gen = 0;
    q->sel = 0, q->scroll = 0, q->went = false; /* other results */
    search_due(q, -1e9);
}

/* Ancestors of what matched hold a match: not dimmed. */
static void search_marks(SView *s) {
    Search *q = &s->sr;
    const StMetrics *m = q->m;
    for (int i = 0; i < m->nnodes; i++) q->hit[i] &= (unsigned char)~QH_IN;
    for (int i = 0; i < m->nnodes; i++)
        if (q->hit[i] & (QH_NAME | QH_TEXT))
            for (int p = m->nodes[i].parent; p >= 0 && !(q->hit[p] & QH_IN); p = m->nodes[p].parent) q->hit[p] |= QH_IN;
}

/* The results changed: the selection kept among them, the marks again. */
static void search_refresh(SView *s) {
    Search *q = &s->sr;
    if (q->sel >= search_total(q)) q->sel = search_total(q) ? search_total(q) - 1 : 0;
    search_marks(s);
}

/* Every node's path against the query, on this thread (ss_names_find: the
 * build's paths folded once, a byte test first, a query extending the last
 * one narrowed to its matches): 0.1 to 0.3 ms on this repo; on 60k paths 1 to
 * 5 ms, 15 ms for a query whose letters scatter (src/model: fuzzy-scored). */
static void search_names(SView *s) {
    Search *q = &s->sr;
    const StMetrics *m = q->m;
    q->names_due = false;
    q->nnames = q->nall = 0;
    for (int i = 0; i < m->nnodes; i++) q->hit[i] &= (unsigned char)~QH_NAME;
    if (q->q[0]) {
        double t0 = now_wall();
        q->nall = ss_names_find(q->ix, q->q, q->found, NAMES_MAX);
        for (int k = 0; k < q->nall; k++) q->hit[q->found[k]] |= QH_NAME;
        q->nnames = q->nall < NAMES_MAX ? q->nall : NAMES_MAX;
        q->names = xrealloc(q->names, sizeof *q->names * (size_t)q->nnames);
        for (int k = 0; k < q->nnames; k++) {
            q->names[k].node = q->found[k];
            ss_names_match(q->ix, q->q, q->found[k], &q->names[k].m);
        }
        if (getenv("AIG_DEBUG"))
            fprintf(stderr, "aigource: search names \"%s\": %d of %d nodes, %.2f ms\n", q->q, q->nall, m->nnodes,
                    (now_wall() - t0) * 1e3);
    }
    search_refresh(s);
}

/* The worker's result for the query asked last: each hit's def (the deepest holding its line), else its file. */
static void search_poll(SView *s) {
    Search *q = &s->sr;
    SsResult r;
    if (!ss_engine_poll(q->eng, &r)) return;
    if (r.gen != q->gen) { ss_result_free(&r); return; }
    search_drop_hits(s);
    const StMetrics *m = q->m;
    q->res = r;
    q->hnode = xmalloc(sizeof *q->hnode * (size_t)r.n);
    q->hdef = xmalloc(sizeof *q->hdef * (size_t)r.n);
    for (int i = 0; i < r.n; i++) {
        const SsHit *h = &r.hits[i];
        int f = q->file_node[h->file], n = f, def = -1;
        for (bool deeper = true; deeper;) {
            deeper = false;
            for (int k = 0; k < m->nodes[n].nkids && !deeper; k++) {
                const StNode *d = &m->nodes[m->nodes[n].kids[k]];
                if (sv_is_def(d) && d->a <= h->line && h->line <= d->b) n = def = m->nodes[n].kids[k], deeper = true;
            }
        }
        q->hnode[i] = n, q->hdef[i] = def;
        q->hit[f] |= QH_TEXT, q->hit[n] |= QH_TEXT;
        q->hfiles += !i || h->file != r.hits[i - 1].file;
    }
    if (getenv("AIG_DEBUG"))
        fprintf(stderr, "aigource: search content \"%s\": %d%s lines in %d files of %d searched, %.2f ms\n", q->q, r.n,
                r.capped ? "+" : "", q->hfiles, r.files, r.ms);
    search_refresh(s);
}

/* The query changed: names at once, content after a pause in typing (2
 * characters and up). The last query's lines go at once: nothing shows, is
 * emphasised or can be gone to that isn't this query's. */
static void search_edit(SView *s) {
    Search *q = &s->sr;
    search_due(q, GetTime());
    q->sel = 0, q->scroll = 0, q->all = false, q->went = false;
    if (q->eng) ss_engine_cancel(q->eng);
    q->gen = 0;
    search_drop_hits(s);
    if (q->hit && q->m == s->m) search_marks(s);
    if (!q->q[0]) si_mark(&s->insp, -1, 0, NULL, false);
}

/* A content search asked and not back yet, names to rank, or a test hook's pick to make. */
static bool search_busy(const SView *s) {
    const Search *q = &s->sr;
    return q->q[0] && (q->names_due || q->content_due || (q->gen && q->res.gen != q->gen) || q->pick);
}

/* Result k, as Enter: its folder (or file, for a function) in focus, it
 * inspected; a line of a file shown and lit in the inspector's code. */
static void search_go(SView *s, int k) {
    Search *q = &s->sr;
    const StMetrics *m = s->m;
    if (!m || q->m != m || k < 0 || k >= search_total(q)) return;
    int n, line = 0;
    if (k < q->nnames) n = q->names[k].node;
    else n = q->hnode[k - q->nnames], line = q->res.hits[k - q->nnames].line;
    int p = m->nodes[n].parent;
    s->focus = p >= 0 ? p : n;
    inspect(s, n);
    si_mark(&s->insp, line ? n : -1, line, q->q, ss_smart_case(q->q));
    q->sel = k, q->open = false, q->went = true;
    q->hold = true, q->hold_at = (Vector2){-1, -1};
}

static void search_update(SView *s) {
    Search *q = &s->sr;
    if (!s->m || !q->eng || q->m != s->m) return;
    if (q->names_due) search_names(s);
    if (q->content_due && GetTime() - q->edited >= SEARCH_WAIT) {
        q->content_due = false;
        q->gen = ss_engine_search(q->eng, q->q, ss_smart_case(q->q));
    }
    search_poll(s);
    if (q->pick && !q->names_due && !q->content_due && !(q->gen && q->res.gen != q->gen)) {
        search_go(s, q->pick - 1);
        q->pick = 0;
    }
}

static void search_open(SView *s) {
    s->sr.open = true;
    s->sr.all = s->sr.q[0] != 0;
    s->sr.follow = true;
}

/* Keys while the box has focus, none reaching anything else: text, Backspace
 * (Ctrl: a word, as Ctrl+W), the selection, Enter, Esc. */
static void search_type(SView *s, bool ctrl) {
    Search *q = &s->sr;
    if (ui_edit_line(q->q, sizeof q->q, SS_QMAX, &q->all, ctrl)) { /* SS_QMAX: what names and content both match */
        search_edit(s);
        if (q->ix && q->m == s->m) search_names(s); /* an Enter in this frame goes to this query's */
    }
    int total = search_total(q), step = 0;
#define KEY(k) (IsKeyPressed(k) || IsKeyPressedRepeat(k))
    if (KEY(KEY_DOWN) || (ctrl && KEY(KEY_N))) step = 1;
    if (KEY(KEY_UP) || (ctrl && KEY(KEY_P))) step = -1;
    if (KEY(KEY_PAGE_DOWN)) step = 10;
    if (KEY(KEY_PAGE_UP)) step = -10;
#undef KEY
    if (step && total) {
        q->sel = q->sel + step < 0 ? 0 : q->sel + step >= total ? total - 1 : q->sel + step;
        q->follow = true;
    }
    if (IsKeyPressed(KEY_ENTER) || IsKeyPressed(KEY_KP_ENTER)) {
        if (total) search_go(s, q->sel < total ? q->sel : total - 1);
        else q->open = false;
    }
    if (IsKeyPressed(KEY_ESCAPE)) q->open = false;
}

/* The box closed, the screen's: '/' or Ctrl+F open it (what follows '/' in
 * the same frame is typed), n / N go to the next and previous result. True
 * when it took the frame's keys. */
static bool search_keys(SView *s, bool ctrl) {
    Search *q = &s->sr;
    if (!s->m) return false;
    if (ctrl && IsKeyPressed(KEY_F)) {
        while (GetCharPressed()) {}
        search_open(s);
        return true;
    }
    for (int c; (c = GetCharPressed());) {
        if (c == '/') {
            search_open(s);
            search_type(s, ctrl);
            return true;
        }
        int total = search_total(q);
        if ((c == 'n' || c == 'N') && q->q[0] && total && !ctrl) /* the first goes to the selected one */
            search_go(s, ((q->sel + (!q->went ? 0 : c == 'n' ? 1 : -1)) % total + total) % total);
    }
    return false;
}

void sv_search(SView *s, const char *query, int pick) {
    Search *q = &s->sr;
    snprintf(q->q, sizeof q->q, "%s", query);
    q->open = true;
    q->pick = pick > 0 ? pick : 0;
    search_due(q, -1e9);
    if (!s->open) sv_toggle(s);
}

/* A finished build: the same folder, node and turns when they're still there. */
static void swap_in(SView *s) {
    pthread_mutex_lock(&s->mu);
    bool done = s->done;
    AnSnapshot *ns = s->new_s;
    StMetrics *nm = s->new_m;
    SsNames *nix = s->new_ix;
    if (done) s->done = false, s->new_s = NULL, s->new_m = NULL, s->new_ix = NULL;
    pthread_mutex_unlock(&s->mu);
    if (!done) return;
    pthread_join(s->th, NULL);
    s->running = false;
    an_cancel(s->an, false);
    if (!nm) { /* cancelled */
        st_free_pair(NULL, ns);
        s->built = -1;
        if (s->restart) start_build(s);
        return;
    }
    StMetrics *om = s->m;
    NodeRef f = ref_of(om, s->focus), sh = ref_of(om, s->shown), mk = ref_of(om, s->insp.mark.node);
    int turn = find_turn(nm, om, s->turn), sturn = find_turn(nm, om, s->shown_turn);
    /* nothing reads the old pair past this point: the caches keyed by it go first */
    lay_free(&s->lay);
    arcs_free(s);
    drop_press(s);
    s->fill_m = NULL;
    s->bars.m = NULL;
    s->insp.doc.m = NULL;
    search_files(s, nm, nix); /* before the old texts go: the search may be reading them */
    st_free_pair(om, s->snap);
    s->snap = ns;
    s->m = nm;
    s->focus = find_ref(nm, f);
    if (s->focus < 0) s->focus = nm->nnodes ? 0 : -1;
    s->shown = sh.id ? find_ref(nm, sh) : s->focus;
    if (s->shown < 0) s->shown = s->focus;
    s->turn = turn;
    s->shown_turn = sturn;
    s->hov = -1;
    s->insp.mark.node = mk.id ? find_ref(nm, mk) : -1;
    free(f.id);
    free(sh.id);
    free(mk.id);
    if (s->restart) start_build(s);
}

static void apply_hooks(SView *s) {
    if (!s->hook_due || !s->m || s->running) return;
    s->hook_due = false;
    const StMetrics *m = s->m;
    if (s->hook) {
        char spec[1024];
        snprintf(spec, sizeof spec, "%s", s->hook);
        char *mode = strchr(spec, ':'), *path = NULL;
        if (mode) *mode++ = 0, path = strchr(mode, ':');
        if (path) *path++ = 0;
        for (int v = 0; v < 3; v++)
            if (!strcmp(spec, VIEW_KEY[v])) s->view = v;
        for (int k = 0; mode && k < ST_NMODES; k++)
            if (!strcmp(mode, MODE_NAME[k])) s->mode = k;
        int n = path && *path ? st_find(m, path) : -1;
        if (path && *path && n < 0) fprintf(stderr, "aigource: --structure: no node %s\n", path);
        if (n >= 0 && m->nodes[n].kind == AN_DIR) set_focus(s, n);
        else if (n >= 0) {
            int p = m->nodes[n].parent;
            if (p >= 0) set_focus(s, p);
            inspect(s, n);
        }
    }
    if (s->hook_turn && m->nturns) {
        int k = s->hook_turn > 0 ? s->hook_turn - 1 : m->nturns + s->hook_turn;
        if (k >= 0 && k < m->nturns) select_turn(s, m->order[k]);
    }
}

/* ---- colours per node ----------------------------------------------------- */

static void ensure_fill(SView *s) {
    const StMetrics *m = s->m;
    if (s->fill_m == m && s->fill_mode == s->mode) return;
    s->fill = xrealloc(s->fill, sizeof *s->fill * (size_t)m->nnodes);
    for (int i = 0; i < m->nnodes; i++) {
        const StNode *d = &m->nodes[i];
        Color c = s->mode == ST_OWNER ? sv_actor_col(d->owner_slot) : sv_ramp(d->heat[s->mode]);
        s->fill[i] = d->kind == AN_DIR ? sv_darker(c, 0.5f) : c;
    }
    s->fill_m = m;
    s->fill_mode = s->mode;
}

static int nflags(int k) { return !!(k & STK_CORR) + !!(k & STK_TEST) + !!(k & STK_BLIND) + !!(k & STK_SLOW); }

/* Flag dots going left from x, in the order corr, test, blind, slow. */
static void flag_dots(int kinds, float x, float y, float a) {
    static const int order[] = {STK_CORR, STK_TEST, STK_BLIND, STK_SLOW};
    int i = 0;
    for (int k = 0; k < 4; k++) {
        if (!(kinds & order[k])) continue;
        Vector2 p = {x - (float)i++ * 9, y};
        DrawCircleV(p, 4.2f, ui_fade(C_BG, a));
        DrawCircleV(p, 3.6f, ui_fade(sv_flag_col(order[k]), a));
    }
}

/* ---- layouts, cached per (view, focus, size, build) ----------------------- */

static void push_drawn(Lay *l, int i) {
    l->drawn[l->ndrawn++] = i;
    l->on[i] = true;
}

/* The focus's subtree, breadth-first (d3's descendants()). */
static void bfs(const StMetrics *m, Lay *l, int root, bool with_root) {
    int head = l->ndrawn;
    if (with_root) push_drawn(l, root);
    else
        for (int k = 0; k < m->nodes[root].nkids; k++) push_drawn(l, m->nodes[root].kids[k]);
    for (; head < l->ndrawn; head++) {
        const StNode *d = &m->nodes[l->drawn[head]];
        for (int k = 0; k < d->nkids; k++) push_drawn(l, d->kids[k]);
    }
}

static float hub_radius(const StNode *f) { return fmaxf(34, fminf(46, (float)strlen(f->name) * 3.8f)); }

static void ensure_layout(SView *s, float w, float h) {
    Lay *l = &s->lay;
    const StMetrics *m = s->m;
    if (l->m == m && l->view == s->view && l->focus == s->focus && l->w == w && l->h == h) return;
    bool same_sim = l->m == m && l->view == 2 && s->view == 2 && l->focus == s->focus && l->sim.n;
    if (same_sim) { /* a resize: the bodies float on where they are */
        l->w = w, l->h = h;
        return;
    }
    lay_free(l);
    drop_press(s); /* its sim bodies and nodes are gone */
    l->m = m, l->view = s->view, l->focus = s->focus, l->w = w, l->h = h;
    int n = m->nnodes, f = s->focus;
    l->drawn = xmalloc(sizeof *l->drawn * (size_t)n);
    l->on = xcalloc((size_t)n, sizeof *l->on);
    if (f < 0) return;
    const StNode *F = &m->nodes[f];
    if (s->view == 0) {
        l->x0 = xmalloc(sizeof *l->x0 * (size_t)n);
        l->x1 = xmalloc(sizeof *l->x1 * (size_t)n);
        lay_partition(m->nodes, f, l->x0, l->x1);
        l->height = lay_height(m->nodes, f);
        bfs(m, l, f, true);
    } else if (s->view == 1) {
        l->r = xmalloc(sizeof *l->r * (size_t)n);
        lay_treemap(m->nodes, f, w, h, LAY_TREEMAP_RATIO, l->r);
        bfs(m, l, f, !F->nkids);
    } else {
        push_drawn(l, f);
        for (int k = 0; k < F->nkids; k++) push_drawn(l, F->kids[k]);
        l->node_body = xmalloc(sizeof *l->node_body * (size_t)n);
        for (int i = 0; i < n; i++) l->node_body[i] = -1;
        int nk = F->nkids;
        float hub = hub_radius(F), area = 0;
        for (int k = 0; k < nk; k++) area += (float)m->nodes[F->kids[k]].lines;
        if (area <= 0) area = 1;
        float kk = sqrtf((nk > 40 ? 0.15f : 0.2f) * w * h / (3.14159265f * area));
        float *r = xmalloc(sizeof *r * (size_t)(nk + 1)), *pad = xmalloc(sizeof *pad * (size_t)(nk + 1));
        l->body_node = xmalloc(sizeof *l->body_node * (size_t)(nk + 1));
        l->below = xcalloc((size_t)(nk + 1), sizeof *l->below);
        l->body_node[0] = f;
        l->node_body[f] = 0;
        for (int k = 0; k < nk; k++) {
            const StNode *d = &m->nodes[F->kids[k]];
            r[k] = fmaxf(d->nkids ? 9 : 5, fminf(fminf(w, h) / 6, kk * sqrtf((float)d->lines)));
            l->below[k + 1] = r[k] < 24;
            pad[k] = l->below[k + 1] ? 15 : 5;
            l->body_node[k + 1] = F->kids[k];
            l->node_body[F->kids[k]] = k + 1;
        }
        lay_sim_init(&l->sim, hub, r, pad, nk, (unsigned)(f * 2654435761u) ^ (unsigned)nk);
        free(r);
        free(pad);
    }
}

#define ICICLE_TOP 28.0f

/* Icicle: rows by depth below the focus, width by lines (stage-relative). */
static Rectangle icicle_rect(const SView *s, int i) {
    const Lay *l = &s->lay;
    const StNode *d = &s->m->nodes[i], *F = &s->m->nodes[s->focus];
    float row = (l->h - ICICLE_TOP) / (float)(l->height > 1 ? l->height : 1);
    float span = l->x1[s->focus] - l->x0[s->focus];
    float sx0 = (l->x0[i] - l->x0[s->focus]) / (span > 0 ? span : 1) * l->w;
    float sx1 = (l->x1[i] - l->x0[s->focus]) / (span > 0 ? span : 1) * l->w;
    float y0 = i == s->focus ? 0 : ICICLE_TOP + (float)(d->depth - F->depth - 1) * row;
    return (Rectangle){sx0, y0, sx1 - sx0, i == s->focus ? ICICLE_TOP : row};
}

/* Where a node is drawn, for arcs (absolute): the draft's ARC.pos. */
static bool node_pos(const SView *s, Rectangle st, int i, Vector2 *p, float *r) {
    const Lay *l = &s->lay;
    *r = 0;
    if (i < 0 || !l->on || !l->on[i]) return false;
    if (s->view == 0) {
        Rectangle q = icicle_rect(s, i);
        *p = (Vector2){st.x + q.x + q.width / 2, st.y + q.y + fminf(q.height / 2, 40)};
    } else if (s->view == 1) {
        LRect q = l->r[i];
        *p = (Vector2){st.x + (q.x0 + q.x1) / 2, st.y + (s->m->nodes[i].nkids ? q.y0 + 9 : (q.y0 + q.y1) / 2)};
    } else {
        int b = l->node_body[i];
        if (b < 0) return false;
        *p = (Vector2){st.x + st.width / 2 + l->sim.b[b].x, st.y + st.height / 2 + l->sim.b[b].y};
        *r = l->sim.b[b].r;
    }
    return true;
}

/* The node under the mouse: the last drawn that holds it. */
static int stage_hit(const SView *s, Rectangle st, Vector2 mp) {
    const Lay *l = &s->lay;
    if (!CheckCollisionPointRec(mp, st)) return -1;
    float x = mp.x - st.x, y = mp.y - st.y;
    for (int k = l->ndrawn - 1; k >= 0; k--) {
        int i = l->drawn[k];
        if (s->view == 0) {
            Rectangle q = icicle_rect(s, i);
            if (x >= q.x && x < q.x + q.width && y >= q.y && y < q.y + q.height) return i;
        } else if (s->view == 1) {
            LRect q = l->r[i];
            if (x >= q.x0 && x < q.x1 && y >= q.y0 && y < q.y1) return i;
        } else {
            const LBody *b = &l->sim.b[l->node_body[i]];
            float dx = x - st.width / 2 - b->x, dy = y - st.height / 2 - b->y;
            if (dx * dx + dy * dy <= b->r * b->r) return i;
        }
    }
    return -1;
}

/* Partners of the inspected node, each at the nearest thing on screen that
 * holds it (once per such thing), cached per (build, layout, node). */
static void ensure_arcs(SView *s, Rectangle st, int from) {
    if (s->arcs.m == s->m && s->arcs.gen == s->lay.gen && s->arcs.from == from && s->arcs.mark) return;
    arcs_free(s);
    const StMetrics *m = s->m;
    s->arcs.m = m, s->arcs.gen = s->lay.gen, s->arcs.from = from;
    s->arcs.mark = xcalloc((size_t)m->nnodes, sizeof *s->arcs.mark);
    Vector2 p;
    float r;
    if (from < 0 || !node_pos(s, st, from, &p, &r)) return;
    const StNode *d = &m->nodes[from];
    if (!d->npartners) return;
    s->arcs.vis = xmalloc(sizeof *s->arcs.vis * (size_t)d->npartners);
    s->arcs.pi = xmalloc(sizeof *s->arcs.pi * (size_t)d->npartners);
    s->arcs.exact = xmalloc(sizeof *s->arcs.exact * (size_t)d->npartners);
    for (int k = 0; k < d->npartners; k++) {
        int v = d->partners[k].node;
        while (v >= 0 && !node_pos(s, st, v, &p, &r)) v = m->nodes[v].parent;
        if (v < 0 || is_anc(m, from, v) || is_anc(m, v, from) || s->arcs.mark[v]) continue;
        s->arcs.mark[v] = true;
        s->arcs.exact[s->arcs.n] = v == d->partners[k].node;
        s->arcs.pi[s->arcs.n] = k;
        s->arcs.vis[s->arcs.n++] = v;
    }
}

/* ---- stage drawing ---------------------------------------------------------- */

/* A selected turn dims what it didn't touch, a search what doesn't match or
 * hold a match; both at once dim what either would. */
static float node_alpha(const SView *s, int i) {
    if (s->turn >= 0 && !st_turn_touches(&s->m->turns[s->turn], i)) return DIM_A;
    if (search_on(s) && !s->sr.hit[i]) return DIM_A;
    return 1;
}

/* Matched by name or content itself: outlined amber (faded as node_alpha fades it). */
static bool found(const SView *s, int i) { return search_on(s) && (s->sr.hit[i] & (QH_NAME | QH_TEXT)); }

static bool partner(const SView *s, int i) { return s->arcs.mark && s->arcs.mark[i]; }

/* Outlines only where they show: 3 px and up. */
static void outline(Rectangle q, float w, Color c) {
    if (q.width >= 3 && q.height >= 3) DrawRectangleLinesEx(q, w, c);
}

/* The hovered, partner or found outline; false when none. */
static bool node_outline(const SView *s, int i, Rectangle r, float a) {
    if (i == s->hov) outline(r, 2, C_FOCUS);
    else if (partner(s, i)) outline(r, 1.5f, WHITE);
    else if (found(s, i)) outline(r, 1.5f, ui_fade(C_HOT, a));
    else return false;
    return true;
}

/* The views draw in two passes, shapes then text: each switch between the
 * shapes' texture and a font's is a draw call (one per node otherwise). */

static void draw_icicle(SView *s, Rectangle st) {
    const StMetrics *m = s->m;
    const Lay *l = &s->lay;
    const StNode *F = &m->nodes[s->focus];
    float row = (l->h - ICICLE_TOP) / (float)(l->height > 1 ? l->height : 1);
    char buf[512], fitb[512];
    for (int pass = 0; pass < 2; pass++)
        for (int k = 0; k < l->ndrawn; k++) {
            int i = l->drawn[k];
            Rectangle q = icicle_rect(s, i);
            if (q.width < 1) continue;
            const StNode *d = &m->nodes[i];
            float a = node_alpha(s, i);
            Rectangle r = {st.x + q.x, st.y + q.y, fmaxf(0, q.width - 1), q.height - 1};
            int nf = i == s->focus ? 0 : nflags(d->kinds);
            if (pass == 0) {
                DrawRectangleRec(r, ui_fade(s->fill[i], a));
                node_outline(s, i, r, a);
                if (nf && q.width > 14 + (float)nf * 9) flag_dots(d->kinds, r.x + q.width - 8, r.y + 9, a);
                continue;
            }
            if (q.width < 12) continue;
            Color ink = ui_fade(sv_ink(s->fill[i]), a);
            if (i == s->focus) {
                char t[64];
                sv_plural(t, sizeof t, d->turns, "turn");
                snprintf(buf, sizeof buf, "%s  \xC2\xB7  %s \xC2\xB7 %d lines%s", d->name, t, d->lines,
                         F->parent >= 0 ? "  \xC2\xB7  click to go up" : "");
                clip_chars(fitb, sizeof fitb, buf, q.width);
                sv_tx(F_MONO, fitb, r.x + 5, st.y + q.y + 17 - PX(12) * 0.8f, 12, ink);
                continue;
            }
            clip_chars(fitb, sizeof fitb, d->name, q.width - (float)nf * 9);
            sv_tx(F_MONO, fitb, r.x + 5, st.y + q.y + 17 - PX(12) * 0.8f, 12, ink);
            if (row > 40 && q.width > 90) {
                if (d->cx) snprintf(buf, sizeof buf, "%dt \xC2\xB7 cx %d", d->turns, d->cx);
                else snprintf(buf, sizeof buf, "%dt", d->turns);
                sv_tx(F_MONO, buf, r.x + 5, st.y + q.y + 33 - PX(11) * 0.8f, 11, ui_fade(ink, 0.7f));
            }
        }
}

/* A group no taller than 40 px has no header strip: its children cover its
 * name and dots (the draft draws them under its children). */
static bool tm_covered(const StNode *d, LRect q) { return d->nkids && q.y1 - q.y0 <= 40; }

static void draw_treemap(SView *s, Rectangle st) {
    const StMetrics *m = s->m;
    const Lay *l = &s->lay;
    char fitb[512];
    for (int pass = 0; pass < 2; pass++)
        for (int k = 0; k < l->ndrawn; k++) {
            int i = l->drawn[k];
            LRect q = l->r[i];
            float w = q.x1 - q.x0, h = q.y1 - q.y0;
            if (w < 1 || h < 1) continue;
            const StNode *d = &m->nodes[i];
            float a = node_alpha(s, i);
            Rectangle r = {st.x + q.x0, st.y + q.y0, w, h};
            int nf = nflags(d->kinds);
            if (pass == 0) {
                Color c = d->nkids ? sv_darker(s->fill[i], 0.5f) : s->fill[i];
                DrawRectangleRec(r, ui_fade(c, a));
                if (!node_outline(s, i, r, a)) outline(r, 0.5f, ui_fade(C_BG, a));
                if (nf && w > 14 + (float)nf * 9 && h > 12) flag_dots(d->kinds, r.x + w - 7, r.y + 8, a);
            } else if (h > 16 && w > 20 && !tm_covered(d, q)) {
                clip_chars(fitb, sizeof fitb, d->name, w - (float)nf * 9);
                if (*fitb) sv_tx(F_MONO, fitb, r.x + 4, r.y + 13 - PX(11.5f) * 0.8f, 11.5f, ui_fade(sv_ink(s->fill[i]), a));
            }
        }
}
/* A dashed circle outline, dashes `on` px with `off` gaps. */
static void dashed_circle(Vector2 c, float r, float w, float on, float off, Color col) {
    float circ = 2 * 3.14159265f * r;
    int n = (int)(circ / (on + off));
    if (n < 4) { DrawRing(c, r - w / 2, r + w / 2, 0, 360, 36, col); return; }
    float step = 360.0f / (float)n, dash = step * on / (on + off);
    for (int i = 0; i < n; i++) DrawRing(c, r - w / 2, r + w / 2, (float)i * step, (float)i * step + dash, 4, col);
}

static void draw_floating(SView *s, Rectangle st) {
    const StMetrics *m = s->m;
    const Lay *l = &s->lay;
    const StNode *F = &m->nodes[s->focus];
    float cx = st.x + st.width / 2, cy = st.y + st.height / 2;
    char buf[256], fitb[256];
    for (int b = 1; b < l->sim.n; b++) /* links aren't nodes: never dimmed */
        DrawLineEx((Vector2){cx + l->sim.b[0].x, cy + l->sim.b[0].y}, (Vector2){cx + l->sim.b[b].x, cy + l->sim.b[b].y},
                   1, HEX(0x2c3a4f));
    for (int b = 0; b < l->sim.n; b++) { /* shapes */
        int i = l->body_node[b];
        const StNode *d = &m->nodes[i];
        const LBody *o = &l->sim.b[b];
        Vector2 p = {cx + o->x, cy + o->y};
        float a = node_alpha(s, i);
        bool hub = b == 0;
        Color f = s->fill[i], fillc, stroke = BLANK;
        float sw = 0;
        if (hub) fillc = C_RAISED, stroke = C_FOCUS, sw = 2;
        else if (d->kind == AN_DIR) fillc = sv_mix_lab(C_PANEL, f, 0.35f), stroke = f, sw = 3;
        else if (d->nkids) fillc = sv_mix_lab(C_PANEL, f, 0.7f), stroke = sv_brighter(f, 0.6f), sw = 1.5f;
        else fillc = f;
        if (i == s->hov && !hub) stroke = C_FOCUS, sw = 2;
        else if (partner(s, i)) stroke = WHITE, sw = 1.5f;
        else if (found(s, i) && !hub) stroke = C_HOT, sw = 2;
        DrawCircleV(p, o->r, ui_fade(fillc, a));
        if (sw > 0) {
            if (d->kind == AN_DIR && !hub) dashed_circle(p, o->r, sw, 5, 3, ui_fade(stroke, a));
            else DrawRing(p, o->r - sw / 2, o->r + sw / 2, 0, 360, 64, ui_fade(stroke, a));
        }
        if (!hub && d->kinds) flag_dots(d->kinds, p.x + o->r * 0.72f + 2, p.y - o->r * 0.72f - 2, a);
    }
    /* names; one under a small circle that would cover a name already drawn
     * (bigger nodes come first) is left out */
    Rectangle *taken = xmalloc(sizeof *taken * (size_t)l->sim.n);
    int ntaken = 0;
    for (int b = 0; b < l->sim.n; b++) {
        int i = l->body_node[b];
        const StNode *d = &m->nodes[i];
        const LBody *o = &l->sim.b[b];
        Vector2 p = {cx + o->x, cy + o->y};
        float a = node_alpha(s, i);
        Color ink = ui_fade(sv_ink(s->fill[i]), a);
        if (b == 0) {
            taken[ntaken++] = (Rectangle){p.x - o->r, p.y - o->r, 2 * o->r, 2 * o->r};
            clip_chars(fitb, sizeof fitb, d->name, o->r * 2 + 4);
            sv_txc(F_MONO_B, fitb, p.x, p.y + (F->parent >= 0 ? -0.45f * 13 : 0), 13, ui_fade(C_FG, a));
            if (F->parent >= 0) sv_txc(F_MONO, "\xE2\x86\x91 back", p.x, p.y + 13 - 3.5f, 10, ui_fade(C_DIM, 0.75f * a));
        } else if (l->below[b]) {
            clip_chars(fitb, sizeof fitb, d->name, 130);
            int fo = d->kind == AN_FUNC ? F_MONO : F_MONO_B;
            float w = sv_tw(fo, fitb, 11), ty = p.y + o->r + 12 - PX(11) * 0.8f;
            Rectangle box = {p.x - w / 2, ty, w, PX(11)};
            bool clash = false;
            for (int k = 0; k < ntaken && !clash; k++) clash = CheckCollisionRecs(box, taken[k]);
            if (clash) continue;
            taken[ntaken++] = box;
            sv_halo(fo, fitb, p.x - w / 2, ty, 11, ui_fade(C_FG, a), ui_fade(C_WIN, a));
        } else {
            clip_chars(fitb, sizeof fitb, d->name, o->r * 2);
            float w = sv_tw(F_MONO_B, fitb, 11);
            taken[ntaken++] = (Rectangle){p.x - w / 2, p.y - PX(11) / 2, w, PX(11)};
            sv_txc(d->kind == AN_FUNC ? F_MONO : F_MONO_B, fitb, p.x, p.y, 11, ink);
            buf[0] = 0;
            if (d->nkids) snprintf(buf, sizeof buf, "%d inside", d->nkids);
            else if (sv_is_def(d)) snprintf(buf, sizeof buf, "cx %d", d->cx);
            if (*buf) sv_txc(F_MONO, buf, p.x, p.y + 16 - 3.5f, 10, ui_fade(ink, 0.75f));
        }
    }
    free(taken);
}

static void draw_arcs(SView *s, Rectangle st, int from) {
    Vector2 a, b;
    float ra, rb;
    if (!s->arcs.n || !node_pos(s, st, from, &a, &ra)) return;
    const StNode *d = &s->m->nodes[from];
    for (int k = 0; k < s->arcs.n; k++) {
        if (!node_pos(s, st, s->arcs.vis[k], &b, &rb)) continue;
        const StPartner *P = &d->partners[s->arcs.pi[k]];
        float dx = b.x - a.x, dy = b.y - a.y, len = hypotf(dx, dy);
        if (len < 1) len = 1;
        float ka = fminf(ra, len / 3), kb = fminf(rb, len / 3), q = 0.22f;
        Vector2 p0 = {a.x + dx / len * ka, a.y + dy / len * ka}, p2 = {b.x - dx / len * kb, b.y - dy / len * kb};
        Vector2 c = {(p0.x + p2.x) / 2 - dy * q, (p0.y + p2.y) / 2 + dx * q};
        Color col = ui_fade(P->cross ? C_CORR : C_FOCUS, 0.8f);
        float w = 1 + fminf((float)P->shared, 6) * 0.45f, run = 0;
        Vector2 prev = p0;
        for (int j = 1; j <= 32; j++) {
            float t = (float)j / 32, u = 1 - t;
            Vector2 cur = {u * u * p0.x + 2 * u * t * c.x + t * t * p2.x, u * u * p0.y + 2 * u * t * c.y + t * t * p2.y};
            float seg = hypotf(cur.x - prev.x, cur.y - prev.y);
            if (s->arcs.exact[k] || fmodf(run, 8) < 4) DrawLineEx(prev, cur, w, col);
            run += seg;
            prev = cur;
        }
    }
}

static void draw_building(SView *s, Rectangle st) {
    char line[320], st_line[256];
    float cx = st.x + st.width / 2, cy = st.y + st.height / 2;
    if (s->m && !s->running) { /* built, and nothing in it */
        sv_txc(F_SANS, "No files to show: these sessions changed nothing here.", cx, cy, 15, C_DIM);
        return;
    }
    pthread_mutex_lock(&s->mu);
    snprintf(st_line, sizeof st_line, "%s", s->stats);
    pthread_mutex_unlock(&s->mu);
    int dots = (int)(GetTime() * 3) % 4;
    snprintf(line, sizeof line, "Building the structure view%.*s", dots, "...");
    sv_tx(F_SANS, line, cx - sv_tw(F_SANS, "Building the structure view...", 15) / 2, sv_mid(cy - 12, 15), 15, C_DIM);
    if (*st_line) snprintf(line, sizeof line, "%s", st_line);
    else snprintf(line, sizeof line, "replaying %ld events on today's files", s->added);
    char fitb[320];
    sv_fit(F_MONO, fitb, sizeof fitb, line, 12, st.width - 40);
    sv_txc(F_MONO, fitb, cx, cy + 14, 12, C_FAINT);
}

/* ---- bands ------------------------------------------------------------------ */

typedef struct {
    Rectangle bar, crumbs, stage, insp, strip, hint;
    Rectangle tab[3], mode[ST_NMODES], heat, prev, next, ticks, cur, legend, turns_lbl;
    Rectangle crumb[64];
    int crumb_node[64], ncrumb;
    Rectangle sbox, slist; /* the search box; its results (open only) */
} Geo;

/* The result list: a heading, the names, a heading, the lines; y of result k from its top. */
static float slist_h(const Search *q) { return 4 + 2 * SHEAD_H + (float)q->nnames * SNAME_H + (float)q->res.n * SHIT_H + 4; }
static float srow_y(const Search *q, int k) {
    return 4 + SHEAD_H + (k < q->nnames ? (float)k * SNAME_H : (float)q->nnames * SNAME_H + SHEAD_H + (float)(k - q->nnames) * SHIT_H);
}
static float srow_h(const Search *q, int k) { return k < q->nnames ? SNAME_H : SHIT_H; }

static float tab_w(int v) { return 10 + sv_tw(F_SANS, VIEW_NAME[v], 13) + 6 + sv_tw(F_MONO, "1", 11) + 10; }
static float mode_w(int k) { return 10 + sv_tw(F_SANS, MODE_NAME[k], 13) + 10; }

/* The bar as the draft's flex-wrap: brand and tabs, then the heat modes at
 * the right, on as many rows as the width needs. */
static float bar_layout(Geo *g, float W) {
    float x = sv_brand("structure", 0, false) + 16, y = 4, row = BAR_ROW, right = W - 12;
    for (int v = 0; v < 3; v++) {
        if (x + tab_w(v) > right && x > 12) x = 12, y += row;
        g->tab[v] = (Rectangle){x, y + 4, tab_w(v), 28};
        x += g->tab[v].width + 4;
    }
    float lw = sv_tw(F_SANS, "heat", 12) + 2, mw = lw, widest = 0;
    for (int k = 0; k < ST_NMODES; k++) mw += 4 + mode_w(k), widest = fmaxf(widest, 4 + mode_w(k));
    if (x + 12 + mw > right) x = 12, y += row; /* the modes on rows of their own */
    /* rows of about equal width, each flush right */
    float avail = right - x, rows = ceilf(mw / fmaxf(1, right - 12)), limit = fminf(avail, mw / rows + widest);
    int first = 0;
    float rx = lw;
    for (int k = 0; k <= ST_NMODES; k++) {
        if (k == ST_NMODES || (rx + 4 + mode_w(k) > limit && k > first)) { /* close the row [first, k) */
            float shift = right - rx - (first == 0 ? x : 12);
            float cx = (first == 0 ? x : 12) + shift + (first == 0 ? lw : 0);
            if (first == 0) g->heat = (Rectangle){cx - lw, y + 4, lw, 28};
            for (int j = first; j < k; j++) {
                cx += 4;
                g->mode[j] = (Rectangle){cx, y + 4, mode_w(j), 28};
                cx += mode_w(j);
            }
            if (k == ST_NMODES) break;
            first = k, rx = 0, y += row;
        }
        rx += 4 + mode_w(k);
    }
    return y + row + 4;
}

static Geo geometry(SView *s) {
    Geo g;
    memset(&g, 0, sizeof g);
    float W = (float)GetScreenWidth(), H = (float)GetScreenHeight();
    float bar_h = bar_layout(&g, W);
    g.bar = (Rectangle){0, 0, W, bar_h};
    g.crumbs = (Rectangle){0, bar_h, W, CRUMBS_H};
    float body_y = bar_h + CRUMBS_H, body_h = fmaxf(40, H - body_y - STRIP_H - HINT_H);
    float iw = fminf(INSP_W, fmaxf(fminf(300, W * 0.5f), W * 0.34f));
    g.stage = (Rectangle){0, body_y, W - iw, body_h};
    g.insp = (Rectangle){W - iw, body_y, iw, body_h};
    g.strip = (Rectangle){0, body_y + body_h, W, STRIP_H};
    g.hint = (Rectangle){0, body_y + body_h + STRIP_H, W, HINT_H};
    float sy = g.strip.y + (STRIP_H - 22) / 2, lw = sv_tws(F_SANS_B, "TURNS", 11, 0.9f);
    g.turns_lbl = (Rectangle){12, sy, lw, 22};
    float lx = 12 + lw + 10;
    g.prev = (Rectangle){lx, sy, 24, 22};
    g.next = (Rectangle){lx + 26, sy, 24, 22};
    /* the selected turn's title over the legend, in a column of its own width (bars don't shift on a pick) */
    float lgw = ui_turn_legend(ui_mono, PX(11), 0, 0, C_FAINT, false);
    float cw = fminf(W * 0.38f, fmaxf(lgw, fminf(W * 0.3f, 480)));
    g.cur = (Rectangle){W - 12 - cw, g.strip.y + 10, cw, 20};
    g.legend = (Rectangle){g.cur.x, g.strip.y + 34, lgw <= cw ? lgw : 0, 20};
    float tx0 = lx + 52 + 10;
    g.ticks = (Rectangle){tx0, g.strip.y + (STRIP_H - BARS_H) / 2, fmaxf(0, g.cur.x - 14 - tx0), BARS_H};
    if (s->m && s->focus >= 0) { /* breadcrumb buttons */
        int path[64], np = 0;
        for (int n = s->focus; n >= 0 && np < 64; n = s->m->nodes[n].parent) path[np++] = n;
        float cx = 12;
        for (int k = np - 1; k >= 0 && g.ncrumb < 64; k--) {
            float w = sv_tw(F_MONO, s->m->nodes[path[k]].name, 13) + 10;
            g.crumb[g.ncrumb] = (Rectangle){cx, g.crumbs.y + (CRUMBS_H - 24) / 2, w, 24};
            g.crumb_node[g.ncrumb++] = path[k];
            cx += w + sv_tw(F_MONO, "/", 13) + 6;
        }
    }
    /* the search box at the stage's right end of the path's band, its results under it */
    float bw = fminf(420, fmaxf(220, g.stage.width * 0.42f));
    g.sbox = (Rectangle){g.stage.x + g.stage.width - 12 - bw, g.crumbs.y + (CRUMBS_H - 28) / 2, bw, 28};
    if (s->sr.open && s->m) {
        float sw = fminf(fmaxf(bw, 660), W - 24), sh = fminf(slist_h(&s->sr), fmaxf(80, g.stage.y + g.stage.height - g.sbox.y - 28 - 12));
        g.slist = (Rectangle){fminf(g.sbox.x, W - 12 - sw), g.sbox.y + g.sbox.height + 4, sw, sh};
    }
    return g;
}

/* A title over r: shown once hovered for TIP_DELAY (one at a time). */
static const char *title_at(Vector2 mp, Rectangle r, const char *t) { return CheckCollisionPointRec(mp, r) ? t : NULL; }

static void draw_bar(SView *s, const Geo *g, Vector2 mp) {
    DrawRectangleRec(g->bar, C_PANEL);
    sv_hline(0, g->bar.width, g->bar.y + g->bar.height - 1, C_LINE);
    sv_brand("structure", 4 + BAR_ROW / 2, true);
    for (int v = 0; v < 3; v++) {
        Rectangle r = g->tab[v];
        bool sel = v == s->view, hot = CheckCollisionPointRec(mp, r);
        if (sel || hot) DrawRectangleRounded(r, 5 / 14.0f, 6, C_RAISED);
        if (sel) DrawRectangleRoundedLinesEx(r, 5 / 14.0f, 6, 1, C_LINE);
        float tw = sv_tw(F_SANS, VIEW_NAME[v], 13);
        sv_tx(F_SANS, VIEW_NAME[v], r.x + 10, sv_mid(r.y + 14, 13), 13, sel || hot ? C_FG : C_DIM);
        char k[2] = {(char)('1' + v), 0};
        sv_tx(F_MONO, k, r.x + 10 + tw + 6, sv_mid(r.y + 15, 11), 11, sel ? C_DIM : C_FAINT);
    }
    if (s->running && s->m) { /* a live rebuild: the old one stays on screen */
        float x = g->tab[2].x + g->tab[2].width + 14;
        if (x + sv_tw(F_SANS, "updating\xE2\x80\xA6", 12) < g->heat.x - 8 || g->heat.y > g->tab[2].y)
            sv_tx(F_SANS, "updating\xE2\x80\xA6", x, sv_mid(g->tab[2].y + 14, 12), 12, C_FAINT);
    }
    sv_tx(F_SANS, "heat", g->heat.x, sv_mid(g->heat.y + 15, 12), 12, C_FAINT);
    for (int k = 0; k < ST_NMODES; k++) {
        Rectangle r = g->mode[k];
        bool sel = k == s->mode, hot = CheckCollisionPointRec(mp, r);
        if (sel) DrawRectangleRounded(r, 5 / 14.0f, 6, C_HOT);
        else if (hot) DrawRectangleRounded(r, 5 / 14.0f, 6, C_RAISED);
        sv_tx(F_SANS, MODE_NAME[k], r.x + 10, sv_mid(r.y + 14, 13), 13, sel ? C_BG : hot ? C_FG : C_DIM);
    }
}

static void draw_crumbs(SView *s, const Geo *g, Vector2 mp) {
    Rectangle c = g->crumbs;
    DrawRectangleRec(c, C_WIN);
    sv_hline(0, c.width, c.y + c.height - 1, C_LINE);
    float cy = c.y + c.height / 2, end;
    BeginScissorMode(0, (int)c.y, (int)(g->sbox.x - 8), (int)c.height);
    for (int k = 0; k < g->ncrumb; k++) {
        Rectangle r = g->crumb[k];
        const char *nm = s->m->nodes[g->crumb_node[k]].name;
        bool last = k == g->ncrumb - 1;
        if (!last && CheckCollisionPointRec(mp, r) && mp.x < g->sbox.x - 8) DrawRectangleRounded(r, 4 / 12.0f, 6, C_RAISED);
        sv_tx(F_MONO, nm, r.x + 5, sv_mid(cy, 13), 13, last ? C_FG : C_FOCUS);
        if (!last) sv_tx(F_MONO, "/", r.x + r.width + 3, sv_mid(cy, 13), 13, C_FAINT);
    }
    EndScissorMode();
    end = g->sbox.x + g->sbox.width; /* the scale goes right of the search box */
    /* the scale: lo -> hi words, or the owners' colours */
    float x = c.width - 12;
    if (s->mode == ST_OWNER && s->m) {
        int n = s->m->nactors < 6 ? s->m->nactors : 6;
        float w = 0, ws[6];
        char nm[6][96];
        for (int i = 0; i < n; i++) {
            sv_actor_name(s->m->snap, s->m->actors[i], true, nm[i], sizeof nm[i]);
            slice_chars(nm[i], 22);
            ws[i] = 8 + 9 + 4 + sv_tw(F_MONO, nm[i], 11.5f);
            w += ws[i];
        }
        if (x - w < end + 20) return;
        x -= w;
        for (int i = 0; i < n; i++) {
            DrawRectangleRounded((Rectangle){x + 8, cy - 4.5f, 9, 9}, 0.3f, 4, sv_actor_col(i));
            sv_tx(F_MONO, nm[i], x + 21, sv_mid(cy, 11.5f), 11.5f, C_FAINT);
            x += ws[i];
        }
        return;
    }
    char hi[96];
    snprintf(hi, sizeof hi, "%s at its level", MODE_HI[s->mode]);
    float wl = sv_tw(F_MONO, MODE_LO[s->mode], 11.5f), wh = sv_tw(F_MONO, hi, 11.5f);
    if (x - wl - wh - 120 - 16 < end + 20) return;
    x -= wh;
    sv_tx(F_MONO, hi, x, sv_mid(cy, 11.5f), 11.5f, C_FAINT);
    x -= 8 + 120;
    static const float at[] = {0, 0.3f, 0.7f, 1};
    for (int i = 0; i < 3; i++)
        DrawRectangleGradientH((int)(x + at[i] * 120), (int)(cy - 4), (int)ceilf((at[i + 1] - at[i]) * 120), 8,
                               sv_ramp(at[i]), sv_ramp(at[i + 1]));
    x -= 8 + wl;
    sv_tx(F_MONO, MODE_LO[s->mode], x, sv_mid(cy, 11.5f), 11.5f, C_FAINT);
}

/* A turn's tags for its bar (ui_turn_bar). */
static int turn_tags(const StMetrics *m, int t) {
    const StTurn *T = &m->turns[t];
    const AnTurn *a = &m->snap->turns[t];
    return (a->fix ? UT_FIX : 0) | (a->corr || a->intr ? UT_CORR : 0) | (T->failing ? UT_FAILING : 0) |
           (T->untested ? UT_UNTESTED : 0) | (T->slow ? UT_SLOW : 0);
}

/* The turn strip's bars, in time order: flex-grow widths (bar_w) with a 4 px
 * minimum and 2 px gaps; when crowded a 2, then 1 px minimum with 1 px gaps;
 * past that, bins of consecutive turns 2 px wide. Snapped to whole pixels so
 * every gap shows: touching bars of one colour would read as one block.
 * Drawing and hit-testing both read these. Cached per (build, strip x, width). */
static void bar_spans(SView *s, Rectangle t) {
    const StMetrics *m = s->m;
    if (s->bars.m == m && s->bars.x0 == t.x && s->bars.w == t.width) return;
    int n = m->nturns;
    float W = floorf(t.width), gap = 2, minw = 4;
    s->bars.m = m, s->bars.x0 = t.x, s->bars.w = t.width;
    if (n * (minw + gap) - gap > W) gap = 1, minw = n * 3 - 1 <= W ? 2 : 1;
    int ns = n * (minw + gap) - gap > W ? (int)((W + gap) / (2 + gap)) : n; /* bins */
    if (ns < 1) ns = 1;
    s->bars.slot = xrealloc(s->bars.slot, sizeof *s->bars.slot * (size_t)ns);
    s->bars.n = n ? ns : 0, s->bars.gap = gap;
    if (!n) return;
    float *w = xcalloc((size_t)ns, sizeof *w);
    if (ns < n) {
        for (int k = 0; k < ns; k++) w[k] = 2;
    } else { /* flex: those under the minimum get it, the rest share what's left */
        bool *mn = xcalloc((size_t)n, sizeof *mn), again = true;
        float avail = W - gap * (float)(n - 1), fixed = 0, grow = 0;
        while (again) {
            again = false, grow = fixed = 0;
            for (int k = 0; k < n; k++) {
                if (mn[k]) fixed += minw;
                else grow += (float)m->turns[m->order[k]].bar_w;
            }
            for (int k = 0; k < n; k++)
                if (!mn[k] && (grow > 0 ? (float)m->turns[m->order[k]].bar_w / grow * (avail - fixed) : 0) < minw)
                    mn[k] = again = true;
        }
        for (int k = 0; k < n; k++)
            w[k] = mn[k] ? minw : (float)m->turns[m->order[k]].bar_w / grow * fmaxf(0, avail - fixed);
        free(mn);
    }
    float cx = t.x;
    for (int k = 0; k < ns; k++) {
        Slot *q = &s->bars.slot[k];
        q->x = floorf(cx + 0.5f);
        q->w = fmaxf(1, floorf(cx + w[k] + 0.5f) - q->x);
        q->a = (int)((long long)k * n / ns), q->b = (int)((long long)(k + 1) * n / ns);
        cx += w[k] + gap;
    }
    free(w);
}

/* The turn under the mouse: its slot's, or in a bin the one at the mouse's place in it. */
static int strip_hit(SView *s, const Geo *g, Vector2 mp) {
    const StMetrics *m = s->m;
    if (!m || !m->nturns || g->ticks.width <= 0 || !CheckCollisionPointRec(mp, g->ticks)) return -1;
    bar_spans(s, g->ticks);
    for (int k = 0; k < s->bars.n; k++) {
        const Slot *q = &s->bars.slot[k];
        float span = q->w + s->bars.gap;
        if (mp.x < q->x || mp.x >= q->x + span) continue;
        int i = q->a + (int)((mp.x - q->x) / span * (float)(q->b - q->a));
        return m->order[i < q->b ? i : q->b - 1];
    }
    return -1;
}

static void draw_strip(SView *s, const Geo *g, Vector2 mp) {
    Rectangle r = g->strip;
    DrawRectangleRec(r, C_PANEL);
    sv_hline(0, r.width, r.y, C_LINE);
    float cy = r.y + r.height / 2;
    sv_txs(F_SANS_B, "TURNS", 12, sv_mid(cy, 11), 11, 0.9f, C_FAINT);
    for (int b = 0; b < 2; b++) {
        Rectangle q = b ? g->next : g->prev;
        ui_box(q, 4 / 11.0f, CheckCollisionPointRec(mp, q) ? sv_brighter(C_RAISED, 0.5f) : C_RAISED, 1, C_LINE);
        sv_txc(F_MONO, b ? "\xE2\x80\xBA" : "\xE2\x80\xB9", q.x + q.width / 2, q.y + q.height / 2, 13, C_DIM);
    }
    const StMetrics *m = s->m;
    if (m && m->nturns && g->ticks.width > 0) {
        bar_spans(s, g->ticks);
        int hot = strip_hit(s, g, mp);
        float base = g->ticks.y + g->ticks.height;
        BeginScissorMode((int)g->ticks.x - 1, (int)g->ticks.y - 2, (int)g->ticks.width + 2, (int)g->ticks.height + 3);
        for (int k = 0; k < s->bars.n; k++) {
            const Slot *q = &s->bars.slot[k];
            double bh = 0;
            int tags = 0;
            bool sel = false, hov = false;
            for (int i = q->a; i < q->b; i++) { /* a bin: its tallest, all its tags */
                int t = m->order[i];
                bh = fmax(bh, m->turns[t].bar_h);
                tags |= turn_tags(m, t);
                sel |= t == s->turn, hov |= t == hot;
            }
            float h = floorf((float)bh / 22 * BARS_H + 0.5f);
            ui_turn_bar((Rectangle){q->x, base - h, q->w, h}, tags, sel, hov);
        }
        EndScissorMode();
    }
    char fitb[512];
    bool sel = m && s->turn >= 0;
    const char *ct = sel ? m->snap->turns[s->turn].title : "pick a turn: everything one prompt changed";
    sv_fit(F_SANS, fitb, sizeof fitb, ct ? ct : "", 12.5f, g->cur.width + 1);
    sv_tx(F_SANS, fitb, g->cur.x, sv_mid(g->cur.y + g->cur.height / 2, 12.5f), 12.5f, sel ? C_FG : C_DIM);
    if (g->legend.width > 0)
        ui_turn_legend(ui_mono, PX(11), g->legend.x, g->legend.y + g->legend.height / 2, C_FAINT, true);
}

static void draw_hint(const Geo *g) {
    Rectangle r = g->hint;
    DrawRectangleRec(r, C_WIN);
    sv_hline(0, r.width, r.y, C_LINE);
    float cy = r.y + r.height / 2;
    static const char *const keys[][2] = {{"hover", "inspect + partners"}, {"wheel", "scroll panel"},
                                          {"click", "open"},               {"center / path / Esc", "back"},
                                          {"/", "search"},                 {"1 2 3", "views"},
                                          {"[ ]", "turns"},                {"S", "stage"}};
    static const char *const flags[] = {"corrected / interrupted", "untested / failing", "changed unread", "slow"};
    static const int fk[] = {STK_CORR, STK_TEST, STK_BLIND, STK_SLOW};
    float kw = 0;
    for (int i = 0; i < 4; i++) kw += 13 + sv_tw(F_MONO, flags[i], 12) + (i ? 12 : 0);
    float kx = r.width - 12 - kw, x = sv_hints(keys, 8, 12, cy, kx - 18, 18);
    if (kx < x) return;
    for (int i = 0; i < 4; i++) {
        DrawCircleV((Vector2){kx + 4, cy}, 4, sv_flag_col(fk[i]));
        sv_tx(F_MONO, flags[i], kx + 13, sv_mid(cy, 12), 12, C_FAINT);
        kx += 13 + sv_tw(F_MONO, flags[i], 12) + 12;
    }
}

/* ---- the search box and its results ------------------------------------------- */

/* The result under mp, -1 none. */
static int search_row_at(const SView *s, const Geo *g, Vector2 mp) {
    const Search *q = &s->sr;
    if (!q->open || !CheckCollisionPointRec(mp, g->slist)) return -1;
    float y = mp.y - g->slist.y + q->scroll;
    for (int k = 0, n = search_total(q); k < n; k++)
        if (y >= srow_y(q, k) && y < srow_y(q, k) + srow_h(q, k)) return k;
    return -1;
}

static bool search_over(const SView *s, const Geo *g, Vector2 mp) {
    return CheckCollisionPointRec(mp, g->sbox) || (s->sr.open && CheckCollisionPointRec(mp, g->slist));
}

/* A click: on a result, it; on the closed box, open it; elsewhere, close an
 * open box (and nothing else). True when it took the click. */
static bool search_press(SView *s, const Geo *g, Vector2 mp) {
    Search *q = &s->sr;
    if (!s->m) return false;
    if (q->open && CheckCollisionPointRec(mp, g->slist)) {
        search_go(s, search_row_at(s, g, mp));
        return true;
    }
    if (CheckCollisionPointRec(mp, g->sbox)) {
        if (!q->open) search_open(s);
        return true;
    }
    if (!q->open) return false;
    q->open = false; /* only closes it: the keys go back to the screen first */
    return true;
}

/* s in mono at css, its middle at cy: the bytes mk marks bold in hc (on a
 * faint amber box when box), the rest in c; cut to maxw with "…", from the
 * left (a path keeps its name) or the right. */
static void draw_marked(const char *s, const unsigned char *mk, float x, float cy, float css, Color c, Color hc, bool box,
                        float maxw, bool from_left) {
    size_t n = strlen(s), a = 0, b = n;
    float cw = sv_tw(F_MONO, "M", css), y = sv_mid(cy, css);
    int len = (int)utf8_count(s, SIZE_MAX);
    if ((float)len * cw > maxw) {
        int fit = (int)((maxw - cw) / cw), skip = from_left ? len - fit : fit;
        if (fit < 1) return;
        size_t i = 0;
        for (int k = 0; i < n && k < skip; k++)
            for (i++; i < n && UTF8_CONT(s[i]); i++) {}
        if (from_left) a = i;
        else b = i;
    }
    if (a) sv_tx(F_MONO, "\xE2\x80\xA6", x, y, css, C_FAINT), x += cw;
    char run[256];
    for (size_t i = a; i < b;) {
        bool on = mk && mk[i];
        size_t j = i + 1;
        while (j < b && (UTF8_CONT(s[j]) || (j - i < sizeof run - 8 && (!mk || !!mk[j] == on)))) j++;
        memcpy(run, s + i, j - i);
        run[j - i] = 0;
        float w = (float)utf8_count(run, SIZE_MAX) * cw;
        if (on && box) DrawRectangleRounded((Rectangle){x - 1, cy - PX(css) / 2 - 1, w + 2, PX(css) + 2}, 0.3f, 4, ui_fade(C_HOT, 0.22f));
        sv_tx(on ? F_MONO_B : F_MONO, run, x, y, css, on ? hc : c);
        x += w;
        i = j;
    }
    if (b < n) sv_tx(F_MONO, "\xE2\x80\xA6", x, y, css, C_FAINT);
}

/* A magnifier at (x, y), its lens' centre */
static void magnifier(float x, float y, Color c) {
    DrawRing((Vector2){x, y}, 3.6f, 5.0f, 0, 360, 24, c);
    DrawLineEx((Vector2){x + 3.6f, y + 3.6f}, (Vector2){x + 7.5f, y + 7.5f}, 1.6f, c);
}

static void search_heading(const char *label, const char *what, float x, float y) {
    float cy = y + SHEAD_H / 2 + 2;
    sv_txs(F_SANS_B, label, x + 12, sv_mid(cy, 10.5f), 10.5f, 0.9f, C_FAINT);
    sv_tx(F_SANS, what, x + 12 + sv_tws(F_SANS_B, label, 10.5f, 0.9f) + 10, sv_mid(cy, 12), 12, C_DIM);
}

static void draw_search(SView *s, const Geo *g, Vector2 mp) {
    Search *q = &s->sr;
    Rectangle b = g->sbox;
    float cy = b.y + b.height / 2;
    ui_box(b, 5 / 14.0f, C_BG, 1, q->open ? C_FOCUS : CheckCollisionPointRec(mp, b) ? C_DIM : C_LINE);
    magnifier(b.x + 14, cy - 1, q->open || q->q[0] ? C_SOFT : C_FAINT);
    char right[96] = "";
    int total = search_total(q);
    if (q->q[0] && !q->open && total && q->went) snprintf(right, sizeof right, "%d/%d  n N", q->sel + 1, total);
    else if (q->q[0] && !q->open && total) snprintf(right, sizeof right, "\xE2\x80\x93/%d  n N", total);
    else if (q->q[0] && search_busy(s)) snprintf(right, sizeof right, "\xE2\x80\xA6");
    else if (q->q[0]) snprintf(right, sizeof right, "%d", total);
    float rw = *right ? sv_tw(F_MONO, right, 11) + 10 : 0, tx = b.x + 28, tw = b.width - 28 - 8 - rw;
    if (*right) sv_tx(F_MONO, right, b.x + b.width - 8 - rw + 10, sv_mid(cy, 11), 11, C_FAINT);
    if (!q->q[0]) {
        sv_tx(F_SANS, q->open ? "names and file contents" : "/  search names and file contents", tx, sv_mid(cy, 12.5f), 12.5f, C_FAINT);
    } else {
        float qw = fminf(tw, (float)utf8_count(q->q, SIZE_MAX) * sv_tw(F_MONO, "M", 13));
        if (q->open && q->all) DrawRectangle((int)tx - 1, (int)(cy - 9), (int)qw + 2, 18, ui_fade(C_FOCUS, 0.35f));
        draw_marked(q->q, NULL, tx, cy, 13, q->open ? C_FG : C_SOFT, C_FG, false, tw, true);
        if (q->open && !q->all) DrawRectangle((int)(tx + qw + 1), (int)(cy - 8), 1, 16, C_FOCUS);
    }
    if (q->open && !q->q[0]) DrawRectangle((int)tx - 1, (int)(cy - 8), 1, 16, C_FOCUS);
    if (!q->open) return;

    /* the results */
    Rectangle r = g->slist;
    float full = slist_h(q), maxs = fmaxf(0, full - r.height);
    if (q->follow && total) { /* the selection in view, with its heading when it's a section's first */
        int k = q->sel < total ? q->sel : total - 1;
        float y0 = srow_y(q, k) - (k == 0 ? 4 + SHEAD_H : k == q->nnames ? SHEAD_H : 0), y1 = srow_y(q, k) + srow_h(q, k);
        if (y0 < q->scroll) q->scroll = y0;
        if (y1 > q->scroll + r.height) q->scroll = y1 - r.height + 4;
        q->follow = false;
    }
    q->scroll = fmaxf(0, fminf(maxs, q->scroll));
    DrawRectangleRounded((Rectangle){r.x + 2, r.y + 3, r.width, r.height}, 6 / fminf(r.width, r.height), 6, ui_fade(BLACK, 0.35f));
    ui_box(r, 6 / fminf(r.width, r.height), HEX(0x0b0f15), 1, C_LINE);
    BeginScissorMode((int)r.x + 1, (int)r.y + 1, (int)r.width - 2, (int)r.height - 2);
    float oy = r.y - q->scroll, w = r.width;
    int hot = search_row_at(s, g, mp);
    char what[160], buf[600];
    const StMetrics *m = s->m;
    if (!q->nall) snprintf(what, sizeof what, "none");
    else if (q->nall > q->nnames) snprintf(what, sizeof what, "%d of %d", q->nnames, q->nall);
    else snprintf(what, sizeof what, "%d", q->nall);
    search_heading("NAMES", what, r.x, oy + 4);
    if (utf8_count(q->q, SIZE_MAX) < 2) snprintf(what, sizeof what, "type 2 characters");
    else if (q->gen && q->res.gen != q->gen) snprintf(what, sizeof what, "searching\xE2\x80\xA6");
    else if (!q->res.n) snprintf(what, sizeof what, "none");
    else if (q->res.capped) snprintf(what, sizeof what, "%d+ lines in %d files", SS_CAP, q->hfiles);
    else snprintf(what, sizeof what, "%d line%s in %d file%s", q->res.n, q->res.n == 1 ? "" : "s", q->hfiles,
                  q->hfiles == 1 ? "" : "s");
    search_heading("IN FILES", what, r.x, oy + 4 + SHEAD_H + (float)q->nnames * SNAME_H);
    for (int k = 0; k < total; k++) {
        float y = oy + srow_y(q, k), h = srow_h(q, k);
        if (y + h < r.y || y > r.y + r.height) continue;
        Rectangle row = {r.x + 4, y, w - 8, h};
        if (k == q->sel) {
            DrawRectangleRounded(row, 4 / h, 6, sv_brighter(C_RAISED, 0.4f));
            DrawRectangle((int)row.x, (int)row.y + 3, 2, (int)h - 6, C_FOCUS);
        } else if (k == hot) DrawRectangleRounded(row, 4 / h, 6, C_RAISED);
        if (k < q->nnames) { /* kind, path with the matched characters */
            const NameHit *nh = &q->names[k];
            const StNode *d = &m->nodes[nh->node];
            const char *path = sv_rel(d->id);
            size_t pn = strlen(path);
            unsigned char *mk = xcalloc(pn + 1, 1);
            for (int i = 0; i < nh->m.npos; i++)
                for (size_t j = (size_t)nh->m.pos[i]; j < pn && (j == (size_t)nh->m.pos[i] || UTF8_CONT(path[j])); j++)
                    mk[j] = 1;
            sv_tx(F_SANS, sv_kind_word(d->kind), row.x + 10, sv_mid(y + h / 2, 11.5f), 11.5f, C_FAINT);
            draw_marked(path, mk, row.x + 74, y + h / 2, 12.5f, C_SOFT, C_HOT, false, row.width - 84, true);
            free(mk);
            continue;
        }
        /* path:line, the function holding it; the line, its match marked */
        const SsHit *hh = &q->res.hits[k - q->nnames];
        int def = q->hdef[k - q->nnames];
        const StNode *f = &m->nodes[q->file_node[hh->file]];
        snprintf(buf, sizeof buf, "%s:%d", sv_rel(f->id), hh->line);
        char fn[300] = "";
        if (def >= 0) snprintf(fn, sizeof fn, "in %s", m->nodes[def].name);
        float fw = *fn ? fminf(sv_tw(F_SANS, fn, 12), row.width * 0.4f) : 0;
        draw_marked(buf, NULL, row.x + 10, y + 11, 12, C_DIM, C_FG, false, row.width - 20 - (fw ? fw + 14 : 0), true);
        if (*fn) {
            char fitb[300];
            sv_fit(F_SANS, fitb, sizeof fitb, fn, 12, fw + 1);
            sv_tx(F_SANS, fitb, row.x + row.width - 10 - fw, sv_mid(y + 11, 12), 12, C_FAINT);
        }
        size_t sn = strlen(hh->snip);
        unsigned char *mk = xcalloc(sn + 1, 1);
        for (int j = hh->ms; j < hh->ms + hh->ml && (size_t)j < sn; j++) mk[j] = 1;
        draw_marked(hh->snip, mk, row.x + 22, y + 28, 12.5f, C_SOFT, C_HOT, true, row.width - 32, false);
        free(mk);
    }
    EndScissorMode();
    if (maxs > 0) ui_thumb(r.x + r.width - 6, r.y + 4, 3, r.height - 8, r.height, full, q->scroll, maxs, 20, ui_fade(C_DIM, 0.45f));
}

/* ---- the frame -------------------------------------------------------------- */

bool sv_keys(SView *s) {
    if (!s->open) return false;
    bool ctrl = IsKeyDown(KEY_LEFT_CONTROL) || IsKeyDown(KEY_RIGHT_CONTROL);
    if (s->sr.open) { /* typing: the screen's keys stay off; Esc closes the box first */
        search_type(s, ctrl);
        return IsKeyPressed(KEY_ESCAPE);
    }
    if (search_keys(s, ctrl)) return false;
    if (IsKeyPressed(KEY_ONE)) s->view = 0;
    if (IsKeyPressed(KEY_TWO)) s->view = 1;
    if (IsKeyPressed(KEY_THREE)) s->view = 2;
    if (IsKeyPressed(KEY_R)) request_build(s, true);
    bool esc = IsKeyPressed(KEY_ESCAPE);
    if (!s->m) {
        if (esc) s->open = false;
        return esc;
    }
    if (IsKeyPressed(KEY_LEFT_BRACKET)) step_turn(s, -1);
    if (IsKeyPressed(KEY_RIGHT_BRACKET)) step_turn(s, 1);
    int up = s->focus >= 0 ? s->m->nodes[s->focus].parent : -1;
    if (esc) {
        if (s->sr.q[0]) s->sr.q[0] = 0, search_edit(s); /* a query undims, as a turn does */
        else if (s->turn >= 0) select_turn(s, -1);
        else if (up >= 0) set_focus(s, up);
        else s->open = false;
    }
    if (IsKeyPressed(KEY_BACKSPACE) && up >= 0) set_focus(s, up);
    return esc;
}

/* The inspector's code line under mp (0 none), its file's absolute path into
 * path: a file still on disk (ids start with the root's name). */
static int code_at(SView *s, Rectangle insp, Vector2 mp, char *path, size_t n) {
    const char *id, *rel;
    int line = si_code_at(&s->insp, insp, mp, &id);
    if (line <= 0 || !*(rel = sv_rel(id))) return 0;
    snprintf(path, n, "%s/%s", s->root, rel);
    struct stat st;
    return !stat(path, &st) && S_ISREG(st.st_mode) ? line : 0;
}

static bool handle_press(SView *s, const Geo *g, Vector2 mp) {
    if (search_press(s, g, mp)) return true;
    for (int v = 0; v < 3; v++)
        if (CheckCollisionPointRec(mp, g->tab[v])) { s->view = v; return true; }
    for (int k = 0; k < ST_NMODES; k++)
        if (CheckCollisionPointRec(mp, g->mode[k])) { s->mode = k; return true; }
    if (!s->m) return true;
    for (int k = 0; k < g->ncrumb - 1; k++) /* not where the box covers them */
        if (CheckCollisionPointRec(mp, g->crumb[k]) && mp.x < g->sbox.x - 8) { set_focus(s, g->crumb_node[k]); return true; }
    if (CheckCollisionPointRec(mp, g->prev)) { step_turn(s, -1); return true; }
    if (CheckCollisionPointRec(mp, g->next)) { step_turn(s, 1); return true; }
    if (CheckCollisionPointRec(mp, g->cur)) {
        if (s->turn >= 0) s->shown_turn = s->turn;
        return true;
    }
    int t = strip_hit(s, g, mp);
    if (t >= 0) { select_turn(s, t == s->turn ? -1 : t); return true; }
    if (CheckCollisionPointRec(mp, g->insp)) {
        if (s->insp.doc.m != s->m) return true; /* laid out for the build just swapped out */
        char path[PATH_MAX];
        int line = code_at(s, g->insp, mp, path, sizeof path);
        if (line) { /* a line of code: in nvim */
            nv_open(s->root, path, line);
            return true;
        }
        SiAct a = si_click(&s->insp, g->insp, mp);
        if (a.turn >= 0) select_turn(s, a.turn);
        else if (a.node >= 0) go(s, a.node);
        return true;
    }
    return false;
}

/* Live: a rebuild once events stop for a moment (or have waited long enough). */
static void live(SView *s) {
    if (!s->m || s->running || s->added == s->built) return;
    double now = GetTime();
    if (now - s->last_add >= DEBOUNCE || now - s->first_pending >= DEBOUNCE_MAX) start_build(s);
}

void sv_frame(SView *s, Vector2 mp, float dt) {
    swap_in(s);
    apply_hooks(s);
    if (!s->open) return;
    live(s);
    search_update(s);
    Search *q = &s->sr;
    if (q->hold && q->hold_at.x < 0) q->hold_at = mp;
    Geo g = geometry(s);
    float wheel = GetMouseWheelMove();
    const StMetrics *m = s->m;
    bool ready = m && s->focus >= 0;
    if (ready) ensure_fill(s), ensure_layout(s, g.stage.width, g.stage.height);
    Lay *l = &s->lay;
    /* input: bands first, then the stage */
    if (IsMouseButtonPressed(MOUSE_BUTTON_LEFT)) {
        drop_press(s);
        s->pressed = true;
        s->press_at = mp;
        if (!handle_press(s, &g, mp) && ready) s->press_node = stage_hit(s, g.stage, mp);
        g = geometry(s);
        if (ready) ensure_fill(s), ensure_layout(s, g.stage.width, g.stage.height);
    }
    if (ready && s->pressed && s->press_node >= 0 && s->view == 2 && l->node_body && IsMouseButtonDown(MOUSE_BUTTON_LEFT)) {
        int b = l->node_body[s->press_node];
        if (s->drag_body < 0 && b > 0 && hypotf(mp.x - s->press_at.x, mp.y - s->press_at.y) > 3) {
            s->drag_body = b;
            l->sim.alpha_target = 0.2f;
        }
        if (s->drag_body > 0) {
            LBody *o = &l->sim.b[s->drag_body];
            o->pinned = true;
            o->fx = mp.x - g.stage.x - g.stage.width / 2;
            o->fy = mp.y - g.stage.y - g.stage.height / 2;
        }
    }
    if (s->pressed && !IsMouseButtonDown(MOUSE_BUTTON_LEFT)) {
        if (s->drag_body > 0) {
            l->sim.b[s->drag_body].pinned = false;
            l->sim.alpha_target = 0.01f;
        } else if (ready && s->press_node >= 0 && s->drag_body < 0 && hypotf(mp.x - s->press_at.x, mp.y - s->press_at.y) < 4 &&
                   stage_hit(s, g.stage, mp) == s->press_node) {
            open_node(s, s->press_node);
            ensure_layout(s, g.stage.width, g.stage.height);
        }
        drop_press(s);
    }
    /* Over the stage the wheel scrolls the panel too (si_wheel: outside the code box), so the hovered
     * node's panel reads without crossing other nodes to reach it. */
    bool over = ready && search_over(s, &g, mp);
    if (wheel != 0 && over) q->scroll -= wheel * 3 * SNAME_H;
    else if (wheel != 0 && ready && (CheckCollisionPointRec(mp, g.insp) || CheckCollisionPointRec(mp, g.stage)))
        si_wheel(&s->insp, g.insp, mp, wheel, IsKeyDown(KEY_LEFT_SHIFT) || IsKeyDown(KEY_RIGHT_SHIFT));
    if (ready && s->view == 2 && l->sim.n) {
        s->clock += dt;
        lay_sim_tick(&l->sim, s->clock, true);
        lay_sim_clamp(&l->sim, g.stage.width, g.stage.height, l->below);
    }
    s->hov = -1;
    if (ready && !over && !(s->pressed && s->press_node < 0))
        s->hov = s->drag_body > 0 ? l->body_node[s->drag_body] : stage_hit(s, g.stage, mp);
    /* hovering inspects, and it stays when the mouse moves on; after a search's jump, once the mouse moves */
    if (q->hold && q->hold_at.x >= 0 && hypotf(mp.x - q->hold_at.x, mp.y - q->hold_at.y) > 3) q->hold = false;
    if (s->hov >= 0 && !q->hold) inspect(s, s->hov);

    /* drawing */
    DrawRectangle(0, 0, GetScreenWidth(), GetScreenHeight(), C_WIN);
    draw_bar(s, &g, mp);
    if (ready) draw_crumbs(s, &g, mp);
    else DrawRectangleRec(g.crumbs, C_WIN), sv_hline(0, g.crumbs.width, g.crumbs.y + g.crumbs.height - 1, C_LINE);
    if (!ready) {
        draw_building(s, g.stage);
    } else {
        int from = s->shown_turn >= 0 ? -1 : s->shown;
        ensure_arcs(s, g.stage, from);
        BeginScissorMode((int)g.stage.x, (int)g.stage.y, (int)g.stage.width, (int)g.stage.height);
        if (s->view == 0) draw_icicle(s, g.stage);
        else if (s->view == 1) draw_treemap(s, g.stage);
        else draw_floating(s, g.stage);
        draw_arcs(s, g.stage, from);
        EndScissorMode();
    }
    DrawRectangleRec(g.insp, C_PANEL);
    DrawRectangle((int)g.insp.x, (int)g.insp.y, 1, (int)g.insp.height, C_LINE);
    s->insp.nvim = false;
    if (ready) {
        si_show(&s->insp, m, s->fill, s->mode, s->shown, s->shown_turn, g.insp.width - 28);
        char path[PATH_MAX];
        Vector2 imp = over ? (Vector2){-1, -1} : mp; /* the result list covers the inspector there */
        s->insp.nvim = !s->pressed && !over && code_at(s, g.insp, imp, path, sizeof path) && nv_find(s->root, NULL, 0);
        si_draw(&s->insp, g.insp, imp);
    }
    ui_hand(s->insp.nvim);
    draw_strip(s, &g, mp);
    draw_hint(&g);
    if (ready) draw_search(s, &g, mp);
    /* tooltips: the node under the mouse at once; titles after a moment */
    char buf[600];
    int bar;
    if (over) return;
    if (ready && s->hov >= 0 && !s->pressed) {
        const StNode *d = &m->nodes[s->hov];
        char t[64];
        sv_plural(t, sizeof t, d->turns, "turn");
        if (sv_is_def(d)) snprintf(buf, sizeof buf, " \xC2\xB7 %s \xC2\xB7 cx %d", t, d->cx);
        else snprintf(buf, sizeof buf, " \xC2\xB7 %s", t);
        sv_tooltip(d->name, buf, mp);
        return;
    }
    const char *title = NULL;
    Vector2 at = mp;
    if (ready && (bar = strip_hit(s, &g, mp)) >= 0) { /* at once, as a node's */
        const AnTurn *T = &m->snap->turns[bar];
        char w[64], d[64], tg[128], head[160];
        sv_when(w, sizeof w, T->ts);
        sv_fmt_dur(d, sizeof d, (double)T->secs);
        ui_turn_tags(tg, sizeof tg, turn_tags(m, bar));
        snprintf(head, sizeof head, "%s \xC2\xB7 %s", w, d);
        snprintf(buf, sizeof buf, " \xC2\xB7 +%d \xE2\x88\x92%d%s%s \xC2\xB7 %s", m->turns[bar].add, m->turns[bar].del,
                 *tg ? " \xC2\xB7 " : "", tg, T->title ? T->title : "");
        sv_tooltip(head, buf, mp);
        s->tip.key = NULL;
        return;
    }
    for (int k = 0; !title && k < ST_NMODES; k++)
        if ((title = title_at(mp, g.mode[k], MODE_TITLE[k]))) at = (Vector2){mp.x, g.mode[k].y + 20};
    if (!title) title = title_at(mp, g.turns_lbl, "Bar height: lines changed \xC2\xB7 bar width: time the turn took");
    if (!title)
        title = title_at(mp, g.legend, "Fill: corrected or interrupted, else a fix (the prompt reports a problem) \xC2\xB7 "
                                       "base: the last build/test after a change failed, else none ran \xC2\xB7 top: slow");
    if (!title) title = title_at(mp, g.prev, "Previous turn ([)");
    if (!title) title = title_at(mp, g.next, "Next turn (])");
    if (!title && ready && CheckCollisionPointRec(mp, g.insp) && s->insp.tip) title = s->insp.tip;
    if (title && s->tip.key && !strcmp(title, s->tip.key)) s->tip.t += dt;
    else s->tip.t = 0;
    static char key[600];
    snprintf(key, sizeof key, "%s", title ? title : "");
    s->tip.key = title ? key : NULL;
    if (title && s->tip.t > TIP_DELAY) sv_tooltip(NULL, title, at);
}
