#include "ingest.h"

#include <ctype.h>
#include <stdatomic.h>
#include <dirent.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "transcript.h"
#include "watch.h"
#include "util.h"
#include "yyjson.h"

#define RESCAN_SECS 1.0
#define PROJDIR_SECS 10.0  /* new project dirs (sessions in new subdirs) */
#define META_WAIT_SECS 5.0 /* a subagent's .meta.json can lag its transcript */
#define SETTLE_SECS 2.5    /* a file quiet this long is one change */
#define DUP_SLACK 3.0      /* clock slack around the agent call that recorded a file */
#define DEFER_MAX 600.0    /* stop waiting on a running command after this */
#define BASH_MAX 620.0     /* Bash times out at 10 min: older unanswered calls are dead sessions */
#define RECENT 512
#define POLL_USECS 100000

typedef struct {
    char *path;
    char *actor, *actor_name;
    dev_t dev;
    ino_t ino;
    off_t off;
    char *buf; /* partial last line */
    size_t blen, bcap;
    Transcript *tr;
} Tail;

struct Ingest {
    char *root;
    double since;
    int explicit_files;
    char **projdirs;
    int nproj;
    Tail *tails;
    int ntails, captails;
    StrMap tracked; /* path -> tail index */
    StrMap rejected; /* prefix-matching project dir -> its mtime when found foreign */
    EvQueue *q;
    unsigned long seq;
    double last_scan, last_projscan;
    /* Filesystem watcher and what it needs for attribution. */
    Watch *watch;
    struct { char *path; double start, end; } recent[RECENT]; /* agent file events, call span */
    int recent_head;
    int meta_waiting; /* a subagent transcript exists but isn't tracked yet */
    pthread_t th;
    int started;
    atomic_int stop;
};

static void push(Ingest *in, Event *e) {
    e->seq = in->seq++;
    evq_push(in->q, *e);
}

/* Transcript events; file ones are remembered so the watcher can tell the
 * agent's own writes from other changes. */
static void on_event(void *ud, Event *e) {
    Ingest *in = ud;
    if (e->path && (e->kind == EV_CREATE || e->kind == EV_EDIT || e->kind == EV_DELETE || e->kind == EV_TOUCH)) {
        int k = in->recent_head++ % RECENT;
        free(in->recent[k].path);
        in->recent[k].path = xstrdup(e->path);
        in->recent[k].start = e->t0 > 0 && e->t0 < e->ts ? e->t0 : e->ts;
        in->recent[k].end = e->ts;
    }
    push(in, e);
}


static char *path_join(const char *a, const char *b) {
    size_t na = strlen(a), nb = strlen(b);
    char *p = xmalloc(na + nb + 2);
    memcpy(p, a, na);
    p[na] = '/';
    memcpy(p + na + 1, b, nb + 1);
    return p;
}

static int ends_with(const char *s, const char *suf) {
    size_t n = strlen(s), m = strlen(suf);
    return n >= m && !strcmp(s + n - m, suf);
}

/* Actor id/name from a transcript path: main session or subagents/agent-<id>.jsonl. */
static void actor_for(const char *path, char **id, char **name) {
    const char *base = strrchr(path, '/');
    base = base ? base + 1 : path;
    size_t n = strlen(base) - (ends_with(base, ".jsonl") ? 6 : 0);
    if (!strncmp(base, "agent-", 6) && strstr(path, "/subagents/")) {
        *id = xstrndup(base + 6, n - 6);
        char *meta = xmalloc(strlen(path) + 16);
        memcpy(meta, path, strlen(path) - 6);
        strcpy(meta + strlen(path) - 6, ".meta.json");
        char type[96] = "agent";
        yyjson_doc *doc = yyjson_read_file(meta, 0, NULL, NULL);
        const char *t = doc ? yyjson_get_str(yyjson_obj_get(yyjson_doc_get_root(doc), "agentType")) : NULL;
        if (t) {
            const char *colon = strrchr(t, ':'); /* "plugin:executor" -> "executor" */
            snprintf(type, sizeof type, "%s", colon ? colon + 1 : t);
        }
        yyjson_doc_free(doc);
        free(meta);
        char buf[160];
        snprintf(buf, sizeof buf, "%s\xC2\xB7%.6s", type, *id); /* "executor·a075f1" */
        *name = xstrdup(buf);
    } else {
        *id = xstrndup(base, n);
        char buf[64];
        snprintf(buf, sizeof buf, "claude\xC2\xB7%.8s", *id);
        *name = xstrdup(buf);
    }
}

static void track(Ingest *in, const char *path) {
    if (strmap_get(&in->tracked, path) >= 0) return;
    if (in->ntails == in->captails) {
        in->captails = in->captails ? in->captails * 2 : 16;
        in->tails = xrealloc(in->tails, in->captails * sizeof *in->tails);
    }
    char *id, *name;
    actor_for(path, &id, &name);
    Tail *t = &in->tails[in->ntails];
    memset(t, 0, sizeof *t);
    t->path = xstrdup(path);
    t->actor = id;
    t->actor_name = name;
    t->tr = tr_new(in->root, id, name, on_event, in);
    strmap_put(&in->tracked, path, in->ntails++);
}

/* Claude Code's project dir name: every non-alphanumeric char of the cwd -> '-'. */
static char *slug(const char *root) {
    char *s = xstrdup(root);
    for (char *p = s; *p; p++)
        if (!isalnum((unsigned char)*p)) *p = '-';
    return s;
}

/* cwd recorded in the first records of a transcript, or NULL. */
static char *transcript_cwd(const char *path) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) return NULL;
    char buf[65536];
    ssize_t n = read(fd, buf, sizeof buf - 1);
    close(fd);
    if (n <= 0) return NULL;
    buf[n] = 0;
    char *p = strstr(buf, "\"cwd\":\"");
    if (!p) return NULL;
    p += 7;
    char *e = strchr(p, '"');
    return e ? xstrndup(p, (size_t)(e - p)) : NULL;
}

/* Project dirs whose sessions ran in root or below it (a session launched in
 * root/sub gets its own dir, named by the same slug rule). */
static int known_projdir(Ingest *in, const char *dir) {
    for (int i = 0; i < in->nproj; i++)
        if (!strcmp(in->projdirs[i], dir)) return 1;
    return 0;
}

static void find_projdirs(Ingest *in) {
    const char *cfg = getenv("CLAUDE_CONFIG_DIR");
    char *base = cfg ? path_join(cfg, "projects") : NULL;
    if (!base) {
        const char *home = getenv("HOME");
        base = path_join(home ? home : "", ".claude/projects");
    }
    char *want = slug(in->root);
    size_t wn = strlen(want);
    DIR *d = opendir(base);
    struct dirent *de;
    while (d && (de = readdir(d))) {
        if (strncmp(de->d_name, want, wn) || (de->d_name[wn] && de->d_name[wn] != '-')) continue;
        char *dir = path_join(base, de->d_name);
        if (known_projdir(in, dir)) { free(dir); continue; }
        int ok = !de->d_name[wn];
        struct stat dst;
        int mt = stat(dir, &dst) ? -1 : (int)dst.st_mtim.tv_sec;
        if (!ok && mt >= 0 && strmap_get(&in->rejected, dir) == mt) { free(dir); continue; } /* unchanged */
        if (!ok) { /* prefix match: confirm with a transcript's real cwd */
            DIR *sd = opendir(dir);
            struct dirent *se;
            while (sd && !ok && (se = readdir(sd))) {
                if (!ends_with(se->d_name, ".jsonl")) continue;
                char *f = path_join(dir, se->d_name);
                char *cwd = transcript_cwd(f);
                size_t rn = strlen(in->root);
                ok = cwd && !strncmp(cwd, in->root, rn) && cwd[rn] == '/';
                free(cwd);
                free(f);
            }
            if (sd) closedir(sd);
        }
        if (ok) {
            in->projdirs = xrealloc(in->projdirs, (in->nproj + 1) * sizeof *in->projdirs);
            in->projdirs[in->nproj++] = dir;
        } else {
            strmap_put(&in->rejected, dir, mt);
            free(dir);
        }
    }
    if (d) closedir(d);
    free(want);
    free(base);
}

static void scan_subagents(Ingest *in, const char *projdir, const char *sid) {
    char *sub = xmalloc(strlen(projdir) + strlen(sid) + 16);
    sprintf(sub, "%s/%s/subagents", projdir, sid);
    DIR *d = opendir(sub);
    struct dirent *de;
    while (d && (de = readdir(d))) {
        if (!ends_with(de->d_name, ".jsonl")) continue;
        char *f = path_join(sub, de->d_name);
        if (strmap_get(&in->tracked, f) < 0) {
            /* Name comes from the .meta.json; give it a moment to appear. */
            char *meta = xmalloc(strlen(f) + 16);
            memcpy(meta, f, strlen(f) - 6);
            strcpy(meta + strlen(f) - 6, ".meta.json");
            struct stat st;
            int ready = !access(meta, R_OK) ||
                        (!stat(f, &st) && now_wall() - st.st_mtim.tv_sec > META_WAIT_SECS);
            free(meta);
            if (ready) track(in, f);
            else in->meta_waiting = 1;
        }
        free(f);
    }
    if (d) closedir(d);
    free(sub);
}

static void rescan(Ingest *in) {
    in->meta_waiting = 0;
    char *newest = NULL;
    double newest_mt = 0;
    for (int p = 0; p < in->nproj; p++) {
        DIR *d = opendir(in->projdirs[p]);
        struct dirent *de;
        while (d && (de = readdir(d))) {
            if (!ends_with(de->d_name, ".jsonl")) continue;
            char *f = path_join(in->projdirs[p], de->d_name);
            struct stat st;
            double mt = stat(f, &st) ? 0 : st.st_mtim.tv_sec + st.st_mtim.tv_nsec / 1e9;
            if (mt > newest_mt) { free(newest); newest = xstrdup(f); newest_mt = mt; }
            if (strmap_get(&in->tracked, f) >= 0 || mt >= in->since) {
                track(in, f);
                char *sid = xstrndup(de->d_name, strlen(de->d_name) - 6);
                scan_subagents(in, in->projdirs[p], sid);
                free(sid);
            }
            free(f);
        }
        if (d) closedir(d);
    }
    /* Nothing active: show the most recent session rather than an empty screen. */
    if (!in->ntails && newest) {
        track(in, newest);
        const char *slash = strrchr(newest, '/');
        char *dir = xstrndup(newest, (size_t)(slash - newest));
        char *sid = xstrndup(slash + 1, strlen(slash + 1) - 6);
        scan_subagents(in, dir, sid);
        free(dir);
        free(sid);
    }
    free(newest);
}

static void read_tail(Ingest *in, Tail *t) {
    struct stat st;
    if (stat(t->path, &st)) return;
    int replaced = t->ino && (st.st_ino != t->ino || st.st_dev != t->dev);
    if (replaced || st.st_size < t->off) { /* rotated or truncated: start over */
        tr_free(t->tr);
        t->tr = tr_new(in->root, t->actor, t->actor_name, on_event, in);
        t->off = 0;
        t->blen = 0;
    }
    t->dev = st.st_dev;
    t->ino = st.st_ino;
    if (st.st_size == t->off) return;
    int fd = open(t->path, O_RDONLY);
    if (fd < 0) return;
    lseek(fd, t->off, SEEK_SET);
    char chunk[1 << 16];
    ssize_t n;
    while ((n = read(fd, chunk, sizeof chunk)) > 0) {
        t->off += n;
        size_t start = 0;
        for (size_t i = 0; i < (size_t)n; i++) {
            if (chunk[i] != '\n') continue;
            const char *line = chunk + start;
            size_t len = i - start;
            if (t->blen) { /* complete the buffered partial line */
                if (t->blen + len > t->bcap) {
                    t->bcap = (t->blen + len) * 2;
                    t->buf = xrealloc(t->buf, t->bcap);
                }
                memcpy(t->buf + t->blen, line, len);
                tr_feed(t->tr, t->buf, t->blen + len);
                t->blen = 0;
            } else if (len) {
                tr_feed(t->tr, line, len);
            }
            start = i + 1;
        }
        size_t rest = (size_t)n - start;
        if (rest) {
            if (t->blen + rest > t->bcap) {
                t->bcap = (t->blen + rest) * 2;
                t->buf = xrealloc(t->buf, t->bcap);
            }
            memcpy(t->buf + t->blen, chunk + start, rest);
            t->blen += rest;
        }
    }
    close(fd);
}

/* A filesystem change the agents' transcripts don't account for is yours.
 * Claude Code records every file an agent's Edit/Write/Bash changed (Bash via
 * bashEditDiff), so: drop what a transcript has, wait while an agent call that
 * may still report it is open, and attribute the rest to "you".
 * Returns 0 to wait, -1 dropped, 1 emitted. */
static int attribute(Ingest *in, WatchChange *c, double now) {
    /* The transcript already has it: changed while the call that recorded it ran. */
    for (int i = 0; i < RECENT; i++)
        if (in->recent[i].path && !strcmp(in->recent[i].path, c->path) &&
            c->ts >= in->recent[i].start - DUP_SLACK && c->ts <= in->recent[i].end + DUP_SLACK)
            return -1;
    int wait = in->meta_waiting; /* an agent we can't read yet */
    for (int i = 0; i < in->ntails && !wait; i++) {
        double start;
        wait = tr_writing(in->tails[i].tr, c->path) ||
               (tr_bash_running(in->tails[i].tr, now - BASH_MAX, &start) && start <= c->ts + 1);
    }
    if (wait && now - c->ts < DEFER_MAX) return 0;

    const char *actor = "you", *name = "you";
    static const EvKind kinds[] = {[WC_CREATE] = EV_CREATE, [WC_EDIT] = EV_EDIT, [WC_DELETE] = EV_DELETE};
    Event e = {.ts = c->ts, .kind = kinds[c->kind], .actor = xstrdup(actor), .actor_name = xstrdup(name),
               .path = xstrdup(c->path), .add = c->add, .del = c->del, .lines = c->lines};
    push(in, &e); /* not into `recent`: your next save of the same file is a new change */
    return 1;
}

static void poll_watch(Ingest *in, double now) {
    watch_poll(in->watch, now);
    WatchChange *cs;
    int n = watch_take(in->watch, now, SETTLE_SECS, &cs);
    if (n && !in->explicit_files) { /* a session that just started may have made it: look first */
        if (now - in->last_projscan >= SETTLE_SECS) {
            find_projdirs(in);
            in->last_projscan = now;
        }
        rescan(in);
        in->last_scan = now;
        for (int i = 0; i < in->ntails; i++) read_tail(in, &in->tails[i]);
    }
    for (int i = 0; i < n; i++) {
        if (attribute(in, &cs[i], now) == 0) watch_defer(in->watch, &cs[i]);
        else free(cs[i].path);
    }
    free(cs);
}

Ingest *ingest_new(const IngestOpts *o, EvQueue *q) {
    Ingest *in = xcalloc(1, sizeof *in);
    in->root = xstrdup(o->root);
    in->since = o->since;
    in->q = q;
    if (o->watch) in->watch = watch_new(o->root);
    if (o->nfiles) {
        in->explicit_files = 1;
        for (int i = 0; i < o->nfiles; i++) track(in, o->files[i]);
    }
    return in;
}

void ingest_poll(Ingest *in) {
    double now = now_wall();
    if (!in->explicit_files && now - in->last_projscan >= PROJDIR_SECS) {
        find_projdirs(in);
        in->last_projscan = now;
    }
    if (!in->explicit_files && now - in->last_scan >= RESCAN_SECS) {
        rescan(in);
        in->last_scan = now;
    }
    for (int i = 0; i < in->ntails; i++) read_tail(in, &in->tails[i]);
    if (in->watch) poll_watch(in, now);
}

int ingest_nfiles(const Ingest *in) { return in->ntails; }

static void *loop(void *ud) {
    Ingest *in = ud;
    while (!atomic_load(&in->stop)) {
        ingest_poll(in);
        usleep(POLL_USECS);
    }
    return NULL;
}

int ingest_start(Ingest *in) {
    in->started = !pthread_create(&in->th, NULL, loop, in);
    return in->started ? 0 : -1;
}

void ingest_stop(Ingest *in) {
    if (!in->started) return;
    atomic_store(&in->stop, 1);
    pthread_join(in->th, NULL);
}
