/* Search on the structure screen: names ranked like a file finder (fzy's
 * scoring for the fuzzy tier), file contents for a literal query, and the
 * worker that runs content searches (ssearch.h). */
#include "ssearch.h"

#include <limits.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "util.h"

/* ---- characters ------------------------------------------------------------ */

/* The codepoint at s[i] and its length; a byte that doesn't start valid
 * UTF-8 (a stray continuation byte, C0, C1, F5-FF, a truncated, overlong or
 * surrogate sequence) is a codepoint of its own above Unicode's, so it
 * matches only itself. */
static uint32_t dec(const char *s, size_t n, size_t i, int *len) {
    const unsigned char *u = (const unsigned char *)s + i;
    size_t left = n - i;
    *len = 1;
    if (u[0] < 0x80) return u[0];
    int k = u[0] >= 0xF5 ? 0 : u[0] >= 0xF0 ? 4 : u[0] >= 0xE0 ? 3 : u[0] >= 0xC2 ? 2 : 0;
    if (!k || (size_t)k > left) return 0x110000u + u[0];
    uint32_t c = u[0] & (0x7F >> k);
    for (int j = 1; j < k; j++) {
        if ((u[j] & 0xC0) != 0x80) return 0x110000u + u[0];
        c = c << 6 | (u[j] & 0x3F);
    }
    if ((k == 3 && (c < 0x800 || (c >= 0xD800 && c <= 0xDFFF))) || (k == 4 && (c < 0x10000 || c > 0x10FFFF)))
        return 0x110000u + u[0]; /* overlong, a surrogate, past Unicode */
    *len = k;
    return c;
}

/* Lowercase, one codepoint to one of the same UTF-8 length: ASCII, Latin-1,
 * Greek and Cyrillic capitals. */
static uint32_t fold(uint32_t c) {
    if (c < 0x80) return c >= 'A' && c <= 'Z' ? c + 32 : c;
    if ((c >= 0xC0 && c <= 0xDE && c != 0xD7) || (c >= 0x391 && c <= 0x3A9 && c != 0x3A2) || (c >= 0x410 && c <= 0x42F))
        return c + 32;
    if (c >= 0x400 && c <= 0x40F) return c + 80;
    return c;
}

bool ss_smart_case(const char *q) {
    size_t n = strlen(q);
    for (size_t i = 0; i < n;) {
        int l;
        uint32_t c = dec(q, n, i, &l);
        if (fold(c) != c) return true;
        i += (size_t)l;
    }
    return false;
}

bool ss_binary(const char *t, size_t n) { return memchr(t, 0, n < 8000 ? n : 8000) != NULL; }

/* ---- names ----------------------------------------------------------------- */

#define PMAX 1024 /* path characters the fuzzy tier looks at */

/* fzy's scores, times 1000 */
#define NEG (INT_MIN / 2)
#define GAP_LEAD -5
#define GAP_TRAIL -5
#define GAP_INNER -10
#define CONSEC 1000
#define B_SLASH 900
#define B_WORD 800
#define B_CAPITAL 700
#define B_DOT 600

/* s[0, n) folded into out: each character to its lowercase of the same
 * length, so offsets stay; a byte that isn't valid UTF-8 copied. */
static void fold_bytes(const char *s, size_t n, char *out) {
    for (size_t i = 0; i < n;) {
        unsigned char b = (unsigned char)s[i];
        if (b < 0x80) { /* most of a path */
            out[i++] = (char)(b >= 'A' && b <= 'Z' ? b + 32 : b);
            continue;
        }
        int l;
        uint32_t c = dec(s, n, i, &l), f = fold(c);
        if (f == c) memcpy(out + i, s + i, (size_t)l);
        else if (l == 1) out[i] = (char)f;
        else out[i] = (char)(0xC0 | f >> 6), out[i + 1] = (char)(0x80 | (f & 0x3F)); /* the folds are all 2-byte */
        i += (size_t)l;
    }
}

/* A query: its first SS_QMAX characters, folded, and where each starts. */
typedef struct {
    char f[SS_QBYTES];
    int fb, qn;
    bool ascii;
    int off[SS_QMAX + 1];
    uint32_t cp[SS_QMAX];
} Query;

static void query_init(Query *q, const char *s) {
    size_t n = strlen(s), i = 0;
    q->qn = 0;
    while (i < n && q->qn < SS_QMAX && i < SS_QBYTES - 4) {
        int l;
        q->off[q->qn] = (int)i;
        q->cp[q->qn++] = fold(dec(s, n, i, &l));
        i += (size_t)l;
    }
    q->off[q->qn] = (int)i;
    q->fb = (int)i;
    fold_bytes(s, i, q->f);
    q->f[i] = 0;
    q->ascii = true;
    for (size_t k = 0; k < i; k++) q->ascii &= (unsigned char)s[k] < 0x80;
}

/* The fuzzy tier's tables, reused. */
typedef struct {
    int *t;
    size_t cap;
    uint32_t raw[PMAX], cp[PMAX]; /* as written, folded */
    int off[PMAX];
} Scratch;

/* fzy's bonus for a match at i: after a slash, a word break, a dot, or a capital after a lowercase letter. */
static int bonus(const uint32_t *raw, int i) {
    if (!i) return B_SLASH;
    uint32_t a = raw[i - 1], c = raw[i];
    if (a == '/') return B_SLASH;
    if (a == '-' || a == '_' || a == ' ') return B_WORD;
    if (a == '.') return B_DOT;
    if (a >= 'a' && a <= 'z' && c >= 'A' && c <= 'Z') return B_CAPITAL;
    return 0;
}

/* fzy: the best alignment of q in the path's first PMAX characters, its
 * score; the matched characters' offsets into pos. False when q isn't in
 * that part. The table spans only the columns from q's first character's
 * first place to its last character's last. */
static bool fuzzy(const char *raw, int len, const Query *q, Scratch *sc, int *score, int *pos) {
    int n = 0, m = q->qn, j0 = -1, j1 = -1;
    for (int i = 0; i < len && n < PMAX; n++) {
        int l = 1;
        uint32_t c = (unsigned char)raw[i];
        sc->off[n] = i;
        sc->raw[n] = c < 0x80 ? c : dec(raw, (size_t)len, (size_t)i, &l);
        sc->cp[n] = fold(sc->raw[n]);
        if (j0 < 0 && sc->cp[n] == q->cp[0]) j0 = n;
        if (sc->cp[n] == q->cp[m - 1]) j1 = n;
        i += l;
    }
    if (j0 < 0 || j1 < j0 + m - 1) return false;
    int w = j1 - j0 + 1;
    size_t need = (size_t)m * (size_t)w * 2;
    if (need > sc->cap) sc->cap = need, sc->t = xrealloc(sc->t, sizeof *sc->t * need);
    int *D = sc->t, *M = D + m * w; /* D: q[i] at column j; M: best for q[..i] in columns ..j */
    for (int i = 0; i < m; i++) {
        int prev = NEG, gap = i == m - 1 ? GAP_TRAIL : GAP_INNER;
        for (int j = 0; j < w; j++) {
            int d = NEG, at = j0 + j;
            if (q->cp[i] == sc->cp[at]) {
                if (!i) d = at * GAP_LEAD + bonus(sc->raw, at);
                else if (j) {
                    int a = M[(i - 1) * w + j - 1], b = D[(i - 1) * w + j - 1];
                    a = a == NEG ? NEG : a + bonus(sc->raw, at);
                    b = b == NEG ? NEG : b + CONSEC;
                    d = a > b ? a : b;
                }
            }
            int best = prev == NEG ? NEG : prev + gap;
            D[i * w + j] = d;
            M[i * w + j] = prev = d > best ? d : best;
        }
    }
    if (M[m * w - 1] == NEG) return false;
    *score = M[m * w - 1] + (n - 1 - j1) * GAP_TRAIL; /* the columns after the table */
    bool need_prev = false; /* backtrack: a consecutive match pins the one before it */
    for (int i = m - 1, j = w - 1; i >= 0; i--)
        for (; j >= 0; j--)
            if (D[i * w + j] != NEG && (need_prev || D[i * w + j] == M[i * w + j])) {
                need_prev = i && j && M[i * w + j] == D[(i - 1) * w + j - 1] + CONSEC;
                pos[i] = sc->off[j0 + j--];
                break;
            }
    return true;
}

/* q against a path (raw, and folded: fold), its basename from bs. A cheap
 * byte test first: q's folded bytes in order somewhere in the path. Without
 * score, a fuzzy match whose bytes already tell it (an ASCII query, a path
 * the fuzzy tier sees whole) comes back unscored: SS_FUZZY, no positions. */
static bool match(const char *raw, const char *fold, int len, int bs, const Query *q, Scratch *sc, bool score,
                  SsName *m) {
    m->tier = m->score = m->npos = 0;
    if (!q->qn || q->fb > len) return false;
    const char *p = fold, *e = fold + len;
    for (int i = 0; i < q->fb; i++, p++)
        if (!(p = memchr(p, q->f[i], (size_t)(e - p)))) return false;
    const char *at = NULL;
    if (len - bs == q->fb && !memcmp(fold + bs, q->f, (size_t)q->fb)) m->tier = SS_EXACT, at = fold + bs;
    else if (len - bs >= q->fb && !memcmp(fold + bs, q->f, (size_t)q->fb)) m->tier = SS_PREFIX, at = fold + bs;
    else if ((at = memmem(fold + bs, (size_t)(len - bs), q->f, (size_t)q->fb))) m->tier = SS_BASE;
    else if ((at = memmem(fold, (size_t)len, q->f, (size_t)q->fb))) m->tier = SS_PATH;
    if (at) {
        for (int k = 0; k < q->qn; k++) m->pos[k] = (int)(at - fold) + q->off[k];
    } else if (!score && q->ascii && len <= PMAX) {
        m->tier = SS_FUZZY;
        return true;
    } else {
        if (!fuzzy(raw, len, q, sc, &m->score, m->pos)) return false;
        m->tier = SS_FUZZY;
    }
    m->npos = q->qn;
    return true;
}

static int base_of(const char *s, int len) {
    const char *b = memrchr(s, '/', (size_t)len);
    return b ? (int)(b - s) + 1 : 0;
}

bool ss_name(const char *q, const char *path, SsName *m) {
    Query qq;
    query_init(&qq, q);
    int len = (int)strlen(path);
    char *f = xmalloc((size_t)len + 1);
    fold_bytes(path, (size_t)len, f);
    Scratch *sc = xcalloc(1, sizeof *sc);
    bool ok = match(path, f, len, base_of(path, len), &qq, sc, true, m);
    free(sc->t), free(sc), free(f);
    return ok;
}

int ss_name_cmp(const SsName *a, const char *pa, const SsName *b, const char *pb) {
    if (a->tier != b->tier) return a->tier < b->tier ? -1 : 1;
    if (a->score != b->score) return a->score > b->score ? -1 : 1;
    size_t la = strlen(pa), lb = strlen(pb);
    if (la != lb) return la < lb ? -1 : 1;
    return strcmp(pa, pb);
}

struct SsNames {
    int n;
    char *raw, *fold;    /* the paths, NUL-separated, as given and folded */
    int *at, *len, *bs;  /* each one's start, length, basename */
    Scratch sc;
    char last[SS_QBYTES]; /* the last query (folded) and every path it matched */
    int *prev, nprev;
    bool has_prev;
    int *cand;           /* the paths looked at */
    unsigned char *top;  /* per path: among the best (ss_names_find's) */
};

SsNames *ss_names_new(const char *const *paths, int n) {
    SsNames *x = xcalloc(1, sizeof *x);
    size_t total = 0;
    for (int i = 0; i < n; i++) total += strlen(paths[i]) + 1;
    x->n = n;
    x->raw = xmalloc(total + 1), x->fold = xmalloc(total + 1);
    x->at = xmalloc(sizeof *x->at * (size_t)(n + 1));
    x->len = xmalloc(sizeof *x->len * (size_t)(n + 1));
    x->bs = xmalloc(sizeof *x->bs * (size_t)(n + 1));
    x->prev = xmalloc(sizeof *x->prev * (size_t)(n + 1));
    x->cand = xmalloc(sizeof *x->cand * (size_t)(n + 1));
    x->top = xcalloc((size_t)(n + 1), 1);
    size_t o = 0;
    for (int i = 0; i < n; i++) {
        size_t l = strlen(paths[i]);
        memcpy(x->raw + o, paths[i], l + 1);
        fold_bytes(paths[i], l, x->fold + o);
        x->fold[o + l] = 0;
        x->at[i] = (int)o, x->len[i] = (int)l, x->bs[i] = base_of(paths[i], (int)l);
        o += l + 1;
    }
    return x;
}

void ss_names_free(SsNames *x) {
    if (!x) return;
    free(x->raw), free(x->fold), free(x->at), free(x->len), free(x->bs), free(x->prev), free(x->cand), free(x->top), free(x->sc.t);
    free(x);
}

bool ss_names_match(SsNames *x, const char *q, int i, SsName *m) {
    Query qq;
    query_init(&qq, q);
    return i >= 0 && i < x->n && match(x->raw + x->at[i], x->fold + x->at[i], x->len[i], x->bs[i], &qq, &x->sc, true, m);
}

typedef struct { int i, tier, score; } Rank;

/* a before b: tier, score, the shorter path, its bytes (ss_name_cmp's order) */
static bool better(const SsNames *x, Rank a, Rank b) {
    if (a.tier != b.tier) return a.tier < b.tier;
    if (a.score != b.score) return a.score > b.score;
    if (x->len[a.i] != x->len[b.i]) return x->len[a.i] < x->len[b.i];
    return strcmp(x->raw + x->at[a.i], x->raw + x->at[b.i]) < 0;
}

/* The heap's worst at its root. */
static void sift(const SsNames *x, Rank *h, int n, int k) {
    for (;;) {
        int c = 2 * k + 1;
        if (c >= n) return;
        if (c + 1 < n && better(x, h[c], h[c + 1])) c++;
        if (!better(x, h[k], h[c])) return;
        Rank t = h[k];
        h[k] = h[c], h[c] = t, k = c;
    }
}

static int cmp_rank(const void *a, const void *b, void *ud) {
    Rank p = *(const Rank *)a, q = *(const Rank *)b;
    return better(ud, p, q) ? -1 : better(ud, q, p) ? 1 : 0;
}

int ss_names_find(SsNames *x, const char *q, int *out, int top) {
    Query qq;
    query_init(&qq, q);
    int nc = 0;
    if (x->has_prev && x->last[0] && !strncmp(qq.f, x->last, strlen(x->last))) /* it extends the last one */
        memcpy(x->cand, x->prev, sizeof *x->cand * (size_t)x->nprev), nc = x->nprev;
    else
        for (int i = 0; i < x->n; i++) x->cand[nc++] = i;
    Rank *h = xmalloc(sizeof *h * (size_t)(top > 0 ? top : 1));
    int nh = 0, nm = 0, nf = 0;
    /* Substring tiers first; the fuzzy ones are scored only when fewer than
     * `top` matched better, as they can't make the list otherwise. */
    for (int pass = 0; pass < 2; pass++) {
        int nk = pass ? nf : nc;
        if (pass && nh >= top) break;
        for (int k = 0; k < nk; k++) {
            int i = x->cand[k];
            SsName m;
            if (!match(x->raw + x->at[i], x->fold + x->at[i], x->len[i], x->bs[i], &qq, &x->sc, pass == 1, &m)) continue;
            if (!pass) x->prev[nm++] = i; /* in place: prev is read before it's written */
            if (!pass && m.tier == SS_FUZZY && !m.npos) { /* unscored: for the second pass */
                x->cand[nf++] = i; /* cand[k] read already */
                continue;
            }
            Rank r = {i, m.tier, m.score};
            if (nh < top) {
                h[nh++] = r;
                for (int j = nh - 1; j > 0 && better(x, h[(j - 1) / 2], h[j]); j = (j - 1) / 2) {
                    Rank t = h[j];
                    h[j] = h[(j - 1) / 2], h[(j - 1) / 2] = t;
                }
            } else if (top > 0 && better(x, r, h[0])) {
                h[0] = r;
                sift(x, h, nh, 0);
            }
        }
    }
    qsort_r(h, (size_t)nh, sizeof *h, cmp_rank, x);
    for (int k = 0; k < nh; k++) out[k] = h[k].i, x->top[h[k].i] = 1;
    int o = nh;
    for (int k = 0; k < nm; k++) /* the rest, in path order */
        if (!x->top[x->prev[k]]) out[o++] = x->prev[k];
    for (int k = 0; k < nh; k++) x->top[h[k].i] = 0;
    free(h);
    x->nprev = nm, x->has_prev = qq.qn > 0;
    memcpy(x->last, qq.f, (size_t)qq.fb + 1);
    return nm;
}

/* ---- content ----------------------------------------------------------------- */

typedef struct {
    const char *q;
    size_t qn;
    bool cs, ascii;
    unsigned char lo[SS_QBYTES]; /* ascii, case-insensitive: q lowercased */
    uint32_t cp[SS_QBYTES];      /* otherwise case-insensitive: q's codepoints folded */
    int ncp;
} Pat;

static void pat_init(Pat *p, const char *q, bool cs) {
    memset(p, 0, sizeof *p);
    p->q = q, p->qn = strlen(q), p->cs = cs, p->ascii = true;
    if (p->qn >= SS_QBYTES) p->qn = SS_QBYTES - 1;
    for (size_t i = 0; i < p->qn; i++) {
        unsigned char c = (unsigned char)q[i];
        p->ascii &= c < 0x80;
        p->lo[i] = c >= 'A' && c <= 'Z' ? c + 32 : c;
    }
    for (size_t i = 0; !cs && !p->ascii && i < p->qn;) {
        int l;
        p->cp[p->ncp++] = fold(dec(q, p->qn, i, &l));
        i += (size_t)l;
    }
}

/* A match starting in t[from, smax) (it may end past smax): its offset, -1 none. */
static long pat_find(const Pat *p, const char *t, size_t n, size_t from, size_t smax, size_t *mlen) {
    if (!p->qn || from >= n) return -1;
    if (p->cs) {
        size_t end = smax + p->qn - 1 < n ? smax + p->qn - 1 : n;
        if (end <= from) return -1;
        const char *h = memmem(t + from, end - from, p->q, p->qn);
        *mlen = p->qn;
        return h ? h - t : -1;
    }
    if (p->ascii) {
        const unsigned char *u = (const unsigned char *)t, q0 = p->lo[0];
        for (size_t i = from; i < smax && i + p->qn <= n; i++) {
            unsigned char c = u[i];
            if ((c >= 'A' && c <= 'Z' ? c + 32 : c) != q0) continue;
            size_t k = 1;
            for (; k < p->qn; k++) {
                c = u[i + k];
                if ((c >= 'A' && c <= 'Z' ? c + 32 : c) != p->lo[k]) break;
            }
            if (k == p->qn) { *mlen = p->qn; return (long)i; }
        }
        return -1;
    }
    for (size_t i = from; i < smax && i < n; i++) {
        if (((unsigned char)t[i] & 0xC0) == 0x80) continue; /* inside a character */
        size_t j = i;
        int k = 0, l;
        for (; k < p->ncp && j < n; k++, j += (size_t)l)
            if (fold(dec(t, n, j, &l)) != p->cp[k]) break;
        if (k == p->ncp) { *mlen = j - i; return (long)i; }
    }
    return -1;
}

long ss_match(const char *t, size_t n, size_t from, const char *q, bool cs, size_t *mlen) {
    Pat p;
    pat_init(&p, q, cs);
    return pat_find(&p, t, n, from, n, mlen);
}

#define WINDOW 65536

int ss_find(const char *t, size_t n, const char *q, bool cs, SsLine *out, int max, const atomic_bool *stop) {
    Pat p;
    pat_init(&p, q, cs);
    if (!p.qn) return 0;
    size_t pos = 0, ls = 0, ml = 0;
    int line = 1, count = 0;
    while (pos < n) {
        if (stop && atomic_load(stop)) return -1;
        size_t smax = pos + WINDOW < n ? pos + WINDOW : n;
        long m = pat_find(&p, t, n, pos, smax, &ml);
        if (m < 0) { pos = smax; continue; }
        for (const char *nl; (nl = memchr(t + ls, '\n', (size_t)m - ls));) line++, ls = (size_t)(nl - t) + 1;
        if (count < max) out[count] = (SsLine){line, (int)((size_t)m - ls), (int)ml, ls};
        if (++count > max) break;
        const char *e = memchr(t + m, '\n', n - (size_t)m);
        if (!e) break;
        pos = ls = (size_t)(e - t) + 1;
        line++;
    }
    return count;
}

/* Back to the start of the character holding t[i]. */
static size_t char_start(const char *t, size_t lo, size_t i) {
    while (i > lo && ((unsigned char)t[i] & 0xC0) == 0x80) i--;
    return i;
}

void ss_snip(const char *t, size_t n, const SsLine *h, char *out, size_t cap, int *ms, int *ml) {
    size_t s = h->at, m0 = h->at + (size_t)h->col, m1 = m0 + (size_t)h->len;
    const char *nl = memchr(t + s, '\n', n - s);
    size_t e = nl ? (size_t)(nl - t) : n;
    if (e > s && t[e - 1] == '\r') e--;
    if (m1 > e) m1 = e;
    while (s < m0 && (t[s] == ' ' || t[s] == '\t')) s++;
    bool cut0 = false, cut1 = false;
    if (e - s > SS_SNIP) {
        if (m0 - s > 40) s = char_start(t, s, m0 - 40), cut0 = true;
        if (e - s > SS_SNIP) e = char_start(t, s, s + SS_SNIP), cut1 = true;
        if (m1 > e) m1 = e;
    }
    size_t o = 0;
    if (cut0) memcpy(out, "\xE2\x80\xA6", 3), o = 3;
    *ms = (int)(o + m0 - s), *ml = m1 > m0 ? (int)(m1 - m0) : 0;
    for (size_t i = s; i < e && o + 4 < cap; i++) {
        unsigned char c = (unsigned char)t[i];
        out[o++] = c < 0x20 || c == 0x7F ? ' ' : (char)c;
    }
    if (cut1 && o + 4 < cap) memcpy(out + o, "\xE2\x80\xA6", 3), o += 3;
    out[o] = 0;
}

/* ---- the worker -------------------------------------------------------------- */

struct SsEngine {
    pthread_t th;
    pthread_mutex_t mu;
    pthread_cond_t cv;
    atomic_bool stop;    /* the running search gives up */
    /* under mu */
    SsFile *files;
    int nfiles;
    char q[SS_QBYTES];
    bool cs, pending, running, quit, has_res;
    unsigned gen;
    SsResult res;
};

void ss_result_free(SsResult *r) {
    for (int i = 0; i < r->n; i++) free(r->hits[i].snip);
    free(r->hits);
    memset(r, 0, sizeof *r);
}

/* One search over the files; false when cancelled. */
static bool run(SsEngine *e, const char *q, bool cs, SsResult *r) {
    double t0 = now_wall();
    SsLine *lines = xmalloc(sizeof *lines * (SS_CAP + 1));
    int cap = 0;
    bool ok = true;
    for (int f = 0; f < e->nfiles && !r->capped; f++) {
        if (atomic_load(&e->stop)) { ok = false; break; }
        size_t len = e->files[f].len;
        const char *t = e->files[f].src;
        if (!t || ss_binary(t, len)) continue;
        r->files++;
        int room = SS_CAP - r->n, k = ss_find(t, len, q, cs, lines, room, &e->stop);
        if (k < 0) { ok = false; break; }
        for (int j = 0; j < k && j < room; j++) {
            if (r->n == cap) {
                cap = cap ? cap * 2 : 64;
                r->hits = xrealloc(r->hits, sizeof *r->hits * (size_t)cap);
            }
            char snip[SS_SNIP + 16];
            SsHit *h = &r->hits[r->n++];
            ss_snip(t, len, &lines[j], snip, sizeof snip, &h->ms, &h->ml);
            h->file = f, h->line = lines[j].line, h->col = lines[j].col, h->len = lines[j].len;
            h->snip = xstrdup(snip);
        }
        r->capped = k > room;
    }
    free(lines);
    r->ms = (now_wall() - t0) * 1e3;
    return ok;
}

static void *worker(void *arg) {
    SsEngine *e = arg;
    pthread_mutex_lock(&e->mu);
    for (;;) {
        while (!e->pending && !e->quit) pthread_cond_wait(&e->cv, &e->mu);
        if (e->quit) break;
        char q[SS_QBYTES];
        memcpy(q, e->q, sizeof q);
        bool cs = e->cs;
        unsigned gen = e->gen;
        e->pending = false, e->running = true;
        atomic_store(&e->stop, false);
        pthread_mutex_unlock(&e->mu);
        SsResult r = {.gen = gen};
        bool ok = run(e, q, cs, &r); /* the files change only while nothing runs */
        pthread_mutex_lock(&e->mu);
        e->running = false;
        pthread_cond_broadcast(&e->cv);
        if (ok && gen == e->gen) {
            ss_result_free(&e->res);
            e->res = r, e->has_res = true;
        } else ss_result_free(&r);
    }
    pthread_mutex_unlock(&e->mu);
    return NULL;
}

SsEngine *ss_engine_new(void) {
    SsEngine *e = xcalloc(1, sizeof *e);
    pthread_mutex_init(&e->mu, NULL);
    pthread_cond_init(&e->cv, NULL);
    atomic_init(&e->stop, false);
    if (pthread_create(&e->th, NULL, worker, e)) {
        perror("aigource: search thread");
        exit(1);
    }
    return e;
}

static void files_free(SsEngine *e) {
    free(e->files);
    e->files = NULL, e->nfiles = 0;
}

void ss_engine_free(SsEngine *e) {
    if (!e) return;
    pthread_mutex_lock(&e->mu);
    e->quit = true;
    atomic_store(&e->stop, true);
    pthread_cond_broadcast(&e->cv);
    pthread_mutex_unlock(&e->mu);
    pthread_join(e->th, NULL);
    files_free(e);
    ss_result_free(&e->res);
    pthread_cond_destroy(&e->cv);
    pthread_mutex_destroy(&e->mu);
    free(e);
}

void ss_engine_files(SsEngine *e, const SsFile *f, int n) {
    pthread_mutex_lock(&e->mu);
    atomic_store(&e->stop, true);
    e->pending = false;
    while (e->running) pthread_cond_wait(&e->cv, &e->mu);
    files_free(e);
    e->files = xmalloc(sizeof *e->files * (size_t)(n ? n : 1));
    if (n) memcpy(e->files, f, sizeof *f * (size_t)n);
    e->nfiles = n;
    ss_result_free(&e->res);
    e->has_res = false;
    e->gen++;
    pthread_mutex_unlock(&e->mu);
}

unsigned ss_engine_search(SsEngine *e, const char *q, bool cs) {
    pthread_mutex_lock(&e->mu);
    atomic_store(&e->stop, true); /* the running one, if any */
    snprintf(e->q, sizeof e->q, "%s", q);
    e->cs = cs;
    e->pending = true;
    unsigned gen = ++e->gen;
    pthread_cond_broadcast(&e->cv);
    pthread_mutex_unlock(&e->mu);
    return gen;
}

void ss_engine_cancel(SsEngine *e) {
    pthread_mutex_lock(&e->mu);
    atomic_store(&e->stop, true);
    e->pending = false;
    e->gen++;
    ss_result_free(&e->res);
    e->has_res = false;
    pthread_mutex_unlock(&e->mu);
}

bool ss_engine_poll(SsEngine *e, SsResult *r) {
    pthread_mutex_lock(&e->mu);
    bool has = e->has_res;
    if (has) *r = e->res, memset(&e->res, 0, sizeof e->res), e->has_res = false;
    pthread_mutex_unlock(&e->mu);
    return has;
}

bool ss_engine_busy(SsEngine *e) {
    pthread_mutex_lock(&e->mu);
    bool busy = e->pending || e->running;
    pthread_mutex_unlock(&e->mu);
    return busy;
}
