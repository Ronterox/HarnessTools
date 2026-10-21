/* Syntax highlighting against Neovim's installed grammars; skipped when the
 * machine has none for a language. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "syntax.h"

static int fails, checks;

/* The class of the first byte of `word` in text should be want. */
static void expect(SynDoc *d, const char *text, const unsigned char *cls, const char *word, int want) {
    const char *p = strstr(text, word);
    checks++;
    if (!p) { printf("FAIL syntax: %s not in text\n", word); fails++; return; }
    if (cls[p - text] != want) { printf("FAIL syntax: %s is %d, want %d\n", word, cls[p - text], want); fails++; }
    (void)d;
}

/* text is a fragment of a file: parsed after its prefix, as the detail does. */
static void run(const char *path, const char *frag, const char *const *words, const int *want, int n) {
    SynLang *l = syn_lang_for(path);
    if (!l) { printf("skip syntax %s (no grammar installed)\n", path); return; }
    char buf[1024];
    snprintf(buf, sizeof buf, "%s%s", syn_fragment_prefix(l, frag, (uint32_t)strlen(frag)), frag);
    const char *text = buf;
    uint32_t len = (uint32_t)strlen(text);
    unsigned char cls[1024] = {0};
    SynDoc *d = syn_doc(l, text, len);
    /* Two halves, as the detail window highlights blocks of lines lazily. */
    syn_highlight(d, 0, len / 2, cls);
    syn_highlight(d, len / 2, len, cls + len / 2);
    for (int i = 0; i < n; i++) expect(d, text, cls, words[i], want[i]);
    syn_doc_free(d);
}

/* Defs as "name cls a-b parent cxN dN|" each. */
static void defs(const char *path, const char *text, const char *want) {
    SynDef *d;
    int n = syn_defs(path, text, (uint32_t)strlen(text), &d, NULL);
    if (!n) { printf("skip syntax defs %s (no textobjects query installed)\n", path); return; }
    char got[1024] = "";
    for (int i = 0; i < n; i++)
        snprintf(got + strlen(got), sizeof got - strlen(got), "%s %d %d-%d %d cx%d d%d|", d[i].name, d[i].cls, d[i].a,
                 d[i].b, d[i].parent, d[i].cx, d[i].depth);
    checks++;
    if (strcmp(got, want)) { printf("FAIL syntax defs %s:\n  got  %s\n  want %s\n", path, got, want); fails++; }
    syn_defs_free(d, n);
}

static int same_defs(const SynDef *x, int nx, const SynDef *y, int ny) {
    int same = nx == ny;
    for (int i = 0; same && i < nx; i++)
        same = !strcmp(x[i].name, y[i].name) && x[i].cls == y[i].cls && x[i].a == y[i].a && x[i].b == y[i].b &&
               x[i].parent == y[i].parent && x[i].cx == y[i].cx && x[i].depth == y[i].depth;
    return same;
}

/* A real file in n versions, each the file with one random line edit (a line
 * dropped, doubled, swapped with the next, or an unbalanced one put in): each
 * version's defs parsed from the version before are a fresh parse's. The
 * structure analysis caches defs by text alone (analysis.c, defs_of), so which
 * version was parsed before must not matter. */
static void edit_chain(const char *path, const char *junk, int n) {
    FILE *f = fopen(path, "rb");
    if (!f || !syn_lang_for(path)) {
        if (f) fclose(f);
        printf("skip syntax edit chain %s\n", path);
        return;
    }
    char *text = NULL, **line = NULL, *l = NULL;
    size_t cap = 0;
    int nl = 0;
    while (getline(&l, &cap, f) > 0) {
        line = realloc(line, (size_t)(nl + 1) * sizeof *line);
        line[nl++] = strdup(l);
    }
    free(l);
    fclose(f);
    size_t full = strlen(junk); /* room for the longest line added */
    for (int i = 0; i < nl; i++) full += 2 * strlen(line[i]);
    text = malloc(full + 1);
    SynParse *pp = syn_parse_new();
    unsigned seed = 12345;
    int bad = 0;
    for (int v = 0; v < n && nl > 2; v++) {
        seed = seed * 1103515245u + 12345u;
        int at = (int)((seed >> 8) % (unsigned)(nl - 1)), op = (int)((seed >> 4) % 4);
        size_t len = 0;
        for (int i = 0; i < nl; i++) {
            const char *a = line[i], *b = NULL; /* line i as this version has it, and one after it */
            if (i == at && op == 0) a = "";
            else if (i == at && op == 1) b = line[i];
            else if (i == at && op == 2) a = line[i + 1];
            else if (i == at + 1 && op == 2) a = line[at];
            else if (i == at && op == 3) b = junk;
            len += strlen(strcpy(text + len, a));
            if (b) len += strlen(strcpy(text + len, b));
        }
        SynDef *x, *y;
        int nx = syn_defs(path, text, (uint32_t)len, &x, pp), ny = syn_defs(path, text, (uint32_t)len, &y, NULL);
        bad += !same_defs(x, nx, y, ny);
        syn_defs_free(x, nx);
        syn_defs_free(y, ny);
    }
    checks++;
    if (bad) { printf("FAIL syntax edit chain %s: %d of %d versions reparsed differ\n", path, bad, n); fails++; }
    syn_parse_free(pp);
    for (int i = 0; i < nl; i++) free(line[i]);
    free(line);
    free(text);
}

int main(void) {
    const char *c = "/* note */\nstatic int main(void) {\n    const char *s = \"hi\\n\";\n    return 42 + MAX_N;\n}\n";
    const char *cw[] = {"/* note", "static", "int", "main", "\"hi", "\\n", "return", "42", "MAX_N"};
    const int cc[] = {SYN_COMMENT, SYN_KEYWORD, SYN_TYPE, SYN_FUNCTION, SYN_STRING, SYN_ESCAPE, SYN_KEYWORD, SYN_CONSTANT, SYN_CONSTANT};
    run("src/a.c", c, cw, cc, 9);
    const char *py = "import os\n\ndef f(self, x):\n    # c\n    return None if x else 'y'\n";
    const char *pw[] = {"import", "def", "f(", "self", "# c", "None", "'y'"};
    const int pc[] = {SYN_KEYWORD, SYN_KEYWORD, SYN_FUNCTION, SYN_BUILTIN, SYN_COMMENT, SYN_CONSTANT, SYN_STRING};
    run("x/b.py", py, pw, pc, 7);
    /* Fragments starting inside a block comment or a code fence. */
    const char *cf = "   still a comment */\nint x;\n";
    const char *cfw[] = {"still", "int"};
    const int cfc[] = {SYN_COMMENT, SYN_TYPE};
    run("a.h", cf, cfw, cfc, 2);
    const char *md = "tail of code\n```\n\n## Heading\n\n```sh\nls\n```\n";
    const char *mdw[] = {"tail", "## Heading"};
    const int mdc[] = {SYN_STRING, SYN_HEADING};
    run("README.md", md, mdw, mdc, 2);
    /* A big text is parsed in windows: offsets must stay the text's own. */
    SynLang *cl = syn_lang_for("big.c");
    if (cl) {
        const char *unit = "static int f(void) { return \"s\"; } // c\n";
        size_t ul = strlen(unit), reps = (2u << 20) / ul;
        char *big = malloc(ul * reps + 1);
        for (size_t i = 0; i < reps; i++) memcpy(big + i * ul, unit, ul);
        big[ul * reps] = 0;
        SynDoc *d = syn_doc(cl, big, (uint32_t)(ul * reps));
        unsigned char cls[256];
        size_t at[] = {reps - 1, reps / 2, 3}; /* the end, the middle, back to the start */
        for (int k = 0; k < 3; k++) {
            uint32_t a = (uint32_t)(at[k] * ul);
            syn_highlight(d, a, a + (uint32_t)ul, cls);
            const int want[] = {SYN_KEYWORD, SYN_FUNCTION, SYN_KEYWORD, SYN_STRING, SYN_COMMENT};
            const char *word[] = {"static", "f(", "return", "\"s", "// c"};
            for (int w = 0; w < 5; w++) {
                checks++;
                int got = cls[strstr(unit, word[w]) - unit];
                if (got != want[w]) { printf("FAIL syntax window %d: %s is %d, want %d\n", k, word[w], got, want[w]); fails++; }
            }
        }
        syn_doc_free(d);
        free(big);
    }
    if (syn_lang_for("notes.unknownext") || syn_lang_for("README")) { printf("FAIL syntax: language for no grammar\n"); fails++; }
    /* Functions and classes from the textobjects queries. */
    const char *cdefs = "int proto(void);\n"
                        "static char *name(int a, int b) {\n    if (a && b) return 0;\n    for (;;) { while (a) a--; }\n    return a ? 0 : 0;\n}\n"
                        "typedef struct {\n    int x;\n} Point;\n";
    const char *cwant = "name 0 2-6 -1 cx6 d1|Point 1 7-9 -1 cx1 d0|";
    defs("src/a.c", cdefs, cwant);
    const char *pydefs = "class A:\n    @property\n    def x(self):\n        def inner():\n            return [a for a in b if a or c]\n        return 1\n\n"
                         "def top():\n    try:\n        pass\n    except E:\n        pass\n";
    const char *pywant = "A 1 1-6 -1 cx4 d2|x 0 3-6 0 cx4 d1|inner 0 4-5 1 cx4 d0|top 0 8-12 -1 cx2 d1|";
    defs("x/b.py", pydefs, pywant);
    /* The previous version's parse reused: the same defs as a fresh parse. */
    SynParse *pp = syn_parse_new();
    const char *versions[] = {cdefs, "int proto(void);\nint added(void) { return 1; }\n",
                              "int added(void) { return 1; }\nstatic char *name(int a) {\n    return 0;\n}\n", cdefs};
    for (int v = 0; v < 4 && syn_lang_for("src/a.c"); v++) {
        SynDef *x, *y;
        int nx = syn_defs("src/a.c", versions[v], (uint32_t)strlen(versions[v]), &x, pp);
        int ny = syn_defs("src/a.c", versions[v], (uint32_t)strlen(versions[v]), &y, NULL);
        if (!ny) { /* no grammar or query installed: nothing to compare */
            syn_defs_free(x, nx);
            continue;
        }
        checks++;
        if (!same_defs(x, nx, y, ny)) { printf("FAIL syntax defs: version %d reparsed differs\n", v); fails++; }
        syn_defs_free(x, nx);
        syn_defs_free(y, ny);
    }
    syn_parse_free(pp);
    edit_chain("src/analysis.c", "    if (x) {\n", 25);
    edit_chain("draft/build_data.py", "        if x:\n", 25);
    if (!fails) printf("ok   syntax (%d checks)\n", checks);
    return fails != 0;
}
