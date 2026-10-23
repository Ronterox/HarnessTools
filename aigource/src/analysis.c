/* Structure analysis: a port of aigource/draft/build_data.py (kept as the test
 * oracle, tests/structure_parity.py), per aigource/draft/METHODS.md. Names in
 * comments (apply, resync, later_text, ...) are build_data.py's.
 *
 * Deviations from build_data.py, on purpose:
 * - Scope: the whole repo (git ls-files, tracked and untracked-not-ignored, at
 *   most 5000, binary files skipped) instead of .c/.py under chosen folders.
 *   Changes count for those files and for files history changed that are gone
 *   from disk now; not for ignored files that are still there.
 * - Functions and classes come from tree-sitter textobjects queries
 *   (syn_defs), not regexes and indentation; complexity counts branch nodes.
 * - Events come from transcript.c: a command's reads are found by cmdguess
 *   (less, more, tac, < count too; a token needs no extension; quotes and
 *   here-documents are lexed); reads outside the repo carry absolute paths, shown
 *   as ~/... like build_data's. Searches outside the repo come from the plain
 *   steps transcript.c makes for them. A comment comes from the first copy of
 *   a resumed session's records (the event ingest keeps), not the last.
 * - The tree is capped at 5000 files: the files history changed first, then
 *   the others in path order (stats.cut counts those left out).
 * - A subagent's name is its agentType after the last ':' (METHODS.md).
 * - git baselines: one `git log` per file, then the first commit at or before
 *   the change's second, which is what `log -1 --before=<ts>` gives.
 * - Texts aren't newline-translated (Python reads files with universal
 *   newlines); file nodes carry no "src".
 *
 * How: lines line up the way difflib does (an_line_map: longest matching
 * block first), so origins carry over exactly as the oracle's and blame
 * agrees line for line; past DIFFLIB_MAX line pairs (difflib is superlinear
 * on repeated lines), prefix, suffix and patience anchors first. The replay runs twice: first
 * in time order for texts, origins, turns and costs (nothing there needs a
 * file's functions), then per file on worker threads for everything that does
 * (functions touched, changed unread, rework and trend per function), each
 * file's versions parsed incrementally (syn_defs with its SynParse).
 * Builds hand what they computed to the next (git's answers, records read,
 * functions found): caches on the Analysis, keyed by what each value was
 * computed from (docs/STRUCTURE.md, "Rebuilds and caching").
 */
#include "analysis.h"

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <limits.h>
#include <math.h>
#include <pthread.h>
#include <stdatomic.h>
#include <regex.h>
#include <spawn.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "cmdguess.h"
#include "syntax.h"
#include "util.h"

extern char **environ;

#ifndef TREE_MAX
#define TREE_MAX 5000 /* files in the tree */
#endif
#define FILE_MAX (8 << 20) /* bigger files are left out of the tree */
#define COMMENT_MAX 900    /* characters of a comment */
#define TOP_READS 8
#define WORKERS 8          /* threads finding functions, file by file */

enum { K_NOTE = EV_KIND_COUNT }; /* an event kept only for the comments before its call */

typedef struct {
    double ts, t0;
    unsigned long seq;
    int kind;
    int actor, src;    /* name table indices (src -1: none) */
    char *path, *text, *tool_id;
    long src_off, note_off;
    int notes, via, prompt, err, first, last, not_run;
    int tok[3];
    char *type, *session; /* TITLE of a subagent */
} AnEv;

/* ---- caches across builds ----------------------------------------------- */

/* What one build hands the next: values under keys that name everything they
 * were computed from, so an entry is never stale, only unused. Each entry
 * remembers the last build that used it; after a build the others go
 * (cache_sweep), so a cache holds about one build's working set. */
typedef struct {
    StrMap ix; /* key -> slot */
    struct Slot { void *val; unsigned used; } *e; /* val owned, freed with drop; used: the build that last used it */
    int n, cap;
    void (*drop)(void *);
} Cache;

/* key's slot, marked used by build; -1 when absent. */
static int cache_find(Cache *c, const char *key, unsigned build) {
    int k = strmap_get(&c->ix, key);
    if (k >= 0) c->e[k].used = build;
    return k;
}

/* v under key, used by build. One already there wins: v is dropped. */
static void *cache_put(Cache *c, const char *key, void *v, unsigned build) {
    int k = strmap_get(&c->ix, key);
    if (k >= 0) {
        c->drop(v);
        c->e[k].used = build;
        return c->e[k].val;
    }
    strmap_put(&c->ix, key, c->n);
    PUSH(c->e, c->n, c->cap, ((struct Slot){v, build}));
    return v;
}

/* Drops what build didn't use. */
static void cache_sweep(Cache *c, unsigned build) {
    int kept = 0;
    while (kept < c->n && c->e[kept].used == build) kept++;
    if (kept == c->n) return; /* all used */
    int *to = xmalloc((size_t)c->n * sizeof *to), n = 0;
    for (int k = 0; k < c->n; k++) {
        if (c->e[k].used != build) {
            c->drop(c->e[k].val);
            to[k] = -1;
            continue;
        }
        c->e[n] = c->e[k];
        to[k] = n++;
    }
    StrMap ix = {0};
    for (size_t i = 0; i < c->ix.cap; i++)
        if (c->ix.keys[i] && to[c->ix.vals[i]] >= 0) strmap_put(&ix, c->ix.keys[i], to[c->ix.vals[i]]);
    strmap_free(&c->ix);
    c->ix = ix;
    c->n = n;
    free(to);
}

static void cache_free(Cache *c) {
    for (int k = 0; k < c->n; k++) c->drop(c->e[k].val);
    strmap_free(&c->ix);
    free(c->e);
}

struct Analysis {
    char *root;
    pthread_mutex_t mu;
    AnEv *ev;
    size_t nev, cap;
    StrMap nameix; /* actor ids and transcript paths, interned */
    char **name;
    int nname, capname;
    regex_t search;
    bool no_defs; /* AIG_NO_DEFS: no functions (tests that mustn't depend on installed grammars) */
    atomic_int cancel; /* an_cancel: the running build gives up */
    /* Caches across builds. defs is shared by the build's worker threads, under
     * cmu; the others are used by the build's own thread only. */
    pthread_mutex_t cmu;
    unsigned builds; /* builds started: the running one marks what it uses */
    Cache defs;      /* language, text hash and length -> Defs */
    Cache shows;     /* commit and path -> Text: git show, when it succeeded */
    Cache logs;      /* HEAD commit and path -> GitLog: the file's commits */
    Cache changes;   /* transcript, offset, tool id and path -> Record, when complete */
    Cache notes;     /* transcript, offset, count and tool id -> Notes, when all were there */
    Cache cmds;      /* pattern, start and command -> cmd_runs' bits (not a pointer) */
    bool debug;      /* AIG_DEBUG: each build's phases on stderr */
    /* What the running build spent in git and in tree-sitter (summed over threads) */
    struct { int git, parsed; double git_s, parse_s; } clk;
};

static double mono(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (double)t.tv_sec + t.tv_nsec / 1e9;
}

/* ---- texts ------------------------------------------------------------- */

typedef struct {
    const char *p;
    uint32_t n;
} Line;

/* A text and its lines as Python's split("\n") gives them; the lines point
 * into the one buffer. Shared by reference. */
typedef struct {
    char *s;
    size_t len;
    Line *l;
    int n;
    int refs;
    uint64_t h; /* hash64 of s, 0: not yet (defs_of) */
} Text;

static Text *text_own(char *s, size_t len) { /* takes s */
    Text *t = xcalloc(1, sizeof *t);
    t->refs = 1;
    t->s = s;
    t->len = len;
    int n = 1;
    for (size_t i = 0; i < len; i++) n += s[i] == '\n';
    t->l = xmalloc((size_t)n * sizeof *t->l);
    size_t at = 0;
    for (size_t i = 0; i <= len; i++)
        if (i == len || s[i] == '\n') {
            t->l[t->n++] = (Line){s + at, (uint32_t)(i - at)};
            at = i + 1;
        }
    return t;
}

static Text *text_new(const char *s, size_t len) { return text_own(xstrndup(s, len), len); }
static Text *text_take(char *s) { return text_own(s, strlen(s)); }

static Text *text_ref(Text *t) {
    if (t) t->refs++;
    return t;
}

static void text_free(Text *t) { /* drops a reference */
    if (!t || --t->refs > 0) return;
    free(t->s);
    free(t->l);
    free(t);
}

static void text_drop(void *t) { text_free(t); }

/* Lines the way an editor counts them: a final newline ends the last line. */
static int nlines(const Text *t) { return t->len && t->s[t->len - 1] == '\n' ? t->n - 1 : t->n; }

static bool line_is(Line l, const char *z) { return strlen(z) == l.n && !memcmp(l.p, z, l.n); }

static uint64_t hash64(const char *s, size_t n) {
    uint64_t h = 1469598103934665603ull;
    for (size_t i = 0; i < n; i++) h = (h ^ (unsigned char)s[i]) * 1099511628211ull;
    return h;
}

/* ---- line diff ----------------------------------------------------------- */

/* Up to this many line pairs, lines line up exactly as difflib.SequenceMatcher
 * does (longest matching block first, recursively), so origins carry over as
 * build_data.py's do. Its cost grows with repeated lines, though: above it the
 * common prefix and suffix are matched first and the rest anchored on lines
 * unique to both sides (patience), with difflib again in small enough gaps. */
#define DIFFLIB_MAX 10000000LL

typedef struct {
    const int *ida, *idb;
    int nid;
    const int *start, *js; /* b2j: for each id, the lines of b holding it, ascending */
    int *len0, *len1;      /* j2len rows, valid where stamped with the row before */
    long *st0, *st1;
    long row;
    int (*queue)[4];
    int *cnta, *cntb, *posb; /* patience: occurrences per id in the current ranges */
    int *map;
} Align;

/* difflib's get_matching_blocks over a[alo, ahi) and b[blo, bhi). */
static void difflib_map(Align *x, int alo0, int ahi0, int blo0, int bhi0) {
    int nq = 0;
    x->queue[nq][0] = alo0, x->queue[nq][1] = ahi0, x->queue[nq][2] = blo0, x->queue[nq][3] = bhi0, nq++;
    while (nq) {
        nq--;
        int alo = x->queue[nq][0], ahi = x->queue[nq][1], blo = x->queue[nq][2], bhi = x->queue[nq][3];
        /* find_longest_match: the longest block, earliest in a, then in b */
        int bi = alo, bj = blo, bk = 0;
        long prev = ++x->row; /* stamp of the empty row before alo */
        int *pl = x->len0, *nl = x->len1;
        long *ps = x->st0, *ns = x->st1;
        for (int i = alo; i < ahi; i++) {
            long cur = ++x->row;
            int id = x->ida[i], lo = x->start[id], hi = x->start[id + 1];
            while (lo < hi) { /* the first of them at or after blo */
                int m = (lo + hi) / 2;
                if (x->js[m] < blo) lo = m + 1;
                else hi = m;
            }
            for (int q = lo; q < x->start[id + 1]; q++) {
                int j = x->js[q];
                if (j >= bhi) break;
                int k = (j > 0 && ps[j - 1] == prev ? pl[j - 1] : 0) + 1;
                nl[j] = k;
                ns[j] = cur;
                if (k > bk) bi = i - k + 1, bj = j - k + 1, bk = k;
            }
            prev = cur;
            int *t = pl; pl = nl; nl = t;
            long *u = ps; ps = ns; ns = u;
        }
        while (bi > alo && bj > blo && x->ida[bi - 1] == x->idb[bj - 1]) bi--, bj--, bk++;
        while (bi + bk < ahi && bj + bk < bhi && x->ida[bi + bk] == x->idb[bj + bk]) bk++;
        if (!bk) continue;
        for (int t = 0; t < bk; t++) x->map[bj + t] = bi + t;
        if (alo < bi && blo < bj) x->queue[nq][0] = alo, x->queue[nq][1] = bi, x->queue[nq][2] = blo, x->queue[nq][3] = bj, nq++;
        if (bi + bk < ahi && bj + bk < bhi)
            x->queue[nq][0] = bi + bk, x->queue[nq][1] = ahi, x->queue[nq][2] = bj + bk, x->queue[nq][3] = bhi, nq++;
    }
}

/* Big ranges: common prefix and suffix, then anchors on lines that occur once
 * on each side, longest increasing run of them (patience), gaps recursively.
 * A gap with no such line and too big for difflib stays unmatched. */
static void align(Align *x, int alo, int ahi, int blo, int bhi) {
    while (alo < ahi && blo < bhi && x->ida[alo] == x->idb[blo]) x->map[blo++] = alo++;
    while (alo < ahi && blo < bhi && x->ida[ahi - 1] == x->idb[bhi - 1]) x->map[--bhi] = --ahi;
    if (alo == ahi || blo == bhi) return;
    if ((long long)(ahi - alo) * (bhi - blo) <= DIFFLIB_MAX) {
        difflib_map(x, alo, ahi, blo, bhi);
        return;
    }
    for (int i = alo; i < ahi; i++) x->cnta[x->ida[i]]++;
    for (int j = blo; j < bhi; j++) x->cntb[x->idb[j]]++, x->posb[x->idb[j]] = j;
    int n = 0, *pi = xmalloc((size_t)(ahi - alo) * sizeof *pi), *pj = xmalloc((size_t)(ahi - alo) * sizeof *pj);
    for (int i = alo; i < ahi; i++)
        if (x->cnta[x->ida[i]] == 1 && x->cntb[x->ida[i]] == 1) pi[n] = i, pj[n++] = x->posb[x->ida[i]];
    for (int i = alo; i < ahi; i++) x->cnta[x->ida[i]] = 0;
    for (int j = blo; j < bhi; j++) x->cntb[x->idb[j]] = 0;
    /* longest increasing subsequence of pj (by i order): tails + back links */
    int *tail = xmalloc((size_t)(n + 1) * sizeof *tail), *back = xmalloc((size_t)(n + 1) * sizeof *back), nt = 0;
    for (int k = 0; k < n; k++) {
        int lo = 0, hi = nt;
        while (lo < hi) {
            int m = (lo + hi) / 2;
            if (pj[tail[m]] < pj[k]) lo = m + 1;
            else hi = m;
        }
        back[k] = lo ? tail[lo - 1] : -1;
        tail[lo] = k;
        if (lo == nt) nt++;
    }
    int *chain = xmalloc((size_t)(nt + 1) * sizeof *chain);
    for (int k = nt ? tail[nt - 1] : -1, c = nt; k >= 0; k = back[k]) chain[--c] = k;
    int a0 = alo, b0 = blo;
    for (int c = 0; c < nt; c++) {
        int i = pi[chain[c]], j = pj[chain[c]];
        align(x, a0, i, b0, j);
        x->map[j] = i;
        a0 = i + 1, b0 = j + 1;
    }
    if (nt) align(x, a0, ahi, b0, bhi);
    free(pi), free(pj), free(tail), free(back), free(chain);
}

/* For each line of b, the line of a it lines up with, -1 none. */
static int *line_map(const Line *a, int na, const Line *b, int nb) {
    int *map = xmalloc((size_t)(nb ? nb : 1) * sizeof *map);
    for (int j = 0; j < nb; j++) map[j] = -1;
    /* Lines interned to ids: equal lines, equal ids. */
    int cap = 64;
    while (cap < 2 * (na + nb)) cap *= 2;
    int *slot = xmalloc((size_t)cap * sizeof *slot), nid = 0;
    const Line **rep = xmalloc((size_t)(na + nb + 1) * sizeof *rep);
    for (int i = 0; i < cap; i++) slot[i] = -1;
    int *ida = xmalloc((size_t)(na + 1) * sizeof *ida), *idb = xmalloc((size_t)(nb + 1) * sizeof *idb);
    for (int k = 0; k < na + nb; k++) {
        const Line *s = k < na ? &a[k] : &b[k - na];
        size_t h = (size_t)hash64(s->p, s->n) & (size_t)(cap - 1);
        while (slot[h] >= 0 && (rep[slot[h]]->n != s->n || memcmp(rep[slot[h]]->p, s->p, s->n)))
            h = (h + 1) & (size_t)(cap - 1);
        if (slot[h] < 0) {
            rep[nid] = s;
            slot[h] = nid++;
        }
        if (k < na) ida[k] = slot[h];
        else idb[k - na] = slot[h];
    }
    int *start = xcalloc((size_t)nid + 2, sizeof *start), *js = xmalloc((size_t)(nb + 1) * sizeof *js);
    for (int j = 0; j < nb; j++) start[idb[j] + 1]++;
    for (int i = 0; i < nid; i++) start[i + 1] += start[i];
    int *fill = xmalloc((size_t)(nid + 1) * sizeof *fill);
    memcpy(fill, start, (size_t)nid * sizeof *fill);
    for (int j = 0; j < nb; j++) js[fill[idb[j]]++] = j;
    Align x = {.ida = ida, .idb = idb, .nid = nid, .start = start, .js = js, .map = map,
               .len0 = xcalloc((size_t)nb + 1, sizeof(int)), .len1 = xcalloc((size_t)nb + 1, sizeof(int)),
               .st0 = xcalloc((size_t)nb + 1, sizeof(long)), .st1 = xcalloc((size_t)nb + 1, sizeof(long)),
               .queue = xmalloc((size_t)(na + nb + 2) * sizeof *x.queue)};
    if ((long long)na * nb <= DIFFLIB_MAX) {
        difflib_map(&x, 0, na, 0, nb);
    } else {
        x.cnta = xcalloc((size_t)nid + 1, sizeof *x.cnta);
        x.cntb = xcalloc((size_t)nid + 1, sizeof *x.cntb);
        x.posb = xcalloc((size_t)nid + 1, sizeof *x.posb);
        align(&x, 0, na, 0, nb);
        free(x.cnta), free(x.cntb), free(x.posb);
    }
    free(x.queue), free(x.len0), free(x.len1), free(x.st0), free(x.st1);
    free(start), free(js), free(fill), free(ida), free(idb), free(slot), free(rep);
    return map;
}

static Line *lines_of(char *const *s, int n) {
    Line *l = xmalloc((size_t)(n + 1) * sizeof *l);
    for (int i = 0; i < n; i++) l[i] = (Line){s[i], (uint32_t)strlen(s[i])};
    return l;
}

int *an_line_map(char *const *a, int na, char *const *b, int nb) {
    Line *la = lines_of(a, na), *lb = lines_of(b, nb);
    int *m = line_map(la, na, lb, nb);
    free(la), free(lb);
    return m;
}

/* ---- patches ----------------------------------------------------------- */

static char *join_lines(const Line *l, int n) {
    size_t len = 0;
    for (int i = 0; i < n; i++) len += l[i].n + 1;
    char *s = xmalloc(len + 1), *o = s;
    for (int i = 0; i < n; i++) {
        memcpy(o, l[i].p, l[i].n);
        o += l[i].n;
        if (i + 1 < n) *o++ = '\n';
    }
    *o = 0;
    return s;
}

/* apply: hunks onto the lines of old. */
static char *apply_lines(const Line *ol, int on, const Hunk *h, int nh, bool check, const int *orig, int cid,
                         int **out_orig, int **rem, int *nrem) {
    Line *nw = NULL;
    int nn = 0, ncap = 0, *no = NULL, non = 0, nocap = 0, *rm = NULL, nrm = 0, rmcap = 0, at = 0;
    for (int k = 0; k < nh; k++) {
        int s = h[k].old_lines ? h[k].old_start - 1 : h[k].old_start;
        if (s < 0) s = 0;
        for (int i = at; i < s && i < on; i++) {
            PUSH(nw, nn, ncap, ol[i]);
            if (orig) PUSH(no, non, nocap, orig[i]);
        }
        at = s;
        for (int j = 0; j < h[k].n; j++) {
            const char *x = h[k].lines[j];
            if (*x == ' ' || *x == '-') {
                if (check && (at >= on || !line_is(ol[at], x + 1))) {
                    free(nw), free(no), free(rm);
                    return NULL;
                }
                if (*x == ' ') {
                    Line l = {x + 1, (uint32_t)strlen(x + 1)};
                    PUSH(nw, nn, ncap, l);
                    if (orig) PUSH(no, non, nocap, at < on ? orig[at] : -1);
                } else if (rem && orig && at < on) {
                    PUSH(rm, nrm, rmcap, at + 1);
                    PUSH(rm, nrm, rmcap, orig[at]);
                }
                at++;
            } else if (*x == '+') {
                Line l = {x + 1, (uint32_t)strlen(x + 1)};
                PUSH(nw, nn, ncap, l);
                if (orig) PUSH(no, non, nocap, cid);
            }
        }
    }
    for (int i = at; i < on; i++) {
        PUSH(nw, nn, ncap, ol[i]);
        if (orig) PUSH(no, non, nocap, orig[i]);
    }
    if (orig && !non) PUSH(no, non, nocap, -1); /* "" is one line */
    char *s = join_lines(nw, nn);
    free(nw);
    if (out_orig) *out_orig = no;
    else free(no);
    if (rem) *rem = rm, *nrem = nrm / 2;
    else free(rm);
    return s;
}

char *an_apply(const char *old, const Hunk *h, int nh, bool check, const int *orig, int cid, int **out_orig,
               int **removed, int *nremoved) {
    Text *t = text_new(old, strlen(old));
    char *s = apply_lines(t->l, t->n, h, nh, check, orig, cid, out_orig, removed, nremoved);
    text_free(t);
    return s;
}

static bool has_new_lines(const Line *tl, int tn, const Hunk *h, int nh) {
    for (int k = 0; k < nh; k++) {
        int at = h[k].new_start - 1;
        for (int j = 0; j < h[k].n; j++) {
            const char *x = h[k].lines[j];
            if (*x != ' ' && *x != '+') continue;
            if (at < 0 || at >= tn || !line_is(tl[at], x + 1)) return false;
            at++;
        }
    }
    return true;
}

bool an_has_new(const char *text, const Hunk *h, int nh) {
    Text *t = text_new(text, strlen(text));
    bool r = has_new_lines(t->l, t->n, h, nh);
    text_free(t);
    return r;
}

static bool blank(const char *s) {
    for (; *s; s++)
        if (!isspace((unsigned char)*s)) return false;
    return true;
}

/* relocate */
static bool relocate_lines(const Line *tl, int tn, const Hunk *h, int nh, int *starts) {
    for (int k = 0; k < nh; k++) {
        const char **blk = xmalloc((size_t)(h[k].n + 1) * sizeof *blk);
        int nb = 0;
        bool any = false;
        for (int j = 0; j < h[k].n; j++)
            if (*h[k].lines[j] == ' ' || *h[k].lines[j] == '+') {
                blk[nb++] = h[k].lines[j] + 1;
                any |= !blank(h[k].lines[j] + 1);
            }
        if (!nb || !any) {
            starts[k] = h[k].new_start;
            free(blk);
            continue;
        }
        int best = -1;
        for (int i = 0; i + nb <= tn; i++) {
            int q = 0;
            while (q < nb && line_is(tl[i + q], blk[q])) q++;
            if (q == nb && (best < 0 || abs(i + 1 - h[k].new_start) < abs(best + 1 - h[k].new_start))) best = i;
        }
        free(blk);
        if (best < 0) return false;
        starts[k] = best + 1;
    }
    return true;
}

bool an_relocate(const char *text, const Hunk *h, int nh, int *starts) {
    Text *t = text_new(text, strlen(text));
    bool r = relocate_lines(t->l, t->n, h, nh, starts);
    text_free(t);
    return r;
}

/* ---- git ----------------------------------------------------------------- */

#define GIT_TIMEOUT_MS 10000 /* a git that hangs (a lock, a lazy fetch) is killed */

/* git's stdout and exit status (*status), NULL when it couldn't run or was
 * killed. No shell: paths are plain argv, and literal (no pathspec magic). The
 * pipe is close-on-exec, so other threads' children never hold it open. */
static char *git(Analysis *an, const char *const *args, size_t *len, int *status) {
    double c0 = mono();
    int na = 0;
    while (args[na]) na++;
    const char **argv = xmalloc((size_t)(na + 8) * sizeof *argv);
    int n = 0;
    argv[n++] = "git", argv[n++] = "--literal-pathspecs", argv[n++] = "-c", argv[n++] = "core.fsmonitor=";
    argv[n++] = "-C", argv[n++] = an->root;
    for (int i = 0; i < na; i++) argv[n++] = args[i];
    argv[n] = NULL;
    int ne = 0;
    while (environ[ne]) ne++;
    char **env = xmalloc((size_t)(ne + 3) * sizeof *env);
    int k = 0;
    for (int i = 0; i < ne; i++)
        if (strncmp(environ[i], "GIT_TERMINAL_PROMPT=", 20) && strncmp(environ[i], "GIT_NO_LAZY_FETCH=", 18))
            env[k++] = environ[i];
    env[k++] = "GIT_TERMINAL_PROMPT=0";
    env[k++] = "GIT_NO_LAZY_FETCH=1";
    env[k] = NULL;
    int fd[2];
    char *out = NULL;
    if (pipe2(fd, O_CLOEXEC)) goto done;
    posix_spawn_file_actions_t fa;
    posix_spawn_file_actions_init(&fa);
    posix_spawn_file_actions_addopen(&fa, 0, "/dev/null", O_RDONLY, 0);
    posix_spawn_file_actions_adddup2(&fa, fd[1], 1);
    posix_spawn_file_actions_addopen(&fa, 2, "/dev/null", O_WRONLY, 0);
    pid_t pid;
    int rc = posix_spawnp(&pid, "git", &fa, NULL, (char *const *)argv, env);
    posix_spawn_file_actions_destroy(&fa);
    close(fd[1]);
    if (rc) {
        close(fd[0]);
        goto done;
    }
    size_t cap = 1 << 14, got = 0;
    out = xmalloc(cap);
    bool killed = false;
    double t0 = mono();
    for (;;) {
        long left = GIT_TIMEOUT_MS - (long)((mono() - t0) * 1000);
        struct pollfd p = {fd[0], POLLIN, 0};
        if (left <= 0 || (poll(&p, 1, (int)left) < 0 && errno != EINTR)) {
            kill(pid, SIGKILL);
            killed = true;
            break;
        }
        if (!p.revents) continue;
        ssize_t r = read(fd[0], out + got, cap - got - 1);
        if (r < 0 && errno == EINTR) continue;
        if (r <= 0) break;
        got += (size_t)r;
        if (cap - got < 2) out = xrealloc(out, cap *= 2);
    }
    close(fd[0]);
    int st = 0;
    while (waitpid(pid, &st, 0) < 0 && errno == EINTR) {}
    out[got] = 0;
    if (killed || !WIFEXITED(st)) {
        free(out);
        out = NULL;
    } else {
        if (len) *len = got;
        if (status) *status = WEXITSTATUS(st);
    }
done:
    free(argv);
    free(env);
    an->clk.git++;
    an->clk.git_s += mono() - c0;
    return out;
}

/* git's stdout when it exits 0, else NULL. */
static char *git_ok(Analysis *an, const char *const *args, size_t *len) {
    int st = -1;
    char *out = git(an, args, len, &st);
    if (out && st) {
        free(out);
        out = NULL;
    }
    return out;
}

/* A file's commits, newest first, as `git log --format="%H %ct"` lists them. */
typedef struct {
    struct Commit { char *id; long long ct; } *c;
    int n;
} GitLog;

static const GitLog no_log; /* no repo, no HEAD, or git failed */

static void log_drop(void *p) {
    GitLog *l = p;
    if (!l) return;
    for (int i = 0; i < l->n; i++) free(l->c[i].id);
    free(l->c), free(l);
}

/* ---- the accumulator ----------------------------------------------------- */

/* s's index among names (ix maps them to it), a copy added when new. */
static int name_ix(StrMap *ix, char ***names, int *n, int *cap, const char *s) {
    int k = strmap_get(ix, s);
    if (k >= 0) return k;
    PUSH(*names, *n, *cap, xstrdup(s));
    strmap_put(ix, s, *n - 1);
    return *n - 1;
}

static int intern(Analysis *a, const char *s) { return name_ix(&a->nameix, &a->name, &a->nname, &a->capname, s); }

static void defs_drop(void *d);
/* A change's record, its texts shared by reference: c->original and c->created
 * point into them. */
typedef struct {
    Change *c;
    Text *original, *created;
} Record;

static void record_drop(void *p) {
    Record *r = p;
    if (!r) return;
    r->c->original = r->c->created = NULL; /* the texts' */
    change_free(r->c);
    text_free(r->original);
    text_free(r->created);
    free(r);
}
static void no_drop(void *v) { (void)v; }
static void notes_drop(void *p) {
    if (p) notes_free(p), free(p);
}

Analysis *an_new(const char *root) {
    Analysis *a = xcalloc(1, sizeof *a);
    a->root = xstrdup(root);
    size_t n = strlen(a->root);
    while (n > 1 && a->root[n - 1] == '/') a->root[--n] = 0;
    pthread_mutex_init(&a->mu, NULL);
    pthread_mutex_init(&a->cmu, NULL);
    a->defs.drop = defs_drop;
    a->shows.drop = text_drop;
    a->logs.drop = log_drop;
    a->changes.drop = record_drop;
    a->notes.drop = notes_drop;
    a->cmds.drop = no_drop;
    a->no_defs = getenv("AIG_NO_DEFS") != NULL;
    a->debug = getenv("AIG_DEBUG") != NULL;
    regcomp(&a->search, "^(grep|rg|ag|fd|find|git\\s+grep)(\\s|$)", REG_EXTENDED | REG_NOSUB);
    return a;
}

/* ---- the rules the stage shares (an_fix_prompt ...) ---------------------- */

static regex_t re_verify, re_fix, re_corr;
static pthread_once_t re_once = PTHREAD_ONCE_INIT;

/* METHODS.md's patterns; glibc's ERE has \b \s \S \w. Compiled once, kept
 * for the process (regexec on them is thread-safe). */
static void re_init(void) {
    regcomp(&re_verify, CMD_VERIFY_ERE, REG_EXTENDED | REG_NOSUB);
    regcomp(&re_fix,
            "\\b(bugs?|broken|doesn'?t work|does not work|not working|didn'?t work|crash\\w*|regress\\w*|"
            "fail(s|ed|ing)|errors?|wrong|can'?t|cannot|still|isn'?t|not (showing|working|catching)|horrible)\\b",
            REG_EXTENDED | REG_NOSUB | REG_ICASE);
    regcomp(&re_corr,
            "\\b(bugs?|broken|wrong|doesn'?t work|does not work|not working|didn'?t work|revert|undo|"
            "regress\\w*|crash\\w*|fail(s|ed|ing)?|errors?|strange|weird|horrible|not what i|why did you)\\b|^no\\b",
            REG_EXTENDED | REG_NOSUB | REG_ICASE);
}

bool an_fix_prompt(const char *title) {
    pthread_once(&re_once, re_init);
    return title && !regexec(&re_fix, title, 0, NULL, 0);
}

bool an_corr_prompt(const char *next) {
    pthread_once(&re_once, re_init);
    return next && !regexec(&re_corr, next, 0, NULL, 0);
}

bool an_verify_cmd(const char *cmd, const char *root, const char *start) {
    pthread_once(&re_once, re_init);
    return cmd && (cmd_runs(&re_verify, cmd, root, start) & CMD_RUN);
}

static bool starts(const char *s, const char *p) { return s && !strncmp(s, p, strlen(p)); }

void an_add(Analysis *a, const Event *evs, size_t n) {
    pthread_mutex_lock(&a->mu);
    for (size_t i = 0; i < n; i++) {
        const Event *e = &evs[i];
        int kind = (int)e->kind;
        bool keep;
        switch (e->kind) {
        case EV_CREATE:
        case EV_EDIT:
        case EV_DELETE: keep = e->via == VIA_TRANSCRIPT && e->tool_id && e->src && e->path; break;
        case EV_TOOL: /* searches outside the repo, failed ones */
            keep = starts(e->text, "Grep") || starts(e->text, "Glob");
            break;
        case EV_TURN: case EV_TURN_COPY: case EV_READ: case EV_SEARCH: case EV_USAGE: case EV_COMPACT:
        case EV_INTERRUPT: case EV_TITLE: case EV_BASH_END: keep = true; break;
        default: keep = false;
        }
        if (!keep && e->tool_id && e->src && e->notes > 0) keep = true, kind = K_NOTE;
        if (!keep || !e->actor) continue;
        AnEv v = {.ts = e->ts, .t0 = e->t0, .seq = e->seq, .kind = kind, .actor = intern(a, e->actor),
                  .src = e->src ? intern(a, e->src) : -1, .src_off = e->src_off, .note_off = e->note_off,
                  .notes = e->notes, .via = e->via};
        if (kind != K_NOTE) {
            v.path = dup_or_null(e->path);
            v.text = dup_or_null(e->text);
        }
        v.tool_id = dup_or_null(e->tool_id);
        if (kind == EV_READ) v.first = e->range.first, v.last = e->range.last;
        if (kind == EV_USAGE) v.tok[0] = e->tok.out, v.tok[1] = e->tok.in, v.tok[2] = e->tok.cached;
        if (kind == EV_TURN || kind == EV_TURN_COPY) v.prompt = e->prompt;
        if (kind == EV_BASH_END) v.err = e->err || e->failed_out, v.not_run = e->not_run;
        if (kind == EV_TITLE && e->who.type) {
            v.type = xstrdup(e->who.type);
            v.session = xstrdup(e->who.session ? e->who.session : "");
        }
        PUSH(a->ev, a->nev, a->cap, v);
    }
    pthread_mutex_unlock(&a->mu);
}

void an_free(Analysis *a) {
    if (!a) return;
    for (size_t i = 0; i < a->nev; i++) {
        AnEv *v = &a->ev[i];
        free(v->path), free(v->text), free(v->tool_id), free(v->type), free(v->session);
    }
    free(a->ev);
    strv_free(a->name, a->nname);
    strmap_free(&a->nameix);
    regfree(&a->search);
    pthread_mutex_destroy(&a->mu);
    cache_free(&a->defs);
    cache_free(&a->shows);
    cache_free(&a->logs);
    cache_free(&a->changes);
    cache_free(&a->notes);
    cache_free(&a->cmds);
    pthread_mutex_destroy(&a->cmu);
    free(a->root);
    free(a);
}

/* ---- the build ----------------------------------------------------------- */

typedef struct {
    double ts;
    char *title;
    int key;
} Prompt;

typedef struct { /* what an agent gathered toward its next change */
    double ts;
    int key;   /* its prompt (turn key) */
    int path;  /* read: shown path id; search: -1 */
} Item;

typedef struct {
    double ts;
    int key;
    int tok[3];
} Spend;

typedef struct {
    Item *items;
    int nitems, capitems;
    Spend *spent;
    int nspent, capspent;
    int last_turn; /* of its last change, -1 none */
    double last_ts;
    int gen;       /* compactions so far: what it has seen counts from the last */
} Ctx;

typedef struct { /* per node of a file: the file ("") or a def key */
    char *key;
    AnCh *ch;
    int nch, capch;
    AnRw *rw;
    int nrw, caprw;
    StrMap rwix;
    AnTr *tr;
    int ntr, captr;
} Acc;

typedef struct {
    SynDef *d;
    int n;
    char **key; /* "Outer.inner" */
} Defs;

typedef struct {
    int ev;      /* change event */
    int sub;     /* 0 its text before (originalFile, or the created file), 1 after; 2 today */
    Text *t;     /* loaded on first use */
    bool loaded;
} Known;

/* What the replay leaves for the second pass, which needs the file's
 * functions (found per file, in parallel): a read, or a change. */
typedef struct {
    bool change;
    int actor, gen;    /* whose context, since its last compaction */
    Text *t;           /* read: what it read; change: the text after (or the later one it lines up with) */
    int a, z;          /* read: its lines */
    int cid;
    Text *old;         /* change: the text before, when known */
    const Change *chg; /* its record (hunks); borrowed from the cache */
    Hunk *moved;       /* its hunks moved to the later text */
    bool create, placed;
    int *rem, nrem;    /* lines it removed: (old line, origin) */
} Work;

typedef struct {
    char *rel;
    bool in_tree, no_defs;
    long long size; /* today's, on disk */
    double mtime;
    Work *work;
    int nwork, capwork;
    int blind;  /* changes that changed a def unread */
    Text *today;
    Text *text; /* tracked: NULL = never seen */
    int *orig;
    bool unplaced;
    int fill;   /* the change that couldn't be placed */
    Known *known;
    int nknown, capknown;
    Acc *acc;
    int nacc, capacc;
    StrMap accix;
    StrMap seen; /* "actor\x1fkey" -> that agent's compaction gen */
    SynParse *parse;
    const GitLog *log; /* NULL until needed; borrowed */
    StrMap shownix;
    Text **shown;
    int nshown, capshown;
} File;

typedef struct { /* per name: an actor id, a transcript path or a session id */
    int sess;      /* its session's name index */
    bool sub;
    char *title;   /* session: its last AI title */
    char *type, *task;
    Prompt *pr;    /* session: its prompts, sorted by (ts, title) */
    int npr, cappr;
    /* comments: an actor's calls with text before them, by position */
    struct NoteCall { long off, note_off; int notes; double start; const char *id; } *calls;
    int ncalls, capcalls;
    double *resets; /* an actor's prompts and compactions, by time */
    int nresets, capresets;
    Ctx ctx;
    int out;       /* snapshot actor index, -1 */
} Name;

typedef struct {
    Analysis *an;
    const char *root;
    AnEv *ev;
    int nev;
    char **name; /* actor ids, transcript paths, and session ids added here */
    int nname, capname;
    StrMap nameix;
    Name *nm;    /* per name */
    StrMap keyix; /* prompt "(ts, title)" -> key */
    int nkeys;
    int *turn_of_key;
    int capkeys;
    File *files;
    int nfiles, capfiles;
    StrMap fileix;
    StrMap pathix; /* shown paths read */
    StrMap spathix; /* ...and those in the snapshot */
    char **pathname;
    int npathname, cappathname;
    StrMap bashids; /* tool ids of Bash calls */
    StrMap noteix;  /* tool id -> comment (cached result) */
    int *note_val;
    int nnote_val, capnote_val;
    AnSnapshot *s;
    int capturns, capchanges, capcomments, cappaths;
    StrMap commentix;
    int capactors;
    int *chs;       /* per change: its session, */
    double *cht;    /* and time */
    int capcht, capchs;
    bool *drop;     /* per event: a usage superseded by a later copy */
    char **tree;    /* today's files, sorted */
    int ntree;
    char *head;     /* HEAD's commit, NULL: none (or not a repo) */
    int owned_from; /* names past this are ours to free */
} B;

static int b_name(B *b, const char *s) {
    int k = strmap_get(&b->nameix, s);
    if (k >= 0) return k;
    PUSH(b->name, b->nname, b->capname, (char *)s); /* borrowed from the Analysis, or owned (added) */
    strmap_put(&b->nameix, s, b->nname - 1);
    return b->nname - 1;
}

static long long micros(double ts) { return llround(ts * 1e6); }
/* The epoch as Python's datetime.timestamp() gives it (exact microseconds / 1e6). */
static double epoch(double ts) { return (double)micros(ts) / 1e6; }

static int cmp_double(const void *pa, const void *pb) {
    double a = *(const double *)pa, b = *(const double *)pb;
    return a < b ? -1 : a > b;
}

long an_active(double *xs, int n) {
    for (int i = 0; i < n; i++) xs[i] = epoch(xs[i]);
    qsort(xs, (size_t)n, sizeof *xs, cmp_double);
    double s = 0;
    for (int i = 1; i < n; i++) s += xs[i] - xs[i - 1] < AN_IDLE_CAP ? xs[i] - xs[i - 1] : AN_IDLE_CAP;
    return (long)nearbyint(s);
}

static int cmp_ev(const void *pa, const void *pb) {
    const AnEv *a = pa, *b = pb;
    if (a->ts != b->ts) return a->ts < b->ts ? -1 : 1;
    int ua = a->kind == EV_USAGE, ub = b->kind == EV_USAGE; /* usage after the rest, as build_data pushes it */
    if (ua != ub) return ua - ub;
    return a->seq < b->seq ? -1 : a->seq > b->seq;
}

static int cmp_prompt(const void *pa, const void *pb) {
    const Prompt *a = pa, *b = pb;
    if (a->ts != b->ts) return a->ts < b->ts ? -1 : 1;
    return strcmp(a->title, b->title);
}

/* The prompt (turn key) running in session at ts: the latest at or before it. */
static int prompt_at(const B *b, int session, double ts, int *idx) {
    const Prompt *p = b->nm[session].pr;
    int lo = 0, hi = b->nm[session].npr;
    while (lo < hi) {
        int m = (lo + hi) / 2;
        if (p[m].ts <= ts) lo = m + 1;
        else hi = m;
    }
    if (idx) *idx = lo - 1;
    return lo ? p[lo - 1].key : -1;
}

/* turn_of: the turn of session at ts; made (with make) on the first change in it. */
static int turn_of(B *b, int session, double ts, bool make) {
    int i, key = prompt_at(b, session, ts, &i);
    if (key < 0) return -1;
    if (b->turn_of_key[key] >= 0 || !make) return b->turn_of_key[key];
    AnSnapshot *s = b->s;
    const Prompt *p = &b->nm[session].pr[i];
    const char *nxt = i + 1 < b->nm[session].npr ? b->nm[session].pr[i + 1].title : NULL;
    AnTurn t = {.title = xstrdup(p->title), .ts = p->ts, .actor = session,
                .corr = an_corr_prompt(nxt) ? xstrdup(nxt) : NULL, .fix = an_fix_prompt(p->title)};
    PUSH(s->turns, s->nturns, b->capturns, t);
    return b->turn_of_key[key] = s->nturns - 1;
}

static int actor_out(B *b, int name) {
    Name *n = &b->nm[name];
    if (n->out >= 0) return n->out;
    AnSnapshot *s = b->s;
    AnActor a = {.sub = n->sub, .id = xstrndup(b->name[name], strnlen(b->name[name], 8))};
    if (a.sub) {
        a.name = xstrdup(n->type && *n->type ? n->type : "subagent");
        a.task = xstrdup(n->task ? n->task : "");
    } else if (n->title && *n->title) {
        a.name = xstrdup(n->title);
    } else {
        a.name = xasprintf("session %s", a.id);
    }
    PUSH(s->actors, s->nactors, b->capactors, a);
    return n->out = s->nactors - 1;
}

/* ---- comments ----------------------------------------------------------- */

static void strip(char **s) {
    char *p = *s;
    while (*p && (is_space(*p) || (*p >= 0x1c && *p <= 0x1f))) p++;
    size_t n = strlen(p);
    while (n && (is_space(p[n - 1]) || (p[n - 1] >= 0x1c && p[n - 1] <= 0x1f))) n--;
    memmove(*s, p, n);
    (*s)[n] = 0;
}

/* At most COMMENT_MAX characters: cut at the last space before, then " …". */
static void cut_comment(char **s) {
    size_t i = 0;
    int n = 0;
    for (; (*s)[i] && n < COMMENT_MAX; n++)
        do i++; while (((unsigned char)(*s)[i] & 0xC0) == 0x80);
    if (!(*s)[i]) return;
    (*s)[i] = 0;
    char *sp = strrchr(*s, ' ');
    if (sp) *sp = 0;
    size_t k = strlen(*s);
    *s = xrealloc(*s, k + 5);
    memcpy(*s + k, " \xE2\x80\xA6", 5);
}

static int cmp_call(const void *pa, const void *pb) {
    const struct NoteCall *a = pa, *b = pb;
    return a->off < b->off ? -1 : a->off > b->off;
}

/* tr_notes, cached: the text blocks before a call never change once all n of
 * them are written. A copy (comment_of strips them in place). */
static Notes notes_of(B *b, const char *src, long off, int n, const char *id) {
    Analysis *an = b->an;
    char *key = xasprintf("%s\x1f%ld\x1f%d\x1f%s", src, off, n, id);
    int k = cache_find(&an->notes, key, an->builds);
    const Notes *c = k >= 0 ? an->notes.e[k].val : NULL;
    Notes ns = c ? (Notes){xmalloc((size_t)c->n * sizeof *ns.s), c->n} : tr_notes(src, off, n, id);
    for (int i = 0; c && i < c->n; i++) ns.s[i] = xstrdup(c->s[i]);
    if (!c && ns.n == n) {
        Notes *keep = xmalloc(sizeof *keep);
        *keep = (Notes){xmalloc((size_t)n * sizeof *keep->s), n};
        for (int i = 0; i < n; i++) keep->s[i] = xstrdup(ns.s[i]);
        cache_put(&an->notes, key, keep, an->builds);
    }
    free(key);
    return ns;
}

/* comment_of: the change's comment index, or -1 - index for the turn's earlier text. */
static int comment_of(B *b, const AnEv *e) {
    int k = strmap_get(&b->noteix, e->tool_id);
    if (k >= 0) return b->note_val[k];
    const char *src = b->name[e->src];
    char *text = NULL;
    Notes ns = notes_of(b, src, e->note_off, e->notes, e->tool_id);
    size_t len = 0;
    for (int i = 0; i < ns.n; i++) len += strlen(ns.s[i]) + 2;
    text = xcalloc(len + 1, 1);
    for (int i = 0; i < ns.n; i++) {
        strip(&ns.s[i]);
        if (i) strcat(text, "\n\n");
        strcat(text, ns.s[i]);
    }
    notes_free(&ns);
    strip(&text);
    bool own = *text;
    if (!own) { /* said: the latest text before the call since the prompt (or compaction) */
        const Name *a = &b->nm[e->actor];
        int lo = 0, hi = a->ncalls;
        while (lo < hi) {
            int m = (lo + hi) / 2;
            if (a->calls[m].off < e->src_off) lo = m + 1;
            else hi = m;
        }
        double xs = e->t0 > 0 ? e->t0 : e->ts;
        if (lo) {
            const struct NoteCall *y = &a->calls[lo - 1];
            bool reset = false;
            for (int r = 0; r < a->nresets && !reset; r++) reset = a->resets[r] > y->start && a->resets[r] <= xs;
            if (!reset) {
                Notes ys = notes_of(b, src, y->note_off, y->notes, y->id);
                if (ys.n) {
                    free(text);
                    text = xstrdup(ys.s[ys.n - 1]);
                    strip(&text);
                }
                notes_free(&ys);
            }
        }
    }
    cut_comment(&text);
    AnSnapshot *s = b->s;
    int ix = strmap_get(&b->commentix, text);
    if (ix < 0) {
        strmap_put(&b->commentix, text, s->ncomments);
        PUSH(s->comments, s->ncomments, b->capcomments, text);
        ix = s->ncomments - 1;
    } else {
        free(text);
    }
    int r = own || !*s->comments[ix] ? ix : -1 - ix;
    strmap_put(&b->noteix, e->tool_id, b->nnote_val);
    PUSH(b->note_val, b->nnote_val, b->capnote_val, r);
    return r;
}

/* ---- files ----------------------------------------------------------- */

static File *file_get(B *b, const char *rel) {
    int k = strmap_get(&b->fileix, rel);
    return k >= 0 ? &b->files[k] : NULL;
}

static File *file_add(B *b, const char *rel) {
    File *f = file_get(b, rel);
    if (f) return f;
    File nf = {.rel = xstrdup(rel), .fill = -1, .no_defs = b->an->no_defs};
    PUSH(b->files, b->nfiles, b->capfiles, nf);
    strmap_put(&b->fileix, rel, b->nfiles - 1);
    return &b->files[b->nfiles - 1];
}

static Text *read_text(const char *root, const char *rel, bool skip_binary, struct stat *out) {
    char *p = xasprintf("%s/%s", root, rel);
    struct stat st;
    FILE *fp = !stat(p, &st) && S_ISREG(st.st_mode) && st.st_size <= FILE_MAX ? fopen(p, "rb") : NULL;
    free(p);
    if (!fp) return NULL;
    char *s = xmalloc((size_t)st.st_size + 1);
    size_t n = fread(s, 1, (size_t)st.st_size, fp);
    fclose(fp);
    s[n] = 0;
    if (skip_binary && memchr(s, 0, n < 8000 ? n : 8000)) {
        free(s);
        return NULL;
    }
    if (out) *out = st;
    return text_own(s, n);
}

static void defs_drop(void *p) {
    Defs *d = p;
    if (!d) return;
    strv_free(d->key, d->n);
    syn_defs_free(d->d, d->n);
    free(d);
}

/* defs: the file's functions and classes in text. Cached by language and
 * content, across builds: the same text in the same language has the same defs. */
static const Defs *defs_of(Analysis *an, File *f, const Text *t) {
    char lang[SYN_NAME_MAX], key[SYN_NAME_MAX + 48];
    syn_lang_name(f->rel, lang);
    if (!t->h) ((Text *)t)->h = hash64(t->s, t->len) | 1; /* a file's texts are its worker's alone */
    snprintf(key, sizeof key, "%s\x1f%016llx\x1f%zu", lang, (unsigned long long)t->h, t->len);
    pthread_mutex_lock(&an->cmu);
    int k = cache_find(&an->defs, key, an->builds);
    const Defs *hit = k >= 0 ? an->defs.e[k].val : NULL;
    pthread_mutex_unlock(&an->cmu);
    if (hit) return hit;
    Defs *d = xcalloc(1, sizeof *d);
    if (!f->parse) f->parse = syn_parse_new();
    double c0 = mono();
    d->n = f->no_defs ? 0 : syn_defs(f->rel, t->s, (uint32_t)t->len, &d->d, f->parse);
    double c1 = mono();
    d->key = xcalloc((size_t)d->n, sizeof *d->key);
    for (int i = 0; i < d->n; i++) {
        int p = d->d[i].parent;
        d->key[i] = p < 0 ? xstrdup(d->d[i].name) : xasprintf("%s.%s", d->key[p], d->d[i].name);
    }
    pthread_mutex_lock(&an->cmu);
    an->clk.parsed++;
    an->clk.parse_s += c1 - c0;
    const Defs *r = cache_put(&an->defs, key, d, an->builds); /* another thread's, if it was first */
    pthread_mutex_unlock(&an->cmu);
    return r;
}

static Acc *acc_of(File *f, const char *key) {
    int k = strmap_get(&f->accix, key);
    if (k >= 0) return &f->acc[k];
    Acc a = {.key = xstrdup(key)};
    PUSH(f->acc, f->nacc, f->capacc, a);
    strmap_put(&f->accix, key, f->nacc - 1);
    return &f->acc[f->nacc - 1];
}

/* The file's commits as of HEAD (b->head, read once per build): `git log
 * HEAD -- file` is fixed for a HEAD commit, so it's cached by the two. */
static const GitLog *git_log(B *b, const char *rel) {
    if (!b->head) return &no_log;
    Analysis *an = b->an;
    char *key = xasprintf("%s\x1f%s", b->head, rel);
    int k = cache_find(&an->logs, key, an->builds);
    if (k >= 0) {
        free(key);
        return an->logs.e[k].val;
    }
    const char *args[] = {"log", "--format=%H %ct", b->head, "--", rel, NULL};
    char *out = git_ok(an, args, NULL);
    if (!out) { /* not cached: it may work next time */
        free(key);
        return &no_log;
    }
    GitLog *l = xcalloc(1, sizeof *l);
    int cap = 0;
    for (char *line = out, *nl; *line; line = nl + 1) {
        nl = strchr(line, '\n');
        if (!nl) nl = line + strlen(line) - 1;
        char *sp = memchr(line, ' ', (size_t)(nl - line));
        if (sp) PUSH(l->c, l->n, cap, ((struct Commit){xstrndup(line, (size_t)(sp - line)), atoll(sp + 1)}));
        if (!nl[1]) break;
    }
    free(out);
    cache_put(&an->logs, key, l, an->builds);
    free(key);
    return l;
}

/* The file at a commit (a reference), NULL when git can't show it. What a
 * commit holds never changes: cached by commit and path. */
static Text *git_show(B *b, const char *commit, const char *rel) {
    Analysis *an = b->an;
    char *key = xasprintf("%s:./%s", commit, rel);
    int k = cache_find(&an->shows, key, an->builds);
    if (k >= 0) {
        free(key);
        return text_ref(an->shows.e[k].val);
    }
    const char *args[] = {"show", key, NULL};
    size_t len = 0;
    char *out = git_ok(an, args, &len);
    Text *t = out ? text_own(out, len) : NULL;
    if (t) cache_put(&an->shows, key, text_ref(t), an->builds); /* a failure isn't: it may work next time */
    free(key);
    return t;
}

/* git_text: the file as of the last commit at or before ts. */
static const Text *git_text(B *b, File *f, double ts) {
    if (!f->log) f->log = git_log(b, f->rel);
    const GitLog *l = f->log;
    long long sec = (long long)floor(epoch(ts));
    int i = 0;
    while (i < l->n && l->c[i].ct > sec) i++;
    if (i == l->n) return NULL;
    int k = strmap_get(&f->shownix, l->c[i].id);
    if (k < 0) {
        PUSH(f->shown, f->nshown, f->capshown, git_show(b, l->c[i].id, f->rel));
        strmap_put(&f->shownix, l->c[i].id, k = f->nshown - 1);
    }
    return f->shown[k];
}

/* resync: tracked text -> t; lines that line up keep their origins, the
 * others get the change that couldn't be placed (or unknown). */
static void resync(File *f, const Text *t) {
    int fill = f->unplaced ? f->fill : -1;
    f->unplaced = false;
    if (!f->text) {
        f->text = text_ref((Text *)t);
        f->orig = xmalloc((size_t)t->n * sizeof *f->orig);
        for (int i = 0; i < t->n; i++) f->orig[i] = -1;
        return;
    }
    if (f->text->len == t->len && !memcmp(f->text->s, t->s, t->len)) return;
    int *map = line_map(f->text->l, f->text->n, t->l, t->n);
    int *o = xmalloc((size_t)t->n * sizeof *o);
    for (int j = 0; j < t->n; j++) o[j] = map[j] >= 0 ? f->orig[map[j]] : fill;
    free(map);
    free(f->orig);
    text_free(f->text);
    f->text = text_ref((Text *)t);
    f->orig = o;
}

static int cmp_known(const void *pa, const void *pb, void *ud) {
    const Known *a = pa, *b = pb;
    const AnEv *ev = ud;
    double ta = a->sub == 2 ? INFINITY : ev[a->ev].ts, tb = b->sub == 2 ? INFINITY : ev[b->ev].ts;
    if (ta != tb) return ta < tb ? -1 : 1;
    if (a->sub != b->sub) return a->sub - b->sub;
    return a->ev - b->ev;
}

/* change_of: the change's record (tr_change), NULL when it has no result yet
 * or can't be read. Transcripts are only appended to, so a record with its
 * result never changes: cached by where it is and the file it picks. Its
 * texts are that file's alone, like the rest of its texts (worker threads). */
static const Record *change_of(B *b, const AnEv *e) {
    Analysis *an = b->an;
    const char *src = b->name[e->src];
    char *key = xasprintf("%s\x1f%ld\x1f%s\x1f%s", src, e->src_off, e->tool_id, e->path);
    int k = cache_find(&an->changes, key, an->builds);
    Record *r = k >= 0 ? an->changes.e[k].val : NULL;
    if (k < 0) {
        Change *c = tr_change(src, e->src_off, e->tool_id, b->root, e->path);
        if (c->state == 0) {
            r = xmalloc(sizeof *r);
            *r = (Record){c, c->original ? text_take(c->original) : NULL, c->created ? text_take(c->created) : NULL};
            cache_put(&an->changes, key, r, an->builds);
        } else {
            change_free(c); /* not cached: it may be complete next time */
        }
    }
    free(key);
    return r;
}

/* later_text: the first known text of the file after ts holding every hunk's
 * new side; the hunks moved there (into *moved, caller frees). */
static const Text *later_text(B *b, File *f, double ts, const Hunk *h, int nh, Hunk **moved) {
    int *st = xmalloc((size_t)nh * sizeof *st);
    for (int i = 0; i < f->nknown; i++) {
        Known *k = &f->known[i];
        if (k->sub != 2 && !(b->ev[k->ev].ts > ts || (b->ev[k->ev].ts == ts && k->sub == 1))) continue;
        if (!k->loaded) {
            k->loaded = true;
            const Record *r = change_of(b, &b->ev[k->ev]);
            const Change *c = r ? r->c : NULL;
            if (c && !c->cmd) {
                if (c->created && k->sub == 0) k->t = text_ref(r->created);
                else if (!c->created && c->original && k->sub == 0) k->t = text_ref(r->original);
                else if (!c->created && c->original && k->sub == 1)
                    k->t = text_take(an_apply(c->original, c->hunks, c->nhunks, false, NULL, 0, NULL, NULL, NULL));
            }
        }
        if (!k->t || !nh || !relocate_lines(k->t->l, k->t->n, h, nh, st)) continue;
        *moved = xmalloc((size_t)nh * sizeof **moved);
        for (int j = 0; j < nh; j++) {
            (*moved)[j] = h[j];
            (*moved)[j].new_start = (*moved)[j].old_start = st[j];
        }
        free(st);
        return k->t;
    }
    free(st);
    return NULL;
}

typedef struct {
    char *key;
    int add, del;
} Touch;

static Touch *touch(Touch **t, int *n, int *cap, const char *key) {
    for (int i = 0; i < *n; i++)
        if (!strcmp((*t)[i].key, key)) return &(*t)[i];
    Touch x = {xstrdup(key), 0, 0};
    PUSH(*t, *n, *cap, x);
    return &(*t)[*n - 1];
}

static void seen_key(char *buf, size_t n, int actor, const char *key) { snprintf(buf, n, "%d\x1f%s", actor, key); }

static int path_ix(B *b, const char *p) { return name_ix(&b->pathix, &b->pathname, &b->npathname, &b->cappathname, p); }

/* A read's shown path, for one outside the repo: ~/... or absolute. */
static char *shown_path(const char *p) {
    const char *home = getenv("HOME");
    size_t hn = home ? strlen(home) : 0;
    return hn && !strncmp(p, home, hn) && p[hn] == '/' ? xasprintf("~%s", p + hn) : xstrdup(p);
}

static void gather(Ctx *c, double ts, int key, int path) { PUSH(c->items, c->nitems, c->capitems, ((Item){ts, key, path})); }

/* A read: what's read in this agent's context (for "changed unread"); its
 * functions are found in the second pass. */
static void replay_read(B *b, const AnEv *e, File *f, int key) {
    Ctx *c = &b->nm[e->actor].ctx;
    char *shown = *e->path == '/' ? shown_path(e->path) : NULL; /* outside the repo */
    gather(c, e->ts, key, path_ix(b, shown ? shown : e->path));
    free(shown);
    if (!f) return;
    Text *t = f->text && !f->unplaced ? f->text : (Text *)git_text(b, f, e->ts);
    if (!t) return;
    Work w = {.actor = e->actor, .gen = c->gen, .t = text_ref(t), .a = e->first > 0 ? e->first : 1,
              .z = e->first > 0 && e->last > 0 ? e->last : INT_MAX};
    if (e->last < 0) w.a = nlines(t) + e->last + 1 > 1 ? nlines(t) + e->last + 1 : 1; /* tail: the end of this text */
    PUSH(f->work, f->nwork, f->capwork, w);
}

static void work_read(Analysis *an, File *f, const Work *w) {
    const Defs *d = defs_of(an, f, w->t);
    char buf[1200];
    for (int i = 0; i < d->n; i++)
        if (d->d[i].a <= w->z && d->d[i].b >= w->a) {
            seen_key(buf, sizeof buf, w->actor, d->key[i]);
            strmap_put(&f->seen, buf, w->gen);
        }
}

static void add_rw(Acc *a, int origin, int cid) {
    char k[32];
    snprintf(k, sizeof k, "%d:%d", origin, cid);
    int i = strmap_get(&a->rwix, k);
    if (i >= 0) { a->rw[i].n++; return; }
    AnRw r = {origin, cid, 1};
    PUSH(a->rw, a->nrw, a->caprw, r);
    strmap_put(&a->rwix, k, a->nrw - 1);
}

static void replay_change(B *b, int ei) {
    AnEv *e = &b->ev[ei];
    File *f = file_get(b, e->path);
    if (!f) return;
    AnSnapshot *s = b->s;
    const Record *rec = change_of(b, e);
    const Change *chg = rec ? rec->c : NULL;
    if (!chg || (!chg->cmd && !chg->created && !chg->patched)) return;
    bool cmd = chg->cmd, create = !cmd && chg->created;
    const Hunk *hunks = chg->hunks;
    int nh = chg->nhunks;
    if (cmd && f->text && !f->unplaced && has_new_lines(f->text->l, f->text->n, hunks, nh)) {
        s->stats.dups++; /* an edit made while a command ran: Claude Code credits it to the command too */
        return;
    }
    int session = b->nm[e->actor].sess;
    int t = turn_of(b, session, e->ts, true);
    if (t < 0) return;
    int key = prompt_at(b, session, e->ts, NULL);
    int cid = s->nchanges;
    AnChange ch = {.turn = t, .comment = comment_of(b, e), .actor = actor_out(b, e->actor), .verify = AN_UNTESTED};
    ch.tool = xstrdup(cmd ? "Bash" : chg->tool ? chg->tool : create ? "Write" : "Edit");
    ch.epoch = (long long)nearbyint(epoch(e->ts));
    s->stats.no_text_before += !cmd && !create && !chg->original;
    /* Context cost: what this agent read and searched in this turn before it. */
    Ctx *c = &b->nm[e->actor].ctx;
    int ni = 0;
    for (int i = 0; i < c->nitems; i++)
        if (c->items[i].key == key) c->items[ni++] = c->items[i];
    c->nitems = ni;
    AnRead *top = NULL;
    int ntop = 0, captop = 0;
    for (int i = 0; i < ni; i++) {
        if (c->items[i].path < 0) { ch.searches++; continue; }
        ch.reads++;
        int j = 0;
        while (j < ntop && top[j].path != c->items[i].path) j++;
        if (j == ntop) {
            AnRead r = {c->items[i].path, 0};
            PUSH(top, ntop, captop, r);
        }
        top[j].n++;
    }
    /* most_common: by count, ties in first-read order (a stable sort) */
    for (int i = 1; i < ntop; i++)
        for (int j = i; j > 0 && top[j].n > top[j - 1].n; j--) {
            AnRead x = top[j]; top[j] = top[j - 1]; top[j - 1] = x;
        }
    if (ntop > TOP_READS) ntop = TOP_READS;
    for (int i = 0; i < ntop; i++) /* paths indexed in the order they're first shown */
        top[i].path = name_ix(&b->spathix, &s->paths, &s->npaths, &b->cappaths, b->pathname[top[i].path]);
    ch.top = top;
    ch.ntop = ntop;
    /* Cost: this agent's responses in this turn since its last change, and the time since then. */
    double *stamps = xmalloc((size_t)(c->nspent + 2) * sizeof *stamps), first = -1;
    int nst = 0;
    for (int i = 0; i < c->nspent; i++)
        if (c->spent[i].key == key) {
            for (int k = 0; k < 3; k++) ch.tok[k] += c->spent[i].tok[k];
            if (first < 0) first = c->spent[i].ts;
        }
    double since = c->last_turn == t ? c->last_ts : first >= 0 ? first : e->ts;
    stamps[nst++] = since;
    stamps[nst++] = e->ts;
    for (int i = 0; i < c->nspent; i++)
        if (c->spent[i].key == key && since <= c->spent[i].ts && c->spent[i].ts <= e->ts) stamps[nst++] = c->spent[i].ts;
    ch.secs = an_active(stamps, nst);
    free(stamps);
    c->nspent = 0;
    c->last_turn = t;
    c->last_ts = e->ts;
    PUSH(s->changes, s->nchanges, b->capchanges, ch);
    GROW(b->chs, cid, b->capchs);
    GROW(b->cht, cid, b->capcht);
    b->chs[cid] = session, b->cht[cid] = e->ts;

    /* The replay: the text after, with its lines' origins. */
    Text *old = NULL, *new = NULL; /* the text before, when known; after */
    char *nw = NULL;
    const Text *later = NULL;
    int *no = NULL, *rem = NULL, nrem = 0;
    Hunk *moved = NULL;
    if (cmd || (!create && !chg->original)) { /* no text before it: as a command */
        if (f->text && !f->unplaced) {
            nw = apply_lines(f->text->l, f->text->n, hunks, nh, true, f->orig, cid, &no, &rem, &nrem);
            if (nw) old = text_ref(f->text);
        }
        if (!nw) {
            Text *base = (Text *)git_text(b, f, e->ts);
            char *chk = base ? apply_lines(base->l, base->n, hunks, nh, true, NULL, 0, NULL, NULL, NULL) : NULL;
            if (chk) {
                free(chk);
                resync(f, base);
                old = text_ref(base);
                free(rem);
                rem = NULL;
                nw = apply_lines(base->l, base->n, hunks, nh, false, f->orig, cid, &no, &rem, &nrem);
                s->stats.from_git++;
            }
        }
        if (!nw) {
            later = later_text(b, f, e->ts, hunks, nh, &moved);
            if (later) s->stats.lined_up_later++;
            else s->stats.file_level_only++;
            f->unplaced = true;
            f->fill = cid;
        }
    } else if (create) {
        new = text_ref(rec->created);
        f->unplaced = false;
    } else {
        old = text_ref(rec->original);
        resync(f, old);
        nw = apply_lines(old->l, old->n, hunks, nh, false, f->orig, cid, &no, &rem, &nrem);
    }
    if (nw || new) {
        if (nw) new = text_take(nw);
        if (create) {
            no = xmalloc((size_t)new->n * sizeof *no);
            for (int i = 0; i < new->n; i++) no[i] = cid;
        }
        text_free(f->text);
        free(f->orig);
        f->text = text_ref(new);
        f->orig = no;
    }
    Work w = {.change = true, .actor = e->actor, .gen = c->gen, .t = new ? new : text_ref((Text *)later), .cid = cid,
              .old = old, .chg = chg, .moved = moved, .create = create, .placed = new != NULL, .rem = rem, .nrem = nrem};
    PUSH(f->work, f->nwork, f->capwork, w);
}

/* The second pass of a change: the functions it touched (holding its + lines in
 * the text after, its - lines in the one before), whether it changed one its
 * agent hadn't read, rework and trend per function. */
static void work_change(Analysis *an, File *f, Work *w) {
    const Change *chg = w->chg;
    const Hunk *hunks = w->moved ? w->moved : chg->hunks;
    int nh = chg->nhunks, cid = w->cid;
    bool have_hunks = !w->create;
    Touch *touched = NULL;
    int ntouched = 0, captouched = 0;
    if (w->create) {
        const Defs *d = defs_of(an, f, w->t);
        for (int i = 0; i < d->n; i++) touch(&touched, &ntouched, &captouched, d->key[i])->add += d->d[i].b - d->d[i].a + 1;
    }
    bool old_set = w->old && w->old->len; /* build_data's `if old`: a non-empty text */
    /* cached Defs stay put until the build's sweep */
    const Defs *od = old_set ? defs_of(an, f, w->old) : w->t ? defs_of(an, f, w->t) : NULL;
    int nod = od ? od->n : 0;
    int fa = 0, fd = 0;
    if (have_hunks && nh) {
        const Defs *nd = w->t ? defs_of(an, f, w->t) : NULL;
        for (int k = 0; k < nh; k++) {
            int o = hunks[k].old_start, n = hunks[k].new_start;
            for (int j = 0; j < hunks[k].n; j++) {
                char x = hunks[k].lines[j][0];
                if (x == '+') {
                    fa++;
                    for (int i = 0; nd && i < nd->n; i++)
                        if (nd->d[i].a <= n && n <= nd->d[i].b) touch(&touched, &ntouched, &captouched, nd->key[i])->add++;
                    n++;
                } else if (x == '-') {
                    fd++;
                    for (int i = 0; i < nod; i++)
                        if (od->d[i].a <= o && o <= od->d[i].b) touch(&touched, &ntouched, &captouched, od->key[i])->del++;
                    o++;
                } else {
                    o++, n++;
                }
            }
        }
    } else if (w->create) {
        fa = nlines(w->t);
    }
    /* Rework: lines this change removed, by the change that wrote them. */
    if (old_set)
        for (int r = 0; r < w->nrem; r++) {
            int line = w->rem[2 * r], origin = w->rem[2 * r + 1];
            if (origin < 0) continue;
            add_rw(acc_of(f, ""), origin, cid);
            for (int i = 0; i < nod; i++)
                if (od->d[i].a <= line && line <= od->d[i].b) add_rw(acc_of(f, od->key[i]), origin, cid);
        }
    /* Trend: each def it touched, its size and complexity right after; the file's
     * length. Not from a later text it was lined up with: that isn't the file right after it. */
    if (w->placed) {
        if (have_hunks && nh) {
            const Defs *d = defs_of(an, f, w->t);
            for (int i = 0; i < d->n; i++) {
                bool hit = false;
                for (int q = 0; q < ntouched && !hit; q++) hit = !strcmp(touched[q].key, d->key[i]);
                if (!hit) continue;
                AnTr tr = {cid, d->d[i].b - d->d[i].a + 1, d->d[i].cx};
                Acc *a = acc_of(f, d->key[i]);
                PUSH(a->tr, a->ntr, a->captr, tr);
            }
        }
        AnTr tr = {cid, nlines(w->t), 0};
        Acc *a = acc_of(f, "");
        PUSH(a->tr, a->ntr, a->captr, tr);
    }
    /* Blind: an existing def changed with none of it read (or written) in this context since its compaction. */
    char buf[1200];
    bool any_blind = false;
    unsigned char *blind = xcalloc((size_t)ntouched, 1);
    for (int q = 0; q < ntouched; q++) {
        bool existed = false;
        for (int i = 0; old_set && i < nod && !existed; i++) existed = !strcmp(od->key[i], touched[q].key);
        seen_key(buf, sizeof buf, w->actor, touched[q].key); /* keys are distinct: one doesn't see another */
        blind[q] = existed && strmap_get(&f->seen, buf) != w->gen;
        any_blind |= blind[q];
        strmap_put(&f->seen, buf, w->gen);
    }
    AnCh fc = {cid, fa, fd, any_blind};
    Acc *fa_acc = acc_of(f, "");
    PUSH(fa_acc->ch, fa_acc->nch, fa_acc->capch, fc);
    for (int q = 0; q < ntouched; q++) {
        AnCh dc = {cid, touched[q].add, touched[q].del, blind[q]};
        Acc *a = acc_of(f, touched[q].key);
        PUSH(a->ch, a->nch, a->capch, dc);
    }
    f->blind += any_blind;
    free(blind);
    for (int q = 0; q < ntouched; q++) free(touched[q].key);
    free(touched);
}

/* The second pass over one file: its work in order, then today's functions.
 * Cancelled: only what the work holds is freed. */
static void work_file(Analysis *an, File *f) {
    atomic_int *cancel = &an->cancel;
    for (int i = 0; i < f->nwork; i++) {
        Work *w = &f->work[i];
        if (atomic_load(cancel)) {
        } else if (w->change) work_change(an, f, w);
        else work_read(an, f, w);
        text_free(w->t);
        text_free(w->old);
        free(w->moved);
        free(w->rem);
    }
    free(f->work);
    f->work = NULL;
    f->nwork = 0;
    if (f->today && !atomic_load(cancel)) defs_of(an, f, f->today); /* cached for the tree */
    syn_parse_free(f->parse); /* its last tree: only the next version would use it */
    f->parse = NULL;
}

typedef struct {
    Analysis *an;
    File *files;
    int *order, n;
    atomic_int next;
} Pool;

static void *worker(void *ud) {
    Pool *p = ud;
    for (int i; (i = atomic_fetch_add(&p->next, 1)) < p->n;) work_file(p->an, &p->files[p->order[i]]);
    return NULL;
}

static int cmp_work(const void *pa, const void *pb, void *ud) {
    const File *f = ud;
    return f[*(const int *)pb].nwork - f[*(const int *)pa].nwork;
}

/* Files' second passes on worker threads, the busiest first. */
static void work_all(B *b) {
    Pool p = {.an = b->an, .files = b->files, .order = xmalloc((size_t)b->nfiles * sizeof(int)), .n = b->nfiles};
    for (int i = 0; i < b->nfiles; i++) p.order[i] = i;
    qsort_r(p.order, (size_t)p.n, sizeof *p.order, cmp_work, b->files);
    atomic_init(&p.next, 0);
    long ncpu = sysconf(_SC_NPROCESSORS_ONLN);
    int nt = ncpu < 1 ? 1 : ncpu > WORKERS ? WORKERS : (int)ncpu;
    pthread_t th[WORKERS];
    int started = 0;
    for (int i = 1; i < nt; i++) started += !pthread_create(&th[started], NULL, worker, &p);
    worker(&p); /* this thread too; it also does everything the others couldn't start */
    for (int i = 0; i < started; i++) pthread_join(th[i], NULL);
    free(p.order);
    for (int i = 0; i < b->nfiles; i++) b->s->stats.blind += b->files[i].blind;
}

/* ---- the tree ---------------------------------------------------------- */

static int node_add(AnSnapshot *s, int *cap, int parent, const char *name, int kind) {
    AnNode n = {.name = xstrdup(name), .kind = kind, .parent = parent};
    PUSH(s->nodes, s->nnodes, *cap, n);
    int ix = s->nnodes - 1;
    if (parent >= 0) {
        AnNode *p = &s->nodes[parent];
        PUSH(p->kids, p->nkids, p->capkids, ix);
    }
    return ix;
}

static void *dup_arr(const void *p, int n, size_t sz) {
    void *d = xmalloc((size_t)n * sz);
    if (n) memcpy(d, p, (size_t)n * sz);
    return d;
}

static void node_fill(AnNode *n, Acc *a) {
    if (!a) return;
    n->ch = dup_arr(a->ch, n->nch = a->nch, sizeof *n->ch);
    n->rw = dup_arr(a->rw, n->nrw = a->nrw, sizeof *n->rw);
    n->tr = dup_arr(a->tr, n->ntr = a->ntr, sizeof *n->tr);
}

static void build_tree(B *b, char **tree, int ntree) {
    AnSnapshot *s = b->s;
    int cap = 0;
    const char *base = strrchr(b->root, '/');
    node_add(s, &cap, -1, base && base[1] ? base + 1 : b->root, AN_DIR);
    StrMap dirs = {0};
    for (int i = 0; i < ntree; i++) {
        File *f = file_get(b, tree[i]);
        if (!f || !f->today) continue;
        int parent = 0;
        for (const char *p = tree[i], *sl; (sl = strchr(p, '/')); p = sl + 1) {
            char *dir = xstrndup(tree[i], (size_t)(sl - tree[i]));
            int k = strmap_get(&dirs, dir);
            if (k < 0) {
                char *nm = xstrndup(p, (size_t)(sl - p));
                k = node_add(s, &cap, parent, nm, AN_DIR);
                free(nm);
                strmap_put(&dirs, dir, k);
            }
            free(dir);
            parent = k;
        }
        const char *nm = strrchr(tree[i], '/');
        int fi = node_add(s, &cap, parent, nm ? nm + 1 : tree[i], AN_FILE);
        AnNode *fn = &s->nodes[fi];
        char lang[SYN_NAME_MAX];
        syn_lang_name(f->rel, lang);
        fn->lang = xstrdup(lang);
        fn->lines = nlines(f->today);
        int k = strmap_get(&f->accix, "");
        node_fill(fn, k >= 0 ? &f->acc[k] : NULL);
        int nl = fn->lines;
        AnRun *runs = NULL;
        int nr = 0, capr = 0;
        for (int j = 0; j < nl; j++) {
            int o = f->text && j < f->text->n ? f->orig[j] : -1;
            if (nr && runs[nr - 1].origin == o) runs[nr - 1].n++;
            else {
                AnRun r = {1, o};
                PUSH(runs, nr, capr, r);
            }
        }
        fn->blame = runs;
        fn->nblame = nr;
        const Defs *d = defs_of(b->an, f, f->today);
        int *ix = xmalloc((size_t)d->n * sizeof *ix), cx = 0;
        for (int j = 0; j < d->n; j++) {
            const SynDef *sd = &d->d[j];
            ix[j] = node_add(s, &cap, sd->parent >= 0 ? ix[sd->parent] : fi, sd->name, sd->cls ? AN_CLASS : AN_FUNC);
            AnNode *dn = &s->nodes[ix[j]];
            dn->a = sd->a, dn->b = sd->b, dn->lines = sd->b - sd->a + 1, dn->cx = sd->cx, dn->depth = sd->depth;
            int q = strmap_get(&f->accix, d->key[j]);
            node_fill(dn, q >= 0 ? &f->acc[q] : NULL);
            if (sd->parent < 0) cx += sd->cx;
        }
        s->nodes[fi].cx = cx;
        free(ix);
        fn = &s->nodes[fi]; /* the source goes to the node: nothing reads it here any more */
        fn->src = f->today->s;
        fn->srclen = f->today->len;
        fn->size = f->size;
        fn->mtime = f->mtime;
        f->today->s = NULL;
    }
    strmap_free(&dirs);
}

/* Files on disk today under root: git's (tracked, and untracked not ignored), sorted. */
static char **list_tree(B *b, int *n, bool *is_git) {
    const char *args[] = {"ls-files", "-z", "--cached", "--others", "--exclude-standard", NULL};
    size_t len = 0;
    char *out = git_ok(b->an, args, &len);
    char **l = NULL;
    int nl = 0, cap = 0;
    *is_git = out != NULL;
    for (size_t i = 0; out && i < len;) {
        size_t k = strlen(out + i);
        if (k) PUSH(l, nl, cap, xstrndup(out + i, k));
        i += k + 1;
    }
    free(out);
    if (nl) qsort(l, (size_t)nl, sizeof *l, cmp_str);
    int w = 0;
    for (int i = 0; i < nl; i++) /* a file deleted but still in the index is listed twice, or once and gone */
        if (w && !strcmp(l[w - 1], l[i])) free(l[i]);
        else l[w++] = l[i];
    *n = w;
    return l;
}

/* Actors: subagents (their TITLE names a type and session), session titles. */
static void setup_actors(B *b) {
    for (int i = 0; i < b->nev; i++)
        if (b->ev[i].kind == EV_TITLE && b->ev[i].session && strmap_get(&b->nameix, b->ev[i].session) < 0)
            b_name(b, xstrdup(b->ev[i].session));
    int nn = b->nname;
    b->nm = xcalloc((size_t)nn, sizeof *b->nm);
    for (int i = 0; i < nn; i++) b->nm[i].sess = i, b->nm[i].ctx.last_turn = -1, b->nm[i].out = -1;
    for (int i = 0; i < b->nev; i++) {
        AnEv *e = &b->ev[i];
        if (e->kind != EV_TITLE) continue;
        Name *n = &b->nm[e->actor];
        if (e->session) n->sub = true, n->sess = strmap_get(&b->nameix, e->session), n->type = e->type, n->task = e->text;
        else n->title = e->text;
    }
    /* Usage: the last per response (the most output). */
    b->drop = xcalloc((size_t)b->nev, sizeof *b->drop);
    StrMap usage = {0};
    for (int i = 0; i < b->nev; i++) {
        AnEv *e = &b->ev[i];
        if (e->kind != EV_USAGE || !e->tool_id) continue;
        int k = strmap_get(&usage, e->tool_id);
        if (k >= 0) b->drop[k] = true;
        strmap_put(&usage, e->tool_id, i);
    }
    strmap_free(&usage);
}

/* Prompts per session (typed or pasted, main sessions only; a resumed copy's
 * count for it too), what resets comments, calls with comments, Bash calls. */
static void setup_prompts(B *b) {
    int nn = b->nname;
    StrMap callseen = {0};
    for (int i = 0; i < b->nev; i++) {
        AnEv *e = &b->ev[i];
        bool prompt = (e->kind == EV_TURN || e->kind == EV_TURN_COPY) && e->text &&
                      (e->prompt == PROMPT_HUMAN || e->prompt == PROMPT_PASTED);
        Name *n = &b->nm[e->actor];
        if (prompt && !n->sub) PUSH(n->pr, n->npr, n->cappr, ((Prompt){e->ts, e->text, -1}));
        if (prompt || e->kind == EV_COMPACT) PUSH(n->resets, n->nresets, n->capresets, e->ts);
        if (e->tool_id && e->src >= 0 && e->notes > 0 && e->kind != EV_USAGE) {
            char k[600];
            snprintf(k, sizeof k, "%d\x1f%s", e->actor, e->tool_id);
            if (strmap_get(&callseen, k) < 0) {
                strmap_put(&callseen, k, 1);
                struct NoteCall nc = {e->src_off, e->note_off, e->notes, e->t0 > 0 ? e->t0 : e->ts, e->tool_id};
                PUSH(n->calls, n->ncalls, n->capcalls, nc);
            }
        }
        if (e->kind == EV_BASH_END && e->tool_id) strmap_put(&b->bashids, e->tool_id, 1);
    }
    strmap_free(&callseen);
    for (int i = 0; i < nn; i++) {
        Name *n = &b->nm[i];
        if (n->ncalls) qsort(n->calls, (size_t)n->ncalls, sizeof *n->calls, cmp_call);
        if (!n->npr) continue;
        qsort(n->pr, (size_t)n->npr, sizeof *n->pr, cmp_prompt);
        for (int k = 0; k < n->npr; k++) {
            Prompt *p = &n->pr[k];
            char *key = xasprintf("%lld\x1f%s", micros(p->ts), p->title);
            p->key = strmap_get(&b->keyix, key);
            if (p->key < 0) {
                strmap_put(&b->keyix, key, p->key = b->nkeys);
                PUSH(b->turn_of_key, b->nkeys, b->capkeys, -1);
            }
            free(key);
        }
    }
}

/* Paths among cand that git ignores (a path a change touched, gone from disk:
 * a temporary file in an ignored folder isn't part of the repo). */
static void git_ignored(B *b, char **cand, int n, StrMap *out) {
    for (int at = 0; at < n;) {
        int k = n - at < 200 ? n - at : 200;
        const char **args = xmalloc((size_t)(k + 4) * sizeof *args);
        int na = 0;
        args[na++] = "check-ignore", args[na++] = "-z", args[na++] = "--";
        for (int i = 0; i < k; i++) args[na++] = cand[at + i];
        args[na] = NULL;
        size_t len = 0;
        int st = -1;
        char *o = git(b->an, args, &len, &st); /* exit 1: none ignored */
        for (size_t i = 0; o && st == 0 && i < len; i += strlen(o + i) + 1)
            if (o[i]) strmap_put(out, o + i, 1);
        free(o);
        free(args);
        at += k;
    }
}

/* Files: today's tree (the files history changed first, then the rest up to
 * TREE_MAX, so the cap never drops a file with history), the files history
 * changed that are gone now, and their known texts. */
static void setup_files(B *b) {
    StrMap changed = {0};
    for (int i = 0; i < b->nev; i++) {
        AnEv *e = &b->ev[i];
        if ((e->kind == EV_CREATE || e->kind == EV_EDIT || e->kind == EV_DELETE) && *e->path)
            strmap_put(&changed, e->path, 1);
    }
    bool is_git;
    int n;
    char **all = list_tree(b, &n, &is_git);
    { /* what the files' git logs are as of */
        const char *args[] = {"rev-parse", "-q", "--verify", "HEAD^{commit}", NULL};
        char *out = git_ok(b->an, args, NULL);
        if (out && *out) out[strcspn(out, "\n")] = 0, b->head = out;
        else free(out);
    }
    char **tree = xmalloc((size_t)n * sizeof *tree);
    bool *took = xcalloc((size_t)n, sizeof *took);
    b->ntree = 0;
    for (int pass = 0; pass < 2; pass++)
        for (int i = 0; i < n && b->ntree < TREE_MAX; i++) {
            if (took[i] || (strmap_get(&changed, all[i]) >= 0) != (pass == 0)) continue;
            took[i] = true;
            struct stat st;
            Text *t = read_text(b->root, all[i], true, &st);
            if (!t) continue;
            File *f = file_add(b, all[i]);
            f->in_tree = true;
            f->today = t;
            f->size = st.st_size;
            f->mtime = st.st_mtim.tv_sec + st.st_mtim.tv_nsec / 1e9;
            tree[b->ntree++] = xstrdup(all[i]);
        }
    for (int i = 0; i < n; i++) {
        b->s->stats.cut += !took[i];
        free(all[i]);
    }
    free(all);
    free(took);
    if (b->ntree) qsort(tree, (size_t)b->ntree, sizeof *tree, cmp_str);
    b->tree = tree;
    /* Changed files not in the tree: gone from disk (unless git ignores them),
     * or anything when root isn't a git repo. On disk but not listed: ignored. */
    char **gone = NULL;
    int ngone = 0, capgone = 0;
    for (size_t i = 0; i < changed.cap; i++) {
        const char *rel = changed.keys[i];
        if (!rel || file_get(b, rel)) continue;
        struct stat st;
        char *p = xasprintf("%s/%s", b->root, rel);
        bool there = !stat(p, &st);
        free(p);
        if (!is_git) {
            File *f = file_add(b, rel);
            if (there) {
                f->today = read_text(b->root, rel, true, &st);
                f->in_tree = f->today != NULL;
                f->size = st.st_size;
                f->mtime = st.st_mtim.tv_sec + st.st_mtim.tv_nsec / 1e9;
            }
        } else if (!there) {
            PUSH(gone, ngone, capgone, (char *)rel);
        }
    }
    if (ngone) {
        qsort(gone, (size_t)ngone, sizeof *gone, cmp_str);
        StrMap ign = {0};
        git_ignored(b, gone, ngone, &ign);
        for (int i = 0; i < ngone; i++)
            if (strmap_get(&ign, gone[i]) < 0) file_add(b, gone[i]);
        strmap_free(&ign);
    }
    free(gone);
    if (!is_git) { /* the files history touched, sorted */
        int cap = b->ntree;
        for (int i = 0; i < b->nfiles; i++) {
            File *f = &b->files[i];
            if (!f->in_tree) continue;
            bool listed = false;
            for (int k = 0; k < b->ntree && !listed; k++) listed = !strcmp(b->tree[k], f->rel);
            if (listed) continue;
            if (b->ntree >= TREE_MAX) { /* past the cap: not in the tree */
                b->s->stats.cut++;
                f->in_tree = false;
                text_free(f->today);
                f->today = NULL;
                continue;
            }
            PUSH(b->tree, b->ntree, cap, xstrdup(f->rel));
        }
        if (b->ntree) qsort(b->tree, (size_t)b->ntree, sizeof *b->tree, cmp_str);
    }
    strmap_free(&changed);
    /* known: each file's texts by time, for lining up commands' patches */
    for (int i = 0; i < b->nev; i++) {
        AnEv *e = &b->ev[i];
        if (e->kind != EV_CREATE && e->kind != EV_EDIT && e->kind != EV_DELETE) continue;
        File *f = file_get(b, e->path);
        if (!f || strmap_get(&b->bashids, e->tool_id) >= 0) continue;
        Known k0 = {i, 0, NULL, false}, k1 = {i, 1, NULL, false};
        PUSH(f->known, f->nknown, f->capknown, k0);
        PUSH(f->known, f->nknown, f->capknown, k1);
    }
    for (int i = 0; i < b->nfiles; i++) {
        File *f = &b->files[i];
        if (f->in_tree) {
            Known k = {-1, 2, f->today, true}; /* borrowed */
            PUSH(f->known, f->nknown, f->capknown, k);
        }
        if (f->nknown) qsort_r(f->known, (size_t)f->nknown, sizeof *f->known, cmp_known, b->ev);
    }
}

/* cmd_runs of a command: a test or build run under the root from start
 * (verify; relative to the root), or a search anywhere. A function of the
 * strings alone (the root is fixed): cached by them. */
static int runs(B *b, bool verify, const char *cmd, const char *start) {
    Analysis *an = b->an;
    const char *st = verify && start ? start : ""; /* none: the root, as "" is */
    char *key = xasprintf("%c%zu:%s%s", verify ? 'v' : 's', strlen(st), st, cmd);
    int k = cache_find(&an->cmds, key, an->builds), r;
    if (k >= 0) r = (int)(intptr_t)an->cmds.e[k].val;
    else {
        pthread_once(&re_once, re_init);
        r = verify ? cmd_runs(&re_verify, cmd, an->root, start) : cmd_runs(&an->search, cmd, NULL, NULL);
        cache_put(&an->cmds, key, (void *)(intptr_t)r, an->builds);
    }
    free(key);
    return r;
}

/* The replay, in time order: texts, origins, turns, costs; what needs the
 * functions goes to the files' work for the second pass. */
static void replay(B *b) {
    for (int i = 0; i < b->nev; i++) {
        if (!(i & 63) && atomic_load(&b->an->cancel)) break; /* work_all still frees what was queued */
        AnEv *e = &b->ev[i];
        Ctx *c = &b->nm[e->actor].ctx;
        int session = b->nm[e->actor].sess;
        switch (e->kind) {
        case EV_COMPACT: c->gen++; break;
        case EV_SEARCH:
        case EV_TOOL: gather(c, e->ts, prompt_at(b, session, e->ts, NULL), -1); break;
        case EV_BASH_END:
            if (e->text && (runs(b, false, e->text, NULL) & CMD_RUN)) gather(c, e->ts, prompt_at(b, session, e->ts, NULL), -1);
            break;
        case EV_USAGE:
            if (!b->drop[i]) {
                Spend sp = {e->ts, prompt_at(b, session, e->ts, NULL), {e->tok[0], e->tok[1], e->tok[2]}};
                PUSH(c->spent, c->nspent, c->capspent, sp);
            }
            break;
        case EV_READ:
            if (e->first >= 0 && e->path)
                replay_read(b, e, *e->path ? file_get(b, e->path) : NULL, prompt_at(b, session, e->ts, NULL));
            break;
        case EV_CREATE:
        case EV_EDIT:
        case EV_DELETE: replay_change(b, i); break;
        default: break;
        }
    }
}

/* Verification: a test or build run later in the same turn, by any agent of
 * that session; the last one's result. */
static void verify(B *b) {
    AnSnapshot *s = b->s;
    /* runs_of[turn]: the session's test runs in that turn, in time order */
    struct { int *ev, n, cap; } *runs_of = xcalloc((size_t)s->nturns, sizeof *runs_of);
    for (int i = 0; i < b->nev; i++) {
        const AnEv *e = &b->ev[i];
        if (e->kind != EV_BASH_END || !e->text || e->not_run) continue; /* never ran: no verdict */
        /* where it began: the event's path (cwd). A run in another tree (a
         * worktree, a scratch copy) doesn't verify this one: ignored. */
        if (!(runs(b, true, e->text, e->path) & CMD_RUN)) continue;
        int t = turn_of(b, b->nm[e->actor].sess, e->ts, false);
        if (t >= 0) PUSH(runs_of[t].ev, runs_of[t].n, runs_of[t].cap, i);
    }
    for (int k = 0; k < s->nchanges; k++) {
        AnChange *ch = &s->changes[k];
        int t = ch->turn, last = -1;
        for (int v = 0; v < runs_of[t].n; v++) {
            const AnEv *e = &b->ev[runs_of[t].ev[v]];
            if (b->nm[e->actor].sess == b->chs[k] && e->ts > b->cht[k]) last = runs_of[t].ev[v];
        }
        ch->verify = last < 0 ? AN_UNTESTED : b->ev[last].err ? AN_FAILING : AN_PASSED;
    }
    for (int t = 0; t < s->nturns; t++) free(runs_of[t].ev);
    free(runs_of);
}

/* Turns: tokens, interrupts, time (prompt to its last response), actor. */
static void finish_turns(B *b) {
    AnSnapshot *s = b->s;
    struct { double *ts; int n, cap; } *stamps = xcalloc((size_t)s->nturns, sizeof *stamps);
    for (int i = 0; i < b->nev; i++) {
        const AnEv *e = &b->ev[i];
        if ((e->kind != EV_USAGE || b->drop[i]) && e->kind != EV_INTERRUPT) continue;
        int t = turn_of(b, b->nm[e->actor].sess, e->ts, false);
        if (t < 0) continue;
        AnTurn *tn = &s->turns[t];
        if (e->kind == EV_INTERRUPT) {
            tn->intr = true;
            continue;
        }
        tn->has_tok = true;
        for (int k = 0; k < 3; k++) tn->tok[k] += e->tok[k];
        PUSH(stamps[t].ts, stamps[t].n, stamps[t].cap, e->ts);
    }
    for (int t = 0; t < s->nturns; t++) {
        AnTurn *tn = &s->turns[t];
        PUSH(stamps[t].ts, stamps[t].n, stamps[t].cap, tn->ts);
        tn->secs = an_active(stamps[t].ts, stamps[t].n);
        free(stamps[t].ts);
        tn->actor = actor_out(b, tn->actor); /* was its session's name index */
    }
    free(stamps);
}

static void file_free(File *f) {
    free(f->rel);
    text_free(f->today);
    text_free(f->text);
    free(f->orig);
    for (int k = 0; k < f->nknown; k++)
        if (f->known[k].sub != 2) text_free(f->known[k].t);
    free(f->known);
    for (int k = 0; k < f->nacc; k++) {
        Acc *a = &f->acc[k];
        free(a->key), free(a->ch), free(a->rw), free(a->tr);
        strmap_free(&a->rwix);
    }
    free(f->acc);
    strmap_free(&f->accix);
    strmap_free(&f->seen);
    syn_parse_free(f->parse);
    strmap_free(&f->shownix);
    for (int k = 0; k < f->nshown; k++) text_free(f->shown[k]);
    free(f->shown);
}

static void b_free(B *b) {
    strv_free(b->tree, b->ntree);
    free(b->drop);
    for (int i = 0; i < b->nfiles; i++) file_free(&b->files[i]);
    free(b->files);
    strmap_free(&b->fileix);
    for (int i = 0; i < b->nname; i++) {
        Name *n = &b->nm[i];
        free(n->ctx.items), free(n->ctx.spent), free(n->pr), free(n->calls), free(n->resets);
    }
    free(b->nm);
    strmap_free(&b->keyix);
    free(b->turn_of_key);
    strmap_free(&b->pathix);
    strmap_free(&b->spathix);
    strv_free(b->pathname, b->npathname);
    strmap_free(&b->bashids);
    strmap_free(&b->noteix);
    free(b->note_val);
    strmap_free(&b->commentix);
    free(b->chs), free(b->cht);
    for (int i = b->owned_from; i < b->nname; i++) free(b->name[i]);
    free(b->name);
    strmap_free(&b->nameix);
    free(b->ev);
    free(b->head);
}

/* AIG_DEBUG: a build's phases (wall ms), and what git and tree-sitter took. */
static void print_phases(Analysis *an, const double *t, int n, const char *const *name) {
    fprintf(stderr, "an_build %.0f ms:", (t[n] - t[0]) * 1e3);
    for (int i = 0; i < n; i++) fprintf(stderr, " %s %.0f", name[i], (t[i + 1] - t[i]) * 1e3);
    fprintf(stderr, "; git %d calls %.0f ms, parsed %d texts %.0f ms (all threads)", an->clk.git, an->clk.git_s * 1e3,
            an->clk.parsed, an->clk.parse_s * 1e3);
    fprintf(stderr, "; kept defs %d, shows %d, logs %d, changes %d, notes %d, cmds %d\n", an->defs.n, an->shows.n,
            an->logs.n, an->changes.n, an->notes.n, an->cmds.n);
}

AnSnapshot *an_build(Analysis *an) {
    B bb = {.an = an, .root = an->root}, *b = &bb;
    AnSnapshot *s = b->s = xcalloc(1, sizeof *s);
    static const char *const phase[] = {"setup", "files", "replay", "work", "verify", "resync", "tree", "sweep"};
    double t[9];
    int np = 0;
    t[np] = mono();
    memset(&an->clk, 0, sizeof an->clk);
    an->builds++; /* one build at a time: sview's worker, or the caller's thread */
    pthread_mutex_lock(&an->mu); /* a copy of what was added; its strings stay put */
    b->nev = (int)an->nev;
    b->ev = xmalloc((size_t)b->nev * sizeof *b->ev);
    memcpy(b->ev, an->ev, (size_t)b->nev * sizeof *b->ev);
    for (int i = 0; i < an->nname; i++) b_name(b, an->name[i]);
    b->owned_from = b->nname;
    pthread_mutex_unlock(&an->mu);
    qsort(b->ev, (size_t)b->nev, sizeof *b->ev, cmp_ev);
    setup_actors(b);
    setup_prompts(b);
    t[++np] = mono();
    setup_files(b);
    t[++np] = mono();
    if (!atomic_load(&an->cancel)) replay(b);
    t[++np] = mono();
    work_all(b);
    t[++np] = mono();
    if (atomic_load(&an->cancel)) goto cancelled;
    verify(b);
    t[++np] = mono();
    /* At the end every file lines up with today's: each line has an origin or none. */
    for (int i = 0; i < b->nfiles; i++)
        if (b->files[i].in_tree) resync(&b->files[i], b->files[i].today);
    t[++np] = mono();
    if (atomic_load(&an->cancel)) goto cancelled;
    finish_turns(b);
    build_tree(b, b->tree, b->ntree);
    t[++np] = mono();
    b_free(b);
    /* What this build didn't use goes; a cancelled build keeps all (its entries are whole). */
    cache_sweep(&an->defs, an->builds);
    cache_sweep(&an->shows, an->builds);
    cache_sweep(&an->logs, an->builds);
    cache_sweep(&an->changes, an->builds);
    cache_sweep(&an->notes, an->builds);
    cache_sweep(&an->cmds, an->builds);
    t[++np] = mono();
    if (an->debug) print_phases(an, t, np, phase);
    return s;
cancelled:
    b_free(b);
    an_snapshot_free(s);
    return NULL;
}

void an_cancel(Analysis *a, bool on) { atomic_store(&a->cancel, on); }

void an_snapshot_free(AnSnapshot *s) {
    if (!s) return;
    for (int i = 0; i < s->nnodes; i++) {
        AnNode *n = &s->nodes[i];
        free(n->name), free(n->kids), free(n->lang), free(n->ch), free(n->rw), free(n->tr), free(n->blame), free(n->src);
    }
    free(s->nodes);
    for (int i = 0; i < s->nturns; i++) free(s->turns[i].title), free(s->turns[i].corr);
    free(s->turns);
    strv_free(s->comments, s->ncomments);
    strv_free(s->paths, s->npaths);
    for (int i = 0; i < s->nactors; i++) free(s->actors[i].id), free(s->actors[i].name), free(s->actors[i].task);
    free(s->actors);
    for (int i = 0; i < s->nchanges; i++) free(s->changes[i].tool), free(s->changes[i].top);
    free(s->changes);
    free(s);
}

/* ---- JSON ---------------------------------------------------------------- */

static void j_node(FILE *f, const AnSnapshot *s, int i) {
    const AnNode *n = &s->nodes[i];
    static const char *kinds[] = {"dir", "file", "function", "class"};
    fputs("{\"name\":", f);
    json_str(f, n->name);
    fprintf(f, ",\"kind\":\"%s\"", kinds[n->kind]);
    if (n->kind == AN_FILE) fprintf(f, ",\"lines\":%d", n->lines);
    if (n->kind >= AN_FUNC) fprintf(f, ",\"lines\":%d,\"a\":%d,\"b\":%d", n->lines, n->a, n->b);
    fputs(",\"kids\":[", f);
    for (int k = 0; k < n->nkids; k++) {
        if (k) fputc(',', f);
        j_node(f, s, n->kids[k]);
    }
    fputc(']', f);
    if (n->kind != AN_DIR) {
        fputs(",\"ch\":[", f);
        for (int k = 0; k < n->nch; k++)
            fprintf(f, "%s[%d,%d,%d,%d]", k ? "," : "", n->ch[k].cid, n->ch[k].add, n->ch[k].del, n->ch[k].blind);
        fputc(']', f);
        if (n->kind == AN_FILE) {
            fputs(",\"lang\":", f);
            json_str(f, n->lang);
        } else {
            fprintf(f, ",\"cx\":%d,\"depth\":%d", n->cx, n->depth);
        }
        fputs(",\"rw\":[", f);
        for (int k = 0; k < n->nrw; k++) fprintf(f, "%s[%d,%d,%d]", k ? "," : "", n->rw[k].origin, n->rw[k].cid, n->rw[k].n);
        fputs("],\"tr\":[", f);
        for (int k = 0; k < n->ntr; k++) fprintf(f, "%s[%d,%d,%d]", k ? "," : "", n->tr[k].cid, n->tr[k].lines, n->tr[k].cx);
        fputc(']', f);
        if (n->kind == AN_FILE) {
            fputs(",\"blame\":[", f);
            for (int k = 0; k < n->nblame; k++) fprintf(f, "%s[%d,%d]", k ? "," : "", n->blame[k].n, n->blame[k].origin);
            fprintf(f, "],\"cx\":%d", n->cx);
        }
    }
    fputc('}', f);
}

static void j_strs(FILE *f, char *const *s, int n) {
    fputc('[', f);
    for (int i = 0; i < n; i++) {
        if (i) fputc(',', f);
        json_str(f, s[i]);
    }
    fputc(']', f);
}

static void iso(char *buf, size_t n, double ts) {
    long long us = micros(ts);
    time_t sec = (time_t)(us / 1000000);
    struct tm tm;
    gmtime_r(&sec, &tm);
    snprintf(buf, n, "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ", tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday, tm.tm_hour,
             tm.tm_min, tm.tm_sec, (int)(us % 1000000 / 1000));
}

void an_snapshot_json(FILE *f, const AnSnapshot *s) {
    fputs("{\"tree\":", f);
    if (s->nnodes) j_node(f, s, 0);
    else fputs("null", f);
    fputs(",\"turns\":[", f);
    for (int i = 0; i < s->nturns; i++) {
        const AnTurn *t = &s->turns[i];
        char ts[64];
        iso(ts, sizeof ts, t->ts);
        fputs(i ? ",{\"title\":" : "{\"title\":", f);
        json_str(f, t->title);
        fprintf(f, ",\"ts\":\"%s\",\"corr\":", ts);
        if (t->corr) json_str(f, t->corr);
        else fputs("null", f);
        fprintf(f, ",\"intr\":%s,\"fix\":%s", t->intr ? "true" : "false", t->fix ? "true" : "false");
        if (t->has_tok) fprintf(f, ",\"tok\":[%lld,%lld,%lld]", t->tok[0], t->tok[1], t->tok[2]);
        fprintf(f, ",\"actor\":%d,\"secs\":%ld}", t->actor, t->secs);
    }
    fputs("],\"comments\":", f);
    j_strs(f, s->comments, s->ncomments);
    fputs(",\"paths\":", f);
    j_strs(f, s->paths, s->npaths);
    fputs(",\"actors\":[", f);
    for (int i = 0; i < s->nactors; i++) {
        const AnActor *a = &s->actors[i];
        fprintf(f, "%s{\"kind\":\"%s\",\"id\":", i ? "," : "", a->sub ? "subagent" : "session");
        json_str(f, a->id);
        fputs(",\"name\":", f);
        json_str(f, a->name);
        if (a->sub) {
            fputs(",\"task\":", f);
            json_str(f, a->task);
        }
        fputc('}', f);
    }
    /* change: turn, comment, tool, verify, reads, searches, files read, time, actor,
     * tokens (out, new, cached), seconds */
    fputs("],\"changes\":[", f);
    for (int i = 0; i < s->nchanges; i++) {
        const AnChange *c = &s->changes[i];
        fprintf(f, "%s[%d,%d,", i ? "," : "", c->turn, c->comment);
        json_str(f, c->tool);
        fprintf(f, ",%d,%d,%d,[", c->verify, c->reads, c->searches);
        for (int k = 0; k < c->ntop; k++) fprintf(f, "%s[%d,%d]", k ? "," : "", c->top[k].path, c->top[k].n);
        fprintf(f, "],%lld,%d,%lld,%lld,%lld,%ld]", c->epoch, c->actor, c->tok[0], c->tok[1], c->tok[2], c->secs);
    }
    fputs("]}\n", f);
}

void an_snapshot_stats(FILE *f, const AnSnapshot *s) {
    long blamed = 0, total = 0;
    for (int i = 0; i < s->nnodes; i++)
        for (int k = 0; k < s->nodes[i].nblame; k++) {
            total += s->nodes[i].blame[k].n;
            if (s->nodes[i].blame[k].origin >= 0) blamed += s->nodes[i].blame[k].n;
        }
    int v[3] = {0}, corr = 0, intr = 0;
    for (int i = 0; i < s->nchanges; i++) v[s->changes[i].verify]++;
    for (int i = 0; i < s->nturns; i++) corr += s->turns[i].corr != NULL, intr += s->turns[i].intr;
    fprintf(f, "changes %d turns %d {'edit without text before': %d, 'blind': %d, 'file level only': %d, "
               "'lined up later': %d, 'from git': %d, 'dups': %d, 'cut': %d} blamed lines %ld/%ld verify [%d, %d, %d] "
               "corrections %d interrupted %d\n",
            s->nchanges, s->nturns, s->stats.no_text_before, s->stats.blind, s->stats.file_level_only,
            s->stats.lined_up_later, s->stats.from_git, s->stats.dups, s->stats.cut, blamed, total, v[0], v[1], v[2], corr, intr);
}
