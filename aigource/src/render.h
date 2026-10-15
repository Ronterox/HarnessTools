#ifndef AIG_RENDER_H
#define AIG_RENDER_H
#include "event.h"

typedef struct {
    const char *root;
    double speed;      /* replay: sim seconds per real second */
    int live;          /* skip the replay animation */
    int width, height;
    const char *shot;  /* save a PNG after `frames` frames and exit */
    int frames;
    double seek;           /* test hook: start paused at this fraction of the timeline (<0 = off) */
    float mouse_x, mouse_y; /* test hook: mouse position for --shot (<0 = real mouse) */
    const char *turn;      /* test hook: open this actor's turn window (id prefix) */
    const char *step;      /* test hook: then open its step N's detail (negative: from the end) */
    const char *file;      /* test hook: open this file's last change, as a click on it does */
} VizOpts;

int viz_run(const VizOpts *o, EvQueue *q);

#endif
