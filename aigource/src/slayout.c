#include "slayout.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "util.h"

int lay_height(const StNode *nodes, int i) {
    int h = 0;
    for (int k = 0; k < nodes[i].nkids; k++) {
        int c = 1 + lay_height(nodes, nodes[i].kids[k]);
        if (c > h) h = c;
    }
    return h;
}

/* d3.treemapDice: kids side by side in x, by value. */
static void dice(const StNode *nodes, const int *kids, int n, double value, float x0, float x1, float *ox0,
                 float *ox1) {
    double k = value > 0 ? (x1 - x0) / value : 0, x = x0;
    for (int i = 0; i < n; i++) {
        ox0[kids[i]] = (float)x;
        x += nodes[kids[i]].value * k;
        ox1[kids[i]] = (float)x;
    }
}

void lay_partition(const StNode *nodes, int root, float *x0, float *x1) {
    x0[root] = 0;
    x1[root] = 1;
    /* breadth-first below root: the array order is parents first, but a
     * subtree's nodes aren't contiguous, so walk it */
    int *stack = xmalloc(sizeof *stack * 64), cap = 64, sp = 0;
    stack[sp++] = root;
    while (sp) {
        int i = stack[--sp];
        const StNode *d = &nodes[i];
        if (!d->nkids) continue;
        dice(nodes, d->kids, d->nkids, d->value, x0[i], x1[i], x0, x1);
        if (sp + d->nkids > cap) cap = (sp + d->nkids) * 2, stack = xrealloc(stack, sizeof *stack * (size_t)cap);
        for (int k = 0; k < d->nkids; k++) stack[sp++] = d->kids[k];
    }
    free(stack);
}

/* ---- treemap ------------------------------------------------------------ */

static void set_rect(LRect *r, double x0, double y0, double x1, double y1) {
    *r = (LRect){(float)x0, (float)y0, (float)x1, (float)y1};
}

/* d3.treemapSquarify's rows: the kids of parent laid in x0..x1, y0..y1, a row
 * at a time, the row grown while its worst aspect ratio improves. The parent's
 * own value is left over at the end. */
static void squarify(const StNode *nodes, const StNode *parent, double ratio, double x0, double y0, double x1,
                     double y1, LRect *out) {
    const int *kids = parent->kids;
    int n = parent->nkids, i0 = 0, i1 = 0;
    double value = parent->value;
    while (i0 < n) {
        double dx = x1 - x0, dy = y1 - y0, sum, nv;
        do sum = nodes[kids[i1++]].value; while (!sum && i1 < n);
        double minv = sum, maxv = sum;
        double alpha = fmax(dy / dx, dx / dy) / (value * ratio);
        double beta = sum * sum * alpha;
        double min_ratio = fmax(maxv / beta, beta / minv);
        for (; i1 < n; ++i1) {
            sum += nv = nodes[kids[i1]].value;
            if (nv < minv) minv = nv;
            if (nv > maxv) maxv = nv;
            beta = sum * sum * alpha;
            double r = fmax(maxv / beta, beta / minv);
            if (r > min_ratio) { sum -= nv; break; }
            min_ratio = r;
        }
        if (dx < dy) { /* dice: a row across, at the top */
            double ny = dy && value ? y0 + dy * sum / value : y1, k = sum ? (x1 - x0) / sum : 0, x = x0;
            for (int i = i0; i < i1; i++) {
                double v = nodes[kids[i]].value;
                set_rect(&out[kids[i]], x, y0, x + v * k, ny);
                x += v * k;
            }
            y0 = ny;
        } else { /* slice: a column, at the left */
            double nx = dx && value ? x0 + dx * sum / value : x1, k = sum ? (y1 - y0) / sum : 0, y = y0;
            for (int i = i0; i < i1; i++) {
                double v = nodes[kids[i]].value;
                set_rect(&out[kids[i]], x0, y, nx, y + v * k);
                y += v * k;
            }
            x0 = nx;
        }
        value -= sum;
        i0 = i1;
    }
}

/* d3.treemap's positionNode: inset by the padding the parent gave (p), then
 * tile the children inside this node's own padding. */
static void tm_position(const StNode *nodes, int i, int depth, double p, double ratio, LRect *out) {
    LRect *r = &out[i];
    double x0 = r->x0 + p, y0 = r->y0 + p, x1 = r->x1 - p, y1 = r->y1 - p;
    if (x1 < x0) x0 = x1 = (x0 + x1) / 2;
    if (y1 < y0) y0 = y1 = (y0 + y1) / 2;
    set_rect(r, x0, y0, x1, y1);
    const StNode *d = &nodes[i];
    if (!d->nkids) return;
    double pi = 1; /* paddingInner(2) / 2 */
    double top = depth == 0 ? 3 : y1 - y0 > 40 ? 18 : 3;
    x0 += 3 - pi, y0 += top - pi, x1 -= 3 - pi, y1 -= 3 - pi;
    if (x1 < x0) x0 = x1 = (x0 + x1) / 2;
    if (y1 < y0) y0 = y1 = (y0 + y1) / 2;
    squarify(nodes, d, ratio, x0, y0, x1, y1, out);
    for (int k = 0; k < d->nkids; k++) tm_position(nodes, d->kids[k], depth + 1, pi, ratio, out);
}

static void tm_round(const StNode *nodes, int i, LRect *out) {
    LRect *r = &out[i];
    *r = (LRect){floorf(r->x0 + 0.5f), floorf(r->y0 + 0.5f), floorf(r->x1 + 0.5f), floorf(r->y1 + 0.5f)};
    for (int k = 0; k < nodes[i].nkids; k++) tm_round(nodes, nodes[i].kids[k], out);
}

void lay_treemap(const StNode *nodes, int root, float w, float h, double ratio, LRect *out) {
    out[root] = (LRect){0, 0, w, h};
    tm_position(nodes, root, 0, 0, ratio, out);
    tm_round(nodes, root, out);
}

/* ---- floating nodes: d3-force ------------------------------------------ */

#define ALPHA_MIN 0.001f
#define GRID_FROM 120 /* bodies past which far ones are grouped */
#define VELOCITY_KEEP 0.6f /* 1 - d3's velocityDecay(0.4) */

static float rnd(unsigned *s) {
    *s = *s * 1664525u + 1013904223u;
    return (float)(*s >> 8) / 16777216.0f;
}

void lay_sim_init(LSim *s, float hub_r, const float *r, const float *pad, int nk, unsigned seed) {
    s->n = nk + 1;
    s->b = xcalloc((size_t)s->n, sizeof *s->b);
    s->hub_r = hub_r;
    s->alpha = 1;
    s->alpha_target = 0.01f;
    s->b[0] = (LBody){.r = hub_r, .pad = 5, .pinned = true};
    unsigned st = seed ? seed : 1;
    for (int i = 0; i < nk; i++) {
        float a = (float)i / (float)nk * 2 * 3.14159265f;
        s->b[i + 1] = (LBody){.x = cosf(a) * (hub_r + 4), .y = sinf(a) * (hub_r + 4), .r = r[i], .pad = pad[i],
                              .phase = rnd(&st) * 6.28f};
    }
}

static float strength(const LBody *b, int i) { return i == 0 ? -200 : -20 - b->r * 2.5f; }

/* Body j's (or a group's, str at x, y) push on body i. */
static void charge(LBody *bi, float str, float x, float y, float alpha) {
    float dx = x - bi->x, dy = y - bi->y, l = dx * dx + dy * dy;
    if (!dx && !dy) return;
    if (l < 1) l = sqrtf(l);
    float w = str * alpha / l;
    bi->vx += dx * w, bi->vy += dy * w;
}

/* d3's forceCollide for one pair, at the positions their velocities lead to
 * (body a's taken once per pass, at xi, yi, as d3 does). */
static void collide(LBody *a, LBody *c, int i, int j, float xi, float yi) {
    float ri = a->r + a->pad, rj = c->r + c->pad, r = ri + rj;
    float x = xi - c->x - c->vx, y = yi - c->y - c->vy, l = x * x + y * y;
    if (l >= r * r) return;
    if (!x) x = 1e-6f * (float)(j - i);
    if (!y) y = 1e-6f * (float)(i + 1);
    l = sqrtf(x * x + y * y);
    l = (r - l) / l;
    x *= l, y *= l;
    float share = rj * rj / (ri * ri + rj * rj);
    a->vx += x * share, a->vy += y * share;
    c->vx -= x * (1 - share), c->vy -= y * (1 - share);
}

/* Many bodies: a grid, exact within the 3x3 cells around a body, each farther
 * cell as one body at its centre of strength (d3 groups far ones too). */
typedef struct {
    int g, *head, *next;
    float x0, y0, cs;
} Grid;

static void grid_build(Grid *gr, const LBody *b, int n, int g, float min_cs, bool ahead) {
    float lx = 1e30f, ly = 1e30f, hx = -1e30f, hy = -1e30f;
    for (int i = 0; i < n; i++) {
        float x = b[i].x + (ahead ? b[i].vx : 0), y = b[i].y + (ahead ? b[i].vy : 0);
        lx = fminf(lx, x), ly = fminf(ly, y), hx = fmaxf(hx, x), hy = fmaxf(hy, y);
    }
    float span = fmaxf(hx - lx, hy - ly) + 1;
    gr->cs = fmaxf(min_cs, span / (float)g);
    gr->g = (int)(span / gr->cs) + 1;
    gr->x0 = lx, gr->y0 = ly;
    gr->head = xmalloc(sizeof *gr->head * (size_t)(gr->g * gr->g));
    gr->next = xmalloc(sizeof *gr->next * (size_t)n);
    for (int k = 0; k < gr->g * gr->g; k++) gr->head[k] = -1;
    for (int i = 0; i < n; i++) {
        int cx = (int)((b[i].x + (ahead ? b[i].vx : 0) - lx) / gr->cs), cy = (int)((b[i].y + (ahead ? b[i].vy : 0) - ly) / gr->cs);
        int c = (cy < gr->g ? cy : gr->g - 1) * gr->g + (cx < gr->g ? cx : gr->g - 1);
        gr->next[i] = gr->head[c];
        gr->head[c] = i;
    }
}

static void grid_free(Grid *gr) { free(gr->head), free(gr->next); }

static int cell_of(const Grid *gr, float x, float y) {
    int cx = (int)((x - gr->x0) / gr->cs), cy = (int)((y - gr->y0) / gr->cs);
    cx = cx < 0 ? 0 : cx >= gr->g ? gr->g - 1 : cx;
    cy = cy < 0 ? 0 : cy >= gr->g ? gr->g - 1 : cy;
    return cy * gr->g + cx;
}

static void charge_grid(LBody *b, int n, float alpha) {
    Grid gr;
    grid_build(&gr, b, n, 8, 1, false);
    int nc = gr.g * gr.g;
    float *sx = xcalloc((size_t)nc * 3, sizeof *sx), *sy = sx + nc, *st = sy + nc;
    for (int c = 0; c < nc; c++)
        for (int j = gr.head[c]; j >= 0; j = gr.next[j]) {
            float w = strength(&b[j], j);
            sx[c] += b[j].x * w, sy[c] += b[j].y * w, st[c] += w;
        }
    for (int i = 0; i < n; i++) {
        int ci = cell_of(&gr, b[i].x, b[i].y), ix = ci % gr.g, iy = ci / gr.g;
        for (int c = 0; c < nc; c++) {
            if (!st[c]) continue;
            int dx = c % gr.g - ix, dy = c / gr.g - iy;
            if (dx >= -1 && dx <= 1 && dy >= -1 && dy <= 1) {
                for (int j = gr.head[c]; j >= 0; j = gr.next[j])
                    if (j != i) charge(&b[i], strength(&b[j], j), b[j].x, b[j].y, alpha);
            } else charge(&b[i], st[c], sx[c] / st[c], sy[c] / st[c], alpha);
        }
    }
    free(sx);
    grid_free(&gr);
}

static void collide_grid(LBody *b, int n) {
    float most = 0;
    for (int i = 0; i < n; i++) most = fmaxf(most, b[i].r + b[i].pad);
    Grid gr;
    grid_build(&gr, b, n, 64, 2 * most, true);
    for (int i = 0; i < n; i++) {
        float xi = b[i].x + b[i].vx, yi = b[i].y + b[i].vy;
        int ci = cell_of(&gr, xi, yi), ix = ci % gr.g, iy = ci / gr.g;
        for (int dy = -1; dy <= 1; dy++)
            for (int dx = -1; dx <= 1; dx++) {
                int x = ix + dx, y = iy + dy;
                if (x < 0 || y < 0 || x >= gr.g || y >= gr.g) continue;
                for (int j = gr.head[y * gr.g + x]; j >= 0; j = gr.next[j])
                    if (j > i) collide(&b[i], &b[j], i, j, xi, yi);
            }
    }
    grid_free(&gr);
}

void lay_sim_tick(LSim *s, double t, bool drift) {
    int n = s->n, nk = n - 1;
    LBody *b = s->b;
    s->alpha += (s->alpha_target - s->alpha) * (1 - powf(ALPHA_MIN, 1.0f / 300));
    float alpha = s->alpha, hub_r = s->hub_r;
    /* link hub -> child: distance hub_r + r + 40, strength 0.08; the hub's end
     * barely moves (bias by link count) and is pinned anyway */
    for (int i = 1; i < n; i++) {
        float x = b[i].x + b[i].vx - b[0].x - b[0].vx, y = b[i].y + b[i].vy - b[0].y - b[0].vy;
        if (!x && !y) x = 1e-6f;
        float l = sqrtf(x * x + y * y), dist = hub_r + b[i].r + 40;
        l = (l - dist) / l * alpha * 0.08f;
        x *= l, y *= l;
        float bias = (float)nk / (float)(nk + 1);
        b[i].vx -= x * bias, b[i].vy -= y * bias;
        b[0].vx += x * (1 - bias), b[0].vy += y * (1 - bias);
    }
    if (n > GRID_FROM) {
        charge_grid(b, n, alpha);
        for (int it = 0; it < 2; it++) collide_grid(b, n);
    } else {
        /* many-body, exactly: hub -200, others -20 - 2.5 r */
        for (int i = 0; i < n; i++)
            for (int j = 0; j < n; j++)
                if (i != j) charge(&b[i], strength(&b[j], j), b[j].x, b[j].y, alpha);
        /* collide: radius r + pad, two passes, at the positions the velocities lead to */
        for (int it = 0; it < 2; it++)
            for (int i = 0; i < n; i++) {
                float xi = b[i].x + b[i].vx, yi = b[i].y + b[i].vy;
                for (int j = i + 1; j < n; j++) collide(&b[i], &b[j], i, j, xi, yi);
            }
    }
    /* ring around the hub, and a weak pull to the centre */
    float ring = nk > 30 ? 70 : 45;
    for (int i = 1; i < n; i++) {
        float dx = b[i].x ? b[i].x : 1e-6f, dy = b[i].y ? b[i].y : 1e-6f, r = sqrtf(dx * dx + dy * dy);
        float k = (hub_r + b[i].r + ring - r) * 0.05f * alpha / r;
        b[i].vx += dx * k, b[i].vy += dy * k;
    }
    for (int i = 0; i < n; i++) {
        b[i].vx += -b[i].x * 0.02f * alpha;
        b[i].vy += -b[i].y * 0.03f * alpha;
    }
    for (int i = 0; i < n; i++) {
        if (b[i].pinned) {
            b[i].x = b[i].fx, b[i].y = b[i].fy, b[i].vx = b[i].vy = 0;
            continue;
        }
        b[i].x += b[i].vx *= VELOCITY_KEEP;
        b[i].y += b[i].vy *= VELOCITY_KEEP;
        if (drift) {
            b[i].vx += sinf((float)t * 0.5f + b[i].phase) * 0.01f;
            b[i].vy += cosf((float)t * 0.4f + b[i].phase) * 0.01f;
        }
    }
}

void lay_sim_clamp(LSim *s, float w, float h, const bool *below) {
    float bx = w / 2, by = h / 2;
    for (int i = 1; i < s->n; i++) {
        LBody *o = &s->b[i];
        float lox = -bx + o->r + 4, hix = bx - o->r - 4, loy = -by + o->r + 8, hiy = by - o->r - (below[i] ? 18 : 4);
        o->x = fmaxf(lox, fminf(hix, o->x));
        o->y = fmaxf(loy, fminf(hiy, o->y));
    }
}

void lay_sim_free(LSim *s) {
    free(s->b);
    *s = (LSim){0};
}

/* ---- floating windows ------------------------------------------------------ */

#define SEED_PAD 12
#define SEED_CELL 256

float lay_win_angle(unsigned id) { return (float)((double)(uint32_t)((id + 1u) * 2654435761u) / 4294967296.0 * 2 * M_PI); }

/* A fresh window's free spot: a grid of the placed rects by cell, searched
 * ring by ring from its target distance, alternating sides of its angle. */
typedef struct {
    int *head, nb;       /* buckets: first entry, -1 */
    int *rect, *next;    /* entries: the rect, the next in its bucket */
    long long *cell;     /* entries: their cell */
    int n, cap;
} SeedGrid;

static unsigned bucket(const SeedGrid *g, long long cell) {
    uint64_t h = (uint64_t)cell * 0x9E3779B97F4A7C15ull;
    return (unsigned)(h >> 40) % (unsigned)g->nb;
}

static long long cell_key(int i, int j) { return (long long)i * 1000003 + j; }

static void seed_add(SeedGrid *g, const LWin *w, int k) {
    const LWin *m = &w[k];
    for (int i = (int)floorf((m->x - m->w / 2 - SEED_PAD) / SEED_CELL); i <= (int)floorf((m->x + m->w / 2 + SEED_PAD) / SEED_CELL); i++)
        for (int j = (int)floorf((m->y - m->h / 2 - SEED_PAD) / SEED_CELL); j <= (int)floorf((m->y + m->h / 2 + SEED_PAD) / SEED_CELL); j++) {
            if (g->n == g->cap) {
                g->cap = g->cap ? g->cap * 2 : 256;
                g->rect = xrealloc(g->rect, sizeof *g->rect * (size_t)g->cap);
                g->next = xrealloc(g->next, sizeof *g->next * (size_t)g->cap);
                g->cell = xrealloc(g->cell, sizeof *g->cell * (size_t)g->cap);
            }
            long long c = cell_key(i, j);
            unsigned b = bucket(g, c);
            g->rect[g->n] = k, g->cell[g->n] = c, g->next[g->n] = g->head[b];
            g->head[b] = g->n++;
        }
}

static bool seed_free(const SeedGrid *g, const LWin *w, const LWin *n, float x, float y) {
    for (int i = (int)floorf((x - n->w / 2 - SEED_PAD) / SEED_CELL); i <= (int)floorf((x + n->w / 2 + SEED_PAD) / SEED_CELL); i++)
        for (int j = (int)floorf((y - n->h / 2 - SEED_PAD) / SEED_CELL); j <= (int)floorf((y + n->h / 2 + SEED_PAD) / SEED_CELL); j++) {
            long long c = cell_key(i, j);
            for (int e = g->head[bucket(g, c)]; e >= 0; e = g->next[e]) {
                if (g->cell[e] != c) continue;
                const LWin *m = &w[g->rect[e]];
                if (fabsf(m->x - x) * 2 < m->w + n->w + SEED_PAD * 2 && fabsf(m->y - y) * 2 < m->h + n->h + SEED_PAD * 2)
                    return false;
            }
        }
    return true;
}

/* w[0..n) and the hub at w[n] */
static void seed(LWin *w, int n) {
    SeedGrid g = {.nb = 1024};
    g.head = xmalloc(sizeof *g.head * (size_t)g.nb);
    for (int b = 0; b < g.nb; b++) g.head[b] = -1;
    for (int k = 0; k <= n; k++)
        if (k == n || !w[k].fresh) seed_add(&g, w, k);
    for (int k = 0; k < n; k++) {
        LWin *m = &w[k];
        if (!m->fresh) continue;
        m->fresh = false;
        if (!m->pinned) {
            float th0 = lay_win_angle(m->id);
            bool done = false;
            for (float r = m->tr; r < m->tr + 4000 && !done; r += 24) {
                int steps = (int)fmaxf(12, floorf(2 * (float)M_PI * r / 40));
                for (int s = 0; s < steps; s++) {
                    float th = th0 + (s % 2 ? 1.0f : -1.0f) * (float)((s + 1) / 2) * 2 * (float)M_PI / (float)steps;
                    float x = r * cosf(th), y = r * sinf(th);
                    if (seed_free(&g, w, m, x, y)) {
                        m->x = x, m->y = y, done = true;
                        break;
                    }
                }
            }
        }
        seed_add(&g, w, k);
    }
    free(g.head), free(g.rect), free(g.next), free(g.cell);
}

static int cmp_left(const void *a, const void *b, void *ud) {
    const LWin *w = ud;
    int i = *(const int *)a, j = *(const int *)b;
    float l = w[i].x - w[i].w / 2, r = w[j].x - w[j].w / 2;
    return l < r ? -1 : l > r ? 1 : i - j;
}

/* The draft's rectCollide over w[0..n] (the hub last, pinned): a sweep by
 * left edge. Forces (ranked false): the pair pushed apart on the axis that
 * overlaps less, a pinned one not at all. Settling (ranked): the later ranked
 * (or the unpinned) one jumps outward past the other, on the outward axis that
 * needs less. Returns the overlapping pairs met; count only: nothing moves. */
static int collide_rects(LWin *w, int n, int *ord, float alpha, bool ranked, bool count) {
    int m = n + 1, hits = 0;
    for (int i = 0; i < m; i++) ord[i] = i;
    qsort_r(ord, (size_t)m, sizeof *ord, cmp_left, w);
    for (int i = 0; i < m; i++) {
        LWin *a = &w[ord[i]];
        for (int j = i + 1; j < m; j++) {
            LWin *b = &w[ord[j]];
            if (b->x - b->w / 2 > a->x + a->w / 2 + LAY_WIN_PAD) break;
            float dx = b->x - a->x, dy = b->y - a->y;
            float ox = (a->w + b->w) / 2 + LAY_WIN_PAD - fabsf(dx), oy = (a->h + b->h) / 2 + LAY_WIN_PAD - fabsf(dy);
            if (ox <= 0.5f || oy <= 0.5f || (a->pinned && b->pinned)) continue; /* touching is fine; two pinned stay */
            hits++;
            if (count) continue;
            if (ranked) {
                LWin *mv = a->pinned || (!b->pinned && b->rank > a->rank) ? b : a, *o = mv == a ? b : a;
                float sx = mv->x >= 0 ? 1 : -1, sy = mv->y >= 0 ? 1 : -1;
                float nx = o->x + sx * ((mv->w + o->w) / 2 + LAY_WIN_PAD + 1), ny = o->y + sy * ((mv->h + o->h) / 2 + LAY_WIN_PAD + 1);
                float dx2 = (nx - mv->x) * sx, dy2 = (ny - mv->y) * sy;
                if (dx2 >= 0 && (dx2 <= dy2 || dy2 < 0)) mv->x = nx;
                else if (dy2 >= 0) mv->y = ny;
                else mv->x = nx;
                continue;
            }
            float fa = a->pinned ? 0 : b->pinned ? 1 : 0.5f, fb = 1 - fa, k = fminf(1, 0.5f + alpha);
            if (ox < oy) {
                float s = (ox + 1) * k * (dx < 0 ? -1 : 1);
                a->x -= s * fa, b->x += s * fb;
            } else {
                float s = (oy + 1) * k * (dy < 0 ? -1 : 1);
                a->y -= s * fa, b->y += s * fb;
            }
        }
    }
    return hits;
}

/* The hub as the draft's AGENT_NODE: pinned at 0,0, ranked before all. */
static LWin *with_hub(const LWin *w, int n, float hub) {
    LWin *all = xmalloc(sizeof *all * (size_t)(n + 1));
    memcpy(all, w, sizeof *all * (size_t)n);
    all[n] = (LWin){.w = hub, .h = hub, .rank = -1, .pinned = true};
    return all;
}

void lay_wins(LWin *w, int n, float hub, int ticks, int passes) {
    LWin *all = with_hub(w, n, hub);
    int *ord = xmalloc(sizeof *ord * (size_t)(n + 1));
    seed(all, n);
    /* d3.forceSimulation: alpha 0.35 decaying to 0.001 over the ticks, velocity decay 0.5 */
    float alpha = 0.35f, decay = ticks > 0 ? 1 - powf(0.001f, 1.0f / (float)ticks) : 0;
    for (int k = 0; k < n; k++) all[k].vx = all[k].vy = 0;
    for (int t = 0; t < ticks; t++) {
        alpha += -alpha * decay;
        for (int k = 0; k < n; k++) { /* forceRadial */
            LWin *m = &all[k];
            float dx = m->x ? m->x : 1e-6f, dy = m->y ? m->y : 1e-6f, r = sqrtf(dx * dx + dy * dy);
            float s = (m->tr - r) * 0.12f * alpha / r;
            m->vx += dx * s, m->vy += dy * s;
        }
        collide_rects(all, n, ord, alpha, false, false);
        for (int k = 0; k < n; k++) {
            LWin *m = &all[k];
            if (m->pinned) { m->vx = m->vy = 0; continue; }
            m->x += m->vx *= 0.5f;
            m->y += m->vy *= 0.5f;
        }
    }
    for (int p = 0; p < passes && collide_rects(all, n, ord, 0.5f, true, false); p++) {} /* until no two overlap */
    memcpy(w, all, sizeof *w * (size_t)n);
    free(ord);
    free(all);
}

int lay_wins_overlaps(const LWin *w, int n, float hub) {
    LWin *all = with_hub(w, n, hub);
    for (int k = 0; k < n; k++) all[k].pinned = false; /* pinned pairs count too */
    int *ord = xmalloc(sizeof *ord * (size_t)(n + 1));
    int hits = collide_rects(all, n, ord, 0, false, true);
    free(ord);
    free(all);
    return hits;
}
