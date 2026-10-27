#ifndef AIG_SERVE_H
#define AIG_SERVE_H
#include <stdbool.h>

#include "engine.h"
#include "ingest.h"

/* AI blame for a root's files over a unix socket in Linux's abstract
 * namespace, "\0aigource/<first 16 hex of sha1(root)>" (the prefix from
 * AIG_SOCK_NS when set): NDJSON, one request per line (README, "Serve").
 * Answers from an Engine whose ENG_SERVE part is srv_part(). Headless with
 * --serve (serve_run), or hosted by a GUI started with -a (render.c). */
typedef struct Server Server;

#define SRV_QUIET 1.0 /* a serving engine rebuilds this long after the last create, edit, delete or turn... */
#define SRV_CAP 3.0   /* ...or once the first of them waited this long (EngOpts.changes) */

/* What a GUI host shows after a focus request, read back from the screen:
 * the inspected file (path below the root), the selected turn (an index of
 * the latest build, -1 none), the lit line (0 none). */
typedef struct { char file[1024]; int turn, line; } SrvShown;
/* A GUI host's focus request: turn (an index of the engine's latest build)
 * and file (its path below the root, both known to the build) shown, line
 * lit; what shows then into *shown. */
typedef void (*SrvFocus)(void *ctx, int turn, const char *file, int line, SrvShown *shown);

EngPart srv_part(void);
/* Not bound yet; focus NULL: headless. NULL when AIG_SOCK_NS makes the name too long. */
Server *srv_new(const char *root, Engine *e, SrvFocus focus, void *ctx);
/* 0 bound; 1 the name is another process's; -1 an error (errno). */
int srv_bind(Server *s);
/* A GUI host's takeover, one step that never blocks (call it every frame):
 * bind when the name is free, else ask whoever holds it to quit and bind as
 * soon as its listener is gone (trying every 20 ms for 5 s), with a backoff
 * from 50 ms to 1 s between askings; a server started in the gap is asked
 * again. A holder that doesn't go in 5 s is reported once on stderr and asked
 * again 5 s to 60 s apart. After srv_release (this host was asked to quit by
 * another) it only takes the name when it is free, every RETAKE_WAIT s: the
 * newest host wins, an earlier one hosts again only once the name is free.
 * True once bound. */
bool srv_take(Server *s);
bool srv_bound(const Server *s);
/* Clients' I/O, waiting up to ms for it (0: none): accepts, reads, answers,
 * drops the ones gone. */
void srv_poll(Server *s, int ms);
/* A build just swapped in; old: the ENG_SERVE part before it (NULL before the
 * first), not yet dropped. Pushes `changed` to subscribers. */
void srv_swapped(Server *s, const void *old);
bool srv_quitting(const Server *s); /* a client sent quit */
/* For quit, never blocking: releases the name, queues the handover to every
 * client; srv_poll sends it and closes each once it is read (2 s at most). */
void srv_release(Server *s);
int srv_clients(const Server *s);
double srv_idle(const Server *s); /* seconds since the last client left (or since srv_new), 0 with clients */
/* Closes the clients (they see EOF) and the socket. */
void srv_free(Server *s);

/* --serve: no window; one Engine on every session of the root (since 0, no
 * watcher). It binds before reading any transcript, so of two started at
 * once one serves and the other returns 0 at once. Exits 0 after
 * AIG_SERVE_IDLE seconds (default 300) with no client, counted from launch
 * and from each last disconnect, or on a quit request. io->root is resolved. */
int serve_run(const IngestOpts *io);

#endif
