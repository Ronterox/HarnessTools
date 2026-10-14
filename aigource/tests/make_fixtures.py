#!/usr/bin/env python3
"""Writes tests/fixtures: hand-built transcripts covering each parser rule.
Shapes mirror real Claude Code records (see src/transcript.c). Root is /proj."""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "fixtures", "proj")
t = [0]

def ts():
    t[0] += 1
    return "2026-10-14T10:00:%02d.000Z" % t[0]

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
    result("o1", {"file": {"totalLines": 3}}),
    dict(use("o2", "Read", {"file_path": "sub.py"}), cwd="/proj/pkg"),
    result("o2", {"file": {"totalLines": 1}}),
    use("o3", "Read", {"file_path": "/proj/../etc/passwd"}),       # climbs out: no event
    result("o3", {"file": {"totalLines": 1}}),
]
sub = [
    user("You are a helper. Fix tests."),
    use("s1", "Edit", {"file_path": "/proj/tests/t.py"}),
    result("s1", {"originalFile": "a\n", "structuredPatch": [{"lines": ["+b"]}]}),
]

def write(path, recs):
    with open(path, "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")

os.makedirs(os.path.join(OUT, "s1", "subagents"), exist_ok=True)
write(os.path.join(OUT, "s1.jsonl"), main)
with open(os.path.join(OUT, "s1.jsonl"), "a") as f:  # lone surrogate: record must survive
    f.write('{"type":"user","timestamp":"2026-10-14T10:01:00.000Z","message":{"content":"bad \\ud800 text"}}\n')
write(os.path.join(OUT, "s1", "subagents", "agent-abc123def.jsonl"), sub)
with open(os.path.join(OUT, "s1", "subagents", "agent-abc123def.meta.json"), "w") as f:
    json.dump({"agentType": "oh-my-claudecode:executor"}, f)
