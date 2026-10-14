#include "transcript.h"

#include <stdlib.h>
#include <string.h>

#include "util.h"
#include "yyjson.h"

#define MAX_TEXT 120

typedef struct {
    char *id, *name, *path, *pattern;
    int turn;
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
};

Transcript *tr_new(const char *root, const char *actor, const char *actor_name,
                   EmitFn emit, void *ud) {
    Transcript *t = xcalloc(1, sizeof *t);
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
}

void tr_free(Transcript *t) {
    if (!t) return;
    for (int i = 0; i < t->npend; i++) pending_free(&t->pend[i]);
    free(t->pend);
    free(t->root);
    free(t->actor);
    free(t->actor_name);
    free(t->last_prompt_parent);
    free(t->cwd);
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

/* Tool path -> repo-relative ("" = root); NULL when outside the repo. Relative
 * paths resolve against the session's cwd. */
static char *rel_path(Transcript *t, const char *p) {
    if (!p || !*p) return NULL;
    const char *base = *p == '/' ? "" : (t->cwd ? t->cwd : t->root);
    size_t bn = strlen(base), pn = strlen(p);
    char *abs = xmalloc(bn + pn + 2);
    memcpy(abs, base, bn);
    abs[bn] = '/';
    memcpy(abs + bn + 1, p, pn + 1);
    normalize(abs);
    char *rel = NULL;
    if (t->rootlen == 1 && t->root[0] == '/') rel = xstrdup(abs + 1);
    else if (!strncmp(abs, t->root, t->rootlen) && (abs[t->rootlen] == 0 || abs[t->rootlen] == '/'))
        rel = xstrdup(abs[t->rootlen] ? abs + t->rootlen + 1 : "");
    free(abs);
    return rel;
}

static void emit(Transcript *t, EvKind kind, double ts, char *path, char *text,
                 int add, int del, int lines) {
    Event e = {.ts = ts, .kind = kind, .actor = xstrdup(t->actor),
               .actor_name = xstrdup(t->actor_name), .path = path, .text = text,
               .add = add, .del = del, .lines = lines, .turn = t->turn};
    t->emit(t->ud, &e);
}

/* ---- prompts ------------------------------------------------------------ */

/* Text of a human prompt record, or NULL when the record isn't one. */
static char *prompt_text(yyjson_val *rec, yyjson_val *content) {
    if (yyjson_get_bool(yyjson_obj_get(rec, "isMeta")) ||
        yyjson_get_bool(yyjson_obj_get(rec, "isCompactSummary")))
        return NULL;
    const char *kind = jstr(yyjson_obj_get(rec, "origin"), "kind");
    if (kind && strcmp(kind, "human")) { /* harness-generated turn, e.g. a task finished */
        char buf[96];
        snprintf(buf, sizeof buf, "(%s)", kind);
        return xstrdup(buf);
    }
    const char *s = NULL;
    if (yyjson_is_str(content)) s = yyjson_get_str(content);
    else if (yyjson_is_arr(content)) {
        size_t i, n;
        yyjson_val *c;
        yyjson_arr_foreach(content, i, n, c) {
            const char *ct = jstr(c, "type");
            if (ct && !strcmp(ct, "text")) { s = jstr(c, "text"); break; }
        }
    }
    if (!s) return NULL;
    while (*s == ' ' || *s == '\n') s++;
    if (!strncmp(s, "<local-command", 14) || !strncmp(s, "<command-message>", 17) ||
        !strncmp(s, "[Request interrupted", 20) || !*s)
        return NULL;
    const char *cmd = strstr(s, "<command-name>");
    if (cmd) {
        cmd += 14;
        const char *end = strstr(cmd, "</command-name>");
        return end ? xstrndup(cmd, (size_t)(end - cmd)) : NULL;
    }
    if (!strncmp(s, "<pasted_content", 15)) {
        const char *gt = strchr(s, '>');
        if (gt) s = gt + 1;
    }
    char *line = first_line(s, MAX_TEXT);
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

static void tool_start(Transcript *t, yyjson_val *c, double ts) {
    const char *name = jstr(c, "name"), *id = jstr(c, "id");
    if (!name || !id || !is_tracked_tool(name)) return;
    yyjson_val *in = yyjson_obj_get(c, "input");
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
    p->turn = t->turn;
    if (!strcmp(name, "Bash")) {
        const char *cmd = jstr(in, "command");
        emit(t, EV_BASH, ts, NULL, first_line(cmd ? cmd : "", MAX_TEXT), 0, 0, -1);
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

    if (!strcmp(name, "Bash")) {
        /* A failing command may still have changed files: no error check. */
        yyjson_val *diff = yyjson_obj_get(res, "bashEditDiff");
        if (yyjson_is_obj(diff)) bash_files(t, diff, ts);
    } else if (!err) {
        char *rel = rel_path(t, p.path);
        if (!strcmp(name, "Grep") || !strcmp(name, "Glob")) {
            if (!p.path) rel = xstrdup("");
            if (rel) emit(t, EV_SEARCH, ts, rel, p.pattern ? first_line(p.pattern, MAX_TEXT) : NULL,
                          0, 0, -1);
        } else if (!rel || !*rel) {
            free(rel);
        } else if (!strcmp(name, "Read")) {
            yyjson_val *file = yyjson_obj_get(res, "file");
            yyjson_val *tl = yyjson_obj_get(file, "totalLines");
            emit(t, EV_READ, ts, rel, NULL, 0, 0, yyjson_is_int(tl) ? (int)yyjson_get_int(tl) : -1);
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

void tr_feed(Transcript *t, const char *line, size_t len) {
    /* Tool output cut mid-character leaves lone surrogates / bad UTF-8: keep the record. */
    yyjson_doc *doc = yyjson_read(line, len, YYJSON_READ_ALLOW_INVALID_UNICODE);
    if (!doc) {
        char *fixed = fix_surrogates(line, len);
        doc = yyjson_read(fixed, len, YYJSON_READ_ALLOW_INVALID_UNICODE);
        free(fixed); /* yyjson copies input unless INSITU */
        if (!doc) return;
    }
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
    if (!type || !ts) goto done;

    if (!strcmp(type, "assistant") && yyjson_is_arr(content)) {
        size_t i, n;
        yyjson_val *c;
        yyjson_arr_foreach(content, i, n, c) {
            const char *ct = jstr(c, "type");
            if (ct && !strcmp(ct, "tool_use")) tool_start(t, c, ts);
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
        if (!had_result) {
            char *text = prompt_text(rec, content);
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
                emit(t, EV_TURN, ts, NULL, text, 0, 0, -1);
            }
        }
    }
done:
    yyjson_doc_free(doc);
}
