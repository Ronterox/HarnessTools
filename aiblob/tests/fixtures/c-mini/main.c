#include <stdio.h>
#include <stdlib.h>
#include "store.h"
#include "util.h"

int verbose = 0; /* read by store_add too */

static int parse_args(int argc, char **argv) {
    for (int i = 1; i < argc; i++) {
        if (argv[i][0] != '-') {
            log_error("bad argument");
            return -1;
        }
        verbose = 1;
    }
    return 0;
}

int main(int argc, char **argv) {
    if (parse_args(argc, argv) < 0) {
        usage();
        exit(2);
    }
    const char *home = getenv("HOME");
    Store *s = store_open("data/items.txt");
    if (!s) return 1;
    atexit(cleanup);
    store_add(s, home ? home : "nobody");
    dispatch(s, 0);
    store_close(s);
    return 0;
}
