-- Questions view (`aic://questions`): a nofile view of chat.md plus an input
-- area under a final `## You` line. The plugin never writes chat.md; `aic ask`
-- appends to it.
local backend = require('aic.backend')
local state = require('aic.state')
local util = require('aic.util')

local api = vim.api
local M = { buf = nil }
local FOOTER = '## You'
local ns = api.nvim_create_namespace('aic_chat')
local ns_tpl = api.nvim_create_namespace('aic_chat_template')

local function footer_index(lines)
  for i = #lines, 1, -1 do
    if lines[i] == FOOTER then
      return i
    end
  end
end

-- Typed input (lines after the last `## You`).
function M.input_lines()
  if not (M.buf and api.nvim_buf_is_valid(M.buf)) then
    return {}
  end
  local lines = api.nvim_buf_get_lines(M.buf, 0, -1, false)
  local f = footer_index(lines)
  return f and vim.list_slice(lines, f + 1) or {}
end

function M.render()
  local b = M.buf
  if not (b and api.nvim_buf_is_valid(b)) then
    return
  end
  local old = api.nvim_buf_get_lines(b, 0, -1, false)
  local of = footer_index(old)
  local input = of and vim.list_slice(old, of + 1) or {}
  if #input == 0 then
    input = { '' }
  end
  local content = state.dir and util.read_lines(state.dir .. '/chat.md') or {}
  if not state.dir then
    content = { '(no worker session)' }
  end
  local lines = vim.list_extend({}, content)
  if #lines > 0 then
    table.insert(lines, '')
  end
  table.insert(lines, FOOTER)
  local nf = #lines
  vim.list_extend(lines, input)

  -- remember cursors relative to the footer when they are in the input area
  local cursors = {}
  for _, w in ipairs(vim.fn.win_findbuf(b)) do
    local c = api.nvim_win_get_cursor(w)
    cursors[w] = (of and c[1] >= of) and { nf + (c[1] - of), c[2] } or { math.min(c[1], #lines), c[2] }
  end
  api.nvim_buf_set_lines(b, 0, -1, false, lines)
  vim.bo[b].modified = false
  for w, c in pairs(cursors) do
    pcall(api.nvim_win_set_cursor, w, c)
  end
  api.nvim_buf_clear_namespace(b, ns_tpl, 0, -1)
  local tpl = require('aic.ask').template
  if tpl then -- the questions go through another template than `ask`
    api.nvim_buf_set_extmark(b, ns_tpl, nf - 1, 0, {
      virt_text = { { '  template: ' .. tpl, 'AicSubmit' } },
      virt_text_pos = 'eol',
    })
  end
  require('aic.anchors').highlight(b)
end

local function set_pending(on)
  if not (M.buf and api.nvim_buf_is_valid(M.buf)) then
    return
  end
  api.nvim_buf_clear_namespace(M.buf, ns, 0, -1)
  if on then
    local f = footer_index(api.nvim_buf_get_lines(M.buf, 0, -1, false))
    if f then
      api.nvim_buf_set_extmark(M.buf, ns, f - 1, 0, {
        virt_text = { { '  thinking…', 'Comment' } },
        virt_text_pos = 'eol',
      })
    end
  end
end

-- Send `text` to the compiler via `aic ask`.
function M.ask(text)
  text = vim.trim(text or '')
  if text == '' then
    util.notify('aic: nothing to send (type under "## You")')
    return
  end
  if not state.root then
    util.notify('aic: not active', vim.log.levels.WARN)
    return
  end
  state.asking = state.asking + 1
  set_pending(true)
  local args = { 'ask', '--doc', require('aic.doc').current() }
  vim.list_extend(args, require('aic.ask').template_args())
  vim.list_extend(args, { '--', text })
  backend.run(args, state.root, function(ok, out, err, code)
    state.asking = state.asking - 1
    set_pending(state.asking > 0)
    if not ok then
      util.notify(('aic ask failed (%s): %s'):format(code, vim.trim(err ~= '' and err or out)), vim.log.levels.ERROR)
    end
    state.refresh()
    M.render()
  end)
end

function M.send()
  local text = table.concat(M.input_lines(), '\n')
  if vim.trim(text) == '' then
    util.notify('aic: nothing to send (type under "## You")')
    return
  end
  local lines = api.nvim_buf_get_lines(M.buf, 0, -1, false)
  local f = footer_index(lines)
  api.nvim_buf_set_lines(M.buf, f, -1, false, { '' })
  vim.bo[M.buf].modified = false
  M.ask(text)
end

function M.get_buf()
  if M.buf and api.nvim_buf_is_valid(M.buf) then
    return M.buf
  end
  local b = api.nvim_create_buf(false, true)
  pcall(api.nvim_buf_set_name, b, 'aic://questions')
  vim.bo[b].buftype = 'nofile'
  vim.bo[b].bufhidden = 'hide'
  vim.bo[b].swapfile = false
  vim.bo[b].filetype = 'markdown'
  M.buf = b
  vim.keymap.set('n', '<CR>', M.send, { buffer = b, desc = 'aic: send chat input' })
  require('aic.anchors').attach(b)
  local first = true
  api.nvim_create_autocmd('BufWinEnter', {
    buffer = b,
    callback = function()
      local win = api.nvim_get_current_win()
      local wo = vim.wo[win][0] -- buffer-local: never leaks into the next buffer
      wo.wrap = true
      wo.linebreak = true
      wo.number = false
      wo.relativenumber = false
      require('aic.usage').set_winbar(win, 'questions')
      if first then -- the input area, the first time only
        first = false
        api.nvim_win_set_cursor(win, { api.nvim_buf_line_count(b), 0 })
      end
    end,
  })
  M.render()
  return b
end

return M
