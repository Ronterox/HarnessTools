#include "store.h"
#include "util.h"

typedef struct {
    const char *name;
    int (*fn)(Store *s);
} Handler;

static int on_add(Store *s) {
    store_add(s, "item");
    return 0;
}

static int on_dump(Store *s) {
    if (!s) {
        LOG("no store");
        log_error("no store");
        return -1;
    }
    return store_dump(s);
}

static const Handler HANDLERS[] = {
    {"add", on_add},
    {.name = "dump", .fn = on_dump},
};

void dispatch(Store *s, int i) {
    HANDLERS[i].fn(s);
}
