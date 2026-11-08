#include <stdlib.h>
#include <string.h>
#include "store.h"
#include "util.h"

extern int verbose;
static int nopen; /* this file's own */
struct Stats stats;

Store *store_open(const char *path) {
    Store *s = calloc(1, sizeof *s);
    if (!s) {
        log_error("out of memory");
        return NULL;
    }
    s->f = fopen(path, "r");
    nopen++;
    return s;
}

void store_add(Store *s, const char *item) {
    s->n = clampi(s->n + 1, 0, 100);
    stats.adds++;
    if (verbose) printf("%s\n", item);
}

int store_dump(Store *s) {
    return fprintf(stdout, "%d\n", s->n) < 0 ? -1 : 0;
}

void store_close(Store *s) {
    nopen--;
    free(s);
}

static int cmp_items(const void *a, const void *b) {
    return strcmp(*(char *const *)a, *(char *const *)b);
}

void store_sort(char **v, int n) {
    qsort(v, (size_t)n, sizeof *v, cmp_items);
}
