/* Unit tests for nvim.c against real headless Neovims in a throwaway socket
 * directory ($AIG_NVIM_DIR): discovery by working directory (the newest of
 * several), opening a file at a line in the current window, a root with no
 * nvim opening nothing, an nvim error coming back as a hint. Only the nvims
 * started here are touched, and killed. */
#include <limits.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "nvim.h"

static int fails;
#define CHECK(c)                                                               \
  do {                                                                         \
    if (!(c)) {                                                                \
      printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c);                       \
      fails++;                                                                 \
    }                                                                          \
  } while (0)

static char sockdir[64]; /* short: a unix socket path over ~108 bytes fails */
static pid_t pids[4];
static int npids;

static void nap(int ms) {
  nanosleep(&(struct timespec){ms / 1000, ms % 1000 * 1000000L}, NULL);
}

/* A headless nvim working in dir, listening on sockdir/nvim.<pid>.0 (its socket
 * into sock). */
static pid_t spawn(const char *dir, char *sock, size_t n) {
  fflush(stdout); /* not printed twice by the child */
  pid_t pid = fork();
  if (pid == 0) {
    char s[128];
    snprintf(s, sizeof s, "%s/nvim.%d.0", sockdir, (int)getpid());
    if (chdir(dir))
      _exit(127);
    freopen("/dev/null", "r", stdin);
    freopen("/dev/null", "w", stdout);
    freopen("/dev/null", "w",
            stderr); /* "Caught deadly signal SIGTERM" at the end */
    execlp("nvim", "nvim", "--headless", "--clean", "--listen", s,
           (char *)NULL);
    _exit(127);
  }
  pids[npids++] = pid;
  snprintf(sock, n, "%s/nvim.%d.0", sockdir, (int)pid);
  char out[256];
  for (int i = 0; i < 100; i++) { /* up to 5 s for it to answer */
    struct stat st;
    if (!stat(sock, &st) &&
        !nv_exec(sock, "return 'up'", "", 0, 300, out, sizeof out))
      return pid;
    nap(50);
  }
  printf("FAIL nvim in %s never answered on %s\n", dir, sock);
  fails++;
  return pid;
}

/* "name:line:windows:tabs" of the current window of the nvim on sock. */
static void where(const char *sock, char *out, size_t n) {
  char err[PATH_MAX + 64];
  if (nv_exec(
          sock,
          "return vim.api.nvim_buf_get_name(0) .. ':' .. vim.fn.line('.') .. "
          "':' .. "
          "#vim.api.nvim_list_wins() .. ':' .. #vim.api.nvim_list_tabpages()",
          "", 0, 1000, err, sizeof err))
    snprintf(out, n, "(no answer: %s)", err);
  else
    snprintf(out, n, "%s", err);
}

/* Waits up to 2 s for a hint starting with want. */
static bool hint_is(const char *want) {
  char h[PATH_MAX + 64];
  for (int i = 0; i < 40; i++) {
    if (nv_hint(h, sizeof h, NULL, NULL) && !strncmp(h, want, strlen(want)))
      return true;
    nap(50);
  }
  printf("     hint: \"%s\", wanted \"%s…\"\n",
         nv_hint(h, sizeof h, NULL, NULL) ? h : "", want);
  return false;
}

/* A socket file nothing listens on: what a killed nvim leaves behind. */
static void stale_socket(const char *path) {
  struct sockaddr_un a = {.sun_family = AF_UNIX};
  snprintf(a.sun_path, sizeof a.sun_path, "%s", path);
  int fd = socket(AF_UNIX, SOCK_STREAM, 0);
  if (bind(fd, (struct sockaddr *)&a, sizeof a))
    perror(path);
  close(fd);
}

static void lines(const char *path, int n) {
  FILE *f = fopen(path, "w");
  for (int i = 1; i <= n; i++)
    fprintf(f, "line %d\n", i);
  fclose(f);
}

int main(void) {
  signal(SIGPIPE, SIG_IGN);
  snprintf(sockdir, sizeof sockdir, "/tmp/aignv.XXXXXX");
  if (!mkdtemp(sockdir)) {
    perror("mkdtemp");
    return 1;
  }
  setenv("AIG_NVIM_DIR", sockdir, 1); /* never the user's own nvims */
  char root[PATH_MAX], tmp[PATH_MAX], other[PATH_MAX + 16], none[PATH_MAX + 16],
      a[PATH_MAX + 16], b[PATH_MAX + 16];
  snprintf(tmp, sizeof tmp, "%s/root", sockdir);
  mkdir(tmp, 0700);
  if (!realpath(tmp, root)) {
    perror(tmp);
    return 1;
  }
  snprintf(other, sizeof other, "%s/other",
           root); /* inside the root: a cwd below it is not it */
  snprintf(none, sizeof none, "%s/none", sockdir);
  mkdir(other, 0700);
  mkdir(none, 0700);
  snprintf(a, sizeof a, "%s/a.txt", root);
  snprintf(b, sizeof b, "%s/b b.txt", root); /* a space: fnameescape */
  lines(a, 20);
  lines(b, 30);

  char sa[128], so[128], sc[128], got[PATH_MAX + 64], want[PATH_MAX + 64];
  spawn(root, sa, sizeof sa);
  spawn(other, so, sizeof so);

  /* Discovery: by cwd, each root its own; a root with none, nothing. */
  char s[PATH_MAX];
  CHECK(nv_find(root, s, sizeof s) && !strcmp(s, sa));
  CHECK(nv_find(other, s, sizeof s) && !strcmp(s, so));
  CHECK(!nv_find(none, s, sizeof s));

  /* Opening at a line: that buffer current, the cursor on it, still one window
   * and one tab. */
  CHECK(!nv_open_sync(sa, a, 7, got, sizeof got));
  where(sa, got, sizeof got);
  snprintf(want, sizeof want, "%s:7:1:1", a);
  if (strcmp(got, want))
    printf("     got %s\n", got);
  CHECK(!strcmp(got, want));

  /* The same, off the thread, as a click does: its hint says where. */
  nv_open(root, b, 12);
  CHECK(hint_is("nvim: b b.txt:12"));
  where(sa, got, sizeof got);
  snprintf(want, sizeof want, "%s:12:1:1", b);
  if (strcmp(got, want))
    printf("     got %s\n", got);
  CHECK(!strcmp(got, want));

  /* Already the current buffer: the cursor moves, the buffer stays; past the
   * end, the last line. */
  char nb[64], nb2[64];
  nv_exec(sa, "return tostring(vim.api.nvim_get_current_buf())", "", 0, 1000,
          nb, sizeof nb);
  CHECK(!nv_open_sync(sa, b, 999, got, sizeof got));
  nv_exec(sa, "return tostring(vim.api.nvim_get_current_buf())", "", 0, 1000,
          nb2, sizeof nb2);
  CHECK(!strcmp(nb, nb2));
  where(sa, got, sizeof got);
  snprintf(want, sizeof want, "%s:30:1:1", b);
  CHECK(!strcmp(got, want));

  /* No nvim in that root: nothing opened anywhere, the hint says so. */
  nv_open(none, a, 3);
  CHECK(hint_is("no nvim open in "));
  where(sa, got, sizeof got);
  CHECK(!strcmp(got, want));
  where(so, got, sizeof got);
  CHECK(!strcmp(got, ":1:1:1")); /* the other root's nvim: untouched */

  /* An error in nvim comes back as a hint: E37, a modified buffer with
   * 'nohidden'. */
  nv_exec(sa, "vim.o.hidden = false; vim.bo.modified = true", "", 0, 1000, got,
          sizeof got);
  nv_open(root, a, 2);
  CHECK(hint_is("nvim: E37"));
  nv_exec(sa, "vim.bo.modified = false", "", 0, 1000, got, sizeof got);

  /* A name no Ex command can be smuggled in: a newline (":so" on the next
   * line would source .evil.vim), a bar. Each opens as itself. */
  char evil[PATH_MAX + 16], nl[PATH_MAX + 32], bar[PATH_MAX + 32];
  snprintf(evil, sizeof evil, "%s/.evil.vim", root);
  FILE *f = fopen(evil, "w");
  fputs("let g:pwned = 1\n", f);
  fclose(f);
  snprintf(nl, sizeof nl, "%s/x\nso .evil.vim", root);
  snprintf(bar, sizeof bar, "%s/y|so .evil.vim", root);
  lines(nl, 5);
  lines(bar, 5);
  CHECK(!nv_open_sync(sa, nl, 4, got, sizeof got));
  where(sa, got, sizeof got);
  snprintf(want, sizeof want, "%s:4:1:1", nl);
  CHECK(!strcmp(got, want));
  CHECK(!nv_open_sync(sa, bar, 3, got, sizeof got));
  where(sa, got, sizeof got);
  snprintf(want, sizeof want, "%s:3:1:1", bar);
  CHECK(!strcmp(got, want));
  nv_exec(sa, "return tostring(vim.g.pwned)", "", 0, 1000, got, sizeof got);
  CHECK(!strcmp(got, "nil"));

  /* A special current window (a scratch buffer, 'winfixbuf') gives way to
   * the tab's normal one: no window more, no tab. */
  nv_exec(sa, "vim.cmd('vsplit | enew'); vim.bo.buftype = 'nofile'", "", 0,
          1000, got, sizeof got);
  CHECK(!nv_open_sync(sa, a, 9, got, sizeof got));
  where(sa, got, sizeof got);
  snprintf(want, sizeof want, "%s:9:2:1", a);
  if (strcmp(got, want))
    printf("     got %s\n", got);
  CHECK(!strcmp(got, want));
  nv_exec(sa, "vim.cmd('wincmd p'); vim.wo.winfixbuf = true", "", 0, 1000, got,
          sizeof got);
  CHECK(!nv_open_sync(sa, b, 6, got, sizeof got));
  where(sa, got, sizeof got);
  snprintf(want, sizeof want, "%s:6:2:1", b);
  if (strcmp(got, want))
    printf("     got %s\n", got);
  CHECK(!strcmp(got, want));
  nv_exec(sa, "vim.cmd('only!'); vim.wo.winfixbuf = false", "", 0, 1000, got,
          sizeof got);

  /* Clicks faster than nvim answers: the last one wins. */
  nv_open(root, a, 11);
  nv_open(root, b, 21);
  CHECK(hint_is("nvim: b b.txt:21"));
  nap(100);
  where(sa, got, sizeof got);
  snprintf(want, sizeof want, "%s:21:1:1", b);
  if (strcmp(got, want))
    printf("     got %s\n", got);
  CHECK(!strcmp(got, want));

  /* A stale socket whose pid now belongs to something else, working in
   * the root: not an nvim, never picked, however new. */
  fflush(stdout);
  pid_t imp = fork();
  if (imp == 0) {
    if (chdir(root))
      _exit(1);
    pause();
    _exit(0);
  }
  pids[npids++] = imp;
  char fake[128];
  snprintf(fake, sizeof fake, "%s/nvim.%d.0", sockdir, (int)imp);
  stale_socket(fake);
  nap(50);
  CHECK(nv_find(other, s,
                sizeof s)); /* another root first: the cache is per root */
  CHECK(nv_find(root, s, sizeof s) && !strcmp(s, sa));

  /* Two in the root: the newest. */
  nap(50);
  spawn(root, sc, sizeof sc);
  CHECK(nv_find(other, s, sizeof s));
  CHECK(nv_find(root, s, sizeof s) && !strcmp(s, sc));

  /* The newest stops listening (its socket left behind): the next one opens it.
   */
  nv_exec(sc,
          "vim.schedule(function() vim.fn.serverstop(vim.v.servername) end)",
          "", 0, 1000, got, sizeof got);
  struct stat st;
  for (int i = 0; i < 40 && !stat(sc, &st); i++)
    nap(50);
  stale_socket(sc);
  CHECK(nv_find(other, s, sizeof s));
  CHECK(nv_find(root, s, sizeof s) &&
        !strcmp(s, sc)); /* still listed: it is an nvim in the root */
  nv_open(root, a, 5);
  CHECK(hint_is("nvim: a.txt:5"));
  where(sa, got, sizeof got);
  snprintf(want, sizeof want, "%s:5:1:1", a);
  if (strcmp(got, want))
    printf("     got %s\n", got);
  CHECK(!strcmp(got, want));
  nv_shutdown();

  for (int i = 0; i < npids; i++) {
    kill(pids[i], SIGTERM);
    waitpid(pids[i], NULL, 0);
  }
  char cmd[128];
  snprintf(cmd, sizeof cmd, "rm -rf '%s'", sockdir);
  if (system(cmd))
    printf("could not remove %s\n", sockdir);
  if (!fails)
    puts("ok   nvim");
  return fails != 0;
}
