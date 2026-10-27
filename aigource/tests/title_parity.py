#!/usr/bin/env python3
"""Turn titles and colours through both data paths, on tests/fixtures/titles (prompts
the title rules must cut alike: odd whitespace, CR, NUL, bytes that aren't UTF-8, the
140-character cut). In a throwaway repo whose sessions are found as a real repo's:

- aigource --structure-dump --metrics: the app's titles and each turn's slot
  (StTurn.slot); written to OUT/titles-data.json and OUT/titles-metrics.json, where
  tests/metrics_parity.js checks the page's turnSlot(TURNS[t]) against them;
- draft/build_data.py on the same transcripts: every turn there, matched by (time,
  title), is one of the app's;
- the draft's turnSlot on build_data's turns (tests/slot_parity.js) gives the app's slot.

  tests/title_parity.py BINARY OUT_DIR   (exit 1 on a mismatch, 2 when node is missing)
"""
import json, os, re, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
AIG = os.path.dirname(HERE)
BIN, OUT = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + "titles: " + name + ("" if cond else ": " + str(detail)))
    fails += not cond


def git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_DATE="2026-10-14T09:00:00Z", GIT_COMMITTER_DATE="2026-10-14T09:00:00Z")
    subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                    "-c", "core.hooksPath=/dev/null", *args], check=True, env=env, capture_output=True)


tmp = tempfile.mkdtemp(prefix="aigt.")
try:
    repo = os.path.realpath(os.path.join(tmp, "repo"))
    os.makedirs(os.path.join(repo, "src"))
    with open(os.path.join(HERE, "fixtures", "titles", "t1.jsonl"), "rb") as f:
        tr = f.read().replace(b"/REPO", repo.encode())
    for m in re.finditer(rb'"file_path":"[^"]*/src/(t\d+\.py)","content":"x = (\d+)\\n"', tr):
        with open(os.path.join(repo, "src", m.group(1).decode()), "w") as f:
            f.write("x = %s\n" % m.group(2).decode())
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    proj = os.path.join(tmp, "cfg", "projects", "".join(c if c.isalnum() else "-" for c in repo))
    os.makedirs(proj)
    with open(os.path.join(proj, "t1.jsonl"), "wb") as f:
        f.write(tr)
    alias = os.path.join(tmp, "cfg", "projects", re.sub(r"[/.]", "-", repo))  # build_data's slug: only / and .
    if alias != proj:
        os.symlink(proj, alias)
    env = dict(os.environ, CLAUDE_CONFIG_DIR=os.path.join(tmp, "cfg"))
    env.pop("AIG_NO_DEFS", None)

    os.makedirs(OUT, exist_ok=True)
    mpath = os.path.join(OUT, "titles-metrics.json")
    app = subprocess.run([BIN, "--structure-dump", "--all", "--metrics", mpath, "--root", repo], env=env,
                         capture_output=True, check=True).stdout
    with open(os.path.join(OUT, "titles-data.json"), "wb") as f:
        f.write(app)
    app = json.loads(app)
    with open(mpath) as f:
        slots = [t["slot"] for t in json.load(f)["turns"]]
    draft = json.loads(subprocess.run([sys.executable, "-I", os.path.join(AIG, "draft", "build_data.py"), repo, "src/"],
                                      env=env, capture_output=True, check=True).stdout)
    n = sum(1 for line in tr.splitlines() if b'"origin"' in line)
    check("every prompt a turn in the app (%d)" % n, len(app["turns"]) == n, len(app["turns"]))
    check("every prompt a turn in build_data (%d)" % n, len(draft["turns"]) == n, len(draft["turns"]))
    by = {(t["ts"], t["title"]): i for i, t in enumerate(app["turns"])}
    table = []
    for t in draft["turns"]:
        i = by.get((t["ts"], t["title"]))
        check("build_data title %r is the app's" % t["title"][:40], i is not None,
              [a["title"] for a in app["turns"] if a["ts"] == t["ts"]])
        if i is not None:
            table.append({"ts": t["ts"], "title": t["title"], "slot": slots[i]})
    tpath = os.path.join(OUT, "titles-slots.json")
    with open(tpath, "w") as f:
        json.dump(table, f)
    if not shutil.which("node"):
        print("skip titles: turnSlot on build_data's turns (no node)")
        sys.exit(2 if not fails else 1)
    r = subprocess.run(["node", os.path.join(HERE, "slot_parity.js"), tpath], capture_output=True, text=True)
    print(r.stdout.rstrip())
    check("the draft's turnSlot on build_data's turns is the app's slot", r.returncode == 0 and len(table) == n)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
sys.exit(1 if fails else 0)
