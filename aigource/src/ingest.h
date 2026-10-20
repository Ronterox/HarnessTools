#ifndef AIG_INGEST_H
#define AIG_INGEST_H
#include "event.h"

/* Finds the Claude sessions for a repo and tails their transcripts (main
 * sessions + their subagents), pushing Events into a queue. */
typedef struct Ingest Ingest;

typedef struct {
    const char *root;    /* repo root (absolute, resolved) */
    double since;        /* select sessions written to after this epoch; 0 = all */
    const char **files;  /* explicit transcript files (overrides discovery) */
    int nfiles;
    int watch;           /* also watch the filesystem (live only) */
} IngestOpts;

Ingest *ingest_new(const IngestOpts *o, EvQueue *q);
void ingest_poll(Ingest *in);  /* one pass: rescan + read new lines */
int ingest_start(Ingest *in);  /* poll on a background thread; -1 on failure */
void ingest_stop(Ingest *in);
void ingest_free(Ingest *in);
int ingest_nfiles(const Ingest *in);

#endif
