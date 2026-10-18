-- Headless smoke test. Run from aic/nvim:
--   nvim --headless -u NONE -l tests/smoke.lua
local plugin_dir = vim.fn.fnamemodify(debug.getinfo(1, 'S').source:sub(2), ':p:h:h')
vim.opt.rtp:prepend(plugin_dir)
vim.o.swapfile = false
vim.o.shadafile = 'NONE'
vim.cmd.runtime('plugin/aic.lua')

local failures = 0
local function ok(cond, msg)
  if cond then
    io.stdout:write('ok   - ' .. msg .. '\n')
  else
    failures = failures + 1
    io.stdout:write('FAIL - ' .. msg .. '\n')
  end
end
local function wait(ms, fn)
  return vim.wait(ms, fn, 20)
end
local function write(path, text)
  vim.fn.mkdir(vim.fn.fnamemodify(path, ':h'), 'p')
  local f = assert(io.open(path, 'wb'))
  f:write(text)
  f:close()
end
local function read(path)
  local f = io.open(path, 'rb')
  if not f then
    return nil
  end
  local s = f:read('*a')
  f:close()
  return s
end
local function jl(t)
  return vim.json.encode(t) .. '\n'
end
local function count(s, pat)
  return select(2, (s or ''):gsub(pat, ''))
end

-- silence notifications but keep them for assertions
local notes = {}
vim.notify = function(msg)
  table.insert(notes, msg)
end

---------------------------------------------------------------------------
-- fake project
local root = vim.fn.resolve(vim.fn.tempname())
local sid = 'S1'
local dir = root .. '/.aic/sessions/' .. sid
local log = root .. '/aic.log'
vim.fn.mkdir(dir, 'p')

local app = {}
for i = 1, 30 do
  app[i] = 'line ' .. i
end
write(root .. '/src/app.js', table.concat(app, '\n') .. '\n')

local now = os.time()
write(
  dir .. '/trace.jsonl',
  jl({ id = 'e1', seq = 1, ts = now, kind = 'prompt', turn = 1, text = 'Add a greeting\nmore detail' })
    .. jl({
      id = 'e2',
      seq = 2,
      ts = now + 1,
      kind = 'tool',
      turn = 1,
      tool = 'Edit',
      title = 'Edit src/app.js:3-5',
      why = 'Insert the greeting helper',
      file = 'src/app.js',
      hunks = { { start = 3, count = 3, lines = { '-line 3', '+line 3', '+hello', '+line 5' } } },
      input = 'src/app.js',
      error = false,
    })
    .. jl({
      id = 'e3',
      seq = 3,
      ts = now + 2,
      kind = 'human',
      turn = 1,
      file = 'src/app.js',
      hunks = { { start = 10, count = 1, lines = { '-old', '+line 10' } } },
    })
    .. jl({ id = 'e4', seq = 4, ts = now + 3, kind = 'text', turn = 1, text = 'Done.' })
)
write(
  dir .. '/touched.json',
  vim.json.encode({
    ['src/app.js'] = {
      { start = 3, ['end'] = 5, event = 'e2', actor = 'worker' },
      { start = 10, ['end'] = 10, event = 'e3', actor = 'human' },
    },
  })
)
write(dir .. '/diagnostics.spec.json', vim.json.encode({ compiled_at = 1, upto_seq = 1, summary = 'old', items = {} }))
local spec_path = root .. '/.aic/docs/spec.md'
write(spec_path, '<!-- aic: compile -->\n# Spec\n\n- greet users @src/app.js:4\n- helper @src/app.js:hello\n')
write(dir .. '/seen.json', vim.json.encode({ seen = {} }))
write(dir .. '/chat.md', '## You\nhi\n\n## Compiler\nhello\n')

-- usage.json: compile 41%, ask 63%, worker with unknown window; interleaved calls
local function usage_json(ask_tokens)
  local cc, ac = {}, {}
  for i = 1, 9 do
    table.insert(cc, { ts = now - 2 * i * 60, ['in'] = 2, cache_read = 30000 + i, cache_write = 4000, out = 900, cost = 0.08, note = 'compile #' .. i })
    table.insert(ac, { ts = now - (2 * i - 1) * 60, ['in'] = 3, cache_read = 50000, cache_write = 1500, out = 1200, cost = 0.1, note = 'ask #' .. i })
  end
  return vim.json.encode({
    compile = { session_id = 'C1', model = 'claude-sonnet-5-5', ctx_tokens = 82000, ctx_window = 200000, turns = 12,
      cost_usd = 1.2, updated = now, compacted_from = 'C0abcdefgh', calls = cc },
    ask = { session_id = 'A1', model = 'claude-opus-5-5', ctx_tokens = ask_tokens or 126000, ctx_window = 200000, turns = 9,
      cost_usd = 2.85, updated = now, calls = ac },
    worker = { model = 'claude-sonnet-5-5', ctx_tokens = 106701, ctx_window = vim.NIL, turn = 21, updated = now },
    windows = { ['claude-sonnet-5-5'] = 200000 },
  })
end
write(dir .. '/usage.json', usage_json())
local function iso(t)
  return os.date('!%Y-%m-%dT%H:%M:%SZ', t)
end
local quota_json = vim.json.encode({ sessions = {}, quota = {
  five_hour = 57, five_hour_resets_at = iso(now + 3 * 3600 + 30 * 60 + 40),
  weekly = 15, weekly_resets_at = iso(now + 6 * 86400 + 7 * 3600 + 30 * 60),
  buckets = { { id = 'fable', label = 'Fable', percent = 0, resets_at = iso(now + 86400), isActive = false } },
  age_s = 30,
} })
write(root .. '/quota.json', quota_json)

local bin = root .. '/fake-aic'
write(
  bin,
  ([[#!/bin/sh
echo "$*" >> '%s'
case "$1" in
  status) printf '{"root":"%s","session_id":"%s","dir":".aic/sessions/%s","worker":{"session_id":"%s","state":"busy"},"hold":false,"compiling":false,"watching":true}\n' ;;
  sessions) printf '[{"session_id":"%s","updated":%d,"state":"busy","title":"greeting","pane":null,"attached":true}]\n' ;;
  watch) cat >/dev/null ;;
  prompts) cat <<'EOT'
[{"name":"review","source":"built-in","path":"/x/review.md","description":"Review only","header":"Review:","requires":[],"input":null,"uses_input":false,"marks_sent":false},
 {"name":"spec","source":"built-in","path":"/x/spec.md","description":"The doc","header":"Here:","requires":["changed"],"input":null,"uses_input":false,"marks_sent":true},
 {"name":"message","source":"user","path":"/x/message.md","description":"A message","header":"Read:","requires":[],"input":"Message","uses_input":true,"marks_sent":false},
 {"name":"bad","source":"project","path":"/x/bad.md","error":"template bad: unknown key"}]
EOT
;;
  render) case "$*" in
    *refuse*) echo "aic: nothing to send" >&2; exit 1 ;;
  esac
  printf '{"header":"[aic] Rendered by %%s","body":"body one\\nbody two","receipt":{"prompt":"%%s","doc":"%%s","marks_sent":false}}\n' "$4" "$4" "$6" ;;
  submit) if [ "$2" = --rendered ]; then cp "$3" rendered.json; fi; echo "submitted to %%9: sent" ;;
  fix) prev=; for a in "$@"; do if [ "$prev" = --content-file ]; then cp "$a" fixcontent.md; fi; prev=$a; done
    if [ -f fixfail ]; then echo "aic: no diagnostics to fix in notes (compile it first)" >&2; exit 1; fi
    cat fixes.json ;;
  docs) printf '[{"name":"spec","path":".aic/docs/spec.md","mtime":%d,"compile":true,"prompt":null}]\n' ;;
  ask) case "$*" in
    *fence*) cat <<'EOT'
Here:
```lua
step one
step two
```
see @server.js:3
EOT
;;
    *) echo "**answer**" ;;
  esac ;;
  compile) if [ -f queue ]; then echo "compile queued: it runs again on your latest spec when the current one finishes"; else echo "ok compile"; fi ;;
  usage) cat quota.json ;;
  compact) if [ "$2" = worker ] && [ -f refuse ]; then echo "dialog open" >&2; exit 1; fi
    echo "compacted $2 $3" ;;
  anchor) cat <<'EOT'
[{"anchor":"@log","kind":"log","path":null,"abs":null,"line":null,"end":null,"symbol":null,"exists":true,"text":"commit abc123\nAuthor: t"}]
EOT
;;
  *) echo "ok $1" ;;
esac
]]):format(log, root, sid, sid, sid, sid, now, now)
)
vim.fn.setfperm(bin, 'rwxr-xr-x')

-- anchors: a symbol target, and a git repo (one commit) for @diff
write(root .. '/server.js', '// handle is called below\nconst x = 1\nfunction handle(req) {\n  return 1\n}\n')
local function git(...)
  return vim.system({ 'git', '-c', 'user.name=t', '-c', 'user.email=t@t', ... }, { cwd = root, text = true }):wait()
end
git('init', '-q')
git('add', '-A')
git('commit', '-qm', 'init')

---------------------------------------------------------------------------
vim.cmd.cd(root)
local aic = require('aic')
aic.setup({ bin = bin })
local state = require('aic.state')
local trace = require('aic.trace')
local view = require('aic.view')

-- One tab, one (non-floating) window, always: checked after every step below
-- that switches views or jumps.
local function solid_wins()
  return vim.tbl_filter(function(w)
    return vim.api.nvim_win_get_config(w).relative == ''
  end, vim.api.nvim_tabpage_list_wins(0))
end
local function single(what)
  ok(#vim.api.nvim_list_tabpages() == 1 and #solid_wins() == 1,
    what .. ': one tab, one window (' .. #vim.api.nvim_list_tabpages() .. ' tabs, ' .. #solid_wins() .. ' windows)')
end
-- the code window's options before any aic view was shown in it
vim.cmd.edit(vim.fn.fnameescape(root .. '/src/app.js'))
local code0 = vim.api.nvim_get_current_buf()
local function opts_now()
  return { vim.wo.foldmethod, vim.wo.wrap, vim.wo.number, vim.wo.winbar, vim.wo.signcolumn, vim.wo.linebreak }
end
local wo0 = opts_now()
-- A buffer never shown in this window gets the window's own (global) values:
-- the only way to see a view's options leak (a buffer shown before gets its
-- saved values back).
local function fresh_opts()
  vim.cmd('enew')
  local o = opts_now()
  vim.cmd('bwipeout')
  return o
end

-- open: history view in the current window
aic.open()
ok(wait(3000, function()
  return state.dir ~= nil and vim.api.nvim_get_current_buf() == trace.buf
end), 'history shown in the current window and session resolved')
ok(state.dir == dir, 'session dir resolved relative to root: ' .. tostring(state.dir))
single(':Aic')
ok(vim.wo.foldmethod == 'expr' and not vim.wo.winfixbuf, 'trace options set, window not pinned')
ok(wait(2000, function()
  return (read(log) or ''):find('watch', 1, true) ~= nil
end), '`aic watch` started')

-- trace render
local tl = vim.api.nvim_buf_get_lines(trace.buf, 0, -1, false)
local text = table.concat(tl, '\n')
ok(tl[1]:find('^▸ Turn 1  Add a greeting') ~= nil, 'turn header: ' .. tl[1])
ok(text:find('  ● Edit src/app.js:3-5 — Insert the greeting helper', 1, true) ~= nil, 'tool line rendered')
ok(text:find('  ✎ human src/app.js:10-10', 1, true) ~= nil, 'human line rendered')
ok(text:find('  ● ◆ Done.', 1, true) ~= nil, 'text line rendered')
ok(text:find('    +hello', 1, true) ~= nil, 'hunk lines rendered')
ok(vim.bo[trace.buf].filetype == 'aictrace', 'filetype aictrace')

-- ]u navigation + mark seen
vim.api.nvim_win_set_cursor(0, { 1, 0 })
local l = trace.next_unseen(1)
ok(l and vim.api.nvim_get_current_line():find('Edit src/app.js', 1, true) ~= nil, ']u moves to unseen tool node')
ok(vim.fn.foldlevel(1) == 1 and vim.fn.foldlevel(l) == 2 and vim.fn.foldlevel(l + 1) == 2, 'fold levels: turn 1, event with details 2')
ok(vim.fn.foldclosed(1) == -1, 'turn fold open')
trace.toggle_seen()
local seen = vim.json.decode(read(dir .. '/seen.json'))
ok(vim.tbl_contains(seen.seen, 'e2'), 'seen persisted to seen.json')
ok(vim.api.nvim_get_current_line():find('○ Edit', 1, true) ~= nil, 'node shows seen glyph')
local l2 = trace.next_unseen(1)
ok(l2 and vim.api.nvim_get_current_line():find('◆ Done.', 1, true) ~= nil, ']u moves to next unseen (text)')

-- status string
require('aic.status').update()
-- fixture diagnostics (no upto_seq) predate its code events: stale
ok(aic.status() == 'aic 1● stale ⚙', 'status: ' .. aic.status())

-- watcher: append an event, trace buffer updates via fs_event
local f = io.open(dir .. '/trace.jsonl', 'ab')
f:write(jl({ id = 'e5', seq = 5, ts = now + 4, kind = 'hold', turn = 1 }))
f:close()
ok(wait(3000, function()
  return table.concat(vim.api.nvim_buf_get_lines(trace.buf, 0, -1, false), '\n'):find('⏸ hold', 1, true) ~= nil
end), 'trace re-rendered after trace.jsonl append (watcher)')

-- quickfix from diagnostics.json
write(
  dir .. '/diagnostics.spec.json',
  vim.json.encode({
    compiled_at = 2,
    upto_seq = 5,
    summary = 'Two problems',
    items = {
      { kind = 'mismatch', severity = 'error', file = 'spec', line = 3, spec_line = 3, message = 'not done' },
      { kind = 'unspecified', severity = 'warning', file = 'src/app.js', line = 4, message = 'extra' },
    },
  })
)
ok(wait(3000, function()
  return vim.fn.getqflist({ title = 0 }).title == 'aic compile spec'
end), 'quickfix loaded from diagnostics.spec.json')
local qf = vim.fn.getqflist()
ok(#qf == 2, 'two quickfix items')
ok(qf[1] and qf[1].type == 'E' and qf[1].text == '[mismatch] not done', 'qf item 1 type/text')
ok(qf[1] and vim.api.nvim_buf_get_name(qf[1].bufnr) == spec_path, 'spec item points at spec.md')
ok(qf[2] and vim.api.nvim_buf_get_name(qf[2].bufnr) == root .. '/src/app.js' and qf[2].type == 'W', 'code item points at project file')
local spec = require('aic.doc')
ok(#vim.diagnostic.get(spec.get_buf(), { namespace = spec.ns }) == 1, 'spec inline diagnostic set (on load)')
require('aic.status').update()
ok(aic.status() == 'aic 1● 1E ⚙', 'status with error: ' .. aic.status())
ok(vim.tbl_contains(notes, 'aic compile spec: 1E 1W 0I — Two problems'), 'compile summary notified')

-- jump to code from trace -> same window; signs placed
aic.show('trace')
vim.api.nvim_win_set_cursor(0, { 1, 0 })
for i, t in ipairs(vim.api.nvim_buf_get_lines(trace.buf, 0, -1, false)) do
  if t:find('Edit src/app.js', 1, true) then
    vim.api.nvim_win_set_cursor(0, { i, 0 })
    break
  end
end
local trace_line = vim.fn.line('.')
trace.jump()
single('trace jump')
local cbuf = vim.api.nvim_get_current_buf()
ok(vim.deep_equal(opts_now(), wo0), 'code shown after the trace keeps its own window options: ' .. vim.inspect(opts_now()))
vim.cmd([[execute "normal! \<C-o>"]])
ok(vim.api.nvim_get_current_buf() == trace.buf and vim.fn.line('.') == trace_line, '<C-o> returns to the trace line')
vim.cmd([[execute "normal! \<C-^>"]])
ok(vim.api.nvim_get_current_buf() == cbuf, '<C-^> from the trace goes back to the code')
do
  aic.show('trace')
  local o = fresh_opts()
  ok(vim.deep_equal(o, wo0), 'trace options do not leak into a fresh buffer: ' .. vim.inspect(o))
  vim.api.nvim_set_current_buf(cbuf)
end
ok(vim.api.nvim_buf_get_name(cbuf) == root .. '/src/app.js' and vim.fn.line('.') == 3, 'cursor at hunk start')
local signs = require('aic.signs')
local marks = vim.api.nvim_buf_get_extmarks(cbuf, signs.ns, 0, -1, { details = true })
ok(#marks == 4, 'signs placed on touched lines (' .. #marks .. ')')
ok(marks[1] and marks[1][4].sign_text and marks[1][4].sign_text:find('▎', 1, true), 'sign text ▎')
vim.api.nvim_win_set_cursor(0, { 1, 0 })
ok(signs.jump(1) == 3, ']w -> 3')
ok(signs.jump(1) == 10, ']w -> 10')
ok(signs.jump(-1) == 3, '[w -> 3')

-- gd on anchor
vim.cmd.edit(vim.fn.fnameescape(spec_path))
local p, t = spec.anchor_at('- helper @src/app.js:hello', 12)
ok(p == 'src/app.js' and t == 'hello', 'anchor parse symbol')
p, t = spec.anchor_at('- greet users @src/app.js:4.', 16)
ok(p == 'src/app.js' and t == 4, 'anchor parse line')
vim.cmd.edit(vim.fn.fnameescape(root .. '/src/app.js'))

-- human edit -> human.jsonl
vim.api.nvim_buf_set_lines(cbuf, 6, 7, false, { 'changed 7', 'inserted' })
vim.cmd('silent write')
local hj = read(dir .. '/human.jsonl')
ok(hj ~= nil, 'human.jsonl written')
local ev = hj and vim.json.decode(vim.split(hj, '\n')[1])
ok(ev and ev.file == 'src/app.js', 'human event file')
ok(ev and ev.hunks[1].start == 7 and ev.hunks[1].count == 2, 'hunk start/count')
ok(ev and vim.deep_equal(ev.hunks[1].lines, { '-line 7', '+changed 7', '+inserted' }), 'hunk lines')
vim.api.nvim_buf_set_lines(cbuf, 0, 1, false, {})
vim.cmd('silent write')
local hl = vim.split(vim.trim(read(dir .. '/human.jsonl')), '\n')
local ev2 = vim.json.decode(hl[2] or 'null')
ok(ev2 and ev2.hunks[1].start == 1 and ev2.hunks[1].count == 0 and ev2.hunks[1].lines[1] == '-line 1', 'deletion hunk')

-- toggle: code <-> last aic view, in this window
aic.toggle()
ok(vim.api.nvim_get_current_buf() == spec.bufnr(), 'toggle -> last aic buffer (the spec, opened for gd above)')
aic.toggle()
ok(vim.api.nvim_get_current_buf() == cbuf, 'toggle -> back to the code')
aic.show('questions')
ok(vim.api.nvim_get_current_buf() == require('aic.chat').buf and vim.wo.wrap and not vim.wo.number, 'questions view shown')
single('questions view')
do
  local o = fresh_opts()
  ok(vim.deep_equal(o, wo0), 'questions options do not leak into a fresh buffer: ' .. vim.inspect(o))
  aic.show('questions')
end
aic.toggle()
aic.toggle()
ok(vim.api.nvim_get_current_buf() == require('aic.chat').buf, 'toggle returns to the questions view (last aic view)')
vim.cmd([[execute "normal! \<C-^>"]])
ok(vim.api.nvim_get_current_buf() == cbuf and vim.deep_equal(opts_now(), wo0), '<C-^> from questions -> code, options intact')

-- hold / continue reach the backend
aic.hold()
aic.continue('go on')
ok(wait(3000, function()
  local s = read(log) or ''
  return s:find('hold', 1, true) and s:find('continue -- go on', 1, true)
end), 'hold/continue invoked backend')

-- submit: template picker -> preview (current window) -> edit -> :w sends exactly that
local pick_name = 'review'
local picker_items
vim.ui.select = function(items, o, cb)
  picker_items = vim.tbl_map(function(it)
    return o.format_item and o.format_item(it) or it
  end, items)
  for _, it in ipairs(items) do
    if type(it) == 'table' and it.name == pick_name then
      return cb(it)
    end
  end
  cb(items[1])
end
local submit = require('aic.submit')
local before_buf = vim.api.nvim_get_current_buf()
aic.submit({})
ok(wait(3000, function()
  return vim.api.nvim_buf_get_name(0) == 'aic://preview'
end), 'submit: picked a template, the preview shows in the current window')
single('submit preview')
ok((read(log) or ''):find('prompts --json', 1, true) and (read(log) or ''):find('render --json --prompt review --doc spec', 1, true),
  'submit runs `aic prompts` then `aic render --prompt review --doc spec`')
ok(picker_items and picker_items[4]:find('^%[project%] bad  %(broken') and picker_items[2] == '[built-in] review — Review only',
  'picker lists templates with description and source, broken ones marked: ' .. vim.inspect(picker_items))
ok(vim.deep_equal(vim.api.nvim_buf_get_lines(0, 0, -1, false), { '[aic] Rendered by review', 'body one', 'body two' }),
  'preview: line 1 = typed header, then the pasted body')
ok(vim.bo.buftype == 'acwrite' and #vim.api.nvim_buf_get_extmarks(0, -1, 0, 0, { details = true }) > 0, 'preview: acwrite, separator shown')
vim.api.nvim_buf_set_lines(0, 2, 3, false, { 'body two, edited' })
vim.api.nvim_buf_set_lines(0, 0, 1, false, { 'Fix item 3' }) -- a digit in the typed header
ok(wait(1000, function()
  return #vim.diagnostic.get(0) == 1
end), 'a digit in the header is flagged live')
local n_sub0 = count(read(log), 'submit %-%-rendered')
vim.cmd('write')
vim.wait(200)
ok(count(read(log), 'submit %-%-rendered') == n_sub0 and vim.api.nvim_buf_get_name(0) == 'aic://preview',
  ':w with a bad header sends nothing; the preview stays')
vim.api.nvim_buf_set_lines(0, 0, 1, false, { 'Do it\u{E0041}\u{200B}' }) -- invisible tag + zero-width
ok(wait(1000, function()
  return #vim.diagnostic.get(0) == 2
end), 'invisible characters in the header are flagged, one by one')
ok(#vim.tbl_filter(function(m)
  return m[4].virt_text and m[4].virt_text[1][1]:find('U+E0041', 1, true)
end, vim.api.nvim_buf_get_extmarks(0, -1, 0, -1, { details = true })) == 1, 'an invisible character gets a visible ⟨U+…⟩ label')
vim.api.nvim_buf_set_lines(0, 0, 1, false, { '[aic] Review only the edited part:' })
ok(wait(1000, function()
  return #vim.diagnostic.get(0) == 0
end), 'fixed header: flag cleared')
local n_already = #vim.tbl_filter(function(m) return m == 'aic: already sending this prompt' end, notes)
vim.cmd('write')
pcall(vim.cmd, 'write') -- a second :w while the first send runs
ok(wait(3000, function()
  return vim.api.nvim_get_current_buf() == before_buf
end), ':w sent: back to the buffer before the preview')
local sent = vim.json.decode(read(root .. '/rendered.json') or 'null')
ok(sent and sent.header == '[aic] Review only the edited part:' and sent.body == 'body one\nbody two, edited'
  and sent.receipt.prompt == 'review', 'what is sent is the edited preview, with its receipt: ' .. vim.inspect(sent))
ok(submit.last_prompt.spec == 'review', 'the template is remembered for this doc')
ok(#vim.tbl_filter(function(m) return m == 'aic: already sending this prompt' end, notes) == n_already + 1
  and count(read(log), 'submit %-%-rendered') == n_sub0 + 1, 'a second :w while sending sends nothing')
-- 'nohidden' + an unsaved buffer: the preview can't show; nothing is left behind, the cursor stays
do
  vim.cmd('enew')
  vim.api.nvim_buf_set_lines(0, 0, -1, false, { 'a', 'b', 'c' })
  vim.api.nvim_win_set_cursor(0, { 3, 0 })
  vim.o.hidden = false
  submit.preview({ header = 'h', body = 'b', receipt = { prompt = 'p', doc = 'spec' } }, {})
  ok(vim.fn.bufexists('aic://preview') == 0 and vim.api.nvim_win_get_cursor(0)[1] == 3,
    "'nohidden': a refused preview leaves no buffer and keeps the cursor")
  vim.o.hidden = true
  vim.cmd('bwipeout!')
end
-- next time it is offered first; leaving the preview cancels
aic.submit({})
ok(wait(3000, function()
  return vim.api.nvim_buf_get_name(0) == 'aic://preview'
end) and picker_items[1]:find('^%[built%-in%] review') ~= nil, 'the last template for the doc is offered first')
local n_notes = #notes
vim.cmd([[execute "normal! \<C-^>"]])
ok(vim.fn.bufexists('aic://preview') == 0 and notes[#notes] == 'aic: preview closed, nothing sent' and #notes > n_notes,
  'leaving the preview cancels (wiped, notified)')
-- a template with input asks for it; a render error is reported, nothing opens
pick_name = 'message'
vim.ui.input = function(o, cb)
  ok(o.prompt == 'aic message: Message: ', 'input prompt from the template: ' .. o.prompt)
  cb('please refuse this')
end
aic.submit({})
ok(wait(3000, function()
  return (read(log) or ''):find('render --json --prompt message --doc spec --input=please refuse this', 1, true) ~= nil
end), 'input is passed to render')
ok(wait(3000, function()
  return notes[#notes]:find('aic render failed', 1, true) ~= nil
end) and vim.fn.bufexists('aic://preview') == 0, 'render error notified, no preview')
pick_name = 'review'
-- :AicPrompts: a built-in is copied (project or user) before editing; new ones from a skeleton
do
  local real_select = vim.ui.select
  vim.ui.select = function(items, o, cb)
    if o.prompt:find('^aic: put') then
      return cb(items[1]) -- project
    end
    return real_select(items, o, cb)
  end
  vim.cmd('AicPrompts')
  ok(wait(3000, function()
    return vim.api.nvim_buf_get_name(0) == root .. '/.aic/prompts/review.md'
  end) and (read(root .. '/.aic/prompts/review.md') or ''):find('^%-%-%-\ndescription:') ~= nil,
    ':AicPrompts on a built-in copies it to .aic/prompts and opens it here')
  single(':AicPrompts')
  pick_name = 'none' -- -> the last item: + new template
  local real_items_last = vim.ui.select
  vim.ui.select = function(items, o, cb)
    if o.prompt == 'aic: prompt templates' then
      return cb(items[#items])
    end
    return real_items_last(items, o, cb)
  end
  vim.ui.input = function(_, cb)
    cb('mine.md')
  end
  vim.cmd('AicPrompts')
  ok(wait(3000, function()
    return vim.api.nvim_buf_get_name(0) == root .. '/.aic/prompts/mine.md'
  end) and (read(root .. '/.aic/prompts/mine.md') or ''):find('{{content}}', 1, true) ~= nil,
    '+ new template: skeleton in .aic/prompts/mine.md, opened here')
  vim.ui.select = real_select
  pick_name = 'review'
end

-- spec :w -> compile
aic.show('doc')
single('spec view')
ok(vim.api.nvim_get_current_buf() == spec.bufnr(), 'spec view shows spec.md')
vim.cmd('silent write')
ok(wait(3000, function()
  return (read(log) or ''):find('compile', 1, true) ~= nil
end), ':w in spec runs aic compile')

-- :w during a running compile still calls `aic compile`; the backend queues it
wait(3000, function()
  return not state.local_compiling
end)
local before = count(read(log), 'compile')
write(root .. '/queue', '')
state.compiling = true -- a compile is running (the `compiling` file)
vim.cmd('silent write')
ok(wait(3000, function()
  return vim.tbl_contains(notes, 'aic: compile queued')
end), ':w while compiling runs `aic compile` and notifies "aic: compile queued"')
ok(count(read(log), 'compile') == before + 1, 'no local short-circuit while compiling')
require('aic.status').update()
ok(aic.status():find('compiling+queued', 1, true) ~= nil, 'status shows queued: ' .. aic.status())
os.remove(root .. '/queue')
state.compiling = false
state.compile_queued = false

-- folds survive re-render: open the tool node, append an event, still open
aic.show('trace')
local tool_l
for i, t in ipairs(vim.api.nvim_buf_get_lines(trace.buf, 0, -1, false)) do
  if t:find('Edit src/app.js', 1, true) then
    tool_l = i
  end
end
vim.api.nvim_win_set_cursor(0, { tool_l, 0 })
vim.cmd('normal! zo')
ok(vim.fn.foldclosed(tool_l + 1) == -1, 'tool node opened')
f = io.open(dir .. '/trace.jsonl', 'ab')
f:write(jl({ id = 'e6', seq = 6, ts = now + 5, kind = 'release', turn = 1 }))
f:close()
ok(wait(3000, function()
  return table.concat(vim.api.nvim_buf_get_lines(trace.buf, 0, -1, false), '\n'):find('▶ release', 1, true) ~= nil
end), 'release event rendered')
ok(vim.fn.foldclosed(tool_l + 1) == -1, 'tool node still open after re-render')

-- <CR> on a node with details shows/hides them; on a bullet without details it
-- never folds the turn; new edits arrive folded
local function find_line(pat)
  for i, t in ipairs(vim.api.nvim_buf_get_lines(trace.buf, 0, -1, false)) do
    if t:find(pat, 1, true) then
      return i
    end
  end
end
vim.api.nvim_win_set_cursor(0, { tool_l, 0 })
trace.enter()
ok(vim.fn.foldclosed(tool_l) == tool_l, '<CR> on open edit node hides its details')
trace.enter()
ok(vim.fn.foldclosed(tool_l + 1) == -1, '<CR> on closed edit node shows its details')
ok(vim.api.nvim_get_current_buf() == trace.buf, '<CR> on an edit node stays in the trace')
trace.enter()
local rel_l = find_line('▶ release')
vim.api.nvim_win_set_cursor(0, { rel_l, 0 })
trace.enter()
ok(vim.fn.foldclosed(1) == -1 and vim.fn.foldclosed(rel_l) == -1, '<CR> on a bullet without details leaves the turn open')
ok(vim.api.nvim_get_current_buf() == trace.buf, '<CR> on a bullet without file stays in the trace')
ok(vim.fn.maparg('gf', 'n', false, true).desc == 'aic: jump to code', 'gf jumps to code')
f = io.open(dir .. '/trace.jsonl', 'ab')
f:write(jl({ id = 'e7', seq = 7, ts = now + 6, kind = 'tool', turn = 1, tool = 'Edit', title = 'Edit src/app.js:20-20',
  why = 'later edit', file = 'src/app.js', hunks = { { start = 20, count = 1, lines = { '-a', '+b' } } } }))
f:close()
ok(wait(3000, function()
  return find_line('app.js:20-20') ~= nil
end), 'new edit rendered')
local e7 = find_line('app.js:20-20')
ok(vim.fn.foldclosed(e7) == e7, 'new edit arrives with details hidden')
ok(vim.fn.foldclosed(tool_l) == tool_l, 'edit hidden with <CR> stays hidden after re-render')
state.set_seen({ 'e7' }, true) -- one-line change inside folds: levels must stay right
ok(wait(2000, function()
  return find_line('○ Edit src/app.js:20-20') ~= nil
end), 'seen glyph re-rendered')
ok(vim.fn.foldlevel(e7 + 3) == 2 and vim.fn.foldlevel(tool_l + 5) == 2 and vim.fn.foldclosed(e7) == e7,
  'fold levels intact after a one-line re-render')
vim.api.nvim_win_set_cursor(0, { 1, 0 })
trace.enter()
ok(vim.fn.foldclosed(1) == 1, '<CR> on a turn header hides the turn')
f = io.open(dir .. '/trace.jsonl', 'ab')
f:write(jl({ id = 'e8', seq = 8, ts = now + 7, kind = 'release', turn = 1 }))
f:close()
ok(wait(3000, function()
  return #vim.tbl_filter(function(t) return t:find('▶ release', 1, true) end, vim.api.nvim_buf_get_lines(trace.buf, 0, -1, false)) == 2
end) and vim.fn.foldclosed(1) == 1, 'hidden turn stays hidden after re-render')
trace.enter()
ok(vim.fn.foldclosed(1) == -1 and vim.fn.foldclosed(e7) == e7, '<CR> shows the turn again, its edits still hidden')
-- <CR> acts on the visible line when the cursor hides inside a closed fold
vim.api.nvim_win_set_cursor(0, { rel_l, 0 })
vim.cmd('normal! zc')
trace.enter()
ok(vim.fn.foldclosed(1) == -1, '<CR> on a turn closed with zc from a child line reopens it')
vim.api.nvim_win_set_cursor(0, { e7 + 2, 0 })
vim.cmd('normal! zo')
vim.cmd('normal! zc')
ok(vim.fn.foldclosed(e7) == e7, 'zc from a hunk line hides the edit')
trace.enter()
ok(vim.fn.foldclosed(e7 + 1) == -1 and vim.api.nvim_get_current_buf() == trace.buf,
  '<CR> on an edit closed with zc from a hunk line opens it (no jump)')
-- za/zo state survives re-showing the trace window without a render in between
vim.api.nvim_win_set_cursor(0, { e7, 0 })
vim.cmd('normal! zc')
vim.cmd('normal! zo')
view.show('trace')
ok(vim.fn.foldclosed(e7 + 1) == -1, 're-showing the shown trace is a no-op (zo state kept)')
-- ... and the trace leaving its window (BufWinLeave) and coming back
local tw = vim.api.nvim_get_current_win()
local scratch = vim.api.nvim_create_buf(false, true)
vim.api.nvim_win_set_buf(tw, scratch)
view.show('trace')
ok(vim.fn.foldclosed(e7 + 1) == -1 and vim.fn.foldclosed(tool_l) == tool_l, 'fold state survives the trace leaving its window')
vim.api.nvim_buf_delete(scratch, { force = true })
vim.api.nvim_win_set_cursor(0, { e7, 0 })
vim.cmd('normal! zc')

-- worker context compacted: one line, not an unseen item
local unseen_before = state.unseen_count()
f = io.open(dir .. '/trace.jsonl', 'ab')
f:write(jl({ id = 'e7', seq = 7, ts = now + 6, kind = 'compact', turn = 1, trigger = 'manual', pre = 106701, post = 9800 }))
f:close()
ok(wait(3000, function()
  return vim.tbl_contains(vim.api.nvim_buf_get_lines(trace.buf, 0, -1, false), '  ⇣ compacted 107k → 10k (manual)')
end), 'compact event rendered as one line')
ok(not table.concat(vim.api.nvim_buf_get_lines(trace.buf, 0, -1, false), '\n'):find('· compact', 1, true)
  and state.unseen_count() == unseen_before and not state.seenable(state.by_id.e7), 'compact event is not an unseen item')
do
  local cl
  for i, t in ipairs(vim.api.nvim_buf_get_lines(trace.buf, 0, -1, false)) do
    if t:find('⇣ compacted', 1, true) then
      cl = i - 1
    end
  end
  local hm = cl and vim.api.nvim_buf_get_extmarks(trace.buf, -1, { cl, 0 }, { cl, -1 }, { details = true }) or {}
  ok(hm[1] and hm[1][4].hl_group == 'AicCompact' and vim.fn.hlexists('AicCompact') == 1, 'compact line uses AicCompact')
end

-- ask at cursor (trace node) -> float -> P appends to spec
vim.ui.input = function(_, cb)
  cb('is this right?')
end
vim.api.nvim_win_set_cursor(0, { tool_l, 0 })
local ctx = require('aic.ask').context()
ok(ctx.where == 'trace' and ctx.event and ctx.event.id == 'e2', 'ask context for trace node')
aic.ask()
ok(wait(3000, function()
  local b = vim.api.nvim_get_current_buf()
  return vim.bo[b].filetype == 'markdown' and vim.api.nvim_buf_get_lines(b, 0, 1, false)[1] == '**answer**'
end), 'ask float shows answer')
ok((read(log) or ''):find('ask %-%-doc spec %-%-context%-file .+%.json %-%- is this right%?') ~= nil, 'aic ask --context-file invoked')
local float_lines = vim.api.nvim_buf_get_lines(0, 0, -1, false)
ok(float_lines[#float_lines] == '_q close · P append to doc · S append pseudocode to doc + preview submit · Y yank pseudocode_', 'footer lists uppercase keys: ' .. float_lines[#float_lines])
ok(vim.fn.maparg('p', 'n') == '' and vim.fn.maparg('y', 'n') == '' and vim.fn.maparg('s', 'n') == '' and vim.fn.maparg('f', 'n') == '',
  'no lowercase p/y/s/f maps on the answer float')
vim.api.nvim_feedkeys('P', 'x', false)
local sl = vim.api.nvim_buf_get_lines(spec.bufnr(), 0, -1, false)
ok(sl[#sl] == '**answer**' and vim.tbl_contains(sl, '## Notes'), 'P appended answer under ## Notes')

-- what-if -> F folds into spec
aic.show('trace')
aic.whatif()
ok(wait(3000, function()
  return vim.api.nvim_buf_get_lines(0, 0, 1, false)[1] == '**answer**'
end), 'what-if float shows answer')
ok((read(log) or ''):find('ask %-%-doc spec %-%-context%-file .+ %-%-whatif %-%- is this right%?') ~= nil, 'aic ask --whatif invoked')
vim.api.nvim_feedkeys('F', 'x', false)
sl = vim.api.nvim_buf_get_lines(spec.bufnr(), 0, -1, false)
ok(vim.tbl_contains(sl, '## What-if: is this right?'), 'F folded what-if into spec')
aic.show('doc')
vim.cmd('silent write')

-- chat send
local chat = require('aic.chat')
local cb = chat.buf
vim.api.nvim_buf_set_lines(cb, -2, -1, false, { 'why?' })
ok(vim.deep_equal(chat.input_lines(), { 'why?' }), 'chat input under ## You')
chat.send()
ok(wait(3000, function()
  return (read(log) or ''):find('ask --doc spec -- why?', 1, true) ~= nil
end), 'chat <CR> sends via aic ask')
ok(read(dir .. '/chat.md'):find('why', 1, true) == nil, 'plugin did not write chat.md')

---------------------------------------------------------------------------
-- anchors: parsing
local anchors = require('aic.anchors')
local function A(line)
  return anchors.anchor_at(line, line:find('@', 1, true) - 1)
end
local function is(line, kind, fields)
  local a = A(line)
  local good = a and a.kind == kind
  for k, v in pairs(fields or {}) do
    good = good and a[k] == v
  end
  ok(good, ('parse %q -> %s%s'):format(line, kind, fields and (' ' .. vim.inspect(fields, { newline = '' })) or ''))
end
is('@server.js', 'file', { path = 'server.js', abs = root .. '/server.js', anchor = '@server.js' })
is('x @src/app.js:4', 'line', { path = 'src/app.js', line = 4 })
is('(@src/app.js:10-20)', 'range', { line = 10, ['end'] = 20, anchor = '@src/app.js:10-20' })
is('see @server.js:handle.', 'symbol', { symbol = 'handle' })
is('"@src/"', 'dir', { abs = root .. '/src' })
is('@' .. root .. '/server.js', 'file', { outside = true })
is('@~/', 'dir', { outside = true })
is('@diff', 'diff', { path = nil })
is('@diff:src/app.js,', 'diff', { path = 'src/app.js' })
is('@staged;', 'staged')
is('@turn:1', 'turn', { turn = 1 })
is('[@turn:last]', 'turn', { turn = 'last' })
is('@log', 'log')
is('@log:src/app.js', 'log', { path = 'src/app.js' })
is('@show:HEAD~1', 'show', { rev = 'HEAD~1' })
is('{@show:HEAD@{1}}', 'show', { rev = 'HEAD@{1}' })
is('@server.js:12.', 'line', { line = 12, anchor = '@server.js:12' })
ok(A('mail a@b.com') == nil, 'a@b.com is not an anchor')
ok(A('@nonexistent.js') == nil, '@nonexistent.js is not an anchor')
ok(A('@server.js/') == nil, 'file with trailing / is not an anchor')
ok(A('`@server.js`') == nil, 'backtick prefix is not an anchor')
ok(A('@show:-x') == nil, '@show:-x is not an anchor')
ok(A('@turn:x') == nil, '@turn:x is not an anchor')
local all = anchors.all('- see @server.js and @src/app.js:3, x@y @nope')
ok(#all == 2 and all[1].s == 7 and all[1].e == 16 and all[2].anchor == '@src/app.js:3', 'all(): two anchors with columns')
ok(anchors.anchor_at('- see @server.js', 3) == nil and anchors.anchor_at('- see @server.js', 6) ~= nil, 'anchor_at respects cursor column')
ok(A('@server.js:0') == nil and A('@server.js:0-3') == nil and A('@server.js:5-2') == nil, 'line 0 and reversed ranges rejected')

-- shared grammar fixture (also used by the Python tests)
do
  local fx = vim.json.decode(read(plugin_dir .. '/../tests/anchor_cases.json'))
  local froot = vim.fn.resolve(vim.fn.tempname())
  for _, d in ipairs(fx.dirs) do
    vim.fn.mkdir(froot .. '/' .. d, 'p')
  end
  for _, fname in ipairs(fx.files) do
    write(froot .. '/' .. fname, fname == 'src/app.js' and fx.app_js or 'x\n')
  end
  local saved = state.root
  state.root = froot
  for _, c in ipairs(fx.cases) do
    local got = vim.tbl_map(function(a)
      return { a.kind, a.anchor }
    end, anchors.all(c.text))
    ok(vim.deep_equal(got, c.anchors), ('fixture %q -> %s'):format(c.text, vim.json.encode(got)))
  end
  state.root = saved
end

-- highlighting + omnifunc in the spec buffer
local sb = spec.bufnr()
local hl_marks = vim.api.nvim_buf_get_extmarks(sb, anchors.ns, 0, -1, { details = true })
ok(#hl_marks >= 2 and hl_marks[1][4].hl_group == 'AicAnchor', 'spec anchors highlighted with AicAnchor (' .. #hl_marks .. ')')
ok(vim.bo[sb].omnifunc:find('aic.anchors', 1, true) ~= nil, 'spec omnifunc set')
ok(vim.bo[chat.buf].omnifunc:find('aic.anchors', 1, true) ~= nil, 'chat omnifunc set')
aic.show('doc')
vim.api.nvim_buf_set_lines(sb, -1, -1, false, { '- (@ser' })
vim.api.nvim_win_set_cursor(0, { vim.api.nvim_buf_line_count(sb), 7 })
ok(anchors.omnifunc(1, '') == 3, 'omnifunc findstart at @')
local words = vim.tbl_map(function(c)
  return c.word
end, anchors.omnifunc(0, '@ser'))
ok(words[1] == '@server.js', 'omnifunc @ser -> @server.js: ' .. table.concat(words, ' '))
words = vim.tbl_map(function(c)
  return c.word
end, anchors.omnifunc(0, '@'))
ok(vim.tbl_contains(words, '@src/') and vim.tbl_contains(words, '@src/app.js') and vim.tbl_contains(words, '@turn:1')
  and vim.tbl_contains(words, '@diff') and vim.tbl_contains(words, '@log') and vim.tbl_contains(words, '@show:HEAD')
  and not vim.tbl_contains(words, '@.aic/'), 'omnifunc @ -> dirs, files, specials, turns; no .aic')
vim.api.nvim_buf_set_lines(sb, -2, -1, false, {})

-- gd from the spec -> code in the same window
local function gd_on(text, col)
  aic.show('doc')
  vim.api.nvim_buf_set_lines(sb, -1, -1, false, { '- ' .. text })
  vim.api.nvim_win_set_cursor(0, { vim.api.nvim_buf_line_count(sb), col or 3 })
  vim.api.nvim_feedkeys('gd', 'x', false)
  vim.api.nvim_buf_set_lines(sb, -2, -1, false, {})
end
local function at(path, line)
  return #vim.api.nvim_list_tabpages() == 1 and #solid_wins() == 1
    and vim.fs.normalize(vim.api.nvim_buf_get_name(0)) == path
    and (line == nil or vim.fn.line('.') == line)
end
gd_on('@server.js')
ok(at(root .. '/server.js', 1), 'gd file -> same window, line 1')
gd_on('@src/app.js:7')
ok(at(root .. '/src/app.js', 7), 'gd line -> line 7')
gd_on('@src/app.js:4-6')
ok(at(root .. '/src/app.js', 4) and vim.fn.mode() == 'V' and vim.fn.line('v') == 6, 'gd range -> visual 4-6, cursor at 4')
vim.api.nvim_feedkeys(vim.keycode('<Esc>'), 'x', false)
ok(vim.fn.line("'<") == 4 and vim.fn.line("'>") == 6, "range marks '< '> set")
gd_on('@server.js:handle')
ok(at(root .. '/server.js', 3), 'gd symbol -> definition line 3 (not the comment)')
gd_on('@src/')
ok(at(root .. '/src'), 'gd dir -> directory buffer')
gd_on('plain words', 4)
ok(vim.api.nvim_get_current_buf() == sb, 'gd off an anchor stays (default gd)')
gd_on('@diff')
ok(wait(3000, function()
  return vim.bo.filetype == 'diff'
end) and #solid_wins() == 1, 'gd @diff -> diff buffer in the same window')
local dl = table.concat(vim.api.nvim_buf_get_lines(0, 0, -1, false), '\n')
ok(dl:find('diff --git a/src/app.js', 1, true) ~= nil and vim.bo.buftype == 'nofile', 'diff buffer has git diff HEAD')
gd_on('@diff:server.js')
ok(wait(3000, function()
  return vim.tbl_contains(notes, 'aic: no changes for @diff:server.js')
end), 'gd @diff:path with no changes notifies')
gd_on('@log')
ok(wait(3000, function()
  return vim.bo.filetype == 'git'
end) and vim.api.nvim_buf_get_lines(0, 0, 1, false)[1] == 'commit abc123', 'gd @log -> aic anchor --json text in git buffer')
ok((read(log) or ''):find('anchor --json @log', 1, true) ~= nil, 'aic anchor --json invoked')
gd_on('@turn:1')
ok(vim.api.nvim_get_current_buf() == trace.buf and vim.api.nvim_get_current_line():find('^▸ Turn 1') ~= nil, 'gd @turn:1 -> trace turn header')
gd_on('@turn:last')
ok(vim.api.nvim_get_current_line():find('^▸ Turn 1') ~= nil, 'gd @turn:last -> last turn header')

-- gd off an anchor calls a pre-existing buffer `gd` mapping
do
  local hit = 0
  local b = vim.api.nvim_create_buf(false, true)
  vim.keymap.set('n', 'gd', function()
    hit = hit + 1
  end, { buffer = b })
  anchors.attach(b)
  vim.api.nvim_set_current_buf(b)
  vim.api.nvim_buf_set_lines(b, 0, -1, false, { 'plain @server.js' })
  vim.api.nvim_win_set_cursor(0, { 1, 0 })
  vim.api.nvim_feedkeys('gd', 'x', false)
  ok(hit == 1, 'gd fallback calls the previous buffer gd callback')
  local b2 = vim.api.nvim_create_buf(false, true)
  vim.api.nvim_buf_set_keymap(b2, 'n', 'gd', '<Cmd>let g:aic_prev_gd = 1<CR>', { noremap = true })
  anchors.attach(b2)
  vim.api.nvim_set_current_buf(b2)
  vim.api.nvim_buf_set_lines(b2, 0, -1, false, { 'plain' })
  vim.api.nvim_feedkeys('gd', 'x', false)
  ok(vim.g.aic_prev_gd == 1, 'gd fallback feeds the previous buffer gd rhs')
  local b3 = vim.api.nvim_create_buf(false, true)
  anchors.attach(b3)
  vim.api.nvim_set_current_buf(b3)
  vim.api.nvim_buf_set_lines(b3, 0, -1, false, { '' })
  local n0 = #notes
  local okk = pcall(vim.api.nvim_feedkeys, 'gd', 'x', false)
  ok(okk and #notes == n0 + 1 and notes[#notes]:find('^aic: gd:') ~= nil, 'failing builtin gd is notified, not raised')
end

-- gd + send in chat
aic.show('questions')
vim.api.nvim_buf_set_lines(chat.buf, -2, -1, false, { '@server.js:3 why?' })
vim.api.nvim_win_set_cursor(0, { vim.api.nvim_buf_line_count(chat.buf), 2 })
vim.api.nvim_feedkeys('gd', 'x', false)
ok(at(root .. '/server.js', 3), 'gd in questions -> server.js:3 in the same window')
aic.show('questions')
vim.api.nvim_feedkeys(vim.keycode('<CR>'), 'x', false)
ok(wait(3000, function()
  return (read(log) or ''):find('ask --doc spec -- @server.js:3 why?', 1, true) ~= nil
end), 'chat <CR> sends anchors as-is')

-- global <prefix>g in a plain buffer
vim.cmd('enew')
vim.api.nvim_buf_set_lines(0, 0, -1, false, { 'go @src/app.js:9' })
vim.api.nvim_win_set_cursor(0, { 1, 5 })
vim.api.nvim_feedkeys(vim.keycode('<leader>zg'), 'x', false)
ok(at(root .. '/src/app.js', 9), '<prefix>g follows anchor in any buffer')

-- answer float: S appends first fenced block + submits, Y yanks, gd works
local function ask_float()
    aic.show('doc')
  vim.ui.input = function(_, cb2)
    cb2('fence it')
  end
  aic.ask()
  return wait(3000, function()
    return vim.api.nvim_get_current_line() == 'Here:'
  end)
end
ok(ask_float(), 'fenced answer float shown')
local fbuf, fwin = vim.api.nvim_get_current_buf(), vim.api.nvim_get_current_win()
ok(#vim.api.nvim_buf_get_extmarks(fbuf, anchors.ns, 0, -1, {}) == 1, 'anchor highlighted in answer float')
local cur = vim.api.nvim_buf_get_lines(sb, 0, -1, false)
local first = #cur + 1 + (cur[#cur] ~= '' and 1 or 0)
local last = first + 3
vim.api.nvim_feedkeys('S', 'x', false)
sl = vim.api.nvim_buf_get_lines(sb, 0, -1, false)
ok(sl[first] == '## From compiler' and sl[first + 2] == 'step one' and sl[last] == 'step two' and #sl == last,
  'S appended first fenced block under ## From compiler: heading')
ok(not vim.bo[sb].modified and read(spec_path):find('## From compiler', 1, true) ~= nil, 'S saved spec.md')
ok(wait(3000, function()
  return (read(log) or ''):find(('render --json --prompt selection --doc spec --from %d --to %d'):format(first, last), 1, true) ~= nil
end), ('S renders the selection template for lines %d-%d'):format(first, last))
ok(not vim.api.nvim_win_is_valid(fwin), 'S closed the float')
ok(wait(3000, function()
  return vim.api.nvim_buf_get_name(0) == 'aic://preview'
end), 'S: the preview opens in the window under the float')
single('S preview')
vim.cmd('write')
ok(wait(3000, function()
  return vim.api.nvim_buf_get_name(0) ~= 'aic://preview'
end) and vim.json.decode(read(root .. '/rendered.json')).receipt.prompt == 'selection', 'S preview :w sends it')

ok(ask_float(), 'second fenced answer float shown')
vim.api.nvim_feedkeys('Y', 'x', false)
ok(vim.fn.getreg('"') == 'step one\nstep two\n' and vim.fn.getregtype('"') == 'V', 'Y yanked first fenced block linewise')

ok(ask_float(), 'third fenced answer float shown')
fwin = vim.api.nvim_get_current_win()
vim.api.nvim_win_set_cursor(0, { 6, 5 })
vim.api.nvim_feedkeys('gd', 'x', false)
ok(at(root .. '/server.js', 3), 'gd in answer float -> server.js:3')
pcall(vim.api.nvim_win_close, fwin, true)

-- a block that brings its own heading gets no extra "## From compiler"
do
  local spec = require('aic.doc')
  local f, l = spec.append_section(nil, { '## Feature: x', 'do y' }, false)
  local cur = vim.api.nvim_buf_get_lines(sb, 0, -1, false)
  ok(cur[f] == '## Feature: x' and cur[l] == 'do y' and cur[f - 2] ~= '## From compiler',
    'heading-less append keeps the block heading only')
end

-- :AicLastAnswer reopens the closed float with its keys
vim.cmd('AicLastAnswer')
ok(vim.api.nvim_win_get_config(0).relative ~= '' and vim.api.nvim_get_current_line() == 'Here:',
  ':AicLastAnswer reopened the last answer')
vim.api.nvim_feedkeys('Y', 'x', false)
ok(vim.fn.getreg('"') == 'step one\nstep two\n', 'reopened float keeps its Y key')

-- S is one-shot per answer, also across :AicLastAnswer
local function submits()
  return count(read(log), 'render %-%-json %-%-prompt selection %-%-doc spec %-%-from')
end
local n_sub = submits()
vim.cmd('AicLastAnswer')
vim.api.nvim_feedkeys('S', 'x', false)
ok(wait(3000, function()
  return submits() == n_sub + 1
end), 'S on the reopened answer submits once')
vim.cmd('AicLastAnswer')
fwin = vim.api.nvim_get_current_win()
vim.api.nvim_feedkeys('S', 'x', false)
ok(notes[#notes]:find('^aic: already appended to the doc') ~= nil, 'second S says the block is already in the doc')
vim.wait(300)
ok(submits() == n_sub + 1, 'second S did not submit again')
pcall(vim.api.nvim_win_close, fwin, true)

-- questions starting with `-` are not parsed as options
aic.show('doc')
vim.ui.input = function(_, cb2)
  cb2('-h what?')
end
aic.ask()
ok(wait(3000, function()
  return (read(log) or ''):find(' -- -h what?', 1, true) ~= nil
end), 'ask passes `--` before a question starting with -')
pcall(vim.api.nvim_win_close, vim.api.nvim_get_current_win(), true)
aic.continue('-x go')
ok(wait(3000, function()
  return (read(log) or ''):find('continue -- -x go', 1, true) ~= nil
end), 'continue passes `--` before the message')

---------------------------------------------------------------------------
-- usage: winbars, quota, 80% hint, :AicUsage float, compact/reset
do
  local usage = require('aic.usage')
  local function strip(s)
    return (s:gsub('%%#[^#]*#', ''):gsub('%%%*', ''):gsub('%%%%', '%%'))
  end
  -- show view `name` in the window, then evaluate that window's winbar
  local function bar_of(name)
    aic.show(name)
    local w = vim.api.nvim_get_current_win()
    return vim.api.nvim_eval_statusline(vim.wo[w].winbar, { winid = w, use_winbar = true, maxwidth = 300 }).str
  end
  -- the headless screen is 80 columns: check the full format here, narrow apart
  require('aic.usage').force_full = true
  local W_SPEC = 'compile · sonnet  ctx:[####------] 82k/200k (41%)  12 runs  $1.20'
  local W_TRACE = 'worker · sonnet  ctx 107k (?%)  turn 21'
  local W_CHAT = 'ask · opus  ctx:[######----] 126k/200k (63%)  9 turns  $2.85   5h:57% wk:15%'
  ok(wait(3000, function()
    return strip(usage.cache.questions) == W_CHAT
  end), 'chat winbar (ask + quota, inactive 0% bucket omitted): ' .. strip(usage.cache.questions))
  ok(strip(usage.cache.doc) == W_SPEC, 'spec winbar (compile): ' .. strip(usage.cache.doc))
  ok(strip(usage.cache.trace) == W_TRACE, 'trace winbar (worker, null ctx_window): ' .. strip(usage.cache.trace))
  ok(usage.cache.doc:find('%#AicUsageOk#[####------] 82k/200k (41%%)%*', 1, true) ~= nil, 'bar+percent colored with AicUsageOk')
  ok(bar_of('doc') == W_SPEC and bar_of('trace') == W_TRACE and bar_of('questions') == W_CHAT,
    'view winbars evaluate to the cached strings: ' .. vim.inspect({ bar_of('doc'), bar_of('trace'), bar_of('questions') }))
  aic.show('doc')
  ok(vim.api.nvim_get_option_value('winbar', { win = 0, scope = 'local' }):find('aic.usage', 1, true) ~= nil
    and vim.go.winbar == '', 'winbar is local (global empty)')
  vim.cmd.edit(vim.fn.fnameescape(root .. '/src/app.js'))
  ok(vim.wo.winbar == '', 'the winbar goes away with the view (code in the same window has none)')
  aic.show('doc')
  ok(fresh_opts()[4] == '', 'the spec winbar does not leak into a fresh buffer')
  vim.cmd.edit(vim.fn.fnameescape(root .. '/src/app.js'))
  single('winbar checks')
  aic.show('doc')
  vim.cmd('split') -- the user's own split
  local split_win = vim.api.nvim_get_current_win()
  vim.cmd.edit(vim.fn.fnameescape(root .. '/server.js'))
  ok(vim.wo[split_win].winbar == '', 'code opened in a split of a view has no winbar')
  vim.api.nvim_win_close(split_win, true)
  ok(usage.short_model('claude-fable-5-1') == 'fable' and usage.short_model('claude-haiku-5-5') == 'haiku',
    'short model names')

  -- quota omitted when null
  write(root .. '/quota.json', '{"sessions":{},"quota":null}')
  usage.fetch_quota()
  ok(wait(3000, function()
    return strip(usage.cache.questions) == 'ask · opus  ctx:[######----] 126k/200k (63%)  9 turns  $2.85'
  end), 'quota omitted when null: ' .. strip(usage.cache.questions))
  write(root .. '/quota.json', quota_json)
  usage.fetch_quota()
  ok(wait(3000, function()
    return strip(usage.cache.questions) == W_CHAT
  end), 'quota back')

  -- 80% hint: once per session per crossing
  local function hints()
    local n = 0
    for _, m in ipairs(notes) do
      if m:find('aic: ask context', 1, true) then
        n = n + 1
      end
    end
    return n
  end
  write(dir .. '/usage.json', usage_json(164000))
  ok(wait(3000, function()
    return vim.tbl_contains(notes, 'aic: ask context 82% — <leader>zU then C to compact (R to reset)')
  end), '80% hint notified')
  write(dir .. '/usage.json', usage_json(171000) .. ' ')
  ok(wait(3000, function()
    return strip(usage.cache.questions):find('86%', 1, true) ~= nil
  end) and hints() == 1, 'still above 80%: no second hint (' .. hints() .. ')')
  ok(usage.cache.questions:find('%#AicUsageHot#', 1, true) ~= nil, '>= 85% uses AicUsageHot')
  -- big but far from full (1M window): the token threshold hints anyway
  do
    local orig = read(dir .. '/usage.json')
    local u = vim.json.decode(orig)
    u.compile.session_id, u.compile.ctx_tokens, u.compile.ctx_window = 'BIG1', 260000, 1000000
    write(dir .. '/usage.json', vim.json.encode(u))
    ok(wait(3000, function()
      return vim.tbl_contains(notes, 'aic: compile context 26% — <leader>zU then c to compact (r to reset)')
    end), '250k-token hint on a 1M window')
    write(dir .. '/usage.json', orig .. '  ')
    wait(3000, function()
      return strip(usage.cache.doc or ''):find('41%', 1, true) ~= nil
    end)
  end

  -- narrow window: the bar is dropped, tokens/window (percent) stay
  do
    local usage_m = require('aic.usage')
    usage_m.force_full = false
    aic.show('doc')
    vim.cmd('vsplit') -- room to make one narrow
    local w = vim.api.nvim_get_current_win()
    vim.api.nvim_win_set_width(w, 30)
    local got = vim.api.nvim_win_call(w, function() return usage_m.winbar('doc') end)
    ok(strip(got) == 'compile · sonnet  ctx:82k/200k (41%)  12 runs  $1.20', 'narrow winbar drops the bar: ' .. strip(got))
    vim.api.nvim_win_close(w, true)
    usage_m.force_full = true
  end

  -- :AicUsage float
    vim.cmd('AicUsage')
  local ub = vim.api.nvim_get_current_buf()
  local ul = vim.api.nvim_buf_get_lines(ub, 0, -1, false)
  local ut = table.concat(ul, '\n')
  ok(vim.api.nvim_win_get_config(0).relative ~= '' and vim.bo[ub].filetype == 'markdown', ':AicUsage opens a markdown float')
  ok(ut:find('## compile · sonnet  `C1`', 1, true) and ut:find('- ctx `[####------]` 82k / 200k (41%)', 1, true)
    and ut:find('- 12 runs · $1.20', 1, true) and ut:find('- compacted from `C0abcdef`', 1, true), 'compile block')
  ok(ut:find('## ask · opus', 1, true) and ut:find('- ctx `[#########-]` 171k / 200k (86%)', 1, true)
    and ut:find('- 9 turns · $2.85', 1, true), 'ask block')
  ok(ut:find('## worker · sonnet', 1, true) and ut:find('- ctx 107k / ? (?%): the window size is learned on its next call', 1, true) and ut:find('- turn 21', 1, true), 'worker block')
  ok(ut:find('## account quota  (updated 30s ago)', 1, true) and ut:find('- 5h: 57%  resets in 3h30m', 1, true)
    and ut:find('- week: 15%  resets in 6d7h', 1, true) and ut:find('- Fable: 0%  resets in', 1, true), 'quota block: ' .. ut)
  local rows = {}
  for _, line in ipairs(ul) do
    if line:find('^| %d') then
      table.insert(rows, line)
    end
  end
  ok(#rows == 15, 'last calls capped at 15 (' .. #rows .. ')')
  ok(rows[1] and rows[1]:find('^| ' .. os.date('%H:%M', now - 60) .. ' | ask | ask #1 | 3 | 50k | 2k | 1k | $0.10 |') ~= nil
    and rows[2]:find('| compile | compile #1 |', 1, true) and rows[15]:find('| ask | ask #8 |', 1, true),
    'last calls newest first across compile+ask: ' .. tostring(rows[1]))
  local FOOT = '_c compact compile · C compact ask · w compact worker · r reset compile · R reset ask · q close_'
  ok(ul[#ul - 2] == FOOT and ul[#ul]:find('reset = free', 1, true) ~= nil, 'usage footer keys + cost note')

  -- c / R with confirmation stubbed to yes
  vim.ui.select = function(_, _, cb2)
    cb2('yes')
  end
  local uwin = vim.api.nvim_get_current_win()
  vim.api.nvim_feedkeys('c', 'x', false)
  local function last()
    local l = vim.api.nvim_buf_get_lines(ub, 0, -1, false)
    return l[#l]:find('compacting', 1, true) and l[#l] or l[#l - 2]
  end
  ok(last():find('compacting…', 1, true) ~= nil, 'footer shows compacting… while running')
  ok(wait(3000, function()
    return (read(log) or ''):find('compact compile\n', 1, true) ~= nil and last() == FOOT
  end), 'c runs `aic compact compile` and re-renders')
  ok(vim.tbl_contains(notes, 'compacted compile'), 'compact result notified')
  vim.api.nvim_feedkeys('R', 'x', false)
  ok(wait(3000, function()
    return (read(log) or ''):find('compact ask --reset', 1, true) ~= nil and last() == FOOT
  end) and vim.api.nvim_win_is_valid(uwin), 'R runs `aic compact ask --reset`, float stays open')
  local asked
  vim.ui.select = function(_, o, cb2)
    asked = o.prompt
    cb2('yes')
  end
  vim.api.nvim_feedkeys('w', 'x', false)
  ok(asked == 'aic: compact the worker session?', 'w asks yes/no first: ' .. tostring(asked))
  ok(wait(3000, function()
    return (read(log) or ''):find('compact worker\n', 1, true) ~= nil and last() == FOOT
  end) and vim.tbl_contains(notes, 'compacted worker') and vim.api.nvim_win_is_valid(uwin),
    'w runs `aic compact worker`, notifies, float stays open')
  write(root .. '/refuse', '')
  vim.api.nvim_feedkeys('w', 'x', false)
  ok(wait(3000, function()
    return vim.tbl_contains(notes, 'aic compact worker failed (1): dialog open')
      and last() == FOOT
  end), 'worker compact refusal notified as an error')
  os.remove(root .. '/refuse')
  vim.ui.select = function(_, _, cb2)
    cb2('no')
  end
  local before = read(log)
  vim.api.nvim_feedkeys('w', 'x', false)
  vim.wait(200)
  ok(read(log) == before, 'w answered no runs nothing')
  pcall(vim.api.nvim_win_close, uwin, true)
  ok(vim.deep_equal(vim.fn.getcompletion('AicCompact a', 'cmdline'), { 'ask' })
    and vim.deep_equal(vim.fn.getcompletion('AicCompact ', 'cmdline'), { 'compile', 'ask', 'worker' })
    and vim.deep_equal(vim.fn.getcompletion('AicReset ', 'cmdline'), { 'compile', 'ask' }), ':AicCompact/:AicReset complete names')
  local before_rw = read(log)
  vim.cmd('AicReset worker')
  vim.wait(200)
  ok(read(log) == before_rw and vim.tbl_contains(notes, 'aic: reset compile|ask'), ':AicReset worker is refused (reset is compile|ask only)')
  vim.cmd('AicCompact worker')
  ok(wait(3000, function()
    local n = 0
    for _ in (read(log) or ''):gmatch('compact worker\n') do
      n = n + 1
    end
    return n == 3 and usage.running == nil
  end), ':AicCompact worker runs `aic compact worker`')
  vim.cmd('AicReset compile')
  ok(wait(3000, function()
    return (read(log) or ''):find('compact compile --reset', 1, true) ~= nil
  end), ':AicReset compile runs `aic compact compile --reset`')
  ok(vim.fn.maparg('<leader>zU', 'n') ~= '', '<prefix>U mapped')

  -- the winbar comes back with the view
  vim.cmd.edit(vim.fn.fnameescape(root .. '/src/app.js'))
  ok(bar_of('questions'):find('^ask · opus') ~= nil and bar_of('doc') == W_SPEC,
    'winbars back when the views come back: ' .. vim.inspect({ bar_of('doc'), bar_of('questions') }))
  single('winbar views')
end

---------------------------------------------------------------------------
-- chat anchors: only the visible lines (±50) are highlighted
do
  local anchors = require('aic.anchors')
  local chat = require('aic.chat')
  local big = { '## Compiler' }
  for i = 1, 400 do
    big[#big + 1] = 'see @server.js:3 line ' .. i
  end
  write(dir .. '/chat.md', table.concat(big, '\n') .. '\n')
  ok(wait(3000, function()
    return vim.api.nvim_buf_line_count(chat.buf) > 400
  end), 'big chat.md rendered')
  aic.show('questions')
  local cw = vim.api.nvim_get_current_win()
  vim.api.nvim_win_set_cursor(cw, { vim.api.nvim_buf_line_count(chat.buf), 0 })
  anchors.highlight(chat.buf)
  local top, bot = unpack(vim.api.nvim_win_call(cw, function()
    return { vim.fn.line('w0'), vim.fn.line('w$') }
  end))
  local hm = vim.api.nvim_buf_get_extmarks(chat.buf, anchors.ns, 0, -1, {})
  local inside = #hm > 0
  for _, m in ipairs(hm) do
    if m[2] + 1 < top - 50 or m[2] + 1 > bot + 50 then
      inside = false
    end
  end
  ok(inside and #hm <= (bot - top + 1) + 100, ('chat highlights only visible ±50 lines (%d marks, view %d-%d)'):format(#hm, top, bot))
end

---------------------------------------------------------------------------
-- signs: bounded to the visible lines ±200, only changed buffers re-applied
do
  local signs = require('aic.signs')
  local bl = {}
  for i = 1, 3000 do
    bl[i] = 'b' .. i
  end
  write(root .. '/src/big.js', table.concat(bl, '\n') .. '\n')
  local function touched(app_end)
    write(dir .. '/touched.json', vim.json.encode({
      ['src/app.js'] = { { start = 3, ['end'] = app_end, event = 'e2', actor = 'worker' } },
      ['src/big.js'] = { { start = 1, ['end'] = 3000, event = 'e2', actor = 'worker' } },
    }))
  end
  touched(5)
  ok(wait(3000, function()
    return state.touched['src/big.js'] ~= nil
  end), 'touched.json with a 3000-line range loaded')
  vim.cmd('silent! only')
  vim.cmd.edit(vim.fn.fnameescape(root .. '/src/big.js'))
  local bbuf = vim.api.nvim_get_current_buf()
  local bwin = vim.api.nvim_get_current_win()
  vim.cmd('botright split ' .. vim.fn.fnameescape(root .. '/src/app.js'))
  local abuf = vim.api.nvim_get_current_buf()
  vim.api.nvim_set_current_win(bwin)
  local function rows(b)
    local r = {}
    for _, m in ipairs(vim.api.nvim_buf_get_extmarks(b, signs.ns, 0, -1, {})) do
      r[#r + 1] = m[2]
    end
    return r
  end
  local function ids(b)
    local r = {}
    for _, m in ipairs(vim.api.nvim_buf_get_extmarks(b, signs.ns, 0, -1, {})) do
      r[#r + 1] = m[1]
    end
    return r
  end
  local h = vim.api.nvim_win_get_height(bwin)
  local br = rows(bbuf)
  ok(#br > 0 and #br <= h + 2 * signs.margin + 1 and br[1] == 0,
    ('big range: signs only around the view (%d marks for 3000 lines)'):format(#br))
  vim.api.nvim_win_set_cursor(bwin, { 2500, 0 })
  vim.cmd('normal! zt')
  vim.api.nvim_exec_autocmds('WinScrolled', {})
  ok(wait(2000, function()
    local r = rows(bbuf)
    return vim.tbl_contains(r, 2499) and not vim.tbl_contains(r, 0)
  end), 'signs follow the view after WinScrolled')
  ok(signs.jump(1) == nil and signs.jump(-1) == 1, 'inside one big range: ]w finds none, [w goes to its start')
  vim.api.nvim_win_set_cursor(bwin, { 2500, 0 })
  signs.blame()
  local fb = vim.api.nvim_get_current_buf()
  ok(vim.api.nvim_buf_get_lines(fb, 0, 1, false)[1] == '# Edit src/app.js:3-5', 'blame on a line outside the drawn signs uses the range')
  vim.api.nvim_win_close(0, true)
  vim.api.nvim_set_current_win(bwin)
  local before_big, before_app = ids(bbuf), rows(abuf)
  ok(vim.deep_equal(before_app, { 2, 3, 4 }), 'app.js signs 3-5: ' .. vim.inspect(before_app))
  touched(7)
  ok(wait(3000, function()
    return vim.deep_equal(rows(abuf), { 2, 3, 4, 5, 6 })
  end), 'changed file re-applied (app.js 3-7)')
  ok(vim.deep_equal(ids(bbuf), before_big), 'unchanged file not re-applied (same extmark ids)')
end

---------------------------------------------------------------------------
-- debounce/throttle timers are reused across set_dir/activate
do
  local function timers()
    local n = 0
    vim.uv.walk(function(hd)
      if hd:get_type() == 'timer' and not hd:is_closing() then
        n = n + 1
      end
    end)
    return n
  end
  vim.wait(400)
  local before = timers()
  for _ = 1, 3 do
    state.deactivate()
    state.activate(root)
    wait(3000, function()
      return state.dir == dir
    end)
  end
  vim.wait(400)
  ok(state.dir == dir and timers() <= before, ('no timer leak on re-activate (%d -> %d)'):format(before, timers()))
end

---------------------------------------------------------------------------
-- docs: several files, compile only with the marker, diagnostics per doc
do
  local doc = require('aic.doc')
  local notes_path = root .. '/.aic/docs/notes.md'
  vim.cmd('silent! only') -- the signs test above made its own split
  vim.cmd.edit(vim.fn.fnameescape(root .. '/src/app.js'))
  local answer = 'No'
  vim.ui.select = function(items, o, cb)
    if o.prompt and o.prompt:find('when saved', 1, true) then
      return cb(answer)
    end
    for _, it in ipairs(items) do -- the template picker: review
      if type(it) == 'table' and it.name == 'review' then
        return cb(it)
      end
    end
    cb(items[1])
  end
  vim.cmd('AicDocNew notes')
  ok(vim.api.nvim_get_current_buf() == doc.bufnr('notes') and read(notes_path) == '# notes\n\n',
    ':AicDocNew notes (compile: No) creates the doc without a marker and shows it here')
  single('new doc')
  ok(doc.current() == 'notes' and require('aic.view').name_of(0) == 'doc', 'notes is the current doc')
  local n_compile = count(read(log), 'compile')
  vim.api.nvim_buf_set_lines(0, -1, -1, false, { '- idea @src/app.js:3' })
  vim.cmd('silent write')
  vim.wait(300)
  ok(count(read(log), 'compile') == n_compile, ':w on a doc without the marker does not compile')
  answer = 'Yes'
  vim.cmd('AicDocNew plan')
  ok(read(root .. '/.aic/docs/plan.md') == '<!-- aic: compile -->\n# plan\n\n', ':AicDocNew plan (compile: Yes) writes the marker')
  vim.cmd('silent write')
  ok(wait(3000, function()
    return (read(log) or ''):find('compile --doc plan', 1, true) ~= nil
  end), ':w on a doc with the marker runs `aic compile --doc plan`')
  vim.cmd('AicDocNew bad/name')
  ok(notes[#notes]:find('bad doc name', 1, true) ~= nil and not vim.uv.fs_stat(root .. '/.aic/docs/bad'), 'bad doc names are refused')
  -- per-doc diagnostics: notes gets its own, spec keeps its own
  vim.cmd('AicDoc notes')
  write(dir .. '/diagnostics.notes.json', vim.json.encode({ doc = 'notes', compiled_at = 9, upto_seq = 99, summary = 'notes ok',
    items = { { kind = 'incoherent', severity = 'error', file = 'doc', line = 3, message = 'which idea?' },
      { kind = 'model', severity = 'error', file = 'doc', line = 1, message = 'no' } } }))
  ok(wait(3000, function()
    return #vim.diagnostic.get(doc.bufnr('notes'), { namespace = doc.ns }) == 2
  end), 'diagnostics.notes.json -> inline diagnostics on notes')
  ok(#vim.diagnostic.get(doc.bufnr('spec'), { namespace = doc.ns }) == 1, 'spec keeps its own diagnostics')
  ok(vim.fn.getqflist({ title = 0 }).title == 'aic compile notes' and vim.tbl_contains(notes, 'aic compile notes: 2E 0W 0I — notes ok'),
    'notes compile -> quickfix list + summary named after the doc')
  require('aic.status').update()
  ok(aic.status():find('2E', 1, true) ~= nil, 'statusline counts the current doc (notes): ' .. aic.status())
  vim.cmd('AicDoc spec')
  ok(aic.status():find('1E', 1, true) ~= nil, 'statusline follows the doc entered (spec): ' .. aic.status())
  -- submit / ask / range carry the doc
  vim.cmd('AicDoc notes')
  vim.cmd('3,3AicSubmit')
  ok(wait(3000, function()
    return (read(log) or ''):find('render --json --prompt review --doc notes --from 3 --to 3', 1, true) ~= nil
  end), 'range submit in notes -> render --doc notes --from 3 --to 3')
  vim.cmd.edit(vim.fn.fnameescape(root .. '/src/app.js'))
  aic.submit({})
  ok(wait(3000, function()
    return (read(log) or ''):find('render --json --prompt review --doc notes\n', 1, true) ~= nil
  end), 'submit from code uses the last doc entered (notes)')
  vim.cmd.edit(vim.fn.fnameescape(root .. '/src/app.js')) -- (leaves the preview: cancelled)
  aic.show('questions')
  chat.ask('which doc?')
  ok(wait(3000, function()
    return (read(log) or ''):find('ask --doc notes -- which doc?', 1, true) ~= nil
  end), 'questions ask about the last doc (notes)')
  -- picker
  vim.cmd('AicDocs')
  ok(wait(3000, function()
    return vim.api.nvim_get_current_buf() == doc.bufnr('spec')
  end) and (read(log) or ''):find('docs --json', 1, true) ~= nil, ':AicDocs lists docs (aic docs --json) and shows the pick')
  ok(vim.deep_equal(doc.names(), { 'notes', 'plan', 'spec' }), ':AicDoc completes doc names: ' .. table.concat(doc.names(), ' '))
  -- a typed .md is not doubled; a deleted last doc is not recreated by accident
  vim.cmd('AicDocNew scratch.md')
  ok(vim.uv.fs_stat(root .. '/.aic/docs/scratch.md') ~= nil and not vim.uv.fs_stat(root .. '/.aic/docs/scratch.md.md'),
    ':AicDocNew scratch.md makes scratch.md')
  vim.cmd.edit(vim.fn.fnameescape(root .. '/src/app.js'))
  vim.cmd('bwipeout! ' .. doc.bufnr('scratch'))
  os.remove(root .. '/.aic/docs/scratch.md')
  ok(doc.last == 'scratch' and doc.current() == 'spec', 'the last doc deleted: the current doc falls back to spec')
  -- removing a doc's diagnostics file clears them
  os.remove(dir .. '/diagnostics.notes.json')
  ok(wait(3000, function()
    return #vim.diagnostic.get(doc.bufnr('notes'), { namespace = doc.ns }) == 0
  end), 'diagnostics.notes.json removed -> notes diagnostics cleared')
  -- a doc opened through a symlinked path is still a doc
  local link = vim.fn.tempname()
  vim.uv.fs_symlink(root, link)
  vim.cmd.edit(vim.fn.fnameescape(link .. '/.aic/docs/plan.md'))
  ok(doc.name_of(0) == 'plan', 'a doc opened through a symlink is a doc: ' .. tostring(doc.name_of(0)))
  vim.cmd.edit(vim.fn.fnameescape(root .. '/src/app.js'))
  -- the docs dir is created only after `aic docs` had the chance to migrate
  local docs_dir = root .. '/.aic/docs'
  vim.cmd.edit(vim.fn.fnameescape(root .. '/src/app.js'))
  os.rename(docs_dir, docs_dir .. '.away')
  local n_docs = count(read(log), 'docs %-%-json')
  doc.ensure_file('fresh', false)
  ok(count(read(log), 'docs %-%-json') == n_docs + 1 and read(docs_dir .. '/fresh.md') == '# fresh\n\n',
    'first doc in a project: `aic docs` (migration) runs before the docs dir is made')
  vim.fn.delete(docs_dir, 'rf')
  os.rename(docs_dir .. '.away', docs_dir)
end

---------------------------------------------------------------------------
-- :AicFix: proposals in a float, picked like radio buttons, applied in one undo step
do
  local doc = require('aic.doc')
  local fix = require('aic.fix')
  vim.cmd('AicDoc notes')
  local nb = doc.bufnr('notes')
  vim.api.nvim_buf_set_lines(nb, 0, -1, false, { '# notes', '', '- idea @src/app.js:3', '- unsaved' })
  write(dir .. '/diagnostics.notes.json', vim.json.encode({ doc = 'notes', compiled_at = 11, upto_seq = 99, summary = 's',
    items = { { kind = 'incoherent', severity = 'error', file = 'doc', line = 3, message = 'which idea?' },
      { kind = 'model', severity = 'warning', file = 'doc', line = 1, message = 'title?' },
      { kind = 'unspecified', severity = 'info', file = 'src/app.js', line = 9, message = 'cache' } } }))
  ok(wait(3000, function()
    return #vim.diagnostic.get(nb, { namespace = doc.ns }) == 2
  end), 'notes has two doc diagnostics before fixing')
  write(root .. '/fixes.json', vim.json.encode({ doc = 'notes', compiled_at = 11, created_at = 1,
    fixes = {
      { id = 1, item = 1, title = 'Name the idea', why = 'Say which one.', kind = 'incoherent', message = 'which idea?',
        edits = { { old = '- idea @src/app.js:3', new = '- greeting idea @src/app.js:3', line = 3, end_line = 3 } }, conflicts = { 2 } },
      { id = 2, item = 1, title = 'Drop the idea', why = '', edits = { { old = '- idea @src/app.js:3\n', new = '', line = 3, end_line = 3 } },
        conflicts = { 1 } },
      { id = 3, item = 2, title = 'Title it', why = 'w', edits = { { old = '# notes', new = '# Notes: greeting', line = 1, end_line = 1 } },
        conflicts = {} },
      { id = 4, title = 'Stale one', edits = { { old = 'not in the doc', new = 'x', line = 9, end_line = 9 } }, conflicts = {} },
    },
    rejected = { { title = 'Twice', reason = 'its text occurs 2 times in the doc' } },
    skipped = { { item = 3, message = 'cache', reason = 'the worker should drop it' } } }))
  local function fbuf_lines()
    return vim.api.nvim_buf_get_lines(0, 0, -1, false)
  end
  local function line_of(pat)
    for i, l in ipairs(fbuf_lines()) do
      if l:find(pat, 1, true) then
        return i
      end
    end
  end
  local function press_on(pat, key)
    vim.api.nvim_win_set_cursor(0, { line_of(pat), 0 })
    vim.api.nvim_feedkeys(vim.keycode(key), 'x', false)
  end
  vim.cmd('AicFix')
  ok(wait(3000, function()
    return line_of('( ) 1. Name the idea') ~= nil
  end), ':AicFix shows the proposed fixes in a float, none picked')
  local fwin = vim.api.nvim_get_current_win()
  ok(vim.api.nvim_win_get_config(fwin).relative ~= '' and #solid_wins() == 1 and #vim.api.nvim_list_tabpages() == 1,
    'the fix list is a float: one tab, one window')
  ok((read(log) or ''):find('fix --doc notes --json --content-file ', 1, true) ~= nil
    and (read(root .. '/fixcontent.md') or ''):find('- unsaved', 1, true) ~= nil,
    'aic fix gets the unsaved buffer text')
  ok(line_of('stale: its text is no longer in the doc') ~= nil and line_of('✗ unusable: Twice') ~= nil
    and line_of('not a doc fix: #3 cache — the worker should drop it') ~= nil, 'stale, unusable and skipped fixes are listed')
  press_on('1. Name the idea', '<Space>')
  ok(line_of('(•) 1. Name the idea') ~= nil, '<Space> picks a fix')
  press_on('2. Drop the idea', '<Space>')
  ok(line_of('(•) 2. Drop the idea') ~= nil and line_of('( ) 1. Name the idea') ~= nil,
    'picking a fix that changes the same text unpicks the other (radio)')
  press_on('4. Stale one', '<Space>')
  ok(line_of('( ) 4. Stale one') ~= nil, 'a stale fix cannot be picked')
  press_on('3. Title it', '<Tab>')
  ok(line_of('      - # notes') ~= nil and line_of('      + # Notes: greeting') ~= nil, '<Tab> shows the edit as a diff')
  press_on('3. Title it', '<Space>')
  press_on('1. Name the idea', '<Space>')
  ok(line_of('(•) 1.') and line_of('( ) 2.') and line_of('(•) 3.'), 'picked: 1 and 3')
  vim.api.nvim_feedkeys(vim.keycode('<CR>'), 'x', false)
  ok(not vim.api.nvim_win_is_valid(fwin) and vim.api.nvim_get_current_buf() == nb, '<CR> closes the float and shows the doc')
  ok(vim.deep_equal(vim.api.nvim_buf_get_lines(nb, 0, -1, false), { '# Notes: greeting', '', '- greeting idea @src/app.js:3', '- unsaved' })
    and vim.bo[nb].modified, 'the picked fixes are applied to the buffer, unsaved')
  ok(#vim.diagnostic.get(nb, { namespace = doc.ns }) == 0, 'the fixed items are hidden until the next compile')
  single('fix applied')
  vim.cmd('silent undo')
  ok(vim.deep_equal(vim.api.nvim_buf_get_lines(nb, 0, -1, false), { '# notes', '', '- idea @src/app.js:3', '- unsaved' }),
    'one u undoes every applied fix')
  -- :AicFix! shows the last proposal; undone fixes can be picked again; an edit through the last line
  vim.api.nvim_buf_set_lines(nb, 3, 4, false, {})
  vim.cmd('AicFix!')
  ok(line_of('( ) 1. Name the idea') ~= nil and not line_of('(✓)'), ':AicFix! reopens; undone fixes are offered again')
  press_on('2. Drop the idea', '<Space>')
  vim.api.nvim_feedkeys(vim.keycode('<CR>'), 'x', false)
  ok(vim.deep_equal(vim.api.nvim_buf_get_lines(nb, 0, -1, false), { '# notes', '' }), 'an edit through the last line applies')
  vim.cmd('AicFix!')
  ok(line_of('(✓) 2. Drop the idea') ~= nil and line_of('stale: its text is no longer in the doc', 1) ~= nil,
    'applied fixes stay marked; ones it removed the text of are stale')
  vim.cmd('close')
  -- a failure shows in the float
  write(root .. '/fixfail', '')
  vim.cmd('AicFix')
  ok(wait(3000, function()
    return line_of('no diagnostics to fix in notes') ~= nil
  end), 'aic fix failing shows its error in the float')
  vim.cmd('close')
  os.remove(root .. '/fixfail')
  vim.cmd('silent undo')
  vim.cmd('silent write')
  ok(fix.last and fix.last.doc == 'notes', 'the last proposal is kept')
end

-- :AicFix edge cases: the end of the doc, touching edits, applied state
do
  local doc = require('aic.doc')
  local fix = require('aic.fix')
  doc.ensure_file('edge', false)
  vim.cmd('AicDoc edge')
  local eb = doc.bufnr('edge')
  local function lines_now()
    return vim.api.nvim_buf_get_lines(eb, 0, -1, false)
  end
  local function line_of(pat)
    for i, l in ipairs(vim.api.nvim_buf_get_lines(0, 0, -1, false)) do
      if l:find(pat, 1, true) then
        return i
      end
    end
  end
  local n = 0
  -- set the doc, propose `fixes`, pick `picks` and apply
  local function run(lines, fixes, picks)
    vim.cmd('AicDoc edge')
    vim.api.nvim_buf_set_lines(eb, 0, -1, false, lines)
    for i, f in ipairs(fixes) do
      f.id, f.title, f.conflicts = i, f.title or ('fix ' .. i), f.conflicts or {}
      for _, e in ipairs(f.edits) do
        e.line, e.end_line = 1, 1
      end
    end
    n = n + 1
    write(root .. '/fixes.json', vim.json.encode({ doc = 'edge', created_at = n, fixes = fixes, rejected = {}, skipped = {} }))
    vim.cmd('AicFix')
    wait(3000, function()
      return line_of('. fix 1') ~= nil or line_of('. ' .. (fixes[1].title or '')) ~= nil
    end)
    for _, id in ipairs(picks or {}) do
      vim.api.nvim_win_set_cursor(0, { line_of(('%d. '):format(id)), 0 })
      vim.api.nvim_feedkeys(' ', 'x', false)
    end
    if picks then
      vim.api.nvim_feedkeys(vim.keycode('<CR>'), 'x', false)
    end
  end
  run({ '# t', 'items' }, { { edits = { { old = 's\n', new = '' } } } }, { 1 })
  ok(vim.deep_equal(lines_now(), { '# t', 'item' }), 'deleting text that ends at the end of the doc removes only that text: '
    .. table.concat(lines_now(), '|'))
  run({ '# t', 'abc foo' }, { { edits = { { old = 'foo\n', new = '' } } } }, { 1 })
  ok(vim.deep_equal(lines_now(), { '# t', 'abc ' }), '... mid-line too')
  run({ 'prev', 'last' }, { { edits = { { old = '\nlast\n', new = '' } } } }, { 1 })
  ok(vim.deep_equal(lines_now(), { 'prev' }), '... and starting with a newline')
  run({ '- a', '- b', '- c' }, { { edits = { { old = '- b\n', new = '- B\n' } } }, { edits = { { old = '- c\n', new = '' } } } }, { 1, 2 })
  ok(vim.deep_equal(lines_now(), { '- a', '- B' }), 'touching edits of two fixes both apply')
  vim.cmd('silent undo')
  ok(vim.deep_equal(lines_now(), { '- a', '- b', '- c' }), '... and one u undoes both')
  -- a fix edited after it was applied stays applied (never applied twice)
  run({ '# t', '- retry' }, { { title = 'Count', edits = { { old = '- retry', new = '- retry 3 times' } } } }, { 1 })
  vim.api.nvim_buf_set_lines(eb, 1, 2, false, { '- retry 4 times' })
  vim.cmd('AicFix!')
  ok(line_of('(✓) 1. Count') ~= nil, 'a fix edited after applying stays applied')
  vim.cmd('close')
  -- undo it, redo it: applied again (never offered twice)
  vim.cmd('silent undo')
  vim.cmd('silent undo')
  vim.cmd('silent redo')
  vim.cmd('silent redo')
  vim.cmd('AicFix!')
  ok(line_of('(✓) 1. Count') ~= nil and lines_now()[2] == '- retry 4 times', 'undo + redo: the fix is applied again')
  vim.cmd('close')
  -- undone, then edited elsewhere: offered again
  vim.cmd('silent undo')
  vim.cmd('silent undo')
  vim.api.nvim_buf_set_lines(eb, 0, 1, false, { '# title' })
  vim.cmd('AicFix!')
  ok(line_of('( ) 1. Count') ~= nil, 'undone and then edited elsewhere: offered again')
  vim.cmd('close')
  -- the apply trimmed from the undo history: still applied
  run({ '# t', '- retry' }, { { title = 'Count', edits = { { old = '- retry', new = '- retry 3 times' } } } }, { 1 })
  vim.bo[eb].undolevels = 1
  for _, t in ipairs({ '# t2', '# t3', '- retry 4 times' }) do
    vim.cmd('let &g:undolevels = &g:undolevels')
    vim.api.nvim_buf_set_lines(eb, t:sub(1, 1) == '#' and 0 or 1, t:sub(1, 1) == '#' and 1 or 2, false, { t })
  end
  vim.cmd('AicFix!')
  ok(line_of('(✓) 1. Count') ~= nil, 'an apply trimmed from the undo history still counts as applied')
  vim.cmd('close')
  vim.bo[eb].undolevels = -123456
  -- an applied insertion is "already in the doc" for a fresh copy of the proposal (e.g. after a restart)
  run({ '# t', 'L1', 'end' }, {
    { title = 'Insert', edits = { { old = 'L1\n', new = 'L1\nNEW\n' } }, conflicts = { 2 } },
    { title = 'Change', edits = { { old = 'L1\n', new = 'L1 changed\n' } }, conflicts = { 1 } },
  }, { 1 })
  ok(vim.deep_equal(lines_now(), { '# t', 'L1', 'NEW', 'end' }), 'insertion applied')
  vim.cmd('AicFix!')
  ok(line_of('( ) 2. Change  doc:2  its alternative 1 is applied') ~= nil, 'the alternative of an applied fix is blocked')
  vim.api.nvim_feedkeys(vim.keycode('a<CR>'), 'x', false)
  ok(vim.deep_equal(lines_now(), { '# t', 'L1', 'NEW', 'end' }), 'pick-all + apply cannot apply it')
  vim.cmd('silent! close')
  fix.by_doc.edge = nil
  write(dir .. '/fixes.edge.json', read(root .. '/fixes.json')) -- (what the real backend writes)
  vim.cmd('AicFix!')
  ok(line_of('( ) 1. Insert  doc:2  its text is already in the doc') ~= nil, 'reloaded: an applied insertion is already in the doc')
  vim.cmd('close')
  -- a new text that also appears elsewhere in the doc is not "already in the doc"
  run({ '```python', 'a', '```', '```pyhton', 'b', '```' }, { { title = 'Typo', edits = { { old = '```pyhton', new = '```python' } } } })
  ok(line_of('( ) 1. Typo  doc:4') ~= nil and not line_of('already in the doc'), 'a typo fix to a word used elsewhere can be picked')
  vim.cmd('close')
  -- multi-line text from the compiler renders on one line
  run({ '# t', 'x' }, { { title = 'Two\nlines', message = 'a\nb', kind = 'model', edits = { { old = 'x', new = 'y' } } } })
  ok(line_of('( ) 1. Two lines') ~= nil and line_of('[model] a b') ~= nil, 'newlines in titles/messages do not break the float')
  vim.cmd('close')
  vim.cmd('AicDoc edge')
  vim.cmd('silent write')
end

---------------------------------------------------------------------------
-- sweep: no command or key ever adds a tab, a split or the quickfix window,
-- and no view pins its window
do
  vim.cmd('silent! only')
  vim.cmd('silent! tabonly')
  vim.cmd('cclose')
  vim.cmd.edit(vim.fn.fnameescape(root .. '/src/app.js'))
  vim.ui.select = function(items, _, cb)
    cb(items[1])
  end
  vim.ui.input = function(_, cb)
    cb('sweep question')
  end
  local function qf_open()
    for _, w in ipairs(vim.api.nvim_tabpage_list_wins(0)) do
      if vim.bo[vim.api.nvim_win_get_buf(w)].buftype == 'quickfix' then
        return true
      end
    end
    return false
  end
  local function close_floats()
    for _, w in ipairs(vim.api.nvim_tabpage_list_wins(0)) do
      if vim.api.nvim_win_get_config(w).relative ~= '' then
        pcall(vim.api.nvim_win_close, w, true)
      end
    end
  end
  local steps = { 'Aic', 'AicHistory', 'AicQuestions', 'AicDoc', 'AicToggle', 'AicToggle', 'AicDiagnostics',
    'AicNextUnseen', 'AicPrevUnseen', 'AicAttach', 'AicUsage', 'AicBlame', 'AicLastAnswer', 'AicAsk', 'AicWhatIf',
    'AicRefresh', 'AicCompile', 'AicHold', 'AicContinue', 'AicFix', 'AicFix!' }
  local keys = { 'z', 't', 'q', 'd', 'e', 'u', 'U', 'b', 'l', '?', 'w', 'h', 'c', 'g', ']', '[', 'a', 's', 'S', 'p', 'f' }
  for _, c in ipairs(steps) do
    pcall(vim.cmd, c)
    vim.wait(50)
    close_floats()
    ok(#vim.api.nvim_list_tabpages() == 1 and #solid_wins() == 1 and not qf_open(), ':' .. c .. ' keeps one tab, one window')
    ok(not vim.wo.winfixbuf, ':' .. c .. ' leaves the window unpinned')
  end
  for _, k in ipairs(keys) do
    pcall(vim.api.nvim_feedkeys, vim.keycode('<leader>z' .. k), 'x', false)
    vim.wait(50)
    close_floats()
    ok(#vim.api.nvim_list_tabpages() == 1 and #solid_wins() == 1 and not qf_open(), '<prefix>' .. k .. ' keeps one tab, one window')
  end
  -- views never land in special windows (quickfix, help)
  vim.cmd.edit(vim.fn.fnameescape(root .. '/src/app.js'))
  local code_win = vim.api.nvim_get_current_win()
  vim.cmd('copen')
  local qf_win = vim.api.nvim_get_current_win()
  aic.show('trace')
  ok(vim.api.nvim_get_current_win() == code_win and vim.api.nvim_get_current_buf() == trace.buf
    and vim.bo[vim.api.nvim_win_get_buf(qf_win)].buftype == 'quickfix', 'a view from the quickfix window shows in the code window')
  vim.cmd('cclose')
  -- a float that is not aic's is never closed; aic's own are
  vim.cmd.edit(vim.fn.fnameescape(root .. '/src/app.js'))
  local fb = vim.api.nvim_create_buf(false, true)
  local fw = vim.api.nvim_open_win(fb, true, { relative = 'editor', row = 1, col = 1, width = 20, height = 3 })
  aic.show('questions')
  ok(vim.api.nvim_win_is_valid(fw) and vim.api.nvim_get_current_buf() == require('aic.chat').buf
    and vim.api.nvim_get_current_win() ~= fw, 'a foreign float stays open; the view shows under it')
  vim.api.nvim_win_close(fw, true)
  local _, aw = require('aic.util').float({ 'x' }, {})
  aic.show('trace')
  ok(not vim.api.nvim_win_is_valid(aw) and vim.api.nvim_get_current_buf() == trace.buf, "aic's own float closes when a view is shown from it")
  -- 'nohidden' + unsaved spec: the refused switch is notified, not raised
  aic.show('doc')
  vim.o.hidden = false
  vim.api.nvim_buf_set_lines(0, -1, -1, false, { 'unsaved' })
  local n0 = #notes
  local okk = pcall(aic.show, 'trace')
  ok(okk and #notes > n0 and notes[#notes]:find('E37', 1, true) ~= nil and vim.api.nvim_get_current_buf() == spec.bufnr(),
    "'nohidden': switching away from an unsaved spec is notified (E37), not raised")
  vim.api.nvim_buf_set_lines(0, -2, -1, false, {})
  vim.bo.modified = false
  vim.o.hidden = true
  for _, name in ipairs({ 'trace', 'questions', 'doc' }) do
    vim.cmd.edit(vim.fn.fnameescape(root .. '/src/app.js'))
    aic.show(name)
    local vb = vim.api.nvim_get_current_buf()
    vim.cmd([[execute "normal! \<C-^>"]])
    ok(vim.api.nvim_get_current_buf() ~= vb and vim.api.nvim_buf_get_name(0) == root .. '/src/app.js',
      '<C-^> works from the ' .. name .. ' view')
  end
end

---------------------------------------------------------------------------
-- `aic watch` per project root; all stopped on exit
do
  local backend = require('aic.backend')
  local root2 = vim.fn.resolve(vim.fn.tempname())
  vim.fn.mkdir(root2 .. '/.aic', 'p')
  ok(backend.watch_jobs[root] ~= nil, 'watcher running for the first root')
  backend.ensure_watch(root2)
  local j1, j2 = backend.watch_jobs[root], backend.watch_jobs[root2]
  ok(j2 ~= nil and j1 ~= nil and j1 ~= j2, 'a second root gets its own watcher')
  backend.ensure_watch(root2)
  ok(backend.watch_jobs[root2] == j2, 'ensure_watch is idempotent per root')
  vim.api.nvim_exec_autocmds('VimLeavePre', {})
  ok(next(backend.watch_jobs) == nil and wait(3000, function()
    return j1:is_closing() and j2:is_closing()
  end), 'VimLeavePre stops every watcher')
end

io.stdout:write(('\n%d failure(s)\n'):format(failures))
if failures > 0 then
  vim.cmd('cquit 1')
else
  vim.cmd('qa!')
end
