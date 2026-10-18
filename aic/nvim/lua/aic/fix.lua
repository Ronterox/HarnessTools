-- :AicFix: the compiler proposes edits to the current doc that fix its
-- diagnostics (`aic fix`). A float lists them: <Space> picks (fixes that touch
-- the same text act like radio buttons: picking one unpicks the others), <Tab>
-- shows the edits, <CR> applies the picked ones to the doc buffer in one undo
-- step. Nothing is saved: review, then :w (or u).
local backend = require('aic.backend')
local state = require('aic.state')
local util = require('aic.util')

local api = vim.api
local M = {
  last = nil, -- the last proposal shown
  by_doc = {}, -- doc -> its last proposal (reopened by :AicFix!)
}
local ns = api.nvim_create_namespace('aic-fix')

local MARKER = '^%s*<!%-%-%s*aic:.-%-%->%s*$'

-- The doc's text as its buffer (or file) has it now, plus its lines.
local function doc_text(name)
  local doc = require('aic.doc')
  local b = doc.bufnr(name)
  local lines = b and api.nvim_buf_get_lines(b, 0, -1, false) or vim.tbl_map(function(l)
    return (l:gsub('\r$', '')) -- (a CRLF file, as its buffer will show it)
  end, util.read_lines(doc.path(name)) or {})
  return table.concat(lines, '\n') .. '\n', lines
end

-- 1-based line of 0-based byte offset `off` in `text`.
local function line_of(text, off)
  local _, n = text:sub(1, off):gsub('\n', '')
  return n + 1
end

-- Where fix `f`'s edits are in `text` now: { s, e (0-based bytes, end
-- exclusive), new, line }, or nil + why it can't be applied.
local function locate(f, text, lines)
  local protect = (lines[1] or ''):match(MARKER) and #lines[1] or -1
  local out = {}
  for _, e in ipairs(f.edits) do
    local s = text:find(e.old, 1, true)
    if not s then
      return nil, 'stale: its text is no longer in the doc'
    elseif text:find(e.old, s + 1, true) then
      return nil, 'stale: its text now occurs more than once'
    elseif s - 1 <= protect then
      return nil, 'it changes the aic marker line'
    end
    table.insert(out, { s = s - 1, e = s - 1 + #e.old, new = e.new, line = line_of(text, s - 1) })
  end
  return out
end

-- Every edit's new text is in `text` already, around the text it replaces:
-- an applied insertion (or a new text that contains the old) still finds its
-- old text, and must not be applied twice.
local function in_doc(f, ranges, text)
  for i, e in ipairs(f.edits) do
    local r, found, from = ranges[i], false, 1
    while #e.new > #e.old and not found do
      local p = text:find(e.new, from, true)
      if not p then
        break
      end
      found = p - 1 <= r.s and p - 1 + #e.new >= r.e
      from = p + 1
    end
    if not found then
      return false
    end
  end
  return true
end

-- `text` with the (non-overlapping) byte ranges `edits` replaced.
local function splice(text, edits)
  table.sort(edits, function(x, y)
    return x.s > y.s
  end)
  for _, e in ipairs(edits) do
    text = text:sub(1, e.s) .. e.new .. text:sub(e.e + 1)
  end
  return text
end

local function undotree(buf)
  return api.nvim_buf_call(buf, function()
    return vim.fn.undotree()
  end)
end

-- Is undo change `seq` part of `buf`'s current state, i.e. on the undo-tree
-- path to it? (Undone, or undone and then edited on a new branch: no.)
local function in_effect(buf, seq)
  local tree = undotree(buf)
  local cur = tree.seq_cur
  local function upto(list, i)
    for j = 1, i do
      if list[j].seq == seq then
        return true
      end
    end
    return false
  end
  -- true/false once `cur` is found in this branch; an `alt` replaces its entry
  local function walk(list)
    for i, e in ipairs(list) do
      for _, alt in ipairs(e.alt or {}) do
        local r = walk(alt)
        if r ~= nil then
          return r or upto(list, i - 1)
        end
      end
      if e.seq == cur then
        return upto(list, i)
      end
    end
  end
  local ul = vim.bo[buf].undolevels
  if ul == -123456 then -- (unset: the global value)
    ul = vim.go.undolevels
  end
  local oldest = math.huge -- (what 'undolevels' trimmed is not in the tree)
  local function scan(list)
    for _, e in ipairs(list) do
      oldest = math.min(oldest, e.seq)
      for _, alt in ipairs(e.alt or {}) do
        scan(alt)
      end
    end
  end
  scan(tree.entries)
  if tree.seq_last < seq or ul < 0 or seq < oldest then
    return true -- the history that had it is gone: assume it still is (never apply twice)
  end
  return cur > 0 and walk(tree.entries) == true
end

---------------------------------------------------------------------------
-- the float

-- Applied fixes follow the undo tree of their buffer: undone, they can be
-- picked again; redone, they are applied again; edited after the apply, they
-- stay applied.
-- Then what can't be picked: stale, already in the doc, or an alternative of
-- an applied fix. f.ranges = where its edits are now.
local function refresh_stale(entry)
  local text, lines = doc_text(entry.doc)
  for _, f in ipairs(entry.fixes) do
    if f.seq and f.buf and api.nvim_buf_is_loaded(f.buf) then -- (undo and redo)
      f.applied = in_effect(f.buf, f.seq)
    end
  end
  for _, f in ipairs(entry.fixes) do
    f.stale, f.ranges = nil, nil
    if not f.applied then
      local ranges, why = locate(f, text, lines)
      if ranges and in_doc(f, ranges, text) then
        why = 'its text is already in the doc'
      end
      for _, id in ipairs(f.conflicts or {}) do
        if not why and (entry.fixes[id] or {}).applied then
          why = ('its alternative %d is applied'):format(id)
        end
      end
      f.stale, f.ranges = why, ranges
      if why then
        f.picked = false
      end
    end
  end
end

local function oneline(s)
  return (tostring(s or ''):gsub('%s+', ' '))
end

function M.render(entry)
  local buf = entry.buf
  if not (buf and api.nvim_buf_is_valid(buf)) then
    return
  end
  refresh_stale(entry)
  local lines, marks, at = {}, {}, {}
  local function add(s, group, s_col, e_col)
    table.insert(lines, s)
    if group then
      table.insert(marks, { #lines - 1, group, s_col, e_col })
    end
  end
  for _, f in ipairs(entry.fixes) do
    local box = f.applied and '(✓)' or f.picked and '(•)' or '( )'
    local where = 'doc:' .. ((f.ranges or f.edits)[1] or {}).line
    local note = f.applied and '  applied' or f.stale and ('  ' .. f.stale)
      or (#(f.conflicts or {}) > 0 and ('  overlaps ' .. table.concat(f.conflicts, ', ')) or '')
    at[#lines + 1] = f
    add(('%s %d. %s  %s%s'):format(box, f.id, oneline(f.title), where, note),
      (f.applied or f.stale) and 'Comment' or f.picked and 'Title' or nil)
    if f.picked then
      table.insert(marks, { #lines - 1, 'Special', 0, #box })
    end
    local first = #lines + 1
    if f.message then
      add(('      [%s] %s'):format(f.kind or '?', oneline(f.message)), 'Comment')
    end
    if f.why ~= nil and f.why ~= '' then
      add('      why: ' .. oneline(f.why), 'Comment')
    end
    if entry.open[f.id] then
      for i, e in ipairs(f.edits) do
        add(('      @@ line %d'):format(((f.ranges or f.edits)[i] or e).line), 'Identifier')
        for _, l in ipairs(vim.split(e.old, '\n', { plain = true })) do
          add('      - ' .. l, 'Removed')
        end
        for _, l in ipairs(vim.split(e.new, '\n', { plain = true })) do
          add('      + ' .. l, 'Added')
        end
      end
    end
    for i = first, #lines do
      at[i] = f
    end
  end
  if #entry.fixes == 0 then
    add('No fixes proposed.', 'Comment')
  end
  if #(entry.rejected or {}) + #(entry.skipped or {}) > 0 then
    add('')
  end
  for _, r in ipairs(entry.rejected or {}) do
    add(('✗ unusable: %s (%s)'):format(oneline(r.title), oneline(r.reason)), 'Comment')
  end
  for _, s in ipairs(entry.skipped or {}) do
    add(('– not a doc fix: #%s %s — %s'):format(s.item or '?', oneline(s.message), oneline(s.reason)), 'Comment')
  end
  add('')
  add('<Space> pick · <Tab> show edits · a pick all/none · <CR> apply picked · q close', 'Comment')
  entry.at = at
  util.set_lines(buf, lines)
  api.nvim_buf_clear_namespace(buf, ns, 0, -1)
  for _, m in ipairs(marks) do
    api.nvim_buf_set_extmark(buf, ns, m[1], m[3] or 0, { end_col = m[4] or #lines[m[1] + 1], hl_group = m[2] })
  end
end

local function fix_at_cursor(entry)
  return entry.at and entry.at[api.nvim_win_get_cursor(0)[1]]
end

-- Keep the cursor on fix `f`'s first line after a re-render.
local function cursor_to(entry, f)
  for l = 1, api.nvim_buf_line_count(entry.buf) do
    if entry.at[l] == f then
      pcall(api.nvim_win_set_cursor, 0, { l, 0 })
      return
    end
  end
end

-- Pick/unpick `f`. Fixes that change the same text are alternatives: picking
-- one unpicks the others (radio buttons).
local function toggle(entry, f)
  if f.applied or f.stale then
    util.notify(('aic: fix %d: %s'):format(f.id, f.applied and 'already applied' or f.stale))
    return
  end
  f.picked = not f.picked
  if f.picked then
    for _, id in ipairs(f.conflicts or {}) do
      local g = entry.fixes[id]
      if g and g.picked then
        g.picked = false
        util.notify(('aic: fix %d unpicked (it changes the same text as %d)'):format(id, f.id))
      end
    end
  end
end

local function pick_all(entry)
  local any = false
  for _, f in ipairs(entry.fixes) do
    any = any or f.picked
  end
  for _, f in ipairs(entry.fixes) do
    f.picked = false
  end
  if any then
    return
  end
  for _, f in ipairs(entry.fixes) do
    if not (f.applied or f.stale) then
      local free = true
      for _, id in ipairs(f.conflicts or {}) do
        free = free and not (entry.fixes[id] or {}).picked
      end
      f.picked = free
    end
  end
end

-- Apply the picked fixes to the doc buffer: all or none, one undo step.
function M.apply(entry)
  refresh_stale(entry) -- (unpicks what can't be applied any more)
  local picked = vim.tbl_filter(function(f)
    return f.picked and not f.applied
  end, entry.fixes)
  if #picked == 0 then
    util.notify('aic: pick fixes with <Space> first')
    return M.render(entry)
  end
  local doc = require('aic.doc')
  local b = doc.get_buf(entry.doc)
  if not b then
    return
  end
  local text, lines = doc_text(entry.doc)
  local edits = {}
  for _, f in ipairs(picked) do
    local ranges, why = locate(f, text, lines)
    if not ranges then
      util.notify(('aic: fix %d: %s; nothing applied'):format(f.id, why), vim.log.levels.WARN)
      return M.render(entry)
    end
    vim.list_extend(edits, ranges)
  end
  table.sort(edits, function(x, y)
    return x.s < y.s
  end)
  for i = 2, #edits do
    if edits[i - 1].e > edits[i].s then
      util.notify('aic: two picked fixes change the same text; pick one', vim.log.levels.WARN)
      return
    end
  end
  local new = splice(text, edits)
  if not (lines[1] or ''):match(MARKER) and new:match('^[^\n]*'):match(MARKER) then
    util.notify('aic: these fixes would add an aic marker line; nothing applied', vim.log.levels.WARN)
    return
  end
  if not vim.bo[b].modifiable then
    util.notify('aic: ' .. entry.doc .. ' is not modifiable', vim.log.levels.WARN)
    return
  end
  -- one set_lines over the changed lines only: one undo step, and marks
  -- (diagnostics, signs) outside them stay where they are
  local new_lines = vim.split((new:gsub('\n$', '')), '\n', { plain = true })
  local p, q = 0, 0
  while p < #lines and p < #new_lines and lines[p + 1] == new_lines[p + 1] do
    p = p + 1
  end
  while q < #lines - p and q < #new_lines - p and lines[#lines - q] == new_lines[#new_lines - q] do
    q = q + 1
  end
  local items = {}
  for _, f in ipairs(picked) do
    table.insert(items, f.item)
  end
  if p == #lines - q and p == #new_lines - q then
    util.notify('aic: the picked fixes change no lines; nothing applied')
    return
  end
  doc.resolve_items(entry.doc, entry.compiled_at, items) -- (before the change: the rest keep following their lines)
  local ok, err = pcall(api.nvim_buf_call, b, function()
    vim.cmd('let &g:undolevels = &g:undolevels') -- its own undo step, never joined to an earlier API change
    api.nvim_buf_set_lines(b, p, #lines - q, false, vim.list_slice(new_lines, p + 1, #new_lines - q))
  end)
  if not ok then
    doc.resolve_items(entry.doc, entry.compiled_at, items, true)
    util.notify('aic: applying failed: ' .. tostring(err), vim.log.levels.ERROR)
    return
  end
  local seq = undotree(b).seq_cur
  for _, f in ipairs(picked) do
    f.applied, f.picked, f.buf, f.seq = true, false, b, seq
  end
  if entry.win and api.nvim_win_is_valid(entry.win) then
    api.nvim_win_close(entry.win, true)
  end
  local win = require('aic.view').show_buf(b)
  if win and api.nvim_win_get_buf(win) == b then
    pcall(api.nvim_win_set_cursor, win, { math.min(p + 1, api.nvim_buf_line_count(b)), 0 })
  end
  util.notify(('aic: applied %d fix%s to %s (unsaved; u undoes them all)'):format(
    #picked, #picked == 1 and '' or 'es', entry.doc))
end

function M.show(entry)
  local buf, win = util.float({ '' }, { title = ' aic fix: ' .. entry.doc .. ' ', width = 100, height = 24 })
  vim.bo[buf].modifiable = false
  vim.wo[win].cursorline = true
  entry.buf, entry.win = buf, win
  local function on(key, fn, desc)
    vim.keymap.set('n', key, function()
      if not entry.fixes then
        util.notify('aic: no fixes yet')
        return
      end
      fn()
    end, { buffer = buf, nowait = true, desc = 'aic: ' .. desc })
  end
  local function pick()
    local f = fix_at_cursor(entry)
    if f then
      toggle(entry, f)
      M.render(entry)
      cursor_to(entry, f)
    end
  end
  on('<Space>', pick, 'pick fix')
  on('x', pick, 'pick fix')
  on('<Tab>', function()
    local f = fix_at_cursor(entry)
    if f then
      entry.open[f.id] = not entry.open[f.id]
      M.render(entry)
      cursor_to(entry, f)
    end
  end, 'show edits')
  on('a', function()
    pick_all(entry)
    M.render(entry)
  end, 'pick all/none')
  on('<CR>', function()
    M.apply(entry)
  end, 'apply picked fixes')
  if entry.fixes then
    M.render(entry)
  else
    util.set_lines(buf, { '_asking the compiler for fixes to ' .. entry.doc .. '…_' })
  end
end

-- Proposal `res` (`aic fix --json` / fixes.<doc>.json) into `entry`.
local function fill(entry, res)
  entry.doc = res.doc or entry.doc
  entry.compiled_at = res.compiled_at
  entry.created_at = res.created_at
  entry.rejected = res.rejected or {}
  entry.skipped = res.skipped or {}
  entry.fixes = res.fixes or {}
  entry.open = entry.open or {}
  return entry
end

-- :AicFix [range]: ask for fixes to the current doc (range: only the items
-- about those lines). The doc's unsaved text is what the compiler sees.
function M.start(o)
  o = o or {}
  local doc = require('aic.doc')
  local name = doc.current()
  if not util.exists(doc.path(name)) then
    util.notify('aic: no doc ' .. name, vim.log.levels.WARN)
    return
  end
  local args = { 'fix', '--doc', name, '--json' }
  if o.range and o.range > 0 then
    if doc.name_of(api.nvim_get_current_buf()) ~= name then
      util.notify('aic: a range must be lines of a doc', vim.log.levels.WARN)
      return
    end
    vim.list_extend(args, { '--from', tostring(o.line1), '--to', tostring(o.line2) })
  end
  local tmp
  if doc.bufnr(name) then
    tmp = vim.fn.tempname() .. '.md'
    util.write_file(tmp, (doc_text(name)))
    vim.list_extend(args, { '--content-file', tmp })
  end
  local entry = { doc = name, open = {} }
  M.show(entry)
  backend.run(args, state.root, function(ok, out, err, code)
    if tmp then
      os.remove(tmp)
    end
    state.refresh()
    local res = ok and util.decode(out)
    if type(res) ~= 'table' then
      local msg = ('aic fix failed (%s): %s'):format(code, vim.trim(err ~= '' and err or out))
      if api.nvim_buf_is_valid(entry.buf) then
        util.set_lines(entry.buf, util.split(msg))
      else
        util.notify(util.oneline(msg), vim.log.levels.ERROR)
      end
      return
    end
    M.last = fill(entry, res)
    M.by_doc[entry.doc] = entry
    if api.nvim_buf_is_valid(entry.buf) then
      M.render(entry)
    else
      util.notify(('aic: %d fix%s for %s ready: :AicFix! shows them'):format(
        #entry.fixes, #entry.fixes == 1 and '' or 'es', entry.doc))
    end
  end)
end

-- :AicFix!: the current doc's last proposal again (what was applied stays marked).
function M.reopen()
  local name = require('aic.doc').current()
  local entry = M.by_doc[name]
  if not (entry and entry.fixes) then
    local res = state.dir and util.read_json(state.dir .. '/fixes.' .. name .. '.json')
    if type(res) ~= 'table' then
      util.notify('aic: no fixes proposed for ' .. name .. ' yet (:AicFix asks for them)')
      return
    end
    entry = fill({ doc = name }, res)
    M.by_doc[name] = entry
  end
  M.last = entry
  M.show(entry)
end

return M
