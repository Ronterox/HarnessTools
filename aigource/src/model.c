#include "model.h"

#include <fcntl.h>
#include <math.h>
#include <sys/stat.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define DIR_GAP 40.0f       /* free space between a dir and its child dirs */
#define CONE_MAX 2.6f       /* widest cone a dir's subtree gets (radians, under a half turn) */
#define TAU 6.2831853f
#define FADE_IN 0.4f
#define FADE_OUT 1.5f
#define FLASH_DECAY 0.8f    /* per second */
#define BUBBLE_SECS 4.0f
#define GOLDEN_ANGLE 2.39996323f
#define DIR_MIN 4.0f        /* drawn size of a dir with nothing in it */
/* The average file size starts as PRIOR_FILES files of PRIOR_LINES, so the
 * first few files seen don't set it alone; no file counts past SIZE_CAP. */
#define PRIOR_FILES 5
#define PRIOR_LINES 200.0
#define SIZE_CAP 4000

static const Color ACTOR_COLORS[] = {
    {255, 184, 108, 255}, {139, 233, 253, 255}, {255, 121, 198, 255}, {189, 147, 249, 255},
    {80, 250, 123, 255},  {241, 250, 140, 255}, {255, 140, 90, 255},  {130, 170, 255, 255},
};

Color kind_color(EvKind k) {
    switch (k) {
    case EV_READ: return (Color){100, 160, 255, 255};
    case EV_SEARCH: return (Color){90, 220, 220, 255};
    case EV_CREATE: return (Color){80, 250, 123, 255};
    case EV_EDIT: return (Color){250, 200, 90, 255};
    case EV_DELETE: return (Color){255, 85, 85, 255};
    case EV_TOUCH: return (Color){190, 140, 255, 255};
    case EV_TOOL: return (Color){240, 130, 200, 255};
    default: return (Color){200, 200, 200, 255};
    }
}

static float frand(void) { return (float)rand() / (float)RAND_MAX; }

/* ---- nodes -------------------------------------------------------------- */

static int add_node(Scene *m, const char *path, int parent, bool is_dir) {
    if (m->nnodes == m->capnodes) {
        m->capnodes = m->capnodes ? m->capnodes * 2 : 256;
        m->nodes = xrealloc(m->nodes, m->capnodes * sizeof *m->nodes);
    }
    int i = m->nnodes++;
    Node *n = &m->nodes[i];
    memset(n, 0, sizeof *n);
    const char *slash = strrchr(path, '/');
    n->name = xstrdup(slash ? slash + 1 : path);
    n->path = xstrdup(path);
    n->parent = parent;
    n->is_dir = is_dir;
    n->lines = -1;
    n->counted = -1;
    n->radius = is_dir ? 10 : 5;
    n->size = DIR_MIN;
    if (parent >= 0) {
        /* Sprout away from the grandparent so the tree grows outward. */
        Node *p = &m->nodes[parent];
        float base = frand() * 6.2832f;
        if (p->parent >= 0) {
            Vector2 gp = m->nodes[p->parent].pos;
            base = atan2f(p->pos.y - gp.y, p->pos.x - gp.x) + (frand() - 0.5f) * 1.6f;
        }
        float dist = is_dir ? p->radius + DIR_GAP + 20 : p->radius * 0.5f;
        n->pos = (Vector2){p->pos.x + cosf(base) * dist, p->pos.y + sinf(base) * dist};
    }
    n->color = (Color){160, 170, 190, 255};
    n->act_kind = EV_READ;
    char key[8 + 4096];
    snprintf(key, sizeof key, "%c:%s", is_dir ? 'd' : 'f', path);
    strmap_put(&m->index, key, i);
    return i;
}

static void attach(Scene *m, int parent, int child) {
    if (m->nodes[child].attached) return;
    m->nodes[child].attached = true;
    Node *p = &m->nodes[parent];
    if (p->nkids == p->capkids) {
        p->capkids = p->capkids ? p->capkids * 2 : 8;
        p->kids = xrealloc(p->kids, p->capkids * sizeof *p->kids);
    }
    p->kids[p->nkids++] = child;
}

static void detach(Scene *m, int parent, int child) {
    if (!m->nodes[child].attached) return;
    m->nodes[child].attached = false;
    Node *p = &m->nodes[parent];
    for (int i = 0; i < p->nkids; i++)
        if (p->kids[i] == child) {
            memmove(p->kids + i, p->kids + i + 1, (size_t)(p->nkids - i - 1) * sizeof *p->kids);
            p->nkids--;
            return;
        }
}

static int lookup(const Scene *m, char kind, const char *path) {
    char key[8 + 4096];
    snprintf(key, sizeof key, "%c:%s", kind, path);
    return strmap_get(&m->index, key);
}

static int ensure_dir(Scene *m, const char *path) {
    if (!*path) return 0;
    int i = lookup(m, 'd', path);
    if (i >= 0) return i;
    const char *slash = strrchr(path, '/');
    char *parent_path = slash ? xstrndup(path, (size_t)(slash - path)) : xstrdup("");
    int parent = ensure_dir(m, parent_path);
    free(parent_path);
    i = add_node(m, path, parent, true);
    Node *p = &m->nodes[parent];
    if (p->ndirs == p->capdirs) {
        p->capdirs = p->capdirs ? p->capdirs * 2 : 4;
        p->dirs = xrealloc(p->dirs, p->capdirs * sizeof *p->dirs);
    }
    p->dirs[p->ndirs++] = i;
    return i;
}

static int ensure_file(Scene *m, const char *path) {
    int i = lookup(m, 'f', path);
    if (i >= 0) {
        Node *n = &m->nodes[i];
        if (n->deleted) { /* recreated */
            n->deleted = false;
            attach(m, n->parent, i);
        }
        return i;
    }
    const char *slash = strrchr(path, '/');
    char *parent_path = slash ? xstrndup(path, (size_t)(slash - path)) : xstrdup("");
    int parent = ensure_dir(m, parent_path);
    free(parent_path);
    i = add_node(m, path, parent, false);
    attach(m, parent, i);
    return i;
}

/* Deepest existing node for a path, never creating one (searches only look). */
static int nearest(Scene *m, const char *path) {
    int i = lookup(m, 'f', path);
    if (i >= 0) return i;
    char *p = xstrdup(path);
    for (;;) {
        if ((i = lookup(m, 'd', p)) >= 0 || !*p) break;
        char *slash = strrchr(p, '/');
        if (slash) *slash = 0;
        else *p = 0;
    }
    free(p);
    return i >= 0 ? i : 0;
}

/* Current line count on disk; used when the transcript doesn't say. Cached:
 * timeline seeks rebuild the scene and would re-read every file. */
static StrMap g_disk_cache;

static int disk_lines_uncached(Scene *m, const char *path);

static int disk_lines(Scene *m, const char *path) {
    int v = strmap_get(&g_disk_cache, path);
    if (v >= 0) return v - 1; /* stored +1 so -1 (unreadable) is cacheable */
    v = disk_lines_uncached(m, path);
    strmap_put(&g_disk_cache, path, v + 1);
    return v;
}

static int disk_lines_uncached(Scene *m, const char *path) {
    char full[8192];
    snprintf(full, sizeof full, "%s/%s", m->root, path);
    int fd = open(full, O_RDONLY | O_NONBLOCK); /* never hang on a FIFO */
    if (fd < 0) return -1;
    struct stat st;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode)) { close(fd); return -1; }
    static char buf[1 << 16];
    long lines = 0, bytes = 0;
    int binary = 0, last = '\n';
    ssize_t n;
    while ((n = read(fd, buf, sizeof buf)) > 0 && bytes < (1 << 20)) {
        for (ssize_t i = 0; i < n; i++) {
            lines += buf[i] == '\n';
            binary |= buf[i] == 0;
        }
        last = buf[n - 1];
        bytes += n;
    }
    close(fd);
    if (binary) return (int)(st.st_size / 50); /* rough visual size for binaries */
    if (st.st_size > bytes && bytes) lines = (long)((double)lines * st.st_size / bytes); /* extrapolate */
    return (int)(lines + (bytes && last != '\n'));
}

/* ---- actors & effects --------------------------------------------------- */

static int actor_for(Scene *m, const Event *e) {
    for (int i = 0; i < m->nactors; i++)
        if (!strcmp(m->actors[i].id, e->actor)) return i;
    m->actors = xrealloc(m->actors, (m->nactors + 1) * sizeof *m->actors);
    Actor *a = &m->actors[m->nactors];
    memset(a, 0, sizeof *a);
    a->id = xstrdup(e->actor);
    a->name = xstrdup(e->actor_name ? e->actor_name : e->actor);
    a->color = ACTOR_COLORS[m->nactors % (int)(sizeof ACTOR_COLORS / sizeof *ACTOR_COLORS)];
    a->pos = (Vector2){(frand() - 0.5f) * 200, (frand() - 0.5f) * 200};
    return m->nactors++;
}

static Fx *add_fx(Scene *m, FxKind kind, EvKind ev, int actor, int node, float strength,
                  float life, Color c) {
    int slot = (m->fx_head + m->fx_count) % MAX_FX;
    if (m->fx_count == MAX_FX) m->fx_head = (m->fx_head + 1) % MAX_FX; /* drop oldest */
    else m->fx_count++;
    m->fx[slot] = (Fx){kind, ev, actor, node, strength, 0, life, c, RING_MINOR, 0};
    return &m->fx[slot];
}

static float diff_strength(int n) { return fminf(1.0f, log2f(1.0f + (float)n) / 9.0f); }

/* Keeps the average file size current after n's length or state changed. */
static void recount(Scene *m, Node *n) {
    if (n->counted >= 0) { m->sum_lines -= n->counted; m->nsized--; }
    n->counted = n->deleted || n->lines < 0 ? -1 : n->lines < SIZE_CAP ? n->lines : SIZE_CAP;
    if (n->counted >= 0) { m->sum_lines += n->counted; m->nsized++; }
}

/* How big a change of `lines` lines is: over the file's length before it, or
 * over the average file when that's bigger (a new file, a small one). 10
 * lines in a 1000-line file are 0.01; a new file of 500 where files run 200
 * is 2.5. */
static float change_size(const Scene *m, int lines, int before) {
    double avg = (m->sum_lines + PRIOR_FILES * PRIOR_LINES) / (m->nsized + PRIOR_FILES);
    return (float)(lines / fmax(fmax(before, avg), 1));
}

/* A pulse for a change of that size: minor, notable from RING_NOTABLE_AT,
 * major from a whole file's worth up; strength is how far into its tier. */
static void add_ring(Scene *m, EvKind ev, int actor, int node, float size, Color c) {
    static const float life[] = {0.7f, 1.3f, 2.2f};
    RingTier t = size < RING_NOTABLE_AT ? RING_MINOR : size < RING_MAJOR_AT ? RING_NOTABLE : RING_MAJOR;
    float s = t == RING_MINOR ? size / RING_NOTABLE_AT
            : t == RING_NOTABLE ? (size - RING_NOTABLE_AT) / (RING_MAJOR_AT - RING_NOTABLE_AT)
                                : (size - RING_MAJOR_AT) / (RING_MAJOR_TOP - RING_MAJOR_AT);
    Fx *f = add_fx(m, FX_PULSE, ev, actor, node, fminf(1, s), life[t], c);
    f->tier = t;
    f->phase = frand() * 6.2832f;
}

static void set_bubble(Actor *a, const char *text) {
    free(a->bubble);
    a->bubble = first_line(text, 70);
    a->bubble_t = BUBBLE_SECS;
}

/* ---- events ------------------------------------------------------------- */

Scene *model_new(const char *root) {
    Scene *m = xcalloc(1, sizeof *m);
    m->root = xstrdup(root);
    const char *slash = strrchr(root, '/');
    add_node(m, "", -1, true);
    free(m->nodes[0].name);
    m->nodes[0].name = xstrdup(slash && slash[1] ? slash + 1 : root);
    m->nodes[0].alpha = 1;
    return m;
}

void model_free(Scene *m) {
    for (int i = 0; i < m->nnodes; i++) {
        free(m->nodes[i].name);
        free(m->nodes[i].path);
        free(m->nodes[i].kids);
        free(m->nodes[i].dirs);
    }
    for (int i = 0; i < m->nactors; i++) {
        free(m->actors[i].id);
        free(m->actors[i].name);
        free(m->actors[i].prompt);
        free(m->actors[i].bubble);
    }
    free(m->nodes);
    free(m->actors);
    strmap_free(&m->index);
    free(m->root);
    free(m);
}

bool dir_merged(const Scene *m, const Node *n) {
    if (!dir_passes(n)) return false;
    const Node *c = &m->nodes[n->dirs[0]];
    return fabsf(c->pos.x - n->pos.x) + fabsf(c->pos.y - n->pos.y) < 1;
}

int model_find(const Scene *m, bool is_dir, const char *path) {
    return lookup(m, is_dir ? 'd' : 'f', path);
}

void model_adopt_layout(Scene *m, const Scene *old) {
    for (int i = 0; i < m->nnodes; i++) {
        Node *n = &m->nodes[i];
        int j = model_find(old, n->is_dir, n->path);
        if (j < 0) continue;
        const Node *o = &old->nodes[j];
        n->pos = o->pos;
        n->radius = o->radius;
        n->size = o->size;
        n->placed = true;
        n->disp_lines = o->disp_lines;
        n->alpha = n->deleted ? 0 : 1;
    }
    for (int i = 0; i < m->nactors; i++)
        for (int j = 0; j < old->nactors; j++)
            if (!strcmp(m->actors[i].id, old->actors[j].id)) m->actors[i].pos = old->actors[j].pos;
}

void model_apply(Scene *m, const Event *e, bool effects) {
    int ai = actor_for(m, e); /* may realloc m->actors: index first */
    Actor *a = &m->actors[ai];
    a->last_active = e->ts;
    a->turn = e->turn;

    switch (e->kind) {
    case EV_TURN:
        free(a->prompt);
        a->prompt = xstrdup(e->text ? e->text : "");
        return;
    case EV_BASH: {
        m->commands++;
        char buf[160];
        snprintf(buf, sizeof buf, "$ %s", e->text ? e->text : "");
        set_bubble(a, buf);
        return;
    }
    case EV_TOOL: {
        char buf[160];
        snprintf(buf, sizeof buf, "%s", e->text ? e->text : "");
        set_bubble(a, buf);
        return;
    }
    case EV_SEARCH: {
        m->searches++;
        int ni = nearest(m, e->path ? e->path : "");
        a->target = ni;
        if (e->text) set_bubble(a, e->text);
        if (effects) add_fx(m, FX_BEAM, e->kind, ai, ni, 0.1f, 1.0f, kind_color(e->kind));
        return;
    }
    default: break;
    }
    if (!e->path || !*e->path || *e->path == '/') return; /* outside the repo (a read): no node */

    int ni = ensure_file(m, e->path);
    Node *n = &m->nodes[ni];
    n->last_touch = e->ts;
    n->last_kind = e->kind;
    n->last_actor = ai;
    n->last_add = e->add;
    a->target = ni;

    int old = n->lines;
    /* A delete with no count (an `rm` guessed from a command) removed what the
     * file had, when that's known. */
    int del = e->kind == EV_DELETE && !e->del && old > 0 ? old : e->del;
    n->last_del = del;
    if (e->kind == EV_DELETE) {
        n->deleted = true;
        n->lines = 0;
    } else if (e->lines >= 0) {
        n->lines = e->lines;
    } else if (n->lines >= 0) {
        n->lines = n->lines + e->add - e->del > 0 ? n->lines + e->add - e->del : 0;
    } else {
        n->lines = disk_lines(m, e->path);
    }
    /* Sized against the file as it was and the average before this change.
     * A file first seen here sized from disk is approximate: in a replay
     * that's its length now, not then. */
    int before = old >= 0 ? old : n->lines >= 0 ? n->lines - e->add + del : 0;
    if (before < 0) before = 0;
    float add_size = change_size(m, e->add, before), del_size = change_size(m, del, before);
    recount(m, n);

    switch (e->kind) {
    case EV_READ: m->reads++; a->reads++; break;
    case EV_CREATE: m->files_created++; a->files_created++; break;
    case EV_EDIT: case EV_TOUCH: m->files_edited++; a->files_edited++; break;
    case EV_DELETE: m->files_deleted++; a->files_deleted++; break;
    default: break;
    }
    if (e->kind != EV_READ) {
        n->churn++;
        n->act_kind = e->kind;
        n->add += e->add;
        n->del += e->del;
        a->add += e->add;
        a->del += e->del;
        m->total_add += e->add;
        m->total_del += e->del;
    }
    if (!effects) return; /* quiet replay (seek, backlog): no flashes either */
    n->flash = e->kind == EV_READ ? fmaxf(n->flash, 0.35f) : 1;
    n->label = LABEL_SECS;
    float s = diff_strength(e->add + e->del);
    add_fx(m, FX_BEAM, e->kind, ai, ni, e->kind == EV_READ ? 0.1f : 0.25f + 0.75f * s, 1.4f,
           kind_color(e->kind));
    if (e->add) add_ring(m, e->kind, ai, ni, add_size, kind_color(EV_CREATE));
    if (del) add_ring(m, e->kind, ai, ni, del_size, kind_color(EV_DELETE));
    if (!e->add && !del && e->kind != EV_READ) /* size unknown */
        add_ring(m, e->kind, ai, ni, 0.1f, kind_color(e->kind));
}

/* ---- layout ------------------------------------------------------------- */

static float file_radius(const Node *n) {
    if (n->lines < 0 && n->disp_lines <= 0) return 5;
    return fminf(26.0f, 3.0f + 2.0f * sqrtf(fmaxf(n->disp_lines, 0) / 10.0f));
}

/* Files sit on a sunflower spiral around their dir, starting past `inner`
 * (the dir's own circle); the dir's radius is its extent. */
static Vector2 slot_offset(int i, float spacing, float inner) {
    float r = inner + spacing * 0.62f * sqrtf((float)i + 0.5f);
    float a = (float)i * GOLDEN_ANGLE;
    return (Vector2){cosf(a) * r, sinf(a) * r};
}

/* Dirs are laid out in nested cones, so no edge crosses another. Each child
 * dir owns a cone from its parent's center, a share of the parent's own cone
 * by subtree weight, and sits on its axis far enough out for its files to fit
 * inside it. Its children split a cone from where it sits, which stays inside
 * its own as far as its subtree reaches (ext): the same angle always would (a
 * cone moved forward along its axis does), and a wider one does when the
 * subtree is small next to its distance, which keeps big trees from spreading
 * level after level. The root's children share the whole circle; no cone is
 * wider than CONE_MAX, so each opens away from the edge it hangs from. */
static float balloon_measure(Scene *m, int i) {
    Node *d = &m->nodes[i];
    float w = 0;
    d->ext = d->radius;
    for (int k = 0; k < d->ndirs; k++) {
        Node *c = &m->nodes[d->dirs[k]];
        w += balloon_measure(m, d->dirs[k]);
        d->ext = fmaxf(d->ext, hypotf(c->pos.x - d->pos.x, c->pos.y - d->pos.y) + c->ext);
    }
    return d->weight = fmaxf(1, w) + 0.3f + d->radius / 60; /* its subdirs, then its own files */
}

static void balloon_place(Scene *m, int i, float facing, float cone, float ease) {
    Node *d = &m->nodes[i];
    if (dir_passes(d)) { /* merged into its only subdir, which sits right here (sliding in) */
        Node *c = &m->nodes[d->dirs[0]];
        float e = c->placed ? ease : 1;
        c->placed = true;
        c->pos.x += (d->pos.x - c->pos.x) * e;
        c->pos.y += (d->pos.y - c->pos.y) * e;
        balloon_place(m, d->dirs[0], facing, cone, ease);
        return;
    }
    float total = 0;
    for (int k = 0; k < d->ndirs; k++) total += m->nodes[d->dirs[k]].weight;
    float a = facing - cone / 2;
    for (int k = 0; k < d->ndirs; k++) {
        Node *c = &m->nodes[d->dirs[k]], *end = c; /* a merged chain is as big as its last dir */
        while (dir_passes(end)) end = &m->nodes[end->dirs[0]];
        float share = cone * c->weight / total, w = fminf(share, CONE_MAX), th = a + share / 2;
        float dist = fmaxf(d->radius + DIR_GAP + end->radius, (end->radius + DIR_GAP / 4) / sinf(w / 2));
        Vector2 t = {d->pos.x + cosf(th) * dist, d->pos.y + sinf(th) * dist};
        float e = c->placed ? ease : 1; /* a new dir starts in its place, not sweeping across others */
        c->placed = true;
        c->pos.x += (t.x - c->pos.x) * e;
        c->pos.y += (t.y - c->pos.y) * e;
        /* Within the subtree's reach x <= ext, a cone of half-angle b from here
         * stays in this one while x tan b <= (dist + x) tan(w/2). */
        float b = atanf(tanf(w / 2) * (1 + dist / fmaxf(c->ext, 1)));
        balloon_place(m, d->dirs[k], th, 2 * fminf(b, CONE_MAX / 2), ease);
        a += share;
    }
}

void model_update(Scene *m, float dt, double now) {
    (void)now;
    float real_dt = dt; /* labels keep real time even on slow frames */
    if (dt > 1.0f / 20) dt = 1.0f / 20;
    Node *N = m->nodes;

    /* Files: size easing, fades, spiral slots. */
    for (int i = 0; i < m->nnodes; i++) {
        Node *n = &N[i];
        n->flash = fmaxf(0, n->flash - FLASH_DECAY * dt);
        n->label = fmaxf(0, n->label - real_dt);
        if (n->is_dir) continue;
        if (n->lines >= 0) n->disp_lines += ((float)n->lines - n->disp_lines) * fminf(1, dt * 4);
        n->radius = file_radius(n);
        if (n->deleted) {
            n->alpha = fmaxf(0, n->alpha - dt / FADE_OUT);
            if (n->alpha <= 0) detach(m, n->parent, i);
        } else if (n->alpha < 1) {
            n->alpha = fminf(1, n->alpha + dt / FADE_IN);
        }
    }
    /* Dirs are drawn at the average size of their children, files and dirs
     * (a child dir as it was last frame). */
    for (int i = 0; i < m->nnodes; i++) N[i].kids_sum = 0, N[i].kids_n = 0;
    for (int i = 1; i < m->nnodes; i++) {
        Node *n = &N[i];
        if (n->alpha <= 0 && !n->is_dir) continue; /* faded out */
        N[n->parent].kids_sum += n->is_dir ? n->size : n->radius;
        N[n->parent].kids_n++;
    }
    for (int d = 0; d < m->nnodes; d++) {
        Node *dir = &N[d];
        if (!dir->is_dir) continue;
        if (dir->alpha < 1) dir->alpha = fminf(1, dir->alpha + dt / FADE_IN);
        float size = dir->kids_n ? fmaxf(DIR_MIN, dir->kids_sum / (float)dir->kids_n) : DIR_MIN;
        dir->size += (size - dir->size) * fminf(1, dt * 4);
        float inner = dir->size + 2;
        float maxr = 0, sum = 0;
        for (int k = 0; k < dir->nkids; k++) {
            sum += N[dir->kids[k]].radius;
            maxr = fmaxf(maxr, N[dir->kids[k]].radius);
        }
        float spacing = dir->nkids ? fmaxf(12.0f, 2.2f * sum / dir->nkids + 4) : 12;
        float target_r = 10 + dir->size + (dir->nkids ? spacing * 0.62f * sqrtf((float)dir->nkids) + maxr : 0);
        dir->radius += (target_r - dir->radius) * fminf(1, dt * 3);
        for (int k = 0; k < dir->nkids; k++) {
            Node *f = &N[dir->kids[k]];
            Vector2 o = slot_offset(k, spacing, inner);
            Vector2 t = {dir->pos.x + o.x, dir->pos.y + o.y};
            float ease = fminf(1, dt * 6);
            f->pos.x += (t.x - f->pos.x) * ease;
            f->pos.y += (t.y - f->pos.y) * ease;
        }
    }

    /* Dirs: a balloon tree from the root, which stays put. */
    balloon_measure(m, 0);
    balloon_place(m, 0, -TAU / 4, TAU, fminf(1, dt * 4));

    /* Actors: spring toward the side of their target node. */
    for (int i = 0; i < m->nactors; i++) {
        Actor *a = &m->actors[i];
        Node *t = &N[a->target];
        float ang = (float)i * 2.1f;
        Vector2 goal = {t->pos.x + cosf(ang) * (t->radius + 26), t->pos.y + sinf(ang) * (t->radius + 26)};
        a->vel.x = (a->vel.x + (goal.x - a->pos.x) * 18.0f * dt) * powf(0.004f, dt);
        a->vel.y = (a->vel.y + (goal.y - a->pos.y) * 18.0f * dt) * powf(0.004f, dt);
        a->pos.x += a->vel.x * dt;
        a->pos.y += a->vel.y * dt;
        if (a->bubble_t > 0) a->bubble_t -= dt;
    }

    for (int k = 0; k < m->fx_count; k++) m->fx[(m->fx_head + k) % MAX_FX].age += dt;
    while (m->fx_count && m->fx[m->fx_head].age >= m->fx[m->fx_head].life) {
        m->fx_head = (m->fx_head + 1) % MAX_FX;
        m->fx_count--;
    }
}
