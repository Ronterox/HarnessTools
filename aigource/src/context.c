/* The context view's data: a port of draft/context/build_ctx.py for one agent,
 * and of what the draft page's prepare() derives from it. docs/CONTEXT.md is
 * the spec; each rule there names the function here that implements it.
 *
 * The draft is Python, and its numbers depend on Python's semantics: lengths
 * are code points, \s and \w are Unicode classes, json.dumps has its own
 * spacing, float repr and escapes (pyjson.c), round() rounds half to even.
 * The helpers below reproduce them; where they only approximate (\w beyond
 * ASCII), CONTEXT.md says so. */
#include "context.h"

#include <dirent.h>
#include <limits.h>
#include <locale.h>
#include <math.h>
#include <pthread.h>
#include <pwd.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "pyjson.h"
#include "util.h"
#include "yyjson.h"


const char *const ctx_kind_names[CK_COUNT] = {"system", "tools", "injected", "prompt", "summary",
                                              "text", "thinking", "action", "result", "missing"};

/* Prices per million tokens, from the claude-api skill bundled with Claude
 * Code 2.1.306 (model table "cached: 2026-10-06"; shared/models.md;
 * shared/prompt-caching.md "Economics": cache writes 1.25x input for the
 * 5-minute TTL, 2x for 1 hour; cache reads $0.20 on Opus 5.5 and Sonnet 5.5).
 * The same table as PRICES in build_ctx.py. Models not listed get tokens only. */
const CtxPrice ctx_prices[] = {
    {"claude-opus-5-5", 4.00, 0.20, 5.00, 8.00, 20.00},
    {"claude-sonnet-5-5", 2.00, 0.20, 2.50, 4.00, 10.00},
};
const int ctx_nprices = sizeof ctx_prices / sizeof *ctx_prices;
const char ctx_price_source[] = "claude-api skill (Claude Code 2.1.306), model table cached 2026-10-06; "
                                "cache writes 1.25x input (5 min) / 2x (1 h), per its prompt-caching guide";
static const struct { const char *model; int tokens; } WINDOWS[] = {
    {"claude-opus-5-5", 1000000}, {"claude-sonnet-5-5", 1000000}, {"claude-haiku-4-5", 200000}};

const CtxPrice *ctx_price(const char *model) {
    for (int i = 0; model && i < ctx_nprices; i++)
        if (!strcmp(ctx_prices[i].model, model)) return &ctx_prices[i];
    return NULL;
}

int ctx_window(const char *model) {
    for (size_t i = 0; model && i < sizeof WINDOWS / sizeof *WINDOWS; i++)
        if (!strcmp(WINDOWS[i].model, model)) return WINDOWS[i].tokens;
    return 0;
}

/* ---- Python's view of text ---- */

typedef struct {
    const char *s;
    size_t n;
} Str;

static Str S_(const char *s) { return (Str){s, s ? strlen(s) : 0}; }

/* The code point at s[i] (valid UTF-8: yyjson checks), its length in *k. */
static unsigned cp_at(const char *s, size_t n, size_t i, int *k) {
    const unsigned char *p = (const unsigned char *)s + i;
    int len = p[0] < 0x80 ? 1 : p[0] < 0xE0 ? 2 : p[0] < 0xF0 ? 3 : 4;
    if (i + (size_t)len > n) len = 1;
    unsigned c = len == 1 ? p[0] : len == 2 ? p[0] & 0x1F : len == 3 ? p[0] & 0x0F : p[0] & 0x07;
    for (int j = 1; j < len; j++) c = c << 6 | (p[j] & 0x3F);
    if (k) *k = len;
    return c;
}

static unsigned cp_before(const char *s, size_t i) {
    size_t j = i;
    while (j > 0 && ((unsigned char)s[j - 1] & 0xC0) == 0x80) j--;
    if (j > 0) j--;
    return cp_at(s, i, j, NULL);
}

static size_t nchars(const char *s, size_t n) { /* len() of a str */
    size_t c = 0;
    for (size_t i = 0; i < n; i++) c += ((unsigned char)s[i] & 0xC0) != 0x80;
    return c;
}

static size_t char_off(const char *s, size_t n, size_t k) { /* byte offset of character k (s[:k]) */
    size_t c = 0;
    for (size_t i = 0; i < n; i++)
        if (((unsigned char)s[i] & 0xC0) != 0x80 && c++ == k) return i;
    return n;
}

/* str.isspace() and re's \s on a str */
static int py_space(unsigned c) {
    return (c >= 0x09 && c <= 0x0D) || (c >= 0x1C && c <= 0x20) || c == 0x85 || c == 0xA0 || c == 0x1680 ||
           (c >= 0x2000 && c <= 0x200A) || c == 0x2028 || c == 0x2029 || c == 0x202F || c == 0x205F || c == 0x3000;
}

/* re's \w on a str: exact for ASCII; beyond it letters and digits are
 * approximated by blocks (punctuation, symbols, arrows, emoji, combining marks
 * and private use are not word characters). */
static int py_word(unsigned c) {
    if (c < 0x80) return (c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || c == '_';
    if (c < 0xC0)
        return c == 0xAA || c == 0xB2 || c == 0xB3 || c == 0xB5 || c == 0xB9 || c == 0xBA || (c >= 0xBC && c <= 0xBE);
    if (c == 0xD7 || c == 0xF7) return 0;
    if ((c >= 0x300 && c <= 0x36F) || (c >= 0x2000 && c <= 0x2BFF) || (c >= 0x3000 && c <= 0x303F) ||
        (c >= 0xE000 && c <= 0xF8FF) || (c >= 0xFE00 && c <= 0xFE6F) || (c >= 0xFF00 && c <= 0xFF0F) ||
        (c >= 0xFF1A && c <= 0xFF20) || (c >= 0x1F000 && c <= 0x1FFFF))
        return 0;
    return 1;
}

static int word_at(const char *s, size_t n, size_t i) { return i < n && py_word(cp_at(s, n, i, NULL)); }
static int word_before(const char *s, size_t i) { return i > 0 && py_word(cp_before(s, i)); }

static size_t skip_space(const char *s, size_t n, size_t i) { /* \s* */
    int k;
    while (i < n && py_space(cp_at(s, n, i, &k))) i += (size_t)k;
    return i;
}

static Str lstrip(Str t) {
    size_t i = skip_space(t.s, t.n, 0);
    return (Str){t.s + i, t.n - i};
}

static Str strip(Str t) {
    t = lstrip(t);
    while (t.n && py_space(cp_before(t.s, t.n))) {
        size_t j = t.n - 1;
        while (j > 0 && ((unsigned char)t.s[j] & 0xC0) == 0x80) j--;
        t.n = j;
    }
    return t;
}

static int starts(Str t, const char *p) {
    size_t k = strlen(p);
    return t.n >= k && !memcmp(t.s, p, k);
}

static int contains(Str t, const char *p) { return memmem(t.s, t.n, p, strlen(p)) != NULL; }

static int ends(const char *s, const char *suf) {
    size_t n = strlen(s), m = strlen(suf);
    return n >= m && !strcmp(s + n - m, suf);
}

/* one_line: " ".join(s.split()), cut to n characters with "…". */
static void one_line(PyBuf *o, Str s, size_t n) {
    size_t start = o->n;
    for (size_t i = 0; i < s.n;) {
        i = skip_space(s.s, s.n, i);
        if (i >= s.n) break;
        size_t j = i;
        int k;
        while (j < s.n && !py_space(cp_at(s.s, s.n, j, &k))) j += (size_t)k;
        if (o->n > start) pyb_put(o, " ", 1);
        pyb_put(o, s.s + i, j - i);
        i = j;
    }
    size_t len = nchars(o->s ? o->s + start : "", o->n - start);
    if (len > n) {
        o->n = start + char_off(o->s + start, o->n - start, n - 1);
        pyb_puts(o, "\xE2\x80\xA6");
    }
}

/* str(x) for a JSON value: a string as it is, else roughly Python's repr. */
static void py_str(PyBuf *o, yyjson_val *v) {
    size_t n0 = o->n;
    if (!v || yyjson_is_null(v)) pyb_puts(o, "None");
    else if (yyjson_is_str(v)) pyb_put(o, yyjson_get_str(v), yyjson_get_len(v));
    else if (yyjson_is_bool(v)) pyb_puts(o, yyjson_get_bool(v) ? "True" : "False");
    else if (pyj_dumps(o, v, 0)) { /* too deep: not a partial dump */
        o->n = n0;
        pyb_puts(o, "[nested too deeply to show]");
    }
}

/* Python truthiness of a JSON value. */
static int truthy(yyjson_val *v) {
    if (!v) return 0;
    switch (yyjson_get_type(v)) {
    case YYJSON_TYPE_BOOL: return yyjson_get_bool(v);
    case YYJSON_TYPE_NUM: return yyjson_get_num(v) != 0;
    case YYJSON_TYPE_RAW: return 1;
    case YYJSON_TYPE_STR: return yyjson_get_len(v) > 0;
    case YYJSON_TYPE_ARR: return yyjson_arr_size(v) > 0;
    case YYJSON_TYPE_OBJ: return yyjson_obj_size(v) > 0;
    default: return 0;
    }
}

static yyjson_val *get(yyjson_val *o, const char *k) { return yyjson_is_obj(o) ? yyjson_obj_get(o, k) : NULL; }
static const char *gets(yyjson_val *o, const char *k) { return yyjson_get_str(get(o, k)); }
static Str getstr(yyjson_val *o, const char *k) { /* o.get(k, "") as a string */
    yyjson_val *v = get(o, k);
    return yyjson_is_str(v) ? (Str){yyjson_get_str(v), yyjson_get_len(v)} : (Str){"", 0};
}
/* int(v) for a JSON number (a bool is 1 or 0, a string is parsed), saturated
 * to 64 bits: Python's ints have no limit, C's casts of huge reals are UB. */
static long long num_ll(yyjson_val *v) {
    double d;
    switch (yyjson_get_subtype(v) | yyjson_get_type(v)) {
    case YYJSON_TYPE_NUM | YYJSON_SUBTYPE_UINT: {
        uint64_t u = yyjson_get_uint(v);
        return u > (uint64_t)LLONG_MAX ? LLONG_MAX : (long long)u;
    }
    case YYJSON_TYPE_NUM | YYJSON_SUBTYPE_SINT: return yyjson_get_sint(v);
    case YYJSON_TYPE_NUM | YYJSON_SUBTYPE_REAL: d = yyjson_get_real(v); break;
    case YYJSON_TYPE_RAW: d = strtod(yyjson_get_raw(v), NULL); break;
    case YYJSON_TYPE_STR: return strtoll(yyjson_get_str(v), NULL, 10); /* saturates */
    case YYJSON_TYPE_BOOL | YYJSON_SUBTYPE_TRUE: return 1;
    default: return 0;
    }
    if (d != d) return 0;
    return d >= 9.2e18 ? LLONG_MAX : d <= -9.2e18 ? LLONG_MIN : (long long)d;
}

/* o.get(k) or 0, for counts (tokens, keep, indices): clamped to +-1e15, far
 * past any real one, so that sums and differences of them can't overflow. */
#define COUNT_MAX 1000000000000000LL
static long long geti(yyjson_val *o, const char *k) {
    long long v = num_ll(get(o, k));
    return v > COUNT_MAX ? COUNT_MAX : v < -COUNT_MAX ? -COUNT_MAX : v;
}
static int geti32(yyjson_val *o, const char *k) { /* an index or a count: clamped well inside int */
    long long v = geti(o, k);
    return v > 1000000000 ? 1000000000 : v < -1000000000 ? -1000000000 : (int)v;
}

/* datetime.fromisoformat(ts).timestamp(), exactly: whole microseconds / 1e6. */
static double epoch(const char *ts) {
    if (!ts) return 0;
    int y, mo, d, h, mi, s, k = 0;
    if (sscanf(ts, "%d-%d-%dT%d:%d:%d%n", &y, &mo, &d, &h, &mi, &s, &k) != 6) return 0;
    const char *p = ts + k;
    long us = 0;
    if (*p == '.') {
        int nd = 0;
        for (p++; *p >= '0' && *p <= '9'; p++)
            if (nd < 6) us = us * 10 + (*p - '0'), nd++;
        for (; nd < 6; nd++) us *= 10;
    }
    long off = 0;
    if ((*p == '+' || *p == '-') && p[1]) {
        int oh = 0, om = 0;
        sscanf(p + 1, "%d:%d", &oh, &om);
        off = (*p == '-' ? -1 : 1) * (oh * 3600L + om * 60L);
    }
    struct tm tm = {.tm_year = y - 1900, .tm_mon = mo - 1, .tm_mday = d, .tm_hour = h, .tm_min = mi, .tm_sec = s};
    long long sec = (long long)timegm(&tm) - off;
    return (double)(sec * 1000000LL + us) / 1e6;
}

/* Numbers are written and read with printf/strtod, which follow the locale: a
 * comma-decimal locale would change every hash. The public entry points run
 * in the "C" numeric locale on their thread (uselocale), whatever the app's. */
static locale_t c_numeric_loc;
static void make_c_numeric(void) { c_numeric_loc = newlocale(LC_NUMERIC_MASK, "C", (locale_t)0); }

static locale_t c_numeric(void) { /* the thread's locale before */
    static pthread_once_t once = PTHREAD_ONCE_INIT;
    pthread_once(&once, make_c_numeric);
    return c_numeric_loc ? uselocale(c_numeric_loc) : uselocale((locale_t)0);
}

static double round1(double x) { /* round(x, 1): correctly rounded, ties to even */
    char b[64];
    snprintf(b, sizeof b, "%.1f", x);
    return strtod(b, NULL);
}

/* ---- paths, as os.path does them ---- */

typedef struct {
    char *root, *home, *config;
} Env;

static char *expanduser(const char *p) {
    if (p[0] != '~') return xstrdup(p);
    const char *slash = strchr(p, '/');
    size_t i = slash ? (size_t)(slash - p) : strlen(p);
    const char *home = NULL;
    char pwbuf[4096];
    struct passwd pwd, *pw = NULL;
    if (i == 1) home = getenv("HOME");
    if (!home) { /* the _r forms: loads may run on worker threads */
        char name[256];
        if (i == 1) getpwuid_r(getuid(), &pwd, pwbuf, sizeof pwbuf, &pw);
        else if (i - 1 < sizeof name) {
            memcpy(name, p + 1, i - 1);
            name[i - 1] = 0;
            getpwnam_r(name, &pwd, pwbuf, sizeof pwbuf, &pw);
        }
        home = pw ? pw->pw_dir : NULL;
        if (!home) return xstrdup(p);
    }
    size_t hn = strlen(home);
    while (hn && home[hn - 1] == '/') hn--;
    PyBuf o = {0};
    pyb_put(&o, home, hn);
    pyb_puts(&o, p + i);
    if (!o.n) pyb_puts(&o, "/");
    return o.s;
}

static char *normpath(const char *p) {
    if (!*p) return xstrdup(".");
    int initial = p[0] == '/' ? (p[1] == '/' && p[2] != '/' ? 2 : 1) : 0;
    char *copy = xstrdup(p);
    const char **comps = xmalloc((strlen(p) / 2 + 2) * sizeof *comps);
    int nc = 0;
    for (char *tok, *save = NULL, *q = copy; (tok = strtok_r(q, "/", &save)); q = NULL) {
        if (!strcmp(tok, ".")) continue;
        if (strcmp(tok, "..") || (!initial && !nc) || (nc && !strcmp(comps[nc - 1], ".."))) comps[nc++] = tok;
        else if (nc) nc--;
    }
    PyBuf o = {0};
    for (int i = 0; i < initial; i++) pyb_puts(&o, "/");
    for (int i = 0; i < nc; i++) {
        if (i) pyb_puts(&o, "/");
        pyb_puts(&o, comps[i]);
    }
    if (!o.n) pyb_puts(&o, ".");
    free(comps);
    free(copy);
    return o.s;
}

static char *path_join(const char *a, const char *b) {
    if (b[0] == '/' || !*a) return xstrdup(b);
    PyBuf o = {0};
    pyb_puts(&o, a);
    if (a[strlen(a) - 1] != '/') pyb_puts(&o, "/");
    pyb_puts(&o, b);
    return o.s;
}

static const char *base_name(const char *p) {
    const char *s = strrchr(p, '/');
    return s ? s + 1 : p;
}

/* os.path.splitext(p)[1], lowercased (ASCII) into ext. */
static void ext_of(const char *p, size_t n, char *ext, size_t cap) {
    ext[0] = 0;
    size_t sep = 0, dot = SIZE_MAX;
    for (size_t i = 0; i < n; i++) {
        if (p[i] == '/') sep = i + 1, dot = SIZE_MAX;
        else if (p[i] == '.') dot = i;
    }
    if (dot == SIZE_MAX) return;
    size_t f = sep;
    while (f < dot && p[f] == '.') f++;
    if (f == dot) return;
    size_t k = 0;
    for (size_t i = dot; i < n && k + 1 < cap; i++) {
        char c = p[i];
        ext[k++] = c >= 'A' && c <= 'Z' ? c + 32 : c;
    }
    ext[k] = 0;
}

static const char *LANG[][2] = {
    {".c", "c"}, {".h", "c"}, {".py", "python"}, {".js", "javascript"}, {".mjs", "javascript"},
    {".ts", "typescript"}, {".sh", "bash"}, {".md", "markdown"}, {".json", "json"}, {".html", "xml"},
    {".xml", "xml"}, {".css", "css"}, {".lua", "lua"}, {".vim", "vim"}, {".yaml", "yaml"}, {".yml", "yaml"},
    {".toml", "ini"}, {".rs", "rust"}, {".go", "go"}, {".cpp", "cpp"}, {".hpp", "cpp"}, {".java", "java"},
    {".rb", "ruby"}, {".txt", "plaintext"}};
static const char *FILE_EXT[] = {".png", ".jpg", ".jpeg", ".log", ".jsonl", ".svg", ".csv", ".diff",
                                 ".patch", ".cfg", ".ini", ".conf", ".tpl", ".mk", ".lock", ".out",
                                 ".tgz", ".gz", ".tar", ".pdf", ".so", ".a", ".o"};

static const char *lang_ext(const char *ext) {
    for (size_t i = 0; i < sizeof LANG / sizeof *LANG; i++)
        if (!strcmp(LANG[i][0], ext)) return LANG[i][1];
    return NULL;
}

static int known_ext(const char *ext) {
    if (lang_ext(ext)) return 1;
    for (size_t i = 0; i < sizeof FILE_EXT / sizeof *FILE_EXT; i++)
        if (!strcmp(FILE_EXT[i], ext)) return 1;
    return 0;
}

static const char *lang_of(const char *p) {
    if (!p) return NULL;
    const char *b = base_name(p);
    if (!strcmp(b, "Makefile") || !strcmp(b, "makefile")) return "makefile";
    char ext[16];
    ext_of(b, strlen(b), ext, sizeof ext);
    return lang_ext(ext);
}

/* rel: repo-relative under the root, ~/... under home, else as it is. */
static char *rel(const Env *e, const char *p) {
    size_t rn = strlen(e->root), hn = strlen(e->home);
    if (rn && !strncmp(p, e->root, rn) && p[rn] == '/') return xstrdup(p + rn + 1);
    if (!strncmp(p, e->home, hn) && p[hn] == '/') {
        char *o = xmalloc(strlen(p) - hn + 2);
        o[0] = '~';
        strcpy(o + 1, p + hn);
        return o;
    }
    return xstrdup(p);
}

/* ---- string pools: texts are stored once ---- */

typedef struct {
    char **s;
    size_t *len;
    uint64_t *hash;
    int n, cap;
    int *ix; /* open addressing, -1 empty */
    size_t ixcap;
} Pool;

static uint64_t fnv(const char *s, size_t n) {
    uint64_t h = 1469598103934665603ULL;
    for (size_t i = 0; i < n; i++) h = (h ^ (unsigned char)s[i]) * 1099511628211ULL;
    return h;
}

static int pool_find(const Pool *p, const char *s, size_t n) {
    if (!p->ixcap) return -1;
    uint64_t h = fnv(s, n);
    for (size_t i = h & (p->ixcap - 1);; i = (i + 1) & (p->ixcap - 1)) {
        int k = p->ix[i];
        if (k < 0) return -1;
        if (p->hash[k] == h && p->len[k] == n && !memcmp(p->s[k], s, n)) return k;
    }
}

static int pool_add(Pool *p, const char *s, size_t n) {
    int k = pool_find(p, s, n);
    if (k >= 0) return k;
    if ((size_t)(p->n + 1) * 2 > p->ixcap) {
        size_t cap = p->ixcap ? p->ixcap * 2 : 1024;
        int *ix = xmalloc(cap * sizeof *ix);
        memset(ix, -1, cap * sizeof *ix);
        for (int j = 0; j < p->n; j++) {
            size_t i = p->hash[j] & (cap - 1);
            while (ix[i] >= 0) i = (i + 1) & (cap - 1);
            ix[i] = j;
        }
        free(p->ix);
        p->ix = ix;
        p->ixcap = cap;
    }
    if (p->n == p->cap) {
        p->cap = p->cap ? p->cap * 2 : 256;
        p->s = xrealloc(p->s, (size_t)p->cap * sizeof *p->s);
        p->len = xrealloc(p->len, (size_t)p->cap * sizeof *p->len);
        p->hash = xrealloc(p->hash, (size_t)p->cap * sizeof *p->hash);
    }
    k = p->n++;
    p->s[k] = xstrndup(s, n);
    p->len[k] = n;
    p->hash[k] = fnv(s, n);
    size_t i = p->hash[k] & (p->ixcap - 1);
    while (p->ix[i] >= 0) i = (i + 1) & (p->ixcap - 1);
    p->ix[i] = k;
    return k;
}

static const char *pool_str(Pool *p, const char *s, size_t n) {
    int k = pool_add(p, s, n); /* before reading p->s: adding may move it */
    return p->s[k];
}

static void pool_free(Pool *p, int keep_strings) {
    if (!keep_strings)
        for (int i = 0; i < p->n; i++) free(p->s[i]);
    if (!keep_strings) free(p->s), free(p->len);
    free(p->hash);
    free(p->ix);
}

/* ---- transcripts ---- */

enum { T_OTHER, T_USER, T_ASSISTANT, T_ATTACH, T_SYSTEM, T_REQ, T_BLOB, T_SHAPE, T_TITLE };

typedef struct {
    yyjson_doc *doc;
    yyjson_val *r;
    int type;
    long abs;  /* index among the file's records (the ones kept), from its start */
    off_t off; /* file offset of its line */
    size_t len; /* bytes of its line, newline included */
} Rec;

typedef struct {
    Rec *r;
    int n, cap;
} Recs;

static int rec_type(yyjson_val *r) {
    const char *t = gets(r, "type");
    if (!t) return T_OTHER;
    static const char *names[] = {"user", "assistant", "attachment", "system", "api-request", "api-request-blob",
                                  "api-request-shape", "ai-title"};
    for (int i = 0; i < 8; i++)
        if (!strcmp(t, names[i])) return i + 1;
    return T_OTHER;
}

/* One line: a record the context needs is kept (others are dropped as read;
 * lines that aren't JSON objects are skipped, as json.loads failures are).
 * Returns 1 for a JSON line, kept or not. */
static int take_line(Recs *out, const char *line, size_t n, long *abs, off_t off) {
    yyjson_doc *doc = yyjson_read_opts((char *)line, n, PYJ_READ, NULL, NULL);
    yyjson_val *r = yyjson_doc_get_root(doc);
    int t = rec_type(r);
    if (!doc || !yyjson_is_obj(r) || t == T_OTHER) {
        yyjson_doc_free(doc);
        return doc != NULL;
    }
    GROW(out->r, out->n, out->cap);
    out->r[out->n++] = (Rec){doc, r, t, (*abs)++, off, n};
    return 1;
}

/* Every record of a file. *whole: the bytes up to the end of its last
 * newline-terminated line; *tail_json: a last line without its newline that
 * still parsed (it counts, as the draft reads it too). */
static int read_records_at(const char *path, Recs *out, atomic_int *cancel, off_t *whole, int *tail_json) {
    FILE *f = fopen(path, "r");
    if (!f) return -1;
    char *line = NULL;
    size_t cap = 0;
    ssize_t n;
    off_t pos = 0;
    long abs = 0;
    if (whole) *whole = 0;
    if (tail_json) *tail_json = 0;
    for (long k = 0; (n = getline(&line, &cap, f)) > 0; k++) {
        if (cancel && !(k & 1023) && atomic_load(cancel)) break;
        int json = take_line(out, line, (size_t)n, &abs, pos);
        pos += n;
        if (line[n - 1] == '\n') {
            if (whole) *whole = pos;
        } else if (tail_json) *tail_json = json;
    }
    free(line);
    fclose(f);
    return 0;
}

static int read_records(const char *path, Recs *out, atomic_int *cancel) {
    return read_records_at(path, out, cancel, NULL, NULL);
}

static void recs_free(Recs *rs) {
    for (int i = 0; i < rs->n; i++) yyjson_doc_free(rs->r[i].doc);
    free(rs->r);
    memset(rs, 0, sizeof *rs);
}

/* ---- messages and their hashes ---- */

enum { ROLE_USER, ROLE_ASSISTANT, ROLE_SYSTEM, ROLE_OTHER };

typedef struct {
    yyjson_val *v;
    int wrap; /* v is a string content, standing for {"type": "text", "text": v} (norm) */
} Blk;

typedef struct {
    yyjson_val *v; /* the message as recorded; NULL when rebuilt from records */
    int role;      /* rebuilt: ROLE_* */
    int b0, nb;    /* rebuilt: its content blocks in Build.blk */
    long src;      /* the record it comes from (a run's first, a response's first), absolute; -1 another file's */
} Msg;

typedef struct {
    int *recs; /* records of one response (one message id), in order (in Store.recs; none for one kept as
                * metadata from an earlier load: old) */
    int n;
    uint8_t d[32]; /* H(response_message) */
    int bad;       /* nested too deeply to hash */
    /* the agent's own responses (meta): what the calls need of them, owned */
    char *mid, *ref, *rq, *model; /* message id; requestRef of its first record; requestId and model of its last */
    int has_u;
    long long u[5];       /* usage_of its last record's usage */
    long long out_t, th_t; /* that usage's output and thinking tokens */
    char **keys;          /* its blocks' ids, else hashes (block_resp) */
    int nkeys;
    long first;           /* its first record, absolute */
    int nrec, old;
} Resp;

typedef struct { /* sha256 digest -> int */
    uint8_t (*k)[32];
    int *v;
    size_t cap, n;
} DMap;

static size_t dslot(const DMap *m, const uint8_t *d) {
    uint64_t h;
    memcpy(&h, d, 8);
    for (size_t i = h & (m->cap - 1);; i = (i + 1) & (m->cap - 1))
        if (m->v[i] < 0 || !memcmp(m->k[i], d, 32)) return i;
}

static int dmap_get(const DMap *m, const uint8_t *d) { return m->cap ? m->v[dslot(m, d)] : -1; }

static void dmap_put(DMap *m, const uint8_t *d, int v, int replace) {
    if ((m->n + 1) * 2 > m->cap) {
        DMap g = {xmalloc((m->cap ? m->cap * 2 : 256) * 32), xmalloc((m->cap ? m->cap * 2 : 256) * sizeof(int)),
                  m->cap ? m->cap * 2 : 256, 0};
        memset(g.v, -1, g.cap * sizeof(int));
        for (size_t i = 0; i < m->cap; i++)
            if (m->v[i] >= 0) {
                size_t j = dslot(&g, m->k[i]);
                memcpy(g.k[j], m->k[i], 32);
                g.v[j] = m->v[i];
                g.n++;
            }
        free(m->k), free(m->v);
        *m = g;
    }
    size_t i = dslot(m, d);
    if (m->v[i] < 0) {
        memcpy(m->k[i], d, 32);
        m->v[i] = v;
        m->n++;
    } else if (replace) m->v[i] = v;
}

static void dmap_free(DMap *m) {
    free(m->k), free(m->v);
    memset(m, 0, sizeof *m);
}

static int hex_digest(const char *h, uint8_t d[32]) { /* a lowercase hex sha256, else 0 */
    if (!h || strlen(h) != 64) return 0;
    for (int i = 0; i < 64; i++) {
        int c = h[i], v = c >= '0' && c <= '9' ? c - '0' : c >= 'a' && c <= 'f' ? c - 'a' + 10 : -1;
        if (v < 0) return 0;
        if (i & 1) d[i / 2] |= (uint8_t)v;
        else d[i / 2] = (uint8_t)(v << 4);
    }
    return 1;
}

/* Everything one transcript can supply, by hash: blobs, user records, runs
 * of user records, responses (store in build_ctx.py). */
typedef struct {
    char *path;
    Recs recs;
    int loaded;
    DMap map; /* digest -> msgs */
    Msg *msgs;
    int nmsgs, capmsgs;
    Resp *resps; /* in order of first record */
    int nresps;
    StrMap resp_ix; /* message id -> resps */
} Store;

/* ---- one load ---- */

typedef struct {
    uint8_t d[32];
    int16_t sub;  /* split text segment, or 1000 + an injected segment of a result; -1 */
    uint8_t role; /* ROLE_* */
    uint8_t tag;  /* 0 a block, 1 an empty system message, 2 a missing message */
} Key;

typedef struct {
    const char *h; /* the message's hash, as sent */
    int k0, nk;    /* its blocks in Build.keys / Build.kitems */
} Inst;

typedef struct {
    const char *sh; /* shapeHash ("\x01" when none) */
    int *inst, ninst;
    const char *pmodel; /* its request's model */
    int kept;           /* messages it kept of its base */
} CallB; /* a call while building */

static char *dup_or_null(const char *s) { return s ? xstrdup(s) : NULL; }

/* ---- what a load keeps to resume (ctx_reload) ----
 *
 * A load reads the agent's records once; a reload reads only what was
 * appended. It resumes from the previous load's Loader: everything the next
 * records can extend (the calls' and instances' keys, the request shapes, the
 * responses before the window as metadata, the thinking drops, the stale-read
 * maps, the title), and the window: the raw lines from just after the last
 * response before the last request to the end, which the next requests'
 * messages are found in. Nothing else of the file. What appended data could
 * change in what was built already is detected (the sets below) and makes the
 * reload a full load instead. */
typedef struct {
    char *path;
    dev_t dev;
    ino_t ino;
    off_t off;
    char *ai, *first; /* its last ai-title, its first prompt typed as a string (one line) */
} TitleScan;

typedef struct {
    char *rq;
    int mi, bi;
} Drop;

typedef struct Loader {
    int ok;                    /* resumable: an exact build, whole lines */
    char *root, *home, *config; /* labels depend on them */
    dev_t dev;
    ino_t ino;
    off_t off;                 /* bytes taken: whole lines */
    unsigned char chk[256];    /* the bytes just before off: a rewritten file shows there */
    int nchk;
    char *win;                 /* the window's lines: file bytes woff..off */
    size_t nwin;
    off_t woff;
    long wabs, nabs;           /* absolute index of the window's first record; records taken */
    long last_req;             /* the last request taken (absolute index), -1 */
    char **ih;                 /* per instance: its message's hash */
    int nih;
    Key *keys;                 /* per instance block (the agent's inst items in order): its content key */
    int nkeys;
    char **csh, **cpm;         /* per call: shapeHash, its request's model (NULL none) */
    int *ckept;                /* per call: messages kept of its base */
    int **cextra, *ncextra;    /* per call: its instances between kept and the public tail (keep past the base) */
    int ncalls;
    StrMap shape_ix;           /* shapeHash -> shape_raw */
    char **shape_raw;          /* request shapes (the record's "shape", as JSON; the last one counts) */
    int nshape;
    Resp *old;                 /* responses before the window: metadata */
    int nold;
    Drop *drops;               /* thinking_drop attachments, in order */
    int ndrops;
    DMap old_only;             /* store entries from before the window: a message needed again means a full load */
    DMap unresolved;           /* hashes calls found nowhere or in another file: one appended means a full load */
    DMap found_else;           /* hashes found in another file (counted once) */
    DMap tail_seen;            /* hashes calls looked up: a response or blob appended with one means a full load */
    DMap rep_ix;               /* a looked-up hash -> rep: how its message was written (msg_rep): items
                                * depend on that, not only on its hash */
    uint8_t (*rep)[32];
    int nrep;
    StrMap block_missed;       /* block keys a changed response's lookup missed */
    StrMap used;               /* responses items took their weights from: message id -> its records then */
    StrMap rt_ix, cwd_ix;      /* tool_use id -> its result's time; -> its record's cwd (stale reads) */
    double *rt;
    char **cwd;
    int nrt, ncwd;
    char *ai_title, *first_prompt; /* the session title of a main transcript */
    TitleScan main;            /* a subagent: its session's main transcript, for the title */
    struct { char *path; dev_t dev; ino_t ino; off_t size; } *srcs; /* other files messages were found in */
    int nsrcs;
    StrMap wix;                /* links_of's words: name -> wnames */
    char **wnames;
    int nwn;
    struct LinkCache *lc;      /* per item (the first nlc): what links_of takes from it, which never changes */
    int nlc;
} Loader;

typedef struct LinkCache {
    int *w, nw;     /* a result's words, an action's (sorted ids) */
    char **p;       /* a result's files (Read, Bash, Grep: files_of), an action's paths; sorted */
    int np, done;   /* done: w and p computed */
    int act;        /* a result: the action its files came from (post links it; a later one may replace it) */
} LinkCache;

static void resp_clear(Resp *rr);

static char **strv_copy(char **v, int n) {
    char **o = xmalloc(((size_t)n + 1) * sizeof *o);
    for (int i = 0; i < n; i++) o[i] = v[i] ? xstrdup(v[i]) : NULL;
    return o;
}

static void strv_free(char **v, int n) {
    for (int i = 0; v && i < n; i++) free(v[i]);
    free(v);
}

static void *mem_copy(const void *p, size_t n) {
    void *o = xmalloc(n ? n : 1);
    if (n) memcpy(o, p, n);
    return o;
}

static void strmap_copy(StrMap *to, const StrMap *from) {
    memset(to, 0, sizeof *to);
    for (size_t i = 0; i < from->cap; i++)
        if (from->keys[i]) strmap_put(to, from->keys[i], from->vals[i]);
}

static void dmap_copy(DMap *to, const DMap *from) {
    *to = (DMap){from->cap ? mem_copy(from->k, from->cap * 32) : NULL,
                 from->cap ? mem_copy(from->v, from->cap * sizeof(int)) : NULL, from->cap, from->n};
}

static void resp_copy(Resp *to, const Resp *from) { /* metadata only */
    *to = *from;
    to->recs = NULL, to->n = 0;
    to->mid = dup_or_null(from->mid), to->ref = dup_or_null(from->ref);
    to->rq = dup_or_null(from->rq), to->model = dup_or_null(from->model);
    to->keys = strv_copy(from->keys, from->nkeys);
}

static void loader_free(Loader *l) {
    if (!l) return;
    free(l->root), free(l->home), free(l->config), free(l->win);
    strv_free(l->ih, l->nih);
    free(l->keys);
    strv_free(l->csh, l->ncalls), strv_free(l->cpm, l->ncalls);
    free(l->ckept);
    for (int i = 0; i < l->ncalls; i++) free(l->cextra[i]);
    free(l->cextra), free(l->ncextra);
    strmap_free(&l->shape_ix);
    strv_free(l->shape_raw, l->nshape);
    for (int i = 0; i < l->nold; i++) resp_clear(&l->old[i]);
    free(l->old);
    for (int i = 0; i < l->ndrops; i++) free(l->drops[i].rq);
    free(l->drops);
    dmap_free(&l->old_only), dmap_free(&l->unresolved), dmap_free(&l->found_else), dmap_free(&l->tail_seen);
    dmap_free(&l->rep_ix);
    free(l->rep);
    strmap_free(&l->block_missed), strmap_free(&l->used), strmap_free(&l->rt_ix), strmap_free(&l->cwd_ix);
    free(l->rt);
    strv_free(l->cwd, l->ncwd);
    free(l->ai_title), free(l->first_prompt);
    free(l->main.path), free(l->main.ai), free(l->main.first);
    for (int i = 0; i < l->nsrcs; i++) free(l->srcs[i].path);
    free(l->srcs);
    strmap_free(&l->wix);
    strv_free(l->wnames, l->nwn);
    for (int i = 0; i < l->nlc; i++) free(l->lc[i].w), strv_free(l->lc[i].p, l->lc[i].np);
    free(l->lc);
    free(l);
}

static Loader *loader_copy(const Loader *f) {
    Loader *l = xmalloc(sizeof *l);
    *l = *f;
    l->root = dup_or_null(f->root), l->home = dup_or_null(f->home), l->config = dup_or_null(f->config);
    l->win = mem_copy(f->win, f->nwin);
    l->ih = strv_copy(f->ih, f->nih);
    l->keys = mem_copy(f->keys, (size_t)f->nkeys * sizeof *f->keys);
    l->csh = strv_copy(f->csh, f->ncalls), l->cpm = strv_copy(f->cpm, f->ncalls);
    l->ckept = mem_copy(f->ckept, (size_t)f->ncalls * sizeof *f->ckept);
    l->ncextra = mem_copy(f->ncextra, (size_t)f->ncalls * sizeof *f->ncextra);
    l->cextra = xmalloc(((size_t)f->ncalls + 1) * sizeof *l->cextra);
    for (int i = 0; i < f->ncalls; i++) l->cextra[i] = mem_copy(f->cextra[i], (size_t)f->ncextra[i] * sizeof(int));
    strmap_copy(&l->shape_ix, &f->shape_ix);
    l->shape_raw = strv_copy(f->shape_raw, f->nshape);
    l->old = xmalloc(((size_t)f->nold + 1) * sizeof *l->old);
    for (int i = 0; i < f->nold; i++) resp_copy(&l->old[i], &f->old[i]);
    l->drops = mem_copy(f->drops, (size_t)f->ndrops * sizeof *f->drops);
    for (int i = 0; i < f->ndrops; i++) l->drops[i].rq = xstrdup(f->drops[i].rq);
    dmap_copy(&l->old_only, &f->old_only), dmap_copy(&l->unresolved, &f->unresolved);
    dmap_copy(&l->found_else, &f->found_else), dmap_copy(&l->tail_seen, &f->tail_seen);
    dmap_copy(&l->rep_ix, &f->rep_ix);
    l->rep = mem_copy(f->rep, (size_t)f->nrep * sizeof *f->rep);
    strmap_copy(&l->block_missed, &f->block_missed), strmap_copy(&l->used, &f->used);
    strmap_copy(&l->rt_ix, &f->rt_ix), strmap_copy(&l->cwd_ix, &f->cwd_ix);
    l->rt = mem_copy(f->rt, (size_t)f->nrt * sizeof *f->rt);
    l->cwd = strv_copy(f->cwd, f->ncwd);
    l->ai_title = dup_or_null(f->ai_title), l->first_prompt = dup_or_null(f->first_prompt);
    l->main.path = dup_or_null(f->main.path), l->main.ai = dup_or_null(f->main.ai);
    l->main.first = dup_or_null(f->main.first);
    l->srcs = mem_copy(f->srcs, (size_t)f->nsrcs * sizeof *f->srcs);
    for (int i = 0; i < f->nsrcs; i++) l->srcs[i].path = xstrdup(f->srcs[i].path);
    strmap_copy(&l->wix, &f->wix);
    l->wnames = strv_copy(f->wnames, f->nwn);
    l->lc = mem_copy(f->lc, (size_t)f->nlc * sizeof *f->lc);
    for (int i = 0; i < f->nlc; i++) {
        l->lc[i].w = f->lc[i].nw ? mem_copy(f->lc[i].w, (size_t)f->lc[i].nw * sizeof(int)) : NULL;
        l->lc[i].p = f->lc[i].np ? strv_copy(f->lc[i].p, f->lc[i].np) : NULL;
    }
    return l;
}

static void dset_add(DMap *m, const uint8_t d[32]) { dmap_put(m, d, 1, 0); }
static int dset_has(const DMap *m, const uint8_t d[32]) { return dmap_get(m, d) >= 0; }

typedef struct {
    CtxAgent *a;
    Env env;
    atomic_int *cancel;
    Store own;
    Store *others; /* the project's other files, loaded when a hash is missing */
    char **files;  /* the project's transcripts, in build_ctx.py's order */
    int nfiles;
    Blk *blk;
    int nblk, capblk;
    PyBuf buf;
    struct { const void *p; uint8_t d[32]; } *hc; /* block hash cache, by value pointer */
    size_t hccap, hcn;
    Pool S, L; /* texts (the S table), labels and other strings */
    CtxItem *items;
    int nitems, capitems;
    Inst *inst;
    int ninst, capinst;
    Key *keys;
    int *kitems;
    int nkeys, capkeys;
    StrMap tool_use;  /* tool_use id -> action item */
    StrMap result_ix; /* tool_use id -> result_of slot */
    struct { const char *tu; int item; } *result_of;
    int nresult, capresult;
    CallB *cb;
    int **shapes; /* the shape lists built (calls share them until handed over) */
    int nshapes;
    const char *kh; /* what new items are keyed by (item_key): a message's hash, a shape, ... */
    int kpos;       /* and the place in it */
    uint64_t *kset; /* keys given so far (open addressing, 0 empty) */
    size_t kcap, kn;
    Loader *ld;      /* what this load resumes from and extends (an empty one for a full load) */
    int resuming;    /* a reload: what can't be applied to the earlier build sets fail */
    const char *fail; /* why a reload has to be a full load */
    char *raw;       /* a reload: the window and the appended bytes, from file offset raw_off */
    off_t raw_off;
    off_t new_off;   /* bytes taken now: whole lines */
    long new_abs;    /* records taken now */
    int tail_json;   /* a last line without its newline parsed: not resumable */
    dev_t dev;
    ino_t ino;
} Build;

static int canon_blk(PyBuf *o, Blk k) { /* -1: nested too deeply to hash */
    if (!k.wrap) return pyj_dumps(o, k.v, PYJ_CANON);
    pyb_puts(o, "{\"text\":");
    pyj_str(o, yyjson_get_str(k.v), yyjson_get_len(k.v));
    pyb_puts(o, ",\"type\":\"text\"}");
    return 0;
}

static void hash_buf(const PyBuf *o, uint8_t d[32]) {
    Sha256 c;
    sha256_init(&c);
    sha256_update(&c, o->s ? o->s : "", o->n);
    sha256_final(&c, d);
}

/* H(b) of a content block, cached by its value. A block nested too deeply to
 * hash gets a digest of its address instead: it matches nothing. */
static const uint8_t *blk_hash(Build *b, Blk k) {
    const void *p = (const char *)k.v + k.wrap;
    if ((b->hcn + 1) * 2 > b->hccap) {
        size_t cap = b->hccap ? b->hccap * 2 : 4096;
        __typeof__(b->hc) g = xcalloc(cap, sizeof *g);
        for (size_t i = 0; i < b->hccap; i++) {
            if (!b->hc[i].p) continue;
            size_t j = ((uintptr_t)b->hc[i].p >> 4) & (cap - 1);
            while (g[j].p) j = (j + 1) & (cap - 1);
            g[j] = b->hc[i];
        }
        free(b->hc);
        b->hc = g;
        b->hccap = cap;
    }
    size_t i = ((uintptr_t)p >> 4) & (b->hccap - 1);
    while (b->hc[i].p && b->hc[i].p != p) i = (i + 1) & (b->hccap - 1);
    if (!b->hc[i].p) {
        b->buf.n = 0;
        if (canon_blk(&b->buf, k)) {
            b->buf.n = 0;
            pyb_put(&b->buf, "\0unhashable", 11);
            pyb_put(&b->buf, (const char *)&p, sizeof p);
        }
        hash_buf(&b->buf, b->hc[i].d);
        b->hc[i].p = p;
        b->hcn++;
    }
    return b->hc[i].d;
}

static int role_of(const char *r) {
    if (!r) return ROLE_OTHER;
    return !strcmp(r, "user") ? ROLE_USER : !strcmp(r, "assistant") ? ROLE_ASSISTANT
           : !strcmp(r, "system") ? ROLE_SYSTEM : ROLE_OTHER;
}
static const char *ROLE_NAMES[] = {"user", "assistant", "system", "None"};

static int add_blk(Build *b, yyjson_val *v, int wrap) {
    GROW(b->blk, b->nblk, b->capblk);
    b->blk[b->nblk] = (Blk){v, wrap};
    return b->nblk++;
}

/* norm(content) appended to Build.blk; returns how many. */
static int norm_into(Build *b, yyjson_val *c) {
    if (yyjson_is_str(c)) {
        add_blk(b, c, 1);
        return 1;
    }
    if (!yyjson_is_arr(c)) return 0;
    size_t i, m;
    yyjson_val *x;
    yyjson_arr_foreach(c, i, m, x) add_blk(b, x, 0);
    return (int)m;
}

static int msg_role(const Msg *m) { return m->v ? role_of(gets(m->v, "role")) : m->role; }

/* A message's blocks: for a recorded one, norm(content) appended now. */
static void msg_blocks(Build *b, const Msg *m, int *b0, int *nb) {
    if (!m->v) {
        *b0 = m->b0, *nb = m->nb;
        return;
    }
    *b0 = b->nblk;
    *nb = norm_into(b, get(m->v, "content"));
}

/* H of a rebuilt message: {"content": [blocks], "role": role}, keys sorted. */
static int hash_rebuilt(Build *b, int role, int b0, int nb, uint8_t d[32]) { /* -1: unhashable */
    PyBuf *o = &b->buf;
    o->n = 0;
    pyb_puts(o, "{\"content\":[");
    for (int i = 0; i < nb; i++) {
        if (i) pyb_puts(o, ",");
        if (canon_blk(o, b->blk[b0 + i])) return -1;
    }
    pyb_puts(o, "],\"role\":\"");
    pyb_puts(o, ROLE_NAMES[role]);
    pyb_puts(o, "\"}");
    hash_buf(o, d);
    return 0;
}

static int store_msg(Store *s, Msg m) {
    GROW(s->msgs, s->nmsgs, s->capmsgs);
    s->msgs[s->nmsgs] = m;
    return s->nmsgs++;
}

/* responses(): message id (or the record's uuid) -> its assistant records.
 * Returns 0, or -1 when a record belongs to a response kept as metadata only
 * (from before an earlier load's window): it can't grow here. */
static int find_responses(Store *s) {
    int rc = 0;
    for (int i = 0; i < s->recs.n; i++) {
        Rec *r = &s->recs.r[i];
        yyjson_val *msg = get(r->r, "message");
        if (r->type != T_ASSISTANT || !yyjson_is_obj(msg)) continue;
        const char *mid = gets(msg, "id");
        if (!mid || !*mid) mid = gets(r->r, "uuid");
        if (!mid || !*mid) mid = "\x01"; /* None */
        int k = strmap_get(&s->resp_ix, mid);
        if (k < 0) {
            s->resps = xrealloc(s->resps, (size_t)(s->nresps + 1) * sizeof *s->resps);
            s->resps[s->nresps] = (Resp){0};
            strmap_put(&s->resp_ix, mid, k = s->nresps++);
        }
        Resp *rr = &s->resps[k];
        if (rr->old) {
            rc = -1;
            continue;
        }
        rr->recs = xrealloc(rr->recs, (size_t)(rr->n + 1) * sizeof *rr->recs);
        rr->recs[rr->n++] = i;
    }
    return rc;
}

static yyjson_val *resp_msg(const Store *s, const Resp *rr, int i) { return get(s->recs.r[rr->recs[i]].r, "message"); }

/* response_message(rr): its blocks appended to Build.blk. */
static void resp_blocks(Build *b, const Store *s, const Resp *rr, int *b0, int *nb) {
    *b0 = b->nblk;
    for (int i = 0; i < rr->n; i++) {
        yyjson_val *c = get(resp_msg(s, rr, i), "content");
        if (yyjson_is_arr(c)) norm_into(b, c);
    }
    *nb = b->nblk - *b0;
}

/* store(f): blobs by their recorded hash (a later one replaces), then user
 * records, every run of consecutive user records sent as one message, and
 * responses, each by its own hash (the first one kept). */
static void flush_run(Build *b, Store *s, int *run, int nrun) {
    if (!nrun) return;
    int *off = xmalloc((size_t)(nrun + 1) * sizeof *off);
    int start = b->nblk;
    for (int i = 0; i < nrun; i++) {
        off[i] = b->nblk - start;
        norm_into(b, get(get(s->recs.r[run[i]].r, "message"), "content"));
    }
    off[nrun] = b->nblk - start;
    /* each block's canonical JSON once; each run[a:b] hashed incrementally */
    PyBuf *cj = xcalloc((size_t)off[nrun] + 1, sizeof *cj);
    char *bad = xcalloc((size_t)off[nrun] + 1, 1); /* unhashable: no run holding it is stored */
    for (int i = 0; i < off[nrun]; i++) bad[i] = canon_blk(&cj[i], b->blk[start + i]) != 0;
    static const char tail[] = "],\"role\":\"user\"}";
    for (int a = 0; a < nrun; a++) {
        Sha256 c;
        sha256_init(&c);
        sha256_update(&c, "{\"content\":[", 12);
        for (int e = a + 1; e <= nrun; e++) {
            int broken = 0;
            for (int k = off[e - 1]; k < off[e]; k++) {
                if (k > off[a]) sha256_update(&c, ",", 1);
                sha256_update(&c, cj[k].s ? cj[k].s : "", cj[k].n);
                broken |= bad[k];
            }
            if (broken) break;
            Sha256 f = c;
            uint8_t d[32];
            sha256_update(&f, tail, sizeof tail - 1);
            sha256_final(&f, d);
            if (dmap_get(&s->map, d) < 0)
                dmap_put(&s->map, d,
                         store_msg(s, (Msg){NULL, ROLE_USER, start + off[a], off[e] - off[a], s->recs.r[run[a]].abs}), 0);
        }
    }
    for (int i = 0; i < off[nrun]; i++) pyb_free(&cj[i]);
    free(cj), free(bad);
    free(off);
}

static long long *usage_of(yyjson_val *u, long long out[5]);

/* What the calls need of a response, kept so that a later load needs none of
 * its records. */
static void resp_meta(Build *b, Store *s, Resp *rr, int b0, int nb) {
    yyjson_val *r0 = s->recs.r[rr->recs[0]].r, *rl = s->recs.r[rr->recs[rr->n - 1]].r, *ml = get(rl, "message");
    yyjson_val *m0 = get(r0, "message");
    const char *mid = gets(m0, "id");
    if (!mid || !*mid) mid = gets(r0, "uuid");
    rr->mid = xstrdup(mid && *mid ? mid : "\x01");
    rr->ref = dup_or_null(gets(r0, "requestRef"));
    rr->rq = dup_or_null(gets(rl, "requestId"));
    rr->model = dup_or_null(gets(ml, "model"));
    yyjson_val *u = get(ml, "usage");
    rr->has_u = usage_of(u, rr->u) != NULL;
    rr->out_t = geti(u, "output_tokens"), rr->th_t = geti(get(u, "output_tokens_details"), "thinking_tokens");
    rr->first = s->recs.r[rr->recs[0]].abs;
    rr->nrec = rr->n;
    rr->keys = xmalloc(((size_t)nb + 1) * sizeof *rr->keys);
    rr->nkeys = nb;
    for (int i = 0; i < nb; i++) {
        const char *id = b->blk[b0 + i].wrap ? NULL : gets(b->blk[b0 + i].v, "id");
        char hx[65];
        if (!id || !*id) {
            sha256_hex(blk_hash(b, b->blk[b0 + i]), hx);
            id = hx;
        }
        rr->keys[i] = xstrdup(id);
    }
}

static void resp_clear(Resp *rr) {
    free(rr->recs), free(rr->mid), free(rr->ref), free(rr->rq), free(rr->model);
    for (int i = 0; i < rr->nkeys; i++) free(rr->keys[i]);
    free(rr->keys);
}

/* The store of a file; meta: the agent's own (its responses' metadata).
 * Returns -1 when a response kept as metadata only would grow (find_responses). */
static int store_build_meta(Build *b, Store *s, int meta);
static void store_build(Build *b, Store *s) { store_build_meta(b, s, 0); }

static int store_build_meta(Build *b, Store *s, int meta) {
    int *run = NULL, nrun = 0, caprun = 0;
    for (int i = 0; i < s->recs.n; i++) {
        if (b->cancel && !(i & 511) && atomic_load(b->cancel)) break;
        Rec *r = &s->recs.r[i];
        if (r->type == T_BLOB) {
            uint8_t d[32];
            yyjson_val *m = get(r->r, "message");
            if (hex_digest(gets(r->r, "hash"), d) && m) dmap_put(&s->map, d, store_msg(s, (Msg){m, 0, 0, 0, r->abs}), 1);
        } else if (r->type == T_ASSISTANT) {
            flush_run(b, s, run, nrun);
            nrun = 0;
        } else if (r->type == T_USER && yyjson_is_obj(get(r->r, "message"))) {
            yyjson_val *m = get(r->r, "message");
            b->buf.n = 0;
            uint8_t d[32];
            if (!pyj_dumps(&b->buf, m, PYJ_CANON)) {
                hash_buf(&b->buf, d);
                if (dmap_get(&s->map, d) < 0) dmap_put(&s->map, d, store_msg(s, (Msg){m, 0, 0, 0, r->abs}), 0);
            }
            GROW(run, nrun, caprun);
            run[nrun++] = i;
        }
    }
    flush_run(b, s, run, nrun);
    free(run);
    int rc = find_responses(s);
    for (int k = 0; k < s->nresps; k++) {
        if (s->resps[k].old) continue; /* its hash is an earlier load's (old_only) */
        int b0, nb;
        resp_blocks(b, s, &s->resps[k], &b0, &nb);
        if (b->cancel && !(k & 255) && atomic_load(b->cancel)) break;
        if (meta) resp_meta(b, s, &s->resps[k], b0, nb);
        if ((s->resps[k].bad = hash_rebuilt(b, ROLE_ASSISTANT, b0, nb, s->resps[k].d) != 0)) continue;
        if (dmap_get(&s->map, s->resps[k].d) < 0)
            dmap_put(&s->map, s->resps[k].d,
                     store_msg(s, (Msg){NULL, ROLE_ASSISTANT, b0, nb, s->recs.r[s->resps[k].recs[0]].abs}), 0);
    }
    s->loaded = 1;
    return rc;
}

static void store_free(Store *s) {
    recs_free(&s->recs);
    dmap_free(&s->map);
    free(s->msgs);
    for (int i = 0; i < s->nresps; i++) resp_clear(&s->resps[i]);
    free(s->resps);
    strmap_free(&s->resp_ix);
    free(s->path);
}

/* resolve(h): the own file, else the project's other files in order. A
 * message found elsewhere is remembered in the own store (in found_else
 * across reloads, so it counts once). Resuming, a message only an earlier
 * load's records had (old_only) can't be rebuilt: a full load. */
static const Msg *resolve(Build *b, const char *h) {
    uint8_t d[32];
    if (!hex_digest(h, d)) {
        b->a->missing++;
        return NULL;
    }
    int k = dmap_get(&b->own.map, d);
    if (k >= 0) return &b->own.msgs[k];
    if (b->resuming && dset_has(&b->ld->old_only, d)) {
        b->fail = "a message only an earlier part of the transcript holds";
        return NULL;
    }
    for (int i = 0; i < b->nfiles; i++) {
        if (!strcmp(b->files[i], b->own.path)) continue;
        Store *s = &b->others[i];
        if (!s->loaded) {
            if (b->cancel && atomic_load(b->cancel)) break;
            s->path = xstrdup(b->files[i]);
            read_records(s->path, &s->recs, b->cancel);
            store_build(b, s);
        }
        if ((k = dmap_get(&s->map, d)) >= 0) {
            Loader *ld = b->ld; /* the file must still hold it at the next reload */
            int known = 0;
            for (int j = 0; j < ld->nsrcs && !known; j++) known = !strcmp(ld->srcs[j].path, b->files[i]);
            struct stat st;
            if (!known && !stat(b->files[i], &st)) {
                ld->srcs = xrealloc(ld->srcs, (size_t)(ld->nsrcs + 1) * sizeof *ld->srcs);
                ld->srcs[ld->nsrcs].path = xstrdup(b->files[i]);
                ld->srcs[ld->nsrcs].dev = st.st_dev, ld->srcs[ld->nsrcs].ino = st.st_ino;
                ld->srcs[ld->nsrcs++].size = st.st_size;
            }
            if (!dset_has(&b->ld->found_else, d)) b->a->found_elsewhere++;
            dset_add(&b->ld->found_else, d);
            dset_add(&b->ld->unresolved, d);
            Msg m = s->msgs[k];
            m.src = -1;
            dmap_put(&b->own.map, d, store_msg(&b->own, m), 1);
            return &b->own.msgs[b->own.nmsgs - 1];
        }
    }
    b->a->missing++;
    dset_add(&b->ld->unresolved, d);
    return NULL;
}

/* ---- items ---- */

typedef struct {
    CtxKind kind;
    const char *s; /* label (Build.L) */
    Str t;         /* text (borrowed) */
    double w;
    int m; /* made, -1 */
    const char *tool, *tuid, *p; /* Build.L */
    int err;
} Fields;

/* A new item's key: its message (or shape) and place in it; a key already
 * given (the same message sent again as a new instance) takes the next free
 * one in creation order, which reloads repeat. */
static uint64_t kset_take(Build *b, uint64_t k) { /* k, or the next free one in its sequence */
    if ((b->kn + 1) * 2 > b->kcap) {
        size_t cap = b->kcap ? b->kcap * 2 : 1024;
        uint64_t *g = xcalloc(cap, sizeof *g);
        for (size_t i = 0; i < b->kcap; i++)
            if (b->kset[i]) {
                size_t j = b->kset[i] & (cap - 1);
                while (g[j]) j = (j + 1) & (cap - 1);
                g[j] = b->kset[i];
            }
        free(b->kset);
        b->kset = g;
        b->kcap = cap;
    }
    for (;;) {
        if (!k) k = 1;
        size_t i = k & (b->kcap - 1);
        while (b->kset[i] && b->kset[i] != k) i = (i + 1) & (b->kcap - 1);
        if (!b->kset[i]) {
            b->kset[i] = k;
            b->kn++;
            return k;
        }
        k = k * 0x100000001B3ULL + 0x2545F4914F6CDD1DULL; /* taken: the next in sequence */
    }
}

static uint64_t item_key(Build *b) {
    return kset_take(b, fnv(b->kh ? b->kh : "", b->kh ? strlen(b->kh) : 0) ^
                            ((uint64_t)(b->kpos + 1) * 0x9E3779B97F4A7C15ULL));
}

static int finish_item(Build *b, const Fields *f) {
    GROW(b->items, b->nitems, b->capitems);
    CtxItem *it = &b->items[b->nitems];
    memset(it, 0, sizeof *it);
    it->kind = f->kind;
    it->src = f->s;
    it->x = pool_add(&b->S, f->t.s, f->t.n);
    it->n = (int)nchars(f->t.s, f->t.n);
    it->w = f->w;
    it->made = f->m;
    it->tool = f->tool;
    it->tuid = f->tuid;
    it->path = f->p;
    it->err = f->err;
    it->enter = it->leave = it->action = it->result = -1;
    it->key = item_key(b);
    int id = b->nitems++;
    if (f->tuid && f->kind == CK_ACTION) strmap_put(&b->tool_use, f->tuid, id);
    else if (f->tuid && f->kind == CK_RESULT) {
        int k = strmap_get(&b->result_ix, f->tuid);
        if (k < 0) {
            GROW(b->result_of, b->nresult, b->capresult);
            b->result_of[b->nresult].tu = f->tuid;
            strmap_put(&b->result_ix, f->tuid, k = b->nresult++);
        }
        b->result_of[k].item = id;
    }
    return id;
}

static const char *L_(Build *b, const char *s) { return pool_str(&b->L, s, strlen(s)); }
static const char *Lbuf(Build *b, PyBuf *o) { return pool_str(&b->L, o->s ? o->s : "", o->n); }

/* inj_source: what injected text is, from its start. */
static const char *inj_source(Str t, char name[160]) {
    Str head = {t.s, char_off(t.s, t.n, 600)};
    if (contains(head, "CLAUDE.md") || contains(head, "claudeMd") || contains(head, "Codebase and user instructions"))
        return "CLAUDE.md";
    if (contains(head, "MEMORY.md") || contains(head, "auto-memory")) return "memory";
    /* \s*(?:<system-reminder>\s*)?([A-Za-z]+(?::[A-Za-z_*|]+)?) hook (?:additional context|blocking error|feedback) */
    size_t i = skip_space(t.s, t.n, 0);
    if (starts((Str){t.s + i, t.n - i}, "<system-reminder>")) i = skip_space(t.s, t.n, i + 17);
    size_t j = i;
    while (j < t.n && ((t.s[j] | 32) >= 'a' && (t.s[j] | 32) <= 'z')) j++;
    if (j > i) {
        size_t k = j;
        if (k < t.n && t.s[k] == ':') {
            size_t q = k + 1;
            while (q < t.n && (((t.s[q] | 32) >= 'a' && (t.s[q] | 32) <= 'z') || t.s[q] == '_' || t.s[q] == '*' ||
                               t.s[q] == '|'))
                q++;
            if (q > k + 1) k = q;
        }
        Str rest = {t.s + k, t.n - k};
        if (starts(rest, " hook additional context") || starts(rest, " hook blocking error") ||
            starts(rest, " hook feedback")) {
            snprintf(name, 160, "hook %.*s", (int)(k - i), t.s + i);
            return name;
        }
    }
    for (size_t q = 0; q + 4 <= head.n; q++)
        if ((head.s[q] | 32) == 'h' && (head.s[q + 1] | 32) == 'o' && (head.s[q + 2] | 32) == 'o' &&
            (head.s[q + 3] | 32) == 'k')
            return "hook output";
    Str l = lstrip(t);
    if (starts(l, "<total_tokens>")) return "token count";
    if (contains(head, "<local-command") || contains(head, "<command-name>")) return "slash command";
    if (starts(l, "<task-notification>")) return "task notification";
    return "system reminder";
}

typedef struct {
    CtxKind kind;
    const char *src; /* NULL for a prompt before classification */
    Str t;
} Seg;

/* split_text: injected segments (<system-reminder>...) split out of the text
 * they ride in; the rest classified as typed, a summary, or injected. */
static int split_text(Build *b, Str t, Seg **out) {
    char nm[160];
    Seg *v = NULL;
    int n = 0, cap = 0;
    size_t pos = 0;
    for (;;) {
        const char *o = memmem(t.s + pos, t.n - pos, "<system-reminder>", 17);
        if (!o) break;
        size_t oi = (size_t)(o - t.s);
        const char *c = memmem(t.s + oi + 17, t.n - oi - 17, "</system-reminder>", 18);
        if (!c) break;
        size_t end = (size_t)(c - t.s) + 18;
        if (strip((Str){t.s + pos, oi - pos}).n) {
            GROW(v, n, cap);
            v[n++] = (Seg){CK_PROMPT, NULL, {t.s + pos, oi - pos}};
        }
        Str inj = {t.s + oi, end - oi};
        GROW(v, n, cap);
        v[n++] = (Seg){CK_INJECTED, L_(b, inj_source(inj, nm)), inj};
        pos = end;
    }
    if (strip((Str){t.s + pos, t.n - pos}).n || !n) {
        GROW(v, n, cap);
        v[n++] = (Seg){CK_PROMPT, NULL, {t.s + pos, t.n - pos}};
    }
    static const char *harness[] = {"<local-command", "<command-name>", "<command-message>", "<total_tokens>",
                                    "<task-notification>", "<bash-input>", "<bash-stdout>",
                                    "<user-prompt-submit-hook>"};
    for (int i = 0; i < n; i++) {
        if (v[i].kind != CK_PROMPT) continue;
        Str st = lstrip(v[i].t);
        int h = 0;
        for (size_t k = 0; k < sizeof harness / sizeof *harness; k++) h |= starts(st, harness[k]);
        if (h) v[i].kind = CK_INJECTED, v[i].src = L_(b, inj_source(v[i].t, nm));
        else if (starts(st, "This session is being continued from a previous conversation"))
            v[i].kind = CK_SUMMARY, v[i].src = L_(b, "compaction summary");
        else if (starts(st, "[Request interrupted")) v[i].kind = CK_INJECTED, v[i].src = L_(b, "interrupt");
        else v[i].src = L_(b, "prompt");
    }
    *out = v;
    return n;
}

/* img_info: "png 1000x100" and tokens (w*h/750 after the 1568 px long-edge resize). */
static long long img_info(yyjson_val *src, PyBuf *info) {
    Str data = getstr(src, "data");
    const char *mt = gets(src, "media_type");
    if (!mt || !*mt) mt = "image/?";
    const char *slash = strrchr(mt, '/');
    const char *ext = slash ? slash + 1 : mt;
    unsigned char raw[3000];
    size_t nr = 0;
    unsigned acc = 0;
    int bits = 0;
    for (size_t i = 0; i < data.n && i < 4000 && nr < sizeof raw; i++) {
        int c = data.s[i], v = c >= 'A' && c <= 'Z' ? c - 'A' : c >= 'a' && c <= 'z' ? c - 'a' + 26
                             : c >= '0' && c <= '9' ? c - '0' + 52 : c == '+' ? 62 : c == '/' ? 63 : -1;
        if (c == '=') break;
        if (v < 0) continue;
        acc = acc << 6 | (unsigned)v;
        if ((bits += 6) >= 8) raw[nr++] = (unsigned char)(acc >> (bits -= 8));
    }
    unsigned long w = 0, h = 0;
    if (nr >= 24 && !memcmp(raw, "\x89PNG\r\n\x1a\n", 8)) {
        w = (unsigned long)raw[16] << 24 | raw[17] << 16 | raw[18] << 8 | raw[19];
        h = (unsigned long)raw[20] << 24 | raw[21] << 16 | raw[22] << 8 | raw[23];
    } else if (nr >= 2 && raw[0] == 0xFF && raw[1] == 0xD8) {
        for (size_t i = 2; i + 9 < nr;) {
            if (raw[i] != 0xFF) break;
            int mk = raw[i + 1], ln = raw[i + 2] << 8 | raw[i + 3];
            if (mk == 0xC0 || mk == 0xC1 || mk == 0xC2) {
                h = (unsigned long)(raw[i + 5] << 8 | raw[i + 6]);
                w = (unsigned long)(raw[i + 7] << 8 | raw[i + 8]);
                break;
            }
            i += 2 + (size_t)ln;
        }
    }
    long long tok = 1000;
    if (w && h) {
        double s = fmin(1.0, 1568.0 / (double)(w > h ? w : h));
        tok = (long long)((double)w * s * (double)h * s / 750);
    }
    char t[64];
    if (w) snprintf(t, sizeof t, "%s %lux%lu", ext, w, h);
    else snprintf(t, sizeof t, "%s", ext);
    pyb_puts(info, t);
    return tok;
}

/* action_label: "Read src/a.c 1-40", "Bash make test", ... */
static void action_label(Build *b, PyBuf *o, Str name, yyjson_val *inp) {
    char num[64];
    if (!yyjson_is_obj(inp)) inp = NULL;
#define IS(x) (name.n == strlen(x) && !memcmp(name.s, x, name.n))
    if (IS("Read")) {
        pyb_puts(o, "Read ");
        const char *p = gets(inp, "file_path");
        char *r = p ? rel(&b->env, p) : xstrdup("");
        pyb_puts(o, r);
        free(r);
        yyjson_val *off = get(inp, "offset"), *lim = get(inp, "limit");
        if (truthy(off) || truthy(lim)) {
            long long a = truthy(off) ? num_ll(off) : 1, l = truthy(lim) ? num_ll(lim) : 2000, z;
            if (__builtin_add_overflow(a, l - (l > LLONG_MIN), &z)) z = a < 0 ? LLONG_MIN : LLONG_MAX; /* a + l - 1 */
            snprintf(num, sizeof num, " %lld-%lld", a, z);
            pyb_puts(o, num);
        }
    } else if (IS("Edit") || IS("Write") || IS("MultiEdit") || IS("NotebookEdit")) {
        pyb_put(o, name.s, name.n);
        pyb_puts(o, " ");
        yyjson_val *p = get(inp, "file_path");
        if (!truthy(p)) p = get(inp, "notebook_path");
        char *r = yyjson_is_str(p) ? rel(&b->env, yyjson_get_str(p)) : xstrdup("");
        pyb_puts(o, r);
        free(r);
    } else if (IS("Bash")) {
        /* the usual cd into the repo: ^\s*cd\s+("[^"]*"|\S+)\s*&&\s* */
        Str c = getstr(inp, "command");
        size_t i = skip_space(c.s, c.n, 0), cut = 0;
        if (starts((Str){c.s + i, c.n - i}, "cd")) {
            size_t j = skip_space(c.s, c.n, i + 2);
            if (j > i + 2) {
                size_t e = 0;
                if (j < c.n && c.s[j] == '"') {
                    const char *q = memchr(c.s + j + 1, '"', c.n - j - 1);
                    if (q) {
                        size_t k = skip_space(c.s, c.n, (size_t)(q - c.s) + 1);
                        if (starts((Str){c.s + k, c.n - k}, "&&")) e = k + 2;
                    }
                }
                if (!e) { /* \S+, backing off until "&&" follows */
                    size_t L = j;
                    int k;
                    while (L < c.n && !py_space(cp_at(c.s, c.n, L, &k))) L += (size_t)k;
                    for (size_t x = L; x > j && !e;) {
                        size_t y = x == L ? skip_space(c.s, c.n, x) : x;
                        if (starts((Str){c.s + y, c.n - y}, "&&")) e = y + 2;
                        else {
                            x--;
                            while (x > j && ((unsigned char)c.s[x] & 0xC0) == 0x80) x--;
                        }
                    }
                }
                if (e) cut = skip_space(c.s, c.n, e);
            }
        }
        pyb_puts(o, "Bash ");
        one_line(o, (Str){c.s + cut, c.n - cut}, 80);
    } else if (IS("Grep") || IS("Glob")) {
        pyb_put(o, name.s, name.n);
        pyb_puts(o, " ");
        PyBuf pat = {0};
        yyjson_val *pv = get(inp, "pattern");
        if (pv) py_str(&pat, pv);
        one_line(o, (Str){pat.s ? pat.s : "", pat.n}, 50);
        pyb_free(&pat);
        yyjson_val *p = get(inp, "path");
        if (truthy(p)) {
            pyb_puts(o, " in ");
            char *r = yyjson_is_str(p) ? rel(&b->env, yyjson_get_str(p)) : xstrdup("");
            pyb_puts(o, r);
            free(r);
        }
    } else if (IS("Agent") || IS("Task")) {
        pyb_put(o, name.s, name.n);
        pyb_puts(o, " ");
        yyjson_val *t = get(inp, "subagent_type");
        if (truthy(t)) py_str(o, t);
        pyb_puts(o, ": ");
        PyBuf d = {0};
        yyjson_val *dv = get(inp, "description");
        if (dv) py_str(&d, dv);
        one_line(o, (Str){d.s ? d.s : "", d.n}, 60);
        pyb_free(&d);
    } else if (IS("Skill")) {
        pyb_puts(o, "Skill ");
        if (get(inp, "skill")) py_str(o, get(inp, "skill"));
    } else if (IS("WebFetch")) {
        pyb_puts(o, "WebFetch ");
        PyBuf u = {0};
        if (get(inp, "url")) py_str(&u, get(inp, "url"));
        one_line(o, (Str){u.s ? u.s : "", u.n}, 70);
        pyb_free(&u);
    } else { /* f"{name} {one_line(first string value, 60)}".strip() */
        Str first = {"", 0};
        size_t i, m;
        yyjson_val *k, *v;
        if (inp) yyjson_obj_foreach(inp, i, m, k, v) if (yyjson_is_str(v)) {
                first = (Str){yyjson_get_str(v), yyjson_get_len(v)};
                break;
            }
        PyBuf t = {0};
        pyb_put(&t, name.s, name.n);
        pyb_puts(&t, " ");
        one_line(&t, first, 60);
        Str s = strip((Str){t.s, t.n});
        pyb_put(o, s.s, s.n);
        pyb_free(&t);
    }
#undef IS
}

/* A Str of a JSON value's dumps into a scratch buffer kept alive by the caller. */
static Str dumps_to(PyBuf *o, Blk k, int flags) {
    o->n = 0;
    if (k.wrap && flags == 0) {
        pyb_puts(o, "{\"type\": \"text\", \"text\": ");
        pyj_str(o, yyjson_get_str(k.v), yyjson_get_len(k.v));
        pyb_puts(o, "}");
    } else if (k.wrap) {
        pyb_puts(o, "{\n \"type\": \"text\",\n \"text\": ");
        pyj_str(o, yyjson_get_str(k.v), yyjson_get_len(k.v));
        pyb_puts(o, "\n}");
    } else if (pyj_dumps(o, k.v, flags)) {
        o->n = 0;
        pyb_puts(o, "[nested too deeply to show]");
    }
    return (Str){o->s ? o->s : "", o->n};
}

/* Content keys: role + ":" + H(block) [+ ":" segment]. */
static Key key_of(Build *b, int role, Blk k, int sub) {
    Key key;
    memset(&key, 0, sizeof key);
    memcpy(key.d, blk_hash(b, k), 32);
    key.sub = (int16_t)sub;
    key.role = (uint8_t)role;
    return key;
}

/* Items of dropped messages, by content key, that a rewritten message may take
 * back (free_items in build_ctx.py). */
typedef struct {
    Key *k;
    int **v; /* per key: its items, v[i][0] = count */
    int *cap;
    int n, capn;
    int *ix;
    size_t ixcap;
} Free;

static uint64_t key_hash(const Key *k) {
    uint64_t h;
    memcpy(&h, k->d, 8);
    return h ^ ((uint64_t)(uint16_t)k->sub << 48) ^ ((uint64_t)k->role << 40) ^ ((uint64_t)k->tag << 32);
}

static int free_find(const Free *f, const Key *k) {
    if (!f->ixcap) return -1;
    for (size_t i = key_hash(k) & (f->ixcap - 1);; i = (i + 1) & (f->ixcap - 1)) {
        if (f->ix[i] < 0) return -1;
        if (!memcmp(&f->k[f->ix[i]], k, sizeof *k)) return f->ix[i];
    }
}

static void free_add(Free *f, const Key *k, int item) {
    int j = free_find(f, k);
    if (j < 0) {
        if ((size_t)(f->n + 1) * 2 > f->ixcap) {
            size_t cap = f->ixcap ? f->ixcap * 2 : 256;
            free(f->ix);
            f->ix = xmalloc(cap * sizeof *f->ix);
            memset(f->ix, -1, cap * sizeof *f->ix);
            f->ixcap = cap;
            for (int q = 0; q < f->n; q++) {
                size_t i = key_hash(&f->k[q]) & (cap - 1);
                while (f->ix[i] >= 0) i = (i + 1) & (cap - 1);
                f->ix[i] = q;
            }
        }
        if (f->n == f->capn) {
            f->capn = f->capn ? f->capn * 2 : 64;
            f->k = xrealloc(f->k, (size_t)f->capn * sizeof *f->k);
            f->v = xrealloc(f->v, (size_t)f->capn * sizeof *f->v);
            f->cap = xrealloc(f->cap, (size_t)f->capn * sizeof *f->cap);
        }
        j = f->n++;
        f->k[j] = *k;
        f->cap[j] = 4;
        f->v[j] = xmalloc(4 * sizeof(int));
        f->v[j][0] = 0;
        size_t i = key_hash(k) & (f->ixcap - 1);
        while (f->ix[i] >= 0) i = (i + 1) & (f->ixcap - 1);
        f->ix[i] = j;
    }
    int *v = f->v[j];
    if (v[0] + 1 >= f->cap[j]) v = f->v[j] = xrealloc(v, (size_t)(f->cap[j] *= 2) * sizeof(int));
    v[++v[0]] = item;
}

static int free_pop(Free *f, const Key *k) { /* the first item under k, -1 */
    int j = free_find(f, k);
    if (j < 0 || !f->v[j][0]) return -1;
    int *v = f->v[j], it = v[1];
    memmove(v + 1, v + 2, (size_t)(v[0] - 1) * sizeof(int));
    v[0]--;
    return it;
}

static void free_remove(Free *f, const Key *k, int item) { /* list.remove(item) when present */
    int j = free_find(f, k);
    if (j < 0) return;
    int *v = f->v[j];
    for (int i = 1; i <= v[0]; i++)
        if (v[i] == item) {
            memmove(v + i, v + i + 1, (size_t)(v[0] - i) * sizeof(int));
            v[0]--;
            return;
        }
}

static void free_reset(Free *f) {
    for (int j = 0; j < f->n; j++) free(f->v[j]);
    f->n = 0;
    if (f->ixcap) memset(f->ix, -1, f->ixcap * sizeof *f->ix);
}

static void free_done(Free *f) {
    free_reset(f);
    free(f->k), free(f->v), free(f->cap), free(f->ix);
}

/* One block's (key, item): an item of a dropped message with the same key is
 * taken back, else a new one. */
static void emit(Build *b, Free *fr, Key key, const Fields *f) {
    int iid = fr ? free_pop(fr, &key) : -1;
    if (iid < 0) iid = finish_item(b, f);
    b->kpos++;
    if (b->nkeys == b->capkeys) {
        b->capkeys = b->capkeys ? b->capkeys * 2 : 1024;
        b->keys = xrealloc(b->keys, (size_t)b->capkeys * sizeof *b->keys);
        b->kitems = xrealloc(b->kitems, (size_t)b->capkeys * sizeof *b->kitems);
    }
    b->keys[b->nkeys] = key;
    b->kitems[b->nkeys++] = iid;
}

/* blocks_of: a message -> its items, one per content block (injected text
 * split out), keyed by content. resp: the response that made an assistant
 * message (its usage sizes the blocks), NULL. */
static void blocks_of(Build *b, Free *fr, const Msg *m, int made, const Resp *resp) {
    int role = msg_role(m);
    PyBuf tb = {0}, lb = {0};
    if (role == ROLE_SYSTEM) {
        int empty = m->v ? !truthy(get(m->v, "content")) : !m->nb;
        if (empty) {
            yyjson_val *eff = get(get(m->v, "output_config"), "effort");
            if (truthy(eff)) {
                pyb_puts(&tb, "(effort: ");
                py_str(&tb, eff);
                pyb_puts(&tb, ")");
            } else pyb_puts(&tb, "(empty system message)");
            Key key;
            memset(&key, 0, sizeof key);
            Sha256 c;
            sha256_init(&c);
            sha256_update(&c, tb.s, tb.n);
            sha256_final(&c, key.d);
            key.tag = 1;
            key.sub = -1;
            Fields f = {CK_INJECTED, L_(b, truthy(eff) ? "effort" : "system message"), {tb.s, tb.n}, 4, -1,
                        NULL, NULL, NULL, 0};
            emit(b, fr, key, &f);
            pyb_free(&tb);
            return;
        }
    }
    int b0, nb;
    msg_blocks(b, m, &b0, &nb);
    int has_results = 0;
    for (int i = 0; i < nb; i++) {
        const char *t = gets(b->blk[b0 + i].v, "type");
        has_results |= !b->blk[b0 + i].wrap && t && !strcmp(t, "tool_result");
    }
    /* an assistant response's blocks: thinking gets its thinking tokens, the
     * rest share the remaining output tokens by size */
    double *aw = NULL;
    if (role == ROLE_ASSISTANT && resp) {
        long long out_t = resp->out_t, th_t = resp->th_t;
        aw = xcalloc((size_t)nb + 1, sizeof *aw);
        long long *sz = xcalloc((size_t)nb + 1, sizeof *sz);
        long long sig_sum = 0, size_sum = 0;
        for (int i = 0; i < nb; i++) {
            Blk k = b->blk[b0 + i];
            const char *t = k.wrap ? "text" : gets(k.v, "type");
            int th = t && (!strcmp(t, "thinking") || !strcmp(t, "redacted_thinking"));
            if (th) {
                Str sig = getstr(k.v, "signature");
                if (!sig.n) sig = getstr(k.v, "data");
                Str tt = getstr(k.v, "thinking");
                sz[i] = (long long)(nchars(sig.s, sig.n) + nchars(tt.s, tt.n) + 1);
                sig_sum += sz[i];
            } else {
                Str d = dumps_to(&tb, k, 0);
                sz[i] = (long long)nchars(d.s, d.n) + 1;
                size_sum += sz[i];
            }
        }
        for (int i = 0; i < nb; i++) {
            Blk k = b->blk[b0 + i];
            const char *t = k.wrap ? "text" : gets(k.v, "type");
            int th = t && (!strcmp(t, "thinking") || !strcmp(t, "redacted_thinking"));
            if (th) {
                Str tt = getstr(k.v, "thinking");
                aw[i] = th_t ? (double)th_t * (double)sz[i] / (double)sig_sum : (double)nchars(tt.s, tt.n) / 4;
            } else {
                long long rest = out_t - th_t > 0 ? out_t - th_t : 0;
                aw[i] = out_t ? (double)rest * (double)sz[i] / (double)size_sum : (double)sz[i] / 4;
            }
        }
        free(sz);
    }
    for (int bi = 0; bi < nb; bi++) {
        Blk k = b->blk[b0 + bi];
        const char *bt = k.wrap ? "text" : gets(k.v, "type");
        yyjson_val *bv = k.wrap ? NULL : k.v;
        Str text = k.wrap ? (Str){yyjson_get_str(k.v), yyjson_get_len(k.v)} : getstr(bv, "text");
        lb.n = 0;
        if (role == ROLE_SYSTEM) {
            Fields f = {CK_INJECTED, NULL, {0}, 0, -1, NULL, NULL, NULL, 0};
            if (bt && !strcmp(bt, "text")) {
                char nm[160];
                f.t = text;
                f.s = L_(b, inj_source(text, nm));
            } else if (bt && !strcmp(bt, "tool_addition")) {
                yyjson_val *tool = get(bv, "tool"), *d = get(tool, "definition");
                if (!truthy(d)) d = tool;
                yyjson_doc *empty = NULL;
                if (!truthy(d)) {
                    empty = yyjson_read("{}", 2, 0);
                    d = yyjson_doc_get_root(empty);
                }
                f.t = dumps_to(&tb, (Blk){d, 0}, PYJ_INDENT1);
                pyb_puts(&lb, "tool added: ");
                yyjson_val *nm = get(d, "name");
                if (nm) py_str(&lb, nm);
                else pyb_puts(&lb, "?");
                f.s = Lbuf(b, &lb);
                yyjson_doc_free(empty);
            } else {
                f.t = dumps_to(&tb, k, 0);
                pyb_puts(&lb, "system ");
                if (bt) pyb_puts(&lb, bt);
                else pyb_puts(&lb, "None");
                f.s = Lbuf(b, &lb);
            }
            f.w = (double)nchars(f.t.s, f.t.n) / 4;
            emit(b, fr, key_of(b, role, k, -1), &f);
        } else if (role == ROLE_ASSISTANT) {
            double *w = aw ? &aw[bi] : NULL;
            Fields f = {CK_TEXT, NULL, {0}, 0, made, NULL, NULL, NULL, 0};
            if (bt && !strcmp(bt, "text")) {
                f.t = text;
                f.s = L_(b, "agent text");
                f.w = w ? *w : (double)nchars(text.s, text.n) / 4;
            } else if (bt && (!strcmp(bt, "thinking") || !strcmp(bt, "redacted_thinking"))) {
                f.kind = CK_THINKING;
                f.t = getstr(bv, "thinking");
                f.s = L_(b, f.t.n ? "thinking" : "thinking (not returned)");
                f.w = w ? *w : (double)nchars(f.t.s, f.t.n) / 4;
            } else if (bt && (!strcmp(bt, "tool_use") || !strcmp(bt, "server_tool_use"))) {
                yyjson_val *inp = get(bv, "input");
                yyjson_doc *empty = NULL;
                if (!truthy(inp)) {
                    empty = yyjson_read("{}", 2, 0);
                    inp = yyjson_doc_get_root(empty);
                }
                f.kind = CK_ACTION;
                f.t = dumps_to(&tb, (Blk){inp, 0}, PYJ_INDENT1);
                yyjson_val *nm = get(bv, "name");
                Str name = yyjson_is_str(nm) ? (Str){yyjson_get_str(nm), yyjson_get_len(nm)}
                           : nm ? (Str){"None", 4} : (Str){"?", 1};
                action_label(b, &lb, name, inp);
                f.s = Lbuf(b, &lb);
                f.w = w ? *w : (double)nchars(f.t.s, f.t.n) / 4 + 20;
                f.tool = yyjson_is_str(nm) ? L_(b, yyjson_get_str(nm)) : NULL;
                f.tuid = gets(bv, "id") ? L_(b, gets(bv, "id")) : NULL;
                yyjson_val *p = get(inp, "file_path");
                if (!truthy(p)) p = get(inp, "notebook_path");
                if (truthy(p)) {
                    char *r = yyjson_is_str(p) ? rel(&b->env, yyjson_get_str(p)) : xstrdup("");
                    f.p = L_(b, r);
                    free(r);
                }
                yyjson_doc_free(empty);
            } else {
                f.t = dumps_to(&tb, k, 0);
                if (bt) pyb_puts(&lb, bt);
                else pyb_puts(&lb, "None");
                f.s = Lbuf(b, &lb);
                f.w = w ? *w : (double)nchars(f.t.s, f.t.n) / 4;
            }
            emit(b, fr, key_of(b, role, k, -1), &f);
        } else { /* user */
            if (bt && !strcmp(bt, "text")) {
                Seg *sg;
                int ns = split_text(b, text, &sg);
                for (int si = 0; si < ns; si++) {
                    Fields f = {sg[si].kind, sg[si].src, sg[si].t, (double)nchars(sg[si].t.s, sg[si].t.n) / 4,
                                -1, NULL, NULL, NULL, 0};
                    if (f.kind == CK_PROMPT && has_results) f.kind = CK_INJECTED, f.s = L_(b, "with tool results");
                    emit(b, fr, key_of(b, role, k, si), &f);
                }
                free(sg);
            } else if (bt && !strcmp(bt, "image")) {
                PyBuf info = {0};
                long long tok = img_info(get(bv, "source"), &info);
                pyb_puts(&lb, "image ");
                pyb_put(&lb, info.s, info.n);
                tb.n = 0;
                pyb_puts(&tb, "[image ");
                pyb_put(&tb, info.s, info.n);
                pyb_puts(&tb, ": not embedded in the draft]");
                Fields f = {CK_PROMPT, Lbuf(b, &lb), {tb.s, tb.n}, (double)tok, -1, NULL, NULL, NULL, 0};
                emit(b, fr, key_of(b, role, k, -1), &f);
                pyb_free(&info);
            } else if (bt && !strcmp(bt, "tool_result")) {
                PyBuf parts = {0}, js = {0};
                Seg *segs = NULL;
                int nsegs = 0, capsegs = 0, np = 0;
                double w = 0;
                int c0 = b->nblk, nc = norm_into(b, get(bv, "content"));
                for (int ci = 0; ci < nc; ci++) {
                    Blk c = b->blk[c0 + ci];
                    const char *ct = c.wrap ? "text" : gets(c.v, "type");
                    Str piece = {0};
                    int have = 0;
                    if (ct && !strcmp(ct, "text")) {
                        Str ctext = c.wrap ? (Str){yyjson_get_str(c.v), yyjson_get_len(c.v)} : getstr(c.v, "text");
                        Seg *sg;
                        int ns = split_text(b, ctext, &sg);
                        for (int si = 0; si < ns; si++) {
                            if (sg[si].kind == CK_INJECTED) {
                                GROW(segs, nsegs, capsegs);
                                segs[nsegs++] = sg[si];
                            } else {
                                if (np++) pyb_puts(&parts, "\n");
                                pyb_put(&parts, sg[si].t.s, sg[si].t.n);
                                w += (double)nchars(sg[si].t.s, sg[si].t.n) / 4;
                            }
                        }
                        free(sg);
                    } else if (ct && !strcmp(ct, "image")) {
                        PyBuf info = {0};
                        long long tok = img_info(get(c.v, "source"), &info);
                        if (np++) pyb_puts(&parts, "\n");
                        pyb_puts(&parts, "[image ");
                        pyb_put(&parts, info.s, info.n);
                        pyb_puts(&parts, ": not embedded in the draft]");
                        w += (double)tok;
                        pyb_free(&info);
                    } else {
                        piece = dumps_to(&js, c, 0);
                        have = 1;
                    }
                    if (have) {
                        if (np++) pyb_puts(&parts, "\n");
                        pyb_put(&parts, piece.s, piece.n);
                        w += (double)nchars(piece.s, piece.n) / 4;
                    }
                }
                Fields f = {CK_RESULT, L_(b, "result"), {parts.s ? parts.s : "", parts.n}, w + 10, -1, NULL,
                            gets(bv, "tool_use_id") ? L_(b, gets(bv, "tool_use_id")) : NULL, NULL,
                            truthy(get(bv, "is_error"))};
                emit(b, fr, key_of(b, role, k, -1), &f);
                for (int si = 0; si < nsegs; si++) {
                    Fields g = {CK_INJECTED, segs[si].src, segs[si].t, (double)nchars(segs[si].t.s, segs[si].t.n) / 4,
                                -1, NULL, NULL, NULL, 0};
                    emit(b, fr, key_of(b, role, k, 1000 + si), &g);
                }
                free(segs);
                pyb_free(&parts);
                pyb_free(&js);
            } else {
                Fields f = {CK_PROMPT, NULL, dumps_to(&tb, k, 0), 0, -1, NULL, NULL, NULL, 0};
                if (bt) pyb_puts(&lb, bt);
                else pyb_puts(&lb, "None");
                f.s = Lbuf(b, &lb);
                f.w = (double)nchars(f.t.s, f.t.n) / 4;
                emit(b, fr, key_of(b, role, k, -1), &f);
            }
        }
    }
    free(aw);
    pyb_free(&tb);
    pyb_free(&lb);
}

static int new_inst(Build *b, const char *h, int k0) {
    GROW(b->inst, b->ninst, b->capinst);
    b->inst[b->ninst] = (Inst){h, k0, b->nkeys - k0};
    return b->ninst++;
}

static long long *usage_of(yyjson_val *u, long long out[5]) { /* NULL when no usage */
    if (!truthy(u)) return NULL;
    long long w = geti(u, "cache_creation_input_tokens"), w1h = geti(get(u, "cache_creation"), "ephemeral_1h_input_tokens");
    out[0] = geti(u, "input_tokens"), out[1] = geti(u, "cache_read_input_tokens");
    out[2] = w - w1h, out[3] = w1h, out[4] = geti(u, "output_tokens");
    return out;
}

static CtxCall *new_call(Build *b) {
    CtxAgent *a = b->a;
    a->calls = xrealloc(a->calls, (size_t)(a->ncalls + 1) * sizeof *a->calls);
    b->cb = xrealloc(b->cb, (size_t)(a->ncalls + 1) * sizeof *b->cb);
    memset(&b->cb[a->ncalls], 0, sizeof *b->cb);
    CtxCall *c = &a->calls[a->ncalls++];
    memset(c, 0, sizeof *c);
    c->base = -1;
    return c;
}

static int sh_eq(const char *x, const char *y) { return x && y && !strcmp(x, y); }

/* What a message's items depend on, beyond the hash it was sent under: its
 * role, an empty system message's effort, and each block of its content as
 * the draft normalizes it (a string is one text block): its canonical hash
 * and the order of its objects' keys (items' texts follow it). Claude Code
 * writes one message under one hash in more than one way (content as a string
 * or a list), so the hash alone doesn't say. */
static void key_order(Sha256 *c, yyjson_val *v, int depth) {
    if (depth > PYJ_MAX_DEPTH) return;
    size_t i, m;
    yyjson_val *k, *x;
    if (yyjson_is_obj(v)) {
        sha256_update(c, "{", 1);
        yyjson_obj_foreach(v, i, m, k, x) {
            sha256_update(c, yyjson_get_str(k), yyjson_get_len(k) + 1); /* with its NUL: keys can't run together */
            key_order(c, x, depth + 1);
        }
        sha256_update(c, "}", 1);
    } else if (yyjson_is_arr(v)) {
        sha256_update(c, "[", 1);
        yyjson_arr_foreach(v, i, m, x) key_order(c, x, depth + 1);
        sha256_update(c, "]", 1);
    }
}

static void msg_rep(Build *b, const Msg *m, uint8_t out[32]) {
    Sha256 c;
    sha256_init(&c);
    int role = msg_role(m), b0, nb;
    const char *rn = m->v ? gets(m->v, "role") : ROLE_NAMES[m->role];
    sha256_update(&c, rn ? rn : "\x01", strlen(rn ? rn : "\x01") + 1);
    if (role == ROLE_SYSTEM && m->v && !truthy(get(m->v, "content"))) {
        PyBuf e = {0};
        py_str(&e, get(get(m->v, "output_config"), "effort"));
        sha256_update(&c, e.s ? e.s : "", e.n + 1);
        pyb_free(&e);
    }
    msg_blocks(b, m, &b0, &nb);
    for (int i = 0; i < nb; i++) {
        Blk k = b->blk[b0 + i];
        sha256_update(&c, blk_hash(b, k), 32);
        if (k.wrap) sha256_update(&c, "{type\0text\0}", 12);
        else key_order(&c, k.v, 0);
    }
    sha256_final(&c, out);
}

static void rep_put(Loader *ld, const uint8_t d[32], const uint8_t r[32]) {
    int k = dmap_get(&ld->rep_ix, d);
    if (k < 0) {
        ld->rep = xrealloc(ld->rep, (size_t)(ld->nrep + 1) * sizeof *ld->rep);
        dmap_put(&ld->rep_ix, d, k = ld->nrep++, 0);
    }
    memcpy(ld->rep[k], r, 32);
}

/* What records not taken before add to the state every build needs: tool
 * results' times and calls' cwds (stale reads), the session title, thinking
 * drops. */
static void scan_new(Build *b) {
    Loader *ld = b->ld;
    Recs *R = &b->own.recs;
    for (int i = 0; i < R->n; i++) {
        Rec *r = &R->r[i];
        if (r->abs < ld->nabs) continue;
        yyjson_val *c = get(get(r->r, "message"), "content");
        size_t k, m;
        yyjson_val *x;
        if (r->type == T_USER && yyjson_is_arr(c)) {
            yyjson_arr_foreach(c, k, m, x) {
                const char *t = gets(x, "type");
                if (!t || strcmp(t, "tool_result")) continue;
                const char *tu = gets(x, "tool_use_id");
                if (!tu) tu = "\x01";
                int q = strmap_get(&ld->rt_ix, tu);
                if (q < 0) {
                    ld->rt = xrealloc(ld->rt, (size_t)(ld->nrt + 1) * sizeof *ld->rt);
                    strmap_put(&ld->rt_ix, tu, q = ld->nrt++);
                }
                ld->rt[q] = epoch(gets(r->r, "timestamp")); /* the last one counts */
            }
        } else if (r->type == T_USER && yyjson_is_str(c) && !ld->first_prompt) {
            PyBuf o = {0};
            one_line(&o, (Str){yyjson_get_str(c), yyjson_get_len(c)}, 70);
            ld->first_prompt = o.s ? o.s : xstrdup("");
        } else if (r->type == T_ASSISTANT) {
            yyjson_arr_foreach(c, k, m, x) {
                const char *t = gets(x, "type");
                if (!t || strcmp(t, "tool_use")) continue;
                const char *id = gets(x, "id");
                if (!id) id = "\x01";
                int q = strmap_get(&ld->cwd_ix, id);
                if (q < 0) {
                    ld->cwd = xrealloc(ld->cwd, (size_t)(ld->ncwd + 1) * sizeof *ld->cwd);
                    ld->cwd[ld->ncwd] = NULL;
                    strmap_put(&ld->cwd_ix, id, q = ld->ncwd++);
                }
                free(ld->cwd[q]);
                ld->cwd[q] = dup_or_null(gets(r->r, "cwd"));
            }
        } else if (r->type == T_TITLE && truthy(get(r->r, "aiTitle")) && gets(r->r, "aiTitle")) {
            free(ld->ai_title);
            ld->ai_title = xstrdup(gets(r->r, "aiTitle"));
        } else if (r->type == T_ATTACH) {
            yyjson_val *at = get(r->r, "attachment");
            const char *t = gets(at, "type");
            yyjson_val *first = get(get(at, "newlyDropped"), "first");
            if (t && !strcmp(t, "thinking_drop") && truthy(first)) {
                const char *rq = gets(at, "requestId");
                ld->drops = xrealloc(ld->drops, (size_t)(ld->ndrops + 1) * sizeof *ld->drops);
                ld->drops[ld->ndrops++] = (Drop){xstrdup(rq ? rq : "\x01"), geti32(first, "messageIndex"),
                                                 geti32(first, "blockIndex")};
            }
        }
    }
}

/* The calls of a transcript with request records (build_exact), from the
 * first request not taken yet (all of them for a full load). The maps the
 * draft builds over the whole file are built over the responses before the
 * window (kept as metadata) and the records read now. Resuming, what appended
 * records would change in calls already built sets b->fail. */
static void build_calls(Build *b) {
    CtxAgent *a = b->a;
    Loader *ld = b->ld;
    Store *own = &b->own;
    Recs *R = &own->recs;
    StrMap resp_by_req = {0}, block_resp = {0}, reqid_call = {0}, pos_of = {0}, by_id = {0};
    DMap msg_resp = {0};
    for (int k = 0; k < own->nresps; k++) { /* old ones first: the order of first records */
        Resp *rr = &own->resps[k];
        if (rr->ref && *rr->ref) strmap_put(&resp_by_req, rr->ref, k);
        if (!rr->bad) dmap_put(&msg_resp, rr->d, k, 1);
        for (int i = 0; i < rr->nkeys; i++)
            if (strmap_get(&block_resp, rr->keys[i]) < 0) strmap_put(&block_resp, rr->keys[i], k);
    }
    for (int c = 0; c < a->ncalls; c++)
        if (a->calls[c].id) strmap_put(&by_id, a->calls[c].id, c);
    if (b->resuming) { /* appended responses must not change what calls already took */
        for (int k = 0; k < own->nresps && !b->fail; k++) {
            Resp *rr = &own->resps[k];
            if (rr->old) continue;
            int was = strmap_get(&ld->used, rr->mid), same = was == rr->nrec;
            if (was >= 0 && !same) b->fail = "a response items were weighed by grew";
            else if (!same && !rr->bad && dset_has(&ld->tail_seen, rr->d))
                b->fail = "a response a call looked up appeared";
            for (int i = 0; i < rr->nkeys && !same && !b->fail; i++)
                if (strmap_get(&ld->block_missed, rr->keys[i]) >= 0) b->fail = "a block a call looked up appeared";
        }
        for (size_t i = 0; i < own->map.cap && !b->fail; i++) {
            if (own->map.v[i] < 0) continue;
            const Msg *m = &own->msgs[own->map.v[i]];
            if (m->src >= 0 && dset_has(&ld->unresolved, own->map.k[i])) b->fail = "a message a call missed appeared";
        }
    }
    int *reqs = NULL, nreqs = 0, capreqs = 0;
    long *compact = NULL;
    int ncompact = 0, capcompact = 0;
    for (int i = 0; i < R->n && !b->fail; i++) {
        Rec *r = &R->r[i];
        int fresh = r->abs >= ld->nabs; /* not taken by an earlier load */
        if (r->type == T_REQ && r->abs > ld->last_req) {
            GROW(reqs, nreqs, capreqs);
            reqs[nreqs++] = i;
            const char *id = gets(r->r, "id");
            if (id) {
                if (b->resuming && strmap_get(&by_id, id) >= 0) b->fail = "a request id seen before";
                strmap_put(&pos_of, id, (int)(r->abs - ld->last_req));
            }
        } else if (r->type == T_SHAPE && fresh) {
            const char *sh = gets(r->r, "shapeHash");
            if (!sh) continue;
            int k = strmap_get(&ld->shape_ix, sh);
            yyjson_val *shv = get(r->r, "shape");
            char *raw = shv ? yyjson_val_write(shv, 0, NULL) : NULL; /* read back as the same values */
            if (!raw) raw = xstrdup("null");
            if (k >= 0 && !strcmp(ld->shape_raw[k], raw)) free(raw); /* the same shape again */
            else if (k >= 0) { /* the last one counts */
                if (b->resuming) b->fail = "a request shape defined again, changed";
                free(ld->shape_raw[k]);
                ld->shape_raw[k] = raw;
            } else {
                ld->shape_raw = xrealloc(ld->shape_raw, (size_t)(ld->nshape + 1) * sizeof *ld->shape_raw);
                ld->shape_raw[ld->nshape] = raw;
                strmap_put(&ld->shape_ix, sh, ld->nshape++);
            }
        } else if (r->type == T_SYSTEM && r->abs > ld->last_req) {
            const char *st = gets(r->r, "subtype");
            if (st && !strcmp(st, "compact_boundary")) {
                GROW(compact, ncompact, capcompact);
                compact[ncompact++] = r->abs;
            }
        } else if (r->type == T_BLOB && fresh && b->resuming) {
            uint8_t d[32], rp[32];
            yyjson_val *m = get(r->r, "message");
            if (m && hex_digest(gets(r->r, "hash"), d) && (dset_has(&ld->old_only, d) || dset_has(&ld->tail_seen, d))) {
                int k = dmap_get(&ld->rep_ix, d);
                msg_rep(b, &(Msg){m, 0, 0, 0, r->abs}, rp); /* the last blob counts: written the same, no change */
                if (k < 0 || memcmp(ld->rep[k], rp, 32)) b->fail = "a message written again as a blob, differently";
            }
        }
    }
    yyjson_doc **shape_doc = xcalloc((size_t)ld->nshape + 1, sizeof *shape_doc);
    Free fr = {0};
    StrMap same_ix = {0}; /* hash -> its queue of dropped instances (this call) */
    long prev_pos = ld->last_req;
    for (int qi = 0; qi < nreqs && !b->fail; qi++) {
        if (b->cancel && atomic_load(b->cancel)) break;
        int ci = a->ncalls;
        Rec *rec = &R->r[reqs[qi]];
        yyjson_val *r = rec->r;
        const char *bid = gets(r, "base");
        int base = bid ? strmap_get(&by_id, bid) : -1;
        int ref = base >= 0 ? base : ci - 1;
        CallB *prev = ref >= 0 ? &b->cb[ref] : NULL;
        int keep = geti32(r, "keep");
        int kept = base >= 0 ? keep : 0;
        int nprev = prev ? prev->ninst : 0;
        if (kept > nprev) kept = nprev;
        if (kept < 0) kept = 0;
        int *inst = NULL, ninst = 0, capinst = 0;
        for (int i = 0; i < kept; i++) {
            GROW(inst, ninst, capinst);
            inst[ninst++] = prev->inst[i];
        }
        /* dropped messages: their items, by key, for rewritten ones */
        free_reset(&fr);
        strmap_free(&same_ix);
        int ndropped = nprev - kept, *dropped = prev ? prev->inst + kept : NULL;
        int *qnext = xmalloc(((size_t)ndropped + 1) * sizeof *qnext); /* next dropped with the same hash */
        int *qhead = NULL, nq = 0;
        for (int i = 0; i < ndropped; i++) {
            Inst *in = &b->inst[dropped[i]];
            for (int k = 0; k < in->nk; k++) free_add(&fr, &b->keys[in->k0 + k], b->kitems[in->k0 + k]);
            qnext[i] = -1;
        }
        for (int i = ndropped - 1; i >= 0; i--) { /* queues in dropped order */
            const char *h = b->inst[dropped[i]].h;
            int q = strmap_get(&same_ix, h);
            if (q < 0) {
                qhead = xrealloc(qhead, (size_t)(nq + 1) * sizeof *qhead);
                qhead[nq] = -1;
                strmap_put(&same_ix, h, q = nq++);
            }
            qnext[i] = qhead[q];
            qhead[q] = i;
        }
        yyjson_val *tail = get(r, "tail");
        size_t ti, tm;
        yyjson_val *hv;
        yyjson_arr_foreach(tail, ti, tm, hv) {
            const char *h = yyjson_get_str(hv);
            if (!h) h = "";
            int q = strmap_get(&same_ix, h), same = -1;
            if (q >= 0) {
                /* the first not taken yet: a taken one leaves its queue (a call holds an instance
                 * once, so the draft's "not in used" needs nothing more) */
                if (qhead[q] >= 0) same = qhead[q], qhead[q] = qnext[same];
            }
            if (same >= 0) { /* an unchanged message sent again keeps its instance */
                GROW(inst, ninst, capinst);
                inst[ninst++] = dropped[same];
                Inst *in = &b->inst[dropped[same]];
                for (int k = 0; k < in->nk; k++) free_remove(&fr, &b->keys[in->k0 + k], b->kitems[in->k0 + k]);
                continue;
            }
            uint8_t d[32];
            int valid = hex_digest(h, d);
            if (valid) dset_add(&ld->tail_seen, d);
            const Msg *m = resolve(b, h);
            if (b->fail) break;
            if (m && valid) {
                uint8_t rp[32];
                msg_rep(b, m, rp);
                rep_put(ld, d, rp);
            }
            int rk = valid ? dmap_get(&msg_resp, d) : -1;
            if (rk < 0 && m && msg_role(m) == ROLE_ASSISTANT) {
                /* a response sent back changed (a block dropped): found by any block it still has */
                int b0, nb;
                msg_blocks(b, m, &b0, &nb);
                for (int i = 0; i < nb && rk < 0; i++) {
                    Blk k = b->blk[b0 + i];
                    const char *id = k.wrap ? NULL : gets(k.v, "id");
                    if (id) {
                        if ((rk = strmap_get(&block_resp, id)) < 0) strmap_put(&ld->block_missed, id, 1);
                    }
                    if (rk < 0) {
                        char hx[65];
                        sha256_hex(blk_hash(b, k), hx);
                        if ((rk = strmap_get(&block_resp, hx)) < 0) strmap_put(&ld->block_missed, hx, 1);
                    }
                }
            }
            Resp *rr = rk >= 0 ? &own->resps[rk] : NULL;
            int made = -1;
            if (rr) {
                strmap_put(&ld->used, rr->mid, rr->nrec); /* its weights are in items now */
                if (rr->ref) made = strmap_get(&by_id, rr->ref);
            }
            int k0 = b->nkeys;
            b->kh = h, b->kpos = 0;
            if (!m) {
                PyBuf t = {0};
                char hh[13];
                snprintf(hh, sizeof hh, "%.12s", h);
                pyb_puts(&t, "[message ");
                pyb_puts(&t, hh);
                pyb_puts(&t, "\xE2\x80\xA6 not found in any transcript of the project]");
                Fields f = {CK_MISSING, L_(b, "message not found"), {t.s, t.n}, 0, -1, NULL, NULL, NULL, 0};
                Key key;
                memset(&key, 0, sizeof key);
                if (!hex_digest(h, key.d)) {
                    Sha256 c;
                    sha256_init(&c);
                    sha256_update(&c, h, strlen(h));
                    sha256_final(&c, key.d);
                }
                key.tag = 2;
                key.sub = -1;
                emit(b, NULL, key, &f);
                pyb_free(&t);
            } else blocks_of(b, &fr, m, made, rr);
            GROW(inst, ninst, capinst);
            inst[ninst++] = new_inst(b, L_(b, h), k0);
        }
        free(qnext), free(qhead);
        if (b->fail) {
            free(inst);
            break;
        }
        /* system and tools */
        const char *sh = gets(r, "shapeHash");
        int *ids = NULL, nids = 0, capids = 0;
        CtxCall *rc = ref >= 0 ? &a->calls[ref] : NULL;
        if (prev && (sh_eq(prev->sh, sh) || (!prev->sh && !sh))) {
            ids = rc->shape, nids = rc->nshape; /* shared with the call it came from */
        } else { /* new or changed: unchanged system blocks keep their item, the rest are new */
            int sk = sh ? strmap_get(&ld->shape_ix, sh) : -1;
            if (sk >= 0 && !shape_doc[sk])
                shape_doc[sk] = yyjson_read_opts(ld->shape_raw[sk], strlen(ld->shape_raw[sk]), PYJ_READ, NULL, NULL);
            yyjson_val *shape = sk >= 0 ? yyjson_doc_get_root(shape_doc[sk]) : NULL;
            StrMap old = {0}; /* text -> system item of the call before */
            char key[24];
            for (int i = 0; rc && i < rc->nshape; i++) {
                CtxItem *it = &b->items[rc->shape[i]];
                if (it->kind != CK_SYSTEM) continue;
                snprintf(key, sizeof key, "%d", it->x);
                strmap_put(&old, key, rc->shape[i]);
            }
            yyjson_val *sys = get(shape, "system");
            size_t i, m;
            yyjson_val *x;
            PyBuf kh = {0};
            pyb_puts(&kh, "shape ");
            pyb_puts(&kh, sh ? sh : "");
            b->kh = kh.s;
            yyjson_arr_foreach(sys, i, m, x) {
                b->kpos = (int)i + 1;
                PyBuf t = {0};
                if (yyjson_is_obj(x)) {
                    Str st = getstr(x, "text");
                    pyb_put(&t, st.s, st.n);
                } else py_str(&t, x);
                int same = -1, tx = pool_find(&b->S, t.s ? t.s : "", t.n);
                if (tx >= 0) {
                    snprintf(key, sizeof key, "%d", tx);
                    same = strmap_get(&old, key);
                    if (same >= 0) strmap_put(&old, key, -1); /* popped */
                }
                if (same < 0) {
                    Fields f = {CK_SYSTEM, L_(b, "system prompt"), {t.s ? t.s : "", t.n},
                                (double)nchars(t.s ? t.s : "", t.n) / 4, -1, NULL, NULL, NULL, 0};
                    same = finish_item(b, &f);
                }
                GROW(ids, nids, capids);
                ids[nids++] = same;
                pyb_free(&t);
            }
            strmap_free(&old);
            yyjson_val *tools = get(shape, "tools");
            if (yyjson_is_arr(tools) && yyjson_arr_size(tools)) {
                int nt = (int)yyjson_arr_size(tools);
                int *tx = xmalloc((size_t)nt * sizeof *tx);
                const char **names = xmalloc((size_t)nt * sizeof *names);
                long long total = 0;
                PyBuf t = {0}, lab = {0};
                yyjson_arr_foreach(tools, i, m, x) {
                    dumps_to(&t, (Blk){x, 0}, PYJ_INDENT1);
                    total += (long long)nchars(t.s, t.n);
                    tx[i] = -1;
                    yyjson_val *nm = get(x, "name");
                    lab.n = 0;
                    if (nm) py_str(&lab, nm);
                    else pyb_puts(&lab, "?");
                    names[i] = Lbuf(b, &lab);
                }
                char st[48];
                snprintf(st, sizeof st, "tool definitions (%d)", nt);
                b->kpos = 0;
                Fields f = {CK_TOOLS, L_(b, st), {"", 0}, (double)total / 4, -1, NULL, NULL, NULL, 0};
                int iid = finish_item(b, &f);
                yyjson_arr_foreach(tools, i, m, x) { /* the S table: after the item's own "" */
                    dumps_to(&t, (Blk){x, 0}, PYJ_INDENT1);
                    tx[i] = pool_add(&b->S, t.s, t.n);
                }
                b->items[iid].tx = tx;
                b->items[iid].ntx = nt;
                b->items[iid].names = names;
                b->items[iid].n = (int)total;
                GROW(ids, nids, capids);
                memmove(ids + 1, ids, (size_t)nids * sizeof *ids);
                ids[0] = iid;
                nids++;
                pyb_free(&t);
                pyb_free(&lab);
            }
            if (!truthy(shape)) {
                Fields f = {CK_MISSING, L_(b, "system + tools not found"), S_("[request shape not found]"), 0, -1,
                            NULL, NULL, NULL, 0};
                b->kpos = -1;
                GROW(ids, nids, capids);
                ids[nids++] = finish_item(b, &f);
            }
            b->kh = NULL;
            pyb_free(&kh);
            b->shapes = xrealloc(b->shapes, (size_t)(b->nshapes + 1) * sizeof *b->shapes);
            b->shapes[b->nshapes++] = ids;
        }
        const char *rid = gets(r, "id");
        long pos = rid ? ld->last_req + strmap_get(&pos_of, rid) : rec->abs; /* the last request with that id */
        int comp = 0;
        for (int k = 0; k < ncompact; k++) comp |= prev_pos < compact[k] && compact[k] < pos;
        comp = comp && keep == 0;
        prev_pos = pos;
        if (keep && base < 0) a->broken++;
        CtxCall *c = new_call(b);
        b->cb[ci].sh = sh ? L_(b, sh) : NULL;
        b->cb[ci].inst = inst;
        b->cb[ci].ninst = ninst;
        b->cb[ci].kept = kept;
        const char *pm = gets(get(r, "params"), "model");
        b->cb[ci].pmodel = pm ? L_(b, pm) : NULL;
        c->t = epoch(gets(r, "timestamp"));
        c->id = rid ? L_(b, rid) : NULL;
        c->query = gets(r, "querySource") ? L_(b, gets(r, "querySource")) : NULL;
        c->base = base;
        c->keep = keep;
        c->shape = ids;
        c->nshape = nids;
        c->comp = comp;
        if (rid) strmap_put(&by_id, rid, ci);
    }
    if (nreqs && !b->fail) ld->last_req = R->r[reqs[nreqs - 1]].abs;
    /* every call's response (a later record may answer an earlier call): its model, usage, request id */
    for (int ci = 0; ci < a->ncalls && !b->fail; ci++) {
        CtxCall *c = &a->calls[ci];
        int rk = c->id ? strmap_get(&resp_by_req, c->id) : -1;
        Resp *rr = rk >= 0 ? &own->resps[rk] : NULL;
        const char *model = b->cb[ci].pmodel;
        if (rr && rr->model && *rr->model) model = rr->model;
        c->model = model ? L_(b, model) : NULL;
        c->has_u = rr && rr->has_u;
        if (c->has_u) memcpy(c->u, rr->u, sizeof c->u);
        else memset(c->u, 0, sizeof c->u);
        if (rr) strmap_put(&reqid_call, rr->rq ? rr->rq : "\x01", ci);
    }
    /* thinking blocks the API dropped (prefix mismatch): weight 0 at that call; by request id in order of
     * first appearance, as the draft groups them */
    for (int ci = 0; ci < a->ncalls; ci++) {
        free(a->calls[ci].drop);
        a->calls[ci].drop = NULL, a->calls[ci].ndrop = 0;
    }
    StrMap group = {0};
    int *order = xmalloc(((size_t)ld->ndrops + 1) * sizeof *order), norder = 0;
    for (int k = 0; k < ld->ndrops; k++)
        if (strmap_get(&group, ld->drops[k].rq) < 0) strmap_put(&group, ld->drops[k].rq, 1), order[norder++] = k;
    int *th = NULL, nth = 0, capth = 0;
    for (int g = 0; g < norder && !b->fail; g++) {
        const char *rq = ld->drops[order[g]].rq;
        int ci = strmap_get(&reqid_call, rq);
        if (ci < 0 || ci >= a->ncalls) continue;
        CtxCall *c = &a->calls[ci];
        CallB *cb = &b->cb[ci];
        for (int j = order[g]; j < ld->ndrops; j++) {
            if (strcmp(ld->drops[j].rq, rq)) continue;
            /* the indices as Python takes them: negative from the end (beyond it the draft fails;
             * skipped here), a block past the last thinking one is the last */
            int mi = ld->drops[j].mi, bi = ld->drops[j].bi;
            if (mi < 0) mi += cb->ninst;
            if (mi < 0 || mi >= cb->ninst) continue;
            Inst *in = &b->inst[cb->inst[mi]];
            for (int q = 0; q < in->nk; q++)
                if (b->items[b->kitems[in->k0 + q]].kind == CK_THINKING) {
                    GROW(th, nth, capth);
                    th[nth++] = b->kitems[in->k0 + q];
                }
            if (bi < 0) bi += nth;
            if (!nth || bi < 0) {
                nth = 0;
                continue;
            }
            c->drop = xrealloc(c->drop, (size_t)(c->ndrop + 1) * sizeof *c->drop);
            c->drop[c->ndrop++] = th[bi < nth ? bi : nth - 1];
            nth = 0;
        }
    }
    free(th), free(order);
    strmap_free(&group);
    for (int k = 0; k < ld->nshape; k++) yyjson_doc_free(shape_doc[k]);
    free(shape_doc);
    free(reqs), free(compact);
    free_done(&fr);
    strmap_free(&same_ix), strmap_free(&resp_by_req), strmap_free(&block_resp);
    strmap_free(&reqid_call), strmap_free(&pos_of);
    strmap_free(&by_id);
    dmap_free(&msg_resp);
}

/* Older transcripts (build_estimated): the conversation records give the
 * messages; one call per assistant response. */
static void build_estimated(Build *b) {
    CtxAgent *a = b->a;
    Store *own = &b->own;
    Recs *R = &own->recs;
    Fields sf = {CK_SYSTEM, L_(b, "system prompt + tools (estimated)"),
                 S_("[not in the transcript: estimated from the first call's usage]"), 0, -1, NULL, NULL, NULL, 0};
    b->kh = "estimated system", b->kpos = 0;
    int sys_id = finish_item(b, &sf);
    typedef struct { char h[65]; Msg m; int resp, made; } EMsg;
    EMsg *msgs = NULL;
    int nmsgs = 0, capmsgs = 0;
    char (*prev)[65] = NULL;
    int nprev = 0, comp_next = 0;
    char *seen = xcalloc((size_t)own->nresps + 1, 1);
    PyBuf hb = {0};
    for (int i = 0; i < R->n; i++) {
        if (b->cancel && atomic_load(b->cancel)) break;
        Rec *r = &R->r[i];
        yyjson_val *msg = get(r->r, "message");
        if (r->type == T_SYSTEM && gets(r->r, "subtype") && !strcmp(gets(r->r, "subtype"), "compact_boundary")) {
            nmsgs = 0;
            comp_next = 1;
        } else if (r->type == T_USER && yyjson_is_obj(msg)) {
            GROW(msgs, nmsgs, capmsgs);
            EMsg *e = &msgs[nmsgs++];
            hb.n = 0;
            uint8_t d[32];
            if (pyj_dumps(&hb, msg, PYJ_CANON)) snprintf(e->h, sizeof e->h, "unhashable %d", nmsgs); /* equals no other */
            else {
                hash_buf(&hb, d);
                sha256_hex(d, e->h);
            }
            e->m = (Msg){msg, 0, 0, 0, r->abs};
            e->resp = e->made = -1;
        } else if (r->type == T_ATTACH) {
            yyjson_val *at = get(r->r, "attachment");
            yyjson_val *txt = yyjson_is_str(get(at, "content")) ? get(at, "content") : get(at, "text");
            const char *ty = gets(at, "type");
            if (yyjson_is_str(txt) && strip((Str){yyjson_get_str(txt), yyjson_get_len(txt)}).n &&
                !(ty && !strcmp(ty, "hook_success"))) {
                GROW(msgs, nmsgs, capmsgs);
                EMsg *e = &msgs[nmsgs++];
                int b0 = add_blk(b, txt, 1);
                uint8_t d[32];
                hash_rebuilt(b, ROLE_SYSTEM, b0, 1, d);
                sha256_hex(d, e->h);
                e->m = (Msg){NULL, ROLE_SYSTEM, b0, 1, r->abs};
                e->resp = e->made = -1;
            }
        } else if (r->type == T_ASSISTANT && yyjson_is_obj(msg)) {
            const char *mid = gets(msg, "id");
            if (!mid || !*mid) mid = gets(r->r, "uuid");
            if (!mid || !*mid) mid = "\x01";
            int rk = strmap_get(&own->resp_ix, mid);
            if (rk < 0 || seen[rk]) continue;
            seen[rk] = 1;
            Resp *rr = &own->resps[rk];
            int keep = 0;
            while (keep < nmsgs && keep < nprev && !strcmp(msgs[keep].h, prev[keep])) keep++;
            int base = a->ncalls - 1, ci = a->ncalls;
            CallB *bb = base >= 0 ? &b->cb[base] : NULL;
            int *inst = NULL, ninst = 0, capinst = 0;
            for (int k = 0; bb && k < keep && k < bb->ninst; k++) {
                GROW(inst, ninst, capinst);
                inst[ninst++] = bb->inst[k];
            }
            for (int k = keep; k < nmsgs; k++) {
                int k0 = b->nkeys;
                Resp *r2 = msgs[k].resp >= 0 ? &own->resps[msgs[k].resp] : NULL;
                b->kh = msgs[k].h, b->kpos = 0;
                blocks_of(b, NULL, &msgs[k].m, msgs[k].made, r2);
                GROW(inst, ninst, capinst);
                inst[ninst++] = new_inst(b, L_(b, msgs[k].h), k0);
            }
            yyjson_val *last = resp_msg(own, rr, rr->n - 1);
            CtxCall *c = new_call(b);
            b->cb[ci].inst = inst;
            b->cb[ci].ninst = ninst;
            c->t = epoch(gets(own->recs.r[rr->recs[0]].r, "timestamp"));
            c->id = L_(b, mid);
            c->model = gets(last, "model") ? L_(b, gets(last, "model")) : NULL;
            c->base = base;
            c->keep = keep;
            if (!b->nshapes) {
                b->shapes = xmalloc(sizeof *b->shapes);
                b->shapes[b->nshapes++] = xmalloc(sizeof **b->shapes);
                b->shapes[0][0] = sys_id;
            }
            c->shape = b->shapes[0];
            c->nshape = 1;
            c->has_u = usage_of(get(last, "usage"), c->u) != NULL;
            c->comp = comp_next;
            comp_next = 0;
            prev = xrealloc(prev, (size_t)(nmsgs + 1) * sizeof *prev);
            for (int k = 0; k < nmsgs; k++) memcpy(prev[k], msgs[k].h, 65);
            nprev = nmsgs;
            GROW(msgs, nmsgs, capmsgs);
            EMsg *e = &msgs[nmsgs++];
            int b0, nb;
            resp_blocks(b, own, rr, &b0, &nb);
            e->m = (Msg){NULL, ROLE_ASSISTANT, b0, nb, rr->first};
            if (rr->bad) snprintf(e->h, sizeof e->h, "unhashable %d", nmsgs); /* equals no other */
            else sha256_hex(rr->d, e->h);
            e->resp = rk;
            e->made = ci;
        }
    }
    /* the system prompt and tools: what the first call's usage leaves after its messages */
    if (a->ncalls && a->calls[0].has_u) {
        CtxCall *c = &a->calls[0];
        double tot = (double)(c->u[0] + c->u[1] + c->u[2] + c->u[3]), msgw = 0;
        for (int i = 0; i < b->cb[0].ninst; i++) {
            Inst *in = &b->inst[b->cb[0].inst[i]];
            for (int k = 0; k < in->nk; k++) msgw += b->items[b->kitems[in->k0 + k]].w;
        }
        b->items[sys_id].w = tot - msgw > 0 ? tot - msgw : 0;
    }
    free(msgs), free(prev), free(seen);
    pyb_free(&hb);
}

/* After the calls (post): each call's order, items' enter and leave, tool
 * calls linked with their results. */
static void post(Build *b) {
    CtxAgent *a = b->a;
    int *mark = xcalloc((size_t)b->nitems + 1, sizeof *mark), *cur = xcalloc((size_t)b->nitems + 1, sizeof *cur);
    for (int ci = 0; ci < a->ncalls; ci++) {
        CtxCall *c = &a->calls[ci];
        CallB *cb = &b->cb[ci];
        int n = c->nshape;
        for (int i = 0; i < cb->ninst; i++) n += b->inst[cb->inst[i]].nk;
        c->order = xmalloc((size_t)(n + 1) * sizeof *c->order);
        c->norder = 0;
        for (int i = 0; i < c->nshape; i++) c->order[c->norder++] = c->shape[i];
        for (int i = 0; i < cb->ninst; i++) {
            Inst *in = &b->inst[cb->inst[i]];
            for (int k = 0; k < in->nk; k++) c->order[c->norder++] = b->kitems[in->k0 + k];
        }
        int ref = c->base >= 0 ? c->base : ci - 1; /* no base: compared with the call before */
        for (int i = 0; ref >= 0 && i < a->calls[ref].norder; i++) mark[a->calls[ref].order[i]] = ci + 1;
        for (int i = 0; i < c->norder; i++) {
            int it = c->order[i];
            cur[it] = ci + 1;
            if (mark[it] != ci + 1 && b->items[it].enter < 0) b->items[it].enter = ci;
        }
        for (int i = 0; ref >= 0 && i < a->calls[ref].norder; i++) {
            int it = a->calls[ref].order[i];
            if (cur[it] != ci + 1 && b->items[it].leave < 0) b->items[it].leave = ci;
        }
    }
    free(mark), free(cur);
    for (int k = 0; k < b->nresult; k++) {
        int aid = strmap_get(&b->tool_use, b->result_of[k].tu), rid = b->result_of[k].item;
        if (aid < 0) continue;
        CtxItem *r = &b->items[rid], *ac = &b->items[aid];
        r->action = aid;
        ac->result = rid;
        r->src = ac->src;
        r->tool = ac->tool;
        if (ac->path) r->path = ac->path;
    }
}

/* ---- shell commands, as build_ctx.py reads them ---- */

typedef struct {
    char **w;
    int n, cap;
} Words;

static void words_add(Words *ws, const char *s, size_t n) {
    GROW(ws->w, ws->n, ws->cap);
    ws->w[ws->n++] = xstrndup(s, n);
}

static void words_free(Words *ws) {
    for (int i = 0; i < ws->n; i++) free(ws->w[i]);
    free(ws->w);
    memset(ws, 0, sizeof *ws);
}

/* shlex.split(s): POSIX mode, whitespace split, no comments; -1 on its
 * ValueError (no closing quotation, no escaped character). */
static int shlex_split(const char *s, size_t n, Words *out) {
    size_t i = 0;
    PyBuf tok = {0};
    for (;;) {
        char state = ' ', esc = 0;
        int quoted = 0;
        tok.n = 0;
        for (;;) {
            int eof = i >= n;
            char c = eof ? 0 : s[i++];
            int ws = c == ' ' || c == '\t' || c == '\r' || c == '\n';
            if (state == ' ') {
                if (eof) { state = 0; break; }
                if (ws) { if (tok.n || quoted) break; continue; }
                if (c == '\\') esc = 'a', state = '\\';
                else if (c == '\'' || c == '"') state = c;
                else { pyb_put(&tok, &c, 1); state = 'a'; }
            } else if (state == '\'' || state == '"') {
                quoted = 1;
                if (eof) { pyb_free(&tok); return -1; }
                if (c == state) state = 'a';
                else if (c == '\\' && state == '"') esc = state, state = '\\';
                else pyb_put(&tok, &c, 1);
            } else if (state == '\\') {
                if (eof) { pyb_free(&tok); return -1; }
                if ((esc == '\'' || esc == '"') && c != '\\' && c != esc) pyb_put(&tok, "\\", 1);
                pyb_put(&tok, &c, 1);
                state = esc;
            } else { /* 'a' */
                if (eof) { state = 0; break; }
                if (ws) { state = ' '; if (tok.n || quoted) break; continue; }
                if (c == '\'' || c == '"') state = c;
                else if (c == '\\') esc = 'a', state = '\\';
                else pyb_put(&tok, &c, 1);
            }
        }
        if (!quoted && !tok.n) break; /* None: the end */
        words_add(out, tok.s ? tok.s : "", tok.n);
    }
    pyb_free(&tok);
    return 0;
}

static void split_ws(const char *s, size_t n, Words *out) { /* str.split() */
    for (size_t i = 0; i < n;) {
        i = skip_space(s, n, i);
        if (i >= n) break;
        size_t j = i;
        int k;
        while (j < n && !py_space(cp_at(s, n, j, &k))) j += (size_t)k;
        words_add(out, s + i, j - i);
        i = j;
    }
}

/* split_unquoted: split on any of seps outside single or double quotes. */
static int split_unquoted(Str s, const char *const *seps, int nseps, Str **out) {
    Str *v = NULL;
    int n = 0, cap = 0;
    size_t start = 0;
    char q = 0;
    for (size_t i = 0; i < s.n;) {
        char ch = s.s[i];
        if (q) {
            if (ch == q) q = 0;
            else if (ch == '\\' && q == '"' && i + 1 < s.n) i++;
        } else if (ch == '\'' || ch == '"') q = ch;
        else {
            int hit = -1;
            for (int k = 0; k < nseps && hit < 0; k++)
                if (starts((Str){s.s + i, s.n - i}, seps[k])) hit = k;
            if (hit >= 0) {
                GROW(v, n, cap);
                v[n++] = (Str){s.s + start, i - start};
                i += strlen(seps[hit]);
                start = i;
                continue;
            }
        }
        i++;
    }
    GROW(v, n, cap);
    v[n++] = (Str){s.s + start, s.n - start};
    *out = v;
    return n;
}

typedef struct {
    char *cwd;
    Str seg;
    Words words;
} Segment;

/* segments: (cwd, segment, words of its pipeline's first command), following cd. */
static int segments(const Env *e, Str cmd, const char *cwd0, Segment **out) {
    static const char *const seps[] = {"&&", "||", ";", "\n"}, *const pipe[] = {"|"};
    Str *segs;
    int ns = split_unquoted(cmd, seps, 4, &segs);
    char *cwd = xstrdup(cwd0 && *cwd0 ? cwd0 : e->root);
    Segment *v = NULL;
    int n = 0, cap = 0;
    for (int i = 0; i < ns; i++) {
        Str *ps;
        split_unquoted(segs[i], pipe, 1, &ps);
        Words w = {0};
        if (shlex_split(ps[0].s, ps[0].n, &w) < 0) {
            words_free(&w);
            split_ws(ps[0].s, ps[0].n, &w);
        }
        free(ps);
        if (!w.n) { words_free(&w); continue; }
        if (!strcmp(w.w[0], "cd") && w.n > 1) {
            char *t = w.w[1];
            size_t a = 0, z = strlen(t);
            while (a < z && (t[a] == '\'' || t[a] == '"')) a++;
            while (z > a && (t[z - 1] == '\'' || t[z - 1] == '"')) z--;
            char *raw = xstrndup(t + a, z - a), *d = expanduser(raw);
            free(raw);
            if (d[0] == '/') {
                free(cwd);
                cwd = d;
            } else {
                char *j = path_join(cwd, d);
                free(cwd);
                cwd = normpath(j);
                free(j);
                free(d);
            }
            words_free(&w);
            continue;
        }
        GROW(v, n, cap);
        v[n++] = (Segment){xstrdup(cwd), segs[i], w};
    }
    free(cwd);
    free(segs);
    *out = v;
    return n;
}

static void segments_free(Segment *v, int n) {
    for (int i = 0; i < n; i++) free(v[i].cwd), words_free(&v[i].words);
    free(v);
}

static Str strip_quotes(const char *t) {
    size_t a = 0, z = strlen(t);
    while (a < z && (t[a] == '\'' || t[a] == '"')) a++;
    while (z > a && (t[z - 1] == '\'' || t[z - 1] == '"')) z--;
    return (Str){t + a, z - a};
}

/* re.match(r"^[class]+$", s): $ also before a final newline. */
static int all_of(Str s, const char *extra, int word) {
    size_t n = s.n;
    if (n && s.s[n - 1] == '\n') n--;
    if (!n) return 0;
    for (size_t i = 0; i < n;) {
        int k;
        unsigned c = cp_at(s.s, n, i, &k);
        if (!((word && py_word(c)) || (c < 0x80 && strchr(extra, (int)c)))) return 0;
        i += (size_t)k;
    }
    return 1;
}

static int file_like(const char *tok) {
    Str t = strip_quotes(tok);
    if (!t.n || t.s[0] == '-' || memchr(t.s, '*', t.n) || memchr(t.s, '$', t.n) || !all_of(t, "./~+@-", 1)) return 0;
    char ext[16];
    ext_of(t.s, t.n, ext, sizeof ext);
    char *p = xstrndup(t.s, t.n);
    const char *bn = base_name(p);
    int ok = known_ext(ext) || !strcmp(bn, "Makefile") || !strcmp(bn, "makefile");
    free(p);
    return ok;
}

static char *absolute(const char *tok, const char *cwd) {
    Str t = strip_quotes(tok);
    char *raw = xstrndup(t.s, t.n), *p = expanduser(raw);
    free(raw);
    char *j = p[0] == '/' ? xstrdup(p) : path_join(cwd, p), *o = normpath(j);
    free(p), free(j);
    return o;
}

/* PY_WRITE: open\([^)]*,\s*['"][wa]|\.write_text\(|\.write\( */
static int py_write_at(Str s, size_t i) {
    Str r = {s.s + i, s.n - i};
    if (starts(r, ".write_text(") || starts(r, ".write(")) return 1;
    if (!starts(r, "open(")) return 0;
    for (size_t j = i + 5; j < s.n && s.s[j] != ')'; j++) {
        if (s.s[j] != ',') continue;
        size_t k = skip_space(s.s, s.n, j + 1);
        if (k + 1 < s.n && (s.s[k] == '\'' || s.s[k] == '"') && (s.s[k + 1] == 'w' || s.s[k + 1] == 'a')) return 1;
    }
    return 0;
}

static int py_write(Str s) {
    for (size_t i = 0; i < s.n; i++)
        if ((s.s[i] == 'o' || s.s[i] == '.') && py_write_at(s, i)) return 1;
    return 0;
}

static int word_lit(Str s, size_t i, const char *w) { /* \bw\b at i */
    size_t k = strlen(w);
    return i + k <= s.n && !memcmp(s.s + i, w, k) && !word_before(s.s, i) && !word_at(s.s, s.n, i + k);
}

static int letters_with_i(Str s, size_t k, int upper) { /* -[a-z]*i (or [a-zA-Z]) at k */
    if (k >= s.n || s.s[k] != '-') return 0;
    for (size_t j = k + 1; j < s.n; j++) {
        char c = s.s[j];
        if (c == 'i') return 1;
        if (!((c >= 'a' && c <= 'z') || (upper && c >= 'A' && c <= 'Z'))) return 0;
    }
    return 0;
}

/* WRITE_CMD.search(cmd): a command that may write files. */
static int write_cmd(Str s) {
    static const char *const words[] = {"tee", "mv", "cp", "rm", "truncate", "patch"};
    static const char *const git[] = {"checkout", "restore", "reset", "stash", "apply", "merge", "rebase", "pull", "am"};
    for (size_t i = 0; i < s.n; i++) {
        char c = s.s[i];
        if (c == '>' && i > 0 && s.s[i - 1] != '&' && s.s[i - 1] != '2') return 1;
        if ((c == 'o' || c == '.') && py_write_at(s, i)) return 1;
        if (!((c >= 'a' && c <= 'z'))) continue;
        if (word_before(s.s, i)) continue; /* every literal starts with \b */
        if (starts((Str){s.s + i, s.n - i}, "sed")) {
            size_t k = skip_space(s.s, s.n, i + 3);
            if (k > i + 3 && (letters_with_i(s, k, 1) || starts((Str){s.s + k, s.n - k}, "--in-place"))) return 1;
        }
        for (size_t w = 0; w < sizeof words / sizeof *words; w++)
            if (word_lit(s, i, words[w])) return 1;
        if (starts((Str){s.s + i, s.n - i}, "git")) {
            size_t k = skip_space(s.s, s.n, i + 3);
            if (k > i + 3)
                for (size_t g = 0; g < sizeof git / sizeof *git; g++) {
                    size_t gl = strlen(git[g]);
                    if (k + gl <= s.n && !memcmp(s.s + k, git[g], gl) && !word_at(s.s, s.n, k + gl)) return 1;
                }
        }
        if (starts((Str){s.s + i, s.n - i}, "perl")) {
            size_t k = skip_space(s.s, s.n, i + 4);
            if (k > i + 4 && letters_with_i(s, k, 0)) return 1;
        }
    }
    return 0;
}

typedef struct {
    char **p;
    int n, cap;
} PathSet; /* a small set of paths, insertion ordered */

static void set_add(PathSet *ps, char *p) { /* takes p */
    for (int i = 0; i < ps->n; i++)
        if (!strcmp(ps->p[i], p)) { free(p); return; }
    GROW(ps->p, ps->n, ps->cap);
    ps->p[ps->n++] = p;
}

static int set_has(const PathSet *ps, const char *p) {
    for (int i = 0; i < ps->n; i++)
        if (!strcmp(ps->p[i], p)) return 1;
    return 0;
}

static void set_free(PathSet *ps) {
    for (int i = 0; i < ps->n; i++) free(ps->p[i]);
    free(ps->p);
    memset(ps, 0, sizeof *ps);
}

static int cmp_strp(const void *a, const void *b) { return strcmp(*(char *const *)a, *(char *const *)b); }
static void set_sort(PathSet *ps) { if (ps->n > 1) qsort(ps->p, (size_t)ps->n, sizeof *ps->p, cmp_strp); }

/* QUOTED_FILE.findall(cmd): file names a python heredoc assigns or opens:
 * (?:open\(\s*|\w\s*=\s*|[\[(,]\s*)['"]([\w./~+-]+\.[A-Za-z]\w{0,4})['"] */
static void quoted_files(Str s, const char *cwd, PathSet *out) {
    for (size_t i = 0; i < s.n;) {
        int k;
        unsigned c = cp_at(s.s, s.n, i, &k);
        size_t q = SIZE_MAX;
        if (starts((Str){s.s + i, s.n - i}, "open(")) q = skip_space(s.s, s.n, i + 5);
        if ((q == SIZE_MAX || q >= s.n || (s.s[q] != '\'' && s.s[q] != '"')) && py_word(c)) {
            size_t j = skip_space(s.s, s.n, i + (size_t)k);
            q = j < s.n && s.s[j] == '=' ? skip_space(s.s, s.n, j + 1) : SIZE_MAX;
        }
        if ((q == SIZE_MAX || q >= s.n || (s.s[q] != '\'' && s.s[q] != '"')) && (c == '[' || c == '(' || c == ','))
            q = skip_space(s.s, s.n, i + 1);
        if (q != SIZE_MAX && q < s.n && (s.s[q] == '\'' || s.s[q] == '"')) {
            size_t a = q + 1, z = a;
            int kk;
            while (z < s.n) {
                unsigned d = cp_at(s.s, s.n, z, &kk);
                if (!(py_word(d) || (d < 0x80 && strchr("./~+-", (int)d)))) break;
                z += (size_t)kk;
            }
            int ok = 0;
            if (z < s.n && z > a && (s.s[z] == '\'' || s.s[z] == '"')) {
                for (size_t dot = a + 1; dot + 1 < z && !ok; dot++) { /* name.ext with ext [A-Za-z]\w{0,4} */
                    if (s.s[dot] != '.') continue;
                    char l = s.s[dot + 1];
                    if (!((l >= 'a' && l <= 'z') || (l >= 'A' && l <= 'Z'))) continue;
                    size_t rest = nchars(s.s + dot + 2, z - dot - 2);
                    int words = 1;
                    for (size_t x = dot + 2; x < z && words;) {
                        words = py_word(cp_at(s.s, z, x, &kk));
                        x += (size_t)kk;
                    }
                    ok = rest <= 4 && words;
                }
            }
            if (ok) {
                char *name = xstrndup(s.s + a, z - a);
                if (file_like(name)) set_add(out, absolute(name, cwd));
                free(name);
                i = z + 1;
                continue;
            }
        }
        i += (size_t)k;
    }
}

/* cmd_write_files: absolute paths a shell command writes, as far as patterns see. */
static void cmd_write_files(const Env *e, Str cmd, const char *cwd, PathSet *out) {
    int py = py_write(cmd);
    Segment *sg;
    int ns = segments(e, cmd, cwd, &sg);
    static const char *const git[] = {"checkout", "restore", "reset", "stash", "apply", "am", "merge", "rebase", "pull"};
    for (int s = 0; s < ns; s++) {
        Words *w = &sg[s].words;
        const char *d = sg[s].cwd;
        for (int i = 0; i < w->n; i++) { /* ^\d?>>?(.*)$, not 2> */
            const char *t = w->w[i];
            size_t j = 0;
            if (t[j] >= '0' && t[j] <= '9') j++;
            if (t[j] != '>' || !strncmp(t, "2>", 2)) continue;
            j++;
            if (t[j] == '>') j++;
            size_t len = strlen(t + j);
            if (len && t[j + len - 1] == '\n') len--;
            if (memchr(t + j, '\n', len)) continue;
            char *target = len ? xstrndup(t + j, len) : xstrdup(i + 1 < w->n ? w->w[i + 1] : "");
            if (file_like(target)) set_add(out, absolute(target, d));
            free(target);
        }
        const char *w0 = w->w[0];
        int writes = !strcmp(w0, "tee") || !strcmp(w0, "rm") || !strcmp(w0, "truncate") || !strcmp(w0, "patch") ||
                     !strcmp(w0, "mv");
        if (!strcmp(w0, "sed") || !strcmp(w0, "perl"))
            for (int i = 1; i < w->n && !writes; i++) {
                const char *a = w->w[i];
                if (a[0] != '-') continue;
                while (*a == '-') a++;
                writes = memchr(a, 'i', strnlen(a, 3)) != NULL;
            }
        if (!strcmp(w0, "git") && w->n > 1)
            for (size_t g = 0; g < sizeof git / sizeof *git; g++) writes |= !strcmp(w->w[1], git[g]);
        if (writes) {
            for (int i = 1; i < w->n; i++)
                if (file_like(w->w[i])) set_add(out, absolute(w->w[i], d));
        } else if (!strcmp(w0, "cp") && file_like(w->w[w->n - 1])) set_add(out, absolute(w->w[w->n - 1], d));
        if (py && !strncmp(w0, "python", 6)) {
            quoted_files(cmd, d, out);
            py = 0;
        }
    }
    segments_free(sg, ns);
}

/* cmd_read_files: the files a read-only command shows (cat, sed -n, head, ...). */
static void cmd_read_files(const Env *e, Str cmd, const char *cwd, PathSet *out) {
    static const char *const verbs[] = {"cat", "sed", "head", "tail", "nl", "grep", "rg", "less", "awk", "wc", "bat", "diff"};
    if (py_write(cmd)) return;
    Segment *sg;
    int ns = segments(e, cmd, cwd, &sg);
    for (int s = 0; s < ns; s++) {
        Words *w = &sg[s].words;
        int verb = 0, inplace = 0;
        for (size_t k = 0; k < sizeof verbs / sizeof *verbs; k++) verb |= !strcmp(w->w[0], verbs[k]);
        if (!strcmp(w->w[0], "sed"))
            for (int i = 1; i < w->n; i++) inplace |= !strncmp(w->w[i], "-i", 2);
        if (!verb || inplace) continue;
        for (int i = 1; i < w->n; i++)
            if (file_like(w->w[i]) && !strchr(w->w[i], '>')) set_add(out, absolute(w->w[i], sg[s].cwd));
    }
    segments_free(sg, ns);
}

/* cmd_paths: file-like words of a command (with an extension), repo-relative where they can be. */
static void cmd_paths(const Env *e, Str cmd, PathSet *out) {
    for (size_t i = 0; i < cmd.n;) {
        size_t j = i;
        int k;
        for (; j < cmd.n; j += (size_t)k) { /* [\s;|&<>()=,]+ separates */
            unsigned c = cp_at(cmd.s, cmd.n, j, &k);
            if (py_space(c) || (c < 0x80 && strchr(";|&<>()=,", (int)c))) break;
        }
        Str tok = {cmd.s + i, j - i};
        while (tok.n && strchr("'\"`", tok.s[0])) tok.s++, tok.n--;
        while (tok.n && strchr("'\"`", tok.s[tok.n - 1])) tok.n--;
        i = j;
        while (i < cmd.n) {
            unsigned c = cp_at(cmd.s, cmd.n, i, &k);
            if (!(py_space(c) || (c < 0x80 && strchr(";|&<>()=,", (int)c)))) break;
            i += (size_t)k;
        }
        if (!tok.n || starts(tok, "-") || starts(tok, "http") || starts(tok, "/dev/") || starts(tok, "$") ||
            memchr(tok.s, '*', tok.n) || memchr(tok.s, '@', tok.n))
            continue;
        char ext[16];
        ext_of(tok.s, tok.n, ext, sizeof ext);
        if (!known_ext(ext)) continue;
        size_t bs = tok.n;
        while (bs && tok.s[bs - 1] != '/') bs--;
        Str bn = {tok.s + bs, tok.n - bs};
        if (!bn.n) continue;
        unsigned c0 = cp_at(bn.s, bn.n, 0, &k);
        if (!(py_word(c0) || c0 == '+' || c0 == '-')) continue;
        if (!all_of(bn, ".+-", 1) || !all_of(tok, "./~+-", 1)) continue;
        char *t = xstrndup(tok.s, tok.n);
        if (t[0] == '/' || t[0] == '~') {
            char *x = expanduser(t);
            set_add(out, rel(e, x));
            free(x);
            free(t);
        } else set_add(out, t);
    }
}

/* ---- file changes across the project (for stale reads) ---- */

typedef struct {
    double t;
    int file; /* index in Build.files */
    const char *tu, *label;
    int by_cmd;
} Edit;

typedef struct {
    StrMap ix; /* abs path -> slot */
    struct { Edit *e; int n, cap; double *seen; int *seen_file; int nseen, capseen; } *v;
    int n;
} Changes;

static int changes_slot(Changes *c, const char *p) {
    int k = strmap_get(&c->ix, p);
    if (k < 0) {
        c->v = xrealloc(c->v, (size_t)(c->n + 1) * sizeof *c->v);
        memset(&c->v[c->n], 0, sizeof *c->v);
        strmap_put(&c->ix, p, k = c->n++);
    }
    return k;
}

typedef struct { /* one change a call made: its file as recorded (Edit) or resolved (Bash) */
    char *path;
    double t;
    const char *tu, *label; /* in CtxProject.L */
    int by_cmd;
} PEdit;

typedef struct {
    char *id, *name, *cwd;
    char *cmd;  /* Bash: the command */
    char *path; /* the others: file_path, else notebook_path (a string, not empty); NULL */
    int valid;  /* ed is up to date with this call and its result */
    PEdit *ed;
    int ned;
} Use;

/* One file's calls that may change files, record by record (project_changes):
 * Edit, Write, MultiEdit, NotebookEdit and Bash calls with their results'
 * times. Kept between scans, so a file that grew is read from where the last
 * scan stopped. */
typedef struct {
    StrMap uses, done;
    Use *u;
    int nu, capu;
    struct { double t; int err; } *dv;
    int nd;
} FileScan;

typedef struct {
    char *path;
    dev_t dev;
    ino_t ino;
    off_t size;
    long long mtime_ns;
    off_t off; /* bytes scanned: whole lines */
    FileScan fs;
    struct { char *path; double t; } *seen; /* files Claude Code noticed changing (edited_text_file) */
    int nseen, capseen;
} PFile;

struct CtxProject {
    pthread_mutex_t mu;
    char *root;
    Env env;
    Pool L; /* labels and tool_use ids of the edits */
    PFile *f;
    int n, cap;
    StrMap ix; /* path -> f */
};


static void use_clear(Use *u) {
    free(u->id), free(u->name), free(u->cwd), free(u->cmd), free(u->path);
    for (int i = 0; i < u->ned; i++) free(u->ed[i].path);
    free(u->ed);
    memset(u, 0, sizeof *u);
}

static void pfile_reset(PFile *pf) {
    for (int q = 0; q < pf->fs.nu; q++) use_clear(&pf->fs.u[q]);
    strmap_free(&pf->fs.uses), strmap_free(&pf->fs.done);
    free(pf->fs.u), free(pf->fs.dv);
    memset(&pf->fs, 0, sizeof pf->fs);
    for (int i = 0; i < pf->nseen; i++) free(pf->seen[i].path);
    free(pf->seen);
    pf->seen = NULL, pf->nseen = pf->capseen = 0;
    pf->off = pf->size = 0;
    pf->mtime_ns = 0;
}

static void fc_record(PFile *pf, yyjson_val *r, int type) {
    FileScan *fs = &pf->fs;
    yyjson_val *c = get(get(r, "message"), "content");
    size_t k, m;
    yyjson_val *x;
    if (type == T_ASSISTANT) {
        yyjson_arr_foreach(c, k, m, x) {
            const char *t = gets(x, "type"), *nm = gets(x, "name"), *id = gets(x, "id");
            if (!t || strcmp(t, "tool_use") || !nm || !id) continue;
            if (strcmp(nm, "Edit") && strcmp(nm, "Write") && strcmp(nm, "MultiEdit") && strcmp(nm, "NotebookEdit") &&
                strcmp(nm, "Bash"))
                continue;
            int q = strmap_get(&fs->uses, id);
            if (q < 0) {
                GROW(fs->u, fs->nu, fs->capu);
                memset(&fs->u[fs->nu], 0, sizeof *fs->u);
                strmap_put(&fs->uses, id, q = fs->nu++);
            }
            Use *u = &fs->u[q]; /* uses[id] = (block, cwd): the last one counts */
            use_clear(u);
            yyjson_val *inp = get(x, "input"), *p = get(inp, "file_path");
            if (!truthy(p)) p = get(inp, "notebook_path");
            *u = (Use){xstrdup(id), xstrdup(nm), dup_or_null(gets(r, "cwd")), dup_or_null(gets(inp, "command")),
                       truthy(p) && yyjson_is_str(p) ? xstrdup(yyjson_get_str(p)) : NULL, 0, NULL, 0};
            if (!strcmp(nm, "Bash") && !u->cmd) u->cmd = xstrdup("");
        }
    } else if (type == T_USER) {
        yyjson_arr_foreach(c, k, m, x) {
            const char *t = gets(x, "type"), *tu = gets(x, "tool_use_id");
            int q;
            if (!t || strcmp(t, "tool_result") || !tu || (q = strmap_get(&fs->uses, tu)) < 0) continue;
            int d = strmap_get(&fs->done, tu);
            if (d < 0) {
                fs->dv = xrealloc(fs->dv, (size_t)(fs->nd + 1) * sizeof *fs->dv);
                strmap_put(&fs->done, tu, d = fs->nd++);
            }
            fs->dv[d].t = epoch(gets(r, "timestamp"));
            fs->dv[d].err = truthy(get(x, "is_error"));
            fs->u[q].valid = 0;
        }
    } else if (type == T_ATTACH) {
        yyjson_val *at = get(r, "attachment");
        const char *t = gets(at, "type"), *fn = gets(at, "filename");
        if (t && !strcmp(t, "edited_text_file") && fn && *fn) {
            GROW(pf->seen, pf->nseen, pf->capseen);
            pf->seen[pf->nseen].path = xstrdup(fn);
            pf->seen[pf->nseen++].t = epoch(gets(r, "timestamp"));
        }
    }
}

/* A call's changes, once it and its result are known: nothing for a failed
 * call; a command's files as far as patterns see (cmd_write_files). */
static void use_edits(CtxProject *p, FileScan *fs, Use *u) {
    for (int i = 0; i < u->ned; i++) free(u->ed[i].path);
    u->ned = 0;
    u->valid = 1;
    int dk = strmap_get(&fs->done, u->id);
    if (dk < 0 || fs->dv[dk].err) return;
    double t = fs->dv[dk].t;
    const char *tu = pool_str(&p->L, u->id, strlen(u->id));
    PyBuf lab = {0};
    if (!strcmp(u->name, "Bash")) {
        Str cmd = S_(u->cmd);
        if (write_cmd(cmd)) {
            PathSet ps = {0};
            cmd_write_files(&p->env, cmd, u->cwd, &ps);
            u->ed = xrealloc(u->ed, ((size_t)ps.n + 1) * sizeof *u->ed);
            for (int i = 0; i < ps.n; i++) {
                lab.n = 0;
                pyb_puts(&lab, base_name(ps.p[i]));
                pyb_puts(&lab, ": Bash ");
                one_line(&lab, cmd, 60);
                u->ed[u->ned++] = (PEdit){xstrdup(ps.p[i]), t, tu, pool_str(&p->L, lab.s, lab.n), 1};
            }
            set_free(&ps);
        }
    } else if (u->path) {
        pyb_puts(&lab, u->name);
        pyb_puts(&lab, " ");
        char *r = rel(&p->env, u->path);
        pyb_puts(&lab, r);
        free(r);
        u->ed = xrealloc(u->ed, sizeof *u->ed);
        u->ed[u->ned++] = (PEdit){xstrdup(u->path), t, tu, pool_str(&p->L, lab.s, lab.n), 0};
    }
    pyb_free(&lab);
}

/* Can a line matter for changes? Transcripts are compact JSON: a tool call
 * names its tool as "name":"Bash", a result its call as "tool_use_id":"...". */
static int relevant_line(const FileScan *fs, const char *l, size_t n) {
    static const char *const names[] = {"\"name\":\"Edit\"", "\"name\":\"Write\"", "\"name\":\"MultiEdit\"",
                                        "\"name\":\"NotebookEdit\"", "\"name\":\"Bash\""};
    if (memmem(l, n, "edited_text_file", 16)) return 1;
    if (memmem(l, n, "\"tool_use\"", 10))
        for (size_t i = 0; i < sizeof names / sizeof *names; i++)
            if (memmem(l, n, names[i], strlen(names[i]))) return 1;
    for (const char *p = l, *e = l + n; (p = memmem(p, (size_t)(e - p), "\"tool_use_id\":\"", 15));) {
        p += 15;
        const char *q = memchr(p, '"', (size_t)(e - p));
        if (!q) break;
        char *id = xstrndup(p, (size_t)(q - p));
        int hit = strmap_get(&fs->uses, id) >= 0;
        free(id);
        if (hit) return 1;
        p = q;
    }
    return 0;
}

static void scan_line(PFile *pf, const char *ls, size_t n) {
    if (n >= 20 && !strncmp(ls, "{\"type\":\"api-request", 20)) return;
    if (!relevant_line(&pf->fs, ls, n)) return;
    yyjson_doc *doc = yyjson_read_opts((char *)ls, n, PYJ_READ, NULL, NULL);
    yyjson_val *r = yyjson_doc_get_root(doc);
    if (doc && yyjson_is_obj(r)) fc_record(pf, r, rec_type(r));
    yyjson_doc_free(doc);
}

/* Bring a file's scan up to date: nothing when unchanged, the appended bytes
 * when it only grew, else from the start. The file is mapped and searched for
 * the patterns a change needs; only lines that can hold one are parsed. A
 * last line without its newline is taken when it parses (the draft reads it
 * too), else left for the next scan. */
static void scan_file(PFile *pf, atomic_int *cancel) {
    int fd = open(pf->path, O_RDONLY);
    struct stat st;
    if (fd < 0 || fstat(fd, &st)) {
        if (fd >= 0) close(fd);
        pfile_reset(pf);
        return;
    }
    long long mt = (long long)st.st_mtim.tv_sec * 1000000000LL + st.st_mtim.tv_nsec;
    if (st.st_dev == pf->dev && st.st_ino == pf->ino && st.st_size == pf->size && mt == pf->mtime_ns) {
        close(fd);
        return;
    }
    if (st.st_dev != pf->dev || st.st_ino != pf->ino || st.st_size <= pf->size) pfile_reset(pf); /* replaced or rewritten */
    pf->dev = st.st_dev, pf->ino = st.st_ino;
    size_t size = (size_t)st.st_size;
    char *m = size ? mmap(NULL, size, PROT_READ, MAP_PRIVATE, fd, 0) : MAP_FAILED;
    close(fd);
    if (m == MAP_FAILED || !m) {
        pf->size = st.st_size, pf->mtime_ns = mt;
        return;
    }
    const char *end = m + size, *use = NULL, *edit = NULL, *pos = m + pf->off;
    const char *last = memrchr(pos, '\n', (size_t)(end - pos));
    const char *full = last ? last + 1 : pos; /* whole lines end here */
    for (const char *from = pos; pos < full;) {
        if (cancel && atomic_load(cancel)) break;
        /* the next line holding "tool_use (a call or a result) or edited_text_file */
        if (pos == from || (use && use < pos)) use = memmem(pos, (size_t)(full - pos), "\"tool_use", 9);
        if (pos == from || (edit && edit < pos)) edit = memmem(pos, (size_t)(full - pos), "edited_text_file", 16);
        const char *hit = !use ? edit : !edit ? use : use < edit ? use : edit;
        if (!hit) {
            pos = full;
            break;
        }
        const char *ls = hit;
        while (ls > pos && ls[-1] != '\n') ls--;
        const char *le = memchr(hit, '\n', (size_t)(full - hit));
        scan_line(pf, ls, (size_t)(le - ls));
        pos = le + 1;
    }
    if (pos == full && full < end) { /* a last line without its newline */
        yyjson_doc *doc = yyjson_read_opts((char *)full, (size_t)(end - full), PYJ_READ, NULL, NULL);
        if (doc) {
            scan_line(pf, full, (size_t)(end - full));
            pos = end;
        }
        yyjson_doc_free(doc);
    }
    pf->off = (off_t)(pos - m);
    pf->size = pos == end || pos == full ? st.st_size : pf->off; /* a cancelled scan resumes */
    pf->mtime_ns = pos == end || pos == full ? mt : 0;
    munmap(m, size);
}

CtxProject *ctx_project_new(void) {
    CtxProject *p = xcalloc(1, sizeof *p);
    pthread_mutex_init(&p->mu, NULL);
    return p;
}

static void project_clear(CtxProject *p) {
    for (int i = 0; i < p->n; i++) {
        pfile_reset(&p->f[i]);
        free(p->f[i].path);
    }
    free(p->f);
    p->f = NULL, p->n = p->cap = 0;
    strmap_free(&p->ix);
    pool_free(&p->L, 0);
    memset(&p->L, 0, sizeof p->L);
    free(p->root), free(p->env.home), free(p->env.config);
    p->root = NULL;
    memset(&p->env, 0, sizeof p->env);
}

void ctx_project_free(CtxProject *p) {
    if (!p) return;
    project_clear(p);
    pthread_mutex_destroy(&p->mu);
    free(p);
}

/* project_changes: every file's changes, brought up to date in the cache,
 * gathered by path; file = the index in files. Labels and ids are copied into
 * the load's pool while the project is locked: the project may change after. */
static void project_changes(CtxProject *p, const Env *env, char **files, int nfiles, Changes *ch, Pool *L,
                            atomic_int *cancel) {
    pthread_mutex_lock(&p->mu);
    if (!p->root || strcmp(p->root, env->root) || strcmp(p->env.home, env->home) || strcmp(p->env.config, env->config)) {
        project_clear(p); /* labels and command paths depend on these */
        p->root = xstrdup(env->root);
        p->env = (Env){p->root, xstrdup(env->home), xstrdup(env->config)};
    }
    for (int i = 0; i < nfiles && !(cancel && atomic_load(cancel)); i++) {
        int k = strmap_get(&p->ix, files[i]);
        if (k < 0) {
            GROW(p->f, p->n, p->cap);
            memset(&p->f[p->n], 0, sizeof *p->f);
            p->f[p->n].path = xstrdup(files[i]);
            strmap_put(&p->ix, files[i], k = p->n++);
        }
        PFile *pf = &p->f[k];
        scan_file(pf, cancel);
        for (int q = 0; q < pf->fs.nu; q++) {
            Use *u = &pf->fs.u[q];
            if (!u->valid) use_edits(p, &pf->fs, u);
            for (int e = 0; e < u->ned; e++) {
                int s = changes_slot(ch, u->ed[e].path);
                GROW(ch->v[s].e, ch->v[s].n, ch->v[s].cap);
                const char *tu = u->ed[e].tu, *lab = u->ed[e].label;
                ch->v[s].e[ch->v[s].n++] = (Edit){u->ed[e].t, i, pool_str(L, tu, strlen(tu)), pool_str(L, lab, strlen(lab)),
                                                  u->ed[e].by_cmd};
            }
        }
        for (int j = 0; j < pf->nseen; j++) {
            int s = changes_slot(ch, pf->seen[j].path);
            GROW(ch->v[s].seen, ch->v[s].nseen, ch->v[s].capseen);
            ch->v[s].seen_file = xrealloc(ch->v[s].seen_file, (size_t)ch->v[s].capseen * sizeof(int));
            ch->v[s].seen[ch->v[s].nseen] = pf->seen[j].t;
            ch->v[s].seen_file[ch->v[s].nseen++] = i;
        }
    }
    pthread_mutex_unlock(&p->mu);
}

static int cmp_stale(const void *x, const void *y) { /* sorted(ev): by time, kind, item, label */
    const CtxStale *a = x, *b = y;
    if (a->t != b->t) return a->t < b->t ? -1 : 1;
    if (a->kind != b->kind) return a->kind - b->kind;
    if (a->item != b->item) return a->item - b->item;
    return strcmp(a->label, b->label);
}

/* stale_events: per read (a Read result, or a read-only command's output
 * naming files), the changes to its file after the read while it stayed in
 * the context. */
static void stale_events(Build *b, Changes *ch, int own_file, const int *session, int nsession) {
    CtxAgent *a = b->a;
    Loader *ld = b->ld; /* its maps: every record of the agent's own file (scan_new) */
    double last_t = a->ncalls ? a->calls[a->ncalls - 1].t : 0;
    PyBuf lab = {0};
    for (int i = 0; i < b->nitems; i++) {
        CtxItem *it = &b->items[i];
        if (it->kind != CK_RESULT || !it->tool || (strcmp(it->tool, "Read") && strcmp(it->tool, "Bash")) ||
            it->action < 0 || it->err)
            continue;
        CtxItem *act = &b->items[it->action];
        const char *tu = act->tuid;
        int rq = tu ? strmap_get(&ld->rt_ix, tu) : -1, cq = tu ? strmap_get(&ld->cwd_ix, tu) : -1;
        double t0 = rq >= 0 ? ld->rt[rq] : 0;
        const char *cwd = cq >= 0 ? ld->cwd[cq] : NULL;
        yyjson_doc *doc = yyjson_read_opts(b->S.s[act->x], b->S.len[act->x], PYJ_READ, NULL, NULL);
        yyjson_val *inp = yyjson_doc_get_root(doc);
        if (!doc) continue;
        int bash = !strcmp(it->tool, "Bash");
        PathSet paths = {0};
        if (!bash) {
            if (gets(inp, "file_path")) set_add(&paths, xstrdup(gets(inp, "file_path")));
        } else cmd_read_files(&b->env, getstr(inp, "command"), cwd, &paths);
        double t1 = it->leave >= 0 ? a->calls[it->leave].t : last_t;
        if (!t0 || !paths.n || !a->ncalls) {
            set_free(&paths);
            yyjson_doc_free(doc);
            continue;
        }
        CtxStale *ev = NULL;
        int nev = 0, capev = 0;
        PathSet own_writes = {0};
        if (bash) cmd_write_files(&b->env, getstr(inp, "command"), cwd, &own_writes);
        for (int p = 0; p < paths.n; p++) {
            int k = strmap_get(&ch->ix, paths.p[p]);
            if (k < 0) continue;
            for (int e = 0; e < ch->v[k].n; e++) {
                Edit *ed = &ch->v[k].e[e];
                if (!(t0 < ed->t && ed->t <= t1)) continue;
                int in_session = 0;
                for (int s = 0; s < nsession; s++) in_session |= session[s] == ed->file;
                int kind = ed->file == own_file ? (ed->by_cmd ? CST_OWN_CMD : CST_OWN_EDIT)
                           : in_session ? CST_SESSION : CST_PROJECT;
                int item = -1;
                if (kind < CST_SESSION) item = strmap_get(&b->tool_use, ed->tu);
                GROW(ev, nev, capev);
                ev[nev++] = (CtxStale){round1(ed->t), kind, item, ed->label};
            }
            /* Claude Code noticed a change it did not make with Edit/Write; skipped when a command of the
             * project (this read's own included) wrote the file just before */
            for (int s = 0; s < ch->v[k].nseen; s++) {
                double t = ch->v[k].seen[s];
                if (!(t0 < t && t <= t1)) continue;
                int near = 0;
                for (int e = 0; e < ch->v[k].n && !near; e++) {
                    double et = ch->v[k].e[e].t;
                    near = t0 - 5 <= et && et <= t;
                }
                if (near || set_has(&own_writes, paths.p[p])) continue;
                lab.n = 0;
                char *r = rel(&b->env, paths.p[p]);
                pyb_puts(&lab, r);
                free(r);
                pyb_puts(&lab, " changed by the user or a program (Claude Code noticed)");
                GROW(ev, nev, capev);
                ev[nev++] = (CtxStale){round1(t), CST_NOTICED, -1, Lbuf(b, &lab)};
            }
        }
        set_free(&own_writes);
        set_free(&paths);
        yyjson_doc_free(doc);
        if (nev) {
            qsort(ev, (size_t)nev, sizeof *ev, cmp_stale);
            it->st = ev;
            it->nst = nev;
        } else free(ev);
    }
    pyb_free(&lab);
}

/* ---- why it decided (links_of) ---- */

static const char STOP_WORDS[] =
    "self this that with from into have none true false null return const static void char size_t uint "
    "int long else then elif while break continue print printf path file line lines text name type data value values list "
    "dict item items index json args kwargs main test tests make build echo grep head tail true false string number object "
    "command description file_path old_string new_string replace_all offset limit pattern output_mode content";

/* WORD.findall: [A-Za-z_][A-Za-z0-9_]{3,}(?:\.[a-z]{1,4})? */
static void find_words(Str s, StrMap *ix, int **ids, int *n, int *cap, int add_new, char ***names, int *nnames) {
    char w[256];
    for (size_t i = 0; i < s.n;) {
        char c = s.s[i];
        if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || c == '_')) { i++; continue; }
        size_t j = i + 1;
        while (j < s.n && ((s.s[j] >= 'A' && s.s[j] <= 'Z') || (s.s[j] >= 'a' && s.s[j] <= 'z') ||
                           (s.s[j] >= '0' && s.s[j] <= '9') || s.s[j] == '_'))
            j++;
        if (j - i < 4) { i++; continue; }
        if (j + 1 < s.n && s.s[j] == '.' && s.s[j + 1] >= 'a' && s.s[j + 1] <= 'z') {
            size_t e = j + 1;
            while (e < s.n && e < j + 5 && s.s[e] >= 'a' && s.s[e] <= 'z') e++;
            j = e;
        }
        char *word = j - i < sizeof w ? w : xmalloc(j - i + 1); /* any length, as the draft's */
        memcpy(word, s.s + i, j - i);
        word[j - i] = 0;
        int id = strmap_get(ix, word);
        if (id < 0 && add_new) {
            *names = xrealloc(*names, (size_t)(*nnames + 1) * sizeof **names);
            (*names)[*nnames] = xstrdup(word);
            strmap_put(ix, word, id = (*nnames)++);
        }
        if (word != w) free(word);
        if (id >= 0) {
            GROW(*ids, *n, *cap);
            (*ids)[(*n)++] = id;
        }
        i = j;
    }
}

static int cmp_int(const void *a, const void *b) { return (*(const int *)a > *(const int *)b) - (*(const int *)a < *(const int *)b); }

static int uniq(int *v, int n) { /* sort and dedupe */
    if (n < 2) return n;
    qsort(v, (size_t)n, sizeof *v, cmp_int);
    int k = 1;
    for (int i = 1; i < n; i++)
        if (v[i] != v[k - 1]) v[k++] = v[i];
    return k;
}

static int same_file(const char *a, const char *b) {
    size_t na = strlen(a), nb = strlen(b);
    return !strcmp(a, b) || (na > nb && a[na - nb - 1] == '/' && !strcmp(a + na - nb, b)) ||
           (nb > na && b[nb - na - 1] == '/' && !strcmp(b + nb - na, a));
}

typedef struct {
    double score;
    int item;
    int top[3], ntop;
} Scored;

static int cmp_scored(const void *x, const void *y) { /* sort(reverse=True) of (score, item, ...) */
    const Scored *a = x, *b = y;
    if (a->score != b->score) return a->score < b->score ? 1 : -1;
    return b->item - a->item;
}

typedef struct {
    const int *df;
    char **names;
} Rarity;

static int cmp_rare(const void *x, const void *y, void *ud) { /* a hit's words: rarest first, then by name */
    const Rarity *r = ud;
    int a = *(const int *)x, b = *(const int *)y;
    if (r->df[a] != r->df[b]) return r->df[a] - r->df[b];
    return strcmp(r->names[a], r->names[b]);
}

/* links_of: for each action, the items most likely behind it, from the
 * context of the call that made it. */
/* What links_of takes from an item, once (the loader keeps it): a result's
 * words (the first 200000 characters) and the files it is about (files_of);
 * an action's words (its string inputs) and the paths it acts on. Words get
 * ids in a table only links_of uses: ids never reach the output. */
static void link_cache(Build *b, LinkCache *c, int i, StrMap *wix, char ***names, int *nnames) {
    CtxItem *it = &b->items[i];
    PathSet ps = {0};
    int cap = 0;
    free(c->w), strv_free(c->p, c->np);
    memset(c, 0, sizeof *c);
    c->done = 1;
    c->act = it->action;
    if (it->kind == CK_RESULT) {
        int x = it->x;
        size_t lim = char_off(b->S.s[x], b->S.len[x], 200000);
        find_words((Str){b->S.s[x], lim}, wix, &c->w, &c->nw, &cap, 1, names, nnames);
        if (it->tool && !strcmp(it->tool, "Read")) {
            if (it->path) set_add(&ps, xstrdup(it->path));
        } else if (it->tool && (!strcmp(it->tool, "Bash") || !strcmp(it->tool, "Grep")) && it->action >= 0) {
            CtxItem *ac = &b->items[it->action];
            yyjson_doc *d = yyjson_read_opts(b->S.s[ac->x], b->S.len[ac->x], PYJ_READ, NULL, NULL);
            yyjson_val *inp = yyjson_doc_get_root(d);
            cmd_paths(&b->env, getstr(inp, "command"), &ps);
            if (gets(inp, "path")) set_add(&ps, rel(&b->env, gets(inp, "path")));
            yyjson_doc_free(d);
        }
    } else if (it->kind == CK_ACTION) {
        yyjson_doc *d = yyjson_read_opts(b->S.s[it->x], b->S.len[it->x], PYJ_READ, NULL, NULL);
        yyjson_val *inp = yyjson_doc_get_root(d);
        const char *pk[] = {"file_path", "notebook_path"};
        for (int k = 0; k < 2; k++)
            if (gets(inp, pk[k])) set_add(&ps, rel(&b->env, gets(inp, pk[k])));
        if (it->tool && !strcmp(it->tool, "Bash")) cmd_paths(&b->env, getstr(inp, "command"), &ps);
        PyBuf text = {0};
        if (yyjson_is_obj(inp)) {
            size_t k, m;
            yyjson_val *key, *v;
            int first = 1;
            yyjson_obj_foreach(inp, k, m, key, v) if (yyjson_is_str(v)) {
                if (!first) pyb_puts(&text, " ");
                first = 0;
                pyb_put(&text, yyjson_get_str(v), yyjson_get_len(v));
            }
        } else pyb_put(&text, b->S.s[it->x], b->S.len[it->x]);
        find_words((Str){text.s ? text.s : "", text.n}, wix, &c->w, &c->nw, &cap, 1, names, nnames);
        pyb_free(&text);
        yyjson_doc_free(d);
    }
    c->nw = uniq(c->w, c->nw);
    set_sort(&ps);
    c->p = ps.p, c->np = ps.n;
}

/* links_of: for each action, the items most likely behind it, from the
 * context of the call that made it. Every action's links are found again at
 * each load (word counts and thresholds change as results come), from
 * cached per-item words and paths, results by word (an inverted index)
 * marked by the call's context: the same links as scanning the context. */
static void links_of(Build *b) {
    CtxAgent *a = b->a;
    Loader *ld = b->ld;
    StrMap wix = ld->wix;
    char **names = ld->wnames;
    int nnames = ld->nwn;
    LinkCache *lc = xcalloc((size_t)b->nitems + 1, sizeof *lc);
    if (ld->nlc) memcpy(lc, ld->lc, (size_t)(ld->nlc < b->nitems ? ld->nlc : b->nitems) * sizeof *lc);
    for (int i = b->nitems; i < ld->nlc; i++) free(ld->lc[i].w), strv_free(ld->lc[i].p, ld->lc[i].np);
    free(ld->lc);
    int nres = 0;
    for (int i = 0; i < b->nitems; i++) {
        CtxItem *it = &b->items[i];
        if (it->kind == CK_RESULT) nres++;
        if ((!lc[i].done || (it->kind == CK_RESULT && lc[i].act != it->action)) &&
            (it->kind == CK_RESULT || (it->kind == CK_ACTION && it->made >= 0)))
            link_cache(b, &lc[i], i, &wix, &names, &nnames);
    }
    int *df = xcalloc((size_t)nnames + 1, sizeof *df);
    for (int i = 0; i < b->nitems; i++)
        if (b->items[i].kind == CK_RESULT)
            for (int k = 0; k < lc[i].nw; k++) df[lc[i].w[k]]++;
    double common = fmax(3, 0.15 * nres), rare = fmax(2, 0.03 * nres);
    /* stop words: STOP, the common ones, the pieces of the root, home and config paths */
    StrMap stop = {0};
    {
        char *sw = xstrdup(STOP_WORDS);
        for (char *t, *sv = NULL, *q = sw; (t = strtok_r(q, " ", &sv)); q = NULL) strmap_put(&stop, t, 1);
        free(sw);
        PyBuf pth = {0};
        pyb_puts(&pth, b->env.root);
        pyb_puts(&pth, "/");
        pyb_puts(&pth, b->env.home);
        pyb_puts(&pth, "/");
        pyb_puts(&pth, b->env.config);
        char *pp = pth.s;
        for (char *t, *sv = NULL, *q = pp; (t = strtok_r(q, "/-_.", &sv)); q = NULL) strmap_put(&stop, t, 1);
        pyb_free(&pth);
        static const char *extra[] = {"claude", "jsonl", "projects", "scratchpad", "tmp"};
        for (size_t i = 0; i < 5; i++) strmap_put(&stop, extra[i], 1);
        for (int w = 0; w < nnames; w++)
            if (df[w] > common) strmap_put(&stop, names[w], 1);
    }
    char *stopped = xmalloc((size_t)nnames + 1); /* per word id: a stop word, as itself or lowercased */
    for (int w = 0; w < nnames; w++) {
        char *low = xstrdup(names[w]);
        for (char *c = low; *c; c++) *c = (char)(*c >= 'A' && *c <= 'Z' ? *c + 32 : *c);
        stopped[w] = strmap_get(&stop, names[w]) >= 0 || strmap_get(&stop, low) >= 0;
        free(low);
    }
    /* results by word, in item order */
    int *pstart = xcalloc((size_t)nnames + 2, sizeof *pstart);
    for (int i = 0; i < b->nitems; i++)
        if (b->items[i].kind == CK_RESULT)
            for (int k = 0; k < lc[i].nw; k++) pstart[lc[i].w[k] + 1]++;
    for (int w = 0; w < nnames; w++) pstart[w + 1] += pstart[w];
    int *post = xmalloc(((size_t)pstart[nnames] + 1) * sizeof *post), *fill = mem_copy(pstart, (size_t)(nnames + 1) * sizeof(int));
    for (int i = 0; i < b->nitems; i++)
        if (b->items[i].kind == CK_RESULT)
            for (int k = 0; k < lc[i].nw; k++) post[fill[lc[i].w[k]]++] = i;
    free(fill);
    /* by_made: items each call's response made, in id order */
    int **made = xcalloc((size_t)a->ncalls + 1, sizeof *made), *nmade = xcalloc((size_t)a->ncalls + 1, sizeof *nmade),
        *capmade = xcalloc((size_t)a->ncalls + 1, sizeof *capmade);
    for (int i = 0; i < b->nitems; i++) {
        int m = b->items[i].made;
        if (m < 0 || m >= a->ncalls) continue;
        GROW(made[m], nmade[m], capmade[m]);
        made[m][nmade[m]++] = i;
    }
    int *inctx = xcalloc((size_t)b->nitems + 1, sizeof *inctx); /* times an item is in the call's context */
    char *linked = xcalloc((size_t)b->nitems + 1, 1);
    int **hits = xcalloc((size_t)b->nitems + 1, sizeof *hits), *nhits = xcalloc((size_t)b->nitems + 1, sizeof *nhits),
        *caphits = xcalloc((size_t)b->nitems + 1, sizeof *caphits), *touched = NULL, ntouched = 0, captouched = 0;
    PyBuf lab = {0};
    for (int mc = 0; mc < a->ncalls; mc++) {
        if (b->cancel && atomic_load(b->cancel)) break;
        int any = 0;
        for (int q = 0; q < nmade[mc] && !any; q++) any = b->items[made[mc][q]].kind == CK_ACTION;
        if (!any) continue;
        CtxCall *ctx = &a->calls[mc];
        for (int q = 0; q < ctx->norder; q++) inctx[ctx->order[q]]++;
        int prompt = -1; /* the prompt that started the turn: the last one in the context */
        for (int q = ctx->norder - 1; q >= 0 && prompt < 0; q--) {
            const char *s = b->items[ctx->order[q]].src;
            if (s && (!strcmp(s, "prompt") || !strcmp(s, "compaction summary"))) prompt = ctx->order[q];
        }
        for (int mq = 0; mq < nmade[mc]; mq++) {
            int ii = made[mc][mq];
            CtxItem *it = &b->items[ii];
            if (it->kind != CK_ACTION) continue;
            LinkCache *ac = &lc[ii];
            CtxLink *lk = NULL;
            int nlk = 0, caplk = 0;
            if (ac->np) { /* reads of the file it acts on: the latest two per file, four at most */
                int *per = xcalloc((size_t)ac->np, sizeof *per), tot = 0;
                for (int q = ctx->norder - 1; q >= 0; q--) {
                    int iid = ctx->order[q];
                    CtxItem *r = &b->items[iid];
                    if (r->kind != CK_RESULT || !r->tool ||
                        (strcmp(r->tool, "Read") && strcmp(r->tool, "Bash") && strcmp(r->tool, "Grep")))
                        continue;
                    LinkCache *rc = &lc[iid];
                    int hit = -1;
                    for (int p = 0; p < ac->np && hit < 0; p++)
                        for (int f = 0; f < rc->np && hit < 0; f++)
                            if (same_file(ac->p[p], rc->p[f])) hit = p;
                    if (hit >= 0 && ac->p[hit][0] && per[hit] < 2 && tot < 4) {
                        per[hit]++, tot++;
                        lab.n = 0;
                        pyb_puts(&lab, strcmp(r->tool, "Read") ? "saw " : "read of ");
                        pyb_puts(&lab, base_name(ac->p[hit]));
                        GROW(lk, nlk, caplk);
                        lk[nlk++] = (CtxLink){iid, Lbuf(b, &lab)};
                    }
                }
                free(per);
            }
            if (prompt >= 0) {
                GROW(lk, nlk, caplk);
                lk[nlk++] = (CtxLink){prompt, L_(b, "the prompt of this turn")};
            }
            for (int q = 0; q < nmade[mc] && made[mc][q] != ii; q++) { /* its text and thinking just before */
                CtxItem *t = &b->items[made[mc][q]];
                if (t->kind == CK_TEXT || t->kind == CK_THINKING) {
                    GROW(lk, nlk, caplk);
                    lk[nlk++] = (CtxLink){made[mc][q], L_(b, t->kind == CK_TEXT ? "its text just before" : "its thinking just before")};
                }
            }
            /* results that mention names in the action's input, rarest names first: each word's results in
             * the context, not linked yet; a result as many times as the context holds it */
            for (int q = 0; q < nlk; q++) linked[lk[q].item] = 1;
            for (int k = 0; k < ac->nw; k++) {
                int w = ac->w[k];
                if (stopped[w]) continue;
                for (int pi = pstart[w]; pi < pstart[w + 1]; pi++) {
                    int r = post[pi];
                    if (!inctx[r] || linked[r]) continue;
                    if (!nhits[r]) {
                        GROW(touched, ntouched, captouched);
                        touched[ntouched++] = r;
                    }
                    GROW(hits[r], nhits[r], caphits[r]);
                    hits[r][nhits[r]++] = w;
                }
            }
            Scored *sc = NULL;
            int nsc = 0, capsc = 0;
            for (int t = 0; t < ntouched; t++) {
                int r = touched[t], nh = nhits[r], any_rare = 0;
                for (int k = 0; k < nh; k++) any_rare |= df[hits[r][k]] <= rare;
                if (any_rare) {
                    qsort_r(hits[r], (size_t)nh, sizeof(int), cmp_rare, &(Rarity){df, names});
                    double score = 0;
                    for (int k = 0; k < nh; k++) score += 1.0 / df[hits[r][k]];
                    for (int n = 0; n < inctx[r]; n++) {
                        GROW(sc, nsc, capsc);
                        sc[nsc] = (Scored){score, r, {0}, nh < 3 ? nh : 3};
                        for (int k = 0; k < sc[nsc].ntop; k++) sc[nsc].top[k] = hits[r][k];
                        nsc++;
                    }
                }
                nhits[r] = 0;
            }
            ntouched = 0;
            for (int q = 0; q < nlk; q++) linked[lk[q].item] = 0;
            if (nsc > 1) qsort(sc, (size_t)nsc, sizeof *sc, cmp_scored);
            for (int q = 0; q < nsc && q < 3; q++) {
                if (sc[q].score < 0.34) continue;
                lab.n = 0;
                pyb_puts(&lab, "mentions ");
                for (int k = 0; k < sc[q].ntop; k++) {
                    if (k) pyb_puts(&lab, ", ");
                    pyb_puts(&lab, names[sc[q].top[k]]);
                }
                GROW(lk, nlk, caplk);
                lk[nlk++] = (CtxLink){sc[q].item, Lbuf(b, &lab)};
            }
            free(sc);
            it->lk = lk;
            it->nlk = nlk;
            if (!nlk) it->lk = xmalloc(1); /* an empty list, not none */
        }
        for (int q = 0; q < ctx->norder; q++) inctx[ctx->order[q]] = 0;
    }
    pyb_free(&lab);
    for (int c = 0; c < a->ncalls; c++) free(made[c]);
    for (int i = 0; i < b->nitems; i++) free(hits[i]);
    ld->wix = wix, ld->wnames = names, ld->nwn = nnames; /* kept */
    ld->lc = lc, ld->nlc = b->nitems;
    free(df), free(stopped), free(pstart), free(post), free(made), free(nmade), free(capmade), free(inctx), free(linked);
    free(hits), free(nhits), free(caphits), free(touched);
    strmap_free(&stop);
}

/* ---- what the page derives (prepare) ---- */

static void derive(CtxAgent *a) {
    int n = a->ncalls;
    for (int c = 0; c < n; c++) {
        CtxCall *C = &a->calls[c];
        double w = 0;
        for (int i = 0; i < C->norder; i++) {
            int it = C->order[i], dropped = 0;
            for (int d = 0; d < C->ndrop; d++) dropped |= C->drop[d] == it;
            if (!dropped) w += a->items[it].w;
        }
        C->wsum = w;
        if (C->has_u) {
            C->total = (double)(C->u[0] + C->u[1] + C->u[2] + C->u[3]);
            C->f = w ? C->total / w : 0;
        } else C->noresp = 1;
    }
    /* calls without a response (interrupted, failed): the nearest call's tokens per estimated unit */
    for (int c = 0; c < n; c++) {
        if (!a->calls[c].noresp) continue;
        double g = 0;
        for (int d = 1; d < n && !g; d++) {
            if (c - d >= 0 && !a->calls[c - d].noresp) g = a->calls[c - d].f;
            else if (c + d < n && !a->calls[c + d].noresp) g = a->calls[c + d].f;
        }
        a->calls[c].f = g ? g : 1.3;
        a->calls[c].total = a->calls[c].wsum * a->calls[c].f;
    }
    /* cache state of each item at each call (its middle falls in the read, written or new part of the
     * prompt) and what holding it has cost up to then */
    double *acc = xcalloc((size_t)a->nitems + 1, sizeof *acc);
    for (int c = 0; c < n; c++) {
        CtxCall *C = &a->calls[c];
        C->state = xmalloc((size_t)C->norder + 1);
        C->cum = xmalloc(((size_t)C->norder + 1) * sizeof *C->cum);
        const CtxPrice *p = ctx_price(C->model);
        double R = C->has_u ? (double)C->u[1] : 0, W = C->has_u ? (double)(C->u[2] + C->u[3]) : 0;
        double wrate = p && W ? ((double)C->u[2] * p->w5 + (double)C->u[3] * p->w1h) / W : 0;
        double x = 0;
        for (int i = 0; i < C->norder; i++) {
            int it = C->order[i];
            double t = ctx_tokens(a, c, i), mid = x + t / 2;
            x += t;
            C->state[i] = !C->has_u ? CS_NONE : mid <= R ? CS_READ : mid <= R + W ? CS_WRITTEN : CS_NEW;
            if (p && C->has_u)
                acc[it] += t * (C->state[i] == CS_READ ? p->read : C->state[i] == CS_WRITTEN ? wrate : p->in) / 1e6;
            C->cum[i] = (float)acc[it];
        }
    }
    free(acc);
}

double ctx_tokens(const CtxAgent *a, int c, int pos) {
    const CtxCall *C = &a->calls[c];
    int it = C->order[pos];
    for (int d = 0; d < C->ndrop; d++)
        if (C->drop[d] == it) return 0;
    return a->items[it].w * C->f;
}

int ctx_pos(const CtxAgent *a, int c, int item) {
    const CtxCall *C = &a->calls[c];
    for (int i = 0; i < C->norder; i++)
        if (C->order[i] == item) return i;
    return -1;
}

void ctx_positions(const CtxAgent *a, int c, int *by_item) {
    for (int i = 0; i < a->nitems; i++) by_item[i] = -1;
    const CtxCall *C = &a->calls[c];
    for (int i = C->norder - 1; i >= 0; i--) by_item[C->order[i]] = i; /* the first place, as ctx_pos */
}

/* As the page compares them: both times rounded to 0.1 s relative to t0. */
int ctx_stale_at(const CtxAgent *a, int item, int c, const int *by_item, int *known) {
    locale_t prev = c_numeric(); /* round1 prints */
    const CtxItem *it = &a->items[item];
    double T = round1(a->calls[c].t - a->t0);
    int n = 0, all = 1;
    for (int i = 0; i < it->nst; i++) {
        if (round1(it->st[i].t - a->t0) > T) continue;
        n++;
        int own = it->st[i].kind == CST_OWN_EDIT || it->st[i].kind == CST_OWN_CMD, at = -1;
        if (own && it->st[i].item >= 0) at = by_item ? by_item[it->st[i].item] : ctx_pos(a, c, it->st[i].item);
        all &= at >= 0;
    }
    uselocale(prev);
    if (known) *known = n && all;
    return n;
}

/* The page's rule, /^\s*(\d+)[\t→](.*)$/ per line: a line holding \r (or
 * U+2028, U+2029) doesn't match, as JavaScript's "." doesn't take them. */
static int code_line(char *l, int *num, char **code) {
    char *q = l;
    while (*q == ' ' || *q == '\t' || *q == '\v' || *q == '\f' || *q == '\r') q++;
    char *d = q;
    while (*d >= '0' && *d <= '9') d++;
    if (d == q) return 0;
    int arrow = !strncmp(d, "\xE2\x86\x92", 3);
    if (*d != '\t' && !arrow) return 0;
    char *c = d + (arrow ? 3 : 1);
    if (strchr(c, '\r') || strstr(c, "\xE2\x80\xA8") || strstr(c, "\xE2\x80\xA9")) return 0;
    *num = atoi(q);
    *code = c;
    return 1;
}

int ctx_code_lines(const CtxItem *it, char **buf, char ***lines, int **nums, const char **rest) {
    *buf = NULL, *lines = NULL, *nums = NULL, *rest = "";
    if (it->kind != CK_RESULT || !it->tool || strcmp(it->tool, "Read")) return 0;
    char *t = xstrndup(it->text, it->len);
    int nl = 1;
    for (char *p = t; *p; p++) nl += *p == '\n';
    char **ls = xmalloc((size_t)nl * sizeof *ls);
    int *ns = xmalloc((size_t)nl * sizeof *ns), n = 0;
    const char *rst = NULL;
    for (char *p = t; p;) { /* numbered lines up to the first that isn't */
        char *e = strchr(p, '\n');
        if (e) *e = 0;
        char *code;
        if (!code_line(p, &ns[n], &code)) {
            rst = it->text + (p - t); /* the rest, from here: one text */
            break;
        }
        ls[n++] = code;
        p = e ? e + 1 : NULL;
    }
    if (n < 1 || n * 2 < nl) {
        free(t), free(ls), free(ns);
        return 0;
    }
    *buf = t, *lines = ls, *nums = ns;
    *rest = rst ? rst : "";
    return n;
}

/* ---- finding agents ---- */

static char *projects_dir(void) {
    const char *cfg = getenv("CLAUDE_CONFIG_DIR");
    if (cfg) return path_join(cfg, "projects");
    char *h = expanduser("~/.claude/projects");
    return h;
}

static char *project_of(const char *root) { /* Claude Code's project dir: every non-alphanumeric char of the cwd -> '-' */
    char *pd = projects_dir(), *slug = xstrdup(root);
    for (char *p = slug; *p; p++)
        if (!((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') || (*p >= '0' && *p <= '9'))) *p = '-';
    char *d = path_join(pd, slug);
    free(pd), free(slug);
    return d;
}

static int list_dir(const char *dir, const char *suffix, char ***out) { /* sorted names */
    DIR *d = opendir(dir);
    struct dirent *de;
    char **v = NULL;
    int n = 0;
    while (d && (de = readdir(d))) {
        if (de->d_name[0] == '.' || (suffix && !ends(de->d_name, suffix))) continue;
        v = xrealloc(v, (size_t)(n + 1) * sizeof *v);
        v[n++] = xstrdup(de->d_name);
    }
    if (d) closedir(d);
    if (n > 1) qsort(v, (size_t)n, sizeof *v, cmp_strp);
    *out = v;
    return n;
}

static void free_list(char **v, int n) {
    for (int i = 0; i < n; i++) free(v[i]);
    free(v);
}

char *ctx_find(const char *root, const char *agent, char **err) {
    struct stat st;
    char buf[512];
    if (strchr(agent, '/') || ends(agent, ".jsonl")) {
        if (!stat(agent, &st) && S_ISREG(st.st_mode)) return xstrdup(agent);
        snprintf(buf, sizeof buf, "%s: no such transcript", agent);
        *err = xstrdup(buf);
        return NULL;
    }
    const char *id = !strncmp(agent, "agent-", 6) ? agent + 6 : agent;
    size_t idn = strlen(id);
    char *dir = project_of(root), **names, *found = NULL;
    int n = list_dir(dir, NULL, &names), hits = 0, exact = 0;
    for (int i = 0; i < n; i++) {
        char *p = path_join(dir, names[i]);
        if (ends(names[i], ".jsonl")) {
            int ex = strlen(names[i]) == idn + 6;
            if (!strncmp(names[i], id, idn) && (!exact || ex)) {
                if (ex && !exact) hits = 0, free(found), found = NULL;
                exact |= ex;
                hits++;
                free(found);
                found = xstrdup(p);
            }
        } else if (!stat(p, &st) && S_ISDIR(st.st_mode)) {
            char *sub = path_join(p, "subagents"), **subs;
            int ns = list_dir(sub, ".jsonl", &subs);
            for (int k = 0; k < ns; k++) {
                if (strncmp(subs[k], "agent-", 6) || strncmp(subs[k] + 6, id, idn)) continue;
                int ex = strlen(subs[k]) == idn + 12;
                if (exact && !ex) continue;
                if (ex && !exact) hits = 0;
                exact |= ex;
                hits++;
                free(found);
                found = path_join(sub, subs[k]);
            }
            free_list(subs, ns);
            free(sub);
        }
        free(p);
    }
    free_list(names, n);
    if (hits != 1) {
        snprintf(buf, sizeof buf, "%s: %s in %s", agent, hits ? "more than one agent matches" : "no such session or subagent", dir);
        *err = xstrdup(buf);
        free(found);
        found = NULL;
    }
    free(dir);
    return found;
}

/* The project dir, session id and main transcript of a transcript path. */
static void locate(const char *path, char **projdir, char **session, char **main, int *is_sub) {
    const char *sub = strstr(path, "/subagents/");
    for (const char *p; sub && (p = strstr(sub + 1, "/subagents/"));) sub = p;
    *is_sub = sub && !strncmp(base_name(path), "agent-", 6);
    if (*is_sub) {
        const char *sid = sub;
        while (sid > path && sid[-1] != '/') sid--;
        *session = xstrndup(sid, (size_t)(sub - sid));
        *projdir = sid > path ? xstrndup(path, (size_t)(sid - path - 1)) : xstrdup(".");
    } else {
        const char *b = base_name(path);
        *session = xstrndup(b, strlen(b) - (ends(b, ".jsonl") ? 6 : 0));
        *projdir = b > path ? xstrndup(path, (size_t)(b - path - 1)) : xstrdup(".");
    }
    PyBuf m = {0};
    pyb_puts(&m, *projdir);
    pyb_puts(&m, "/");
    pyb_puts(&m, *session);
    pyb_puts(&m, ".jsonl");
    *main = m.s;
}

static char *meta_path(const char *jsonl) { /* agent-x.jsonl -> agent-x.meta.json */
    size_t n = strlen(jsonl) - (ends(jsonl, ".jsonl") ? 6 : 0);
    char *m = xmalloc(n + 11);
    memcpy(m, jsonl, n);
    strcpy(m + n, ".meta.json");
    return m;
}

int ctx_session_agents(const char *path, CtxRef **out) {
    char *pd, *sid, *main;
    int is_sub;
    locate(path, &pd, &sid, &main, &is_sub);
    CtxRef *v = xcalloc(1, sizeof *v);
    int n = 1;
    v[0] = (CtxRef){main, xstrdup(sid), xstrdup("main"), NULL, NULL};
    char *dir = xmalloc(strlen(pd) + strlen(sid) + 16);
    sprintf(dir, "%s/%s/subagents", pd, sid);
    char **subs;
    int ns = list_dir(dir, ".jsonl", &subs);
    for (int k = 0; k < ns; k++) {
        if (strncmp(subs[k], "agent-", 6)) continue;
        v = xrealloc(v, (size_t)(n + 1) * sizeof *v);
        char *p = path_join(dir, subs[k]), *meta = meta_path(p);
        yyjson_doc *doc = yyjson_read_file(meta, 0, NULL, NULL);
        yyjson_val *root = yyjson_doc_get_root(doc);
        const char *t = gets(root, "agentType"), *d = gets(root, "description"), *tu = gets(root, "toolUseId");
        v[n++] = (CtxRef){p, xstrndup(subs[k] + 6, strlen(subs[k]) - 12), xstrdup(t && *t ? t : "subagent"),
                          d ? xstrdup(d) : NULL, tu ? xstrdup(tu) : NULL};
        yyjson_doc_free(doc);
        free(meta);
    }
    free_list(subs, ns);
    free(dir), free(pd), free(sid);
    *out = v;
    return n;
}

void ctx_refs_free(CtxRef *r, int n) {
    for (int i = 0; i < n; i++) free(r[i].path), free(r[i].id), free(r[i].name), free(r[i].desc), free(r[i].tu);
    free(r);
}

static int cmp_mtime(const void *x, const void *y) { /* by mtime, then name: the same order every time */
    const struct { char *p; double mt; } *a = x, *b = y;
    return a->mt != b->mt ? (a->mt > b->mt) - (a->mt < b->mt) : strcmp(a->p, b->p);
}

/* The project's transcripts as build_ctx.py lists them: main sessions by
 * mtime, then every subagent by path. */
static int project_files(const char *pd, char ***out) {
    char **names;
    int n = list_dir(pd, NULL, &names), nm = 0;
    struct { char *p; double mt; } *mains = xcalloc((size_t)n + 1, sizeof *mains);
    char **subs = NULL;
    int nsub = 0;
    struct stat st;
    for (int i = 0; i < n; i++) {
        char *p = path_join(pd, names[i]);
        if (ends(names[i], ".jsonl") && !stat(p, &st)) {
            mains[nm].p = p;
            mains[nm++].mt = st.st_mtim.tv_sec + st.st_mtim.tv_nsec / 1e9;
            continue;
        }
        if (!stat(p, &st) && S_ISDIR(st.st_mode)) {
            char *sd = path_join(p, "subagents"), **ss;
            int k = list_dir(sd, ".jsonl", &ss);
            for (int j = 0; j < k; j++) {
                subs = xrealloc(subs, (size_t)(nsub + 1) * sizeof *subs);
                subs[nsub++] = path_join(sd, ss[j]);
            }
            free_list(ss, k);
            free(sd);
        }
        free(p);
    }
    free_list(names, n);
    if (nm > 1) qsort(mains, (size_t)nm, sizeof *mains, cmp_mtime);
    if (nsub > 1) qsort(subs, (size_t)nsub, sizeof *subs, cmp_strp);
    char **v = xmalloc((size_t)(nm + nsub + 1) * sizeof *v);
    for (int i = 0; i < nm; i++) v[i] = mains[i].p;
    for (int i = 0; i < nsub; i++) v[nm + i] = subs[i];
    free(mains), free(subs);
    *out = v;
    return nm + nsub;
}

/* title_of_file, resumable: a subagent's session title, from its main
 * transcript, reading only what was appended since the last scan (whole
 * lines; a last line without its newline counts when it parses, as in a full
 * read, but is read again next time). */
static char *title_scan(TitleScan *ts, const char *path) {
    struct stat st;
    if (!ts->path || strcmp(ts->path, path) || stat(path, &st) || st.st_dev != ts->dev || st.st_ino != ts->ino ||
        st.st_size < ts->off) {
        free(ts->path), free(ts->ai), free(ts->first);
        *ts = (TitleScan){xstrdup(path), 0, 0, 0, NULL, NULL};
        if (!stat(path, &st)) ts->dev = st.st_dev, ts->ino = st.st_ino;
    }
    FILE *f = fopen(path, "r");
    char *tail_ai = NULL, *tail_first = NULL;
    if (f && !fseeko(f, ts->off, SEEK_SET)) {
        char *line = NULL;
        size_t cap = 0;
        ssize_t n;
        while ((n = getline(&line, &cap, f)) > 0) {
            int whole = line[n - 1] == '\n';
            if (whole) ts->off += n;
            int title = memmem(line, (size_t)n, "\"ai-title\"", 10) != NULL;
            if (!title && (ts->first || !memmem(line, (size_t)n, "\"user\"", 6))) continue;
            yyjson_doc *doc = yyjson_read_opts(line, (size_t)n, PYJ_READ, NULL, NULL);
            yyjson_val *r = yyjson_doc_get_root(doc), *c = get(get(r, "message"), "content");
            int t = rec_type(r);
            char **ai = whole ? &ts->ai : &tail_ai, **first = whole ? &ts->first : &tail_first;
            if (t == T_TITLE && truthy(get(r, "aiTitle")) && gets(r, "aiTitle")) {
                free(*ai);
                *ai = xstrdup(gets(r, "aiTitle"));
            } else if (t == T_USER && yyjson_is_str(c) && !ts->first && !*first) {
                PyBuf o = {0};
                one_line(&o, (Str){yyjson_get_str(c), yyjson_get_len(c)}, 70);
                *first = o.s ? o.s : xstrdup("");
            }
            yyjson_doc_free(doc);
        }
        free(line);
    }
    if (f) fclose(f);
    const char *t = tail_ai ? tail_ai : ts->ai ? ts->ai : ts->first ? ts->first : tail_first ? tail_first : "";
    char *o = xstrdup(t);
    free(tail_ai), free(tail_first);
    return o;
}

/* ---- loading ---- */

static CtxAgent *load(const char *root, const char *path, CtxProject *proj, atomic_int *cancel, char **err,
                      const CtxAgent *prev, const char **refused);

CtxAgent *ctx_load(const char *root, const char *path, CtxProject *proj, atomic_int *cancel, char **err) {
    locale_t prev = c_numeric();
    CtxAgent *a = load(root, path, proj, cancel, err, NULL, NULL);
    uselocale(prev);
    return a;
}

typedef struct {
    Pool L;         /* labels and other strings the items point into */
    uint64_t *keys; /* item keys, sorted */
    int *ix;        /* their items */
    Loader *ld;     /* what a reload resumes from; NULL: not resumable (a full load next time) */
} Priv;

static int cmp_key_ix(const void *x, const void *y, void *ud) {
    const uint64_t *k = ud;
    uint64_t a = k[*(const int *)x], b = k[*(const int *)y];
    return (a > b) - (a < b);
}

int ctx_item_by_key(const CtxAgent *a, uint64_t key) {
    const Priv *p = a->priv;
    int lo = 0, hi = a->nitems - 1;
    while (lo <= hi) {
        int mid = (lo + hi) / 2;
        if (p->keys[mid] == key) return p->ix[mid];
        if (p->keys[mid] < key) lo = mid + 1;
        else hi = mid - 1;
    }
    return -1;
}

/* The previous load's agent, as the build it came from: its items, texts,
 * instances and calls copied (every string it points to taken into this
 * build's pools, so the new agent owns its own), what was derived from them
 * reset (post, stale reads, links and the hand-over recompute it), and its
 * loader copied. prev stays as it was. */
static void restore(Build *b, const CtxAgent *prev) {
    CtxAgent *a = b->a;
    const Priv *pp = prev->priv;
    Loader *ld = b->ld = loader_copy(pp->ld);
    b->resuming = 1;
#define RE(s) ((s) ? L_(b, (s)) : NULL)
    for (int i = 0; i < prev->nstr; i++) pool_add(&b->S, prev->str[i], prev->slen[i]); /* the same indices */
    b->items = xmalloc(((size_t)prev->nitems + 1) * sizeof *b->items);
    b->nitems = b->capitems = prev->nitems;
    for (int i = 0; i < prev->nitems; i++) {
        const CtxItem *o = &prev->items[i];
        CtxItem *d = &b->items[i];
        *d = *o;
        d->src = RE(o->src), d->tool = RE(o->tool), d->tuid = RE(o->tuid), d->path = RE(o->path);
        d->text = NULL, d->len = 0, d->lang = NULL, d->line0 = d->line1 = 0;
        d->cmd = d->old_str = d->new_str = d->content = d->desc = NULL;
        d->tx = o->ntx ? mem_copy(o->tx, (size_t)o->ntx * sizeof *o->tx) : NULL;
        d->names = NULL;
        if (o->ntx) {
            d->names = xmalloc((size_t)o->ntx * sizeof *d->names);
            for (int k = 0; k < o->ntx; k++) d->names[k] = RE(o->names[k]);
        }
        d->st = NULL, d->nst = 0, d->lk = NULL, d->nlk = 0;
        d->enter = d->leave = d->action = d->result = -1;
        kset_take(b, d->key);
        if (d->tuid && d->kind == CK_ACTION) strmap_put(&b->tool_use, d->tuid, i);
        else if (d->tuid && d->kind == CK_RESULT) {
            int k = strmap_get(&b->result_ix, d->tuid);
            if (k < 0) {
                GROW(b->result_of, b->nresult, b->capresult);
                b->result_of[b->nresult].tu = d->tuid;
                strmap_put(&b->result_ix, d->tuid, k = b->nresult++);
            }
            b->result_of[k].item = i;
        }
    }
    b->ninst = b->capinst = prev->ninst;
    b->inst = xmalloc(((size_t)b->ninst + 1) * sizeof *b->inst);
    b->nkeys = b->capkeys = ld->nkeys;
    b->keys = mem_copy(ld->keys, (size_t)ld->nkeys * sizeof *ld->keys);
    b->kitems = xmalloc(((size_t)ld->nkeys + 1) * sizeof *b->kitems);
    for (int i = 0, k = 0; i < prev->ninst; i++) {
        b->inst[i] = (Inst){ld->ih[i], k, prev->inst[i].n};
        if (prev->inst[i].n) memcpy(b->kitems + k, prev->inst[i].items, (size_t)prev->inst[i].n * sizeof(int));
        k += prev->inst[i].n;
    }
    a->ncalls = prev->ncalls;
    a->calls = xmalloc(((size_t)a->ncalls + 1) * sizeof *a->calls);
    b->cb = xcalloc((size_t)a->ncalls + 1, sizeof *b->cb);
    for (int c = 0; c < a->ncalls; c++) {
        const CtxCall *o = &prev->calls[c];
        CtxCall *d = &a->calls[c];
        memset(d, 0, sizeof *d);
        d->t = o->t, d->base = o->base, d->keep = o->keep, d->comp = o->comp;
        d->id = RE(o->id), d->query = RE(o->query);
        d->nshape = o->nshape;
        d->shape = mem_copy(o->shape, (size_t)o->nshape * sizeof *o->shape);
        b->shapes = xrealloc(b->shapes, (size_t)(b->nshapes + 1) * sizeof *b->shapes);
        b->shapes[b->nshapes++] = d->shape; /* freed once the calls have their copies */
        CallB *cb = &b->cb[c];
        cb->sh = RE(ld->csh[c]), cb->pmodel = RE(ld->cpm[c]), cb->kept = ld->ckept[c];
        /* its messages: its base's first kept, what lay between them and the public tail, the tail */
        const CallB *bb = o->base >= 0 ? &b->cb[o->base] : NULL;
        int kept = bb ? cb->kept : 0;
        cb->ninst = kept + ld->ncextra[c] + o->ntail;
        cb->inst = xmalloc(((size_t)cb->ninst + 1) * sizeof *cb->inst);
        if (kept) memcpy(cb->inst, bb->inst, (size_t)kept * sizeof(int));
        if (ld->ncextra[c]) memcpy(cb->inst + kept, ld->cextra[c], (size_t)ld->ncextra[c] * sizeof(int));
        if (o->ntail) memcpy(cb->inst + kept + ld->ncextra[c], o->tail, (size_t)o->ntail * sizeof(int));
    }
#undef RE
    a->missing = prev->missing, a->found_elsewhere = prev->found_elsewhere, a->broken = prev->broken;
    /* the responses before the window: metadata only, first */
    Store *own = &b->own;
    own->resps = xmalloc(((size_t)ld->nold + 1) * sizeof *own->resps);
    for (int k = 0; k < ld->nold; k++) {
        resp_copy(&own->resps[k], &ld->old[k]);
        own->resps[k].old = 1;
        strmap_put(&own->resp_ix, own->resps[k].mid, k);
    }
    own->nresps = ld->nold;
}

/* A reload's records: the window again, then what was appended since. The
 * file must be the one the window came from: the same inode, not shorter,
 * the same bytes just before the end taken. */
static void read_new(Build *b, const char *path) {
    Loader *ld = b->ld;
    int fd = open(path, O_RDONLY);
    struct stat st;
    if (fd < 0 || fstat(fd, &st)) {
        if (fd >= 0) close(fd);
        b->fail = "the transcript can't be read";
        return;
    }
    unsigned char chk[256];
    if (st.st_dev != ld->dev || st.st_ino != ld->ino) b->fail = "the transcript was replaced";
    else if (st.st_size < ld->off) b->fail = "the transcript shrank";
    else if (ld->nchk && (pread(fd, chk, (size_t)ld->nchk, ld->off - ld->nchk) != ld->nchk ||
                          memcmp(chk, ld->chk, (size_t)ld->nchk)))
        b->fail = "the transcript was rewritten";
    if (b->fail) {
        close(fd);
        return;
    }
    size_t nnew = (size_t)(st.st_size - ld->off), got = 0;
    char *buf = xmalloc(ld->nwin + nnew + 1);
    if (ld->nwin) memcpy(buf, ld->win, ld->nwin);
    while (got < nnew) {
        ssize_t r = pread(fd, buf + ld->nwin + got, nnew - got, ld->off + (off_t)got);
        if (r <= 0) break;
        got += (size_t)r;
    }
    close(fd);
    size_t n = ld->nwin + got, pos = 0;
    long abs = ld->wabs;
    for (long k = 0; pos < n; k++) {
        if (b->cancel && !(k & 1023) && atomic_load(b->cancel)) break;
        const char *nl = memchr(buf + pos, '\n', n - pos);
        if (!nl) {
            yyjson_doc *d = yyjson_read_opts(buf + pos, n - pos, PYJ_READ, NULL, NULL);
            if (d) b->fail = "a last line without its newline"; /* a full load counts it */
            yyjson_doc_free(d);
            break;
        }
        size_t e = (size_t)(nl - buf) + 1;
        take_line(&b->own.recs, buf + pos, e - pos, &abs, ld->woff + (off_t)pos);
        pos = e;
    }
    if (abs < ld->nabs && !b->fail) b->fail = "the window's records are not all there";
    b->raw = buf;
    b->raw_off = ld->woff;
    b->new_off = ld->woff + (off_t)pos;
    b->new_abs = abs;
    b->dev = st.st_dev, b->ino = st.st_ino;
}

/* What the next reload resumes from: this build's instances and calls, the
 * window from just after the last response before the last request, what
 * moved out of it (store entries into old_only, responses into metadata). */
static void save_loader(Build *b, const char *path) {
    CtxAgent *a = b->a;
    Loader *ld = b->ld;
    free(ld->root), free(ld->home), free(ld->config);
    ld->root = xstrdup(b->env.root), ld->home = xstrdup(b->env.home), ld->config = xstrdup(b->env.config);
    ld->ih = xrealloc(ld->ih, ((size_t)b->ninst + 1) * sizeof *ld->ih);
    for (int i = ld->nih; i < b->ninst; i++) ld->ih[i] = xstrdup(b->inst[i].h);
    ld->nih = b->ninst;
    free(ld->keys);
    ld->keys = mem_copy(b->keys, (size_t)b->nkeys * sizeof *b->keys);
    ld->nkeys = b->nkeys;
    strv_free(ld->csh, ld->ncalls), strv_free(ld->cpm, ld->ncalls);
    for (int i = 0; i < ld->ncalls; i++) free(ld->cextra[i]);
    free(ld->ckept), free(ld->cextra), free(ld->ncextra);
    ld->ncalls = a->ncalls;
    ld->csh = xmalloc(((size_t)a->ncalls + 1) * sizeof *ld->csh);
    ld->cpm = xmalloc(((size_t)a->ncalls + 1) * sizeof *ld->cpm);
    ld->ckept = xmalloc(((size_t)a->ncalls + 1) * sizeof *ld->ckept);
    ld->cextra = xmalloc(((size_t)a->ncalls + 1) * sizeof *ld->cextra);
    ld->ncextra = xmalloc(((size_t)a->ncalls + 1) * sizeof *ld->ncextra);
    for (int c = 0; c < a->ncalls; c++) {
        CallB *cb = &b->cb[c];
        CtxCall *C = &a->calls[c];
        ld->csh[c] = dup_or_null(cb->sh), ld->cpm[c] = dup_or_null(cb->pmodel), ld->ckept[c] = cb->kept;
        int from = C->base >= 0 ? (C->keep < cb->ninst ? C->keep : cb->ninst) : 0, kept = C->base >= 0 ? cb->kept : 0;
        if (from < kept) from = kept;
        ld->ncextra[c] = from - kept;
        ld->cextra[c] = mem_copy(cb->inst + kept, (size_t)(from - kept) * sizeof(int));
    }
    /* the window: from just after the last response before the last request */
    Recs *R = &b->own.recs;
    long W = ld->wabs;
    off_t woff = ld->woff;
    for (int i = R->n - 1; i >= 0; i--)
        if (R->r[i].type == T_ASSISTANT && R->r[i].abs < ld->last_req) {
            W = R->r[i].abs + 1;
            break;
        }
    for (int i = 0; i < R->n; i++)
        if (R->r[i].abs == W) {
            woff = R->r[i].off;
            break;
        }
    if (W >= b->new_abs) woff = b->new_off; /* nothing after it */
    for (size_t i = 0; i < b->own.map.cap; i++) {
        if (b->own.map.v[i] < 0) continue;
        const Msg *m = &b->own.msgs[b->own.map.v[i]];
        if (m->src >= 0 && m->src < W) dset_add(&ld->old_only, b->own.map.k[i]);
    }
    for (int k = 0; k < b->own.nresps; k++) {
        Resp *rr = &b->own.resps[k];
        if (rr->old || rr->first >= W) continue;
        ld->old = xrealloc(ld->old, (size_t)(ld->nold + 1) * sizeof *ld->old);
        resp_copy(&ld->old[ld->nold], rr);
        ld->old[ld->nold++].old = 1;
    }
    free(ld->win);
    ld->nwin = (size_t)(b->new_off - woff);
    ld->win = xmalloc(ld->nwin + 1);
    if (b->raw) memcpy(ld->win, b->raw + (woff - b->raw_off), ld->nwin);
    int fd = open(path, O_RDONLY);
    if (!b->raw && (fd < 0 || pread(fd, ld->win, ld->nwin, woff) != (ssize_t)ld->nwin)) ld->ok = 0;
    ld->nchk = b->new_off < (off_t)sizeof ld->chk ? (int)b->new_off : (int)sizeof ld->chk;
    if (fd < 0 || pread(fd, ld->chk, (size_t)ld->nchk, b->new_off - ld->nchk) != ld->nchk) ld->ok = 0;
    if (fd >= 0) close(fd);
    ld->woff = woff, ld->wabs = W, ld->nabs = b->new_abs, ld->off = b->new_off;
    ld->dev = b->dev, ld->ino = b->ino;
}

/* A load (prev NULL) or a reload resuming from prev. A reload that can't
 * apply what was appended returns NULL with *refused set to why (no *err):
 * the caller loads in full. */
static CtxAgent *load(const char *root, const char *path, CtxProject *proj, atomic_int *cancel, char **err,
                      const CtxAgent *prev, const char **refused) {
    *err = NULL;
    static const char *const phase[] = {"changes", "read", "store", "calls", "stale", "links", "derive"};
    double t[8];
    int np = 0;
    t[np++] = now_wall();
    FILE *probe = fopen(path, "r");
    if (!probe) {
        char m[512];
        snprintf(m, sizeof m, "%s: cannot read", path);
        *err = xstrdup(m);
        return NULL;
    }
    fclose(probe);
    Build B = {0}, *b = &B;
    CtxAgent *a = b->a = xcalloc(1, sizeof *a);
    b->cancel = cancel;
    a->path = xstrdup(path);
    a->root = xstrdup(root ? root : "");
    b->env.root = a->root;
    b->env.home = expanduser("~");
    {
        const char *cfg = getenv("CLAUDE_CONFIG_DIR");
        b->env.config = cfg ? xstrdup(cfg) : expanduser("~/.claude");
    }
    b->own.path = xstrdup(path);
    char *pd, *main_path;
    int is_sub;
    locate(path, &pd, &a->session, &main_path, &is_sub);
    b->nfiles = project_files(pd, &b->files);
    b->others = xcalloc((size_t)b->nfiles + 1, sizeof *b->others);
    /* every agent of the project: the changes that make reads stale. First, so
     * the transcripts it maps aren't resident beside the own file's records */
    Changes ch = {0};
    int own_file = -1, *session = xcalloc((size_t)b->nfiles + 1, sizeof *session), nsession = 0;
    char *sub_prefix = xmalloc(strlen(pd) + strlen(a->session) + 16);
    sprintf(sub_prefix, "%s/%s/subagents/", pd, a->session);
    for (int i = 0; i < b->nfiles; i++) {
        if (!strcmp(b->files[i], path)) own_file = i;
        if (!strcmp(b->files[i], main_path) || !strncmp(b->files[i], sub_prefix, strlen(sub_prefix)))
            session[nsession++] = i;
    }
    if (own_file < 0) { /* a transcript outside its project dir's listing */
        own_file = b->nfiles;
        b->files[b->nfiles++] = xstrdup(path);
        session[nsession++] = own_file;
    }
    CtxProject *pr = proj ? proj : ctx_project_new();
    project_changes(pr, &b->env, b->files, b->nfiles, &ch, &b->L, cancel);
    if (!proj) ctx_project_free(pr);
    t[np++] = now_wall();
    if (prev) {
        restore(b, prev);
        Loader *pl = b->ld;
        if (strcmp(pl->root, b->env.root) || strcmp(pl->home, b->env.home) || strcmp(pl->config, b->env.config))
            b->fail = "another root, home or config";
        else if (prev->missing) b->fail = "messages found nowhere (another file may hold them now)";
        for (int j = 0; j < pl->nsrcs && !b->fail; j++) {
            struct stat st;
            if (stat(pl->srcs[j].path, &st) || st.st_dev != pl->srcs[j].dev || st.st_ino != pl->srcs[j].ino ||
                st.st_size < pl->srcs[j].size)
                b->fail = "a file a message was found in changed";
        }
        if (!b->fail) read_new(b, path);
    } else {
        b->ld = xcalloc(1, sizeof *b->ld);
        b->ld->last_req = -1;
        off_t whole = 0;
        int tail_json = 0;
        read_records_at(path, &b->own.recs, cancel, &whole, &tail_json);
        b->new_off = whole, b->new_abs = b->own.recs.n, b->tail_json = tail_json;
        struct stat st;
        if (!stat(path, &st)) b->dev = st.st_dev, b->ino = st.st_ino;
    }
    Loader *ld = b->ld;
    if (!b->fail) scan_new(b);
    a->id = is_sub ? xstrndup(base_name(path) + 6, strlen(base_name(path)) - 12) : xstrdup(a->session);
    a->kind = xstrdup(is_sub ? "subagent" : "main");
    if (is_sub) {
        char *meta = meta_path(path);
        yyjson_doc *doc = yyjson_read_file(meta, 0, NULL, NULL);
        yyjson_val *r = yyjson_doc_get_root(doc);
        const char *type = gets(r, "agentType");
        a->name = xstrdup(type && *type ? type : "subagent");
        a->desc = xstrdup(gets(r, "description") ? gets(r, "description") : "");
        a->tu = gets(r, "toolUseId") ? xstrdup(gets(r, "toolUseId")) : NULL;
        a->model = gets(r, "model") ? xstrdup(gets(r, "model")) : NULL;
        yyjson_doc_free(doc);
        free(meta);
        a->title = title_scan(&ld->main, main_path);
    } else {
        a->name = xstrdup("main");
        a->title = xstrdup(ld->ai_title ? ld->ai_title : ld->first_prompt ? ld->first_prompt : "");
    }
    t[np++] = now_wall();
    if (!b->fail && store_build_meta(b, &b->own, 1) < 0 && b->resuming) b->fail = "a response from before the window grew";
    t[np++] = now_wall();
    int has_req = b->resuming; /* a resumable load was an exact one */
    for (int i = 0; i < b->own.recs.n && !has_req; i++) has_req = b->own.recs.r[i].type == T_REQ;
    if (b->fail) {
    } else if (has_req) build_calls(b);
    else {
        a->est = 1;
        build_estimated(b);
    }
    if (!b->fail) post(b);
    t[np++] = now_wall();
    if (!b->fail) stale_events(b, &ch, own_file, session, nsession);
    t[np++] = now_wall();
    for (int k = 0; k < ch.n; k++) free(ch.v[k].e), free(ch.v[k].seen), free(ch.v[k].seen_file);
    free(ch.v);
    strmap_free(&ch.ix);
    free(session), free(sub_prefix);
    if (!b->fail) links_of(b);
    ld->ok = has_req && !b->tail_json && !b->fail && !(cancel && atomic_load(cancel));
    if (ld->ok) save_loader(b, path); /* before the hand-over frees the calls' instances */
    t[np++] = now_wall();
    /* hand over: items, instances, calls' tails, texts */
    for (int i = 0; i < b->nitems; i++) {
        CtxItem *it = &b->items[i];
        it->w = round1(it->w);
        it->text = b->S.s[it->x];
        it->len = b->S.len[it->x];
        it->lang = lang_of(it->path);
        if (it->kind == CK_ACTION && it->tool) { /* the inputs a window shows, parsed once */
            yyjson_doc *doc = yyjson_read_opts(b->S.s[it->x], b->S.len[it->x], PYJ_READ, NULL, NULL);
            yyjson_val *inp = yyjson_doc_get_root(doc);
#define INPUT(field, key)                                                                             \
    do {                                                                                              \
        yyjson_val *v_ = get(inp, key);                                                               \
        if (yyjson_is_str(v_)) it->field = pool_str(&b->L, yyjson_get_str(v_), yyjson_get_len(v_));   \
    } while (0)
            if (!strcmp(it->tool, "Bash")) {
                INPUT(cmd, "command");
                INPUT(desc, "description");
            } else if (!strcmp(it->tool, "Edit")) {
                INPUT(old_str, "old_string");
                INPUT(new_str, "new_string");
            } else if (!strcmp(it->tool, "Write")) INPUT(content, "content");
#undef INPUT
            yyjson_doc_free(doc);
        }
    }
    for (int i = 0; i < b->nitems; i++) /* a Bash result shows its command */
        if (b->items[i].kind == CK_RESULT && b->items[i].action >= 0) b->items[i].cmd = b->items[b->items[i].action].cmd;
    a->items = b->items;
    a->nitems = b->nitems;
    for (int i = 0; i < a->nitems; i++) { /* a Read's line range */
        CtxItem *it = &a->items[i];
        char *buf, **ls;
        int *ns, n = ctx_code_lines(it, &buf, &ls, &ns, &(const char *){NULL});
        if (n) it->line0 = ns[0], it->line1 = ns[n - 1];
        free(buf), free(ls), free(ns);
    }
    a->inst = xmalloc(((size_t)b->ninst + 1) * sizeof *a->inst);
    a->ninst = b->ninst;
    for (int i = 0; i < b->ninst; i++) {
        a->inst[i].n = b->inst[i].nk;
        a->inst[i].items = xmalloc(((size_t)b->inst[i].nk + 1) * sizeof(int));
        if (b->inst[i].nk) memcpy(a->inst[i].items, b->kitems + b->inst[i].k0, (size_t)b->inst[i].nk * sizeof(int));
    }
    for (int c = 0; c < a->ncalls; c++) {
        CtxCall *C = &a->calls[c];
        CallB *cb = &b->cb[c];
        int from = C->base >= 0 ? (C->keep < cb->ninst ? C->keep : cb->ninst) : 0;
        if (from < 0) from = 0;
        C->ntail = cb->ninst - from;
        C->tail = xmalloc(((size_t)C->ntail + 1) * sizeof *C->tail);
        if (C->ntail) memcpy(C->tail, cb->inst + from, (size_t)C->ntail * sizeof *C->tail);
        free(cb->inst);
    }
    /* shape lists are shared between calls with the same shape: each call gets its own copy */
    for (int c = 0; c < a->ncalls; c++) {
        int *s = xmalloc(((size_t)a->calls[c].nshape + 1) * sizeof *s);
        if (a->calls[c].nshape) memcpy(s, a->calls[c].shape, (size_t)a->calls[c].nshape * sizeof *s);
        a->calls[c].shape = s;
    }
    for (int i = 0; i < b->nshapes; i++) free(b->shapes[i]);
    free(b->shapes);
    for (int i = 0; i < a->nitems; i++) { /* what each call's response made */
        int m = a->items[i].made;
        if (m < 0 || m >= a->ncalls) continue;
        a->calls[m].made = xrealloc(a->calls[m].made, (size_t)(a->calls[m].nmade + 1) * sizeof(int));
        a->calls[m].made[a->calls[m].nmade++] = i;
    }
    for (int c = 0; c < a->ncalls; c++)
        if (a->calls[c].t && (!a->t0 || a->calls[c].t < a->t0)) a->t0 = a->calls[c].t;
    derive(a);
    t[np++] = now_wall();
    if (getenv("AIG_DEBUG")) { /* the load's phases (wall ms) */
        fprintf(stderr, "%s %.0f ms:", prev ? "ctx_reload" : "ctx_load", (t[np - 1] - t[0]) * 1e3);
        for (int i = 0; i + 1 < np; i++) fprintf(stderr, " %s %.0f", phase[i], (t[i + 1] - t[i]) * 1e3);
        int others = 0;
        for (int i = 0; i < b->nfiles; i++) others += b->others[i].loaded;
        fprintf(stderr, "; %d project files, %d read for blobs; %ld records%s%s", b->nfiles, others,
                (long)b->own.recs.n, b->fail ? ", full load: " : "", b->fail ? b->fail : "");
        if (ld->ok) { /* what a reload resumes from */
            size_t sh = 0, keys = 0;
            for (int i = 0; i < ld->nshape; i++) sh += strlen(ld->shape_raw[i]);
            for (int i = 0; i < ld->nold; i++) keys += (size_t)ld->old[i].nkeys;
            fprintf(stderr, "; kept: window %.0f KB, %d instance keys, %d calls, shapes %.0f KB, %d older responses "
                    "(%zu block keys), digests %zu + %zu + %zu", ld->nwin / 1e3, ld->nkeys, ld->ncalls, sh / 1e3,
                    ld->nold, keys, ld->old_only.n, ld->tail_seen.n, ld->unresolved.n);
        }
        fputc('\n', stderr);
    }
    a->str = b->S.s;
    a->slen = b->S.len;
    a->nstr = b->S.n;
    pool_free(&b->S, 1);
    Priv *pv = xcalloc(1, sizeof *pv);
    pv->L = b->L;
    pv->keys = xmalloc(((size_t)a->nitems + 1) * sizeof *pv->keys);
    pv->ix = xmalloc(((size_t)a->nitems + 1) * sizeof *pv->ix);
    uint64_t *by_item = xmalloc(((size_t)a->nitems + 1) * sizeof *by_item);
    for (int i = 0; i < a->nitems; i++) pv->ix[i] = i, by_item[i] = a->items[i].key;
    if (a->nitems > 1) qsort_r(pv->ix, (size_t)a->nitems, sizeof *pv->ix, cmp_key_ix, by_item);
    for (int i = 0; i < a->nitems; i++) pv->keys[i] = by_item[pv->ix[i]];
    free(by_item);
    a->priv = pv;
    if (ld->ok) pv->ld = ld;
    else loader_free(ld);
    /* the build's own memory */
    store_free(&b->own);
    for (int i = 0; i < b->nfiles; i++) {
        if (b->others[i].loaded) store_free(&b->others[i]);
        free(b->files[i]);
    }
    free(b->others), free(b->files), free(b->blk), free(b->hc), free(b->inst), free(b->keys), free(b->kitems);
    free(b->result_of), free(b->cb), free(b->kset);
    pyb_free(&b->buf);
    strmap_free(&b->tool_use), strmap_free(&b->result_ix);
    free(b->env.home), free(b->env.config);
    free(pd), free(main_path), free(b->raw);
    if (b->fail) {
        if (refused) *refused = b->fail;
        ctx_free(a);
        return NULL;
    }
    if (cancel && atomic_load(cancel)) {
        ctx_free(a);
        return NULL;
    }
    return a;
}

CtxAgent *ctx_reload(const CtxAgent *prev, CtxProject *proj, atomic_int *cancel, char **err) {
    const Priv *pp = prev->priv;
    if (pp && pp->ld && pp->ld->ok) {
        locale_t loc = c_numeric();
        const char *refused = NULL;
        CtxAgent *a = load(prev->root, prev->path, proj, cancel, err, prev, &refused);
        uselocale(loc);
        if (a || !refused || (cancel && atomic_load(cancel))) return a;
    }
    return ctx_load(prev->root, prev->path, proj, cancel, err);
}

void ctx_free(CtxAgent *a) {
    if (!a) return;
    for (int i = 0; i < a->nitems; i++) {
        CtxItem *it = &a->items[i];
        free(it->tx), free(it->names), free(it->st), free(it->lk);
    }
    free(a->items);
    for (int i = 0; i < a->ninst; i++) free(a->inst[i].items);
    free(a->inst);
    for (int c = 0; c < a->ncalls; c++) {
        CtxCall *C = &a->calls[c];
        free(C->tail), free(C->shape), free(C->drop), free(C->order), free(C->state), free(C->cum), free(C->made);
    }
    free(a->calls);
    for (int i = 0; i < a->nstr; i++) free(a->str[i]);
    free(a->str), free(a->slen);
    Priv *pv = a->priv;
    if (pv) {
        pool_free(&pv->L, 0);
        loader_free(pv->ld);
        free(pv->keys), free(pv->ix), free(pv);
    }
    free(a->path), free(a->session), free(a->id), free(a->kind), free(a->name), free(a->desc), free(a->tu);
    free(a->model), free(a->title), free(a->root);
    free(a);
}

/* ---- JSON ---- */

static void jstr(FILE *f, const char *s, size_t n) {
    PyBuf o = {0};
    pyj_str(&o, s ? s : "", s ? n : 0);
    /* the page embeds it in a <script>: no "</" may close it early */
    for (size_t i = 0; i < o.n; i++) {
        if (o.s[i] == '<' && i + 1 < o.n && o.s[i + 1] == '/') fputs("<\\", f);
        else fputc(o.s[i], f);
    }
    pyb_free(&o);
}

static void js(FILE *f, const char *s) {
    if (s) jstr(f, s, strlen(s));
    else fputs("null", f);
}

static void jnum(FILE *f, double d) {
    PyBuf o = {0};
    pyj_float(&o, d);
    fwrite(o.s, 1, o.n, f);
    pyb_free(&o);
}

static void jints(FILE *f, const int *v, int n) {
    fputc('[', f);
    for (int i = 0; i < n; i++) fprintf(f, i ? ",%d" : "%d", v[i]);
    fputc(']', f);
}

static void json_out(FILE *f, const CtxAgent *a, const int *at, int nat);

void ctx_json(FILE *f, const CtxAgent *a, const int *at, int nat) {
    locale_t prev = c_numeric();
    json_out(f, a, at, nat);
    uselocale(prev);
}

static void json_out(FILE *f, const CtxAgent *a, const int *at, int nat) {
    double t0 = 0, t1 = 0;
    for (int c = 0; c < a->ncalls; c++) {
        double t = a->calls[c].t;
        if (t && (!t0 || t < t0)) t0 = t;
        if (t > t1) t1 = t;
    }
    fputs("{\"meta\":{\"repo\":", f);
    js(f, a->root);
    fputs(",\"t0\":", f);
    jnum(f, t0);
    fputs(",\"prices\":{", f);
    for (int i = 0; i < ctx_nprices; i++) {
        const CtxPrice *p = &ctx_prices[i];
        fprintf(f, "%s\"%s\":{\"in\":", i ? "," : "", p->model);
        jnum(f, p->in);
        fputs(",\"read\":", f);
        jnum(f, p->read);
        fputs(",\"w5\":", f);
        jnum(f, p->w5);
        fputs(",\"w1h\":", f);
        jnum(f, p->w1h);
        fputs(",\"out\":", f);
        jnum(f, p->out);
        fputc('}', f);
    }
    fputs("},\"price_source\":", f);
    js(f, ctx_price_source);
    fputs(",\"windows\":{", f);
    for (size_t i = 0; i < sizeof WINDOWS / sizeof *WINDOWS; i++)
        fprintf(f, "%s\"%s\":%d", i ? "," : "", WINDOWS[i].model, WINDOWS[i].tokens);
    fprintf(f, "},\"stats\":{\"missing\":%d,\"found_elsewhere\":%d,\"calls\":%d,\"broken\":%d,\"left_out\":[]}},",
            a->missing, a->found_elsewhere, a->ncalls, a->broken);
    fputs("\"sessions\":[{\"id\":", f);
    js(f, a->session);
    fputs(",\"title\":", f);
    js(f, a->title);
    fprintf(f, ",\"start\":0,\"end\":%.0f,\"agents\":[{\"kind\":", rint(t1 - t0));
    js(f, a->kind);
    fputs(",\"name\":", f);
    js(f, a->name);
    if (!strcmp(a->kind, "subagent")) {
        fputs(",\"desc\":", f);
        js(f, a->desc);
        fputs(",\"tu\":", f);
        js(f, a->tu);
        fputs(",\"model\":", f);
        js(f, a->model);
    }
    fputs(",\"id\":", f);
    js(f, a->id);
    fprintf(f, ",\"est\":%s,\"missing\":%d,\"calls\":[", a->est ? "true" : "false", a->missing);
    for (int c = 0; c < a->ncalls; c++) {
        const CtxCall *C = &a->calls[c];
        fputs(c ? ",{\"t\":" : "{\"t\":", f);
        if (C->t) jnum(f, round1(C->t - t0));
        else fputs("null", f);
        fputs(",\"m\":", f);
        js(f, C->model);
        if (C->base >= 0) fprintf(f, ",\"b\":%d", C->base);
        else fputs(",\"b\":null", f);
        fprintf(f, ",\"k\":%d,\"tl\":", C->keep);
        jints(f, C->tail, C->ntail);
        fputs(",\"sh\":", f);
        jints(f, C->shape, C->nshape);
        fputs(",\"u\":", f);
        if (C->has_u)
            fprintf(f, "[%lld,%lld,%lld,%lld,%lld]", C->u[0], C->u[1], C->u[2], C->u[3], C->u[4]);
        else fputs("null", f);
        if (C->comp) fputs(",\"cmp\":1", f);
        if (C->ndrop) {
            fputs(",\"drop\":", f);
            jints(f, C->drop, C->ndrop);
        }
        fputc('}', f);
    }
    fputs("],\"inst\":[", f);
    for (int i = 0; i < a->ninst; i++) {
        if (i) fputc(',', f);
        jints(f, a->inst[i].items, a->inst[i].n);
    }
    /* S: the texts in the order the items use them (x, then tx) */
    int *remap = xmalloc(((size_t)a->nstr + 1) * sizeof *remap), *order = xmalloc(((size_t)a->nstr + 1) * sizeof *order), ns = 0;
    memset(remap, -1, ((size_t)a->nstr + 1) * sizeof *remap);
    for (int i = 0; i < a->nitems; i++) {
        const CtxItem *it = &a->items[i];
        if (remap[it->x] < 0) order[ns] = it->x, remap[it->x] = ns++;
        for (int k = 0; k < it->ntx; k++)
            if (remap[it->tx[k]] < 0) order[ns] = it->tx[k], remap[it->tx[k]] = ns++;
    }
    fputs("],\"items\":[", f);
    for (int i = 0; i < a->nitems; i++) {
        const CtxItem *it = &a->items[i];
        fprintf(f, "%s{\"k\":\"%s\",\"s\":", i ? "," : "", ctx_kind_names[it->kind]);
        js(f, it->src);
        fputs(",\"w\":", f);
        jnum(f, it->w);
        if (it->made >= 0) fprintf(f, ",\"m\":%d", it->made);
        if (it->kind == CK_ACTION || it->action >= 0) {
            fputs(",\"tool\":", f);
            js(f, it->tool);
        }
        if (it->path) {
            fputs(",\"p\":", f);
            js(f, it->path);
        }
        if (it->err) fputs(",\"err\":1", f);
        fprintf(f, ",\"x\":%d,\"n\":%d", remap[it->x], it->n);
        if (it->enter >= 0) fprintf(f, ",\"e\":%d", it->enter);
        if (it->leave >= 0) fprintf(f, ",\"l\":%d", it->leave);
        if (it->action >= 0) fprintf(f, ",\"a\":%d", it->action);
        if (it->result >= 0) fprintf(f, ",\"r\":%d", it->result);
        if (it->ntx) {
            fputs(",\"tx\":[", f);
            for (int k = 0; k < it->ntx; k++) fprintf(f, k ? ",%d" : "%d", remap[it->tx[k]]);
            fputs("],\"names\":[", f);
            for (int k = 0; k < it->ntx; k++) {
                if (k) fputc(',', f);
                js(f, it->names[k]);
            }
            fputc(']', f);
        }
        if (it->nst) {
            fputs(",\"st\":[", f);
            for (int k = 0; k < it->nst; k++) {
                fputs(k ? ",[" : "[", f);
                jnum(f, round1(it->st[k].t - t0));
                fprintf(f, ",%d,%d,", it->st[k].kind, it->st[k].item);
                js(f, it->st[k].label);
                fputc(']', f);
            }
            fputc(']', f);
        }
        if (it->lk) {
            fputs(",\"lk\":[", f);
            for (int k = 0; k < it->nlk; k++) {
                fprintf(f, "%s[%d,", k ? "," : "", it->lk[k].item);
                js(f, it->lk[k].why);
                fputc(']', f);
            }
            fputc(']', f);
        }
        fputc('}', f);
    }
    fputc(']', f);
    if (nat) {
        fputs(",\"at\":{", f);
        for (int k = 0; k < nat; k++) {
            const CtxCall *C = &a->calls[at[k]];
            fprintf(f, "%s\"%d\":[", k ? "," : "", at[k]);
            for (int i = 0; i < C->norder; i++) {
                fputs(i ? ",[" : "[", f);
                jnum(f, ctx_tokens(a, at[k], i));
                fprintf(f, ",%d,", C->state[i]);
                jnum(f, C->cum[i]);
                fputc(']', f);
            }
            fputc(']', f);
        }
        fputc('}', f);
    }
    fputs("}]}],\"S\":[", f);
    for (int i = 0; i < ns; i++) {
        if (i) fputc(',', f);
        jstr(f, a->str[order[i]], a->slen[order[i]]);
    }
    fputs("]}\n", f);
    free(remap), free(order);
}
