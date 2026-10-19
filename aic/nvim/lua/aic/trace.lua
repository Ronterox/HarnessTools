-- Trace buffer: trace.jsonl rendered as a foldable tree grouped by turn.
local config = require('aic.config')
local state = require('aic.state')
local util = require('aic.util')

local api = vim.api
local M = {}

M.buf = nil
M.map = {} -- [lnum] = { ev = event, head = bool, line = target line in file }
M.levels = {} -- [lnum] = foldexpr value
-- Fold state the human chose, by node key ('turn:N' or event id); true = open.
-- Unset = default: turns closed (config `history_turns`), details closed.
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

-- Text as the trace shows it: tabs as 4 spaces (measured from column 0 they
-- would depend on whichever buffer's 'tabstop' is current) and no \r.
local function norm(s)
  return (s:gsub('\t', '    '):gsub('\r', ''))
end

local function dw(s)
  return api.nvim_strwidth(s)
end

-- `s` cut to at most `n` display cells (composing characters stay with theirs).
local function cut(s, n)
  local k = vim.fn.strchars(s, 1)
  local out = vim.fn.strcharpart(s, 0, math.min(k, math.max(n, 0)), 1)
  while #out > 0 and dw(out) > n do
    out = vim.fn.strcharpart(out, 0, vim.fn.strchars(out, 1) - 1, 1)
  end
  return out
end

-- `s` as it fits in `n` cells: (s, false), or (cut with …, true).
local function fit(s, n)
  s = norm(s)
  if dw(s) <= n then
    return s, false
  end
  return n >= 1 and (cut(s, n - 1) .. '…') or '', true
end

-- `text` (every line) wrapped to `n` cells, at spaces when it can. Cached:
-- events never change, and every render wraps every text again.
local wrap_cache, wrap_cached = {}, 0
local function wrap(text, n)
  n = math.max(n, 10)
  local key = n .. '\0' .. text
  if wrap_cache[key] then
    return wrap_cache[key]
  end
  local out = {}
  for _, line in ipairs(util.split(norm(text))) do
    if vim.trim(line) == '' then
      table.insert(out, '')
    elseif dw(line) <= n then
      table.insert(out, line)
    else
      local cur = line:match('^%s*')
      for word in line:gmatch('%S+%s*') do
        local bare = vim.trim(word)
        if vim.trim(cur) ~= '' and dw(cur .. bare) > n and dw(bare) <= n then
          table.insert(out, (cur:gsub('%s+$', '')))
          cur = ''
        end
        while dw(cur .. bare) > n do -- a word longer than a line: fill this one, go on
          local head = cut(cur .. word, n)
          table.insert(out, head)
          word = (cur .. word):sub(#head + 1)
          bare, cur = vim.trim(word), ''
        end
        cur = cur .. word
      end
      if vim.trim(cur) ~= '' then
        table.insert(out, (cur:gsub('%s+$', '')))
      end
    end
  end
  if wrap_cached > 20000 then
    wrap_cache, wrap_cached = {}, 0
  end
  wrap_cache[key], wrap_cached = out, wrap_cached + 1
  return out
end

-- Width the trace text has: its window's, else the screen's.
local function text_width()
  local w = M.buf and vim.fn.bufwinid(M.buf) or -1
  return math.max(40, (w ~= -1 and api.nvim_win_get_width(w) or vim.o.columns) - 1)
end
M.width = nil -- the width the last render used (re-rendered when it changes)

-- What a tool was given, readable (its title shows only the start): the
-- event's `detail`, or, for events written before it existed, rebuilt from
-- `input` (cut at 300 characters by the backend). nil for file tools.
local details = setmetatable({}, { __mode = 'k' }) -- ev -> its readable input (parsed once)
local tool_detail_of
local function tool_detail(ev)
  local d = details[ev]
  if d == nil then
    d = tool_detail_of(ev) or false
    details[ev] = d
  end
  return d or nil
end

function tool_detail_of(ev)
  if type(ev.detail) == 'string' then
    return ev.detail
  end
  if (ev.file and ev.file ~= vim.NIL) or type(ev.input) ~= 'string' or not ev.input:match('^%s*{') then
    return nil -- (inputs are JSON objects)
  end
  local ok, inp = pcall(vim.json.decode, ev.input)
  if ok and type(inp) == 'table' then
    local parts = {}
    for _, k in ipairs({ 'description', 'prompt' }) do
      if type(inp[k]) == 'string' then
        table.insert(parts, inp[k])
      end
    end
    if type(inp.command) == 'string' then
      table.insert(parts, '$ ' .. inp.command)
    end
    return #parts > 0 and table.concat(parts, '\n') or ev.input
  end
  local cmd = ev.input:match('"command":%s*"(.*)$') -- cut JSON: the command, unescaped as far as it goes
  if cmd then
    cmd = cmd:gsub('\\\\', '\0'):gsub('\\n', '\n'):gsub('\\t', '\t'):gsub('\\"', '"'):gsub('%z', '\\')
    return '$ ' .. cmd
  end
  return ev.input
end

local function build()
  local lines, map, indents, hls = {}, {}, {}, {}
  local width = text_width()
  M.width = width

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

  -- A head line whose last part (`parts[#parts]`) is cut to the width with …;
  -- returns true when something was cut.
  local function add_head(indent, parts, node, depth)
    local used = 2 * indent
    for i = 1, #parts - 1 do
      used = used + dw(parts[i][1])
    end
    local last = parts[#parts]
    local shown, was_cut = fit(last[1], width - used)
    parts[#parts] = { shown, last[2] }
    add(indent, parts, node, depth)
    return was_cut
  end

  -- The details of a text: the lines after the first, or, when the head had
  -- to cut the first, all of them; wrapped to the width (never cut off).
  local function add_rest(ev, text, indent, hl, depth, all)
    local ls = util.split(vim.trim(text or ''))
    local rest = all and ls or vim.list_slice(ls, 2)
    for _, l in ipairs(wrap(table.concat(rest, '\n'), width - 2 * indent)) do
      add(indent, { { l, hl } }, { ev = ev }, depth)
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
    local time = ts and ('   ' .. os.date('%H:%M', math.floor(ts))) or ''
    local lead = '▸ Turn ' .. t .. '  '
    local prompt, prompt_cut = fit(g.prompt and util.first_line(g.prompt.text) or '', width - dw(lead) - dw(time))
    add(0, {
      { '▸ ', 'AicTurn' },
      { 'Turn ' .. t, 'AicTurn' },
      { '  ' .. prompt, 'AicPrompt' },
      { time, 'AicTime' },
    }, { ev = g.prompt, head = true, turn = t })
    if g.prompt then -- part of the turn, not a fold of its own
      add_rest(g.prompt, g.prompt.text, 2, 'AicPrompt', 1, prompt_cut)
    end
    for _, ev in ipairs(g.events) do
      local glyph = state.seenable(ev) and { state.seen[ev.id] and '○ ' or '● ', state.seen[ev.id] and 'AicSeen' or 'AicUnseen' }
        or { '', nil }
      local node = { ev = ev, head = true }
      if ev.kind == 'tool' then
        local why = util.first_line(ev.why)
        local s = (ev.hunks and ev.hunks[1]) and tonumber(ev.hunks[1].start) or nil
        node.line = s
        local title = tostring(ev.title or ev.tool or 'tool')
        local title_shown, title_cut = fit(title, width - 2 - dw(glyph[1]))
        local why_cut = add_head(1, {
          glyph,
          { title_shown, ev.error and 'AicError' or 'AicTitle' },
          { (why ~= '' and not title_cut) and (' — ' .. why) or '', 'AicWhy' },
        }, node)
        -- the full input when the title doesn't show it all (cut here, cut by
        -- the backend, or a description in place of the command)
        local detail = tool_detail(ev)
        local said = title:gsub('^[^:]*:%s*', '')
        if detail and vim.trim((detail:gsub('^%$ ', ''))) == vim.trim(said) and not title_cut and title:sub(-3) ~= '…' then
          detail = nil
        end
        detail = detail or (title_cut and title) or nil
        for _, l in ipairs(detail and wrap(detail, width - 4) or {}) do
          add(2, { { l, ev.error and 'AicError' or 'AicTitle' } }, { ev = ev })
        end
        local whyl = util.split(vim.trim(ev.why or ''))
        if #whyl > 1 or why_cut or detail then
          add_rest(ev, ev.why, 2, 'AicWhy', nil, true)
        end
        add_hunks(ev, 2)
      elseif ev.kind == 'human' then
        local s, e = hunk_range(ev.hunks)
        node.line = s
        add_head(1, {
          { '✎ human ', 'AicHuman' },
          { (ev.file or '?') .. (s and (':' .. s .. '-' .. e) or ''), 'AicHuman' },
        }, node)
        add_hunks(ev, 2)
      elseif ev.kind == 'submit' then
        local c = add_head(1, { { '⇢ submit ', 'AicSubmit' }, { util.first_line(ev.text), 'AicSubmit' } }, node)
        add_rest(ev, ev.text, 2, 'AicSubmit', nil, c)
      elseif ev.kind == 'hold' then
        add(1, { { '⏸ hold', 'AicHold' } }, node)
      elseif ev.kind == 'release' then
        add(1, { { '▶ release', 'AicHold' } }, node)
      elseif ev.kind == 'held' then
        add(1, { { '⏹ held' .. (ev.tool and (' before ' .. ev.tool) or ''), 'AicHold' } }, node)
      elseif ev.kind == 'guarded' then -- the worker tried to change a doc or prompt template
        add_head(1, { { ('⛔ blocked %s %s'):format(ev.tool or '?', ev.file or '?'), 'AicError' } }, node)
      elseif ev.kind == 'prompt' then
        local c = add_head(1, { { ev.midturn and '✉ you (mid-turn) ' or '✉ you ', 'AicPrompt' },
          { util.first_line(ev.text), 'AicPrompt' } }, node)
        add_rest(ev, ev.text, 2, 'AicPrompt', nil, c)
      elseif ev.kind == 'text' then
        local c = add_head(1, { glyph, { '◆ ', 'AicText' }, { util.first_line(ev.text), 'AicText' } }, node)
        add_rest(ev, ev.text, 2, 'AicText', nil, c)
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
local function turns_open()
  return config.options.history_turns == 'open'
end

local function apply_folds(win)
  api.nvim_win_call(win, function()
    local view = vim.fn.winsaveview()
    -- even after a zm/zr: turns as configured ('history_turns'), details closed
    vim.wo[0][0].foldlevel = turns_open() and 1 or 0
    vim.cmd('silent! normal! zX')
    local lnums = vim.tbl_keys(M.map)
    table.sort(lnums)
    local want, turn_l = {}, nil -- turn header line -> open?
    for _, lnum in ipairs(lnums) do
      local node = M.map[lnum]
      if node.turn and node.head then
        turn_l = lnum
        local open = M.folds[fold_key(node)]
        if open == nil then
          open = turns_open()
        end
        want[lnum] = open
      elseif starts_fold(lnum) and M.folds[fold_key(node)] and turn_l then
        -- details the human opened: open them (inside their turn, opened for it)
        vim.cmd(('silent! %dfoldopen'):format(turn_l))
        vim.cmd(('silent! %dfoldopen'):format(lnum))
      end
    end
    for lnum, open in pairs(want) do -- after the details: a turn's own fold is the innermost at its header
      if open then
        vim.cmd(('silent! %dfoldopen'):format(lnum))
      elseif vim.fn.foldclosed(lnum) == -1 then
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
  M.reclaim()
  local lines, map, levels, hls = build()
  local old = api.nvim_buf_get_lines(b, 0, -1, false)
  local old_levels = M.levels

  local new_session = folds_dir ~= state.dir
  if new_session then
    M.folds, folds_dir = {}, state.dir
  end
  local wins, anchor = {}, {}
  for _, w in ipairs(vim.fn.win_findbuf(b)) do
    api.nvim_win_call(w, function() -- following the tail: on the last visible line
      local cur = api.nvim_win_get_cursor(w)[1]
      local e = vim.fn.foldclosedend(cur)
      wins[w] = (e == -1 and cur or e) == #old
      -- the node under the cursor, to keep the cursor on it when lines above it change
      local node = M.map[cur]
      if node and node.ev and node.ev.id and not new_session then
        local first = cur
        while first > 1 and M.map[first - 1] and M.map[first - 1].ev == node.ev and not M.map[first].head do
          first = first - 1
        end
        anchor[w] = { id = node.ev.id, turn = node.head and node.turn, off = cur - first,
                      top = vim.fn.line('w0'), cur = cur }
      end
    end)
    if vim.wo[w].foldmethod == 'expr' and not new_session then
      harvest(w)
    end
  end

  M.map, M.levels = map, levels
  vim.bo[b].modifiable = true
  local changed = #lines ~= #old
  local n = math.min(#old, #lines)
  local differ, full = {}, false
  for i = 1, n do
    if old[i] ~= lines[i] or old_levels[i] ~= levels[i] then
      table.insert(differ, i)
    end
  end
  if #differ > n / 2 then -- (a re-wrap): one call, not one per line
    api.nvim_buf_set_lines(b, 0, -1, false, lines)
    changed, full = true, true
  else
    for _, i in ipairs(differ) do
      api.nvim_buf_set_lines(b, i - 1, i, false, { lines[i] })
      changed = true
    end
    if #lines > #old then
      api.nvim_buf_set_lines(b, #old, #old, false, vim.list_slice(lines, #old + 1))
    elseif #lines < #old then
      api.nvim_buf_set_lines(b, #lines, -1, false, {})
    end
  end
  vim.bo[b].modifiable = false
  vim.bo[b].modified = false

  -- highlights: only on the lines that changed or came (the others keep theirs)
  local redo = {}
  if full or not M.highlighted then
    api.nvim_buf_clear_namespace(b, ns, 0, -1)
  else
    for _, i in ipairs(differ) do
      redo[i - 1] = true
      api.nvim_buf_clear_namespace(b, ns, i - 1, i)
    end
  end
  for _, h in ipairs(hls) do
    if full or not M.highlighted or redo[h[1]] or h[1] >= #old then
      api.nvim_buf_set_extmark(b, ns, h[1], h[2], { end_col = h[3], hl_group = h[4], priority = 150 })
    end
  end
  M.highlighted = true

  for w, was_last in pairs(wins) do
    if changed and vim.wo[w].foldmethod == 'expr' then
      apply_folds(w)
    end
    local a = anchor[w]
    if was_last and #lines > #old and #old > 1 then
      api.nvim_win_set_cursor(w, { #lines, 0 })
    elseif a and changed then -- the same node, even when lines above it came or went
      for l = 1, #lines do
        local node = map[l]
        if node and node.ev and node.ev.id == a.id and (a.turn == nil or node.turn == a.turn) then
          local target = math.min(l + a.off, #lines)
          if target ~= a.cur then
            api.nvim_win_call(w, function()
              api.nvim_win_set_cursor(w, { target, 0 })
              vim.fn.winrestview({ topline = math.max(1, a.top + target - a.cur) })
            end)
          end
          break
        end
      end
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
-- it; elsewhere jump to code, or (nothing to jump to) mark it read. Never
-- folds the enclosing turn by accident.
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
  if not M.jump() and n and n.head and state.seenable(n.ev) and not state.seen[n.ev.id] then
    state.set_seen({ n.ev.id }, true) -- nothing to show or open: <CR> still marks it read
  end
end

-- Window options are set buffer-locally (:setlocal), so they stay with the
-- trace and never leak into the code buffer shown next in the same window.
local function setup_window(win)
  local wo = vim.wo[win][0]
  wo.foldmethod = 'expr'
  wo.foldexpr = "v:lua.require'aic.trace'.foldexpr(v:lnum)"
  wo.foldtext = ''
  wo.foldlevel = turns_open() and 1 or 0
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

-- Take the trace's folds back when something else changed them in one of
-- its windows (nvim-ufo, loaded while the trace is shown, attaches to every
-- shown window: manual folds and its own 'foldtext', which shows `0` here).
local reclaims, reclaim_warned = {}, false
function M.reclaim()
  if not (M.buf and api.nvim_buf_is_valid(M.buf)) then
    return
  end
  for _, w in ipairs(vim.fn.win_findbuf(M.buf)) do
    local wo = vim.wo[w]
    local ours = wo.foldmethod == 'expr' and wo.foldexpr:find('aic.trace', 1, true) ~= nil
    if not ours or wo.foldtext ~= '' then
      local now = util.now()
      reclaims = vim.tbl_filter(function(t)
        return now - t < 1
      end, reclaims)
      if #reclaims >= 5 then -- something keeps taking them: stop fighting, say so once
        if not reclaim_warned then
          reclaim_warned = true
          util.notify('aic: another plugin keeps changing the history view\'s folds', vim.log.levels.WARN)
        end
        return
      end
      table.insert(reclaims, now)
      util.detach_ufo(M.buf)
      if ours then -- only the fold text: the folds themselves (and what the human opened) are fine
        wo[0].foldtext = ''
      else
        setup_window(w)
      end
    end
  end
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
  M.highlighted = false -- (a new buffer has none)

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
  util.detach_ufo(b) -- before it is ever shown: ufo then never attaches (or sets its foldtext)
  api.nvim_create_autocmd('BufWinEnter', {
    buffer = b,
    callback = function()
      local win = api.nvim_get_current_win()
      util.detach_ufo(b)
      setup_window(win) -- after the detach: our fold options win
      if M.width ~= text_width() then
        M.render() -- (wrapped to another width)
      end
    end,
  })
  api.nvim_create_autocmd('OptionSet', {
    group = api.nvim_create_augroup('aic_trace_folds', { clear = true }),
    pattern = { 'foldmethod', 'foldtext', 'foldexpr' },
    callback = function()
      if api.nvim_get_current_buf() == M.buf then
        vim.schedule(M.reclaim) -- (after whoever set it is done)
      end
    end,
  })
  local rewrap = util.debounce(80, function() -- (dragging a border sends many)
    if M.buf and api.nvim_buf_is_valid(M.buf) and vim.fn.bufwinid(M.buf) ~= -1 and M.width ~= text_width() then
      M.render()
    end
  end)
  api.nvim_create_autocmd({ 'WinResized', 'VimResized' }, {
    group = api.nvim_create_augroup('aic_trace_width', { clear = true }),
    callback = rewrap,
  })
  M.render()
  return b
end

return M
