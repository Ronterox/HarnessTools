#ifndef AIG_SVIEW_INT_H
#define AIG_SVIEW_INT_H
/* Shared by sview.c (screen, stage, strip) and sinsp.c (the inspector). */
#include <stdbool.h>

#include "analysis.h"
#include "metrics.h"
#include "raylib.h"
#include "ui.h"

/* The draft's palette */
#define HEX(v) ((Color){(unsigned char)((v) >> 16 & 255), (unsigned char)((v) >> 8 & 255), (unsigned char)((v)&255), 255})
#define C_BG HEX(0x0d1117)
#define C_WIN HEX(0x10161e)
#define C_PANEL HEX(0x131a23)
#define C_RAISED HEX(0x18212c)
#define C_LINE HEX(0x253041)
#define C_FG HEX(0xe6edf3)
#define C_SOFT HEX(0xc9d1d9)
#define C_DIM HEX(0x8b949e)
#define C_FAINT HEX(0x5b6673)
#define C_HOT HEX(0xf2a541)
#define C_FOCUS HEX(0x9ecbff)
#define C_ADD HEX(0x56d364)
#define C_DEL HEX(0xf85149)
#define C_CORR HEX(0xff7b72)
#define C_TEST HEX(0xe3b341)
#define C_BLIND HEX(0xbc8cff)
#define C_SLOW HEX(0x39c5cf)
#define C_UNKNOWN HEX(0x2a3442) /* lines from before these transcripts */
#define C_CXLINE HEX(0x79c0ff)

/* CSS px as raylib sizes: raylib's size is ascent + descent, ~1.16 em for
 * DejaVu, so a CSS 12px mono (7.2 px cells) is size 14. */
#define PX(css) ((css)*1.16f)
#define TIP_DELAY 0.35f /* a title shows after hovering this long */

enum { F_SANS, F_SANS_B, F_MONO, F_MONO_B };
Font sv_font(int f);
Font sv_bold(int f); /* the bold of f's family */
float sv_tw(int f, const char *s, float css);
float sv_tws(int f, const char *s, float css, float sp);
/* Text with its top at y (raylib's origin); sp: extra letter spacing in px. */
void sv_tx(int f, const char *s, float x, float y, float css, Color c);
void sv_txs(int f, const char *s, float x, float y, float css, float sp, Color c);
/* s cut to maxw px with "…" */
void sv_fit(int f, char *out, size_t n, const char *s, float css, float maxw);
/* y of the top of a line whose middle is at cy */
static inline float sv_mid(float cy, float css) { return cy - PX(css) * 0.5f; }

Color sv_darker(Color c, float k);
Color sv_brighter(Color c, float k);
Color sv_mix_lab(Color a, Color b, float t);
Color sv_ramp(double t);
Color sv_ink(Color c); /* text on c */
Color sv_actor_col(int slot);
Color sv_blame_col(int slot);
Color sv_flag_col(int k); /* STK_* */

double sv_jfix(double v, int digits); /* rounded as JavaScript's toFixed: half away from zero */
void sv_fmt_k(char *b, size_t n, long long v);
void sv_fmt_age(char *b, size_t n, double s);
void sv_fmt_dur(char *b, size_t n, double s);
void sv_when(char *b, size_t n, double ts);
void sv_plural(char *b, size_t n, long v, const char *w);
/* "before these transcripts", a session's title, a subagent's "type · task" (first: the part before " · ") */
void sv_actor_name(const AnSnapshot *S, int a, bool first, char *b, size_t n);
/* The metric in words, as the draft's METRIC_WORD */
void sv_metric_word(const StMetrics *m, const StNode *d, int mode, char *b, size_t n);
const char *sv_kind_word(int kind);
bool sv_is_def(const StNode *d);
/* A small dark box at `at` with a bold part and the rest (either may be NULL or ""). */
void sv_tooltip(const char *bold, const char *rest, Vector2 at);

/* ---- the inspector (sinsp.c) ---- */

enum { IT_TEXT, IT_BOX, IT_DOT, IT_LINK, IT_LEGEND, IT_CODE, IT_CARD, IT_SPARK, IT_NOTE, IT_LINE, IT_TIP };

typedef struct {
    unsigned char k, font;
    bool hl;        /* text: takes its row's hover colour */
    float x, y, w, h, size, sp, radius, bw;
    Color c, c2;    /* text or fill; border (a link: its hover background, then its hover text colour) */
    char *s;
    unsigned char *st; /* text: per byte UI_PLAIN/UI_BOLD/UI_CODE, NULL plain */
    char *tip;      /* a title shown when hovered */
    int node, turn; /* a link's target (-1 none) */
    int aux;        /* legend: its turn (-1 unknown, INT_MIN none); note: its NoteView */
} IItem;

typedef struct {
    IItem *it;
    int n, cap;
    float h;
    NoteView *nv;
    UiNoteStyle *nvs;
    int nnv, capnv;
    CodeView cv;
    int *orig, norig, line0, cols; /* code: each line's origin, first line number, longest line */
    int code_node;                 /* whose legend colours the strips */
    int mark;                      /* code: the file line a search hit lights, 0 none */
    float code_scroll, code_hscroll;
    int card;                      /* blame card item, -1 */
    const StMetrics *m;
    int node, turn, mode;
    float w;
} Doc;

typedef struct {
    Doc doc, card;     /* card: the blame card's text */
    float scroll;
    char key[512];     /* what it shows, the same across builds */
    int legend_turn;   /* hovered legend row's turn, INT_MIN none */
    int strip_line;    /* hovered strip (code line index), -1 */
    int card_line;     /* the line the card shows: the last strip hovered, -1 */
    const char *tip;   /* this frame: the hovered item's title */
    Vector2 tip_at;
    bool nvim;         /* this frame: the mouse is on a code line an nvim in the root can open */
    /* a search hit: node's code shows file line `line`, lit, with q's matches
     * marked; jump: scroll to it at the next layout */
    struct { int node, line; bool jump, cs; char q[256]; } mark;
} Insp;

typedef struct { int node, turn; } SiAct; /* a click: open a node, select a turn; -1 none */

/* Show node (or turn, when >= 0) at width w; fill: node colours for the
 * current mode (lists' dots). Laid out only when one of them changed; the
 * scroll stays while it shows the same thing (a rebuild keeps it). */
void si_show(Insp *in, const StMetrics *m, const Color *fill, int mode, int node, int turn, float w);
/* A search hit: when node is shown, its code shows file line `line` lit and
 * scrolled into view (once), q's matches in it marked (cs: case-sensitive);
 * node -1 clears it. */
void si_mark(Insp *in, int node, int line, const char *q, bool cs);
void si_draw(Insp *in, Rectangle r, Vector2 mp);
SiAct si_click(Insp *in, Rectangle r, Vector2 mp);
/* The code line under mp (its number in the file; 0 none), its file node's id into *id. */
int si_code_at(const Insp *in, Rectangle r, Vector2 mp, const char **id);
void si_wheel(Insp *in, Rectangle r, Vector2 mp, float wheel, bool shift);
void si_free(Insp *in);

#endif
