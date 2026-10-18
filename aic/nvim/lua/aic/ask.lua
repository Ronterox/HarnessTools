-- Ask-at-cursor and what-if: build a context file, run `aic ask`, show a float.
local backend = require('aic.backend')
local state = require('aic.state')
local util = require('aic.util')

local api = vim.api
local M = {}

local function excerpt(buf, line)
  local n = api.nvim_buf_line_count(buf)
  local s, e = math.max(1, line - 10), math.min(n, line + 10)
  return table.concat(api.nvim_buf_get_lines(buf, s - 1, e, false), '\n')
end

-- Context for the cursor position in the current buffer (PROTOCOL "Context file").
function M.context()
  local buf = api.nvim_get_current_buf()
  local line = api.nvim_win_get_cursor(0)[1]
  local trace = require('aic.trace')
  local chat = require('aic.chat')
  local spec = require('aic.spec')
  if buf == trace.buf then
    return { where = 'trace', event = trace.event_at(line) }
  end
  if vim.bo[buf].buftype == 'quickfix' then
    local wi = vim.fn.getwininfo(api.nvim_get_current_win())[1]
    local items = (wi and wi.loclist == 1) and vim.fn.getloclist(0) or vim.fn.getqflist()
    local it = items[line]
    if not it then
      return { where = 'diagnostic' }
    end
    local diag = type(it.user_data) == 'table' and it.user_data
      or { message = it.text, file = util.rel(state.root, vim.fn.bufname(it.bufnr)), line = it.lnum }
    return { where = 'diagnostic', diagnostic = diag }
  end
  if buf == chat.buf then
    return { where = 'chat', line = line, excerpt = excerpt(buf, line) }
  end
  local name = api.nvim_buf_get_name(buf)
  local rel = util.rel(state.root, name)
  if spec.path() and vim.fs.normalize(name) == vim.fs.normalize(spec.path()) then
    return { where = 'spec', file = rel, line = line, excerpt = excerpt(buf, line) }
  end
  return { where = 'code', file = rel or name, line = line, excerpt = excerpt(buf, line) }
end

-- First fenced code block of `lines` (without the fences), else all of them.
function M.first_block(lines)
  local s
  for i, l in ipairs(lines) do
    if l:match('^%s*```') then
      if s then
        return vim.list_slice(lines, s + 1, i - 1)
      end
      s = i
    end
  end
  return lines
end

-- Append the answer's pseudocode to spec.md, save it and `aic submit` it.
function M.submit_answer(q, answer)
  local spec = require('aic.spec')
  -- The block's own heading names it; otherwise a neutral one. Never the
  -- question: it holds instructions meant for the compiler, not the worker.
  local block = M.first_block(answer)
  local heading = not (block[1] or ''):match('^#+%s') and '## From compiler' or nil
  local first, last = spec.append_section(heading, block, false)
  if not first then
    return
  end
  local buf = spec.bufnr()
  if buf then
    api.nvim_buf_call(buf, function()
      vim.cmd('silent write')
    end)
  end
  backend.run({ 'submit', '--from', tostring(first), '--to', tostring(last) }, state.root, function(ok, out, err, code)
    out, err = vim.trim(out), vim.trim(err)
    if ok then
      util.notify(util.oneline(out ~= '' and out or 'aic submit: ok'))
    else
      util.notify(util.oneline(('aic submit failed (%s): %s'):format(code, err ~= '' and err or out)), vim.log.levels.ERROR)
    end
    state.refresh()
  end)
end

-- Show the answer float for question `q`.
-- `actions` = { key = { desc, fn(answer_lines, entry) } }; `S` and `Y` are
-- always added. Uppercase keys: lowercase ones would shadow yank/find motions.
local function run(args, ctx, q, title, actions)
  actions = vim.tbl_extend('keep', actions, {
    S = {
      'append pseudocode to spec + submit',
      function(answer, entry)
        if entry.submitted then
          util.notify('aic: already submitted')
          return
        end
        entry.submitted = true
        M.submit_answer(q, answer)
      end,
    },
    Y = {
      'yank pseudocode',
      function(answer)
        vim.fn.setreg('"', M.first_block(answer), 'l')
        util.notify('aic: yanked')
      end,
    },
  })
  local tmp = vim.fn.tempname() .. '.json'
  util.write_file(tmp, vim.json.encode(ctx))
  local buf, win = util.float({ '_thinking…_' }, { title = ' ' .. title .. ' ', filetype = 'markdown' })
  local entry = { q = q, title = title, actions = actions }
  M.attach(buf, win, entry)
  local cmd = { 'ask', '--context-file', tmp }
  vim.list_extend(cmd, args)
  backend.run(cmd, state.root, function(ok, out, err, code)
    os.remove(tmp)
    state.refresh()
    if ok then
      entry.answer = util.split(vim.trim(out))
      M.last = entry -- reopen with :AicLastAnswer after closing the float
      M.render(buf, entry)
    else
      util.set_lines(buf, util.split(('**aic ask failed (%s)**\n\n%s'):format(code, vim.trim(err ~= '' and err or out))))
    end
  end)
end

-- Action keys on an answer float (they wait for the answer to arrive).
function M.attach(buf, win, entry)
  require('aic.anchors').attach(buf)
  for key, a in pairs(entry.actions) do
    vim.keymap.set('n', key, function()
      if not entry.answer then
        util.notify('aic: no answer yet')
        return
      end
      -- close first: the action then runs in the window under the float
      if api.nvim_win_is_valid(win) then
        api.nvim_win_close(win, true)
      end
      a[2](entry.answer, entry)
    end, { buffer = buf, nowait = true, desc = 'aic: ' .. a[1] })
  end
end

function M.render(buf, entry)
  local hints = {}
  for key, a in pairs(entry.actions) do
    table.insert(hints, key .. ' ' .. a[1])
  end
  table.sort(hints)
  local shown = vim.list_extend({}, entry.answer)
  vim.list_extend(shown, { '', '---', '_q close · ' .. table.concat(hints, ' · ') .. '_' })
  util.set_lines(buf, shown)
  require('aic.anchors').highlight(buf)
end

-- Reopen the last answer (and its S/Y/P/F keys) after the float was closed.
function M.reopen()
  if not (M.last and M.last.answer) then
    util.notify('aic: no answer yet this session')
    return
  end
  local buf, win = util.float({ '' }, { title = ' ' .. M.last.title .. ' ', filetype = 'markdown' })
  M.attach(buf, win, M.last)
  M.render(buf, M.last)
end

local function with_root(fn)
  if not state.root then
    util.notify('aic: not active (run :Aic)', vim.log.levels.WARN)
    return
  end
  fn()
end

-- Ask about the thing under the cursor.
function M.ask()
  with_root(function()
    local ctx = M.context()
    vim.ui.input({ prompt = 'aic ask: ' }, function(q)
      if not q or vim.trim(q) == '' then
        return
      end
      run({ '--', q }, ctx, q, 'aic: ' .. q, {
        P = {
          'append to spec',
          function(answer)
            require('aic.spec').append_section('## Notes', answer, true)
          end,
        },
      })
    end)
  end)
end

-- What-if on the trace node (or anything) under the cursor.
function M.whatif()
  with_root(function()
    local ctx = M.context()
    vim.ui.input({ prompt = 'aic what-if — instead of what? ' }, function(q)
      if not q or vim.trim(q) == '' then
        return
      end
      run({ '--whatif', '--', q }, ctx, q, 'aic what-if: ' .. q, {
        F = {
          'fold into spec',
          function(answer)
            require('aic.spec').append_section('## What-if: ' .. q, answer, false)
          end,
        },
      })
    end)
  end)
end

return M
