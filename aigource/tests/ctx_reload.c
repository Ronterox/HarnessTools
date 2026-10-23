/* ctx_reload against a fresh ctx_load: every reload must equal a load of the
 * same bytes.
 *   ctx_reload ROOT FROM TO CUT...     (tests/context_reload.py drives it)
 *     copies transcript FROM into TO up to the first cut, loads it, then
 *     appends up to each next cut (the rest at the end) and reloads. Cuts
 *     may fall inside a line or a response.
 *   ctx_reload --cmd ROOT PATH         (the fixed cases of tests/context_reload.py)
 *     commands on stdin, one reply line each: "load", "reload", "env NAME
 *     VALUE"; the caller changes the files between them. The reply is "ok"
 *     or what differs.
 * "Equal": the JSON with each call's derived values, and every field of every
 * item and call the UI reads, item by item, and the item of every key.
 * Prints "same: N reloads" or the first difference. With AIG_DEBUG, each
 * load's phases go to stderr, a reload that had to load in full with why. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "context.h"

static char *json_of(const CtxAgent *a) {
    int *all = malloc(((size_t)a->ncalls + 1) * sizeof *all);
    for (int c = 0; c < a->ncalls; c++) all[c] = c;
    char *s = NULL;
    size_t n = 0;
    FILE *f = open_memstream(&s, &n);
    ctx_json(f, a, all, a->ncalls);
    fclose(f);
    free(all);
    return s;
}

static int streq(const char *a, const char *b) { return a == b || (a && b && !strcmp(a, b)); }
static int intseq(const int *a, int na, const int *b, int nb) { return na == nb && (!na || !memcmp(a, b, (size_t)na * sizeof *a)); }

#define NEED(cond, ...)                      \
    do {                                     \
        if (!(cond)) {                       \
            snprintf(why, n, __VA_ARGS__);   \
            return 1;                        \
        }                                    \
    } while (0)

/* The first difference between two agents' items and calls, field by field
 * (ctx_json leaves some of them out): 1 and why, or 0. */
static int differ(const CtxAgent *a, const CtxAgent *f, char *why, size_t n) {
    NEED(a->nitems == f->nitems && a->ncalls == f->ncalls && a->ninst == f->ninst, "counts: %d/%d items, %d/%d calls, %d/%d insts",
         a->nitems, f->nitems, a->ncalls, f->ncalls, a->ninst, f->ninst);
    NEED(a->missing == f->missing && a->found_elsewhere == f->found_elsewhere && a->broken == f->broken,
         "agent: missing %d/%d, elsewhere %d/%d, broken %d/%d", a->missing, f->missing, a->found_elsewhere, f->found_elsewhere,
         a->broken, f->broken);
    NEED(a->est == f->est && a->t0 == f->t0, "agent: est %d/%d, t0 %g/%g", a->est, f->est, (double)a->t0, (double)f->t0);
#define ASAME(field) NEED(streq(a->field, f->field), "agent: %s differs: \"%.80s\" / \"%.80s\"", #field, a->field ? a->field : "(null)", f->field ? f->field : "(null)")
    ASAME(title); ASAME(session); ASAME(id); ASAME(kind); ASAME(name); ASAME(desc); ASAME(tu); ASAME(model);
#undef ASAME
    for (int i = 0; i < a->ninst; i++) NEED(intseq(a->inst[i].items, a->inst[i].n, f->inst[i].items, f->inst[i].n), "inst %d", i);
    for (int i = 0; i < a->nitems; i++) {
        const CtxItem *x = &a->items[i], *y = &f->items[i];
#define SAME(field) NEED(streq(x->field, y->field), "item %d: %s differs: \"%.80s\" / \"%.80s\"", i, #field, x->field ? x->field : "(null)", y->field ? y->field : "(null)")
#define EQ(field) NEED(x->field == y->field, "item %d: %s differs: %g / %g", i, #field, (double)x->field, (double)y->field)
        EQ(kind); EQ(len); EQ(n); EQ(w); EQ(made); EQ(err); EQ(enter); EQ(leave); EQ(action); EQ(result); EQ(line0); EQ(line1); EQ(key);
        SAME(src); SAME(tool); SAME(path); SAME(lang); SAME(cmd); SAME(tuid); SAME(old_str); SAME(new_str); SAME(content); SAME(desc);
        NEED(x->len == y->len && !memcmp(x->text, y->text, x->len), "item %d: text differs", i);
        NEED(x->ntx == y->ntx && x->nst == y->nst && x->nlk == y->nlk, "item %d: %d/%d tools, %d/%d stale, %d/%d links", i, x->ntx,
             y->ntx, x->nst, y->nst, x->nlk, y->nlk);
        for (int k = 0; k < x->ntx; k++) {
            NEED(streq(x->names[k], y->names[k]), "item %d: tool name %d differs", i, k);
            NEED(!strcmp(a->str[x->tx[k]], f->str[y->tx[k]]), "item %d: tool %d differs", i, k);
        }
        for (int k = 0; k < x->nst; k++)
            NEED(x->st[k].t == y->st[k].t && x->st[k].kind == y->st[k].kind && x->st[k].item == y->st[k].item &&
                     streq(x->st[k].label, y->st[k].label),
                 "item %d: stale change %d differs", i, k);
        for (int k = 0; k < x->nlk; k++)
            NEED(x->lk[k].item == y->lk[k].item && streq(x->lk[k].why, y->lk[k].why), "item %d: link %d differs", i, k);
        NEED(ctx_item_by_key(a, x->key) == i && ctx_item_by_key(f, x->key) == i, "item %d: its key finds item %d / %d", i,
             ctx_item_by_key(a, x->key), ctx_item_by_key(f, x->key));
#undef SAME
#undef EQ
    }
    for (int c = 0; c < a->ncalls; c++) {
        const CtxCall *x = &a->calls[c], *y = &f->calls[c];
#define EQ(field) NEED(x->field == y->field, "call %d: %s differs: %g / %g", c, #field, (double)x->field, (double)y->field)
        EQ(t); EQ(base); EQ(keep); EQ(has_u); EQ(comp); EQ(wsum); EQ(f); EQ(total); EQ(noresp); EQ(norder);
        NEED(streq(x->id, y->id) && streq(x->model, y->model) && streq(x->query, y->query), "call %d: id/model/query differ", c);
        NEED(!memcmp(x->u, y->u, sizeof x->u), "call %d: usage differs", c);
        NEED(intseq(x->tail, x->ntail, y->tail, y->ntail), "call %d: tail differs", c);
        NEED(intseq(x->shape, x->nshape, y->shape, y->nshape), "call %d: shape differs", c);
        NEED(intseq(x->drop, x->ndrop, y->drop, y->ndrop), "call %d: drop differs", c);
        NEED(intseq(x->made, x->nmade, y->made, y->nmade), "call %d: made differs", c);
        NEED(intseq(x->order, x->norder, y->order, y->norder), "call %d: order differs", c);
        NEED(!x->norder || !memcmp(x->state, y->state, (size_t)x->norder), "call %d: states differ", c);
        NEED(!x->norder || !memcmp(x->cum, y->cum, (size_t)x->norder * sizeof *x->cum), "call %d: costs differ", c);
#undef EQ
    }
    return 0;
}

/* b (a reload) against fresh: 1 and a line saying what differs, or 0. */
static int compare(const CtxAgent *b, const CtxAgent *fresh, const char *where) {
    char why[300];
    char *jb = json_of(b), *jf = json_of(fresh);
    int bad = 0;
    if (strcmp(jb, jf)) {
        size_t i = 0;
        while (jb[i] == jf[i]) i++;
        size_t s0 = i > 200 ? i - 200 : 0;
        printf("%s: the JSON differs at byte %zu\n reload: %.400s\n fresh:  %.400s\n", where, i, jb + s0, jf + s0);
        bad = 1;
    } else if (differ(b, fresh, why, sizeof why)) {
        printf("%s: %s\n", where, why);
        bad = 1;
    }
    free(jb), free(jf);
    return bad;
}

static int run_cmds(const char *root, const char *path) {
    CtxProject *p = ctx_project_new(), *pf = ctx_project_new(); /* the fresh loads' own cache: independent */
    CtxAgent *a = NULL;
    char *err = NULL, line[512];
    int bad = 0;
    setvbuf(stdout, NULL, _IOLBF, 0);
    while (!bad && fgets(line, sizeof line, stdin)) {
        line[strcspn(line, "\n")] = 0;
        if (!strncmp(line, "env ", 4)) {
            char *v = strchr(line + 4, ' ');
            if (v) *v++ = 0, setenv(line + 4, v, 1);
            puts("ok");
            continue;
        }
        int re = !strcmp(line, "reload");
        if (!re && strcmp(line, "load")) {
            printf("unknown command: %s\n", line);
            return 2;
        }
        CtxAgent *b = re && a ? ctx_reload(a, p, NULL, &err) : ctx_load(root, path, p, NULL, &err);
        char *berr = err; /* the fresh load resets err: keep the reload's */
        err = NULL;
        CtxAgent *fresh = ctx_load(root, path, pf, NULL, &err);
        if (!b || !fresh) {
            printf("%s: %s\n", line, !b && berr ? berr : err ? err : "no agent");
            bad = 1;
        } else if (!(bad = compare(b, fresh, line))) puts("ok");
        free(berr), free(err);
        err = NULL;
        ctx_free(fresh);
        ctx_free(a);
        a = b;
    }
    ctx_free(a);
    ctx_project_free(p), ctx_project_free(pf);
    free(err);
    return bad;
}

int main(int argc, char **argv) {
    if (argc == 4 && !strcmp(argv[1], "--cmd")) return run_cmds(argv[2], argv[3]);
    if (argc < 4) {
        fputs("usage: ctx_reload ROOT FROM TO CUT... | ctx_reload --cmd ROOT PATH\n", stderr);
        return 2;
    }
    const char *root = argv[1], *from = argv[2], *to = argv[3];
    FILE *f = fopen(from, "rb");
    if (!f) {
        perror(from);
        return 2;
    }
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    char *all = malloc((size_t)n + 1);
    if (fread(all, 1, (size_t)n, f) != (size_t)n) return 2;
    fclose(f);
    char *err = NULL;
    CtxProject *p = ctx_project_new(), *pf = ctx_project_new(); /* the fresh loads' own cache: independent */
    CtxAgent *a = NULL;
    int bad = 0, reloads = 0;
    long done = 0;
    for (int k = 4; k <= argc && !bad; k++) {
        long cut = k < argc ? atol(argv[k]) : n;
        if (cut > n) cut = n;
        if (cut < done) cut = done;
        FILE *o = fopen(to, done ? "ab" : "wb"); /* appended, as a live transcript grows */
        fwrite(all + done, 1, (size_t)(cut - done), o);
        fclose(o);
        done = cut;
        CtxAgent *b = a ? ctx_reload(a, p, NULL, &err) : ctx_load(root, to, p, NULL, &err);
        reloads += a != NULL;
        CtxAgent *fresh = ctx_load(root, to, pf, NULL, &err);
        if (!b || !fresh) {
            printf("cut %ld: %s\n", cut, err ? err : "no agent");
            bad = 1;
        } else {
            char where[40];
            snprintf(where, sizeof where, "cut %ld", cut);
            bad = compare(b, fresh, where);
        }
        ctx_free(fresh);
        ctx_free(a);
        a = b;
    }
    ctx_free(a);
    ctx_project_free(p), ctx_project_free(pf);
    free(all);
    free(err);
    if (!bad) printf("same: %d reloads\n", reloads);
    return bad;
}
