#!/usr/bin/env node
/* Parity: src/metrics.c (st_build) against the draft page's own JavaScript.
 * Runs draft/structure.tpl.html's script headless (DOM stubbed, its rendering
 * tail cut) on the snapshot aigource printed, reads the values it derives
 * (prep, the per-node loop, METRIC/heat, slow, partners(), TINFO, turnBar)
 * and compares them with the C side's, node by node and turn by turn.
 *
 *   aigource --structure-dump --metrics M.json ... > DATA.json
 *   node tests/metrics_parity.js DATA.json M.json D3.js [--verbose]
 *
 * D3.js: d3 7.9.0, the version the page loads (vendor/d3-7.9.0.min.js, from
 * scripts/fetch-deps.sh). Exit 1 on any mismatch.
 *
 * Fields the page has no counterpart for, so this can't check: StNode.an and
 * StMetrics.of_an (snapshot indices), StNodeTurn.tools as pointers (their
 * text is checked), st_find, st_line_origins, st_blame_slot and
 * st_turn_touches (tests/test_metrics.c covers those). */
"use strict";
const fs = require("fs"), path = require("path"), vm = require("vm");

const args = process.argv.slice(2).filter(a => !a.startsWith("--"));
const verbose = process.argv.includes("--verbose");
if (args.length < 3) { console.error("usage: metrics_parity.js DATA.json METRICS.json D3.js [--verbose]"); process.exit(2); }
const [dataPath, metricsPath, d3Path] = args;
const tpl = fs.readFileSync(path.join(__dirname, "..", "draft", "structure.tpl.html"), "utf8");

/* The page's inline script, its data from __RAW, cut before it draws. */
const body = tpl.slice(tpl.indexOf("<script>\n") + 9, tpl.lastIndexOf("</script>"));
const cut = body.indexOf("const start = BY_ID.get(");
if (cut < 0 || !body.includes("const RAW = __DATA__;")) { console.error("metrics_parity: page script changed shape"); process.exit(2); }
const page = body.slice(0, cut).replace("const RAW = __DATA__;", "const RAW = globalThis.__RAW;");

/* Everything the C side computes, from the page's values and its pure functions (reworkPairs, trendPts, growing,
 * ageBuckets, turnRows, turnStats, barH/barW, ...): nothing here re-implements the page's logic, it only reshapes. */
const extract = String.raw`
const IDX = new Map(ROOT.descendants().map((n, i) => [n, i]));
const MODES = Object.keys(METRIC);
const KNOWN = [...ACTOR_COLOR.keys()].filter(a => a >= 0);
const relNode = p => BY_REL.has(PATHS[p]) ? IDX.get(BY_REL.get(PATHS[p])) : -1;
const rows8 = rows => rows.slice(0, 8).map(([p, k]) => [p, k, relNode(p)]);
const touches = es => es.map(([n, e]) => [IDX.get(n), e.a, e.d]);
globalThis.__OUT = {
  order: ORDER, now: NOW ?? 0, first: FIRST ?? 0, ageSpan: AGE_SPAN,
  avgTime: [0, AVG_TIME.file, AVG_TIME.def], turnAvg: TURN_AVG,
  max: ["dir", "file", "def"].map(lv => MODES.map(m => MAX[lv + m] ?? 1)),
  actors: KNOWN, barBig: BAR_BIG,
  nodes: ROOT.descendants().map(n => {
    const d = n.data, def = isDef(n), pts = trendPts(n), ab = ageBuckets(d), fk = flagKinds(d), f = d.flags;
    const blame = d.kind === "dir" ? [] : (o => [...blameOf(d.kind === "file" ? o : o.slice(d.a - 1, d.b)).values()].map(r => [r.t, r.n]))(originsOf(fileOf(n)));
    return {
      id: d.id, name: d.name, kind: d.kind, parent: n.parent ? IDX.get(n.parent) : -1, kids: (n.children || []).map(k => IDX.get(k)),
      depth: n.depth, own: own(d), value: n.value, lines: d.lines, cx: d.cx || 0, nest: def ? d.depth : 0,
      a: def ? d.a : 0, b: def ? d.b : 0, file: d.kind === "dir" ? -1 : IDX.get(fileOf(n)), lang: d.kind === "dir" ? null : fileOf(n).data.lang,
      ownerSlot: d.owner < 0 ? -1 : KNOWN.indexOf(d.owner) % ACTOR_PALETTE.length,
      corrTurns: corrTurns(d).slice(0, 2),
      turns: d.turns, edits: d.edits, add: d.add, dels: d.dels, changes: new Set(d.ch.map(c => c[0])).size,
      rework: d.rework, reworkSame: d.reworkSame, reworkPct: reworkPct(d),
      rwPairs: reworkPairs(d).slice(0, 4).map(p => [p.to, p.tc, p.k]),
      fixTurns: d.fixTurns, ctxTurns: d.ctxFirst.length, ctxAvg: d.ctxAvg, ctxReads: ctxMeans(d)[0], ctxSearches: ctxMeans(d)[1],
      ctxRows: rows8(readsOf(d.ctxFirst)),
      tok: d.tok, perTurn: perTurn(d), who: ownerRows(d), owner: d.owner, ageMedian: d.ageMedian, ages: [...ab.counts, ab.unknown],
      flags: [f.corrected, f.interrupted, f.untested, f.failing, f.blind, f.slow],
      kinds: ["corr", "test", "blind", "slow"].reduce((b, k, i) => b | (fk.includes(k) ? 1 << i : 0), 0),
      metric: MODES.map(m => METRIC[m](d)),
      heat: MODES.map(m => { S.mode = m; return heat(d); }),
      partners: partners(n).map(p => [IDX.get(p.node), p.shared, p.degree, p.cross, Math.round(100 * p.shared / d.turns)]),
      trend: pts, growing: growing(n, pts), blame,
      rows: turnRows(n).map(r => [r.t, r.cs.length, r.a, r.d, (r.untested ? 1 : 0) | (r.failing ? 2 : 0) | (r.blind ? 4 : 0), r.notes, r.tools]),
    };
  }),
  turns: TURNS.map((T, t) => {
    const st = turnStats(t), ti = TINFO[t];
    return {
      rank: ORDER.indexOf(t), cids: st.cids, files: touches(st.files), defs: touches(st.defs),
      add: ti.add, del: ti.del, kept: ti.kept, survival: st.survival, nodes: [...ti.ids].sort(), reads: rows8(st.reads), notes: st.notes,
      untested: st.untested, failing: st.failing, tested: st.tested, blind: st.blind, slow: st.slow, slowX: st.slowX,
      tok: st.tok, onChanges: st.onChanges, ledPct: st.ledPct, agents: st.agents.size, subagents: st.subagents,
      barH: barH(t), barW: barW(t),
    };
  }),
};`;

/* A DOM that takes anything and draws nothing. */
const stub = new Proxy(function () {}, {
  get: (t, k) => k === Symbol.toPrimitive ? () => 0 : k === Symbol.iterator ? function* () {} :
                 k === "forEach" ? () => {} : k === "length" ? 0 : stub,
  apply: () => stub, construct: () => stub, set: () => true,
});
const ctx = vm.createContext({
  document: stub, hljs: stub, CSS: { escape: s => s }, innerWidth: 1600, console,
  addEventListener() {}, matchMedia: () => ({ matches: true }), ResizeObserver: class { observe() {} },
  __RAW: JSON.parse(fs.readFileSync(dataPath, "utf8")),
});
vm.runInContext(fs.readFileSync(d3Path, "utf8"), ctx, { filename: "d3.js" });
vm.runInContext(page + "\n" + extract, ctx, { filename: "structure.tpl.html" });
const js = ctx.__OUT, c = JSON.parse(fs.readFileSync(metricsPath, "utf8"));

/* Compare: per field, how many values and how many differ. */
const stats = new Map(), examples = [];
const close = (a, b) => typeof a === "number" && typeof b === "number" ?
  a === b || Math.abs(a - b) <= 1e-9 * Math.max(1, Math.abs(a), Math.abs(b)) : false;
function same(a, b) {
  if (close(a, b)) return true;
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, i) => same(x, b[i]));
  return a === b;
}
function cmp(field, where, a, b) {
  const s = stats.get(field) || { n: 0, bad: 0 };
  s.n++;
  if (!same(a, b)) {
    s.bad++;
    if (examples.length < 40 || verbose) examples.push(`${field} @ ${where}: c=${JSON.stringify(a)} js=${JSON.stringify(b)}`);
  }
  stats.set(field, s);
}
for (const k of ["order", "now", "first", "ageSpan", "avgTime", "turnAvg", "max", "actors", "barBig"]) cmp(k, "global", c[k], js[k]);
cmp("nodes.count", "global", c.nodes.length, js.nodes.length);
const skip = new Set(["an"]);
c.nodes.forEach((n, i) => {
  const j = js.nodes[i];
  if (!j) return;
  for (const k of Object.keys(j)) if (!skip.has(k)) cmp("node." + k, n.id, n[k], j[k]);
});
cmp("turns.count", "global", c.turns.length, js.turns.length);
c.turns.forEach((t, i) => {
  const j = js.turns[i];
  if (!j) return;
  for (const k of Object.keys(j)) {
    if (k === "nodes") cmp("turn.nodes", "turn " + i, [...new Set(t.nodes.map(x => c.nodes[x].id))].sort(), j.nodes);
    else cmp("turn." + k, "turn " + i, t[k], j[k]);
  }
});

let bad = 0;
for (const [k, s] of stats) {
  bad += s.bad;
  console.log(`${s.bad ? "DIFF" : "ok  "} ${k.padEnd(18)} ${s.n} compared, ${s.bad} differ`);
}
for (const e of examples) console.log("  " + e);
console.log(bad ? `metrics parity: ${bad} mismatches` : `metrics parity: all ${[...stats.values()].reduce((a, s) => a + s.n, 0)} values match`);
process.exit(bad ? 1 : 0);
