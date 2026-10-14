#include "util.h"

#include <stdlib.h>
#include <string.h>
#include <time.h>

void *xmalloc(size_t n) {
    void *p = malloc(n ? n : 1);
    if (!p) { perror("malloc"); exit(1); }
    return p;
}

void *xcalloc(size_t n, size_t sz) {
    void *p = calloc(n ? n : 1, sz ? sz : 1);
    if (!p) { perror("calloc"); exit(1); }
    return p;
}

void *xrealloc(void *p, size_t n) {
    p = realloc(p, n ? n : 1);
    if (!p) { perror("realloc"); exit(1); }
    return p;
}

char *xstrdup(const char *s) { return xstrndup(s, strlen(s)); }

char *xstrndup(const char *s, size_t n) {
    char *d = xmalloc(n + 1);
    memcpy(d, s, n);
    d[n] = 0;
    return d;
}

double parse_iso8601(const char *s) {
    if (!s) return 0;
    struct tm tm = {0};
    double sec = 0;
    if (sscanf(s, "%d-%d-%dT%d:%d:%lf", &tm.tm_year, &tm.tm_mon, &tm.tm_mday,
               &tm.tm_hour, &tm.tm_min, &sec) != 6)
        return 0;
    tm.tm_year -= 1900;
    tm.tm_mon -= 1;
    tm.tm_sec = 0;
    return (double)timegm(&tm) + sec; /* transcripts are always UTC ("Z") */
}

double now_wall(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

int count_lines(const char *s, size_t n) {
    if (!s || !n) return 0;
    int c = 0;
    for (size_t i = 0; i < n; i++) c += s[i] == '\n';
    return c + (s[n - 1] != '\n');
}

char *first_line(const char *s, size_t max) {
    if (!s) return xstrdup("");
    while (*s == '\n' || *s == '\r' || *s == ' ' || *s == '\t') s++;
    size_t n = strcspn(s, "\r\n");
    while (n && (s[n - 1] == ' ' || s[n - 1] == '\t')) n--;
    if (n <= max) return xstrndup(s, n);
    size_t cut = max;
    while (cut && ((unsigned char)s[cut] & 0xC0) == 0x80) cut--; /* don't split a code point */
    char *d = xmalloc(cut + 4);
    memcpy(d, s, cut);
    memcpy(d + cut, "\xE2\x80\xA6", 4); /* … */
    return d;
}

/* Length of the valid UTF-8 sequence at s, 0 when invalid. */
static int utf8_len(const unsigned char *s) {
    int n = s[0] >= 0xC2 && s[0] <= 0xDF ? 2 : s[0] >= 0xE0 && s[0] <= 0xEF ? 3
          : s[0] >= 0xF0 && s[0] <= 0xF4 ? 4 : 0;
    for (int i = 1; i < n; i++)
        if ((s[i] & 0xC0) != 0x80) return 0;
    if (n == 3 && s[0] == 0xED && s[1] >= 0xA0) return 0; /* surrogate */
    if (n == 3 && s[0] == 0xE0 && s[1] < 0xA0) return 0;  /* overlong */
    if (n == 4 && s[0] == 0xF0 && s[1] < 0x90) return 0;
    if (n == 4 && s[0] == 0xF4 && s[1] >= 0x90) return 0;
    return n;
}

void json_str(FILE *f, const char *s) {
    fputc('"', f);
    for (; s && *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c >= 0x80) { /* pass valid UTF-8 through, replace the rest with U+FFFD */
            int n = utf8_len((const unsigned char *)s);
            if (n) { fwrite(s, 1, (size_t)n, f); s += n - 1; }
            else fputs("\xEF\xBF\xBD", f);
            continue;
        }
        if (c == '"' || c == '\\') { fputc('\\', f); fputc(c, f); }
        else if (c == '\n') fputs("\\n", f);
        else if (c == '\t') fputs("\\t", f);
        else if (c < 0x20) fprintf(f, "\\u%04x", c);
        else fputc(c, f);
    }
    fputc('"', f);
}

uint32_t hash_str(const char *s) {
    uint32_t h = 2166136261u; /* FNV-1a */
    for (; *s; s++) { h ^= (unsigned char)*s; h *= 16777619u; }
    return h;
}

int strmap_get(const StrMap *m, const char *key) {
    if (!m->cap) return -1;
    for (size_t i = hash_str(key) & (m->cap - 1);; i = (i + 1) & (m->cap - 1)) {
        if (!m->keys[i]) return -1;
        if (!strcmp(m->keys[i], key)) return m->vals[i];
    }
}

static void strmap_grow(StrMap *m) {
    StrMap g = {xcalloc(m->cap ? m->cap * 2 : 64, sizeof(char *)),
                xcalloc(m->cap ? m->cap * 2 : 64, sizeof(int)), m->cap ? m->cap * 2 : 64, 0};
    for (size_t i = 0; i < m->cap; i++) {
        if (!m->keys[i]) continue;
        size_t j = hash_str(m->keys[i]) & (g.cap - 1);
        while (g.keys[j]) j = (j + 1) & (g.cap - 1);
        g.keys[j] = m->keys[i];
        g.vals[j] = m->vals[i];
        g.n++;
    }
    free(m->keys);
    free(m->vals);
    *m = g;
}

void strmap_put(StrMap *m, const char *key, int val) {
    if ((m->n + 1) * 2 > m->cap) strmap_grow(m);
    size_t i = hash_str(key) & (m->cap - 1);
    while (m->keys[i] && strcmp(m->keys[i], key)) i = (i + 1) & (m->cap - 1);
    if (!m->keys[i]) { m->keys[i] = xstrdup(key); m->n++; }
    m->vals[i] = val;
}

void strmap_free(StrMap *m) {
    for (size_t i = 0; i < m->cap; i++) free(m->keys[i]);
    free(m->keys);
    free(m->vals);
    memset(m, 0, sizeof *m);
}
