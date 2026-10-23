#include "cmdguess.h"

#include <ctype.h>
#include <limits.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>

#include "util.h"

/* A small shell lexer: enough of POSIX sh quoting, operators, redirects and
 * here-documents to find the plain words of each simple command. */

typedef struct {
    char *s;
    bool dyn; /* built at run time: unquoted $, `, glob or brace */
} Word;

typedef struct { char *dir; bool lost; } Where; /* where cd had gone */

typedef struct {
    GuessFn fn;
    void *ud;
    char *dir;  /* where cd went, as written */
    bool lost;  /* a cd we can't follow: relative paths mean nothing */
    Word *w;
    int n, cap;
    struct Heredoc { char *delim; bool tabs; } hd[8]; /* bodies start at the next newline */
    int nhd;
    Where sub[8]; /* ( subshells ): their cd doesn't last */
    int nsub;
    Where push[8]; /* pushd: where popd goes back to */
    int npush;
    bool test; /* inside [[ ]]: < and > compare */
    /* cmd_runs: each simple command's words joined by spaces, whether a single |
     * pipes it into the next, and where cd had gone before it */
    void (*seg)(void *ud, const char *text, bool piped, const char *dir, bool lost);
    bool piped;
} G;

static void put(Buf *b, char c) { buf_put(b, &c, 1); }

static bool is_delim(char c) {
    return !c || c == ' ' || c == '\t' || c == '\n' || c == ';' || c == '&' || c == '|' || c == '(' || c == ')' ||
           c == '<' || c == '>';
}

static size_t read_word(const char *s, size_t i, char **out, bool *dyn);

/* Past a here-document body: the lines from s[i] through the one that is `delim`. */
static size_t skip_body(const char *s, size_t i, const char *delim, bool tabs) {
    while (s[i]) {
        size_t e = i;
        while (s[e] && s[e] != '\n') e++;
        size_t b = i;
        if (tabs) while (b < e && s[b] == '\t') b++;
        bool end = e - b == strlen(delim) && !strncmp(s + b, delim, e - b);
        i = s[e] ? e + 1 : e;
        if (end) break;
    }
    return i;
}

/* Past a balanced (...) starting at s[i] == '(' (quotes and here-documents respected). */
static size_t skip_parens(const char *s, size_t i) {
    int depth = 0;
    char *delim = NULL;
    bool tabs = false, arith = s[i + 1] == '('; /* $((1<<2)): a shift, not a here-document */
    for (; s[i]; i++) {
        if (s[i] == '\n' && delim) { /* a here-document's body: its quotes mean nothing */
            i = skip_body(s, i + 1, delim, tabs) - 1; /* the loop steps past it */
            free(delim);
            delim = NULL;
            continue;
        }
        if (s[i] == '<' && s[i + 1] == '<' && s[i + 2] != '<' && !delim && !arith) {
            size_t k = i + 2;
            tabs = s[k] == '-';
            k += tabs;
            while (s[k] == ' ' || s[k] == '\t') k++;
            bool dyn;
            i = read_word(s, k, &delim, &dyn) - 1;
            continue;
        }
        if (s[i] == '(' && s[i + 1] == '(' && depth) { i = skip_parens(s, i) - 1; continue; } /* nested $(( )) */
        if (s[i] == '\\' && s[i + 1]) i++;
        else if (s[i] == '\'') { while (s[i + 1] && s[i + 1] != '\'') i++; if (s[i + 1]) i++; }
        else if (s[i] == '"') { while (s[i + 1] && s[i + 1] != '"') { if (s[i + 1] == '\\' && s[i + 2]) i++; i++; } if (s[i + 1]) i++; }
        else if (s[i] == '(') depth++;
        else if (s[i] == ')' && --depth == 0) { free(delim); return i + 1; }
    }
    free(delim);
    return i;
}

/* One word from s[i]: quotes removed, escapes resolved. Returns the index after it. */
static size_t read_word(const char *s, size_t i, char **out, bool *dyn) {
    Buf b = {0};
    buf_put(&b, "", 0);
    *dyn = false;
    while (!is_delim(s[i])) {
        char c = s[i];
        if (c == '\'') {
            for (i++; s[i] && s[i] != '\''; i++) put(&b, s[i]);
            if (s[i]) i++;
        } else if (c == '"') {
            for (i++; s[i] && s[i] != '"'; i++) {
                if (s[i] == '$' && s[i + 1] == '(') { /* "$(...)": its own quoting inside */
                    *dyn = true;
                    i = skip_parens(s, i + 1) - 1;
                    continue;
                }
                if (s[i] == '\\' && s[i + 1] && strchr("$`\"\\\n", s[i + 1])) i++;
                else if (s[i] == '$' || s[i] == '`') *dyn = true;
                put(&b, s[i]);
            }
            if (s[i]) i++;
        } else if (c == '\\') {
            if (s[i + 1] == '\n') { i += 2; continue; }
            if (s[i + 1]) put(&b, s[++i]);
            i++;
        } else if (c == '$' && s[i + 1] == '(') {
            *dyn = true;
            i = skip_parens(s, i + 1);
        } else if (c == '`') {
            *dyn = true;
            for (i++; s[i] && s[i] != '`'; i++) if (s[i] == '\\' && s[i + 1]) i++;
            if (s[i]) i++;
        } else {
            if (c == '$' || c == '*' || c == '?' || c == '[' || c == '{') *dyn = true;
            put(&b, c);
            i++;
        }
    }
    *out = b.s;
    return i;
}

static void emit(G *g, const char *path, GuessKind k, const char *into, int first, int last) {
    if (!g->fn || !*path || !strncmp(path, "/dev/", 5) || !strncmp(path, "/proc/", 6)) return;
    if (path[0] == '~' && path[1] && path[1] != '/') return; /* ~user */
    if (path[0] != '/' && path[0] != '~' && g->lost) return;
    g->fn(g->ud, g->dir, path, k, into, first, last);
}

static const char *base_name(const char *p, char *buf, size_t n) {
    size_t len = strlen(p);
    while (len > 1 && p[len - 1] == '/') len--;
    size_t start = len;
    while (start > 0 && p[start - 1] != '/') start--;
    snprintf(buf, n, "%.*s", (int)(len - start), p + start);
    return buf;
}

static bool is_option(const Word *w, bool ddash) { return !ddash && w->s[0] == '-' && w->s[1]; }

/* A short-option cluster ("-pi.bak", "-ne"): the first of `letters` in it, or 0. */
static char cluster_has(const char *s, const char *letters) {
    if (s[0] != '-' || s[1] == '-') return 0;
    for (const char *p = s + 1; *p; p++)
        if (strchr(letters, *p)) return *p;
    return 0;
}

static void cmd_cd(G *g, Word *a, int na) {
    int k = 0;
    while (k < na && (!strcmp(a[k].s, "-P") || !strcmp(a[k].s, "-L") || !strcmp(a[k].s, "--"))) k++;
    if (k >= na || a[k].dyn || !strcmp(a[k].s, "-") || (a[k].s[0] == '~' && a[k].s[1] && a[k].s[1] != '/')) {
        g->lost = true; /* home, previous dir, $VAR, ~user */
        return;
    }
    const char *d = a[k].s;
    char *nd;
    if (d[0] == '/' || d[0] == '~' || !g->dir) {
        if (g->lost && d[0] != '/' && d[0] != '~') return; /* relative to a dir we lost */
        nd = xstrdup(d);
        g->lost = false;
    } else {
        nd = xasprintf("%s/%s", g->dir, d);
    }
    free(g->dir);
    g->dir = nd;
}

#define SED_RANGES 16

/* sed -n's script as line ranges, added to r[n..]: each command is `A,Bp`, `Ap`
 * or `A,$p` (; or newlines between). -1 for anything else: the whole file. */
static int sed_ranges(const char *s, int r[][2], int n) {
    for (;;) {
        while (*s == ' ' || *s == '\t' || *s == ';' || *s == '\n') s++;
        if (!*s) return n;
        if (!isdigit((unsigned char)*s) || n == SED_RANGES) return -1;
        char *e;
        long a = strtol(s, &e, 10), b = a;
        if (*e == ',') {
            if (e[1] == '$') b = 0, e += 2;
            else if (isdigit((unsigned char)e[1])) b = strtol(e + 1, &e, 10);
            else return -1;
        }
        while (*e == ' ' || *e == '\t') e++;
        if (*e != 'p' || a < 1) return -1;
        for (e++; *e == ' ' || *e == '\t'; e++) {}
        if (*e && *e != ';' && *e != '\n') return -1;
        if (b && b < a) b = a; /* 5,2p prints line 5 */
        a = a > INT_MAX ? INT_MAX : a, b = b > INT_MAX ? INT_MAX : b;
        r[n][0] = a == 1 && !b ? 0 : (int)a; /* 1,$: the whole file */
        r[n++][1] = (int)b;
        s = e;
    }
}

/* sed -i / perl -i: the files after the script, changed; without -i, sed
 * reads them (perl's operands may be its script's own arguments): with -n and
 * a script of line prints only those lines, each range a read of each file. */
static void cmd_inplace(G *g, Word *a, int na, bool perl) {
    bool inplace = false, script = false, ddash = false, quiet = false;
    int r[SED_RANGES][2], nr = 0, *ops = xmalloc((size_t)(na + 1) * sizeof *ops), nops = 0;
#define SCRIPT(w, dyn) (script = true, nr = nr < 0 || (dyn) ? -1 : sed_ranges((w), r, nr))
    for (int k = 0; k < na; k++) {
        const char *s = a[k].s;
        if (is_option(&a[k], ddash)) {
            if (!strcmp(s, "--")) { ddash = true; continue; }
            if (!strncmp(s, "--in-place", 10)) { inplace = true; continue; }
            if (!strcmp(s, "--quiet") || !strcmp(s, "--silent")) { quiet = true; continue; }
            if (!strcmp(s, "--expression")) { if (k + 1 < na) SCRIPT(a[k + 1].s, a[k + 1].dyn); k++; continue; }
            if (!strncmp(s, "--expression=", 13)) { SCRIPT(s + 13, a[k].dyn); continue; }
            if (!strcmp(s, "--file")) { script = true, nr = -1, k++; continue; }
            if (!strncmp(s, "--file=", 7)) { script = true, nr = -1; continue; }
            if (s[1] == '-') continue;
            for (const char *p = s + 1; *p; p++) {
                if (*p == 'i') { /* the rest is a backup suffix; BSD sed takes it as the next word */
                    inplace = true;
                    if (!p[1] && !perl && k + 1 < na && !a[k + 1].s[0]) k++;
                    break;
                }
                if (*p == 'e' || (perl ? *p == 'E' : *p == 'f')) {
                    if (perl || *p == 'f') script = true, nr = -1;
                    else if (p[1]) SCRIPT(p + 1, a[k].dyn);
                    else if (k + 1 < na) SCRIPT(a[k + 1].s, a[k + 1].dyn);
                    if (!p[1]) k++;
                    break;
                }
                if (!perl && *p == 'n') quiet = true;
                if (perl && (*p == 'l' || *p == '0')) { while (p[1] >= '0' && p[1] <= '9') p++; continue; } /* -l[octal] */
                if (perl ? strchr("IMm", *p) != NULL : *p == 'l') { if (!p[1] && !perl) k++; break; } /* takes a value */
            }
            continue;
        }
        if (!script) { SCRIPT(s, a[k].dyn); continue; }
        if (a[k].dyn) continue;
        if (inplace) emit(g, s, GUESS_CHANGE, NULL, 0, 0);
        else if (!perl && strcmp(s, "-")) ops[nops++] = k; /* "-": stdin */
    }
#undef SCRIPT
    bool ranged = quiet && nr > 0;
    for (int i = 0; i < nops; i++)
        for (int j = 0; j < (ranged ? nr : 1); j++)
            emit(g, a[ops[i]].s, GUESS_READ, NULL, ranged ? r[j][0] : 0, ranged ? r[j][1] : 0);
    free(ops);
}

/* Each operand (non-option word). `valued`: short options that take the next
 * word; `long_valued`: long ones that do when written apart ("--lines 5").
 * For reads, "-" (stdin) and "+cmd" (less, more) aren't files. */
static void cmd_operands(G *g, Word *a, int na, const char *valued, const char *const *long_valued, GuessKind kind,
                         int first, int last) {
    bool ddash = false;
    for (int k = 0; k < na; k++) {
        if (is_option(&a[k], ddash)) {
            if (!strcmp(a[k].s, "--")) ddash = true;
            else if (a[k].s[1] != '-' && a[k].s[2] == 0 && strchr(valued, a[k].s[1])) k++;
            else if (a[k].s[1] == '-' && long_valued)
                for (int l = 0; long_valued[l]; l++)
                    if (!strcmp(a[k].s, long_valued[l])) { k++; break; }
            continue;
        }
        if (a[k].dyn || (kind == GUESS_READ && !ddash && (!strcmp(a[k].s, "-") || a[k].s[0] == '+'))) continue;
        emit(g, a[k].s, kind, NULL, first, last);
    }
}

/* head: lines 1..N of each file; tail: the last N, or from line N with +N. N
 * from -n N, -nN, -N, --lines[=]N, the last one given; 10 without one. Bytes
 * (-c), all but the last N (head -n -N) or a count that isn't a plain number:
 * the whole file. N = 0 reads nothing. */
static void cmd_headtail(G *g, Word *a, int na, bool tail) {
    static const char *const lng[] = {"--lines", "--bytes", "--pid", "--sleep-interval", "--max-unchanged-stats", NULL};
    const char *v = "10";
    bool whole = false;
    for (int k = 0; k < na && strcmp(a[k].s, "--"); k++) {
        const char *s = a[k].s, *val = NULL;
        bool dyn = a[k].dyn;
        if (s[0] != '-' || !s[1]) continue;
        if (!strcmp(s, "-n") || !strcmp(s, "--lines")) {
            if (k + 1 < na) val = a[k + 1].s, dyn = a[k + 1].dyn;
            k++;
        } else if (!strncmp(s, "--lines=", 8)) val = s + 8;
        else if (s[1] == 'n') val = s + 2;
        else if (isdigit((unsigned char)s[1])) val = s + 1;
        else if (s[1] == 'c' || !strncmp(s, "--bytes", 7)) whole = true;
        if (val) v = dyn ? "x" : val;
    }
    char sign = *v == '+' || *v == '-' ? *v++ : 0;
    char *e;
    long n = strtol(v, &e, 10);
    if (!isdigit((unsigned char)*v) || *e || (sign == '-' && !tail)) whole = true;
    if (n > INT_MAX) n = INT_MAX;
    if (!whole && !n) return;
    int first = 0, last = 0;
    if (!whole && tail && sign == '+') first = n > 1 ? (int)n : 0;
    else if (!whole && tail) last = (int)-n;
    else if (!whole) first = 1, last = (int)n;
    cmd_operands(g, a, na, "ncs", lng, GUESS_READ, first, last);
}

/* mv / cp: the destination gets each source; mv also removes the sources. */
static void cmd_move(G *g, Word *a, int na, bool move) {
    bool ddash = false;
    const char *target = NULL;
    int ops[64], nops = 0;
    for (int k = 0; k < na; k++) {
        const char *s = a[k].s;
        if (is_option(&a[k], ddash)) {
            if (!strcmp(s, "--")) ddash = true;
            else if (!strcmp(s, "-t") && k + 1 < na) target = a[++k].s;
            else if (!strncmp(s, "--target-directory=", 19)) target = s + 19;
            else if (!strcmp(s, "-S") || !strcmp(s, "--suffix")) k++;
            else if (!move && (cluster_has(s, "rRa") || !strcmp(s, "--recursive") || !strcmp(s, "--archive"))) return;
            continue;
        }
        if (a[k].dyn || nops == 64) return; /* can't tell which word is the destination */
        ops[nops++] = k;
    }
    if (!target) {
        if (nops < 2) return;
        target = a[ops[--nops]].s;
    }
    char into[256];
    for (int i = 0; i < nops; i++) {
        const char *src = a[ops[i]].s;
        if (move) emit(g, src, GUESS_DELETE, NULL, 0, 0);
        emit(g, target, GUESS_CHANGE, base_name(src, into, sizeof into), 0, 0);
    }
}

static bool is_assignment(const Word *w) {
    const char *eq = strchr(w->s, '=');
    if (!eq || eq == w->s) return false;
    for (const char *p = w->s; p < eq; p++)
        if (!(*p == '_' || (*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') || (p > w->s && *p >= '0' && *p <= '9')))
            return false;
    return true;
}

static Where save(const G *g) { return (Where){dup_or_null(g->dir), g->lost}; }
static void restore(G *g, Where w) {
    free(g->dir);
    g->dir = w.dir, g->lost = w.lost;
}

static void end_cmd(G *g) {
    Word *a = g->w;
    int n = g->n, k = 0;
    if (g->seg && n) {
        Buf t = {0};
        for (int i = 0; i < n; i++) buf_puts(&t, i ? " " : ""), buf_puts(&t, a[i].s);
        g->seg(g->ud, t.s, g->piped, g->dir, g->lost);
        buf_free(&t);
    }
    g->piped = false;
    static const char *skip[] = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}", "time",
                                 "sudo", "command", "nohup", "exec", "env", NULL};
    for (bool again = true; again && k < n;) {
        again = false;
        if (is_assignment(&a[k])) { k++; again = true; continue; }
        for (int s = 0; skip[s]; s++)
            if (!strcmp(a[k].s, skip[s])) {
                k++;
                while (k < n && s >= 11 && a[k].s[0] == '-') k++; /* sudo -u x / env -i: options (values not handled) */
                again = true;
                break;
            }
    }
    if (k < n && !a[k].dyn) {
        char nb[64];
        const char *cmd = base_name(a[k].s, nb, sizeof nb);
        Word *args = a + k + 1;
        int na = n - k - 1;
        static const char *const bat_long[] = {"--language", "--line-range", "--highlight-line", "--map-syntax",
                                               "--theme", "--style", "--tabs", "--wrap", "--terminal-width", NULL};
        if (!strcmp(cmd, "cd")) cmd_cd(g, args, na);
        else if (!strcmp(cmd, "pushd") && g->npush < 8) {
            g->push[g->npush++] = save(g);
            cmd_cd(g, args, na);
        } else if (!strcmp(cmd, "popd")) /* none pushed: back to somewhere from before this command */
            restore(g, g->npush ? g->push[--g->npush] : (Where){NULL, true});
        else if (!strcmp(cmd, "sed") || !strcmp(cmd, "gsed")) cmd_inplace(g, args, na, false);
        else if (!strcmp(cmd, "perl")) cmd_inplace(g, args, na, true);
        else if (!strcmp(cmd, "tee")) cmd_operands(g, args, na, "", NULL, GUESS_CHANGE, 0, 0);
        else if (!strcmp(cmd, "touch")) cmd_operands(g, args, na, "dtr", NULL, GUESS_CHANGE, 0, 0);
        else if (!strcmp(cmd, "truncate")) cmd_operands(g, args, na, "sr", NULL, GUESS_CHANGE, 0, 0);
        else if (!strcmp(cmd, "rm")) {
            bool rec = false;
            for (int i = 0; i < na; i++) rec |= cluster_has(args[i].s, "rR") || !strcmp(args[i].s, "--recursive");
            if (!rec) cmd_operands(g, args, na, "", NULL, GUESS_DELETE, 0, 0); /* a tree: which files, unknown */
        } else if (!strcmp(cmd, "mv")) cmd_move(g, args, na, true);
        else if (!strcmp(cmd, "cp")) cmd_move(g, args, na, false);
        else if (!strcmp(cmd, "cat") || !strcmp(cmd, "more")) cmd_operands(g, args, na, "", NULL, GUESS_READ, 0, 0);
        else if (!strcmp(cmd, "tac")) cmd_operands(g, args, na, "s", NULL, GUESS_READ, 0, 0);
        else if (!strcmp(cmd, "less")) cmd_operands(g, args, na, "bhjkopPtTxyz", NULL, GUESS_READ, 0, 0);
        else if (!strcmp(cmd, "bat") || !strcmp(cmd, "batcat")) cmd_operands(g, args, na, "lrHm", bat_long, GUESS_READ, 0, 0);
        else if (!strcmp(cmd, "head") || !strcmp(cmd, "tail")) cmd_headtail(g, args, na, cmd[0] == 't');
        else if (!strcmp(cmd, "nl")) cmd_operands(g, args, na, "bdfhilnsvw", NULL, GUESS_READ, 0, 0);
    }
    for (int i = 0; i < n; i++) free(a[i].s);
    g->n = 0;
    g->test = false;
}

static void push_word(G *g, char *s, bool dyn) {
    if (!strcmp(s, "[[")) g->test = true; /* after if, !, x=1 too; "[[" reads as a glob: dyn */
    else if (!strcmp(s, "]]")) g->test = false;
    PUSH(g->w, g->n, g->cap, ((Word){s, dyn}));
}

static size_t skip_blank(const char *s, size_t i) {
    while (s[i] == ' ' || s[i] == '\t') i++;
    return i;
}

/* A redirect's target word from s[i]: emitted as `kind` when `emit_it`. */
static size_t redir_target(G *g, const char *s, size_t i, bool emit_it, GuessKind kind) {
    i = skip_blank(s, i);
    if (s[i] == '(') return skip_parens(s, i); /* >(cmd) */
    char *w;
    bool dyn;
    i = read_word(s, i, &w, &dyn);
    if (emit_it && !dyn) emit(g, w, kind, NULL, 0, 0);
    free(w);
    return i;
}

/* After a newline: the bodies of pending here-documents. */
static size_t skip_heredocs(G *g, const char *s, size_t i) {
    for (int h = 0; h < g->nhd; h++) i = skip_body(s, i, g->hd[h].delim, g->hd[h].tabs), free(g->hd[h].delim);
    g->nhd = 0;
    return i;
}

static void lex(G *g, const char *s) {
    size_t i = 0;
    while (s[i]) {
        char c = s[i];
        if (c == ' ' || c == '\t') { i++; continue; }
        if (c == '\\' && s[i + 1] == '\n') { i += 2; continue; }
        if (c == '\n') { end_cmd(g); i = skip_heredocs(g, s, i + 1); continue; }
        if (c == '#') { while (s[i] && s[i] != '\n') i++; continue; }
        if (c == '&' && s[i + 1] == '>') { /* &> and &>>: stdout and stderr to a file */
            i += 2 + (s[i + 2] == '>');
            i = redir_target(g, s, i, true, GUESS_CHANGE);
            continue;
        }
        if (c == '(' && s[i + 1] == '(') { /* (( arithmetic )): < and > compare */
            i = skip_parens(s, i);
            continue;
        }
        if (c == '(' || c == ')') { /* a subshell: cd inside it ends with it */
            if (c == ')') { /* (make) 2>&1 | tail: the subshell's last command is piped */
                size_t j = skip_blank(s, i + 1);
                if (s[j] == '2' && s[j + 1] == '>' && s[j + 2] == '&' && s[j + 3] == '1') j = skip_blank(s, j + 4);
                g->piped = s[j] == '|' && s[j + 1] != '|';
            }
            end_cmd(g);
            if (c == '(' && g->nsub < 8) g->sub[g->nsub++] = save(g);
            else if (c == ')' && g->nsub) restore(g, g->sub[--g->nsub]);
            i++;
            continue;
        }
        if (c == ';' || c == '&' || c == '|') {
            g->piped = c == '|' && s[i + 1] != '|' && (!i || s[i - 1] != '|');
            end_cmd(g);
            i++;
            continue;
        }
        if (g->test && (c == '<' || c == '>')) { /* [[ a < b ]] */
            push_word(g, xstrdup(c == '<' ? "<" : ">"), false);
            i++;
            continue;
        }
        size_t j = i;
        while (s[j] >= '0' && s[j] <= '9') j++;
        if (s[j] == '<') {
            if (s[j + 1] == '<' && s[j + 2] == '<') { i = redir_target(g, s, j + 3, false, GUESS_READ); continue; } /* here-string */
            if (s[j + 1] == '<') { /* here-document: its body follows the line */
                size_t k = j + 2;
                bool tabs = s[k] == '-';
                k = skip_blank(s, k + tabs);
                char *d;
                bool dyn;
                i = read_word(s, k, &d, &dyn);
                if (g->nhd < 8) g->hd[g->nhd++] = (struct Heredoc){d, tabs};
                else free(d);
                continue;
            }
            if (s[j + 1] == '(') i = skip_parens(s, j + 1); /* <(cmd) */
            else if (s[j + 1] == '&') i = redir_target(g, s, j + 2, false, GUESS_READ); /* <&3 */
            else i = redir_target(g, s, j + 1, true, GUESS_READ);
            continue;
        }
        if (s[j] == '>') {
            size_t k = j + 1;
            if (s[k] == '>' || s[k] == '|') k++;
            if (s[k] == '&') { /* >&2 duplicates a descriptor; >&file is &>file */
                size_t t = skip_blank(s, k + 1);
                bool fd = s[t] == '-' || (s[t] >= '0' && s[t] <= '9' && is_delim(s[t + 1]));
                i = redir_target(g, s, k + 1, !fd, GUESS_CHANGE);
                continue;
            }
            i = redir_target(g, s, k, true, GUESS_CHANGE);
            continue;
        }
        char *w;
        bool dyn;
        i = read_word(s, i, &w, &dyn);
        push_word(g, w, dyn);
    }
    end_cmd(g);
    for (int h = 0; h < g->nhd; h++) free(g->hd[h].delim);
    for (int k = 0; k < g->nsub; k++) free(g->sub[k].dir);
    for (int k = 0; k < g->npush; k++) free(g->push[k].dir);
    free(g->w);
    free(g->dir);
}

void cmd_guess(const char *s, GuessFn fn, void *ud) {
    G g = {.fn = fn, .ud = ud};
    lex(&g, s);
}

/* ---- command segments (docs/STRUCTURE.md section 0) ---------------------- */

static const char *skip_ws(const char *p) {
    while (is_space(*p)) p++;
    return p;
}
static const char *tok_end(const char *p) {
    while (*p && !is_space(*p)) p++;
    return p;
}

/* timeout's `(-opt [arg])* N `, backtracking as Python's re does: the end, or NULL. */
static const char *timeout_args(const char *p) {
    if (*p == '-') {
        const char *e = tok_end(p);
        if (e > p + 1 && is_space(*e)) {
            const char *q = skip_ws(e), *e2 = tok_end(q);
            if (e2 > q && is_space(*e2)) {
                const char *r = timeout_args(skip_ws(e2));
                if (r) return r;
            }
            const char *r = timeout_args(q);
            if (r) return r;
        }
    }
    const char *e = tok_end(p);
    return e > p && is_space(*e) ? skip_ws(e) : NULL;
}

/* A segment without what only sets how it runs: `(`, env, VAR=x, sudo, time, timeout N. */
static const char *unwrap(const char *p) {
    p = skip_ws(p);
    if (*p == '(') p = skip_ws(p + 1);
    for (;;) {
        const char *q = NULL, *w = p;
        while (isalnum((unsigned char)*w) || *w == '_') w++;
        if (!strncmp(p, "env", 3) && is_space(p[3])) q = skip_ws(p + 3);
        else if (w > p && *w == '=') {
            const char *e = tok_end(w + 1);
            if (is_space(*e)) q = skip_ws(e);
        }
        if (!q && !strncmp(p, "sudo", 4) && is_space(p[4])) q = skip_ws(p + 4);
        if (!q && !strncmp(p, "time", 4) && is_space(p[4])) q = skip_ws(p + 4);
        if (!q && !strncmp(p, "timeout", 7) && is_space(p[7])) q = timeout_args(skip_ws(p + 7));
        if (!q) return p;
        p = q;
    }
}

typedef struct {
    const regex_t *re;
    const char *root, *start;
    int hit;
    bool pipefail;
} Runs;

/* Where cd had gone is under root (start: where the command began). A cd we
 * can't follow ($VAR, $(...), cd -, a bare cd) counts as elsewhere. */
static bool in_tree(const Runs *r, const char *dir, bool lost) {
    if (lost) return false;
    const char *home = getenv("HOME");
    char *p;
    if (!dir) p = xstrdup(r->start);
    else if (dir[0] == '/') p = xstrdup(dir);
    else if (dir[0] == '~' && home) p = xasprintf("%s/%s", home, dir + 1);
    else p = xasprintf("%s/%s", r->start, dir);
    path_normalize(p);
    size_t n = strlen(r->root);
    bool in = !strncmp(p, r->root, n) && (!p[n] || p[n] == '/' || (n == 1 && r->root[0] == '/'));
    free(p);
    return in;
}

static void on_seg(void *ud, const char *text, bool piped, const char *dir, bool lost) {
    Runs *r = ud;
    if (!strncmp(text, "set ", 4)) /* set -o pipefail, set -euo pipefail; set +o pipefail */
        for (const char *p = text + 4; (p = strstr(p, " pipefail"));) {
            const char *w = p;
            while (w > text && w[-1] != ' ') w--;
            if ((*w == '-' || *w == '+') && memchr(w, 'o', (size_t)(p - w)) && (!p[9] || p[9] == ' '))
                r->pipefail = *w == '-';
            p += 9;
        }
    const char *s = unwrap(text);
    size_t n = strlen(s);
    while (n && is_space(s[n - 1])) n--;
    char *seg = xstrndup(s, n);
    bool hit = *seg && !regexec(r->re, seg, 0, NULL, 0);
    free(seg);
    if (!hit) return;
    if (r->root && !in_tree(r, dir, lost)) { r->hit |= CMD_AWAY; return; }
    r->hit |= CMD_RUN | (piped && !r->pipefail ? CMD_PIPED : 0);
}

int cmd_runs(const regex_t *re, const char *cmd, const char *root, const char *start) {
    char *rt = dup_or_null(root), *st = root ? path_join(root, start ? start : "") : NULL;
    if (rt) path_normalize(rt), path_normalize(st);
    Runs r = {re, rt, st, 0, false};
    G g = {.ud = &r, .seg = on_seg};
    lex(&g, cmd);
    free(rt), free(st);
    return r.hit;
}
