#include "model.h"

#include <fcntl.h>
#include <math.h>
#include <sys/stat.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define DIR_GAP 40.0f       /* free space between a dir and its child dirs */
#define FADE_IN 0.4f
#define FADE_OUT 1.5f
#define FLASH_DECAY 0.8f    /* per second */
#define BUBBLE_SECS 4.0f
#define GOLDEN_ANGLE 2.39996323f
#define MAX_SPEED 1500.0f   /* world units/s: keeps the explicit integrator stable */

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
    n->radius = is_dir ? 10 : 5;
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
    m->nodes[parent].ndirs++;
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

static void add_fx(Scene *m, FxKind kind, EvKind ev, int actor, int node, float strength,
                   float life, Color c) {
    int slot = (m->fx_head + m->fx_count) % MAX_FX;
    if (m->fx_count == MAX_FX) m->fx_head = (m->fx_head + 1) % MAX_FX; /* drop oldest */
    else m->fx_count++;
    m->fx[slot] = (Fx){kind, ev, actor, node, strength, 0, life, c};
}

static float diff_strength(int n) { return fminf(1.0f, log2f(1.0f + (float)n) / 9.0f); }

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
    if (!e->path || !*e->path) return;

    int ni = ensure_file(m, e->path);
    Node *n = &m->nodes[ni];
    n->last_touch = e->ts;
    n->last_kind = e->kind;
    n->last_actor = ai;
    n->last_add = e->add;
    n->last_del = e->del;
    a->target = ni;

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
    float s = diff_strength(e->add + e->del);
    add_fx(m, FX_BEAM, e->kind, ai, ni, e->kind == EV_READ ? 0.1f : 0.25f + 0.75f * s, 1.4f,
           kind_color(e->kind));
    if (e->add) add_fx(m, FX_PULSE, e->kind, ai, ni, diff_strength(e->add), 1.2f, kind_color(EV_CREATE));
    if (e->del) add_fx(m, FX_PULSE, e->kind, ai, ni, diff_strength(e->del), 1.2f, kind_color(EV_DELETE));
    if (!e->add && !e->del && e->kind != EV_READ)
        add_fx(m, FX_PULSE, e->kind, ai, ni, 0.3f, 1.0f, kind_color(e->kind));
}

/* ---- layout ------------------------------------------------------------- */

static float file_radius(const Node *n) {
    if (n->lines < 0 && n->disp_lines <= 0) return 5;
    return fminf(26.0f, 3.0f + 2.0f * sqrtf(fmaxf(n->disp_lines, 0) / 10.0f));
}

/* Files sit on a sunflower spiral around their dir; the dir's radius is its extent. */
static Vector2 slot_offset(int i, float spacing) {
    float r = spacing * 0.62f * sqrtf((float)i + 0.5f);
    float a = (float)i * GOLDEN_ANGLE;
    return (Vector2){cosf(a) * r, sinf(a) * r};
}

void model_update(Scene *m, float dt, double now) {
    (void)now;
    if (dt > 1.0f / 20) dt = 1.0f / 20;
    Node *N = m->nodes;

    /* Files: size easing, fades, spiral slots. */
    for (int i = 0; i < m->nnodes; i++) {
        Node *n = &N[i];
        n->flash = fmaxf(0, n->flash - FLASH_DECAY * dt);
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
    for (int d = 0; d < m->nnodes; d++) {
        Node *dir = &N[d];
        if (!dir->is_dir) continue;
        if (dir->alpha < 1) dir->alpha = fminf(1, dir->alpha + dt / FADE_IN);
        float maxr = 0, sum = 0;
        for (int k = 0; k < dir->nkids; k++) {
            sum += N[dir->kids[k]].radius;
            maxr = fmaxf(maxr, N[dir->kids[k]].radius);
        }
        float spacing = dir->nkids ? fmaxf(12.0f, 2.2f * sum / dir->nkids + 4) : 12;
        float target_r = 10 + (dir->nkids ? spacing * 0.62f * sqrtf((float)dir->nkids) + maxr : 0);
        dir->radius += (target_r - dir->radius) * fminf(1, dt * 3);
        for (int k = 0; k < dir->nkids; k++) {
            Node *f = &N[dir->kids[k]];
            Vector2 o = slot_offset(k, spacing);
            Vector2 t = {dir->pos.x + o.x, dir->pos.y + o.y};
            float ease = fminf(1, dt * 6);
            f->pos.x += (t.x - f->pos.x) * ease;
            f->pos.y += (t.y - f->pos.y) * ease;
        }
    }

    /* Dirs: overlap repulsion between all dirs, spring to parent. O(dirs^2). */
    for (int i = 1; i < m->nnodes; i++) {
        Node *a = &N[i];
        if (!a->is_dir) continue;
        Vector2 f = {0, 0};
        Node *p = &N[a->parent];
        float dx = a->pos.x - p->pos.x, dy = a->pos.y - p->pos.y;
        float dist = sqrtf(dx * dx + dy * dy) + 1e-3f;
        float rest = p->radius + a->radius + DIR_GAP;
        float k = (dist - rest) * 4.0f;
        f.x -= dx / dist * k;
        f.y -= dy / dist * k;
        for (int j = 0; j < m->nnodes; j++) {
            Node *b = &N[j];
            if (j == i || !b->is_dir) continue;
            float ex = a->pos.x - b->pos.x, ey = a->pos.y - b->pos.y;
            float d2 = ex * ex + ey * ey;
            float min = a->radius + b->radius + DIR_GAP * 0.75f;
            if (d2 < 1e-4f) { ex = frand() - 0.5f; ey = frand() - 0.5f; d2 = 0.01f; }
            float d = sqrtf(d2);
            float push = d < min ? (min - d) * 12.0f : 0;
            /* spread siblings; distance floored so near-coincident spawns don't explode */
            push += 4000.0f / fmaxf(d2, 400.0f) * (b->parent == a->parent ? 3.0f : 1.0f);
            f.x += ex / d * push;
            f.y += ey / d * push;
        }
        a->vel.x = (a->vel.x + f.x * dt) * powf(0.05f, dt);
        a->vel.y = (a->vel.y + f.y * dt) * powf(0.05f, dt);
        float sp = sqrtf(a->vel.x * a->vel.x + a->vel.y * a->vel.y);
        if (sp > MAX_SPEED) { a->vel.x *= MAX_SPEED / sp; a->vel.y *= MAX_SPEED / sp; }
    }
    for (int i = 1; i < m->nnodes; i++) {
        Node *a = &N[i];
        if (!a->is_dir) continue;
        a->pos.x += a->vel.x * dt;
        a->pos.y += a->vel.y * dt;
    }

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
