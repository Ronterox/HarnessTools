#ifndef AIG_METRICS_H
#define AIG_METRICS_H
#include <stdbool.h>
#include <stdio.h>

#include "analysis.h"
#include "util.h"

/* What the structure view shows, derived from an AnSnapshot: a port of the
 * data side of aigource/draft/structure.tpl.html (prep, the per-node loop,
 * METRIC/heat, slow, partners, TINFO, turnBar), per METHODS.md. Pure; built
 * on the worker thread right after an_build, so drawing only reads it.
 *
 * Nodes are the page's: folder chains merged ("a/b"), children by size
 * (d3.hierarchy().sum(own).sort, stable), stored breadth-first as
 * d3's descendants(), so nodes[0] is the root and parents come before
 * children. Turn indices and change ids are the snapshot's.
 *
 * Lifetime: the metrics keep pointers into their snapshot (snap, tool and
 * lang strings), so the snapshot must outlive every read of the metrics. On a
 * swap, free the old metrics first, then their snapshot (st_free_pair). */

enum { ST_TURNS, ST_LINES, ST_RISK, ST_REWORK, ST_FIXES, ST_CONTEXT, ST_COST, ST_TIME, ST_AGE, ST_OWNER, ST_NMODES };
enum { ST_LV_DIR, ST_LV_FILE, ST_LV_DEF };              /* heat levels */
enum { STK_CORR = 1, STK_TEST = 2, STK_BLIND = 4, STK_SLOW = 8 }; /* flagKinds: the node's dots */
enum { STT_UNTESTED = 1, STT_FAILING = 2, STT_BLIND = 4 };        /* a node's changes in a turn */
enum { ST_AGE_HOUR, ST_AGE_DAY, ST_AGE_WEEK, ST_AGE_OLDER, ST_AGE_UNKNOWN, ST_NAGES }; /* < 1 h, < 1 day, < 1 week */
#define ST_SLOW_X 2
#define ST_REWORK_TURNS 2
#define ST_TOP_ROWS 8      /* read rows, partners */
#define ST_RW_PAIRS 4
#define ST_ACTOR_COLORS 8  /* owner colours repeat after these */
#define ST_BLAME_COLORS 8  /* blame legend colours repeat after these */

typedef struct { int path, n, node; } StRead;          /* path (snapshot index) read n times; node: its file, -1 */
typedef struct { int actor, lines; } StWho;            /* actor -1: before these transcripts */
typedef struct { int node, shared, pct; double degree; bool cross; } StPartner;
typedef struct { int cid, lines, cx; } StPoint;        /* cid -1: today ("now") */
typedef struct { int turn, n; } StBlame;               /* legend row: n lines by turn (-1 unknown) */
typedef struct { int to, by, n; } StRwPair;            /* turn `by` rewrote n of the lines turn `to` wrote */
typedef struct { int node, add, del; } StTouch;

typedef struct {       /* the node's changes in one turn (inspector "Turns") */
    int turn, changes, add, del, flags; /* STT_* */
    int *notes, nnotes;                 /* file/def: comments with text, as AnChange.comment (distinct, in order) */
    const char **tools;                 /* distinct, in order */
    int ntools;
} StNodeTurn;

typedef struct {
    int kind;          /* AN_DIR, AN_FILE, AN_FUNC, AN_CLASS */
    int level;         /* ST_LV_* */
    char *name;        /* a merged folder chain: "a/b/c" */
    char *id;          /* path of names from the root's, '/'-joined (the page's data-id) */
    int an;            /* snapshot node with its data (a chain's last folder) */
    int parent, *kids, nkids, depth;
    int own;           /* size of its own (d3 sum accessor): lines not in its children */
    int value;         /* own plus its children's values */
    int lines, cx, nest;  /* nest: a def's nesting depth */
    int a, b;          /* def: first and last line (1-based) in its file */
    int file;          /* the file node it's in (a file: itself), -1 for folders */
    const char *lang;  /* file/def: its file's grammar name ("" unknown); NULL for folders */
    int turns, edits, add, del, ncids;
    int rework, rework_same, rework_pct;
    StRwPair rw_pairs[ST_RW_PAIRS];
    int nrw_pairs;
    int fix_turns;
    int ctx_turns;     /* turns with a first change here */
    double ctx_avg, ctx_reads, ctx_searches; /* per such turn, before that change */
    StRead *ctx_rows;  /* "read to change it": top ST_TOP_ROWS */
    int nctx_rows;
    long long tok[4];  /* output, new input, cached input, seconds (defs: line share) */
    double per_turn;   /* tok[3] / turns */
    StWho *who;        /* lines by agent: known first, most lines first */
    int nwho, owner;   /* owner -1: none known */
    int owner_slot;    /* st_actor_color(owner) */
    long long age_median; /* seconds before the last change, -1: no known line */
    int ages[ST_NAGES];
    struct { int corrected, interrupted, untested, failing, blind, slow; } flags;
    int corr_turns[2], ncorr_turns; /* the first two corrected turns, in change order (the flags section) */
    int kinds;         /* STK_* */
    double metric[ST_NMODES], heat[ST_NMODES]; /* heat 0..1; owner's is turns' (it's a colour, st_actor_color) */
    StPartner *partners;
    int npartners;
    StPoint *trend;    /* file/def: after each change, plus "now" when today differs */
    int ntrend;
    bool growing;
    StBlame *blame;    /* file/def legend: newest turn first, unknown last; colour = index % ST_BLAME_COLORS */
    int nblame;
    StNodeTurn *rows;  /* newest first: its turn set */
    int nrows;
} StNode;

typedef struct {       /* TINFO, inspectTurn and the turn strip */
    int rank;          /* position in time order */
    int *cids, ncids;  /* ascending */
    StTouch *files, *defs; /* most +/- first */
    int nfiles, ndefs;
    int add, del, kept, survival_pct;
    int *nodes, nnodes; /* nodes it touched and their ancestors, ascending (the rest dims) */
    StRead *reads;     /* top ST_TOP_ROWS */
    int nreads;
    int *notes, nnotes; /* comments in order, as AnChange.comment */
    int untested, failing; /* changes */
    bool tested, blind, slow;
    double slow_x;     /* secs / turn_avg */
    long long tok[3], on_changes; /* on_changes: output tokens that led up to its changes */
    /* led_pct: on_changes of tok[0], at most 100 (a turn can lack its own count: tok[0] 0) */
    int led_pct, nagents;
    bool subagents;
    double bar_h, bar_w; /* turnBar: px height, flex-grow width */
} StTurn;

typedef struct StMetrics {
    const AnSnapshot *snap; /* what it was built from; see Lifetime above */
    StNode *nodes;
    int nnodes;
    int *of_an;        /* snapshot node -> display node (chain folders -> the chain) */
    int nan;
    StTurn *turns;
    int nturns;
    int *order;        /* turns in time order (ORDER) */
    long long now, first, age_span; /* last and first change, max(1 h, span) */
    double avg_time[3]; /* per level: mean per_turn of nodes with turns (ST_LV_DIR unused, 0) */
    double turn_avg;
    double max[3][ST_NMODES]; /* per level, at least 1 */
    int *actors, nactors; /* known agents by lines owned: colour = position % ST_ACTOR_COLORS */
    int bar_big;       /* most +/- of a turn, at least 1 */
    StrMap by_id, by_rel; /* st_find's: id -> node, id below the root -> node (first wins) */
} StMetrics;

StMetrics *st_build(const AnSnapshot *s);
void st_free(StMetrics *m);
/* Frees m, then its snapshot s (either may be NULL). */
void st_free_pair(StMetrics *m, AnSnapshot *s);
/* A node by its id ("root/a/b.c/fn") or its path below the root ("a/b.c/fn"); -1 none. */
int st_find(const StMetrics *m, const char *id_or_rel);
/* Today's lines of a file or def, each the change that wrote it (-1 unknown):
 * *n of them, lines a..b for a def. Caller frees; NULL (*n 0) for a folder. */
int *st_line_origins(const StMetrics *m, int node, int *n);
/* The blame legend colour slot of a turn in this node's legend (index into
 * ST_BLAME_COLORS), -1 for the unknown row or a turn not in it. */
int st_blame_slot(const StNode *d, int turn);
/* The turn touched this node or something inside it (T->nodes, bsearch). */
bool st_turn_touches(const StTurn *T, int node);
/* Colour slot of an agent (index into ST_ACTOR_COLORS), -1 for unknown/none. */
int st_actor_color(const StMetrics *m, int actor);
void st_json(FILE *f, const StMetrics *m);

#endif
