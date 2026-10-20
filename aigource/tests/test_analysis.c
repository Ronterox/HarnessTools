/* The structure analysis' text machinery: the line diff (as difflib lines up
 * lines; expectations from Python's difflib), applying hunks with origins,
 * the "recorded twice" check and relocating hunks into a later text. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "analysis.h"

static int fails, checks;

#define CHECK(cond, ...)                          \
    do {                                          \
        checks++;                                 \
        if (!(cond)) {                            \
            printf("FAIL analysis: " __VA_ARGS__); \
            printf(" (line %d)\n", __LINE__);     \
            fails++;                              \
        }                                         \
    } while (0)

/* Words split on single spaces, as Python's str.split(" "). */
static int words(const char *s, char **out) {
    if (!*s) return 0;
    int n = 0;
    for (const char *p = s;; p++) {
        const char *e = strchr(p, ' ');
        size_t k = e ? (size_t)(e - p) : strlen(p);
        out[n] = malloc(k + 1);
        memcpy(out[n], p, k);
        out[n++][k] = 0;
        if (!e) break;
        p = e;
    }
    return n;
}

static void line_map(const char *a, const char *b, const int *want, int nwant) {
    char *la[32], *lb[32];
    int na = words(a, la), nb = words(b, lb);
    int *m = an_line_map(la, na, lb, nb);
    int ok = nb == nwant;
    for (int i = 0; ok && i < nb; i++) ok = m[i] == want[i];
    CHECK(ok, "line map %s -> %s", a, b);
    free(m);
    for (int i = 0; i < na; i++) free(la[i]);
    for (int i = 0; i < nb; i++) free(lb[i]);
}

static Hunk hunk(int os, int ol, int ns, int nl, char **lines, int n) {
    return (Hunk){os, ol, ns, nl, lines, n};
}

int main(void) {
    /* difflib's matching blocks: the longest first, earliest in a then b */
    line_map("a b c d", "a x c d", (int[]){0, -1, 2, 3}, 4);
    line_map("} x } y", "} y", (int[]){2, 3}, 2);
    line_map("a  b  c", "a  c  b", (int[]){0, 1, -1, -1, 2}, 5);
    line_map("x y z", "", NULL, 0);
    line_map("p q r s t", "t s r q p", (int[]){-1, -1, -1, -1, 0}, 5);
    line_map("{ a } { b } { c }", "{ b } { a } { c }", (int[]){-1, -1, -1, 0, 1, 2, 3, 7, 8}, 9);

    /* apply: kept lines keep their origins, added ones get the change */
    char *l1[] = {" one", "-two", "+2", "+2b", " three"};
    Hunk h1 = hunk(1, 3, 1, 4, l1, 5);
    int orig[] = {7, 8, 9, -1}, *no = NULL, *rem = NULL, nrem = 0;
    char *t = an_apply("one\ntwo\nthree\n", &h1, 1, true, orig, 42, &no, &rem, &nrem);
    CHECK(t && !strcmp(t, "one\n2\n2b\nthree\n"), "apply text: %s", t ? t : "(null)");
    CHECK(no && no[0] == 7 && no[1] == 42 && no[2] == 42 && no[3] == 9 && no[4] == -1, "apply origins");
    CHECK(nrem == 1 && rem[0] == 2 && rem[1] == 8, "apply removed: %d", nrem);
    free(t), free(no), free(rem);
    /* a context line that doesn't match: no text when checking, applied anyway when not */
    t = an_apply("one\nTWO\nthree\n", &h1, 1, true, NULL, 0, NULL, NULL, NULL);
    CHECK(!t, "apply check");
    t = an_apply("one\nTWO\nthree\n", &h1, 1, false, NULL, 0, NULL, NULL, NULL);
    CHECK(t && !strcmp(t, "one\n2\n2b\nthree\n"), "apply unchecked: %s", t ? t : "(null)");
    free(t);
    /* an insertion (oldLines 0 starts after oldStart) and two hunks */
    char *l2[] = {"+zero"}, *l3[] = {"-three", "+3"};
    Hunk h2[] = {hunk(0, 0, 1, 1, l2, 1), hunk(3, 1, 4, 1, l3, 2)};
    t = an_apply("one\ntwo\nthree", h2, 2, true, NULL, 0, NULL, NULL, NULL);
    CHECK(t && !strcmp(t, "zero\none\ntwo\n3"), "apply two hunks: %s", t ? t : "(null)");
    free(t);
    /* everything removed: "" is one line, with no origin */
    char *l4[] = {"-a"};
    Hunk h4 = hunk(1, 1, 0, 0, l4, 1);
    int o4[] = {3};
    t = an_apply("a", &h4, 1, true, o4, 5, &no, NULL, NULL);
    CHECK(t && !*t && no[0] == -1, "apply all removed");
    free(t), free(no);

    /* has_new: the text already reads as the hunks leave it */
    CHECK(an_has_new("one\n2\n2b\nthree\n", &h1, 1), "has_new");
    CHECK(!an_has_new("one\ntwo\nthree\n", &h1, 1), "has_new before");
    CHECK(an_has_new("anything", NULL, 0), "has_new without hunks");

    /* relocate: each hunk's new side, nearest its newStart */
    int st[2];
    CHECK(an_relocate("x\none\n2\n2b\nthree\ny\none\n2\n2b\nthree", &h1, 1, st) && st[0] == 2, "relocate nearest: %d", st[0]);
    Hunk h1far = hunk(1, 3, 9, 4, l1, 5);
    CHECK(an_relocate("x\none\n2\n2b\nthree\ny\none\n2\n2b\nthree", &h1far, 1, st) && st[0] == 7, "relocate far: %d", st[0]);
    CHECK(!an_relocate("one\ntwo\nthree", &h1, 1, st), "relocate missing");
    char *l5[] = {"+", "+  "}; /* only blank lines: stays where it says */
    Hunk h5[] = {hunk(1, 3, 1, 4, l1, 5), hunk(9, 0, 12, 2, l5, 2)};
    CHECK(an_relocate("one\n2\n2b\nthree", h5, 2, st) && st[0] == 1 && st[1] == 12, "relocate blank");

    /* Big repetitive files (a lockfile): 50k lines, most of them repeated, a few
     * edits spread out. Lined up in well under a second, unique lines exactly. */
    enum { BLK = 10000, N = BLK * 5 };
    char **la = malloc(N * sizeof *la), **lb = malloc((N + 64) * sizeof *lb);
    static const char *rep_[] = {"{", NULL, "  version: 1.0.0", "  resolved: true", "}"};
    for (int k = 0; k < BLK; k++)
        for (int r = 0; r < 5; r++) {
            char buf[64];
            snprintf(buf, sizeof buf, "  name: pkg%d", k);
            la[k * 5 + r] = strdup(rep_[r] ? rep_[r] : buf);
        }
    int nb = 0, *want = malloc((N + 64) * sizeof *want);
    for (int i = 0; i < N; i++) {
        if (i >= 20000 && i < 20100) continue;                                   /* 100 lines removed */
        if (i == 100 || i == 40000) lb[nb] = strdup("  extra: x"), want[nb++] = -1; /* lines added */
        if (i == 45002) { lb[nb] = strdup("  version: 2.0.0"), want[nb++] = -1; continue; } /* one changed */
        lb[nb] = strdup(la[i]);
        want[nb++] = i;
    }
    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    int *m = an_line_map(la, N, lb, nb);
    clock_gettime(CLOCK_MONOTONIC, &t1);
    double secs = (double)(t1.tv_sec - t0.tv_sec) + (t1.tv_nsec - t0.tv_nsec) / 1e9;
    int wrong_unique = 0, unmatched = 0;
    for (int j = 0; j < nb; j++) {
        if (want[j] >= 0 && !strncmp(lb[j], "  name:", 7) && m[j] != want[j]) wrong_unique++;
        unmatched += want[j] >= 0 && m[j] < 0;
    }
    CHECK(secs < 1.0, "50k-line line map took %.2f s", secs);
    CHECK(!wrong_unique, "50k-line line map: %d unique lines misplaced", wrong_unique);
    CHECK(unmatched < 50, "50k-line line map: %d kept lines unmatched", unmatched);
    printf("     50k-line line map: %.3f s, %d of %d kept lines unmatched\n", secs, unmatched, nb);
    free(m);
    /* No unique line at all: 50k alternating lines, one inserted in the middle. */
    for (int i = 0; i < N; i++) free(la[i]), la[i] = strdup(i % 2 ? "b" : "a");
    for (int j = 0; j < nb; j++) free(lb[j]);
    nb = 0;
    for (int i = 0; i < N; i++) {
        if (i == N / 2) lb[nb++] = strdup("c");
        lb[nb++] = strdup(la[i]);
    }
    clock_gettime(CLOCK_MONOTONIC, &t0);
    m = an_line_map(la, N, lb, nb);
    clock_gettime(CLOCK_MONOTONIC, &t1);
    secs = (double)(t1.tv_sec - t0.tv_sec) + (t1.tv_nsec - t0.tv_nsec) / 1e9;
    CHECK(secs < 1.0 && m[0] == 0 && m[N / 2] == -1 && m[N / 2 + 1] == N / 2 && m[nb - 1] == N - 1,
          "50k repeated lines: %.2f s", secs);
    free(m);
    for (int i = 0; i < N; i++) free(la[i]);
    for (int j = 0; j < nb; j++) free(lb[j]);
    free(la), free(lb), free(want);

    printf("%s analysis (%d checks)\n", fails ? "FAIL" : "ok  ", checks);
    return fails != 0;
}
