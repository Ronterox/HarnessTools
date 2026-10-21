#ifndef AIG_NVIM_H
#define AIG_NVIM_H
#include <stdbool.h>
#include <stddef.h>

/* Click code, open it in the user's Neovim. Only an instance whose working
 * directory is the repo root qualifies: Neovim (>= 0.9) listens by default on
 * $XDG_RUNTIME_DIR/nvim.<pid>.0 ($AIG_NVIM_DIR instead when set), that pid
 * must be an nvim, and its /proc/<pid>/cwd the root's realpath; several, the
 * most recently started first. Spoken to with msgpack-RPC over the socket:
 * no nvim client process. Nothing here draws. */

#define NV_HINT_SECS 3.0 /* a hint stays this long */
enum { NV_OK, NV_BUSY, NV_ERR }; /* a hint: opened; sent, no reply yet; not opened */
/* nv_exec's failures */
enum { NV_E_NVIM = -1, NV_E_TIMEOUT = -2, NV_E_CONNECT = -3 };

/* The newest matching instance's socket into sock (may be NULL): false when
 * none. Cached for about 2 s, so a hover can ask every frame. */
bool nv_find(const char *root, char *sock, size_t n);
/* Opens path (absolute) at line in a matching instance, newest first, the
 * next when one doesn't answer: in its current window, or the first window
 * of the tab with a normal buffer when the current one is special (a
 * terminal, a file tree, quickfix, 'winfixbuf'); never a new tab or split.
 * The cursor on the line, centred. Done on one worker thread, the latest
 * request waiting for it (a click replaces one not yet sent); how it went
 * comes back as a hint. With no instance, nothing is opened and the hint
 * says so. */
void nv_open(const char *root, const char *path, int line);
/* Stops the worker (a call in flight is cut off) and joins it; nv_open does
 * nothing afterwards. */
void nv_shutdown(void);
/* The latest hint ("nvim: src/a.c:12", "no nvim open in …", "nvim: E37: …")
 * while it's fresh: its age in seconds into *age, its NV_OK/NV_BUSY/NV_ERR
 * into *kind (either may be NULL); false when none is. */
bool nv_hint(char *buf, size_t n, double *age, int *kind);
/* nvim_exec_lua(code, [path, line]) on socket sock, waiting at most
 * timeout_ms for the reply. 0 when it ran, its result into out when a string
 * ("" otherwise); else an NV_E_* with what went wrong into out. Blocking. */
int nv_exec(const char *sock, const char *code, const char *path, int line, int timeout_ms, char *out, size_t n);
/* What nv_open runs on one socket, blocking: 0, or an NV_E_* with the error into err. */
int nv_open_sync(const char *sock, const char *path, int line, char *err, size_t n);

#endif
