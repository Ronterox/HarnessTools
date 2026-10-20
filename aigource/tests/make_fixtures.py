#!/usr/bin/env python3
"""Writes tests/fixtures: hand-built transcripts covering each parser rule.
Shapes mirror real Claude Code records (see src/transcript.c). Root is /proj."""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "fixtures", "proj")
t = [0]

def ts():
    t[0] += 1
    return "2026-10-14T10:%02d:%02d.000Z" % divmod(t[0], 60)

def user(content, **kw):
    return {"type": "user", "timestamp": ts(), "message": {"role": "user", "content": content}, **kw}

def use(tid, name, inp):
    return {"type": "assistant", "timestamp": ts(),
            "message": {"content": [{"type": "text", "text": "doing it"},
                                    {"type": "tool_use", "id": tid, "name": name, "input": inp}]}}

def result(tid, res, err=False):
    return user([{"type": "tool_result", "tool_use_id": tid, "is_error": err, "content": "x"}],
                toolUseResult=res)

main = [
    {"type": "mode", "mode": "x"},                                    # ignored record type
    user("Build the thing\nsecond line", parentUuid="p0", origin={"kind": "human"}),
    user("Build the thing (edited)", parentUuid="p0"),                 # re-sent: same turn
    use("r1", "Read", {"file_path": "/proj/src/a.py"}),
    result("r1", {"type": "text", "file": {"filePath": "/proj/src/a.py", "totalLines": 40}}),
    use("e1", "Edit", {"file_path": "/proj/src/a.py", "old_string": "a", "new_string": "b"}),
    result("e1", {"filePath": "/proj/src/a.py", "originalFile": "1\n2\n3\n",
                  "structuredPatch": [{"lines": [" 1", "-2", "+2b", "+2c", " 3"]}]}),
    use("w1", "Write", {"file_path": "/proj/src/new/b.py", "content": "x\ny\n"}),
    result("w1", {"type": "create", "filePath": "/proj/src/new/b.py", "content": "x\ny\n",
                  "structuredPatch": []}),
    use("w2", "Write", {"file_path": "/proj/README.md", "content": "a\nb\nc"}),
    result("w2", {"type": "update", "content": "a\nb\nc",
                  "structuredPatch": [{"lines": ["-old", "+a", "+b", "+c"]}]}),
    use("e2", "Edit", {"file_path": "/proj/src/a.py"}),
    result("e2", "Error: string not found", err=True),                 # failed: no event
    use("e3", "Edit", {"file_path": "/elsewhere/x.py"}),
    result("e3", {"originalFile": "", "structuredPatch": []}),         # outside root: no event
    use("g1", "Grep", {"pattern": "def main\nmore", "path": "/proj/src"}),
    result("g1", {"mode": "files_with_matches"}),
    use("g2", "Glob", {"pattern": "**/*.md"}),
    result("g2", {"filenames": []}),
    use("b1", "Bash", {"command": "make gen && rm old.txt\n# second line", "description": "gen"}),
    result("b1", {"stdout": "", "bashEditDiff": {
        "files": [
            {"filePath": "/proj/old.txt", "hunks": [{"lines": ["-a", "-b"]}], "deleted": True},
            {"filePath": "/proj/gen/out.c", "hunks": [{"lines": ["+1", "+2", "+3"]}], "created": True},
            {"filePath": "/proj/src/a.py", "hunks": [{"lines": ["+z"]}]},
        ],
        "changedFiles": ["/proj/old.txt", "/proj/gen/out.c", "/proj/src/a.py",
                         "/proj/gen/out.o", "/tmp/elsewhere"]}}, err=True),  # failing cmd still counts
    use("t1", "Agent", {"description": "help"}),                       # untracked tool
    result("t1", {"status": "async_launched"}),
    user("<command-name>/review</command-name><command-args>x</command-args>"),
    user("<local-command-stdout>ok</local-command-stdout>"),           # not a prompt
    user("[Request interrupted by user]"),                             # not a prompt
    user("<task-notification>done</task-notification>", origin={"kind": "task-notification"}),
    user([{"type": "text", "text": "<pasted_content id=\"1\">\npasted prompt"}]),
    user("meta", isMeta=True),
    # Two results in one record: toolUseResult can't be attributed, so no line data.
    use("m1", "Read", {"file_path": "/proj/src/a.py"}),
    use("m2", "Read", {"file_path": "/proj/src/c.py"}),
    user([{"type": "tool_result", "tool_use_id": "m1", "content": "x"},
          {"type": "tool_result", "tool_use_id": "m2", "content": "y"}],
         toolUseResult={"type": "text", "file": {"totalLines": 7}}),
    # Odd paths: "//", ".", ".." and a relative path resolved against the record's cwd.
    use("o1", "Read", {"file_path": "/proj//src/./x/../d.py"}),
    result("o1", {"type": "text", "file": {"totalLines": 3}}),
    dict(use("o2", "Read", {"file_path": "sub.py"}), cwd="/proj/pkg"),
    result("o2", {"type": "text", "file": {"totalLines": 1}}),
    use("o3", "Read", {"file_path": "/proj/../etc/passwd"}),       # climbs out: a read by absolute path
    result("o3", {"type": "text", "file": {"totalLines": 1}}),
]
sub = [
    user("You are a helper. Fix tests."),
    use("s1", "Edit", {"file_path": "/proj/tests/t.py"}),
    result("s1", {"originalFile": "a\n", "structuredPatch": [{"lines": ["+b"]}]}),
]

# Detail view: what each kind of step shows in full.
detail = [
    user("Show me everything"),
    use("d1", "Edit", {"file_path": "/proj/src/a.py", "old_string": "two", "new_string": "2"}),
    result("d1", {"structuredPatch": [{"oldStart": 4, "oldLines": 3, "newStart": 4, "newLines": 3,
                                       "lines": [" one", "-two", "+2", " three"]}]}),
    use("d2", "Write", {"file_path": "/proj/src/new.py", "content": "print(1)\n\tindented\n"}),
    result("d2", {"type": "create", "structuredPatch": []}),
    use("d3", "Read", {"file_path": "/proj/src/a.py", "offset": 10}),
    result("d3", {"type": "text", "file": {"content": "ten\neleven", "startLine": 10, "totalLines": 40}}),
    use("d4", "Bash", {"command": "make gen\nmake test", "description": "build"}),
    result("d4", {"stdout": "built\nok", "stderr": "warning: x", "interrupted": False, "bashEditDiff": {
        "files": [{"filePath": "/proj/gen.c", "hunks": [{"oldStart": 1, "oldLines": 0, "newStart": 1,
                                                          "newLines": 1, "lines": ["+int x;"]}],
                   "created": True}]}}),
    use("d5", "Grep", {"pattern": "def", "path": "/proj/src", "output_mode": "content"}),
    user([{"type": "tool_result", "tool_use_id": "d5", "content": "src/a.py:1:def a():"}],
         toolUseResult={"mode": "content"}),
    use("d6", "mcp__deskpilot__ui", {"window": "Firefox", "query": "Save"}),
    user([{"type": "tool_result", "tool_use_id": "d6",
           "content": [{"type": "text", "text": "[e12] button Save"}, {"type": "image"}]}]),
    use("d7", "mcp__web__fetch", {"url": "http://x"}),
    user([{"type": "tool_result", "tool_use_id": "d7", "is_error": True, "content": "timeout"}]),
    use("d8", "MultiEdit", {"file_path": "/proj/src/a.py",
                            "edits": [{"old_string": "a", "new_string": "b"}]}),
    result("d8", {"filePath": "/proj/src/a.py"}),                  # no patch: the edits as asked
    # Two calls in one record; the second never answered (still running).
    {"type": "assistant", "timestamp": ts(), "message": {"content": [
        {"type": "tool_use", "id": "d9", "name": "WebSearch", "input": {"query": "raylib"}},
        {"type": "tool_use", "id": "d10", "name": "Bash", "input": {"command": "sleep 99"}}]}},
    user([{"type": "tool_result", "tool_use_id": "d9", "content": "3 results"}]),
    # An id that prefixes a later one: x10's result comes first and must not match x1.
    {"type": "assistant", "timestamp": ts(), "message": {"content": [
        {"type": "tool_use", "id": "x1", "name": "WebFetch", "input": {"url": "http://one"}},
        {"type": "tool_use", "id": "x10", "name": "WebFetch", "input": {"url": "http://ten"}}]}},
    user([{"type": "tool_result", "tool_use_id": "x10", "content": "ten"}]),
    user([{"type": "tool_result", "tool_use_id": "x1", "content": "one"}]),
    # Comments as Claude Code writes them: one block per record. n1 gets the text
    # before it; n2 also the text between the calls; n3 none (after a result).
    {"type": "assistant", "timestamp": ts(), "message": {"content": [
        {"type": "text", "text": "Checking two things.\n\nFirst the **config**."}]}},
    {"type": "assistant", "timestamp": ts(), "message": {"content": [{"type": "thinking", "thinking": ""}]}},
    {"type": "assistant", "timestamp": ts(), "message": {"content": [
        {"type": "tool_use", "id": "n1", "name": "WebFetch", "input": {"url": "http://cfg"}}]}},
    {"type": "assistant", "timestamp": ts(), "message": {"content": [{"type": "text", "text": "  \n"}]}},
    {"type": "assistant", "timestamp": ts(), "message": {"content": [{"type": "text", "text": "Then the docs."}]}},
    {"type": "assistant", "timestamp": ts(), "message": {"content": [
        {"type": "tool_use", "id": "n2", "name": "WebFetch", "input": {"url": "http://docs"}}]}},
    user([{"type": "tool_result", "tool_use_id": "n1", "content": "cfg"}]),
    user([{"type": "tool_result", "tool_use_id": "n2", "content": "docs"}]),
    {"type": "assistant", "timestamp": ts(), "message": {"content": [
        {"type": "tool_use", "id": "n3", "name": "WebFetch", "input": {"url": "http://more"}}]}},
    user([{"type": "tool_result", "tool_use_id": "n3", "content": "more"}]),
    # A failed command records no changes: the call is flagged and its files
    # guessed from the text (cd followed, /dev/null and here-doc bodies skipped).
    use("f1", "Bash", {"command": "cd /proj/src && sed -i '1,2d' a.py b.py && mv old.py new.py "
                                  "2>/dev/null && cat <<EOF > gen.h\necho no > nope\nEOF\nfalse"}),
    user([{"type": "tool_result", "tool_use_id": "f1", "is_error": True, "content": "Error: Exit code 1"}],
         toolUseResult="Error: Exit code 1"),
    # Nothing to guess: just flagged.
    use("f2", "Bash", {"command": "make test"}),
    user([{"type": "tool_result", "tool_use_id": "f2", "is_error": True, "content": "Error: Exit code 2\nFAIL"}],
         toolUseResult="Error: Exit code 2"),
    # A relative cd: the result's record carries the cwd after the command, so the
    # guess resolves against the call's own (src/c.py, not src/src/c.py).
    dict(use("f3", "Bash", {"command": "cd src && sed -i 1d c.py && false"}), cwd="/proj"),
    user([{"type": "tool_result", "tool_use_id": "f3", "is_error": True, "content": "Exit code 1"}],
         toolUseResult="Error: Exit code 1", cwd="/proj/src"),
    # Rejected or blocked by a hook: it never ran. Not flagged, nothing guessed.
    use("f4", "Bash", {"command": "rm keep.py"}),
    user([{"type": "tool_result", "tool_use_id": "f4", "is_error": True,
           "content": "The user doesn't want to proceed with this tool use."}], toolUseResult="User rejected tool use"),
    use("f5", "Bash", {"command": "rm keep2.py"}),
    user([{"type": "tool_result", "tool_use_id": "f5", "is_error": True,
           "content": "PreToolUse:Bash hook error: blocked"}]),
]

# For the analysis (written after the rest so earlier timestamps stay put).
def part(mid, out, content):  # one part of a response: each repeats its usage, output growing
    return {"type": "assistant", "timestamp": ts(), "message": {"id": mid, "content": content, "usage": {
        "output_tokens": out, "input_tokens": 3, "cache_creation_input_tokens": 100,
        "cache_read_input_tokens": 2000}}}

title = {"type": "ai-title", "aiTitle": "Build the thing", "sessionId": "s1"}  # untimed
prompt = user("Analyse it", parentUuid="p1")
r_use = use("r2", "Read", {"file_path": "/proj/src/a.py", "offset": 5, "limit": 10})
r_res = result("r2", {"type": "text", "file": {"filePath": "/proj/src/a.py", "startLine": 5, "numLines": 10,
                                               "totalLines": 40}})
i_use = use("r3", "Read", {"file_path": "/proj/shot.png"})
i_res = result("r3", {"type": "image", "file": {"type": "image/png", "originalSize": 1234}})  # no lines
p1 = part("msg1", 5, [{"type": "thinking", "thinking": ""}])
p2 = part("msg1", 9, [{"type": "text", "text": "ok"}])
p3 = part("msg1", 9, [{"type": "tool_use", "id": "b2", "name": "Bash", "input": {"command": "make test"}}])
b_res = result("b2", {"stdout": "ok"})
e_use = use("e4", "Edit", {"file_path": "/proj/src/a.py"})
e_res = result("e4", {"filePath": "/proj/src/a.py", "originalFile": "1\n2\n",
                      "structuredPatch": [{"oldStart": 2, "oldLines": 1, "newStart": 2, "newLines": 1,
                                           "lines": [" 1", "-2", "+two"]}]})
mm_use = use("e6", "MultiEdit", {"file_path": "/proj/src/a.py", "edits": []})
mm_res = result("e6", {"filePath": "/proj/src/a.py", "originalFile": "one\ntwo\nthree\n",
                       "structuredPatch": [{"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1, "lines": ["-one", "+1"]},
                                           {"oldStart": 3, "oldLines": 1, "newStart": 3, "newLines": 2,
                                            "lines": ["-three", "+3", "+4"]}]})
wu_use = use("w3", "Write", {"file_path": "/proj/README.md", "content": "a\nB\n"})
wu_res = result("w3", {"type": "update", "filePath": "/proj/README.md", "content": "a\nB\n", "originalFile": "a\nb\n",
                       "structuredPatch": [{"oldStart": 1, "oldLines": 2, "newStart": 1, "newLines": 2,
                                            "lines": [" a", "-b", "+B"]}]})
mk_use = use("b3", "Bash", {"command": "sed -i s/x/y/ Makefile sub/Makefile"})
mk_res = result("b3", {"stdout": "", "bashEditDiff": {"files": [  # same name, two folders: tr_change must not mix them
    {"filePath": "/proj/sub/Makefile", "hunks": [{"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1, "lines": ["-x", "+y"]}]},
    {"filePath": "/proj/Makefile", "hunks": [{"oldStart": 2, "oldLines": 1, "newStart": 2, "newLines": 1, "lines": ["-xx", "+yy"]}]}]}})
intr = user([{"type": "text", "text": "[Request interrupted by user for tool use]"}])
boundary = {"type": "system", "subtype": "compact_boundary", "timestamp": ts(), "content": "Conversation compacted"}
summary = user("This session is being continued from a previous conversation.", isCompactSummary=True)
extra = [title, prompt, r_use, r_res, i_use, i_res, p1, p2, p3, b_res, title, e_use, e_res, mm_use, mm_res, wu_use, wu_res,
         mk_use, mk_res, intr, boundary, summary,
         dict(title, aiTitle="Build and analyse the thing")]
# A resumed session: its file starts with copies of earlier records (same
# timestamps, ids), then goes on. Copies count once; the usage copy with more
# output wins, at the first part's time (only the second part is copied); the
# compaction repeats.
resumed = [
    boundary, prompt, dict(p2, message=dict(p2["message"], usage=dict(p2["message"]["usage"], output_tokens=11))),
    e_use, e_res, intr,
    user("Carry on", parentUuid="p2"),
    use("e5", "Edit", {"file_path": "/proj/src/a.py"}),
    result("e5", {"filePath": "/proj/src/a.py", "originalFile": "1\ntwo\n",
                  "structuredPatch": [{"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1,
                                       "lines": ["-1", "+one", " two"]}]}),
]

def write(path, recs):
    with open(path, "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")

os.makedirs(os.path.join(OUT, "s1", "subagents"), exist_ok=True)
write(os.path.join(OUT, "s1.jsonl"), main)
with open(os.path.join(OUT, "s1.jsonl"), "a") as f:  # lone surrogate: record must survive
    f.write('{"type":"user","timestamp":"2026-10-14T10:01:00.000Z","message":{"content":"bad \\ud800 text"}}\n')
    for r in extra:
        f.write(json.dumps(r) + "\n")
write(os.path.join(OUT, "s2.jsonl"), resumed)
write(os.path.join(OUT, "s1", "subagents", "agent-abc123def.jsonl"), sub)
os.makedirs(os.path.join(HERE, "fixtures", "detail"), exist_ok=True)
write(os.path.join(HERE, "fixtures", "detail", "d1.jsonl"), detail)
with open(os.path.join(OUT, "s1", "subagents", "agent-abc123def.meta.json"), "w") as f:
    json.dump({"agentType": "oh-my-claudecode:executor", "description": "Fix the tests"}, f)

# ---- Structure analysis (aigource --structure-dump): a repo at /REPO (tests/run.sh
# builds it in a temp dir from fixtures/structure/repo: before/ committed, after/
# on disk) worked on by a session, its subagent and a resumed copy of it.
ST = os.path.join(HERE, "fixtures", "structure")
REPO = "/REPO"
st = [0]


def sts():
    st[0] += 10
    return "2026-10-14T11:%02d:%02d.000Z" % divmod(st[0], 60)


def sts_last():  # the time of the record before, without moving on
    return "2026-10-14T11:%02d:%02d.000Z" % divmod(st[0], 60)


def srec(typ, content, ts=None, **kw):
    return {"type": typ, "timestamp": ts or sts(), "cwd": REPO, "message": {"role": typ, "content": content}, **kw}


def say(mid, text, call=None, out=20, ts=None):
    blocks = ([{"type": "text", "text": text}] if text else []) + ([call] if call else [])
    r = srec("assistant", blocks, ts)
    r["message"].update(id=mid, usage={"output_tokens": out, "input_tokens": 5, "cache_creation_input_tokens": 50,
                                       "cache_read_input_tokens": 1000})
    return r


def call(tid, name, inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def res(tid, result, err=False, content="ok"):
    return srec("user", [{"type": "tool_result", "tool_use_id": tid, "is_error": err, "content": content}],
                toolUseResult=result)


def prompt_rec(text):
    return srec("user", text, origin={"kind": "human"})


def apply(old, hunks):
    ol, new, at = old.split("\n"), [], 0
    for h in hunks:
        s = h["oldStart"] - 1 if h["oldLines"] else h["oldStart"]
        new += ol[at:s]
        at = s
        for x in h["lines"]:
            if x[0] in " -":
                assert ol[at] == x[1:], (ol[at], x)
                if x[0] == " ":
                    new.append(x[1:])
                at += 1
            else:
                new.append(x[1:])
    return "\n".join(new + ol[at:])


def hunk(o, ol, n, nl, *lines):
    return {"oldStart": o, "oldLines": ol, "newStart": n, "newLines": nl, "lines": list(lines)}


app0 = 'def greet(name):\n    return "hi " + name\n\n\ndef main():\n    print(greet("x"))\n'
lib0 = "int add(int a, int b) {\n    return a + b;\n}\n"
h_e1 = [hunk(1, 3, 1, 5, " def greet(name):", '-    return "hi " + name', "+    if not name:", '+        return "hi"',
             '+    return "hi " + name', " ")]
app1 = apply(app0, h_e1)
h_e2 = [hunk(2, 1, 2, 3, "-    return a + b;", "+    int s = a + b;", "+    if (s < 0) return 0;", "+    return s;")]
lib1 = apply(lib0, h_e2)
new0 = "def helper():\n    return 1\n"
h_b1 = [hunk(7, 2, 7, 2, " def main():", '-    print(greet("x"))', '+    print(greet("world"))')]
app2 = apply(app1, h_b1)
h_s1 = [hunk(2, 2, 2, 2, "     if not name:", '-        return "hi"', '+        return "hello"')]
app3 = apply(app2, h_s1)
h_e4 = [hunk(3, 1, 3, 1, '-        return "hello"', '+        return "hi there"')]
app4 = apply(app3, h_e4)
h_e5 = [hunk(2, 1, 2, 1, "-    return 1", "+    return 2")]
new1 = apply(new0, h_e5)
P = REPO + "/src/"

prompt1 = prompt_rec("Add a greeting fallback\nand tidy the C side")
main1 = [
    {"type": "ai-title", "aiTitle": "Greeting work", "sessionId": "st1"},
    prompt1,
    say("m1", "Let me look at the app first.", call("r1", "Read", {"file_path": P + "app.py", "limit": 2})),
    res("r1", {"type": "text", "file": {"filePath": P + "app.py", "startLine": 1, "numLines": 2, "totalLines": 6}}),
    say("m2", "Adding the fallback.", out=5),
    say("m2", "", call("e1", "Edit", {"file_path": P + "app.py"}), out=30),  # a later part: more output
    res("e1", {"filePath": P + "app.py", "originalFile": app0, "structuredPatch": h_e1}),
    say("m3", "", call("g1", "Bash", {"command": "grep -n greet src/app.py"})),
    res("g1", {"stdout": "1:def greet(name):"}),
    say("m4", "", call("c1", "Bash", {"command": "cat src/lib.c | head -3"})),
    res("c1", {"stdout": lib0}),
    say("m5", "Now the C side: keep the sum in a variable."),
    # A task notification between the agent's text and its call: the text is still its comment.
    srec("user", "<task-notification>done</task-notification>", sts_last(), origin={"kind": "task-notification"}),
    say("m5", "", call("e2", "Edit", {"file_path": P + "lib.c"}), ts=sts_last()),
    res("e2", {"filePath": P + "lib.c", "structuredPatch": h_e2}),  # no originalFile: lined up with git
    say("m6", "", call("x1", "Bash", {"command": "./fmt.sh src/lib.c"})),  # the same edit credited to it too
    res("x1", {"stdout": "", "bashEditDiff": {"files": [{"filePath": P + "lib.c", "hunks": h_e2}]}}),
    say("m7", "", call("w1", "Write", {"file_path": P + "new.py", "content": new0})),
    res("w1", {"type": "create", "filePath": P + "new.py", "content": new0, "structuredPatch": []}),
    say("m8", "Renaming the default with sed.", call("b1", "Bash", {"command": "sed -i s/x/world/ src/app.py"})),
    # Claude Code copies the result into an api-request-blob record first: not the result.
    {"type": "api-request-blob", "sessionId": "st1", "hash": "h",
     "message": {"role": "user", "content": [{"tool_use_id": "b1", "type": "tool_result", "content": "copy"}]}},
    res("b1", {"stdout": "", "bashEditDiff": {"files": [{"filePath": P + "app.py", "hunks": h_b1}]}}),
    say("m9", "", call("t1", "Bash", {"command": "make test"})),
    res("t1", {"stdout": "ok"}),
]
sub = [  # runs between t1 and t2
    srec("user", "Polish the greeting"),
    say("ms1", "Changing the fallback text.", call("s1e", "Edit", {"file_path": P + "app.py"})),
    res("s1e", {"filePath": P + "app.py", "originalFile": app2, "structuredPatch": h_s1}),
]
main2 = [
    say("m10", "", call("t2", "Bash", {"command": "cd sub && make test"})),
    res("t2", {"stdout": "", "stderr": "1 failed"}, err=True, content="Exit code 2"),
    srec("user", "[Request interrupted by user]"),
    {"type": "system", "subtype": "compact_boundary", "timestamp": sts(), "content": "Conversation compacted"},
]
prompt2 = prompt_rec("That's wrong, the greeting broke")
e4_use = say("m11", "Back to a plain fallback.", call("e4", "Edit", {"file_path": P + "app.py"}))
e4_res = res("e4", {"filePath": P + "app.py", "originalFile": app3, "structuredPatch": h_e4})
main3 = [prompt2, e4_use, e4_res,
         say("m12", "", call("t3", "Bash", {"command": "bash tests/run.sh"})),
         res("t3", {"stdout": "ok"})]
resumed = [  # copies of the last records, then new work
    prompt2, dict(e4_use, message=dict(e4_use["message"], usage=dict(e4_use["message"]["usage"], output_tokens=99))),
    e4_res,
    prompt_rec("Carry on with the helper"),
    say("m13", "Bumping the helper.", call("e5", "Edit", {"file_path": P + "new.py"})),
    res("e5", {"filePath": P + "new.py", "originalFile": new0, "structuredPatch": h_e5}),
]
# Timestamps: the subagent's records sit between t1 and t2 (assigned in order above).
os.makedirs(os.path.join(ST, "st1", "subagents"), exist_ok=True)
write(os.path.join(ST, "st1.jsonl"), main1 + main2 + main3)
write(os.path.join(ST, "st1", "subagents", "agent-sub1.jsonl"), sub)
with open(os.path.join(ST, "st1", "subagents", "agent-sub1.meta.json"), "w") as f:
    json.dump({"agentType": "oh-my-claudecode:executor", "description": "Polish the greeting"}, f)
write(os.path.join(ST, "st2.jsonl"), resumed)
for side, files in (("before", {"src/app.py": app0, "src/lib.c": lib0, "README.md": "# Demo\n"}),
                    ("after", {"src/app.py": app4, "src/lib.c": lib1, "src/new.py": new1, "README.md": "# Demo\n"})):
    for rel, text in files.items():
        p = os.path.join(ST, "repo", side, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
