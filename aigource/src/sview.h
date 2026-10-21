#ifndef AIG_SVIEW_H
#define AIG_SVIEW_H
#include <stdbool.h>
#include <stddef.h>

#include "event.h"
#include "raylib.h"

/* The structure screen (aigource/draft/structure.tpl.html, natively): the
 * repo as icicle, treemap or floating nodes coloured by a heat mode, an
 * inspector, the turn strip. It owns an Analysis fed every event the viz
 * drains once it exists; builds (an_build, then st_build) run on a worker
 * thread, one at a time, and the finished (snapshot, metrics) pair is swapped
 * in at the start of a frame, the old pair freed there, after the last frame
 * that drew it. While open, new events rebuild it (debounced); while closed
 * nothing builds, and opening rebuilds when events arrived. */
typedef struct SView SView;

SView *sv_new(const char *root);
/* Cancels and joins a running build, frees everything. */
void sv_free(SView *s);
/* Events for the analysis (copied), each once; any kind. */
void sv_add(SView *s, const Event *evs, size_t n);
/* S: open (building first, or again when events arrived since the last
 * build) or close. */
void sv_toggle(SView *s);
bool sv_is_open(const SView *s);
/* Open, a build drawn and none running: what a --shot waits for. */
bool sv_ready(const SView *s);
/* Test hooks: --structure VIEW[:MODE[:PATH]] and --structure-turn N (1-based
 * in time order, negative from the end, 0 none); opens the screen and applies
 * them once the first build is in. */
void sv_hooks(SView *s, const char *spec, int turn);
/* Test hook: --structure-search QUERY and --structure-pick N; opens the
 * screen with the search box showing QUERY's results, then goes to result N
 * (1-based) as Enter does, 0 none. */
void sv_search(SView *s, const char *query, int pick);
/* The search box has focus: every key is its (S and Q included). */
bool sv_typing(const SView *s);
/* The screen's keys, before anything is drawn this frame; true when it used
 * Esc (which may also have closed it). */
bool sv_keys(SView *s);
/* One frame of the screen between BeginDrawing and EndDrawing: the mouse,
 * a swap of a finished build, the drawing; dt drives the floating nodes. */
void sv_frame(SView *s, Vector2 mouse, float dt);

#endif
