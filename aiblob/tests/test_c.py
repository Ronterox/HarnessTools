"""C through analyze(): the c-mini fixture exactly, plus one C heuristic per test."""
import os

import pytest

pytest.importorskip('tree_sitter_c')

from conftest import run, quals, call, edges_of

MINI = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'c-mini')


def load(root):
    files = {}
    for dp, _, fn in os.walk(root):
        for n in fn:
            p = os.path.join(dp, n)
            with open(p, encoding='utf-8') as fh:
                files[os.path.relpath(p, root).replace(os.sep, '/')] = fh.read()
    return dict(sorted(files.items()))


@pytest.fixture(scope='module')
def mini():
    return run(load(MINI))


def test_mini_blobs(mini):
    r, blobs, _ = mini
    assert r['entry'] == 'main'
    got = {b['id']: (b['kind'], b['name'], b['module'], quals(b)) for b in blobs.values()}
    assert got == {
        # main() and the file-scope code are one entry; everything only main reaches joins it
        'main.<module>': ('entry', 'main', 'main', ['handlers.dispatch', 'main.<module>', 'main.main', 'main.parse_args',
                                                    'store.store_close', 'store.store_open', 'util.cleanup', 'util.usage']),
        # only ever reached from the file-scope HANDLERS table: hooks named after themselves
        'handlers.on_add': ('hook', 'on_add', 'handlers', ['handlers.on_add']),
        'handlers.on_dump': ('hook', 'on_dump', 'handlers', ['handlers.on_dump', 'store.store_dump']),
        'hub:store': ('hub', 'store', 'store', ['store.store_add']),            # main and on_add
        'hub:util': ('hub', 'util', 'util', ['util.log_error']),                # main and on_dump
        'hub:util_h': ('hub', 'util_h', 'util_h', ['util_h.clampi']),           # a header's static inline
        'orphan:store': ('orphan', 'store', 'store', ['store.cmp_items', 'store.store_sort']),   # only the test calls it
        'tests.test_store': ('tests', 'test_store', 'tests.test_store', ['tests.test_store.check', 'tests.test_store.main']),
        'file:data/items.txt': ('file', 'items.txt', 'data', []),
    }


def test_mini_errors_and_data(mini):
    _, blobs, _ = mini
    assert blobs['main.<module>']['errors'] == 3       # parse_args' return -1, exit(2), store_open's return NULL
    assert blobs['handlers.on_dump']['errors'] == 1    # return -1
    assert blobs['main.<module>']['data'] == ['env:HOME', 'global:handlers.HANDLERS',   # dispatch reads the table
                                              'global:store.nopen', 'global:verbose']
    assert blobs['hub:store']['data'] == ['global:stats.adds', 'global:verbose']     # extern struct, field access
    assert blobs['tests.test_store']['data'] == ['global:tests.test_store.failures']  # static: qualified by module


def test_mini_edges(mini):
    _, _, edges = mini
    assert [(e['a'], e['b'], e['kind'], e['n'], e.get('via')) for e in edges] == [
        ('handlers.on_add', 'hub:store', 'main', 1, None),
        ('hub:store', 'hub:util_h', 'main', 1, None),
        ('main.<module>', 'hub:store', 'main', 1, None),
        ('tests.test_store', 'hub:store', 'main', 1, None),
        ('tests.test_store', 'main.<module>', 'main', 2, None),
        ('tests.test_store', 'orphan:store', 'main', 1, None),
        ('handlers.on_dump', 'hub:util', 'error', 1, None),          # log_error before return -1
        ('main.<module>', 'hub:util', 'error', 2, None),             # parse_args and store_open's error branches
        ('hub:store', 'main.<module>', 'data', 1, ['global:verbose']),
        ('main.<module>', 'file:data/items.txt', 'ref', 1, ['data/items.txt']),
        ('tests.test_store', 'file:data/items.txt', 'ref', 1, ['data/items.txt']),
    ]


def test_mini_calls(mini):
    _, blobs, _ = mini
    main = blobs['main.<module>']
    assert call(main, 'main.main', 'util.cleanup') == {'to': 'util.cleanup', 'error': False, 'deferred': True}   # atexit
    assert call(main, 'main.main', 'util.usage') == {'to': 'util.usage', 'error': True}    # before exit(2)
    assert call(main, 'main.parse_args', 'util.log_error') == {'to': 'util.log_error', 'error': True}
    assert call(main, 'main.main', 'store.store_add') == {'to': 'store.store_add', 'error': False}
    assert call(blobs['orphan:store'], 'store.store_sort', 'store.cmp_items')['deferred'] is True   # qsort(..., cmp)


def test_mini_unresolved(mini):
    r, _, _ = mini
    # 17 calls that can be the program's own; the miss is flush_logs(). exit / printf / LOG(...) never count
    assert (r['stats']['calls'], r['stats']['unresolved']) == (17, 1)


def test_header_includes_pair_with_their_c():
    r, _, _ = run({'src/a.c': '#include "b.h"\nint main(void) { return b(); }\n',
                   'src/b.h': 'int b(void);\n', 'src/b.c': '#include "b.h"\nint b(void) { return 1; }\n'})
    assert r['modules']['src.a']['imports'] == ['src.b', 'src.b_h']
    assert r['modules']['src.b']['imports'] == ['src.b_h']


def test_static_functions_resolve_only_within_their_file():
    r, blobs, _ = run({
        'a.c': 'static int helper(void) { return 1; }\nint a(void) { return helper(); }\n',
        'b.c': 'static int helper(void) { return 2; }\nint b(void) { return helper(); }\n',
        'main.c': 'int a(void); int b(void);\nint main(void) { return a() + b() + helper(); }\n',
    })
    main = blobs['main.<module>']
    assert call(main, 'a.a', 'a.helper') and call(main, 'b.b', 'b.helper')
    assert [c['to'] for c in next(f for f in main['funcs'] if f['qual'] == 'main.main')['calls']] == ['a.a', 'b.b']
    assert r['stats']['unresolved'] == 1          # main's helper(): both are static elsewhere


def test_deferred_registrations_inside_functions():
    _, blobs, _ = run({'main.c': '''#include <pthread.h>
#include <signal.h>
static void on_sig(int s) { (void)s; }
static void *worker(void *p) { return p; }
struct ops { void (*run)(void); };
static void tick(void) {}
int main(void) {
    pthread_t t;
    signal(SIGINT, on_sig);
    pthread_create(&t, NULL, worker, NULL);
    struct ops o = {.run = tick};
    o.run();
    return 0;
}
'''})
    main = blobs['main.<module>']
    for to in ('main.on_sig', 'main.worker', 'main.tick'):
        assert call(main, 'main.main', to) == {'to': to, 'error': False, 'deferred': True}
    assert not any(b['kind'] == 'hook' for b in blobs.values())    # registered from a function: not a table


def test_goto_fail_and_error_tails():
    _, blobs, edges = run({
        'main.c': '''#include "log.h"
int load(const char *p) {
    if (!p) goto fail;
    if (p[0] == 0) { warn_user(p); return 0; }
    note(p);
    return 0;
fail:
    report(p);
    return -1;
}
int main(void) { return load("x"); }
''',
        'log.h': 'void report(const char *); void note(const char *); void warn_user(const char *);\n',
        'log.c': 'void report(const char *m) {}\nvoid note(const char *m) {}\nvoid warn_user(const char *m) {}\n',
    })
    main = blobs['main.<module>']
    assert call(main, 'main.load', 'log.report')['error'] is True      # after the fail: label
    assert call(main, 'main.load', 'log.note')['error'] is False
    assert call(main, 'main.load', 'log.warn_user')['error'] is False  # its branch ends in return 0
    assert next(f for f in main['funcs'] if f['qual'] == 'main.load')['errors'] == 1


def test_file_env_and_struct_field_data():
    _, blobs, _ = run({
        'main.c': '''#include <stdio.h>
#include <stdlib.h>
#include "cfg.h"
struct conf g_conf;
int main(void) {
    FILE *f = fopen("conf/app.ini", "r");
    g_conf.port = atoi(getenv("PORT"));
    g_conf.host = "localhost";
    return f == NULL;
}
''',
        'cfg.h': 'struct conf { int port; const char *host; };\nextern struct conf g_conf;\n',
        'conf/app.ini': 'port=1\n',
    })
    main = blobs['main.<module>']
    assert main['data'] == ['env:PORT', 'file:conf/app.ini', 'global:g_conf.host', 'global:g_conf.port']
    assert blobs['file:conf/app.ini']['kind'] == 'file'


def test_macro_calls_and_bodies():
    r, blobs, _ = run({'main.c': '''#define TWICE(f) do { f(); f(); } while (0)
#define bump(x) ((x) + undefined_in_macro())
void INIT(void) {}
static void step(void) {}
int main(void) {
    INIT();
    TWICE(step);
    return bump(1) + UNKNOWN_MACRO(2);
}
'''})
    main = blobs['main.<module>']
    assert call(main, 'main.main', 'main.INIT') == {'to': 'main.INIT', 'error': False}     # a caps name the program defines
    assert call(main, 'main.main', 'main.step')['deferred'] is True                       # named in a macro call
    assert r['stats']['unresolved'] == 0          # TWICE / bump / UNKNOWN_MACRO are macros; their bodies are skipped


def test_typeof_compound_literal_does_not_swallow_the_file():
    _, blobs, _ = run({'main.c': '''struct p { int a; } *v;
static void push(void) { v[0] = (__typeof__(*v)){1}; }
static void later(void) {}
int main(void) { push(); later(); return 0; }
'''})
    assert quals(blobs['main.<module>']) == ['main.<module>', 'main.later', 'main.main', 'main.push']


def test_missing_library_headers_make_their_names_external():
    from analyze import analyze
    files = {
        'src/ui.h': '#include "raylib.h"\nvoid ui_draw(void);\n',
        'src/ui.c': '#include "ui.h"\nvoid ui_draw(void) { DrawText("x", 0, 0, 10, BLACK); }\n',   # raylib via ui.h
        'src/main.c': '''#include "ui.h"
#include "yyjson.h"
#include <tree_sitter/api.h>
int main(void) {
    yyjson_doc *d = yyjson_read("{}", 2, 0);       /* yyjson.h -> yyjson_* */
    ts_parser_new();                               /* <tree_sitter/api.h> -> ts_* */
    ui_draw();
    DrawCircle(1, 1, 1, RED);                      /* raylib.h only through ui.h: still raylib's */
    not_from_any_header();                         /* the one miss */
    return yyjson_doc_free(d), 0;
}
''',
    }
    r, _, _ = run(files)
    assert (r['stats']['calls'], r['stats']['unresolved']) == (2, 1)        # ui_draw placed, the miss counted
    # once yyjson.h is in the snapshot it is the program's own: yyjson_read / yyjson_doc_free it lacks are misses,
    # and an incremental run (memo) notices the new header like a fresh one does
    memo, cache = {}, {}
    first = analyze(files, cache, memo)
    assert first['stats']['unresolved'] == 1
    with_h = dict(files, **{'src/yyjson.h': 'typedef struct yyjson_doc yyjson_doc;\n'})
    inc, fresh = analyze(with_h, cache, memo), analyze(with_h)
    assert (inc['stats']['calls'], inc['stats']['unresolved']) == (fresh['stats']['calls'], fresh['stats']['unresolved']) == (4, 3)
    back = analyze(files, cache, memo)                                       # and forgets it again
    assert back['stats']['unresolved'] == 1


@pytest.mark.parametrize('line', ['    printf("typeof(\\n");', '    /* GNU typeof( is used */', "    char c = '('; // typeof(",
                                  '    int n = typeof(a;'])
def test_typeof_in_strings_comments_or_unclosed_is_left_alone(line):
    _, blobs, _ = run({'main.c': f'''#include <stdio.h>
static void first(void) {{
{line}
}}
static void second(void) {{}}
int main(void) {{ first(); second(); return 0; }}
'''})
    assert quals(blobs['main.<module>']) == ['main.first', 'main.main', 'main.second']


def _program(cmds, root='main', via=None):
    """A C program whose main (through `via` when given) calls each of `cmds`; every command calls its own six
    3-line steps and the shared log_msg (main calls it too, so it is a helper, never a command)."""
    files = {'log.c': 'void log_msg(const char *m) {\n    (void)m;\n}\n'}
    for c in cmds:
        steps = ''.join(f'static void {c}_step{i}(void) {{\n    log_msg("{c}{i}");\n}}\n' for i in range(6))
        body = ''.join(f'    {c}_step{i}();\n' for i in range(6))
        files[f'{c}.c'] = f'void log_msg(const char *m);\n{steps}void {c}(void) {{\n{body}    log_msg("{c}");\n}}\n'
    protos = ''.join(f'void {c}(void);\n' for c in cmds)
    calls = ''.join(f'    {c}();\n' for c in cmds)
    if via:
        files[f'{via}.c'] = f'{protos}void {via}(void) {{\n{calls}}}\n'
        protos, calls = f'void {via}(void);\n', f'    {via}();\n'
    files['main.c'] = (f'void log_msg(const char *m);\n{protos}static int one(void) {{ return 1; }}\n'
                       f'int {root}(void) {{\n    log_msg("start");\n{calls}    return one();\n}}\n')
    return files


def test_big_entry_splits_at_mains_callees():
    _, blobs, edges = run(_program(['load', 'draw', 'save']))
    got = {b['id']: (b['kind'], b['name'], len(b['funcs'])) for b in blobs.values()}
    assert got == {
        'main.<module>': ('entry', 'main', 2),        # main and the 1-line one(): tiny, stays
        'load.load': ('command', 'load', 7), 'draw.draw': ('command', 'draw', 7), 'save.save': ('command', 'save', 7),
        'hub:log': ('hub', 'log', 1),                 # log_msg: main and every command reach it
    }
    assert blobs['main.<module>']['depth'] == 1
    assert 'depth' not in blobs['load.load']
    assert quals(blobs['main.<module>']) == ['main.main', 'main.one']
    assert {(e['a'], e['b']) for e in edges if e['kind'] == 'main'} >= {('main.<module>', 'draw.draw'), ('draw.draw', 'hub:log')}


def test_one_main_loop_callee_splits_one_level_deeper():
    _, blobs, _ = run(_program(['draw', 'update', 'handle'], via='run'))
    got = {b['id']: (b['kind'], len(b['funcs'])) for b in blobs.values()}
    assert got == {'main.<module>': ('entry', 3),       # main, one() and run(): the main loop stays with main
                   'draw.draw': ('command', 7), 'update.update': ('command', 7), 'handle.handle': ('command', 7),
                   'hub:log': ('hub', 1)}
    assert blobs['main.<module>']['depth'] == 2
    assert quals(blobs['main.<module>']) == ['main.main', 'main.one', 'run.run']


def test_small_entry_is_not_split(mini):
    _, blobs, _ = mini
    assert 'depth' not in blobs['main.<module>']           # 8 functions: below CLang.split_min
    _, blobs, _ = run(_program(['load', 'draw']))          # 2 commands + main: 17 functions
    assert [b['kind'] for b in blobs.values()] == ['entry']


def _cmd(name, n):
    """name.c: name() calls its n own 3-line steps and log_msg."""
    body = ''.join(f'static void {name}_s{i}(void) {{\n    log_msg("{i}");\n}}\n' for i in range(n))
    calls = ''.join(f'    {name}_s{i}();\n' for i in range(n))
    return f'void log_msg(const char *m);\n{body}void {name}(void) {{\n{calls}    log_msg("{name}");\n}}\n'


@pytest.mark.parametrize('steps, depth', [(7, 2), (5, 1)])
def test_ten_callees_one_owning_most_splits_one_level_deeper(steps, depth):
    # main calls 9 small commands (2 functions each) and loop(), whose 3 parts have `steps` steps each:
    # loop owns 4 + 3 * steps of the entry's 25 + 3 * steps functions: 25 / 46 = 54% (7 steps), 19 / 40 = 47.5% (5)
    small, parts = [f'c{i}' for i in range(9)], ['draw', 'update', 'handle']
    files = {f'{c}.c': _cmd(c, 1) for c in small}
    files.update({f'{p}.c': _cmd(p, steps) for p in parts})
    files['log.c'] = 'void log_msg(const char *m) {\n    (void)m;\n}\n'
    files['loop.c'] = ''.join(f'void {p}(void);\n' for p in parts) + \
        'void loop(void) {\n' + ''.join(f'    {p}();\n' for p in parts) + '}\n'
    files['main.c'] = 'void log_msg(const char *m);\n' + ''.join(f'void {c}(void);\n' for c in small + ['loop']) + \
        'int main(void) {\n    log_msg("start");\n' + ''.join(f'    {c}();\n' for c in small + ['loop']) + '    return 0;\n}\n'
    _, blobs, _ = run(files)
    entry = blobs['main.<module>']
    assert entry['depth'] == depth
    cmds = sorted(b['name'] for b in blobs.values() if b['kind'] == 'command')
    if depth == 2:      # loop stays with main, its parts are the commands next to the 9 small ones
        assert cmds == sorted(small + parts) and quals(entry) == ['loop.loop', 'main.main']
        assert len(blobs['draw.draw']['funcs']) == steps + 1
    else:
        assert cmds == sorted(small + ['loop']) and quals(entry) == ['main.main']
    assert blobs['hub:log']['kind'] == 'hub'


def _loop_program(run_body, extra=''):
    """main calls run() (body run_body) and log_msg; draw / update / handle have 7 steps each."""
    files = {f'{p}.c': _cmd(p, 7) for p in ('draw', 'update', 'handle')}
    files['log.c'] = 'void log_msg(const char *m) {\n    (void)m;\n}\n'
    files['run.c'] = 'void draw(void); void update(void); void handle(void); void shared(void);\n' + run_body
    files['main.c'] = 'void log_msg(const char *m); void run(void);\n' + extra
    return files


def test_depth2_helper_counts_the_depth1_commands_as_callers():
    # shared() is called by run() and by cfg() / cfg2(), main's other commands: a helper (hub), not a command
    files = _loop_program('void run(void) {\n    draw();\n    update();\n    handle();\n    shared();\n}\n',
                          'void shared(void);\nvoid cfg(void) {\n    shared();\n}\nvoid cfg2(void) {\n    shared();\n}\n'
                          'int main(void) {\n    log_msg("go");\n    run();\n    cfg();\n    cfg2();\n    return 0;\n}\n')
    files['shared.c'] = 'void shared(void) {\n    return;\n}\n'
    _, blobs, _ = run(files)
    assert blobs['main.<module>']['depth'] == 2
    assert sorted(b['name'] for b in blobs.values() if b['kind'] == 'command') == ['cfg', 'cfg2', 'draw', 'handle', 'update']
    assert quals(blobs['hub:shared']) == ['shared.shared']
    assert quals(blobs['main.<module>']) == ['main.main', 'run.run']


def test_a_short_wrapper_owning_most_still_splits_one_level_deeper():
    # run() is one line, so never a command itself, but it owns the program: the split goes through it
    files = _loop_program('void run(void) { draw(); update(); handle(); }\n',
                          'int main(void) {\n    log_msg("go");\n    run();\n    return 0;\n}\n')
    _, blobs, _ = run(files)
    assert blobs['main.<module>']['depth'] == 2
    assert sorted(b['name'] for b in blobs.values() if b['kind'] == 'command') == ['draw', 'handle', 'update']
    assert quals(blobs['main.<module>']) == ['main.main', 'run.run']
