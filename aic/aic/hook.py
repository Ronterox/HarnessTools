"""`aic hook`: the worker-side gate. Installed as a Claude Code hook.

Records which session/tmux pane/process the worker lives in and whether it is
busy, and enforces a one-shot hold: the next tool call is denied and the turn
ends. It also keeps the worker's file tools out of the human's docs
(.aic/docs). Prompt templates (.aic/prompts) are the worker's to write too
(the aic-prompts skill drafts them there); every send from the editor shows
the rendered prompt first.
The compiler never reaches this hook (no project hooks, AIC_ROLE=compiler) and
never writes files anyway.

It never injects instructions into the worker. Claude (rightly) treats hook
text as untrusted, so the human's words always arrive as real user input
(typed into the worker's pane by `aic submit`).
"""
import json
import os
import re
import shlex
import sys

from . import paths

STATE = {
    "SessionStart": "idle",
    "UserPromptSubmit": "busy",
    "PreToolUse": "busy",
    "PostToolUse": "busy",
    "PostToolUseFailure": "busy",
    "Stop": "idle",
    "SessionEnd": "ended",
}

HOLD_REASON = ("Paused by the human user via aic (hold). Do not retry. "
               "End your turn and wait for their next message.")
GUARD_REASON = ("This file is the human's own (an aic doc, under .aic/docs). Do not change it, by any means. "
                "If it should change, say so in your reply.")
FILE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
GUARDED = ("docs",)
# Bash: a command that writes into this project's .aic/docs
# (or replaces .aic itself), read the way a shell reads it: quoted text is one
# word, paths resolve against the cwd (following `cd`), and only destinations
# count: a `>` redirect, the target of cp/mv/rsync/install/ln, the files of
# rm/touch/tee/sed -i/..., find -delete, an output flag (-o, -O, -C, -d), a
# writing git subcommand. `$(...)`, backticks and `sh -c` are read too; an
# inline python/node/ruby/perl script counts when it names such a path and
# looks like it writes. Heredoc bodies are data. Reading and mentioning (grep,
# cat, commit messages, test strings) go through. A speed bump for an
# overeager worker, not a sandbox: variables, globs and a script fed on stdin
# are not followed.
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1")
SUBST = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
OPS = {";", "&&", "||", "|", "&", "(", ")", "{", "}", "|&", ";;", "\n"}
REDIRS = {">", ">>", ">|", "&>", "&>>", "<>"}
KEYWORDS = {"do", "then", "else", "elif", "if", "while", "until", "!", "time", "exec"}
WRAPPERS = {"sudo", "env", "command", "builtin", "xargs", "nohup", "nice", "stdbuf", "busybox", "ionice", "chronic"}
# wrapper -> its options that take a value (skipped with it)
WRAPPER_ARGS = {"sudo": {"-u", "-g", "-C", "-h", "-p", "-U"}, "env": {"-u", "-C", "-S"}, "nice": {"-n"},
                "ionice": {"-c", "-n", "-p"}, "xargs": {"-I", "-n", "-P", "-L", "-d", "-E", "-s", "-a"},
                "timeout": {"-s", "-k"}}
DEST_ONLY = {"cp", "rsync", "install", "ln"}  # (mv also loses its sources)
ANY_ARG = {"mv", "rm", "touch", "truncate", "tee", "chmod", "chown", "unlink", "mkdir", "rmdir", "patch", "shred",
           "ed", "sponge"}
SHELLS = {"sh", "bash", "zsh", "dash"}
SCRIPT = re.compile(r"^(?:python|node|ruby|perl)[\d.]*$")
GIT_WRITES = {"checkout", "restore", "apply", "stash", "rm", "mv", "clean", "reset"}
# command -> its options that take an output path
OUT_FLAGS = {"curl": {"-o", "--output"}, "wget": {"-O", "-P", "--output-document", "--directory-prefix"},
             "sort": {"-o", "--output"}, "unzip": {"-d"}, "cp": {"-t", "--target-directory"},
             "mv": {"-t", "--target-directory"}, "install": {"-t", "--target-directory"},
             "ln": {"-t", "--target-directory"}, "tar": {"-C", "--directory"}}
# an inline script that writes files (not str.replace or dict.copy)
SCRIPT_WRITES = re.compile(r"\b(?:os\.(?:remove|unlink|rename|replace|rmdir|removedirs|makedirs|mkdir|symlink)|"
                           r"shutil\.\w+|fs\.\w*(?:[Ww]rite|[Aa]ppend|[Uu]nlink|rm|[Rr]ename|[Cc]opy)\w*|"
                           r"File\.(?:write|delete|rename)|unlink|rmtree)\s*\(|"
                           r"\.(?:write(?:_text|_bytes)?|unlink|touch|rename|mkdir)\s*\(|"
                           r"\.(?:writeFile|appendFile|unlink|rm|rename|copyFile|mkdir)(?:Sync)?\s*\(|"
                           r"open\([^)]*['\"](?:[wax]|r\+)[bt]?\+?['\"]|>{1,2}\s*['\"]?\S*\.aic")
VIM_WRITES = re.compile(r"(?:^|[|:\s])(?:w|wq|x|wa|wall|wqa|xa|xall|sav|saveas|write|up|update)!?(?:\s|$|\|)")


def _strip_heredocs(cmd):
    out, pos = [], 0
    for m in HEREDOC.finditer(cmd):
        if m.start() < pos:
            continue
        nl = cmd.find("\n", m.end())
        if nl < 0:
            break
        end = re.compile(r"^[ \t]*%s[ \t]*$" % re.escape(m.group(2)), re.M).search(cmd, nl + 1)
        out.append(cmd[pos:nl + 1])
        pos = end.end() if end else len(cmd)
    out.append(cmd[pos:])
    return "".join(out)


def _unquote_single(cmd):
    """`cmd` with single-quoted text blanked (the shell runs no $(...) there)."""
    out, single, double = [], False, False
    for c in cmd:
        if c == "'" and not double:
            single = not single
        elif c == '"' and not single:
            double = not double
        out.append(" " if single and c != "'" else c)
    return "".join(out)


def _inplace(name, a):
    if name not in ("sed", "perl", "gawk", "awk"):
        return False
    if a.startswith("--in-place") or a.startswith("-i"):
        return True
    m = re.match(r"^-([a-zA-Z]+)$", a)  # a cluster like -pi or -Ei (never -Ilib, -Mstrict)
    return bool(m) and "i" in m.group(1) and a[1] not in "IMm"


def _tokens(cmd):
    lex = shlex.shlex(cmd.replace("\n", " ; "), posix=True, punctuation_chars=";&|()<>")
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:  # unbalanced quotes: plain words
        return cmd.replace("\n", " ; ").split()


def _guarded_path(aic, p):
    """Real path `p` is .aic/docs (`aic` = real .aic), or in it."""
    return any(p == b or p.startswith(b + os.sep) for b in (os.path.join(aic, d) for d in GUARDED))


class _Guard:
    def __init__(self, root, cwd):
        self.root = os.path.realpath(root)
        self.aic = os.path.realpath(paths.aic_dir(root))
        self.cwd = cwd or root

    def hit(self, word, whole=False):
        """`word` as a path is in .aic/docs (whole: or is .aic itself,
        for what replaces or removes it): -> it, else None."""
        if not word or word.startswith("-") and "=" not in word:
            return None
        word = word.split("=", 1)[1] if re.match(r"^-*\w[\w-]*=", word) else word
        if not word or "$" in word:
            return None
        p = os.path.realpath(os.path.join(self.cwd, os.path.expanduser(word)))
        if p == self.aic:
            return word if whole else None
        return word if _guarded_path(self.aic, p) else None

    def simple(self, words):
        """One simple command (no operators, redirects taken out)."""
        while words and (words[0] in KEYWORDS or words[0] in WRAPPERS or words[0] == "timeout"
                         or re.match(r"^\w+=", words[0])):
            w, words = words[0], words[1:]
            if w in WRAPPERS or w == "timeout":  # and their options: env -u X, nice -n 5, timeout -s KILL 5
                while words and words[0].startswith("-") and words[0] != "--":
                    words = words[2:] if words[0] in WRAPPER_ARGS.get(w, ()) else words[1:]
                words = words[1:] if words[:1] == ["--"] else words
                if w == "timeout" and words:
                    words = words[1:]  # the duration
        if not words:
            return None
        name = os.path.basename(words[0].lstrip("\\"))
        args = words[1:]
        files = [a for a in args if not a.startswith("-")]
        if name == "cd":
            self.cwd = os.path.join(self.cwd, os.path.expanduser(files[0])) if files else os.path.expanduser("~")
            return None
        if name in SHELLS:  # sh -c / bash -lc SCRIPT
            for i, a in enumerate(args):
                if re.match(r"^-[a-zA-Z]*c[a-zA-Z]*$", a) and i + 1 < len(args):
                    return self.command(args[i + 1])
        if SCRIPT.match(name):
            for flag in ("-c", "-e", "-pe", "-ne", "-pie", "-E"):
                if flag in args and args.index(flag) + 1 < len(args):
                    script = args[args.index(flag) + 1]
                    if SCRIPT_WRITES.search(script):
                        for w in re.findall(r"[\w./~-]*\.aic[\w./-]*", script):
                            if self.hit(w):
                                return self.hit(w)
        flags = OUT_FLAGS.get(name, set())
        if name == "tar" and not any(a.startswith("--extract") or a.startswith("-x")
                                     or re.match(r"^-?[a-zA-Z]*x", a) for a in args[:1] + args[1:2]):
            flags = set()  # tar -C only matters when extracting
        for i, a in enumerate(args):  # -o FILE, --output=FILE, a cluster ending in it (-so FILE)
            short = re.match(r"^-[a-zA-Z]*([a-zA-Z])$", a)
            if (a in flags or (short and "-" + short.group(1) in flags)) and i + 1 < len(args) and self.hit(args[i + 1]):
                return args[i + 1]
            if "=" in a and a.split("=", 1)[0] in flags and self.hit(a.split("=", 1)[1]):
                return a.split("=", 1)[1]
        if name in DEST_ONLY:
            return self.hit(files[-1], whole=True) if len(files) >= 2 else None
        if name == "dd":  # only its of=
            return next((a[3:] for a in args if a.startswith("of=") and self.hit(a[3:])), None)
        if name in ANY_ARG or any(_inplace(name, a) for a in args):
            for a in files:
                if self.hit(a, whole=name in ("mv", "rm")):
                    return a
        if name == "find" and self.hit(files[0] if files else "."):
            start = files[0] if files else "."
            if "-delete" in args:
                return start
            for flag in ("-exec", "-execdir", "-ok", "-okdir"):
                if flag in args:  # the command it runs, with {} = the start path
                    sub = args[args.index(flag) + 1:]
                    end = next((j for j, w in enumerate(sub) if w in (";", "+", "\\;")), len(sub))
                    if self.simple([start if w == "{}" else w for w in sub[:end]]):
                        return start
        if name == "git":
            while args[:1] and args[0] in ("-C", "-c") and len(args) > 1:  # git -C DIR / -c k=v
                if args[0] == "-C":
                    self.cwd = os.path.join(self.cwd, args[1])
                args = args[2:]
            sub = [a for a in args if not a.startswith("-")]
            index_only = "--cached" in args or "--staged" in args or (sub[:1] == ["reset"] and "--hard" not in args)
            if sub and sub[0] in GIT_WRITES and not index_only:
                for a in sub[1:] or ["."]:
                    if self.hit(a):
                        return a
        if name in ("vim", "nvim", "vi"):
            cmds = [args[i + 1] for i, a in enumerate(args[:-1]) if a == "-c"] + [a[1:] for a in args if a.startswith("+")]
            if any(VIM_WRITES.search(c) for c in cmds):
                for a in files:
                    if self.hit(a):
                        return a
        return None

    def command(self, cmd):
        """The guarded path `cmd` writes, or None."""
        cmd = _strip_heredocs(cmd)
        for m in SUBST.finditer(_unquote_single(cmd)):
            hit = _Guard(self.root, self.cwd).command(m.group(1) if m.group(1) is not None else m.group(2))
            if hit:
                return hit
        words, toks = [], _tokens(cmd) + [";"]
        i = 0
        while i < len(toks):
            t = toks[i]
            if t in REDIRS or (t.endswith(">") and t.lstrip("&0123456789") in REDIRS):
                if i + 1 < len(toks) and not toks[i + 1].startswith("&") and self.hit(toks[i + 1]):
                    return toks[i + 1]
                i += 2
                continue
            if t in ("<", "<<", "<<<", "<&"):  # input: the next word is read, never written
                i += 2
                continue
            if t in OPS:
                hit = self.simple(words)
                if hit:
                    return hit
                words = []
            elif re.match(r"^\d+$", t) and i + 1 < len(toks) and toks[i + 1] in REDIRS:
                pass  # the fd number of `2>`
            else:
                words.append(t)
            i += 1
        return None


def bash_writes(cmd, root, cwd=None):
    """The .aic/docs path (or .aic) a Bash command writes, or None."""
    return _Guard(root, cwd).command(cmd)


def guarded(root, data):
    """The .aic/docs path a worker file tool (or a Bash command)
    would change, or None."""
    key = FILE_TOOLS.get(data.get("tool_name"))
    inp = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    if data.get("tool_name") == "Bash":
        cmd = inp.get("command")
        return bash_writes(cmd, root, data.get("cwd")) if isinstance(cmd, str) else None
    target = inp.get(key) if key else None
    if not isinstance(target, str) or not target:
        return None
    target = os.path.realpath(os.path.join(data.get("cwd") or root, os.path.expanduser(target)))
    if _guarded_path(os.path.realpath(paths.aic_dir(root)), target):
        return os.path.relpath(target, os.path.realpath(root))
    return None


def worker_process():
    """(pid, tty) of the nearest ancestor that owns a terminal: the Claude Code
    TUI (hooks themselves run with piped stdin). (None, None) off Linux."""
    pid = os.getppid()
    for _ in range(8):
        if not pid or pid <= 1:
            break
        tty = paths.tty(pid)
        if tty:
            return pid, tty
        st = paths.proc_stat(pid)
        pid = int(st[1]) if st else None
    return None, None


def handle(data):
    if os.environ.get("AIC_ROLE") == "compiler":
        return None
    event = data.get("hook_event_name", "")
    sid = data.get("session_id")
    root = paths.find_root(data.get("cwd"))
    if not root or not sid:
        return None

    wpath = os.path.join(paths.aic_dir(root), "workers", sid + ".json")
    w = paths.read_json(wpath, {}) or {}
    w.update({
        "session_id": sid,
        "transcript_path": data.get("transcript_path") or w.get("transcript_path"),
        "cwd": data.get("cwd") or w.get("cwd"),
        "last_event": event,
        "updated": paths.now(),
    })
    if event in ("SessionStart", "UserPromptSubmit") or not w.get("pid"):
        pid, tty = worker_process()
        # $TMUX = "<socket>,<pid>,<session>": the server the pane id belongs to
        sock = (os.environ.get("TMUX") or "").split(",")[0] or None
        w.update(pid=pid, tty=tty, pane=os.environ.get("TMUX_PANE"), tmux_socket=sock)
    if event in ("SessionStart", "UserPromptSubmit"):
        w["prompted"] = paths.now()
    if event in STATE:
        w["state"] = STATE[event]
    elif event == "Notification" and "permission" in (data.get("message") or "").lower():
        w["state"] = "asking"  # advisory: delivery trusts what the pane shows
    if event == "SessionEnd":
        w["pane"] = None  # never type into this pane again

    out = None
    hold = os.path.join(paths.session_dir(root, sid), "hold")
    if event == "PreToolUse" and os.path.exists(hold):
        try:
            os.unlink(hold)  # one-shot; a parallel call that loses the race just runs
        except FileNotFoundError:
            hold = None
        if hold:
            w["state"] = "held"
            paths.control(root, sid, "held", tool=data.get("tool_name"))
            out = {
                "continue": False,
                "stopReason": "aic: held by the human",
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": HOLD_REASON,
                },
            }
    if event == "PreToolUse" and out is None:
        g = guarded(root, data)
        if g:
            paths.control(root, sid, "guarded", tool=data.get("tool_name"), file=g)
            out = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": GUARD_REASON,
                },
            }
    elif event in ("Stop", "SessionEnd") and os.path.exists(hold):
        # The turn ended on its own before the next tool call: nothing left to hold.
        try:
            os.unlink(hold)
            paths.control(root, sid, "hold-expired")
        except FileNotFoundError:
            pass

    paths.write_json(wpath, w)
    return out


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    try:
        out = handle(data) if isinstance(data, dict) else None
    except Exception as e:  # the gate must never break the worker
        sys.stderr.write("aic hook: %s\n" % e)
        return 0
    if out is not None:
        sys.stdout.write(json.dumps(out))
    return 0
