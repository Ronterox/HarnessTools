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
    if (!fails) printf("ok   syntax (%d checks)\n", checks);
    return fails != 0;
}
