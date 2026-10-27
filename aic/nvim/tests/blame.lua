-- AI blame (lua/aic/blame.lua) against a fake aigource server
-- (tests/fake_aigource.py, aigource/README.md's protocol). Run from aic/nvim:
--   nvim --headless -u NONE -l tests/blame.lua
local plugin_dir = vim.fn.fnamemodify(debug.getinfo(1, 'S').source:sub(2), ':p:h:h')
vim.opt.rtp:prepend(plugin_dir)
local gitsigns_dir = vim.fn.expand('~/.local/share/nvim/lazy/gitsigns.nvim')
local has_gitsigns = vim.fn.isdirectory(gitsigns_dir) == 1
if has_gitsigns then
  vim.opt.rtp:append(gitsigns_dir) -- its formatter, not its setup
end
vim.o.swapfile = false
vim.o.shadafile = 'NONE'
vim.o.hidden = true
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
local notes = {}
vim.notify = function(msg)
  table.insert(notes, msg)
end

---------------------------------------------------------------------------
-- two repos, a stub `aigource` that records its argv (and serves with the fake)
local tmp = vim.fn.resolve(vim.fn.tempname())
local fdir = tmp .. '/fake'
vim.fn.mkdir(fdir, 'p')
local fake = plugin_dir .. '/tests/fake_aigource.py'
local stub = tmp .. '/aigource'
write(stub, ('#!/bin/sh\necho "$*" >> "$AIG_FAKE_DIR/argv"\n[ "$1" = --serve ] && exec python3 %s "$@"\nexit 0\n'):format(fake))
vim.fn.setfperm(stub, 'rwxr-xr-x')
vim.env.AIG_SOCK_NS = 'aicblame' .. vim.uv.os_getpid()
vim.env.AIG_FAKE_DIR = fdir
vim.env.AIG_SERVE_IDLE = '3'

-- Claude Code's project dir name for a root (aigource: ingest.c slug)
local cfg = tmp .. '/cfg'
vim.env.CLAUDE_CONFIG_DIR = cfg
local function slug(root)
  return (root:gsub('[^A-Za-z0-9]', '-'))
end
local function sessions(root, suffix, cwd)
  local d = cfg .. '/projects/' .. slug(root) .. (suffix or '')
  vim.fn.mkdir(d, 'p')
  if cwd then
    write(d .. '/s1.jsonl', vim.json.encode({ type = 'user', cwd = cwd }) .. '\n')
  end
end

local function repo(name, files, untracked, no_sessions)
  local r = tmp .. '/' .. name
  if not no_sessions then
    sessions(r)
  end
  for f, t in pairs(files) do
    write(r .. '/' .. f, t)
  end
  local function git(...)
    return vim.system({ 'git', '-c', 'user.name=t', '-c', 'user.email=t@t', '-c', 'commit.gpgsign=false', ... },
      { cwd = r, text = true }):wait()
  end
  git('init', '-q')
  git('add', '-A')
  git('commit', '-qm', 'init')
  for f, t in pairs(untracked or {}) do
    write(r .. '/' .. f, t)
  end
  return r
end
local function lines(n, p)
  local t = {}
  for i = 1, n do
    t[i] = p .. i
  end
  return table.concat(t, '\n') .. '\n'
end
local rA = repo('a', { ['a.lua'] = lines(10, 'a'), ['README'] = 'hi\n', ['sub/c.lua'] = lines(4, 'c'), ['plain.lua'] = lines(3, 'p') },
  { ['new.lua'] = lines(2, 'n') })
local rB = repo('b', { ['b.lua'] = lines(3, 'b') })

local now = os.time()
local K1, K2, K3 = '1700000000000000\31Add the greeting', '1700000100000000\31Refactor the loop', '1700000200000000\31B things'
local turns = {
  [K1] = { slot = 2, prompt = 'Add the greeting', ts = now - 200 },
  [K2] = { slot = 5, prompt = 'Refactor the loop', ts = now - 4000 },
}
local changes = {
  ['0'] = { turn_key = K1, tool = 'Edit', agent = 'main session', ts = now - 120 },
  ['1'] = { turn_key = K2, tool = 'Write', agent = 'Explore · find x', ts = now - 3600 },
}
local spec = {
  [rA] = {
    rev = 1,
    files = {
      ['a.lua'] = { text = lines(10, 'a'), runs = { { 2, -1 }, { 3, 0 }, { 1, -1 }, { 2, 1 }, { 2, -1 } }, changes = changes, turns = turns },
      ['new.lua'] = { text = lines(2, 'n'), runs = { { 1, 0 }, { 1, -1 } }, changes = { ['0'] = changes['0'] }, turns = { [K1] = turns[K1] } },
      ['sub/c.lua'] = { text = lines(4, 'c'), runs = { { 1, -1 }, { 3, 1 } }, changes = { ['1'] = changes['1'] }, turns = { [K2] = turns[K2] } },
      ['plain.lua'] = { text = lines(3, 'p'), runs = { { 3, -1 } }, changes = {}, turns = {} },
    },
    turn_lines = {
      [K2] = { { file = 'a.lua', runs = { { 6, -1 }, { 2, 1 }, { 2, -1 } } }, { file = 'sub/c.lua', runs = { { 1, -1 }, { 3, 4 } } } },
    },
  },
  [rB] = {
    rev = 7,
    files = {
      ['b.lua'] = { text = lines(3, 'b'), runs = { { 1, 3 } }, changes = { ['3'] = { turn_key = K3, tool = 'Edit', agent = 's', ts = now } },
        turns = { [K3] = { slot = 7, prompt = 'B things', ts = now } } },
    },
  },
}
local function put_spec()
  write(fdir .. '/spec.json', vim.json.encode(spec))
end
put_spec()

local function log()
  local out = {}
  local whole = (read(fdir .. '/log.jsonl') or ''):match('^(.*\n)') or '' -- not a line being written
  for _, l in ipairs(vim.split(whole, '\n', { trimempty = true })) do
    out[#out + 1] = vim.json.decode(l)
  end
  return out
end
local function argv()
  return vim.split(read(fdir .. '/argv') or '', '\n', { trimempty = true })
end
-- the pid of the fake that last answered a request for `root`
local function server_of(root)
  local pid
  for _, e in ipairs(log()) do
    if e.root == root then
      pid = e.pid
    end
  end
  return pid
end
local function ctl(pid, cmd)
  local f = assert(io.open(fdir .. '/ctl.' .. pid, 'a'))
  f:write(vim.json.encode(cmd) .. '\n')
  f:close()
end
local function reqs(pid, cmd)
  return vim.tbl_filter(function(e)
    return e.pid == pid and e.req.cmd == cmd
  end, log())
end
local manual = {} -- fakes started by the test itself
local function start_fake(root)
  table.insert(manual, vim.system({ 'python3', fake, '--serve', '-r', root }))
end

local blame = require('aic.blame')
-- buffer line -> its number highlight
local function marks(buf)
  local m = {}
  for _, e in ipairs(vim.api.nvim_buf_get_extmarks(buf, blame.ns, 0, -1, { details = true })) do
    m[e[2] + 1] = e[4].number_hl_group
  end
  return m
end
-- (extmarks move with the text: clear them so only a new draw counts)
local function marks_are(buf, want, keep)
  if not keep then
    vim.api.nvim_buf_clear_namespace(buf, blame.ns, 0, -1)
  end
  return wait(3000, function()
    return vim.deep_equal(marks(buf), want)
  end)
end
local function keys(k)
  vim.api.nvim_feedkeys(vim.keycode(k), 'x', false)
end

---------------------------------------------------------------------------
require('aic').setup({ aigource = stub })

-- the first buffer of a root: refused, the server spawned, its blame drawn
vim.cmd.edit(rA .. '/a.lua')
local abuf = vim.api.nvim_get_current_buf()
local T3, T6 = 'AicTurn3', 'AicTurn6'
ok(marks_are(abuf, { [3] = T3, [4] = T3, [5] = T3, [7] = T6, [8] = T6 }), 'number highlights on the AI lines, by slot: '
  .. vim.inspect(marks(abuf)))
ok(argv()[1] == '--serve -r ' .. rA, 'refused: `aigource --serve -r <root>` spawned: ' .. vim.inspect(argv()))
ok(vim.api.nvim_get_hl(0, { name = 'AicTurn1' }).fg == 0xf2a541 and vim.api.nvim_get_hl(0, { name = 'AicTurn8' }).fg == 0xffa198,
  "AicTurn1..8: aigource's palette")
local cA = blame.clients[rA]
local hex = vim.fn.system({ 'sha1sum' }, rA):sub(1, 16)
ok(cA and cA.name == '\0' .. vim.env.AIG_SOCK_NS .. '/' .. hex, 'socket: \\0<AIG_SOCK_NS>/<sha1(root)[:16]>')
local pidA = server_of(rA)
ok(#reqs(pidA, 'subscribe') == 1, 'subscribed once')

-- unsaved edits shift the colours through vim.diff against `text`
vim.api.nvim_buf_set_lines(abuf, 0, 0, false, { 'new1', 'new2' })
vim.api.nvim_buf_set_lines(abuf, 5, 6, false, { 'edited' })
ok(marks_are(abuf, { [5] = T3, [7] = T3, [9] = T6, [10] = T6 }), 'unsaved edit: added lines none, the rest shifted: '
  .. vim.inspect(marks(abuf)))

-- gitsigns' formatter
local util = require('aic.util')
local f = blame.format('t', { final_lnum = 5, author = 't', author_time = now, summary = 's' })
ok(vim.deep_equal(f, { { '◆ ', T3 }, { 'Add the greeting · ' .. util.ago(now - 120) .. ' ago', 'GitSignsCurrentLineBlame' } }),
  'format: an AI line gives the turn chunks: ' .. vim.inspect(f))
local info = { final_lnum = 1, author = 't', author_time = now - 3600, summary = 'init commit', abbrev_sha = 'abc1234' }
f = blame.format('t', info)
if has_gitsigns then
  local want = require('gitsigns.blame_formatter').expand_string(require('gitsigns.config').schema.current_line_blame_formatter.default,
    't', info, { self_author_text = 'You' })
  ok(vim.deep_equal(f, { { want, 'GitSignsCurrentLineBlame' } }), "format: another line gives gitsigns' default: " .. vim.inspect(f))
  local nc = blame.format('t', { final_lnum = 6, author = 'Not Committed Yet' })
  ok(vim.deep_equal(nc, { { ' Not Committed Yet', 'GitSignsCurrentLineBlame' } }), 'format: an edited line, not committed: _nc default: '
    .. vim.inspect(nc))
end
local saved_bf = package.loaded['gitsigns.blame_formatter']
package.loaded['gitsigns.blame_formatter'] = {} -- moved
f = blame.format('t', info)
package.loaded['gitsigns.blame_formatter'] = saved_bf
ok(vim.deep_equal(f, { { ' You, ' .. util.ago(now - 3600) .. ' ago - init commit ', 'GitSignsCurrentLineBlame' } }),
  'format: without the expander, author, ago - summary: ' .. vim.inspect(f))

-- ]w / [w: starts of AI runs
vim.api.nvim_win_set_cursor(0, { 1, 0 })
local seen = {}
for _ = 1, 4 do
  keys(']w')
  seen[#seen + 1] = vim.fn.line('.')
end
keys('[w')
seen[#seen + 1] = vim.fn.line('.')
ok(vim.deep_equal(seen, { 5, 7, 9, 9, 7 }), ']w ]w ]w ]w [w: ' .. vim.inspect(seen))
ok(vim.tbl_contains(notes, 'aic: no next AI run'), 'past the last run: notified')
ok(blame.jump(-1) == 5, '[w -> 5')

-- detail float: `detail` for the base line; a stale rev refetches and asks again
spec[rA].files['a.lua'].detail = { ['7'] = { turn_key = K2, tool = 'Write', agent = 'Explore · find x', ts = now - 3600,
  prompt = 'Refactor the loop\nkeep the API', comment = 'Split the loop in two.', truncated = true,
  prompt_truncated = true, comment_truncated = true,
  hunks = { { old_start = 7, old_lines = 1, new_start = 7, new_lines = 2, lines = { '-old', '+a7', '+a8' } } } } }
spec[rA].rev = 2 -- no push: the buffer's rev 1 is stale
put_spec()
vim.api.nvim_win_set_cursor(0, { 9, 0 })
keys('<leader>zb')
local fl
ok(wait(3000, function()
  fl = vim.api.nvim_buf_get_lines(0, 0, -1, false)
  return vim.api.nvim_win_get_config(0).relative ~= ''
end), '<prefix>b: a float')
ok(vim.deep_equal({ unpack(fl, 1, 3) }, { '# Refactor the loop', 'keep the API', '(truncated)' })
  and table.concat(fl, '\n'):find('Split the loop in two.\n(truncated)', 1, true) and vim.tbl_contains(fl, '- tool: Write')
  and vim.tbl_contains(fl, '- agent: Explore · find x') and vim.tbl_contains(fl, '- line 9 (line 7 as aigource read it) · change 1 at rev 2')
  and vim.tbl_contains(fl, 'Split the loop in two.') and table.concat(fl, '\n'):find('```diff\n@@ -7,1 +7,2 @@\n-old\n+a7\n+a8\n```\n(truncated)', 1, true),
  'detail float: full prompt, comment, diff: ' .. vim.inspect(fl))
local dr = vim.tbl_map(function(e)
  return { e.req.cmd, e.req.rev, e.req.line, e.req.file }
end, vim.tbl_filter(function(e)
  return e.req.cmd == 'detail'
end, log()))
ok(vim.deep_equal(dr, { { 'detail', 1, 7, 'a.lua' }, { 'detail', 2, 7, 'a.lua' } }), 'stale: blame again, detail asked once more: ' .. vim.inspect(dr))
vim.api.nvim_win_close(0, true)

-- quickfix of the turn's lines
keys('<leader>zr')
ok(wait(2000, function()
  return vim.fn.getqflist({ title = 0 }).title == 'aic turn: Refactor the loop'
end), '<prefix>r: quickfix list of turn_lines')
local qf = vim.tbl_map(function(it)
  return { vim.api.nvim_buf_get_name(it.bufnr), it.lnum, it.end_lnum, it.text }
end, vim.fn.getqflist())
ok(vim.deep_equal(qf, { { rA .. '/a.lua', 7, 8, 'Refactor the loop' }, { rA .. '/sub/c.lua', 2, 4, 'Refactor the loop' } }),
  'quickfix items: ' .. vim.inspect(qf))
ok(#vim.api.nvim_tabpage_list_wins(0) == 1, 'no quickfix window opened')

-- untracked: our own cursor-line text; tracked: gitsigns' (none of ours)
local function line_text(buf)
  local m = vim.api.nvim_buf_get_extmarks(buf, blame.line_ns, 0, -1, { details = true })
  return m[1] and { m[1][2] + 1, m[1][4].virt_text }
end
vim.api.nvim_exec_autocmds('CursorMoved', { buffer = abuf })
ok(line_text(abuf) == nil, 'tracked buffer: no cursor-line text of ours')
vim.cmd.edit(rA .. '/new.lua')
local nbuf = vim.api.nvim_get_current_buf()
vim.api.nvim_win_set_cursor(0, { 1, 0 })
ok(wait(3000, function()
  return line_text(nbuf) ~= nil
end), 'untracked buffer: cursor-line text drawn')
local lt = line_text(nbuf)
ok(lt and lt[1] == 1 and vim.deep_equal(lt[2], blame.format('t', { final_lnum = 1 })), 'its chunks are the formatter\'s: ' .. vim.inspect(lt))
vim.api.nvim_win_set_cursor(0, { 2, 0 })
vim.api.nvim_exec_autocmds('CursorMoved', { buffer = nbuf })
ok(line_text(nbuf) == nil, 'a line no AI wrote: none')
vim.api.nvim_win_set_cursor(0, { 1, 0 })
vim.system({ 'git', 'add', 'new.lua' }, { cwd = rA }):wait()
vim.api.nvim_exec_autocmds('User', { pattern = 'GitSignsUpdate', data = { buffer = nbuf } })
ok(wait(2000, function()
  return blame.bufs[nbuf].untracked == false and line_text(nbuf) == nil
end), 'git add, then GitSignsUpdate: tracked now, gitsigns draws it, not us')
vim.system({ 'git', 'rm', '-q', '--cached', 'new.lua' }, { cwd = rA }):wait()
vim.cmd('silent write')
ok(wait(2000, function()
  return blame.bufs[nbuf].untracked == true and line_text(nbuf) ~= nil
end), 'untracked again: noticed on BufWritePost')

-- a file with no AI line: no base kept, nothing mapped while typing
vim.cmd.edit(rA .. '/plain.lua')
local pbuf = vim.api.nvim_get_current_buf()
ok(wait(2000, function()
  return blame.bufs[pbuf].status == 'ok'
end) and blame.bufs[pbuf].base_text == nil, 'no AI lines: no base text')
vim.api.nvim_buf_set_lines(pbuf, 0, 0, false, { 'x' })
vim.wait(300)
ok(blame.bufs[pbuf].cid == nil and blame.bufs[pbuf].map == nil, '...and no mapping after an edit')
vim.cmd('silent edit!')

-- a link into the repo: the repo's file, its path below the real root
vim.fn.mkdir(tmp .. '/links', 'p')
vim.uv.fs_symlink(rA .. '/sub/c.lua', tmp .. '/links/c.lua')
vim.cmd.edit(tmp .. '/links/c.lua')
local lbuf = vim.api.nvim_get_current_buf()
ok(marks_are(lbuf, { [2] = T6, [3] = T6, [4] = T6 }), 'symlinked file: blamed through its target')
ok(blame.bufs[lbuf].root == rA and blame.bufs[lbuf].rel == 'sub/c.lua' and log()[#log()].req.file == 'sub/c.lua',
  'asked as sub/c.lua of the real root')

-- none: no colours, ]w says so
vim.cmd.edit(rA .. '/README')
local rbuf = vim.api.nvim_get_current_buf()
ok(wait(2000, function()
  return blame.bufs[rbuf] and blame.bufs[rbuf].status == 'none'
end) and next(marks(rbuf)) == nil, 'none: no highlights')
blame.jump(1)
ok(notes[#notes] == 'aic: no AI blame here', ']w where there is no blame: notified')

-- back to a.lua: :e! drops the edits (reload maps again), :w sends saved
vim.cmd.buffer(abuf)
vim.cmd('edit!')
ok(marks_are(abuf, { [3] = T3, [4] = T3, [5] = T3, [7] = T6, [8] = T6 }), ':e!: colours back on the file\'s lines')
vim.cmd('silent write')
ok(wait(2000, function()
  return #vim.tbl_filter(function(e)
    return e.req.file == 'a.lua'
  end, reqs(pidA, 'saved')) == 1
end), 'BufWritePost: saved {file}')

-- undograft: an RPC edit written with noautocmd (no BufWritePost), then the
-- server's changed push: the new base replaces the old, the mapping follows
local gs_updates = 0
package.loaded['gitsigns.current_line_blame'] = { update = function()
  gs_updates = gs_updates + 1
end }
vim.api.nvim_buf_set_lines(abuf, 0, 0, false, { 'ai1' })
vim.cmd('silent noautocmd write!')
ok(marks_are(abuf, { [4] = T3, [5] = T3, [6] = T3, [8] = T6, [9] = T6 }), 'the AI edit, before the push: shifted against the old base')
spec[rA].rev = 3
spec[rA].files['a.lua'].text = read(rA .. '/a.lua')
spec[rA].files['a.lua'].runs = { { 1, 0 }, { 2, -1 }, { 3, 0 }, { 1, -1 }, { 2, 1 }, { 2, -1 } }
put_spec()
ctl(pidA, { push = { 'a.lua' } })
ok(marks_are(abuf, { [1] = T3, [4] = T3, [5] = T3, [6] = T3, [8] = T6, [9] = T6 }), 'changed push: refetched, the AI\'s new line colored')
ok(blame.bufs[abuf].rev == 3 and blame.bufs[abuf].base_text == read(rA .. '/a.lua'), 'the base is the new text, at rev 3')
ok(#vim.tbl_filter(function(e)
  return e.req.file == 'a.lua'
end, reqs(pidA, 'saved')) == 1, 'noautocmd write: no saved sent')
ok(gs_updates > 0, "gitsigns' cursor-line blame refreshed after the push")
package.loaded['gitsigns.current_line_blame'] = nil

-- building: nothing yet; the first build's push brings it
spec[rA].building = true
put_spec()
local nb = #reqs(pidA, 'blame')
vim.cmd('edit!')
local asked = wait(2000, function()
  return #reqs(pidA, 'blame') == nb + 1
end)
vim.wait(200) -- its reply in
ok(asked and next(marks(abuf)) == nil and blame.bufs[abuf].base_text == nil, 'building: no colours')
spec[rA].building = nil
put_spec()
ctl(pidA, { push = { 'a.lua', 'new.lua' } })
ok(marks_are(abuf, { [1] = T3, [4] = T3, [5] = T3, [6] = T3, [8] = T6, [9] = T6 }), 'after building: the push draws it')

-- a second root: its own client and server
vim.cmd.edit(rB .. '/b.lua')
local bbuf = vim.api.nvim_get_current_buf()
ok(marks_are(bbuf, { [1] = 'AicTurn8' }), 'second root: its blame')
local cB = blame.clients[rB]
ok(cB and cA ~= cB and cA.name ~= cB.name and vim.tbl_count(blame.clients) == 2, 'two roots, two clients')
ok(argv()[2] == '--serve -r ' .. rB and server_of(rB) ~= pidA, 'a server spawned for the second root')
vim.cmd.buffer(abuf)

-- handover: wait for the next host, never spawn
local nspawn = #argv()
ctl(pidA, { handover = true })
ok(wait(2000, function()
  return cA.pipe == nil and cA.wait_until ~= nil
end), 'handover: disconnected, waiting')
vim.wait(1500)
ok(#argv() == nspawn and cA.pipe == nil, 'handover: 1.5 s later still waiting, nothing spawned')
local nnc = #notes
vim.api.nvim_win_set_cursor(0, { 5, 0 })
keys('<leader>zr')
ok(#notes == nnc + 1 and notes[#notes] == 'aic: blame server not connected', '<prefix>r with no connection: notified')
start_fake(rA)
local pidA2
ok(wait(5000, function()
  pidA2 = server_of(rA)
  return pidA2 ~= pidA and #reqs(pidA2, 'subscribe') == 1 and #reqs(pidA2, 'blame') >= 1
end), 'the new host: subscribed, blame asked again')
ok(#argv() == nspawn, 'handover: no spawn')
ok(marks_are(abuf, { [1] = T3, [4] = T3, [5] = T3, [6] = T3, [8] = T6, [9] = T6 }, true), 'colours from the new host')

-- EOF (no handover): spawn again; a request it never answered is notified
ctl(pidA2, { mute = true })
vim.wait(200)
vim.api.nvim_win_set_cursor(0, { 5, 0 })
keys('<leader>zr')
local nmute = #notes
ctl(pidA2, { eof = true })
ok(wait(3000, function()
  return #notes > nmute and notes[nmute + 1] == 'aic: blame server not connected (it went away); try again'
end), 'a request the server dropped: notified')
local pidA3
ok(wait(5000, function()
  pidA3 = server_of(rA)
  return #argv() == nspawn + 1 and pidA3 ~= pidA2 and #reqs(pidA3, 'blame') >= 1
end), 'EOF: spawned again, reconnected: ' .. vim.inspect(argv()))
ok(argv()[nspawn + 1] == '--serve -r ' .. rA, 'EOF spawn argv')

-- focus: a headless host starts the GUI, which gets the focus once it hosts
vim.api.nvim_win_set_cursor(0, { 5, 0 })
keys('<leader>zo')
ok(wait(3000, function()
  return #argv() == nspawn + 2
end) and argv()[nspawn + 2] == '-a -r ' .. rA .. ' --structure icicle::a.lua', 'headless focus: GUI spawned: '
  .. vim.inspect(argv()[nspawn + 2]))
ok(#reqs(pidA3, 'focus') == 1 and reqs(pidA3, 'focus')[1].req.turn_key == K1, 'focus sent with the turn key')
spec[rA].host = 'gui'
put_spec()
ctl(pidA3, { handover = true })
vim.wait(300)
start_fake(rA)
local pidA4
ok(wait(5000, function()
  pidA4 = server_of(rA)
  return pidA4 ~= pidA3 and #reqs(pidA4, 'focus') == 1
end), 'the GUI host: focus sent again')
local fr = pidA4 and reqs(pidA4, 'focus')[1]
ok(fr and fr.req.turn_key == K1 and fr.req.file == 'a.lua' and fr.req.line == 5, 'refocus: turn, file, base line: '
  .. vim.inspect(fr and fr.req))

-- blame_max_lines: colours dropped (one notice), back under the limit
local opts = require('aic.config').options
opts.blame_max_lines = 5
local nbig = #notes
vim.api.nvim_buf_set_lines(abuf, 0, 0, false, { 'x' })
ok(wait(2000, function()
  return next(marks(abuf)) == nil and blame.bufs[abuf].cid == nil
end), 'over blame_max_lines: no colours, nothing mapped')
vim.api.nvim_buf_set_lines(abuf, 0, 1, false, {})
vim.wait(300)
local bigs = vim.tbl_filter(function(m)
  return m:find('blame_max_lines', 1, true) ~= nil
end, { unpack(notes, nbig + 1) })
ok(#bigs == 1, 'one notice: ' .. vim.inspect(bigs))
opts.blame_max_lines = 50000
vim.api.nvim_buf_set_lines(abuf, 0, 0, false, { 'x' })
vim.api.nvim_buf_set_lines(abuf, 0, 1, false, {})
ok(marks_are(abuf, { [1] = T3, [4] = T3, [5] = T3, [6] = T3, [8] = T6, [9] = T6 }), 'under the limit again: colours back')

-- keymaps = false: no ]w
opts.keymaps = false
spec[rB].files['k.lua'] = spec[rB].files['b.lua']
put_spec()
write(rB .. '/k.lua', lines(3, 'b'))
vim.cmd.edit(rB .. '/k.lua')
local kbuf = vim.api.nvim_get_current_buf()
ok(marks_are(kbuf, { [1] = 'AicTurn8' }) and vim.fn.maparg(']w', 'n') == '', 'keymaps = false: blamed, no ]w')
opts.keymaps = true

-- no transcripts: no server, until a session appears (asked again on BufEnter)
local nsp = #argv()
local rE = repo('e', { ['e.lua'] = 'e\n' }, nil, true)
sessions(rE, '-other', rE .. '-other/x') -- a sibling's dir: its cwd isn't below rE
spec[rE] = { rev = 1, files = { ['e.lua'] = { text = 'e\n', runs = { { 1, 3 } }, changes = spec[rB].files['b.lua'].changes,
  turns = spec[rB].files['b.lua'].turns } } }
put_spec()
vim.cmd.edit(rE .. '/e.lua')
local ebuf = vim.api.nvim_get_current_buf()
vim.wait(500)
ok(blame.clients[rE] == nil and #argv() == nsp, 'no transcripts (only a sibling prefix dir): no client, no spawn')
sessions(rE, '-sub', rE .. '/sub') -- a session started in rE/sub
vim.api.nvim_exec_autocmds('BufEnter', { buffer = ebuf })
vim.wait(200)
ok(blame.clients[rE] == nil, 'within M.recheck: not asked again')
blame.recheck = 0
vim.api.nvim_exec_autocmds('BufEnter', { buffer = ebuf })
ok(marks_are(ebuf, { [1] = 'AicTurn8' }) and argv()[#argv()] == '--serve -r ' .. rE, 'a <slug>-sub dir with a session below root: blamed')

-- many sibling <slug>-… dirs: read off the main loop, rejected ones cached by mtime
local rH = repo('h', { ['h.lua'] = 'e\n' }, nil, true)
for k = 1, 300 do
  local d = cfg .. '/projects/' .. slug(rH) .. '-s' .. k
  for j = 1, 5 do
    write(d .. '/t' .. j .. '.jsonl', ('{"type":"user","message":"%s"}\n'):format(('x'):rep(1000)))
  end
end
local hit = cfg .. '/projects/' .. slug(rH) .. '-s150'
spec[rH] = { rev = 1, files = { ['h.lua'] = spec[rE].files['e.lua'] } }
put_spec()
local t0 = vim.uv.hrtime()
vim.cmd.edit(rH .. '/h.lua')
local hbuf = vim.api.nvim_get_current_buf()
vim.api.nvim_exec_autocmds('BufEnter', { buffer = hbuf })
local ms = (vim.uv.hrtime() - t0) / 1e6
ok(ms < 20, ('300 sibling dirs: :edit + BufEnter return in %.1f ms'):format(ms))
ok(wait(5000, function()
  return vim.tbl_count(blame.sessions[rH].rejected) == 300 and not blame.sessions[rH].scanning
end) and blame.clients[rH] == nil, 'all 300 read and rejected, no client')
write(hit .. '/t6.jsonl', vim.json.encode({ type = 'user', cwd = rH .. '/sub' }) .. '\n') -- that dir's mtime moves
local scans = {}
local scandir = vim.uv.fs_scandir
vim.uv.fs_scandir = function(path, ...)
  scans[#scans + 1] = path
  return scandir(path, ...)
end
vim.api.nvim_exec_autocmds('BufEnter', { buffer = hbuf }) -- M.recheck is 0 here
ok(marks_are(hbuf, { [1] = 'AicTurn8' }), 'the changed dir now has a session below root: blamed')
vim.uv.fs_scandir = scandir
ok(#scans == 2 and scans[2] == hit, 'the re-check read only the base and the changed dir: ' .. vim.inspect(scans))

-- a dir in no repo: asked again later (git init)
local rG = tmp .. '/g'
write(rG .. '/g.lua', 'e\n')
vim.cmd.edit(rG .. '/g.lua')
local gbuf = vim.api.nvim_get_current_buf()
ok(blame.bufs[gbuf] == nil, 'not a repo: not followed')
vim.system({ 'git', 'init', '-q' }, { cwd = rG }):wait()
sessions(rG)
spec[rG] = { rev = 1, files = { ['g.lua'] = spec[rE].files['e.lua'] } }
put_spec()
vim.api.nvim_exec_autocmds('BufEnter', { buffer = gbuf })
ok(marks_are(gbuf, { [1] = 'AicTurn8' }), 'after git init: blamed')
blame.recheck = 30000

-- a missing binary: one notice, blame off
require('aic.config').options.aigource = tmp .. '/nope'
local rC = repo('c', { ['c.lua'] = 'c\n', ['d.lua'] = 'd\n' })
local n0 = #notes
vim.cmd.edit(rC .. '/c.lua')
vim.cmd.edit(rC .. '/d.lua')
local offs = vim.tbl_filter(function(m)
  return m:find('AI blame off', 1, true) ~= nil
end, { unpack(notes, n0 + 1) })
ok(#offs == 1 and blame.clients[rC] == nil, 'missing binary: one notice, no client: ' .. vim.inspect(offs))

---------------------------------------------------------------------------
for _, p in ipairs(vim.split(read(fdir .. '/pids') or '', '\n', { trimempty = true })) do
  if (read('/proc/' .. p .. '/cmdline') or ''):find('fake_aigource.py', 1, true) then -- still ours
    vim.uv.kill(tonumber(p), 'sigterm')
  end
end
for _, h in ipairs(manual) do
  pcall(h.wait, h, 1000)
end
vim.fn.delete(tmp, 'rf')
io.stdout:write(('\n%d failure(s)\n'):format(failures))
if failures > 0 then
  vim.cmd('cquit 1')
else
  vim.cmd('qa!')
end
