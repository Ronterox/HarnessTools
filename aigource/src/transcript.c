#include "transcript.h"

#include <pthread.h>
#include <regex.h>
#include <stdlib.h>
#include <string.h>

#include <sys/stat.h>

#include "cmdguess.h"
#include "util.h"
#include "yyjson.h"

#define MAX_TEXT 120

typedef struct {
    char *id, *name, *path, *pattern, *cmd, *cwd; /* cmd, cwd: Bash, for guessing */
    int turn;
    double ts;
    long off; /* of the line holding the tool_use */
    long note_off;
    int notes;
} Pending; /* tool_use waiting for its tool_result */

struct Transcript {
    char *root;
    size_t rootlen;
    char *actor, *actor_name;
    EmitFn emit;
    void *ud;
    Pending *pend;
    int npend, cappend;
    int turn;
    char *last_prompt_parent;
    char *cwd; /* of the latest record: relative tool paths resolve against it */
    double t0; /* start of the tool call whose result is being handled */
    const char *src;
    long line_off;     /* offset of the line being fed */
    long ev_off;       /* tool_use line + id of the call events come from now */
    const char *ev_id;
    long ev_note_off;  /* ...and its comments */
    int ev_notes;
    int ev_via;        /* VIA_* of the events emitted now */
    long note_off;     /* text blocks since the last tool result or prompt */
    int notes;
    struct Failed { /* recent failed Bash calls, for the watcher */
        char *id;
        double start, end;
        long off, note_off;
        int notes, turn;
    } failed[16];
    int failed_head;
    char *usage_id;  /* the response whose parts are coming in: its message id, */
    int usage_out;   /* the largest output emitted for it */
    double usage_ts; /* and when it was first seen */
    char *title;     /* the last AI title emitted */
    double last_ts;  /* of the latest timestamped record */
};

int tr_failed_call(const Transcript *t, double ts, double slack, Event *e) {
    const struct Failed *best = NULL;
    for (int i = 0; i < 16; i++) {
        const struct Failed *f = &t->failed[i];
        if (f->id && ts >= f->start - slack && ts <= f->end + slack && (!best || f->start > best->start)) best = f;
    }
    if (!best) return 0;
    e->src = t->src;
    e->src_off = best->off;
    e->tool_id = xstrdup(best->id);
    e->note_off = best->note_off;
    e->notes = best->notes;
    e->turn = best->turn;
    e->t0 = best->start;
    e->ts = best->end;
    return 1;
}

const char *tr_actor(const Transcript *t) { return t->actor; }

static char *rel_path(Transcript *t, const char *p);

int tr_writing(Transcript *t, const char *rel) {
    for (int i = 0; i < t->npend; i++) {
        Pending *p = &t->pend[i];
        if (!p->path || !strcmp(p->name, "Read") || !strcmp(p->name, "Bash") || !strcmp(p->name, "Grep") ||
            !strcmp(p->name, "Glob"))
            continue;
        char *r = rel_path(t, p->path);
        int hit = r && !strcmp(r, rel);
        free(r);
        if (hit) return 1;
    }
    return 0;
}
const char *tr_actor_name(const Transcript *t) { return t->actor_name; }

int tr_bash_running(const Transcript *t, double since, double *start) {
    int found = 0;
    for (int i = 0; i < t->npend; i++) {
        if (strcmp(t->pend[i].name, "Bash") || t->pend[i].ts <= since) continue;
        if (!found || t->pend[i].ts < *start) *start = t->pend[i].ts;
        found = 1;
    }
    return found;
}

Transcript *tr_new(const char *root, const char *actor, const char *actor_name, const char *src,
                   EmitFn emit, void *ud) {
    Transcript *t = xcalloc(1, sizeof *t);
    t->src = src;
    t->root = xstrdup(root);
    t->rootlen = strlen(root);
    while (t->rootlen > 1 && t->root[t->rootlen - 1] == '/') t->root[--t->rootlen] = 0;
    t->actor = xstrdup(actor);
    t->actor_name = xstrdup(actor_name);
    t->emit = emit;
    t->ud = ud;
    return t;
}

static void pending_free(Pending *p) {
    free(p->id);
    free(p->name);
    free(p->path);
    free(p->pattern);
    free(p->cmd);
    free(p->cwd);
}

void tr_free(Transcript *t) {
    if (!t) return;
    for (int i = 0; i < t->npend; i++) pending_free(&t->pend[i]);
    free(t->pend);
    for (int i = 0; i < 16; i++) free(t->failed[i].id);
    free(t->root);
    free(t->actor);
    free(t->actor_name);
    free(t->last_prompt_parent);
    free(t->cwd);
    free(t->usage_id);
    free(t->title);
    free(t);
}

/* ---- json helpers ------------------------------------------------------- */

static const char *jstr(yyjson_val *o, const char *k) { return yyjson_get_str(yyjson_obj_get(o, k)); }

static void count_patch(yyjson_val *patch, int *add, int *del) {
    size_t i, n;
    yyjson_val *hunk;
    yyjson_arr_foreach(patch, i, n, hunk) {
        size_t j, m;
        yyjson_val *ln;
        yyjson_arr_foreach(yyjson_obj_get(hunk, "lines"), j, m, ln) {
            const char *s = yyjson_get_str(ln);
            if (s && *s == '+') (*add)++;
            else if (s && *s == '-') (*del)++;
        }
    }
}

static int str_lines(yyjson_val *v) {
    return yyjson_is_str(v) ? count_lines(yyjson_get_str(v), yyjson_get_len(v)) : -1;
}

/* Lexically normalize an absolute path in place: collapse "//", drop ".",
 * resolve "..". */
static void normalize(char *p) {
    char *out = p, *in = p;
    while (*in) {
        while (*in == '/') in++;
        char *seg = in;
        while (*in && *in != '/') in++;
        size_t n = (size_t)(in - seg);
        if (!n || (n == 1 && seg[0] == '.')) continue;
        if (n == 2 && seg[0] == '.' && seg[1] == '.') {
            while (out > p && *--out != '/') {}
            continue;
        }
        *out++ = '/';
        memmove(out, seg, n);
        out += n;
    }
    if (out == p) *out++ = '/';
    *out = 0;
}

/* A tool path made absolute: relative ones resolve against the session's cwd. */
static char *abs_path(Transcript *t, const char *p) {
    const char *base = *p == '/' ? "" : (t->cwd ? t->cwd : t->root);
    size_t bn = strlen(base), pn = strlen(p);
    char *abs = xmalloc(bn + pn + 2);
    memcpy(abs, base, bn);
    abs[bn] = '/';
    memcpy(abs + bn + 1, p, pn + 1);
    normalize(abs);
    return abs;
}

/* Tool path -> repo-relative ("" = root); NULL when outside the repo. Relative
 * paths resolve against the session's cwd. */
static char *rel_path(Transcript *t, const char *p) {
    if (!p || !*p) return NULL;
    char *abs = abs_path(t, p);
    char *rel = NULL;
    if (t->rootlen == 1 && t->root[0] == '/') rel = xstrdup(abs + 1);
    else if (!strncmp(abs, t->root, t->rootlen) && (abs[t->rootlen] == 0 || abs[t->rootlen] == '/'))
        rel = xstrdup(abs[t->rootlen] ? abs + t->rootlen + 1 : "");
    free(abs);
    return rel;
}

static Event mk(Transcript *t, EvKind kind, double ts, char *path, char *text, int add, int del, int lines) {
    return (Event){.ts = ts, .kind = kind, .actor = xstrdup(t->actor),
                   .actor_name = xstrdup(t->actor_name), .path = path, .text = text,
                   .add = add, .del = del, .lines = lines, .turn = t->turn, .t0 = t->t0,
                   .src = t->ev_id ? t->src : NULL, .src_off = t->ev_off,
                   .tool_id = t->ev_id ? xstrdup(t->ev_id) : NULL,
                   .note_off = t->ev_id ? t->ev_note_off : 0, .notes = t->ev_id ? t->ev_notes : 0,
                   .via = t->ev_via};
}

static void emit(Transcript *t, EvKind kind, double ts, char *path, char *text,
                 int add, int del, int lines) {
    Event e = mk(t, kind, ts, path, text, add, del, lines);
    t->emit(t->ud, &e);
}

/* ---- prompts ------------------------------------------------------------ */

/* A user record's text: the string, or its first text block. */
static const char *user_text(yyjson_val *content) {
    if (yyjson_is_str(content)) return yyjson_get_str(content);
    size_t i, n;
    yyjson_val *c;
    yyjson_arr_foreach(content, i, n, c) {
        const char *ct = jstr(c, "type");
        if (ct && !strcmp(ct, "text")) return jstr(c, "text");
    }
    return NULL;
}

/* The first non-empty line, cut at TR_PROMPT_MAX characters (no ellipsis: it's
 * matched against, e.g. "fix" keywords). */
static char *prompt_line(const char *s) {
    char *line = first_line(s, (size_t)-1);
    size_t i = 0;
    for (int n = 0; line[i] && n < TR_PROMPT_MAX; n++)
        do i++; while (((unsigned char)line[i] & 0xC0) == 0x80);
    line[i] = 0;
    return line;
}

/* Text of a prompt record and its PROMPT_* kind, or NULL when the record isn't one. */
static char *prompt_text(yyjson_val *rec, yyjson_val *content, int *kind) {
    if (yyjson_get_bool(yyjson_obj_get(rec, "isMeta")) ||
        yyjson_get_bool(yyjson_obj_get(rec, "isCompactSummary")))
        return NULL;
    const char *origin = jstr(yyjson_obj_get(rec, "origin"), "kind");
    if (origin && strcmp(origin, "human")) { /* harness-generated turn, e.g. a task finished */
        char buf[96];
        snprintf(buf, sizeof buf, "(%s)", origin);
        *kind = PROMPT_HARNESS;
        return xstrdup(buf);
    }
    const char *s = user_text(content);
    if (!s) return NULL;
    while (*s == ' ' || *s == '\n') s++;
    if (!strncmp(s, "<local-command", 14) || !strncmp(s, "<command-message>", 17) ||
        !strncmp(s, "[Request interrupted", 20) || !strncmp(s, "Caveat:", 7) || !*s)
        return NULL;
    const char *cmd = *s == '<' ? strstr(s, "<command-name>") : NULL;
    if (cmd) {
        cmd += 14;
        const char *end = strstr(cmd, "</command-name>");
        *kind = PROMPT_COMMAND;
        return end ? xstrndup(cmd, (size_t)(end - cmd)) : NULL;
    }
    *kind = PROMPT_HUMAN;
    if (!strncmp(s, "<pasted_content", 15)) {
        const char *gt = strchr(s, '>');
        if (gt) s = gt + 1;
        *kind = PROMPT_PASTED;
    }
    char *line = prompt_line(s);
    if (!*line) { free(line); return NULL; }
    return line;
}

/* ---- tools -------------------------------------------------------------- */

static int is_tracked_tool(const char *name) {
    static const char *names[] = {"Read", "Grep", "Glob", "Edit", "MultiEdit", "Write",
                                  "NotebookEdit", "Bash", NULL};
    for (int i = 0; names[i]; i++)
        if (!strcmp(name, names[i])) return 1;
    return 0;
}

/* "mcp__server__tool" -> "server: tool"; other names as they are. */
static char *tool_label(const char *name) {
    if (strncmp(name, "mcp__", 5)) return xstrdup(name);
    const char *srv = name + 5, *sep = strstr(srv, "__");
    if (!sep) return xstrdup(srv);
    char *s = xmalloc(strlen(srv) + 2);
    sprintf(s, "%.*s: %s", (int)(sep - srv), srv, sep + 2);
    return s;
}

/* A one-line gist of a tool's input: the first telling string field. */
static const char *tool_gist(yyjson_val *in) {
    static const char *keys[] = {"description", "query", "url", "prompt", "pattern", "command", "path",
                                 "file_path", "skill", "subject", "message", "name", NULL};
    for (int i = 0; keys[i]; i++)
        if (jstr(in, keys[i]) && *jstr(in, keys[i])) return jstr(in, keys[i]);
    size_t i, n;
    yyjson_val *k, *v;
    yyjson_obj_foreach(in, i, n, k, v) if (yyjson_is_str(v) && yyjson_get_len(v)) return yyjson_get_str(v);
    return "";
}

static void tool_start(Transcript *t, yyjson_val *c, double ts) {
    const char *name = jstr(c, "name"), *id = jstr(c, "id");
    if (!name || !id) return;
    yyjson_val *in = yyjson_obj_get(c, "input");
    if (!is_tracked_tool(name)) { /* shown as a step; no file effect */
        char *label = tool_label(name);
        char *gist = first_line(tool_gist(in), MAX_TEXT);
        char *text = xmalloc(strlen(label) + strlen(gist) + 3);
        sprintf(text, "%s%s%s", label, *gist ? "  " : "", gist);
        free(label);
        free(gist);
        t->ev_off = t->line_off;
        t->ev_id = id;
        t->ev_note_off = t->note_off;
        t->ev_notes = t->notes;
        emit(t, EV_TOOL, ts, NULL, text, 0, 0, -1);
        t->ev_id = NULL;
        return;
    }
    const char *path = jstr(in, "file_path");
    if (!path) path = jstr(in, "notebook_path");
    if (!path) path = jstr(in, "path");
    if (t->npend == t->cappend) {
        t->cappend = t->cappend ? t->cappend * 2 : 16;
        t->pend = xrealloc(t->pend, t->cappend * sizeof *t->pend);
    }
    Pending *p = &t->pend[t->npend++];
    p->id = xstrdup(id);
    p->name = xstrdup(name);
    p->path = path ? xstrdup(path) : NULL;
    p->pattern = jstr(in, "pattern") ? xstrdup(jstr(in, "pattern")) : NULL;
    p->cmd = !strcmp(name, "Bash") && jstr(in, "command") ? xstrdup(jstr(in, "command")) : NULL;
    p->cwd = p->cmd && t->cwd ? xstrdup(t->cwd) : NULL; /* the result's record has the cwd after it */
    p->turn = t->turn;
    p->ts = ts;
    p->off = t->line_off;
    p->note_off = t->note_off;
    p->notes = t->notes;
    if (!strcmp(name, "Bash")) {
        const char *cmd = jstr(in, "command");
        t->ev_off = t->line_off;
        t->ev_id = id;
        t->ev_note_off = t->note_off;
        t->ev_notes = t->notes;
        emit(t, EV_BASH, ts, NULL, first_line(cmd ? cmd : "", MAX_TEXT), 0, 0, -1);
        t->ev_id = NULL;
    }
}

/* Files a Bash command changed, as recorded by Claude Code in bashEditDiff. */
static void bash_files(Transcript *t, yyjson_val *diff, double ts) {
    StrMap seen = {0};
    size_t i, n;
    yyjson_val *f;
    yyjson_arr_foreach(yyjson_obj_get(diff, "files"), i, n, f) {
        const char *fp = jstr(f, "filePath");
        char *rel = rel_path(t, fp);
        if (!rel || !*rel) { free(rel); continue; }
        strmap_put(&seen, fp, 1);
        int add = 0, del = 0;
        count_patch(yyjson_obj_get(f, "hunks"), &add, &del);
        if (yyjson_get_bool(yyjson_obj_get(f, "deleted")))
            emit(t, EV_DELETE, ts, rel, NULL, add, del, 0);
        else if (yyjson_get_bool(yyjson_obj_get(f, "created")))
            emit(t, EV_CREATE, ts, rel, NULL, add, del, add);
        else
            emit(t, EV_EDIT, ts, rel, NULL, add, del, -1);
    }
    yyjson_arr_foreach(yyjson_obj_get(diff, "changedFiles"), i, n, f) {
        const char *fp = yyjson_get_str(f);
        if (!fp || strmap_get(&seen, fp) >= 0) continue;
        char *rel = rel_path(t, fp);
        if (!rel || !*rel) { free(rel); continue; }
        emit(t, EV_TOUCH, ts, rel, NULL, 0, 0, -1);
    }
    strmap_free(&seen);
}

/* ---- files from a command's text ---------------------------------------- */

typedef struct {
    Transcript *t;
    const char *cwd;
    double ts;
    bool reads; /* the files it read (a command that ran fine), else wrote */
    StrMap seen; /* repo path (and a read's range) -> index in rel/kind: the last action on a file wins */
    char **rel;
    GuessKind *kind;
    int (*range)[2];
    int n, cap;
} GuessCtx;

static char *cat3(const char *a, const char *b, const char *c) {
    char *s = xmalloc(strlen(a) + strlen(b) + strlen(c) + 1);
    sprintf(s, "%s%s%s", a, b, c);
    return s;
}

/* A file a command read, or a failed one probably wrote or removed: resolved
 * like the shell would from the session's cwd and the command's own cds; kept
 * when in the repo. Each range of a file read is a read of its own (once per
 * file and range per command). */
static void on_guess(void *ud, const char *dir, const char *path, GuessKind kind, const char *into, int first,
                     int last) {
    GuessCtx *g = ud;
    if ((kind == GUESS_READ) != g->reads) return;
    Transcript *t = g->t;
    const char *home = getenv("HOME");
    const char *cwd = g->cwd ? g->cwd : t->root;
    char *base, *abs;
    if (!dir) base = xstrdup(cwd);
    else if (dir[0] == '~' && home) base = cat3(home, "", dir + 1);
    else if (dir[0] == '/') base = xstrdup(dir);
    else base = cat3(cwd, "/", dir);
    if (path[0] == '~' && (path[1] == '/' || !path[1]) && home) abs = cat3(home, "", path + 1);
    else if (path[0] == '/') abs = xstrdup(path);
    else abs = cat3(base, "/", path);
    free(base);
    struct stat st;
    bool is_dir = !stat(abs, &st) && S_ISDIR(st.st_mode);
    if (into && (is_dir || abs[strlen(abs) - 1] == '/')) { /* mv/cp into a directory */
        char *in_dir = cat3(abs, abs[strlen(abs) - 1] == '/' ? "" : "/", into);
        free(abs);
        abs = in_dir;
        is_dir = !stat(abs, &st) && S_ISDIR(st.st_mode);
    }
    /* A read counts only for a file that's there: `cat f 2>/dev/null` probes for
     * one that may not be. "There" is now, not then: no beam for a file deleted
     * since, and a probe before a file was created reads it early. */
    bool gone = g->reads && (stat(abs, &st) || !S_ISREG(st.st_mode));
    char *rel = is_dir || gone ? NULL : rel_path(t, abs); /* a directory: a tree, not a file */
    if (!rel && g->reads && !is_dir && !gone) { /* a file outside the repo: its absolute path */
        normalize(abs);
        rel = abs;
        abs = NULL;
    }
    free(abs);
    if (!rel || !*rel) { free(rel); return; }
    char *key = g->reads ? xmalloc(strlen(rel) + 32) : rel;
    if (g->reads) sprintf(key, "%s\x1f%d\x1f%d", rel, first, last);
    int k = strmap_get(&g->seen, key);
    if (k >= 0) { /* touch x && rm x: it ends deleted */
        g->kind[k] = kind;
        if (key != rel) free(key);
        free(rel);
        return;
    }
    if (g->n == g->cap) {
        g->cap = g->cap ? g->cap * 2 : 8;
        g->rel = xrealloc(g->rel, (size_t)g->cap * sizeof *g->rel);
        g->kind = xrealloc(g->kind, (size_t)g->cap * sizeof *g->kind);
        g->range = xrealloc(g->range, (size_t)g->cap * sizeof *g->range);
    }
    strmap_put(&g->seen, key, g->n);
    if (key != rel) free(key);
    g->rel[g->n] = rel;
    g->range[g->n][0] = first, g->range[g->n][1] = last;
    g->kind[g->n++] = kind;
}

static void emit_guesses(GuessCtx *g) {
    Transcript *t = g->t;
    t->ev_via = g->reads ? VIA_CMD : VIA_GUESS;
    for (int i = 0; i < g->n; i++) {
        if (g->kind[i] != GUESS_READ) {
            emit(t, g->kind[i] == GUESS_DELETE ? EV_DELETE : EV_TOUCH, g->ts, g->rel[i], NULL, 0, 0,
                 g->kind[i] == GUESS_DELETE ? 0 : -1);
            continue;
        }
        Event e = mk(t, EV_READ, g->ts, g->rel[i], NULL, 0, 0, -1);
        e.range.first = g->range[i][0], e.range.last = g->range[i][1];
        t->emit(t->ud, &e);
    }
    t->ev_via = 0;
    free(g->rel);
    free(g->kind);
    free(g->range);
    strmap_free(&g->seen);
}

/* Claude Code records what a command changed only when it succeeds. For one
 * that ran and failed: flag the call, remember it for the watcher, guess its
 * files from its text. Rejected or hook-blocked calls never ran: left alone. */
static void bash_failed(Transcript *t, const Pending *p, yyjson_val *block, double ts) {
    yyjson_val *c = yyjson_obj_get(block, "content");
    const char *msg = yyjson_is_str(c) ? yyjson_get_str(c) : jstr(yyjson_arr_get_first(c), "text");
    if (!msg) return;
    if (!strncmp(msg, "Error: ", 7)) msg += 7;
    if (strncmp(msg, "Exit code ", 10) && strncmp(msg, "Command timed out", 17)) return;
    struct Failed *f = &t->failed[t->failed_head++ % 16];
    free(f->id);
    *f = (struct Failed){xstrdup(p->id), p->ts, ts, p->off, p->note_off, p->notes, p->turn};
    emit(t, EV_FAILED, ts, NULL, first_line(msg, MAX_TEXT), 0, 0, -1);
    if (!p->cmd) return;
    GuessCtx g = {.t = t, .cwd = p->cwd, .ts = ts};
    cmd_guess(p->cmd, on_guess, &g);
    emit_guesses(&g);
}

/* ---- a test run's status hidden by a pipe ------------------------------- */

/* Strong signs that a test or build failed, anchored where a tool prints them:
 * make's "***" and collect2 at a line's start, compiler errors at file:line[:col]
 * (fatal error also after "cc1: "), FAIL / FAILED, a Python traceback whose first
 * frame is a file (not <stdin> or <string>: an inline script, usually an edit),
 * a sanitizer error that stops the run (LeakSanitizer's exit-time report doesn't
 * count), a runtime error at file:line:col, a segfault or "(core dumped)",
 * "N failed" / "N errors" / "N failure(s)" (N > 0), make's "Error N" ending a
 * line (not "(ignored)"), "Exit code N" starting one, Neovim's E5108 / E5113 (a
 * -l script that aborted). Not weak words: "0 failed", "error" in a name, "no
 * errors". draft/build_data.py's FAILED_OUT is the same pattern. */
#define FAILED_OUT_ERE                                                                                   \
    "^make(\\[[0-9]+\\])?: \\*\\*\\* |:[0-9]+:([0-9]+:)? error:|(:[0-9]+:([0-9]+:)? |^[^ :]+: )fatal error:|" \
    "^collect2: error:|^(--- )?FAIL\\b|\\bFAILED\\b|Traceback \\(most recent call last\\):\n  File \"[^<]|"  \
    "ERROR: (Address|Memory|Thread|UndefinedBehavior)Sanitizer|:[0-9]+:[0-9]+: runtime error:|"            \
    "(^|[0-9] )Segmentation fault|\\(core dumped\\)|\\b[1-9][0-9]* (failed|errors?)\\b|\\bError [0-9]+$|"   \
    "^[Ee]xit (code|status) [1-9]|^E5(108|113): |\\b[1-9][0-9]* failures?\\b"

static regex_t verify_re, failed_re;
static pthread_once_t re_once = PTHREAD_ONCE_INIT;
static void re_init(void) {
    regcomp(&verify_re, CMD_VERIFY_ERE, REG_EXTENDED | REG_NOSUB);
    regcomp(&failed_re, FAILED_OUT_ERE, REG_EXTENDED | REG_NOSUB | REG_NEWLINE);
}

static bool says_failed(const char *s) { return s && !regexec(&failed_re, s, 0, NULL, 0); }

/* A Bash call not marked is_error whose test or build run was piped into
 * another command (`make | tail`, no pipefail before it): its status is the
 * pipe's end's, so its output decides. The output is toolUseResult's stdout
 * and stderr, else the result block's text. */
static bool failed_out(const Transcript *t, const Pending *p, yyjson_val *block, yyjson_val *res) {
    pthread_once(&re_once, re_init);
    if (!(cmd_runs(&verify_re, p->cmd, t->root, p->cwd) & CMD_PIPED)) return false;
    if (yyjson_is_str(yyjson_obj_get(res, "stdout")) || yyjson_is_str(yyjson_obj_get(res, "stderr")))
        return says_failed(jstr(res, "stdout")) || says_failed(jstr(res, "stderr"));
    yyjson_val *c = yyjson_obj_get(block, "content"), *b;
    if (yyjson_is_str(c)) return says_failed(yyjson_get_str(c));
    size_t i, n;
    yyjson_arr_foreach(c, i, n, b) if (says_failed(jstr(b, "text"))) return true;
    return false;
}

/* A Bash call that never ran: the user rejected it, a hook or Claude Code
 * blocked it (as recorded in this repo's transcripts), or it was interrupted
 * before it printed anything. draft/build_data.py's never_ran is the same. */
static bool never_ran(yyjson_val *block, yyjson_val *res, int err) {
    static const char *const no[] = {"The user doesn't want to proceed with this tool use",
                                     "The user doesn't want to take this action right now",
                                     "[Request interrupted by user for tool use]", "<tool_use_error>", "PreToolUse:",
                                     "Hook PreToolUse", "This session is isolated in the worktree", NULL};
    if (err) {
        yyjson_val *c = yyjson_obj_get(block, "content");
        const char *m = yyjson_is_str(c) ? yyjson_get_str(c) : jstr(yyjson_arr_get_first(c), "text");
        if (m && !strncmp(m, "Error: ", 7)) m += 7;
        for (int i = 0; m && no[i]; i++)
            if (!strncmp(m, no[i], strlen(no[i]))) return true;
    }
    const char *o = jstr(res, "stdout"), *e = jstr(res, "stderr");
    return yyjson_get_bool(yyjson_obj_get(res, "interrupted")) && (!o || !*o) && (!e || !*e);
}

/* A file tool call with no effect on the graph (failed, or outside the repo):
 * listed as a plain step, e.g. "Edit failed  /proj/a.py". */
static void plain_step(Transcript *t, const Pending *p, double ts, const char *note) {
    const char *what = p->path ? p->path : p->pattern ? p->pattern : "";
    char *gist = first_line(what, MAX_TEXT);
    char *text = xmalloc(strlen(p->name) + strlen(note) + strlen(gist) + 3);
    sprintf(text, "%s%s%s%s", p->name, note, *gist ? "  " : "", gist);
    free(gist);
    emit(t, EV_TOOL, ts, NULL, text, 0, 0, -1);
}

static void tool_finish(Transcript *t, yyjson_val *block, yyjson_val *res, double ts) {
    const char *id = jstr(block, "tool_use_id");
    int k = -1;
    for (int i = 0; id && i < t->npend; i++)
        if (!strcmp(t->pend[i].id, id)) { k = i; break; }
    if (k < 0) return;
    Pending p = t->pend[k];
    t->pend[k] = t->pend[--t->npend];
    int err = yyjson_get_bool(yyjson_obj_get(block, "is_error"));
    const char *name = p.name;
    t->t0 = p.ts;
    t->ev_off = p.off;
    t->ev_id = p.id;
    t->ev_note_off = p.note_off;
    t->ev_notes = p.notes;

    if (!strcmp(name, "Bash")) {
        /* path: where it ran when that isn't the repo root (relative to it, or absolute) */
        char *at = p.cwd ? rel_path(t, p.cwd) : NULL;
        if (!at && p.cwd) at = xstrdup(p.cwd);
        if (at && !*at) { free(at); at = NULL; }
        Event end = mk(t, EV_BASH_END, ts, at, xstrdup(p.cmd ? p.cmd : ""), 0, 0, -1);
        end.err = err;
        end.failed_out = !err && p.cmd && failed_out(t, &p, block, res);
        end.not_run = never_ran(block, res, err);
        t->emit(t->ud, &end);
        if (!err && p.cmd) { /* what it read (cat, sed -n, ...), before what it changed */
            GuessCtx g = {.t = t, .cwd = p.cwd, .ts = ts, .reads = true};
            cmd_guess(p.cmd, on_guess, &g);
            emit_guesses(&g);
        }
        /* A failing command may still have changed files: no error check. */
        yyjson_val *diff = yyjson_obj_get(res, "bashEditDiff");
        if (yyjson_is_obj(diff)) bash_files(t, diff, ts);
        else if (err) bash_failed(t, &p, block, ts);
    } else if (err) { /* no effect, but still a step to look at */
        plain_step(t, &p, ts, " failed");
    } else {
        char *rel = rel_path(t, p.path);
        if (!rel && p.path && *p.path && !strcmp(name, "Read")) rel = abs_path(t, p.path); /* outside the repo */
        if (!strcmp(name, "Grep") || !strcmp(name, "Glob")) {
            if (!p.path) rel = xstrdup("");
            if (rel) emit(t, EV_SEARCH, ts, rel, p.pattern ? first_line(p.pattern, MAX_TEXT) : NULL,
                          0, 0, -1);
            else plain_step(t, &p, ts, "");
        } else if (!rel || !*rel) { /* outside the repo: no node for it */
            free(rel);
            plain_step(t, &p, ts, "");
        } else if (!strcmp(name, "Read")) {
            yyjson_val *file = yyjson_obj_get(res, "file");
            yyjson_val *tl = yyjson_obj_get(file, "totalLines");
            Event e = mk(t, EV_READ, ts, rel, NULL, 0, 0, yyjson_is_int(tl) ? (int)yyjson_get_int(tl) : -1);
            const char *type = jstr(res, "type");
            if (res && (!type || strcmp(type, "text"))) e.range.first = -1; /* an image: no lines read */
            else if (yyjson_is_obj(file)) { /* no numLines: to the end */
                int first = (int)yyjson_get_int(yyjson_obj_get(file, "startLine"));
                int nl = (int)yyjson_get_int(yyjson_obj_get(file, "numLines"));
                e.range.first = first > 0 ? first : 1;
                e.range.last = nl > 0 ? e.range.first + nl - 1 : 0;
            }
            t->emit(t->ud, &e);
        } else if (!strcmp(name, "Write")) {
            const char *type = jstr(res, "type");
            int lines = str_lines(yyjson_obj_get(res, "content"));
            if (type && !strcmp(type, "create")) {
                emit(t, EV_CREATE, ts, rel, NULL, lines > 0 ? lines : 0, 0, lines);
            } else {
                int add = 0, del = 0;
                count_patch(yyjson_obj_get(res, "structuredPatch"), &add, &del);
                emit(t, EV_EDIT, ts, rel, NULL, add, del, lines);
            }
        } else { /* Edit, MultiEdit, NotebookEdit */
            int add = 0, del = 0;
            count_patch(yyjson_obj_get(res, "structuredPatch"), &add, &del);
            int orig = str_lines(yyjson_obj_get(res, "originalFile"));
            emit(t, EV_EDIT, ts, rel, NULL, add, del, orig >= 0 ? orig + add - del : -1);
        }
    }
    t->t0 = 0;
    t->ev_id = NULL;
    pending_free(&p);
}

/* Tool calls that never got a result (interrupted, killed) would pile up. */
static void prune_pending(Transcript *t) {
    for (int i = 0; i < t->npend;) {
        if (t->pend[i].turn < t->turn - 2) {
            pending_free(&t->pend[i]);
            t->pend[i] = t->pend[--t->npend];
        } else {
            i++;
        }
    }
}

/* ---- usage, titles ------------------------------------------------------ */

/* A response's tokens. Its parts each repeat them, output growing: emitted when
 * first seen and whenever the output grows, at the first part's time. */
static void usage(Transcript *t, yyjson_val *msg, double ts) {
    yyjson_val *u = yyjson_obj_get(msg, "usage");
    const char *id = jstr(msg, "id");
    if (!yyjson_is_obj(u) || !id) return;
    int out = yyjson_get_int(yyjson_obj_get(u, "output_tokens"));
    if (t->usage_id && !strcmp(t->usage_id, id)) {
        if (out <= t->usage_out) return;
    } else {
        free(t->usage_id);
        t->usage_id = xstrdup(id);
        t->usage_ts = ts;
    }
    t->usage_out = out;
    Event e = mk(t, EV_USAGE, t->usage_ts, NULL, NULL, 0, 0, -1);
    e.tool_id = xstrdup(id);
    e.tok.out = out;
    e.tok.in = yyjson_get_int(yyjson_obj_get(u, "input_tokens")) +
               yyjson_get_int(yyjson_obj_get(u, "cache_creation_input_tokens"));
    e.tok.cached = yyjson_get_int(yyjson_obj_get(u, "cache_read_input_tokens"));
    t->emit(t->ud, &e);
}

/* ai-title records come untimed and are rewritten often: emitted when changed. */
static void ai_title(Transcript *t, const char *title) {
    if (!title || (t->title && !strcmp(t->title, title))) return;
    free(t->title);
    t->title = xstrdup(title);
    emit(t, EV_TITLE, t->last_ts, NULL, xstrdup(title), 0, 0, -1);
}

/* ---- entry -------------------------------------------------------------- */

static int hex4(const char *s, unsigned *v) {
    *v = 0;
    for (int i = 0; i < 4; i++) {
        char c = s[i];
        unsigned d = c >= '0' && c <= '9' ? (unsigned)(c - '0')
                   : c >= 'a' && c <= 'f' ? (unsigned)(c - 'a' + 10)
                   : c >= 'A' && c <= 'F' ? (unsigned)(c - 'A' + 10) : 16;
        if (d == 16) return 0;
        *v = *v << 4 | d;
    }
    return 1;
}

/* Copy of line with unpaired \uD800-\uDFFF escapes replaced by \uFFFD (same
 * length). yyjson rejects them even with ALLOW_INVALID_UNICODE; JavaScript's
 * JSON.stringify writes them when tool output is cut mid-emoji. */
static char *fix_surrogates(const char *line, size_t len) {
    char *s = xstrndup(line, len);
    for (size_t i = 0; i + 1 < len; i++) {
        if (s[i] != '\\') continue;
        if (s[i + 1] != 'u') { i++; continue; } /* skip escaped char, e.g. \\ */
        unsigned v, w;
        if (i + 6 > len || !hex4(s + i + 2, &v)) continue;
        if (v >= 0xD800 && v <= 0xDBFF && i + 12 <= len && s[i + 6] == '\\' && s[i + 7] == 'u' &&
            hex4(s + i + 8, &w) && w >= 0xDC00 && w <= 0xDFFF) {
            i += 11; /* valid pair */
        } else if (v >= 0xD800 && v <= 0xDFFF) {
            memcpy(s + i + 2, "FFFD", 4);
            i += 5;
        } else {
            i += 5;
        }
    }
    return s;
}

/* Tool output cut mid-character leaves lone surrogates / bad UTF-8: keep the record. */
static yyjson_doc *parse_line(const char *line, size_t len) {
    yyjson_doc *doc = yyjson_read(line, len, YYJSON_READ_ALLOW_INVALID_UNICODE);
    if (!doc) {
        char *fixed = fix_surrogates(line, len);
        doc = yyjson_read(fixed, len, YYJSON_READ_ALLOW_INVALID_UNICODE);
        free(fixed); /* yyjson copies input unless INSITU */
    }
    return doc;
}

/* A text block worth showing as a comment: not just whitespace. */
static bool is_note(const char *s) {
    if (!s) return false;
    while (*s == ' ' || *s == '\n' || *s == '\t' || *s == '\r') s++;
    return *s;
}

/* A record after which earlier text is no comment on later calls: a tool
 * result, or a typed or pasted prompt. Task notifications, slash commands and
 * interrupts aren't: the agent's text before them still leads to its next call. */
static bool ends_notes(yyjson_val *rec) {
    const char *type = jstr(rec, "type");
    if (!type || strcmp(type, "user")) return false;
    yyjson_val *content = yyjson_obj_get(yyjson_obj_get(rec, "message"), "content");
    size_t i, n;
    yyjson_val *c;
    yyjson_arr_foreach(content, i, n, c) if (jstr(c, "type") && !strcmp(jstr(c, "type"), "tool_result")) return true;
    int kind = -1;
    char *text = prompt_text(rec, content, &kind);
    bool typed = text && (kind == PROMPT_HUMAN || kind == PROMPT_PASTED);
    free(text);
    return typed;
}

void tr_feed(Transcript *t, const char *line, size_t len, long off) {
    t->line_off = off;
    yyjson_doc *doc = parse_line(line, len);
    if (!doc) return;
    yyjson_val *rec = yyjson_doc_get_root(doc);
    const char *type = jstr(rec, "type");
    const char *cwd = jstr(rec, "cwd");
    if (cwd && *cwd == '/' && (!t->cwd || strcmp(cwd, t->cwd))) {
        free(t->cwd);
        t->cwd = xstrdup(cwd);
        normalize(t->cwd);
    }
    double ts = parse_iso8601(jstr(rec, "timestamp"));
    yyjson_val *content = yyjson_obj_get(yyjson_obj_get(rec, "message"), "content");
    /* A result or a prompt: what came before is about earlier calls (tr_notes
     * stops there too, timestamp or not). */
    if (ends_notes(rec)) t->notes = 0;
    if (ts) t->last_ts = ts;
    if (type && !strcmp(type, "ai-title")) ai_title(t, jstr(rec, "aiTitle"));
    if (!type || !ts) goto done;

    const char *sub = jstr(rec, "subtype");
    if ((!strcmp(type, "system") && sub && !strcmp(sub, "compact_boundary")) ||
        yyjson_get_bool(yyjson_obj_get(rec, "isCompactSummary"))) {
        emit(t, EV_COMPACT, ts, NULL, NULL, 0, 0, -1);
        t->notes = 0;
        goto done;
    }
    if (!strcmp(type, "assistant")) usage(t, yyjson_obj_get(rec, "message"), ts);
    if (!strcmp(type, "assistant") && yyjson_is_arr(content)) {
        size_t i, n;
        yyjson_val *c;
        yyjson_arr_foreach(content, i, n, c) {
            const char *ct = jstr(c, "type");
            if (ct && !strcmp(ct, "tool_use")) tool_start(t, c, ts);
            else if (ct && !strcmp(ct, "text") && is_note(jstr(c, "text"))) {
                if (!t->notes++) t->note_off = t->line_off; /* comments on the calls that follow */
            }
        }
    } else if (!strcmp(type, "user")) {
        int nresults = 0;
        if (yyjson_is_arr(content)) {
            size_t i, n;
            yyjson_val *c;
            yyjson_arr_foreach(content, i, n, c) {
                const char *ct = jstr(c, "type");
                nresults += ct && !strcmp(ct, "tool_result");
            }
            /* toolUseResult belongs to the record: only trust it for a lone result. */
            yyjson_val *res = nresults == 1 ? yyjson_obj_get(rec, "toolUseResult") : NULL;
            yyjson_arr_foreach(content, i, n, c) {
                const char *ct = jstr(c, "type");
                if (ct && !strcmp(ct, "tool_result")) tool_finish(t, c, res, ts);
            }
        }
        int had_result = nresults > 0;
        const char *said = had_result ? NULL : user_text(content);
        if (said && !strncmp(said, "[Request interrupted", 20)) {
            emit(t, EV_INTERRUPT, ts, NULL, first_line(said, MAX_TEXT), 0, 0, -1);
        } else if (!had_result) {
            int kind = 0;
            char *text = prompt_text(rec, content, &kind);
            if (text) {
                /* Same parent as the previous prompt: the human re-sent it, same turn. */
                const char *parent = jstr(rec, "parentUuid");
                int resent = t->turn && parent && t->last_prompt_parent &&
                             !strcmp(parent, t->last_prompt_parent);
                if (!resent) {
                    free(t->last_prompt_parent);
                    t->last_prompt_parent = parent ? xstrdup(parent) : NULL;
                    t->turn++;
                    prune_pending(t);
                }
                Event e = mk(t, EV_TURN, ts, NULL, text, 0, 0, -1);
                e.prompt = kind;
                t->emit(t->ud, &e);
            }
        }
    }
done:
    yyjson_doc_free(doc);
}

/* ---- detail view -------------------------------------------------------- */

#define DL_MAX 50000      /* lines in one detail */
#define DL_COLS 4000      /* bytes kept of one line */
#define SCAN_MAX (512L << 20) /* bytes searched for a call's result */

static void dl_add(Detail *d, DlKind kind, int no, const char *s, size_t len) {
    if (d->n > DL_MAX) return;
    if (d->n == DL_MAX) { kind = DL_DIM; no = 0; s = "\xE2\x80\xA6 (truncated)"; len = strlen(s); }
    if (d->n == d->cap) {
        d->cap = d->cap ? d->cap * 2 : 64;
        d->lines = xrealloc(d->lines, (size_t)d->cap * sizeof *d->lines);
    }
    if (len > DL_COLS) len = DL_COLS;
    size_t tabs = 0;
    for (size_t i = 0; i < len; i++) tabs += s[i] == '\t';
    char *o = xmalloc(len + tabs * 3 + 1), *p = o; /* tabs -> 4 spaces, no \r */
    for (size_t i = 0; i < len; i++) {
        if (s[i] == '\t') { memcpy(p, "    ", 4); p += 4; }
        else if (s[i] != '\r') *p++ = s[i];
    }
    *p = 0;
    d->lines[d->n++] = (DetailLine){kind, no, o};
}

/* Text split into lines; numbered from `no` when no > 0. */
static void dl_text(Detail *d, DlKind kind, const char *s, int no) {
    if (!s) return;
    size_t n = strlen(s);
    if (n && s[n - 1] == '\n') n--;
    for (const char *p = s, *end = s + n; p <= end;) {
        const char *nl = memchr(p, '\n', (size_t)(end - p));
        if (!nl) nl = end;
        dl_add(d, kind, no > 0 ? no++ : 0, p, (size_t)(nl - p));
        p = nl + 1;
    }
}

static void dl_fmt(Detail *d, DlKind kind, const char *fmt, const char *a) {
    char buf[512];
    snprintf(buf, sizeof buf, fmt, a);
    dl_add(d, kind, 0, buf, strlen(buf));
}

static void dl_gap(Detail *d) {
    if (d->n && d->lines[d->n - 1].s[0]) dl_add(d, DL_TEXT, 0, "", 0);
}

/* structuredPatch / bashEditDiff hunks: [{oldStart, newStart, lines: ["+x", "-y", " z"]}] */
static void dl_hunks(Detail *d, yyjson_val *hunks) {
    size_t i, n;
    yyjson_val *h;
    yyjson_arr_foreach(hunks, i, n, h) {
        int old = (int)yyjson_get_int(yyjson_obj_get(h, "oldStart"));
        int new = (int)yyjson_get_int(yyjson_obj_get(h, "newStart"));
        char buf[96];
        snprintf(buf, sizeof buf, "@@ -%d,%d +%d,%d @@", old, (int)yyjson_get_int(yyjson_obj_get(h, "oldLines")), new,
                 (int)yyjson_get_int(yyjson_obj_get(h, "newLines")));
        dl_add(d, DL_HUNK, 0, buf, strlen(buf));
        size_t j, m;
        yyjson_val *ln;
        yyjson_arr_foreach(yyjson_obj_get(h, "lines"), j, m, ln) {
            const char *s = yyjson_get_str(ln);
            if (!s) continue;
            size_t len = yyjson_get_len(ln);
            if (*s == '+') dl_add(d, DL_ADD, new++, s + 1, len - 1);
            else if (*s == '-') dl_add(d, DL_DEL, old++, s + 1, len - 1);
            else { dl_add(d, DL_TEXT, new++, len ? s + 1 : s, len ? len - 1 : 0); old++; }
        }
    }
}

/* Any JSON value under a label: strings as text, the rest as pretty JSON. */
static void dl_field(Detail *d, const char *key, yyjson_val *v) {
    if (yyjson_is_str(v) && !memchr(yyjson_get_str(v), '\n', yyjson_get_len(v))) {
        char *line = xmalloc(strlen(key) + yyjson_get_len(v) + 3);
        sprintf(line, "%s: %s", key, yyjson_get_str(v));
        dl_add(d, DL_TEXT, 0, line, strlen(line));
        free(line);
        return;
    }
    dl_fmt(d, DL_DIM, "%s:", key);
    if (yyjson_is_str(v)) { dl_text(d, DL_TEXT, yyjson_get_str(v), 0); return; }
    char *json = yyjson_val_write(v, YYJSON_WRITE_PRETTY_TWO_SPACES | YYJSON_WRITE_ALLOW_INVALID_UNICODE, NULL);
    dl_text(d, DL_TEXT, json, 0);
    free(json);
}

static void dl_input(Detail *d, yyjson_val *in, DlKind kind) {
    size_t i, n;
    yyjson_val *k, *v;
    int first = d->n;
    yyjson_obj_foreach(in, i, n, k, v) dl_field(d, yyjson_get_str(k), v);
    if (kind != DL_TEXT)
        for (int j = first; j < d->n; j++) d->lines[j].kind = kind;
}

/* The tool_result block's own content: a string or [{type: text|image, ...}]. */
static void dl_result_content(Detail *d, yyjson_val *block, DlKind kind) {
    yyjson_val *c = yyjson_obj_get(block, "content");
    if (yyjson_is_str(c)) { dl_text(d, kind, yyjson_get_str(c), 0); return; }
    size_t i, n;
    yyjson_val *b;
    yyjson_arr_foreach(c, i, n, b) {
        const char *type = jstr(b, "type");
        if (type && !strcmp(type, "text")) dl_text(d, kind, jstr(b, "text"), 0);
        else dl_fmt(d, DL_DIM, "[%s]", type ? type : "?");
    }
}

static bool ends_with_path(const char *abs, const char *rel) {
    size_t a = strlen(abs), r = strlen(rel);
    return a > r && abs[a - r - 1] == '/' && !strcmp(abs + a - r, rel);
}

static void detail_body(Detail *d, const char *name, yyjson_val *in, yyjson_val *block, yyjson_val *res,
                        const char *path) {
    bool err = block && yyjson_get_bool(yyjson_obj_get(block, "is_error"));
    DlKind out = err ? DL_ERR : DL_TEXT;
    yyjson_val *patch = yyjson_obj_get(res, "structuredPatch");

    if (!strcmp(name, "Bash")) {
        /* One file the command changed: its diff first, then the command. */
        size_t i, n;
        yyjson_val *f;
        yyjson_arr_foreach(yyjson_obj_get(yyjson_obj_get(res, "bashEditDiff"), "files"), i, n, f) {
            const char *fp = jstr(f, "filePath");
            if (!path || !fp || !ends_with_path(fp, path)) continue;
            dl_fmt(d, DL_DIM, "changes to %s, made by:", path);
            dl_hunks(d, yyjson_obj_get(f, "hunks"));
            dl_gap(d);
        }
        if (err && path && !yyjson_is_obj(yyjson_obj_get(res, "bashEditDiff"))) {
            dl_fmt(d, DL_ERR, "the command failed, so Claude Code didn't record its changes to %s", path);
            dl_gap(d);
        }
        const char *cmd = jstr(in, "command");
        char *pre = xmalloc(strlen(cmd ? cmd : "") + 3);
        sprintf(pre, "$ %s", cmd ? cmd : "");
        dl_text(d, DL_HEAD, pre, 0);
        free(pre);
        if (jstr(in, "description")) dl_fmt(d, DL_DIM, "# %s", jstr(in, "description"));
        if (!block) return;
        dl_gap(d);
        const char *so = jstr(res, "stdout"), *se = jstr(res, "stderr");
        if (res && (so || se)) {
            if (so && *so) dl_text(d, out, so, 0);
            if (se && *se) dl_text(d, DL_ERR, se, 0);
            if ((!so || !*so) && (!se || !*se)) dl_add(d, DL_DIM, 0, "(no output)", 11);
        } else {
            dl_result_content(d, block, out);
        }
        if (yyjson_get_bool(yyjson_obj_get(res, "interrupted"))) dl_add(d, DL_ERR, 0, "(interrupted)", 13);
        if (jstr(res, "backgroundTaskId")) dl_fmt(d, DL_DIM, "(running in the background: %s)", jstr(res, "backgroundTaskId"));
        return;
    }
    if (!strcmp(name, "Edit") || !strcmp(name, "MultiEdit") || !strcmp(name, "Write") || !strcmp(name, "NotebookEdit")) {
        const char *fp = jstr(in, "file_path") ? jstr(in, "file_path") : jstr(in, "notebook_path");
        if (fp) dl_fmt(d, DL_DIM, "%s", fp);
        if (!block) return;
        if (err) { dl_result_content(d, block, DL_ERR); return; }
        const char *type = jstr(res, "type");
        if (!strcmp(name, "Write") && type && !strcmp(type, "create")) {
            dl_text(d, DL_ADD, jstr(in, "content"), 1); /* the whole new file */
        } else if (!strcmp(name, "Write") && type && !yyjson_arr_size(patch)) {
            dl_add(d, DL_DIM, 0, "(rewritten, no changes)", 23);
        } else if (!strcmp(name, "Write") && !yyjson_arr_size(patch)) {
            dl_text(d, DL_ADD, jstr(in, "content"), 1); /* no result record: what it wrote */
        } else if (yyjson_arr_size(patch)) {
            dl_hunks(d, patch);
        } else if (!strcmp(name, "NotebookEdit")) {
            if (jstr(in, "cell_id")) dl_fmt(d, DL_DIM, "cell %s", jstr(in, "cell_id"));
            dl_text(d, DL_ADD, jstr(in, "new_source"), 0);
        } else { /* no patch recorded: the edit as asked */
            yyjson_val *edits = yyjson_obj_get(in, "edits");
            if (!edits) {
                dl_text(d, DL_DEL, jstr(in, "old_string"), 0);
                dl_text(d, DL_ADD, jstr(in, "new_string"), 0);
            }
            size_t i, n;
            yyjson_val *e;
            yyjson_arr_foreach(edits, i, n, e) {
                dl_add(d, DL_HUNK, 0, "@@ edit @@", 10);
                dl_text(d, DL_DEL, jstr(e, "old_string"), 0);
                dl_text(d, DL_ADD, jstr(e, "new_string"), 0);
            }
        }
        return;
    }
    if (!strcmp(name, "Read")) {
        if (jstr(in, "file_path")) dl_fmt(d, DL_DIM, "%s", jstr(in, "file_path"));
        if (!block) return;
        yyjson_val *file = yyjson_obj_get(res, "file");
        const char *content = jstr(file, "content");
        if (!err && content) {
            int start = (int)yyjson_get_int(yyjson_obj_get(file, "startLine"));
            dl_text(d, DL_TEXT, content, start > 0 ? start : 1);
        } else {
            dl_result_content(d, block, out);
        }
        return;
    }
    /* Grep, Glob, MCP tools, web, agents, ...: the input, then what came back. */
    dl_input(d, in, strcmp(name, "Grep") && strcmp(name, "Glob") ? DL_TEXT : DL_HEAD);
    if (!block) return;
    dl_gap(d);
    dl_result_content(d, block, out);
}

/* The next line of f, without its newline; NULL at EOF. */
static char *read_line(FILE *f, size_t *len) {
    char *line = NULL;
    size_t cap = 0;
    ssize_t n = getline(&line, &cap, f);
    if (n <= 0) { free(line); return NULL; }
    if (line[n - 1] == '\n') line[--n] = 0;
    *len = (size_t)n;
    return line;
}

static yyjson_val *find_block(yyjson_val *rec, const char *type, const char *key, const char *id) {
    yyjson_val *content = yyjson_obj_get(yyjson_obj_get(rec, "message"), "content");
    size_t i, n;
    yyjson_val *c;
    yyjson_arr_foreach(content, i, n, c) {
        const char *ct = jstr(c, "type"), *cid = jstr(c, key);
        if (ct && cid && !strcmp(ct, type) && !strcmp(cid, id)) return c;
    }
    return NULL;
}

/* A tool call re-read from its transcript: the tool_use, and its result block and
 * the record's toolUseResult (NULL when not there yet). */
typedef struct {
    yyjson_doc *use_doc, *res_doc;
    yyjson_val *use, *block, *res;
    bool gave_up; /* no result within SCAN_MAX */
} Call;

/* The call with tool_use `id` on the line at byte `off` of `src`: 0 found, -1 no
 * such file or line, -2 the line holds no such call. */
static int call_read(const char *src, long off, const char *id, Call *c) {
    *c = (Call){0};
    FILE *f = src && id ? fopen(src, "r") : NULL;
    if (!f || fseeko(f, off, SEEK_SET)) {
        if (f) fclose(f);
        return -1;
    }
    size_t len;
    char *line = read_line(f, &len);
    c->use_doc = line ? parse_line(line, len) : NULL;
    free(line);
    c->use = c->use_doc ? find_block(yyjson_doc_get_root(c->use_doc), "tool_use", "id", id) : NULL;
    if (!c->use) {
        yyjson_doc_free(c->use_doc);
        c->use_doc = NULL;
        fclose(f);
        return -2;
    }
    /* Its result comes later in the same file: the first tool_result naming the id. */
    yyjson_doc *res_doc = NULL;
    yyjson_val *block = NULL, *res = NULL;
    long scanned = 0;
    while (!block && scanned < SCAN_MAX && (line = read_line(f, &len))) {
        scanned += (long)len + 1;
        if (strstr(line, id) && strstr(line, "\"tool_result\"")) {
            res_doc = parse_line(line, len);
            yyjson_val *rec = res_doc ? yyjson_doc_get_root(res_doc) : NULL;
            /* Only the user record: others (api-request-blob) copy the block without toolUseResult. */
            const char *type = jstr(rec, "type");
            block = type && !strcmp(type, "user") ? find_block(rec, "tool_result", "tool_use_id", id) : NULL;
            if (block) {
                /* toolUseResult belongs to the record: only trust it for a lone result. */
                yyjson_val *content = yyjson_obj_get(yyjson_obj_get(rec, "message"), "content");
                size_t i, n, results = 0;
                yyjson_val *b;
                yyjson_arr_foreach(content, i, n, b) results += jstr(b, "type") && !strcmp(jstr(b, "type"), "tool_result");
                res = results == 1 ? yyjson_obj_get(rec, "toolUseResult") : NULL;
            } else {
                yyjson_doc_free(res_doc);
                res_doc = NULL;
            }
        }
        free(line);
    }
    fclose(f);
    c->res_doc = res_doc;
    c->block = block;
    c->res = res;
    c->gave_up = !block && scanned >= SCAN_MAX;
    return 0;
}

static void call_free(Call *c) {
    yyjson_doc_free(c->use_doc);
    yyjson_doc_free(c->res_doc);
}

Detail *tr_detail(const char *src, long off, const char *id, const char *path) {
    Detail *d = xcalloc(1, sizeof *d);
    d->state = -1;
    Call c;
    int rc = call_read(src, off, id, &c);
    if (rc) {
        d->tool = xstrdup("?");
        if (rc == -1) dl_add(d, DL_DIM, 0, "no record of this step", 22);
        else dl_add(d, DL_DIM, 0, "the transcript changed: record not found", 40);
        return d;
    }
    const char *name = jstr(c.use, "name");
    d->tool = tool_label(name ? name : "?");
    detail_body(d, name ? name : "?", yyjson_obj_get(c.use, "input"), c.block, c.res, path);
    d->state = c.block ? 0 : 1;
    if (c.gave_up) { /* don't keep looking */
        d->state = -1;
        dl_add(d, DL_DIM, 0, "(no result within 512 MB of the call)", 37);
    }
    call_free(&c);
    return d;
}

static void change_hunks(Change *ch, yyjson_val *hunks) {
    size_t i, n;
    yyjson_val *h;
    ch->hunks = xcalloc(yyjson_arr_size(hunks) + 1, sizeof *ch->hunks);
    yyjson_arr_foreach(hunks, i, n, h) {
        Hunk *k = &ch->hunks[ch->nhunks++];
        k->old_start = yyjson_get_int(yyjson_obj_get(h, "oldStart"));
        k->old_lines = yyjson_get_int(yyjson_obj_get(h, "oldLines"));
        k->new_start = yyjson_get_int(yyjson_obj_get(h, "newStart"));
        k->new_lines = yyjson_get_int(yyjson_obj_get(h, "newLines"));
        yyjson_val *lines = yyjson_obj_get(h, "lines");
        k->lines = xcalloc(yyjson_arr_size(lines) + 1, sizeof *k->lines);
        size_t j, m;
        yyjson_val *ln;
        yyjson_arr_foreach(lines, j, m, ln)
            if (yyjson_is_str(ln)) k->lines[k->n++] = xstrndup(yyjson_get_str(ln), yyjson_get_len(ln));
    }
}

static char *jdup(yyjson_val *v) { return yyjson_is_str(v) ? xstrndup(yyjson_get_str(v), yyjson_get_len(v)) : NULL; }

Change *tr_change(const char *src, long off, const char *id, const char *root, const char *path) {
    Change *ch = xcalloc(1, sizeof *ch);
    Call c;
    if (call_read(src, off, id, &c)) {
        ch->state = -1;
        return ch;
    }
    ch->state = c.block ? 0 : c.gave_up ? -1 : 1;
    const char *name = jstr(c.use, "name");
    ch->tool = name ? xstrdup(name) : NULL;
    if (name && !strcmp(name, "Bash")) {
        ch->cmd = 1;
        size_t i, n;
        yyjson_val *f;
        char *want = NULL;
        if (root && path) { /* the file's absolute path, as normalize() writes it */
            want = xmalloc(strlen(root) + strlen(path) + 3);
            sprintf(want, "/%s/%s", root, path);
            normalize(want);
        }
        yyjson_arr_foreach(yyjson_obj_get(yyjson_obj_get(c.res, "bashEditDiff"), "files"), i, n, f) {
            const char *fp = jstr(f, "filePath");
            if (!want || !fp || *fp != '/') continue;
            char *abs = xstrdup(fp);
            normalize(abs);
            bool hit = !strcmp(abs, want);
            free(abs);
            if (hit) {
                change_hunks(ch, yyjson_obj_get(f, "hunks"));
                break;
            }
        }
        free(want);
    } else if (c.res) {
        ch->original = jdup(yyjson_obj_get(c.res, "originalFile"));
        const char *type = jstr(c.res, "type");
        if (type && !strcmp(type, "create")) {
            yyjson_val *content = yyjson_obj_get(c.res, "content");
            ch->created = jdup(yyjson_is_str(content) ? content : yyjson_obj_get(yyjson_obj_get(c.use, "input"), "content"));
        }
        ch->patched = yyjson_is_arr(yyjson_obj_get(c.res, "structuredPatch"));
        change_hunks(ch, yyjson_obj_get(c.res, "structuredPatch"));
    }
    call_free(&c);
    return ch;
}

void change_free(Change *ch) {
    if (!ch) return;
    for (int i = 0; i < ch->nhunks; i++) {
        for (int j = 0; j < ch->hunks[i].n; j++) free(ch->hunks[i].lines[j]);
        free(ch->hunks[i].lines);
    }
    free(ch->hunks);
    free(ch->tool);
    free(ch->original);
    free(ch->created);
    free(ch);
}

#define NOTES_SCAN (64L << 20) /* bytes read looking for a call's comments */
#define NOTE_MAX (64 << 10)     /* bytes kept of one comment */

Notes tr_notes(const char *src, long off, int n, const char *id) {
    Notes ns = {0};
    FILE *f = src && id && n > 0 ? fopen(src, "r") : NULL;
    if (!f) return ns;
    if (fseeko(f, off, SEEK_SET)) { fclose(f); return ns; }
    ns.s = xcalloc((size_t)n, sizeof *ns.s);
    long scanned = 0;
    bool done = false;
    char *line;
    size_t len;
    while (!done && ns.n < n && scanned < NOTES_SCAN && (line = read_line(f, &len))) {
        scanned += (long)len + 1;
        yyjson_doc *doc = parse_line(line, len);
        free(line);
        yyjson_val *rec = doc ? yyjson_doc_get_root(doc) : NULL;
        const char *type = jstr(rec, "type");
        if (ends_notes(rec)) done = true; /* past the call: the transcript changed */
        yyjson_val *content = type && !strcmp(type, "assistant")
                                  ? yyjson_obj_get(yyjson_obj_get(rec, "message"), "content") : NULL;
        size_t i, m;
        yyjson_val *c;
        yyjson_arr_foreach(content, i, m, c) {
            const char *ct = jstr(c, "type");
            if (done || ns.n == n) break;
            if (!ct) continue;
            if (!strcmp(ct, "tool_use") && jstr(c, "id") && !strcmp(jstr(c, "id"), id)) done = true;
            else if (!strcmp(ct, "text") && is_note(jstr(c, "text"))) {
                const char *txt = jstr(c, "text");
                size_t tl = yyjson_get_len(yyjson_obj_get(c, "text"));
                if (tl > NOTE_MAX) { /* cut on a character boundary */
                    tl = NOTE_MAX;
                    while (tl && ((unsigned char)txt[tl] & 0xC0) == 0x80) tl--;
                }
                ns.s[ns.n++] = xstrndup(txt, tl);
            }
        }
        yyjson_doc_free(doc);
    }
    fclose(f);
    return ns;
}

void notes_free(Notes *ns) {
    for (int i = 0; i < ns->n; i++) free(ns->s[i]);
    free(ns->s);
    *ns = (Notes){0};
}

Detail *detail_new(const char *tool) {
    Detail *d = xcalloc(1, sizeof *d);
    d->tool = xstrdup(tool);
    return d;
}

void detail_line(Detail *d, DlKind kind, const char *s) { dl_text(d, kind, s, 0); }

void detail_free(Detail *d) {
    if (!d) return;
    for (int i = 0; i < d->n; i++) free(d->lines[i].s);
    free(d->lines);
    free(d->tool);
    free(d);
}
