"""Context and cost per session (compile, ask, worker) in usage.json, plus the
account quota read from the OMC HUD cache when present (never fetched).

"How full is the context" is the prompt size of a session's latest model
call: input + cache read + cache write of the last assistant message in that
session's transcript. The totals in a `claude -p` result are summed over every
call of the run, so they are only used for cost.
"""
import json
import os

from . import paths

MAX_CALLS = 30
OMC_CACHE = os.path.expanduser("~/.claude/plugins/oh-my-claudecode/.usage-cache-anthropic.json")


def ctx_tokens(u):
    if not isinstance(u, dict):
        return None
    return sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                             "cache_read_input_tokens"))


def last_assistant_usage(transcript, tail_bytes=400000):
    """(model, usage) of the newest assistant message in a transcript, or (None, None)."""
    try:
        with open(transcript, "rb") as f:
            f.seek(max(0, os.path.getsize(transcript) - tail_bytes))
            lines = f.read().splitlines()
    except OSError:
        return None, None
    for raw in reversed(lines):
        if b'"compact_boundary"' in raw:  # compacted after the last reply: use its size
            try:
                meta = json.loads(raw).get("compactMetadata") or {}
            except ValueError:
                meta = {}
            if isinstance(meta.get("postTokens"), int):
                return None, {"input_tokens": meta["postTokens"]}
        if b'"assistant"' not in raw or b'"usage"' not in raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        m = d.get("message") if isinstance(d, dict) else None
        if isinstance(m, dict) and isinstance(m.get("usage"), dict) and not d.get("isSidechain"):
            return m.get("model"), m["usage"]
    return None, None


def _path(sdir):
    return os.path.join(sdir, "usage.json")


def modify(sdir, fn):
    p = _path(sdir)
    with paths.locked(p):
        data = paths.read_json(p, {}) or {}
        fn(data)
        paths.write_json(p, data)
        return data


def record_call(root, sdir, key, out, note):
    """After a compiler call: context from its transcript, cost from the result."""
    sid = out.get("session_id")
    model, u = last_assistant_usage(os.path.join(paths.claude_project_dir(root), "%s.jsonl" % sid))
    mu = out.get("modelUsage") if isinstance(out.get("modelUsage"), dict) else {}
    windows = {m: v.get("contextWindow") for m, v in mu.items()
               if isinstance(v, dict) and isinstance(v.get("contextWindow"), int)}
    model = model or (max(mu, key=lambda m: (mu[m] or {}).get("costUSD") or 0) if mu else None)
    total = out.get("usage") if isinstance(out.get("usage"), dict) else {}
    cost = float(out.get("total_cost_usd") or 0)

    def upd(d):
        d.setdefault("windows", {}).update(windows)
        e = d.get(key) if isinstance(d.get(key), dict) else {}
        if e.get("session_id") != sid:  # a new session (first call or after compact/reset)
            e = {"compacted_from": e.get("compacted_from"), "calls": e.get("calls", [])}
        e.update(session_id=sid, model=model, updated=paths.now(),
                 turns=int(e.get("turns") or 0) + 1,
                 cost_usd=round(float(e.get("cost_usd") or 0) + cost, 4))
        if u:
            e["ctx_tokens"] = ctx_tokens(u)
        e["ctx_window"] = d["windows"].get(model) or e.get("ctx_window")
        e["calls"] = (e.get("calls") or [])[-(MAX_CALLS - 1):] + [{
            "ts": paths.now(), "note": note, "cost": round(cost, 4),
            "in": int(total.get("input_tokens") or 0),
            "cache_read": int(total.get("cache_read_input_tokens") or 0),
            "cache_write": int(total.get("cache_creation_input_tokens") or 0),
            "out": int(total.get("output_tokens") or 0)}]
        d[key] = e
    return modify(sdir, upd)


def restart(sdir, key, old_sid):
    """A compact/reset: the next call opens a fresh session."""
    def upd(d):
        e = d.get(key) if isinstance(d.get(key), dict) else {}
        d[key] = {"session_id": None, "model": e.get("model"), "ctx_tokens": 0,
                  "ctx_window": e.get("ctx_window"), "turns": 0, "cost_usd": 0.0,
                  "compacted_from": old_sid, "calls": e.get("calls", []), "updated": paths.now()}
    modify(sdir, upd)


def record_worker(sdir, model, u, turn):
    tokens = ctx_tokens(u)

    def upd(d):
        old = d.get("worker") or {}
        m = model or old.get("model")
        if (old.get("ctx_tokens"), old.get("model"), old.get("turn")) == (tokens, m, turn) \
                and not old.get("backfilled"):
            return
        d["worker"] = {"model": m, "ctx_tokens": tokens, "turn": turn, "updated": paths.now(),
                       "ctx_window": (d.get("windows") or {}).get(m)}
    modify(sdir, upd)


def quota():
    """Account quota from the OMC HUD cache, or None. Never makes a request."""
    d = paths.read_json(OMC_CACHE)
    if not isinstance(d, dict) or not isinstance(d.get("data"), dict) or d.get("error"):
        return None
    q = d["data"]
    ts = d.get("lastSuccessAt") or d.get("timestamp")
    return {
        "five_hour": q.get("fiveHourPercent"), "five_hour_resets_at": q.get("fiveHourResetsAt"),
        "weekly": q.get("weeklyPercent"), "weekly_resets_at": q.get("weeklyResetsAt"),
        "buckets": [{"id": b.get("id"), "label": b.get("label"), "percent": b.get("percent"),
                     "resets_at": b.get("resetsAt"), "active": b.get("isActive")}
                    for b in q.get("scopedWeeklyBuckets") or [] if isinstance(b, dict)],
        "age_s": int(paths.now() - ts / 1000.0) if isinstance(ts, (int, float)) else None,
    }


def report(root, sid):
    """usage.json, with sessions that predate it filled in from their
    transcripts (context size and model, no turn/cost history) and saved, so
    editors reading usage.json see them too."""
    sdir = paths.session_dir(root, sid)
    sessions = paths.read_json(_path(sdir), {}) or {}
    windows = sessions.get("windows") or {}
    st = paths.read_json(os.path.join(sdir, "state.json"), {}) or {}
    tdir = paths.claude_project_dir(root)
    todo = [(k, st.get(k)) for k in ("compile", "ask") if st.get(k) and not isinstance(sessions.get(k), dict)]
    if not isinstance(sessions.get("worker"), dict):
        todo.append(("worker", sid))
    filled = {}
    for key, s_id in todo:
        model, u = last_assistant_usage(os.path.join(tdir, "%s.jsonl" % s_id))
        if u:
            filled[key] = {"session_id": s_id, "model": model, "ctx_tokens": ctx_tokens(u),
                           "ctx_window": windows.get(model), "backfilled": True, "updated": paths.now()}
    if filled:
        def upd(d):
            for k, e in filled.items():
                if not isinstance(d.get(k), dict):  # never overwrite a real record
                    d[k] = e
        sessions = modify(sdir, upd)
    return {"sessions": sessions, "quota": quota()}


def _k(n):
    return "%dk" % round((n or 0) / 1000.0) if (n or 0) >= 1000 else str(n or 0)


def text(rep):
    lines = []
    for key in ("compile", "ask", "worker"):
        e = rep["sessions"].get(key)
        if not isinstance(e, dict):
            lines.append("%-8s (no session yet)" % key)
            continue
        win = e.get("ctx_window")
        ctx = "%s/%s (%d%%)" % (_k(e.get("ctx_tokens")), _k(win), 100 * (e.get("ctx_tokens") or 0) // win) \
            if win else _k(e.get("ctx_tokens"))
        if e.get("backfilled"):
            extra = "(from transcript)"
        elif key == "worker":
            extra = "turn %s" % e.get("turn")
        else:
            extra = "%s calls  $%.2f" % (e.get("turns", 0), e.get("cost_usd") or 0)
        lines.append("%-8s %-20s ctx %-18s %s" % (key, e.get("model") or "?", ctx, extra))
    q = rep.get("quota")
    if q:
        lines.append("quota    5h %s%%  weekly %s%%%s" % (
            q.get("five_hour"), q.get("weekly"),
            "".join("  %s %s%%" % (b["label"], b["percent"]) for b in q["buckets"])))
    return "\n".join(lines)
