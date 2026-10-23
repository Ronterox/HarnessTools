#include "pyjson.h"

#include <locale.h>
#include <math.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "util.h"

void pyj_str(Buf *b, const char *s, size_t n) {
    buf_put(b, "\"", 1);
    size_t run = 0; /* bytes copied as they are */
    for (size_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)s[i];
        if (c >= 0x20 && c != '"' && c != '\\') continue;
        buf_put(b, s + run, i - run);
        run = i + 1;
        char e[8];
        switch (c) {
        case '"': buf_put(b, "\\\"", 2); break;
        case '\\': buf_put(b, "\\\\", 2); break;
        case '\n': buf_put(b, "\\n", 2); break;
        case '\r': buf_put(b, "\\r", 2); break;
        case '\t': buf_put(b, "\\t", 2); break;
        case '\b': buf_put(b, "\\b", 2); break;
        case '\f': buf_put(b, "\\f", 2); break;
        default:
            snprintf(e, sizeof e, "\\u%04x", c);
            buf_put(b, e, 6);
        }
    }
    buf_put(b, s + run, n - run);
    buf_put(b, "\"", 1);
}

static locale_t c_loc;
static void make_c_loc(void) { c_loc = newlocale(LC_NUMERIC_MASK, "C", (locale_t)0); }
locale_t c_numeric(void) {
    static pthread_once_t once = PTHREAD_ONCE_INIT;
    pthread_once(&once, make_c_loc);
    return c_loc ? uselocale(c_loc) : uselocale((locale_t)0);
}

static double c_strtod(const char *s) {
    locale_t prev = c_numeric();
    double d = strtod(s, NULL);
    uselocale(prev);
    return d;
}

void pyj_float(Buf *b, double d) {
    if (isnan(d)) { buf_puts(b, "NaN"); return; }
    if (isinf(d)) { buf_puts(b, d < 0 ? "-Infinity" : "Infinity"); return; }
    if (d == 0) { buf_puts(b, signbit(d) ? "-0.0" : "0.0"); return; }
    char e[40];
    locale_t prev = c_numeric();
    for (int p = 1; p <= 17; p++) { /* the shortest digits that read back as d */
        snprintf(e, sizeof e, "%.*e", p - 1, d);
        if (strtod(e, NULL) == d) break;
    }
    uselocale(prev);
    /* e: [-]D[.DDD]e±XX -> digits and the decimal point's position */
    const char *q = e;
    char digits[24];
    int nd = 0, neg = *q == '-';
    if (neg) q++;
    for (; *q && *q != 'e'; q++)
        if (*q != '.') digits[nd++] = *q;
    int decpt = atoi(q + 1) + 1;
    while (nd > 1 && digits[nd - 1] == '0') nd--;
    char o[64];
    int k = 0;
    if (neg) o[k++] = '-';
    if (decpt > -4 && decpt <= 16) { /* repr's fixed notation */
        if (decpt <= 0) {
            o[k++] = '0', o[k++] = '.';
            for (int i = 0; i < -decpt; i++) o[k++] = '0';
            for (int i = 0; i < nd; i++) o[k++] = digits[i];
        } else if (decpt >= nd) {
            for (int i = 0; i < nd; i++) o[k++] = digits[i];
            for (int i = nd; i < decpt; i++) o[k++] = '0';
            o[k++] = '.', o[k++] = '0';
        } else {
            for (int i = 0; i < nd; i++) {
                if (i == decpt) o[k++] = '.';
                o[k++] = digits[i];
            }
        }
        buf_put(b, o, (size_t)k);
    } else {
        o[k++] = digits[0];
        if (nd > 1) {
            o[k++] = '.';
            for (int i = 1; i < nd; i++) o[k++] = digits[i];
        }
        k += snprintf(o + k, sizeof o - (size_t)k, "e%c%02d", decpt - 1 < 0 ? '-' : '+', abs(decpt - 1));
        buf_put(b, o, (size_t)k);
    }
}

static void put_indent(Buf *b, int level) {
    buf_put(b, "\n", 1);
    for (int i = 0; i < level; i++) buf_put(b, " ", 1);
}

typedef struct {
    yyjson_val *k, *v;
} Pair;

static int cmp_key(const void *a, const void *b) {
    yyjson_val *x = ((const Pair *)a)->k, *y = ((const Pair *)b)->k;
    size_t nx = yyjson_get_len(x), ny = yyjson_get_len(y);
    int c = memcmp(yyjson_get_str(x), yyjson_get_str(y), nx < ny ? nx : ny);
    return c ? c : (nx > ny) - (nx < ny);
}

static int dumps(Buf *b, yyjson_val *v, int flags, int level) {
    char num[32];
    if (level > PYJ_MAX_DEPTH) return -1;
    const char *item_sep = flags & (PYJ_COMPACT | PYJ_INDENT1) ? "," : ", ";
    const char *key_sep = flags & PYJ_COMPACT ? ":" : ": ";
    switch (yyjson_get_type(v)) {
    case YYJSON_TYPE_NULL: buf_puts(b, "null"); return 0;
    case YYJSON_TYPE_BOOL: buf_puts(b, yyjson_get_bool(v) ? "true" : "false"); return 0;
    case YYJSON_TYPE_STR: pyj_str(b, yyjson_get_str(v), yyjson_get_len(v)); return 0;
    case YYJSON_TYPE_RAW: { /* a number too big for 64 bits: an int stays exact, a float is +-inf */
        const char *r = yyjson_get_raw(v);
        if (strpbrk(r, ".eE")) pyj_float(b, c_strtod(r));
        else buf_puts(b, r);
        return 0;
    }
    case YYJSON_TYPE_NUM:
        switch (yyjson_get_subtype(v)) {
        case YYJSON_SUBTYPE_UINT: snprintf(num, sizeof num, "%llu", (unsigned long long)yyjson_get_uint(v)); break;
        case YYJSON_SUBTYPE_SINT: snprintf(num, sizeof num, "%lld", (long long)yyjson_get_sint(v)); break;
        default: pyj_float(b, yyjson_get_real(v)); return 0;
        }
        buf_puts(b, num);
        return 0;
    case YYJSON_TYPE_ARR: {
        size_t n = yyjson_arr_size(v);
        if (!n) { buf_puts(b, "[]"); return 0; }
        buf_put(b, "[", 1);
        size_t i, m;
        yyjson_val *x;
        yyjson_arr_foreach(v, i, m, x) {
            if (i) buf_puts(b, item_sep);
            if (flags & PYJ_INDENT1) put_indent(b, level + 1);
            if (dumps(b, x, flags, level + 1)) return -1;
        }
        if (flags & PYJ_INDENT1) put_indent(b, level);
        buf_put(b, "]", 1);
        return 0;
    }
    case YYJSON_TYPE_OBJ: {
        size_t n = yyjson_obj_size(v);
        if (!n) { buf_puts(b, "{}"); return 0; }
        Pair stack[16], *p = n <= 16 ? stack : xmalloc(n * sizeof *p);
        size_t i, m;
        yyjson_val *k, *x;
        yyjson_obj_foreach(v, i, m, k, x) p[i].k = k, p[i].v = x;
        if (flags & PYJ_SORT) qsort(p, n, sizeof *p, cmp_key); /* keys are unique: qsort's instability is moot */
        int rc = 0;
        buf_put(b, "{", 1);
        for (i = 0; i < n && !rc; i++) {
            if (i) buf_puts(b, item_sep);
            if (flags & PYJ_INDENT1) put_indent(b, level + 1);
            pyj_str(b, yyjson_get_str(p[i].k), yyjson_get_len(p[i].k));
            buf_puts(b, key_sep);
            rc = dumps(b, p[i].v, flags, level + 1);
        }
        if (!rc) {
            if (flags & PYJ_INDENT1) put_indent(b, level);
            buf_put(b, "}", 1);
        }
        if (p != stack) free(p);
        return rc;
    }
    default: buf_puts(b, "null"); return 0;
    }
}

int pyj_dumps(Buf *b, yyjson_val *v, int flags) { return dumps(b, v, flags, 0); }

/* ---- sha256 ---- */

static const uint32_t K[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
};

#define ROR(x, n) (((x) >> (n)) | ((x) << (32 - (n))))

static void block(Sha256 *c, const uint8_t *p) {
    uint32_t w[64];
    for (int i = 0; i < 16; i++)
        w[i] = (uint32_t)p[4 * i] << 24 | (uint32_t)p[4 * i + 1] << 16 | (uint32_t)p[4 * i + 2] << 8 | p[4 * i + 3];
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = ROR(w[i - 15], 7) ^ ROR(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = ROR(w[i - 2], 17) ^ ROR(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = c->h[0], b = c->h[1], cc = c->h[2], d = c->h[3], e = c->h[4], f = c->h[5], g = c->h[6], h = c->h[7];
    for (int i = 0; i < 64; i++) {
        uint32_t t1 = h + (ROR(e, 6) ^ ROR(e, 11) ^ ROR(e, 25)) + ((e & f) ^ (~e & g)) + K[i] + w[i];
        uint32_t t2 = (ROR(a, 2) ^ ROR(a, 13) ^ ROR(a, 22)) + ((a & b) ^ (a & cc) ^ (b & cc));
        h = g, g = f, f = e, e = d + t1, d = cc, cc = b, b = a, a = t1 + t2;
    }
    c->h[0] += a, c->h[1] += b, c->h[2] += cc, c->h[3] += d;
    c->h[4] += e, c->h[5] += f, c->h[6] += g, c->h[7] += h;
}

void sha256_init(Sha256 *c) {
    static const uint32_t h0[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                                   0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    memcpy(c->h, h0, sizeof h0);
    c->len = 0;
    c->nbuf = 0;
}

void sha256_update(Sha256 *c, const void *data, size_t n) {
    const uint8_t *p = data;
    c->len += n;
    if (c->nbuf) {
        size_t k = 64 - c->nbuf < n ? 64 - c->nbuf : n;
        memcpy(c->buf + c->nbuf, p, k);
        c->nbuf += k, p += k, n -= k;
        if (c->nbuf < 64) return;
        block(c, c->buf);
        c->nbuf = 0;
    }
    for (; n >= 64; p += 64, n -= 64) block(c, p);
    memcpy(c->buf, p, n);
    c->nbuf = n;
}

void sha256_final(Sha256 *c, uint8_t out[32]) {
    uint64_t bits = c->len * 8;
    uint8_t pad[72] = {0x80};
    size_t k = (c->nbuf < 56 ? 56 : 120) - c->nbuf;
    for (int i = 0; i < 8; i++) pad[k + (size_t)i] = (uint8_t)(bits >> (56 - 8 * i));
    sha256_update(c, pad, k + 8);
    for (int i = 0; i < 8; i++) {
        out[4 * i] = (uint8_t)(c->h[i] >> 24), out[4 * i + 1] = (uint8_t)(c->h[i] >> 16);
        out[4 * i + 2] = (uint8_t)(c->h[i] >> 8), out[4 * i + 3] = (uint8_t)c->h[i];
    }
}

void sha256_hex(const uint8_t d[32], char out[65]) {
    static const char hx[] = "0123456789abcdef";
    for (int i = 0; i < 32; i++) out[2 * i] = hx[d[i] >> 4], out[2 * i + 1] = hx[d[i] & 15];
    out[64] = 0;
}
