-- Anchors (PROTOCOL "Anchors"): parsing, `gd`, highlighting, omnifunc.
local state = require('aic.state')
local util = require('aic.util')

local api = vim.api
local uv = vim.uv or vim.loop
local M = {}

M.ns = api.nvim_create_namespace('aic_anchor')

local function root()
  return state.root or vim.fn.getcwd()
end

-- Characters that may precede `@` (besides start of line / whitespace).
local PREFIX = '[%s%(%[{"\',]'
-- An anchor token runs until whitespace or one of these.
local TOKEN = '^@([^%s%(%)%[%]{}"\'`,;<>|]+)'

-- Resolve a path as written in an anchor: { abs, outside }.
local function resolve(p)
  if p:sub(1, 2) == '~/' then
    return vim.fs.normalize(uv.os_homedir() .. p:sub(2)), true
  elseif p:sub(1, 1) == '/' then
    return vim.fs.normalize(p), true
  end
  return vim.fs.normalize(root() .. '/' .. p), false
end

-- Parse the text after `@` (trailing punctuation already stripped) into an
-- anchor table, or nil when it is not a (valid, existing) anchor.
local function parse(tok)
  if tok == 'diff' or tok == 'staged' or tok == 'log' then
    return { kind = tok }
  end
  local k, dp = tok:match('^(diff):(.+)$')
  if not k then
    k, dp = tok:match('^(log):(.+)$')
  end
  if k then
    return { kind = k, path = dp, abs = (resolve(dp)) }
  end
  local rev = tok:match('^show:([%w_%./%^~@{}%-]+)$')
  if rev and rev:sub(1, 1) ~= '-' then
    return { kind = 'show', rev = rev }
  end
  local turn = tok:match('^turn:(%d+)$') or tok:match('^turn:(last)$')
  if turn then
    return { kind = 'turn', turn = tonumber(turn) or 'last' }
  end
  local path, suffix = tok:match('^([^:]+):(.+)$')
  path = path or tok
  local a = { path = path }
  if suffix then
    local s, e = suffix:match('^(%d+)%-(%d+)$')
    if s then
      s, e = tonumber(s), tonumber(e)
      if s < 1 or e < s then
        return nil
      end
      a.kind, a.line, a['end'] = 'range', s, e
    elseif suffix:match('^%d+$') then
      if tonumber(suffix) < 1 then
        return nil
      end
      a.kind, a.line = 'line', tonumber(suffix)
    elseif suffix:match('^[A-Za-z_][%w_%.]*$') then
      a.kind, a.symbol = 'symbol', suffix
    else
      return nil
    end
  end
  a.abs, a.outside = resolve(path)
  local st = uv.fs_stat(a.abs)
  if not st then
    return nil
  end
  if st.type == 'directory' then
    if suffix then
      return nil
    end
    a.kind = 'dir'
  elseif path:sub(-1) == '/' then
    return nil
  end
  a.kind = a.kind or 'file'
  return a
end

-- All valid anchors in `line`, each with s/e = 1-based byte columns of the
-- anchor text (inclusive) and `anchor` = that text.
function M.all(line)
  local out = {}
  local init = 1
  while true do
    local s = line:find('@', init, true)
    if not s then
      return out
    end
    init = s + 1
    if s == 1 or line:sub(s - 1, s - 1):match(PREFIX) then
      local tok = line:sub(s):match('^@(show:[%w_%./%^~@{}%-]+)') or line:sub(s):match(TOKEN)
      while tok and tok ~= '' do
        -- keep `..` path components (`@log:../..`)
        if tok:sub(-3) == '/..' or (tok:gsub('^diff:', ''):gsub('^log:', '')) == '..' then
          break
        end
        local c = tok:sub(-1)
        -- a trailing `}` is punctuation unless it closes a `{` (`@show:HEAD@{1}`)
        if c:match('[%.,;:%)%]]')
          or (c == '}' and select(2, tok:gsub('}', '')) > select(2, tok:gsub('{', ''))) then
          tok = tok:sub(1, -2)
        else
          break
        end
      end
      if tok then
        local a = tok ~= '' and parse(tok)
        if a then
          a.anchor = '@' .. tok
          a.s, a.e = s, s + #tok
          table.insert(out, a)
          init = a.e + 1
        end
      end
    end
  end
end

-- Anchor under 0-based byte column `col` of `line`, or nil.
function M.anchor_at(line, col)
  for _, a in ipairs(M.all(line)) do
    if col + 1 >= a.s and col + 1 <= a.e then
      return a
    end
  end
end

---------------------------------------------------------------------------
-- following

local function search_symbol(sym)
  local names = { sym }
  local last = sym:match('%.([%w_]+)$')
  if last then
    table.insert(names, last)
  end
  local pats = {
    [[\v<(local\s+|export\s+|async\s+|pub\s+)*(function|def|class|const|let|var|fn|func|type|interface|struct|enum)\s+%s>]],
    [[\v<%s>\s*[=:]([=]@!|$)]],
    [[\v<%s>\s*\(]],
    [[\v<%s>]],
  }
  for _, pat in ipairs(pats) do
    for _, n in ipairs(names) do
      api.nvim_win_set_cursor(0, { 1, 0 })
      if vim.fn.search(pat:format(vim.fn.escape(n, '\\.')), 'cW') > 0 then
        vim.cmd('normal! zvzz')
        return true
      end
    end
  end
end

local function scratch(lines, ft)
  local b = api.nvim_create_buf(false, true)
  vim.bo[b].buftype = 'nofile'
  vim.bo[b].bufhidden = 'wipe'
  api.nvim_buf_set_lines(b, 0, -1, false, lines)
  vim.bo[b].modifiable = false
  vim.bo[b].filetype = ft
  require('aic.layout').open_buf(b)
end

-- @log / @log:path / @show:REV: content from `aic anchor --json`.
local function show_resolved(a)
  require('aic.backend').run({ 'anchor', '--json', a.anchor }, root(), function(ok, out, err)
    local list = ok and util.decode(out)
    local text = type(list) == 'table' and type(list[1]) == 'table' and list[1].text
    if type(text) ~= 'string' or text == '' then
      util.notify(util.oneline('aic: cannot resolve ' .. a.anchor .. ': ' .. vim.trim(err ~= '' and err or out)), vim.log.levels.ERROR)
      return
    end
    scratch(util.split((text:gsub('\n$', ''))), 'git')
  end)
end

local function show_diff(a)
  local args = { 'git', '-c', 'core.fsmonitor=', '-C', root(), 'diff', '--no-ext-diff', '--no-textconv' }
  if a.kind == 'staged' then
    table.insert(args, '--cached')
  else
    table.insert(args, 'HEAD')
    if a.path then
      vim.list_extend(args, { '--', a.abs })
    end
  end
  vim.system(args, { text = true }, function(res)
    vim.schedule(function()
      if res.code ~= 0 then
        util.notify(util.oneline('aic: ' .. (res.stderr or 'git diff failed')), vim.log.levels.ERROR)
        return
      end
      local lines = util.split((res.stdout or ''):gsub('\n$', ''))
      if #lines == 0 then
        util.notify(util.oneline('aic: no changes for ' .. a.anchor))
        return
      end
      scratch(lines, 'diff')
    end)
  end)
end

-- Act on a parsed anchor (PROTOCOL "gd in Neovim").
function M.follow(a)
  local layout = require('aic.layout')
  if a.kind == 'diff' or a.kind == 'staged' then
    return show_diff(a)
  elseif a.kind == 'log' or a.kind == 'show' then
    return show_resolved(a)
  elseif a.kind == 'turn' then
    layout.focus('trace')
    if not require('aic.trace').goto_turn(a.turn) then
      util.notify('aic: no turn ' .. tostring(a.turn) .. ' in the trace', vim.log.levels.WARN)
    end
    return
  end
  layout.open_code(a.abs, a.line or 1)
  if vim.fs.normalize(api.nvim_buf_get_name(0)) ~= a.abs then
    return -- open failed (already notified)
  end
  if a.kind == 'range' then
    local n = api.nvim_buf_line_count(0)
    local s = math.max(1, math.min(tonumber(a.line) or 1, n))
    local e = math.max(s, math.min(tonumber(a['end']) or s, n))
    api.nvim_buf_set_mark(0, '<', s, 0, {})
    api.nvim_buf_set_mark(0, '>', e, 0, {})
    vim.cmd(('normal! %dGV%dG'):format(math.floor(e), math.floor(s)))
  elseif a.kind == 'symbol' and not search_symbol(a.symbol) then
    util.notify(util.oneline('aic: symbol not found: ' .. a.symbol), vim.log.levels.WARN)
  end
end

-- Follow the anchor under the cursor. fallback=true: plain `gd` otherwise.
function M.follow_cursor(fallback)
  local a = M.anchor_at(api.nvim_get_current_line(), api.nvim_win_get_cursor(0)[2])
  if a then
    return M.follow(a)
  end
  if fallback then
    -- the `gd` mapping this one replaced (see M.attach), else the builtin
    local prev = vim.b.aic_prev_gd
    local m = prev and M.prev_gd[prev]
    local okc, err
    if m then
      okc, err = pcall(function()
        local keys
        if m.callback then
          keys = m.callback()
          keys = m.expr == 1 and keys or nil
        else
          keys = m.expr == 1 and vim.fn.eval(m.rhs) or m.rhs
        end
        if type(keys) == 'string' and keys ~= '' then
          api.nvim_feedkeys(api.nvim_replace_termcodes(keys, true, true, true), m.noremap == 1 and 'n' or 'm', false)
        end
      end)
    else
      okc, err = pcall(vim.cmd, 'normal! gd')
    end
    if not okc then
      util.notify(util.oneline('aic: gd: ' .. tostring(err)), vim.log.levels.WARN)
    end
  else
    util.notify('aic: no anchor under cursor')
  end
end

---------------------------------------------------------------------------
-- highlighting

-- Small buffers are highlighted whole; big ones, and the chat buffer (it grows
-- with every answer and re-renders on each chat.md change), only around the
-- visible lines of their windows.
M.chat_margin = 50

function M.highlight(buf)
  if not api.nvim_buf_is_valid(buf) then
    return
  end
  local n = api.nvim_buf_line_count(buf)
  local chat = api.nvim_buf_get_name(buf) == 'aic://chat'
  local ranges = {}
  if n < 2000 and not chat then
    ranges = { { 1, n } }
  else
    local m = chat and M.chat_margin or 0
    for _, w in ipairs(vim.fn.win_findbuf(buf)) do
      table.insert(ranges, api.nvim_win_call(w, function()
        return { math.max(1, vim.fn.line('w0') - m), math.min(n, vim.fn.line('w$') + m) }
      end))
    end
  end
  api.nvim_buf_clear_namespace(buf, M.ns, 0, -1)
  for _, r in ipairs(ranges) do
    for i, l in ipairs(api.nvim_buf_get_lines(buf, r[1] - 1, r[2], false)) do
      for _, a in ipairs(M.all(l)) do
        api.nvim_buf_set_extmark(buf, M.ns, r[1] + i - 2, a.s - 1, { end_col = a.e, hl_group = 'AicAnchor' })
      end
    end
  end
end

---------------------------------------------------------------------------
-- completion

local SKIP = { ['.git'] = true, ['.aic'] = true, node_modules = true }

-- Project files and directories (relative, dirs end in `/`).
function M.project_paths()
  local r = root()
  local files = {}
  local res = vim.system({ 'git', '-c', 'core.fsmonitor=', '-C', r, 'ls-files', '-co', '--exclude-standard' }, { text = true }):wait()
  if res.code == 0 then
    for _, f in ipairs(util.split(vim.trim(res.stdout or ''))) do
      local top = f:match('^[^/]+')
      if f ~= '' and not SKIP[top] then
        table.insert(files, f)
      end
    end
  else
    for name, type in vim.fs.dir(r, {
      depth = 20,
      skip = function(d)
        return not SKIP[vim.fs.basename(d)]
      end,
    }) do
      if not SKIP[name:match('^[^/]+')] then
        table.insert(files, type == 'directory' and (name .. '/') or name)
        if #files >= 5000 then
          break
        end
      end
    end
  end
  local seen, out = {}, {}
  for _, f in ipairs(files) do
    for d in f:gmatch('()/') do
      local dir = f:sub(1, d)
      if not seen[dir] then
        seen[dir] = true
        table.insert(out, dir)
      end
    end
    if not seen[f] then
      seen[f] = true
      table.insert(out, f)
    end
  end
  table.sort(out)
  return out
end

function M.omnifunc(findstart, base)
  if findstart == 1 then
    local before = api.nvim_get_current_line():sub(1, api.nvim_win_get_cursor(0)[2])
    local s = before:match('.*()@[^%s@]*$')
    if s and (s == 1 or before:sub(s - 1, s - 1):match(PREFIX)) then
      return s - 1
    end
    return -3
  end
  local q = base:gsub('^@', '')
  local cands = { 'diff', 'diff:', 'staged', 'log', 'log:', 'show:HEAD', 'show:HEAD~1', 'turn:last' }
  local turns = {}
  for _, ev in ipairs(state.events or {}) do
    local t = tonumber(ev.turn)
    if t and not turns[t] then
      turns[t] = true
      table.insert(cands, 'turn:' .. t)
    end
  end
  local prefix = q:match('^diff:') or q:match('^log:') or ''
  for _, p in ipairs(M.project_paths()) do
    table.insert(cands, prefix .. p)
  end
  local out = {}
  local rest = {} -- substring matches come after prefix matches
  for _, c in ipairs(cands) do
    if vim.startswith(c, q) then
      table.insert(out, { word = '@' .. c, menu = '[aic]' })
    elseif c:find(q, 1, true) then
      table.insert(rest, { word = '@' .. c, menu = '[aic]' })
    end
  end
  vim.list_extend(out, rest)
  return out
end

---------------------------------------------------------------------------

-- Pre-existing `gd` mappings (callbacks cannot live in vim.b), by id.
M.prev_gd, M.prev_gd_n = {}, 0

-- Buffer setup for spec / chat / answer float: gd, highlight, omnifunc.
function M.attach(buf)
  if vim.b[buf].aic_anchors then
    return
  end
  vim.b[buf].aic_anchors = true
  local prev = api.nvim_buf_call(buf, function()
    return vim.fn.maparg('gd', 'n', false, true)
  end)
  if type(prev) == 'table' and (prev.callback or (prev.rhs and prev.rhs ~= '')) then
    M.prev_gd_n = M.prev_gd_n + 1
    M.prev_gd[M.prev_gd_n] = prev
    vim.b[buf].aic_prev_gd = M.prev_gd_n
  end
  vim.keymap.set('n', 'gd', function()
    M.follow_cursor(true)
  end, { buffer = buf, desc = 'aic: go to anchor' })
  vim.bo[buf].omnifunc = "v:lua.require'aic.anchors'.omnifunc"
  local pending = false
  api.nvim_create_autocmd({ 'TextChanged', 'TextChangedI', 'BufWinEnter', 'WinScrolled' }, {
    buffer = buf,
    callback = function()
      if pending then
        return
      end
      pending = true
      vim.defer_fn(function()
        pending = false
        M.highlight(buf)
      end, 150)
    end,
  })
  M.highlight(buf)
end

return M
