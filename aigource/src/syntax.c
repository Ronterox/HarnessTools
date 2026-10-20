#include "syntax.h"

#include <ctype.h>
#include <dlfcn.h>
#include <pthread.h>
#include <regex.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <unistd.h>
#include <tree_sitter/api.h>

#include "util.h"

/* ---- predicates: the query conditions tree-sitter leaves to its host ---- */

typedef enum { P_EQ, P_ANY_OF, P_MATCH, P_CONTAINS, P_HAS_ANCESTOR, P_HAS_PARENT } POp;

typedef struct {
    POp op;
    bool neg;      /* not-... */
    uint32_t cap;  /* the capture tested */
    int cap2;      /* eq? against another capture; -1 = against strs */
    const char **strs; /* owned array, strings owned by the query */
    int nstr;
    regex_t re;
    bool re_ok;
} Pred;

typedef struct {
    Pred *p;
    int n;
    unsigned char prio; /* #set! priority, default 100 like Neovim */
    bool dead;          /* a predicate we can't evaluate correctly: never matches */
} Pattern;

/* textobjects.scm, for syn_defs: only its patterns capturing @function.outer
 * or @class.outer stay enabled. */
typedef struct {
    TSQuery *q;
    Pattern *pats;
    int fcap, ccap;     /* their capture ids, -1 none */
    unsigned char *sym; /* per grammar symbol: SYM_* flags, for measure() */
    uint32_t nsym;
} TextObj;

struct SynLang {
    char name[SYN_NAME_MAX];
    const TSLanguage *lang; /* NULL: not available (remembered) */
    TSQuery *q;
    Pattern *pats;
    signed char *cls; /* per capture id: SynClass, -1 ignored */
    TextObj *to;      /* loaded on first use (to_tried); NULL: none */
    bool to_tried;
    SynLang *next;
};

struct SynDoc {
    SynLang *l;
    const char *text;
    uint32_t len;
    TSTree *tree; /* of text[ws, we): all of it, or a window of a big text */
    uint32_t ws, we;
    TSQueryCursor *cur;
};

#define SYN_WHOLE (512u << 10)  /* texts up to this are parsed whole */
#define SYN_WINDOW (256u << 10) /* bigger ones, this much around what's drawn */

static bool debug(void) { return getenv("AIG_DEBUG") != NULL; }

/* A Lua pattern (#lua-match?) as a POSIX extended regex; NULL when it uses
 * what ERE can't say (%b, %f, negated classes inside a set). */
static char *lua_to_ere(const char *s) {
    char *out = xmalloc(strlen(s) * 12 + 1), *o = out;
    bool in_set = false;
    for (; *s; s++) {
        char c = *s;
        if (c == '%' && s[1]) {
            char k = *++s;
            const char *cls = NULL;
            switch (tolower((unsigned char)k)) {
            case 'a': cls = "alpha"; break;
            case 'd': cls = "digit"; break;
            case 'l': cls = "lower"; break;
            case 'u': cls = "upper"; break;
            case 's': cls = "space"; break;
            case 'w': cls = "alnum"; break;
            case 'x': cls = "xdigit"; break;
            case 'p': cls = "punct"; break;
            case 'c': cls = "cntrl"; break;
            case 'g': cls = "graph"; break;
            }
            if (k == 'b' || k == 'f') { free(out); return NULL; }
            if (cls) {
                bool negated = isupper((unsigned char)k);
                if (in_set && negated) { free(out); return NULL; }
                o += sprintf(o, in_set ? "[:%s:]" : negated ? "[^[:%s:]]" : "[[:%s:]]", cls);
            } else {
                if (!in_set && strchr(".[]()*+?^$\\{}|", k)) *o++ = '\\';
                *o++ = k;
            }
            continue;
        }
        if (in_set) {
            if (c == ']') in_set = false;
            *o++ = c;
            continue;
        }
        if (c == '[') {
            in_set = true;
            *o++ = c;
            if (s[1] == '^') *o++ = *++s;
            if (s[1] == ']') *o++ = *++s; /* a leading ] is literal */
            continue;
        }
        if (c == '-') { *o++ = '*'; continue; } /* lazy repeat: greedy is close enough for a whole-capture test */
        if (strchr("{}|\\", c)) *o++ = '\\';
        *o++ = c;
    }
    *o = 0;
    return out;
}

/* A Vim regex (#match?) as ERE: the queries use the common subset. \d is an
 * escape ERE lacks, \c (ignore case) a flag, \v (very magic) is ERE already. */
static char *vim_to_ere(const char *s, bool *icase) {
    char *out = xmalloc(strlen(s) * 5 + 1), *o = out;
    for (; *s; s++) {
        if (s[0] == '\\' && s[1] == 'd') { o += sprintf(o, "[0-9]"); s++; }
        else if (s[0] == '\\' && (s[1] == 'c' || s[1] == 'v')) { *icase |= s[1] == 'c'; s++; }
        else *o++ = *s;
    }
    *o = 0;
    return out;
}

/* Turn pattern i's predicate steps into Preds. */
static void compile_pattern(const char *lname, TSQuery *q, Pattern *pats, uint32_t i) {
    Pattern *pt = &pats[i];
    pt->prio = 100;
    uint32_t n;
    const TSQueryPredicateStep *st = ts_query_predicates_for_pattern(q, i, &n);
    for (uint32_t a = 0; a < n;) {
        uint32_t b = a;
        while (b < n && st[b].type != TSQueryPredicateStepTypeDone) b++;
        /* st[a] is the name, st[a+1 .. b) the arguments */
        uint32_t len;
        const char *name = st[a].type == TSQueryPredicateStepTypeString ? ts_query_string_value_for_id(q, st[a].value_id, &len) : "";
        int nargs = (int)(b - a - 1);
        const TSQueryPredicateStep *arg = st + a + 1;
        if (!strcmp(name, "set!")) { /* (#set! priority N) */
            for (int k = 0; k + 1 < nargs; k++)
                if (arg[k].type == TSQueryPredicateStepTypeString && arg[k + 1].type == TSQueryPredicateStepTypeString &&
                    !strcmp(ts_query_string_value_for_id(q, arg[k].value_id, &len), "priority")) {
                    int p = atoi(ts_query_string_value_for_id(q, arg[k + 1].value_id, &len));
                    pt->prio = (unsigned char)(p < 0 ? 0 : p > 255 ? 255 : p);
                }
        } else if (nargs >= 1 && arg[0].type == TSQueryPredicateStepTypeCapture && *name && name[strlen(name) - 1] == '?') {
            Pred p = {.cap = arg[0].value_id, .cap2 = -1};
            const char *op = name;
            if (!strncmp(op, "not-", 4)) p.neg = true, op += 4;
            if (!strncmp(op, "any-", 4) && strcmp(op, "any-of?")) op += 4;
            bool known = true;
            if (!strcmp(op, "eq?")) p.op = P_EQ;
            else if (!strcmp(op, "any-of?")) p.op = P_ANY_OF;
            else if (!strcmp(op, "match?") || !strcmp(op, "lua-match?") || !strcmp(op, "vim-match?")) p.op = P_MATCH;
            else if (!strcmp(op, "contains?")) p.op = P_CONTAINS;
            else if (!strcmp(op, "has-ancestor?")) p.op = P_HAS_ANCESTOR;
            else if (!strcmp(op, "has-parent?")) p.op = P_HAS_PARENT;
            else known = false; /* host-specific (is?, a language's own): ignored */
            if (known) {
                if (p.op == P_EQ && nargs >= 2 && arg[1].type == TSQueryPredicateStepTypeCapture) p.cap2 = (int)arg[1].value_id;
                p.strs = xcalloc((size_t)(nargs > 1 ? nargs - 1 : 1), sizeof *p.strs);
                for (int k = 1; k < nargs; k++)
                    if (arg[k].type == TSQueryPredicateStepTypeString)
                        p.strs[p.nstr++] = ts_query_string_value_for_id(q, arg[k].value_id, &len);
                if (p.op == P_MATCH) {
                    bool icase = false;
                    char *ere = p.nstr ? (!strcmp(op, "lua-match?") ? lua_to_ere(p.strs[0]) : vim_to_ere(p.strs[0], &icase)) : NULL;
                    p.re_ok = ere && !regcomp(&p.re, ere, REG_EXTENDED | REG_NOSUB | (icase ? REG_ICASE : 0));
                    if (!p.re_ok) {
                        if (debug()) fprintf(stderr, "syntax %s: can't use pattern %s\n", lname, p.nstr ? p.strs[0] : "");
                        pt->dead = true;
                    }
                    free(ere);
                }
                pt->p = xrealloc(pt->p, (size_t)(pt->n + 1) * sizeof *pt->p);
                pt->p[pt->n++] = p;
            }
        }
        a = b + 1;
    }
}

/* Capture names (Neovim's, both the current and the older ones) to classes,
 * by longest dotted prefix: "keyword.function" is a keyword. */
static const struct { const char *name; signed char cls; } CAPTURES[] = {
    {"keyword", SYN_KEYWORD}, {"conditional", SYN_KEYWORD}, {"repeat", SYN_KEYWORD},
    {"include", SYN_KEYWORD}, {"exception", SYN_KEYWORD}, {"storageclass", SYN_KEYWORD},
    {"type.qualifier", SYN_KEYWORD}, {"tag", SYN_TAG}, {"tag.attribute", SYN_PROPERTY},
    {"tag.delimiter", -1}, {"string", SYN_STRING}, {"character", SYN_STRING},
    {"string.escape", SYN_ESCAPE}, {"string.regexp", SYN_ESCAPE}, {"string.special", SYN_ESCAPE},
    {"character.special", SYN_ESCAPE}, {"comment", SYN_COMMENT}, {"function", SYN_FUNCTION},
    {"method", SYN_FUNCTION}, {"constructor", SYN_TYPE}, {"type", SYN_TYPE},
    {"number", SYN_CONSTANT}, {"float", SYN_CONSTANT}, {"boolean", SYN_CONSTANT},
    {"constant", SYN_CONSTANT}, {"property", SYN_PROPERTY}, {"field", SYN_PROPERTY},
    {"variable.member", SYN_PROPERTY}, {"attribute", SYN_PROPERTY}, {"label", SYN_PROPERTY},
    {"module", SYN_PROPERTY}, {"namespace", SYN_PROPERTY}, {"variable.builtin", SYN_BUILTIN},
    {"variable.parameter.builtin", SYN_BUILTIN}, {"markup.heading", SYN_HEADING},
    {"markup.strong", SYN_STRONG}, {"markup.link", SYN_LINK}, {"markup.raw", SYN_STRING},
    {"markup.list", SYN_LIST}, {"text.title", SYN_HEADING}, {"text.literal", SYN_STRING},
    {"text.uri", SYN_LINK}, {"none", SYN_NONE},
};

static signed char capture_class(const char *name) {
    if (name[0] == '_') return -1; /* a predicate's helper capture */
    size_t best = 0;
    signed char cls = -1;
    for (size_t i = 0; i < sizeof CAPTURES / sizeof *CAPTURES; i++) {
        size_t n = strlen(CAPTURES[i].name);
        if (n > best && !strncmp(name, CAPTURES[i].name, n) && (name[n] == 0 || name[n] == '.'))
            best = n, cls = CAPTURES[i].cls;
    }
    return cls;
}

/* ---- loading a language: grammar + highlights.scm ----------------------- */

static char *read_file(const char *path, size_t *len) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    char *s = xmalloc((size_t)(n > 0 ? n : 0) + 1);
    size_t got = n > 0 ? fread(s, 1, (size_t)n, f) : 0;
    fclose(f);
    s[got] = 0;
    if (len) *len = got;
    return s;
}

/* lang's query file (highlights.scm, textobjects.scm) from dir, after the
 * queries it inherits ("; inherits: ecma,jsx" on its first lines; "(jsx)"
 * marks an optional one). */
static char *read_query(const char *dir, const char *lang, const char *file, int depth) {
    char path[1200];
    snprintf(path, sizeof path, "%s/%s/%s", dir, lang, file);
    size_t n;
    char *own = read_file(path, &n);
    if (!own) return NULL;
    char names[8][64];
    int nn = 0;
    for (const char *line = own; *line == ';' && depth < 4;) {
        const char *eol = strchr(line, '\n'), *end = eol ? eol : line + strlen(line);
        const char *in = strstr(line, "inherits:");
        if (in && in < end)
            for (const char *p = in + 9; p < end && nn < 8;) {
                while (p < end && strchr(", ()\t\r", *p)) p++;
                const char *q = p;
                while (q < end && !strchr(", ()\t\r", *q)) q++;
                if (q > p && q - p < 64) snprintf(names[nn++], 64, "%.*s", (int)(q - p), p);
                p = q;
            }
        if (!eol) break;
        line = eol + 1;
    }
    size_t total = n + 1;
    char *parents[8] = {0};
    for (int i = 0; i < nn; i++)
        if ((parents[i] = read_query(dir, names[i], file, depth + 1))) total += strlen(parents[i]) + 1;
    char *all = xmalloc(total), *o = all;
    for (int i = 0; i < nn; i++)
        if (parents[i]) o += sprintf(o, "%s\n", parents[i]), free(parents[i]);
    memcpy(o, own, n + 1);
    free(own);
    return all;
}

/* Where Neovim keeps grammars, each with the queries written for it. */
static int search_dirs(char (*parser)[512], char (*query)[512]) {
    const char *xdg = getenv("XDG_DATA_HOME"), *home = getenv("HOME");
    char data[400];
    if (xdg && *xdg) snprintf(data, sizeof data, "%s", xdg);
    else snprintf(data, sizeof data, "%s/.local/share", home ? home : "");
    int n = 0;
    snprintf(parser[n], 512, "%s/nvim/site/parser", data);
    snprintf(query[n++], 512, "%s/nvim/site/queries", data);
    snprintf(parser[n], 512, "%s/nvim/lazy/nvim-treesitter/parser", data);
    snprintf(query[n++], 512, "%s/nvim/lazy/nvim-treesitter/queries", data);
    static const char *sys[][2] = {
        {"/usr/local/lib/nvim/parser", "/usr/local/share/nvim/runtime/queries"},
        {"/usr/lib/x86_64-linux-gnu/nvim/parser", "/usr/share/nvim/runtime/queries"},
        {"/usr/lib/nvim/parser", "/usr/share/nvim/runtime/queries"},
    };
    for (size_t i = 0; i < sizeof sys / sizeof *sys; i++) {
        snprintf(parser[n], 512, "%s", sys[i][0]);
        snprintf(query[n++], 512, "%s", sys[i][1]);
    }
    return n;
}

/* The first installed grammar of this language that this tree-sitter can
 * read and whose highlights query compiles against it. */
static void load_lang(SynLang *L) {
    char parser[8][512], query[8][512];
    int nd = search_dirs(parser, query);
    for (int i = 0; i < nd; i++) {
        char path[600], sym[64];
        snprintf(path, sizeof path, "%s/%s.so", parser[i], L->name);
        void *so = access(path, R_OK) ? NULL : dlopen(path, RTLD_NOW | RTLD_LOCAL);
        if (!so) continue;
        snprintf(sym, sizeof sym, "tree_sitter_%s", L->name);
        const TSLanguage *(*fn)(void) = (const TSLanguage *(*)(void))dlsym(so, sym);
        const TSLanguage *lang = fn ? fn() : NULL;
        uint32_t abi = lang ? ts_language_abi_version(lang) : 0;
        if (abi < TREE_SITTER_MIN_COMPATIBLE_LANGUAGE_VERSION || abi > TREE_SITTER_LANGUAGE_VERSION) {
            if (debug()) fprintf(stderr, "syntax %s: %s has ABI %u\n", L->name, path, abi);
            dlclose(so);
            continue;
        }
        size_t qn = 0;
        char *src = read_query(query[i], L->name, "highlights.scm", 0);
        uint32_t err_off = 0;
        TSQueryError err = TSQueryErrorNone;
        TSQuery *q = src ? ts_query_new(lang, src, (uint32_t)(qn = strlen(src)), &err_off, &err) : NULL;
        if (!q) {
            if (debug()) fprintf(stderr, "syntax %s: query in %s %s (error %d at %u)\n", L->name, query[i],
                                 src ? "doesn't compile" : "missing", (int)err, err_off);
            free(src);
            dlclose(so);
            continue;
        }
        free(src);
        L->lang = lang; /* the grammar stays loaded for the session */
        L->q = q;
        uint32_t np = ts_query_pattern_count(q), nc = ts_query_capture_count(q);
        L->pats = xcalloc(np ? np : 1, sizeof *L->pats);
        for (uint32_t k = 0; k < np; k++) compile_pattern(L->name, q, L->pats, k);
        L->cls = xmalloc(nc ? nc : 1);
        for (uint32_t k = 0; k < nc; k++) {
            uint32_t len;
            L->cls[k] = capture_class(ts_query_capture_name_for_id(q, k, &len));
        }
        if (debug()) fprintf(stderr, "syntax %s: %s, %u patterns\n", L->name, path, np);
        return;
    }
}

static void pats_free(Pattern *pats, uint32_t n) {
    for (uint32_t i = 0; pats && i < n; i++) {
        for (int k = 0; k < pats[i].n; k++) {
            free(pats[i].p[k].strs);
            if (pats[i].p[k].re_ok) regfree(&pats[i].p[k].re);
        }
        free(pats[i].p);
    }
    free(pats);
}

/* A language loaded twice (two threads at once): the copy that lost. */
static void lang_drop(SynLang *L) {
    if (L->q) {
        pats_free(L->pats, ts_query_pattern_count(L->q));
        ts_query_delete(L->q);
    }
    free(L->cls);
    free(L);
}

/* ---- languages by file -------------------------------------------------- */

static const char *const EXT_LANG[][2] = {
    {"h", "c"},           {"cc", "cpp"},         {"cxx", "cpp"},        {"hpp", "cpp"},       {"hh", "cpp"},
    {"py", "python"},     {"pyi", "python"},     {"sh", "bash"},        {"zsh", "bash"},      {"js", "javascript"},
    {"mjs", "javascript"}, {"cjs", "javascript"}, {"jsx", "javascript"}, {"ts", "typescript"}, {"mts", "typescript"},
    {"rs", "rust"},       {"md", "markdown"},    {"yml", "yaml"},       {"cs", "c_sharp"},    {"kt", "kotlin"},
    {"rb", "ruby"},       {"scm", "query"},      {"mk", "make"},        {"htm", "html"},      {"pl", "perl"},
    {"ex", "elixir"},     {"exs", "elixir"},     {"ps1", "powershell"}, {"jsonl", "json"},    {"ndjson", "json"},
};
static const char *const NAME_LANG[][2] = {
    {"Makefile", "make"},   {"makefile", "make"},       {"GNUmakefile", "make"},
    {"Dockerfile", "dockerfile"}, {"CMakeLists.txt", "cmake"}, {".gitignore", "gitignore"},
    {".gitattributes", "gitattributes"}, {"COMMIT_EDITMSG", "gitcommit"},
};

/* Languages load once and stay; the analysis looks them up from its worker
 * thread while the render thread highlights: loading is under this lock, and
 * a loaded SynLang never changes. */
static SynLang *g_langs;
static pthread_mutex_t g_mu = PTHREAD_MUTEX_INITIALIZER;

int syn_lang_name(const char *path, char name[SYN_NAME_MAX]) {
    *name = 0;
    if (!path) return 0;
    const char *base = strrchr(path, '/') ? strrchr(path, '/') + 1 : path, *dot = strrchr(base, '.');
    for (size_t i = 0; i < sizeof NAME_LANG / sizeof *NAME_LANG && !*name; i++)
        if (!strcmp(base, NAME_LANG[i][0])) snprintf(name, SYN_NAME_MAX, "%s", NAME_LANG[i][1]);
    if (!*name && dot && dot[1]) {
        for (size_t i = 0; i < sizeof EXT_LANG / sizeof *EXT_LANG && !*name; i++)
            if (!strcasecmp(dot + 1, EXT_LANG[i][0])) snprintf(name, SYN_NAME_MAX, "%s", EXT_LANG[i][1]);
        if (!*name && strlen(dot + 1) < SYN_NAME_MAX) /* go, lua, json, toml, css, ... are their own names */
            for (int k = 0; (name[k] = (char)tolower((unsigned char)dot[1 + k])); k++)
                if (!isalnum((unsigned char)name[k]) && name[k] != '_') { name[0] = 0; break; }
    }
    return *name != 0;
}

static SynLang *lang_find(const char *name) {
    SynLang *L = g_langs;
    while (L && strcmp(L->name, name)) L = L->next;
    return L;
}

SynLang *syn_lang_for(const char *path) {
    char name[SYN_NAME_MAX];
    if (!syn_lang_name(path, name)) return NULL;
    pthread_mutex_lock(&g_mu);
    SynLang *L = lang_find(name);
    pthread_mutex_unlock(&g_mu);
    if (!L) { /* loaded outside the lock (slow: dlopen, query compiles), then added if still absent */
        SynLang *mine = xcalloc(1, sizeof *mine);
        snprintf(mine->name, sizeof mine->name, "%s", name);
        load_lang(mine);
        pthread_mutex_lock(&g_mu);
        if (!(L = lang_find(name))) {
            mine->next = g_langs;
            g_langs = L = mine;
            mine = NULL;
        }
        pthread_mutex_unlock(&g_mu);
        if (mine) lang_drop(mine);
    }
    return L->lang ? L : NULL;
}

/* ---- highlighting ------------------------------------------------------- */

/* Is the fence on line l a bare one (``` or ~~~ with no info string)? */
static bool bare_fence(const char *l, const char *end) {
    while (l < end && *l == ' ') l++;
    if (end - l < 3 || !(!strncmp(l, "```", 3) || !strncmp(l, "~~~", 3))) return false;
    for (l += 3; l < end && (*l == '`' || *l == '~' || *l == ' ' || *l == '\r'); l++) {}
    return l == end;
}

static bool is_fence(const char *l, const char *end) {
    while (l < end && *l == ' ') l++;
    return end - l >= 3 && (!strncmp(l, "```", 3) || !strncmp(l, "~~~", 3));
}

const char *syn_fragment_prefix(const SynLang *l, const char *text, uint32_t len) {
    if (!strcmp(l->name, "markdown")) { /* odd fences, the first bare: it closes one opened above */
        int fences = 0;
        bool first_bare = false;
        for (const char *p = text, *end = text + len; p < end;) {
            const char *eol = memchr(p, '\n', (size_t)(end - p));
            if (!eol) eol = end;
            if (is_fence(p, eol) && !fences++) first_bare = bare_fence(p, eol);
            if (eol == end) break;
            p = eol + 1;
        }
        return fences % 2 && first_bare ? "```\n" : "";
    }
    static const char C_COMMENTS[] = " c cpp javascript typescript tsx java go rust css scss c_sharp kotlin php swift dart zig jsonc glsl ";
    char key[48];
    snprintf(key, sizeof key, " %s ", l->name);
    if (!strstr(C_COMMENTS, key)) return "";
    const char *close = memmem(text, len, "*/", 2), *open = memmem(text, len, "/*", 2);
    return close && (!open || close < open) ? "/*" : "";
}

SynDoc *syn_doc(SynLang *l, const char *text, uint32_t len) {
    SynDoc *d = xcalloc(1, sizeof *d);
    d->l = l;
    d->text = text;
    d->len = len;
    return d;
}

void syn_doc_free(SynDoc *d) {
    if (!d) return;
    if (d->tree) ts_tree_delete(d->tree);
    if (d->cur) ts_query_cursor_delete(d->cur);
    free(d);
}

static bool node_text_is(const char *text, TSNode n, const char *s) {
    uint32_t a = ts_node_start_byte(n), b = ts_node_end_byte(n);
    return strlen(s) == b - a && !memcmp(text + a, s, b - a);
}

static bool pred_ok(const char *text, const Pred *p, const TSQueryMatch *m) {
    const TSNode *node = NULL, *node2 = NULL;
    for (uint16_t i = 0; i < m->capture_count; i++) {
        if (!node && m->captures[i].index == p->cap) node = &m->captures[i].node;
        if (!node2 && p->cap2 >= 0 && m->captures[i].index == (uint32_t)p->cap2) node2 = &m->captures[i].node;
    }
    if (!node) return true; /* an optional capture that didn't match */
    uint32_t a = ts_node_start_byte(*node), b = ts_node_end_byte(*node);
    bool r = false;
    switch (p->op) {
    case P_EQ:
        if (p->cap2 >= 0) {
            if (!node2) return true;
            uint32_t a2 = ts_node_start_byte(*node2), b2 = ts_node_end_byte(*node2);
            r = b - a == b2 - a2 && !memcmp(text + a, text + a2, b - a);
        } else r = p->nstr && node_text_is(text, *node, p->strs[0]);
        break;
    case P_ANY_OF:
        for (int i = 0; i < p->nstr && !r; i++) r = node_text_is(text, *node, p->strs[i]);
        break;
    case P_MATCH: {
        if (!p->re_ok) return false;
        char small[256], *t = b - a < sizeof small ? small : xmalloc(b - a + 1);
        memcpy(t, text + a, b - a);
        t[b - a] = 0;
        r = !regexec(&p->re, t, 0, NULL, 0);
        if (t != small) free(t);
        break;
    }
    case P_CONTAINS:
        for (int i = 0; i < p->nstr && !r; i++) r = memmem(text + a, b - a, p->strs[i], strlen(p->strs[i])) != NULL;
        break;
    case P_HAS_ANCESTOR:
    case P_HAS_PARENT:
        for (TSNode up = ts_node_parent(*node); !ts_node_is_null(up) && !r; up = ts_node_parent(up)) {
            for (int i = 0; i < p->nstr && !r; i++) r = !strcmp(ts_node_type(up), p->strs[i]);
            if (p->op == P_HAS_PARENT) break;
        }
        break;
    }
    return r != p->neg;
}

void syn_highlight(SynDoc *d, uint32_t start, uint32_t end, unsigned char *out) {
    if (end > d->len) end = d->len;
    if (start >= end) return;
    memset(out, SYN_NONE, end - start);
    SynLang *L = d->l;
    if (!d->cur || start < d->ws || end > d->we) { /* parse what's needed: once, or when leaving the window */
        uint32_t ws = 0, we = d->len;
        if (d->len > SYN_WHOLE) {
            uint32_t mid = start + (end - start) / 2;
            ws = mid > SYN_WINDOW / 2 ? mid - SYN_WINDOW / 2 : 0;
            if (ws > start) ws = start;
            we = ws + SYN_WINDOW < d->len ? ws + SYN_WINDOW : d->len;
            if (we < end) we = end;
            while (ws > 0 && d->text[ws - 1] != '\n') ws--; /* whole lines */
            while (we < d->len && d->text[we - 1] != '\n') we++;
        }
        if (d->tree) ts_tree_delete(d->tree);
        d->tree = NULL;
        TSParser *ps = ts_parser_new();
        /* A window is parsed in place, so node offsets stay the text's own. */
        TSRange r = {{0, 0}, {UINT32_MAX, UINT32_MAX}, ws, we};
        if (ts_parser_set_language(ps, L->lang) && (we - ws == d->len || ts_parser_set_included_ranges(ps, &r, 1)))
            d->tree = ts_parser_parse_string(ps, NULL, d->text, d->len);
        ts_parser_delete(ps);
        d->ws = ws;
        d->we = we;
        if (!d->cur) {
            d->cur = ts_query_cursor_new();
            ts_query_cursor_set_match_limit(d->cur, 256); /* as Neovim: deep trees stay fast */
        }
    }
    if (!d->tree) return;
    unsigned char *prio = xcalloc(end - start, 1);
    ts_query_cursor_set_byte_range(d->cur, start, end);
    ts_query_cursor_exec(d->cur, L->q, ts_tree_root_node(d->tree));
    TSQueryMatch m;
    uint32_t ci;
    while (ts_query_cursor_next_capture(d->cur, &m, &ci)) {
        const TSQueryCapture *c = &m.captures[ci];
        signed char cls = L->cls[c->index];
        const Pattern *pt = &L->pats[m.pattern_index];
        if (cls < 0 || pt->dead) continue;
        bool ok = true;
        for (int i = 0; i < pt->n && ok; i++) ok = pred_ok(d->text, &pt->p[i], &m);
        if (!ok) continue;
        uint32_t a = ts_node_start_byte(c->node), b = ts_node_end_byte(c->node);
        if (a < start) a = start;
        if (b > end) b = end;
        /* Later and inner captures win at the same priority, as in Neovim. */
        for (uint32_t k = a; k < b; k++)
            if (pt->prio >= prio[k - start]) out[k - start] = (unsigned char)cls, prio[k - start] = pt->prio;
    }
    free(prio);
}

/* ---- functions and classes (textobjects) -------------------------------- */

/* Branch points, by node type across grammars: if/elif/else-if, loops, cases,
 * catch/except, conditional expressions (comprehension ifs and fors too). */
static const char *const BRANCHES[] = {
    "if_statement", "if_expression", "elif_clause", "else_if_clause", "elseif_statement", "elsif", "if", "unless",
    "if_modifier", "unless_modifier", "if_clause", "for_statement", "for_in_statement", "for_expression",
    "for_range_loop", "enhanced_for_statement", "foreach_statement", "c_style_for_statement", "for_in_clause",
    "for", "while_statement", "while_expression", "while", "until", "while_modifier", "until_modifier",
    "loop_expression", "do_statement", "repeat_statement", "case_statement", "case_clause", "case_item",
    "switch_case", "switch_label", "match_arm", "expression_case", "type_case", "communication_case", "when",
    "catch_clause", "except_clause", "rescue", "conditional_expression", "ternary_expression", "conditional",
};
/* Nodes that open a level of nesting: blocks and bodies. */
static const char *const BLOCKS[] = {
    "compound_statement", "block", "statement_block", "class_body", "field_declaration_list",
    "declaration_list", "body_statement", "do_block", "switch_body", "switch_block",
};

static bool in_list(const char *s, const char *const *list, size_t n) {
    for (size_t i = 0; i < n; i++)
        if (!strcmp(s, list[i])) return true;
    return false;
}

enum { SYM_BRANCH = 1, SYM_BLOCK = 2, SYM_BINARY = 4, SYM_LOGIC = 8 };

/* Where textobjects.scm lives: the plugin's own queries first, then the
 * places highlight queries come from. */
static TextObj *load_textobjects(const SynLang *L) {
    const char *xdg = getenv("XDG_DATA_HOME"), *home = getenv("HOME");
    char data[400], dirs[6][512];
    if (xdg && *xdg) snprintf(data, sizeof data, "%s", xdg);
    else snprintf(data, sizeof data, "%s/.local/share", home ? home : "");
    int nd = 0;
    snprintf(dirs[nd++], 512, "%s/nvim/lazy/nvim-treesitter-textobjects/queries", data);
    snprintf(dirs[nd++], 512, "%s/nvim/site/pack/packer/start/nvim-treesitter-textobjects/queries", data);
    snprintf(dirs[nd++], 512, "%s/nvim/site/queries", data);
    snprintf(dirs[nd++], 512, "%s/nvim/lazy/nvim-treesitter/queries", data);
    snprintf(dirs[nd++], 512, "/usr/local/share/nvim/runtime/queries");
    snprintf(dirs[nd++], 512, "/usr/share/nvim/runtime/queries");
    for (int i = 0; i < nd; i++) {
        char *src = read_query(dirs[i], L->name, "textobjects.scm", 0);
        if (!src) continue;
        uint32_t err_off = 0;
        TSQueryError err = TSQueryErrorNone;
        TSQuery *q = ts_query_new(L->lang, src, (uint32_t)strlen(src), &err_off, &err);
        free(src);
        if (!q) {
            if (debug()) fprintf(stderr, "syntax %s: textobjects in %s don't compile (error %d at %u)\n", L->name,
                                 dirs[i], (int)err, err_off);
            continue;
        }
        int fcap = -1, ccap = -1;
        for (uint32_t k = 0, nc = ts_query_capture_count(q); k < nc; k++) {
            uint32_t len;
            const char *name = ts_query_capture_name_for_id(q, k, &len);
            if (!strcmp(name, "function.outer")) fcap = (int)k;
            else if (!strcmp(name, "class.outer")) ccap = (int)k;
        }
        if (fcap < 0 && ccap < 0) { ts_query_delete(q); continue; }
        uint32_t np = ts_query_pattern_count(q);
        TextObj *to = xcalloc(1, sizeof *to);
        to->pats = xcalloc(np ? np : 1, sizeof *to->pats);
        for (uint32_t k = 0; k < np; k++) {
            bool used = (fcap >= 0 && ts_query_capture_quantifier_for_id(q, k, (uint32_t)fcap) != TSQuantifierZero) ||
                        (ccap >= 0 && ts_query_capture_quantifier_for_id(q, k, (uint32_t)ccap) != TSQuantifierZero);
            if (used) compile_pattern(L->name, q, to->pats, k);
            else ts_query_disable_pattern(q, k); /* loops, calls, parameters, ...: not needed */
        }
        to->q = q;
        to->fcap = fcap;
        to->ccap = ccap;
        uint32_t ns = ts_language_symbol_count(L->lang);
        to->sym = xcalloc(ns ? ns : 1, 1);
        to->nsym = ns;
        for (uint32_t k = 0; k < ns; k++) {
            const char *t = ts_language_symbol_name(L->lang, (TSSymbol)k);
            if (!t) continue;
            if (ts_language_symbol_type(L->lang, (TSSymbol)k) != TSSymbolTypeRegular) {
                if (!strcmp(t, "&&") || !strcmp(t, "||") || !strcmp(t, "and") || !strcmp(t, "or")) to->sym[k] |= SYM_LOGIC;
                continue;
            }
            if (in_list(t, BRANCHES, sizeof BRANCHES / sizeof *BRANCHES)) to->sym[k] |= SYM_BRANCH;
            if (in_list(t, BLOCKS, sizeof BLOCKS / sizeof *BLOCKS)) to->sym[k] |= SYM_BLOCK;
            if (strstr(t, "binary") || strstr(t, "boolean_operator") || strstr(t, "logical")) to->sym[k] |= SYM_BINARY;
        }
        if (debug()) fprintf(stderr, "syntax %s: textobjects from %s\n", L->name, dirs[i]);
        return to;
    }
    return NULL;
}

static void textobj_free(TextObj *to) {
    if (!to) return;
    pats_free(to->pats, ts_query_pattern_count(to->q));
    ts_query_delete(to->q);
    free(to->sym);
    free(to);
}

static unsigned sym_flags(const TextObj *to, TSNode x) {
    TSSymbol k = ts_node_symbol(x);
    return k < to->nsym ? to->sym[k] : 0; /* ERROR is 0xFFFF */
}

/* 1 + branch points under def, and its deepest block nesting below its body. */
static void measure(const TextObj *to, TSNode def, int *cx, int *depth) {
    TSTreeCursor c = ts_tree_cursor_new(def);
    unsigned char *stack = NULL; /* per level: does that node open a block */
    int lvl = 0, cap = 0, d = 0, deep = 0, n = 1;
    for (bool enter = true;;) {
        if (enter) {
            TSNode x = ts_tree_cursor_current_node(&c);
            unsigned f = sym_flags(to, x);
            bool blk = f & SYM_BLOCK;
            n += (f & SYM_BRANCH) != 0;
            if (f & SYM_BINARY) /* && || and or */
                for (uint32_t i = 0, k = ts_node_child_count(x); i < k; i++)
                    if (sym_flags(to, ts_node_child(x, i)) & SYM_LOGIC) { n++; break; }
            if (lvl == cap) stack = xrealloc(stack, (size_t)(cap = cap ? cap * 2 : 64));
            stack[lvl++] = blk;
            d += blk;
            if (d > deep) deep = d;
            if (ts_tree_cursor_goto_first_child(&c)) continue;
        }
        d -= stack[--lvl]; /* leave the current node */
        if (ts_tree_cursor_goto_next_sibling(&c)) { enter = true; continue; }
        if (!lvl || !ts_tree_cursor_goto_parent(&c)) break;
        enter = false;
    }
    ts_tree_cursor_delete(&c);
    free(stack);
    *cx = n;
    *depth = deep > 1 ? deep - 1 : 0;
}

/* A def's name node: its name field, else down its declarator chain (C:
 * function_definition -> function_declarator -> identifier); an unnamed
 * struct takes its typedef's name. */
static TSNode def_name(TSNode n) {
    TSNode x = n;
    for (int i = 0; i < 8; i++) {
        TSNode k = ts_node_child_by_field_name(x, "name", 4);
        if (!ts_node_is_null(k)) return k;
        k = ts_node_child_by_field_name(x, "declarator", 10);
        if (ts_node_is_null(k)) break;
        x = k;
    }
    if (!ts_node_eq(x, n) && strstr(ts_node_type(x), "identifier")) return x;
    TSNode up = ts_node_parent(n);
    if (!ts_node_is_null(up) && !strcmp(ts_node_type(up), "type_definition")) {
        TSNode k = ts_node_child_by_field_name(up, "declarator", 10);
        if (!ts_node_is_null(k) && strstr(ts_node_type(k), "identifier")) return k;
    }
    return (TSNode){0};
}

typedef struct {
    TSNode node;     /* null for a def kept from the previous version */
    uint32_t a, b;   /* bytes */
    int cls;
    int keep;        /* index of the kept def, -1 */
} DefNode;

static int cmp_defnode(const void *pa, const void *pb) {
    const DefNode *x = pa, *y = pb;
    if (x->a != y->a) return x->a < y->a ? -1 : 1;
    if (x->b != y->b) return x->b > y->b ? -1 : 1; /* outer first */
    return x->cls - y->cls;
}

struct SynParse {
    SynLang *l;
    char *text;
    uint32_t len;
    TSTree *tree;
    SynDef *defs; /* of text, with their bytes */
    uint32_t *da, *db;
    int nd;
};

SynParse *syn_parse_new(void) { return xcalloc(1, sizeof(SynParse)); }

static void parse_forget(SynParse *p) {
    if (p->tree) ts_tree_delete(p->tree);
    free(p->text);
    syn_defs_free(p->defs, p->nd);
    free(p->da);
    free(p->db);
    SynLang *l = p->l;
    memset(p, 0, sizeof *p);
    p->l = l;
}

void syn_parse_free(SynParse *p) {
    if (!p) return;
    parse_forget(p);
    free(p);
}

static TSPoint point_at(const char *text, uint32_t off) {
    TSPoint p = {0, 0};
    for (uint32_t i = 0; i < off; i++)
        if (text[i] == '\n') p.row++, p.column = 0;
        else p.column++;
    return p;
}

static void push_def(DefNode **dn, int *n, int *cap, DefNode d) {
    if (*n == *cap) *dn = xrealloc(*dn, (size_t)(*cap = *cap ? *cap * 2 : 32) * sizeof **dn);
    (*dn)[(*n)++] = d;
}

/* The query's function and class captures within [lo, hi) of the tree. */
static void query_defs(const TextObj *to, TSTree *tree, const char *text, uint32_t lo, uint32_t hi, DefNode **dn, int *n,
                       int *cap) {
    TSQueryCursor *cur = ts_query_cursor_new();
    ts_query_cursor_set_byte_range(cur, lo, hi);
    ts_query_cursor_exec(cur, to->q, ts_tree_root_node(tree));
    TSQueryMatch m;
    while (ts_query_cursor_next_match(cur, &m)) {
        const Pattern *pt = &to->pats[m.pattern_index];
        if (pt->dead) continue;
        bool ok = true;
        for (int i = 0; i < pt->n && ok; i++) ok = pred_ok(text, &pt->p[i], &m);
        if (!ok) continue;
        for (uint16_t i = 0; i < m.capture_count; i++) {
            int ci = (int)m.captures[i].index;
            if (ci != to->fcap && ci != to->ccap) continue;
            TSNode x = m.captures[i].node;
            uint32_t a = ts_node_start_byte(x), b = ts_node_end_byte(x);
            if (b <= lo || a >= hi) continue;
            /* A prototype (C's `int f(void);` is a declaration with a function
             * declarator) declares, it doesn't define. */
            const char *t = ts_node_type(x);
            size_t tl = strlen(t);
            if (ci == to->fcap && tl >= 11 && !strcmp(t + tl - 11, "declaration") &&
                ts_node_is_null(ts_node_child_by_field_name(x, "body", 4)))
                continue;
            push_def(dn, n, cap, (DefNode){x, a, b, ci == to->ccap, -1});
        }
    }
    ts_query_cursor_delete(cur);
}

int syn_defs(const char *path, const char *text, uint32_t len, SynDef **out, SynParse *prev) {
    *out = NULL;
    SynLang *L = syn_lang_for(path);
    if (!L) return 0;
    pthread_mutex_lock(&g_mu);
    bool tried = L->to_tried;
    const TextObj *to = L->to;
    pthread_mutex_unlock(&g_mu);
    if (!tried) { /* as languages: loaded outside the lock, kept if first */
        TextObj *mine = load_textobjects(L);
        pthread_mutex_lock(&g_mu);
        if (!L->to_tried) {
            L->to = mine;
            L->to_tried = true;
            mine = NULL;
        }
        to = L->to;
        pthread_mutex_unlock(&g_mu);
        textobj_free(mine);
    }
    if (!to) return 0;
    if (prev && prev->l != L) parse_forget(prev), prev->l = L;
    /* The previous version's tree, edited where the texts differ (common
     * prefix and suffix kept), makes the reparse incremental; and its defs
     * outside what changed carry over, so the query runs only there. */
    TSTree *old = NULL;
    uint32_t pre = 0, suf = 0;
    TSInputEdit e = {0};
    if (prev && prev->tree) {
        uint32_t n = prev->len < len ? prev->len : len;
        while (pre < n && prev->text[pre] == text[pre]) pre++;
        while (suf < n - pre && prev->text[prev->len - 1 - suf] == text[len - 1 - suf]) suf++;
        e = (TSInputEdit){pre, prev->len - suf, len - suf, point_at(text, pre), {0, 0}, {0, 0}};
        e.old_end_point = point_at(prev->text, e.old_end_byte);
        e.new_end_point = point_at(text, e.new_end_byte);
        old = ts_tree_copy(prev->tree);
        ts_tree_edit(old, &e);
    }
    TSParser *ps = ts_parser_new();
    TSTree *tree = ts_parser_set_language(ps, L->lang) ? ts_parser_parse_string(ps, old, text, len) : NULL;
    /* Error recovery can come out differently incrementally, and restructure
     * past the changed ranges: a text with errors is parsed afresh and its
     * defs found anew. */
    bool broken = tree && ts_node_has_error(ts_tree_root_node(tree));
    if (old && (broken || ts_node_has_error(ts_tree_root_node(prev->tree)))) {
        ts_tree_delete(old);
        old = NULL;
        if (broken) {
            ts_tree_delete(tree);
            tree = ts_parser_parse_string(ps, NULL, text, len);
        }
    }
    ts_parser_delete(ps);
    if (!tree) {
        if (old) ts_tree_delete(old);
        return 0;
    }
    DefNode *dn = NULL;
    int n = 0, cap = 0;
    uint32_t lo = 0, hi = len; /* where to query */
    if (old) {
        lo = e.start_byte, hi = e.new_end_byte;
        uint32_t nr;
        TSRange *r = ts_tree_get_changed_ranges(old, tree, &nr);
        for (uint32_t i = 0; i < nr; i++) {
            if (r[i].start_byte < lo) lo = r[i].start_byte;
            if (r[i].end_byte > hi) hi = r[i].end_byte;
        }
        free(r);
        /* Whole lines, and the defs touching them: a typedef's name after its
         * struct's `}` changes the struct's name. */
        while (lo && text[lo - 1] != '\n') lo--;
        while (hi < len && text[hi] != '\n') hi++;
        if (lo) lo--;
        if (hi < len) hi++;
        int64_t shift = (int64_t)len - (int64_t)prev->len;
        for (int i = 0; i < prev->nd; i++) {
            uint32_t a = prev->da[i], b = prev->db[i];
            if (b > e.start_byte && a < e.old_end_byte) continue; /* edited */
            if (a >= e.old_end_byte) a = (uint32_t)(a + shift), b = (uint32_t)(b + shift);
            if (b > lo && a < hi) continue; /* changed: queried again */
            push_def(&dn, &n, &cap, (DefNode){.a = a, .b = b, .cls = prev->defs[i].cls, .keep = i});
        }
        ts_tree_delete(old);
    }
    query_defs(to, tree, text, lo, hi, &dn, &n, &cap);
    int rows = old ? (int)e.new_end_point.row - (int)e.old_end_point.row : 0;
    if (n) qsort(dn, (size_t)n, sizeof *dn, cmp_defnode);
    SynDef *d = n ? xcalloc((size_t)n, sizeof *d) : NULL;
    uint32_t *da = n ? xmalloc((size_t)n * sizeof *da) : NULL, *db = n ? xmalloc((size_t)n * sizeof *db) : NULL;
    int nd = 0, *open = n ? xmalloc((size_t)n * sizeof *open) : NULL, nopen = 0; /* enclosing defs */
    for (int i = 0; i < n; i++) {
        if (nd && dn[i].a == da[nd - 1] && dn[i].b == db[nd - 1]) continue; /* captured twice */
        SynDef *f = &d[nd];
        if (dn[i].keep >= 0) {
            const SynDef *k = &prev->defs[dn[i].keep];
            *f = *k;
            f->name = xstrdup(k->name);
            if (prev->da[dn[i].keep] >= e.old_end_byte) f->a += rows, f->b += rows;
        } else {
            TSNode nm = def_name(dn[i].node);
            if (ts_node_is_null(nm)) continue;
            uint32_t s = ts_node_start_byte(nm), z = ts_node_end_byte(nm);
            const char *nl = memchr(text + s, '\n', z - s);
            if (nl) z = (uint32_t)(nl - text);
            if (z - s > 200) z = s + 200;
            f->name = xstrndup(text + s, z - s);
            f->cls = dn[i].cls;
            TSPoint p0 = ts_node_start_point(dn[i].node), p1 = ts_node_end_point(dn[i].node);
            f->a = (int)p0.row + 1;
            f->b = (int)p1.row + 1 - (p1.column == 0 && p1.row > p0.row); /* ends at a line start: not that line */
            measure(to, dn[i].node, &f->cx, &f->depth);
        }
        while (nopen && db[open[nopen - 1]] <= dn[i].a) nopen--;
        f->parent = nopen ? open[nopen - 1] : -1;
        da[nd] = dn[i].a;
        db[nd] = dn[i].b;
        open[nopen++] = nd;
        nd++;
    }
    free(open);
    free(dn);
    if (prev) { /* this version is the next one's previous */
        parse_forget(prev);
        prev->text = xstrndup(text, len);
        prev->len = len;
        prev->tree = tree;
        prev->defs = xmalloc((size_t)(nd ? nd : 1) * sizeof *prev->defs);
        for (int i = 0; i < nd; i++) prev->defs[i] = d[i], prev->defs[i].name = xstrdup(d[i].name);
        prev->da = da;
        prev->db = db;
        prev->nd = nd;
    } else {
        ts_tree_delete(tree);
        free(da);
        free(db);
    }
    if (!nd) { free(d); d = NULL; }
    *out = d;
    return nd;
}

void syn_defs_free(SynDef *d, int n) {
    for (int i = 0; i < n; i++) free(d[i].name);
    free(d);
}
