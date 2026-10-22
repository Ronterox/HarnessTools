#ifndef AIG_CONTEXT_H
#define AIG_CONTEXT_H
#include <stdatomic.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

/* The context view's data (docs/CONTEXT.md): what one agent, a session's main
 * agent or one of its subagents, had in its context window at every API call,
 * rebuilt from its transcript. A port of draft/context/build_ctx.py for one
 * agent, plus what the draft page derives from it (prepare(): tokens scaled
 * to real usage, cache state, carrying cost).
 *
 * ctx_load reads the agent's transcript on demand, not through the event
 * stream (request blobs are large and only the open agent needs them), and
 * the project's other transcripts for the changes that make reads stale (and,
 * rarely, a blob only another file has). Those changes are kept in a
 * CtxProject between loads: a load rescans only the files that changed, from
 * where the last scan stopped when a file only grew. It touches no other
 * shared state, so it may run on a worker thread; the CtxAgent it returns is
 * immutable, owned by the caller, and doesn't depend on the project. Texts
 * are shared: every item's text is one string of CtxAgent.str, however many
 * calls hold it. Numbers are read and written in the "C" locale whatever the
 * app's (the entry points switch their thread's numeric locale).
 *
 * Actors: the stage's actor id is the transcript's name. A main session is
 * <projects>/<slug>/<session id>.jsonl, a subagent
 * <projects>/<slug>/<session id>/subagents/agent-<id>.jsonl with its
 * agent-<id>.meta.json beside it; ctx_find turns an id into that path. */

typedef enum {
    CK_SYSTEM,   /* a system prompt block */
    CK_TOOLS,    /* the tool definitions (one item per request shape) */
    CK_INJECTED, /* text Claude Code adds: system reminders, CLAUDE.md, memory, hook output, ... */
    CK_PROMPT,   /* typed or pasted by the human (or an image) */
    CK_SUMMARY,  /* a compaction summary */
    CK_TEXT,     /* the agent's text */
    CK_THINKING, /* its thinking (text empty when only the signature was recorded) */
    CK_ACTION,   /* its tool call; text = the input as JSON (indent 1) */
    CK_RESULT,   /* a tool result */
    CK_MISSING,  /* a message or request shape found in no transcript */
    CK_COUNT
} CtxKind;
extern const char *const ctx_kind_names[CK_COUNT]; /* "system", "tools", "injected", ... as the draft names them */

enum { CS_READ, CS_WRITTEN, CS_NEW, CS_NONE }; /* cache state; NONE: the call has no recorded response */

/* Why a read is stale: the change's author. */
enum { CST_OWN_EDIT, CST_OWN_CMD, CST_SESSION, CST_PROJECT, CST_NOTICED };

typedef struct {
    double t;          /* epoch of the change */
    int kind;          /* CST_* */
    int item;          /* own Edit/Write/command: its action item; else -1 */
    const char *label; /* "Edit src/a.c", "a.c: Bash sed -i ...", ... */
} CtxStale;

typedef struct {
    int item;        /* an item in the context of the call that made the action */
    const char *why; /* "read of a.c", "the prompt of this turn", "mentions foo, bar", ... */
} CtxLink;

typedef struct {
    CtxKind kind;
    const char *src;  /* source: "Read src/a.c 1-40", "Bash make test", "system reminder", "prompt", ... */
    const char *text; /* exact content (CtxAgent.str[x]): a Read's numbered lines, a command's output, an
                       * action's input as JSON, a prompt; "" for CK_TOOLS (see tx) */
    size_t len;       /* bytes of text */
    int x;            /* index of text in CtxAgent.str */
    int n;            /* characters (code points) of text; CK_TOOLS: of all tool definitions */
    double w;         /* size estimate before scaling, rounded to 0.1 as the draft writes it */
    int made;         /* the agent's own blocks: the call whose response made it; -1 */
    const char *tool; /* an action and its result: the tool's name; else NULL */
    const char *path; /* a file the action names (file_path / notebook_path), repo-relative or ~/...; NULL */
    const char *lang; /* the language of path ("c", "python", ... as highlight.js names it); NULL */
    const char *cmd;  /* Bash action and its result: the command; NULL */
    const char *tuid; /* action, result: the tool_use id */
    int err;          /* a result marked is_error */
    int enter, leave; /* the call it entered, the call it left (-1: still there at the end) */
    int action, result; /* a result's action item, an action's result item; -1 */
    int line0, line1;   /* a Read result: the first and last line number shown; 0 when not code */
    const char *old_str, *new_str; /* an Edit action's old_string, new_string; NULL */
    const char *content; /* a Write action's content; NULL */
    const char *desc;    /* a Bash action's description; NULL */
    uint64_t key;        /* identity that survives reloads (an index doesn't): the message's hash and the
                          * block's place in it, the shape and the block's, ... (ctx_item_by_key) */
    int *tx, ntx;       /* CK_TOOLS: each tool's definition (JSON, indent 1) in CtxAgent.str */
    const char **names; /* CK_TOOLS: each tool's name */
    CtxStale *st;       /* reads (Read, read-only commands): changes to the file while it stayed in context */
    int nst;
    CtxLink *lk; /* actions: the items likely behind it */
    int nlk;
} CtxItem;

typedef struct {
    double t;          /* epoch of the request */
    const char *id;    /* the api-request id (estimated: the response's message id) */
    const char *model; /* the response's model, else the request's */
    const char *query; /* querySource; NULL */
    int base, keep;    /* this call's messages = call base's first keep + tail (base -1: tail only) */
    int *tail, ntail;  /* message instances (CtxAgent.inst) */
    int *shape, nshape; /* tools and system items */
    int has_u;
    long long u[5]; /* usage: input, cache read, cache write 5 min, cache write 1 h, output */
    int comp;       /* a compaction happened just before */
    int *drop, ndrop; /* thinking items the API dropped at this call (weight 0) */
    int *made, nmade; /* items its response made (they enter at a later call), in item order */
    /* derived, as the page's prepare(): */
    int *order, norder;   /* every item in the prompt: tools, system, messages */
    double wsum;          /* sum of the items' w (dropped ones excluded) */
    double f;             /* tokens per unit of w: total / wsum; a call without usage takes the nearest's */
    double total;         /* input + cache read + cache write (estimated without usage) */
    int noresp;           /* no response recorded (interrupted or failed) */
    unsigned char *state; /* per position of order: CS_* */
    float *cum;           /* per position: what holding the item has cost so far, in USD (0 without prices) */
} CtxCall;

typedef struct {
    int *items, n;
} CtxInst; /* one message as sent: its items in order */

typedef struct {
    const char *model;
    double in, read, w5, w1h, out; /* USD per million tokens */
} CtxPrice;
extern const CtxPrice ctx_prices[];
extern const int ctx_nprices;
extern const char ctx_price_source[];
const CtxPrice *ctx_price(const char *model); /* NULL: tokens only */
int ctx_window(const char *model);            /* context window in tokens, 0 when unknown */

typedef struct {
    char *path;    /* the transcript */
    char *session; /* the session id */
    char *id;      /* the actor id: the session id, or the subagent's id */
    char *kind;    /* "main" or "subagent" */
    char *name;    /* "main", or the subagent's agentType */
    char *desc, *tu, *model; /* a subagent's description, toolUseId, model (from its .meta.json); NULL */
    char *title;   /* the session's title */
    char *root;    /* the repo root paths are shown relative to */
    double t0;     /* the first call's time: times compare to 0.1 s relative to it, as the page's */
    int est;       /* no request records: estimated from the conversation */
    int missing;   /* messages found in no transcript of the project */
    int found_elsewhere; /* messages found only in another transcript of the project */
    int broken;    /* calls with keep > 0 whose base call is not in the file */
    CtxCall *calls;
    int ncalls;
    CtxInst *inst;
    int ninst;
    CtxItem *items;
    int nitems;
    char **str; /* texts, each NUL-terminated (a text may hold NULs: see slen) */
    size_t *slen;
    int nstr;
    void *priv; /* labels and other strings the items point into */
} CtxAgent;

/* The changes of a project's transcripts, cached between loads. One project
 * serves one root (a load with another clears it). Loads may share it from
 * several threads: its scan is locked. */
typedef struct CtxProject CtxProject;
CtxProject *ctx_project_new(void);
void ctx_project_free(CtxProject *p); /* no load may be using it */

/* Load an agent from its transcript. root: the repo (absolute) paths are
 * shown relative to. proj (may be NULL: a throwaway one): the change cache.
 * cancel (may be NULL): set it to give up; then NULL is returned without
 * *err. On failure NULL and *err (malloc'd). */
CtxAgent *ctx_load(const char *root, const char *path, CtxProject *proj, atomic_int *cancel, char **err);
/* The same agent again after its transcript grew (live): reads only the bytes
 * appended since prev was loaded, resuming from what prev keeps (a loader
 * state: no file text but a small window of it), and equals a fresh ctx_load
 * of the file (the same JSON, the same item keys). When what was appended
 * can't be applied to prev's build (the file rewritten or replaced, a request
 * needing a message only earlier records held, ...: docs/CONTEXT.md, 6a) it
 * is a full load. prev is not changed: it may still be shown, or reloaded
 * again (after a cancelled reload, say). */
CtxAgent *ctx_reload(const CtxAgent *prev, CtxProject *proj, atomic_int *cancel, char **err);
void ctx_free(CtxAgent *a);
int ctx_item_by_key(const CtxAgent *a, uint64_t key); /* the item with that key, -1 */

/* The transcript of agent: a path to one, or an actor id (a session id or a
 * subagent id, or a unique prefix of one) among the root's sessions in
 * $CLAUDE_CONFIG_DIR/projects (~/.claude by default). NULL and *err when none
 * or more than one matches. */
char *ctx_find(const char *root, const char *agent, char **err);

/* The agents of the session a transcript belongs to: its main transcript
 * first, then its subagents in path order. */
typedef struct {
    char *path, *id, *name, *desc;
    char *tu; /* a subagent: the parent's Agent tool_use id that started it (toolUseId); NULL */
} CtxRef;
int ctx_session_agents(const char *path, CtxRef **out);
void ctx_refs_free(CtxRef *r, int n);

/* At call c, the item at position pos of its order: tokens (0 when the API
 * dropped it). */
double ctx_tokens(const CtxAgent *a, int c, int pos);
int ctx_pos(const CtxAgent *a, int c, int item); /* position of item in call c's order, -1 (a scan) */
/* by_item[nitems]: each item's position in call c's order, -1 when not in it;
 * built once per call shown. */
void ctx_positions(const CtxAgent *a, int c, int *by_item);
/* An item's stale changes up to call c (count; *known: all of them the
 * agent's own, with the change still in its context). by_item: call c's
 * positions (ctx_positions), or NULL to look them up. */
int ctx_stale_at(const CtxAgent *a, int item, int c, const int *by_item, int *known);

/* A Read result's text as code: numbered lines ("  12→code") as they come,
 * the draft's rule: code when at least half the lines are numbered, up to the
 * first that isn't. Returns the lines (pointers into a malloc'd copy, *buf)
 * and their numbers; 0 when the text isn't code. *rest: the text after them. */
int ctx_code_lines(const CtxItem *it, char **buf, char ***lines, int **nums, const char **rest);

/* JSON shaped like the draft's data.json with this one agent: meta, one
 * session, S. at (may be NULL): calls to add the derived values for, as
 * "at": {call: [[tokens, state, carried], ...per position of its order]}. */
void ctx_json(FILE *f, const CtxAgent *a, const int *at, int nat);

#endif
