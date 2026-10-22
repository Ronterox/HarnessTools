#ifndef AIG_CVIEW_INT_H
#define AIG_CVIEW_INT_H
/* Shared by cview.c (the screen, its input, the side panel and the strip)
 * and cwin.c (the floating windows). */
#include <stdbool.h>

#include "clay.h"
#include "context.h"
#include "raylib.h"
#include "sview_int.h" /* the draft's palette and the text helpers */
#include "ui.h"

#define C_WARN HEX(0xff7b72)
#define C_RESP HEX(0x111a16) /* this call's response: its window's background */

/* item kinds, as the draft colours them */
static inline Color cv_kind_col(int k) {
    static const unsigned col[CK_COUNT] = {0x6e7681, 0x6e7681, 0xbc8cff, 0xf2a541, 0xe3b341,
                                           0x9ecbff, 0x79c0ff, 0x56d364, 0x39c5cf, 0xf85149};
    return k >= 0 && k < CK_COUNT ? HEX(col[k]) : HEX(0x888888);
}

/* A window's body: its content as rows, laid out for one width (cwin.c). */
enum { CR_MONO, CR_PROSE, CR_NOTE, CR_CODE, CR_TOOL };
typedef struct {
    float y, h, x;      /* top, height, left: in the content, unscrolled */
    unsigned char k;
    unsigned char font; /* prose and notes: F_* */
    bool box;           /* inside a fenced block: on its background */
    float size;         /* css px */
    Color c;
    int s;              /* the body's string */
    int a, n;           /* its bytes */
    int line;           /* CR_CODE: the code view's line; CR_TOOL: the tool */
} CRow;

typedef struct {
    float w, h;         /* laid out for content width w: its height */
    CRow *rows;
    int n, cap;
    char **str;         /* texts the rows point into (tabs as spaces, markdown markers dropped) */
    unsigned char **st; /* each one's UI_PLAIN / UI_BOLD / UI_CODE per byte, NULL plain */
    int nstr, capstr;
    CodeView cv;        /* a Read's lines, a Write's content: at most one per window */
    int *nums;          /* each code line's number in its file */
    int cols;           /* the longest code line, cells */
    float gut;          /* the gutter's width */
    bool read;          /* the code is a Read's: a click opens the file in nvim */
    unsigned char *tool_open; /* the tool definitions shown (CK_TOOLS) */
} CBody;

/* One item's window while its call shows it. x, y, w, h: the layout's
 * (centre, size, world px); it is drawn where its animation is. */
typedef struct {
    int item, kind;
    float x, y, w, h;
    float ax, ay, aa;   /* drawn: from where, with what alpha, the animation started */
    double t0;          /* when it started (CView clock), < 0 none */
    bool open, fresh, resp, vis;
    unsigned z;         /* drawing order: when it was made */
    float scroll, hscroll;
    CBody *body;        /* open and seen once */
    char title[512];
    char fit[512];      /* the title cut to fit_w */
    float fit_w;
    /* the header at call hdr_c */
    int hdr_c;
    ClFacts f;
    ClRun meta[CL_RUNS];
    int nmeta;
    char tok[24];
    bool warn;          /* stale with a change not its own: ⚠ after the title */
    float meta_w, meta_h; /* the meta row laid out for width meta_w */
    float rx[CL_RUNS], ry[CL_RUNS]; /* each run's place in it */
    /* leaving */
    double gone;        /* when it left (CView clock) */
    unsigned char leave; /* LV_*: how */
    float gx, gy;       /* where it goes: a compaction's summary, else where it was */
} CNode;

/* What a window needs to draw itself. */
typedef struct {
    const CtxAgent *a;
    int c;
    Camera2D cam;       /* the world's: clipping to its content is done on screen */
    Rectangle stage;    /* screen */
    Vector2 mw;         /* the mouse, world; far away when it isn't over the stage */
    bool nvim;          /* an nvim works in the root: Read lines are links */
    bool busy;          /* a drag is on: nothing lights up */
    double clock;
} CwCtx;

/* How a window leaves: it fades; it folds into a compaction's summary; cleared thinking greys out, slower. */
enum { LV_FADE, LV_FOLD, LV_THINK };

/* Parts of a window under a point. */
enum { CW_NONE, CW_TITLE, CW_WHY, CW_FOLD, CW_GRIP, CW_BODY, CW_CODE, CW_TOOL, CW_STALE, CW_META };

#define CW_HD 24.0f     /* the title bar: a chip's whole height */
#define CW_ROW 18.0f    /* a mono or code row (DET_ROW) */
#define CW_ANIM 0.45    /* a window's move or fade, seconds */

/* Where n is drawn now (its animation), and its alpha. */
void cw_pos(const CNode *n, double clock, float *x, float *y, float *a);
float cw_scale(const CNode *n, double clock); /* a fold shrinks it into the summary (the draft's scale(0.08)) */
double cw_life(const CNode *n);               /* a leaving window's animation, seconds */
/* The header's facts and words at call c, once per call (when hdr_c != c). */
void cw_header(CNode *n, const CtxAgent *a, int c, const int *by_item);
/* Draw n at x, y (centre, world) with alpha al; linked, sel: outlined. Its
 * header must be the call's (cw_header). */
void cw_draw(CNode *n, const CwCtx *x, float cx, float cy, float al, bool linked, bool sel);
/* The part of n (drawn at cx, cy) under world point p; a code line's number
 * in its file into *line, a tool's index into *line for CW_TOOL. */
int cw_hit(CNode *n, float cx, float cy, Vector2 p, int *line);
/* The wheel over n's content: scroll it (shift: sideways, code). False when
 * p isn't over its content. */
bool cw_wheel(CNode *n, float cx, float cy, Vector2 p, float wheel, bool shift);
void cw_toggle_tool(CNode *n, int tool);
void cw_body_free(CNode *n);

#endif
