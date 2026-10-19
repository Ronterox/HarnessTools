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

#endif
