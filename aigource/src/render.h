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
} VizOpts;

int viz_run(const VizOpts *o, EvQueue *q);

#endif
