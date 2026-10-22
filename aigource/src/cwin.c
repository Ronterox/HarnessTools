/* The context screen's floating windows (context.tpl.html's .win): one item
 * of the agent's context each, a title bar (kind, source, tokens; "why" and
 * fold buttons when open), the header facts under it, the exact content:
 * a Read's lines as code with their numbers, a command's output in mono,
 * prompts and the agent's text as light markdown. Folded, a window is its
 * title bar: a chip. Content is laid out once per width, as rows, and only
 * the rows in view are drawn. */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cview_int.h"
#include "util.h"

#define PROSE_ROW 18.75f /* 12.5 px sans at line height 1.5 */
#define NOTE_ROW_H 18.0f
#define META_ROW 15.0f
#define TOOL_ROW 20.0f
#define LOD_PX 4.0f /* text smaller than this on screen isn't drawn */

static const char *kind_upper(int k) {
    static char up[CK_COUNT][16];
    if (k < 0 || k >= CK_COUNT) return "";
    if (!up[k][0])
        for (int i = 0; ctx_kind_names[k][i] && i < 15; i++) up[k][i] = (char)(ctx_kind_names[k][i] & ~0x20);
    return up[k];
}

double cw_life(const CNode *n) { return n->gone > 0 && n->leave == LV_THINK ? 2 * CW_ANIM : CW_ANIM; }

static float ease(const CNode *n, double clock) {
    double p = n->t0 < 0 ? 1 : (clock - n->t0) / cw_life(n);
    return p >= 1 ? 1 : 1 - powf(1 - (float)fmax(0, p), 3);
}

float cw_scale(const CNode *n, double clock) {
    return n->gone > 0 && n->leave == LV_FOLD ? 1 - 0.92f * ease(n, clock) : 1;
}

void cw_pos(const CNode *n, double clock, float *x, float *y, float *a) {
    bool gone = n->gone > 0;
    float tx = gone ? n->gx : n->x, ty = gone ? n->gy : n->y, ta = gone ? 0 : 1;
    double p = n->t0 < 0 ? 1 : (clock - n->t0) / cw_life(n);
    if (p >= 1) { *x = tx, *y = ty, *a = ta; return; }
    float e = 1 - powf(1 - (float)fmax(0, p), 3); /* ease out, as the draft's cubic-bezier(.2,.7,.2,1) */
    *x = n->ax + (tx - n->ax) * e, *y = n->ay + (ty - n->ay) * e, *a = n->aa + (ta - n->aa) * e;
}

void cw_header(CNode *n, const CtxAgent *a, int c, const int *by_item) {
    cl_facts(a, n->item, c, by_item, &n->f);
    n->nmeta = cl_meta(a, &n->f, n->meta);
    cl_fmt_k(n->tok, sizeof n->tok, n->f.tok);
    n->warn = n->f.nstale && !n->f.known;
    n->hdr_c = c;
    n->meta_w = -1;
    n->fit_w = -1; /* the ⚠ changes the room */
}

/* ---- the body ---------------------------------------------------------------- */

void cw_body_free(CNode *n) {
    CBody *b = n->body;
    if (!b) return;
    for (int i = 0; i < b->nstr; i++) free(b->str[i]), free(b->st[i]);
    free(b->str), free(b->st), free(b->rows), free(b->nums), free(b->tool_open);
    ui_code_free(&b->cv);
    free(b);
    n->body = NULL;
}

static int add_str(CBody *b, char *s, unsigned char *st) {
    if (b->nstr == b->capstr) {
        b->capstr = b->capstr ? b->capstr * 2 : 8;
        b->str = xrealloc(b->str, sizeof *b->str * (size_t)b->capstr);
        b->st = xrealloc(b->st, sizeof *b->st * (size_t)b->capstr);
    }
    b->str[b->nstr] = s, b->st[b->nstr] = st;
    return b->nstr++;
}

static void add_row(CBody *b, CRow r) {
    GROW(b->rows, b->n, b->cap);
    b->rows[b->n++] = r;
}

/* Text as a pre block: tabs as 4 spaces, \r dropped, NULs as spaces, lines
 * wrapped at cols cells (pre-wrap), from *y. */
static void pre(CBody *b, const char *t, size_t len, float *y, float x, int cols, Color c, bool box) {
    size_t tabs = 0;
    for (size_t i = 0; i < len; i++) tabs += t[i] == '\t';
    char *s = xmalloc(len + tabs * 3 + 1), *o = s;
    for (size_t i = 0; i < len; i++) {
        if (t[i] == '\t') memcpy(o, "    ", 4), o += 4;
        else if (t[i] != '\r') *o++ = t[i] ? t[i] : ' ';
    }
    *o = 0;
    int si = add_str(b, s, NULL), n = (int)(o - s);
    if (cols < 8) cols = 8;
    for (int k = 0;;) {
        int e = k;
        while (e < n && s[e] != '\n') e++;
        int a = k;
        do { /* cols code points a row */
            int z = a, cells = 0;
            while (z < e && cells < cols) {
                z++;
                while (z < e && ((unsigned char)s[z] & 0xC0) == 0x80) z++;
                cells++;
            }
            add_row(b, (CRow){.y = *y, .h = CW_ROW, .x = x, .k = CR_MONO, .c = c, .s = si, .a = a, .n = z - a, .box = box});
            *y += CW_ROW;
            a = z;
        } while (a < e);
        if (e >= n) break;
        k = e + 1;
    }
}

/* Sans text wrapped at w (indent: the first row narrower), from *y. */
static void wrapped(CBody *b, char *s, unsigned char *st, float *y, float x, float w, float css, float row, int font,
                    Color c) {
    int si = add_str(b, s, st), cap = 0;
    UiSpan *sp = NULL;
    int nr = ui_wrap(sv_font(font), sv_bold(font), s, st, (int)strlen(s), PX(css), w, 0, &sp, &cap);
    for (int r = 0; r < nr; r++) {
        add_row(b, (CRow){.y = *y, .h = row, .x = x, .k = CR_PROSE, .font = (unsigned char)font, .size = css, .c = c,
                          .s = si, .a = sp[r].start, .n = sp[r].len});
        *y += row;
    }
    free(sp);
}

static void note(CBody *b, const char *t, float *y, float w) {
    *y += 6;
    wrapped(b, xstrdup(t), NULL, y, 9, w - 18, 12, NOTE_ROW_H, F_SANS, C_FAINT);
    for (int i = b->n - 1; i >= 0 && b->rows[i].s == b->nstr - 1; i--) b->rows[i].k = CR_NOTE;
    *y += 6;
}

static bool blank(const char *t, size_t n) {
    for (size_t i = 0; i < n; i++)
        if (t[i] != ' ' && t[i] != '\n' && t[i] != '\t' && t[i] != '\r') return false;
    return true;
}

/* One line of markdown source [s, e) for ui_md. */
static char *line_dup(const char *s, const char *e) { return xstrndup(s, (size_t)(e - s)); }

/* The draft's md(): headings, lists, fences, inline code, bold; a paragraph per line. */
static void prose(CBody *b, const char *t, size_t len, float *y, float w, Color c) {
    *y += 6;
    const char *p = t, *end = t + len;
    bool list = false, first = true;
    while (p < end) {
        const char *e = memchr(p, '\n', (size_t)(end - p));
        if (!e) e = end;
        if (e - p >= 3 && !strncmp(p, "```", 3)) { /* a fence, to the next one */
            const char *q = e < end ? e + 1 : end, *f = q;
            while (f < end) {
                const char *fe = memchr(f, '\n', (size_t)(end - f));
                if (!fe) fe = end;
                if (fe - f >= 3 && !strncmp(f, "```", 3)) break;
                f = fe < end ? fe + 1 : end;
            }
            if (list) *y += 6, list = false;
            *y += 4 + 6;
            float w0 = w - 18 - 16;
            pre(b, q, (size_t)((f > q && f[-1] == '\n' ? f - 1 : f) - q), y, 9 + 8,
                (int)(w0 / ui_code_cell()), c, true);
            *y += 6 + 6;
            const char *fe = f < end ? memchr(f, '\n', (size_t)(end - f)) : NULL;
            p = fe ? fe + 1 : end;
            first = false;
            continue;
        }
        const char *s = p;
        while (s < e && (*s == ' ' || *s == '\t')) s++;
        const char *li = NULL;
        if (s < e && (*s == '-' || *s == '*') && s + 1 < e && (s[1] == ' ' || s[1] == '\t')) li = s + 2;
        else if (s < e && *s >= '0' && *s <= '9') {
            const char *d = s;
            while (d < e && *d >= '0' && *d <= '9') d++;
            if (d + 1 < e && *d == '.' && (d[1] == ' ' || d[1] == '\t')) li = d + 2;
        }
        int h = 0;
        while (p + h < e && p[h] == '#' && h < 5) h++;
        bool head = h >= 1 && h <= 4 && p + h < e && (p[h] == ' ' || p[h] == '\t');
        char *src = NULL, *o;
        unsigned char *st;
        if (li) {
            while (li < e && (*li == ' ' || *li == '\t')) li++;
            list = true;
            src = line_dup(li, e);
            ui_md(src, &o, &st);
            free(src);
            add_row(b, (CRow){.y = *y, .h = PROSE_ROW, .x = 9 + 6, .k = CR_PROSE, .font = F_SANS, .size = 12.5f,
                              .c = c, .s = add_str(b, xstrdup("\xE2\x80\xA2"), NULL), .a = 0, .n = 3});
            wrapped(b, o, st, y, 9 + 18, w - 18 - 18, 12.5f, PROSE_ROW, F_SANS, c);
        } else {
            if (list) *y += 6, list = false;
            if (head) {
                const char *q = p + h;
                while (q < e && (*q == ' ' || *q == '\t')) q++;
                if (!first) *y += 2;
                src = line_dup(q, e);
                ui_md(src, &o, &st);
                free(src);
                for (size_t i = 0; o[i]; i++) st[i] = st[i] == UI_CODE ? UI_CODE : UI_BOLD;
                wrapped(b, o, st, y, 9, w - 18, 12.5f, PROSE_ROW, F_SANS, C_FG);
                *y += 4;
            } else if (!blank(p, (size_t)(e - p))) {
                src = line_dup(p, e);
                ui_md(src, &o, &st);
                free(src);
                wrapped(b, o, st, y, 9, w - 18, 12.5f, PROSE_ROW, F_SANS, c);
                *y += 6;
            }
        }
        first = false;
        p = e < end ? e + 1 : end;
    }
    if (list) *y += 6;
}

/* Code lines with their numbers: a Read's (read), a Write's content. */
static void code(CBody *b, const CtxItem *it, char **lines, const int *nums, int n, float *y, bool read) {
    if (b->cv.d) goto rows; /* kept from the last layout: its rows don't depend on the width */
    size_t len = 0;
    for (int i = 0; i < n; i++) len += strlen(lines[i]) + 1;
    char *t = xmalloc(len + 1), *o = t;
    for (int i = 0; i < n; i++) {
        size_t l = strlen(lines[i]);
        memcpy(o, lines[i], l), o += l, *o++ = '\n';
    }
    *o = 0;
    ui_code_init(&b->cv, it->path ? it->path : "", t, 1, n);
    free(t);
    b->nums = xmalloc(sizeof *b->nums * (size_t)(n ? n : 1));
    for (int i = 0; i < n; i++) b->nums[i] = nums[i];
    for (int i = 0; i < b->cv.n && i < n; i++) b->cv.d->lines[i].no = nums[i];
    b->read = read;
    b->cols = ui_code_cols(&b->cv);
    char last[16];
    snprintf(last, sizeof last, "%d", n ? nums[n - 1] : 0);
    b->gut = (float)strlen(last) * ui_code_cell() + 12;
rows:
    *y += 6;
    for (int i = 0; i < b->cv.n; i++) {
        add_row(b, (CRow){.y = *y, .h = CW_ROW, .k = CR_CODE, .line = i});
        *y += CW_ROW;
    }
    *y += 6;
}

/* The content of item it at content width w (the draft's body()). */
static void body_build(CNode *n, const CtxAgent *a, float w) {
    const CtxItem *it = &a->items[n->item];
    CBody keep = {0}; /* what doesn't depend on the width: the code view, the tools shown */
    if (n->body) {
        CBody *o = n->body;
        keep.cv = o->cv, keep.nums = o->nums, keep.cols = o->cols, keep.gut = o->gut, keep.read = o->read;
        keep.tool_open = o->tool_open;
        memset(&o->cv, 0, sizeof o->cv), o->nums = NULL, o->tool_open = NULL;
    }
    cw_body_free(n);
    CBody *b = n->body = xcalloc(1, sizeof *b);
    b->cv = keep.cv, b->nums = keep.nums, b->cols = keep.cols, b->gut = keep.gut, b->read = keep.read;
    b->tool_open = keep.tool_open;
    b->w = w;
    float y = 0;
    int cols = (int)((w - 16) / ui_code_cell());
    const char *t = it->text ? it->text : "";
    char buf[512];
    switch (it->kind) {
    case CK_RESULT: {
        char *cb, **lines;
        int *nums;
        const char *rest;
        int k = ctx_code_lines(it, &cb, &lines, &nums, &rest);
        if (k) {
            code(b, it, lines, nums, k, &y, true);
            size_t rl = strlen(rest);
            if (!blank(rest, rl)) y += 6, pre(b, rest, rl, &y, 8, cols, C_SOFT, false), y += 6;
            free(cb), free(lines), free(nums);
        } else if (!blank(t, it->len)) {
            y += 6, pre(b, t, it->len, &y, 8, cols, C_SOFT, false), y += 6;
        } else note(b, "(empty result)", &y, w);
        break;
    }
    case CK_ACTION:
        if (it->tool && !strcmp(it->tool, "Bash")) {
            char *s = xmalloc(strlen(it->cmd ? it->cmd : "") + 3);
            sprintf(s, "$ %s", it->cmd ? it->cmd : "");
            y += 6, pre(b, s, strlen(s), &y, 8, cols, C_SOFT, false), y += 6;
            free(s);
            if (it->desc) note(b, it->desc, &y, w);
        } else if (it->tool && !strcmp(it->tool, "Edit")) {
            y += 6;
            pre(b, it->path ? it->path : "", strlen(it->path ? it->path : ""), &y, 8, cols, C_FAINT, false);
            for (int side = 0; side < 2; side++) {
                const char *s = side ? it->new_str : it->old_str;
                for (const char *p = s ? s : "";;) {
                    const char *e = strchr(p, '\n');
                    size_t l = e ? (size_t)(e - p) : strlen(p);
                    char *ln = xmalloc(l + 3);
                    memcpy(ln, side ? "+ " : "- ", 2), memcpy(ln + 2, p, l), ln[l + 2] = 0;
                    pre(b, ln, l + 2, &y, 8, cols, side ? C_ADD : C_DEL, false);
                    free(ln);
                    if (!e) break;
                    p = e + 1;
                }
            }
            y += 6;
        } else if (it->tool && !strcmp(it->tool, "Write") && it->content) {
            char *s = xstrdup(it->content), **lines;
            int nl = 1;
            for (char *p = s; *p; p++) nl += *p == '\n';
            lines = xmalloc(sizeof *lines * (size_t)nl);
            int *nums = xmalloc(sizeof *nums * (size_t)nl), k = 0;
            for (char *p = s; p; k++) {
                char *e = strchr(p, '\n');
                if (e) *e = 0;
                lines[k] = p, nums[k] = k + 1;
                p = e ? e + 1 : NULL;
            }
            code(b, it, lines, nums, k, &y, false);
            free(s), free(lines), free(nums);
        } else y += 6, pre(b, t, it->len, &y, 8, cols, C_SOFT, false), y += 6;
        break;
    case CK_THINKING:
        if (it->len) prose(b, t, it->len, &y, w, C_DIM);
        else {
            char k[32];
            cl_fmt_k(k, sizeof k, it->w);
            snprintf(buf, sizeof buf, "The thinking text was not returned (only its signature is in the transcript): %s "
                     "tokens of it ride in the context.", k);
            note(b, buf, &y, w);
        }
        break;
    case CK_PROMPT:
    case CK_TEXT:
    case CK_SUMMARY: prose(b, t, it->len, &y, w, C_SOFT); break;
    case CK_TOOLS:
        snprintf(buf, sizeof buf, "%d tool definitions, in this order:", it->ntx);
        note(b, buf, &y, w);
        if (!b->tool_open) b->tool_open = xcalloc((size_t)(it->ntx ? it->ntx : 1), 1);
        for (int k = 0; k < it->ntx; k++) {
            snprintf(buf, sizeof buf, "%s %s", b->tool_open[k] ? "\xE2\x96\xBE" : "\xE2\x96\xB8", it->names[k]);
            add_row(b, (CRow){.y = y, .h = TOOL_ROW, .x = 8, .k = CR_TOOL, .c = C_FG, .s = add_str(b, xstrdup(buf), NULL),
                              .n = (int)strlen(buf), .line = k});
            y += TOOL_ROW;
            if (b->tool_open[k]) {
                int x = it->tx[k];
                y += 6, pre(b, a->str[x], a->slen[x], &y, 16, cols - 1, C_SOFT, false), y += 6;
            }
        }
        y += 4;
        break;
    default: y += 6, pre(b, t, it->len, &y, 8, cols, C_SOFT, false), y += 6;
    }
    b->h = y;
}

void cw_toggle_tool(CNode *n, int tool) {
    if (!n->body || !n->body->tool_open) return;
    n->body->tool_open[tool] ^= 1;
    n->body->w = -1; /* laid out again at the next draw */
}

/* ---- geometry ------------------------------------------------------------------ */

typedef struct {
    Rectangle frame, why, fold, grip, meta, content;
    float kx, tx, tokx; /* kind, title, tokens: their left */
} Geo;

static float btn_w(const char *s) { return sv_tw(F_MONO, s, 11) + 10; }

static Geo geo(const CNode *n, float cx, float cy) {
    Geo g;
    memset(&g, 0, sizeof g);
    float L = cx - n->w / 2, T = cy - n->h / 2, R = L + n->w;
    g.frame = (Rectangle){L, T, n->w, n->h};
    float x = R - 1 - 6, by = T + (CW_HD - 18) / 2;
    if (n->open) {
        float fw = btn_w("\xE2\x80\x93");
        g.fold = (Rectangle){x - fw, by, fw, 18};
        x -= fw + 6;
        if (n->kind == CK_ACTION) {
            float ww = btn_w("why");
            g.why = (Rectangle){x - ww, by, ww, 18};
            x -= ww + 6;
        }
    }
    g.tokx = x - sv_tw(F_MONO, n->tok, 11);
    g.kx = L + 3 + 7;
    g.tx = g.kx + sv_tws(F_MONO_B, kind_upper(n->kind), 9.5f, 0.57f) + 6;
    if (n->open) {
        float mh = n->meta_w > 0 ? n->meta_h : META_ROW + 4;
        g.meta = (Rectangle){L + 3, T + CW_HD, n->w - 4, mh};
        g.content = (Rectangle){L + 3, T + CW_HD + mh, n->w - 4, fmaxf(0, n->h - CW_HD - mh - 1)};
        g.grip = (Rectangle){R - 14, T + n->h - 14, 14, 14};
    }
    return g;
}

/* The meta row's runs placed in width w: a part goes to the next line when it doesn't fit. */
static void meta_layout(CNode *n, float w) {
    float x = 0, y = 0, maxx = w - 7 - 7;
    for (int i = 0; i < n->nmeta; i++) {
        if (n->meta[i].brk && x > 0) {
            float pw = 0;
            for (int j = i; j < n->nmeta && (j == i || !n->meta[j].brk); j++) pw += sv_tw(F_MONO, n->meta[j].s, 10.5f);
            if (x + pw > maxx) x = 0, y += META_ROW;
        }
        n->rx[i] = x, n->ry[i] = y;
        x += sv_tw(F_MONO, n->meta[i].s, 10.5f);
    }
    n->meta_w = w;
    n->meta_h = y + META_ROW + 3 + 1;
}

static void content_clamp(CNode *n, const Geo *g) {
    CBody *b = n->body;
    float maxs = b ? fmaxf(0, b->h - g->content.height) : 0;
    n->scroll = fmaxf(0, fminf(maxs, n->scroll));
    float maxh = b && b->cv.n ? fmaxf(0, (float)b->cols * ui_code_cell() + 16 - (g->content.width - b->gut - 1)) : 0;
    n->hscroll = fmaxf(0, fminf(maxh, n->hscroll));
}

/* The first row at or below content y (rows are in y order). */
static int row_at(const CBody *b, float y) {
    int lo = 0, hi = b->n;
    while (lo < hi) {
        int m = (lo + hi) / 2;
        if (b->rows[m].y + b->rows[m].h <= y) lo = m + 1;
        else hi = m;
    }
    return lo;
}

/* ---- drawing -------------------------------------------------------------------- */

static Rectangle to_screen(const Camera2D *cam, Rectangle r) {
    return (Rectangle){cam->offset.x + (r.x - cam->target.x) * cam->zoom, cam->offset.y + (r.y - cam->target.y) * cam->zoom,
                       r.width * cam->zoom, r.height * cam->zoom};
}

static Rectangle clip(Rectangle a, Rectangle b) {
    float x0 = fmaxf(a.x, b.x), y0 = fmaxf(a.y, b.y), x1 = fminf(a.x + a.width, b.x + b.width),
          y1 = fminf(a.y + a.height, b.y + b.height);
    return (Rectangle){x0, y0, fmaxf(0, x1 - x0), fmaxf(0, y1 - y0)};
}

static float rnd(float r, Rectangle q) { return 2 * r / fmaxf(1, fminf(q.width, q.height)); }

/* A rounded box, or a plain one where its corners are under a pixel and a half on screen. */
static void box(Rectangle q, float r, float k, Color c) {
    if (r * k < 1.5f) DrawRectangleRec(q, c);
    else DrawRectangleRounded(q, rnd(r, q), 6, c);
}

static void dashed_line(Vector2 a, Vector2 b, Color c) {
    float len = hypotf(b.x - a.x, b.y - a.y);
    for (float t = 0; t < len; t += 7) {
        float t1 = fminf(len, t + 4);
        DrawLineEx((Vector2){a.x + (b.x - a.x) * t / len, a.y + (b.y - a.y) * t / len},
                   (Vector2){a.x + (b.x - a.x) * t1 / len, a.y + (b.y - a.y) * t1 / len}, 1, c);
    }
}

static void button(Rectangle r, const char *s, Color fg, Color border, bool hot, float al) {
    if (hot) DrawRectangleRoundedLinesEx(r, rnd(3, r), 4, 1, ui_fade(C_LINE, al));
    else if (border.a) DrawRectangleRoundedLinesEx(r, rnd(3, r), 4, 1, ui_fade(border, al));
    sv_tx(F_MONO, s, r.x + 5, sv_mid(r.y + r.height / 2, 11), 11, ui_fade(hot ? C_FG : fg, al));
}

static void draw_body(CNode *n, const CwCtx *x, const Geo *g, float al, bool text) {
    CBody *b = n->body;
    Rectangle cr = g->content;
    Rectangle sc = clip(to_screen(&x->cam, cr), x->stage);
    if (sc.width < 1 || sc.height < 1) return;
    BeginScissorMode((int)floorf(sc.x), (int)floorf(sc.y), (int)ceilf(sc.width), (int)ceilf(sc.height));
    float top = cr.y - n->scroll, cw = ui_code_cell();
    float cx0 = cr.x + b->gut + 1; /* code: right of the gutter */
    int hot = -1;
    if (x->nvim && b->read && !x->busy && CheckCollisionPointRec(x->mw, cr) && x->mw.x >= cr.x) {
        int r = row_at(b, x->mw.y - top);
        if (r < b->n && b->rows[r].k == CR_CODE && x->mw.y - top >= b->rows[r].y) hot = r;
    }
    char piece[DET_ROW_BYTES + 8];
    bool gutter = false;
    for (int r = row_at(b, n->scroll); r < b->n; r++) {
        const CRow *w = &b->rows[r];
        float y = top + w->y;
        if (y > cr.y + cr.height) break;
        if (w->box) DrawRectangle((int)(cr.x + 9), (int)y, (int)(cr.width - 18), (int)ceilf(w->h), ui_fade(C_BG, al));
        if (w->k == CR_CODE) {
            gutter = true;
            if (r == hot) DrawRectangleRec((Rectangle){cr.x, y, cr.width, w->h}, ui_fade(C_FG, 0.06f * al));
            if (!text) continue;
            ui_code_row(&b->cv, w->line, cx0 + 8 - n->hscroll, y + 2, C_FG, al, cx0, cr.x + cr.width);
            continue;
        }
        if (!text || !w->n) continue;
        const char *s = b->str[w->s] + w->a;
        if (w->k == CR_MONO) {
            int m = w->n < DET_ROW_BYTES ? w->n : DET_ROW_BYTES;
            memcpy(piece, s, (size_t)m), piece[m] = 0;
            ui_draw_text(ui_mono, piece, (Vector2){cr.x + w->x, y + 2}, DET_FONT, 0, ui_fade(w->c, al));
        } else if (w->k == CR_TOOL) {
            sv_tx(F_MONO, s, cr.x + w->x, sv_mid(y + w->h / 2, 12), 12, ui_fade(w->c, al));
        } else {
            const unsigned char *st = b->st[w->s] ? b->st[w->s] + w->a : NULL;
            Font f = sv_font(w->font);
            float z = PX(w->size);
            ui_rich_draw(f, sv_bold(w->font), s, st, w->n, cr.x + w->x, y + (w->h - z) / 2, z, ui_fade(w->c, al),
                         ui_fade(w->c, al));
        }
    }
    if (gutter) { /* sticky at the left, over the code scrolled under it */
        DrawRectangleRec((Rectangle){cr.x, cr.y, b->gut, cr.height}, ui_fade(C_PANEL, al));
        DrawRectangleRec((Rectangle){cr.x + b->gut, cr.y, 1, cr.height}, ui_fade(C_LINE, al));
        if (text)
            for (int r = row_at(b, n->scroll); r < b->n; r++) {
                const CRow *w = &b->rows[r];
                float y = top + w->y;
                if (y > cr.y + cr.height) break;
                if (w->k != CR_CODE) continue;
                char no[16];
                snprintf(no, sizeof no, "%d", b->nums[w->line]);
                ui_draw_text(ui_mono, no, (Vector2){cr.x + b->gut - 6 - (float)strlen(no) * cw, y + 2}, DET_FONT, 0,
                             ui_fade(C_FAINT, al));
            }
    }
    float maxs = b->h - cr.height;
    if (maxs > 0) {
        float th = cr.height - 4, bh = fmaxf(16, th * cr.height / b->h);
        DrawRectangleRounded((Rectangle){cr.x + cr.width - 5, cr.y + 2 + (th - bh) * n->scroll / maxs, 3, bh}, 1, 4,
                             ui_fade(C_DIM, 0.5f * al));
    }
    if (hot >= 0 && cr.height >= 3 * 18 && text) { /* in a corner, away from the hovered row */
        static const char hint[] = "click: open in nvim";
        float hw = sv_tw(F_SANS, hint, 11) + 12, hx = cr.x + cr.width - hw - 10, hy = cr.y + cr.height - 18 - 6;
        if (x->mw.y > cr.y + cr.height / 2) hy = cr.y + 6;
        DrawRectangleRounded((Rectangle){hx, hy, hw, 18}, 0.4f, 6, ui_fade(C_RAISED, 0.95f * al));
        sv_tx(F_SANS, hint, hx + 6, sv_mid(hy + 9, 11), 11, ui_fade(C_DIM, al));
    }
    EndScissorMode();
}

void cw_draw(CNode *n, const CwCtx *x, float cx, float cy, float al, bool linked, bool sel) {
    const CtxAgent *a = x->a;
    const CtxItem *it = &a->items[n->item];
    if (n->f.dropped) al *= 0.35f;
    if (al <= 0.01f) return;
    float k = x->cam.zoom;
    bool think = n->gone > 0 && n->leave == LV_THINK; /* cleared thinking: unreadable at once, grey, slower */
    bool text = k * 12 >= LOD_PX && !think;
    if (n->open && n->meta_w != n->w) meta_layout(n, n->w);
    Geo g = geo(n, cx, cy);
    Rectangle fr = g.frame;
    Color kc = think ? C_FAINT : cv_kind_col(it->kind);
    float r = n->open ? 6 : 4;
    Color bg = n->resp ? C_RESP : n->open ? C_PANEL : C_RAISED;
    if (n->open && !n->resp && r * k >= 1.5f) { /* 0 4px 18px #0007 */
        DrawRectangleRounded((Rectangle){fr.x - 6, fr.y - 2, fr.width + 12, fr.height + 12}, rnd(12, fr), 6, ui_fade(BLACK, 0.12f * al));
        DrawRectangleRounded((Rectangle){fr.x - 2, fr.y + 2, fr.width + 4, fr.height + 4}, rnd(8, fr), 6, ui_fade(BLACK, 0.22f * al));
    }
    if (linked || sel) DrawRectangleRoundedLinesEx((Rectangle){fr.x - 3, fr.y - 3, fr.width + 6, fr.height + 6}, rnd(r + 3, fr),
                                                   6, 2, ui_fade(sel ? C_FG : C_HOT, al));
    if (!n->resp) box(fr, r, k, ui_fade(C_LINE, al)); /* the border */
    box((Rectangle){fr.x, fr.y, fminf(fr.width, 14), fr.height}, r, k, ui_fade(kc, al)); /* border-left: 3px, the kind's */
    box((Rectangle){fr.x + 3, fr.y + 1, fr.width - 4, fr.height - 2}, r - 1, k, ui_fade(bg, al));
    if (n->resp) { /* dashed, the left side solid */
        Color lc = ui_fade(C_LINE, al);
        dashed_line((Vector2){fr.x + 3, fr.y + 0.5f}, (Vector2){fr.x + fr.width, fr.y + 0.5f}, lc);
        dashed_line((Vector2){fr.x + fr.width - 0.5f, fr.y}, (Vector2){fr.x + fr.width - 0.5f, fr.y + fr.height}, lc);
        dashed_line((Vector2){fr.x + 3, fr.y + fr.height - 0.5f}, (Vector2){fr.x + fr.width, fr.y + fr.height - 0.5f}, lc);
    }
    /* the title bar */
    float hy = fr.y + CW_HD / 2;
    if (k * 9.5f >= LOD_PX * 0.75f && !think) {
        sv_txs(F_MONO_B, kind_upper(it->kind), g.kx, sv_mid(hy, 9.5f), 9.5f, 0.57f, ui_fade(kc, al));
        float room = g.tokx - 6 - g.tx - (n->warn ? sv_tw(F_MONO, " \xE2\x9A\xA0", 12) : 0);
        if (n->fit_w != room) sv_fit(F_MONO, n->fit, sizeof n->fit, n->title, 12, fmaxf(0, room)), n->fit_w = room;
        sv_tx(F_MONO, n->fit, g.tx, sv_mid(hy, 12), 12, ui_fade(C_FG, al));
        if (n->warn) sv_tx(F_MONO, " \xE2\x9A\xA0", g.tx + sv_tw(F_MONO, n->fit, 12), sv_mid(hy, 12), 12, ui_fade(C_WARN, al));
        sv_tx(F_MONO, n->tok, g.tokx, sv_mid(hy, 11), 11, ui_fade(C_DIM, al));
        if (n->open) {
            bool hw = !x->busy && CheckCollisionPointRec(x->mw, g.why), hf = !x->busy && CheckCollisionPointRec(x->mw, g.fold);
            if (g.why.width > 0) button(g.why, "why", C_HOT, (Color){242, 165, 65, 85}, hw, al);
            button(g.fold, "\xE2\x80\x93", C_DIM, BLANK, hf, al);
        }
    }
    if (!n->open) return;
    /* the facts */
    Rectangle m = g.meta;
    if (text)
        for (int i = 0; i < n->nmeta; i++) {
            static const Color sc[] = {C_FAINT, C_DIM, C_ADD, C_HOT, C_WARN, C_WARN};
            int s = n->meta[i].style < 6 ? n->meta[i].style : 0;
            sv_tx(F_MONO, n->meta[i].s, m.x + 7 + n->rx[i], m.y + n->ry[i] + (META_ROW - PX(10.5f)) / 2, 10.5f,
                  ui_fade(sc[s], al));
        }
    DrawRectangleRec((Rectangle){m.x, m.y + m.height - 1, m.width, 1}, ui_fade(C_LINE, al));
    /* the content */
    if (g.content.height > 0) {
        if (!n->body || n->body->w != g.content.width) {
            float keep = n->scroll;
            body_build(n, a, g.content.width);
            n->scroll = keep;
        }
        content_clamp(n, &g);
        draw_body(n, x, &g, al, text);
    }
    /* the grip */
    Color gc = ui_fade(C_FAINT, al);
    float R = fr.x + fr.width, B = fr.y + fr.height;
    DrawLineEx((Vector2){R - 7, B - 1.5f}, (Vector2){R - 1.5f, B - 7}, 1.2f, gc);
    DrawLineEx((Vector2){R - 3.5f, B - 1.5f}, (Vector2){R - 1.5f, B - 3.5f}, 1.2f, gc);
}

/* ---- input ---------------------------------------------------------------------- */

int cw_hit(CNode *n, float cx, float cy, Vector2 p, int *line) {
    Geo g = geo(n, cx, cy);
    if (!CheckCollisionPointRec(p, g.frame)) return CW_NONE;
    if (p.y < g.frame.y + CW_HD || !n->open) {
        if (n->open && CheckCollisionPointRec(p, g.why)) return CW_WHY;
        if (n->open && CheckCollisionPointRec(p, g.fold)) return CW_FOLD;
        return CW_TITLE;
    }
    if (CheckCollisionPointRec(p, g.grip)) return CW_GRIP;
    if (CheckCollisionPointRec(p, g.meta)) {
        for (int i = 0; i < n->nmeta; i++) {
            if (strncmp(n->meta[i].s, "stale", 5)) continue;
            Rectangle q = {g.meta.x + 7 + n->rx[i], g.meta.y + n->ry[i], sv_tw(F_MONO, n->meta[i].s, 10.5f), META_ROW};
            if (CheckCollisionPointRec(p, q)) return CW_STALE;
        }
        return CW_META;
    }
    CBody *b = n->body;
    if (!b || !CheckCollisionPointRec(p, g.content)) return CW_BODY;
    float y = p.y - g.content.y + n->scroll;
    int r = row_at(b, y);
    if (r >= b->n || y < b->rows[r].y) return CW_BODY;
    if (b->rows[r].k == CR_CODE && b->read) {
        *line = b->nums[b->rows[r].line];
        return CW_CODE;
    }
    if (b->rows[r].k == CR_TOOL) {
        *line = b->rows[r].line;
        return CW_TOOL;
    }
    return CW_BODY;
}

bool cw_wheel(CNode *n, float cx, float cy, Vector2 p, float wheel, bool shift) {
    if (!n->open || !n->body) return false;
    Geo g = geo(n, cx, cy);
    if (!CheckCollisionPointRec(p, g.content)) return false;
    if (shift) n->hscroll -= wheel * 40;
    else n->scroll -= wheel * CW_ROW * 3;
    content_clamp(n, &g);
    return true;
}
