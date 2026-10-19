-- Docs: the human's files, <root>/.aic/docs/<name>.md, shared by every worker
-- session. A first-line marker `<!-- aic: compile prompt=x -->` makes `:w`
-- compile the doc. Diagnostics come per doc from the session dir. Anchors
-- (`gd`, highlighting, omnifunc) work in every doc.
local backend = require('aic.backend')
local config = require('aic.config')
local state = require('aic.state')
local util = require('aic.util')

local api = vim.api
local uv = vim.uv or vim.loop
local M = {
  DEFAULT = 'spec',
  last = nil, -- name of the last doc entered
}

M.ns = api.nvim_create_namespace('aic')
local last_compiled_at = {} -- doc -> compiled_at already reported

local NAME = '^[A-Za-z_][A-Za-z0-9_.-]*$'

function M.valid(name)
  return type(name) == 'string' and name:match(NAME) ~= nil and not name:find('..', 1, true)
end

function M.dir()
  return state.root and (state.root .. '/.aic/docs') or nil
end

-- Path of doc `name` (default: the current doc).
function M.path(name)
  local d = M.dir()
  name = name or M.current()
  return d and M.valid(name) and (d .. '/' .. name .. '.md') or nil
end

-- Doc name of `buf`, or nil when it is not a doc.
function M.name_of(buf)
  local d = M.dir()
  if not (d and buf and api.nvim_buf_is_valid(buf)) then
    return nil
  end
  local name = vim.fs.normalize(api.nvim_buf_get_name(buf))
  local n = name:match('^.*/(.+)%.md$')
  if not M.valid(n) then
    return nil
  end
  -- same dir as written, or the same real dir (one of them through a symlink)
  local bdir = vim.fs.dirname(name)
  if bdir == vim.fs.normalize(d) or (uv.fs_realpath(bdir) or bdir) == (uv.fs_realpath(d) or d) then
    return n
  end
end

-- The doc being worked on: the current buffer's, else the last doc entered,
-- else the default (`spec`).
function M.current()
  local cur = M.name_of(api.nvim_get_current_buf())
  if cur then
    return cur
  end
  if M.valid(M.last) and util.exists(M.path(M.last)) then -- (not one deleted since)
    return M.last
  end
  return M.DEFAULT
end

-- Loaded buffer of doc `name`, or nil.
function M.bufnr(name)
  local p = M.path(name)
  if not p then
    return nil
  end
  local b = vim.fn.bufnr(p)
  if b > 0 and api.nvim_buf_is_loaded(b) then
    return b
  end
end

-- Settings from a doc's first line: { compile = bool, prompt = name|nil,
-- compile_template = name|nil } (`compile=NAME`: compiled with that template).
function M.marker(first)
  local out = { compile = false }
  local body = type(first) == 'string' and first:match('^%s*<!%-%-%s*aic:(.-)%-%->%s*$')
  for tok in (body or ''):gmatch('%S+') do
    local k, v = tok:match('^([^=]+)=?(.*)$')
    if k == 'compile' then
      out.compile = true
      out.compile_template = v ~= '' and v or nil
    elseif k == 'prompt' and v ~= '' then
      out.prompt = v
    end
  end
  return out
end

-- Create doc `name` if missing. The first time the docs dir is created, the
-- backend migrates the old per-session spec.md first (`aic docs`).
function M.ensure_file(name, compile)
  local p = M.path(name)
  if not p then
    return nil
  end
  if not util.exists(M.dir()) then
    local ok, job = pcall(vim.system, { config.options.bin, 'docs', '--json' }, { cwd = state.root, text = true })
    local res = ok and job:wait(10000)
    if not (res and res.code == 0) then -- no docs dir yet: the next try can still migrate
      util.notify(util.oneline('aic docs failed: ' .. vim.trim(ok and res and (res.stderr or '') or tostring(job))),
        vim.log.levels.ERROR)
      return nil
    end
    vim.fn.mkdir(M.dir(), 'p')
  end
  if not util.exists(p) then
    local lines = { '# ' .. name, '' }
    if compile then
      table.insert(lines, 1, '<!-- aic: compile -->')
    end
    util.write_file(p, table.concat(lines, '\n') .. '\n')
  end
  return p
end

-- Buffer of doc `name` (default: current), created and loaded if needed.
function M.get_buf(name)
  name = name or M.current()
  local p = M.ensure_file(name, config.options.new_doc_compile ~= false)
  if not p then
    util.notify('aic: bad doc name ' .. tostring(name), vim.log.levels.ERROR)
    return nil
  end
  local b = vim.fn.bufadd(p)
  vim.fn.bufload(b)
  vim.bo[b].buflisted = true
  return b
end

-- Show doc `name` (default: current) in the current window.
function M.show(name)
  local b = M.get_buf(name)
  if b then
    require('aic.view').show_buf(b)
  end
end

-- New doc: ask the name (unless given) and, per `new_doc_compile`, whether
-- saving compiles it; then show it here.
function M.new(name)
  local function create(n, compile)
    if not M.valid(n) then
      util.notify('aic: bad doc name ' .. tostring(n) .. ' (letters, digits, _ . -)', vim.log.levels.ERROR)
      return
    end
    if not util.exists(M.path(n)) then
      M.ensure_file(n, compile)
    end
    M.show(n)
  end
  local function with_name(n)
    if not n or vim.trim(n) == '' then
      return
    end
    n = vim.trim(n):gsub('%.md$', '')
    local ask = config.options.new_doc_compile
    if ask ~= 'ask' or util.exists(M.path(n) or '') then
      return create(n, ask ~= false)
    end
    vim.ui.select({ 'Yes', 'No' }, { prompt = 'aic: compile ' .. n .. ' when saved?' }, function(choice)
      if choice then
        create(n, choice == 'Yes')
      end
    end)
  end
  if name and vim.trim(name) ~= '' then
    return with_name(name)
  end
  vim.ui.input({ prompt = 'aic: new doc name: ' }, with_name)
end

local function ago(ts)
  local d = os.time() - (tonumber(ts) or os.time())
  if d < 60 then
    return math.floor(d) .. 's'
  elseif d < 3600 then
    return math.floor(d / 60) .. 'm'
  elseif d < 86400 then
    return math.floor(d / 3600) .. 'h'
  end
  return math.floor(d / 86400) .. 'd'
end

-- Picker: docs (most recently changed first), then "+ new doc".
function M.pick()
  backend.run({ 'docs', '--json' }, state.root, function(ok, out, err)
    local list = ok and util.decode(out)
    if type(list) ~= 'table' then
      util.notify(util.oneline('aic docs failed: ' .. vim.trim(err ~= '' and err or out)), vim.log.levels.ERROR)
      return
    end
    local items = vim.list_extend({}, list)
    table.insert(items, { new = true })
    vim.ui.select(items, {
      prompt = 'aic: doc',
      format_item = function(it)
        if it.new then
          return '+ new doc'
        end
        return ('%s%s  %s ago'):format(it.name, it.compile and '  [compile]' or '', ago(it.mtime))
      end,
    }, function(choice)
      if not choice then
        return
      elseif choice.new then
        M.new()
      else
        M.show(choice.name)
      end
    end)
  end)
end

-- Doc names on disk (for command completion).
function M.names()
  local out = {}
  if not (M.dir() and util.exists(M.dir())) then
    return out
  end
  for name, t in vim.fs.dir(M.dir()) do
    local n = t == 'file' and name:match('^(.+)%.md$')
    if M.valid(n) then
      table.insert(out, n)
    end
  end
  table.sort(out)
  return out
end

---------------------------------------------------------------------------
-- compile + diagnostics

-- Always runs `aic compile --doc`: during a running compile the backend queues
-- one more pass of that doc and exits 0 with "compile queued: ...".
local running = 0
function M.compile(name)
  name = name or M.current()
  if not state.root then
    return
  end
  running = running + 1
  state.set_local_compiling(true)
  backend.run({ 'compile', '--doc', name }, state.root, function(ok, out, err, code)
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
    state.refresh() -- the diagnostics.<doc>.json watcher fills quickfix + notifies
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

-- An item about a line of the compiled doc (`file` "doc"; "spec" before docs existed).
local function on_doc(it)
  return it.file == 'doc' or it.file == 'spec'
end

-- doc -> { at = compiled_at, items = { [index] = true } }: items of that
-- compile fixed with :AicFix, hidden until the next compile.
local resolved = {}

local function hidden(name, d, i)
  local r = resolved[name]
  return r ~= nil and r.at == d.compiled_at and r.items[i] == true
end

-- Hide items `idx` (indexes into the compile's items) of doc `name`'s compile
-- `at` from its diagnostics and the quickfix list; they come back only if
-- a later compile still reports them. unhide=true: show them again.
function M.resolve_items(name, at, idx, unhide)
  local d = state.diagnostics[name]
  if not (d and at ~= nil and d.compiled_at == at and #idx > 0) then
    return
  end
  local r = resolved[name]
  if not (r and r.at == at) then
    r = { at = at, items = {} }
    resolved[name] = r
  end
  for _, i in ipairs(idx) do
    r.items[i] = not unhide or nil
  end
  local b = M.bufnr(name)
  if b then
    M.apply_diagnostics(b)
  end
  if vim.fn.getqflist({ title = 0 }).title == 'aic compile ' .. name then
    M.load_qf(name)
  end
end

local function qf_items(name, d)
  local items = {}
  local p = M.path(name)
  for i, it in ipairs(d.items or {}) do
    if not hidden(name, d, i) then
      local file = on_doc(it) and p or util.abs(state.root, it.file or 'doc')
      local lnum = on_doc(it) and (it.line or it.spec_line) or it.line
      table.insert(items, {
        filename = file,
        lnum = tonumber(lnum) or 1,
        col = 1,
        type = sev_type[it.severity] or 'I',
        text = ('[%s] %s'):format(it.kind or '?', it.message or ''),
        user_data = it,
      })
    end
  end
  return items
end

-- Load doc `name`'s last compile into the quickfix list (never opens it).
function M.load_qf(name)
  name = name or M.current()
  local d = state.diagnostics[name]
  if not d or d.error then
    return false
  end
  local title = 'aic compile ' .. name
  local cur = vim.fn.getqflist({ title = 0 }).title
  vim.fn.setqflist({}, cur:match('^aic compile') and 'r' or ' ', { title = title, items = qf_items(name, d) })
  return true
end

function M.apply_diagnostics(buf)
  local name = M.name_of(buf)
  if not name then
    return
  end
  local d = state.diagnostics[name]
  local out = {}
  local n = api.nvim_buf_line_count(buf)
  for i, it in ipairs((d and not d.error and d.items) or {}) do
    local l = on_doc(it) and (it.line or it.spec_line) or it.spec_line
    l = tonumber(l)
    if l and not hidden(name, d, i) then
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

-- Re-apply diagnostics on every loaded doc buffer (session changed).
function M.apply_all()
  for _, b in ipairs(api.nvim_list_bufs()) do
    if api.nvim_buf_is_loaded(b) and M.name_of(b) then
      M.apply_diagnostics(b)
    end
  end
end

-- state 'diagnostics' listener: doc `name`'s result changed (d = nil: removed).
-- ` [name, source(, shadows user)]` for a compile through a template that is
-- not the built-in `compile`, else ''.
function M.template_tag(d)
  local t = type(d) == 'table' and type(d.template) == 'table' and d.template or {}
  if not t.name or (t.name == 'compile' and t.source == 'built-in') then
    return ''
  end
  local sh = type(t.shadows) == 'table' and #t.shadows > 0 and (', shadows ' .. table.concat(t.shadows, '/')) or ''
  return (' [%s, %s%s]'):format(t.name, t.source or '?', sh)
end

function M.on_diagnostics(name, d, initial)
  local b = M.bufnr(name)
  if b then
    M.apply_diagnostics(b)
  end
  if type(d) ~= 'table' then
    return
  end
  local stamp = d.compiled_at
  if initial then
    last_compiled_at[name] = stamp
    if M.template_tag(d) ~= '' then -- (not the built-in: say so from the start too)
      util.notify(('aic: %s was last compiled with template%s'):format(name, M.template_tag(d)))
    end
    return
  end
  if stamp ~= nil and stamp == last_compiled_at[name] then
    return
  end
  last_compiled_at[name] = stamp
  if d.error then
    util.notify(util.oneline(('aic compile %s error: %s'):format(name, tostring(d.error))), vim.log.levels.ERROR)
    return
  end
  M.load_qf(name)
  local c = M.counts(d)
  local summary = util.first_line(d.summary)
  local tag = M.template_tag(d) -- (not the built-in: always say so)
  util.notify(
    util.oneline(('aic compile %s%s: %dE %dW %dI%s'):format(name, tag, c.error, c.warning, c.info,
      summary ~= '' and (' — ' .. summary) or '')),
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

-- Set up a doc buffer (called from BufReadPost/BufNewFile).
function M.attach(buf)
  if vim.b[buf].aic_doc then
    return
  end
  vim.b[buf].aic_doc = true
  vim.bo[buf].swapfile = false
  require('aic.anchors').attach(buf)
  api.nvim_create_autocmd('BufWritePost', {
    buffer = buf,
    callback = function()
      local name = M.name_of(buf)
      if name and M.marker(api.nvim_buf_get_lines(buf, 0, 1, false)[1]).compile then
        M.compile(name)
      end
    end,
  })
  api.nvim_create_autocmd('BufWinEnter', {
    buffer = buf,
    callback = function()
      require('aic.usage').set_winbar(api.nvim_get_current_win(), 'doc')
    end,
  })
  api.nvim_create_autocmd('BufEnter', {
    buffer = buf,
    callback = function()
      M.last = M.name_of(buf) or M.last
      require('aic.status').update() -- the statusline shows this doc's compile
    end,
  })
  M.apply_diagnostics(buf)
end

---------------------------------------------------------------------------
-- appending (ask/what-if answers)

-- Append `body` lines under `heading` at the end of doc `name` (default: current).
-- reuse=true: don't repeat the heading if it is already the last `## ` heading.
-- Returns the first and last line numbers of the appended heading + body.
function M.append_section(heading, body, reuse, name)
  name = name or M.current()
  local p = M.ensure_file(name, config.options.new_doc_compile ~= false)
  if not p then
    util.notify('aic: no doc to append to', vim.log.levels.WARN)
    return
  end
  local buf = M.bufnr(name)
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
    util.notify(('aic: appended to %s (unsaved)'):format(name))
  else
    vim.list_extend(cur, add)
    util.write_file(p, table.concat(cur, '\n') .. '\n')
    util.notify('aic: appended to ' .. name)
  end
  return first, last, name
end

return M
