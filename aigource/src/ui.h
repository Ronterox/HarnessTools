#ifndef AIG_UI_H
#define AIG_UI_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "event.h"
#include "raylib.h"
#include "syntax.h"
#include "transcript.h"

/* Drawing shared by the stage and the structure screen: fonts, text, the
 * palette, code with syntax and word styles, the agent's comments as boxes.
 * Everything draws in screen space; nothing here knows the scene. */

#define BG (Color){13, 17, 23, 255}
#define PANEL (Color){22, 27, 34, 235}
#define DIM (Color){139, 148, 158, 255}
#define TEXT (Color){230, 237, 243, 255}
#define LIVE_RED (Color){255, 85, 85, 255}
/* Commands in the detail window: a soft lavender, cool like the background
 * (no glare on dark blue), clearly apart from the white of plain text. */
#define CMD_TEXT (Color){180, 190, 254, 255}
/* What stands out in it, bold and in its own color: keywords and programs
 * (mauve), numbers (peach), dates and times (sky). Pastels of the same
 * lightness, apart in hue from each other, the white text and the diff's
 * green and red. */
#define KEY_TEXT (Color){203, 166, 247, 255}
#define NUM_TEXT (Color){250, 179, 135, 255}
#define DATE_TEXT (Color){137, 220, 235, 255}
/* Code, highlighted by tree-sitter, adds: strings (green), functions (blue),
 * types (teal), builtins and escapes (pink); fields take the lavender. */
#define STR_TEXT (Color){166, 227, 161, 255}
#define FUNC_TEXT (Color){137, 180, 250, 255}
#define TYPE_TEXT (Color){148, 226, 213, 255}
#define BUILTIN_TEXT (Color){245, 194, 231, 255}

/* ---- fonts and text ----------------------------------------------------- */

extern Font ui_font, ui_font_bold, ui_mono, ui_mono_bold; /* UI sans and its bold; code mono and its bold, same cell width */

/* After InitWindow: loads the fonts and the user's keywords. */
void ui_init(void);
/* raylib's DrawTextEx / MeasureTextEx, faster for one line of text. */
void ui_draw_text(Font f, const char *s, Vector2 pos, float size, float spacing, Color tint);
float ui_measure_text(Font f, const char *s, float size, float spacing);
/* In the UI font at the house spacing. */
void ui_text(const char *s, float x, float y, float size, Color c);
float ui_text_w(const char *s, float size);
Color ui_fade(Color c, float a); /* alpha times a, clamped to 0..1 */
void ui_fit(char *out, size_t n, const char *s, float size, float maxw); /* s cut to maxw pixels, with "…" */
void ui_fit_font(Font f, char *out, size_t n, const char *s, float size, float spacing, float maxw);
float ui_spacing(Font f, float size); /* the house letter spacing of a font: sans a little, mono none */
void ui_fmt_count(char *buf, size_t n, long v);                          /* 12,345 */
void ui_fmt_ago(char *buf, size_t n, double secs);                       /* 3m 05s ago */
/* The mouse cursor: the pointing hand over what a click opens (code: in
 * nvim), else the default. Once a frame; raylib is told only on a change. */
void ui_hand(bool on);
/* A line being typed into q (cap bytes, up to maxchars characters): this
 * frame's text, Backspace (Ctrl: a word, as Ctrl+W). all: the text is
 * selected, and what is typed replaces it. True when q changed. */
bool ui_edit_line(char *q, size_t cap, int maxchars, bool *all, bool ctrl);
/* A rounded box: filled, then outlined thick. */
void ui_box(Rectangle r, float roundness, Color fill, float thick, Color line);
/* A scrollbar's thumb, w wide at x on a track len long from y: view of full
 * shown, scrolled scroll of maxs; at least minh high. */
void ui_thumb(float x, float y, float w, float len, float view, float full, float scroll, float maxs, float minh, Color c);

/* ---- rich text: **bold** and `code` (the agent's markdown), wrapped ---- */

enum { UI_PLAIN, UI_BOLD, UI_CODE }; /* a byte's style: f, its bold fb, or mono in lavender */
typedef struct { int start, len; } UiSpan;
/* src with its **bold** and `code` (each on one line) as styles, markers
 * dropped: *out and *st (one per byte), both the caller's to free. */
void ui_md(const char *src, char **out, unsigned char **st);
/* Width and drawing of s[0..n) with styles st (NULL: all plain) in f / fb. */
float ui_rich_w(Font f, Font fb, const char *s, const unsigned char *st, int n, float size);
float ui_rich_draw(Font f, Font fb, const char *s, const unsigned char *st, int n, float x, float y, float size, Color c,
                   Color bold);
/* s[0..n) word-wrapped at w pixels (the first row `indent` narrower), '\n'
 * breaking; words wider than a row are cut. Rows into *rows (grown, *cap);
 * returns their count. */
int ui_wrap(Font f, Font fb, const char *s, const unsigned char *st, int n, float size, float w, float indent,
            UiSpan **rows, int *cap);

/* ---- code and styled lines ---------------------------------------------- */

#define DET_FONT 14.0f /* code size, and the row it sits in */
#define DET_ROW 18.0f
#define DET_ROW_BYTES 2048

/* How a detail's lines are styled. Commands when it loads (a heredoc spans
 * lines), everything else when a line is first drawn: code a block of lines
 * at a time, highlighted on the side of the diff it belongs to. */
typedef struct {
    unsigned char **st;  /* per line: the style of each byte, NULL until drawn */
    unsigned char *side; /* per line: 0 not code, 1 the new text, 2 the old one */
    uint32_t *off;       /* where the line starts in its side's text */
    char *text[2];       /* new side (context + added lines), old side (context + removed) */
    uint32_t len[2];
    SynDoc *doc[2];      /* NULL: no grammar installed, the keyword lists instead */
    const char *kw;      /* keyword list without a grammar; "" = markdown headings */
    bool file;           /* a file's text (CodeView): no words read as a command's outcome */
} DetStyle;

/* One wrapped row of a detail line: bytes start..start+len of line `line`. */
typedef struct { int line, start, len; } DetRow;

/* Style detail d of a step of kind k on path (nothing is parsed until a line is drawn). */
void ui_style_init(DetStyle *ds, const Detail *d, EvKind k, const char *path);
/* Per-byte styles of line i, computing its block of lines on first use. */
const unsigned char *ui_style_line(DetStyle *ds, const Detail *d, int i);
/* Is line i highlighted code (drawn in the text color, not the diff's)? */
bool ui_style_syntax(const DetStyle *ds, int i);
void ui_style_free(DetStyle *ds, int n); /* n = the detail's line count */
/* One row of a detail line in runs of one style, by monospace cell cw wide;
 * c is the line's own color, every color's alpha times alpha. */
void ui_draw_row(const DetailLine *dl, const unsigned char *st, const DetRow *row, float x, float y, float cw,
                 Color c, float alpha);
void ui_lineno(int no, float x, float y); /* a line number, right-aligned in 6 cells at x */

/* Lines of a file's text, styled as the detail window styles a read of it.
 * Rows are DET_ROW high, cells ui_mono's width; the caller wraps nothing and
 * clips with a scissor. */
typedef struct {
    int n, first; /* lines held; the file's number of the first */
    Detail *d;
    DetStyle st;
} CodeView;

/* Lines first..last (1-based, first clamped to 1) of text, the content of the
 * file at path (whose extension picks the language). A NULL path or text
 * gives an empty view. */
void ui_code_init(CodeView *cv, const char *path, const char *text, int first, int last);
void ui_code_free(CodeView *cv); /* also safe on a zeroed or empty view */
float ui_code_cell(void); /* width of one character cell */
/* Line i (0-based) of the view, its text starting at x, y; base is the color
 * of unstyled text, alpha dims all of it. Only the cells between x0 and x1
 * are drawn. */
void ui_code_row(CodeView *cv, int i, float x, float y, Color base, float alpha, float x0, float x1);
int ui_code_cols(const CodeView *cv); /* its longest line, in cells */

/* ---- comments: an agent's text before a step, as message boxes ---------- */

#define NOTE_ROW 19.0f /* one line of a comment */

typedef struct {
    Notes ns;            /* the texts, markdown markers dropped */
    unsigned char **st;  /* per text: each byte's UI_PLAIN/UI_BOLD/UI_CODE */
    float w, size;       /* text width and size the rows are wrapped for; 0 = rewrap */
    struct NoteRow { int note, start, len; } *rows;
    int nrows, cap;
} NoteView;

/* How ui_nv_boxes draws: the draft's .note (or .note.earlier: dashed, dim). */
typedef struct {
    Color bg, border, text, bold, head_color;
    bool dashed;
    float size, radius, pad, pad_x, gap; /* text size; corner radius; padding top/bottom, sides; between boxes */
    const char *head;                    /* a caption inside each box, above its text; NULL none */
    float head_size, head_h;
} UiNoteStyle;

void ui_nv_clear(NoteView *nv); /* drops the comments; the rows' memory stays for the next load */
void ui_nv_load(NoteView *nv, const Event *e); /* the comments of step e */
void ui_nv_set(NoteView *nv, const char *const *texts, int n); /* these texts (copied) as the comments */
/* The comments as message boxes, without the panel, in style ns: their height
 * at width w, and drawn into r (boxes outside r are skipped; the caller clips). */
float ui_nv_boxes_h(NoteView *nv, float w, const UiNoteStyle *ns);
void ui_nv_boxes(NoteView *nv, Rectangle r, const UiNoteStyle *ns);
float ui_note_panel_w(void);
/* Height of a comments panel w wide: its boxes, up to maxh. */
float ui_note_panel_h(NoteView *nv, float w, float maxh);
/* The comments panel at r (height from ui_note_panel_h). A preview doesn't
 * scroll: what doesn't fit fades out. */
void ui_note_panel(NoteView *nv, Rectangle r, float *scroll, Color ac, bool preview);
void ui_bubble_icon(float x, float y, Color c); /* a small speech bubble */

/* ---- turn bars: the structure screen's turn strip and the stage's timeline ---- */

/* A turn's tags. One bar shows them all: the fill is the most serious of
 * corrected/interrupted > fix > plain (selected: amber, outlined), a stripe at
 * the base failing > untested, a cyan top edge slow. */
enum { UT_FIX = 1, UT_CORR = 2, UT_UNTESTED = 4, UT_FAILING = 8, UT_SLOW = 16 };
#define UT_PLAIN (Color){59, 90, 122, 255}     /* #3b5a7a */
#define UT_FIX_C (Color){247, 120, 186, 255}   /* pink: the prompt reports a problem (coral in the draft) */
#define UT_CORR_C (Color){255, 123, 114, 255}  /* coral: the next prompt corrected it, or it was interrupted (the flag's) */
#define UT_FAIL_C (Color){248, 81, 73, 255}    /* red: the last build/test after a change failed */
#define UT_TEST_C (Color){227, 179, 65, 255}   /* yellow: a change with no build/test after it */
#define UT_SLOW_C (Color){57, 197, 207, 255}   /* cyan: took at least 2x the average turn */
#define UT_SEL_C (Color){242, 165, 65, 255}    /* amber: the selected turn */

/* The bar of a turn with these tags at q (its full height, standing on q's
 * bottom); sel: amber with an outline; hot: brighter (hovered). */
void ui_turn_bar(Rectangle q, int tags, bool sel, bool hot);
/* Its tags in words, ", "-joined ("" none). */
void ui_turn_tags(char *b, size_t n, int tags);
/* The bars' legend from x, centred on cy, in f at size: its width; drawn
 * only when draw. */
float ui_turn_legend(Font f, float size, float x, float cy, Color text, bool draw);

#endif
