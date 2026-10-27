#!/usr/bin/env python3
"""Which comment each change gets, through both data paths, on tests/fixtures/comments (from
tests/make_comment_fixture.py): aigource --structure-dump and draft/build_data.py (the oracle)
on the same transcripts, in a throwaway repo whose sessions are found as a real repo's.

- every change has the same comment in both (and the same own-or-fallback mark);
- the three Writes of one message share the text before the first one;
- a subagent resumed by the coordinator: its edit with no text since has no comment (never
  the last round's report), its edits with text have theirs.

  tests/comment_parity.py BINARY   (exit 1 on a mismatch)
"""
import json, os, re, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
AIG = os.path.dirname(HERE)
BIN = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(AIG, "build", "aigource"))
FIX = os.path.join(HERE, "fixtures", "comments")
fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + "comments: " + name + ("" if cond else ": " + str(detail)))
    fails += not cond


def setup(tmp):
    """The fixture as a repo under tmp: its root, and an environment whose sessions are it."""
    repo = os.path.realpath(os.path.join(tmp, "repo"))
    os.makedirs(os.path.join(repo, "src"))
    for f in sorted(os.listdir(FIX)) + ["c1/subagents/" + x for x in os.listdir(os.path.join(FIX, "c1", "subagents"))]:
        if not f.endswith(".jsonl"):
            continue
        with open(os.path.join(FIX, f)) as fh:
            for m in re.finditer(r'"file_path":"/REPO/src/([\w.]+)","content":"(.*?)"\}', fh.read()):
                with open(os.path.join(repo, "src", m.group(1)), "w") as out:
                    out.write(json.loads('"%s"' % m.group(2)))
    env = dict(os.environ, GIT_AUTHOR_DATE="2026-10-14T09:00:00Z", GIT_COMMITTER_DATE="2026-10-14T09:00:00Z")
    git = ["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
           "-c", "core.hooksPath=/dev/null"]
    subprocess.run(git + ["init", "-q"], check=True, env=env)
    subprocess.run(git + ["add", "-A"], check=True, env=env)
    subprocess.run(git + ["commit", "-qm", "base"], check=True, env=env)
    cfg = os.path.join(tmp, "cfg")
    proj = os.path.join(cfg, "projects", "".join(c if c.isalnum() else "-" for c in repo))
    for root, _, files in os.walk(FIX):
        for f in files:
            dst = os.path.join(proj, os.path.relpath(os.path.join(root, f), FIX))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(os.path.join(root, f)) as src, open(dst, "w") as out:
                out.write(src.read().replace("/REPO", repo))
    alias = os.path.join(cfg, "projects", re.sub(r"[/.]", "-", repo))  # build_data's slug: only / and .
    if alias != proj:
        os.symlink(proj, alias)
    env = dict(os.environ, CLAUDE_CONFIG_DIR=cfg)
    env.pop("AIG_NO_DEFS", None)
    return repo, env


def comments(data):
    """Per file: its changes' (comment, own) in time order."""
    out = {}

    def walk(n, path):
        for k in n["kids"]:
            p = path + "/" + k["name"] if path else k["name"]
            if k["kind"] == "file":
                cids = sorted({c[0] for c in k["ch"]}, key=lambda c: data["changes"][c][7])
                out[p] = [(data["comments"][ch[1] if ch[1] >= 0 else -1 - ch[1]], ch[1] >= 0)
                          for ch in (data["changes"][c] for c in cids)]
            elif k["kind"] == "dir":
                walk(k, p)

    walk(data["tree"], "")
    return out


def main():
    tmp = tempfile.mkdtemp(prefix="aigc.")
    try:
        repo, env = setup(tmp)
        app = comments(json.loads(subprocess.run([BIN, "--structure-dump", "--all", "--root", repo], env=env,
                                                 capture_output=True, check=True).stdout))
        draft = comments(json.loads(subprocess.run([sys.executable, "-I", os.path.join(AIG, "draft", "build_data.py"),
                                                    repo, "src/"], env=env, capture_output=True, check=True).stdout))
        for f in sorted(set(app) | set(draft)):
            check("%s: the same comment in the app and build_data" % f, app.get(f) == draft.get(f),
                  (app.get(f), draft.get(f)))
        first = "Writing all three files."
        check("one message, three calls: the text before the first is each one's",
              [app.get("src/%s.py" % x) for x in "abc"] == [[(first, True)], [(first, False)], [(first, False)]],
              [app.get("src/%s.py" % x) for x in "abc"])
        check("a subagent's round one: its edit's own text", app.get("src/r1.py") == [("Round one edit.", True)],
              app.get("src/r1.py"))
        check("resumed, no text since: no comment (not the last round's report)", app.get("src/r2.py") == [("", True)],
              app.get("src/r2.py"))
        check("resumed, then text: its own", app.get("src/r3.py") == [("Round two, with words.", True)],
              app.get("src/r3.py"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
