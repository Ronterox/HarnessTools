-- aic views live in the current window: history (trace), questions, spec.
-- Switching is a buffer switch (<C-^> and <C-o> work), code opens in the same
-- window, and aic never creates a tab or a split.
local state = require('aic.state')
local util = require('aic.util')

local api = vim.api
local M = {
  last_aic = nil, -- buffer of the last aic view shown (for toggle)
  last_code = nil, -- last normal file buffer outside aic
}

local function mod(name)
  return require('aic.' .. name)
end

-- view name -> buffer getter
local getters = {
  trace = function()
    return mod('trace').get_buf()
  end,
  questions = function()
    return mod('chat').get_buf()
  end,
  spec = function()
    return mod('spec').get_buf()
  end,
}

-- View name of `buf` ('trace' | 'questions' | 'spec'), or nil.
function M.name_of(buf)
  if not (buf and api.nvim_buf_is_valid(buf)) then
    return nil
  end
  if buf == mod('trace').buf then
    return 'trace'
  elseif buf == mod('chat').buf then
    return 'questions'
  end
  local spec = mod('spec')
  if buf == spec.placeholder then
    return 'spec'
  end
  local p = spec.path()
  if p and vim.fs.normalize(api.nvim_buf_get_name(buf)) == vim.fs.normalize(p) then
    return 'spec'
  end
end

-- Any aic buffer: a view, or a file under .aic/ (e.g. another session's spec).
function M.is_aic(buf)
  if M.name_of(buf) then
    return true
  end
  local name = api.nvim_buf_get_name(buf)
  return name:match('^aic://') ~= nil or name:match('/%.aic/') ~= nil
end

local function floating(win)
  return api.nvim_win_get_config(win).relative ~= ''
end

-- Windows that must keep their buffer: floats, quickfix, help, terminals.
local special = { quickfix = true, help = true, terminal = true, prompt = true }
local function usable(win)
  return api.nvim_win_is_valid(win) and not floating(win) and not special[vim.bo[api.nvim_win_get_buf(win)].buftype]
end

-- The window to show things in: the current one, unless it is a float or a
-- special window; then the previous window, else the first usable one. aic's
-- own floats (answer, blame) are closed on the way; other floats stay.
local function main_win()
  local cur = api.nvim_get_current_win()
  if usable(cur) then
    return cur
  end
  local target
  local prev = vim.fn.win_getid(vim.fn.winnr('#'))
  if prev ~= 0 and usable(prev) then
    target = prev
  else
    for _, w in ipairs(api.nvim_tabpage_list_wins(0)) do
      if usable(w) then
        target = w
        break
      end
    end
  end
  if util.floats[cur] then
    pcall(api.nvim_win_close, cur, false)
  end
  if target then
    api.nvim_set_current_win(target)
  end
  return api.nvim_get_current_win()
end

-- Put `buf` in `win`; a refused switch (E37 with 'nohidden') is notified.
local function set_buf(win, buf)
  local ok, err = pcall(api.nvim_win_set_buf, win, buf)
  if not ok then
    util.notify(util.oneline('aic: ' .. tostring(err)), vim.log.levels.WARN)
  end
  return ok
end

-- Show `buf` in the current window, with a jumplist entry to come back.
function M.show_buf(buf)
  local win = main_win()
  if api.nvim_win_get_buf(win) == buf then
    return win
  end
  vim.cmd("normal! m'")
  set_buf(win, buf)
  return win
end

-- Show view `name` in the current window.
function M.show(name)
  local get = getters[name]
  if not get then
    util.notify('aic: unknown view ' .. tostring(name), vim.log.levels.ERROR)
    return
  end
  return M.show_buf(get())
end

-- :Aic: the last aic view (default: history) in the current window.
function M.open()
  if M.last_aic and api.nvim_buf_is_valid(M.last_aic) then
    return M.show_buf(M.last_aic)
  end
  return M.show('trace')
end

-- In an aic buffer: back to the last code buffer. Elsewhere: the last aic view.
function M.toggle()
  if M.is_aic(api.nvim_get_current_buf()) then
    if M.last_code and api.nvim_buf_is_valid(M.last_code) then
      return M.show_buf(M.last_code)
    end
    util.notify('aic: no code buffer to go back to')
    return
  end
  return M.open()
end

-- Open project file `path` (relative to root or absolute) at `line`, in the
-- current window.
function M.open_code(path, line)
  local abs = util.abs(state.root or vim.fn.getcwd(), path)
  main_win()
  vim.cmd("normal! m'")
  local ok, err = pcall(vim.cmd.edit, vim.fn.fnameescape(abs))
  if not ok then
    util.notify(util.oneline('aic: ' .. tostring(err)), vim.log.levels.ERROR)
    return
  end
  local n = api.nvim_buf_line_count(0)
  api.nvim_win_set_cursor(0, { math.max(1, math.min(tonumber(line) or 1, n)), 0 })
  vim.cmd('normal! zvzz')
end

-- A spec buffer that is not the current session's (another session's spec.md,
-- or the no-session placeholder once a session exists).
local function stale_spec(buf, p)
  local spec = mod('spec')
  if buf == spec.placeholder then
    return p ~= nil
  end
  local name = api.nvim_buf_get_name(buf)
  return name:match('/%.aic/sessions/[^/]+/spec%.md$') ~= nil
    and (not p or vim.fs.normalize(name) ~= vim.fs.normalize(p))
end

-- Session changed: windows showing a stale spec switch to the new one, and
-- toggle no longer returns to it.
function M.on_session()
  local spec = mod('spec')
  local p = spec.path()
  for _, w in ipairs(api.nvim_list_wins()) do
    if stale_spec(api.nvim_win_get_buf(w), p) then
      set_buf(w, spec.get_buf())
    end
  end
  if M.last_aic and api.nvim_buf_is_valid(M.last_aic) and stale_spec(M.last_aic, p) then
    M.last_aic = spec.get_buf()
  end
end

-- Track the last aic view and the last code buffer (for toggle).
function M.setup()
  api.nvim_create_autocmd('BufEnter', {
    group = api.nvim_create_augroup('aic_view', { clear = true }),
    callback = function(a)
      if floating(api.nvim_get_current_win()) then
        return
      end
      if M.is_aic(a.buf) then
        M.last_aic = a.buf
      elseif vim.bo[a.buf].buftype == '' and api.nvim_buf_get_name(a.buf) ~= ''
        and vim.fn.isdirectory(api.nvim_buf_get_name(a.buf)) == 0 then
        M.last_code = a.buf
      end
    end,
  })
end

return M
