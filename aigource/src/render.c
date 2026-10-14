#include "render.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "model.h"
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
    int cps[95 + 8];
    int n = 0;
    for (int c = 32; c < 127; c++) cps[n++] = c;
    int extra[] = {0x2026, 0x00B7, 0x2212, 0x2192, 0x25B6, 0x2016, 0x2190, 0xFFFD}; /* … · − → ▶ ‖ ← � */
    for (int i = 0; i < 8; i++) cps[n++] = extra[i];
    for (int i = 0; paths[i]; i++) {
        if (!FileExists(paths[i])) continue;
        g_font = LoadFontEx(paths[i], 64, cps, n);
        SetTextureFilter(g_font.texture, TEXTURE_FILTER_BILINEAR);
        g_font_ok = true;
        return;
    }
    g_font = GetFontDefault();
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
    }
    v->sim = t;
}

static void ingest_new_events(Viz *v, EvQueue *q) {
    Event *in;
    size_t n = evq_drain(q, &in);
    if (!n) { free(in); return; }
    qsort(in, n, sizeof *in, ev_cmp);
    size_t k = 0; /* events already in the past get applied now */
    while (k < n && in[k].ts <= v->sim) {
        model_apply(v->m, &in[k], v->live && in[k].ts > v->sim - BACKLOG_SECS);
        k++;
    }
    hist_add(&v->h, in, n, k);
    free(in);
}

/* ---- drawing: scene ----------------------------------------------------- */

static void draw_scene(Viz *v) {
    Scene *m = v->m;
    float px = 1.0f / v->cam.zoom; /* one screen pixel in world units */
    Node *N = m->nodes;

    for (int i = 1; i < m->nnodes; i++) { /* tree edges */
        Node *n = &N[i];
        if (n->alpha <= 0) continue;
        Node *p = &N[n->parent];
        if (n->is_dir) DrawLineEx(p->pos, n->pos, 1.5f * px, fade((Color){90, 100, 120, 255}, 0.6f * n->alpha));
        else DrawLineEx(p->pos, n->pos, 1.0f * px, fade((Color){90, 100, 120, 255}, 0.25f * n->alpha));
    }

    BeginBlendMode(BLEND_ADDITIVE);
    for (int k = 0; k < m->fx_count; k++) { /* pulses under the nodes */
        Fx *f = &m->fx[(m->fx_head + k) % MAX_FX];
        if (f->kind != FX_PULSE || f->age >= f->life) continue;
        Node *n = &N[f->node];
        float t = f->age / f->life;
        float r = n->radius + (12 + 90 * f->strength) * t;
        DrawRing(n->pos, r - (2 + 6 * f->strength) * px * 2, r, 0, 360, 48,
                 fade(f->color, (1 - t) * (0.35f + 0.65f * f->strength)));
    }
    EndBlendMode();

    for (int i = 0; i < m->nnodes; i++) {
        Node *n = &N[i];
        if (n->alpha <= 0) continue;
        if (n->is_dir) {
            DrawCircleV(n->pos, 3.5f * px + 2, fade(n->color, 0.8f * n->alpha));
            continue;
        }
        if (n->flash > 0) {
            BeginBlendMode(BLEND_ADDITIVE);
            DrawCircleV(n->pos, n->radius * (1.6f + n->flash), fade(n->color, 0.35f * n->flash));
            EndBlendMode();
        }
        Color c = n->deleted ? LIVE_RED : ColorBrightness(n->color, 0.5f * n->flash);
        DrawCircleV(n->pos, n->radius, fade(c, n->alpha));
    }

    BeginBlendMode(BLEND_ADDITIVE);
    for (int k = 0; k < m->fx_count; k++) { /* beams over the nodes, colored by action */
        Fx *f = &m->fx[(m->fx_head + k) % MAX_FX];
        if (f->kind != FX_BEAM || f->age >= f->life) continue;
        Actor *a = &m->actors[f->actor];
        if (actor_alpha(a, v->sim) <= 0) continue;
        float t = f->age / f->life;
        float w = (1.0f + 9.0f * f->strength) * px;
        DrawLineEx(a->pos, N[f->node].pos, w, fade(f->color, (1 - t) * 0.9f));
    }
    EndBlendMode();

    for (int i = 0; i < m->nactors; i++) {
        Actor *a = &m->actors[i];
        float al = actor_alpha(a, v->sim);
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
            text(n->name, s.x + 6, s.y - 18, 15, fade(DIM, 0.85f * n->alpha));
        } else {
            double age = v->sim - n->last_touch;
            if (age < LABEL_SECS && age >= 0) {
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

static void draw_legend(Viz *v) {
    int W = GetScreenWidth();
    float x = (float)W - LEGEND_W - 16, y = 84;
    if (!v->legend) {
        text("H  legend", (float)W - text_w("H  legend", 14) - 24, y, 14, fade(DIM, 0.7f));
        return;
    }
    /* File types present, most common first. */
    typedef struct { char ext[16]; Color c; int n; } FileType;
    FileType types[32];
    int nt = 0;
    Scene *m = v->m;
    for (int i = 0; i < m->nnodes; i++) {
        Node *n = &m->nodes[i];
        if (n->is_dir || n->alpha <= 0) continue;
        const char *dot = strrchr(n->name, '.');
        const char *ext = dot && dot != n->name ? dot : n->name;
        int k = 0;
        while (k < nt && strcmp(types[k].ext, ext)) k++;
        if (k == nt) {
            if (nt == 32) continue;
            snprintf(types[k].ext, sizeof types[k].ext, "%s", ext);
            types[k].c = n->color;
            types[k].n = 0;
            nt++;
        }
        types[k].n++;
    }
    for (int i = 1; i < nt; i++) /* insertion sort by count */
        for (int j = i; j > 0 && types[j].n > types[j - 1].n; j--) {
            FileType t = types[j];
            types[j] = types[j - 1];
            types[j - 1] = t;
        }
    int shown_types = nt < 8 ? nt : 8;
    float h = 22 + 6 * 20 + 26 + 2 * 20 + 26 + (float)((shown_types + 1) / 2) * 20 + 26 + 4 * 17 + 10;
    DrawRectangleRounded((Rectangle){x, y, LEGEND_W, h}, 0.04f, 6, PANEL);
    float cx = x + 12, cy = y + 8;

    text("ACTIONS  (beam / flash)", cx, cy, 13, DIM);
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
    text("FILES  (color = type, size = lines)", cx, cy, 13, DIM);
    cy += 20;
    for (int i = 0; i < shown_types; i++) {
        float tx = cx + (i % 2) * (LEGEND_W / 2 - 6);
        swatch(tx, cy, types[i].c);
        text(types[i].ext, tx + 20, cy, 15, TEXT);
        if (i % 2 == 1 || i == shown_types - 1) cy += 20;
    }
    cy += 6;
    text("KEYS", cx, cy, 13, DIM);
    cy += 18;
    static const char *keys[] = {
        "space pause    \xE2\x86\x90/\xE2\x86\x92 seek",
        "+/- speed    L live    H legend",
        "drag pan    wheel zoom    C camera",
        "F fullscreen    Q quit",
    };
    for (int i = 0; i < 4; i++, cy += 17) text(keys[i], cx, cy, 13, fade(TEXT, 0.8f));
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
    for (int k = 0; k < shown; k++, y += ROW_H) {
        Actor *a = &m->actors[order[k]];
        float al = actor_alpha(a, v->sim);
        DrawCircle(30, (int)y + 9, 6, fade(a->color, al));
        char sa[32], sd[32];
        fmt_count(sa, sizeof sa, a->add);
        fmt_count(sd, sizeof sd, a->del);
        text(a->name, 44, y, 17, fade(TEXT, al));
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
    if (in_timeline(mouse) || v->drag) return;
    Scene *m = v->m;
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
    if (!v->hover) return;
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
    for (int i = 0; i < m->nnodes; i++) {
        Node *n = &m->nodes[i];
        if (n->alpha <= 0 && i) continue;
        minx = fminf(minx, n->pos.x - n->radius);
        maxx = fmaxf(maxx, n->pos.x + n->radius);
        miny = fminf(miny, n->pos.y - n->radius);
        maxy = fmaxf(maxy, n->pos.y + n->radius);
    }
    for (int i = 0; i < m->nactors; i++) {
        if (actor_alpha(&m->actors[i], v->sim) <= 0) continue;
        minx = fminf(minx, m->actors[i].pos.x);
        maxx = fmaxf(maxx, m->actors[i].pos.x);
        miny = fminf(miny, m->actors[i].pos.y);
        maxy = fmaxf(maxy, m->actors[i].pos.y);
    }
    float W = (float)GetScreenWidth(), H = (float)GetScreenHeight();
    float right = v->legend ? LEGEND_W + 24 : 0;
    float availw = fmaxf(W - right, W * 0.5f);
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
    v->cam.offset = (Vector2){availw / 2, HUD_TOP + avail / 2};
}

/* ---- main loop ---------------------------------------------------------- */

static void handle_input(Viz *v, float dt) {
    (void)dt;
    Vector2 mouse = GetMousePosition();
    if (IsKeyPressed(KEY_SPACE)) v->paused = !v->paused;
    if (IsKeyPressed(KEY_EQUAL) || IsKeyPressed(KEY_KP_ADD) || IsKeyPressed(KEY_UP)) v->speed *= 2;
    if (IsKeyPressed(KEY_MINUS) || IsKeyPressed(KEY_KP_SUBTRACT) || IsKeyPressed(KEY_DOWN))
        v->speed = fmax(1, v->speed / 2);
    if (IsKeyPressed(KEY_C)) v->auto_cam = true;
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
    if (wheel != 0 && !in_timeline(mouse)) {
        v->auto_cam = false;
        Vector2 mw = GetScreenToWorld2D(mouse, v->cam);
        v->cam.offset = mouse;
        v->cam.target = mw;
        v->cam.zoom = fmaxf(0.02f, v->cam.zoom * (wheel > 0 ? 1.15f : 1 / 1.15f));
    }

    if (IsMouseButtonPressed(MOUSE_BUTTON_LEFT)) {
        Track tr = track();
        if (in_timeline(mouse) && mouse.x > tr.x1 - text_w("go live \xE2\x86\x92", 13) && mouse.y > tr.y + 6) {
            seek(v, now_wall()); /* "go live" label */
        } else if (in_timeline(mouse)) {
            v->drag = 2;
            v->scrub_t = x_to_time(v, mouse.x);
        } else {
            v->drag = 1;
        }
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
        if (IsKeyPressed(KEY_Q) || IsKeyPressed(KEY_ESCAPE)) break;
        ingest_new_events(&v, q);
        handle_input(&v, dt);
        advance_clock(&v, dt);
        model_update(v.m, v.paused ? 0 : dt, v.sim);
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
        draw_hover(&v, mouse);
        EndDrawing();

        if (o->shot && ++frame >= o->frames) {
            Image img = LoadImageFromScreen(); /* TakeScreenshot drops the directory */
            ExportImage(img, o->shot);
            UnloadImage(img);
            break;
        }
    }
    CloseWindow();
    return 0;
}
