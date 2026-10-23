#include "nvim.h"

#include <dirent.h>
#include <errno.h>
#include <limits.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

#include "util.h"

#define CACHE_SECS 2.0   /* a discovery stays good this long */
#define REPLY_MS 3000    /* nv_open waits this long for nvim's reply: a first :edit may start plugins, an LSP */
#define REPLY_MAX 65536  /* bytes of a reply read */
#define NCAND 8          /* matching instances kept, newest first */

/* A special current window (a terminal, a file tree, quickfix, a float,
 * 'winfixbuf') gives way to the tab's first window with a normal buffer;
 * then :edit there (no tab, no split) unless it already shows the file, as
 * a structured command: the path is one argument, never Ex text, so no
 * character in it ("|", a newline) can start another command. Then the
 * cursor to the line (clamped: the file may have shrunk since the step),
 * centred. */
static const char OPEN_LUA[] =
    "local path, line = ...\n"
    "local function plain(w)\n"
    "  local ok, fix = pcall(function() return vim.wo[w].winfixbuf end)\n" /* 0.10+ */
    "  return vim.bo[vim.api.nvim_win_get_buf(w)].buftype == '' and not (ok and fix)\n"
    "    and vim.api.nvim_win_get_config(w).relative == ''\n"
    "end\n"
    "if not plain(0) then\n"
    "  for _, w in ipairs(vim.api.nvim_tabpage_list_wins(0)) do\n"
    "    if plain(w) then vim.api.nvim_set_current_win(w) break end\n"
    "  end\n"
    "end\n"
    "local cur = vim.api.nvim_buf_get_name(0)\n"
    "if cur == '' or vim.fn.resolve(cur) ~= vim.fn.resolve(path) then\n"
    "  vim.cmd.edit({args = {path}, magic = {file = false, bar = false}})\n"
    "end\n"
    "local last = vim.api.nvim_buf_line_count(0)\n"
    "vim.api.nvim_win_set_cursor(0, {math.max(1, math.min(line, last)), 0})\n"
    "vim.cmd('normal! zz')\n";

/* ---- discovery ------------------------------------------------------------ */

/* When pid started (clock ticks since boot: /proc/<pid>/stat's 22nd field), 0 unknown. */
static unsigned long long started(int pid) {
    char p[64], buf[1024];
    snprintf(p, sizeof p, "/proc/%d/stat", pid);
    FILE *f = fopen(p, "r");
    if (!f) return 0;
    size_t n = fread(buf, 1, sizeof buf - 1, f);
    fclose(f);
    buf[n] = 0;
    char *s = strrchr(buf, ')'); /* the name may hold spaces and parens */
    if (!s) return 0;
    s++;
    for (int field = 3; field < 22 && s; field++) s = strchr(s + 1, ' '); /* to the space before field 22 */
    return s ? strtoull(s + 1, NULL, 10) : 0;
}

/* Is pid an nvim (not a stale socket's pid reused by something else)? */
static bool is_nvim(int pid) {
    char p[64], comm[32] = "";
    snprintf(p, sizeof p, "/proc/%d/comm", pid);
    FILE *f = fopen(p, "r");
    if (!f) return false;
    bool ok = fgets(comm, sizeof comm, f) && !strncmp(comm, "nvim", 4);
    fclose(f);
    return ok;
}

typedef struct {
    char sock[PATH_MAX];
    unsigned long long t;
} Cand;

static int cmp_newest(const void *a, const void *b) {
    unsigned long long x = ((const Cand *)a)->t, y = ((const Cand *)b)->t;
    return x < y ? 1 : x > y ? -1 : strcmp(((const Cand *)a)->sock, ((const Cand *)b)->sock);
}

/* The sockets of the nvims whose cwd is root, newest first (at most NCAND), found now. */
static int scan(const char *root, Cand *c) {
    char want[PATH_MAX];
    const char *dir = getenv("AIG_NVIM_DIR");
    if (!dir || !*dir) dir = getenv("XDG_RUNTIME_DIR");
    if (!dir || !*dir || !realpath(root, want)) return 0;
    DIR *d = opendir(dir);
    if (!d) return 0;
    Cand all[64];
    int n = 0;
    struct dirent *de;
    while ((de = readdir(d)) && n < 64) {
        int pid = 0, idx = 0, end = 0;
        if (sscanf(de->d_name, "nvim.%d.%d%n", &pid, &idx, &end) != 2 || de->d_name[end] || pid <= 0) continue;
        char p[PATH_MAX], cwd[PATH_MAX];
        snprintf(p, sizeof p, "/proc/%d/cwd", pid);
        ssize_t len = readlink(p, cwd, sizeof cwd - 1); /* the kernel's canonical path: not resolved again */
        if (len <= 0) continue; /* gone, or not ours */
        cwd[len] = 0;
        if (strcmp(cwd, want) || !is_nvim(pid)) continue;
        struct stat st;
        if (snprintf(p, sizeof p, "%s/%s", dir, de->d_name) >= (int)sizeof p || stat(p, &st) || !S_ISSOCK(st.st_mode))
            continue;
        snprintf(all[n].sock, sizeof all[n].sock, "%s", p);
        all[n++].t = started(pid);
    }
    closedir(d);
    qsort(all, (size_t)n, sizeof *all, cmp_newest);
    if (n > NCAND) n = NCAND;
    memcpy(c, all, (size_t)n * sizeof *c);
    return n;
}

/* The render thread's: one root's answer and when it was found. */
static struct {
    char root[PATH_MAX];
    Cand c[NCAND];
    int n;
    double t;
} cache;

static int find(const char *root) {
    double now = now_wall();
    if (strcmp(cache.root, root) || now - cache.t > CACHE_SECS || now < cache.t) {
        snprintf(cache.root, sizeof cache.root, "%s", root);
        cache.n = scan(root, cache.c);
        cache.t = now;
    }
    return cache.n;
}

bool nv_find(const char *root, char *sock, size_t n) {
    if (!find(root)) return false;
    if (sock) snprintf(sock, n, "%s", cache.c[0].sock);
    return true;
}

/* ---- msgpack: just what one request and its reply need ------------------- */

static void put(Buf *o, const void *p, size_t n) { buf_put(o, p, n); }

static void put_be(Buf *o, unsigned char tag, uint64_t v, int bytes) {
    unsigned char t[9] = {tag};
    for (int i = 0; i < bytes; i++) t[1 + i] = (unsigned char)(v >> (8 * (bytes - 1 - i)));
    put(o, t, (size_t)bytes + 1);
}

static void put_array(Buf *o, uint32_t n) {
    if (n < 16) put_be(o, (unsigned char)(0x90 | n), 0, 0);
    else put_be(o, 0xdd, n, 4);
}

static void put_int(Buf *o, long long v) {
    if (v >= 0 && v < 128) put_be(o, (unsigned char)v, 0, 0);
    else if (v >= 0) put_be(o, 0xcf, (uint64_t)v, 8);
    else put_be(o, 0xd3, (uint64_t)v, 8);
}

static void put_str(Buf *o, const char *s) {
    size_t n = strlen(s);
    if (n < 32) put_be(o, (unsigned char)(0xa0 | n), 0, 0);
    else put_be(o, 0xdb, n, 4);
    put(o, s, n);
}

/* A reader over a reply: each call false when the data runs out (more to
 * read) or, with bad set, isn't msgpack. */
typedef struct {
    const unsigned char *p, *end;
    bool bad;
} Rd;

static bool get_be(Rd *r, int bytes, uint64_t *v) {
    if (r->end - r->p < bytes) return false;
    *v = 0;
    for (int i = 0; i < bytes; i++) *v = *v << 8 | *r->p++;
    return true;
}

/* One value's header: its family ('n' nil, 'b' bool, 'i' int, 'f' float,
 * 's' str/bin/ext, 'a' array, 'm' map), its integer value or length. */
static bool get_head(Rd *r, char *fam, uint64_t *v) {
    if (r->p >= r->end) return false;
    unsigned char c = *r->p++;
    *v = 0;
    if (c < 0x80) { *fam = 'i'; *v = c; return true; }
    if (c >= 0xe0) { *fam = 'i'; *v = (uint64_t)(int64_t)(int8_t)c; return true; }
    if ((c & 0xf0) == 0x80) { *fam = 'm'; *v = c & 15; return true; }
    if ((c & 0xf0) == 0x90) { *fam = 'a'; *v = c & 15; return true; }
    if ((c & 0xe0) == 0xa0) { *fam = 's'; *v = c & 31; return true; }
    switch (c) {
    case 0xc0: *fam = 'n'; return true;
    case 0xc2: case 0xc3: *fam = 'b'; *v = c & 1; return true;
    case 0xc4: case 0xd9: *fam = 's'; return get_be(r, 1, v);
    case 0xc5: case 0xda: *fam = 's'; return get_be(r, 2, v);
    case 0xc6: case 0xdb: *fam = 's'; return get_be(r, 4, v);
    case 0xc7: *fam = 's'; if (!get_be(r, 1, v)) return false; *v += 1; return true; /* ext: its type byte too */
    case 0xc8: *fam = 's'; if (!get_be(r, 2, v)) return false; *v += 1; return true;
    case 0xc9: *fam = 's'; if (!get_be(r, 4, v)) return false; *v += 1; return true;
    case 0xca: *fam = 'f'; return get_be(r, 4, v);
    case 0xcb: *fam = 'f'; return get_be(r, 8, v);
    case 0xcc: *fam = 'i'; return get_be(r, 1, v);
    case 0xcd: *fam = 'i'; return get_be(r, 2, v);
    case 0xce: *fam = 'i'; return get_be(r, 4, v);
    case 0xcf: *fam = 'i'; return get_be(r, 8, v);
    case 0xd0: *fam = 'i'; if (!get_be(r, 1, v)) return false; *v = (uint64_t)(int64_t)(int8_t)*v; return true;
    case 0xd1: *fam = 'i'; if (!get_be(r, 2, v)) return false; *v = (uint64_t)(int64_t)(int16_t)*v; return true;
    case 0xd2: *fam = 'i'; if (!get_be(r, 4, v)) return false; *v = (uint64_t)(int64_t)(int32_t)*v; return true;
    case 0xd3: *fam = 'i'; return get_be(r, 8, v);
    case 0xd4: *fam = 's'; *v = 2; return true; /* fixext 1..16: type byte + data */
    case 0xd5: *fam = 's'; *v = 3; return true;
    case 0xd6: *fam = 's'; *v = 5; return true;
    case 0xd7: *fam = 's'; *v = 9; return true;
    case 0xd8: *fam = 's'; *v = 17; return true;
    case 0xdc: *fam = 'a'; return get_be(r, 2, v);
    case 0xdd: *fam = 'a'; return get_be(r, 4, v);
    case 0xde: *fam = 'm'; return get_be(r, 2, v);
    case 0xdf: *fam = 'm'; return get_be(r, 4, v);
    default: r->bad = true; return false; /* 0xc1: never used */
    }
}

/* One whole value, skipped; a string's bytes into out (n > 0) when it is one. */
static bool get_value(Rd *r, char *out, size_t n, int depth) {
    char fam;
    uint64_t v;
    if (depth > 32) r->bad = true;
    if (r->bad || !get_head(r, &fam, &v)) return false;
    if (fam == 's') {
        if ((uint64_t)(r->end - r->p) < v) return false;
        if (n) snprintf(out, n, "%.*s", (int)(v < n ? v : n - 1), (const char *)r->p);
        r->p += v;
        return true;
    }
    uint64_t kids = fam == 'a' ? v : fam == 'm' ? 2 * v : 0;
    for (uint64_t i = 0; i < kids; i++)
        if (!get_value(r, NULL, 0, depth + 1)) return false;
    return true;
}

/* The first message in b[0..len), whole: our reply [1, id, error, result]
 * gives 1 ok (a string result into out) or -1 an error ([type, message]: the
 * message into out); any other message 2 (skip *used bytes); 0 incomplete;
 * -2 not msgpack-RPC. */
static int get_reply(const unsigned char *b, size_t len, uint32_t id, char *out, size_t n, size_t *used) {
    Rd w = {b, b + len, false};
    if (!get_value(&w, NULL, 0, 0)) return w.bad ? -2 : 0;
    *used = (size_t)(w.p - b);
    Rd r = {b, w.p, false};
    char fam;
    uint64_t v, type, msgid;
    if (!get_head(&r, &fam, &v) || fam != 'a' || !v) return -2;
    if (!get_head(&r, &fam, &type) || fam != 'i') return -2;
    if (type != 1) return 2; /* a request or notification: not for us */
    if (v != 4 || !get_head(&r, &fam, &msgid) || fam != 'i') return -2;
    if (msgid != id) return 2;
    Rd e = r;
    get_head(&e, &fam, &v); /* the message is whole: these can't run out */
    out[0] = 0;
    if (fam == 'n') { /* no error: the result */
        get_value(&e, out, n, 0);
        return 1;
    }
    if (fam == 'a') /* [type, message] */
        for (uint64_t i = 0; i < v; i++) get_value(&e, i == 1 ? out : NULL, i == 1 ? n : 0, 1);
    else get_value(&r, out, n, 0);
    if (!out[0]) snprintf(out, n, "error");
    return -1;
}

/* ---- the call --------------------------------------------------------------- */

/* The worker: the latest request, and the socket of the call in flight (cut
 * off at shutdown). */
typedef struct {
    char *socks[NCAND], *path, *label;
    int nsocks, line;
} Job;

static struct {
    pthread_mutex_t mu;
    pthread_cond_t cv;
    pthread_t th;
    bool started, quit;
    Job *next;
    int fd;
} W = {PTHREAD_MUTEX_INITIALIZER, PTHREAD_COND_INITIALIZER, .fd = -1};

static int call(const char *sock, const char *code, const char *path, int line, int timeout_ms, char *out, size_t n,
                bool worker) {
    static uint32_t next_id;
    uint32_t id = __atomic_add_fetch(&next_id, 1, __ATOMIC_RELAXED);
    struct sockaddr_un a = {.sun_family = AF_UNIX};
    if (strlen(sock) >= sizeof a.sun_path) {
        snprintf(out, n, "socket path too long");
        return NV_E_CONNECT;
    }
    memcpy(a.sun_path, sock, strlen(sock) + 1);
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) {
        snprintf(out, n, "%s", strerror(errno));
        return NV_E_CONNECT;
    }
    struct timeval tv = {timeout_ms / 1000, timeout_ms % 1000 * 1000};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
    if (connect(fd, (struct sockaddr *)&a, sizeof a)) {
        snprintf(out, n, "%s", strerror(errno));
        close(fd);
        return NV_E_CONNECT;
    }
    if (worker) {
        pthread_mutex_lock(&W.mu);
        if (W.quit) shutdown(fd, SHUT_RDWR);
        W.fd = fd;
        pthread_mutex_unlock(&W.mu);
    }
    Buf q = {0};
    put_array(&q, 4);
    put_int(&q, 0);
    put_int(&q, id);
    put_str(&q, "nvim_exec_lua");
    put_array(&q, 2);
    put_str(&q, code);
    put_array(&q, 2);
    put_str(&q, path ? path : "");
    put_int(&q, line);
    int rc = 1; /* 1 while waiting */
    for (size_t off = 0; off < q.n;) {
        ssize_t k = send(fd, q.s + off, q.n - off, MSG_NOSIGNAL);
        if (k <= 0) {
            snprintf(out, n, "%s", k < 0 ? strerror(errno) : "closed");
            rc = NV_E_NVIM;
            break;
        }
        off += (size_t)k;
    }
    free(q.s);
    /* The reply, read until it parses whole, by the deadline. */
    unsigned char *b = xmalloc(REPLY_MAX);
    size_t len = 0;
    double until = now_wall() + timeout_ms / 1000.0;
    while (rc == 1) {
        if (len == REPLY_MAX) {
            snprintf(out, n, "reply too large");
            rc = NV_E_NVIM;
            break;
        }
        ssize_t k = recv(fd, b + len, REPLY_MAX - len, 0);
        if (k < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            snprintf(out, n, "no reply in %d ms", timeout_ms);
            rc = NV_E_TIMEOUT;
        } else if (k <= 0) {
            snprintf(out, n, "%s", k < 0 ? strerror(errno) : "closed the connection");
            rc = NV_E_NVIM;
        }
        if (k <= 0) break;
        len += (size_t)k;
        for (size_t used = 0;;) { /* whole messages: ours ends it, others are dropped */
            int got = get_reply(b, len, id, out, n, &used);
            if (got == 2) {
                memmove(b, b + used, len - used);
                len -= used;
                continue;
            }
            if (got == 1) rc = 0;
            else if (got == -1) rc = NV_E_NVIM;
            else if (got == -2) snprintf(out, n, "not a msgpack-RPC reply"), rc = NV_E_NVIM;
            break;
        }
        if (rc == 1 && now_wall() > until) {
            snprintf(out, n, "no reply in %d ms", timeout_ms);
            rc = NV_E_TIMEOUT;
        }
    }
    free(b);
    if (worker) {
        pthread_mutex_lock(&W.mu);
        W.fd = -1;
        pthread_mutex_unlock(&W.mu);
    }
    close(fd);
    return rc;
}

int nv_exec(const char *sock, const char *code, const char *path, int line, int timeout_ms, char *out, size_t n) {
    return call(sock, code, path, line, timeout_ms, out, n, false);
}

int nv_open_sync(const char *sock, const char *path, int line, char *err, size_t n) {
    return call(sock, OPEN_LUA, path, line, REPLY_MS, err, n, false);
}

/* ---- opening, off the render thread ---------------------------------------- */

static pthread_mutex_t hint_mu = PTHREAD_MUTEX_INITIALIZER;
static char hint[PATH_MAX + 64];
static double hint_t = -1e18;
static int hint_kind;

static void set_hint(const char *s, int kind) {
    pthread_mutex_lock(&hint_mu);
    snprintf(hint, sizeof hint, "%s", s);
    hint_t = now_wall();
    hint_kind = kind;
    pthread_mutex_unlock(&hint_mu);
}

bool nv_hint(char *buf, size_t n, double *age, int *kind) {
    pthread_mutex_lock(&hint_mu);
    double a = now_wall() - hint_t;
    bool fresh = a >= 0 && a < NV_HINT_SECS && hint[0];
    if (fresh) snprintf(buf, n, "%s", hint);
    if (kind) *kind = hint_kind;
    pthread_mutex_unlock(&hint_mu);
    if (age) *age = a;
    return fresh;
}

static void job_free(Job *j) {
    if (!j) return;
    for (int i = 0; i < j->nsocks; i++) free(j->socks[i]);
    free(j->path);
    free(j->label);
    free(j);
}

/* One request: the instances newest first, the next while one can't be reached. */
static void run(Job *j) {
    char err[512], msg[700];
    int rc = NV_E_CONNECT;
    for (int i = 0; i < j->nsocks && rc == NV_E_CONNECT; i++) rc = call(j->socks[i], OPEN_LUA, j->path, j->line, REPLY_MS,
                                                                       err, sizeof err, true);
    if (!rc) {
        snprintf(msg, sizeof msg, "nvim: %s:%d", j->label, j->line);
    } else if (rc == NV_E_TIMEOUT) {
        snprintf(msg, sizeof msg, "nvim: %s:%d sent; nvim is busy", j->label, j->line);
    } else {
        /* "Error executing lua: …: Vim(edit):E37: No write…\nstack traceback…": from the E-number, its line */
        const char *e = err;
        for (const char *p = err; *p; p++)
            if (p[0] == 'E' && p[1] >= '0' && p[1] <= '9' && (p == err || p[-1] == ':' || p[-1] == ' ')) {
                e = p;
                break;
            }
        snprintf(msg, sizeof msg, "nvim: %.*s", (int)strcspn(e, "\n"), e);
    }
    set_hint(msg, !rc ? NV_OK : rc == NV_E_TIMEOUT ? NV_BUSY : NV_ERR);
}

static void *worker(void *arg) {
    (void)arg;
    pthread_mutex_lock(&W.mu);
    for (;;) {
        while (!W.next && !W.quit) pthread_cond_wait(&W.cv, &W.mu);
        if (W.quit) break;
        Job *j = W.next;
        W.next = NULL;
        pthread_mutex_unlock(&W.mu);
        run(j);
        job_free(j);
        pthread_mutex_lock(&W.mu);
    }
    pthread_mutex_unlock(&W.mu);
    return NULL;
}

void nv_open(const char *root, const char *path, int line) {
    char msg[PATH_MAX + 32];
    int nc = find(root);
    if (!nc) {
        snprintf(msg, sizeof msg, "no nvim open in %s", root);
        set_hint(msg, NV_ERR);
        return;
    }
    size_t rl = strlen(root);
    const char *label = !strncmp(path, root, rl) && path[rl] == '/' ? path + rl + 1 : path;
    Job *j = xmalloc(sizeof *j);
    *j = (Job){.nsocks = nc, .path = xstrdup(path), .label = xstrdup(label), .line = line};
    for (int i = 0; i < nc; i++) j->socks[i] = xstrdup(cache.c[i].sock);
    pthread_mutex_lock(&W.mu);
    if (W.quit) {
        pthread_mutex_unlock(&W.mu);
        job_free(j);
        return;
    }
    if (!W.started) W.started = !pthread_create(&W.th, NULL, worker, NULL);
    if (!W.started) { /* no thread: run it here */
        pthread_mutex_unlock(&W.mu);
        run(j);
        job_free(j);
        return;
    }
    job_free(W.next); /* not sent yet: this click replaces it */
    W.next = j;
    pthread_cond_signal(&W.cv);
    pthread_mutex_unlock(&W.mu);
}

void nv_shutdown(void) {
    pthread_mutex_lock(&W.mu);
    W.quit = true;
    if (W.fd >= 0) shutdown(W.fd, SHUT_RDWR);
    job_free(W.next);
    W.next = NULL;
    bool join = W.started;
    pthread_cond_signal(&W.cv);
    pthread_mutex_unlock(&W.mu);
    if (join) pthread_join(W.th, NULL);
    W.started = false;
}
