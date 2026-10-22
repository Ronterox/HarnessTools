#ifndef AIG_SLAYOUT_H
#define AIG_SLAYOUT_H
#include <stdbool.h>

#include "metrics.h"

/* Geometry of the structure screen's views, as the draft lays them out with
 * d3 (structure.tpl.html: VIEWS): a partition for the icicle, a squarified
 * treemap, and the floating nodes' force simulation. Pure: no drawing, no
 * raylib, so tests/test_slayout.c checks it headless. Nodes are StMetrics'
 * (parents before children, children by value), indexed as there. */

typedef struct { float x0, y0, x1, y1; } LRect;

/* Levels below node i: 0 for a leaf (d3's node.height). */
int lay_height(const StNode *nodes, int i);

/* d3.partition's x extent of every node of the subtree at root, the root
 * spanning 0..1 (children by value, its own lines left over at the end).
 * x0/x1 are indexed by node; nodes outside the subtree are untouched. */
void lay_partition(const StNode *nodes, int root, float *x0, float *x1);

/* d3.treemap().tile(treemapSquarify.ratio(ratio)).size([w, h]).round(true)
 * .paddingOuter(3).paddingTop(header).paddingInner(2) of the subtree at
 * root, header 18 px for a group taller than 40 px, as the draft's. out is
 * indexed by node; nodes outside the subtree are untouched. */
#define LAY_TREEMAP_RATIO 1.3
void lay_treemap(const StNode *nodes, int root, float w, float h, double ratio, LRect *out);

/* Floating nodes: a hub (body 0, pinned at 0,0) and its children around it,
 * d3-force's link + many-body + collide + radial + x/y forces with the
 * draft's strengths, one tick per frame. Seeded: the same input gives the
 * same run. */
typedef struct {
    float x, y, vx, vy, r;
    float pad;        /* collision room beyond r (a label below needs more) */
    float phase;      /* drift */
    bool pinned;      /* the hub, or a body being dragged: stays at fx, fy */
    float fx, fy;
} LBody;

typedef struct {
    LBody *b;
    int n;            /* bodies, hub included */
    float hub_r, alpha, alpha_target;
} LSim;

/* Hub radius hub_r, children's radii r[0..nk); pad[i] its collision room. */
void lay_sim_init(LSim *s, float hub_r, const float *r, const float *pad, int nk, unsigned seed);
/* One tick; t (seconds) drives the drift, none when drift is false. */
void lay_sim_tick(LSim *s, double t, bool drift);
/* Keep the bodies inside a w x h box centred on the hub; below[i]: room for a label under body i. */
void lay_sim_clamp(LSim *s, float w, float h, const bool *below);
void lay_sim_free(LSim *s);

/* Floating windows (the context screen, context.tpl.html: simulate()):
 * rectangles around a hub fixed at 0,0, each pulled toward its target
 * distance (d3's forceRadial, strength 0.12) and pushed off the others' rects
 * (the draft's rectCollide, LAY_WIN_PAD apart), then settled: of two that
 * still overlap, the later ranked (older) one moves outward, until none do.
 * Pinned ones stay. A fresh one first takes a free spot near its target
 * distance, at an angle from its id, so the forces only fine-tune. */
#define LAY_WIN_PAD 10
typedef struct {
    float x, y, w, h; /* centre, size */
    float tr;         /* target distance from the hub */
    int rank;         /* 0 first: the nearest, the one that stays when two overlap */
    unsigned id;      /* a fresh one's angle */
    bool pinned;      /* stays at x, y */
    bool fresh;       /* not placed yet (cleared once it is) */
    float vx, vy;     /* scratch */
} LWin;

/* ticks of the forces, then up to passes settling passes, on n windows
 * around a hub x hub square. */
void lay_wins(LWin *w, int n, float hub, int ticks, int passes);
/* Pairs (the hub included) nearer than LAY_WIN_PAD on both axes: 0 once settled. */
int lay_wins_overlaps(const LWin *w, int n, float hub);
float lay_win_angle(unsigned id); /* a fresh window's angle, radians */

#endif
