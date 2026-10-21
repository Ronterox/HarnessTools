/* Rebuilds with warm caches: an Analysis built before, built again, gives the
 * same JSON byte for byte as a fresh one built from the same events, after
 * nothing changed, after new events that edit files (one first without its
 * result in the transcript), after a file changed on disk (same size, another
 * mtime), after a new git commit and a reset back, and after cancelled builds
 * (warm, and cold ones cancelled across the whole build). On the structure
 * fixture (tests/fixtures/structure), in a throwaway repo under WORKDIR, with
 * a copy of src/lib.c (two files, one text). Usage: test_rebuild FIXTURE_DIR WORKDIR */
#include <fcntl.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "analysis.h"
#include "transcript.h"
#include "util.h"

static int fails, checks;

#define CHECK(cond, ...)                          \
    do {                                          \
        checks++;                                 \
        if (!(cond)) {                            \
            printf("FAIL rebuild: " __VA_ARGS__); \
            printf(" (line %d)\n", __LINE__);     \
            fails++;                              \
        }                                         \
    } while (0)

static char repo[1024], work[900];

static void sh(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void sh(const char *fmt, ...) {
    char cmd[4096];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(cmd, sizeof cmd, fmt, ap);
    va_end(ap);
    if (system(cmd)) {
        printf("FAIL rebuild: setup: %s\n", cmd);
        exit(1);
    }
}

#define GIT "git -C '%s' -c user.name=t -c user.email=t@t -c commit.gpgsign=false -c core.hooksPath=/dev/null"

/* ---- events from the fixture's transcripts ---- */

typedef struct {
    Event *e;
    size_t n, cap;
} Evs;

static unsigned long seq;
static const char *skip_edit; /* a tool id whose edit is already in (added by hand) */

static void emit(void *ud, Event *ev) {
    Evs *v = ud;
    if (skip_edit && ev->kind == EV_EDIT && ev->tool_id && !strcmp(ev->tool_id, skip_edit)) {
        ev_free(ev);
        return;
    }
    if (v->n == v->cap) v->e = xrealloc(v->e, (v->cap = v->cap ? v->cap * 2 : 64) * sizeof *v->e);
    ev->seq = ++seq;
    v->e[v->n++] = *ev;
}

/* Lines [from, to) of a transcript into t; *off: where line `from` starts, then past `to`. */
static void feed_lines(Transcript *t, const char *path, int from, int to, long *off) {
    FILE *f = fopen(path, "rb");
    if (!f) {
        printf("FAIL rebuild: no %s\n", path);
        exit(1);
    }
    char *line = NULL;
    size_t cap = 0;
    ssize_t k;
    for (int i = 0; i < to && (k = getline(&line, &cap, f)) > 0; i++) {
        if (i < from) continue;
        size_t len = (size_t)k;
        while (len && line[len - 1] == '\n') len--;
        tr_feed(t, line, len, *off);
        *off += k;
    }
    free(line);
    fclose(f);
}

static void feed(Evs *v, const char *path, const char *actor) {
    Transcript *t = tr_new(repo, actor, actor, path, emit, v);
    long off = 0;
    feed_lines(t, path, 0, 1 << 30, &off);
    tr_free(t);
}

/* The first n lines of the fixture's file into path (all: n < 0). */
static void write_lines(const char *fx, const char *name, const char *path, int n) {
    sh("sed 's|/REPO|%s|g' '%s/%s' | head -n %d > '%s'", repo, fx, name, n < 0 ? 1 << 30 : n, path);
}

/* ---- builds ---- */

static char *json_of(AnSnapshot *s) {
    char *buf = NULL;
    size_t len = 0;
    FILE *f = open_memstream(&buf, &len);
    an_snapshot_json(f, s);
    fclose(f);
    an_snapshot_free(s);
    return buf;
}

/* A fresh analysis of events: its JSON. */
static char *fresh(const Event *e, size_t n) {
    Analysis *a = an_new(repo);
    an_add(a, e, n);
    char *j = json_of(an_build(a));
    an_free(a);
    return j;
}

/* The reused analysis built again agrees with a fresh one (or want); its JSON. */
static char *same_as(Analysis *a, const Event *e, size_t n, const char *what, const char *want0) {
    AnSnapshot *s = an_build(a);
    CHECK(s, "%s: no snapshot", what);
    if (!s) return xstrdup("");
    char *got = json_of(s), *want = want0 ? xstrdup(want0) : fresh(e, n);
    size_t i = 0;
    while (got[i] && got[i] == want[i]) i++;
    CHECK(!strcmp(got, want), "%s: differs from a fresh build at byte %zu: ...%.60s... vs ...%.60s...", what, i,
          got + (i > 20 ? i - 20 : 0), want + (i > 20 ? i - 20 : 0));
    free(want);
    return got;
}

static char *same(Analysis *a, const Event *e, size_t n, const char *what) { return same_as(a, e, n, what, NULL); }

static double now(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (double)t.tv_sec + t.tv_nsec / 1e9;
}

/* src/lib.c's text, same size each time, written with the given mtime. */
static void rewrite_lib(const char *cond, time_t mtime) {
    char path[1100];
    snprintf(path, sizeof path, "%s/src/lib.c", repo);
    FILE *f = fopen(path, "w");
    fprintf(f, "int add(int a, int b) {\n    int s = a + b;\n    if (%s) return 0;\n    return s;\n}\n", cond);
    fclose(f);
    struct timespec ts[2] = {{mtime, 0}, {mtime, 0}};
    utimensat(AT_FDCWD, path, ts, 0);
}

typedef struct {
    Analysis *a;
    AnSnapshot *s;
} Run;

static void *run(void *ud) {
    Run *r = ud;
    r->s = an_build(r->a);
    return NULL;
}

/* A build of a, cancelled after us microseconds (-1: before it starts). */
static void cancel_at(Analysis *a, long us) {
    if (us < 0) {
        an_cancel(a, true);
        AnSnapshot *s = an_build(a);
        CHECK(!s, "cancelled before: a snapshot");
        an_snapshot_free(s);
    } else {
        Run r = {a, NULL};
        pthread_t th;
        pthread_create(&th, NULL, run, &r);
        usleep((useconds_t)us);
        an_cancel(a, true);
        pthread_join(th, NULL);
        an_snapshot_free(r.s); /* it may have finished first */
    }
    an_cancel(a, false);
}

int main(int argc, char **argv) {
    if (argc != 3) {
        fputs("usage: test_rebuild FIXTURE_DIR WORKDIR\n", stderr);
        return 2;
    }
    const char *fx = argv[1];
    snprintf(work, sizeof work, "%s", argv[2]);
    snprintf(repo, sizeof repo, "%s/repo", work);
    /* The repo: its first state committed, today's files on disk; transcripts naming it. */
    sh("rm -rf '%s' && mkdir -p '%s' && cp -R '%s/repo/before/.' '%s/'", repo, repo, fx, repo);
    sh("git -C '%s' init -q && git -C '%s' add -A", repo, repo);
    sh("GIT_AUTHOR_DATE=2026-10-14T09:00:00Z GIT_COMMITTER_DATE=2026-10-14T09:00:00Z " GIT " commit -qm base", repo);
    sh("cp -R '%s/repo/after/.' '%s/'", fx, repo);
    const char *names[] = {"st1.jsonl", "st1/subagents/agent-sub1.jsonl", "st2.jsonl"};
    char tr[3][1200];
    for (int i = 0; i < 3; i++) {
        snprintf(tr[i], sizeof tr[i], "%s/%s", work, names[i]);
        sh("mkdir -p \"$(dirname '%s')\"", tr[i]);
        write_lines(fx, names[i], tr[i], i < 2 ? -1 : 2); /* st2: its prompt and first call, no result yet */
    }
    rewrite_lib("s < 0", 1792000000);
    sh("cp -p '%s/src/lib.c' '%s/src/lib2.c'", repo, repo);

    /* A: the session and its subagent. B: a later session's prompt and call
     * with no result in the transcript yet, its edit added by hand as the
     * stage would once the result comes (so its record is read unfinished);
     * then the rest of it, the result and another edit. */
    Evs v = {0};
    feed(&v, tr[0], "st1");
    emit(&v, &(Event){.kind = EV_TITLE, .actor = xstrdup("sub1"), .text = xstrdup("tidy"),
                      .who = {.type = xstrdup("general"), .session = xstrdup("st1")}});
    feed(&v, tr[1], "sub1");
    size_t na = v.n;
    Transcript *t2 = tr_new(repo, "st2", "st2", tr[2], emit, &v);
    long off2 = 0, call_off = 0;
    feed_lines(t2, tr[2], 0, 1, &off2);
    call_off = off2;
    feed_lines(t2, tr[2], 1, 2, &off2);
    emit(&v, &(Event){.kind = EV_EDIT, .ts = parse_iso8601("2026-10-14T11:05:00.000Z"), .actor = xstrdup("st2"),
                      .path = xstrdup("src/app.py"), .tool_id = xstrdup("e4"), .src = tr[2], .src_off = call_off,
                      .via = VIA_TRANSCRIPT, .lines = -1});
    size_t nb = v.n;

    Analysis *a = an_new(repo);
    an_add(a, v.e, na);
    char *j0 = same(a, v.e, na, "first build");
    char *j1 = same(a, v.e, na, "nothing changed");
    CHECK(!strcmp(j0, j1), "nothing changed: the output changed");

    an_add(a, v.e + na, nb - na);
    char *jb = same(a, v.e, nb, "an edit whose record has no result yet");

    write_lines(fx, names[2], tr[2], -1);
    skip_edit = "e4";
    feed_lines(t2, tr[2], 2, 1 << 30, &off2);
    skip_edit = NULL;
    tr_free(t2);
    int edits = 0;
    for (size_t i = na; i < v.n; i++) edits += v.e[i].kind == EV_EDIT;
    CHECK(na > 0 && v.n > nb && edits >= 2, "fixture: %zu events, then %zu, %d edits", na, v.n - na, edits);
    an_add(a, v.e + nb, v.n - nb);
    char *j2 = same(a, v.e, v.n, "new events editing files, the record finished");
    CHECK(strcmp(j1, jb) && strcmp(jb, j2), "new events: the output didn't change");

    rewrite_lib("s > 0", 1792000100); /* same size, another mtime */
    char *j3 = same(a, v.e, v.n, "a file changed on disk");
    CHECK(strcmp(j2, j3), "a file changed on disk: the output didn't change");

    /* A commit before the changes: their baselines (and reads' texts) come from
     * it. Then HEAD back where it was, and the commit again: their outputs too. */
    sh("GIT_AUTHOR_DATE=2026-10-14T10:30:00Z GIT_COMMITTER_DATE=2026-10-14T10:30:00Z " GIT " commit -qam later", repo);
    char *j4 = same(a, v.e, v.n, "a new commit");
    CHECK(strcmp(j3, j4), "a new commit: the output didn't change");
    sh("git -C '%s' reset -q --soft HEAD~1", repo);
    free(same_as(a, v.e, v.n, "HEAD reset to the commit before", j3));
    sh("GIT_AUTHOR_DATE=2026-10-14T10:30:00Z GIT_COMMITTER_DATE=2026-10-14T10:30:00Z " GIT " commit -qm later", repo);
    free(same_as(a, v.e, v.n, "the commit again", j4));

    /* Cancelled builds, each after a change on disk: before one starts, and at
     * times during it; the next whole build is still a fresh one's. */
    const char *conds[] = {"s < 1", "s < 2", "s < 3", "s < 4", "s < 5", "s < 6", "s < 7", "s < 8"};
    long delays[] = {-1, 0, 200, 1000, 3000, 10000, 30000, 100000}; /* microseconds */
    for (int i = 0; i < 8; i++) {
        rewrite_lib(conds[i], 1792000200 + i);
        cancel_at(a, delays[i]);
        char what[64];
        snprintf(what, sizeof what, "after a build cancelled at %ld us", delays[i]);
        free(same(a, v.e, v.n, what));
    }
    /* Cold builds (everything to cache) cancelled across their whole length. */
    double t0 = now();
    char *want = fresh(v.e, v.n);
    double cold = now() - t0;
    for (int i = 0; i < 12; i++) {
        Analysis *c = an_new(repo);
        an_add(c, v.e, v.n);
        cancel_at(c, (long)(cold * 1e6 * i / 10));
        char what[64];
        snprintf(what, sizeof what, "after a cold build cancelled at %d/10 of it", i);
        free(same_as(c, v.e, v.n, what, want));
        an_free(c);
    }
    free(want);
    free(jb);

    an_free(a);
    free(j0), free(j1), free(j2), free(j3), free(j4);
    for (size_t i = 0; i < v.n; i++) ev_free(&v.e[i]);
    free(v.e);
    printf("%s rebuild (%d checks)\n", fails ? "FAIL" : "ok  ", checks);
    return fails != 0;
}
