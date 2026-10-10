-- The dedicated aic tab: spec (left) | trace (right), chat (bottom).
local config = require('aic.config')
local state = require('aic.state')
local util = require('aic.util')

local api = vim.api
local M = { tab = nil, origin = nil, wins = {} }

local function valid_tab(t)
  return t and api.nvim_tabpage_is_valid(t)
end

local function valid_win(w)
  return w and api.nvim_win_is_valid(w)
end

function M.in_aic_tab()
  return valid_tab(M.tab) and api.nvim_get_current_tabpage() == M.tab
end

local function build()
  local spec_win = api.nvim_get_current_win()
  local scratch = api.nvim_get_current_buf()
  require('aic.spec').show(spec_win)
  vim.cmd('rightbelow vsplit')
  local trace_win = api.nvim_get_current_win()
  require('aic.trace').show(trace_win)
  vim.cmd('botright ' .. config.options.chat_height .. 'split')
  local chat_win = api.nvim_get_current_win()
  require('aic.chat').show(chat_win)
  M.wins = { spec = spec_win, trace = trace_win, chat = chat_win }
  require('aic.usage').apply(M.wins)
  api.nvim_set_current_win(trace_win)
  -- wipe the empty buffer `tabnew` created
  if
    api.nvim_buf_is_valid(scratch)
    and api.nvim_buf_get_name(scratch) == ''
    and not vim.bo[scratch].modified
    and #vim.fn.win_findbuf(scratch) == 0
  then
    pcall(api.nvim_buf_delete, scratch, {})
  end
end

local function ensure_windows()
  if valid_win(M.wins.spec) and valid_win(M.wins.trace) and valid_win(M.wins.chat) then
    return
  end
  vim.cmd('silent! only')
  build()
end

-- Open (or switch to) the aic tab.
function M.open()
  local cur = api.nvim_get_current_tabpage()
  if valid_tab(M.tab) then
    if cur ~= M.tab then
      M.origin = cur
      api.nvim_set_current_tabpage(M.tab)
    end
    ensure_windows()
    return
  end
  M.origin = cur
  vim.cmd('tabnew')
  M.tab = api.nvim_get_current_tabpage()
  build()
end

-- Toggle between the aic tab and the origin tab.
function M.toggle()
  if not M.in_aic_tab() then
    return M.open()
  end
  if valid_tab(M.origin) and M.origin ~= M.tab then
    api.nvim_set_current_tabpage(M.origin)
    return
  end
  for _, t in ipairs(api.nvim_list_tabpages()) do
    if t ~= M.tab then
      M.origin = t
      api.nvim_set_current_tabpage(t)
      return
    end
  end
  vim.cmd('tabnew')
  M.origin = api.nvim_get_current_tabpage()
end

function M.focus(name)
  M.open()
  if valid_win(M.wins[name]) then
    api.nvim_set_current_win(M.wins[name])
  end
end

-- Go to a normal window to open code in: from the aic tab, the origin tab's
-- current window; never a special window (quickfix, help, float, ...).
local function goto_code_win()
  if M.in_aic_tab() then
    if valid_tab(M.origin) and M.origin ~= M.tab then
      api.nvim_set_current_tabpage(M.origin)
    else
      vim.cmd('tabnew')
      M.origin = api.nvim_get_current_tabpage()
    end
  end
  if vim.bo.buftype ~= '' then
    -- don't replace a special window (quickfix, help, ...): use a normal one
    for _, w in ipairs(api.nvim_tabpage_list_wins(0)) do
      if vim.bo[api.nvim_win_get_buf(w)].buftype == '' and api.nvim_win_get_config(w).relative == '' then
        api.nvim_set_current_win(w)
        break
      end
    end
  end
end

-- Show buffer `buf` (e.g. a scratch diff) where code would open.
function M.open_buf(buf)
  goto_code_win()
  api.nvim_win_set_buf(0, buf)
end

-- Open project file `path` (relative to root or absolute) at `line`.
-- From the aic tab, the file opens in the origin tab's current window.
function M.open_code(path, line)
  local abs = util.abs(state.root or vim.fn.getcwd(), path)
  goto_code_win()
  local ok, err = pcall(vim.cmd.edit, vim.fn.fnameescape(abs))
  if not ok then
    util.notify(util.oneline('aic: ' .. tostring(err)), vim.log.levels.ERROR)
    return
  end
  local n = api.nvim_buf_line_count(0)
  api.nvim_win_set_cursor(0, { math.max(1, math.min(tonumber(line) or 1, n)), 0 })
  vim.cmd('normal! zvzz')
end

-- Session changed: point the spec window at the new spec.
function M.on_session()
  if valid_win(M.wins.spec) then
    require('aic.spec').show(M.wins.spec)
  end
  require('aic.usage').apply(M.wins)
end

return M
