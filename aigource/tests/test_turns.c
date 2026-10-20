/* turns.c, the stage's per-turn stats, on hand-built events: turns by typed
 * prompt per session (a slash command and a subagent's prompt start none, a
 * subagent's change is its session's), only turns with changes, +/-, fix and
 * corrected by the analysis' patterns, interrupts, the last test run after a
 * change (failing, passed, untested; a run that never ran, one that isn't a
 * test), active time with gaps capped, slow, tags, time order across
 * sessions, and a recompute after more events. */
#include <stdio.h>
#include <string.h>

#include "turns.h"
#include "ui.h"

static int fails, checks;

#define CHECK(cond, ...)                        \
    do {                                        \
        checks++;                               \
        if (!(cond)) {                          \
            printf("FAIL turns: " __VA_ARGS__); \
            printf(" (line %d)\n", __LINE__);   \
            fails++;                            \
        }                                       \
    } while (0)

#define T0 1792000000.0
static unsigned long seq;

static Event ev(EvKind k, double at, const char *actor) {
    return (Event){.ts = T0 + at, .seq = ++seq, .kind = k, .actor = (char *)actor, .lines = -1};
}
static Event prompt(double at, const char *actor, const char *text, int kind) {
    Event e = ev(EV_TURN, at, actor);
    e.text = (char *)text, e.prompt = kind;
    return e;
}
static Event change(double at, const char *actor, int add, int del) {
    Event e = ev(EV_EDIT, at, actor);
    e.path = "a.c", e.add = add, e.del = del, e.via = VIA_TRANSCRIPT;
    return e;
}
static Event run(double at, const char *actor, const char *cmd, int err) {
    Event e = ev(EV_BASH_END, at, actor);
    e.text = (char *)cmd, e.err = err;
    return e;
}

int main(void) {
    TurnStats *t = ts_new("/repo");
    Event sub = ev(EV_TITLE, 0, "sub1");
    sub.text = "a task", sub.who.type = "executor", sub.who.session = "S";
    Event notrun = run(70, "S", "make test", 1);
    notrun.not_run = 1;
    Event evs[] = {
        sub,
        /* turn 1 of S: a subagent's change, then a failing run */
        prompt(0, "S", "Add the parser", PROMPT_HUMAN),
        ev(EV_USAGE, 10, "S"),
        change(20, "sub1", 10, 2),
        run(30, "sub1", "make test", 1),
        change(40, "S", 3, 0),       /* after the run: untested */
        ev(EV_USAGE, 1000, "S"),     /* a 990 s gap counts 300 */
        notrun,                      /* never ran: no verdict */
        prompt(1100, "S", "/clear", PROMPT_COMMAND), /* starts no turn */
        /* turn 2: the prompt reports a problem; passes; then interrupted */
        prompt(2000, "S", "It still crashes on empty input", PROMPT_HUMAN),
        change(2010, "S", 1, 1),
        run(2020, "S", "ls -la", 1), /* not a test run */
        run(2030, "S", "make", 0),
        ev(EV_INTERRUPT, 2040, "S"),
        /* turn 3: no change, so no turn; its prompt reads like a correction of turn 2 */
        prompt(3000, "S", "That's wrong, revert it", PROMPT_HUMAN),
        ev(EV_USAGE, 3010, "S"),
        /* another session, between S's turns 1 and 2 */
        prompt(500, "B", "Write docs", PROMPT_PASTED),
        change(510, "B", 40, 0),
    };
    ts_add(t, evs, sizeof evs / sizeof *evs);
    int n, big;
    const TurnStat *u = ts_get(t, &n, &big);
    CHECK(n == 3, "3 turns with changes, got %d", n);
    if (n == 3) {
        CHECK(!strcmp(u[0].actor, "S") && !strcmp(u[1].actor, "B") && !strcmp(u[2].actor, "S"), "time order");
        CHECK(u[0].changes == 2 && u[0].add == 13 && u[0].del == 2, "turn 1 +/- with the subagent's");
        CHECK(u[0].failing == 1 && u[0].untested == 1, "turn 1 failing %d untested %d", u[0].failing, u[0].untested);
        CHECK(u[0].secs == 10 + 300, "turn 1 secs %ld (gap capped)", u[0].secs);
        CHECK(u[0].end == T0 + 1000, "turn 1 ends at its last response");
        CHECK(!u[0].fix && u[0].corr && !u[0].intr, "turn 1 corrected: the next typed prompt reports a crash");
        CHECK(u[1].untested == 1 && u[1].secs == 0 && u[1].add == 40, "B's turn");
        CHECK(u[2].fix && u[2].corr && u[2].intr, "turn 2 fix, corrected, interrupted");
        CHECK(!u[2].failing && !u[2].untested, "turn 2 passed (the last run counts, ls isn't one)");
        CHECK(big == 40, "big %d", big);
        /* average (310 + 0 + 0) / 3: turn 1 is slow */
        CHECK(u[0].slow && !u[1].slow && !u[2].slow, "slow");
        CHECK(u[0].tags == (UT_CORR | UT_FAILING | UT_UNTESTED | UT_SLOW), "turn 1 tags %d", u[0].tags);
        CHECK(u[2].tags == (UT_FIX | UT_CORR), "turn 2 tags %d", u[2].tags);
    }
    Event more[] = {change(3020, "S", 5, 5)}; /* now turn 3 exists */
    ts_add(t, more, 1);
    u = ts_get(t, &n, &big);
    CHECK(n == 4 && !strcmp(u[3].title, "That's wrong, revert it") && !u[3].corr, "recomputed: %d turns", n);
    ts_free(t);
    printf("%s turns (%d checks)\n", fails ? "FAIL" : "ok  ", checks);
    return fails != 0;
}
