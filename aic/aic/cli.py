"""aic command line. See PROTOCOL.md for the contract editors rely on."""
import argparse
import json
import os
import sys

from . import anchors, compiler, compiler_prompts, docs, fixes, hook, paths, prompts, usage, watch, worker

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "bin", "aic")


def _root():
    root = paths.find_root()
    if not root:
        raise worker.WorkerError("no .aic/ here or above; run `aic init` in the project root")
    return root


def _sid(root):
    sid = paths.attached(root)
    if not sid:
        raise worker.WorkerError("no worker session found for %s; start `claude` there" % root)
    os.makedirs(paths.session_dir(root, sid), exist_ok=True)
    return sid


def main(argv=None):
    ap = argparse.ArgumentParser(prog="aic", description="AI compiler: review a coding AI while it works")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create .aic/ and install worker hooks")
    p = sub.add_parser("sessions", help="list worker sessions")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("attach", help="pin a worker session (or --auto)")
    p.add_argument("sid", nargs="?")
    p.add_argument("--auto", action="store_true")
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    sub.add_parser("watch", help="tail worker transcript into the trace")
    p = sub.add_parser("docs", help="list docs (migrates the old spec.md once)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("compile", help="compile a doc against worker activity")
    p.add_argument("--doc", default=docs.DEFAULT)
    p.add_argument("--print", dest="dry", action="store_true", help="print the prompt; ask nothing")
    p = sub.add_parser("ask", help="ask the compiler")
    p.add_argument("--doc", default=docs.DEFAULT)
    p.add_argument("--template", help="a `task: ask` compiler template (with --whatif: `task: whatif`)")
    p.add_argument("--print", dest="dry", action="store_true", help="print the prompt; ask nothing")
    p.add_argument("--context-file")
    p.add_argument("--whatif", action="store_true")
    p.add_argument("question", nargs="+")
    p = sub.add_parser("fix", help="ask the compiler for edits to a doc that fix its diagnostics (never applied here)")
    p.add_argument("--doc", default=docs.DEFAULT)
    p.add_argument("--from", dest="line_from", type=int)
    p.add_argument("--to", dest="line_to", type=int)
    p.add_argument("--content-file", help="the doc's text as the editor has it (default: the saved doc)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--print", dest="dry", action="store_true", help="print the prompt; ask nothing")
    p = sub.add_parser("prompts", help="list prompt templates (--compiler: the compiler's)")
    p.add_argument("--compiler", action="store_true")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("render", help="render a prompt template (what a submit would send)")
    p.add_argument("--prompt", required=True)
    p.add_argument("--doc", default=docs.DEFAULT)
    p.add_argument("--from", dest="line_from", type=int)
    p.add_argument("--to", dest="line_to", type=int)
    p.add_argument("--input", dest="input_text")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("submit", help="send a doc (through a prompt template) to the worker")
    p.add_argument("--doc", default=docs.DEFAULT)
    p.add_argument("--prompt", help="built-in template, sent without a preview (default: selection with --from, "
                                    "else spec); project/user templates go through render + --rendered")
    p.add_argument("--input", dest="input_text")
    p.add_argument("--rendered", help="JSON file from `aic render --json`, header/body possibly edited")
    p.add_argument("--from", dest="line_from", type=int)
    p.add_argument("--to", dest="line_to", type=int)
    p.add_argument("--now", action="store_true", help="interrupt the running tool (Claude Code send-now)")
    sub.add_parser("hold", help="stop the worker at its next tool call")
    p = sub.add_parser("continue", help="release a hold and resume the worker")
    p.add_argument("message", nargs="*")
    p = sub.add_parser("usage", help="context/cost per session and account quota")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("compact", help="summarize-and-restart a compiler session (or --reset)")
    p.add_argument("which", choices=["compile", "ask", "worker"])
    p.add_argument("--reset", action="store_true")
    p = sub.add_parser("anchor", help="resolve @anchors in TEXT")
    p.add_argument("--json", action="store_true")
    p.add_argument("text", nargs="+")
    sub.add_parser("hook", help="(internal) Claude Code hook entry point")
    a = ap.parse_args(argv)

    if a.cmd == "hook":
        return hook.main()
    try:
        if a.cmd == "init":  # (WorkerError on a broken settings file is reported below)
            root = paths.init_root()
            sp = worker.init(root, BIN)
            docs.migrate(root, paths.attached(root))
            print("aic initialized in %s/.aic (hooks: %s). Restart running claude sessions there "
                  "so they load the hooks." % (root, os.path.relpath(sp, root)))
            return 0
        root = _root()
        if a.cmd == "sessions":
            ss = worker.sessions(root)
            if a.json:
                print(json.dumps(ss))
            else:
                for s in ss:
                    print("%s %s %-5s %s" % ("*" if s["attached"] else " ", s["session_id"],
                                             s["state"] or "-", s["title"]))
        elif a.cmd == "attach":
            print("attached:", worker.attach(root, None if a.auto else a.sid))
        elif a.cmd == "status":
            st = worker.status(root)
            print(json.dumps(st) if a.json else "\n".join("%s: %s" % kv for kv in st.items()))
        elif a.cmd == "usage":
            rep = usage.report(root, _sid(root))
            print(json.dumps(rep) if a.json else usage.text(rep))
        elif a.cmd == "compact":
            print(compiler.compact(root, _sid(root), a.which, a.reset))
        elif a.cmd == "anchor":
            text = " ".join(a.text)
            if a.json:
                sid = paths.attached(root)
                print(json.dumps([anchors.resolve(x, root, sid) for x in anchors.parse(text, root)]))
            else:
                print(anchors.blocks(text, root, paths.attached(root)) or "(no anchors)")
        elif a.cmd == "docs":
            docs.migrate(root, paths.attached(root))
            ds = docs.listing(root)
            if a.json:
                print(json.dumps(ds))
            else:
                for d in ds:
                    print("%-20s %s" % (d["name"], "compile" if d["compile"] else ""))
        elif a.cmd == "watch":
            return watch.main(root)
        elif a.cmd == "compile":
            sid = _sid(root)
            docs.resolve(root, sid, a.doc)
            if a.dry:
                c = compiler.compile_prompt(root, sid, a.doc)
                print("(model %s)\n%s" % (c["model"], c["prompt"]))
                return 0
            d = compiler.compile_spec(root, sid, a.doc)
            if d is None:
                print("compile queued: %s runs again, as saved, when the current compile finishes" % a.doc)
                return 0
            counts = {}
            for it in d["items"]:
                counts[it["severity"]] = counts.get(it["severity"], 0) + 1
            print("compiled %s: %d errors, %d warnings, %d info — %s" % (
                a.doc, counts.get("error", 0), counts.get("warning", 0), counts.get("info", 0), d["summary"]))
        elif a.cmd == "ask":
            sid = _sid(root)
            ctx = paths.read_json(a.context_file) if a.context_file else None
            docs.check_name(a.doc)  # a question needs no doc file: it may not exist yet
            docs.migrate(root, sid)
            if a.dry:
                q = compiler.ask_prompt(root, sid, " ".join(a.question), ctx, a.whatif, a.doc, a.template)
                print("(model %s)\n%s" % (q["model"], q["prompt"]))
                return 0
            print(compiler.ask(root, sid, " ".join(a.question), ctx, a.whatif, a.doc, a.template))
        elif a.cmd == "fix":
            sid = _sid(root)
            docs.resolve(root, sid, a.doc)
            content = None
            if a.content_file:
                try:
                    with open(a.content_file, encoding="utf-8") as f:
                        content = f.read()
                except (OSError, UnicodeError) as e:
                    raise docs.DocError("can't read %s: %s" % (a.content_file, e))
            r = fixes.propose(root, sid, a.doc, content, a.line_from, a.line_to, a.dry)
            if a.dry:
                print(json.dumps(r) if a.json else "(model %s)\n%s" % (r["model"], r["prompt"]))
                return 0
            print(json.dumps(r) if a.json else fixes.describe(r))
        elif a.cmd == "submit":
            sid = _sid(root)
            if a.rendered:
                r = paths.read_json(a.rendered)
                if not isinstance(r, dict) or not isinstance(r.get("receipt"), dict):
                    raise worker.WorkerError("%s is not a rendered prompt (aic render --json)" % a.rendered)
                print(worker.send_rendered(root, sid, r.get("header") or "", r.get("body") or "", r["receipt"], a.now))
            else:
                docs.resolve(root, sid, a.doc)
                print(worker.submit(root, sid, a.line_from, a.line_to, a.now, a.doc, a.prompt, a.input_text))
        elif a.cmd == "prompts":
            ps = compiler_prompts.listing(root) if a.compiler else prompts.listing(root)
            if a.json:
                print(json.dumps(ps))
            else:
                for t in ps:
                    print("%-14s %-9s %-8s %s" % (t["name"], t["source"], t.get("task", ""),
                                                  t.get("error") or t.get("description", "")))
        elif a.cmd == "render":
            sid = _sid(root)
            docs.resolve(root, sid, a.doc)
            r = prompts.render(root, sid, a.prompt, a.doc, a.line_from, a.line_to, a.input_text)
            print(json.dumps(r) if a.json else "%s\n----\n%s" % (r["header"], r["body"]))
        elif a.cmd == "hold":
            print(worker.hold(root, _sid(root)))
        elif a.cmd == "continue":
            print(worker.cont(root, _sid(root), " ".join(a.message) or None))
    except (worker.WorkerError, compiler.CompilerError, docs.DocError, prompts.PromptError) as e:
        sys.stderr.write("aic: %s\n" % e)
        return 1
    return 0
