#ifndef AIG_UTIL_H
#define AIG_UTIL_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

void *xmalloc(size_t n);
void *xcalloc(size_t n, size_t sz);
void *xrealloc(void *p, size_t n);
char *xstrdup(const char *s);
char *xstrndup(const char *s, size_t n);
static inline char *dup_or_null(const char *s) { return s ? xstrdup(s) : NULL; }
void strv_free(char **v, int n); /* v's n strings, then v */
/* Room for one more element at index n of a growable array p (capacity cap). */
#define GROW(p, n, cap)                                                   \
    do {                                                                  \
        if ((n) >= (cap)) {                                               \
            (cap) = (cap) ? (cap) * 2 : 16;                               \
            if ((cap) <= (n)) (cap) = (n) + 1;                            \
            (p) = xrealloc((p), (size_t)(cap) * sizeof *(p));             \
        }                                                                 \
    } while (0)
/* Append v to a growable array. */
#define PUSH(p, n, cap, v)                                                \
    do {                                                                  \
        GROW(p, n, cap);                                                  \
        (p)[(n)++] = (v);                                                 \
    } while (0)

char *xasprintf(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

/* Growable string, always NUL-terminated once anything is put in it. */
typedef struct {
    char *s;
    size_t n, cap;
} Buf;
void buf_put(Buf *b, const char *s, size_t n);
void buf_puts(Buf *b, const char *s);
void buf_free(Buf *b);

#define UTF8_CONT(c) (((unsigned char)(c) & 0xC0) == 0x80) /* a continuation byte */
/* Characters (code points) in the first n bytes of s, stopping at a NUL. */
static inline size_t utf8_count(const char *s, size_t n) {
    size_t c = 0;
    for (size_t i = 0; i < n && s[i]; i++) c += !UTF8_CONT(s[i]);
    return c;
}

/* a/b, or b alone when it is absolute or a is empty. */
char *path_join(const char *a, const char *b);
int ends_with(const char *s, const char *suf);
int cmp_str(const void *a, const void *b); /* qsort over char * */
int cmp_int(const void *a, const void *b); /* qsort over int */
static inline int is_space(char c) { return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f' || c == '\v'; }
/* Lines in a regular file (never blocking on a FIFO), counted over its first
 * `cap` bytes, the rest extrapolated when asked; size/50 for a binary; -1 when
 * it isn't a readable regular file. */
int file_lines(const char *path, long cap, bool extrapolate);
/* p (NULL or "": none) made absolute, from root or ~/ from $HOME, into out:
 * true when it is a regular file. */
bool file_at(const char *root, const char *p, char *out, size_t n);
/* The names in dir ending in suffix (NULL: any), dotfiles skipped, sorted: their count. */
int list_dir(const char *dir, const char *suffix, char ***out);
/* Lexically normalize an absolute path in place: collapse "//", drop ".",
 * resolve "..". */
void path_normalize(char *p);

double parse_iso8601(const char *s); /* epoch seconds, 0 when unparseable */
double now_wall(void);
void fmt_clock(char *buf, size_t n, double ts, const char *fmt); /* strftime, local time */
double jfix(double v, int digits); /* rounded as JavaScript's toFixed: half away from zero */

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
