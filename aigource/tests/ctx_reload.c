/* ctx_reload against a fresh ctx_load, at split points: copies transcript
 * FROM into TO up to the first cut, loads it, then appends up to each next cut (the
 * rest at the end) and reloads, comparing every reload with a fresh load of
 * the same bytes: the JSON with each call's derived values, and every item's
 * key. Cuts may fall inside a line or a response.
 *   ctx_reload ROOT FROM TO CUT...     (tests/context_reload.py drives it)
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

int main(int argc, char **argv) {
    if (argc < 4) {
        fputs("usage: ctx_reload ROOT FROM TO CUT...\n", stderr);
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
            char *jb = json_of(b), *jf = json_of(fresh);
            if (strcmp(jb, jf)) {
                size_t i = 0;
                while (jb[i] == jf[i]) i++;
                size_t s0 = i > 200 ? i - 200 : 0;
                printf("cut %ld: the JSON differs at byte %zu\n reload: %.400s\n fresh:  %.400s\n", cut, i, jb + s0, jf + s0);
                bad = 1;
            }
            for (int i = 0; i < b->nitems && !bad; i++)
                if (b->items[i].key != fresh->items[i].key) {
                    printf("cut %ld: item %d's key differs\n", cut, i);
                    bad = 1;
                }
            free(jb), free(jf);
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
