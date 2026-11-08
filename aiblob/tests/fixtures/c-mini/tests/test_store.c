#include <assert.h>
#include "store.h"

#define CHECK(x) check((x), #x)

static int failures;

static void check(int ok, const char *what) {
    if (!ok) failures++;
    (void)what;
}

int main(void) {
    Store *s = store_open("data/items.txt");
    CHECK(s != NULL);
    store_add(s, "a");
    char *v[] = {"b", "a"};
    store_sort(v, 2);
    store_close(s);
    return failures ? 1 : 0;
}
