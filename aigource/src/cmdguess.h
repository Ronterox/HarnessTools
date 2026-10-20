#ifndef AIG_CMDGUESS_H
#define AIG_CMDGUESS_H
#include <regex.h>

/* Which files a shell command writes, removes or reads, guessed from its text.
 * Writes for a failed Bash call: Claude Code records no file changes for those.
 * Knows sed -i, perl -i, > and >> redirects, tee, touch, truncate, rm, mv and
 * cp; reads by cat, head, tail, sed without -i, nl, less and the like, and <
 * redirects. Follows cd, pushd and popd; skips (( arithmetic )). Words built at run time ($VAR, $(...), globs) are
 * skipped. */
typedef enum { GUESS_CHANGE, GUESS_DELETE, GUESS_READ } GuessKind;

/* path as written; dir: the directory cd moved to before it (as written, may be
 * relative to the start directory; NULL = none). into: when path may be a
 * directory (mv/cp destination), the name the file gets inside it. first, last:
 * the lines a read covers, as EV_READ's range: 0, 0 the whole file; first..last;
 * first..end when last is 0; the last -last lines when last < 0 (tail). One call
 * per range: `sed -n '1,5p;9p' f` reads f twice. */
typedef void (*GuessFn)(void *ud, const char *dir, const char *path, GuessKind kind, const char *into, int first,
                        int last);

void cmd_guess(const char *cmd, GuessFn fn, void *ud);

/* Command segments (docs/STRUCTURE.md section 0): the simple commands of cmd as
 * the lexer above sees them (quotes, here-documents, $(...) and comments
 * understood; redirects dropped), each its words joined by spaces, without a
 * leading env, VAR=x, sudo, time, timeout N. Bits: CMD_RUN, a segment matching
 * re ran under root (relative to start, where the command began; root NULL: any
 * segment counts); CMD_PIPED, one of those was piped into the next command (a
 * single |, or its subshell's) while pipefail was off (`set -o pipefail` /
 * `set -euo pipefail` turn it on, `set +o pipefail` off), so the line's exit
 * status is not its own; CMD_AWAY, a matching segment ran after a cd out of
 * root, or one we can't follow ($VAR, $(...), cd -, a bare cd). */
enum { CMD_RUN = 1, CMD_PIPED = 2, CMD_AWAY = 4 };
int cmd_runs(const regex_t *re, const char *cmd, const char *root, const char *start);

/* A test or build run, matched at the start of a segment (POSIX ERE with GNU \s \S \w). */
#define CMD_VERIFY_ERE                                                                                        \
    "^((ba)?sh\\s+|python3?\\s+|uv\\s+run\\s+)?(make|pytest|python3? -m (pytest|unittest)|\\S*tests?/\\S+|" \
    "\\S*/test_\\w+\\S*|ctest|cargo (test|build)|npm (test|run)|cc|gcc|clang|nvim --headless)(\\s|$)"

#endif
