#ifndef UTIL_H
#define UTIL_H

#define LOG(msg) log_error(msg)

void log_error(const char *msg);
void usage(void);
void cleanup(void);

static inline int clampi(int v, int lo, int hi) {
    return v < lo ? lo : v > hi ? hi : v;
}
#endif
