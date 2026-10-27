#ifndef AIG_SERVE_H
#define AIG_SERVE_H
#include "ingest.h"

/* --serve: no window; one Engine on every session of the root (since 0, no
 * watcher), answering per-file AI blame over a unix socket in Linux's
 * abstract namespace, "\0aigource/<first 16 hex of sha1(root)>" (the prefix
 * from AIG_SOCK_NS when set). It binds before reading any transcript, so of
 * two started at once one serves and the other returns 0 at once. NDJSON,
 * one request per line (README, "Serve"). Exits 0 after AIG_SERVE_IDLE
 * seconds (default 300) with no client, counted from launch and from each
 * last disconnect, or on a quit request. io->root is resolved. */
int serve_run(const IngestOpts *io);

#endif
