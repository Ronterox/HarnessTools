/* tr_change: a change re-read from the fixture transcripts (tests/make_fixtures.py). */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "transcript.h"

static int fail;

#define CHECK(cond)                                                    \
    do {                                                               \
        if (!(cond)) {                                                 \
            printf("FAIL change %s:%d: %s\n", what, __LINE__, #cond); \
            fail = 1;                                                  \
        }                                                              \
    } while (0)

/* Byte offset of the line holding tool_use `id`, as the parser records it. */
static long use_off(const char *src, const char *id) {
    char want[64];
    snprintf(want, sizeof want, "\"type\": \"tool_use\", \"id\": \"%s\"", id);
    FILE *f = fopen(src, "r");
    char *line = NULL;
    size_t cap = 0;
    long off = 0, at = -1;
    ssize_t n;
    while (f && (n = getline(&line, &cap, f)) > 0) {
        if (strstr(line, want)) { at = off; break; }
        off += n;
    }
    free(line);
    if (f) fclose(f);
    return at;
}

static Change *get(const char *src, const char *id, const char *path) {
    return tr_change(src, use_off(src, id), id, "/proj", path);
}

static int lines_are(const Hunk *h, const char *const *want, int n) {
    if (h->n != n) return 0;
    for (int i = 0; i < n; i++)
        if (strcmp(h->lines[i], want[i])) return 0;
    return 1;
}

int main(void) {
    const char *s1 = "tests/fixtures/proj/s1.jsonl", *d1 = "tests/fixtures/detail/d1.jsonl";
    const char *what;
    Change *c;

    what = "edit";
    c = get(s1, "e4", "src/a.py");
    CHECK(c->state == 0 && !c->cmd && !c->created);
    CHECK(c->original && !strcmp(c->original, "1\n2\n"));
    CHECK(c->nhunks == 1);
    if (c->nhunks == 1) {
        const Hunk *h = &c->hunks[0];
        CHECK(h->old_start == 2 && h->old_lines == 1 && h->new_start == 2 && h->new_lines == 1);
        CHECK(lines_are(h, (const char *const[]){" 1", "-2", "+two"}, 3));
    }
    change_free(c);

    what = "create";
    c = get(s1, "w1", "src/new/b.py");
    CHECK(c->state == 0 && !c->cmd && !c->original && c->nhunks == 0);
    CHECK(c->created && !strcmp(c->created, "x\ny\n"));
    change_free(c);

    what = "command";
    c = get(s1, "b1", "gen/out.c");
    CHECK(c->state == 0 && c->cmd && !c->original && !c->created && c->nhunks == 1);
    if (c->nhunks == 1) CHECK(lines_are(&c->hunks[0], (const char *const[]){"+1", "+2", "+3"}, 3));
    change_free(c);
    c = get(s1, "b1", "src/a.py");
    CHECK(c->cmd && c->nhunks == 1);
    if (c->nhunks == 1) CHECK(lines_are(&c->hunks[0], (const char *const[]){"+z"}, 1));
    change_free(c);
    c = get(s1, "b1", "elsewhere.c"); /* not one of its files */
    CHECK(c->state == 0 && c->cmd && c->nhunks == 0);
    change_free(c);

    what = "multiedit";
    c = get(s1, "e6", "src/a.py");
    CHECK(c->state == 0 && !c->cmd && !c->created && c->original && !strcmp(c->original, "one\ntwo\nthree\n"));
    CHECK(c->nhunks == 2);
    if (c->nhunks == 2) {
        CHECK(c->hunks[1].old_start == 3 && c->hunks[1].new_lines == 2);
        CHECK(lines_are(&c->hunks[0], (const char *const[]){"-one", "+1"}, 2));
        CHECK(lines_are(&c->hunks[1], (const char *const[]){"-three", "+3", "+4"}, 3));
    }
    change_free(c);

    what = "write update";
    c = get(s1, "w3", "README.md");
    CHECK(c->state == 0 && !c->cmd && !c->created && c->original && !strcmp(c->original, "a\nb\n"));
    CHECK(c->nhunks == 1);
    if (c->nhunks == 1) CHECK(lines_are(&c->hunks[0], (const char *const[]){" a", "-b", "+B"}, 3));
    change_free(c);

    what = "same name, two folders";
    c = get(s1, "b3", "Makefile");
    CHECK(c->cmd && c->nhunks == 1);
    if (c->nhunks == 1) CHECK(lines_are(&c->hunks[0], (const char *const[]){"-xx", "+yy"}, 2));
    change_free(c);
    c = get(s1, "b3", "sub/Makefile");
    CHECK(c->cmd && c->nhunks == 1);
    if (c->nhunks == 1) CHECK(lines_are(&c->hunks[0], (const char *const[]){"-x", "+y"}, 2));
    change_free(c);

    what = "failed edit";
    c = get(s1, "e2", "src/a.py");
    CHECK(c->state == 0 && !c->original && !c->created && c->nhunks == 0);
    change_free(c);

    what = "no result yet";
    c = get(d1, "d10", NULL);
    CHECK(c->state == 1 && c->nhunks == 0);
    change_free(c);

    what = "no record";
    c = tr_change(s1, 0, "nope", "/proj", "src/a.py");
    CHECK(c->state == -1 && c->nhunks == 0);
    change_free(c);
    c = tr_change("tests/fixtures/missing.jsonl", 0, "e4", "/proj", "src/a.py");
    CHECK(c->state == -1);
    change_free(c);

    if (!fail) puts("ok   change");
    return fail;
}
