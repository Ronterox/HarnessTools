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
#define GUESS_WAIT (SETTLE_SECS + 2) /* --watch: how long a guess waits for the watcher to confirm it */
#define FAILED_SPAN 60.0 /* a failed command claims unguessed changes only if it ran this briefly */
#define BACKLOG 10.0 /* events older than this are history: nothing live to confirm them */
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
    /* --watch: files guessed from a failed command, held until the watcher
     * confirms them (then they're real edits) or the wait runs out (no change). */
    struct { Event e; double until; } *held;
    int nheld, capheld;
    int meta_waiting; /* a subagent transcript exists but isn't tracked yet */
    /* Resumed sessions copy earlier records into their new file: events seen
     * once already (dedupe keys), and per response the largest output so far. */
    StrMap seen;
    StrMap usage_ix;
    struct { int out; double ts; } *usage;
    int nusage, capusage;
    pthread_t th;
    int started;
    atomic_int stop;
};

static void push(Ingest *in, Event *e) {
    e->seq = in->seq++;
    evq_push(in->q, *e);
}

/* A copy of an event already seen: a resumed session's file repeats earlier
 * records. The first copy seen wins (files are read in sorted path order).
 * Unique: a call's events by (kind, tool_use id, file, line range), turns by
 * (prompt time, text; a copy stays, as TURN_COPY), interrupts by time. A
 * response's usage counts once, the copy with the most output, at the first
 * copy's time. Compactions repeat: each starts the context of the actor whose
 * file has it. */
static int duplicate(Ingest *in, Event *e) {
    char key[1024]; /* longer keys (paths near PATH_MAX) are cut: not seen in practice */
    if (e->kind == EV_USAGE) {
        int k = strmap_get(&in->usage_ix, e->tool_id);
        if (k >= 0) {
            if (e->tok.out <= in->usage[k].out) return 1;
            in->usage[k].out = e->tok.out;
            e->ts = in->usage[k].ts;
            return 0;
        }
        if (in->nusage == in->capusage) {
            in->capusage = in->capusage ? in->capusage * 2 : 256;
            in->usage = xrealloc(in->usage, (size_t)in->capusage * sizeof *in->usage);
        }
        in->usage[in->nusage].out = e->tok.out;
        in->usage[in->nusage].ts = e->ts;
        strmap_put(&in->usage_ix, e->tool_id, in->nusage++);
        return 0;
    }
    if (e->tool_id)
        snprintf(key, sizeof key, "c\x1f%d\x1f%s\x1f%s\x1f%d\x1f%d", e->kind, e->tool_id, e->path ? e->path : "",
                 e->kind == EV_READ ? e->range.first : 0, e->kind == EV_READ ? e->range.last : 0);
    else if (e->kind == EV_TURN) snprintf(key, sizeof key, "t\x1f%.3f\x1f%s", e->ts, e->text ? e->text : "");
    else if (e->kind == EV_INTERRUPT) snprintf(key, sizeof key, "i\x1f%.3f", e->ts);
    else return 0;
    if (strmap_get(&in->seen, key) >= 0) {
        if (e->kind != EV_TURN) return 1;
        e->kind = EV_TURN_COPY; /* the resumed session's work before its first new prompt is in it */
        return 0;
    }
    strmap_put(&in->seen, key, 1);
    return 0;
}

/* Transcript events; file ones are remembered so the watcher can tell the
 * agent's own writes from other changes. */
static void on_event(void *ud, Event *e) {
    Ingest *in = ud;
    if (duplicate(in, e)) { ev_free(e); return; }
    if (e->via == VIA_GUESS) { /* not a record: the watcher must still see the change */
        double now = now_wall();
        if (!in->watch || e->ts < now - BACKLOG) { push(in, e); return; }
        if (in->nheld == in->capheld) {
            in->capheld = in->capheld ? in->capheld * 2 : 16;
            in->held = xrealloc(in->held, (size_t)in->capheld * sizeof *in->held);
        }
        in->held[in->nheld].e = *e;
        in->held[in->nheld++].until = now + GUESS_WAIT;
        return;
    }
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

/* Actor id/name from a transcript path: main session or subagents/agent-<id>.jsonl.
 * A subagent also gets *title: an EV_TITLE with its type, task and session (a
 * main session leaves it zeroed). */
static void actor_for(const char *path, char **id, char **name, Event *title) {
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
        const char *task = doc ? yyjson_get_str(yyjson_obj_get(yyjson_doc_get_root(doc), "description")) : NULL;
        if (t) {
            const char *colon = strrchr(t, ':'); /* "plugin:executor" -> "executor" */
            snprintf(type, sizeof type, "%s", colon ? colon + 1 : t);
        }
        char buf[160];
        snprintf(buf, sizeof buf, "%s\xC2\xB7%.6s", type, *id); /* "executor·a075f1" */
        *name = xstrdup(buf);
        /* <session>/subagents/agent-<id>.jsonl */
        const char *sub = strstr(path, "/subagents/"), *sid;
        for (const char *p; (p = strstr(sub + 1, "/subagents/"));) sub = p;
        sid = sub;
        while (sid > path && sid[-1] != '/') sid--;
        *title = (Event){.kind = EV_TITLE, .actor = xstrdup(*id), .actor_name = xstrdup(*name),
                         .text = xstrdup(task ? task : ""), .lines = -1};
        title->who.type = xstrdup(type);
        title->who.session = xstrndup(sid, (size_t)(sub - sid));
        yyjson_doc_free(doc);
        free(meta);
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
    Event title = {0};
    actor_for(path, &id, &name, &title);
    if (title.actor) on_event(in, &title);
    Tail *t = &in->tails[in->ntails];
    memset(t, 0, sizeof *t);
    t->path = xstrdup(path);
    t->actor = id;
    t->actor_name = name;
    t->tr = tr_new(in->root, id, name, t->path, on_event, in);
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

static int cmp_str(const void *a, const void *b) { return strcmp(*(char *const *)a, *(char *const *)b); }

/* The .jsonl files in dir, sorted: which copy of a resumed session's records
 * is seen first (and kept) must not depend on the directory's order. */
static int list_jsonl(const char *dir, char ***out) {
    DIR *d = opendir(dir);
    struct dirent *de;
    char **names = NULL;
    int n = 0;
    while (d && (de = readdir(d))) {
        if (!ends_with(de->d_name, ".jsonl")) continue;
        names = xrealloc(names, (size_t)(n + 1) * sizeof *names);
        names[n++] = xstrdup(de->d_name);
    }
    if (d) closedir(d);
    if (n) qsort(names, (size_t)n, sizeof *names, cmp_str);
    *out = names;
    return n;
}

static void scan_subagents(Ingest *in, const char *projdir, const char *sid) {
    char *sub = xmalloc(strlen(projdir) + strlen(sid) + 16);
    sprintf(sub, "%s/%s/subagents", projdir, sid);
    char **names;
    int nn = list_jsonl(sub, &names);
    for (int k = 0; k < nn; k++) {
        char *f = path_join(sub, names[k]);
        free(names[k]);
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
    free(names);
    free(sub);
}

static void rescan(Ingest *in) {
    in->meta_waiting = 0;
    char *newest = NULL;
    double newest_mt = 0;
    for (int p = 0; p < in->nproj; p++) {
        char **names;
        int nn = list_jsonl(in->projdirs[p], &names);
        for (int k = 0; k < nn; k++) {
            const char *name = names[k];
            char *f = path_join(in->projdirs[p], name);
            struct stat st;
            double mt = stat(f, &st) ? 0 : st.st_mtim.tv_sec + st.st_mtim.tv_nsec / 1e9;
            if (mt > newest_mt) { free(newest); newest = xstrdup(f); newest_mt = mt; }
            if (strmap_get(&in->tracked, f) >= 0 || mt >= in->since) {
                track(in, f);
                char *sid = xstrndup(name, strlen(name) - 6);
                scan_subagents(in, in->projdirs[p], sid);
                free(sid);
            }
            free(f);
            free(names[k]);
        }
        free(names);
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
        t->tr = tr_new(in->root, t->actor, t->actor_name, t->path, on_event, in);
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
        off_t base = t->off; /* file offset of chunk[0] */
        t->off += n;
        size_t start = 0;
        for (const char *nl; start < (size_t)n && (nl = memchr(chunk + start, '\n', (size_t)n - start));) {
            size_t i = (size_t)(nl - chunk);
            const char *line = chunk + start;
            size_t len = i - start;
            if (t->blen) { /* complete the buffered partial line */
                if (t->blen + len > t->bcap) {
                    t->bcap = (t->blen + len) * 2;
                    t->buf = xrealloc(t->buf, t->bcap);
                }
                memcpy(t->buf + t->blen, line, len);
                tr_feed(t->tr, t->buf, t->blen + len, (long)(base - (off_t)t->blen));
                t->blen = 0;
            } else if (len) {
                tr_feed(t->tr, line, len, (long)(base + (off_t)start));
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
    static const EvKind kinds[] = {[WC_CREATE] = EV_CREATE, [WC_EDIT] = EV_EDIT, [WC_DELETE] = EV_DELETE};
    /* The transcript already has it: changed while the call that recorded it ran.
     * A failed command's guess for the same file adds nothing. */
    for (int i = 0; i < RECENT; i++) {
        if (!in->recent[i].path || strcmp(in->recent[i].path, c->path) ||
            c->ts < in->recent[i].start - DUP_SLACK || c->ts > in->recent[i].end + DUP_SLACK)
            continue;
        for (int j = 0; j < in->nheld;) {
            if (strcmp(in->held[j].e.path, c->path)) { j++; continue; }
            ev_free(&in->held[j].e);
            in->held[j] = in->held[--in->nheld];
        }
        return -1;
    }
    /* A failed command's guess, confirmed: a real change, with what the watcher saw. */
    for (int i = 0; i < in->nheld; i++) {
        Event *h = &in->held[i].e;
        double start = h->t0 > 0 && h->t0 < h->ts ? h->t0 : h->ts;
        if (strcmp(h->path, c->path) || c->ts < start - DUP_SLACK || c->ts > h->ts + DUP_SLACK) continue;
        Event e = *h;
        in->held[i] = in->held[--in->nheld];
        e.kind = kinds[c->kind];
        e.add = c->add;
        e.del = c->del;
        e.lines = c->lines;
        e.via = VIA_WATCH;
        push(in, &e);
        return 1;
    }
    int wait = in->meta_waiting; /* an agent we can't read yet */
    for (int i = 0; i < in->ntails && !wait; i++) {
        double start;
        wait = tr_writing(in->tails[i].tr, c->path) ||
               (tr_bash_running(in->tails[i].tr, now - BASH_MAX, &start) && start <= c->ts + 1);
    }
    if (wait && now - c->ts < DEFER_MAX) return 0;

    /* Changed while a brief agent command ran that failed (so recorded nothing):
     * the agent's. A long one (a failing test suite) doesn't claim your saves. */
    for (int i = 0; i < in->ntails; i++) {
        Event e = {.kind = kinds[c->kind], .path = xstrdup(c->path), .add = c->add, .del = c->del,
                   .lines = c->lines, .via = VIA_WATCH};
        if (!tr_failed_call(in->tails[i].tr, c->ts, DUP_SLACK, &e) || e.ts - e.t0 > FAILED_SPAN) {
            free(e.path);
            free(e.tool_id);
            continue;
        }
        e.actor = xstrdup(in->tails[i].actor);
        e.actor_name = xstrdup(in->tails[i].actor_name);
        push(in, &e);
        return 1;
    }

    const char *actor = "you", *name = "you";
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
    for (int i = 0; i < in->nheld;) { /* unconfirmed: the watcher saw no such change */
        if (now < in->held[i].until) { i++; continue; }
        ev_free(&in->held[i].e);
        in->held[i] = in->held[--in->nheld];
    }
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

/* Without --watch (no free for the watcher): what a one-shot run made. */
void ingest_free(Ingest *in) {
    if (!in) return;
    ingest_stop(in);
    for (int i = 0; i < in->ntails; i++) {
        Tail *t = &in->tails[i];
        tr_free(t->tr);
        free(t->path), free(t->actor), free(t->actor_name), free(t->buf);
    }
    free(in->tails);
    for (int i = 0; i < in->nproj; i++) free(in->projdirs[i]);
    free(in->projdirs);
    strmap_free(&in->tracked), strmap_free(&in->rejected), strmap_free(&in->seen), strmap_free(&in->usage_ix);
    for (int i = 0; i < RECENT; i++) free(in->recent[i].path);
    for (int i = 0; i < in->nheld; i++) ev_free(&in->held[i].e);
    free(in->held);
    free(in->usage);
    free(in->root);
    free(in);
}
