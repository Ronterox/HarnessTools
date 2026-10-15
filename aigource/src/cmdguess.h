#ifndef AIG_CMDGUESS_H
#define AIG_CMDGUESS_H

/* Which files a shell command writes or removes, guessed from its text. For a
 * failed Bash call: Claude Code records no file changes for those. Knows sed -i,
 * perl -i, > and >> redirects, tee, touch, truncate, rm, mv and cp, and follows
 * cd. Words built at run time ($VAR, $(...), globs) are skipped. */
typedef enum { GUESS_CHANGE, GUESS_DELETE } GuessKind;

/* path as written; dir: the directory cd moved to before it (as written, may be
 * relative to the start directory; NULL = none). into: when path may be a
 * directory (mv/cp destination), the name the file gets inside it. */
typedef void (*GuessFn)(void *ud, const char *dir, const char *path, GuessKind kind, const char *into);

void cmd_guess(const char *cmd, GuessFn fn, void *ud);

#endif
