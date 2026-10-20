#ifndef AIG_SYNTAX_H
#define AIG_SYNTAX_H
#include <stdint.h>

/* Syntax highlighting with tree-sitter, from the grammars and highlight
 * queries Neovim has installed. Nothing is loaded until it's needed: a
 * language on its first file, a text on its first highlight. */

/* What a byte of code is, as the highlight queries capture it. */
typedef enum {
    SYN_NONE,
    SYN_KEYWORD,
    SYN_TAG,      /* markup tags (html, xml) */
    SYN_STRING,
    SYN_ESCAPE,   /* escapes, regexes, special strings */
    SYN_COMMENT,
    SYN_FUNCTION,
    SYN_TYPE,
    SYN_CONSTANT, /* numbers, booleans, constants */
    SYN_PROPERTY, /* fields, attributes, labels, modules */
    SYN_BUILTIN,  /* self, this, builtin variables */
    SYN_HEADING,
    SYN_STRONG,
    SYN_LINK,
    SYN_LIST,
} SynClass;

typedef struct SynLang SynLang;
typedef struct SynDoc SynDoc;

#define SYN_NAME_MAX 40
/* The grammar name for path ("c", "python", ...) by extension or file name,
 * whether or not it's installed; 0 when none. */
int syn_lang_name(const char *path, char name[SYN_NAME_MAX]);

/* The highlighter for path's language (by extension or file name), or NULL
 * when no grammar with a working query is installed. Loaded once and kept;
 * failures are remembered too. */
SynLang *syn_lang_for(const char *path);
/* Text to put before a fragment of a file (a diff's lines, a partial read)
 * that starts inside a code fence or block comment, so the rest parses as it
 * does in the file: a fence, a comment opener, or "". */
const char *syn_fragment_prefix(const SynLang *l, const char *text, uint32_t len);
/* A text to highlight; `text` must outlive the doc. Parsed on first use:
 * whole when small, a window around what's highlighted when big. */
SynDoc *syn_doc(SynLang *l, const char *text, uint32_t len);
/* The SynClass of each byte of text[start, end), into out[0 .. end-start). */
void syn_highlight(SynDoc *d, uint32_t start, uint32_t end, unsigned char *out);
void syn_doc_free(SynDoc *d);

/* Functions and classes of a text, from the @function.outer / @class.outer
 * captures of nvim-treesitter-textobjects' textobjects.scm for its language
 * (found next to the highlight queries). Outer before inner, in text order.
 * Safe from any thread. */
typedef struct {
    char *name;  /* its name field, or the end of its declarator chain (C) */
    int cls;     /* a class (struct, enum, ...), else a function */
    int a, b;    /* first and last line, 1-based */
    int parent;  /* index of the def it's inside, -1 none */
    int cx;      /* 1 + branch nodes inside it (nested defs included) */
    int depth;   /* deepest block nesting below its own body (0 = flat) */
} SynDef;
/* The last version of a file parsed, so its next version reparses
 * incrementally. One per file; not shared between threads. */
typedef struct SynParse SynParse;
SynParse *syn_parse_new(void);
void syn_parse_free(SynParse *p);
/* Number of defs into *out (free with syn_defs_free); 0 with *out NULL when
 * the language has no grammar or no textobjects query. prev (may be NULL):
 * the file's previous parse, reused and then replaced by this one. */
int syn_defs(const char *path, const char *text, uint32_t len, SynDef **out, SynParse *prev);
void syn_defs_free(SynDef *d, int n);

#endif
