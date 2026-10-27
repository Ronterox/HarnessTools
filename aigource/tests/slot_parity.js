#!/usr/bin/env node
/* Parity: a turn's blame colour in the draft page (turnSlot, from its ISO time
 * and title) against the app's (st_blame_slot), on the fixed table of turns
 * tests/test_metrics.c checks and writes: ASCII, UTF-8, U+FFFD, a byte that
 * isn't UTF-8 (U+FFFD once in JSON), an empty title, microseconds.
 *
 *   test_metrics DIR && node tests/slot_parity.js DIR/slots.json
 *
 * Exit 1 on any mismatch. */
"use strict";
const fs = require("fs"), path = require("path"), vm = require("vm");

if (process.argv.length < 3) { console.error("usage: slot_parity.js SLOTS.json"); process.exit(2); }
const table = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const tpl = fs.readFileSync(path.join(__dirname, "..", "draft", "structure.tpl.html"), "utf8");
/* The page's own functions: the palette through turnSlot. */
const from = tpl.indexOf("const BLAME_COLORS"), to = tpl.indexOf("/* Who wrote these lines");
if (from < 0 || to < from || !tpl.slice(from, to).includes("function turnSlot")) {
    console.error("slot_parity: page script changed shape");
    process.exit(2);
}
const ctx = vm.createContext({ TextEncoder });
vm.runInContext(tpl.slice(from, to) + "\nglobalThis.turnSlot = turnSlot;", ctx);
let bad = 0;
for (const t of table) {
    const js = ctx.turnSlot({ ts: t.ts, title: t.title });
    if (js !== t.slot) {
        bad++;
        console.log(`slot parity: ${t.ts} ${JSON.stringify(t.title)}: page ${js}, app ${t.slot}`);
    }
}
console.log(`slot parity: ${table.length - bad}/${table.length} turns agree`);
process.exit(bad ? 1 : 0);
