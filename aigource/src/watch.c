#include "watch.h"

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/inotify.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#include "util.h"

#define MASK (IN_CLOSE_WRITE | IN_CREATE | IN_DELETE | IN_MOVED_FROM | IN_MOVED_TO)
#define MAX_PRESCAN 20000 /* files whose line counts are learned at startup */
#define PRESCAN_BYTES (64L << 20) /* and at most this much reading */
#define GIT_RESTARTS 20   /* check-ignore dies on paths it refuses; give up after this many */

typedef struct {
    char *path;
    double first, last;
    bool created; /* IN_CREATE seen: may be new */
} Raw;

struct Watch {
    char *root;
    int fd;
    char **wd_path; /* inotify wd -> repo-relative dir ("" = root) */
    int nwd;
    StrMap lines;   /* path -> line count + 1 (last known) */
    StrMap ignored; /* path -> 1 ignored, 2 not (check-ignore cache) */
    Raw *raw;
    int nraw, capraw;
    pid_t git;      /* `git check-ignore --stdin -z` coprocess, 0 = none */
    FILE *git_in, *git_out;
    int git_restarts;
    char *gf[4];    /* its answer: source, line, pattern, path */
    size_t gcap[4];
    bool prescanned, overflowed;
};

/* ---- ignore rules ------------------------------------------------------- */

static bool editor_noise(const char *name) {
    size_t n = strlen(name);
    if (!strcmp(name, "4913")) return true; /* vim's write test */
    if (n && name[n - 1] == '~') return true;
    if (n > 4 && (!strcmp(name + n - 4, ".swp") || !strcmp(name + n - 4, ".swx") ||
                  !strcmp(name + n - 4, ".swo")))
        return true;
    if (!strncmp(name, ".#", 2) || (name[0] == '#' && n > 1 && name[n - 1] == '#')) return true;
    return false;
}

static bool fallback_ignored(const char *name) {
    static const char *dirs[] = {".git", "node_modules", "__pycache__", ".venv", "venv", "build", "dist",
                                 "target", ".cache", ".mypy_cache", ".pytest_cache", NULL};
    for (int i = 0; dirs[i]; i++)
        if (!strcmp(name, dirs[i])) return true;
    return false;
}

/* Runs argv with pipes on its stdin (*to, may be NULL) and stdout (*from).
 * Close-on-exec: another thread's child must not hold them open (git would
 * never see stdin's EOF, a read would never end). */
static pid_t spawn(char *const argv[], FILE **to, FILE **from) {
    int in[2], out[2];
    if (pipe2(in, O_CLOEXEC)) return -1;
    if (pipe2(out, O_CLOEXEC)) { close(in[0]); close(in[1]); return -1; }
    pid_t pid = fork();
    if (pid < 0) {
        close(in[0]); close(in[1]); close(out[0]); close(out[1]);
        return -1;
    }
    if (!pid) {
        dup2(in[0], 0);
        dup2(out[1], 1);
        int devnull = open("/dev/null", O_WRONLY);
        if (devnull >= 0) dup2(devnull, 2);
        close(in[1]);
        close(out[0]);
        execvp(argv[0], argv);
        _exit(127);
    }
    close(in[0]);
    close(out[1]);
    FILE *t = to ? fdopen(in[1], "w") : NULL;
    if (!to) close(in[1]);
    *from = fdopen(out[0], "r");
    if ((to && !t) || !*from) {
        if (t) fclose(t);
        else if (to) close(in[1]);
        if (*from) fclose(*from);
        else close(out[0]);
        kill(pid, SIGTERM);
        waitpid(pid, NULL, 0);
        return -1;
    }
    if (to) *to = t;
    return pid;
}

static void start_git(Watch *w) {
    char *argv[] = {"git", "-C", w->root, "check-ignore", "--stdin", "-z", "-v", "-n", NULL};
    pid_t pid = spawn(argv, &w->git_in, &w->git_out);
    if (pid > 0) w->git = pid;
    signal(SIGPIPE, SIG_IGN);
}

static void stop_git(Watch *w) {
    if (w->git_in) fclose(w->git_in);
    if (w->git_out) fclose(w->git_out);
    if (w->git > 0) waitpid(w->git, NULL, 0); /* it exits on stdin EOF */
    w->git_in = w->git_out = NULL;
    w->git = 0;
}

/* check-ignore's verdict on one path; false when git couldn't answer. Paths are
 * NUL-separated both ways, so any byte in a name is safe. */
static bool git_ask(Watch *w, const char *rel, bool *ig) {
    if (!w->git) return false;
    if (fputs(rel, w->git_in) < 0 || fputc(0, w->git_in) == EOF || fflush(w->git_in)) return false;
    for (int i = 0; i < 4; i++)
        if (getdelim(&w->gf[i], &w->gcap[i], 0, w->git_out) <= 0) return false;
    *ig = w->gf[0][0] && w->gf[2][0] != '!'; /* no source = no rule; "!" = re-included */
    w->git_restarts = 0; /* the budget is for consecutive failures (not a repo) */
    return true;
}

/* .git, gitignored, or (outside git) a well-known build/cache dir. */
static bool ignored(Watch *w, const char *rel) {
    char namebuf[512];
    size_t rl = strlen(rel);
    if (rl && rel[rl - 1] == '/') rl--; /* "dir/" -> name "dir" */
    const char *start = rel + rl;
    while (start > rel && start[-1] != '/') start--;
    snprintf(namebuf, sizeof namebuf, "%.*s", (int)(rel + rl - start), start);
    const char *name = namebuf;
    if (!strcmp(name, ".git") || !strncmp(rel, ".git/", 5) || strstr(rel, "/.git/")) return true;
    if (editor_noise(name)) return true;
    int c = strmap_get(&w->ignored, rel);
    if (c > 0) return c == 1;
    bool ig;
    if (!git_ask(w, rel, &ig)) {
        /* git refused this path (or isn't there): name-based rules for it, and a
         * fresh coprocess for the next one. Not a repo -> restarts run out. */
        if (w->git) {
            stop_git(w);
            if (w->git_restarts++ < GIT_RESTARTS) start_git(w);
        }
        ig = false;
        char *p = xstrdup(rel);
        for (char *tok = strtok(p, "/"); tok; tok = strtok(NULL, "/")) ig |= fallback_ignored(tok);
        free(p);
    }
    strmap_put(&w->ignored, rel, ig ? 1 : 2);
    return ig;
}

/* ---- line counts -------------------------------------------------------- */

static char *abs_path(Watch *w, const char *rel) { return xasprintf("%s/%s", w->root, rel); }

static int rel_lines(Watch *w, const char *rel) {
    char *p = abs_path(w, rel);
    int n = file_lines(p, 4 << 20, false);
    free(p);
    return n;
}

/* Line counts of tracked files, so the first edit of each has a +/-. */
static void prescan(Watch *w) {
    w->prescanned = true;
    char *argv[] = {"git", "-C", w->root, "ls-files", "-z", NULL};
    FILE *f;
    pid_t pid = spawn(argv, NULL, &f);
    if (pid < 0) return;
    char path[4096];
    int n = 0, c, len = 0;
    long bytes = 0;
    while ((c = fgetc(f)) != EOF && n < MAX_PRESCAN && bytes < PRESCAN_BYTES) {
        if (c) { if (len < (int)sizeof path - 1) path[len++] = (char)c; continue; }
        path[len] = 0;
        len = 0;
        struct stat st;
        char *abs = abs_path(w, path);
        int ok = !stat(abs, &st);
        free(abs);
        if (ok) bytes += st.st_size;
        int l = rel_lines(w, path);
        if (l >= 0) strmap_put(&w->lines, path, l + 1);
        n++;
    }
    fclose(f);
    kill(pid, SIGTERM); /* may still be listing when we stop early */
    waitpid(pid, NULL, 0);
}

/* ---- watches ------------------------------------------------------------ */

static void add_dir(Watch *w, const char *rel, double now, bool fresh);

static void note(Watch *w, const char *rel, double now, bool created) {
    for (int i = 0; i < w->nraw; i++)
        if (!strcmp(w->raw[i].path, rel)) {
            w->raw[i].last = now;
            w->raw[i].created |= created;
            return;
        }
    PUSH(w->raw, w->nraw, w->capraw, ((Raw){xstrdup(rel), now, now, created}));
}

/* Watch a dir and its subdirs. A dir that just appeared (fresh) may already
 * hold files written before the watch existed: report them as created. */
static void add_dir(Watch *w, const char *rel, double now, bool fresh) {
    if (*rel) { /* dir-only rules ("build/") match only with the slash */
        char *key = xasprintf("%s/", rel);
        bool ig = ignored(w, key);
        free(key);
        if (ig) return;
    }
    char *abs = *rel ? abs_path(w, rel) : xstrdup(w->root);
    if (*rel) { /* a submodule or nested repo isn't this repo's (and git refuses its paths) */
        char *dotgit = xasprintf("%s/.git", abs);
        bool nested = !access(dotgit, F_OK);
        free(dotgit);
        if (nested) { free(abs); return; }
    }
    int wd = inotify_add_watch(w->fd, abs, MASK | IN_ONLYDIR);
    if (wd < 0) {
        if (errno == ENOSPC) fprintf(stderr, "aigource: out of inotify watches (fs.inotify.max_user_watches)\n");
        free(abs);
        return;
    }
    if (wd >= w->nwd) {
        int n = wd * 2 + 16;
        w->wd_path = xrealloc(w->wd_path, (size_t)n * sizeof *w->wd_path);
        memset(w->wd_path + w->nwd, 0, (size_t)(n - w->nwd) * sizeof *w->wd_path);
        w->nwd = n;
    }
    free(w->wd_path[wd]);
    w->wd_path[wd] = xstrdup(rel);
    DIR *d = opendir(abs);
    struct dirent *de;
    while (d && (de = readdir(d))) {
        if (!strcmp(de->d_name, ".") || !strcmp(de->d_name, "..")) continue;
        char *child = xasprintf("%s%s%s", rel, *rel ? "/" : "", de->d_name);
        int type = de->d_type;
        struct stat st;
        if (type == DT_UNKNOWN) /* some filesystems don't fill d_type */
            type = fstatat(dirfd(d), de->d_name, &st, AT_SYMLINK_NOFOLLOW) ? DT_UNKNOWN
                 : S_ISDIR(st.st_mode) ? DT_DIR : S_ISREG(st.st_mode) ? DT_REG : DT_UNKNOWN;
        if (type == DT_DIR) add_dir(w, child, now, fresh);
        else if (fresh && type == DT_REG && !ignored(w, child)) note(w, child, now, true);
        free(child);
    }
    if (d) closedir(d);
    free(abs);
}

Watch *watch_new(const char *root) {
    Watch *w = xcalloc(1, sizeof *w);
    w->root = xstrdup(root);
    w->fd = inotify_init1(IN_NONBLOCK | IN_CLOEXEC);
    if (w->fd < 0) {
        perror("aigource: inotify");
        free(w->root);
        free(w);
        return NULL;
    }
    setenv("GIT_FLUSH", "1", 1); /* check-ignore answers each path as it's asked; set
                                  * before any thread exists, children inherit it */
    start_git(w);
    add_dir(w, "", now_wall(), false);
    return w;
}

static bool under(const char *path, const char *dir, size_t dn) {
    return !strncmp(path, dir, dn) && (path[dn] == 0 || path[dn] == '/');
}

/* Stop watching a dir and everything under it. When it left the tree (moved
 * away or deleted, `gone`), report its known files as gone too: a move away
 * sends no event per file. A move within the tree re-adds it via IN_MOVED_TO. */
static void drop_dir(Watch *w, const char *rel, double now, bool gone) {
    size_t dn = strlen(rel);
    /* Must not insert into w->lines while walking its slots (a grow frees them). */
    for (int wd = 0; wd < w->nwd; wd++)
        if (w->wd_path[wd] && under(w->wd_path[wd], rel, dn)) {
            inotify_rm_watch(w->fd, wd);
            free(w->wd_path[wd]);
            w->wd_path[wd] = NULL;
        }
    for (size_t i = 0; gone && i < w->lines.cap; i++)
        if (w->lines.keys[i] && w->lines.vals[i] > 0 && under(w->lines.keys[i], rel, dn))
            note(w, w->lines.keys[i], now, false);
}

void watch_poll(Watch *w, double now) {
    if (!w->prescanned) prescan(w);
    char buf[64 * 1024] __attribute__((aligned(__alignof__(struct inotify_event))));
    ssize_t n;
    while ((n = read(w->fd, buf, sizeof buf)) > 0) {
        now = now_wall(); /* the caller's `now` predates its transcript reading */
        for (char *p = buf; p < buf + n;) {
            struct inotify_event *ev = (struct inotify_event *)p;
            p += sizeof *ev + ev->len;
            if (ev->mask & IN_Q_OVERFLOW) { /* changes were lost: relearn the baselines */
                if (!w->overflowed) fprintf(stderr, "aigource: inotify queue overflowed, some changes missed\n");
                w->overflowed = true;
                w->prescanned = false;
                add_dir(w, "", now, false); /* dirs created meanwhile were never seen */
                continue;
            }
            if (ev->wd < 0 || ev->wd >= w->nwd || !w->wd_path[ev->wd]) continue;
            if (ev->mask & IN_IGNORED) { /* the watch is gone */
                free(w->wd_path[ev->wd]);
                w->wd_path[ev->wd] = NULL;
                continue;
            }
            if (!ev->len) continue;
            const char *dir = w->wd_path[ev->wd];
            char *rel = xasprintf("%s%s%s", dir, *dir ? "/" : "", ev->name);
            if (*dir && !strcmp(ev->name, ".git") && (ev->mask & (IN_CREATE | IN_MOVED_TO))) {
                char *d = xstrdup(dir); /* drop_dir frees wd_path entries, dir among them */
                drop_dir(w, d, now, false); /* became a nested repo / submodule checkout */
                free(d);
            } else if (ev->mask & IN_ISDIR) {
                if (ev->mask & (IN_CREATE | IN_MOVED_TO)) add_dir(w, rel, now, true);
                else if (ev->mask & (IN_MOVED_FROM | IN_DELETE)) drop_dir(w, rel, now, true);
            } else if (!ignored(w, rel)) {
                /* a file that appears by rename (atomic save, mv) may be new too */
                note(w, rel, now, (ev->mask & (IN_CREATE | IN_MOVED_TO)) != 0);
            }
            free(rel);
        }
    }
}

int watch_take(Watch *w, double now, double settle, WatchChange **out) {
    int n = 0, cap = 0;
    *out = NULL;
    for (int i = 0; i < w->nraw;) {
        Raw r = w->raw[i];
        if (now - r.last < settle) { i++; continue; }
        w->raw[i] = w->raw[--w->nraw];
        int known = strmap_get(&w->lines, r.path);
        int before = known > 0 ? known - 1 : -1; /* stored +1; 0/absent = unknown */
        int lines = rel_lines(w, r.path);
        WatchChange c = {.path = r.path, .ts = r.first, .lines = lines, .before_unknown = before < 0};
        if (lines < 0) { /* gone */
            if (before < 0 && r.created) { free(r.path); continue; } /* temp file: born and died */
            c.kind = WC_DELETE;
            c.lines = 0;
            c.del = before > 0 ? before : 0;
            strmap_put(&w->lines, r.path, 0);
        } else {
            c.kind = r.created && before < 0 ? WC_CREATE : WC_EDIT;
            if (c.kind == WC_CREATE) c.add = lines;
            else if (before >= 0) { c.add = lines > before ? lines - before : 0; c.del = before > lines ? before - lines : 0; }
            strmap_put(&w->lines, r.path, lines + 1);
        }
        PUSH(*out, n, cap, c);
    }
    return n;
}

void watch_defer(Watch *w, WatchChange *c) {
    /* Re-queue as raw with its original time; the line count was already
     * updated, so restore the old one for a correct diff later. */
    if (c->kind == WC_DELETE) strmap_put(&w->lines, c->path, c->del > 0 ? c->del + 1 : 0);
    else if (c->kind == WC_EDIT)
        strmap_put(&w->lines, c->path, c->before_unknown ? 0 : c->lines - c->add + c->del + 1);
    else strmap_put(&w->lines, c->path, 0); /* unknown again: stays a create */
    note(w, c->path, now_wall(), c->kind == WC_CREATE); /* retried after another settle */
    for (int i = 0; i < w->nraw; i++)
        if (!strcmp(w->raw[i].path, c->path)) w->raw[i].first = c->ts;
    free(c->path);
    c->path = NULL;
}
