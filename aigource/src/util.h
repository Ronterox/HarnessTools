#ifndef AIG_UTIL_H
#define AIG_UTIL_H
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

void *xmalloc(size_t n);
void *xcalloc(size_t n, size_t sz);
void *xrealloc(void *p, size_t n);
char *xstrdup(const char *s);
char *xstrndup(const char *s, size_t n);

double parse_iso8601(const char *s); /* epoch seconds, 0 when unparseable */
double now_wall(void);

/* Lines in a text buffer: newlines, plus one for an unterminated last line. */
int count_lines(const char *s, size_t n);
/* First non-empty line of s, at most max bytes (UTF-8 safe, "…" when cut). */
char *first_line(const char *s, size_t max);
void json_str(FILE *f, const char *s); /* writes a quoted, escaped JSON string */

uint32_t hash_str(const char *s);

/* Open-addressing string -> int map. Keys are copied. */
typedef struct {
    char **keys;
    int *vals;
    size_t cap, n;
} StrMap;
int strmap_get(const StrMap *m, const char *key); /* -1 when absent */
void strmap_put(StrMap *m, const char *key, int val);
void strmap_free(StrMap *m);

#endif
