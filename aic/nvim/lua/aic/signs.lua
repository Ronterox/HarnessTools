-- Per-file signs on touched ranges (touched.json), ]w/[w and blame.
local state = require('aic.state')
local util = require('aic.util')

local api = vim.api
local M = {}

M.ns = api.nvim_create_namespace('aic_signs')
-- Signs are only drawn this many lines around what each window shows, so a big
-- write (thousands of touched lines) costs a screenful, not the whole range.
M.margin = 200
local applied = {} -- [buf] = { key = encoded ranges, list = ranges }

-- Lines (1-based, inclusive) around the visible part of each window on `buf`.
local function spans(buf)
  local out = {}
  for _, w in ipairs(vim.fn.win_findbuf(buf)) do
    local top, bot = unpack(api.nvim_win_call(w, function()
      return { vim.fn.line('w0'), vim.fn.line('w$') }
    end))
    table.insert(out, { top - M.margin, bot + M.margin })
  end
  return out
end

-- (Re)draw the signs of `buf` for its visible spans.
local function draw(buf)
  api.nvim_buf_clear_namespace(buf, M.ns, 0, -1)
  local a = applied[buf]
  if not a or #a.list == 0 then
    return
  end
  local n = api.nvim_buf_line_count(buf)
  local done = {}
  for _, sp in ipairs(spans(buf)) do
    for _, r in ipairs(a.list) do
      local s = math.max(1, tonumber(r.start) or 1, sp[1])
      local e = math.min(n, tonumber(r['end']) or tonumber(r.start) or 1, sp[2])
      local hl = r.actor == 'human' and 'AicHuman' or 'AicWorker'
      for l = s, e do
        if not done[l] then
          done[l] = true
          api.nvim_buf_set_extmark(buf, M.ns, l - 1, 0, { sign_text = '▎', sign_hl_group = hl, priority = 5 })
        end
      end
    end
  end
end

local redraw_visible -- created once (one timer for the whole plugin)
local function autocmds()
  local g = api.nvim_create_augroup('aic_signs', { clear = true })
  redraw_visible = util.debounce(100, function()
    for _, w in ipairs(api.nvim_tabpage_list_wins(0)) do
      local b = api.nvim_win_get_buf(w)
      if applied[b] and api.nvim_buf_is_loaded(b) then
        draw(b)
      end
    end
  end)
  api.nvim_create_autocmd('WinScrolled', { group = g, callback = redraw_visible })
  api.nvim_create_autocmd('BufWinEnter', {
    group = g,
    callback = function(ev)
      if applied[ev.buf] then
        draw(ev.buf)
      end
    end,
  })
  api.nvim_create_autocmd('BufWipeout', {
    group = g,
    callback = function(ev)
      applied[ev.buf] = nil
    end,
  })
end

-- Place the signs of `buf` from state.touched. Without `force`, a buffer whose
-- ranges did not change since the last apply is left alone.
function M.apply(buf, force)
  if not api.nvim_buf_is_loaded(buf) then
    return
  end
  if not redraw_visible then
    autocmds()
  end
  local list
  if state.active() and vim.bo[buf].buftype == '' then
    local rel = util.rel(state.root, api.nvim_buf_get_name(buf))
    list = rel and state.touched[rel]
  end
  if type(list) ~= 'table' or #list == 0 then
    if applied[buf] then
      applied[buf] = nil
      api.nvim_buf_clear_namespace(buf, M.ns, 0, -1)
    end
    return
  end
  local key = vim.json.encode(list)
  if not force and applied[buf] and applied[buf].key == key then
    return
  end
  applied[buf] = { key = key, list = list }
  draw(buf)
  if not vim.b[buf].aic_wmaps then
    vim.b[buf].aic_wmaps = true
    vim.keymap.set('n', ']w', function()
      M.jump(1)
    end, { buffer = buf, desc = 'aic: next touched range' })
    vim.keymap.set('n', '[w', function()
      M.jump(-1)
    end, { buffer = buf, desc = 'aic: previous touched range' })
  end
end

-- Re-apply every loaded buffer whose touched ranges changed.
function M.apply_all()
  for _, b in ipairs(api.nvim_list_bufs()) do
    if api.nvim_buf_is_loaded(b) then
      M.apply(b)
    end
  end
end

-- Jump to the start of the next/previous touched range (adjacent or
-- overlapping ranges count as one).
function M.jump(dir)
  local a = applied[api.nvim_get_current_buf()]
  local n = api.nvim_buf_line_count(0)
  local rs = {}
  for _, r in ipairs(a and a.list or {}) do
    local s = math.max(1, tonumber(r.start) or 1)
    local e = math.min(n, tonumber(r['end']) or s)
    if s <= e then
      table.insert(rs, { s, e })
    end
  end
  table.sort(rs, function(x, y)
    return x[1] < y[1]
  end)
  local starts, last = {}, -1
  for _, r in ipairs(rs) do
    if r[1] > last + 1 then
      table.insert(starts, r[1])
    end
    last = math.max(last, r[2])
  end
  local cur = api.nvim_win_get_cursor(0)[1]
  local target
  for i = dir > 0 and 1 or #starts, dir > 0 and #starts or 1, dir > 0 and 1 or -1 do
    if (dir > 0 and starts[i] > cur) or (dir < 0 and starts[i] < cur) then
      target = starts[i]
      break
    end
  end
  if target then
    api.nvim_win_set_cursor(0, { target, 0 })
    return target
  end
  util.notify('aic: no ' .. (dir > 0 and 'next' or 'previous') .. ' touched range')
end

-- Float with the trace event for the touched range under the cursor.
function M.blame()
  local buf = api.nvim_get_current_buf()
  local row = api.nvim_win_get_cursor(0)[1]
  local r
  for _, x in ipairs(applied[buf] and applied[buf].list or {}) do
    local s = tonumber(x.start) or 1
    if row >= s and row <= (tonumber(x['end']) or s) then
      r = x
      break
    end
  end
  if not r then
    util.notify('aic: line not touched')
    return
  end
  local ev = state.by_id[tostring(r.event)]
  local lines = {}
  if not ev then
    lines = { ('# %s %s-%s'):format(r.actor or 'worker', r.start, r['end']), '', '(event ' .. tostring(r.event) .. ' not in trace)' }
  else
    local title = ev.title or (ev.kind == 'human' and ('human ' .. (ev.file or '')) or ev.kind)
    table.insert(lines, ('# %s'):format(title))
    table.insert(lines, ('_%s · turn %s · %s_'):format(ev.id, tostring(ev.turn), os.date('%H:%M', math.floor(tonumber(ev.ts) or 0))))
    if ev.why and ev.why ~= '' then
      table.insert(lines, '')
      vim.list_extend(lines, util.split(vim.trim(ev.why)))
    end
    if ev.hunks and #ev.hunks > 0 then
      vim.list_extend(lines, { '', '```diff' })
      for _, h in ipairs(ev.hunks) do
        table.insert(lines, ('@@ +%s,%s @@'):format(h.start, h.count))
        vim.list_extend(lines, h.lines or {})
      end
      table.insert(lines, '```')
    end
    if state.seenable(ev) then
      state.set_seen({ ev.id }, true)
    end
  end
  util.float(lines, { title = ' aic blame ', filetype = 'markdown' })
end

return M
