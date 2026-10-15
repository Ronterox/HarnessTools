#include "render.h"

#include <limits.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/stat.h>

#include "model.h"
#include "transcript.h"
#include "raylib.h"
#include "timeline.h"
#include "util.h"

#define BG (Color){13, 17, 23, 255}
#define PANEL (Color){22, 27, 34, 235}
#define DIM (Color){139, 148, 158, 255}
#define TEXT (Color){230, 237, 243, 255}
#define LIVE_RED (Color){255, 85, 85, 255}
#define IDLE_GAP_REAL 0.4  /* replay: longest real-time pause between events */
#define LABEL_SECS 6.0     /* file labels show this long after a touch (sim time) */
#define DIM_SECS 120.0     /* actors dim after this much sim-time idleness */
#define GONE_SECS 600.0    /* ...and leave the stage after this */
#define PANEL_MAX 6        /* actors listed in the bottom-left panel */
#define HUD_TOP 130.0f     /* screen space reserved by the HUD */
#define ROW_H 30.0f
#define TL_H 64.0f         /* timeline strip at the bottom */
#define LEGEND_W 230.0f
#define BACKLOG_SECS 10.0  /* live: events older than this arrive as backlog, no effects */
#define SEEK_THROTTLE 0.08 /* seconds between rebuilds while dragging the timeline */
#define WIN_ROW 22.0f      /* turn window row height */
#define DIM_OTHERS 0.15f   /* alpha of everything outside an open turn */

/* ---- text helpers ------------------------------------------------------- */

static Font g_font;
static bool g_font_ok;

static void load_font(void) {
    static const char *paths[] = {
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        NULL,
    };
    /* ASCII, Latin-1 + Extended-A, punctuation (— “ ” • …), arrows, math, checks: the
     * agents' comments are prose. */
    static const int ranges[][2] = {{32, 126},       {160, 383},       {0x2010, 0x2027}, {0x2190, 0x21FF},
                                    {0x2212, 0x2212}, {0x2260, 0x2265}, {0x25B6, 0x25B6}, {0x2713, 0x2717},
                                    {0xFFFD, 0xFFFD}};
    int cps[600], n = 0;
    for (size_t r = 0; r < sizeof ranges / sizeof *ranges; r++)
        for (int c = ranges[r][0]; c <= ranges[r][1]; c++) cps[n++] = c;
    for (int i = 0; paths[i]; i++) {
        if (!FileExists(paths[i])) continue;
        g_font = LoadFontEx(paths[i], 64, cps, n);
        SetTextureFilter(g_font.texture, TEXTURE_FILTER_BILINEAR);
        g_font_ok = true;
        return;
    }
    g_font = GetFontDefault();
}

/* Monospace for the detail view: code, diffs, command output. */
static Font g_mono;
static bool g_mono_ok;

static void load_mono(void) {
    static const char *paths[] = {
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
        "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
        NULL,
    };
    static const int ranges[][2] = {{32, 126},       {160, 383},       {0x2010, 0x2027}, {0x2190, 0x21FF},
                                    {0x2500, 0x25FF}, {0x2713, 0x2717}, {0xFFFD, 0xFFFD}};
    int cps[1200], n = 0;
    for (size_t r = 0; r < sizeof ranges / sizeof *ranges; r++)
        for (int c = ranges[r][0]; c <= ranges[r][1]; c++) cps[n++] = c;
    for (int i = 0; paths[i]; i++) {
        if (!FileExists(paths[i])) continue;
        SetTraceLogLevel(LOG_ERROR); /* box-drawing glyphs overflow the cell: harmless warnings */
        g_mono = LoadFontEx(paths[i], 32, cps, n);
        SetTraceLogLevel(getenv("AIG_DEBUG") ? LOG_INFO : LOG_WARNING);
        SetTextureFilter(g_mono.texture, TEXTURE_FILTER_BILINEAR);
        g_mono_ok = true;
        return;
    }
    g_mono = g_font;
}

static void text(const char *s, float x, float y, float size, Color c) {
    DrawTextEx(g_font, s, (Vector2){x, y}, size, size * (g_font_ok ? 0.02f : 0.1f), c);
}

static float text_w(const char *s, float size) {
    return MeasureTextEx(g_font, s, size, size * (g_font_ok ? 0.02f : 0.1f)).x;
}

static float dist2d(Vector2 a, Vector2 b) { return hypotf(a.x - b.x, a.y - b.y); }

static Color fade(Color c, float a) {
    c.a = (unsigned char)(fmaxf(0, fminf(1, a)) * c.a);
    return c;
}

/* s cut to fit maxw pixels at size, with "…". */
static void fit(char *out, size_t n, const char *s, float size, float maxw) {
    snprintf(out, n, "%s", s);
    if (text_w(out, size) <= maxw) return;
    size_t len = strlen(out);
    while (len > 0) {
        do len--; while (len > 0 && ((unsigned char)out[len] & 0xC0) == 0x80);
        if (len + 4 > n) continue;
        memcpy(out + len, "\xE2\x80\xA6", 4);
        if (text_w(out, size) <= maxw) return;
    }
}

/* Heat: the hue of the last thing done to the file, brighter the more often it
 * changed. Read-only files stay dim. */
static Color heat_color(const Node *n) {
    Color base = kind_color(n->act_kind);
    float k = n->churn ? 0.45f + 0.55f * fminf(1, log2f(1.0f + (float)n->churn) / log2f(21.0f)) : 0.32f;
    return (Color){(unsigned char)(BG.r + (base.r - BG.r) * k), (unsigned char)(BG.g + (base.g - BG.g) * k),
                   (unsigned char)(BG.b + (base.b - BG.b) * k), 255};
}

static void fmt_count(char *buf, size_t n, long v) {
    char tmp[32];
    snprintf(tmp, sizeof tmp, "%ld", v < 0 ? -v : v);
    size_t len = strlen(tmp), o = 0;
    for (size_t i = 0; i < len && o + 2 < n; i++) {
        if (i && (len - i) % 3 == 0) buf[o++] = ',';
        buf[o++] = tmp[i];
    }
    buf[o] = 0;
}

static void fmt_ago(char *buf, size_t n, double secs) {
    if (secs < 0) secs = 0;
    if (secs < 60) snprintf(buf, n, "%ds ago", (int)secs);
    else if (secs < 3600) snprintf(buf, n, "%dm %02ds ago", (int)secs / 60, (int)secs % 60);
    else if (secs < 86400) snprintf(buf, n, "%dh %02dm ago", (int)secs / 3600, (int)secs % 3600 / 60);
    else snprintf(buf, n, "%dd ago", (int)(secs / 86400));
}

static void fmt_clock(char *buf, size_t n, double ts, const char *fmt) {
    time_t t = (time_t)ts;
    struct tm tm;
    localtime_r(&t, &tm);
    strftime(buf, n, fmt, &tm);
}

static const char *kind_verb(EvKind k) {
    switch (k) {
    case EV_READ: return "Read";
    case EV_SEARCH: return "Search";
    case EV_CREATE: return "Created";
    case EV_EDIT: return "Edit";
    case EV_DELETE: return "Deleted";
    case EV_TOUCH: return "Changed by command";
    default: return "";
    }
}

/* ---- actors ------------------------------------------------------------- */

/* 0 = gone, dimmed when idle, 1 when active. */
static float actor_alpha(const Actor *a, double sim) {
    double idle = sim - a->last_active;
    if (idle > GONE_SECS || idle < 0) return 0;
    return idle > DIM_SECS ? 0.35f : 1;
}

static float panel_height(Scene *m, double sim) {
    int n = 0, idle = 0;
    for (int i = 0; i < m->nactors; i++) {
        if (actor_alpha(&m->actors[i], sim) > 0) n++;
        else idle++;
    }
    int rows = (n < PANEL_MAX ? n : PANEL_MAX) + (idle || n > PANEL_MAX);
    return rows ? ROW_H * (float)rows + 16 : 0;
}

/* ---- comments: an agent's text before a step, as message boxes ---------- */

#define NOTE_FONT 14.0f
#define NOTE_ROW 19.0f
#define NOTE_PAD 8.0f  /* inside a box */
#define NOTE_GAP 8.0f  /* between boxes */
#define NOTE_HEAD 36.0f

typedef struct {
    Notes ns;
    float w; /* text width the rows are wrapped for; 0 = rewrap */
    struct NoteRow { int note, start, len; } *rows;
    int nrows, cap;
} NoteView;

static void nv_clear(NoteView *nv) {
    notes_free(&nv->ns);
    nv->w = 0;
    nv->nrows = 0;
}

/* The comments of step e; markdown emphasis markers dropped, ends trimmed. */
static void nv_load(NoteView *nv, const Event *e) {
    nv_clear(nv);
    nv->ns = tr_notes(e->src, e->note_off, e->notes, e->tool_id);
    for (int i = 0; i < nv->ns.n; i++) {
        char *t = nv->ns.s[i], *o = t;
        for (const char *p = t; *p; p++) {
            if (p[0] == '*' && p[1] == '*') { p++; continue; }
            if (*p != '\r') *o++ = *p;
        }
        while (o > t && (o[-1] == '\n' || o[-1] == ' ')) o--;
        *o = 0;
        size_t lead = strspn(t, "\n");
        memmove(t, t + lead, strlen(t + lead) + 1);
    }
}

static void nv_row(NoteView *nv, int note, int start, int len) {
    if (nv->nrows == nv->cap) {
        nv->cap = nv->cap ? nv->cap * 2 : 64;
        nv->rows = xrealloc(nv->rows, (size_t)nv->cap * sizeof *nv->rows);
    }
    nv->rows[nv->nrows++] = (struct NoteRow){note, start, len};
}

static bool note_fits(const char *s, int len, float w) {
    char buf[1024];
    if (len >= (int)sizeof buf) return false;
    memcpy(buf, s, (size_t)len);
    buf[len] = 0;
    return text_w(buf, NOTE_FONT) <= w;
}

/* Word-wrap every comment at w pixels; words wider than that are cut. */
static void nv_wrap(NoteView *nv, float w) {
    nv->w = w;
    nv->nrows = 0;
    for (int i = 0; i < nv->ns.n; i++) {
        const char *s = nv->ns.s[i];
        for (int k = 0;;) {
            int end = k;
            while (s[end] && s[end] != '\n') end++;
            if (end == k) nv_row(nv, i, k, 0); /* blank line: paragraph break */
            for (int start = k; start < end;) {
                int to = -1;
                for (int q = start; q < end;) {
                    while (q < end && s[q] == ' ') q++;
                    while (q < end && s[q] != ' ' && q - start < 1024) q++; /* note_fits caps there */
                    if (!note_fits(s + start, q - start, w)) break;
                    to = q;
                }
                if (to < 0) { /* one word wider than the box */
                    int c = start;
                    while (c < end) {
                        int nx = c + 1;
                        while (nx < end && ((unsigned char)s[nx] & 0xC0) == 0x80) nx++;
                        if (c > start && !note_fits(s + start, nx - start, w)) break;
                        c = nx;
                    }
                    to = c;
                }
                nv_row(nv, i, start, to - start);
                start = to;
                while (start < end && s[start] == ' ') start++;
            }
            if (!s[end]) break;
            k = end + 1;
        }
    }
}

/* Height of the boxes stacked, once wrapped. */
static float nv_height(const NoteView *nv) {
    return nv->ns.n ? (float)nv->nrows * NOTE_ROW + (float)nv->ns.n * (2 * NOTE_PAD + NOTE_GAP) - NOTE_GAP : 0;
}

/* The boxes from `scroll` pixels down, clipped to r. */
static void nv_draw(const NoteView *nv, Rectangle r, float scroll, Color accent) {
    BeginScissorMode((int)r.x, (int)r.y, (int)r.width, (int)r.height);
    float y = r.y - scroll;
    char buf[1024];
    for (int k = 0; k < nv->nrows;) {
        int note = nv->rows[k].note, nr = 0;
        while (k + nr < nv->nrows && nv->rows[k + nr].note == note) nr++;
        float bh = (float)nr * NOTE_ROW + 2 * NOTE_PAD;
        if (y + bh >= r.y && y <= r.y + r.height) {
            Rectangle box = {r.x, y, r.width, bh};
            DrawRectangleRounded(box, fminf(0.5f, 16 / bh), 6, (Color){32, 39, 50, 255});
            DrawRectangle((int)r.x, (int)(y + 5), 3, (int)(bh - 10), fade(accent, 0.8f));
            for (int j = 0; j < nr; j++) {
                const struct NoteRow *row = &nv->rows[k + j];
                float ry = y + NOTE_PAD + (float)j * NOTE_ROW;
                if (!row->len || ry + NOTE_ROW < r.y || ry > r.y + r.height) continue;
                int len = row->len < (int)sizeof buf - 1 ? row->len : (int)sizeof buf - 1;
                memcpy(buf, nv->ns.s[note] + row->start, (size_t)len);
                buf[len] = 0;
                text(buf, r.x + 14, ry + 1, NOTE_FONT, TEXT);
            }
        }
        y += bh + NOTE_GAP;
        k += nr;
    }
    EndScissorMode();
}

/* A small speech bubble: the step has comments. */
static void bubble_icon(float x, float y, Color c) {
    DrawRectangleRounded((Rectangle){x, y, 13, 9}, 0.5f, 4, c);
    DrawTriangle((Vector2){x + 3, y + 8}, (Vector2){x + 3, y + 12}, (Vector2){x + 7, y + 8}, c);
}

/* ---- viewer state ------------------------------------------------------- */

typedef enum { HOVER_NONE, HOVER_NODE, HOVER_ACTOR } HoverKind;

typedef struct {
    const VizOpts *o;
    Scene *m;
    History h;
    double sim, speed;
    bool live, paused, auto_cam, legend;
    Camera2D cam;
    HoverKind hover;
    int hover_i;
    int drag; /* 0 none, 1 pan, 2 scrub */
    double scrub_t, last_seek;
    /* Turn window: one actor's one turn, its files highlighted. */
    bool turn_open, turn_stick;
    char *turn_actor;
    int turn_no;
    float turn_scroll;
    size_t *steps, nsteps, capsteps;
    bool *hl, *hl_dir;
    int hl_cap, hl_count; /* files touched by the open turn */
    Rectangle win, win_close;
    int win_first, win_vis;
    bool hover_from_win;
    Vector2 press_at;
    bool press_outside;
    /* Detail window: one step in full. Holds the step's identity, not its
     * history index: late events get sorted in and shift indices. */
    Detail *det;
    EvKind det_kind;
    double det_ts, det_reload;
    long det_size; /* transcript size when loaded: reload only after it grows */
    bool det_from_turn; /* opened from a turn step (closes when the turn moves on) */
    char *press_file;   /* file node under a press: a click (not a pan) opens its last change */
    char *det_label, *det_id, *det_path;
    const char *det_src;
    long det_off;
    int det_add, det_del;
    float det_scroll;
    Rectangle det_win, det_close;
    struct DetRow { int line, start, len; } *det_rows; /* wrapped */
    int det_nrows, det_caprows, det_cols, det_vis;
    /* Comments panel beside the detail: what the agent wrote before the step.
     * The hover preview shows a turn step's comments before it is opened. */
    NoteView det_nv, hov_nv;
    char *det_actor;
    long det_note_off; /* for Ctrl+C: the comments as written, re-read */
    int det_notes;
    float note_scroll;
    Rectangle note_win;
    const char *hov_src; /* identity of hov_nv's notes */
    long hov_off;
    int hov_n;
    char *hov_id;
    /* Actor panel rows, for clicks. */
    Rectangle row_rect[PANEL_MAX];
    int row_actor[PANEL_MAX], nrows;
    /* Timeline density cache. */
    float *bin_w, *bin_r;
    int nbins;
    size_t bins_n;
    double bins_t;
} Viz;

static void apply_until(Viz *v, double t, bool effects) {
    while (v->h.applied < v->h.n && v->h.ev[v->h.applied].ts <= t) {
        const Event *e = &v->h.ev[v->h.applied++];
        /* A session picked up mid-run delivers its history at once: apply it quietly. */
        model_apply(v->m, e, effects && (!v->live || e->ts > t - BACKLOG_SECS));
    }
}

/* Jump the scene to time t: forward = apply, backward = rebuild keeping layout. */
static void seek(Viz *v, double t) {
    double now = now_wall();
    if (v->h.n && t < v->h.ev[0].ts - 1) t = v->h.ev[0].ts - 1;
    v->live = t >= now - 1;
    if (v->live) t = now;
    if (t >= v->sim) {
        apply_until(v, t, false);
    } else {
        Scene *nm = model_new(v->o->root);
        size_t k = hist_upper(&v->h, t);
        for (size_t i = 0; i < k; i++) model_apply(nm, &v->h.ev[i], false);
        model_adopt_layout(nm, v->m);
        model_free(v->m);
        v->m = nm;
        v->h.applied = k;
        v->hover = HOVER_NONE;
        v->nrows = 0; /* panel rows index the old scene's actors */
    }
    v->sim = t;
}

/* A failed-call marker: flag its command's BASH event (in this batch or the
 * history). The marker itself goes nowhere. */
static void mark_failed(Viz *v, Event *in, size_t n, const Event *f) {
    for (size_t i = n; i-- > 0;)
        if (in[i].kind == EV_BASH && in[i].tool_id && f->tool_id && !strcmp(in[i].tool_id, f->tool_id)) {
            in[i].failed = 1;
            return;
        }
    for (size_t i = v->h.n; i-- > 0;) {
        Event *e = &v->h.ev[i];
        if (e->kind == EV_BASH && e->tool_id && f->tool_id && !strcmp(e->tool_id, f->tool_id)) {
            e->failed = 1;
            return;
        }
    }
}

static void ingest_new_events(Viz *v, EvQueue *q) {
    Event *in;
    size_t n = evq_drain(q, &in);
    if (!n) { free(in); return; }
    qsort(in, n, sizeof *in, ev_cmp);
    size_t kept = 0;
    for (size_t i = 0; i < n; i++) {
        if (in[i].kind == EV_FAILED) {
            mark_failed(v, in, kept, &in[i]);
            ev_free(&in[i]);
        } else {
            in[kept++] = in[i];
        }
    }
    n = kept;
    if (!n) { free(in); return; }
    size_t k = 0; /* events already in the past get applied now */
    while (k < n && in[k].ts <= v->sim) {
        model_apply(v->m, &in[k], v->live && in[k].ts > v->sim - BACKLOG_SECS);
        k++;
    }
    hist_add(&v->h, in, n, k);
    free(in);
}

/* ---- turn window: data ------------------------------------------------- */

static int actor_index(Scene *m, const char *id) {
    for (int i = 0; i < m->nactors; i++)
        if (!strcmp(m->actors[i].id, id)) return i;
    return -1;
}

static void detail_close(Viz *v);

static void turn_open(Viz *v, int actor) {
    if (actor < 0 || actor >= v->m->nactors) return;
    if (!v->turn_actor || strcmp(v->turn_actor, v->m->actors[actor].id) || v->turn_no != v->m->actors[actor].turn)
        if (v->det_from_turn) detail_close(v); /* another turn: its step no longer belongs */
    free(v->turn_actor);
    v->turn_actor = xstrdup(v->m->actors[actor].id);
    v->turn_no = v->m->actors[actor].turn;
    v->turn_open = true;
    v->turn_stick = true; /* follow new steps while scrolled to the bottom */
    v->turn_scroll = 0;
}

static void turn_close(Viz *v) {
    detail_close(v);
    v->turn_open = false;
    v->hover = HOVER_NONE;
}

static bool is_step(EvKind k) { return k != EV_TURN; }
static bool touches_file(EvKind k) {
    return k == EV_READ || k == EV_CREATE || k == EV_EDIT || k == EV_DELETE || k == EV_TOUCH;
}

/* Steps of the open turn that already happened (applied), and the files they
 * touched marked in hl[] (dirs holding them in hl_dir[]). Recomputed per frame:
 * a seek rebuilds the scene and renumbers nodes. */
static void turn_update(Viz *v) {
    v->nsteps = 0;
    v->hl_count = 0;
    if (!v->turn_open) return;
    Scene *m = v->m;
    /* Follow the actor: its turn at the timeline position (live or after a seek). */
    int ai = actor_index(m, v->turn_actor);
    if (ai < 0 && v->det_from_turn) detail_close(v); /* sought back before the actor: its step is ahead */
    if (ai >= 0 && m->actors[ai].turn != v->turn_no) {
        v->turn_no = m->actors[ai].turn;
        v->turn_scroll = 0;
        v->turn_stick = true;
        if (v->det_from_turn) detail_close(v); /* its step belongs to the old turn */
    }
    if (m->nnodes > v->hl_cap) {
        v->hl_cap = m->nnodes * 2;
        v->hl = xrealloc(v->hl, (size_t)v->hl_cap * sizeof *v->hl);
        v->hl_dir = xrealloc(v->hl_dir, (size_t)v->hl_cap * sizeof *v->hl_dir);
    }
    memset(v->hl, 0, (size_t)m->nnodes * sizeof *v->hl);
    memset(v->hl_dir, 0, (size_t)m->nnodes * sizeof *v->hl_dir);
    for (size_t i = 0; i < v->h.applied; i++) {
        const Event *e = &v->h.ev[i];
        if (e->turn != v->turn_no || !is_step(e->kind) || strcmp(e->actor, v->turn_actor)) continue;
        if (v->nsteps == v->capsteps) {
            v->capsteps = v->capsteps ? v->capsteps * 2 : 64;
            v->steps = xrealloc(v->steps, v->capsteps * sizeof *v->steps);
        }
        v->steps[v->nsteps++] = i;
        if (!touches_file(e->kind) || !e->path) continue;
        int ni = model_find(m, false, e->path);
        if (ni < 0) continue;
        v->hl_count += !v->hl[ni];
        v->hl[ni] = true;
        for (int d = m->nodes[ni].parent; d >= 0; d = m->nodes[d].parent) v->hl_dir[d] = true;
    }
}

/* Alpha multiplier for a node while a turn is open: its files stand out. */
static float hl_alpha(Viz *v, int i) {
    if (!v->turn_open || !v->hl_count) return 1; /* nothing touched: nothing to single out */
    return (v->m->nodes[i].is_dir ? v->hl_dir[i] : v->hl[i]) ? 1 : DIM_OTHERS;
}

/* ---- drawing: scene ----------------------------------------------------- */

/* Soft area around the highlighted files of each folder, in the actor's color. */
static void draw_turn_areas(Viz *v, float px) {
    Scene *m = v->m;
    int ai = actor_index(m, v->turn_actor);
    Color c = ai >= 0 ? m->actors[ai].color : TEXT;
    for (int d = 0; d < m->nnodes; d++) {
        Node *dir = &m->nodes[d];
        if (!dir->is_dir || !v->hl_dir[d]) continue;
        Vector2 cen = {0, 0};
        int n = 0;
        for (int k = 0; k < dir->nkids; k++)
            if (v->hl[dir->kids[k]]) { cen.x += m->nodes[dir->kids[k]].pos.x; cen.y += m->nodes[dir->kids[k]].pos.y; n++; }
        if (!n) continue;
        cen.x /= (float)n;
        cen.y /= (float)n;
        float r = 0;
        for (int k = 0; k < dir->nkids; k++) {
            Node *f = &m->nodes[dir->kids[k]];
            if (v->hl[dir->kids[k]]) r = fmaxf(r, dist2d(cen, f->pos) + f->radius);
        }
        r += 12 * px;
        DrawCircleV(cen, r + 10 * px, fade(c, 0.05f));
        DrawCircleV(cen, r, fade(c, 0.12f));
        DrawRing(cen, r, r + 2 * px, 0, 360, 64, fade(c, 0.5f));
    }
}

static void draw_scene(Viz *v) {
    Scene *m = v->m;
    float px = 1.0f / v->cam.zoom; /* one screen pixel in world units */
    Node *N = m->nodes;

    for (int i = 1; i < m->nnodes; i++) { /* tree edges */
        Node *n = &N[i];
        if (n->alpha <= 0) continue;
        Node *p = &N[n->parent];
        float a = n->alpha * hl_alpha(v, i);
        if (n->is_dir) DrawLineEx(p->pos, n->pos, 1.5f * px, fade((Color){90, 100, 120, 255}, 0.6f * a));
        else DrawLineEx(p->pos, n->pos, 1.0f * px, fade((Color){90, 100, 120, 255}, 0.25f * a));
    }
    if (v->turn_open) draw_turn_areas(v, px);

    BeginBlendMode(BLEND_ADDITIVE);
    for (int k = 0; k < m->fx_count; k++) { /* pulses under the nodes */
        Fx *f = &m->fx[(m->fx_head + k) % MAX_FX];
        if (f->kind != FX_PULSE || f->age >= f->life) continue;
        Node *n = &N[f->node];
        float t = f->age / f->life;
        float r = n->radius + (12 + 90 * f->strength) * t;
        DrawRing(n->pos, r - (2 + 6 * f->strength) * px * 2, r, 0, 360, 48,
                 fade(f->color, (1 - t) * (0.35f + 0.65f * f->strength) * hl_alpha(v, f->node)));
    }
    EndBlendMode();

    for (int i = 0; i < m->nnodes; i++) {
        Node *n = &N[i];
        if (n->alpha <= 0) continue;
        float a = n->alpha * hl_alpha(v, i);
        if (n->is_dir) {
            DrawCircleV(n->pos, 3.5f * px + 2, fade(n->color, 0.8f * a));
            continue;
        }
        Color hc = heat_color(n);
        if (n->flash > 0) {
            BeginBlendMode(BLEND_ADDITIVE);
            DrawCircleV(n->pos, n->radius * (1.6f + n->flash), fade(kind_color(n->act_kind), 0.35f * n->flash * a));
            EndBlendMode();
        }
        Color c = n->deleted ? LIVE_RED : ColorBrightness(hc, 0.5f * n->flash);
        DrawCircleV(n->pos, n->radius, fade(c, a));
    }

    BeginBlendMode(BLEND_ADDITIVE);
    for (int k = 0; k < m->fx_count; k++) { /* beams over the nodes, colored by action */
        Fx *f = &m->fx[(m->fx_head + k) % MAX_FX];
        if (f->kind != FX_BEAM || f->age >= f->life) continue;
        Actor *a = &m->actors[f->actor];
        if (actor_alpha(a, v->sim) <= 0) continue;
        float t = f->age / f->life;
        float w = (1.0f + 9.0f * f->strength) * px;
        float dim = v->turn_open && strcmp(a->id, v->turn_actor) ? DIM_OTHERS : 1;
        DrawLineEx(a->pos, N[f->node].pos, w, fade(f->color, (1 - t) * 0.9f * dim));
    }
    EndBlendMode();

    for (int i = 0; i < m->nactors; i++) {
        Actor *a = &m->actors[i];
        float al = actor_alpha(a, v->sim);
        if (v->turn_open && strcmp(a->id, v->turn_actor)) al *= DIM_OTHERS;
        if (al <= 0) continue;
        DrawCircleV(a->pos, 9 * px + 3, fade(a->color, al));
        DrawCircleV(a->pos, 5 * px + 1, fade(BG, al));
    }

    /* Hover highlight. */
    if (v->hover == HOVER_NODE) {
        Node *n = &N[v->hover_i];
        float r = (n->is_dir ? 3.5f * px + 2 : n->radius) + 4 * px;
        DrawRing(n->pos, r, r + 2 * px, 0, 360, 48, TEXT);
    } else if (v->hover == HOVER_ACTOR) {
        Actor *a = &m->actors[v->hover_i];
        DrawRing(a->pos, 9 * px + 6, 9 * px + 6 + 2 * px, 0, 360, 48, TEXT);
    }
}

static void draw_labels(Viz *v) {
    Scene *m = v->m;
    for (int i = 0; i < m->nnodes; i++) {
        Node *n = &m->nodes[i];
        if (n->alpha <= 0) continue;
        Vector2 s = GetWorldToScreen2D(n->pos, v->cam);
        if (s.x < -200 || s.y < -50 || s.x > GetScreenWidth() + 200 || s.y > GetScreenHeight() + 50) continue;
        if (n->is_dir) {
            text(n->name, s.x + 6, s.y - 18, 15, fade(DIM, 0.85f * n->alpha * hl_alpha(v, i)));
        } else if (v->turn_open && v->hl[i]) { /* the open turn's files stay labeled */
            text(n->name, s.x + n->radius * v->cam.zoom + 4, s.y - 8, 15, fade(TEXT, n->alpha));
        } else {
            double age = v->sim - n->last_touch;
            if (age < LABEL_SECS && age >= 0 && !v->turn_open) {
                float a = (float)(1 - age / LABEL_SECS);
                text(n->name, s.x + n->radius * v->cam.zoom + 4, s.y - 8, 15, fade(TEXT, a * n->alpha));
            }
        }
    }
    for (int i = 0; i < m->nactors; i++) {
        Actor *a = &m->actors[i];
        Vector2 s = GetWorldToScreen2D(a->pos, v->cam);
        float al = actor_alpha(a, v->sim);
        if (al < 1) continue; /* idle actors stay unlabeled */
        if (v->turn_open && strcmp(a->id, v->turn_actor)) continue;
        text(a->name, s.x + 14, s.y - 9, 16, fade(a->color, al));
        if (a->bubble && a->bubble_t > 0) {
            float ba = fminf(1, a->bubble_t);
            float w = text_w(a->bubble, 14);
            DrawRectangleRounded((Rectangle){s.x + 12, s.y + 10, w + 12, 22}, 0.4f, 6,
                                 fade((Color){30, 36, 46, 255}, 0.9f * ba));
            text(a->bubble, s.x + 18, s.y + 13, 14, fade(TEXT, ba));
        }
    }
}

/* ---- drawing: HUD, legend, panel --------------------------------------- */

static void draw_counters(Viz *v) {
    Scene *m = v->m;
    char add[32], del[32], line[256];
    fmt_count(add, sizeof add, m->total_add);
    fmt_count(del, sizeof del, m->total_del);
    snprintf(line, sizeof line, "+%s", add);
    text(line, 24, 18, 56, kind_color(EV_CREATE));
    float x = 24 + text_w(line, 56) + 24;
    snprintf(line, sizeof line, "\xE2\x88\x92%s", del);
    text(line, x, 18, 56, kind_color(EV_DELETE));
    text("lines written / removed", 26, 78, 16, DIM);
    snprintf(line, sizeof line, "%d created   %d edits   %d deleted   %d reads   %d searches   %d commands",
             m->files_created, m->files_edited, m->files_deleted, m->reads, m->searches, m->commands);
    text(line, 26, 100, 16, DIM);

    int W = GetScreenWidth();
    char clock[64];
    fmt_clock(clock, sizeof clock, v->sim, "%Y-%m-%d %H:%M:%S");
    text(clock, (float)W - text_w(clock, 22) - 24, 20, 22, TEXT);
    if (v->paused) snprintf(line, sizeof line, "\xE2\x80\x96 PAUSED");
    else if (v->live) snprintf(line, sizeof line, "LIVE");
    else snprintf(line, sizeof line, "\xE2\x96\xB6 REPLAY  x%g", v->speed);
    Color mc = v->live && !v->paused ? LIVE_RED : DIM;
    text(line, (float)W - text_w(line, 18) - 24, 48, 18, mc);
    if (v->live && !v->paused) DrawCircle(W - (int)text_w(line, 18) - 36, 58, 5, mc);
}

static void swatch(float x, float y, Color c) { DrawCircle((int)x + 6, (int)y + 8, 5, c); }

#define LEGEND_Y 84
#define LEGEND_H (22 + 6 * 20 + 26 + 2 * 20 + 26 + 20 + 20 + 26 + 5 * 17 + 10)

/* Shown, and room for it above the panel. The turn window replaces it. */
static bool legend_shown(Viz *v) {
    return v->legend && !v->turn_open && !v->det &&
           LEGEND_Y + LEGEND_H <= (float)GetScreenHeight() - TL_H - panel_height(v->m, v->sim);
}

static void draw_legend(Viz *v) {
    int W = GetScreenWidth();
    float x = (float)W - LEGEND_W - 16, y = LEGEND_Y;
    if (v->turn_open || v->det) return; /* the open window explains the screen; give the graph room */
    if (!legend_shown(v)) {
        text("H  legend", (float)W - text_w("H  legend", 14) - 24, y, 14, fade(DIM, 0.7f));
        return;
    }
    float h = LEGEND_H;
    DrawRectangleRounded((Rectangle){x, y, LEGEND_W, h}, 0.04f, 6, PANEL);
    float cx = x + 12, cy = y + 8;

    text("ACTIONS  (beam / file color)", cx, cy, 13, DIM);
    cy += 22;
    static const EvKind kinds[] = {EV_READ, EV_SEARCH, EV_CREATE, EV_EDIT, EV_DELETE, EV_TOUCH};
    static const char *names[] = {"read", "search", "create", "edit", "delete", "changed by a command"};
    for (int i = 0; i < 6; i++, cy += 20) {
        swatch(cx, cy, kind_color(kinds[i]));
        text(names[i], cx + 20, cy, 15, TEXT);
    }
    cy += 6;
    text("RINGS  (size = lines)", cx, cy, 13, DIM);
    cy += 20;
    DrawRing((Vector2){cx + 6, cy + 8}, 3, 6, 0, 360, 24, kind_color(EV_CREATE));
    text("lines added", cx + 20, cy, 15, TEXT);
    cy += 20;
    DrawRing((Vector2){cx + 6, cy + 8}, 3, 6, 0, 360, 24, kind_color(EV_DELETE));
    text("lines removed", cx + 20, cy, 15, TEXT);
    cy += 26;
    text("FILES  (color = last action)", cx, cy, 13, DIM);
    cy += 20;
    /* Brightness scale: an edited file at 1, 5 and 20 changes. */
    Node sample = {.act_kind = EV_EDIT};
    static const int churns[] = {1, 5, 20};
    static const char *cl[] = {"1\xC3\x97", "5\xC3\x97", "20\xC3\x97 changed"};
    float sx = cx;
    for (int i = 0; i < 3; i++) {
        sample.churn = churns[i];
        swatch(sx, cy, heat_color(&sample));
        text(cl[i], sx + 16, cy, 14, TEXT);
        sx += 16 + text_w(cl[i], 14) + 12;
    }
    cy += 20;
    sample.churn = 0;
    sample.act_kind = EV_READ;
    swatch(cx, cy, heat_color(&sample));
    text("only read", cx + 20, cy, 15, TEXT);
    cy += 26;
    text("KEYS", cx, cy, 13, DIM);
    cy += 18;
    static const char *keys[] = {
        "click actor: turn    file: last change",
        "space pause    \xE2\x86\x90/\xE2\x86\x92 seek",
        "+/- speed    L live    H legend",
        "drag pan    wheel zoom    C camera",
        "F fullscreen    Esc close    Q quit",
    };
    for (int i = 0; i < 5; i++, cy += 17) text(keys[i], cx, cy, 13, fade(TEXT, 0.8f));
}

static void draw_panel(Viz *v) {
    Scene *m = v->m;
    int H = GetScreenHeight();
    char line[256];
    int order[64], n = 0, idle = 0;
    for (int i = 0; i < m->nactors; i++) {
        if (actor_alpha(&m->actors[i], v->sim) <= 0) { idle++; continue; }
        if (n == 64) { idle++; continue; }
        int j = n++;
        while (j > 0 && m->actors[order[j - 1]].last_active < m->actors[i].last_active) {
            order[j] = order[j - 1];
            j--;
        }
        order[j] = i;
    }
    int shown = n < PANEL_MAX ? n : PANEL_MAX;
    idle += n - shown;
    float y = (float)H - TL_H - panel_height(m, v->sim) + 4;
    v->nrows = shown;
    for (int k = 0; k < shown; k++, y += ROW_H) {
        Actor *a = &m->actors[order[k]];
        v->row_rect[k] = (Rectangle){16, y - 4, 600, ROW_H};
        v->row_actor[k] = order[k];
        float al = actor_alpha(a, v->sim);
        DrawCircle(30, (int)y + 9, 6, fade(a->color, al));
        char sa[32], sd[32];
        fmt_count(sa, sizeof sa, a->add);
        fmt_count(sd, sizeof sd, a->del);
        bool sel = v->turn_open && !strcmp(a->id, v->turn_actor);
        text(a->name, 44, y, 17, fade(sel ? a->color : TEXT, al));
        float nx = 44 + text_w(a->name, 17) + 14;
        snprintf(line, sizeof line, "+%s", sa);
        text(line, nx, y, 17, fade(kind_color(EV_CREATE), al));
        nx += text_w(line, 17) + 8;
        snprintf(line, sizeof line, "\xE2\x88\x92%s", sd);
        text(line, nx, y, 17, fade(kind_color(EV_DELETE), al));
        nx += text_w(line, 17) + 14;
        if (a->prompt) {
            snprintf(line, sizeof line, "turn %d: %s", a->turn, a->prompt);
            text(line, nx, y + 1, 15, fade(DIM, al));
        }
    }
    if (idle) {
        snprintf(line, sizeof line, "+%d idle", idle);
        text(line, 44, y, 15, fade(DIM, 0.7f));
    }
}

/* ---- turn window: drawing ----------------------------------------------- */

/* One line describing a step, as the turn window lists it. */
static void step_label(char *line, size_t n, const Event *e) {
    if (e->kind == EV_BASH) snprintf(line, n, "$ %s", e->text ? e->text : "");
    else if (e->kind == EV_TOOL) snprintf(line, n, "%s", e->text ? e->text : "");
    else if (e->kind == EV_SEARCH)
        snprintf(line, n, "Search  %s%s%s", e->text ? e->text : "", e->path && *e->path ? "  in " : "",
                 e->path ? e->path : "");
    else snprintf(line, n, "%s  %s%s", kind_verb(e->kind), e->path ? e->path : "",
                  e->via == VIA_GUESS ? "  \xC2\xB7  guessed"
                  : e->via == VIA_WATCH && e->src ? "  \xC2\xB7  seen by --watch" : "");
}

/* A failed command with nothing to show for it: no file it touched is known
 * (guessed or seen by the watcher). Shown in red. */
static bool failed_bare(Viz *v, const Event *e) {
    if (e->kind != EV_BASH || !e->failed) return false;
    for (size_t k = 0; k < v->nsteps; k++) {
        const Event *o = &v->h.ev[v->steps[k]];
        if (o != e && o->path && o->tool_id && !strcmp(o->tool_id, e->tool_id)) return false;
    }
    return true;
}

static Color step_color(EvKind k) { return k == EV_BASH ? DIM : kind_color(k); }

/* ---- detail window ------------------------------------------------------ */

static void detail_close(Viz *v) {
    detail_free(v->det);
    v->det = NULL;
    free(v->det_label);
    free(v->det_id);
    free(v->det_path);
    free(v->det_actor);
    v->det_label = v->det_id = v->det_path = v->det_actor = NULL;
    v->det_nrows = 0;
    nv_clear(&v->det_nv);
    v->note_win = (Rectangle){0};
}

static void detail_load(Viz *v) {
    Detail *d;
    if (v->det_src) {
        d = tr_detail(v->det_src, v->det_off, v->det_id, v->det_path);
    } else { /* seen by the watcher: nothing recorded what changed */
        d = detail_new("you");
        char buf[160];
        snprintf(buf, sizeof buf, "%s\nchanged outside the agents (seen by --watch): +%d \xE2\x88\x92%d lines",
                 v->det_path ? v->det_path : "", v->det_add, v->det_del);
        detail_line(d, DL_DIM, buf);
        detail_line(d, DL_DIM, "nothing recorded its content");
    }
    detail_free(v->det);
    v->det = d;
    v->det_cols = 0; /* rewrap */
    v->det_reload = GetTime();
    struct stat st;
    v->det_size = v->det_src && !stat(v->det_src, &st) ? (long)st.st_size : -1;
}

static bool is_detail_of(Viz *v, const Event *e) {
    if (!v->det) return false;
    if (e->tool_id || v->det_id)
        return e->tool_id && v->det_id && !strcmp(e->tool_id, v->det_id) && e->kind == v->det_kind &&
               !strcmp(e->path ? e->path : "", v->det_path ? v->det_path : "");
    return e->ts == v->det_ts && e->kind == v->det_kind && !strcmp(e->path ? e->path : "", v->det_path ? v->det_path : "");
}

static void detail_open(Viz *v, const Event *e, bool from_turn) {
    if (is_detail_of(v, e)) { detail_close(v); return; } /* click again: close */
    detail_close(v);
    v->det_from_turn = from_turn;
    char line[512];
    step_label(line, sizeof line, e);
    v->det_label = xstrdup(line);
    v->det_kind = e->kind;
    v->det_ts = e->ts;
    v->det_src = e->src;
    v->det_off = e->src_off;
    v->det_id = e->tool_id ? xstrdup(e->tool_id) : NULL;
    v->det_path = e->path ? xstrdup(e->path) : NULL;
    v->det_add = e->add;
    v->det_del = e->del;
    v->det_scroll = 0;
    v->det_actor = xstrdup(e->actor);
    v->det_note_off = e->note_off;
    v->det_notes = e->notes;
    nv_load(&v->det_nv, e); /* written before the call: never changes */
    v->note_scroll = 0;
    detail_load(v);
}

/* A file clicked in the graph: its last change up to the timeline position
 * (create, edit, delete, changed by a command), or its last read if never
 * changed. */
static bool file_detail(Viz *v, const char *path) {
    const Event *last_read = NULL;
    for (size_t i = v->h.applied; i-- > 0;) {
        const Event *e = &v->h.ev[i];
        if (!e->path || strcmp(e->path, path)) continue;
        if (e->kind == EV_CREATE || e->kind == EV_EDIT || e->kind == EV_DELETE || e->kind == EV_TOUCH) {
            detail_open(v, e, false);
            return true;
        }
        if (e->kind == EV_READ && !last_read) last_read = e;
    }
    if (last_read) detail_open(v, last_read, false);
    return last_read != NULL;
}

/* Wrap every line at `cols` characters (UTF-8 aware). */
static void detail_wrap(Viz *v, int cols) {
    v->det_cols = cols;
    v->det_nrows = 0;
    for (int i = 0; v->det && i < v->det->n; i++) {
        const char *s = v->det->lines[i].s;
        int start = 0, chars = 0, k = 0;
        for (;; k++) {
            bool end = !s[k];
            if (!end && ((unsigned char)s[k] & 0xC0) == 0x80) continue; /* continuation byte */
            if (end || chars == cols) {
                if (v->det_nrows == v->det_caprows) {
                    v->det_caprows = v->det_caprows ? v->det_caprows * 2 : 256;
                    v->det_rows = xrealloc(v->det_rows, (size_t)v->det_caprows * sizeof *v->det_rows);
                }
                v->det_rows[v->det_nrows++] = (struct DetRow){i, start, k - start};
                if (end) break;
                start = k;
                chars = 0;
            }
            chars++;
        }
    }
}

static void detail_copy(Viz *v) {
    if (!v->det) return;
    size_t n = strlen(v->det_label) + 2;
    for (int i = 0; i < v->det->n; i++) n += strlen(v->det->lines[i].s) + 3;
    char *buf = xmalloc(n), *p = buf;
    p += sprintf(p, "%s\n", v->det_label);
    for (int i = 0; i < v->det->n; i++) {
        DlKind k = v->det->lines[i].kind;
        p += sprintf(p, "%s%s\n", k == DL_ADD ? "+" : k == DL_DEL ? "-" : "", v->det->lines[i].s);
    }
    SetClipboardText(buf);
    free(buf);
}

#define DET_FONT 14.0f
#define DET_ROW 18.0f
#define DET_ROW_BYTES 2048
#define DET_TOP 58.0f /* header height */

static float note_panel_w(void) { return fminf(400, fmaxf(280, (float)GetScreenWidth() * 0.24f)); }

/* Height of a comments panel w wide: its boxes, up to maxh. */
static float note_panel_h(NoteView *nv, float w, float maxh) {
    if (nv->w != w - 44) nv_wrap(nv, w - 44);
    return fminf(maxh, NOTE_HEAD + nv_height(nv) + 10);
}

static Color actor_color_by_id(Viz *v, const char *id) {
    int ai = id ? actor_index(v->m, id) : -1;
    return ai >= 0 ? v->m->actors[ai].color : TEXT;
}

/* The comments panel at r (height from note_panel_h). A preview doesn't scroll:
 * what doesn't fit fades out. */
static void draw_note_panel(NoteView *nv, Rectangle r, float *scroll, Color ac, bool preview) {
    Color bg = {16, 20, 26, 250};
    float round = 10 / fminf(r.width, r.height);
    DrawRectangleRounded(r, round, 6, bg);
    DrawRectangleRoundedLinesEx(r, round, 6, 1.5f, fade(ac, preview ? 0.35f : 0.55f));
    bubble_icon(r.x + 14, r.y + 14, ac);
    char title[64];
    snprintf(title, sizeof title, nv->ns.n == 1 ? "Comment" : "Comments  \xC2\xB7  %d", nv->ns.n);
    text(title, r.x + 34, r.y + 10, 15, ac);
    Rectangle body = {r.x + 10, r.y + NOTE_HEAD, r.width - 20, r.height - NOTE_HEAD - 10};
    float max_scroll = fmaxf(0, nv_height(nv) - body.height);
    const char *hint = preview ? "before this step  \xC2\xB7  click to open"
                     : max_scroll > 0 ? "wheel scroll  \xC2\xB7  Ctrl+C copy" : "Ctrl+C copy";
    char fitbuf[128];
    fit(fitbuf, sizeof fitbuf, hint, 12, r.width - 44 - text_w(title, 15) - 20);
    text(fitbuf, r.x + r.width - 12 - text_w(fitbuf, 12), r.y + 13, 12, DIM);
    DrawLine((int)r.x + 10, (int)(r.y + NOTE_HEAD - 6), (int)(r.x + r.width - 10), (int)(r.y + NOTE_HEAD - 6), fade(DIM, 0.3f));
    if (*scroll > max_scroll) *scroll = max_scroll;
    if (*scroll < 0) *scroll = 0;
    if (body.height <= 0) return; /* window too small for any of it */
    nv_draw(nv, body, *scroll, ac);
    if (max_scroll <= 0) return;
    if (preview) {
        DrawRectangleGradientV((int)body.x, (int)(body.y + body.height - 28), (int)body.width, 28, fade(bg, 0), bg);
        return;
    }
    float bh = fmaxf(20, body.height * body.height / nv_height(nv));
    float by = body.y + (body.height - bh) * (*scroll / max_scroll);
    DrawRectangleRounded((Rectangle){r.x + r.width - 7, by, 3, bh}, 1, 4, fade(DIM, 0.6f));
}

static void draw_detail(Viz *v, Vector2 mouse) {
    v->note_win = (Rectangle){0};
    if (!v->det) { v->det_win = (Rectangle){0}; return; }
    Detail *d = v->det;
    int W = GetScreenWidth(), H = GetScreenHeight();
    float x = v->win.width > 0 ? v->win.x + v->win.width + 10 : 16;
    if (W - x < 420) x = 16; /* too narrow beside it: over it */
    float y = HUD_TOP, w = (float)W - x - 16;
    float h = (float)H - TL_H - panel_height(v->m, v->sim) - 8 - y;
    if (h < 200) h = (float)H - TL_H - 8 - y;
    /* The agent's comments first (it wrote them first), the step beside them;
     * above it when there's no room. */
    if (v->det_nv.ns.n) {
        Color ac = actor_color_by_id(v, v->det_actor);
        float nw = note_panel_w();
        if (w - nw - 10 >= 420) {
            v->note_win = (Rectangle){x, y, nw, note_panel_h(&v->det_nv, nw, h)};
            x += nw + 10;
            w -= nw + 10;
        } else {
            v->note_win = (Rectangle){x, y, w, note_panel_h(&v->det_nv, w, h * 0.4f)};
            y += v->note_win.height + 8;
            h -= v->note_win.height + 8;
        }
        draw_note_panel(&v->det_nv, v->note_win, &v->note_scroll, ac, false);
    }
    if (v->win.width <= 0) { /* alone: leave the graph beside it (the comments count) */
        float beside = v->note_win.width > 0 && v->note_win.y == y ? v->note_win.width + 10 : 0;
        w = fminf(w, fmaxf(420, fmaxf(560, (float)W * 0.5f) - beside));
    }
    v->det_win = (Rectangle){x, y, w, h};
    Color kc = step_color(v->det_kind);
    DrawRectangleRounded(v->det_win, 0.015f, 6, (Color){16, 20, 26, 250});
    DrawRectangleRoundedLinesEx(v->det_win, 0.015f, 6, 1.5f, fade(kc, 0.55f));

    /* Header: what it was, when, and close. */
    char line[512], fitbuf[512], tm[16];
    DrawCircle((int)x + 18, (int)y + 20, 5, kc);
    fmt_clock(tm, sizeof tm, v->det_ts, "%H:%M:%S");
    float tw = text_w(tm, 13);
    v->det_close = (Rectangle){x + w - 34, y + 6, 26, 26};
    fit(fitbuf, sizeof fitbuf, v->det_label, 16, w - 70 - tw - 40);
    text(fitbuf, x + 32, y + 11, 16, TEXT);
    text(tm, x + w - 44 - tw, y + 13, 13, DIM);
    text("\xE2\x9C\x95", v->det_close.x + 6, v->det_close.y + 3, 18, CheckCollisionPointRec(mouse, v->det_close) ? TEXT : DIM);
    snprintf(line, sizeof line, "%s%s  \xC2\xB7  %d lines  \xC2\xB7  wheel/PgUp/PgDn scroll  \xC2\xB7  Ctrl+C copy  \xC2\xB7  Esc close",
             d->tool, d->state == 1 ? "  \xC2\xB7  running\xE2\x80\xA6 (no result yet)" : "", d->n);
    fit(fitbuf, sizeof fitbuf, line, 13, w - 46);
    text(fitbuf, x + 32, y + 34, 13, d->state == 1 ? kind_color(EV_EDIT) : DIM);
    DrawLine((int)x + 10, (int)(y + DET_TOP - 4), (int)(x + w - 10), (int)(y + DET_TOP - 4), fade(DIM, 0.3f));

    /* Body: gutter with line numbers when the content has them, wrapped rows. */
    float cw = MeasureTextEx(g_mono, "M", DET_FONT, 0).x;
    bool numbered = false;
    for (int i = 0; i < d->n && !numbered; i++) numbered = d->lines[i].no > 0;
    float gutter = numbered ? cw * 6 + 8 : 0;
    float tx = x + 14 + gutter + cw * 2; /* room for a +/- mark */
    int cols = (int)((x + w - 18 - tx) / cw);
    if (cols < 10) cols = 10;
    if (cols != v->det_cols) detail_wrap(v, cols);
    int vis = (int)((h - DET_TOP - 10) / DET_ROW);
    int max_scroll = v->det_nrows - vis > 0 ? v->det_nrows - vis : 0;
    if (v->det_scroll > max_scroll) v->det_scroll = (float)max_scroll;
    if (v->det_scroll < 0) v->det_scroll = 0;
    int first = (int)v->det_scroll;
    float ry = y + DET_TOP;
    char buf[DET_ROW_BYTES];
    BeginScissorMode((int)x, (int)(y + DET_TOP - 2), (int)w, (int)(h - DET_TOP));
    for (int r = first; r < v->det_nrows && r < first + vis; r++, ry += DET_ROW) {
        struct DetRow *row = &v->det_rows[r];
        DetailLine *dl = &d->lines[row->line];
        bool cont = r > 0 && v->det_rows[r - 1].line == row->line; /* wrapped continuation */
        Color c = TEXT;
        switch (dl->kind) {
        case DL_ADD: c = (Color){150, 240, 160, 255}; DrawRectangle((int)x + 6, (int)ry, (int)w - 12, (int)DET_ROW, (Color){40, 120, 60, 60}); break;
        case DL_DEL: c = (Color){255, 150, 150, 255}; DrawRectangle((int)x + 6, (int)ry, (int)w - 12, (int)DET_ROW, (Color){140, 40, 40, 60}); break;
        case DL_HUNK: c = (Color){110, 190, 230, 255}; break;
        case DL_HEAD: c = (Color){255, 230, 160, 255}; break;
        case DL_DIM: c = DIM; break;
        case DL_ERR: c = (Color){255, 120, 120, 255}; break;
        default: break;
        }
        if (!cont && dl->no > 0) {
            snprintf(buf, sizeof buf, "%6d", dl->no);
            DrawTextEx(g_mono, buf, (Vector2){x + 10, ry + 1}, DET_FONT, 0, fade(DIM, 0.7f));
        }
        if (!cont && (dl->kind == DL_ADD || dl->kind == DL_DEL))
            DrawTextEx(g_mono, dl->kind == DL_ADD ? "+" : "-", (Vector2){tx - cw * 1.6f, ry + 1}, DET_FONT, 0, c);
        int len = row->len < (int)sizeof buf - 1 ? row->len : (int)sizeof buf - 1;
        memcpy(buf, dl->s + row->start, (size_t)len);
        buf[len] = 0;
        DrawTextEx(g_mono, buf, (Vector2){tx, ry + 1}, DET_FONT, 0, c);
    }
    EndScissorMode();
    if (max_scroll) { /* scrollbar */
        float track_h = h - DET_TOP - 10;
        float bh = fmaxf(20, track_h * (float)vis / (float)v->det_nrows);
        float by = y + DET_TOP + (track_h - bh) * (v->det_scroll / (float)max_scroll);
        DrawRectangleRounded((Rectangle){x + w - 7, by, 3, bh}, 1, 4, fade(DIM, 0.6f));
    }
    v->det_vis = vis;
}

static void draw_turn_window(Viz *v, Vector2 mouse) {
    if (!v->turn_open) { v->win = (Rectangle){0}; return; }
    Scene *m = v->m;
    int W = GetScreenWidth(), H = GetScreenHeight();
    int ai = actor_index(m, v->turn_actor);
    Color ac = ai >= 0 ? m->actors[ai].color : TEXT;
    const char *aname = ai >= 0 ? m->actors[ai].name : v->turn_actor;

    /* Prompt + totals of the turn. */
    const char *prompt = "";
    int add = 0, del = 0, files = 0;
    for (size_t i = 0; i < v->h.applied; i++) {
        const Event *e = &v->h.ev[i];
        if (e->kind == EV_TURN && e->turn == v->turn_no && !strcmp(e->actor, v->turn_actor)) prompt = e->text ? e->text : "";
    }
    for (size_t k = 0; k < v->nsteps; k++) {
        const Event *e = &v->h.ev[v->steps[k]];
        add += e->add;
        del += e->del;
    }
    files = v->hl_count;

    float w = fminf(560, (float)W * 0.48f), x = 16, y = HUD_TOP;
    /* Stop above the actor panel so its rows stay clickable. */
    float list_h = (float)H - TL_H - panel_height(m, v->sim) - 8 - y - 104;
    int rows_fit = (int)(list_h / WIN_ROW);
    if (rows_fit < 3) rows_fit = 3;
    int nrows = (int)v->nsteps;
    int vis = nrows < rows_fit ? nrows : rows_fit;
    float h = 74 + (float)(vis ? vis : 1) * WIN_ROW + 30;
    v->win = (Rectangle){x, y, w, h};
    DrawRectangleRounded(v->win, 0.03f, 6, (Color){22, 27, 34, 245});
    DrawRectangleRoundedLinesEx(v->win, 0.03f, 6, 1.5f, fade(ac, 0.6f));

    char line[512], fitbuf[512];
    snprintf(line, sizeof line, "%s  \xC2\xB7  turn %d", aname, v->turn_no);
    text(line, x + 14, y + 10, 17, ac);
    v->win_close = (Rectangle){x + w - 34, y + 6, 26, 26};
    bool hot_close = CheckCollisionPointRec(mouse, v->win_close);
    text("\xE2\x9C\x95", v->win_close.x + 6, v->win_close.y + 3, 18, hot_close ? TEXT : DIM);
    fit(fitbuf, sizeof fitbuf, prompt, 15, w - 28);
    text(fitbuf, x + 14, y + 36, 15, TEXT);
    DrawLine((int)x + 10, (int)(y + 62), (int)(x + w - 10), (int)(y + 62), fade(DIM, 0.3f));

    /* Steps, scrollable; sticks to the bottom while new steps arrive. */
    int max_scroll = nrows - vis > 0 ? nrows - vis : 0;
    if (v->turn_scroll >= max_scroll) v->turn_stick = true;
    if (v->turn_stick) v->turn_scroll = (float)max_scroll;
    if (v->turn_scroll > max_scroll) v->turn_scroll = (float)max_scroll;
    if (v->turn_scroll < 0) v->turn_scroll = 0;
    int first = (int)v->turn_scroll;
    float ry = y + 68;
    v->win_first = first;
    v->win_vis = vis;
    for (int r = 0; r < vis && first + r < nrows; r++, ry += WIN_ROW) {
        const Event *e = &v->h.ev[v->steps[first + r]];
        Rectangle rr = {x + 6, ry, w - 12, WIN_ROW};
        if (is_detail_of(v, e)) DrawRectangleRec(rr, fade(step_color(e->kind), 0.18f));
        else if (CheckCollisionPointRec(mouse, rr)) DrawRectangleRec(rr, fade(TEXT, 0.06f));
        char tm[16];
        fmt_clock(tm, sizeof tm, e->ts, "%H:%M:%S");
        text(tm, x + 14, ry + 3, 13, DIM);
        float cx = x + 14 + text_w("00:00:00", 13) + 10;
        bool failed = failed_bare(v, e);
        Color kc = failed ? LIVE_RED : step_color(e->kind);
        DrawCircle((int)cx + 4, (int)(ry + 11), 4, kc);
        cx += 14;
        char pa[16], pd[24];
        snprintf(pa, sizeof pa, "+%d", e->add);
        snprintf(pd, sizeof pd, "\xE2\x88\x92%d", e->del);
        bool has_diff = e->add || e->del;
        float dw = has_diff ? text_w(pa, 13) + 6 + text_w(pd, 13) + 8 : 0;
        if (e->notes) { /* it has comments: hover for them */
            bubble_icon(x + w - 14 - dw - 14, ry + 6, fade(TEXT, 0.45f));
            dw += 20;
        }
        step_label(line, sizeof line, e);
        if (failed) {
            float fw = text_w("failed", 12) + 10;
            text("failed", x + w - 14 - dw - fw + 10, ry + 4, 12, LIVE_RED);
            dw += fw;
        }
        fit(fitbuf, sizeof fitbuf, line, 14, x + w - 14 - dw - cx);
        text(fitbuf, cx, ry + 3, 14, failed ? (Color){255, 150, 150, 255} : e->kind == EV_BASH ? fade(TEXT, 0.75f) : TEXT);
        if (has_diff) {
            float dx = x + w - 14 - text_w(pd, 13) - 6 - text_w(pa, 13);
            text(pa, dx, ry + 4, 13, kind_color(EV_CREATE));
            text(pd, dx + text_w(pa, 13) + 6, ry + 4, 13, kind_color(EV_DELETE));
        }
    }
    if (!nrows) text("no steps yet", x + 14, ry + 3, 14, DIM);
    if (max_scroll) { /* scrollbar */
        float track_h = (float)vis * WIN_ROW;
        float bh = fmaxf(16, track_h * (float)vis / (float)nrows);
        float by = y + 68 + (track_h - bh) * (v->turn_scroll / (float)max_scroll);
        DrawRectangleRounded((Rectangle){x + w - 6, by, 3, bh}, 1, 4, fade(DIM, 0.6f));
    }
    char sa[32], sd[32];
    fmt_count(sa, sizeof sa, add);
    fmt_count(sd, sizeof sd, del);
    snprintf(line, sizeof line, "%zu steps  \xC2\xB7  +%s \xE2\x88\x92%s lines  \xC2\xB7  %d files touched", v->nsteps,
             sa, sd, files);
    text(line, x + 14, y + h - 24, 13, DIM);
}

/* The turn step under the mouse, or NULL. */
static const Event *turn_row_at(Viz *v, Vector2 mouse) {
    if (!v->turn_open || !CheckCollisionPointRec(mouse, v->win)) return NULL;
    int r = (int)floorf((mouse.y - (v->win.y + 68)) / WIN_ROW);
    if (r < 0 || r >= v->win_vis || (size_t)(v->win_first + r) >= v->nsteps) return NULL;
    return &v->h.ev[v->steps[v->win_first + r]];
}

/* Hovering a turn step shows its comments beside the row, as the panel will. */
static void draw_note_preview(Viz *v, Vector2 mouse) {
    if (v->det && (CheckCollisionPointRec(mouse, v->det_win) || CheckCollisionPointRec(mouse, v->note_win)))
        return; /* over the windows covering the turn window, not its rows */
    const Event *e = v->drag ? NULL : turn_row_at(v, mouse);
    if (!e || !e->notes || !e->tool_id || is_detail_of(v, e)) return;
    if (v->hov_src != e->src || v->hov_off != e->note_off || v->hov_n != e->notes || !v->hov_id ||
        strcmp(v->hov_id, e->tool_id)) { /* another step: read its comments */
        nv_load(&v->hov_nv, e);
        v->hov_src = e->src;
        v->hov_off = e->note_off;
        v->hov_n = e->notes;
        free(v->hov_id);
        v->hov_id = xstrdup(e->tool_id);
    }
    if (!v->hov_nv.ns.n) return;
    float W = (float)GetScreenWidth(), H = (float)GetScreenHeight();
    float pw = note_panel_w(), x = v->win.x + v->win.width + 10;
    if (x + pw > W - 16) x = fmaxf(16, W - 16 - pw);
    float ph = note_panel_h(&v->hov_nv, pw, (H - TL_H - 8 - HUD_TOP) * 0.6f);
    int r = (int)floorf((mouse.y - (v->win.y + 68)) / WIN_ROW);
    float y = v->win.y + 68 + (float)r * WIN_ROW - 8;
    if (y + ph > H - TL_H - 8) y = H - TL_H - 8 - ph;
    if (y < 8) y = 8;
    float scroll = 0;
    draw_note_panel(&v->hov_nv, (Rectangle){x, y, pw, ph}, &scroll, actor_color_by_id(v, v->turn_actor), true);
}

/* The comments as the agent wrote them (the panel drops markdown markers). */
static void notes_copy(Viz *v) {
    Notes ns = tr_notes(v->det_src, v->det_note_off, v->det_notes, v->det_id);
    size_t n = 1;
    for (int i = 0; i < ns.n; i++) n += strlen(ns.s[i]) + 2;
    char *buf = xmalloc(n), *p = buf;
    *p = 0;
    for (int i = 0; i < ns.n; i++) p += sprintf(p, "%s%s", i ? "\n\n" : "", ns.s[i]);
    SetClipboardText(buf);
    free(buf);
    notes_free(&ns);
}

/* ---- timeline ----------------------------------------------------------- */

typedef struct {
    float x0, x1, y; /* track */
} Track;

static Track track(void) {
    float W = (float)GetScreenWidth(), H = (float)GetScreenHeight();
    return (Track){24, W - 24, H - 30};
}

static bool in_timeline(Vector2 p) { return p.y >= GetScreenHeight() - TL_H; }

static double timeline_end(Viz *v) {
    double now = now_wall();
    double last = v->h.n ? v->h.ev[v->h.n - 1].ts : now;
    return now > last ? now : last;
}

static double x_to_time(Viz *v, float x) {
    Track tr = track();
    double end = timeline_end(v), len = tl_length(&v->h, end);
    double f = (x - tr.x0) / (tr.x1 - tr.x0);
    f = f < 0 ? 0 : f > 1 ? 1 : f;
    return tl_from_v(&v->h, f * len, end);
}

static float time_to_x(Viz *v, double t) {
    Track tr = track();
    double end = timeline_end(v), len = tl_length(&v->h, end);
    if (len <= 0) return tr.x1;
    return tr.x0 + (float)(tl_to_v(&v->h, t, end) / len) * (tr.x1 - tr.x0);
}

/* Event density per pixel column, writes and reads separately. */
static void update_bins(Viz *v) {
    Track tr = track();
    int nb = (int)(tr.x1 - tr.x0);
    double now = now_wall();
    if (nb == v->nbins && v->bins_n == v->h.n && now - v->bins_t < 1.0) return;
    if (nb != v->nbins) {
        v->bin_w = xrealloc(v->bin_w, (size_t)(nb > 0 ? nb : 1) * sizeof *v->bin_w);
        v->bin_r = xrealloc(v->bin_r, (size_t)(nb > 0 ? nb : 1) * sizeof *v->bin_r);
        v->nbins = nb;
    }
    memset(v->bin_w, 0, (size_t)(nb > 0 ? nb : 1) * sizeof *v->bin_w);
    memset(v->bin_r, 0, (size_t)(nb > 0 ? nb : 1) * sizeof *v->bin_r);
    double end = timeline_end(v), len = tl_length(&v->h, end);
    for (size_t i = 0; len > 0 && i < v->h.n; i++) {
        const Event *e = &v->h.ev[i];
        int b = (int)(v->h.v[i] / len * (nb - 1));
        if (b < 0 || b >= nb) continue;
        if (e->kind == EV_CREATE || e->kind == EV_EDIT || e->kind == EV_DELETE || e->kind == EV_TOUCH)
            v->bin_w[b] += 1 + log2f(1.0f + (float)(e->add + e->del));
        else if (e->kind == EV_READ || e->kind == EV_SEARCH)
            v->bin_r[b] += 1;
    }
    v->bins_n = v->h.n;
    v->bins_t = now;
}

static Color actor_color_of(Viz *v, const char *id) {
    for (int i = 0; i < v->m->nactors; i++)
        if (!strcmp(v->m->actors[i].id, id)) return v->m->actors[i].color;
    return DIM;
}

static void draw_timeline(Viz *v, Vector2 mouse) {
    Track tr = track();
    int W = GetScreenWidth(), H = GetScreenHeight();
    bool hot = in_timeline(mouse) || v->drag == 2;
    DrawRectangle(0, H - (int)TL_H, W, (int)TL_H, (Color){10, 13, 18, 230});
    update_bins(v);

    /* Density strip above the track. */
    float base = tr.y - 8;
    for (int b = 0; b < v->nbins; b++) {
        if (v->bin_w[b] > 0) {
            float hgt = fminf(18, 3 + 3 * log2f(1 + v->bin_w[b]));
            DrawRectangle((int)(tr.x0 + b), (int)(base - hgt), 1, (int)hgt, fade(kind_color(EV_EDIT), 0.75f));
        } else if (v->bin_r[b] > 0) {
            float hgt = fminf(8, 2 + 2 * log2f(1 + v->bin_r[b]));
            DrawRectangle((int)(tr.x0 + b), (int)(base - hgt), 1, (int)hgt, fade(kind_color(EV_READ), 0.6f));
        }
    }

    /* Track: played part, rest, idle-gap breaks, turn markers. */
    float th = hot ? 8 : 5;
    double shown_t = v->drag == 2 ? v->scrub_t : v->sim;
    float px = time_to_x(v, shown_t);
    DrawRectangleRounded((Rectangle){tr.x0, tr.y - th / 2, tr.x1 - tr.x0, th}, 1, 6, (Color){48, 54, 61, 255});
    DrawRectangleRounded((Rectangle){tr.x0, tr.y - th / 2, fmaxf(px - tr.x0, th), th}, 1, 6, LIVE_RED);
    double end = timeline_end(v), len = tl_length(&v->h, end);
    for (size_t i = 1; len > 0 && i < v->h.n; i++) {
        if (v->h.ev[i].ts - v->h.ev[i - 1].ts <= TL_GAP_CAP * 5) continue;
        float gx = tr.x0 + (float)((v->h.v[i] - TL_GAP_CAP / 2) / len) * (tr.x1 - tr.x0);
        DrawRectangle((int)gx - 1, (int)(tr.y - th / 2 - 1), 3, (int)th + 2, BG); /* compressed gap */
    }
    for (size_t i = 0; len > 0 && i < v->h.n; i++) {
        const Event *e = &v->h.ev[i];
        if (e->kind != EV_TURN) continue;
        float x = tr.x0 + (float)(v->h.v[i] / len) * (tr.x1 - tr.x0);
        DrawRectangle((int)x, (int)(tr.y - th / 2 - 4), 2, (int)th + 8, actor_color_of(v, e->actor));
    }
    DrawCircleV((Vector2){px, tr.y}, hot ? 8 : 6, TEXT);

    /* Ends: first event date, and LIVE/now. */
    char buf[64];
    if (v->h.n) fmt_clock(buf, sizeof buf, v->h.ev[0].ts, "%b %d %H:%M");
    else buf[0] = 0;
    text(buf, tr.x0, tr.y + 10, 13, DIM);
    const char *endlbl = v->live ? "LIVE" : "go live \xE2\x86\x92";
    text(endlbl, tr.x1 - text_w(endlbl, 13), tr.y + 10, 13, v->live ? LIVE_RED : DIM);

    /* Hover preview: time + the turn running then. */
    if (hot) {
        double t = v->drag == 2 ? v->scrub_t : x_to_time(v, mouse.x);
        float hx = v->drag == 2 ? px : mouse.x;
        char l1[64], l2[200] = "";
        fmt_clock(l1, sizeof l1, t, "%a %b %d  %H:%M:%S");
        size_t k = hist_upper(&v->h, t);
        Color c2 = DIM;
        while (k-- > 0) {
            const Event *e = &v->h.ev[k];
            if (e->kind != EV_TURN) continue;
            snprintf(l2, sizeof l2, "turn %d \xC2\xB7 %s: %s", e->turn, e->actor_name, e->text ? e->text : "");
            c2 = actor_color_of(v, e->actor);
            break;
        }
        float w = fmaxf(text_w(l1, 15), text_w(l2, 14)) + 20;
        float bx = fminf(fmaxf(hx - w / 2, 8), (float)W - w - 8), by = (float)H - TL_H - (l2[0] ? 50 : 32);
        DrawRectangleRounded((Rectangle){bx, by, w, l2[0] ? 46 : 28}, 0.2f, 6, PANEL);
        text(l1, bx + 10, by + 6, 15, TEXT);
        if (l2[0]) text(l2, bx + 10, by + 26, 14, c2);
        DrawLine((int)hx, (int)(H - TL_H), (int)hx, (int)(tr.y + 6), fade(TEXT, 0.4f));
    }
}

/* ---- hover card --------------------------------------------------------- */

static void update_hover(Viz *v, Vector2 mouse) {
    v->hover = HOVER_NONE;
    v->hover_from_win = false;
    if (in_timeline(mouse) || v->drag == 1 || v->drag == 2) return;
    Scene *m = v->m;
    if (v->det && (CheckCollisionPointRec(mouse, v->det_win) || CheckCollisionPointRec(mouse, v->note_win))) return;
    if (v->turn_open && CheckCollisionPointRec(mouse, v->win)) {
        /* A step row: ring its file in the graph (no card over the list). */
        int r = (int)floorf((mouse.y - (v->win.y + 68)) / WIN_ROW);
        if (r >= 0 && r < v->win_vis && (size_t)(v->win_first + r) < v->nsteps) {
            const Event *e = &v->h.ev[v->steps[v->win_first + r]];
            int ni = e->path && touches_file(e->kind) ? model_find(m, false, e->path) : -1;
            if (ni >= 0) { v->hover = HOVER_NODE; v->hover_i = ni; v->hover_from_win = true; }
        }
        return;
    }
    float best = 1e9f;
    for (int i = 0; i < m->nactors; i++) {
        if (actor_alpha(&m->actors[i], v->sim) <= 0) continue;
        Vector2 s = GetWorldToScreen2D(m->actors[i].pos, v->cam);
        float d = dist2d(s, mouse);
        if (d < 14 && d < best) { best = d; v->hover = HOVER_ACTOR; v->hover_i = i; }
    }
    if (v->hover) return;
    for (int i = 0; i < m->nnodes; i++) {
        Node *n = &m->nodes[i];
        if (n->alpha <= 0) continue;
        Vector2 s = GetWorldToScreen2D(n->pos, v->cam);
        float r = (n->is_dir ? 4 : n->radius * v->cam.zoom) + 5;
        float d = dist2d(s, mouse);
        if (d < r && d / r < best) { best = d / r; v->hover = HOVER_NODE; v->hover_i = i; }
    }
}

static void draw_hover(Viz *v, Vector2 mouse) {
    if (!v->hover || v->hover_from_win) return;
    Scene *m = v->m;
    char l[4][256];
    Color c[4] = {TEXT, DIM, DIM, DIM};
    int n = 0;
    char a1[32], a2[32], ago[32];
    if (v->hover == HOVER_ACTOR) {
        Actor *a = &m->actors[v->hover_i];
        snprintf(l[n], sizeof l[n], "%s", a->name);
        c[n++] = a->color;
        if (a->prompt) { snprintf(l[n], sizeof l[n], "turn %d: %s", a->turn, a->prompt); c[n++] = TEXT; }
        fmt_count(a1, sizeof a1, a->add);
        fmt_count(a2, sizeof a2, a->del);
        snprintf(l[n++], sizeof l[n], "+%s \xE2\x88\x92%s lines \xC2\xB7 %d created \xC2\xB7 %d edits \xC2\xB7 %d reads",
                 a1, a2, a->files_created, a->files_edited, a->reads);
        fmt_ago(ago, sizeof ago, v->sim - a->last_active);
        snprintf(l[n++], sizeof l[n], "last active %s", ago);
    } else {
        Node *nd = &m->nodes[v->hover_i];
        const char *path = nd->path[0] ? nd->path : nd->name;
        if (nd->is_dir) {
            snprintf(l[n++], sizeof l[n], "Folder \xC2\xB7 %s", path);
            snprintf(l[n++], sizeof l[n], "%d files \xC2\xB7 %d folders touched", nd->nkids, nd->ndirs);
        } else {
            snprintf(l[n], sizeof l[n], "%s \xC2\xB7 %s", kind_verb(nd->last_kind), path);
            c[n++] = nd->deleted ? LIVE_RED : kind_color(nd->last_kind);
            fmt_count(a1, sizeof a1, nd->add);
            fmt_count(a2, sizeof a2, nd->del);
            if (nd->lines >= 0)
                snprintf(l[n++], sizeof l[n], "%d lines \xC2\xB7 +%s \xE2\x88\x92%s this session", nd->lines, a1, a2);
            else
                snprintf(l[n++], sizeof l[n], "+%s \xE2\x88\x92%s this session", a1, a2);
            fmt_ago(ago, sizeof ago, v->sim - nd->last_touch);
            const Actor *a = &m->actors[nd->last_actor];
            if (nd->last_add || nd->last_del)
                snprintf(l[n], sizeof l[n], "by %s \xC2\xB7 %s \xC2\xB7 +%d \xE2\x88\x92%d", a->name, ago,
                         nd->last_add, nd->last_del);
            else
                snprintf(l[n], sizeof l[n], "by %s \xC2\xB7 %s", a->name, ago);
            c[n++] = a->color;
        }
    }
    float w = 0;
    for (int i = 0; i < n; i++) w = fmaxf(w, text_w(l[i], i ? 14 : 16));
    w += 20;
    float h = 12 + 20 + 18 * (float)(n - 1);
    float x = mouse.x + 16, y = mouse.y + 16;
    if (x + w > GetScreenWidth() - 8) x = mouse.x - w - 12;
    if (y + h > GetScreenHeight() - TL_H) y = mouse.y - h - 12;
    DrawRectangleRounded((Rectangle){x, y, w, h}, 0.12f, 6, PANEL);
    for (int i = 0; i < n; i++) text(l[i], x + 10, y + 7 + (i ? 22 + 18 * (float)(i - 1) : 0), i ? 14 : 16, c[i]);
}

/* ---- camera ------------------------------------------------------------- */

static void auto_camera(Viz *v, float dt) {
    Scene *m = v->m;
    float minx = 1e9f, miny = 1e9f, maxx = -1e9f, maxy = -1e9f;
    /* An open turn frames just its files, beside the window. */
    bool focus = v->turn_open && v->hl_count;
again:
    for (int i = 0; i < m->nnodes; i++) {
        Node *n = &m->nodes[i];
        if (n->alpha <= 0 && i) continue;
        if (focus && !v->hl[i]) continue;
        minx = fminf(minx, n->pos.x - n->radius);
        maxx = fmaxf(maxx, n->pos.x + n->radius);
        miny = fminf(miny, n->pos.y - n->radius);
        maxy = fmaxf(maxy, n->pos.y + n->radius);
    }
    for (int i = 0; i < m->nactors; i++) {
        if (actor_alpha(&m->actors[i], v->sim) <= 0) continue;
        if (focus && strcmp(m->actors[i].id, v->turn_actor)) continue;
        minx = fminf(minx, m->actors[i].pos.x);
        maxx = fmaxf(maxx, m->actors[i].pos.x);
        miny = fminf(miny, m->actors[i].pos.y);
        maxy = fmaxf(maxy, m->actors[i].pos.y);
    }
    if (focus && minx > maxx) { focus = false; goto again; } /* its files all faded: frame everything */
    float W = (float)GetScreenWidth(), H = (float)GetScreenHeight();
    float right = legend_shown(v) ? LEGEND_W + 24 : 0;
    float left = v->turn_open && v->win.width > 0 ? v->win.x + v->win.width + 8 : 0;
    if (v->det && v->det_win.width > 0 && W - (v->det_win.x + v->det_win.width) > W * 0.3f)
        left = fmaxf(left, v->det_win.x + v->det_win.width + 8); /* only when the graph fits beside it */
    float availw = fmaxf(W - right - left, W * 0.3f);
    float bw = fmaxf(maxx - minx, 200) + 160, bh = fmaxf(maxy - miny, 200) + 160;
    float bottom = panel_height(m, v->sim) + TL_H;
    float avail = fmaxf(H - HUD_TOP - bottom, H * 0.35f);
    float zoom = fminf(availw * 0.94f / bw, avail / bh);
    zoom = fminf(zoom, 2.5f);
    Vector2 c = {(minx + maxx) / 2, (miny + maxy) / 2};
    float e = fminf(1, dt * 2);
    v->cam.zoom += (zoom - v->cam.zoom) * e;
    v->cam.target.x += (c.x - v->cam.target.x) * e;
    v->cam.target.y += (c.y - v->cam.target.y) * e;
    v->cam.offset = (Vector2){left + availw / 2, HUD_TOP + avail / 2};
}

/* ---- main loop ---------------------------------------------------------- */

static void handle_input(Viz *v, float dt) {
    (void)dt;
    Vector2 mouse = GetMousePosition();
    if (IsKeyPressed(KEY_SPACE)) v->paused = !v->paused;
    if (IsKeyPressed(KEY_EQUAL) || IsKeyPressed(KEY_KP_ADD) || IsKeyPressed(KEY_UP)) v->speed *= 2;
    if (IsKeyPressed(KEY_MINUS) || IsKeyPressed(KEY_KP_SUBTRACT) || IsKeyPressed(KEY_DOWN))
        /* Stop at real time on the way down, then x0.5 and x0.25. */
        v->speed = v->speed > 1 && v->speed / 2 < 1 ? 1 : fmax(0.25, v->speed / 2);
    if (IsKeyPressed(KEY_C) && !IsKeyDown(KEY_LEFT_CONTROL) && !IsKeyDown(KEY_RIGHT_CONTROL)) v->auto_cam = true;
    if (IsKeyPressed(KEY_H)) v->legend = !v->legend;
    if (IsKeyPressed(KEY_F)) ToggleFullscreen();
    if (IsKeyPressed(KEY_L)) seek(v, now_wall());
    if (IsKeyPressed(KEY_HOME) && v->h.n) seek(v, v->h.ev[0].ts - 1);
    if (IsKeyPressed(KEY_LEFT) || IsKeyPressed(KEY_RIGHT)) {
        /* Step 2% of the timeline, like a video's arrow keys. */
        double end = timeline_end(v), len = tl_length(&v->h, end);
        double cur = tl_to_v(&v->h, v->sim, end);
        double step = (IsKeyPressed(KEY_LEFT) ? -1 : 1) * fmax(len * 0.02, 5);
        seek(v, tl_from_v(&v->h, cur + step, end));
    }

    float wheel = GetMouseWheelMove();
    if (v->det) {
        if (IsKeyPressed(KEY_PAGE_DOWN)) v->det_scroll += (float)(v->det_vis - 1);
        if (IsKeyPressed(KEY_PAGE_UP)) v->det_scroll -= (float)(v->det_vis - 1);
        if ((IsKeyDown(KEY_LEFT_CONTROL) || IsKeyDown(KEY_RIGHT_CONTROL)) && IsKeyPressed(KEY_C)) {
            if (CheckCollisionPointRec(mouse, v->note_win)) notes_copy(v); /* the panel under the mouse */
            else detail_copy(v);
        }
    }
    if (wheel != 0 && v->det && CheckCollisionPointRec(mouse, v->note_win)) {
        v->note_scroll -= wheel * NOTE_ROW * 3;
    } else if (wheel != 0 && v->det && CheckCollisionPointRec(mouse, v->det_win)) {
        v->det_scroll -= wheel * 3;
    } else if (wheel != 0 && v->turn_open && CheckCollisionPointRec(mouse, v->win)) {
        v->turn_scroll -= wheel * 3;
        v->turn_stick = false; /* re-sticks when scrolled back to the bottom */
    } else if (wheel != 0 && !in_timeline(mouse)) {
        v->auto_cam = false;
        Vector2 mw = GetScreenToWorld2D(mouse, v->cam);
        v->cam.offset = mouse;
        v->cam.target = mw;
        v->cam.zoom = fmaxf(0.02f, v->cam.zoom * (wheel > 0 ? 1.15f : 1 / 1.15f));
    }

    if (IsMouseButtonPressed(MOUSE_BUTTON_LEFT)) {
        Track tr = track();
        int row = -1;
        for (int k = 0; k < v->nrows; k++)
            if (CheckCollisionPointRec(mouse, v->row_rect[k])) row = v->row_actor[k];
        v->press_outside = false;
        if (v->det && CheckCollisionPointRec(mouse, v->det_win)) {
            if (CheckCollisionPointRec(mouse, v->det_close)) detail_close(v);
            v->drag = 3;
        } else if (v->det && CheckCollisionPointRec(mouse, v->note_win)) {
            v->drag = 3; /* consumed by the comments panel */
        } else if (v->turn_open && CheckCollisionPointRec(mouse, v->win)) {
            int r = (int)floorf((mouse.y - (v->win.y + 68)) / WIN_ROW);
            if (CheckCollisionPointRec(mouse, v->win_close)) turn_close(v);
            else if (r >= 0 && r < v->win_vis && (size_t)(v->win_first + r) < v->nsteps)
                detail_open(v, &v->h.ev[v->steps[v->win_first + r]], true); /* a step: show it in full */
            v->drag = 3; /* consumed by the window */
        } else if (in_timeline(mouse) && mouse.x > tr.x1 - text_w("go live \xE2\x86\x92", 13) && mouse.y > tr.y + 6) {
            seek(v, now_wall()); /* "go live" label */
        } else if (in_timeline(mouse)) {
            v->drag = 2;
            v->scrub_t = x_to_time(v, mouse.x);
        } else if (v->hover == HOVER_ACTOR && !v->hover_from_win) {
            turn_open(v, v->hover_i);
            v->drag = 3;
        } else if (row >= 0) {
            turn_open(v, row);
            v->drag = 3;
        } else {
            v->drag = 1;
            v->press_at = mouse;
            v->press_outside = v->turn_open || v->det; /* a click (not a pan) outside closes */
            free(v->press_file);
            v->press_file = v->hover == HOVER_NODE && !v->hover_from_win && !v->m->nodes[v->hover_i].is_dir
                                ? xstrdup(v->m->nodes[v->hover_i].path) : NULL;
        }
    }
    if (IsMouseButtonReleased(MOUSE_BUTTON_LEFT) && v->drag == 1 && dist2d(mouse, v->press_at) < 4) {
        bool opened = v->press_file && file_detail(v, v->press_file); /* a file: its last change in full */
        if (!opened && v->press_outside && v->det) detail_close(v); /* one layer at a time */
        else if (!opened && v->press_outside) turn_close(v);
    }
    if (v->drag == 1 && IsMouseButtonDown(MOUSE_BUTTON_LEFT)) {
        Vector2 d = GetMouseDelta();
        if (d.x || d.y) {
            v->auto_cam = false;
            v->cam.target.x -= d.x / v->cam.zoom;
            v->cam.target.y -= d.y / v->cam.zoom;
        }
    }
    if (v->drag == 2) {
        v->scrub_t = x_to_time(v, mouse.x);
        double now = GetTime();
        bool released = IsMouseButtonReleased(MOUSE_BUTTON_LEFT) || !IsMouseButtonDown(MOUSE_BUTTON_LEFT);
        if (released || now - v->last_seek > SEEK_THROTTLE) { /* live preview while dragging */
            seek(v, v->scrub_t);
            v->last_seek = now;
        }
    }
    if (!IsMouseButtonDown(MOUSE_BUTTON_LEFT)) v->drag = 0;
}

static void advance_clock(Viz *v, float dt) {
    if (v->paused || v->drag == 2) return;
    double now = now_wall();
    if (v->live) {
        v->sim = now;
    } else {
        v->sim += dt * v->speed;
        if (v->h.applied < v->h.n && v->h.ev[v->h.applied].ts - v->sim > IDLE_GAP_REAL * v->speed)
            v->sim = v->h.ev[v->h.applied].ts - IDLE_GAP_REAL * v->speed * 0.25;
        if (v->sim >= now || v->h.applied == v->h.n) { /* caught up / replay exhausted */
            v->sim = now;
            v->live = true;
        }
    }
    apply_until(v, v->sim, true);
}

int viz_run(const VizOpts *o, EvQueue *q) {
    SetConfigFlags(FLAG_MSAA_4X_HINT | FLAG_WINDOW_RESIZABLE | FLAG_VSYNC_HINT);
    SetTraceLogLevel(getenv("AIG_DEBUG") ? LOG_INFO : LOG_WARNING);
    InitWindow(o->width, o->height, "aigource");
    SetTargetFPS(60);
    SetExitKey(KEY_NULL);
    load_font();
    load_mono();

    Viz v = {.o = o, .m = model_new(o->root), .speed = o->speed > 0 ? o->speed : 60,
             .auto_cam = true, .legend = true, .cam = {.zoom = 1}};
    v.sim = -1e18; /* nothing is in the past yet: the first batch goes to history unapplied */
    ingest_new_events(&v, q);
    v.sim = v.h.n ? v.h.ev[0].ts - 1 : now_wall();
    if (o->live) {
        seek(&v, now_wall());
        for (int i = 0; i < 240; i++) model_update(v.m, 1.0f / 60, v.sim); /* settle layout */
    } else if (o->seek >= 0 && v.h.n) {
        double end = timeline_end(&v);
        seek(&v, tl_from_v(&v.h, o->seek * tl_length(&v.h, end), end));
        v.paused = true;
        for (int i = 0; i < 240; i++) model_update(v.m, 1.0f / 60, v.sim);
    }

    int frame = 0;
    while (!WindowShouldClose()) {
        float dt = o->shot ? 1.0f / 60 : GetFrameTime();
        if (IsKeyPressed(KEY_Q)) break;
        if (IsKeyPressed(KEY_ESCAPE)) { /* closes the top window; quits when none is open */
            if (v.det) detail_close(&v);
            else if (v.turn_open) turn_close(&v);
            else break;
        }
        ingest_new_events(&v, q);
        handle_input(&v, dt);
        advance_clock(&v, dt);
        model_update(v.m, v.paused ? 0 : dt, v.sim);
        if (o->turn && !v.turn_open && frame == 0) { /* test hook: open an actor's turn */
            for (int i = 0; i < v.m->nactors; i++)
                if (!strncmp(v.m->actors[i].id, o->turn, strlen(o->turn))) { turn_open(&v, i); break; }
        }
        turn_update(&v); /* before anything reads hl[]: input may have just opened a turn */
        if (o->step && v.turn_open && !v.det && frame == 0 && v.nsteps) { /* test hook: a step's detail */
            long k = strtol(o->step, NULL, 10);
            if (k < 0) k += (long)v.nsteps;
            if (k >= 0 && (size_t)k < v.nsteps) detail_open(&v, &v.h.ev[v.steps[k]], true);
        }
        if (o->file && frame == 0) file_detail(&v, o->file); /* test hook: a click on a file */
        if (v.det && v.det->state == 1 && GetTime() - v.det_reload > 1) { /* still running */
            struct stat st;
            v.det_reload = GetTime();
            if (v.det_src && !stat(v.det_src, &st) && (long)st.st_size != v.det_size) detail_load(&v);
        }
        if (v.auto_cam) auto_camera(&v, dt);

        Vector2 mouse = o->shot && o->mouse_x >= 0 ? (Vector2){o->mouse_x, o->mouse_y} : GetMousePosition();
        update_hover(&v, mouse);

        BeginDrawing();
        ClearBackground(BG);
        BeginMode2D(v.cam);
        draw_scene(&v);
        EndMode2D();
        draw_labels(&v);
        draw_counters(&v);
        draw_legend(&v);
        draw_panel(&v);
        draw_timeline(&v, mouse);
        draw_turn_window(&v, mouse);
        draw_detail(&v, mouse);
        draw_note_preview(&v, mouse);
        draw_hover(&v, mouse);
        EndDrawing();

        if (frame < INT_MAX) frame++; /* also gates the test hooks to the first frame */
        if (o->shot && frame >= o->frames) {
            Image img = LoadImageFromScreen(); /* TakeScreenshot drops the directory */
            ExportImage(img, o->shot);
            UnloadImage(img);
            break;
        }
    }
    detail_close(&v);
    free(v.det_rows);
    nv_clear(&v.hov_nv);
    free(v.det_nv.rows);
    free(v.hov_nv.rows);
    free(v.hov_id);
    free(v.press_file);
    CloseWindow();
    return 0;
}
