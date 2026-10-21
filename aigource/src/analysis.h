#ifndef AIG_ANALYSIS_H
#define AIG_ANALYSIS_H
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>

#include "event.h"
#include "transcript.h"

/* The structure view's data (aigource/draft/METHODS.md, sections 0-12): the
 * repo as it is today (folders, files, functions/classes), every change
 * replayed on the file as it was then so each line of today's code knows the
 * change that wrote it, and per change its turn, comment, tool, verification,
 * context, tokens and time. A port of draft/build_data.py.
 *
 * Events go in with an_add (copied; the caller keeps its own), each event
 * once: the ingest already drops resumed sessions' copies, and an event added
 * twice counts twice. an_build is a pure function of what was added, the
 * files on disk and git: it may run on a worker thread while more events are
 * added (an_add and an_build take turns on a lock), but an_free must not run
 * during a build, and builds of one Analysis run one at a time. Its snapshot
 * is immutable and owned by the caller. A build reuses what the ones before
 * it computed (caches the Analysis keeps), so a rebuild costs what changed;
 * the result is the same as a fresh Analysis'.
 * AIG_NO_DEFS in the environment (read by an_new) turns functions off;
 * AIG_DEBUG prints each build's phases and what it reused to stderr. */
typedef struct Analysis Analysis;

enum { AN_DIR, AN_FILE, AN_FUNC, AN_CLASS };           /* AnNode.kind */
enum { AN_UNTESTED, AN_PASSED, AN_FAILING };           /* AnChange.verify */

typedef struct { int cid, add, del, blind; } AnCh;     /* a change of a node: lines added, removed; changed unread */
typedef struct { int origin, cid, n; } AnRw;           /* change cid removed n lines change origin wrote */
typedef struct { int cid, lines, cx; } AnTr;           /* size (and complexity) right after change cid */
typedef struct { int n, origin; } AnRun;               /* blame: n lines written by change origin (-1 unknown) */

typedef struct {
    char *name;
    int kind;
    int parent;         /* node index, -1 for the root */
    int *kids, nkids;
    int lines;          /* file: its lines (nlines); def: b - a + 1 */
    int a, b;           /* def: first and last line */
    int cx, depth;      /* complexity (a file: the sum of its top-level defs'); def: nesting */
    char *lang;         /* file: grammar name, "" when unknown */
    AnCh *ch;
    int nch;
    AnRw *rw;
    int nrw;
    AnTr *tr;
    int ntr;
    AnRun *blame;       /* file: today's lines by origin, run-length */
    int nblame;
    char *src;          /* file: its text today (what blame runs over); not in the JSON */
    size_t srclen;
    long long size;     /* file: bytes on disk, and when it was last written */
    double mtime;
} AnNode;

typedef struct {
    char *title;        /* the prompt's first line (140 characters) */
    double ts;          /* the prompt's time */
    char *corr;         /* the next prompt when it reads like a correction, else NULL */
    bool intr, fix, has_tok;
    long long tok[3];   /* output, new input, cached input of its responses (has_tok) */
    int actor;          /* the session it ran in */
    long secs;          /* prompt to last response, gaps capped at 5 min */
} AnTurn;

typedef struct { int path, n; } AnRead;                /* a path (index) read n times */

typedef struct {
    int turn;
    int comment;        /* index, or -1 - index: none of its own, the turn's latest text */
    char *tool;
    int verify;         /* AN_UNTESTED, AN_PASSED, AN_FAILING */
    int reads, searches; /* by its agent in its turn before it */
    AnRead *top;        /* the 8 most read paths of those */
    int ntop;
    long long epoch;    /* its time, rounded */
    int actor;
    long long tok[3];   /* its agent's responses since its previous change in the turn */
    long secs;
} AnChange;

typedef struct {
    bool sub;           /* a subagent, else a main session */
    char *id;           /* first 8 characters of its id */
    char *name;         /* session: AI title or "session <id>"; subagent: its type */
    char *task;         /* subagent: its description */
} AnActor;

typedef struct AnSnapshot {
    AnNode *nodes;      /* nodes[0]: the root folder */
    int nnodes;
    AnTurn *turns;      /* only turns that changed a file exist */
    int nturns;
    char **comments;
    int ncomments;
    char **paths;
    int npaths;
    AnActor *actors;
    int nactors;
    AnChange *changes;
    int nchanges;
    /* Changes dropped as recorded twice, Edits without the text before them,
     * changes lined up with git, with a later text, placed nowhere (file level
     * only), changes of a function unread; files left out of the tree by the cap. */
    struct { int dups, no_text_before, from_git, lined_up_later, file_level_only, blind, cut; } stats;
} AnSnapshot;

Analysis *an_new(const char *root);
void an_add(Analysis *a, const Event *evs, size_t n);
/* NULL when cancelled. */
AnSnapshot *an_build(Analysis *a);
/* on: a running build (and any started until it's turned off) gives up soon
 * and returns NULL; checked between phases and per file. */
void an_cancel(Analysis *a, bool on);
void an_snapshot_free(AnSnapshot *s);
/* The snapshot as the draft's data.json, without sources. */
void an_snapshot_json(FILE *f, const AnSnapshot *s);
/* One line of counts, like build_data.py's stderr summary. */
void an_snapshot_stats(FILE *f, const AnSnapshot *s);
void an_free(Analysis *a);

/* ---- the rules, shared with the stage's per-turn stats (turns.c) ---- */

#define AN_IDLE_CAP 300.0 /* seconds: a longer gap between responses counts as this */
/* Active seconds over the stamps (made epoch-exact and sorted in place), each
 * gap capped at AN_IDLE_CAP, rounded half-even: a turn's or a change's time. */
long an_active(double *xs, int n);
/* The prompt reports a problem: its turn is a fix. */
bool an_fix_prompt(const char *title);
/* The next prompt reads like a correction of this turn. */
bool an_corr_prompt(const char *next);
/* cmd runs a test or build of the repo at root (start: where it began, NULL
 * the root): a verification of the changes before it. */
bool an_verify_cmd(const char *cmd, const char *root, const char *start);

/* ---- internals, exposed for tests ---- */

/* For each line of b, the line of a it lines up with, -1 none: difflib's
 * matching blocks (longest match first, recursively), so origins carry over
 * exactly as build_data.py's do. Caller frees. */
int *an_line_map(char *const *a, int na, char *const *b, int nb);
/* hunks applied to old: the new text, NULL when check and a context or removed
 * line differs. With orig (one per line of old, as split on '\n'): *out_orig
 * gets the new text's (kept lines keep theirs, added lines get cid), *removed
 * the removed lines' (old line number, origin) pairs (*nremoved of them). */
char *an_apply(const char *old, const Hunk *h, int nh, bool check, const int *orig, int cid, int **out_orig,
               int **removed, int *nremoved);
/* text already reads as the hunks leave it, at their newStart lines. */
bool an_has_new(const char *text, const Hunk *h, int nh);
/* Each hunk's new side found in text, nearest its newStart: starts[i] (1-based),
 * false when one isn't there. */
bool an_relocate(const char *text, const Hunk *h, int nh, int *starts);

#endif
