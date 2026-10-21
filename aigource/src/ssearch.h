#ifndef AIG_SSEARCH_H
#define AIG_SSEARCH_H
#include <stdatomic.h>
#include <stdbool.h>
#include <stddef.h>

/* Search on the structure screen: node paths by name, ranked like a file
 * finder, and file texts for a literal query. Pure matching and ranking, plus
 * a worker thread that runs content searches off the frame (SsEngine). No
 * raylib: tests/test_ssearch.c checks it headless.
 *
 * Case folds ASCII and the cased letters of Latin-1, Greek and Cyrillic
 * (one codepoint each way, same byte length); text is UTF-8, and bytes that
 * aren't valid UTF-8 match only themselves. */

#define SS_QMAX 64       /* query characters that count; the rest is ignored */
#define SS_QBYTES 256    /* a query's bytes, NUL included */
#define SS_CAP 500       /* content hits (lines) kept; more says "500+" */
#define SS_SNIP 160      /* bytes of a hit's line kept for display */

/* ---- names ---- */

enum { SS_EXACT, SS_PREFIX, SS_BASE, SS_PATH, SS_FUZZY }; /* SsName.tier, best first */

typedef struct {
    int tier;            /* SS_* */
    int score;           /* fuzzy: higher is better (contiguous, word starts); 0 otherwise */
    int npos;            /* matched characters */
    int pos[SS_QMAX];    /* each one's first byte in the path, ascending */
} SsName;

/* q against path (a node's path below the root; its basename is after the
 * last '/'), case-insensitive: exact basename, basename prefix, basename
 * substring, path substring, else a fuzzy subsequence of the path. False
 * when none (or q is empty). */
bool ss_name(const char *q, const char *path, SsName *m);
/* Ranking: tier, then score (higher first), then the shorter path, then the path's bytes. */
int ss_name_cmp(const SsName *a, const char *pa, const SsName *b, const char *pb);

/* A build's paths (copied), folded once, for ss_name's matching over all of
 * them: a byte test first (the query's folded bytes in order), the fuzzy
 * tier's tables reused, and a query extending the last one looks only among
 * the last one's matches. */
typedef struct SsNames SsNames;
SsNames *ss_names_new(const char *const *paths, int n);
void ss_names_free(SsNames *x);
/* Every path q matches, as indices into out (room for all n): the best
 * `top` first in ss_name_cmp's order, then the rest in path order. Returns
 * how many. */
int ss_names_find(SsNames *x, const char *q, int *out, int top);
/* Path i against q, for its highlights (ss_name's). */
bool ss_names_match(SsNames *x, const char *q, int i, SsName *m);

/* ---- content ---- */

/* Smart case: case-sensitive when q holds an uppercase letter. */
bool ss_smart_case(const char *q);
/* A NUL in the first 8000 bytes, as git and the analysis tell binaries. */
bool ss_binary(const char *t, size_t n);
/* The first match of q in t[from, n) not crossing a newline: its offset,
 * its length in t into *mlen; -1 none. cs: case-sensitive. */
long ss_match(const char *t, size_t n, size_t from, const char *q, bool cs, size_t *mlen);

typedef struct {
    int line;            /* 1-based */
    int col, len;        /* the first match in the line: byte offset in it, bytes */
    size_t at;           /* the line's first byte in the text */
} SsLine;

/* Every line of t[0, n) holding q, in order (one per line, its first
 * match): at most max into out. Returns how many lines hold it, counting
 * stops at max + 1 (so more than max means capped); -1 when stop became
 * true midway (checked every 64 KB). Lines end at '\n'; a '\r' before it
 * isn't part of the line; a last line without '\n' counts. */
int ss_find(const char *t, size_t n, const char *q, bool cs, SsLine *out, int max, const atomic_bool *stop);
/* A hit's line for display: leading blanks trimmed, '\r' dropped, tabs as
 * spaces, at most SS_SNIP bytes around the match ("…" where cut) into out
 * (cap bytes at least SS_SNIP + 8); the match's place in it into *ms, *ml. */
void ss_snip(const char *t, size_t n, const SsLine *h, char *out, size_t cap, int *ms, int *ml);

/* ---- the worker ---- */

typedef struct {
    int file;            /* index into the files given */
    int line, col, len;  /* as SsLine */
    char *snip;          /* ss_snip's */
    int ms, ml;
} SsHit;

typedef struct {
    unsigned gen;        /* ss_engine_search's */
    SsHit *hits;
    int n;
    bool capped;         /* more than SS_CAP lines hold it */
    int files;           /* files searched */
    double ms;           /* how long it took */
} SsResult;

typedef struct {
    const char *src;     /* a file's text, borrowed: valid until the next ss_engine_files */
    size_t len;
} SsFile;

typedef struct SsEngine SsEngine;

/* A text that looks binary (ss_binary) is skipped. */
SsEngine *ss_engine_new(void);
/* Stops the worker and frees everything (the files given are the caller's). */
void ss_engine_free(SsEngine *e);
/* The files to search, in this order (texts borrowed). Cancels a
 * running search and waits for it to let go of the old files (it checks
 * every 64 KB), drops a pending one and an unread result. */
void ss_engine_files(SsEngine *e, const SsFile *f, int n);
/* Search for q (copied): a newer search cancels an older one, whose result
 * never shows. Returns its gen. */
unsigned ss_engine_search(SsEngine *e, const char *q, bool cs);
/* Drops a running or pending search: nothing comes of it. */
void ss_engine_cancel(SsEngine *e);
/* The newest search's result, once: true and *r filled (the caller's to free). */
bool ss_engine_poll(SsEngine *e, SsResult *r);
/* A search is pending or running. */
bool ss_engine_busy(SsEngine *e);
void ss_result_free(SsResult *r);

#endif
