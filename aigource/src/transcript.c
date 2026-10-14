#include "transcript.h"

#include <stdlib.h>
#include <string.h>

#include "util.h"
#include "yyjson.h"

#define MAX_TEXT 120

typedef struct {
    char *id, *name, *path, *pattern;
    int turn;
    double ts;
    long off; /* of the line holding the tool_use */
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
};

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
               .add = add, .del = del, .lines = lines, .turn = t->turn, .t0 = t->t0,
               .src = t->ev_id ? t->src : NULL, .src_off = t->ev_off,
               .tool_id = t->ev_id ? xstrdup(t->ev_id) : NULL};
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
    p->turn = t->turn;
    p->ts = ts;
    p->off = t->line_off;
    if (!strcmp(name, "Bash")) {
        const char *cmd = jstr(in, "command");
        t->ev_off = t->line_off;
        t->ev_id = id;
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

    if (!strcmp(name, "Bash")) {
        /* A failing command may still have changed files: no error check. */
        yyjson_val *diff = yyjson_obj_get(res, "bashEditDiff");
        if (yyjson_is_obj(diff)) bash_files(t, diff, ts);
    } else if (err) { /* no effect, but still a step to look at */
        plain_step(t, &p, ts, " failed");
    } else {
        char *rel = rel_path(t, p.path);
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

Detail *tr_detail(const char *src, long off, const char *id, const char *path) {
    Detail *d = xcalloc(1, sizeof *d);
    d->state = -1;
    FILE *f = src && id ? fopen(src, "r") : NULL;
    if (!f || fseeko(f, off, SEEK_SET)) {
        d->tool = xstrdup("?");
        dl_add(d, DL_DIM, 0, "no record of this step", 22);
        if (f) fclose(f);
        return d;
    }
    size_t len;
    char *line = read_line(f, &len);
    yyjson_doc *use_doc = line ? parse_line(line, len) : NULL;
    free(line);
    yyjson_val *use = use_doc ? find_block(yyjson_doc_get_root(use_doc), "tool_use", "id", id) : NULL;
    if (!use) {
        d->tool = xstrdup("?");
        dl_add(d, DL_DIM, 0, "the transcript changed: record not found", 40);
        yyjson_doc_free(use_doc);
        fclose(f);
        return d;
    }
    const char *name = jstr(use, "name");
    d->tool = tool_label(name ? name : "?");

    /* Its result comes later in the same file: the first tool_result naming the id. */
    yyjson_doc *res_doc = NULL;
    yyjson_val *block = NULL, *res = NULL;
    long scanned = 0;
    while (!block && scanned < SCAN_MAX && (line = read_line(f, &len))) {
        scanned += (long)len + 1;
        if (strstr(line, id) && strstr(line, "\"tool_result\"")) {
            res_doc = parse_line(line, len);
            yyjson_val *rec = res_doc ? yyjson_doc_get_root(res_doc) : NULL;
            block = rec ? find_block(rec, "tool_result", "tool_use_id", id) : NULL;
            if (block) {
                /* toolUseResult belongs to the record: only trust it for a lone result. */
                yyjson_val *content = yyjson_obj_get(yyjson_obj_get(rec, "message"), "content");
                size_t i, n, results = 0;
                yyjson_val *c;
                yyjson_arr_foreach(content, i, n, c) results += jstr(c, "type") && !strcmp(jstr(c, "type"), "tool_result");
                res = results == 1 ? yyjson_obj_get(rec, "toolUseResult") : NULL;
            } else {
                yyjson_doc_free(res_doc);
                res_doc = NULL;
            }
        }
        free(line);
    }
    fclose(f);
    detail_body(d, name ? name : "?", yyjson_obj_get(use, "input"), block, res, path);
    d->state = block ? 0 : 1;
    if (!block && scanned >= SCAN_MAX) { /* don't keep looking */
        d->state = -1;
        dl_add(d, DL_DIM, 0, "(no result within 512 MB of the call)", 37);
    }
    yyjson_doc_free(use_doc);
    yyjson_doc_free(res_doc);
    return d;
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
