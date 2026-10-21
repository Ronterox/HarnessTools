/* Unit test for the structure screen's search (ssearch.c): names ranked like
 * a file finder and their highlights, content hits with smart case, lines and
 * columns (CRLF, no final newline, UTF-8, bytes that aren't UTF-8), the
 * snippet, the names index against ss_name, and the worker: binary texts
 * skipped, the cap, cancelled and superseded searches. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "ssearch.h"
#include "util.h"

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

static const char *query;
static int by_rank(const void *a, const void *b) {
    const char *pa = *(const char *const *)a, *pb = *(const char *const *)b;
    SsName ma, mb;
    ss_name(query, pa, &ma);
    ss_name(query, pb, &mb);
    return ss_name_cmp(&ma, pa, &mb, pb);
}

static void ranking(void) {
    /* shuffled; the expected order, best first */
    const char *want[] = {"a/sview", "src/sview.c", "src/sview_int.h", "src/old_sview.c", "sview/main.c",
                          "src/s_view.c", "src/sxvxixexwx.c"};
    const char *in[] = {"src/sxvxixexwx.c", "sview/main.c", "src/old_sview.c", "src/sview_int.h", "a/sview",
                        "src/s_view.c", "src/sview.c"};
    int n = 7;
    query = "sview";
    qsort(in, (size_t)n, sizeof *in, by_rank);
    for (int i = 0; i < n; i++) {
        if (strcmp(in[i], want[i])) printf("  rank %d: %s, want %s\n", i, in[i], want[i]);
        CHECK(!strcmp(in[i], want[i]));
    }
    SsName m;
    CHECK(ss_name("sview", "a/sview", &m) && m.tier == SS_EXACT);
    CHECK(ss_name("SView", "src/sview.c", &m) && m.tier == SS_PREFIX); /* names: always case-insensitive */
    CHECK(ss_name("sview", "src/old_sview.c", &m) && m.tier == SS_BASE);
    CHECK(ss_name("src/sv", "src/sview.c", &m) && m.tier == SS_PATH);
    CHECK(ss_name("svc", "src/sview.c", &m) && m.tier == SS_FUZZY);
    CHECK(!ss_name("zz", "src/sview.c", &m));
    CHECK(!ss_name("", "src/sview.c", &m));
    /* fuzzy: contiguous runs and word starts score higher */
    SsName a, b;
    ss_name("bld", "src/build.c", &a);
    ss_name("bld", "src/abxlxd.c", &b);
    CHECK(a.tier == SS_FUZZY && b.tier == SS_FUZZY && a.score > b.score);
    ss_name("sc", "src/s_c.c", &a);   /* c at a word start */
    ss_name("sc", "src/sxc.c", &b);
    CHECK(a.score > b.score);
}

static void highlights(void) {
    SsName m;
    /* s after the slash, v consecutive, c after the dot */
    CHECK(ss_name("svc", "src/sview.c", &m) && m.npos == 3);
    CHECK(m.pos[0] == 4 && m.pos[1] == 5 && m.pos[2] == 10);
    /* a camel-case capital */
    CHECK(ss_name("nb", "src/anBuild.c", &m) && m.tier == SS_BASE); /* "nB" is a substring */
    CHECK(ss_name("ab", "src/anBuild.c", &m) && m.tier == SS_FUZZY && m.pos[0] == 4 && m.pos[1] == 6);
    /* substring tiers: every character of the run */
    CHECK(ss_name("view", "src/sview.c", &m) && m.npos == 4 && m.pos[0] == 5 && m.pos[3] == 8);
    /* UTF-8: byte offsets of characters, capitals folded */
    CHECK(ss_name("\xC3\x87\xC3\x83O", "docs/a\xC3\xA7\xC3\xA3o.md", &m)); /* ÇÃO in ação.md */
    CHECK(m.tier == SS_BASE && m.npos == 3 && m.pos[0] == 6 && m.pos[1] == 8 && m.pos[2] == 10);
    CHECK(ss_name("\xC3\xA7o", "docs/a\xC3\xA7\xC3\xA3o.md", &m) && m.tier == SS_FUZZY && m.pos[0] == 6 && m.pos[1] == 10);
}

static void smart_case(void) {
    CHECK(!ss_smart_case("an_build"));
    CHECK(ss_smart_case("anBuild"));
    CHECK(!ss_smart_case("\xC3\xA9mile")); /* émile */
    CHECK(ss_smart_case("\xC3\x89mile"));  /* Émile */
    const char *t = "Foo bar\nfoo\nFOO\nbar\n";
    SsLine l[8];
    CHECK(ss_find(t, strlen(t), "foo", false, l, 8, NULL) == 3);
    CHECK(ss_find(t, strlen(t), "Foo", true, l, 8, NULL) == 1 && l[0].line == 1);
    CHECK(ss_find(t, strlen(t), "FOO", true, l, 8, NULL) == 1 && l[0].line == 3);
}

static void lines_cols(void) {
    const char *t = "alpha\n  beta gamma gamma\ngamma\n\nx";
    SsLine l[8];
    CHECK(ss_find(t, strlen(t), "gamma", false, l, 8, NULL) == 2); /* once per line */
    CHECK(l[0].line == 2 && l[0].col == 7 && l[0].len == 5 && l[0].at == 6);
    CHECK(l[1].line == 3 && l[1].col == 0 && l[1].at == 25);
    CHECK(ss_find(t, strlen(t), "x", false, l, 8, NULL) == 1 && l[0].line == 5 && l[0].col == 0);
    char s[SS_SNIP + 16];
    int ms, ml;
    CHECK(ss_find(t, strlen(t), "beta", false, l, 8, NULL) == 1);
    ss_snip(t, strlen(t), &l[0], s, sizeof s, &ms, &ml);
    CHECK(!strcmp(s, "beta gamma gamma") && ms == 0 && ml == 4); /* leading blanks trimmed */
    /* a long line: cut around the match */
    char big[600];
    memset(big, 'y', sizeof big);
    memcpy(big + 400, "needle", 6);
    big[599] = 0;
    CHECK(ss_find(big, strlen(big), "needle", false, l, 8, NULL) == 1 && l[0].col == 400);
    ss_snip(big, strlen(big), &l[0], s, sizeof s, &ms, &ml);
    CHECK(!strncmp(s, "\xE2\x80\xA6", 3) && !strncmp(s + ms, "needle", 6) && ml == 6);
    CHECK(strlen(s) <= SS_SNIP + 6 && !strcmp(s + strlen(s) - 3, "\xE2\x80\xA6"));
}

static void crlf(void) {
    const char *t = "one\r\ntwo match\r\n\tthree match"; /* CRLF, and a last line without one */
    SsLine l[8];
    char s[SS_SNIP + 16];
    int ms, ml;
    CHECK(ss_find(t, strlen(t), "match", false, l, 8, NULL) == 2);
    CHECK(l[0].line == 2 && l[0].col == 4 && l[1].line == 3 && l[1].col == 7);
    ss_snip(t, strlen(t), &l[0], s, sizeof s, &ms, &ml);
    CHECK(!strcmp(s, "two match") && ms == 4 && ml == 5);
    ss_snip(t, strlen(t), &l[1], s, sizeof s, &ms, &ml);
    CHECK(!strcmp(s, "three match") && ms == 6 && ml == 5);
    CHECK(ss_find(t, strlen(t), "one", false, l, 8, NULL) == 1);
    ss_snip(t, strlen(t), &l[0], s, sizeof s, &ms, &ml);
    CHECK(!strcmp(s, "one")); /* no \r */
}

static void utf8(void) {
    const char *t = "na\xC3\xAFve caf\xC3\xA9\nCAF\xC3\x89 au lait\n\xCE\xA3\xCE\xBF\xCF\x86\xCE\xAF\xCE\xB1\n"; /* naïve café, CAFÉ, Σοφία */
    SsLine l[8];
    size_t n = strlen(t), ml;
    CHECK(ss_find(t, n, "caf\xC3\xA9", false, l, 8, NULL) == 2); /* café: either case */
    CHECK(l[0].line == 1 && l[0].col == 7 && l[0].len == 5 && l[1].line == 2 && l[1].col == 0 && l[1].len == 5);
    CHECK(ss_find(t, n, "CAF\xC3\x89", true, l, 8, NULL) == 1 && l[0].line == 2); /* CAFÉ: smart case */
    CHECK(ss_find(t, n, "\xCF\x83\xCE\xBF\xCF\x86", false, l, 8, NULL) == 1 && l[0].line == 3); /* σοφ finds Σοφ */
    CHECK(ss_find(t, n, "\xC3\xAF", false, l, 8, NULL) == 1 && l[0].col == 2); /* ï */
    CHECK(ss_match("\xC3\x89""COLE", 6, 0, "\xC3\xA9""cole", false, &ml) == 0 && ml == 6);
    CHECK(ss_match("x\xC3\xA9", 3, 0, "\xA9", true, &ml) == 2); /* a byte matches itself when asked byte-wise */
    /* invalid UTF-8 in the text doesn't stop a search */
    const char *bad = "\xFF\xFE ok\n\xC3 ok\n";
    CHECK(ss_find(bad, strlen(bad), "\xC3\xA9", false, l, 8, NULL) == 0);
    CHECK(ss_find(bad, strlen(bad), "OK", true, l, 8, NULL) == 0 && ss_find(bad, strlen(bad), "ok", false, l, 8, NULL) == 2);
}

static void cap_stop(void) {
    size_t n = 700 * 6;
    char *t = xmalloc(n + 1);
    for (int i = 0; i < 700; i++) memcpy(t + i * 6, "x hit\n", 6);
    t[n] = 0;
    SsLine *l = xmalloc(sizeof *l * (SS_CAP + 1));
    CHECK(ss_find(t, n, "hit", false, l, SS_CAP, NULL) == SS_CAP + 1); /* counting stops past the cap */
    CHECK(l[SS_CAP - 1].line == SS_CAP);
    CHECK(ss_find(t, n, "hit", false, l, 0, NULL) == 1);
    atomic_bool stop;
    atomic_init(&stop, true);
    CHECK(ss_find(t, n, "hit", false, l, SS_CAP, &stop) == -1);
    free(l);
    free(t);
}

/* ---- the worker ---- */

static SsResult wait_result(SsEngine *e) {
    SsResult r = {0};
    for (int i = 0; i < 20000; i++) {
        if (ss_engine_poll(e, &r)) return r;
        usleep(500);
    }
    printf("FAIL no result\n");
    fails++;
    return r;
}

static double ms_since(struct timespec t0) {
    struct timespec t1;
    clock_gettime(CLOCK_MONOTONIC, &t1);
    return (double)(t1.tv_sec - t0.tv_sec) * 1e3 + (double)(t1.tv_nsec - t0.tv_nsec) / 1e6;
}

static void engine(void) {
    const char *a = "first\nthe needle here\n", *mem = "in memory\nneedle too\r\n";
    SsFile f[] = {{a, strlen(a)}, {"needle\0binary", 13}, {"", 0}, {NULL, 0}, {mem, strlen(mem)}};
    SsEngine *e = ss_engine_new();
    ss_engine_files(e, f, 5);
    unsigned g = ss_engine_search(e, "needle", false);
    SsResult r = wait_result(e);
    CHECK(r.gen == g && r.n == 2 && !r.capped && r.files == 3); /* a binary text and a missing one skipped, the empty one searched */
    CHECK(r.n == 2 && r.hits[0].file == 0 && r.hits[0].line == 2 && r.hits[0].col == 4 && !strcmp(r.hits[0].snip, "the needle here"));
    CHECK(r.n == 2 && r.hits[1].file == 4 && r.hits[1].line == 2 && !strcmp(r.hits[1].snip, "needle too"));
    ss_result_free(&r);
    ss_engine_search(e, "NEEDLE", true);
    r = wait_result(e);
    CHECK(r.n == 0 && !ss_engine_busy(e));
    ss_result_free(&r);

    /* the cap */
    size_t n = 700 * 6;
    char *t = xmalloc(n + 1);
    for (int i = 0; i < 700; i++) memcpy(t + i * 6, "x hit\n", 6);
    t[n] = 0;
    SsFile cf[] = {{t, n}, {t, n}};
    ss_engine_files(e, cf, 2);
    ss_engine_search(e, "hit", false);
    r = wait_result(e);
    CHECK(r.n == SS_CAP && r.capped && r.hits[SS_CAP - 1].file == 0);
    ss_result_free(&r);
    ss_engine_files(e, cf, 1);
    ss_engine_search(e, "hit", false);
    r = wait_result(e);
    CHECK(r.n == SS_CAP && r.capped);
    ss_result_free(&r);

    /* cancelled: a newer search replaces a running one, whose result never shows */
    size_t hn = (size_t)64 << 20;
    char *huge = xmalloc(hn + 1);
    memset(huge, 'q', hn);
    huge[hn] = 0;
    SsFile hf[] = {{huge, hn}, {"find me\n", 8}};
    ss_engine_files(e, hf, 2);
    struct timespec t0;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    ss_engine_search(e, "qqqx", false); /* scans all 64 MB */
    r = wait_result(e);
    double full = ms_since(t0);
    CHECK(r.n == 0);
    ss_result_free(&r);
    clock_gettime(CLOCK_MONOTONIC, &t0);
    unsigned old = ss_engine_search(e, "qqqx", false);
    usleep(2000);
    CHECK(ss_engine_busy(e));
    unsigned now = ss_engine_search(e, "find", false);
    r = wait_result(e);
    double sup = ms_since(t0);
    CHECK(r.gen == now && r.gen != old);
    CHECK(r.n == 1 && r.hits[0].file == 1 && r.hits[0].line == 1);
    /* the newer one ran without waiting for the old one's scan */
    if (!(sup < full / 2)) printf("  superseding took %.1f ms, a full scan %.1f ms\n", sup, full);
    CHECK(sup < full / 2);
    ss_result_free(&r);
    usleep(20000);
    CHECK(!ss_engine_poll(e, &r)); /* nothing more: the old one's was dropped */
    ss_engine_search(e, "qqqx", false);
    usleep(2000);
    ss_engine_cancel(e);
    for (int i = 0; i < 2000 && ss_engine_busy(e); i++) usleep(500);
    CHECK(!ss_engine_busy(e) && !ss_engine_poll(e, &r));
    /* new files while one runs: it lets go of the old ones first */
    ss_engine_search(e, "qqqx", false);
    usleep(2000);
    ss_engine_files(e, f, 5);
    free(huge);
    CHECK(!ss_engine_poll(e, &r));
    ss_engine_free(e);
    free(t);
}

/* ss_names_find against ss_name on each path and ss_name_cmp's order, on
 * random paths: the same matches, the same best ones in the same order; a
 * query extending the last one (narrowed to its matches) the same as a fresh one. */
static void names_index(void) {
    static const char *const al[] = {"a", "b", "B", "c", "/", "_", ".", "-", "\xC3\xA9", "\xC3\x89", "s", "v"};
    srand(7);
    int n = 3000, bad = 0;
    char **p = xmalloc(sizeof *p * (size_t)n);
    for (int i = 0; i < n; i++) {
        char b[256] = "";
        for (int k = rand() % 30; k >= 0; k--) strcat(b, al[rand() % 12]);
        p[i] = xstrdup(b);
    }
    SsNames *x = ss_names_new((const char *const *)p, n), *fresh = ss_names_new((const char *const *)p, n);
    int *out = xmalloc(sizeof *out * (size_t)n), *out2 = xmalloc(sizeof *out2 * (size_t)n);
    SsName *ms = xmalloc(sizeof *ms * (size_t)n);
    const char *qs[] = {"a", "ab", "abc", "s", "sv", "s/v", "\xC3\xA9", "\xC3\xA9" "b", "zz", "a.b", "AB", "ba_"};
    for (int k = 0; k < 12; k++) {
        int top = k % 3 ? 20 : 3000, got = ss_names_find(x, qs[k], out, top), want = 0;
        ss_names_find(fresh, "#", out2, 1); /* a fresh search: nothing to narrow from */
        int got2 = ss_names_find(fresh, qs[k], out2, top);
        for (int i = 0; i < n; i++) want += ss_name(qs[k], p[i], &ms[i]);
        bad += got != want || got2 != got;
        for (int i = 0; i < got; i++) bad += !ss_name(qs[k], p[out[i]], &ms[out[i]]);
        for (int i = 0; i + 1 < got && i + 1 < top; i++)
            bad += ss_name_cmp(&ms[out[i]], p[out[i]], &ms[out[i + 1]], p[out[i + 1]]) > 0;
        for (int i = 0; i < got && i < top; i++) bad += out[i] != out2[i];
        /* the best are the best: none of the rest ranks above the last of them */
        for (int i = top; i < got; i++)
            bad += ss_name_cmp(&ms[out[i]], p[out[i]], &ms[out[top - 1]], p[out[top - 1]]) < 0;
        SsName m;
        for (int i = 0; i < got && i < 5; i++)
            bad += !ss_names_match(x, qs[k], out[i], &m) || m.tier != ms[out[i]].tier || m.score != ms[out[i]].score;
    }
    if (bad) printf("  names index: %d mismatches\n", bad);
    CHECK(!bad);
    /* ranking() as an index */
    const char *rk[] = {"src/sxvxixexwx.c", "sview/main.c", "src/old_sview.c", "src/sview_int.h", "a/sview",
                        "src/s_view.c", "src/sview.c", "src/zzz.c"};
    SsNames *r = ss_names_new(rk, 8);
    int o[8];
    CHECK(ss_names_find(r, "sview", o, 3) == 7);
    CHECK(!strcmp(rk[o[0]], "a/sview") && !strcmp(rk[o[1]], "src/sview.c") && !strcmp(rk[o[2]], "src/sview_int.h"));
    ss_names_free(r);
    for (int i = 0; i < n; i++) free(p[i]);
    free(p), free(out), free(out2), free(ms);
    ss_names_free(x);
    ss_names_free(fresh);
}

/* Bytes that aren't valid UTF-8, queries past SS_QMAX, paths past PMAX, empty texts. */
static void edges(void) {
    SsLine l[8];
    size_t ml;
    const char *t = "a\xF5\x80\x80" "b\n\xFF" "b\n";
    CHECK(ss_find(t, strlen(t), "b", false, l, 8, NULL) == 2 && l[0].col == 4 && l[1].col == 1);
    CHECK(ss_match("\xC0\xAF", 2, 0, "/", false, &ml) == -1);         /* an overlong '/' is no '/' */
    CHECK(ss_match("\xE0\x80\xAF", 3, 0, "/", false, &ml) == -1);
    CHECK(ss_match("x\xF0\x80\x80\xAF", 5, 0, "/", false, &ml) == -1);
    CHECK(ss_match("\xED\xA0\x80z", 4, 0, "z", false, &ml) == 3);      /* a surrogate: three bytes of their own */
    CHECK(ss_match("\xF4\x90\x80\x80", 4, 0, "\xF4\x90\x80\x80", false, &ml) == 0 && ml == 4);
    /* through the decoder (a non-ASCII query): an invalid lead is a character of one byte */
    CHECK(ss_match("\xE0\x83\xA9", 3, 0, "\xC3\xA9", false, &ml) == -1); /* an overlong 'é' is no 'é' */
    CHECK(ss_match("\xF0\x80\x83\xA9", 4, 0, "\xC3\xA9", false, &ml) == -1);
    CHECK(ss_match("\xC0\xAF\xC3\xA9", 4, 0, "/\xC3\xA9", false, &ml) == -1);
    CHECK(ss_match("\xC0\xAF", 2, 0, "\xC0", false, &ml) == 0 && ml == 1);
    CHECK(ss_match("\xED\xA0\x80", 3, 0, "\xED", false, &ml) == 0 && ml == 1);
    CHECK(ss_match("\xF5\x80\x80\x80", 4, 0, "\xF5", false, &ml) == 0 && ml == 1);
    SsName m;
    CHECK(!ss_name("/", "\xC0\xAF", &m));
    CHECK(ss_name("\xFF", "dir/\xFF.c", &m) && m.tier == SS_PREFIX && m.pos[0] == 4);
    CHECK(ss_find("", 0, "x", false, l, 8, NULL) == 0);
    CHECK(ss_find("x", 1, "", false, l, 8, NULL) == 0);
    /* a query's first SS_QMAX characters count */
    char q[SS_QMAX + 8], p[SS_QMAX + 8];
    memset(q, 'a', SS_QMAX), memcpy(q + SS_QMAX, "zzzz", 5);
    memset(p, 'a', SS_QMAX), p[SS_QMAX] = 0;
    CHECK(ss_name(q, p, &m) && m.tier == SS_EXACT && m.npos == SS_QMAX);
    /* a path longer than the fuzzy tier looks at: substrings still anywhere, fuzzy only in its start */
    char *lp = xmalloc(3000);
    memset(lp, 'x', 2000);
    strcpy(lp + 2000, "/target_name.c");
    CHECK(ss_name("target", lp, &m) && m.tier == SS_PREFIX && m.pos[0] == 2001);
    CHECK(ss_name("tgname", lp, &m) == false);     /* its letters only past the first 1024 characters */
    CHECK(ss_name("x/t", lp, &m) && m.tier == SS_PATH && m.pos[0] == 1999);
    free(lp);
}

int main(void) {
    ranking();
    highlights();
    smart_case();
    lines_cols();
    crlf();
    utf8();
    cap_stop();
    names_index();
    edges();
    engine();
    if (fails) printf("FAIL ssearch (%d)\n", fails);
    else printf("ok   ssearch\n");
    return fails != 0;
}
