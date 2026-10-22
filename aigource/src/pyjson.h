#ifndef AIG_PYJSON_H
#define AIG_PYJSON_H
#include <stddef.h>
#include <stdint.h>

#include "yyjson.h"

/* Python's json.dumps, byte for byte, for values yyjson read with PYJ_READ
 * (ensure_ascii=False always): Claude Code's request blobs are addressed by
 * sha256 of json.dumps(message, sort_keys=True, separators=(',', ':'),
 * ensure_ascii=False), and the context view's draft sizes items by the length
 * of other dumps. Numbers keep Python's types: an integer stays exact however
 * large (yyjson keeps big ones raw), a float is written as float.__repr__
 * (shortest round trip; exponent past 1e16 or below 1e-4), Infinity and NaN
 * as Python writes them. Strings escape '"', '\\' and control characters
 * (\n \r \t \b \f, the rest \u00xx), nothing else. Sorted keys compare as
 * code points, which is UTF-8 byte order. */
#define PYJ_READ (YYJSON_READ_BIGNUM_AS_RAW | YYJSON_READ_ALLOW_INF_AND_NAN)

enum {
    PYJ_SORT = 1,    /* sort_keys=True */
    PYJ_COMPACT = 2, /* separators=(',', ':'); default (', ', ': ') */
    PYJ_INDENT1 = 4, /* indent=1: separators (',', ': '), one space per level */
};
#define PYJ_CANON (PYJ_SORT | PYJ_COMPACT)

typedef struct {
    char *s;
    size_t n, cap;
} PyBuf;

void pyb_put(PyBuf *b, const char *s, size_t n);
void pyb_puts(PyBuf *b, const char *s);
void pyb_free(PyBuf *b);

/* Returns 0, or -1 for a value nested deeper than PYJ_MAX_DEPTH (b then holds
 * a partial dump): Python's own limit is about as deep, and the recursion
 * here must not overflow the stack. Such a value can't be hashed. */
#define PYJ_MAX_DEPTH 1000
int pyj_dumps(PyBuf *b, yyjson_val *v, int flags);
void pyj_str(PyBuf *b, const char *s, size_t n); /* a string, quoted */
void pyj_float(PyBuf *b, double d);              /* float.__repr__ */

/* sha256 (FIPS 180-4). */
typedef struct {
    uint32_t h[8];
    uint64_t len;
    uint8_t buf[64];
    size_t nbuf;
} Sha256;
void sha256_init(Sha256 *c);
void sha256_update(Sha256 *c, const void *data, size_t n);
void sha256_final(Sha256 *c, uint8_t out[32]);
void sha256_hex(const uint8_t d[32], char out[65]);

#endif
