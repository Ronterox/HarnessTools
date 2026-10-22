#ifndef AIG_CVIEW_H
#define AIG_CVIEW_H
#include <limits.h>
#include <stdbool.h>

#include "raylib.h"

/* The context screen (draft/context/context.tpl.html, natively): what one
 * agent had in its context window at one of its API calls, as floating
 * windows around it, newest nearest; the calls strip to move through them,
 * the context's make-up, the actions and what likely led to the one picked.
 * An agent's transcript is read on demand (context.c's ctx_load) on a worker
 * thread, one load at a time; a CtxProject keeps the project's changes
 * between loads for as long as the screen lives. Opening it on another
 * agent than the one loading drops that load. Live: while open, the shown
 * agent's transcript is watched and reloaded (ctx_reload) once it stops
 * growing for a moment, keeping what the user did (window places, sizes,
 * open or folded, scroll, the selection, the filter, the view) by item key;
 * and it shows the call of the stage's clock until moved on its own. */
typedef struct CView CView;

CView *cv_new(const char *root);
/* Cancels and joins a load in flight, frees everything. */
void cv_free(CView *c);

#define CV_NONE INT_MIN
/* Open the screen on the agent whose transcript is path. call: the call to
 * show (negative from the end), else (CV_NONE) the last at or before epoch
 * t, the last when t is 0, and the stage's clock is followed; sel: an
 * action (its item) to select, which shows the call that made it; -1 none.
 * The agent already shown isn't loaded again (the live reloads keep it
 * fresh), nor one already on its way. */
void cv_open(CView *c, const char *path, int call, int sel, double t);
void cv_close(CView *c); /* cancels a load in flight */
bool cv_is_open(const CView *c);
/* Open, nothing loading, and an agent shown or why there is none: what a --shot waits for. */
bool cv_ready(const CView *c);
/* The stage's clock (epoch): the screen shows the last call at or before
 * it until a scrub, a step, play or an action detaches it (L follows again). */
void cv_clock(CView *c, double t);
/* The actions filter has focus: every key is its. */
bool cv_typing(const CView *c);
/* The screen's keys, before anything is drawn this frame; true when it used
 * Esc (which may also have closed it). */
bool cv_keys(CView *c);
/* One frame of the screen between BeginDrawing and EndDrawing: a finished
 * load swapped in, the mouse, the drawing; dt drives play and animations. */
void cv_frame(CView *c, Vector2 mouse, float dt);

#endif
