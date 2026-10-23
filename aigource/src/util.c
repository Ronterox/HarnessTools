#include "util.h"

#include <dirent.h>
#include <fcntl.h>
#include <math.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

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

void strv_free(char **v, int n) {
    for (int i = 0; v && i < n; i++) free(v[i]);
    free(v);
}

char *xasprintf(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(NULL, 0, fmt, ap);
    va_end(ap);
    char *s = xmalloc((size_t)n + 1);
    va_start(ap, fmt);
    vsnprintf(s, (size_t)n + 1, fmt, ap);
    va_end(ap);
    return s;
}

void buf_put(Buf *b, const char *s, size_t n) {
    if (b->n + n + 1 > b->cap) {
        b->cap = (b->n + n + 1) * 2;
        b->s = xrealloc(b->s, b->cap);
    }
    memcpy(b->s + b->n, s, n);
    b->n += n;
    b->s[b->n] = 0;
}

void buf_puts(Buf *b, const char *s) { buf_put(b, s, strlen(s)); }

void buf_free(Buf *b) {
    free(b->s);
    memset(b, 0, sizeof *b);
}

char *path_join(const char *a, const char *b) {
    if (b[0] == '/' || !*a) return xstrdup(b);
    return xasprintf("%s%s%s", a, a[strlen(a) - 1] == '/' ? "" : "/", b);
}

int ends_with(const char *s, const char *suf) {
    size_t n = strlen(s), m = strlen(suf);
    return n >= m && !strcmp(s + n - m, suf);
}

int cmp_str(const void *a, const void *b) { return strcmp(*(char *const *)a, *(char *const *)b); }

int cmp_int(const void *pa, const void *pb) {
    int a = *(const int *)pa, b = *(const int *)pb;
    return (a > b) - (a < b);
}

int file_lines(const char *path, long cap, bool extrapolate) {
    int fd = open(path, O_RDONLY | O_NONBLOCK);
    if (fd < 0) return -1;
    struct stat st;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode)) { close(fd); return -1; }
    char buf[1 << 16];
    long lines = 0, bytes = 0;
    int last = '\n', binary = 0;
    ssize_t n;
    while ((n = read(fd, buf, sizeof buf)) > 0 && bytes < cap) {
        for (ssize_t i = 0; i < n; i++) { lines += buf[i] == '\n'; binary |= !buf[i]; }
        last = buf[n - 1];
        bytes += n;
    }
    close(fd);
    if (binary) return (int)(st.st_size / 50); /* rough visual size */
    if (extrapolate && st.st_size > bytes && bytes) lines = (long)((double)lines * st.st_size / bytes);
    return (int)(lines + (bytes && last != '\n'));
}

bool file_at(const char *root, const char *p, char *out, size_t n) {
    if (!p || !*p) return false;
    if (p[0] == '/') snprintf(out, n, "%s", p);
    else if (!strncmp(p, "~/", 2)) snprintf(out, n, "%s/%s", getenv("HOME") ? getenv("HOME") : "", p + 2);
    else snprintf(out, n, "%s/%s", root, p);
    struct stat st;
    return !stat(out, &st) && S_ISREG(st.st_mode);
}

int list_dir(const char *dir, const char *suffix, char ***out) {
    DIR *d = opendir(dir);
    struct dirent *de;
    char **v = NULL;
    int n = 0, cap = 0;
    while (d && (de = readdir(d)))
        if (de->d_name[0] != '.' && (!suffix || ends_with(de->d_name, suffix))) PUSH(v, n, cap, xstrdup(de->d_name));
    if (d) closedir(d);
    if (n > 1) qsort(v, (size_t)n, sizeof *v, cmp_str);
    *out = v;
    return n;
}

void path_normalize(char *p) {
    if (*p != '/') return; /* only absolute paths: a relative one would grow */
    char *out = p, *in = p;
    while (*in) {
        while (*in == '/') in++;
        char *seg = in;
        while (*in && *in != '/') in++;
        size_t n = (size_t)(in - seg);
        if (!n || (n == 1 && seg[0] == '.')) continue;
        if (n == 2 && seg[0] == '.' && seg[1] == '.') {
            while (out > p && *--out != '/') {}
            continue;
        }
        *out++ = '/';
        memmove(out, seg, n);
        out += n;
    }
    if (out == p) *out++ = '/';
    *out = 0;
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

void fmt_clock(char *buf, size_t n, double ts, const char *fmt) {
    time_t t = (time_t)ts;
    struct tm tm;
    localtime_r(&t, &tm);
    strftime(buf, n, fmt, &tm);
}

double jfix(double v, int digits) {
    double p = pow(10, digits), a = floor(fabs(v) * p + 0.5) / p;
    return v < 0 ? -a : a;
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
    while (cut && UTF8_CONT(s[cut])) cut--; /* don't split a code point */
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
