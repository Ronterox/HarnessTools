-- Spec buffer: <dir>/spec.md, `:w` compiles, diagnostics, anchors (`gd`).
local backend = require('aic.backend')
local state = require('aic.state')
local util = require('aic.util')

local api = vim.api
local M = {}

M.ns = api.nvim_create_namespace('aic')
local last_compiled_at = nil

M.template = {
  '# Spec',
  '<!-- aic: pseudocode outline. Anchors: @path/file.ext, @path/file.ext:symbol, @path/file.ext:42 (gd jumps). :w compiles. -->',
  '',
}

function M.path()
  return state.dir and (state.dir .. '/spec.md') or nil
end

-- Loaded buffer for spec.md, or nil.
function M.bufnr()
  local p = M.path()
  if not p then
    return nil
  end
  local b = vim.fn.bufnr(p)
  if b > 0 and api.nvim_buf_is_loaded(b) then
    return b
  end
end

function M.ensure_file()
  local p = M.path()
  if not p then
    return nil
  end
  vim.fn.mkdir(state.dir, 'p')
  if not util.exists(p) then
    util.write_file(p, table.concat(M.template, '\n') .. '\n')
  end
  return p
end

-- The spec buffer (loaded, listed), or a placeholder when there is no session.
function M.get_buf()
  local p = M.ensure_file()
  if not p then
    if not (M.placeholder and api.nvim_buf_is_valid(M.placeholder)) then
      M.placeholder = api.nvim_create_buf(false, true)
      pcall(api.nvim_buf_set_name, M.placeholder, 'aic://spec')
      vim.bo[M.placeholder].bufhidden = 'hide'
      api.nvim_buf_set_lines(M.placeholder, 0, -1, false, { '(no worker session — run :AicAttach)' })
      vim.bo[M.placeholder].modifiable = false
    end
    return M.placeholder
  end
  local b = vim.fn.bufadd(p)
  vim.fn.bufload(b)
  vim.bo[b].buflisted = true
  return b
end

---------------------------------------------------------------------------
-- compile + diagnostics

-- Always runs `aic compile`: during a running compile the backend queues
-- one more pass on the latest spec and exits 0 with "compile queued: ...".
local running = 0
function M.compile()
  if not state.root then
    return
  end
  running = running + 1
  state.set_local_compiling(true)
  backend.run({ 'compile' }, state.root, function(ok, out, err, code)
    running = running - 1
    if ok and out:match('^%s*compile queued') then
      state.compile_queued = true
      util.notify('aic: compile queued')
    elseif not ok then
      util.notify(
        util.oneline(('aic compile failed (%s): %s'):format(code, vim.trim(err ~= '' and err or out))),
        vim.log.levels.ERROR
      )
    end
    if running == 0 and not state.compiling then
      state.compile_queued = false
    end
    state.set_local_compiling(running > 0)
    state.refresh() -- diagnostics.json watcher fills quickfix + notifies
  end)
end

local sev_type = { error = 'E', warning = 'W', info = 'I' }
local sev_diag = {
  error = vim.diagnostic.severity.ERROR,
  warning = vim.diagnostic.severity.WARN,
  info = vim.diagnostic.severity.INFO,
}

function M.counts(d)
  local c = { error = 0, warning = 0, info = 0 }
  for _, it in ipairs((d and d.items) or {}) do
    local s = c[it.severity] and it.severity or 'info'
    c[s] = c[s] + 1
  end
  return c
end

local function qf_items(d)
  local items = {}
  local spec = M.path()
  for _, it in ipairs(d.items or {}) do
    local file = it.file == 'spec' and spec or util.abs(state.root, it.file or 'spec')
    local lnum = it.file == 'spec' and (it.line or it.spec_line) or it.line
    table.insert(items, {
      filename = file,
      lnum = tonumber(lnum) or 1,
      col = 1,
      type = sev_type[it.severity] or 'I',
      text = ('[%s] %s'):format(it.kind or '?', it.message or ''),
      user_data = it,
    })
  end
  return items
end

function M.load_qf(d)
  d = d or state.diagnostics
  if not d or d.error then
    return false
  end
  local cur = vim.fn.getqflist({ title = 0 }).title
  vim.fn.setqflist({}, cur == 'aic compile' and 'r' or ' ', { title = 'aic compile', items = qf_items(d) })
  return true
end

function M.apply_diagnostics(buf)
  buf = buf or M.bufnr()
  if not buf then
    return
  end
  local d = state.diagnostics
  local out = {}
  local n = api.nvim_buf_line_count(buf)
  for _, it in ipairs((d and not d.error and d.items) or {}) do
    local l = it.file == 'spec' and (it.line or it.spec_line) or it.spec_line
    l = tonumber(l)
    if l then
      table.insert(out, {
        lnum = math.max(0, math.min(l, n) - 1),
        col = 0,
        severity = sev_diag[it.severity] or vim.diagnostic.severity.INFO,
        message = ('[%s] %s'):format(it.kind or '?', it.message or ''),
        source = 'aic',
      })
    end
  end
  vim.diagnostic.set(M.ns, buf, out)
end

-- state 'diagnostics' listener
function M.on_diagnostics(d, initial)
  M.apply_diagnostics()
  if type(d) ~= 'table' then
    return
  end
  local stamp = d.compiled_at
  if initial then
    last_compiled_at = stamp
    return
  end
  if stamp ~= nil and stamp == last_compiled_at then
    return
  end
  last_compiled_at = stamp
  if d.error then
    util.notify(util.oneline('aic compile error: ' .. tostring(d.error)), vim.log.levels.ERROR)
    return
  end
  M.load_qf(d)
  local c = M.counts(d)
  local summary = util.first_line(d.summary)
  util.notify(
    util.oneline(('aic compile: %dE %dW %dI%s'):format(c.error, c.warning, c.info, summary ~= '' and (' — ' .. summary) or '')),
    c.error > 0 and vim.log.levels.WARN or vim.log.levels.INFO
  )
end

---------------------------------------------------------------------------
-- anchors

-- Anchor under cursor (see aic.anchors): returns path, line or symbol; or nil.
function M.anchor_at(line, col)
  local a = require('aic.anchors').anchor_at(line, col)
  if a then
    return a.path, a.line or a.symbol
  end
end

-- Set up a spec.md buffer (called from BufReadPost/BufNewFile).
function M.attach(buf)
  if vim.b[buf].aic_spec then
    return
  end
  vim.b[buf].aic_spec = true
  vim.bo[buf].swapfile = false
  require('aic.anchors').attach(buf)
  api.nvim_create_autocmd('BufWritePost', {
    buffer = buf,
    callback = function()
      M.compile()
    end,
  })
  api.nvim_create_autocmd('BufWinEnter', {
    buffer = buf,
    callback = function()
      local win = api.nvim_get_current_win()
      if require('aic.view').name_of(buf) == 'spec' then
        require('aic.usage').set_winbar(win, 'spec')
      else -- another session's spec: no (empty) winbar row
        vim.wo[win][0].winbar = ''
      end
    end,
  })
  M.apply_diagnostics(buf)
end

---------------------------------------------------------------------------
-- appending (ask/what-if answers)

-- Append `body` lines under `heading` at the end of spec.md.
-- reuse=true: don't repeat the heading if it is already the last `## ` heading.
-- Returns the first and last line numbers of the appended heading + body.
function M.append_section(heading, body, reuse)
  local p = M.ensure_file()
  if not p then
    util.notify('aic: no session spec', vim.log.levels.WARN)
    return
  end
  local buf = M.bufnr()
  local cur = buf and api.nvim_buf_get_lines(buf, 0, -1, false) or util.read_lines(p) or {}
  local last_h2
  for i = #cur, 1, -1 do
    if cur[i]:match('^## ') then
      last_h2 = cur[i]
      break
    end
  end
  local add = {}
  if #cur > 0 and cur[#cur] ~= '' then
    table.insert(add, '')
  end
  if heading and not (reuse and last_h2 == heading) then
    table.insert(add, heading)
    table.insert(add, '')
  end
  local first = #cur + 1 + ((#cur > 0 and cur[#cur] ~= '') and 1 or 0)
  vim.list_extend(add, body)
  local last = #cur + #add
  if buf then
    api.nvim_buf_set_lines(buf, -1, -1, false, add)
    util.notify('aic: appended to spec.md (unsaved — :w compiles)')
  else
    vim.list_extend(cur, add)
    util.write_file(p, table.concat(cur, '\n') .. '\n')
    util.notify('aic: appended to spec.md')
  end
  return first, last
end

return M
