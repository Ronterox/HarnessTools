/* The structure screen's inspector: a node or a turn, laid out once into a
 * list of items (texts, boxes, links, the code, notes) when what it shows
 * changes, then drawn scrolled every frame. Sections and wording are the
 * draft's (structure.tpl.html: inspect, inspectTurn). */
#include <ctype.h>
#include <limits.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "sview_int.h"
#include "util.h"

#define GAP 16.0f       /* between sections */
#define MAXL_DEF 80     /* a function's snippet: lines shown */
#define MAXL_FILE 400   /* a file's */
#define CODE_MAX_H 320.0f
#define CODE_ROW 18.0f
#define CARD_MIN 40.0f
#define CX_HIGH 15

/* ---- items ---------------------------------------------------------------- */

static IItem *add(Doc *d, int k) {
    if (d->n == d->cap) {
        d->cap = d->cap ? d->cap * 2 : 128;
        d->it = xrealloc(d->it, sizeof *d->it * (size_t)d->cap);
    }
    IItem *it = &d->it[d->n++];
    memset(it, 0, sizeof *it);
    it->k = (unsigned char)k;
    it->node = it->turn = -1;
    it->aux = INT_MIN;
    return it;
}

static void doc_reset(Doc *d) {
    for (int i = 0; i < d->n; i++) free(d->it[i].s), free(d->it[i].st), free(d->it[i].tip);
    d->n = 0;
    d->h = 0;
    for (int i = 0; i < d->nnv; i++) ui_nv_clear(&d->nv[i]), free(d->nv[i].rows);
    d->nnv = 0;
    ui_code_free(&d->cv);
    free(d->orig);
    d->orig = NULL;
    d->norig = d->cols = 0;
    d->code_scroll = d->code_hscroll = 0;
    d->card = -1;
}

static void doc_free(Doc *d) {
    doc_reset(d);
    free(d->it);
    free(d->nv);
    free(d->nvs);
    memset(d, 0, sizeof *d);
    d->card = -1;
}

void si_free(Insp *in) {
    doc_free(&in->doc);
    doc_free(&in->card);
    in->key[0] = 0;
}

/* A line of text whose box is lh high from y. */
static float text(Doc *d, int f, float css, float sp, Color c, float x, float y, float lh, const char *s) {
    IItem *it = add(d, IT_TEXT);
    it->font = (unsigned char)f;
    it->size = css;
    it->sp = sp;
    it->c = it->c2 = c;
    it->x = x, it->y = y, it->h = lh;
    it->s = xstrdup(s);
    it->w = sv_tws(f, s, css, sp);
    return it->w;
}

static IItem *box(Doc *d, float x, float y, float w, float h, Color fill, Color border, float radius) {
    IItem *it = add(d, IT_BOX);
    it->x = x, it->y = y, it->w = w, it->h = h;
    it->c = fill, it->c2 = border, it->radius = radius, it->bw = 1;
    return it;
}

static void dot(Doc *d, float cx, float cy, float r, Color c) {
    IItem *it = add(d, IT_DOT);
    it->x = cx, it->y = cy, it->w = r, it->c = c;
}

/* A title over a rectangle (the draft's title= attributes). */
static void tip(Doc *d, float x, float y, float w, float h, const char *s) {
    IItem *it = add(d, IT_TIP);
    it->x = x, it->y = y, it->w = w, it->h = h;
    it->tip = xstrdup(s);
}

/* s word-wrapped at w px (ui_wrap), the first line `indent` in; with md its
 * **bold** and `code` styled (bold in `bold`). At most maxl lines (the last
 * cut with "…"), 0 any. Returns the height. */
static float wrap_md(Doc *d, int f, float css, Color c, Color bold, float x, float y, float w, float lh, float indent,
                     const char *s, int maxl, bool md) {
    char *plain = (char *)s;
    unsigned char *st = NULL;
    if (md) ui_md(s, &plain, &st);
    int n = (int)strlen(plain), cap = 0;
    UiSpan *rows = NULL;
    int nr = ui_wrap(sv_font(f), sv_bold(f), plain, st, n, PX(css), w, indent, &rows, &cap);
    int shown = maxl && nr > maxl ? maxl : nr;
    for (int k = 0; k < shown; k++) {
        IItem *it = add(d, IT_TEXT);
        it->font = (unsigned char)f;
        it->size = css;
        it->c = c, it->c2 = bold;
        it->x = x + (k ? 0 : indent), it->y = y + (float)k * lh, it->h = lh;
        int len = rows[k].len;
        char *t = xmalloc((size_t)len + 4);
        memcpy(t, plain + rows[k].start, (size_t)len);
        t[len] = 0;
        unsigned char *ts = st ? xmalloc((size_t)len + 4) : NULL;
        if (ts) memcpy(ts, st + rows[k].start, (size_t)len), ts[len] = 0;
        if (k == shown - 1 && shown < nr) { /* cut: "…" after what fits */
            float avail = w - (k ? 0 : indent);
            memcpy(t + len, "\xE2\x80\xA6", 4);
            if (ts) memset(ts + len, UI_PLAIN, 3), ts[len + 3] = 0;
            while (len > 0 && ui_rich_w(sv_font(f), sv_bold(f), t, ts, len + 3, PX(css)) > avail) {
                int l = len - 1;
                while (l > 0 && ((unsigned char)t[l] & 0xC0) == 0x80) l--;
                memmove(t + l, t + len, 4);
                if (ts) memmove(ts + l, ts + len, 4);
                len = l;
            }
        }
        it->s = t;
        it->st = ts;
    }
    free(rows);
    if (md) free(plain), free(st);
    return (float)shown * lh;
}

static float wrap(Doc *d, int f, float css, Color c, float x, float y, float w, float lh, float indent, const char *s,
                  int maxl) {
    return wrap_md(d, f, css, c, c, x, y, w, lh, indent, s, maxl, false);
}

typedef struct {
    int f;
    float css;
    Color c;
    const char *s;
} Run;

/* Runs on one line from x, cut with "…" at w. Returns the width used. */
static float runs_line(Doc *d, float x, float y, float w, float lh, const Run *r, int n) {
    float cx = x;
    for (int i = 0; i < n; i++) {
        if (!r[i].s || !*r[i].s) continue;
        float tw = sv_tw(r[i].f, r[i].s, r[i].css);
        if (cx + tw > x + w) {
            char fitb[1024];
            sv_fit(r[i].f, fitb, sizeof fitb, r[i].s, r[i].css, x + w - cx);
            cx += text(d, r[i].f, r[i].css, 0, r[i].c, cx, y, lh, fitb);
            break;
        }
        cx += text(d, r[i].f, r[i].css, 0, r[i].c, cx, y, lh, r[i].s);
    }
    return cx - x;
}

/* Inline groups that wrap as a whole (flex-wrap). */
typedef struct {
    Doc *d;
    float x0, w, x, y, lh, gx, gy;
} Flow;

static void flow_place(Flow *f, float gw) {
    if (f->x > f->x0 && f->x + gw > f->x0 + f->w) f->x = f->x0, f->y += f->lh + f->gy;
}

static void flow_runs(Flow *f, const Run *r, int n, const char *title) {
    float gw = 0;
    for (int i = 0; i < n; i++) gw += sv_tw(r[i].f, r[i].s, r[i].css);
    flow_place(f, gw);
    if (title) tip(f->d, f->x, f->y, fminf(gw, f->w), f->lh, title);
    if (f->x + gw > f->x0 + f->w) { /* wider than a row: cut with "…" */
        f->x += runs_line(f->d, f->x, f->y, f->x0 + f->w - f->x, f->lh, r, n) + f->gx;
        return;
    }
    for (int i = 0; i < n; i++) f->x += text(f->d, r[i].f, r[i].css, 0, r[i].c, f->x, f->y, f->lh, r[i].s);
    f->x += f->gx;
}

static void flow_text(Flow *f, int font, float css, Color c, const char *s) {
    Run r = {font, css, c, s};
    flow_runs(f, &r, 1, NULL);
}

/* A chip: text in an outlined (or filled) small box. */
static void flow_chip(Flow *f, const char *s, int font, float css, Color fg, Color border, Color fill, const char *title) {
    float tw = sv_tws(font, s, css, 0), w = tw + 10, h = css * 1.5f;
    flow_place(f, w);
    float y = f->y + (f->lh - h) / 2;
    box(f->d, f->x, y, w, h, fill, border, 3);
    text(f->d, font, css, 0, fg, f->x + 5, y, h, s);
    if (title) tip(f->d, f->x, y, w, h, title);
    f->x += w + f->gx;
}

static void flag_chip(Flow *f, const char *s, Color c, bool filled, const char *title) {
    flow_chip(f, s, F_MONO_B, 10.5f, filled ? C_BG : c, c, filled ? c : BLANK, title);
}

static float flow_end(Flow *f) { return f->y + f->lh; }

static void upper(char *out, size_t n, const char *s) {
    snprintf(out, n, "%s", s);
    for (char *p = out; *p; p++) *p = (char)toupper((unsigned char)*p);
}

static void h3(Doc *d, float x, float *y, const char *s) {
    char u[256], fit[300];
    upper(u, sizeof u, s);
    ui_fit_font(sv_font(F_SANS_B), fit, sizeof fit, u, PX(11), ui_spacing(sv_font(F_SANS_B), PX(11)) + 0.9f, d->w - x);
    text(d, F_SANS_B, 11, 0.9f, C_FAINT, x, *y, 16.5f, fit);
    *y += 16.5f + 6;
}

static void big(Doc *d, float x, float *y, float w, const char *s) {
    *y += wrap(d, F_SANS_B, 13.5f, C_FG, x, *y, w, 20, 0, s, 0);
}

static void more(Doc *d, float x, float *y, float w, const char *s) {
    *y += 4;
    *y += wrap(d, F_MONO, 12, C_FAINT, x, *y, w, 18, 0, s, 0);
}

static void empty(Doc *d, float x, float *y, float w, const char *s) {
    *y += wrap(d, F_SANS, 13, C_FAINT, x, *y, w, 19.5f, 0, s, 0);
}

/* A hoverable row: hover background bg, its hl texts turn fg; a click opens node or turn. */
static int row_item(Doc *d, int kind, float x, float y, float w, float h, int node, int turn, Color bg, Color fg) {
    IItem *it = add(d, kind);
    it->x = x, it->y = y, it->w = w, it->h = h;
    it->node = node, it->turn = turn;
    it->c = bg, it->c2 = fg;
    return d->n - 1;
}

static void mark_hl(Doc *d, int from) {
    for (int k = from; k < d->n; k++)
        if (d->it[k].k == IT_TEXT) d->it[k].hl = true;
}

/* A .list button: dot, name and context, a value on the right. Rows with no
 * node (outside the tree) highlight too but don't open. */
static void list_row(Doc *d, float x, float *y, float w, int node, bool has_sw, Color sw, const Run *left, int nl,
                     const char *right) {
    float h = 12.5f * 1.5f + 6;
    row_item(d, IT_LINK, x, *y, w, h, node, -1, C_RAISED, BLANK);
    float rw = right ? sv_tw(F_MONO, right, 12.5f) : 0, lx = x + 6;
    if (has_sw) dot(d, lx + 4, *y + h / 2, 4, sw), lx += 15;
    runs_line(d, lx, *y + 3, x + w - 6 - rw - 10 - lx, h - 6, left, nl);
    if (right) text(d, F_MONO, 12.5f, 0, C_DIM, x + w - 6 - rw, *y + 3, h - 6, right);
    *y += h + 2;
}

/* A .pairs row: bold words on the left (cut), a faint value on the right. */
static void pair_row(Doc *d, float x, float *y, float w, const Run *left, int nl, const char *right) {
    float lh = 12.5f * 1.5f, rw = right ? sv_tw(F_MONO, right, 11.5f) : 0;
    runs_line(d, x, *y, w - rw - 10, lh, left, nl);
    if (right) text(d, F_MONO, 11.5f, 0, C_FAINT, x + w - rw, *y, lh, right);
    *y += lh + 4;
}

/* A .legend row: swatch, words (cut), an optional chip right-aligned before
 * the count. turn: the strips it singles out (INT_MIN none). */
static void legend_row(Doc *d, float x, float *y, float w, Color sw, const char *s, const char *chip, const char *em,
                       int turn) {
    float h = 12.5f * 1.5f + 2;
    int r = row_item(d, IT_LEGEND, x, *y, w, h, -1, -1, C_RAISED, C_FG);
    d->it[r].aux = turn;
    box(d, x + 4, *y + h / 2 - 5, 10, 10, sw, BLANK, 2);
    float ew = em ? sv_tw(F_MONO, em, 11.5f) : 0, cw = chip ? sv_tw(F_MONO_B, chip, 10) + 8 : 0;
    float emx = x + w - 2 - ew, cx = chip ? emx - (em ? 8 : 0) - cw : emx;
    char fitb[512];
    sv_fit(F_SANS, fitb, sizeof fitb, s, 12.5f, cx - 8 - (x + 22));
    int t0 = d->n;
    text(d, F_SANS, 12.5f, 0, C_DIM, x + 22, *y + 1, h - 2, fitb);
    mark_hl(d, t0);
    if (chip) {
        float ch = 15;
        box(d, cx, *y + (h - ch) / 2, cw, ch, BLANK, C_LINE, 3);
        text(d, F_MONO_B, 10, 0, C_FAINT, cx + 4, *y + (h - ch) / 2, ch, chip);
    }
    if (em) text(d, F_MONO, 11.5f, 0, C_FAINT, emx, *y + 1, h - 2, em);
    *y += h + 1;
}

/* An agent's comment as the draft's .note box (NoteView), its caption inside;
 * earlier: dashed and dim. who NULL: no caption. */
static void note(Doc *d, float x, float *y, float w, const char *who, const char *s, bool earlier) {
    if (d->nnv == d->capnv) {
        d->capnv = d->capnv ? d->capnv * 2 : 8;
        d->nv = xrealloc(d->nv, sizeof *d->nv * (size_t)d->capnv);
        d->nvs = xrealloc(d->nvs, sizeof *d->nvs * (size_t)d->capnv);
    }
    NoteView *nv = &d->nv[d->nnv];
    memset(nv, 0, sizeof *nv);
    ui_nv_set(nv, &s, 1);
    IItem *it = add(d, IT_NOTE);
    if (who) {
        char u[256];
        upper(u, sizeof u, who);
        it->s = xstrdup(u);
    }
    d->nvs[d->nnv] = (UiNoteStyle){.bg = earlier ? BLANK : C_RAISED, .border = C_LINE, .dashed = earlier,
                                   .text = earlier ? C_DIM : C_SOFT, .bold = earlier ? C_DIM : C_SOFT,
                                   .head_color = C_FAINT, .size = PX(13), .radius = 6, .pad = 7, .pad_x = 9,
                                   .head = it->s, .head_size = PX(10.5f), .head_h = 17};
    it->x = x, it->y = *y, it->w = w, it->h = ui_nv_boxes_h(nv, w, &d->nvs[d->nnv]);
    it->aux = d->nnv++;
    *y += it->h;
}

static void stack_bar(Doc *d, float x, float *y, float w, const int *v, const Color *c, char *const *tips, int n) {
    long total = 0;
    for (int i = 0; i < n; i++) total += v[i];
    *y += 6;
    box(d, x, *y, w, 10, C_BG, BLANK, 3);
    float cx = x;
    for (int i = 0; i < n && total; i++) {
        float sw = w * (float)v[i] / (float)total;
        if (sw > 0) box(d, cx, *y, sw, 10, c[i], BLANK, 0);
        if (sw > 0 && tips && tips[i]) tip(d, cx, *y, sw, 10, tips[i]);
        cx += sw;
    }
    *y += 10 + 8;
}

/* ---- sections ------------------------------------------------------------- */

static const char *rel_of(const char *id) {
    const char *s = strchr(id, '/');
    return s ? s + 1 : "";
}

static const char *turn_title(const AnSnapshot *S, int t) {
    return t >= 0 && t < S->nturns && S->turns[t].title ? S->turns[t].title : "";
}

static void header(Doc *d, const StMetrics *m, int node, float x, float *y, float w) {
    const StNode *n = &m->nodes[node];
    char u[32];
    upper(u, sizeof u, sv_kind_word(n->kind));
    float cw = sv_tws(F_MONO_B, u, 10.5f, 0.6f) + 10, ch = 16;
    box(d, x, *y + (24 - ch) / 2 + 1, cw, ch, BLANK, C_LINE, 3);
    text(d, F_MONO_B, 10.5f, 0.6f, C_DIM, x + 5, *y + (24 - ch) / 2 + 1, ch, u);
    *y += wrap(d, F_MONO_B, 16, C_FG, x, *y, w, 24, cw + 6, n->name, 0);
    char path[2048] = "";
    int chain[64], nc = 0;
    for (int p = n->parent; p > 0 && nc < 64; p = m->nodes[p].parent) chain[nc++] = p; /* below the root */
    for (int k = nc - 1; k >= 0; k--) {
        size_t l = strlen(path);
        snprintf(path + l, sizeof path - l, "%s%s", k == nc - 1 ? "" : " / ", m->nodes[chain[k]].name);
    }
    if (*path) *y += wrap(d, F_MONO, 12, C_FAINT, x, *y, w, 18, 0, path, 0);
}

static void stats(Doc *d, const StNode *n, float x, float *y, float w) {
    Flow f = {d, x, w, x, *y, 12.5f * 1.5f, 14, 4};
    char a[64], b[64];
    snprintf(a, sizeof a, "%d", n->turns);
    snprintf(b, sizeof b, " turn%s", n->turns == 1 ? "" : "s");
    flow_runs(&f, (Run[]){{F_MONO_B, 12.5f, C_FG, a}, {F_MONO, 12.5f, C_DIM, b}}, 2, NULL);
    snprintf(a, sizeof a, "%d", n->edits);
    flow_runs(&f, (Run[]){{F_MONO_B, 12.5f, C_FG, a}, {F_MONO, 12.5f, C_DIM, " changes"}}, 2, NULL);
    snprintf(a, sizeof a, "+%d", n->add);
    flow_text(&f, F_MONO, 12.5f, C_ADD, a);
    snprintf(a, sizeof a, "\xE2\x88\x92%d", n->del);
    flow_text(&f, F_MONO, 12.5f, C_DEL, a);
    snprintf(a, sizeof a, "%d lines", n->lines);
    flow_text(&f, F_MONO, 12.5f, C_DIM, a);
    if (sv_is_def(n)) {
        snprintf(a, sizeof a, "cx %d \xC2\xB7 depth %d", n->cx, n->nest);
        bool high = n->cx >= CX_HIGH;
        flow_chip(&f, a, F_MONO, 12.5f, high ? C_HOT : C_FG, high ? C_HOT : C_LINE, BLANK, "branch points; nesting depth");
    } else if (n->cx) {
        snprintf(a, sizeof a, "cx %d", n->cx);
        flow_runs(&f, (Run[]){{F_MONO, 12.5f, C_DIM, a}}, 1, "sum of its functions' branch points");
    }
    *y = flow_end(&f);
}

static void flag_row(Doc *d, float x, float *y, float w, Color c, const char *t, const char *small) {
    dot(d, x + 4.5f, *y + 9.5f, 4.5f, c);
    *y += wrap(d, F_SANS, 13, C_SOFT, x + 18, *y, w - 18, 19.5f, 0, t, 0);
    if (small && *small) *y += wrap(d, F_SANS, 12, C_DIM, x + 18, *y, w - 18, 18, 0, small, 0);
    *y += 6;
}

static void flags(Doc *d, const StMetrics *m, const StNode *n, float x, float *y, float w) {
    const AnSnapshot *S = m->snap;
    if (!n->flags.corrected && !n->flags.interrupted && !n->flags.failing && !n->flags.untested && !n->flags.blind &&
        !n->flags.slow)
        return;
    h3(d, x, y, "Flags");
    char t[512], p[64], sm[1200];
    if (n->flags.corrected) {
        sv_plural(p, sizeof p, n->flags.corrected, "turn");
        snprintf(t, sizeof t, "%s followed by a prompt that reads like a correction", p);
        sm[0] = 0;
        for (int k = 0; k < n->ncorr_turns && k < 2; k++) {
            const char *c = S->turns[n->corr_turns[k]].corr;
            size_t l = strlen(sm);
            snprintf(sm + l, sizeof sm - l, "%s\xE2\x80\x9C%s\xE2\x80\x9D", k ? "\n" : "", c ? c : "");
        }
        flag_row(d, x, y, w, C_CORR, t, sm);
    }
    if (n->flags.interrupted) {
        sv_plural(p, sizeof p, n->flags.interrupted, "turn");
        snprintf(t, sizeof t, "%s you interrupted", p);
        flag_row(d, x, y, w, C_CORR, t, NULL);
    }
    if (n->flags.failing) {
        sv_plural(p, sizeof p, n->flags.failing, "change");
        snprintf(t, sizeof t, "%s where the last build/test after it failed", p);
        flag_row(d, x, y, w, C_TEST, t, NULL);
    }
    if (n->flags.untested) {
        sv_plural(p, sizeof p, n->flags.untested, "change");
        snprintf(t, sizeof t, "%s with no build or test after it in the turn", p);
        flag_row(d, x, y, w, C_TEST, t, NULL);
    }
    if (n->flags.blind) {
        sv_plural(p, sizeof p, n->flags.blind, "change");
        snprintf(t, sizeof t, "%s made without reading that code first", p);
        flag_row(d, x, y, w, C_BLIND, t, "No Read, cat or sed -n of it in the agent's context.");
    }
    if (n->flags.slow) {
        char a[64], b[64];
        if (n->kind == AN_DIR) {
            sv_plural(p, sizeof p, n->flags.slow, "file");
            snprintf(t, sizeof t, "%s that took over %d\xC3\x97 the average file's time per turn", p, ST_SLOW_X);
            flag_row(d, x, y, w, C_SLOW, t, NULL);
        } else {
            double avg = m->avg_time[ST_LV_FILE];
            sv_fmt_dur(a, sizeof a, sv_jfix(n->per_turn, 0));
            snprintf(t, sizeof t, "Slow: %s per turn, %.1f\xC3\x97 the average file", a,
                     sv_jfix(avg > 0 ? n->per_turn / avg : 0, 1));
            sv_fmt_dur(b, sizeof b, sv_jfix(avg, 0));
            snprintf(sm, sizeof sm, "Average %s: agent time on its changes, per turn that changed it.", b);
            flag_row(d, x, y, w, C_SLOW, t, sm);
        }
    }
    *y -= 6;
}

typedef struct {
    const StMetrics *m;
    int mode;
} HotCtx;

static int cmp_hot(const void *a, const void *b, void *ud) {
    const HotCtx *c = ud;
    const StNode *x = &c->m->nodes[*(const int *)a], *y = &c->m->nodes[*(const int *)b];
    double d = y->metric[c->mode] - x->metric[c->mode];
    if (d) return d > 0 ? 1 : -1;
    if (y->turns != x->turns) return y->turns - x->turns;
    return *(const int *)a - *(const int *)b; /* stable */
}

static void hottest(Doc *d, const Color *fill, int mode, const StNode *n, float x, float *y, float w, const char *title) {
    const StMetrics *m = d->m;
    int *k = xmalloc(sizeof *k * (size_t)(n->nkids ? n->nkids : 1)), nk = 0;
    for (int i = 0; i < n->nkids; i++)
        if (m->nodes[n->kids[i]].turns) k[nk++] = n->kids[i];
    HotCtx c = {m, mode};
    if (nk) qsort_r(k, (size_t)nk, sizeof *k, cmp_hot, &c);
    if (nk) {
        h3(d, x, y, title);
        for (int i = 0; i < nk && i < 8; i++) {
            char word[128];
            sv_metric_word(m, &m->nodes[k[i]], mode, word, sizeof word);
            Run r = {F_MONO, 12.5f, C_FG, m->nodes[k[i]].name};
            list_row(d, x, y, w, k[i], true, fill[k[i]], &r, 1, word);
        }
        *y -= 2;
    }
    free(k);
}

static void trend(Doc *d, const StNode *n, int node, float x, float *y, float w) {
    if (n->ntrend < 2) return;
    bool def = sv_is_def(n);
    h3(d, x, y, def ? "Size and complexity after each change" : "Size after each change");
    IItem *it = add(d, IT_SPARK);
    it->x = x, it->y = *y, it->w = w, it->h = 64, it->node = node;
    *y += 64 + 4;
    const StPoint *f = &n->trend[0], *l = &n->trend[n->ntrend - 1];
    Flow fl = {d, x, w, x, *y, 18, 12, 4};
    char a[64], b[64], c[64];
    snprintf(a, sizeof a, "%d \xE2\x86\x92 %d", f->lines, l->lines);
    flow_place(&fl, 17 + sv_tw(F_MONO, "lines ", 12) + sv_tw(F_MONO_B, a, 12));
    box(d, fl.x, fl.y + 8, 12, 2, C_HOT, BLANK, 0);
    fl.x += 17;
    flow_runs(&fl, (Run[]){{F_MONO, 12, C_DIM, "lines "}, {F_MONO_B, 12, C_FG, a}}, 2, NULL);
    if (def) {
        snprintf(b, sizeof b, "%d \xE2\x86\x92 %d", f->cx, l->cx);
        flow_place(&fl, 17 + sv_tw(F_MONO, "cx ", 12) + sv_tw(F_MONO_B, b, 12));
        box(d, fl.x, fl.y + 8, 12, 2, C_CXLINE, BLANK, 0);
        fl.x += 17;
        flow_runs(&fl, (Run[]){{F_MONO, 12, C_DIM, "cx "}, {F_MONO_B, 12, C_FG, b}}, 2, NULL);
    }
    sv_plural(c, sizeof c, n->ntrend - (l->cid < 0 ? 1 : 0), "change");
    flow_text(&fl, F_MONO, 12, C_DIM, c);
    if (n->growing) flag_chip(&fl, "growing", C_HOT, false, NULL);
    *y = flow_end(&fl);
}

static void fixes(Doc *d, const StNode *n, float x, float *y, float w) {
    if (!n->fix_turns) return;
    const AnSnapshot *S = d->m->snap;
    h3(d, x, y, "Fixes");
    char t[256], p[64];
    sv_plural(p, sizeof p, n->turns, "turn");
    snprintf(t, sizeof t, "%d of %s that changed it %s", n->fix_turns, p, n->fix_turns == 1 ? "was a fix" : "were fixes");
    big(d, x, y, w, t);
    *y += 6;
    int shown = 0;
    for (int i = 0; i < n->nrows && shown < 4; i++) {
        int tt = n->rows[i].turn;
        if (!S->turns[tt].fix) continue;
        char wh[64];
        sv_when(wh, sizeof wh, S->turns[tt].ts);
        Run r = {F_SANS, 12.5f, C_SOFT, turn_title(S, tt)};
        pair_row(d, x, y, w, &r, 1, wh);
        shown++;
    }
    *y -= 4;
}

static void rework(Doc *d, const StNode *n, float x, float *y, float w) {
    if (!n->add || !n->nrw_pairs) return;
    const AnSnapshot *S = d->m->snap;
    h3(d, x, y, "Rework");
    char t[256];
    snprintf(t, sizeof t, "%d%% of the lines written here were rewritten within %d turns", n->rework_pct, ST_REWORK_TURNS);
    big(d, x, y, w, t);
    snprintf(t, sizeof t, "%d of %d lines \xC2\xB7 %d in the same turn they were written", n->rework, n->add, n->rework_same);
    more(d, x, y, w, t);
    *y += 6;
    for (int i = 0; i < n->nrw_pairs; i++) {
        const StRwPair *p = &n->rw_pairs[i];
        char em[32];
        snprintf(em, sizeof em, "%d ln", p->n);
        if (p->to == p->by)
            pair_row(d, x, y, w,
                     (Run[]){{F_SANS, 12.5f, C_SOFT, turn_title(S, p->to)},
                             {F_SANS, 12.5f, C_DIM, " \xC2\xB7 rewrote its own lines"}},
                     2, em);
        else
            pair_row(d, x, y, w,
                     (Run[]){{F_SANS, 12.5f, C_SOFT, turn_title(S, p->to)},
                             {F_SANS, 12.5f, C_DIM, " \xE2\x86\x92 rewritten by "},
                             {F_SANS, 12.5f, C_SOFT, turn_title(S, p->by)}},
                     3, em);
    }
    *y -= 4;
}

/* "Read to change it", "What it read": files and how often, a click opens them. */
static void read_list(Doc *d, const StRead *rows, int n, const char *self, float x, float *y, float w) {
    const AnSnapshot *S = d->m->snap;
    for (int i = 0; i < n && i < ST_TOP_ROWS; i++) {
        const char *path = rows[i].path >= 0 && rows[i].path < S->npaths ? S->paths[rows[i].path] : "";
        const char *sl = strrchr(path, '/');
        char dir[1024] = "", name[512], k[32];
        snprintf(name, sizeof name, "%s", sl ? sl + 1 : path);
        if (sl) snprintf(dir, sizeof dir, " %.*s", (int)(sl - path), path);
        snprintf(k, sizeof k, "%d\xC3\x97", rows[i].n);
        bool itself = self && !strcmp(self, path);
        Run r[3] = {{F_MONO, 12.5f, C_FG, name}, {F_MONO, 12.5f, C_FAINT, dir},
                    {F_MONO, 12.5f, C_FAINT, itself ? " (itself)" : ""}};
        list_row(d, x, y, w, rows[i].node, false, BLANK, r, 3, k);
    }
    *y -= 2;
}

static void context(Doc *d, const StNode *n, float x, float *y, float w) {
    if (!n->ctx_turns) return;
    const StMetrics *m = d->m;
    h3(d, x, y, "Context cost");
    char t[256], p[64];
    snprintf(t, sizeof t, "%.1f reads and %.1f searches before touching it, per turn", sv_jfix(n->ctx_reads, 1),
             sv_jfix(n->ctx_searches, 1));
    big(d, x, y, w, t);
    sv_plural(p, sizeof p, n->ctx_turns, "turn");
    snprintf(t, sizeof t, "over %s: what the agent had read in the turn before its first change here", p);
    more(d, x, y, w, t);
    if (n->nctx_rows) {
        *y += 10;
        h3(d, x, y, "Read to change it");
        const char *self = n->kind == AN_DIR || n->file < 0 ? NULL : rel_of(m->nodes[n->file].id);
        read_list(d, n->ctx_rows, n->nctx_rows, self, x, y, w);
    }
}

static void partners(Doc *d, const StNode *n, float x, float *y, float w) {
    const StMetrics *m = d->m;
    bool lv = n->kind == AN_FILE || sv_is_def(n);
    if (!n->npartners) {
        if (lv) {
            h3(d, x, y, "Changes together with");
            empty(d, x, y, w, "Nothing changed with it in two or more turns.");
        }
        return;
    }
    h3(d, x, y, "Changes together with");
    for (int i = 0; i < n->npartners; i++) {
        const StPartner *p = &n->partners[i];
        const StNode *q = &m->nodes[p->node];
        const char *ctx = sv_is_def(q) && q->file >= 0 ? m->nodes[q->file].name : q->parent >= 0 ? m->nodes[q->parent].name : "";
        char c[512], right[96];
        snprintf(c, sizeof c, " %s", ctx);
        snprintf(right, sizeof right, "%d/%d turns (%d%% match)", p->shared, n->turns, p->pct);
        Run r[2] = {{F_MONO, 12.5f, C_FG, q->name}, {F_MONO, 12.5f, C_FAINT, c}};
        float before = *y;
        list_row(d, x, y, w, p->node, true, p->cross ? C_CORR : C_FOCUS, r, 2, right);
        if (p->cross) { /* "other file" / "other folder": after the name, where it fits */
            const char *cw = sv_is_def(n) ? "other file" : "other folder";
            float nx = x + 6 + 15 + sv_tw(F_MONO, q->name, 12.5f) + sv_tw(F_MONO, c, 12.5f) + 6;
            float chw = sv_tw(F_MONO, cw, 10.5f) + 8, lim = x + w - 6 - sv_tw(F_MONO, right, 12.5f) - 10;
            if (nx + chw < lim) {
                box(d, nx, before + 5, chw, 15, BLANK, C_CORR, 3);
                text(d, F_MONO, 10.5f, 0, C_CORR, nx + 4, before + 5, 15, cw);
            }
        }
    }
    *y -= 2;
}

static void cost(Doc *d, const StNode *n, float x, float *y, float w) {
    if (!n->tok[0] && !n->tok[1]) return;
    const StMetrics *m = d->m;
    h3(d, x, y, "Cost");
    char a[64], b[64], t[512], p[64];
    sv_fmt_k(a, sizeof a, n->tok[0]);
    sv_fmt_dur(b, sizeof b, (double)n->tok[3]);
    snprintf(t, sizeof t, "%s output tokens \xC2\xB7 %s of agent time", a, b);
    big(d, x, y, w, t);
    if (n->kind != AN_DIR && n->turns) {
        sv_fmt_dur(a, sizeof a, sv_jfix(n->per_turn, 0));
        sv_fmt_dur(b, sizeof b, sv_jfix(m->avg_time[n->level], 0));
        snprintf(t, sizeof t, "%s per turn \xC2\xB7 the average %s takes %s", a, n->level == ST_LV_DEF ? "function" : "file", b);
        more(d, x, y, w, t);
    }
    sv_fmt_k(a, sizeof a, n->tok[1]);
    sv_fmt_k(b, sizeof b, n->tok[2]);
    sv_plural(p, sizeof p, n->ncids, "change");
    snprintf(t, sizeof t, "%s new input \xC2\xB7 %s cached input \xC2\xB7 over %s: the agent's responses in each turn since its previous change", a, b, p);
    more(d, x, y, w, t);
}

static void owner(Doc *d, const StNode *n, float x, float *y, float w) {
    const StMetrics *m = d->m;
    long total = 0;
    for (int i = 0; i < n->nwho; i++) total += n->who[i].lines;
    if (!total) return;
    h3(d, x, y, "Who wrote today's lines");
    int *v = xmalloc(sizeof *v * (size_t)n->nwho);
    Color *c = xmalloc(sizeof *c * (size_t)n->nwho);
    char **names = xcalloc((size_t)n->nwho, sizeof *names);
    for (int i = 0; i < n->nwho; i++) {
        char nm[512];
        v[i] = n->who[i].lines, c[i] = sv_actor_col(st_actor_color(m, n->who[i].actor));
        sv_actor_name(m->snap, n->who[i].actor, false, nm, sizeof nm);
        names[i] = xstrdup(nm);
    }
    *y -= 6;
    stack_bar(d, x, y, w, v, c, names, n->nwho);
    int authors = 0;
    for (int i = 0; i < n->nwho; i++) {
        int a = n->who[i].actor;
        authors += a >= 0;
        if (i >= 6) continue;
        char pct[16];
        snprintf(pct, sizeof pct, "%.0f%%", sv_jfix(100.0 * n->who[i].lines / (double)total, 0));
        legend_row(d, x, y, w, c[i], names[i], a >= 0 ? (m->snap->actors[a].sub ? "subagent" : "session") : NULL, pct,
                   INT_MIN);
    }
    char t[128], p[64];
    sv_plural(p, sizeof p, authors, "author");
    snprintf(t, sizeof t, "%s: sessions and subagents", p);
    more(d, x, y, w, t);
    for (int i = 0; i < n->nwho; i++) free(names[i]);
    free(names);
    free(v);
    free(c);
}

static void age(Doc *d, const StNode *n, float x, float *y, float w) {
    static const char *const label[ST_NAGES] = {"< 1 h", "< 1 day", "< 1 week", "older", "before these transcripts"};
    static const unsigned col[ST_NAGES] = {0xf2a541, 0xc98a3d, 0x3b5a7a, 0x1e2b40, 0x2a3442};
    int total = 0;
    for (int i = 0; i < ST_NAGES; i++) total += n->ages[i];
    if (!total) return;
    h3(d, x, y, "Age of today's lines");
    char t[128], a[64];
    if (n->age_median < 0) snprintf(t, sizeof t, "Older than these transcripts");
    else sv_fmt_age(a, sizeof a, (double)n->age_median), snprintf(t, sizeof t, "Median %s old", a);
    big(d, x, y, w, t);
    int v[ST_NAGES], k = 0;
    Color c[ST_NAGES];
    char tipb[ST_NAGES][96], *tips[ST_NAGES];
    for (int i = 0; i < ST_NAGES; i++)
        if (n->ages[i]) {
            snprintf(tipb[k], sizeof tipb[k], "%s: %d lines", label[i], n->ages[i]);
            tips[k] = tipb[k];
            v[k] = n->ages[i], c[k++] = HEX(col[i]);
        }
    stack_bar(d, x, y, w, v, c, tips, k);
    for (int i = 0; i < ST_NAGES; i++) {
        if (!n->ages[i]) continue;
        char em[32];
        snprintf(em, sizeof em, "%d ln", n->ages[i]);
        legend_row(d, x, y, w, HEX(col[i]), label[i], NULL, em, INT_MIN);
    }
    sv_when(a, sizeof a, (double)d->m->now);
    snprintf(t, sizeof t, "as of the last change, %s", a);
    more(d, x, y, w, t);
}

/* Legend rows of a node's blame: newest turn first, unknown last. */
static void blame_legend(Doc *d, const StNode *n, float x, float *y, float w) {
    for (int i = 0; i < n->nblame; i++) {
        int t = n->blame[i].turn;
        char em[32];
        snprintf(em, sizeof em, "%d ln", n->blame[i].n);
        legend_row(d, x, y, w, t < 0 ? C_UNKNOWN : sv_blame_col(st_blame_slot(n, t)),
                   t < 0 ? "before these transcripts" : turn_title(d->m->snap, t), NULL, em, t);
    }
}

/* Today's code of a def (its first lines) or a file (its first MAXL_FILE),
 * a strip per line in the colour of the turn that wrote it, the blame card
 * under it. */
static void code(Doc *d, int node, float x, float *y, float w) {
    const StMetrics *m = d->m;
    const StNode *n = &m->nodes[node];
    if (n->file < 0) return;
    const StNode *f = &m->nodes[n->file];
    const AnNode *fa = &m->snap->nodes[f->an];
    if (!fa->src) return;
    int norig = 0, *orig = st_line_origins(m, node, &norig);
    if (!orig || !norig) { free(orig); return; }
    bool def = sv_is_def(n);
    int first = def ? n->a : 1, total = norig, cap = def ? MAXL_DEF : MAXL_FILE, shown = total > cap ? cap : total;
    ui_code_init(&d->cv, f->name, fa->src, first, first + shown - 1);
    if (!d->cv.n) { free(orig); ui_code_free(&d->cv); return; }
    d->orig = orig, d->norig = norig, d->line0 = first, d->code_node = node, d->cols = ui_code_cols(&d->cv);
    char t[128];
    snprintf(t, sizeof t, "Code \xC2\xB7 lines %d\xE2\x80\x93%d \xC2\xB7 who wrote each line", first, first + total - 1);
    h3(d, x, y, t);
    IItem *it = add(d, IT_CODE);
    it->x = x, it->y = *y, it->w = w, it->h = fminf(CODE_MAX_H, (float)d->cv.n * CODE_ROW + 12);
    *y += it->h;
    if (total > shown) {
        snprintf(t, sizeof t, "\xE2\x80\xA6 %d more lines", total - shown);
        more(d, x, y, w, t);
    }
    *y += 6;
    IItem *c = add(d, IT_CARD);
    c->x = x, c->y = *y, c->w = w, c->h = CARD_MIN;
    d->card = d->n - 1;
    *y += CARD_MIN;
}

static void turns(Doc *d, int node, bool notes, int maxt, float x, float *y, float w) {
    const StMetrics *m = d->m;
    const StNode *n = &m->nodes[node];
    const AnSnapshot *S = m->snap;
    if (!n->nrows) {
        h3(d, x, y, "Turns");
        empty(d, x, y, w, "No recorded change: it was only read, or changed before these transcripts.");
        return;
    }
    h3(d, x, y, "Turns that changed it \xC2\xB7 newest first");
    const AnNode *an = &S->nodes[n->an];
    for (int i = 0; i < n->nrows && i < maxt; i++) {
        const StNodeTurn *r = &n->rows[i];
        const AnTurn *T = &S->turns[r->turn];
        if (i) {
            IItem *l = add(d, IT_LINE);
            l->x = x, l->y = *y, l->w = w, l->c = C_LINE;
            *y += 10;
        }
        float ty = *y, aw = sv_tw(F_SANS_B, "\xE2\x80\xBA", 13.5f) + 8;
        int lk = row_item(d, IT_LINK, x, ty, w, 0, -1, r->turn, BLANK, C_FOCUS);
        text(d, F_SANS_B, 13.5f, 0, C_HOT, x, ty, 20, "\xE2\x80\xBA");
        int t0 = d->n;
        *y += wrap(d, F_SANS, 13.5f, C_FG, x, ty, w, 20, aw, turn_title(S, r->turn), 0);
        mark_hl(d, t0);
        d->it[lk].h = *y - ty;
        d->it[lk].tip = xstrdup("Everything this turn changed");
        *y += 6;
        /* meta: when, changes and tools, +/-, flag chips */
        char wh[64], ch[64], tools[256] = "", a[32], dl[32], meta[512];
        sv_when(wh, sizeof wh, T->ts);
        sv_plural(ch, sizeof ch, r->changes, "change");
        for (int k = 0; k < r->ntools; k++) {
            size_t l = strlen(tools);
            snprintf(tools + l, sizeof tools - l, "%s%s", k ? ", " : "", r->tools[k]);
        }
        snprintf(meta, sizeof meta, "%s \xC2\xB7 %s by %s \xC2\xB7 ", wh, ch, tools);
        snprintf(a, sizeof a, "+%d", r->add);
        snprintf(dl, sizeof dl, " \xE2\x88\x92%d", r->del);
        Flow f = {d, x, w, x, *y, 17.25f, 8, 4};
        flow_runs(&f, (Run[]){{F_MONO, 11.5f, C_FAINT, meta}, {F_MONO, 11.5f, C_ADD, a}, {F_MONO, 11.5f, C_DEL, dl}}, 3, NULL);
        char nt[600];
        snprintf(nt, sizeof nt, "Next prompt: %s", T->corr ? T->corr : "");
        if (T->fix) flag_chip(&f, "fix", C_CORR, true, "The prompt reports a problem");
        if (T->corr) flag_chip(&f, "corrected next", C_CORR, false, nt);
        if (T->intr) flag_chip(&f, "interrupted", C_CORR, false, NULL);
        if (r->flags & STT_UNTESTED) flag_chip(&f, "untested", C_TEST, false, NULL);
        if (r->flags & STT_FAILING) flag_chip(&f, "test failed after", C_TEST, false, NULL);
        if (r->flags & STT_BLIND) flag_chip(&f, "changed unread", C_BLIND, false, NULL);
        *y = flow_end(&f);
        if (notes)
            for (int k = 0; k < r->nnotes; k++) {
                int c = r->notes[k], ix = c >= 0 ? c : -1 - c;
                if (ix < 0 || ix >= S->ncomments) continue;
                const char *tool = "";
                for (int j = 0; j < an->nch; j++) {
                    const AnChange *C = &S->changes[an->ch[j].cid];
                    if (C->turn == r->turn && C->comment == c) { tool = C->tool ? C->tool : ""; break; }
                }
                char who[128];
                if (c < 0) snprintf(who, sizeof who, "earlier in the turn");
                else snprintf(who, sizeof who, "agent, before %s", tool);
                *y += 6;
                note(d, x, y, w, who, S->comments[ix], c < 0);
            }
        *y += 2;
    }
    if (n->nrows > maxt) {
        char t[64];
        snprintf(t, sizeof t, "\xE2\x80\xA6 %d older turns", n->nrows - maxt);
        more(d, x, y, w, t);
    }
}

#define SECTION(call)                                                                                                  \
    do {                                                                                                               \
        int n0_ = d->n;                                                                                                \
        call;                                                                                                          \
        if (d->n > n0_) y += GAP;                                                                                      \
    } while (0)

static void build_node(Doc *d, const Color *fill, int mode, int node, float x, float w) {
    const StNode *n = &d->m->nodes[node];
    float y = 0;
    SECTION(header(d, d->m, node, x, &y, w));
    SECTION(stats(d, n, x, &y, w));
    SECTION(flags(d, d->m, n, x, &y, w));
    if (sv_is_def(n)) {
        SECTION(code(d, node, x, &y, w));
        if (d->card >= 0) { /* the legend goes with the code */
            y -= GAP - 8;
            blame_legend(d, n, x, &y, w);
            y += GAP;
        }
        SECTION(trend(d, n, node, x, &y, w));
        SECTION(fixes(d, n, x, &y, w));
        SECTION(rework(d, n, x, &y, w));
        SECTION(context(d, n, x, &y, w));
        SECTION(partners(d, n, x, &y, w));
        SECTION(cost(d, n, x, &y, w));
        SECTION(owner(d, n, x, &y, w));
        SECTION(age(d, n, x, &y, w));
        if (n->nkids) SECTION(hottest(d, fill, mode, n, x, &y, w, n->kind == AN_CLASS ? "Methods" : "Nested functions"));
        SECTION(turns(d, node, true, 8, x, &y, w));
    } else if (n->kind == AN_FILE) {
        SECTION(hottest(d, fill, mode, n, x, &y, w, "Hottest functions"));
        SECTION(trend(d, n, node, x, &y, w));
        SECTION(fixes(d, n, x, &y, w));
        SECTION(rework(d, n, x, &y, w));
        SECTION(context(d, n, x, &y, w));
        SECTION(partners(d, n, x, &y, w));
        SECTION(cost(d, n, x, &y, w));
        SECTION(owner(d, n, x, &y, w));
        SECTION(age(d, n, x, &y, w));
        SECTION(code(d, node, x, &y, w));
        if (n->nblame) {
            char t[128];
            snprintf(t, sizeof t, "Today's %d lines by the turn that wrote them", n->lines);
            h3(d, x, &y, t);
            blame_legend(d, n, x, &y, w);
            y += GAP;
        }
        SECTION(turns(d, node, true, 6, x, &y, w));
    } else {
        SECTION(hottest(d, fill, mode, n, x, &y, w, "Hottest inside"));
        SECTION(fixes(d, n, x, &y, w));
        SECTION(rework(d, n, x, &y, w));
        SECTION(context(d, n, x, &y, w));
        SECTION(cost(d, n, x, &y, w));
        SECTION(owner(d, n, x, &y, w));
        SECTION(age(d, n, x, &y, w));
        SECTION(turns(d, node, false, 10, x, &y, w));
    }
    d->h = y;
}

static void touch_list(Doc *d, const StTouch *t, int n, int max, bool defs, float x, float *y, float w) {
    const StMetrics *m = d->m;
    for (int i = 0; i < n && i < max; i++) {
        const StNode *q = &m->nodes[t[i].node];
        int ctxn = defs ? q->file : q->parent;
        char c[512], right[64];
        snprintf(c, sizeof c, " %s", ctxn >= 0 ? m->nodes[ctxn].name : "");
        snprintf(right, sizeof right, "+%d \xE2\x88\x92%d", t[i].add, t[i].del);
        Run r[2] = {{F_MONO, 12.5f, C_FG, q->name}, {F_MONO, 12.5f, C_FAINT, c}};
        list_row(d, x, y, w, t[i].node, false, BLANK, r, 2, right);
    }
    *y -= 2;
}

static void build_turn(Doc *d, int t, float x, float w) {
    const StMetrics *m = d->m;
    const AnSnapshot *S = m->snap;
    const AnTurn *T = &S->turns[t];
    const StTurn *I = &m->turns[t];
    float y = 0;
    char a[128], b[200], c[256], buf[1024];
    /* header */
    float cw = sv_tws(F_MONO_B, "TURN", 10.5f, 0.6f) + 10;
    box(d, x, y + 5, cw, 16, BLANK, C_LINE, 3);
    text(d, F_MONO_B, 10.5f, 0.6f, C_DIM, x + 5, y + 5, 16, "TURN");
    y += wrap(d, F_MONO_B, 16, C_FG, x, y, w, 24, cw + 6, T->title ? T->title : "", 0);
    sv_when(a, sizeof a, T->ts);
    sv_actor_name(S, T->actor, false, b, sizeof b);
    snprintf(buf, sizeof buf, "%s \xC2\xB7 %d of %d \xC2\xB7 %s", a, I->rank + 1, m->nturns, b);
    y += wrap(d, F_MONO, 12, C_FAINT, x, y, w, 18, 0, buf, 0);
    y += GAP;
    /* stats */
    Flow f = {d, x, w, x, y, 18.75f, 14, 4};
    snprintf(a, sizeof a, "%d", I->ncids);
    flow_runs(&f, (Run[]){{F_MONO_B, 12.5f, C_FG, a}, {F_MONO, 12.5f, C_DIM, " changes"}}, 2, NULL);
    snprintf(a, sizeof a, "%d", I->nfiles);
    flow_runs(&f, (Run[]){{F_MONO_B, 12.5f, C_FG, a}, {F_MONO, 12.5f, C_DIM, " files"}}, 2, NULL);
    snprintf(a, sizeof a, "%d", I->ndefs);
    flow_runs(&f, (Run[]){{F_MONO_B, 12.5f, C_FG, a}, {F_MONO, 12.5f, C_DIM, " functions"}}, 2, NULL);
    snprintf(a, sizeof a, "+%d", I->add);
    flow_text(&f, F_MONO, 12.5f, C_ADD, a);
    snprintf(a, sizeof a, "\xE2\x88\x92%d", I->del);
    flow_text(&f, F_MONO, 12.5f, C_DEL, a);
    sv_fmt_dur(a, sizeof a, (double)T->secs);
    sv_fmt_dur(b, sizeof b, sv_jfix(m->turn_avg, 0));
    snprintf(c, sizeof c, " (avg %s)", b);
    flow_runs(&f, (Run[]){{F_MONO_B, 12.5f, C_FG, a}, {F_MONO, 12.5f, C_DIM, c}}, 2,
              "From the prompt to the agent's last response");
    y = flow_end(&f) + GAP;
    /* chips */
    Flow g = {d, x, w, x, y, 18, 6, 6};
    int n0 = d->n;
    if (T->fix) flag_chip(&g, "fix", C_CORR, true, NULL);
    if (T->corr) flag_chip(&g, "corrected next", C_CORR, false, NULL);
    if (T->intr) flag_chip(&g, "interrupted", C_CORR, false, NULL);
    if (I->failing) {
        sv_plural(a, sizeof a, I->failing, "change");
        snprintf(b, sizeof b, "%s failing after", a);
        flag_chip(&g, b, C_TEST, false, NULL);
    }
    if (I->slow) {
        snprintf(b, sizeof b, "slow: %.1f\xC3\x97 the average turn", sv_jfix(I->slow_x, 1));
        flag_chip(&g, b, C_SLOW, false, NULL);
    }
    if (I->untested) {
        sv_plural(a, sizeof a, I->untested, "change");
        snprintf(b, sizeof b, "%s untested", a);
        flag_chip(&g, b, C_TEST, false, NULL);
    }
    if (I->tested) flag_chip(&g, "tested after", C_ADD, false, NULL);
    if (I->blind) flag_chip(&g, "changed unread", C_BLIND, false, NULL);
    if (d->n > n0) y = flow_end(&g) + GAP;
    if (T->corr) {
        h3(d, x, &y, "Your next prompt");
        note(d, x, &y, w, NULL, T->corr, false);
        y += GAP;
    }
    h3(d, x, &y, "Survives today");
    snprintf(buf, sizeof buf, "%d%% of the lines it wrote are still in the code", I->survival_pct);
    big(d, x, &y, w, buf);
    snprintf(buf, sizeof buf, "%d of %d lines", I->kept, I->add);
    more(d, x, &y, w, buf);
    y += GAP;
    h3(d, x, &y, "Cost");
    sv_fmt_k(a, sizeof a, I->tok[0]);
    snprintf(buf, sizeof buf, "%s output tokens in this turn", a);
    big(d, x, &y, w, buf);
    sv_fmt_k(a, sizeof a, I->tok[1]);
    sv_fmt_k(b, sizeof b, I->tok[2]);
    sv_plural(c, sizeof c, I->nagents, "agent");
    snprintf(buf, sizeof buf, "%s new input \xC2\xB7 %s cached input \xC2\xB7 %d%% led up to these changes \xC2\xB7 %s wrote them%s", a,
             b, I->led_pct, c, I->subagents ? " (with subagents)" : "");
    more(d, x, &y, w, buf);
    y += GAP;
    h3(d, x, &y, "Files it changed");
    touch_list(d, I->files, I->nfiles, INT_MAX, false, x, &y, w);
    y += GAP;
    if (I->ndefs) {
        h3(d, x, &y, "Functions it changed");
        touch_list(d, I->defs, I->ndefs, 12, true, x, &y, w);
        if (I->ndefs > 12) {
            snprintf(buf, sizeof buf, "\xE2\x80\xA6 %d more", I->ndefs - 12);
            more(d, x, &y, w, buf);
        }
        y += GAP;
    }
    if (I->nreads) {
        h3(d, x, &y, "What it read");
        read_list(d, I->reads, I->nreads, NULL, x, &y, w);
        y += GAP;
    }
    if (I->nnotes) { /* a .turn block: a line above it */
        IItem *l = add(d, IT_LINE);
        l->x = x, l->y = y - 6, l->w = w, l->c = C_LINE;
        y += 4;
        h3(d, x, &y, "What the agent said, in order");
        for (int k = 0; k < I->nnotes && k < 8; k++) {
            int cm = I->notes[k], ix = cm >= 0 ? cm : -1 - cm;
            if (ix < 0 || ix >= S->ncomments) continue;
            const char *tool = "";
            for (int j = 0; j < I->ncids; j++)
                if (S->changes[I->cids[j]].comment == cm) { tool = S->changes[I->cids[j]].tool; break; }
            char who[128];
            if (cm < 0) snprintf(who, sizeof who, "earlier in the turn");
            else snprintf(who, sizeof who, "agent, before %s", tool ? tool : "");
            if (k) y += 6;
            note(d, x, &y, w, who, S->comments[ix], cm < 0);
        }
        if (I->nnotes > 8) {
            snprintf(buf, sizeof buf, "\xE2\x80\xA6 %d more", I->nnotes - 8);
            more(d, x, &y, w, buf);
        }
        y += GAP;
    }
    d->h = y;
}

void si_show(Insp *in, const StMetrics *m, const Color *fill, int mode, int node, int turn, float w) {
    Doc *d = &in->doc;
    if (d->m == m && d->node == node && d->turn == turn && d->w == w && (d->mode == mode || turn >= 0)) return;
    char key[512];
    if (turn >= 0) snprintf(key, sizeof key, "t:%.3f:%s", m->snap->turns[turn].ts, turn_title(m->snap, turn));
    else if (node >= 0) snprintf(key, sizeof key, "n:%s:%d:%d", m->nodes[node].id, m->nodes[node].a, m->nodes[node].b);
    else key[0] = 0;
    float keep_scroll = in->scroll, keep_code = d->code_scroll;
    bool same = !strcmp(key, in->key);
    doc_reset(d);
    d->m = m, d->node = node, d->turn = turn, d->mode = mode, d->w = w;
    snprintf(in->key, sizeof in->key, "%s", key);
    in->scroll = same ? keep_scroll : 0;
    in->card_line = -1;
    doc_reset(&in->card);
    in->card.w = -1;
    if (turn >= 0) build_turn(d, turn, 0, w);
    else if (node >= 0) build_node(d, fill, mode, node, 0, w);
    if (same) d->code_scroll = keep_code;
}

/* ---- drawing ---------------------------------------------------------------- */

static int line_turn(const Doc *d, int i) {
    int c = d->orig[i];
    return c >= 0 && c < d->m->snap->nchanges ? d->m->snap->changes[c].turn : -1;
}

/* The blame card for card_line, laid out into in->card. */
static void card_build(Insp *in, float w) {
    Doc *c = &in->card;
    const Doc *d = &in->doc;
    doc_reset(c);
    c->w = w;
    float y = 2;
    if (in->card_line < 0) {
        y += wrap(c, F_SANS, 12.5f, C_DIM, 12, y, w - 12, 18.75f, 0,
                  "Hover the strip beside the line numbers: the turn and the comment behind each line.", 0);
        c->h = y + 2;
        return;
    }
    const AnSnapshot *S = d->m->snap;
    int ln = d->line0 + in->card_line, cid = d->orig[in->card_line];
    char l1[64], buf[1024];
    snprintf(l1, sizeof l1, "Line %d", ln);
    float lw = text(c, F_SANS_B, 12.5f, 0, C_FG, 12, y, 18.75f, l1);
    if (cid < 0 || cid >= S->nchanges) {
        y += wrap(c, F_SANS, 12.5f, C_DIM, 12, y, w - 12, 18.75f, lw,
                  " \xC2\xB7 written before these transcripts (or by a change they don't show).", 0);
        c->h = y + 2;
        return;
    }
    const AnChange *C = &S->changes[cid];
    char wh[64], ag[64], who[256];
    sv_when(wh, sizeof wh, S->turns[C->turn].ts);
    sv_fmt_age(ag, sizeof ag, (double)(d->m->now - C->epoch));
    sv_actor_name(S, C->actor, true, who, sizeof who);
    snprintf(buf, sizeof buf, " \xC2\xB7 %s \xC2\xB7 %s (%s before the last change) \xC2\xB7 %s", C->tool ? C->tool : "", wh, ag, who);
    y += wrap(c, F_SANS, 12.5f, C_DIM, 12, y, w - 12, 18.75f, lw, buf, 0);
    y += wrap(c, F_SANS_B, 12.5f, C_FG, 12, y, w - 12, 18.75f, 0, turn_title(S, C->turn), 0);
    int ix = C->comment >= 0 ? C->comment : -1 - C->comment;
    if (ix >= 0 && ix < S->ncomments && S->comments[ix] && *S->comments[ix]) {
        snprintf(buf, sizeof buf, "%s%s", C->comment < 0 ? "(earlier in the turn) " : "", S->comments[ix]);
        y += 2 + wrap_md(c, F_SANS, 12.5f, C_SOFT, C_FG, 12, y + 2, w - 12, 18.75f, 0, buf, 5, true);
    }
    c->h = y + 2;
}

static float card_extra(const Insp *in) { return in->doc.card >= 0 ? fmaxf(0, in->card.h - CARD_MIN) : 0; }

static float item_y(const Insp *in, int i, float oy) {
    return in->doc.it[i].y + oy + (in->doc.card >= 0 && i > in->doc.card ? card_extra(in) : 0);
}

static Rectangle code_rect(const Insp *in, Rectangle r) {
    const Doc *d = &in->doc;
    for (int i = 0; i < d->n; i++)
        if (d->it[i].k == IT_CODE) {
            const IItem *it = &d->it[i];
            return (Rectangle){r.x + 14 + it->x, r.y + 14 - in->scroll + it->y, it->w, it->h};
        }
    return (Rectangle){0};
}

static float gutter_w(const Doc *d) {
    char b[16];
    snprintf(b, sizeof b, "%d", d->line0 + d->cv.n - 1);
    return (float)strlen(b) * sv_tw(F_MONO, "M", 12) + 20;
}

static float max_hscroll(const Doc *d, float box_w) {
    return fmaxf(0, (float)d->cols * ui_code_cell() - (box_w - 6 - gutter_w(d) - 10 - 10));
}

static void spark(Insp *in, const IItem *it, float x, float y, Vector2 mp) {
    const StNode *n = &in->doc.m->nodes[it->node];
    const StPoint *p = n->trend;
    int np = n->ntrend;
    bool def = sv_is_def(n);
    float W = it->w, H = it->h, sy = H / 56;
    double lmin = p[0].lines, lmax = lmin, cmin = p[0].cx, cmax = cmin;
    for (int i = 1; i < np; i++) {
        lmin = fmin(lmin, p[i].lines), lmax = fmax(lmax, p[i].lines);
        cmin = fmin(cmin, p[i].cx), cmax = fmax(cmax, p[i].cx);
    }
    /* d3's nice() over 10 ticks */
    for (int k = 0; k < 2; k++) {
        double *lo = k ? &cmin : &lmin, *hi = k ? &cmax : &lmax, prev = 0;
        for (int it2 = 0; it2 < 10 && *hi > *lo; it2++) {
            double step = (*hi - *lo) / 10, pw = floor(log10(step)), err = step / pow(10, pw);
            double fct = err >= 7.0710678 ? 10 : err >= 3.1622777 ? 5 : err >= 1.4142136 ? 2 : 1;
            double inc = pw >= 0 ? fct * pow(10, pw) : -pow(10, -pw) / fct;
            if (inc == prev) break;
            if (inc > 0) *lo = floor(*lo / inc) * inc, *hi = ceil(*hi / inc) * inc;
            else *lo = ceil(*lo * inc) / inc, *hi = floor(*hi * inc) / inc;
            prev = inc;
        }
    }
    float x0 = 4 * W / 380, x1 = W - 4 * W / 380, bot = H - 4 * sy, top = 6 * sy;
#define SX(j) (x + (np > 1 ? x0 + (x1 - x0) * (float)(j) / (float)(np - 1) : W / 2))
#define SY(v, lo, hi) (y + ((hi) > (lo) ? bot + (float)(((v) - (lo)) / ((hi) - (lo))) * (top - bot) : (bot + top) / 2))
    DrawLineEx((Vector2){x, y + bot}, (Vector2){x + W, y + bot}, 1, C_LINE);
    if (def && cmax > cmin)
        for (int j = 1; j < np; j++) {
            Vector2 a = {SX(j - 1), SY(p[j - 1].cx, cmin, cmax)}, b = {SX(j), SY(p[j].cx, cmin, cmax)};
            float len = hypotf(b.x - a.x, b.y - a.y);
            for (float t = 0; t < len; t += 6) { /* dashed 3 3 */
                float t1 = fminf(len, t + 3);
                DrawLineEx((Vector2){a.x + (b.x - a.x) * t / len, a.y + (b.y - a.y) * t / len},
                           (Vector2){a.x + (b.x - a.x) * t1 / len, a.y + (b.y - a.y) * t1 / len}, 1.5f, C_CXLINE);
            }
        }
    for (int j = 1; j < np; j++)
        DrawLineEx((Vector2){SX(j - 1), SY(p[j - 1].lines, lmin, lmax)}, (Vector2){SX(j), SY(p[j].lines, lmin, lmax)}, 2, C_HOT);
    int hot = -1;
    for (int j = 0; j < np; j++) {
        Vector2 c = {SX(j), SY(p[j].lines, lmin, lmax)};
        DrawCircleV(c, 2.6f, C_HOT);
        if (hypotf(mp.x - c.x, mp.y - c.y) < 5) hot = j;
    }
#undef SX
#undef SY
    if (hot >= 0) { /* the dot's title */
        static char b[512];
        const AnSnapshot *S = in->doc.m->snap;
        const char *lbl = p[hot].cid < 0 ? "now" : turn_title(S, S->changes[p[hot].cid].turn);
        if (def) snprintf(b, sizeof b, "%s: %d lines, cx %d", lbl, p[hot].lines, p[hot].cx);
        else snprintf(b, sizeof b, "%s: %d lines", lbl, p[hot].lines);
        in->tip = b;
        in->tip_at = mp;
    }
}

static void draw_code(Insp *in, const IItem *it, float x, float y, Rectangle clip, Vector2 mp) {
    Doc *d = &in->doc;
    Rectangle b = {x, y, it->w, it->h};
    DrawRectangleRounded(b, 5 / fminf(b.width, b.height), 6, C_BG);
    DrawRectangleRoundedLinesEx(b, 5 / fminf(b.width, b.height), 6, 1, C_LINE);
    float ix0 = fmaxf(clip.x, b.x + 1), iy0 = fmaxf(clip.y, b.y + 1);
    float ix1 = fminf(clip.x + clip.width, b.x + b.width - 1), iy1 = fminf(clip.y + clip.height, b.y + b.height - 1);
    if (ix1 <= ix0 || iy1 <= iy0) return;
    BeginScissorMode((int)ix0, (int)iy0, (int)(ix1 - ix0), (int)(iy1 - iy0));
    float max_scroll = fmaxf(0, (float)d->cv.n * CODE_ROW + 12 - it->h), mh = max_hscroll(d, b.width);
    d->code_scroll = fmaxf(0, fminf(max_scroll, d->code_scroll));
    d->code_hscroll = fmaxf(0, fminf(mh, d->code_hscroll));
    float gw = gutter_w(d), sx = b.x + 6 + gw + 10;
    const StNode *n = &d->m->nodes[d->code_node];
    int on_c = in->strip_line >= 0 ? d->orig[in->strip_line] : INT_MIN;
    int hot = in->nvim ? (int)((mp.y - b.y - 6 + d->code_scroll) / CODE_ROW) : -1; /* a click opens it in nvim */
    for (int i = (int)(d->code_scroll / CODE_ROW); i < d->cv.n; i++) {
        float ry = b.y + 6 + (float)i * CODE_ROW - d->code_scroll;
        if (ry > b.y + b.height) break;
        if (i == hot) DrawRectangle((int)(b.x + 6), (int)ry, (int)(b.width - 6), (int)CODE_ROW, ui_fade(C_FG, 0.06f));
        int t = line_turn(d, i);
        float a = 1;
        if (on_c != INT_MIN) a = d->orig[i] == on_c ? 1 : 0.15f;
        else if (in->legend_turn != INT_MIN) a = t == in->legend_turn ? 1 : 0.15f;
        ui_code_row(&d->cv, i, sx - d->code_hscroll, ry + 2, C_FG, 1, sx, b.x + b.width);
        char no[16];
        snprintf(no, sizeof no, "%d", d->line0 + i);
        sv_tx(F_MONO, no, b.x + 6 + gw - 10 - sv_tw(F_MONO, no, 12), sv_mid(ry + CODE_ROW / 2, 12), 12, C_FAINT);
        DrawRectangle((int)b.x, (int)ry, 6, (int)CODE_ROW,
                      ui_fade(t < 0 ? C_UNKNOWN : sv_blame_col(st_blame_slot(n, t)), a));
    }
    DrawRectangle((int)(b.x + 6 + gw), (int)b.y, 1, (int)b.height, C_LINE);
    float hh = 18;
    if (in->nvim && iy1 - iy0 >= 3 * hh) { /* in a corner of what shows, away from the hovered row */
        static const char hint[] = "click: open in nvim";
        float hw = sv_tw(F_SANS, hint, 11) + 12, hx = b.x + b.width - hw - 8, hy = iy1 - hh - 6;
        if (mp.y > (iy0 + iy1) / 2) hy = iy0 + 6;
        DrawRectangleRounded((Rectangle){hx, hy, hw, hh}, 0.4f, 6, ui_fade(C_RAISED, 0.95f));
        sv_tx(F_SANS, hint, hx + 6, sv_mid(hy + hh / 2, 11), 11, C_DIM);
    }
    EndScissorMode();
    BeginScissorMode((int)clip.x, (int)clip.y, (int)clip.width, (int)clip.height);
    if (max_scroll > 0) {
        float th = it->h - 4, bh = fmaxf(16, th * it->h / ((float)d->cv.n * CODE_ROW + 12));
        DrawRectangleRounded((Rectangle){b.x + b.width - 5, b.y + 2 + (th - bh) * d->code_scroll / max_scroll, 3, bh}, 1,
                             4, ui_fade(C_DIM, 0.5f));
    }
}

static void draw_text_item(const IItem *it, float x, float y, Color c) {
    if (!it->s || !*it->s) return;
    float top = sv_mid(y + it->h / 2, it->size);
    if (it->st)
        ui_rich_draw(sv_font(it->font), sv_bold(it->font), it->s, it->st, (int)strlen(it->s), x, top, PX(it->size), c,
                     it->c2.a ? it->c2 : c);
    else sv_txs(it->font, it->s, x, top, it->size, it->sp, c);
}

void si_draw(Insp *in, Rectangle r, Vector2 mp) {
    float w = r.width - 28;
    Doc *d = &in->doc;
    bool inside = CheckCollisionPointRec(mp, r);
    in->tip = NULL;
    /* the hovered strip (only the strips, not the gutter); the card keeps the last one */
    in->strip_line = -1;
    if (inside && d->norig) {
        Rectangle c = code_rect(in, r);
        if (mp.x >= c.x && mp.x < c.x + 6 && mp.y >= c.y + 6 && mp.y < c.y + c.height) {
            int i = (int)((mp.y - c.y - 6 + d->code_scroll) / CODE_ROW);
            if (i >= 0 && i < d->cv.n) in->strip_line = in->card_line = i;
        }
    }
    if (d->card >= 0 && (in->card.node != in->card_line || in->card.w != w)) {
        card_build(in, w);
        in->card.node = in->card_line;
    }
    float extra = card_extra(in), maxs = fmaxf(0, d->h + extra + 28 - r.height);
    in->scroll = fmaxf(0, fminf(maxs, in->scroll));
    float ox = r.x + 14, oy = r.y + 14 - in->scroll;
    in->legend_turn = INT_MIN;
    int hot = -1;
    Rectangle hot_r = {0};
    for (int i = 0; inside && i < d->n; i++) {
        const IItem *it = &d->it[i];
        if (it->k != IT_LEGEND && it->k != IT_LINK && it->k != IT_TIP) continue;
        Rectangle q = {ox + it->x, item_y(in, i, oy), it->w, it->h};
        if (!CheckCollisionPointRec(mp, q)) continue;
        if (it->tip) in->tip = it->tip, in->tip_at = mp;
        if (it->k == IT_TIP) continue;
        if (it->k == IT_LEGEND) in->legend_turn = it->aux;
        hot = i, hot_r = q;
    }
    BeginScissorMode((int)r.x + 1, (int)r.y, (int)r.width - 1, (int)r.height);
    for (int i = 0; i < d->n; i++) {
        const IItem *it = &d->it[i];
        float x = ox + it->x, y = item_y(in, i, oy);
        if (y > r.y + r.height || y + fmaxf(it->h, 1) < r.y) continue;
        switch (it->k) {
        case IT_TEXT: {
            Color c = it->c;
            if (it->hl && hot >= 0 && d->it[hot].c2.a && CheckCollisionPointRec((Vector2){x + 1, y + it->h / 2}, hot_r))
                c = d->it[hot].c2;
            draw_text_item(it, x, y, c);
            break;
        }
        case IT_BOX: {
            Rectangle q = {x, y, it->w, it->h};
            float rr = it->radius > 0 ? fminf(1, it->radius * 2 / fminf(q.width, q.height)) : 0;
            if (it->c.a) {
                if (rr > 0) DrawRectangleRounded(q, rr, 6, it->c);
                else DrawRectangleRec(q, it->c);
            }
            if (it->c2.a) {
                if (rr > 0) DrawRectangleRoundedLinesEx(q, rr, 6, it->bw, it->c2);
                else DrawRectangleLinesEx(q, it->bw, it->c2);
            }
            break;
        }
        case IT_DOT: DrawCircleV((Vector2){x, y}, it->w, it->c); break;
        case IT_LINK:
        case IT_LEGEND:
            if (i == hot && it->c.a) DrawRectangleRounded((Rectangle){x, y, it->w, it->h}, 4 / fmaxf(8, it->h), 6, it->c);
            break;
        case IT_LINE: DrawRectangle((int)x, (int)y, (int)it->w, 1, it->c); break;
        case IT_NOTE: ui_nv_boxes(&d->nv[it->aux], (Rectangle){x, y, it->w, it->h}, &d->nvs[it->aux]); break;
        case IT_SPARK: spark(in, it, x, y, mp); break;
        case IT_CODE: draw_code(in, it, x, y, (Rectangle){r.x + 1, r.y, r.width - 1, r.height}, mp); break;
        case IT_CARD: {
            float h = fmaxf(CARD_MIN, in->card.h);
            Color bc = C_LINE;
            if (in->card_line >= 0) {
                int t = line_turn(d, in->card_line);
                bc = t < 0 ? C_UNKNOWN : sv_blame_col(st_blame_slot(&d->m->nodes[d->code_node], t));
            }
            DrawRectangle((int)x, (int)y, 3, (int)h, bc);
            for (int k = 0; k < in->card.n; k++) {
                const IItem *c = &in->card.it[k];
                draw_text_item(c, x + c->x, y + c->y, c->c);
            }
            break;
        }
        default: break;
        }
    }
    EndScissorMode();
    if (maxs > 0) {
        float th = r.height - 8, bh = fmaxf(24, th * r.height / (d->h + extra + 28));
        DrawRectangleRounded((Rectangle){r.x + r.width - 6, r.y + 4 + (th - bh) * in->scroll / maxs, 3, bh}, 1, 4,
                             ui_fade(C_DIM, 0.45f));
    }
}

SiAct si_click(Insp *in, Rectangle r, Vector2 mp) {
    Doc *d = &in->doc;
    float ox = r.x + 14, oy = r.y + 14 - in->scroll;
    for (int i = d->n - 1; i >= 0; i--) {
        const IItem *it = &d->it[i];
        if (it->k != IT_LINK) continue;
        Rectangle q = {ox + it->x, item_y(in, i, oy), it->w, it->h};
        if (CheckCollisionPointRec(mp, q)) return (SiAct){it->turn >= 0 ? -1 : it->node, it->turn};
    }
    return (SiAct){-1, -1};
}

int si_code_at(const Insp *in, Rectangle r, Vector2 mp, const char **id) {
    const Doc *d = &in->doc;
    if (!d->cv.n || !d->m || !CheckCollisionPointRec(mp, r)) return 0;
    Rectangle c = code_rect(in, r);
    if (!CheckCollisionPointRec(mp, c) || mp.y < c.y + 6) return 0;
    int i = (int)((mp.y - c.y - 6 + d->code_scroll) / CODE_ROW);
    if (i < 0 || i >= d->cv.n) return 0;
    *id = d->m->nodes[d->m->nodes[d->code_node].file].id;
    return d->line0 + i;
}

void si_wheel(Insp *in, Rectangle r, Vector2 mp, float wheel, bool shift) {
    Doc *d = &in->doc;
    Rectangle c = code_rect(in, r);
    if (d->cv.n && CheckCollisionPointRec(mp, c)) {
        if (shift) {
            d->code_hscroll = fmaxf(0, fminf(max_hscroll(d, c.width), d->code_hscroll - wheel * 40));
            return;
        }
        float maxc = fmaxf(0, (float)d->cv.n * CODE_ROW + 12 - c.height);
        if (maxc > 0 && !((wheel > 0 && d->code_scroll <= 0) || (wheel < 0 && d->code_scroll >= maxc))) {
            d->code_scroll = fmaxf(0, fminf(maxc, d->code_scroll - wheel * CODE_ROW * 3));
            return;
        }
    }
    in->scroll -= wheel * 60;
}
