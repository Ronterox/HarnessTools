#!/usr/bin/env python3
"""Writes tests/fixtures/context/proj: a hand-built project of transcripts for
the context engine (src/context.c), with request records hashed the way
Claude Code hashes them (sha256 of compact JSON with sorted keys). Root /REPO.

s1.jsonl (main session, request records), call by call:
  r1  no base: a prompt with CLAUDE.md injected in it (a user record, no blob)
  r2  base r1, keep 1: the response (rebuilt from its two records) and a run of
      two user records sent as one message (a Read result with a hook's
      reminder in it, then a reminder record)
  r3  base r2: an Edit's result written as a blob (same message as its record)
  r4  base r3: a command's output (it reads src/a.c, then writes it: the Read
      of r2 goes stale, known: its own command); no response (interrupted)
  r5  no base, a full resend: earlier messages unchanged keep their instances;
      m1 is sent back without its thinking (its other items are taken back); an
      effort system message, a tool added mid-conversation, a prompt with an
      image (2000x1000 PNG header); its response edits src/a.c (the Read of r2
      goes stale) and has visible thinking
  r6  after a compaction: the summary alone; two thinking blocks, two Reads of
      src/b.py, and a command that reads src/c.h and writes it with sed -n -i
      (no change recorded: Claude Code's notice of c.h is its own write)
  r7  base r6: a message found only in s2.jsonl; m5 sent back without its
      thinking (its new items find their response by the block left); the API
      dropped r6's second thinking block (thinking_drop, blockIndex 1); src/b.py
      was changed meanwhile by the user (noticed), by the subagent and by
      another session. Its response greps a name two results share (a tie in
      the links) and edits src/b.py (two reads of it before)
  r8  base r7: sends m7 back, so its actions are items with links
s1-append.jsonl (outside the project): r9, appended by tests/test_context.c.
s1/subagents/agent-sub1.jsonl: edits src/b.py, reads it 2 s later; the notice
  1 s after is its own Edit's (within 5 s); a compaction boundary before a
  request that keeps messages (not a compaction).
s1/subagents/agent-sub2.jsonl: one call with exact numbers: the system block's
  middle falls exactly on the cache-read boundary (read).
s2.jsonl: another session: a blob s1 needs, and an Edit of src/b.py.
s3.jsonl: a response with 70 thinking blocks the API drops in later calls by
  blockIndex -1 (the last), 100 (past the end: the last) and -70 (the first),
  once by messageIndex -4 (from the end); a Read result with CRLF lines (not
  code, as the page's regex has it); two results with identifiers of 306
  characters that differ only in the last one, and a Grep for one of them; a
  Write; and its change 0.02 s before the next call, which rounded to 0.1 s
  lands after the call's raw time (the page rounds both and counts it).
odd/ (outside the project; tests/test_context.c only, the draft fails on them):
  drops.jsonl thinking_drop indices beyond either end and usage near 2^63;
  deep.jsonl (no request records) a tool input nested 1200 deep.
e1.jsonl: an old session without request records (estimated): an attachment,
  two responses, a compaction.
Canonical JSON corners ride in tool inputs and the tool definitions: floats
(1.5, 1e+20, 1e-05, -0.0), a 30-digit integer, unicode, a control character,
"/" and keys that sort differently by code point than by case.
"""
import base64, hashlib, json, os, shutil, struct

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "fixtures", "context", "proj")
R = "/REPO"


def H(m):
    return hashlib.sha256(json.dumps(m, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


clock = [0]


def ts(step=1):
    clock[0] += step
    return "2026-10-14T10:%02d:%02d.%03dZ" % (clock[0] // 60, clock[0] % 60, clock[0] * 7 % 1000)


def at(sec, ms=0):
    return "2026-10-14T10:%02d:%02d.%03dZ" % (sec // 60, sec % 60, ms)


def user(content, **kw):
    return {"type": "user", "timestamp": ts(), "cwd": R, "message": {"role": "user", "content": content}, **kw}


def asst(mid, ref, rq, blocks, usage, model="claude-opus-5-5"):
    """One response as Claude Code writes it: a record per content block, usage on each."""
    return [{"type": "assistant", "timestamp": ts(), "cwd": R, "requestRef": ref, "requestId": rq,
             "message": {"model": model, "id": mid, "type": "message", "role": "assistant", "content": [b],
                         "usage": usage}} for b in blocks]


def resp_msg(recs):
    return {"role": "assistant", "content": [c for r in recs for c in r["message"]["content"]]}


def run_msg(*recs):
    out = []
    for r in recs:
        c = r["message"]["content"]
        out += [{"type": "text", "text": c}] if isinstance(c, str) else c
    return {"role": "user", "content": out}


def usage(inp, read, write, w1h, out, think=0):
    u = {"input_tokens": inp, "cache_read_input_tokens": read, "cache_creation_input_tokens": write,
         "cache_creation": {"ephemeral_5m_input_tokens": write - w1h, "ephemeral_1h_input_tokens": w1h},
         "output_tokens": out}
    if think:
        u["output_tokens_details"] = {"thinking_tokens": think}
    return u


def req(rid, tail, base=None, keep=0, sh="sh1"):
    r = {"type": "api-request", "id": rid, "timestamp": ts(), "querySource": "repl_main_thread", "shapeHash": sh,
         "params": {"model": "claude-opus-5-5", "max_tokens": 64000}, "keep": keep, "tail": tail}
    if base:
        r["base"] = base
    return r


def blob(m):
    return {"type": "api-request-blob", "hash": H(m), "message": m}


def png_b64(w, h):
    raw = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00"
    return base64.b64encode(raw + b"\x00" * 12).decode()


CORNERS = {"b": 1.5, "a": 1e20, "Z": 1e-05, "é": -0.0, "_": 123456789012345678901234567890,
           "s": "café → ✓ \x01 a/b \"q\" \\ \t", "list": [[], {}, [1, [2.25, None, True]]]}
SHAPE = {"system": [{"type": "text", "text": "You are Claude Code."}, {"type": "text", "text": "Env: /REPO, linux"}],
         "tools": [{"name": "Read", "description": "Reads a file", "input_schema": {"type": "object", "x": CORNERS}},
                   {"name": "Bash", "description": "Runs a command", "input_schema": {"type": "object"}}]}


def s1():
    out = [{"type": "api-request-shape", "shapeHash": "sh1", "timestamp": ts(), "shape": SHAPE}]
    u1 = user("<system-reminder>\nCodebase and user instructions are shown below.\nContents of /REPO/CLAUDE.md:\nbe brief\n"
              "</system-reminder>\nFix the bug in src/a.c")
    out += [u1, req("r1", [H(u1["message"])])]
    a1 = asst("m1", "r1", "req1", [
        {"type": "thinking", "thinking": "", "signature": "c2lnbmF0dXJl" * 8},
        {"type": "text", "text": "I'll read it."},
        {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": R + "/src/a.c"}}],
        usage(3, 0, 900, 100, 60, think=25))
    out += a1
    u2 = user([{"type": "tool_result", "tool_use_id": "t1", "content": [
        {"type": "text", "text": "     1→int a;\n     2→int b(void) { return a; }\n"},
        {"type": "text", "text": "<system-reminder>\nPostToolUse:Read hook additional context: looks fine\n</system-reminder>"}]}],
        toolUseResult={"type": "text", "file": {"filePath": R + "/src/a.c", "startLine": 1, "numLines": 2}})
    u3 = user([{"type": "text", "text": "<system-reminder>\nToday is Tuesday.\n</system-reminder>"}], isMeta=True)
    out += [u2, u3, req("r2", [H(resp_msg(a1)), H(run_msg(u2, u3))], base="r1", keep=1)]
    a2 = asst("m2", "r2", "req2", [
        {"type": "text", "text": "Found it; fixing."},
        {"type": "tool_use", "id": "t2", "name": "Edit", "input": {
            "file_path": R + "/src/a.c", "old_string": "int a;", "new_string": "int a = 0;", "meta": CORNERS}}],
        usage(5, 950, 200, 0, 40))
    out += a2
    u4 = user([{"type": "tool_result", "tool_use_id": "t2", "content": "The file /REPO/src/a.c has been updated."}])
    out += [u4, blob(u4["message"]), req("r3", [H(resp_msg(a2)), H(u4["message"])], base="r2", keep=3)]
    a3 = asst("m3", "r3", "req3", [
        {"type": "tool_use", "id": "t3", "name": "Bash", "input": {
            "command": "cd /REPO && cat src/a.c | head -5 && sed -i 's/b(void)/b(int)/' src/a.c",
            "description": "Show the fix, then change b's signature"}}],
        usage(4, 1150, 100, 0, 30))
    out += a3
    u5 = user([{"type": "tool_result", "tool_use_id": "t3", "content": "int a = 0;\nint b(void) { return a; }"}])
    out += [u5, req("r4", [H(resp_msg(a3)), H(u5["message"])], base="r3", keep=5)]  # interrupted: no response
    u6 = user([{"type": "text", "text": "[Request interrupted by user]"}])
    u7 = user([{"type": "text", "text": "Go on, and check src/b.py too."},
               {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png_b64(2000, 1000)}}])
    effort = {"role": "system", "content": [], "output_config": {"effort": "high"}}
    added = {"role": "system", "content": [{"type": "tool_addition", "tool": {"definition": {
        "name": "WebFetch", "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}}}}}]}
    a1_less = {"role": "assistant", "content": resp_msg(a1)["content"][1:]}  # re-sent without its thinking
    out += [blob(effort), blob(added), blob(a1_less)]
    tail5 = [H(u1["message"]), H(a1_less), H(run_msg(u2, u3)), H(resp_msg(a2)), H(u4["message"]),
             H(resp_msg(a3)), H(u5["message"]), H(effort), H(added), H(run_msg(u6, u7))]
    out += [u6, u7, req("r5", tail5)]
    a5 = asst("m5", "r5", "req5", [
        {"type": "thinking", "thinking": "The read of a.c is old now; b.py next.", "signature": "c2ln" * 6},
        {"type": "text", "text": "Tightening `b` in src/a.c."},
        {"type": "tool_use", "id": "t5", "name": "Edit", "input": {
            "file_path": R + "/src/a.c", "old_string": "return a;", "new_string": "return a + 1;"}}],
        usage(6, 1300, 3000, 1000, 80, think=30))
    out += a5
    u8 = user([{"type": "tool_result", "tool_use_id": "t5", "content": "updated"}])
    out += [u8, {"type": "system", "subtype": "compact_boundary", "timestamp": ts(), "content": "Conversation compacted"}]
    u9 = user("This session is being continued from a previous conversation that ran out of context. Summary: "
              "fixed src/a.c; src/b.py is next.", isCompactSummary=True)
    out += [u9, req("r6", [H(u9["message"])])]
    a6 = asst("m6", "r6", "req6", [
        {"type": "thinking", "thinking": "Read b.py.", "signature": "c2ln" * 4},
        {"type": "thinking", "thinking": "And c.h.", "signature": "c2ln" * 3},
        {"type": "tool_use", "id": "t6", "name": "Read", "input": {"file_path": R + "/src/b.py", "offset": 3, "limit": 2}},
        {"type": "tool_use", "id": "t6c", "name": "Read", "input": {"file_path": R + "/src/b.py", "limit": 2}},
        {"type": "tool_use", "id": "t6b", "name": "Bash", "input": {"command": "cat src/c.h; sed -n -i 's/x/y/' src/c.h"}}],
        usage(2, 0, 400, 0, 20, think=8))
    out += a6
    u10 = user([{"type": "tool_result", "tool_use_id": "t6",
                 "content": "     3→def helper():\n     4→    return frobnicate_widget(1)\n"},
                {"type": "tool_result", "tool_use_id": "t6c", "content": "     1→import os\n     2→\n"},
                {"type": "tool_result", "tool_use_id": "t6b", "content": "#define X frobnicate_widget"}])
    out += [u10]
    gap = clock[0]
    clock[0] += 30  # in this gap the user changes src/b.py (noticed at +5), then the subagent and s2 edit it
    out += [{"type": "attachment", "timestamp": at(gap + 5), "cwd": R,
             "attachment": {"type": "edited_text_file", "filename": R + "/src/b.py", "snippet": "x"}},
            # c.h changed as t6b's own sed -i wrote it (no edit recorded for that command): not a change of others
            {"type": "attachment", "timestamp": at(gap + 6), "cwd": R,
             "attachment": {"type": "edited_text_file", "filename": R + "/src/c.h", "snippet": "y"}}]
    out += [{"type": "attachment", "timestamp": ts(), "cwd": R, "attachment": {
        "type": "thinking_drop", "requestId": "req7",
        "newlyDropped": {"blockCount": 1, "reason": "prefix_mismatch",
                         "first": {"messageIndex": 1, "blockIndex": 1}, "last": {"messageIndex": 1, "blockIndex": 1}}}}]
    # m5 (never sent back before the compaction) sent now without its thinking: no item of it is free, so its new
    # items find their response by a block left (t5) and weigh by its usage
    a5_less = {"role": "assistant", "content": resp_msg(a5)["content"][1:]}
    out += [blob(a5_less), req("r7", [H(resp_msg(a6)), H(u10["message"]), H(ELSEWHERE), H(a5_less)], base="r6", keep=1)]
    a7 = asst("m7", "r7", "req7", [
        {"type": "text", "text": "Done: helper reviewed."},
        {"type": "tool_use", "id": "t7", "name": "Grep", "input": {"pattern": "frobnicate_widget"}},  # a tie: two results
        {"type": "tool_use", "id": "t7e", "name": "Edit", "input": {  # two reads of b.py before it
            "file_path": R + "/src/b.py", "old_string": "1", "new_string": "2"}}], usage(3, 700, 150, 0, 12))
    out += a7
    # r8 sends m7 back, so its actions are items (and have links)
    u11 = {"type": "user", "timestamp": at(gap + 40), "cwd": R, "message": {"role": "user", "content": "Thanks."}}
    out += [u11, {"type": "api-request", "id": "r8", "timestamp": at(gap + 40), "shapeHash": "sh1", "base": "r7", "keep": 5,
                  "querySource": "repl_main_thread", "params": {"model": "claude-opus-5-5"},
                  "tail": [H(resp_msg(a7)), H(u11["message"])]}]
    a8 = asst("m8", "r8", "req8", [{"type": "text", "text": "You're welcome."}], usage(2, 900, 30, 0, 5))
    for r in a8:
        r["timestamp"] = at(gap + 41)
    out += a8
    out += [{"type": "ai-title", "aiTitle": "Fix the bug in a.c"}]
    S1_END[:] = [a8, gap]
    return out


S1_END = []  # m7's records and the gap's start, for the appended records and the subagents


ELSEWHERE = {"role": "user", "content": [{"type": "text", "text": "<system-reminder>\nA note only s2 recorded.\n</system-reminder>"}]}


def sub1(gap):
    """Edits src/b.py, reads it 2 s later; Claude Code notices the change 1 s after that (inside the 5 s after its
    own Edit: not another change); a compaction boundary before a request that keeps messages (not a compaction)."""
    t = gap + 8
    out = [{"type": "api-request-shape", "shapeHash": "shs", "timestamp": at(t), "shape": {
        "system": [{"type": "text", "text": "You are a subagent."}], "tools": []}}]
    u = {"type": "user", "timestamp": at(t), "cwd": R, "message": {"role": "user", "content": "Rename helper in src/b.py"}}
    out += [u, {"type": "api-request", "id": "q1", "timestamp": at(t), "shapeHash": "shs", "keep": 0,
                "params": {"model": "claude-sonnet-5-5"}, "tail": [H(u["message"])]}]
    sm1 = [{"type": "assistant", "timestamp": at(t), "cwd": R, "requestRef": "q1", "requestId": "rq1",
            "message": {"model": "claude-sonnet-5-5", "id": "sm1", "role": "assistant", "content": [b],
                        "usage": usage(1, 0, 50, 0, 10)}} for b in [
        {"type": "tool_use", "id": "st1", "name": "Edit", "input": {
            "file_path": R + "/src/b.py", "old_string": "helper", "new_string": "helper2"}},
        {"type": "tool_use", "id": "st2", "name": "Read", "input": {"file_path": R + "/src/b.py"}}]]
    out += sm1
    r1 = {"type": "user", "timestamp": at(t, 100), "cwd": R, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "st1", "content": "ok"}]}}
    r2 = {"type": "user", "timestamp": at(t + 2), "cwd": R, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "st2", "content": "     1→def helper2():\n"}]}}
    out += [r1, r2, {"type": "attachment", "timestamp": at(t + 3), "cwd": R,
                     "attachment": {"type": "edited_text_file", "filename": R + "/src/b.py", "snippet": "z"}},
            {"type": "system", "subtype": "compact_boundary", "timestamp": at(t + 3, 500)}]
    out += [{"type": "api-request", "id": "q2", "timestamp": at(t + 4), "shapeHash": "shs", "base": "q1", "keep": 1,
             "params": {"model": "claude-sonnet-5-5"}, "tail": [H(resp_msg(sm1)), H(run_msg(r1, r2))]}]
    out += [{"type": "assistant", "timestamp": at(t + 4, 500), "cwd": R, "requestRef": "q2", "requestId": "rq2",
             "message": {"model": "claude-sonnet-5-5", "id": "sm2", "role": "assistant",
                         "content": [{"type": "text", "text": "Renamed."}], "usage": usage(1, 60, 20, 0, 3)}}]
    return out


def sub2(gap):
    """One call whose numbers are exact: system 16 characters (4.0), prompt 24 (6.0), usage 5 + 2 read + 3 written,
    so the scale is 1 and the system block's middle (2) falls exactly on the cache-read boundary: read."""
    t = gap + 9
    sh = {"system": [{"type": "text", "text": "You are subagent"}], "tools": []}
    u = {"type": "user", "timestamp": at(t), "cwd": R, "message": {"role": "user", "content": "Count the words in a.txt"}}
    return [{"type": "api-request-shape", "shapeHash": "shx", "timestamp": at(t), "shape": sh}, u,
            {"type": "api-request", "id": "x1", "timestamp": at(t), "shapeHash": "shx", "keep": 0,
             "params": {"model": "claude-opus-5-5"}, "tail": [H(u["message"])]},
            {"type": "assistant", "timestamp": at(t, 300), "cwd": R, "requestRef": "x1", "requestId": "rx1",
             "message": {"model": "claude-opus-5-5", "id": "xm1", "role": "assistant",
                         "content": [{"type": "text", "text": "Five."}], "usage": usage(5, 2, 3, 0, 1)}}]


def s1_append():
    """Records appended to s1 later (tests/test_context.c, a reload): one more call and its response."""
    a8, gap = S1_END
    u12 = {"type": "user", "timestamp": at(gap + 50), "cwd": R, "message": {"role": "user", "content": "Bye."}}
    return [u12, {"type": "api-request", "id": "r9", "timestamp": at(gap + 50), "shapeHash": "sh1", "base": "r8", "keep": 7,
                  "querySource": "repl_main_thread", "params": {"model": "claude-opus-5-5"},
                  "tail": [H(resp_msg(a8)), H(u12["message"])]},
            {"type": "assistant", "timestamp": at(gap + 51), "cwd": R, "requestRef": "r9", "requestId": "req9",
             "message": {"model": "claude-opus-5-5", "id": "m9", "role": "assistant",
                         "content": [{"type": "text", "text": "Bye."}], "usage": usage(2, 950, 20, 0, 2)}}]


def s2(t_edit):
    return [blob(ELSEWHERE),
            {"type": "assistant", "timestamp": t_edit, "cwd": R, "message": {"id": "x1", "role": "assistant", "content": [
                {"type": "tool_use", "id": "xt1", "name": "Bash", "input": {"command": "sed -i s/1/2/ src/b.py"}}]}},
            {"type": "user", "timestamp": t_edit, "cwd": R, "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "xt1", "content": ""}]}}]


def s3():
    """See the top. Its own clock: the first call at a whole second (t0), so the rounding is exact."""
    T = 2000
    W = "frob_" + "x" * 300  # 305 characters: identifiers that differ only past 256
    sh = {"system": [{"type": "text", "text": "You are s3."}], "tools": []}
    u1 = {"type": "user", "timestamp": at(T), "cwd": R, "message": {"role": "user", "content": "Look at the s3 sources."}}
    out = [{"type": "api-request-shape", "shapeHash": "sh3", "timestamp": at(T), "shape": sh}, u1,
           {"type": "api-request", "id": "p1", "timestamp": at(T), "shapeHash": "sh3", "keep": 0,
            "params": {"model": "claude-opus-5-5"}, "tail": [H(u1["message"])]}]

    def resp(mid, ref, rq, sec, blocks, u):
        return [{"type": "assistant", "timestamp": at(sec), "cwd": R, "requestRef": ref, "requestId": rq,
                 "message": {"model": "claude-opus-5-5", "id": mid, "role": "assistant", "content": [b], "usage": u}}
                for b in blocks]

    def drop(sec, rq, mi, bi):
        return {"type": "attachment", "timestamp": at(sec), "cwd": R, "attachment": {
            "type": "thinking_drop", "requestId": rq, "newlyDropped": {"first": {"messageIndex": mi, "blockIndex": bi}}}}

    def req3(rid, sec, ms, base, keep, tail):
        return {"type": "api-request", "id": rid, "timestamp": at(sec, ms), "shapeHash": "sh3", "base": base, "keep": keep,
                "params": {"model": "claude-opus-5-5"}, "tail": tail}

    a1 = resp("n1", "p1", "q1", T + 1, [{"type": "thinking", "thinking": "step %d" % i, "signature": "c2ln"} for i in range(70)] + [
        {"type": "tool_use", "id": "v1", "name": "Read", "input": {"file_path": R + "/s3/crlf.c"}},
        {"type": "tool_use", "id": "v2", "name": "Bash", "input": {"command": "grep -rn frob s3/one"}},
        {"type": "tool_use", "id": "v3", "name": "Bash", "input": {"command": "grep -rn frob s3/two"}}],
        usage(4, 0, 600, 0, 900, think=700))
    u2 = {"type": "user", "timestamp": at(T + 2), "cwd": R, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "v1", "content": "     1\u2192int x;\r\n     2\u2192int y;\r\n"},
        {"type": "tool_result", "tool_use_id": "v2", "content": "s3/one:1: " + W + "a here"},
        {"type": "tool_result", "tool_use_id": "v3", "content": "s3/two:1: " + W + "b here"}]}}
    out += a1 + [u2, req3("p2", T + 3, 0, "p1", 1, [H(resp_msg(a1)), H(u2["message"])]), drop(T + 3, "q2", 1, -1)]
    a2 = resp("n2", "p2", "q2", T + 4, [
        {"type": "text", "text": "Found it."},
        {"type": "tool_use", "id": "v4", "name": "Grep", "input": {"pattern": W + "a"}},
        {"type": "tool_use", "id": "v5", "name": "Write", "input": {"file_path": R + "/s3/crlf.c", "content": "int z;\n"}}],
        usage(5, 600, 300, 0, 80))
    u3 = {"type": "user", "timestamp": at(T + 10, 60), "cwd": R, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "v4", "content": "s3/one:1"},
        {"type": "tool_result", "tool_use_id": "v5", "content": "File created"}]}}
    # the Write's result at T + 10.06, the next call at T + 10.08: the change rounds to 10.1, after the call's
    # raw time, but the page rounds both (10.1, 10.1) and counts it at that call
    out += a2 + [u3, req3("p3", T + 10, 80, "p2", 3, [H(resp_msg(a2)), H(u3["message"])]), drop(T + 10, "q3", -4, 100)]
    a3 = resp("n3", "p3", "q3", T + 11, [{"type": "text", "text": "Written."}], usage(3, 900, 40, 0, 5))
    u4 = {"type": "user", "timestamp": at(T + 19), "cwd": R, "message": {"role": "user", "content": "And?"}}
    out += a3 + [u4, req3("p4", T + 20, 0, "p3", 5, [H(resp_msg(a3)), H(u4["message"])]), drop(T + 20, "q4", 1, -70)]
    out += resp("n4", "p4", "q4", T + 21, [{"type": "text", "text": "Done."}], usage(3, 950, 20, 0, 2))
    return out


def odd_drops():
    """thinking_drop indices beyond either end (skipped), usage near 2^63 (clamped)."""
    T = 2500
    big = 9223372036854775000
    u1 = {"type": "user", "timestamp": at(T), "cwd": R, "message": {"role": "user", "content": "Hi."}}
    huge = {"input_tokens": big, "cache_read_input_tokens": big, "cache_creation_input_tokens": big,
            "cache_creation": {"ephemeral_1h_input_tokens": -big}, "output_tokens": big,
            "output_tokens_details": {"thinking_tokens": -big}}
    a1 = [{"type": "assistant", "timestamp": at(T + 1), "requestRef": "o1", "requestId": "oq1", "message": {
        "id": "om1", "role": "assistant", "model": "claude-opus-5-5", "content": [b], "usage": huge}}
          for b in [{"type": "thinking", "thinking": "a", "signature": "s"}, {"type": "thinking", "thinking": "b", "signature": "s"},
                    {"type": "text", "text": "Hello."}]]
    u2 = {"type": "user", "timestamp": at(T + 2), "cwd": R, "message": {"role": "user", "content": "Again."}}
    out = [u1, {"type": "api-request", "id": "o1", "timestamp": at(T), "keep": 0, "tail": [H(u1["message"])]}] + a1 + [u2]
    for mi, bi in [(1, -3), (99, 0), (-99, 0), (1, -2)]:  # beyond the start, past the end, beyond the start; the first
        out.append({"type": "attachment", "timestamp": at(T + 3), "attachment": {
            "type": "thinking_drop", "requestId": "oq2", "newlyDropped": {"first": {"messageIndex": mi, "blockIndex": bi}}}})
    out += [{"type": "api-request", "id": "o2", "timestamp": at(T + 4), "base": "o1", "keep": 1,
             "tail": [H(resp_msg(a1)), H(u2["message"])]},
            {"type": "assistant", "timestamp": at(T + 5), "requestRef": "o2", "requestId": "oq2", "message": {
                "id": "om2", "role": "assistant", "content": [{"type": "text", "text": "Bye."}], "usage": huge}}]
    return out


def odd_deep():
    """No request records: a Skill call whose input is nested 1200 deep (too deep to dump: Python fails too)."""
    deep = "[" * 1200 + "]" * 1200
    return ['{"type":"user","timestamp":"%s","message":{"role":"user","content":"Go."}}' % at(2600),
            '{"type":"assistant","timestamp":"%s","message":{"id":"d1","role":"assistant","content":[{"type":"tool_use",'
            '"id":"dt1","name":"Skill","input":{"skill":%s}}],"usage":{"input_tokens":5,"output_tokens":3}}}' % (at(2601), deep),
            '{"type":"user","timestamp":"%s","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"dt1",'
            '"content":"ok"}]}}' % at(2602),
            '{"type":"assistant","timestamp":"%s","message":{"id":"d2","role":"assistant","content":[{"type":"text",'
            '"text":"Done."}],"usage":{"input_tokens":9,"output_tokens":1}}}' % at(2603)]


def e1():
    clock[0] = 3000
    out = [user("Summarize README.md"),
           {"type": "attachment", "timestamp": ts(), "cwd": R, "attachment": {"type": "date", "content": "Today is Tuesday."}},
           {"type": "attachment", "timestamp": ts(), "cwd": R, "attachment": {"type": "hook_success", "content": "ok"}}]
    out += asst("e-m1", None, "er1", [{"type": "tool_use", "id": "et1", "name": "Read",
                                       "input": {"file_path": R + "/README.md"}}], usage(2, 0, 5000, 0, 20), "claude-sonnet-5-5")
    out += [user([{"type": "tool_result", "tool_use_id": "et1", "content": "     1→# Title\n"}])]
    out += asst("e-m2", None, "er2", [{"type": "text", "text": "It is a title."}], usage(2, 5000, 120, 0, 10), "claude-sonnet-5-5")
    out += [{"type": "system", "subtype": "compact_boundary", "timestamp": ts()},
            user("This session is being continued from a previous conversation. Summary: read README.md.", isCompactSummary=True)]
    out += asst("e-m3", None, "er3", [{"type": "text", "text": "Anything else?"}], usage(2, 0, 3000, 0, 5), "claude-sonnet-5-5")
    for r in out:
        r.pop("requestRef", None)
    return out


def write(path, recs):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    shutil.rmtree(OUT, ignore_errors=True)
    main = s1()
    gap = S1_END[1]  # the subagents work after the read of b.py, before r7; then s2 edits b.py
    write(os.path.join(OUT, "s1.jsonl"), main)
    write(os.path.join(OUT, "..", "s1-append.jsonl"), s1_append())
    write(os.path.join(OUT, "s1", "subagents", "agent-sub1.jsonl"), sub1(gap))
    with open(os.path.join(OUT, "s1", "subagents", "agent-sub1.meta.json"), "w") as f:
        json.dump({"agentType": "executor", "description": "Rename helper", "toolUseId": "toolu_sub1",
                   "model": "claude-sonnet-5-5"}, f)
    write(os.path.join(OUT, "s1", "subagents", "agent-sub2.jsonl"), sub2(gap))
    with open(os.path.join(OUT, "s1", "subagents", "agent-sub2.meta.json"), "w") as f:
        json.dump({"agentType": "counter", "description": "Count words", "toolUseId": "toolu_sub2"}, f)
    write(os.path.join(OUT, "s2.jsonl"), s2(at(gap + 15, 700)))
    write(os.path.join(OUT, "s3.jsonl"), s3())
    write(os.path.join(OUT, "e1.jsonl"), e1())
    odd = os.path.join(OUT, "..", "odd")
    shutil.rmtree(odd, ignore_errors=True)
    write(os.path.join(odd, "drops.jsonl"), odd_drops())
    os.makedirs(odd, exist_ok=True)
    with open(os.path.join(odd, "deep.jsonl"), "w") as f:
        f.write("\n".join(odd_deep()) + "\n")
    print("wrote", OUT)
