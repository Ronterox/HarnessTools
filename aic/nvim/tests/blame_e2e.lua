-- AI blame against the real `aigource --serve` (spawned by the plugin), on
-- aigource's structure fixture with its transcripts. Run from aic/nvim:
--   nvim --headless -u NONE -l tests/blame_e2e.lua [AIGOURCE]
-- (default: ../../aigource/build/aigource)
local plugin_dir = vim.fn.fnamemodify(debug.getinfo(1, 'S').source:sub(2), ':p:h:h')
vim.opt.rtp:prepend(plugin_dir)
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
local notes = {}
vim.notify = function(msg)
  table.insert(notes, msg)
end

local bin = vim.fn.fnamemodify(_G.arg[1] or (plugin_dir .. '/../../aigource/build/aigource'), ':p')
local tmp = vim.fn.resolve(vim.fn.tempname())
vim.fn.mkdir(tmp, 'p')
local r = vim.system({ 'python3', plugin_dir .. '/tests/blame_fixture.py', tmp, bin }, { text = true }):wait()
local fx = r.code == 0 and vim.json.decode(r.stdout)
ok(fx, 'fixture built' .. (fx and '' or (': ' .. r.stderr)))
if not fx then
  vim.cmd('cquit 1')
end
vim.env.CLAUDE_CONFIG_DIR = fx.cfg
vim.env.AIG_SOCK_NS = 'aice2e' .. vim.uv.os_getpid()
vim.env.AIG_SERVE_IDLE = '10'

local blame = require('aic.blame')
require('aic').setup({ aigource = bin })

local function marks(buf)
  local m = {}
  for _, e in ipairs(vim.api.nvim_buf_get_extmarks(buf, blame.ns, 0, -1, { details = true })) do
    m[e[2] + 1] = e[4].number_hl_group
  end
  return m
end

-- one request on a connection of its own, its reply
local function raw(msg)
  local p, buf, reply = vim.uv.new_pipe(false), '', nil
  vim.uv.pipe_connect2(p, blame.clients[fx.repo].name, {}, function(err)
    if err then
      reply = { status = err }
      return
    end
    p:read_start(function(_, d)
      buf = buf .. (d or '')
      local line = buf:match('^([^\n]*)\n')
      if line then
        reply = vim.json.decode(line, { luanil = { object = true } })
      end
    end)
    p:write(vim.json.encode(msg) .. '\n')
  end)
  vim.wait(5000, function()
    return reply ~= nil
  end)
  p:close()
  return reply
end

local paths = vim.tbl_keys(fx.lines)
table.sort(paths)
local ai_lines, saids = 0, 0
for _, path in ipairs(paths) do
  local want = {}
  for l, t in ipairs(fx.lines[path]) do
    if t ~= vim.NIL then
      want[l] = 'AicTurn' .. (t[1] + 1)
      ai_lines = ai_lines + 1
    end
  end
  vim.cmd.edit(fx.repo .. '/' .. path)
  local buf = vim.api.nvim_get_current_buf()
  local got = vim.wait(30000, function()
    return blame.bufs[buf] and blame.bufs[buf].status == 'ok' and vim.deep_equal(marks(buf), want)
  end, 50)
  ok(got, path .. ': number highlights as --structure-dump\'s turns: ' .. vim.inspect(marks(buf)) .. ' want ' .. vim.inspect(want))
  for l, t in ipairs(fx.lines[path]) do
    if t ~= vim.NIL then
      local f = blame.format('t', { final_lnum = l })
      local b = blame.bufs[buf]
      local ch = b.changes[tostring(b.cid[l])] or {}
      local said = type(ch.said) == 'string' and ch.said or nil -- its rule is aigource's (tests/serve.py)
      local what = said or t[2] -- what the agent said, else the prompt
      ok(f[1][1] == '◆ ' and f[1][2] == 'AicTurn' .. (t[1] + 1) and vim.startswith(f[2][1], what:sub(1, 20)),
        path .. ':' .. l .. ': formatter chunks say what the agent said (else the prompt): ' .. vim.inspect(f))
      saids = saids + (said and 1 or 0)
      vim.api.nvim_win_set_cursor(0, { 1, 0 })
      local j = blame.jump(1)
      ok(j ~= nil and fx.lines[path][j] ~= vim.NIL, path .. ': ]w lands on an AI line (' .. tostring(j) .. ')')
      vim.api.nvim_win_set_cursor(0, { l, 0 })
      if path == 'src/new.py' then -- untracked: our own cursor-line text
        vim.wait(3000, function()
          return blame.bufs[buf].untracked ~= nil -- git ls-files answered
        end)
        vim.api.nvim_exec_autocmds('CursorMoved', { buffer = buf })
        local m = vim.api.nvim_buf_get_extmarks(buf, blame.line_ns, 0, -1, { details = true })[1]
        ok(m and m[2] + 1 == l and vim.deep_equal(m[4].virt_text, f), path .. ': untracked, cursor-line text drawn')
      end
      -- detail: the real reply, then the float from it (a stale rev first)
      local b = blame.bufs[buf]
      local d = raw({ cmd = 'detail', file = path, line = l, rev = b.rev })
      ok(d.status == 'ok' and vim.startswith(d.prompt, t[2]) and type(d.hunks) == 'table' and #d.hunks > 0
        and #d.hunks[1].lines > 0 and d.cid ~= nil, path .. ':' .. l .. ': detail ok, full prompt, a hunk: '
        .. vim.inspect({ d.status, d.prompt, d.hunks and #d.hunks, d.cid }))
      local none_line
      for k, x in ipairs(fx.lines[path]) do
        if x == vim.NIL and blame.bufs[buf].cid[k] == -1 then
          none_line = k
          break
        end
      end
      if none_line then
        local n = raw({ cmd = 'detail', file = path, line = none_line, rev = b.rev })
        ok(n.status == 'none', path .. ':' .. none_line .. ': detail on a line no change wrote: ' .. tostring(n.status))
      end
      local cur = b.rev
      b.rev = cur - 1 -- as if the buffer's blame were from the build before
      vim.api.nvim_win_set_cursor(0, { l, 0 })
      blame.detail()
      ok(vim.wait(5000, function()
        return vim.api.nvim_win_get_config(0).relative ~= ''
      end) and blame.bufs[buf].rev == cur, path .. ': stale rev: blame fetched again, float shown')
      local fl = vim.api.nvim_buf_get_lines(0, 0, -1, false)
      local pl = vim.split(vim.trim(d.prompt), '\n', { plain = true })
      ok(fl[1] == '# ' .. pl[1] and vim.deep_equal({ unpack(fl, 2, #pl) }, { unpack(pl, 2) }) and vim.tbl_contains(fl, '```diff')
        and vim.tbl_contains(fl, d.hunks[1].lines[1]) and vim.tbl_contains(fl, '- tool: ' .. d.tool),
        path .. ': float: full prompt, tool, diff: ' .. vim.inspect(fl))
      vim.api.nvim_win_close(0, true)
      vim.fn.setqflist({}, 'r', { title = '', items = {} })
      blame.lines()
      ok(vim.wait(5000, function()
        return vim.fn.getqflist({ title = 0 }).title == 'aic turn: ' .. t[2]
      end) and #vim.fn.getqflist() > 0, path .. ': turn_lines -> quickfix (' .. #vim.fn.getqflist() .. ' ranges)')
      break
    end
  end
end
ok(ai_lines > 0, 'the fixture has AI lines (' .. ai_lines .. ')')
ok(saids > 0, 'and lines whose change has a comment (' .. saids .. ')')
ok(#notes == #vim.tbl_filter(function(m)
  return m:find('ranges of this turn', 1, true) ~= nil
end, notes), 'no other notices: ' .. vim.inspect(notes))

-- quit the server this test's nvim started
local cl = blame.clients[fx.repo]
local p = vim.uv.new_pipe(false)
local done
vim.uv.pipe_connect2(p, cl.name, {}, function(err)
  if not err then
    p:write('{"cmd":"quit"}\n')
  end
  done = err or 'sent'
end)
vim.wait(2000, function()
  return done ~= nil
end)
ok(done == 'sent' and vim.wait(5000, function()
  return cl.pipe == nil and cl.wait_until ~= nil
end), 'quit: the client got the handover and waits for a new host')
p:close()
vim.fn.delete(tmp, 'rf')
io.stdout:write(('\n%d failure(s)\n'):format(failures))
-- leave before the handover wait ends (it would spawn again)
if failures > 0 then
  vim.cmd('cquit 1')
else
  vim.cmd('qa!')
end
