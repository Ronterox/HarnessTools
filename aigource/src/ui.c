#include "ui.h"

#include <ctype.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "model.h"
#include "rlgl.h"
#include "util.h"

/* ---- text helpers ------------------------------------------------------- */

Font ui_font, ui_font_bold, ui_mono, ui_mono_bold;
static bool font_ok;

static bool load_sans(Font *f, const char *const *paths) {
    /* ASCII, Latin-1 + Extended-A, punctuation (— “ ” • …), arrows, math, checks: the
     * agents' comments are prose. */
    static const int ranges[][2] = {{32, 126},       {160, 383},       {0x2010, 0x2027}, {0x2030, 0x203A},
                                    {0x2190, 0x21FF}, {0x2212, 0x2212}, {0x2260, 0x2265}, {0x25B6, 0x25B6},
                                    {0x2713, 0x2717}, {0xFFFD, 0xFFFD}};
    int cps[620], n = 0;
    for (size_t r = 0; r < sizeof ranges / sizeof *ranges; r++)
        for (int c = ranges[r][0]; c <= ranges[r][1]; c++) cps[n++] = c;
    for (int i = 0; paths[i]; i++) {
        if (!FileExists(paths[i])) continue;
        *f = LoadFontEx(paths[i], 64, cps, n);
        SetTextureFilter(f->texture, TEXTURE_FILTER_BILINEAR);
        return true;
    }
    return false;
}

static void load_font(void) {
    static const char *const regular[] = {
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        NULL,
    };
    static const char *const bold[] = {
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        NULL,
    };
    font_ok = load_sans(&ui_font, regular);
    if (!font_ok) ui_font = GetFontDefault();
    if (!load_sans(&ui_font_bold, bold)) ui_font_bold = ui_font;
}

/* Monospace for the detail view: code, diffs, command output. Bold for what
 * should stand out in it; the same cell width, so columns stay aligned. */

static bool load_mono_face(Font *f, const char *const *paths) {
    static const int ranges[][2] = {{32, 126},       {160, 383},       {0x2010, 0x2027}, {0x2030, 0x203A},
                                    {0x2190, 0x21FF}, {0x2212, 0x2212}, {0x2260, 0x2265}, {0x2500, 0x25FF},
                                    {0x2713, 0x2717}, {0xFFFD, 0xFFFD}};
    int cps[1220], n = 0;
    for (size_t r = 0; r < sizeof ranges / sizeof *ranges; r++)
        for (int c = ranges[r][0]; c <= ranges[r][1]; c++) cps[n++] = c;
    for (int i = 0; paths[i]; i++) {
        if (!FileExists(paths[i])) continue;
        SetTraceLogLevel(LOG_ERROR); /* box-drawing glyphs overflow the cell: harmless warnings */
        *f = LoadFontEx(paths[i], 32, cps, n);
        SetTraceLogLevel(getenv("AIG_DEBUG") ? LOG_INFO : LOG_WARNING);
        SetTextureFilter(f->texture, TEXTURE_FILTER_BILINEAR);
        return true;
    }
    return false;
}

static void load_mono(void) {
    static const char *const regular[] = {
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
        "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
        NULL,
    };
    static const char *const bold[] = {
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSansMono-Bold.ttf",
        NULL,
    };
    if (!load_mono_face(&ui_mono, regular)) ui_mono = ui_font;
    if (!load_mono_face(&ui_mono_bold, bold)) ui_mono_bold = ui_mono;
}

/* raylib's DrawTextEx and MeasureTextEx, but finding ASCII glyphs directly
 * (raylib scans the font's ~1000 glyphs for every character) and drawing a
 * string as one batch of quads: the same vertices, a fraction of the CPU.
 * Text with a newline goes to raylib, whose line spacing is its own. */
static int glyph_ix(Font f, int cp) {
    if (cp >= 32 && cp <= 126 && f.glyphCount > cp - 32 && f.glyphs[cp - 32].value == cp) return cp - 32;
    return GetGlyphIndex(f, cp);
}

void ui_draw_text(Font f, const char *s, Vector2 pos, float size, float spacing, Color tint) {
    if (strchr(s, '\n') || !f.texture.id) {
        DrawTextEx(f, s, pos, size, spacing, tint);
        return;
    }
    float scale = size / (float)f.baseSize, x = 0, pad = (float)f.glyphPadding;
    float tw = (float)f.texture.width, th = (float)f.texture.height;
    bool open = false;
    for (int i = 0; s[i];) {
        int n = 0, cp = GetCodepointNext(s + i, &n), ix = glyph_ix(f, cp);
        i += n;
        Rectangle r = f.recs[ix];
        if (cp != ' ' && cp != '\t') {
            Rectangle d = {pos.x + x + ((float)f.glyphs[ix].offsetX - pad) * scale,
                           pos.y + ((float)f.glyphs[ix].offsetY - pad) * scale, (r.width + 2 * pad) * scale,
                           (r.height + 2 * pad) * scale};
            float u0 = (r.x - pad) / tw, v0 = (r.y - pad) / th;
            float u1 = (r.x + r.width + pad) / tw, v1 = (r.y + r.height + pad) / th;
            if (!open) {
                rlSetTexture(f.texture.id);
                rlBegin(RL_QUADS);
                open = true;
            }
            rlColor4ub(tint.r, tint.g, tint.b, tint.a);
            rlNormal3f(0, 0, 1);
            rlTexCoord2f(u0, v0); rlVertex2f(d.x, d.y);
            rlTexCoord2f(u0, v1); rlVertex2f(d.x, d.y + d.height);
            rlTexCoord2f(u1, v1); rlVertex2f(d.x + d.width, d.y + d.height);
            rlTexCoord2f(u1, v0); rlVertex2f(d.x + d.width, d.y);
        }
        x += (f.glyphs[ix].advanceX ? (float)f.glyphs[ix].advanceX : r.width) * scale + spacing;
    }
    if (open) {
        rlEnd();
        rlSetTexture(0);
    }
}

float ui_measure_text(Font f, const char *s, float size, float spacing) {
    if (!*s) return 0;
    if (strchr(s, '\n') || !f.texture.id) return MeasureTextEx(f, s, size, spacing).x;
    float w = 0;
    int count = 0;
    for (int i = 0; s[i]; count++) {
        int n = 0, cp = GetCodepointNext(s + i, &n), ix = glyph_ix(f, cp);
        i += n;
        w += f.glyphs[ix].advanceX > 0 ? (float)f.glyphs[ix].advanceX : f.recs[ix].width + (float)f.glyphs[ix].offsetX;
    }
    return w * size / (float)f.baseSize + (float)(count - 1) * spacing;
}

void ui_text(const char *s, float x, float y, float size, Color c) {
    ui_draw_text(ui_font, s, (Vector2){x, y}, size, size * (font_ok ? 0.02f : 0.1f), c);
}

float ui_text_w(const char *s, float size) {
    return ui_measure_text(ui_font, s, size, size * (font_ok ? 0.02f : 0.1f));
}

Color ui_fade(Color c, float a) {
    c.a = (unsigned char)(fmaxf(0, fminf(1, a)) * c.a);
    return c;
}

static bool is_mono(Font f) { return f.texture.id && (f.texture.id == ui_mono.texture.id || f.texture.id == ui_mono_bold.texture.id); }

float ui_spacing(Font f, float size) { return is_mono(f) ? 0 : size * (font_ok ? 0.02f : 0.1f); }

/* s cut to fit maxw pixels in f at size, with "…". */
void ui_fit_font(Font f, char *out, size_t n, const char *s, float size, float spacing, float maxw) {
    snprintf(out, n, "%s", s);
    if (ui_measure_text(f, out, size, spacing) <= maxw) return;
    size_t len = strlen(out);
    while (len > 0) {
        do len--; while (len > 0 && ((unsigned char)out[len] & 0xC0) == 0x80);
        if (len + 4 > n) continue;
        memcpy(out + len, "\xE2\x80\xA6", 4);
        if (ui_measure_text(f, out, size, spacing) <= maxw) return;
    }
}

void ui_fit(char *out, size_t n, const char *s, float size, float maxw) {
    ui_fit_font(ui_font, out, n, s, size, ui_spacing(ui_font, size), maxw);
}

/* ---- rich text: **bold** and `code` --------------------------------------- */

#define CODE_COLOR (Color){210, 168, 255, 255} /* #d2a8ff, the draft's inline code */

void ui_md(const char *src, char **out, unsigned char **st) {
    size_t n = strlen(src), k = 0;
    char *o = xmalloc(n + 1);
    unsigned char *t = xmalloc(n + 1);
    for (size_t i = 0; i < n;) {
        const char *e;
        if (src[i] == '`') { /* `code`, on one line */
            for (e = src + i + 1; *e && *e != '`' && *e != '\n'; e++) {}
            if (*e == '`' && e > src + i + 1) {
                for (const char *p = src + i + 1; p < e; p++) o[k] = *p, t[k++] = UI_CODE;
                i = (size_t)(e - src) + 1;
                continue;
            }
        }
        if (src[i] == '*' && src[i + 1] == '*') { /* **bold**, no * inside */
            for (e = src + i + 2; *e && *e != '*' && *e != '\n'; e++) {}
            if (e[0] == '*' && e[1] == '*' && e > src + i + 2) {
                for (const char *p = src + i + 2; p < e; p++) o[k] = *p, t[k++] = UI_BOLD;
                i = (size_t)(e - src) + 2;
                continue;
            }
        }
        o[k] = src[i++];
        t[k++] = UI_PLAIN;
    }
    o[k] = 0;
    t[k] = 0;
    *out = o;
    *st = t;
}

static Font rich_font(Font f, Font fb, unsigned char st) { return st == UI_BOLD ? fb : st == UI_CODE ? ui_mono : f; }
static float rich_size(float size, unsigned char st) { return st == UI_CODE ? size * 12 / 13.0f : size; }

/* The run of one style at s[i]: its style, its end *j, its bytes in piece. */
static unsigned char rich_run(const char *s, const unsigned char *st, int n, int i, int *j, char *piece, size_t cap) {
    unsigned char c = st ? st[i] : 0;
    int e = i;
    while (e < n && (st ? st[e] : 0) == c) e++;
    int m = e - i < (int)cap - 1 ? e - i : (int)cap - 1;
    memcpy(piece, s + i, (size_t)m);
    piece[m] = 0;
    *j = e;
    return c;
}

float ui_rich_w(Font f, Font fb, const char *s, const unsigned char *st, int n, float size) {
    float w = 0;
    char piece[1024];
    for (int i = 0, j; i < n; i = j) {
        unsigned char c = rich_run(s, st, n, i, &j, piece, sizeof piece);
        Font g = rich_font(f, fb, c);
        float z = rich_size(size, c), sp = ui_spacing(g, z);
        w += ui_measure_text(g, piece, z, sp) + (j < n ? sp : 0);
    }
    return w;
}

float ui_rich_draw(Font f, Font fb, const char *s, const unsigned char *st, int n, float x, float y, float size, Color c,
                   Color bold) {
    float x0 = x;
    char piece[1024];
    for (int i = 0, j; i < n; i = j) {
        unsigned char k = rich_run(s, st, n, i, &j, piece, sizeof piece);
        Font g = rich_font(f, fb, k);
        float z = rich_size(size, k), sp = ui_spacing(g, z);
        Color col = k == UI_CODE ? ui_fade(CODE_COLOR, c.a / 255.0f) : k == UI_BOLD ? bold : c;
        ui_draw_text(g, piece, (Vector2){x, y + (size - z) * 0.6f}, z, sp, col);
        x += ui_measure_text(g, piece, z, sp) + sp;
    }
    return x - x0;
}

int ui_wrap(Font f, Font fb, const char *s, const unsigned char *st, int n, float size, float w, float indent,
            UiSpan **rows, int *cap) {
    int nr = 0;
#define ROW(a, b)                                                                                                      \
    do {                                                                                                               \
        if (nr == *cap) *cap = *cap ? *cap * 2 : 32, *rows = xrealloc(*rows, sizeof **rows * (size_t)*cap);            \
        (*rows)[nr++] = (UiSpan){(a), (b) - (a)};                                                                      \
    } while (0)
#define FITS(a, b) (ui_rich_w(f, fb, s + (a), st ? st + (a) : NULL, (b) - (a), size) <= w - (nr ? 0 : indent))
    for (int k = 0;;) {
        int end = k;
        while (end < n && s[end] != '\n') end++;
        if (end == k) ROW(k, k); /* blank line */
        for (int start = k; start < end;) {
            int to = -1;
            for (int q = start; q < end;) {
                while (q < end && s[q] == ' ') q++;
                while (q < end && s[q] != ' ' && q - start < 1000) q++;
                if (!FITS(start, q)) break;
                to = q;
            }
            if (to < 0) { /* one word wider than the line: cut it */
                int c = start;
                while (c < end) {
                    int nx = c + 1;
                    while (nx < end && ((unsigned char)s[nx] & 0xC0) == 0x80) nx++;
                    if (c > start && !FITS(start, nx)) break;
                    c = nx;
                }
                to = c;
            }
            ROW(start, to);
            start = to;
            while (start < end && s[start] == ' ') start++;
        }
        if (end >= n) break;
        k = end + 1;
    }
#undef ROW
#undef FITS
    return nr;
}

void ui_fmt_count(char *buf, size_t n, long v) {
    char tmp[32];
    snprintf(tmp, sizeof tmp, "%ld", v < 0 ? -v : v);
    size_t len = strlen(tmp), o = 0;
    for (size_t i = 0; i < len && o + 2 < n; i++) {
        if (i && (len - i) % 3 == 0) buf[o++] = ',';
        buf[o++] = tmp[i];
    }
    buf[o] = 0;
}

void ui_fmt_ago(char *buf, size_t n, double secs) {
    if (secs < 0) secs = 0;
    if (secs < 60) snprintf(buf, n, "%ds ago", (int)secs);
    else if (secs < 3600) snprintf(buf, n, "%dm %02ds ago", (int)secs / 60, (int)secs % 60);
    else if (secs < 86400) snprintf(buf, n, "%dh %02dm ago", (int)secs / 3600, (int)secs % 3600 / 60);
    else snprintf(buf, n, "%dd ago", (int)(secs / 86400));
}

void ui_fmt_clock(char *buf, size_t n, double ts, const char *fmt) {
    time_t t = (time_t)ts;
    struct tm tm;
    localtime_r(&t, &tm);
    strftime(buf, n, fmt, &tm);
}

/* ---- comments: an agent's text before a step, as message boxes ---------- */

#define NOTE_FONT 14.0f
#define NOTE_PAD 8.0f  /* inside a box */
#define NOTE_GAP 8.0f  /* between boxes */
#define NOTE_HEAD 36.0f

void ui_nv_clear(NoteView *nv) {
    for (int i = 0; nv->st && i < nv->ns.n; i++) free(nv->st[i]);
    free(nv->st);
    nv->st = NULL;
    notes_free(&nv->ns);
    nv->w = 0;
    nv->nrows = 0;
}

/* Markdown bold and code as styles (markers dropped), ends trimmed. */
static void nv_tidy(NoteView *nv) {
    nv->st = xcalloc((size_t)(nv->ns.n ? nv->ns.n : 1), sizeof *nv->st);
    for (int i = 0; i < nv->ns.n; i++) {
        char *t;
        unsigned char *st;
        ui_md(nv->ns.s[i], &t, &st);
        size_t o = 0;
        for (size_t p = 0; t[p]; p++)
            if (t[p] != '\r') t[o] = t[p], st[o++] = st[p];
        while (o > 0 && (t[o - 1] == '\n' || t[o - 1] == ' ')) o--;
        size_t lead = strspn(t, "\n");
        if (lead > o) lead = o;
        memmove(t, t + lead, o - lead);
        memmove(st, st + lead, o - lead);
        t[o - lead] = 0;
        free(nv->ns.s[i]);
        nv->ns.s[i] = t;
        nv->st[i] = st;
    }
}

void ui_nv_load(NoteView *nv, const Event *e) {
    ui_nv_clear(nv);
    nv->ns = tr_notes(e->src, e->note_off, e->notes, e->tool_id);
    nv_tidy(nv);
}

void ui_nv_set(NoteView *nv, const char *const *texts, int n) {
    ui_nv_clear(nv);
    nv->ns.s = xcalloc((size_t)(n > 0 ? n : 1), sizeof *nv->ns.s);
    for (int i = 0; i < n; i++) nv->ns.s[i] = xstrdup(texts[i] ? texts[i] : "");
    nv->ns.n = n;
    nv_tidy(nv);
}

/* Every comment wrapped at w pixels (ui_wrap), rows tagged with their comment. */
static void nv_wrap(NoteView *nv, float w, float size) {
    nv->w = w;
    nv->size = size;
    nv->nrows = 0;
    UiSpan *sp = NULL;
    int cap = 0;
    for (int i = 0; i < nv->ns.n; i++) {
        int n = ui_wrap(ui_font, ui_font_bold, nv->ns.s[i], nv->st[i], (int)strlen(nv->ns.s[i]), size, w, 0, &sp, &cap);
        for (int k = 0; k < n; k++) {
            if (nv->nrows == nv->cap) {
                nv->cap = nv->cap ? nv->cap * 2 : 64;
                nv->rows = xrealloc(nv->rows, (size_t)nv->cap * sizeof *nv->rows);
            }
            nv->rows[nv->nrows++] = (struct NoteRow){i, sp[k].start, sp[k].len};
        }
    }
    free(sp);
}

static float nv_row_h(float size) { return size == NOTE_FONT ? NOTE_ROW : size * 1.3f; }

/* Height of the boxes stacked, once wrapped. */
static float nv_height(const NoteView *nv, const UiNoteStyle *ns) {
    float pad = ns ? ns->pad : NOTE_PAD, gap = ns ? ns->gap : NOTE_GAP, head = ns && ns->head ? ns->head_h : 0;
    return nv->ns.n ? (float)nv->nrows * nv_row_h(nv->size) + (float)nv->ns.n * (2 * pad + head + gap) - gap : 0;
}

static void dashed_rect(Rectangle r, Color c) {
    for (float x = r.x + 4; x < r.x + r.width - 4; x += 6) {
        DrawRectangle((int)x, (int)r.y, 3, 1, c);
        DrawRectangle((int)x, (int)(r.y + r.height - 1), 3, 1, c);
    }
    for (float y = r.y + 4; y < r.y + r.height - 4; y += 6) {
        DrawRectangle((int)r.x, (int)y, 1, 3, c);
        DrawRectangle((int)(r.x + r.width - 1), (int)y, 1, 3, c);
    }
}

/* The boxes from `scroll` pixels down, clipped to r (scissor: the caller's own
 * clip is replaced, so boxes drawn inside another clip pass false). ns NULL:
 * the comments panel's look, an accent bar per box. */
static void nv_draw(const NoteView *nv, Rectangle r, float scroll, Color accent, bool scissor, const UiNoteStyle *ns) {
    if (scissor) BeginScissorMode((int)r.x, (int)r.y, (int)r.width, (int)r.height);
    float y = r.y - scroll, pad = ns ? ns->pad : NOTE_PAD, gap = ns ? ns->gap : NOTE_GAP, rh = nv_row_h(nv->size);
    float head = ns && ns->head ? ns->head_h : 0, tx = ns ? r.x + ns->pad_x : r.x + 14;
    for (int k = 0; k < nv->nrows;) {
        int note = nv->rows[k].note, nr = 0;
        while (k + nr < nv->nrows && nv->rows[k + nr].note == note) nr++;
        float bh = (float)nr * rh + 2 * pad + head;
        if (y + bh >= r.y && y <= r.y + r.height) {
            Rectangle box = {r.x, y, r.width, bh};
            float round = fminf(0.5f, (ns ? 2 * ns->radius : 16) / fminf(bh, r.width));
            Color text = ns ? ns->text : TEXT;
            if (!ns) {
                DrawRectangleRounded(box, round, 6, (Color){32, 39, 50, 255});
                DrawRectangle((int)r.x, (int)(y + 5), 3, (int)(bh - 10), ui_fade(accent, 0.8f));
            } else {
                if (ns->bg.a) DrawRectangleRounded(box, round, 6, ns->bg);
                if (ns->dashed) dashed_rect(box, ns->border);
                else if (ns->border.a) DrawRectangleRoundedLinesEx(box, round, 6, 1, ns->border);
                if (ns->head) ui_draw_text(ui_mono_bold, ns->head, (Vector2){tx, y + pad}, ns->head_size, 0.6f, ns->head_color);
            }
            for (int j = 0; j < nr; j++) {
                const struct NoteRow *row = &nv->rows[k + j];
                float ry = y + pad + head + (float)j * rh;
                if (!row->len || ry + rh < r.y || ry > r.y + r.height) continue;
                ui_rich_draw(ui_font, ui_font_bold, nv->ns.s[note] + row->start, nv->st[note] + row->start, row->len, tx,
                             ry + 1, nv->size, text, ns ? ns->bold : TEXT);
            }
        }
        y += bh + gap;
        k += nr;
    }
    if (scissor) EndScissorMode();
}

float ui_nv_boxes_h(NoteView *nv, float w, const UiNoteStyle *ns) {
    if (nv->w != w - 2 * ns->pad_x || nv->size != ns->size) nv_wrap(nv, w - 2 * ns->pad_x, ns->size);
    return nv_height(nv, ns);
}

void ui_nv_boxes(NoteView *nv, Rectangle r, const UiNoteStyle *ns) {
    if (nv->w != r.width - 2 * ns->pad_x || nv->size != ns->size) nv_wrap(nv, r.width - 2 * ns->pad_x, ns->size);
    nv_draw(nv, r, 0, BLANK, false, ns);
}

/* A small speech bubble: the step has comments. */
void ui_bubble_icon(float x, float y, Color c) {
    DrawRectangleRounded((Rectangle){x, y, 13, 9}, 0.5f, 4, c);
    DrawTriangle((Vector2){x + 3, y + 8}, (Vector2){x + 3, y + 12}, (Vector2){x + 7, y + 8}, c);
}

/* ---- detail window: what stands out ------------------------------------ */

/* Style of one byte of a detail line: bold, and a color over the line's own. */
enum {
    ST_BOLD = 1, ST_COLOR = 30, ST_GOOD = 2, ST_BAD = 4, ST_DIM = 6, ST_KEY = 8, ST_NUM = 10, ST_DATE = 12,
    ST_STR = 14, ST_FUNC = 16, ST_TYPE = 18, ST_PROP = 20, ST_BUILTIN = 22,
};

/* Bytes i..e get the style, unless they have a color already (a number in a
 * dim flag, a comment or a string stays as it is). */
static void mark(unsigned char *st, int i, int e, unsigned char style) {
    for (; i < e; i++)
        if (!(st[i] & ST_COLOR)) st[i] |= style;
}

static Color style_color(int sc, Color line) {
    switch (sc) {
    case ST_GOOD: return kind_color(EV_CREATE);
    case ST_BAD: return (Color){255, 120, 120, 255};
    case ST_DIM: return DIM;
    case ST_KEY: return KEY_TEXT;
    case ST_NUM: return NUM_TEXT;
    case ST_DATE: return DATE_TEXT;
    case ST_STR: return STR_TEXT;
    case ST_FUNC: return FUNC_TEXT;
    case ST_TYPE: return TYPE_TEXT;
    case ST_PROP: return CMD_TEXT;
    case ST_BUILTIN: return BUILTIN_TEXT;
    default: return line;
    }
}

/* A tree-sitter class as a style. */
static unsigned char syn_style(unsigned char c) {
    static const unsigned char map[] = {
        [SYN_NONE] = 0,          [SYN_KEYWORD] = ST_BOLD | ST_KEY,   [SYN_TAG] = ST_KEY,
        [SYN_STRING] = ST_STR,   [SYN_ESCAPE] = ST_BUILTIN,          [SYN_COMMENT] = ST_DIM,
        [SYN_FUNCTION] = ST_FUNC, [SYN_TYPE] = ST_TYPE,              [SYN_CONSTANT] = ST_BOLD | ST_NUM,
        [SYN_PROPERTY] = ST_PROP, [SYN_BUILTIN] = ST_BUILTIN,        [SYN_HEADING] = ST_BOLD | ST_KEY,
        [SYN_STRONG] = ST_BOLD,  [SYN_LINK] = ST_FUNC,               [SYN_LIST] = ST_KEY,
    };
    return c < sizeof map ? map[c] : 0;
}

static char *g_user_words; /* " w1 w2 ... ": bold everywhere (~/.config/aigource/keywords) */

static void load_user_words(void) {
    const char *xdg = getenv("XDG_CONFIG_HOME"), *home = getenv("HOME");
    char path[1024];
    if (xdg && *xdg) snprintf(path, sizeof path, "%s/aigource/keywords", xdg);
    else snprintf(path, sizeof path, "%s/.config/aigource/keywords", home ? home : "");
    char *txt = FileExists(path) ? LoadFileText(path) : NULL;
    if (!txt) return;
    g_user_words = xmalloc(strlen(txt) + 3);
    char *p = g_user_words;
    *p++ = ' ';
    for (char *w = strtok(txt, " \t\r\n"); w; w = strtok(NULL, " \t\r\n")) p += sprintf(p, "%s ", w);
    *p = 0;
    UnloadFileText(txt);
}

void ui_init(void) {
    load_font();
    load_mono();
    load_user_words();
}

/* Keywords of the languages a read or edit can show, by file extension. */
static const struct { const char *ext, *kw; } LANGS[] = {
    {" c h cc cpp hpp cxx ",
     " if else for while do switch case default return break continue goto static const struct enum union typedef "
     "extern inline sizeof void int char float double bool unsigned signed long short true false NULL "
     "class public private protected template namespace using new delete virtual nullptr "},
    {" py ", " def class import from as return if elif else for while in not and or is None True False try except "
             "finally raise with lambda yield pass break continue global nonlocal async await assert del "},
    {" sh bash zsh ", " if then else elif fi for while until do done case esac in function return local export "
                      "set unset source exit "},
    {" js ts jsx tsx mjs cjs ", " function return if else for while do switch case default break continue const let "
                                "var new class extends import export from as async await try catch finally throw "
                                "typeof instanceof this null undefined true false of in interface type "},
    {" go ", " func package import return if else for range switch case default break continue go defer chan select "
             "struct interface type map var const nil true false "},
    {" rs ", " fn let mut pub use mod impl trait struct enum match if else for while loop in return break continue "
             "self Self crate super where as const static unsafe async await move ref true false "},
    {" lua ", " function local end if then else elseif for in do while repeat until return break nil true false "
              "and or not "},
    {" md ", ""}, /* headings */
};
/* What a command's output says about how it went. */
static const char BAD_WORDS[] = " error errors failed failure fail fatal panic traceback exception denied refused "
                                "abort aborted segmentation ";
static const char GOOD_WORDS[] = " ok passed pass success succeeded successful ";
static const char WARN_WORDS[] = " warning warnings warn deprecated ";
/* Shell words after which the next word is still a command. */
static const char SH_PREFIX[] = " if then else elif while until do ! sudo time env exec nohup xargs command builtin ";

static bool word_byte(unsigned char c) { return isalnum(c) || c == '_'; }

/* Is s[0..n) one of the words of list (" a b c ")? */
static bool in_list(const char *list, const char *s, int n, bool fold) {
    char w[64];
    if (!list || n > 60) return false;
    w[0] = ' ';
    for (int i = 0; i < n; i++) w[i + 1] = fold ? (char)tolower((unsigned char)s[i]) : s[i];
    w[n + 1] = ' ';
    w[n + 2] = 0;
    return strstr(list, w) != NULL;
}

/* Units a number can carry: 3.25s 40ms 2x 1.5MB. */
static const char UNITS[] = " s ms us ns m h d x k K M G B KB MB GB KiB MiB GiB px ";

/* The end of the number at s[i] (digits, with . or , between digits: 1,234
 * 3.14, and a unit) when it isn't part of a word (v2, 9a99151, 0x1F), else i. */
static int number_end(const char *s, int i) {
    if (!isdigit((unsigned char)s[i]) || (i > 0 && word_byte((unsigned char)s[i - 1]))) return i;
    int k = i, u;
    while (isdigit((unsigned char)s[k]) || (s[k] && strchr(".,", s[k]) && isdigit((unsigned char)s[k + 1]))) k++;
    for (u = k; isalpha((unsigned char)s[u]); u++) {}
    if (u > k && !word_byte((unsigned char)s[u]) && in_list(UNITS, s + k, u - k, false)) k = u;
    return word_byte((unsigned char)s[k]) ? i : k;
}

/* Exactly n digits at s[k]? */
static bool digits(const char *s, int k, int n) {
    for (int j = 0; j < n; j++)
        if (!isdigit((unsigned char)s[k + j])) return false;
    return !isdigit((unsigned char)s[k + n]);
}

/* The end of a date or time at s[i] (2026-10-19, 2026/10/19, 23:49:40, 9:05,
 * 2026-10-19T23:49:40.123Z, ... +02:00), else i. */
static int date_end(const char *s, int i) {
    if (i > 0 && word_byte((unsigned char)s[i - 1])) return i;
    int k = i;
    bool date = digits(s, k, 4) && (s[k + 4] == '-' || s[k + 4] == '/') && digits(s, k + 5, 2) &&
                s[k + 7] == s[k + 4] && digits(s, k + 8, 2);
    if (date) {
        k += 10;
        if ((s[k] == 'T' || s[k] == ' ') && isdigit((unsigned char)s[k + 1])) k++;
        else return word_byte((unsigned char)s[k]) ? i : k;
    }
    int h = digits(s, k, 2) ? 2 : digits(s, k, 1) ? 1 : 0;
    if (!h || s[k + h] != ':' || !digits(s, k + h + 1, 2)) return date ? k - 1 : i; /* no time after it */
    k += h + 3;
    if (s[k] == ':' && digits(s, k + 1, 2)) k += 3;
    if (s[k] == '.' && isdigit((unsigned char)s[k + 1]))
        for (k++; isdigit((unsigned char)s[k]); k++) {}
    if (s[k] == 'Z') k++;
    else if (date && (s[k] == '+' || s[k] == '-')) { /* +02:00 +0200 +02 */
        if (isdigit((unsigned char)s[k + 1]) && isdigit((unsigned char)s[k + 2]) && s[k + 3] == ':' && digits(s, k + 4, 2))
            k += 6;
        else if (digits(s, k + 1, 4)) k += 5;
        else if (digits(s, k + 1, 2)) k += 3;
    }
    return word_byte((unsigned char)s[k]) ? i : k;
}

/* Words of the line in a list get the style; dates, numbers and the user's
 * words bold in their own colors. */
static void style_words(const char *s, unsigned char *st, const char *kw, unsigned char kst, bool outcome) {
    for (int i = 0; s[i];) {
        int e = date_end(s, i);
        if (e > i) {
            mark(st, i, e, ST_BOLD | ST_DATE);
            i = e;
            continue;
        }
        if ((e = number_end(s, i)) > i) {
            mark(st, i, e, ST_BOLD | ST_NUM);
            i = e;
            continue;
        }
        if (!word_byte((unsigned char)s[i]) || (i > 0 && word_byte((unsigned char)s[i - 1]))) { i++; continue; }
        for (e = i; word_byte((unsigned char)s[e]) || (s[e] == '-' && word_byte((unsigned char)s[e + 1])); e++) {}
        unsigned char ws = in_list(g_user_words, s + i, e - i, false) ? ST_BOLD | ST_KEY : 0;
        if (in_list(kw, s + i, e - i, false)) ws = kst;
        if (outcome && in_list(BAD_WORDS, s + i, e - i, true)) ws = ST_BOLD | ST_BAD;
        else if (outcome && in_list(GOOD_WORDS, s + i, e - i, true)) ws = ST_BOLD | ST_GOOD;
        else if (outcome && in_list(WARN_WORDS, s + i, e - i, true)) ws = ST_BOLD | ST_KEY;
        mark(st, i, e, ws);
        i = e;
    }
}

/* A shell command, over its lines: each segment's program bold (after start,
 * && || | ; ( $( and the like), flags and comments dim, heredoc bodies and
 * quoted strings left alone. */
typedef struct {
    bool seg;          /* the next word is a program */
    char quote;        /* inside '...' or "..." */
    char delim[64];    /* heredoc delimiter: pending, or the body runs to it */
    bool in_body, dash;
} ShState;

static void style_shell(ShState *sh, const char *s, unsigned char *st, bool first) {
    if (sh->in_body) {
        const char *t = s;
        if (sh->dash) while (*t == '\t' || *t == ' ') t++; /* tabs arrive as spaces */
        if (!strcmp(t, sh->delim)) sh->in_body = false, sh->delim[0] = 0, sh->seg = true;
        return;
    }
    int n = (int)strlen(s), i = first && !strncmp(s, "$ ", 2) ? 2 : 0;
    if (first) sh->seg = true;
    while (i < n) {
        char c = s[i];
        if (sh->quote) {
            if (c == '\\' && sh->quote == '"') i++;
            else if (c == sh->quote) sh->quote = 0;
            i++;
            continue;
        }
        if (c == '\'' || c == '"') { sh->quote = c; i++; continue; }
        if (c == '#' && (i == 0 || isspace((unsigned char)s[i - 1]))) {
            while (i < n) st[i++] |= ST_DIM;
            break;
        }
        if (c == '<' && s[i + 1] == '<' && s[i + 2] != '<') { /* heredoc: <<EOF <<'EOF' <<-"EOF" */
            i += 2;
            bool dash = s[i] == '-';
            if (dash) i++;
            while (s[i] == ' ' || s[i] == '\'' || s[i] == '"') i++;
            if (!isalpha((unsigned char)s[i]) && s[i] != '_') continue; /* $(( 1 << 3 )): a shift */
            sh->dash = dash;
            int k = 0;
            while ((word_byte((unsigned char)s[i]) || s[i] == '-') && k < (int)sizeof sh->delim - 1) sh->delim[k++] = s[i++];
            sh->delim[k] = 0;
            if (s[i] == '\'' || s[i] == '"') i++;
            continue;
        }
        bool redir = c == '&' && ((i > 0 && s[i - 1] == '>') || s[i + 1] == '>'); /* 2>&1, &> */
        if ((strchr(";|&({`", c) && !redir) || (c == '$' && s[i + 1] == '(')) { sh->seg = true; i += c == '$' ? 2 : 1; continue; }
        if (isspace((unsigned char)c) || strchr(")}<>&", c)) { i++; continue; }
        int e = i;
        while (e < n && !isspace((unsigned char)s[e]) && !strchr(";|&()<>`'\"", s[e])) e++;
        const char *eq = memchr(s + i, '=', (size_t)(e - i));
        if (sh->seg && eq && eq > s + i) { /* VAR=value before the program */
        } else if (sh->seg) {
            mark(st, i, e, ST_BOLD | ST_KEY);
            sh->seg = in_list(SH_PREFIX, s + i, e - i, false);
        } else if (c == '-') {
            for (int k = i; k < e; k++) st[k] |= ST_DIM;
        }
        i = e;
    }
    if (sh->delim[0] && !sh->in_body) sh->in_body = true; /* the body starts on the next line */
    else if (!sh->quote && !(n && s[n - 1] == '\\')) sh->seg = true; /* a newline ends the command */
}

/* The keyword list for path when no grammar is installed ("" = markdown). */
static const char *fallback_keywords(const char *path) {
    const char *dot = strrchr(path, '.'), *slash = strrchr(path, '/');
    if (dot && (!slash || dot > slash))
        for (size_t l = 0; l < sizeof LANGS / sizeof *LANGS; l++)
            if (in_list(LANGS[l].ext, dot + 1, (int)strlen(dot + 1), true)) return LANGS[l].kw;
    return NULL;
}

/* Get ready to style detail d of a step of kind k on path: which lines are
 * code and on which side, and the commands (their state spans lines). Nothing
 * is parsed yet. */
void ui_style_init(DetStyle *ds, const Detail *d, EvKind k, const char *path) {
    memset(ds, 0, sizeof *ds);
    int n = d->n ? d->n : 1;
    ds->st = xcalloc((size_t)n, sizeof *ds->st);
    ds->side = xcalloc((size_t)n, 1);
    ds->off = xcalloc((size_t)n, sizeof *ds->off);
    bool code = path && (k == EV_READ || k == EV_EDIT || k == EV_CREATE || k == EV_TOUCH || k == EV_BASH);
    SynLang *lang = code ? syn_lang_for(path) : NULL;
    ds->kw = code ? fallback_keywords(path) : NULL;
    /* Code: the file's lines, before a command's own (its diff comes first). */
    size_t cap[2] = {0, 0};
    int first_no[2] = {-1, -1}; /* line number of each side's first line */
    for (int i = 0; (lang || ds->kw) && i < d->n && d->lines[i].kind != DL_HEAD; i++) {
        DlKind kd = d->lines[i].kind;
        if (kd != DL_TEXT && kd != DL_ADD && kd != DL_DEL) continue;
        ds->side[i] = kd == DL_DEL ? 2 : 1;
        ds->off[i] = ds->len[ds->side[i] - 1];
        if (!lang) continue;
        size_t len = strlen(d->lines[i].s);
        for (int s = 0; s < 2; s++) {
            if (kd == (s ? DL_ADD : DL_DEL)) continue; /* context goes to both sides */
            if (ds->len[s] + len + 2 > cap[s]) {
                cap[s] = (ds->len[s] + len + 2) * 2;
                ds->text[s] = xrealloc(ds->text[s], cap[s]);
            }
            if (first_no[s] < 0) first_no[s] = d->lines[i].no;
            memcpy(ds->text[s] + ds->len[s], d->lines[i].s, len);
            ds->len[s] += (uint32_t)len;
            ds->text[s][ds->len[s]++] = '\n';
        }
    }
    for (int s = 0; s < 2; s++) {
        if (!ds->len[s]) continue;
        /* From the file's first line (a whole read, a created file) nothing is open before it. */
        const char *pre = first_no[s] == 1 ? "" : syn_fragment_prefix(lang, ds->text[s], ds->len[s]);
        uint32_t pn = (uint32_t)strlen(pre);
        if (pn) { /* the lines move after it */
            ds->text[s] = xrealloc(ds->text[s], ds->len[s] + pn);
            memmove(ds->text[s] + pn, ds->text[s], ds->len[s]);
            memcpy(ds->text[s], pre, pn);
            ds->len[s] += pn;
            for (int i = 0; i < d->n; i++)
                if (ds->side[i] == s + 1) ds->off[i] += pn;
        }
        ds->doc[s] = syn_doc(lang, ds->text[s], ds->len[s]);
    }
    ShState sh = {0};
    bool cmd = false;
    for (int i = 0; i < d->n; i++) {
        const DetailLine *dl = &d->lines[i];
        if (dl->kind != DL_HEAD) continue;
        bool first = !i || d->lines[i - 1].kind != DL_HEAD;
        if (first) cmd = !strncmp(dl->s, "$ ", 2), sh = (ShState){0};
        ds->st[i] = xcalloc(strlen(dl->s) + 1, 1);
        if (cmd) style_shell(&sh, dl->s, ds->st[i], first);
        style_words(dl->s, ds->st[i], NULL, 0, false);
    }
}

#define STYLE_BLOCK 128 /* lines styled together: one query run per side */

/* The style of line i, computing its block of lines on first use. */
const unsigned char *ui_style_line(DetStyle *ds, const Detail *d, int i) {
    if (ds->st[i]) return ds->st[i];
    int b0 = i - i % STYLE_BLOCK, b1 = b0 + STYLE_BLOCK < d->n ? b0 + STYLE_BLOCK : d->n;
    uint32_t lo[2] = {UINT32_MAX, UINT32_MAX}, hi[2] = {0, 0};
    for (int j = b0; j < b1; j++) {
        int s = ds->side[j] - 1;
        if (ds->st[j] || s < 0 || !ds->doc[s]) continue;
        uint32_t e = ds->off[j] + (uint32_t)strlen(d->lines[j].s);
        if (ds->off[j] < lo[s]) lo[s] = ds->off[j];
        if (e > hi[s]) hi[s] = e;
    }
    unsigned char *cls[2] = {NULL, NULL};
    for (int s = 0; s < 2; s++)
        if (hi[s] > lo[s]) {
            cls[s] = xmalloc(hi[s] - lo[s]);
            syn_highlight(ds->doc[s], lo[s], hi[s], cls[s]);
        }
    for (int j = b0; j < b1; j++) {
        if (ds->st[j]) continue;
        const DetailLine *dl = &d->lines[j];
        int n = (int)strlen(dl->s), s = ds->side[j] - 1;
        unsigned char *st = xcalloc((size_t)n + 1, 1);
        bool styled = false;
        if (s >= 0 && cls[s])
            for (int k = 0; k < n; k++) styled |= (st[k] = syn_style(cls[s][ds->off[j] - lo[s] + (uint32_t)k])) != 0;
        /* A markdown heading the grammar missed (no grammar, or a fragment that
         * starts inside a code fence) */
        if (s >= 0 && !styled && ds->kw && !*ds->kw && dl->s[0] == '#' && dl->kind != DL_DEL)
            memset(st, ST_BOLD | ST_KEY, (size_t)n);
        style_words(dl->s, st, s >= 0 && !ds->doc[s] ? ds->kw : NULL, ST_BOLD | ST_KEY,
                    !ds->file && s < 0 && (dl->kind == DL_TEXT || dl->kind == DL_ERR));
        ds->st[j] = st;
    }
    free(cls[0]);
    free(cls[1]);
    return ds->st[i];
}

/* Is line i highlighted code (drawn in the text color, not the diff's)? */
bool ui_style_syntax(const DetStyle *ds, int i) { return ds->side[i] && ds->doc[ds->side[i] - 1]; }

void ui_style_free(DetStyle *ds, int n) {
    for (int i = 0; ds->st && i < n; i++) free(ds->st[i]);
    free(ds->st);
    free(ds->side);
    free(ds->off);
    for (int s = 0; s < 2; s++) syn_doc_free(ds->doc[s]), free(ds->text[s]);
    memset(ds, 0, sizeof *ds);
}

/* One wrapped row of a detail line, in runs of the same style. Runs are placed
 * by cell, so bold never shifts a column. */
void ui_draw_row(const DetailLine *dl, const unsigned char *st, const DetRow *row, float x, float y, float cw,
                 Color c, float alpha) {
    const char *s = dl->s;
    int end = row->start + row->len;
    char buf[DET_ROW_BYTES];
    int n = 0, cells = 0, run_cells = 0;
    unsigned char run = 0;
    for (int k = row->start;;) {
        if (n && (k == end || st[k] != run || n >= (int)sizeof buf - 5)) {
            buf[n] = 0;
            Color rc = ui_fade(style_color(run & ST_COLOR, c), alpha);
            ui_draw_text(run & ST_BOLD ? ui_mono_bold : ui_mono, buf, (Vector2){x + (float)cells * cw, y}, DET_FONT, 0, rc);
            cells += run_cells;
            n = run_cells = 0;
        }
        if (k == end) break;
        run = st[k];
        int cp = 0; /* one whole codepoint; invalid UTF-8 can run on, so at most 4 bytes */
        do buf[n++] = s[k++];
        while (k < end && ((unsigned char)s[k] & 0xC0) == 0x80 && ++cp < 4);
        run_cells++;
    }
}

void ui_lineno(int no, float x, float y) {
    char buf[16];
    snprintf(buf, sizeof buf, "%6d", no);
    ui_draw_text(ui_mono, buf, (Vector2){x, y}, DET_FONT, 0, ui_fade(DIM, 0.7f));
}

float ui_code_cell(void) {
    static float cw;
    static unsigned tex;
    if (tex != ui_mono.texture.id) cw = MeasureTextEx(ui_mono, "M", DET_FONT, 0).x, tex = ui_mono.texture.id;
    return cw;
}

/* The lines as a Detail of plain text lines numbered as in the file: what a
 * read of path would show, so styling goes through the detail window's. */
void ui_code_init(CodeView *cv, const char *path, const char *text, int first, int last) {
    memset(cv, 0, sizeof *cv);
    if (!path || !text) return;
    if (first < 1) first = 1;
    const char *p = text;
    for (int no = 1; *p && no < first; no++) {
        const char *nl = strchr(p, '\n');
        p = nl ? nl + 1 : p + strlen(p);
    }
    const char *e = p;
    for (int no = first; *e && no <= last; no++) {
        const char *nl = strchr(e, '\n');
        e = nl ? nl + 1 : e + strlen(e);
    }
    char *slice = xstrndup(p, (size_t)(e - p));
    cv->d = detail_new(path);
    if (*slice) detail_line(cv->d, DL_TEXT, slice);
    free(slice);
    cv->n = cv->d->n;
    cv->first = first;
    for (int i = 0; i < cv->n; i++)
        if (cv->d->lines[i].kind == DL_TEXT) cv->d->lines[i].no = first + i;
    ui_style_init(&cv->st, cv->d, EV_READ, path);
    cv->st.file = true;
}

void ui_code_free(CodeView *cv) {
    if (cv->d) {
        ui_style_free(&cv->st, cv->d->n);
        detail_free(cv->d);
    }
    memset(cv, 0, sizeof *cv);
}

void ui_code_row(CodeView *cv, int i, float x, float y, Color base, float alpha, float x0, float x1) {
    if (!cv->d || i < 0 || i >= cv->n) return;
    const char *s = cv->d->lines[i].s;
    float cw = ui_code_cell();
    int c0 = x0 > x ? (int)((x0 - x) / cw) : 0, c1 = (int)ceilf((x1 - x) / cw), cell = 0, a = -1, b = 0;
    for (int k = 0;; cell++) { /* the bytes of cells c0..c1 */
        if (cell == c0) a = k;
        if (!s[k] || cell >= c1) { b = k; break; }
        k++;
        while (((unsigned char)s[k] & 0xC0) == 0x80) k++;
    }
    if (a < 0 || b <= a) return;
    DetRow row = {i, a, b - a};
    ui_draw_row(&cv->d->lines[i], ui_style_line(&cv->st, cv->d, i), &row, x + (float)c0 * cw, y, cw, base, alpha);
}

int ui_code_cols(const CodeView *cv) {
    int most = 0;
    for (int i = 0; cv->d && i < cv->n; i++) {
        int c = 0;
        for (const char *p = cv->d->lines[i].s; *p; p++) c += ((unsigned char)*p & 0xC0) != 0x80;
        if (c > most) most = c;
    }
    return most;
}

float ui_note_panel_w(void) { return fminf(400, fmaxf(280, (float)GetScreenWidth() * 0.24f)); }

/* Height of a comments panel w wide: its boxes, up to maxh. */
float ui_note_panel_h(NoteView *nv, float w, float maxh) {
    if (nv->w != w - 44 || nv->size != NOTE_FONT) nv_wrap(nv, w - 44, NOTE_FONT);
    return fminf(maxh, NOTE_HEAD + nv_height(nv, NULL) + 10);
}

/* The comments panel at r (height from ui_note_panel_h). A preview doesn't scroll:
 * what doesn't fit fades out. */
void ui_note_panel(NoteView *nv, Rectangle r, float *scroll, Color ac, bool preview) {
    Color bg = {16, 20, 26, 250};
    float round = 10 / fminf(r.width, r.height);
    DrawRectangleRounded(r, round, 6, bg);
    DrawRectangleRoundedLinesEx(r, round, 6, 1.5f, ui_fade(ac, preview ? 0.35f : 0.55f));
    ui_bubble_icon(r.x + 14, r.y + 14, ac);
    char title[64];
    snprintf(title, sizeof title, nv->ns.n == 1 ? "Comment" : "Comments  \xC2\xB7  %d", nv->ns.n);
    ui_text(title, r.x + 34, r.y + 10, 15, ac);
    Rectangle body = {r.x + 10, r.y + NOTE_HEAD, r.width - 20, r.height - NOTE_HEAD - 10};
    float max_scroll = fmaxf(0, nv_height(nv, NULL) - body.height);
    const char *hint = preview ? "before this step  \xC2\xB7  click to open"
                     : max_scroll > 0 ? "wheel scroll  \xC2\xB7  Ctrl+C copy" : "Ctrl+C copy";
    char fitbuf[128];
    ui_fit(fitbuf, sizeof fitbuf, hint, 12, r.width - 44 - ui_text_w(title, 15) - 20);
    ui_text(fitbuf, r.x + r.width - 12 - ui_text_w(fitbuf, 12), r.y + 13, 12, DIM);
    DrawLine((int)r.x + 10, (int)(r.y + NOTE_HEAD - 6), (int)(r.x + r.width - 10), (int)(r.y + NOTE_HEAD - 6), ui_fade(DIM, 0.3f));
    if (*scroll > max_scroll) *scroll = max_scroll;
    if (*scroll < 0) *scroll = 0;
    if (body.height <= 0) return; /* window too small for any of it */
    nv_draw(nv, body, *scroll, ac, true, NULL);
    if (max_scroll <= 0) return;
    if (preview) {
        DrawRectangleGradientV((int)body.x, (int)(body.y + body.height - 28), (int)body.width, 28, ui_fade(bg, 0), bg);
        return;
    }
    float bh = fmaxf(20, body.height * body.height / nv_height(nv, NULL));
    float by = body.y + (body.height - bh) * (*scroll / max_scroll);
    DrawRectangleRounded((Rectangle){r.x + r.width - 7, by, 3, bh}, 1, 4, ui_fade(DIM, 0.6f));
}