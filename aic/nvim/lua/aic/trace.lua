-- Trace buffer: trace.jsonl rendered as a foldable tree grouped by turn.
-- A subagent's trace (agents/<id>.jsonl) shows nested under the Agent call
-- that started it, and on its own in a view like this one (gs).
local config = require('aic.config')
local state = require('aic.state')
local util = require('aic.util')

local api = vim.api
-- M is the worker's history view; a subagent's view is a table with the same
-- fields plus `agent` (its id).
local M = {}

M.buf = nil
M.map = {} -- [lnum] = { ev = event, head = bool, line = target line in file }
M.levels = {} -- [lnum] = foldexpr value
-- Fold state the human chose, by node key ('turn:N', 'agent:<id>' or event
-- id); true = open. Unset = default: turns closed (config `history_turns`),
-- details closed.
M.folds = {}
M.folds_dir = nil -- session M.folds belongs to (turn numbers repeat across sessions)
local views = {} -- buf -> view
local ns = api.nvim_create_namespace('aic_trace')

local function view_of(buf)
  return views[buf or api.nvim_get_current_buf()] or M
end

-- Is `buf` a trace view (the worker's or a subagent's)?
function M.is_view(buf)
  return buf ~= nil and views[buf] ~= nil and api.nvim_buf_is_valid(buf)
end

function M.foldexpr(lnum)
  return view_of().levels[lnum] or '0' -- (evaluated with the folded buffer current)
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

-- Width the trace text of view `v` has: its window's, else the screen's.
local function text_width(v)
  local w = v.buf and vim.fn.bufwinid(v.buf) or -1
  return math.max(40, (w ~= -1 and api.nvim_win_get_width(w) or vim.o.columns) - 1)
end
M.width = nil -- the width the last render used (re-rendered when it changes)

local AGENT_TOOLS = { Agent = true, Task = true }

-- agents.json `in` of an entry: the subagent that made the call (nil: the worker).
local function in_of(a)
  return type(a['in']) == 'string' and a['in'] or nil
end

-- `events` with the Agent calls still running in that trace (agents.json:
-- the trace has them only once they return), in time order. `agent` = the
-- subagent whose trace it is; nil = the worker's.
local function with_running(events, by_id, agent)
  local live = {}
  for id, a in pairs(state.agents) do
    local ev = type(a) == 'table' and type(a.running) == 'table' and a.running
    if ev and not by_id[id] and in_of(a) == agent then
      ev.id, ev.live = id, true
      table.insert(live, ev)
    end
  end
  if #live == 0 then
    return events
  end
  local function ts(ev) -- (no time: last)
    local t = tonumber(ev.ts)
    return (t and t > 0) and t or math.huge
  end
  table.sort(live, function(x, y)
    return ts(x) < ts(y)
  end)
  local out, k = {}, 1
  for _, ev in ipairs(events) do
    while live[k] and ts(live[k]) < ts(ev) do
      table.insert(out, live[k])
      k = k + 1
    end
    table.insert(out, ev)
  end
  for i = k, #live do
    table.insert(out, live[i])
  end
  return out
end

-- The subagent the Agent call `ev` started (agents.json), as (id, entry).
local function agent_of(ev)
  local a = ev and ev.id and state.agents[ev.id]
  if type(a) == 'table' and type(a.agent) == 'string' then
    return a.agent, a
  end
end

-- The trace event `id` belongs to: nil = the worker's, else a subagent id
-- (false: in none of them).
local function owner(id)
  if state.by_id[id] then
    return nil
  end
  for sid, t in pairs(state.subs) do
    if t.by_id[id] then
      return sid
    end
  end
  local a = state.agents[id]
  if type(a) == 'table' and type(a.running) == 'table' then
    return in_of(a)
  end
  return false
end

local function str(v, default)
  return type(v) == 'string' and v ~= '' and v or default
end

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

local function build(v)
  local lines, map, indents, hls = {}, {}, {}, {}
  local width = text_width(v)
  v.width = width

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

  -- One event at `indent` (its details one deeper). `ctx`: nested = not an
  -- inbox item here (no seen glyph; ]u skips it), depth = how many subagents
  -- deep, open = the subagents being shown around it (never one in itself).
  local add_event
  -- Under an Agent call: the subagent it started, a fold of its own with
  -- what that subagent did.
  local function add_agent(ev, indent, ctx)
    local id, a = agent_of(ev)
    if not id then
      return
    end
    local calls = tonumber(a.calls) or 0
    local st = str(a.state, '?')
    add_head(indent, {
      { '↳ ', 'AicTurn' },
      { ('%s %s · %d call%s · %s · gs'):format(str(a.type, 'subagent'), id:sub(1, 7), calls, calls == 1 and '' or 's', st),
        st == 'running' and 'AicHold' or 'AicSeen' },
    }, { ev = ev, head = true, agent = id, nested = true })
    local sub = state.subs[id]
    if not sub or ctx.open[id] or ctx.depth >= 4 then
      return
    end
    ctx.open[id] = true
    for k, sev in ipairs(with_running(sub.events, sub.by_id, id)) do
      if not (k == 1 and sev.kind == 'prompt') then -- (the Agent call's details show that prompt)
        add_event(sev, indent + 1, { nested = true, depth = ctx.depth + 1, open = ctx.open, sub = true })
      end
    end
    ctx.open[id] = nil
  end

  function add_event(ev, i, ctx)
    local glyph = (not ctx.nested and state.seenable(ev))
        and { state.seen[ev.id] and '○ ' or '● ', state.seen[ev.id] and 'AicSeen' or 'AicUnseen' }
      or { '', nil }
    local node = { ev = ev, head = true, nested = ctx.nested }
    local d = i + 1 -- details
    if ev.kind == 'tool' then
      local why = util.first_line(ev.why)
      local s = (ev.hunks and ev.hunks[1]) and tonumber(ev.hunks[1].start) or nil
      node.line = s
      local title = tostring(ev.title or ev.tool or 'tool')
      local live = ev.live and ' ⋯ running' or ''
      local title_shown, title_cut = fit(title, width - 2 * i - dw(glyph[1]) - dw(live))
      local why_cut = add_head(i, {
        glyph,
        { title_shown, ev.error and 'AicError' or 'AicTitle' },
        { live, 'AicHold' },
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
      for _, l in ipairs(detail and wrap(detail, width - 2 * d) or {}) do
        add(d, { { l, ev.error and 'AicError' or 'AicTitle' } }, { ev = ev })
      end
      local whyl = util.split(vim.trim(ev.why or ''))
      if #whyl > 1 or why_cut or detail then
        add_rest(ev, ev.why, d, 'AicWhy', nil, true)
      end
      if AGENT_TOOLS[ev.tool] then
        add_agent(ev, d, ctx)
      end
      add_hunks(ev, d)
    elseif ev.kind == 'human' then
      local s, e = hunk_range(ev.hunks)
      node.line = s
      add_head(i, {
        { '✎ human ', 'AicHuman' },
        { (ev.file or '?') .. (s and (':' .. s .. '-' .. e) or ''), 'AicHuman' },
      }, node)
      add_hunks(ev, d)
    elseif ev.kind == 'submit' then
      local c = add_head(i, { { '⇢ submit ', 'AicSubmit' }, { util.first_line(ev.text), 'AicSubmit' } }, node)
      add_rest(ev, ev.text, d, 'AicSubmit', nil, c)
    elseif ev.kind == 'hold' then
      add(i, { { '⏸ hold', 'AicHold' } }, node)
    elseif ev.kind == 'release' then
      add(i, { { '▶ release', 'AicHold' } }, node)
    elseif ev.kind == 'held' then
      add(i, { { '⏹ held' .. (ev.tool and (' before ' .. ev.tool) or ''), 'AicHold' } }, node)
    elseif ev.kind == 'guarded' then -- the worker tried to change a doc or prompt template
      add_head(i, { { ('⛔ blocked %s %s'):format(ev.tool or '?', ev.file or '?'), 'AicError' } }, node)
    elseif ev.kind == 'prompt' then
      -- (in a subagent: a message its parent sent it after the first prompt)
      local who = ctx.sub and '✉ message ' or ev.midturn and '✉ you (mid-turn) ' or '✉ you '
      local c = add_head(i, { { who, 'AicPrompt' }, { util.first_line(ev.text), 'AicPrompt' } }, node)
      add_rest(ev, ev.text, d, 'AicPrompt', nil, c)
    elseif ev.kind == 'text' then
      local c = add_head(i, { glyph, { '◆ ', 'AicText' }, { util.first_line(ev.text), 'AicText' } }, node)
      add_rest(ev, ev.text, d, 'AicText', nil, c)
    elseif ev.kind == 'compact' then
      -- the worker's context was compacted (not an inbox item)
      local k = require('aic.usage').k
      local function tok(n)
        return tonumber(n) and k(n) or '?'
      end
      local trig = type(ev.trigger) == 'string' and ev.trigger ~= '' and (' (' .. ev.trigger .. ')') or ''
      add(i, { { ('⇣ compacted %s → %s%s'):format(tok(ev.pre), tok(ev.post), trig), 'AicCompact' } }, node)
    else
      add(i, { { '· ' .. tostring(ev.kind), 'AicSeen' } }, node)
    end
  end

  local src = v.agent and state.subs[v.agent] or (not v.agent and state) or nil
  local events = src and with_running(src.events, src.by_id, v.agent) or {}
  local ctx = v.agent and { nested = true, sub = true, depth = 1, open = { [v.agent] = true } }
    or { depth = 0, open = {} }

  if v.agent then -- what started it; gs goes back there
    local parent = nil
    for id, a in pairs(state.agents) do
      if type(a) == 'table' and a.agent == v.agent then
        parent = { id = id, entry = a }
        break
      end
    end
    local a = parent and parent.entry or {}
    local pev = parent and (state.by_id[parent.id] or (type(a.running) == 'table' and a.running))
    if not pev and parent then
      for _, t in pairs(state.subs) do
        pev = pev or t.by_id[parent.id]
      end
    end
    local from = parent and owner(parent.id)
    add_head(0, {
      { '⇡ ', 'AicTurn' },
      { ('%s %s · %s · '):format(str(a.type, 'subagent'), v.agent:sub(1, 7), str(a.state, '?')), 'AicSeen' },
      { pev and tostring(pev.title or pev.tool or '') or '(not in this session)', 'AicTitle' },
    }, { ev = pev or nil, head = true, nested = true, parent = from == nil and 'worker' or from or nil })
  end

  -- group by turn, in order of first appearance
  local turns, order = {}, {}
  for _, ev in ipairs(events) do
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
      add_event(ev, 1, ctx)
    end
  end

  if #lines == 0 then
    add(0, { { state.dir and '(no trace events yet)' or '(no worker session — :AicAttach)', 'AicSeen' } }, nil)
  elseif v.agent and #lines == 1 then
    add(0, { { '(no events from this subagent yet)', 'AicSeen' } }, nil)
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
  if node.agent then
    return 'agent:' .. node.agent
  end
  return node.ev and node.ev.id
end

local function starts_fold(v, lnum)
  return (v.levels[lnum] or ''):sub(1, 1) == '>'
end

-- Remember what the human opened/closed (with <CR>, za, zo...) before the
-- buffer changes under the folds.
local function harvest(win, v)
  api.nvim_win_call(win, function()
    for lnum, node in pairs(v.map) do
      local k = starts_fold(v, lnum) and fold_key(node)
      if k then
        local c = vim.fn.foldclosed(lnum)
        if c == -1 or c == lnum then -- not hidden inside another closed fold
          v.folds[k] = c == -1
        end
      end
    end
  end)
end

-- Recompute folds from scratch (incremental fold updates go wrong when lines
-- change inside folds) and re-apply the remembered state.
local function turns_open(v)
  return v.agent ~= nil or config.options.history_turns == 'open' -- (a subagent: mostly one turn)
end

local function apply_folds(win, v)
  api.nvim_win_call(win, function()
    local view = vim.fn.winsaveview()
    -- even after a zm/zr: turns as configured ('history_turns'), details closed
    vim.wo[0][0].foldlevel = turns_open(v) and 1 or 0
    vim.cmd('silent! normal! zX')
    local lnums = vim.tbl_keys(v.map)
    table.sort(lnums)
    local want, turn_l = {}, nil -- turn header line -> open?
    local opened, closed = {}, {} -- details the human opened (outermost first) / closed
    for _, lnum in ipairs(lnums) do
      local node = v.map[lnum]
      if node.turn and node.head then
        turn_l = lnum
        local open = v.folds[fold_key(node)]
        if open == nil then
          open = turns_open(v)
        end
        want[lnum] = open
      elseif starts_fold(v, lnum) and v.folds[fold_key(node)] and turn_l then
        -- details the human opened: open them (inside their turn, opened for it)
        vim.cmd(('silent! %dfoldopen'):format(turn_l))
        table.insert(opened, lnum)
      elseif starts_fold(v, lnum) and v.folds[fold_key(node)] == false then
        table.insert(closed, lnum)
      end
    end
    for _, lnum in ipairs(opened) do -- (every level: a subagent's fold sits inside its Agent call's)
      vim.cmd(('silent! %dfoldopen!'):format(lnum))
    end
    for k = #closed, 1, -1 do -- (that opened details the human had closed: close them again, inner first)
      local lnum = closed[k]
      if vim.fn.foldclosed(lnum) == -1 then
        vim.cmd(('silent! %dfoldclose'):format(lnum))
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

-- Re-render view `v`, touching only lines that changed (keeps folds and cursor).
local function render_view(v)
  local b = v.buf
  if not b or not api.nvim_buf_is_valid(b) then
    return
  end
  M.reclaim(v)
  v.stale = false
  local lines, map, levels, hls = build(v)
  local old = api.nvim_buf_get_lines(b, 0, -1, false)
  local old_levels = v.levels

  local new_session = v.folds_dir ~= state.dir
  if new_session then
    v.folds, v.folds_dir = {}, state.dir
  end
  local wins, anchor = {}, {}
  for _, w in ipairs(vim.fn.win_findbuf(b)) do
    api.nvim_win_call(w, function() -- following the tail: on the last visible line
      local cur = api.nvim_win_get_cursor(w)[1]
      local e = vim.fn.foldclosedend(cur)
      wins[w] = (e == -1 and cur or e) == #old
      -- the node under the cursor, to keep the cursor on it when lines above it change
      local node = v.map[cur]
      if node and node.ev and node.ev.id and not new_session then
        local first = cur
        while first > 1 and v.map[first - 1] and v.map[first - 1].ev == node.ev and not v.map[first].head do
          first = first - 1
        end
        anchor[w] = { id = node.ev.id, turn = node.head and node.turn, agent = node.agent, off = cur - first,
                      top = vim.fn.line('w0'), cur = cur }
      end
    end)
    if vim.wo[w].foldmethod == 'expr' and not new_session then
      harvest(w, v)
    end
  end

  v.map, v.levels = map, levels
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
  if full or not v.highlighted then
    api.nvim_buf_clear_namespace(b, ns, 0, -1)
  else
    for _, i in ipairs(differ) do
      redo[i - 1] = true
      api.nvim_buf_clear_namespace(b, ns, i - 1, i)
    end
  end
  for _, h in ipairs(hls) do
    if full or not v.highlighted or redo[h[1]] or h[1] >= #old then
      api.nvim_buf_set_extmark(b, ns, h[1], h[2], { end_col = h[3], hl_group = h[4], priority = 150 })
    end
  end
  v.highlighted = true

  for w, was_last in pairs(wins) do
    if changed and vim.wo[w].foldmethod == 'expr' then
      apply_folds(w, v)
    end
    local a = anchor[w]
    if was_last and #lines > #old and #old > 1 then
      api.nvim_win_set_cursor(w, { #lines, 0 })
    elseif a and changed then -- the same node, even when lines above it came or went
      for l = 1, #lines do
        local node = map[l]
        if node and node.ev and node.ev.id == a.id and (a.turn == nil or node.turn == a.turn)
            and node.agent == a.agent then -- (an Agent call's ↳ line has its event too)
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

-- Re-render every trace view (the worker's and the subagents' shown so far).
function M.render()
  render_view(M)
  for b, v in pairs(views) do
    if v ~= M then
      if not api.nvim_buf_is_valid(b) then
        views[b] = nil
      elseif vim.fn.bufwinid(b) == -1 then
        v.stale = true -- (hidden: rendered when shown again)
      else
        render_view(v)
      end
    end
  end
end

function M.node_at(lnum)
  return view_of().map[lnum or api.nvim_win_get_cursor(0)[1]]
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
  local v = view_of()
  local cur = api.nvim_win_get_cursor(0)[1]
  local total = api.nvim_buf_line_count(0)
  local l = cur + dir
  while l >= 1 and l <= total do
    local n = v.map[l]
    if n and n.head and not n.nested and state.is_unseen(n.ev) then
      api.nvim_win_set_cursor(0, { l, 0 })
      vim.cmd('normal! zv')
      return l
    end
    l = l + dir
  end
  util.notify('aic: no ' .. (dir > 0 and 'next' or 'previous') .. ' unseen node')
end

function M.toggle_seen()
  local n = M.node_at()
  local ev = n and not n.nested and n.ev
  if not state.seenable(ev) then
    return
  end
  state.set_seen({ ev.id }, not state.seen[ev.id])
end

function M.mark_all_seen()
  if view_of().agent then
    return -- (a subagent's view has no inbox items)
  end
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
  if state.seenable(ev) and not n.nested then
    state.set_seen({ ev.id }, true)
  end
  require('aic.view').open_code(ev.file, n.line or 1)
  return true
end

-- The subagent of the line under the cursor: the one its Agent call started,
-- else the one whose event it is.
function M.agent_at(lnum)
  local n = M.node_at(lnum)
  if not n then
    return nil
  end
  if n.agent then
    return n.agent
  end
  local id = agent_of(n.ev)
  if id then
    return id
  end
  local o = n.ev and n.ev.id and owner(n.ev.id)
  return o or nil
end

-- gs: show the subagent under the cursor in its own view (<C-o> comes back);
-- on a subagent view's first line, show what started it.
function M.goto_agent()
  local n = M.node_at()
  if n and n.parent then
    if n.parent == 'worker' then
      return require('aic.view').show('trace')
    end
    return M.open_agent(n.parent)
  end
  local id = M.agent_at()
  if not id or id == view_of().agent then
    util.notify('aic: no subagent here')
    return
  end
  return M.open_agent(id)
end

function M.open_agent(id)
  require('aic.view').show_buf(M.get_agent_buf(id))
end

-- Enter a subagent's view at its last line (follows what it is doing).
local function to_tail(v)
  local w = vim.fn.bufwinid(v.buf)
  if w ~= -1 then
    api.nvim_win_set_cursor(w, { api.nvim_buf_line_count(v.buf), 0 })
  end
end

-- <CR>: on a line that heads a fold (turn, or an event with details) show/hide
-- it; elsewhere jump to code, or (nothing to jump to) mark it read. Never
-- folds the enclosing turn by accident.
function M.enter()
  local v = view_of()
  local lnum = api.nvim_win_get_cursor(0)[1]
  local c = vim.fn.foldclosed(lnum)
  if c ~= -1 and c ~= lnum then -- the cursor hides inside a closed fold: act on what is shown
    lnum = c
    api.nvim_win_set_cursor(0, { lnum, 0 })
  end
  local n = M.node_at(lnum)
  local inbox = n and not n.nested and state.seenable(n.ev) and not state.seen[n.ev.id]
  if starts_fold(v, lnum) then
    local opening = vim.fn.foldclosed(lnum) ~= -1
    vim.cmd(('silent! %d%s'):format(lnum, opening and 'foldopen' or 'foldclose'))
    local k = fold_key(n)
    if k then
      v.folds[k] = opening
    end
    if opening and inbox then
      state.set_seen({ n.ev.id }, true)
    end
    return
  end
  if not M.jump() and n and n.head and inbox then
    state.set_seen({ n.ev.id }, true) -- nothing to show or open: <CR> still marks it read
  end
end

-- Window options are set buffer-locally (:setlocal), so they stay with the
-- trace and never leak into the code buffer shown next in the same window.
local function setup_window(win, v)
  local wo = vim.wo[win][0]
  wo.foldmethod = 'expr'
  wo.foldexpr = "v:lua.require'aic.trace'.foldexpr(v:lnum)"
  wo.foldtext = ''
  wo.foldlevel = turns_open(v) and 1 or 0
  wo.foldenable = true
  wo.foldcolumn = '0'
  wo.wrap = false
  wo.number = false
  wo.relativenumber = false
  wo.signcolumn = 'no'
  wo.cursorline = true
  if api.nvim_win_get_buf(win) == v.buf then
    apply_folds(win, v) -- setting 'foldlevel' reset them
  end
  require('aic.usage').set_winbar(win, 'trace')
end

-- Take the trace's folds back when something else changed them in one of
-- its windows (nvim-ufo, loaded while the trace is shown, attaches to every
-- shown window: manual folds and its own 'foldtext', which shows `0` here).
local reclaims, reclaim_warned = {}, false
function M.reclaim(v)
  v = v or M
  if not (v.buf and api.nvim_buf_is_valid(v.buf)) then
    return
  end
  for _, w in ipairs(vim.fn.win_findbuf(v.buf)) do
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
      util.detach_ufo(v.buf)
      if ours then -- only the fold text: the folds themselves (and what the human opened) are fine
        wo[0].foldtext = ''
      else
        setup_window(w, v)
      end
    end
  end
end

-- A new trace buffer for view `v`: keys, fold upkeep, first render.
local function make_buf(v, name)
  local b = api.nvim_create_buf(false, true)
  pcall(api.nvim_buf_set_name, b, name)
  vim.bo[b].buftype = 'nofile'
  vim.bo[b].bufhidden = 'hide'
  vim.bo[b].swapfile = false
  vim.bo[b].filetype = 'aictrace'
  vim.bo[b].modifiable = false
  v.buf = b
  v.levels = {}
  v.highlighted = false -- (a new buffer has none)
  for ob, ov in pairs(views) do
    if ov == v then
      views[ob] = nil -- (its old buffer, wiped)
    end
  end
  views[b] = v

  local function map(lhs, fn, desc)
    vim.keymap.set('n', lhs, fn, { buffer = b, nowait = true, silent = true, desc = 'aic: ' .. desc })
  end
  map('<CR>', M.enter, 'show/hide details, or jump to code')
  map('gf', M.jump, 'jump to code')
  map('gs', M.goto_agent, 'show the subagent (or, on the first line, what started it)')
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
        if vim.wo[w].foldmethod == 'expr' and v.folds_dir == state.dir then
          harvest(w, v)
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
      setup_window(win, v) -- after the detach: our fold options win
      if v.stale or v.width ~= text_width(v) then
        render_view(v) -- (changed while hidden, or wrapped to another width)
      end
    end,
  })
  api.nvim_create_autocmd('OptionSet', {
    group = api.nvim_create_augroup('aic_trace_folds', { clear = true }),
    pattern = { 'foldmethod', 'foldtext', 'foldexpr' },
    callback = function()
      local cur = views[api.nvim_get_current_buf()]
      if cur then
        vim.schedule(function() -- (after whoever set it is done)
          M.reclaim(cur)
        end)
      end
    end,
  })
  local rewrap = util.debounce(80, function() -- (dragging a border sends many)
    for vb, vv in pairs(views) do
      if api.nvim_buf_is_valid(vb) and vim.fn.bufwinid(vb) ~= -1 and vv.width ~= text_width(vv) then
        render_view(vv)
      end
    end
  end)
  api.nvim_create_autocmd({ 'WinResized', 'VimResized' }, {
    group = api.nvim_create_augroup('aic_trace_width', { clear = true }),
    callback = rewrap,
  })
  render_view(v)
  return b
end

function M.get_buf()
  if M.buf and api.nvim_buf_is_valid(M.buf) then
    return M.buf
  end
  return make_buf(M, 'aic://trace')
end

-- The view of subagent `id` (made on first use; it starts at the tail).
function M.get_agent_buf(id)
  for b, v in pairs(views) do
    if v.agent == id and api.nvim_buf_is_valid(b) then
      return b
    end
  end
  local v = { agent = id, map = {}, levels = {}, folds = {} }
  local b = make_buf(v, 'aic://agent/' .. id)
  api.nvim_create_autocmd('BufWinEnter', {
    buffer = b,
    once = true,
    callback = function()
      to_tail(v)
    end,
  })
  return b
end

return M
