/* The context engine's foundations and API: sha256, Python's json.dumps byte
 * for byte (expected strings written by Python itself), and ctx_* on the
 * fixture project (tests/make_context_fixture.py). */
#include <locale.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "context.h"
#include "pyjson.h"

static int fail;
static const char *what = "";

#define CHECK(cond)                                                     \
    do {                                                                \
        if (!(cond)) {                                                  \
            printf("FAIL context %s:%d: %s\n", what, __LINE__, #cond); \
            fail = 1;                                                   \
        }                                                               \
    } while (0)

static void hex_of(const void *data, size_t n, char out[65]) {
    Sha256 c;
    uint8_t d[32];
    sha256_init(&c);
    sha256_update(&c, data, n);
    sha256_final(&c, d);
    sha256_hex(d, out);
}

/* JSON in; json.dumps out with sort_keys + (',', ':'), default, indent=1; sha256 of the first. */
static const struct { const char *in, *canon, *plain, *indent, *sha; } JS[] = {
    {"{\"b\":1,\"a\":2,\"B\":3,\"_\":4,\"\xc3""\xa9""\":5,\"aa\":6,\"a\\u0000\":7,\"Z\":8,\"\xc3""\xa9""t\xc3""\xa9""\":9,\"\xf0""\x9f""\x98""\x80""\":10,\"\xe2""\x82""\xac""\":11}",
     "{\"B\":3,\"Z\":8,\"_\":4,\"a\":2,\"a\\u0000\":7,\"aa\":6,\"b\":1,\"\xc3""\xa9""\":5,\"\xc3""\xa9""t\xc3""\xa9""\":9,\"\xe2""\x82""\xac""\":11,\"\xf0""\x9f""\x98""\x80""\":10}",
     "{\"b\": 1, \"a\": 2, \"B\": 3, \"_\": 4, \"\xc3""\xa9""\": 5, \"aa\": 6, \"a\\u0000\": 7, \"Z\": 8, \"\xc3""\xa9""t\xc3""\xa9""\": 9, \"\xf0""\x9f""\x98""\x80""\": 10, \"\xe2""\x82""\xac""\": 11}",
     "{\n \"b\": 1,\n \"a\": 2,\n \"B\": 3,\n \"_\": 4,\n \"\xc3""\xa9""\": 5,\n \"aa\": 6,\n \"a\\u0000\": 7,\n \"Z\": 8,\n \"\xc3""\xa9""t\xc3""\xa9""\": 9,\n \"\xf0""\x9f""\x98""\x80""\": 10,\n \"\xe2""\x82""\xac""\": 11\n}",
     "8e3b67be0a16ce5b577b7e7e95ecc4cc142f3bde0364f4979150dfb344a6c769"},
    {"[0,-0,1,-1,18446744073709551615,-9223372036854775808,123456789012345678901234567890,-98765432109876543210987654321]",
     "[0,0,1,-1,18446744073709551615,-9223372036854775808,123456789012345678901234567890,-98765432109876543210987654321]",
     "[0, 0, 1, -1, 18446744073709551615, -9223372036854775808, 123456789012345678901234567890, -98765432109876543210987654321]",
     "[\n 0,\n 0,\n 1,\n -1,\n 18446744073709551615,\n -9223372036854775808,\n 123456789012345678901234567890,\n -98765432109876543210987654321\n]",
     "2342454f0c99393e5f2eceb6b36a5494591d0b691cca70bde3021b59d01540c3"},
    {"[1.0,-0.0,0.0,1e16,1e15,1234567890123456.0,0.0001,1e-05,0.1,123456789.123,1.5e300,5e-324,1e22,2.5e-7,1E+2,3.14159,1e400,-1e400,100.5e-2]",
     "[1.0,-0.0,0.0,1e+16,1000000000000000.0,1234567890123456.0,0.0001,1e-05,0.1,123456789.123,1.5e+300,5e-324,1e+22,2.5e-07,100.0,3.14159,Infinity,-Infinity,1.005]",
     "[1.0, -0.0, 0.0, 1e+16, 1000000000000000.0, 1234567890123456.0, 0.0001, 1e-05, 0.1, 123456789.123, 1.5e+300, 5e-324, 1e+22, 2.5e-07, 100.0, 3.14159, Infinity, -Infinity, 1.005]",
     "[\n 1.0,\n -0.0,\n 0.0,\n 1e+16,\n 1000000000000000.0,\n 1234567890123456.0,\n 0.0001,\n 1e-05,\n 0.1,\n 123456789.123,\n 1.5e+300,\n 5e-324,\n 1e+22,\n 2.5e-07,\n 100.0,\n 3.14159,\n Infinity,\n -Infinity,\n 1.005\n]",
     "f25b4780a6725e5f7640a5ac982f01f8a087bb220a401834ecee11419ed4460d"},
    {"{\"s\":\"caf\xc3""\xa9"" \xe2""\x86""\x92"" \xe2""\x9c""\x93"" \\u0001\\u001f\\u007f a/b \\\"q\\\" \\\\ \\t\\n\\r\\b\\f\",\"u\":\"\xf0""\x9f""\x98""\x80"" tab\\there\"}",
     "{\"s\":\"caf\xc3""\xa9"" \xe2""\x86""\x92"" \xe2""\x9c""\x93"" \\u0001\\u001f\x7f"" a/b \\\"q\\\" \\\\ \\t\\n\\r\\b\\f\",\"u\":\"\xf0""\x9f""\x98""\x80"" tab\\there\"}",
     "{\"s\": \"caf\xc3""\xa9"" \xe2""\x86""\x92"" \xe2""\x9c""\x93"" \\u0001\\u001f\x7f"" a/b \\\"q\\\" \\\\ \\t\\n\\r\\b\\f\", \"u\": \"\xf0""\x9f""\x98""\x80"" tab\\there\"}",
     "{\n \"s\": \"caf\xc3""\xa9"" \xe2""\x86""\x92"" \xe2""\x9c""\x93"" \\u0001\\u001f\x7f"" a/b \\\"q\\\" \\\\ \\t\\n\\r\\b\\f\",\n \"u\": \"\xf0""\x9f""\x98""\x80"" tab\\there\"\n}",
     "36c92901fd85c50faadc83e6020889058638d7d028e30690ce81e47389dca2ac"},
    {"[[],{},[[[]]],[1,[2,[3,{\"z\":[],\"y\":{}}]]],null,true,false]",
     "[[],{},[[[]]],[1,[2,[3,{\"y\":{},\"z\":[]}]]],null,true,false]",
     "[[], {}, [[[]]], [1, [2, [3, {\"z\": [], \"y\": {}}]]], null, true, false]",
     "[\n [],\n {},\n [\n  [\n   []\n  ]\n ],\n [\n  1,\n  [\n   2,\n   [\n    3,\n    {\n     \"z\": [],\n     \"y\": {}\n    }\n   ]\n  ]\n ],\n null,\n true,\n false\n]",
     "218114382de2198c43d774ef5266cf3aa60aa8b3404ed16aa3d403eee81c1480"},
    {"{\"content\":[{\"type\":\"text\",\"text\":\"hi\"},{\"type\":\"tool_use\",\"id\":\"t1\",\"name\":\"Read\",\"input\":{\"file_path\":\"/x/a.c\",\"limit\":2.0}}],\"role\":\"assistant\"}",
     "{\"content\":[{\"text\":\"hi\",\"type\":\"text\"},{\"id\":\"t1\",\"input\":{\"file_path\":\"/x/a.c\",\"limit\":2.0},\"name\":\"Read\",\"type\":\"tool_use\"}],\"role\":\"assistant\"}",
     "{\"content\": [{\"type\": \"text\", \"text\": \"hi\"}, {\"type\": \"tool_use\", \"id\": \"t1\", \"name\": \"Read\", \"input\": {\"file_path\": \"/x/a.c\", \"limit\": 2.0}}], \"role\": \"assistant\"}",
     "{\n \"content\": [\n  {\n   \"type\": \"text\",\n   \"text\": \"hi\"\n  },\n  {\n   \"type\": \"tool_use\",\n   \"id\": \"t1\",\n   \"name\": \"Read\",\n   \"input\": {\n    \"file_path\": \"/x/a.c\",\n    \"limit\": 2.0\n   }\n  }\n ],\n \"role\": \"assistant\"\n}",
     "03c02a90f742ccfa3da94ed21bd510d19e44ba02905b2a973147fccde3db3f62"},
};

static void test_sha256(void) {
    what = "sha256";
    static const struct { size_t n; const char *hex; } V[] = { /* n times 'a' ("abc" for 3) */
        {0, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"},
        {3, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"},
        {55, "9f4390f8d30c2dd92ec9f095b65e2b9ae9b0a925a5258e241c9f1e910f734318"},
        {56, "b35439a4ac6f0948b6d6f9e3c6af0f5f590ce20f1bde7090ef7970686ec6738a"},
        {64, "ffe054fe7ae0cb6dc65c3af9b61d5209f439851db43d0ba5997337df154668eb"},
        {1000000, "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0"},
    };
    char *a = malloc(1000000), hex[65];
    memset(a, 'a', 1000000);
    for (size_t i = 0; i < sizeof V / sizeof *V; i++) {
        hex_of(V[i].n == 3 ? "abc" : a, V[i].n, hex);
        CHECK(!strcmp(hex, V[i].hex));
    }
    /* fed in uneven pieces, as the run hashing does */
    Sha256 c;
    uint8_t d[32];
    sha256_init(&c);
    for (size_t off = 0, k = 1; off < 1000000; off += k, k = k * 7 % 191 + 1)
        sha256_update(&c, a + off, off + k > 1000000 ? 1000000 - off : k);
    sha256_final(&c, d);
    sha256_hex(d, hex);
    CHECK(!strcmp(hex, V[5].hex));
    free(a);
}

static void test_dumps(void) {
    for (size_t i = 0; i < sizeof JS / sizeof *JS; i++) {
        char name[32];
        snprintf(name, sizeof name, "dumps %zu", i);
        what = name;
        yyjson_doc *doc = yyjson_read_opts((char *)JS[i].in, strlen(JS[i].in), PYJ_READ, NULL, NULL);
        CHECK(doc != NULL);
        if (!doc) continue;
        PyBuf b = {0};
        static const int flags[] = {PYJ_CANON, 0, PYJ_INDENT1};
        const char *want[] = {JS[i].canon, JS[i].plain, JS[i].indent};
        for (int k = 0; k < 3; k++) {
            b.n = 0;
            pyj_dumps(&b, yyjson_doc_get_root(doc), flags[k]);
            int ok = b.n == strlen(want[k]) && !memcmp(b.s, want[k], b.n);
            CHECK(ok);
            if (!ok) printf("  got  %.*s\n  want %s\n", (int)b.n, b.s, want[k]);
        }
        char hex[65];
        b.n = 0;
        pyj_dumps(&b, yyjson_doc_get_root(doc), PYJ_CANON);
        hex_of(b.s, b.n, hex);
        CHECK(!strcmp(hex, JS[i].sha));
        pyb_free(&b);
        yyjson_doc_free(doc);
    }
    what = "float repr";
    static const struct { double d; const char *s; } F[] = {
        {1, "1.0"}, {0.1, "0.1"}, {1e16, "1e+16"}, {9999999999999998.0, "9999999999999998.0"}, {1e-4, "0.0001"},
        {1.5e-5, "1.5e-05"}, {-2.5, "-2.5"}, {123.456, "123.456"}, {1e100, "1e+100"}, {0.30000000000000004, "0.30000000000000004"}};
    for (size_t i = 0; i < sizeof F / sizeof *F; i++) {
        PyBuf b = {0};
        pyj_float(&b, F[i].d);
        CHECK(!strcmp(b.s, F[i].s));
        pyb_free(&b);
    }
}

static int find_item(const CtxAgent *a, const char *kind, const char *src) {
    for (int i = 0; i < a->nitems; i++)
        if (!strcmp(ctx_kind_names[a->items[i].kind], kind) && !strcmp(a->items[i].src, src)) return i;
    return -1;
}

static void test_api(const char *fixture, const char *cfg) {
    char *err = NULL, *p;
    what = "find";
    setenv("CLAUDE_CONFIG_DIR", cfg, 1);
    p = ctx_find("/REPO", "s1", &err);
    CHECK(p && strstr(p, "/s1.jsonl"));
    free(p);
    p = ctx_find("/REPO", "sub2", &err); /* a subagent, by its id */
    CHECK(p && strstr(p, "/s1/subagents/agent-sub2.jsonl"));
    free(p);
    p = ctx_find("/REPO", "agent-sub1", &err);
    CHECK(p != NULL);
    free(p);
    p = ctx_find("/REPO", "e", &err); /* a session, by a prefix of its id */
    CHECK(p && strstr(p, "/e1.jsonl"));
    free(p);
    p = ctx_find("/REPO", "su", &err); /* sub1, sub2: ambiguous */
    CHECK(!p && err && strstr(err, "more than one"));
    free(err), err = NULL;
    p = ctx_find("/REPO", "nope", &err);
    CHECK(!p && err);
    free(err), err = NULL;

    char path[512];
    snprintf(path, sizeof path, "%s/s1.jsonl", fixture);
    what = "session agents";
    CtxRef *refs;
    int nr = ctx_session_agents(path, &refs);
    CHECK(nr == 3 && !strcmp(refs[0].id, "s1") && !strcmp(refs[1].id, "sub1") && !strcmp(refs[1].name, "executor"));
    CHECK(nr == 3 && refs[1].desc && !strcmp(refs[1].desc, "Rename helper") && !refs[0].tu &&
          !strcmp(refs[1].tu, "toolu_sub1") && !strcmp(refs[2].id, "sub2"));
    ctx_refs_free(refs, nr);

    what = "load";
    CtxAgent *a = ctx_load("/REPO", path, NULL, NULL, &err);
    CHECK(a && !err);
    if (!a) return;
    CHECK(a->ncalls == 8 && !a->est && !a->missing && a->found_elsewhere == 1);
    CHECK(!strcmp(a->title, "Fix the bug in a.c"));
    /* tokens add up to the call's real usage (rounding aside) */
    what = "tokens";
    for (int c = 0; c < a->ncalls; c++) {
        double sum = 0;
        for (int i = 0; i < a->calls[c].norder; i++) sum += ctx_tokens(a, c, i);
        CHECK(sum > a->calls[c].total - 0.01 && sum < a->calls[c].total + 0.01);
    }
    CHECK(a->calls[3].noresp && a->calls[3].state[0] == CS_NONE && a->calls[3].f == a->calls[2].f);
    CHECK(a->calls[5].comp && a->calls[6].ndrop == 1 && ctx_tokens(a, 6, ctx_pos(a, 6, a->calls[6].drop[0])) == 0);
    what = "read as code";
    int r = find_item(a, "result", "Read src/a.c");
    CHECK(r >= 0);
    if (r >= 0) {
        const CtxItem *it = &a->items[r];
        CHECK(it->line0 == 1 && it->line1 == 2 && it->lang && !strcmp(it->lang, "c") && !strcmp(it->path, "src/a.c"));
        char *buf, **lines;
        int *nums;
        const char *rest;
        int n = ctx_code_lines(it, &buf, &lines, &nums, &rest);
        CHECK(n == 2 && !strcmp(lines[0], "int a;") && nums[1] == 2 && !strcmp(rest, ""));
        free(buf), free(lines), free(nums);
        what = "stale";
        int known;
        CHECK(ctx_stale_at(a, r, 1, NULL, &known) == 0);
        CHECK(ctx_stale_at(a, r, 2, NULL, &known) == 1 && known); /* its own Edit, in context */
        CHECK(ctx_stale_at(a, r, 4, NULL, &known) == 2 && known);  /* and its own command */
        CHECK(ctx_stale_at(a, r, 5, NULL, &known) == 3 && !known); /* r5's Edit, never sent back */
        CHECK(it->nst == 3 && it->st[1].kind == CST_OWN_CMD);
    }
    int b = find_item(a, "result", "Read src/b.py 3-4");
    CHECK(b >= 0 && a->items[b].nst == 3 && a->items[b].st[0].kind == CST_NOTICED &&
          a->items[b].st[1].kind == CST_SESSION && a->items[b].st[2].kind == CST_PROJECT);
    what = "command";
    int s = find_item(a, "action", "Bash cat src/a.c | head -5 && sed -i 's/b(void)/b(int)/' src/a.c");
    CHECK(s >= 0 && a->items[s].cmd && !strncmp(a->items[s].cmd, "cd /REPO && cat", 15) && a->items[s].result >= 0 &&
          a->items[a->items[s].result].cmd == a->items[s].cmd);
    what = "links";
    int e = find_item(a, "action", "Edit src/a.c");
    CHECK(e >= 0 && a->items[e].nlk == 3 && !strcmp(a->items[e].lk[0].why, "read of a.c") && a->items[e].lk[0].item == r);
    CHECK(e >= 0 && !strcmp(a->items[e].old_str, "int a;") && !strcmp(a->items[e].new_str, "int a = 0;"));
    CHECK(s >= 0 && !strcmp(a->items[s].desc, "Show the fix, then change b's signature") && !a->items[s].old_str);
    what = "made";
    CHECK(a->calls[0].nmade == 3 && a->calls[3].nmade == 0);
    for (int c = 0; c < a->ncalls; c++)
        for (int i = 0; i < a->calls[c].nmade; i++) CHECK(a->items[a->calls[c].made[i]].made == c);
    what = "dropped thinking";
    CHECK(a->calls[6].ndrop == 1 && !strcmp(a->items[a->calls[6].drop[0]].text, "And c.h."));
    what = "positions";
    int *by = malloc((size_t)a->nitems * sizeof *by);
    for (int c = 0; c < a->ncalls; c++) {
        ctx_positions(a, c, by);
        for (int i = 0; i < a->nitems; i++) {
            CHECK(by[i] == ctx_pos(a, c, i));
            int k1, k2;
            CHECK(ctx_stale_at(a, i, c, by, &k1) == ctx_stale_at(a, i, c, NULL, &k2) && k1 == k2);
        }
    }
    free(by);
    what = "keys";
    for (int i = 0; i < a->nitems; i++) CHECK(ctx_item_by_key(a, a->items[i].key) == i);
    CHECK(ctx_item_by_key(a, 0) == -1);
    what = "tools";
    int t = find_item(a, "tools", "tool definitions (2)");
    CHECK(t >= 0 && a->items[t].ntx == 2 && !strcmp(a->items[t].names[1], "Bash") &&
          !strncmp(a->str[a->items[t].tx[0]], "{\n \"name\": \"Read\"", 17));
    ctx_free(a);

    what = "estimated";
    snprintf(path, sizeof path, "%s/e1.jsonl", fixture);
    a = ctx_load("/REPO", path, NULL, NULL, &err);
    CHECK(a && a->est && a->ncalls == 3 && a->calls[2].comp && a->items[0].kind == CK_SYSTEM && a->items[0].w > 0);
    ctx_free(a);

    what = "subagent";
    snprintf(path, sizeof path, "%s/s1/subagents/agent-sub1.jsonl", fixture);
    a = ctx_load("/REPO", path, NULL, NULL, &err);
    CHECK(a && !strcmp(a->kind, "subagent") && !strcmp(a->id, "sub1") && !strcmp(a->name, "executor") &&
          !strcmp(a->title, "Fix the bug in a.c") && a->ncalls == 2 && !a->calls[1].comp); /* a boundary, keep 1 */
    ctx_free(a);
    what = "cache boundary"; /* the system block's middle is exactly the cache-read count: read */
    snprintf(path, sizeof path, "%s/s1/subagents/agent-sub2.jsonl", fixture);
    a = ctx_load("/REPO", path, NULL, NULL, &err);
    CHECK(a && a->ncalls == 1 && a->calls[0].f == 1 && a->calls[0].norder == 2 && a->calls[0].state[0] == CS_READ &&
          a->calls[0].state[1] == CS_NEW);
    ctx_free(a);

    what = "cancel";
    atomic_int cancel = 1;
    snprintf(path, sizeof path, "%s/s1.jsonl", fixture);
    a = ctx_load("/REPO", path, NULL, &cancel, &err);
    CHECK(!a && !err);
    a = ctx_load("/REPO", "tests/fixtures/context/none.jsonl", NULL, NULL, &err);
    CHECK(!a && err);
    free(err);
}

static char *json_of(const CtxAgent *a) {
    char *s = NULL;
    size_t n = 0;
    FILE *f = open_memstream(&s, &n);
    ctx_json(f, a, NULL, 0);
    fclose(f);
    return s;
}

static void append(const char *to, const char *from) {
    FILE *in = fopen(from, "r"), *out = fopen(to, "a");
    int c;
    while (in && out && (c = fgetc(in)) != EOF) fputc(c, out);
    if (in) fclose(in);
    if (out) fclose(out);
}

/* A live session: the project cache rescans what grew or was rewritten, and a
 * reload keeps every item's key. Each step equals a fresh load (no cache). */
static void test_reload(const char *fixture, const char *tmp) {
    what = "reload";
    char cmd[2048], s1[1024], s2[1024], extra[1024];
    snprintf(cmd, sizeof cmd, "rm -rf '%s/live' && mkdir -p '%s/live' && cp -R '%s/.' '%s/live/'", tmp, tmp, fixture, tmp);
    CHECK(!system(cmd));
    snprintf(s1, sizeof s1, "%s/live/s1.jsonl", tmp);
    snprintf(s2, sizeof s2, "%s/live/s2.jsonl", tmp);
    snprintf(extra, sizeof extra, "%s/../s1-append.jsonl", fixture);
    char *err = NULL;
    CtxProject *pr = ctx_project_new();
    CtxAgent *a = ctx_load("/REPO", s1, pr, NULL, &err), *b = NULL, *fresh = NULL;
    CHECK(a && a->ncalls == 8);
    if (!a) return;
    for (int step = 0; step < 3; step++) {
        if (step == 0) append(s1, extra); /* its own file grew */
        if (step == 1) { /* another session's file grew: an edit of src/b.py, by a command */
            FILE *f = fopen(s2, "a");
            fputs("{\"type\":\"assistant\",\"timestamp\":\"2026-10-14T10:00:50.000Z\",\"cwd\":\"/REPO\",\"message\":"
                  "{\"id\":\"x2\",\"role\":\"assistant\",\"content\":[{\"type\":\"tool_use\",\"id\":\"xt2\",\"name\":"
                  "\"Bash\",\"input\":{\"command\":\"echo 1 > src/b.py\"}}]}}\n"
                  "{\"type\":\"user\",\"timestamp\":\"2026-10-14T10:00:50.500Z\",\"cwd\":\"/REPO\",\"message\":"
                  "{\"role\":\"user\",\"content\":[{\"type\":\"tool_result\",\"tool_use_id\":\"xt2\",\"content\":\"\"}]}}\n",
                  f);
            fclose(f);
        }
        if (step == 2) { /* rewritten shorter, without the message s1 needs from it: scanned again from the start */
            FILE *f = fopen(s2, "w");
            fputs("{\"type\":\"user\",\"timestamp\":\"2026-10-14T10:01:00.000Z\",\"message\":{\"role\":\"user\","
                  "\"content\":\"hi\"}}\n", f);
            fclose(f);
        }
        b = ctx_reload(a, pr, NULL, &err);
        fresh = ctx_load("/REPO", s1, NULL, NULL, &err);
        CHECK(b && fresh);
        if (!b || !fresh) break;
        char *jb = json_of(b), *jf = json_of(fresh);
        CHECK(!strcmp(jb, jf));
        free(jb), free(jf);
        CHECK(b->ncalls == 9 && b->missing == (step == 2));
        for (int i = 0; i < a->nitems; i++) { /* the same items, by key; the message now missing keeps its key */
            int j = ctx_item_by_key(b, a->items[i].key);
            int gone = step == 2 && strstr(a->items[i].text, "A note only s2 recorded");
            CHECK(j >= 0 && (gone ? b->items[j].kind == CK_MISSING
                                  : b->items[j].kind == a->items[i].kind && !strcmp(b->items[j].text, a->items[i].text)));
        }
        int r = find_item(b, "result", "Read src/b.py 3-4");
        CHECK(r >= 0 && b->items[r].nst == (step == 1 ? 4 : step == 2 ? 2 : 3));
        ctx_free(fresh);
        ctx_free(a);
        a = b;
    }
    ctx_free(a);
    ctx_project_free(pr);
}

/* Under a comma-decimal locale the data is the same: numbers are read and
 * written in the "C" locale. Returns the locale used, NULL when none is
 * installed. */
static const char *test_locale(const char *fixture) {
    what = "locale";
    static const char *const names[] = {"de_DE.UTF-8", "de_DE.utf8", "es_ES.UTF-8", "es_ES.utf8", "fr_FR.UTF-8",
                                        "fr_FR.utf8", "it_IT.utf8", "pt_BR.utf8", "ru_RU.utf8", "nl_NL.utf8"};
    char path[512], *err = NULL;
    snprintf(path, sizeof path, "%s/s1.jsonl", fixture);
    CtxAgent *a = ctx_load("/REPO", path, NULL, NULL, &err);
    char *want = a ? json_of(a) : NULL;
    ctx_free(a);
    const char *used = NULL;
    for (size_t i = 0; i < sizeof names / sizeof *names && !used; i++) {
        if (!setlocale(LC_ALL, names[i])) continue;
        char b[16];
        snprintf(b, sizeof b, "%.1f", 1.5);
        if (!strcmp(b, "1,5")) used = names[i];
        else setlocale(LC_ALL, "C");
    }
    if (used) {
        a = ctx_load("/REPO", path, NULL, NULL, &err);
        CHECK(a && !a->missing && a->found_elsewhere == 1);
        char *got = a ? json_of(a) : NULL;
        CHECK(got && want && !strcmp(got, want));
        int known;
        CHECK(a && ctx_stale_at(a, 8, 2, NULL, &known) == 1);
        free(got);
        ctx_free(a);
        setlocale(LC_ALL, "C");
    }
    free(want);
    return used;
}

/* s3 and odd/ (tests/make_context_fixture.py): thinking drops by indices from
 * the end and past it, a message of 70 thinking blocks, a CRLF read, a change
 * 0.01 s after a call, identifiers past 256 characters, a Write; and what the
 * draft fails on: indices beyond the start, usage near 2^63, nesting 1200 deep. */
static void test_corners(const char *fixture) {
    char path[512], *err = NULL;
    what = "s3";
    snprintf(path, sizeof path, "%s/s3.jsonl", fixture);
    CtxAgent *a = ctx_load("/REPO", path, NULL, NULL, &err);
    CHECK(a && a->ncalls == 4);
    if (!a) return;
    what = "thinking drops";
    static const char *const dropped[] = {NULL, "step 69", "step 69", "step 0"}; /* -1, 100 (mi -4), -70 */
    for (int c = 0; c < 4; c++)
        CHECK(dropped[c] ? a->calls[c].ndrop == 1 && !strcmp(a->items[a->calls[c].drop[0]].text, dropped[c])
                         : a->calls[c].ndrop == 0);
    what = "crlf";
    int r = find_item(a, "result", "Read s3/crlf.c");
    CHECK(r >= 0);
    if (r >= 0) {
        char *buf, **lines;
        int *nums;
        const char *rest;
        CHECK(ctx_code_lines(&a->items[r], &buf, &lines, &nums, &rest) == 0 && a->items[r].line0 == 0);
        what = "stale near a call"; /* the change at t0 + 10.06 (10.1), call 2 at t0 + 10.08: both 10.1 */
        int known;
        CHECK(a->items[r].nst == 1 && ctx_stale_at(a, r, 2, NULL, &known) == 1 && known);
        CHECK(ctx_stale_at(a, r, 1, NULL, &known) == 0);
    }
    what = "write";
    int w = find_item(a, "action", "Write s3/crlf.c");
    CHECK(w >= 0 && a->items[w].content && !strcmp(a->items[w].content, "int z;\n"));
    what = "long names";
    int g = -1;
    for (int i = 0; i < a->nitems && g < 0; i++)
        if (a->items[i].kind == CK_ACTION && a->items[i].tool && !strcmp(a->items[i].tool, "Grep")) g = i;
    CHECK(g >= 0);
    if (g >= 0) {
        int mentions = 0;
        for (int k = 0; k < a->items[g].nlk; k++)
            if (!strncmp(a->items[g].lk[k].why, "mentions ", 9)) {
                mentions++;
                CHECK(strlen(a->items[g].lk[k].why) == 9 + 306 && a->items[g].lk[k].why[9 + 305] == 'a');
            }
        CHECK(mentions == 1); /* the result whose name differs only at character 306 doesn't share it */
    }
    ctx_free(a);

    what = "odd drops and usage";
    snprintf(path, sizeof path, "%s/../odd/drops.jsonl", fixture);
    a = ctx_load("/REPO", path, NULL, NULL, &err);
    CHECK(a && a->ncalls == 2 && a->calls[1].ndrop == 1 && !strcmp(a->items[a->calls[1].drop[0]].text, "a"));
    CHECK(a && a->calls[0].u[0] == 1000000000000000LL && a->calls[0].total > 0 && a->calls[0].total < 1e17);
    ctx_free(a);
    what = "odd depth";
    snprintf(path, sizeof path, "%s/../odd/deep.jsonl", fixture);
    a = ctx_load("/REPO", path, NULL, NULL, &err);
    int d = a ? find_item(a, "action", "Skill [nested too deeply to show]") : -1;
    CHECK(a && a->est && a->ncalls == 2 && d >= 0 && !strcmp(a->items[d].text, "[nested too deeply to show]"));
    ctx_free(a);
    free(err);
}

/* Loads on worker threads, two at once and sharing a project, as the screen
 * will run them. */
static CtxProject *shared;
static void *load_one(void *path) {
    char *err = NULL;
    CtxAgent *a = ctx_load("/REPO", path, shared, NULL, &err);
    int n = a ? a->ncalls : -1;
    ctx_free(a);
    free(err);
    return (void *)(long)n;
}

static void test_threads(const char *fixture) {
    what = "threads";
    char p1[512], p2[512];
    snprintf(p1, sizeof p1, "%s/s1.jsonl", fixture);
    snprintf(p2, sizeof p2, "%s/e1.jsonl", fixture);
    pthread_t t1, t2;
    void *r1, *r2;
    shared = ctx_project_new();
    CHECK(!pthread_create(&t1, NULL, load_one, p1) && !pthread_create(&t2, NULL, load_one, p2));
    pthread_join(t1, &r1);
    pthread_join(t2, &r2);
    CHECK((long)r1 == 8 && (long)r2 == 3);
    ctx_project_free(shared);
}

int main(int argc, char **argv) {
    if (argc < 3) {
        fputs("usage: test_context FIXTURE_PROJECT_DIR CONFIG_DIR (with projects/-REPO linked to the fixture)\n", stderr);
        return 2;
    }
    test_sha256();
    test_dumps();
    test_api(argv[1], argv[2]);
    test_threads(argv[1]);
    test_corners(argv[1]);
    test_reload(argv[1], argv[2]);
    const char *loc = test_locale(argv[1]);
    if (!fail) printf("ok   context (sha256, json.dumps, api, reload; %s%s)\n", loc ? "loaded under " : "no comma-decimal locale installed: locale check skipped", loc ? loc : "");
    return fail;
}
