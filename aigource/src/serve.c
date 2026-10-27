#include "serve.h"

#include <errno.h>
#include <limits.h>
#include <math.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include "analysis.h"
#include "engine.h"
#include "metrics.h"
#include "util.h"
#include "yyjson.h"

#define QUIET 1.0             /* rebuild this long after the last create, edit, delete or turn... */
#define CAP 3.0               /* ...or once the first of them waited this long */
#define IDLE 300.0            /* seconds with no client before exiting (AIG_SERVE_IDLE) */
#define TICK_MS 50            /* the loop wakes at least this often: new events, a finished build */
#define LINE_MAX_IN (1 << 20) /* a request longer than this drops its client */
#define OUT_MAX (256 << 20)   /* replies a client left unread past this drop it */
#define OUT_KEEP (1 << 20)    /* sent bytes kept at the head of a client's queue before it's compacted */
#define EOF_WAIT 5.0          /* a half-closed client's replies get this long to go out */
#define ACCEPT_PAUSE 0.1      /* out of descriptors: stop accepting this long */

/* SHA-1 (FIPS 180-4) of n bytes as 40 hex digits: the socket's name. */
static void sha1_hex(const char *msg, size_t n, char out[41]) {
    uint32_t h[5] = {0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0};
    size_t total = ((n + 8) / 64 + 1) * 64; /* the message, 0x80, zeros, its length in bits */
    unsigned char *m = xcalloc(total, 1);
    memcpy(m, msg, n);
    m[n] = 0x80;
    for (int i = 0; i < 8; i++) m[total - 1 - i] = (unsigned char)((uint64_t)n * 8 >> 8 * i);
    for (size_t at = 0; at < total; at += 64) {
        uint32_t w[80];
        for (int i = 0; i < 16; i++) {
            const unsigned char *p = m + at + 4 * i;
            w[i] = (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
        }
        for (int i = 16; i < 80; i++) {
            uint32_t x = w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16];
            w[i] = x << 1 | x >> 31;
        }
        uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4];
        for (int i = 0; i < 80; i++) {
            uint32_t f = i < 20 ? (b & c) | (~b & d) : i < 40 || i >= 60 ? b ^ c ^ d : (b & c) | (b & d) | (c & d);
            uint32_t k = i < 20 ? 0x5A827999 : i < 40 ? 0x6ED9EBA1 : i < 60 ? 0x8F1BBCDC : 0xCA62C1D6;
            uint32_t t = (a << 5 | a >> 27) + f + e + k + w[i];
            e = d, d = c, c = b << 30 | b >> 2, b = a, a = t;
        }
        h[0] += a, h[1] += b, h[2] += c, h[3] += d, h[4] += e;
    }
    free(m);
    for (int i = 0; i < 5; i++) snprintf(out + 8 * i, 9, "%08x", h[i]);
}

/* ---- a build as the server reads it --------------------------------------- */

/* Made on the engine's worker: the tree's files by path, and each one's blame
 * resolved to turn keys, which is what `changed` compares (a change id or a
 * turn index renumbers when a late transcript arrives; a key doesn't). */
typedef struct {
    StrMap ix;  /* path -> index of the arrays below */
    int n;
    char **path; /* below the root */
    int *node;
    char **sig;  /* its lines' turn keys, run-length; "" for unknown */
    char **key;  /* per turn: its key */
} Index;

/* A turn's identity across builds and sessions: what analysis.c keys prompts by,
 * its bytes that aren't UTF-8 as U+FFFD, as replies carry it (and clients send it back). */
static char *turn_key(const AnTurn *t) {
    char *k = an_turn_key(t->ts, t->title), *fixed = utf8_fix(k);
    free(k);
    return fixed;
}

/* The key of the turn change cid belongs to; NULL for none (an unknown line, a change outside any turn). */
static const char *key_of(const AnSnapshot *s, char *const *key, int cid) {
    if (cid < 0 || cid >= s->nchanges || s->changes[cid].turn < 0) return NULL;
    return key[s->changes[cid].turn];
}

static char *resolve(const AnSnapshot *s, char *const *key, const AnNode *d) {
    Buf b = {0};
    const char *cur = NULL;
    int run = 0;
    for (int k = 0; k <= d->nblame; k++) {
        const char *kk = k < d->nblame ? key_of(s, key, d->blame[k].origin) : NULL;
        if (!kk) kk = "";
        if (k && (k == d->nblame || strcmp(kk, cur))) { /* a run of one key ends: "n len key" */
            char h[48];
            buf_put(&b, h, (size_t)snprintf(h, sizeof h, "%d %zu ", run, strlen(cur)));
            buf_puts(&b, cur);
            run = 0;
        }
        if (k < d->nblame) cur = kk, run += d->blame[k].n;
    }
    return b.s ? b.s : xstrdup("");
}

static void *derive(const AnSnapshot *s) {
    Index *x = xcalloc(1, sizeof *x);
    x->key = xmalloc(sizeof *x->key * (size_t)(s->nturns + 1));
    for (int t = 0; t < s->nturns; t++) x->key[t] = turn_key(&s->turns[t]);
    int cap = 0;
    char **dir = xcalloc((size_t)s->nnodes + 1, sizeof *dir); /* a folder's path; parents come first */
    for (int i = 1; i < s->nnodes; i++) {
        const AnNode *d = &s->nodes[i];
        if (d->kind != AN_DIR && d->kind != AN_FILE) continue;
        char *p = d->parent > 0 ? xasprintf("%s/%s", dir[d->parent], d->name) : xstrdup(d->name);
        if (d->kind == AN_DIR) {
            dir[i] = p;
            continue;
        }
        if (x->n >= cap) {
            cap = cap ? cap * 2 : 64;
            x->path = xrealloc(x->path, sizeof *x->path * (size_t)cap);
            x->node = xrealloc(x->node, sizeof *x->node * (size_t)cap);
            x->sig = xrealloc(x->sig, sizeof *x->sig * (size_t)cap);
        }
        strmap_put(&x->ix, p, x->n);
        x->path[x->n] = p;
        x->node[x->n] = i;
        x->sig[x->n++] = resolve(s, x->key, d);
    }
    strv_free(dir, s->nnodes);
    return x;
}

static void drop(AnSnapshot *s, void *p) {
    Index *x = p;
    if (x) {
        strmap_free(&x->ix);
        strv_free(x->path, x->n);
        strv_free(x->sig, x->n);
        free(x->node);
        strv_free(x->key, s ? s->nturns : 0);
        free(x);
    }
    an_snapshot_free(s);
}

/* ---- clients ---------------------------------------------------------------- */

typedef struct {
    int fd;
    Buf in;      /* read, not yet a whole line */
    Buf out;     /* to send, from `sent` on */
    size_t sent;
    bool sub;    /* gets `changed` pushes */
    bool gone;   /* hung up, failed, or too slow: closed after this pass */
    double eof;  /* it shut its side (now_mono then; 0 not): no more requests, closed once its replies are out */
} Client;

typedef struct {
    const char *root;
    Engine *eng;
    Client *c;
    int nc, capc;
    bool quit;
} Srv;

/* As much of c's queue as the socket takes now; the rest waits for POLLOUT. */
static void flush_out(Client *c) {
    while (!c->gone && c->sent < c->out.n) {
        ssize_t k = send(c->fd, c->out.s + c->sent, c->out.n - c->sent, MSG_NOSIGNAL | MSG_DONTWAIT);
        if (k > 0) c->sent += (size_t)k;
        else if (k < 0 && errno == EINTR) continue;
        else if (k < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) break;
        else c->gone = true;
    }
    if (c->sent == c->out.n) c->out.n = c->sent = 0;
    else if (c->sent >= OUT_KEEP) { /* a reader that keeps lagging: the sent head goes */
        memmove(c->out.s, c->out.s + c->sent, c->out.n - c->sent);
        c->out.n -= c->sent;
        c->sent = 0;
    }
}

static void queue(Client *c, const char *s, size_t n) {
    if (c->gone) return;
    if (c->out.n - c->sent + n > OUT_MAX) { /* it doesn't read: never let it hold the server */
        c->gone = true;
        return;
    }
    buf_put(&c->out, s, n);
    flush_out(c);
}

/* A reply's start: {"id":<the request's>,"rev":N */
static FILE *reply(const Srv *v, yyjson_val *id, char **buf, size_t *len) {
    FILE *f = open_memstream(buf, len);
    if (!f) return NULL;
    /* bytes that aren't UTF-8 (the request was read allowing them) as \uFFFD: valid JSON */
    char *j = id ? yyjson_val_write(id, YYJSON_WRITE_ALLOW_INVALID_UNICODE | YYJSON_WRITE_ESCAPE_UNICODE, NULL) : NULL;
    fprintf(f, "{\"id\":%s,\"rev\":%ld", j ? j : "null", eng_rev(v->eng));
    free(j);
    return f;
}

static void send_reply(Client *c, FILE *f, char **buf, size_t *len) {
    fputs("}\n", f);
    if (!fclose(f)) queue(c, *buf, *len); /* the stream's buffer, final once closed */
    free(*buf);
}

/* ---- requests ----------------------------------------------------------------- */

/* A request's file (absolute, or below the root) as an index of x's files, -1 when it isn't one. */
static int find_file(const Srv *v, const Index *x, const char *file) {
    char p[PATH_MAX], real[PATH_MAX];
    if (*file == '/') snprintf(p, sizeof p, "%s", file);
    else snprintf(p, sizeof p, "%s/%s", v->root, file);
    path_normalize(p);
    size_t rl = strlen(v->root);
    for (int pass = 0; pass < 2; pass++) { /* as named, then through its links */
        const char *q = pass ? real : p;
        if (pass && !realpath(p, real)) break;
        if (strncmp(q, v->root, rl) || q[rl] != '/') continue;
        int i = strmap_get(&x->ix, q + rl + 1);
        if (i >= 0) return i;
    }
    return -1;
}

static void j_runs(FILE *f, const AnNode *d) {
    fputc('[', f);
    for (int k = 0; k < d->nblame; k++) fprintf(f, "%s[%d,%d]", k ? "," : "", d->blame[k].n, d->blame[k].origin);
    fputc(']', f);
}

/* A session's title, a subagent's "type · task", as the structure screen names them. */
static void j_agent(FILE *f, const AnSnapshot *s, int a) {
    if (a < 0 || a >= s->nactors) { fputs("null", f); return; }
    const AnActor *ac = &s->actors[a];
    if (!ac->sub) { json_str(f, ac->name ? ac->name : ""); return; }
    const char *t = ac->name ? strrchr(ac->name, ':') : NULL;
    t = t ? t + 1 : ac->name ? ac->name : "";
    char *nm = xasprintf("%s \xC2\xB7 %s", t, ac->task ? ac->task : "");
    json_str(f, nm);
    free(nm);
}

static void blame(const Srv *v, FILE *f, const char *file) {
    Index *x;
    const AnSnapshot *s = eng_snap(v->eng, (void **)&x);
    int i = s && file ? find_file(v, x, file) : -1;
    if (!s || i < 0) {
        fprintf(f, ",\"status\":\"%s\"", s ? "none" : "building");
        return;
    }
    const AnNode *d = &s->nodes[x->node[i]];
    fputs(",\"status\":\"ok\",\"text\":", f);
    json_strn(f, d->src ? d->src : "", d->src ? d->srclen : 0);
    fputs(",\"runs\":", f);
    j_runs(f, d);
    fputs(",\"changes\":{", f);
    bool *seen = xcalloc((size_t)s->nchanges + (size_t)s->nturns + 1, 1), *tseen = seen + s->nchanges;
    int nout = 0;
    for (int k = 0; k < d->nblame; k++) {
        int cid = d->blame[k].origin;
        if (cid < 0 || cid >= s->nchanges || seen[cid]) continue;
        seen[cid] = true;
        const AnChange *c = &s->changes[cid];
        fprintf(f, "%s\"%d\":{\"turn_key\":", nout++ ? "," : "", cid);
        if (c->turn >= 0) json_str(f, x->key[c->turn]);
        else fputs("null", f);
        fputs(",\"tool\":", f);
        json_str(f, c->tool ? c->tool : "");
        fputs(",\"agent\":", f);
        j_agent(f, s, c->actor);
        fprintf(f, ",\"ts\":%lld}", c->epoch);
    }
    fputs("},\"turns\":{", f);
    nout = 0;
    for (int k = 0; k < d->nblame; k++) {
        int cid = d->blame[k].origin, t = cid >= 0 && cid < s->nchanges ? s->changes[cid].turn : -1;
        if (t < 0 || tseen[t]) continue;
        tseen[t] = true;
        if (nout++) fputc(',', f);
        json_str(f, x->key[t]);
        /* st_blame_slot's hash, over the very bytes sent as the key */
        fprintf(f, ":{\"slot\":%d,\"prompt\":", (int)(hash_str(x->key[t]) % ST_BLAME_COLORS));
        json_str(f, s->turns[t].title);
        fprintf(f, ",\"ts\":%.3f}", s->turns[t].ts);
    }
    fputc('}', f);
    free(seen);
}

/* Every file holding lines of the turn: its runs, with other turns' lines as unknown (-1). */
static void turn_lines(const Srv *v, FILE *f, const char *req) {
    char *key = req ? utf8_fix(req) : NULL; /* as the keys are kept */
    Index *x;
    const AnSnapshot *s = eng_snap(v->eng, (void **)&x);
    if (!s) {
        fputs(",\"status\":\"building\"", f);
        free(key);
        return;
    }
    fputs(",\"status\":\"ok\",\"files\":[", f);
    int nout = 0;
    for (int i = 0; key && i < x->n; i++) {
        const AnNode *d = &s->nodes[x->node[i]];
        bool has = false;
        for (int k = 0; k < d->nblame && !has; k++) {
            const char *kk = key_of(s, x->key, d->blame[k].origin);
            has = kk && !strcmp(kk, key);
        }
        if (!has) continue;
        fputs(nout++ ? ",{\"file\":" : "{\"file\":", f);
        json_str(f, x->path[i]);
        fputs(",\"runs\":[", f);
        int run = 0, cur = 0, nr = 0;
        for (int k = 0; k <= d->nblame; k++) {
            int o = -1;
            if (k < d->nblame) {
                const char *kk = key_of(s, x->key, d->blame[k].origin);
                if (kk && !strcmp(kk, key)) o = d->blame[k].origin;
            }
            if (k && (k == d->nblame || o != cur)) fprintf(f, "%s[%d,%d]", nr++ ? "," : "", run, cur), run = 0;
            if (k < d->nblame) cur = o, run += d->blame[k].n;
        }
        fputs("]}", f);
    }
    fputc(']', f);
    free(key);
}

static void handle(Srv *v, Client *c, const char *line, size_t n) {
    yyjson_doc *doc = yyjson_read(line, n, YYJSON_READ_ALLOW_INVALID_UNICODE);
    yyjson_val *r = yyjson_doc_get_root(doc);
    const char *cmd = yyjson_get_str(yyjson_obj_get(r, "cmd"));
    char *buf = NULL;
    size_t len = 0;
    FILE *f = cmd && !strcmp(cmd, "quit") ? NULL : reply(v, yyjson_obj_get(r, "id"), &buf, &len);
    if (!cmd) {
        if (f) fputs(",\"status\":\"error\",\"error\":\"expected a JSON object with a cmd\"", f);
    } else if (!strcmp(cmd, "quit")) {
        v->quit = true;
    } else if (!f) {
    } else if (!strcmp(cmd, "blame")) {
        blame(v, f, yyjson_get_str(yyjson_obj_get(r, "file")));
    } else if (!strcmp(cmd, "subscribe")) {
        c->sub = true;
        fputs(",\"status\":\"ok\"", f);
    } else if (!strcmp(cmd, "saved")) { /* builds read today's files: the next one sees it */
        eng_touch(v->eng);
        fputs(",\"status\":\"ok\"", f);
    } else if (!strcmp(cmd, "turn_lines")) {
        turn_lines(v, f, yyjson_get_str(yyjson_obj_get(r, "turn_key")));
    } else if (!strcmp(cmd, "focus")) {
        fputs(",\"status\":\"ok\",\"host\":\"headless\"", f);
    } else {
        fputs(",\"status\":\"error\",\"error\":", f);
        char *e = xasprintf("unknown cmd %s", cmd);
        json_str(f, e);
        free(e);
    }
    if (f) send_reply(c, f, &buf, &len);
    yyjson_doc_free(doc);
}

static void read_client(Srv *v, Client *c) {
    char b[65536];
    for (;;) {
        ssize_t k = recv(c->fd, b, sizeof b, MSG_DONTWAIT);
        if (k > 0) {
            buf_put(&c->in, b, (size_t)k);
            continue;
        }
        if (k < 0 && errno == EINTR) continue;
        if (k == 0) c->eof = now_mono(); /* the lines before it still count */
        else if (errno != EAGAIN && errno != EWOULDBLOCK) c->gone = true;
        break;
    }
    size_t at = 0;
    while (!v->quit && !c->gone && at < c->in.n) {
        char *nl = memchr(c->in.s + at, '\n', c->in.n - at);
        if (!nl) break;
        size_t len = (size_t)(nl - (c->in.s + at));
        if (len) handle(v, c, c->in.s + at, len);
        at += len + 1;
    }
    if (at) {
        memmove(c->in.s, c->in.s + at, c->in.n - at);
        c->in.n -= at;
        c->in.s[c->in.n] = 0;
    }
    if (c->in.n > LINE_MAX_IN) c->gone = true;
}

/* Closed after this pass: gone, or half-closed with its replies out (or given up on). */
static bool done_with(const Client *c) { return c->gone || (c->eof && (!c->out.n || now_mono() - c->eof > EOF_WAIT)); }

/* After a build: to each subscriber, the files whose lines' turns differ from
 * the build before (any file, on the first), and the ones gone from the tree. */
static void push_changed(Srv *v, const Index *old, const Index *nw) {
    bool any = false;
    for (int i = 0; i < v->nc; i++) any |= v->c[i].sub && !v->c[i].gone;
    if (!any) return;
    char *buf = NULL;
    size_t len = 0;
    FILE *f = open_memstream(&buf, &len);
    if (!f) return;
    fputs("{\"event\":\"changed\",\"files\":[", f);
    int nout = 0;
    for (int i = 0; i < nw->n; i++) {
        int k = old ? strmap_get(&old->ix, nw->path[i]) : -1;
        if (k >= 0 && !strcmp(old->sig[k], nw->sig[i])) continue;
        if (nout++) fputc(',', f);
        json_str(f, nw->path[i]);
    }
    for (int i = 0; old && i < old->n; i++) {
        if (strmap_get(&nw->ix, old->path[i]) >= 0) continue;
        if (nout++) fputc(',', f);
        json_str(f, old->path[i]);
    }
    fprintf(f, "],\"rev\":%ld}\n", eng_rev(v->eng));
    if (!fclose(f) && nout)
        for (int i = 0; i < v->nc; i++)
            if (v->c[i].sub) queue(&v->c[i], buf, len);
    free(buf);
}

static void close_client(Client *c) {
    close(c->fd);
    buf_free(&c->in);
    buf_free(&c->out);
}

/* The history's first read, off the loop so it answers ("building") meanwhile. */
typedef struct {
    Ingest *in;
    atomic_bool done;
} Load;

static void *load(void *arg) {
    Load *l = arg;
    ingest_poll(l->in);
    atomic_store(&l->done, true);
    return NULL;
}

static void drain(Engine *e, EvQueue *q) {
    Event *evs;
    size_t n = evq_drain(q, &evs);
    eng_add(e, evs, n);
    for (size_t i = 0; i < n; i++) ev_free(&evs[i]);
    free(evs);
}

int serve_run(const IngestOpts *o) {
    IngestOpts io = *o;
    io.since = 0;  /* every session: blame is about all of today's lines */
    io.watch = 0;  /* your own saves aren't anyone's changes */
    signal(SIGPIPE, SIG_IGN); /* stderr may be a pipe the spawning editor no longer reads */
    const char *ns = getenv("AIG_SOCK_NS");
    char hex[41];
    sha1_hex(io.root, strlen(io.root), hex);
    struct sockaddr_un sa = {.sun_family = AF_UNIX}; /* sun_path[0] = 0: the abstract namespace */
    int nl = snprintf(sa.sun_path + 1, sizeof sa.sun_path - 1, "%s/%.16s", ns && *ns ? ns : "aigource", hex);
    if (nl < 0 || (size_t)nl >= sizeof sa.sun_path - 1) {
        fputs("aigource: AIG_SOCK_NS is too long for a socket name\n", stderr);
        return 2;
    }
    int lfd = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (lfd < 0 || bind(lfd, (struct sockaddr *)&sa, (socklen_t)(offsetof(struct sockaddr_un, sun_path) + 1 + (size_t)nl))) {
        int err = errno;
        if (lfd >= 0) close(lfd);
        if (err == EADDRINUSE) { /* another one serves this root */
            fprintf(stderr, "aigource: a server for %s is already up\n", io.root);
            return 0;
        }
        errno = err;
        perror("aigource: serve socket");
        return 1;
    }
    if (listen(lfd, 64)) {
        perror("aigource: serve socket");
        close(lfd);
        return 1;
    }
    const char *ie = getenv("AIG_SERVE_IDLE");
    double idle = ie && *ie ? atof(ie) : IDLE;

    EvQueue q;
    evq_init(&q);
    Ingest *in = ingest_new(&io, &q);
    Load ld = {.in = in};
    pthread_t lt;
    bool loading = !pthread_create(&lt, NULL, load, &ld);
    if (!loading) ingest_poll(in), atomic_store(&ld.done, true);
    Srv v = {.root = io.root,
             .eng = eng_new(io.root, &(EngOpts){.quiet = QUIET, .cap = CAP, .changes_only = true,
                                                .derive = derive, .drop = drop})};
    bool started = false; /* the history read, the first build asked, ingest following */
    double idle_since = now_mono(), accept_at = 0; /* accept_at: accepting again then */
    struct pollfd *pf = NULL;
    int cappf = 0;
    while (!v.quit) {
        int np = 0;
        GROW(pf, np, cappf);
        pf[np++] = (struct pollfd){.fd = now_mono() >= accept_at ? lfd : -1, .events = POLLIN};
        for (int i = 0; i < v.nc; i++) {
            const Client *c = &v.c[i];
            GROW(pf, np, cappf);
            pf[np++] = (struct pollfd){.fd = c->fd, .events = (short)((c->eof ? 0 : POLLIN) | (c->out.n ? POLLOUT : 0))};
        }
        if (poll(pf, (nfds_t)np, TICK_MS) < 0 && errno != EINTR) {
            perror("aigource: serve poll");
            break;
        }
        int nc = v.nc; /* the clients polled: pf[1 + i] */
        for (int i = 0; i < nc && !v.quit; i++) {
            Client *c = &v.c[i];
            if (pf[1 + i].revents & POLLOUT) flush_out(c);
            if (!c->eof && pf[1 + i].revents & (POLLIN | POLLHUP | POLLERR)) read_client(&v, c);
            else if (pf[1 + i].revents & (POLLHUP | POLLERR)) c->gone = true;
        }
        if (pf[0].revents & POLLIN) {
            int fd = 0;
            while (!v.quit && (fd = accept4(lfd, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC)) >= 0)
                PUSH(v.c, v.nc, v.capc, ((Client){.fd = fd}));
            if (fd < 0 && (errno == EMFILE || errno == ENFILE || errno == ENOBUFS || errno == ENOMEM))
                accept_at = now_mono() + ACCEPT_PAUSE; /* the pending connect stays; poll would wake at once */
        }
        drain(v.eng, &q);
        if (!started && atomic_load(&ld.done)) {
            if (loading) pthread_join(lt, NULL);
            drain(v.eng, &q);
            if (ingest_start(in)) perror("aigource: ingest thread");
            eng_request(v.eng, false); /* the first build, even with no history: today's files, all unknown */
            started = true;
        }
        eng_tick(v.eng);
        AnSnapshot *os;
        void *ox;
        if (eng_swap(v.eng, &os, &ox)) {
            Index *nx;
            eng_snap(v.eng, (void **)&nx);
            push_changed(&v, ox, nx);
            eng_drop(v.eng, os, ox);
        }
        int kept = 0;
        for (int i = 0; i < v.nc; i++) {
            if (done_with(&v.c[i])) close_client(&v.c[i]);
            else v.c[kept++] = v.c[i];
        }
        if (v.nc && !kept) idle_since = now_mono();
        v.nc = kept;
        if (!v.nc && now_mono() - idle_since >= idle) break;
    }
    close(lfd); /* first: the next host can bind while the clients hear the handover */
    if (v.quit) { /* to every client, then give them a moment to read it */
        char h[64];
        int hn = snprintf(h, sizeof h, "{\"event\":\"handover\",\"rev\":%ld}\n", eng_rev(v.eng));
        for (int i = 0; i < v.nc; i++) queue(&v.c[i], h, (size_t)hn);
        for (double until = now_mono() + 2; now_mono() < until;) {
            int np = 0;
            for (int i = 0; i < v.nc; i++)
                if (!v.c[i].gone && v.c[i].out.n) {
                    GROW(pf, np, cappf);
                    pf[np++] = (struct pollfd){.fd = v.c[i].fd, .events = POLLOUT};
                }
            if (!np) break;
            poll(pf, (nfds_t)np, TICK_MS);
            for (int i = 0; i < v.nc; i++) flush_out(&v.c[i]);
        }
    }
    for (int i = 0; i < v.nc; i++) close_client(&v.c[i]);
    free(v.c);
    free(pf);
    if (!started && loading) pthread_join(lt, NULL);
    ingest_free(in);
    eng_free(v.eng);
    Event *evs; /* what came after the last pass */
    size_t n = evq_drain(&q, &evs);
    for (size_t i = 0; i < n; i++) ev_free(&evs[i]);
    free(evs);
    pthread_mutex_destroy(&q.mu);
    return 0;
}
