// A minimal Chrome DevTools Protocol driver for the page tests: node cdp_driver.js WS_URL (a page target's
// webSocketDebuggerUrl), then one JSON command per stdin line, one JSON reply per stdout line:
//   {"cmd": "goto", "url"}                    navigate and wait for the load event
//   {"cmd": "eval", "expr"}                   -> {"value"} (awaits promises; the value must be JSON)
//   {"cmd": "wait", "expr", "timeout"}        poll expr until truthy -> {"value"} or {"error": "timeout"}
//   {"cmd": "shot", "path"}                   PNG screenshot of the viewport
//   {"cmd": "heap"}                           -> {"used", "total"} bytes after a forced GC
//   {"cmd": "errors"}                         -> {"value": [uncaught exceptions and console errors so far]}
//   {"cmd": "media", "features"}              emulate CSS media features, e.g. [{"name": "prefers-reduced-motion", "value": "reduce"}]
const readline = require('readline'), fs = require('fs');
const ws = new WebSocket(process.argv[2]);
let id = 0;
const pending = new Map(), errors = [], loaded = [];
ws.onmessage = ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result); return; }
  if (m.method === 'Runtime.exceptionThrown') errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
  if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') errors.push(m.params.args.map(a => a.value ?? a.description).join(' '));
  if (m.method === 'Page.loadEventFired') loaded.splice(0).forEach(f => f());
};
const send = (method, params = {}) => new Promise((resolve, reject) => { const i = ++id; pending.set(i, { resolve, reject }); ws.send(JSON.stringify({ id: i, method, params })); });
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function evaluate(expr) {
  const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result.value;
}
const handlers = {
  async goto(c) { const done = new Promise(r => loaded.push(r)); await send('Page.navigate', { url: c.url }); await done; return {}; },
  async eval(c) { return { value: await evaluate(c.expr) }; },
  async wait(c) {
    const end = Date.now() + (c.timeout || 10000);
    while (Date.now() < end) { try { const v = await evaluate(c.expr); if (v) return { value: v }; } catch (e) { /* not defined yet */ } await sleep(50); }
    return { error: 'timeout: ' + c.expr };
  },
  async shot(c) { const r = await send('Page.captureScreenshot', { format: 'png' }); fs.writeFileSync(c.path, Buffer.from(r.data, 'base64')); return {}; },
  async heap() { await send('HeapProfiler.collectGarbage'); await sleep(200); await send('HeapProfiler.collectGarbage'); const r = await send('Runtime.getHeapUsage'); return { used: r.usedSize, total: r.totalSize }; },
  async errors() { return { value: errors.splice(0) }; },
  async media(c) { await send('Emulation.setEmulatedMedia', { features: c.features || [] }); return {}; },
};
ws.onopen = async () => {
  await send('Page.enable'); await send('Runtime.enable'); await send('HeapProfiler.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: 1400, height: 900, deviceScaleFactor: 1, mobile: false });
  process.stdout.write(JSON.stringify({ ready: true }) + '\n');
  const rl = readline.createInterface({ input: process.stdin });
  for await (const line of rl) {
    const c = JSON.parse(line);
    let out;
    try { out = await handlers[c.cmd](c); } catch (e) { out = { error: String(e.message || e) }; }
    process.stdout.write(JSON.stringify(out) + '\n');
  }
  ws.close(); process.exit(0);
};
ws.onerror = e => { process.stderr.write('cdp: ' + (e.message || 'websocket error') + '\n'); process.exit(2); };
