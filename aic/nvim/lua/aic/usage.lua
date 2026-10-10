-- Context/cost usage (PROTOCOL.md "Usage"): winbars on the aic windows,
-- account quota polling, the :AicUsage float and compact/reset actions.
local backend = require('aic.backend')
local config = require('aic.config')
local state = require('aic.state')
local util = require('aic.util')

local api = vim.api
local uv = vim.uv or vim.loop

local M = {
  cache = { spec = '', trace = '', chat = '' }, -- winbar strings (statusline syntax)
  float = nil, -- { buf, win } of the :AicUsage float
  running = nil, -- 'compile' | 'ask' | 'worker' while a compact/reset runs
}

local WINBAR = "%%{%%v:lua.require'aic.usage'.winbar('%s')%%}"
local SESSION = { spec = 'compile', trace = 'worker', chat = 'ask' }
local QUOTA_EVERY = 60 -- s
local QUOTA_STALE = 900 -- s
local HOT_PCT = 80
-- Big contexts get expensive long before a 1M window is 80% full: every call
-- re-reads the session (a cold-cache compile of a 483k session cost $4.28).
local HOT_TOKENS = 250000

local last_fetch = 0
local fetching = false
local hot = {} -- session id -> true while its ctx is >= HOT_PCT

---------------------------------------------------------------------------
-- formatting

-- claude-sonnet-5-5 -> sonnet, claude-haiku-4-5-20251001 -> haiku
function M.short_model(m)
  if type(m) ~= 'string' or m == '' then
    return '?'
  end
  local s = m:gsub('%[.-%]$', ''):gsub('^claude%-', ''):gsub('[-%d%.]+$', '')
  return s ~= '' and s or m
end

function M.k(n)
  n = tonumber(n) or 0
  if n >= 1e6 then
    return ('%.1fM'):format(n / 1e6)
  elseif n >= 1000 then
    return math.floor(n / 1000 + 0.5) .. 'k'
  end
  return tostring(math.floor(n))
end

local function pct_of(e)
  local tok, win = tonumber(e.ctx_tokens), tonumber(e.ctx_window)
  if not tok or not win or win <= 0 then
    return nil
  end
  return math.floor(tok / win * 100 + 0.5)
end

local function bar(p)
  local n = math.max(0, math.min(10, math.floor(p / 10 + 0.5)))
  return '[' .. ('#'):rep(n) .. ('-'):rep(10 - n) .. ']'
end

local function group(p)
  if p >= 85 then
    return 'AicUsageHot'
  elseif p >= 70 then
    return 'AicUsageWarn'
  end
  return 'AicUsageOk'
end

local function esc(s)
  return (tostring(s):gsub('%%', '%%%%'))
end

local function hl(g, s)
  return '%#' .. g .. '#' .. s .. '%*'
end

-- Seconds -> "3h30m" / "6d7h" / "12m" / "40s".
function M.duration(s)
  s = math.max(0, math.floor(tonumber(s) or 0))
  if s >= 86400 then
    return ('%dd%dh'):format(math.floor(s / 86400), math.floor(s % 86400 / 3600))
  elseif s >= 3600 then
    return ('%dh%dm'):format(math.floor(s / 3600), math.floor(s % 3600 / 60))
  elseif s >= 60 then
    return ('%dm'):format(math.floor(s / 60))
  end
  return s .. 's'
end

local function days_from_civil(y, m, d)
  y = m <= 2 and y - 1 or y
  local era = math.floor(y / 400)
  local yoe = y - era * 400
  local doy = math.floor((153 * ((m + 9) % 12) + 2) / 5) + d - 1
  return era * 146097 + yoe * 365 + math.floor(yoe / 4) - math.floor(yoe / 100) + doy - 719468
end

-- ISO 8601 (UTC "Z" or ±hh:mm offset; no zone = UTC) or epoch number -> epoch.
function M.parse_time(v)
  if type(v) == 'number' then
    return v > 1e11 and v / 1000 or v
  end
  if type(v) ~= 'string' then
    return nil
  end
  local y, mo, d, h, mi, s = v:match('^(%d+)-(%d+)-(%d+)[T ](%d+):(%d+):(%d+)')
  if not y then
    return nil
  end
  local t = days_from_civil(tonumber(y), tonumber(mo), tonumber(d)) * 86400 + h * 3600 + mi * 60 + s
  local sign, oh, om = v:match('([+-])(%d%d):?(%d%d)$')
  if sign then
    t = t - (sign == '+' and 1 or -1) * (oh * 3600 + om * 60)
  end
  return t
end

local function resets(v)
  local t = M.parse_time(v)
  if not t then
    return ''
  end
  local d = t - os.time()
  return d > 0 and ('resets in ' .. M.duration(d)) or 'reset due'
end

---------------------------------------------------------------------------
-- winbars

local function session_part(name, e, narrow)
  if type(e) ~= 'table' then
    return name .. ' · (no session yet)'
  end
  local parts = { name .. ' · ' .. esc(M.short_model(e.model)) }
  local p = pct_of(e)
  if p then
    -- tokens used / window, and what share of the window that is
    table.insert(parts, 'ctx:' .. hl(group(p), (narrow and '' or (bar(p) .. ' ')) .. M.k(e.ctx_tokens) .. '/'
      .. M.k(e.ctx_window) .. ' (' .. p .. '%%)'))
  elseif tonumber(e.ctx_tokens) then
    table.insert(parts, 'ctx ' .. M.k(e.ctx_tokens) .. ' (?%%)') -- window learned on its next call
  end
  if e.backfilled then
    table.insert(parts, '(earlier)') -- from its transcript: no turn/cost history
  elseif name == 'worker' then
    if e.turn then
      table.insert(parts, 'turn ' .. esc(e.turn))
    end
  else
    local n = tonumber(e.turns) or 0
    local unit = name == 'compile' and 'run' or 'turn'
    table.insert(parts, n .. ' ' .. unit .. (n == 1 and '' or 's'))
    table.insert(parts, ('$%.2f'):format(tonumber(e.cost_usd) or 0))
  end
  return table.concat(parts, '  ')
end

local function quota_part(q)
  if type(q) ~= 'table' then
    return nil
  end
  local stale = (tonumber(q.age_s) or 0) > QUOTA_STALE
  local items = {}
  local function add(label, p)
    p = tonumber(p)
    if p then
      local s = label .. ':' .. math.floor(p + 0.5) .. '%%'
      table.insert(items, stale and s or hl(group(p), s))
    end
  end
  add('5h', q.five_hour)
  add('wk', q.weekly)
  for _, b in ipairs(type(q.buckets) == 'table' and q.buckets or {}) do
    if type(b) == 'table' and ((tonumber(b.percent) or 0) > 0 or b.isActive == true or b.active == true) then
      add(esc(tostring(b.id or b.label or '?'):lower()), b.percent)
    end
  end
  if #items == 0 then
    return nil
  end
  local s = table.concat(items, ' ')
  return stale and hl('AicUsageOk', '~' .. s) or s
end

function M.update()
  local u = type(state.usage) == 'table' and state.usage or {}
  local c = {}
  for win, name in pairs(SESSION) do
    c[win] = session_part(name, u[name])
    c[win .. '_narrow'] = session_part(name, u[name], true) -- no bar, for narrow windows
  end
  local q = quota_part(state.quota)
  if q then
    c.chat = c.chat .. '   ' .. q
    c.chat_narrow = c.chat_narrow .. '   ' .. q
  end
  if not vim.deep_equal(c, M.cache) then
    M.cache = c
    pcall(vim.cmd, 'redrawstatus!')
  end
  M.render()
end

-- Winbar expression: cached string, only in the aic window it was set for
-- (%{} items are evaluated with the winbar's window as the current one).
function M.winbar(name)
  local wins = require('aic.layout').wins or {}
  if api.nvim_get_current_win() ~= wins[name] then
    return ''
  end
  local full = M.cache[name] or ''
  local plain = full:gsub('%%#[^#]*#', ''):gsub('%%%*', ''):gsub('%%%%', '%%')
  if not M.force_full and vim.fn.strdisplaywidth(plain) >= api.nvim_win_get_width(0) then
    return M.cache[name .. '_narrow'] or full
  end
  return full
end

-- Set the window-local winbar on the aic windows (after (re)creating them).
function M.apply(wins)
  for name in pairs(SESSION) do
    local w = wins[name]
    if w and api.nvim_win_is_valid(w) then
      api.nvim_set_option_value('winbar', WINBAR:format(name), { win = w, scope = 'local' })
    end
  end
end

---------------------------------------------------------------------------
-- quota

function M.fetch_quota()
  if fetching or not state.root then
    return
  end
  fetching = true
  last_fetch = os.time()
  backend.run({ 'usage', '--json' }, state.root, function(ok, out)
    fetching = false
    local res = ok and util.decode(out)
    if type(res) ~= 'table' then
      return
    end
    state.quota = type(res.quota) == 'table' and res.quota or nil
    M.update()
  end)
end

-- Fetch the quota unless it was fetched in the last minute.
function M.poll()
  if os.time() - last_fetch >= QUOTA_EVERY then
    M.fetch_quota()
  end
end

local fetch_soon = util.debounce(1000, function()
  M.fetch_quota()
end)

local timer
function M.start()
  if timer then
    return
  end
  timer = uv.new_timer()
  timer:start(QUOTA_EVERY * 1000, QUOTA_EVERY * 1000, function()
    vim.schedule(function()
      local layout = require('aic.layout')
      if layout.tab and api.nvim_tabpage_is_valid(layout.tab) and api.nvim_get_current_tabpage() == layout.tab then
        M.poll()
      end
    end)
  end)
  pcall(timer.unref, timer)
  -- windows split off an aic window inherit its winbar: drop it there
  api.nvim_create_autocmd('WinNew', {
    group = api.nvim_create_augroup('aic_usage', { clear = true }),
    callback = function()
      local w = api.nvim_get_current_win()
      if vim.wo[w].winbar:find("aic.usage", 1, true) and not vim.tbl_contains(vim.tbl_values(require('aic.layout').wins or {}), w) then
        api.nvim_set_option_value('winbar', '', { win = w, scope = 'local' })
      end
    end,
  })
end

local function hint_key()
  return config.options.keymaps and (config.options.prefix .. 'U') or ':AicUsage'
end

-- usage.json changed: winbars, float, 80% hint, quota refresh.
function M.on_usage()
  local u = type(state.usage) == 'table' and state.usage or {}
  for _, name in ipairs({ 'compile', 'ask' }) do
    local e = u[name]
    local p = type(e) == 'table' and pct_of(e)
    local tokens = type(e) == 'table' and tonumber(e.ctx_tokens) or 0
    if p or tokens > 0 then
      local sid = tostring(e.session_id or name)
      local is_hot = (p and p >= HOT_PCT) or tokens >= HOT_TOKENS
      if is_hot and not hot[sid] then
        hot[sid] = true
        util.notify(('aic: %s context %s — %s then %s to compact (%s to reset)'):format(
          name, p and (p .. '%') or M.k(tokens), hint_key(), name == 'compile' and 'c' or 'C',
          name == 'compile' and 'r' or 'R'), vim.log.levels.WARN)
      elseif not is_hot then
        hot[sid] = nil
      end
    end
  end
  M.update()
  fetch_soon()
end

---------------------------------------------------------------------------
-- :AicUsage float

local function block(name, e)
  local out = {}
  if type(e) ~= 'table' then
    return { '## ' .. name, '', '(no session yet)', '' }
  end
  local sid = type(e.session_id) == 'string' and ('  `' .. e.session_id:sub(1, 8) .. '`') or ''
  table.insert(out, '## ' .. name .. ' · ' .. M.short_model(e.model) .. sid)
  table.insert(out, '')
  local p = pct_of(e)
  if p then
    table.insert(out, ('- ctx `%s` %s / %s (%d%%)'):format(bar(p), M.k(e.ctx_tokens), M.k(e.ctx_window), p))
  else
    table.insert(out, ('- ctx %s / ? (?%%): the window size is learned on its next call'):format(M.k(e.ctx_tokens)))
  end
  if e.backfilled then
    table.insert(out, '- read from its transcript (it predates usage tracking): no turn/cost history')
  elseif name == 'worker' then
    table.insert(out, '- turn ' .. tostring(e.turn or '?'))
  else
    local n = tonumber(e.turns) or 0
    table.insert(out, ('- %d %s%s · $%.2f'):format(n, name == 'compile' and 'run' or 'turn', n == 1 and '' or 's',
      tonumber(e.cost_usd) or 0))
  end
  if type(e.compacted_from) == 'string' and e.compacted_from ~= '' then
    table.insert(out, '- compacted from `' .. e.compacted_from:sub(1, 8) .. '`')
  end
  table.insert(out, '')
  return out
end

local function quota_block(q)
  if type(q) ~= 'table' then
    return { '## account quota', '', '(unavailable)', '' }
  end
  local age = tonumber(q.age_s)
  local out = { '## account quota' .. (age and ('  (updated ' .. M.duration(age) .. ' ago)') or ''), '' }
  local function line(label, p, at)
    if tonumber(p) then
      table.insert(out, ('- %s: %d%%  %s'):format(label, math.floor(p + 0.5), resets(at)))
    end
  end
  line('5h', q.five_hour, q.five_hour_resets_at)
  line('week', q.weekly, q.weekly_resets_at)
  for _, b in ipairs(type(q.buckets) == 'table' and q.buckets or {}) do
    if type(b) == 'table' then
      line(tostring(b.label or b.id or '?'), b.percent, b.resets_at)
    end
  end
  table.insert(out, '')
  return out
end

local function calls_block(u)
  local calls = {}
  for _, name in ipairs({ 'compile', 'ask' }) do
    local e = u[name]
    for _, c in ipairs(type(e) == 'table' and type(e.calls) == 'table' and e.calls or {}) do
      if type(c) == 'table' then
        table.insert(calls, { s = name, c = c })
      end
    end
  end
  table.sort(calls, function(a, b)
    return (tonumber(a.c.ts) or 0) > (tonumber(b.c.ts) or 0)
  end)
  local out = { '## last calls', '' }
  if #calls == 0 then
    return vim.list_extend(out, { '(none)', '' })
  end
  table.insert(out, '| time | session | note | in | cache_read | cache_write | out | cost |')
  table.insert(out, '|---|---|---|--:|--:|--:|--:|--:|')
  for i = 1, math.min(15, #calls) do
    local c = calls[i].c
    table.insert(out, ('| %s | %s | %s | %s | %s | %s | %s | $%.2f |'):format(
      tonumber(c.ts) and os.date('%H:%M', math.floor(c.ts)) or '?',
      calls[i].s,
      (tostring(c.note or ''):gsub('|', '/')),
      M.k(c['in']),
      M.k(c.cache_read),
      M.k(c.cache_write),
      M.k(c.out),
      tonumber(c.cost) or 0
    ))
  end
  table.insert(out, '')
  return out
end

local FOOTER = '_c compact compile · C compact ask · w compact worker · r reset compile · R reset ask · q close_\n\n'
  .. '_compact = one more call that re-reads the whole session for a summary (costs about one call at '
  .. 'that size); reset = free, the next call starts clean from the spec and trace._'

function M.lines()
  local u = type(state.usage) == 'table' and state.usage or {}
  local lines = {}
  for _, name in ipairs({ 'compile', 'ask', 'worker' }) do
    vim.list_extend(lines, block(name, u[name]))
  end
  vim.list_extend(lines, quota_block(state.quota))
  vim.list_extend(lines, calls_block(u))
  vim.list_extend(lines, vim.split(M.running and ('_compacting… (' .. M.running .. ') · q close_') or FOOTER, '\n'))
  return lines
end

-- Re-render the float if it is open.
function M.render()
  local f = M.float
  if not f or not api.nvim_win_is_valid(f.win) or not api.nvim_buf_is_valid(f.buf) then
    M.float = nil
    return
  end
  local lines = M.lines()
  util.set_lines(f.buf, lines)
  pcall(api.nvim_win_set_height, f.win, math.max(3, math.min(#lines, vim.o.lines - 6)))
end

-- Run `aic compact <which> [--reset]`; notify one line; re-render.
-- `worker` (no reset) types /compact into the worker's pane as the human.
function M.compact(which, reset)
  if which ~= 'compile' and which ~= 'ask' and not (which == 'worker' and not reset) then
    util.notify(reset and 'aic: reset compile|ask' or 'aic: compact compile|ask|worker', vim.log.levels.WARN)
    return
  end
  if M.running then
    util.notify('aic: a compact is already running', vim.log.levels.WARN)
    return
  end
  if not state.root then
    util.notify('aic: not active', vim.log.levels.WARN)
    return
  end
  local args = { 'compact', which }
  if reset then
    table.insert(args, '--reset')
  end
  M.running = which
  M.render()
  backend.run(args, state.root, function(ok, out, err, code)
    M.running = nil
    out, err = vim.trim(out or ''), vim.trim(err or '')
    if ok then
      util.notify(util.oneline(out ~= '' and out or ('aic: ' .. which .. (reset and ' reset' or ' compacted'))))
    else
      util.notify(util.oneline(('aic compact %s failed (%s): %s'):format(which, tostring(code), err ~= '' and err or out)),
        vim.log.levels.ERROR)
    end
    state.refresh()
    M.render()
  end)
end

local function confirm(which, reset)
  local what = (reset and 'reset' or 'compact') .. ' the ' .. which .. ' session'
  vim.ui.select({ 'yes', 'no' }, { prompt = 'aic: ' .. what .. '?' }, function(choice)
    if choice == 'yes' then
      M.compact(which, reset)
    end
  end)
end

function M.open()
  if M.float and api.nvim_win_is_valid(M.float.win) then
    api.nvim_set_current_win(M.float.win)
    M.render()
    return
  end
  local lines = M.lines()
  local buf, win = util.float(lines, { filetype = 'markdown', title = ' aic usage ', width = 96, height = #lines })
  vim.bo[buf].modifiable = false
  M.float = { buf = buf, win = win }
  local keys = {
    c = { 'compile', false },
    C = { 'ask', false },
    r = { 'compile', true },
    R = { 'ask', true },
    w = { 'worker', false },
  }
  for lhs, a in pairs(keys) do
    vim.keymap.set('n', lhs, function()
      confirm(a[1], a[2])
    end, { buffer = buf, nowait = true, desc = 'aic: ' .. (a[2] and 'reset ' or 'compact ') .. a[1] })
  end
  M.poll()
end

return M
