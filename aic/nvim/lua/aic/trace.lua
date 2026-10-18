-- Trace buffer: trace.jsonl rendered as a foldable tree grouped by turn.
local state = require('aic.state')
local util = require('aic.util')

local api = vim.api
local M = {}

M.buf = nil
M.map = {} -- [lnum] = { ev = event, head = bool, line = target line in file }
M.levels = {} -- [lnum] = foldexpr value
-- Fold state the human chose, by node key ('turn:N' or event id); true = open.
-- Unset = default: turns open, an event's details (why, hunks) closed.
M.folds = {}
local folds_dir -- session M.folds belongs to (turn numbers repeat across sessions)
local ns = api.nvim_create_namespace('aic_trace')

function M.foldexpr(lnum)
  return M.levels[lnum] or '0'
end

local function hunk_range(hunks)
  local s, e
  for _, h in ipairs(hunks or {}) do
    local hs = tonumber(h.start) or 1
    local he = hs + math.max((tonumber(h.count) or 1) - 1, 0)
    s = s and math.min(s, hs) or hs
    e = e and math.max(e, he) or he
  end
  return s, e
end

local function build()
  local lines, map, indents, hls = {}, {}, {}, {}

  -- add(indent, parts, node, depth): parts = list of { text, hl }; depth =
  -- fold depth when it differs from the visual indent
  local function add(indent, parts, node, depth)
    local text = string.rep('  ', indent)
    local lnum = #lines + 1
    for _, p in ipairs(parts) do
      if p[2] and p[1] ~= '' then
        table.insert(hls, { lnum - 1, #text, #text + #p[1], p[2] })
      end
      text = text .. p[1]
    end
    lines[lnum] = text
    indents[lnum] = depth or indent
    map[lnum] = node
  end

  local function add_hunks(ev, indent)
    for _, h in ipairs(ev.hunks or {}) do
      local start = tonumber(h.start) or 1
      add(indent, { { ('@@ +%d,%d'):format(start, tonumber(h.count) or 0), 'AicHunk' } }, { ev = ev, line = start })
      local nl = start
      for _, l in ipairs(h.lines or {}) do
        l = tostring(l)
        local c = l:sub(1, 1)
        local hl = c == '+' and 'DiffAdd' or c == '-' and 'DiffDelete' or nil
        add(indent, { { l, hl } }, { ev = ev, line = nl })
        if c ~= '-' then
          nl = nl + 1
        end
      end
    end
  end

  local function add_rest(ev, text, indent, hl, depth)
    local ls = util.split(vim.trim(text or ''))
    for i = 2, #ls do
      add(indent, { { ls[i], hl } }, { ev = ev }, depth)
    end
  end

  -- group by turn, in order of first appearance
  local turns, order = {}, {}
  for _, ev in ipairs(state.events) do
    local t = tonumber(ev.turn) or 0
    if not turns[t] then
      turns[t] = { events = {} }
      table.insert(order, t)
    end
    if ev.kind == 'prompt' and not turns[t].prompt then
      turns[t].prompt = ev
    else
      table.insert(turns[t].events, ev)
    end
  end

  for _, t in ipairs(order) do
    local g = turns[t]
    local first = g.prompt or g.events[1]
    local ts = first and tonumber(first.ts)
    add(0, {
      { '▸ ', 'AicTurn' },
      { 'Turn ' .. t, 'AicTurn' },
      { '  ' .. (g.prompt and util.first_line(g.prompt.text) or ''), 'AicPrompt' },
      { ts and ('   ' .. os.date('%H:%M', math.floor(ts))) or '', 'AicTime' },
    }, { ev = g.prompt, head = true, turn = t })
    if g.prompt then
      add_rest(g.prompt, g.prompt.text, 2, 'AicPrompt', 1) -- part of the turn, not a fold of its own
    end
    for _, ev in ipairs(g.events) do
      local glyph = state.seenable(ev) and { state.seen[ev.id] and '○ ' or '● ', state.seen[ev.id] and 'AicSeen' or 'AicUnseen' }
        or { '', nil }
      local node = { ev = ev, head = true }
      if ev.kind == 'tool' then
        local why = util.first_line(ev.why)
        local s = (ev.hunks and ev.hunks[1]) and tonumber(ev.hunks[1].start) or nil
        node.line = s
        add(1, {
          glyph,
          { ev.title or ev.tool or 'tool', ev.error and 'AicError' or 'AicTitle' },
          { why ~= '' and (' — ' .. why) or '', 'AicWhy' },
        }, node)
        local whyl = util.split(vim.trim(ev.why or ''))
        if #whyl > 1 or (#whyl == 1 and #whyl[1] > 60) then
          for _, l in ipairs(whyl) do
            add(2, { { l, 'AicWhy' } }, { ev = ev })
          end
        end
        add_hunks(ev, 2)
      elseif ev.kind == 'human' then
        local s, e = hunk_range(ev.hunks)
        node.line = s
        add(1, {
          { '✎ human ', 'AicHuman' },
          { (ev.file or '?') .. (s and (':' .. s .. '-' .. e) or ''), 'AicHuman' },
        }, node)
        add_hunks(ev, 2)
      elseif ev.kind == 'submit' then
        add(1, { { '⇢ submit ', 'AicSubmit' }, { util.first_line(ev.text), 'AicSubmit' } }, node)
        add_rest(ev, ev.text, 2, 'AicSubmit')
      elseif ev.kind == 'hold' then
        add(1, { { '⏸ hold', 'AicHold' } }, node)
      elseif ev.kind == 'release' then
        add(1, { { '▶ release', 'AicHold' } }, node)
      elseif ev.kind == 'held' then
        add(1, { { '⏹ held' .. (ev.tool and (' before ' .. ev.tool) or ''), 'AicHold' } }, node)
      elseif ev.kind == 'prompt' then
        add(1, { { ev.midturn and '✉ you (mid-turn) ' or '✉ you ', 'AicPrompt' }, { util.first_line(ev.text), 'AicPrompt' } }, node)
        add_rest(ev, ev.text, 2, 'AicPrompt')
      elseif ev.kind == 'text' then
        add(1, { glyph, { '◆ ', 'AicText' }, { util.first_line(ev.text), 'AicText' } }, node)
        add_rest(ev, ev.text, 2, 'AicText')
      elseif ev.kind == 'compact' then
        -- the worker's context was compacted (not an inbox item)
        local k = require('aic.usage').k
        local function tok(v)
          return tonumber(v) and k(v) or '?'
        end
        local trig = type(ev.trigger) == 'string' and ev.trigger ~= '' and (' (' .. ev.trigger .. ')') or ''
        add(1, { { ('⇣ compacted %s → %s%s'):format(tok(ev.pre), tok(ev.post), trig), 'AicCompact' } }, node)
      else
        add(1, { { '· ' .. tostring(ev.kind), 'AicSeen' } }, node)
      end
    end
  end

  if #lines == 0 then
    add(0, { { state.dir and '(no trace events yet)' or '(no worker session — :AicAttach)', 'AicSeen' } }, nil)
  end

  local levels = {}
  for i = 1, #lines do
    local l = indents[i]
    levels[i] = (indents[i + 1] or 0) > l and ('>' .. (l + 1)) or tostring(l)
  end
  return lines, map, levels, hls
end

local function fold_key(node)
  if not node or not node.head then
    return nil
  end
  if node.turn then
    return 'turn:' .. node.turn
  end
  return node.ev and node.ev.id
end

local function starts_fold(lnum)
  return (M.levels[lnum] or ''):sub(1, 1) == '>'
end

-- Remember what the human opened/closed (with <CR>, za, zo...) before the
-- buffer changes under the folds.
local function harvest(win)
  api.nvim_win_call(win, function()
    for lnum, node in pairs(M.map) do
      local k = starts_fold(lnum) and fold_key(node)
      if k then
        local c = vim.fn.foldclosed(lnum)
        if c == -1 or c == lnum then -- not hidden inside another closed fold
          M.folds[k] = c == -1
        end
      end
    end
  end)
end

-- Recompute folds from scratch (incremental fold updates go wrong when lines
-- change inside folds) and re-apply the remembered state.
local function apply_folds(win)
  api.nvim_win_call(win, function()
    local view = vim.fn.winsaveview()
    vim.wo[0][0].foldlevel = 1 -- even after a zm/zr: turns open, details closed
    vim.cmd('silent! normal! zX')
    local turns = {}
    for lnum, node in pairs(M.map) do
      local k = starts_fold(lnum) and fold_key(node)
      if k ~= nil and M.folds[k] ~= nil then
        if node.turn then
          turns[lnum] = M.folds[k]
        elseif M.folds[k] then
          vim.cmd(('silent! %dfoldopen'):format(lnum))
        end
      end
    end
    for lnum, open in pairs(turns) do -- after events: :foldclose hits the innermost open fold
      if not open then
        vim.cmd(('silent! %dfoldclose'):format(lnum))
      end
    end
    vim.fn.winrestview(view)
  end)
end

-- Re-render, touching only lines that changed (keeps folds and cursor).
function M.render()
  local b = M.buf
  if not b or not api.nvim_buf_is_valid(b) then
    return
  end
  local lines, map, levels, hls = build()
  local old = api.nvim_buf_get_lines(b, 0, -1, false)
  local old_levels = M.levels

  local new_session = folds_dir ~= state.dir
  if new_session then
    M.folds, folds_dir = {}, state.dir
  end
  local wins = {}
  for _, w in ipairs(vim.fn.win_findbuf(b)) do
    api.nvim_win_call(w, function() -- following the tail: on the last visible line
      local cur = api.nvim_win_get_cursor(w)[1]
      local e = vim.fn.foldclosedend(cur)
      wins[w] = (e == -1 and cur or e) == #old
    end)
    if vim.wo[w].foldmethod == 'expr' and not new_session then
      harvest(w)
    end
  end

  M.map, M.levels = map, levels
  vim.bo[b].modifiable = true
  local changed = #lines ~= #old
  local n = math.min(#old, #lines)
  for i = 1, n do
    if old[i] ~= lines[i] or old_levels[i] ~= levels[i] then
      api.nvim_buf_set_lines(b, i - 1, i, false, { lines[i] })
      changed = true
    end
  end
  if #lines > #old then
    api.nvim_buf_set_lines(b, #old, #old, false, vim.list_slice(lines, #old + 1))
  elseif #lines < #old then
    api.nvim_buf_set_lines(b, #lines, -1, false, {})
  end
  vim.bo[b].modifiable = false
  vim.bo[b].modified = false

  api.nvim_buf_clear_namespace(b, ns, 0, -1)
  for _, h in ipairs(hls) do
    api.nvim_buf_set_extmark(b, ns, h[1], h[2], { end_col = h[3], hl_group = h[4], priority = 150 })
  end

  for w, was_last in pairs(wins) do
    if changed and vim.wo[w].foldmethod == 'expr' then
      apply_folds(w)
    end
    if was_last and #lines > #old and #old > 1 then
      api.nvim_win_set_cursor(w, { #lines, 0 })
    end
  end
end

function M.node_at(lnum)
  return M.map[lnum or api.nvim_win_get_cursor(0)[1]]
end

function M.event_at(lnum)
  local n = M.node_at(lnum)
  return n and n.ev
end

-- Put the cursor on turn `n`'s header (n = number or 'last'). Returns lnum or nil.
function M.goto_turn(n)
  local found
  for l = 1, api.nvim_buf_line_count(M.buf or 0) do
    local node = M.map[l]
    if node and node.turn and (n == 'last' or node.turn == n) then
      found = l
      if n ~= 'last' then
        break
      end
    end
  end
  if found and api.nvim_get_current_buf() == M.buf then
    api.nvim_win_set_cursor(0, { found, 0 })
    vim.cmd('normal! zv')
  end
  return found
end

-- Move to the next (dir=1) / previous (dir=-1) unseen node. Returns lnum or nil.
function M.next_unseen(dir)
  dir = dir or 1
  local cur = api.nvim_win_get_cursor(0)[1]
  local total = api.nvim_buf_line_count(0)
  local l = cur + dir
  while l >= 1 and l <= total do
    local n = M.map[l]
    if n and n.head and state.is_unseen(n.ev) then
      api.nvim_win_set_cursor(0, { l, 0 })
      vim.cmd('normal! zv')
      return l
    end
    l = l + dir
  end
  util.notify('aic: no ' .. (dir > 0 and 'next' or 'previous') .. ' unseen node')
end

function M.toggle_seen()
  local ev = M.event_at()
  if not state.seenable(ev) then
    return
  end
  state.set_seen({ ev.id }, not state.seen[ev.id])
end

function M.mark_all_seen()
  local ids = {}
  for _, ev in ipairs(state.events) do
    if state.seenable(ev) then
      table.insert(ids, ev.id)
    end
  end
  state.set_seen(ids, true)
end

-- Jump to the file:line of the node or hunk line (marks the node seen).
function M.jump()
  local n = M.node_at()
  local ev = n and n.ev
  if not ev or not ev.file or ev.file == vim.NIL then
    return false
  end
  if state.seenable(ev) then
    state.set_seen({ ev.id }, true)
  end
  require('aic.view').open_code(ev.file, n.line or 1)
  return true
end

-- <CR>: on a line that heads a fold (turn, or an event with details) show/hide
-- it; elsewhere jump to code. Never folds the enclosing turn by accident.
function M.enter()
  local lnum = api.nvim_win_get_cursor(0)[1]
  local c = vim.fn.foldclosed(lnum)
  if c ~= -1 and c ~= lnum then -- the cursor hides inside a closed fold: act on what is shown
    lnum = c
    api.nvim_win_set_cursor(0, { lnum, 0 })
  end
  local n = M.node_at(lnum)
  if starts_fold(lnum) then
    local opening = vim.fn.foldclosed(lnum) ~= -1
    vim.cmd(('silent! %d%s'):format(lnum, opening and 'foldopen' or 'foldclose'))
    local k = fold_key(n)
    if k then
      M.folds[k] = opening
    end
    if opening and n and state.seenable(n.ev) and not state.seen[n.ev.id] then
      state.set_seen({ n.ev.id }, true)
    end
    return
  end
  M.jump()
end

-- Window options are set buffer-locally (:setlocal), so they stay with the
-- trace and never leak into the code buffer shown next in the same window.
local function setup_window(win)
  local wo = vim.wo[win][0]
  wo.foldmethod = 'expr'
  wo.foldexpr = "v:lua.require'aic.trace'.foldexpr(v:lnum)"
  wo.foldtext = ''
  wo.foldlevel = 1
  wo.foldenable = true
  wo.foldcolumn = '0'
  wo.wrap = false
  wo.number = false
  wo.relativenumber = false
  wo.signcolumn = 'no'
  wo.cursorline = true
  if api.nvim_win_get_buf(win) == M.buf then
    apply_folds(win) -- setting 'foldlevel' reset them
  end
  require('aic.usage').set_winbar(win, 'trace')
end

function M.get_buf()
  if M.buf and api.nvim_buf_is_valid(M.buf) then
    return M.buf
  end
  local b = api.nvim_create_buf(false, true)
  pcall(api.nvim_buf_set_name, b, 'aic://trace')
  vim.bo[b].buftype = 'nofile'
  vim.bo[b].bufhidden = 'hide'
  vim.bo[b].swapfile = false
  vim.bo[b].filetype = 'aictrace'
  vim.bo[b].modifiable = false
  M.buf = b
  M.levels = {}

  local function map(lhs, fn, desc)
    vim.keymap.set('n', lhs, fn, { buffer = b, nowait = true, silent = true, desc = 'aic: ' .. desc })
  end
  map('<CR>', M.enter, 'show/hide details, or jump to code')
  map('gf', M.jump, 'jump to code')
  map(']u', function()
    M.next_unseen(1)
  end, 'next unseen')
  map('[u', function()
    M.next_unseen(-1)
  end, 'previous unseen')
  map('m', M.toggle_seen, 'toggle seen')
  map('M', M.mark_all_seen, 'mark all seen')
  map('a', function()
    require('aic.ask').ask()
  end, 'ask at cursor')
  map('W', function()
    require('aic.ask').whatif()
  end, 'what-if at cursor')
  map('R', function()
    state.resolve(function()
      state.refresh(true)
    end)
  end, 'refresh')

  api.nvim_create_autocmd('BufWinLeave', {
    buffer = b,
    callback = function()
      for _, w in ipairs(vim.fn.win_findbuf(b)) do
        if vim.wo[w].foldmethod == 'expr' and folds_dir == state.dir then
          harvest(w)
        end
      end
    end,
  })
  api.nvim_create_autocmd('BufWinEnter', {
    buffer = b,
    callback = function()
      setup_window(api.nvim_get_current_win())
      util.detach_ufo(b)
    end,
  })
  M.render()
  return b
end

return M
