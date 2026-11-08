#include <stdio.h>
#include "util.h"

void log_error(const char *msg) {
    fprintf(stderr, "error: %s\n", msg);
}

void usage(void) {
    fputs("usage: mini [-v]\n", stderr);
}

void cleanup(void) {
    fflush(stdout);
    flush_logs(); /* defined nowhere: the one unresolved call */
}
