#ifndef STORE_H
#define STORE_H
#include <stdio.h>

typedef struct Store {
    FILE *f;
    int n;
} Store;

struct Stats {
    int adds;
};
extern struct Stats stats;

Store *store_open(const char *path);
void store_add(Store *s, const char *item);
int store_dump(Store *s);
void store_close(Store *s);
void store_sort(char **v, int n);
void dispatch(Store *s, int i);
#endif
