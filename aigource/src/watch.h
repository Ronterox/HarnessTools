#ifndef AIG_WATCH_H
#define AIG_WATCH_H
#include <stdbool.h>

/* Opt-in filesystem watcher (--watch): what changed in the repo, whoever did it.
 * Live only: nothing records past filesystem changes. Raw inotify events are
 * coalesced per file and released once the file has been quiet for a moment;
 * attribution (agent vs you) is the caller's job (ingest.c). */
typedef struct Watch Watch;

typedef enum { WC_CREATE, WC_EDIT, WC_DELETE } WatchKind;

typedef struct {
    char *path;   /* repo-relative */
    double ts;    /* first change in the burst */
    WatchKind kind;
    int lines;    /* now, -1 unknown (0 for deletes) */
    int add, del; /* from the line-count change; 0/0 when the old count was unknown */
} WatchChange;

Watch *watch_new(const char *root); /* NULL (with a message) when inotify fails */
void watch_poll(Watch *w, double now);
/* Changes quiet for `settle` seconds. *out is caller-owned (free paths + array). */
int watch_take(Watch *w, double now, double settle, WatchChange **out);
/* Puts a change back (attribution has to wait, e.g. on a running command). */
void watch_defer(Watch *w, WatchChange *c);

#endif
