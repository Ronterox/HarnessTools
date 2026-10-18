-- Submit through a prompt template: pick a template, answer its input, edit
-- the rendered prompt in a preview buffer (`aic://preview`, current window)
-- and `:w` to send exactly that. Leaving the preview sends nothing.
local backend = require('aic.backend')
local state = require('aic.state')
local util = require('aic.util')

local api = vim.api
local M = {
  last_prompt = {}, -- doc -> template last sent for it (this nvim session)
  buf = nil, -- the preview buffer
}

local ns = api.nvim_create_namespace('aic_preview')
local MAX_HEADER = 300

-- Would the backend refuse codepoint `c` in a typed header? (PROTOCOL "Prompt
-- templates": digits, @, \, and anything invisible: controls, zero-width,
-- bidi, Unicode tags, private use.) Tabs are fine: whitespace is collapsed.
local function bad_char(c)
  if c == 9 then
    return false
  end
  return c < 32 or c == 127 or (c >= 48 and c <= 57) or c == 64 or c == 92
    or (c >= 0x80 and c <= 0x9F) or c == 0xAD or (c >= 0x200B and c <= 0x200F) or (c >= 0x2028 and c <= 0x202E)
    or (c >= 0x2060 and c <= 0x206F) or c == 0xFEFF or (c >= 0xE000 and c <= 0xF8FF) or (c >= 0xE0000)
    or (c >= 0x80 and vim.fn.strwidth(vim.fn.nr2char(c)) == 0)
    or (c > 127 and vim.fn.nr2char(c):match('^%d$') ~= nil)
end

local function doc_mod()
  return require('aic.doc')
end

-- Templates in picker order: the doc's marker `prompt=`, then the one last
-- sent for this doc, then the rest by name.
local function ordered(list, doc_name)
  local d = doc_mod()
  local b = d.bufnr(doc_name)
  local first = b and api.nvim_buf_get_lines(b, 0, 1, false)[1] or (util.read_lines(d.path(doc_name) or '') or {})[1]
  local prefer = { d.marker(first).prompt, M.last_prompt[doc_name] }
  local rank = function(t)
    if t.error then
      return 4 -- broken ones last
    end
    for i = 1, 2 do -- (either may be nil)
      if prefer[i] and t.name == prefer[i] then
        return i
      end
    end
    return 3
  end
  table.sort(list, function(a, b)
    local ra, rb = rank(a), rank(b)
    if ra ~= rb then
      return ra < rb
    end
    return a.name < b.name
  end)
  return list
end

---------------------------------------------------------------------------
-- preview

-- Problems of header `h`: { { col = byte, len = bytes, msg } } (col nil: whole line).
local function header_problems(h)
  local out = {}
  h = h or ''
  if vim.trim(h) == '' then
    return { { msg = 'the header (line 1) is empty: it is your typed request to the worker' } }
  end
  local col = 0
  for _, c in ipairs(vim.fn.str2list(h)) do
    local ch = vim.fn.nr2char(c)
    if bad_char(c) then
      local shown = (c >= 32 and c < 127) and ("'" .. ch .. "'") or ('U+%04X'):format(c)
      table.insert(out, { col = col, len = #ch, msg = shown .. ' is typed into the worker as you: no digits, @, \\ '
        .. 'or invisible characters (move it to the body)' })
    end
    col = col + #ch
  end
  if vim.fn.strchars(h) > MAX_HEADER then
    table.insert(out, { msg = ('the header is %d characters; keep it under %d'):format(vim.fn.strchars(h), MAX_HEADER) })
  end
  return out
end

local function mark(buf, now)
  if not api.nvim_buf_is_valid(buf) then
    return
  end
  api.nvim_buf_clear_namespace(buf, ns, 0, -1)
  local h = api.nvim_buf_get_lines(buf, 0, 1, false)[1] or ''
  api.nvim_buf_set_extmark(buf, ns, 0, 0, {
    virt_lines = { { { ('── pasted below (%s) · :w sends · leave to cancel ──'):format(now and 'send now' or 'queued'), 'Comment' } } },
  })
  local problems = header_problems(h)
  for _, p in ipairs(problems) do
    if p.col then -- the offending character itself; non-ASCII ones (maybe invisible) get a ⟨U+…⟩ label
      api.nvim_buf_set_extmark(buf, ns, 0, p.col, { end_col = p.col + p.len, hl_group = 'AicError',
        virt_text = p.msg:match('^U%+') and { { '⟨' .. p.msg:match('^%S+') .. '⟩', 'AicError' } } or nil,
        virt_text_pos = 'inline' })
    end
  end
  vim.diagnostic.set(ns, buf, vim.tbl_map(function(p)
    return { lnum = 0, col = p.col or 0, severity = vim.diagnostic.severity.ERROR, message = p.msg, source = 'aic' }
  end, problems))
end

local function send(buf, ctx)
  if ctx.sending then
    util.notify('aic: already sending this prompt')
    return
  end
  local lines = api.nvim_buf_get_lines(buf, 0, -1, false)
  local header = lines[1] or ''
  local problems = header_problems(header)
  if #problems > 0 then
    util.notify('aic: not sent: ' .. problems[1].msg, vim.log.levels.ERROR)
    return
  end
  ctx.sending = true
  local tmp = vim.fn.tempname() .. '.json'
  util.write_file(tmp, vim.json.encode({
    header = header,
    body = table.concat(vim.list_slice(lines, 2), '\n'),
    receipt = ctx.receipt,
  }))
  local args = { 'submit', '--rendered', tmp }
  if ctx.now then
    table.insert(args, '--now')
  end
  backend.run(args, state.root, function(ok, out, err, code)
    os.remove(tmp)
    ctx.sending = false
    out, err = vim.trim(out), vim.trim(err)
    if not ok then -- the preview stays (if still open): fix it and :w again
      util.notify(util.oneline(('aic submit failed (%s): %s'):format(code, err ~= '' and err or out)), vim.log.levels.ERROR)
      return
    end
    util.notify(util.oneline(out ~= '' and out or 'aic submit: ok'))
    M.last_prompt[ctx.receipt.doc] = ctx.receipt.prompt
    ctx.sent = true
    if api.nvim_buf_is_valid(buf) then
      if api.nvim_get_current_buf() == buf and ctx.back and api.nvim_buf_is_valid(ctx.back) then
        api.nvim_win_set_buf(0, ctx.back)
      end
      pcall(api.nvim_buf_delete, buf, { force = true })
    end
    state.refresh()
  end)
end

-- Show rendered prompt `r` ({header, body, receipt}) for editing in the
-- current window. ctx = { now = bool }.
function M.preview(r, ctx)
  if M.buf and api.nvim_buf_is_valid(M.buf) then
    pcall(api.nvim_buf_delete, M.buf, { force = true })
  end
  ctx = vim.tbl_extend('force', ctx or {}, { receipt = r.receipt })
  local view = require('aic.view')
  local b = api.nvim_create_buf(false, true)
  M.buf = b
  pcall(api.nvim_buf_set_name, b, 'aic://preview')
  vim.bo[b].buftype = 'acwrite' -- :w runs BufWriteCmd: send
  vim.bo[b].bufhidden = 'wipe' -- leaving it cancels
  vim.bo[b].swapfile = false
  vim.bo[b].filetype = 'markdown'
  local lines = { r.header or '' }
  vim.list_extend(lines, util.split(r.body or ''))
  api.nvim_buf_set_lines(b, 0, -1, false, lines)
  vim.bo[b].modified = false
  local group = api.nvim_create_augroup('aic_preview_' .. b, { clear = true })
  api.nvim_create_autocmd('BufWriteCmd', {
    group = group,
    buffer = b,
    callback = function()
      send(b, ctx)
    end,
  })
  api.nvim_buf_attach(b, false, {
    on_lines = function() -- any edit (typed or not): re-check the header
      vim.schedule(function()
        if api.nvim_buf_is_valid(b) then
          vim.bo[b].modified = false -- never blocks leaving ('nohidden'): leaving = cancel
          mark(b, ctx.now)
        end
      end)
    end,
  })
  api.nvim_create_autocmd('BufWipeout', {
    group = group,
    buffer = b,
    callback = function()
      if ctx.sending then
        util.notify('aic: preview closed while sending; the send goes on')
      elseif not ctx.sent then
        util.notify('aic: preview closed, nothing sent')
      end
      pcall(api.nvim_del_augroup_by_id, group)
    end,
  })
  ctx.back = api.nvim_get_current_buf()
  local win = view.show_buf(b)
  if not (win and api.nvim_win_is_valid(win) and api.nvim_win_get_buf(win) == b) then
    ctx.sent = true -- (no "nothing sent" notice: the refusal was notified already)
    pcall(api.nvim_buf_delete, b, { force = true })
    return nil
  end
  ctx.back = vim.fn.bufnr('#') > 0 and vim.fn.bufnr('#') or ctx.back
  mark(b, ctx.now)
  api.nvim_win_set_cursor(win, { 1, 0 })
  return b
end

---------------------------------------------------------------------------
-- render

-- Render template `o.prompt` for doc `o.doc` and open the preview.
-- o = { prompt, doc, line1, line2, input, now }
function M.render(o)
  local args = { 'render', '--json', '--prompt', o.prompt, '--doc', o.doc }
  if o.line1 then
    vim.list_extend(args, { '--from', tostring(o.line1), '--to', tostring(o.line2 or o.line1) })
  end
  if o.input then
    table.insert(args, '--input=' .. o.input) -- (one argv item even when it starts with -)
  end
  backend.run(args, state.root, function(ok, out, err, code)
    local r = ok and util.decode(out)
    if type(r) ~= 'table' or type(r.receipt) ~= 'table' then
      util.notify(util.oneline(('aic render failed (%s): %s'):format(code, vim.trim(err ~= '' and err or out))),
        vim.log.levels.ERROR)
      return
    end
    M.preview(r, { now = o.now })
  end)
end

-- Ask for the template's input (if it has one), then render.
local function with_input(t, o)
  if not (t.uses_input or t.input) then
    return M.render(o)
  end
  local label = type(t.input) == 'string' and t.input ~= '' and t.input or 'Message'
  vim.ui.input({ prompt = 'aic ' .. t.name .. ': ' .. label .. ': ' }, function(text)
    if text and vim.trim(text) ~= '' then
      o.input = text
      M.render(o)
    end
  end)
end

-- <prefix>s: pick a template, then preview. o = { range, line1, line2, now }
function M.start(o)
  o = o or {}
  local d = doc_mod()
  local buf = api.nvim_get_current_buf()
  local name = d.name_of(buf) or d.current()
  local req = { doc = name, now = o.now }
  if o.range and o.range > 0 then
    if not d.name_of(buf) then
      util.notify('aic: range submit only works in a doc buffer', vim.log.levels.WARN)
      return
    end
    req.line1, req.line2 = o.line1, o.line2
  end
  local db = d.bufnr(name)
  if db and vim.bo[db].modified then
    util.notify(('aic: %s has unsaved changes; the saved file is sent'):format(name), vim.log.levels.WARN)
  end
  backend.run({ 'prompts', '--json' }, state.root, function(ok, out, err)
    local list = ok and util.decode(out)
    if type(list) ~= 'table' then
      util.notify(util.oneline('aic prompts failed: ' .. vim.trim(err ~= '' and err or out)), vim.log.levels.ERROR)
      return
    end
    vim.ui.select(ordered(list, name), {
      prompt = ('aic: submit %s%s with'):format(name, req.line1 and (' lines ' .. req.line1 .. '-' .. req.line2) or ''),
      format_item = function(t)
        if t.error then
          return ('[%s] %s  (broken: %s)'):format(t.source, t.name, util.oneline(t.error))
        end
        -- source first and the description capped: a long description can't hide it
        local desc = vim.fn.strcharpart(util.oneline(t.description ~= '' and t.description or t.header), 0, 80)
        local over = (t.overrides and #t.overrides > 0) and (', overrides ' .. table.concat(t.overrides, ', ')) or ''
        return ('[%s%s] %s — %s'):format(t.source, over, t.name, desc)
      end,
    }, function(t)
      if not t then
        return
      elseif t.error then
        util.notify('aic: ' .. t.error, vim.log.levels.ERROR)
        return
      end
      req.prompt = t.name
      with_input(t, req)
    end)
  end)
end

---------------------------------------------------------------------------
-- managing templates

local SKELETON = [[---
description: what this prompt is for (shown in the picker)
header: "Your typed request to the worker, one line (no digits, no @):"
---
{{content}}
]]

local function user_dir()
  local cfg = vim.env.XDG_CONFIG_HOME
  return ((cfg and cfg ~= '') and cfg or (vim.env.HOME .. '/.config')) .. '/aic/prompts'
end

local function edit_file(path)
  require('aic.view').open_code(path, 1)
end

-- Copy (or create) template `name` into the project or user dir, then edit it.
local function place(name, text)
  vim.ui.select({ 'project (.aic/prompts)', 'user (~/.config/aic/prompts)' }, { prompt = 'aic: put ' .. name .. ' in' },
    function(choice)
      if not choice then
        return
      end
      local d = choice:match('^project') and (state.root .. '/.aic/prompts') or user_dir()
      local p = d .. '/' .. name .. '.md'
      if not util.exists(p) then
        vim.fn.mkdir(d, 'p')
        util.write_file(p, text)
      end
      edit_file(p)
    end)
end

-- <prefix>p: pick a template to edit (built-ins are copied first) or make one.
function M.manage()
  backend.run({ 'prompts', '--json' }, state.root, function(ok, out, err)
    local list = ok and util.decode(out)
    if type(list) ~= 'table' then
      util.notify(util.oneline('aic prompts failed: ' .. vim.trim(err ~= '' and err or out)), vim.log.levels.ERROR)
      return
    end
    table.insert(list, { new = true })
    vim.ui.select(list, {
      prompt = 'aic: prompt templates',
      format_item = function(t)
        if t.new then
          return '+ new template'
        end
        return ('%s  [%s]%s'):format(t.name, t.source, t.error and '  (broken)' or '')
      end,
    }, function(t)
      if not t then
        return
      elseif t.new then
        vim.ui.input({ prompt = 'aic: new template name: ' }, function(n)
          n = n and vim.trim(n):gsub('%.md$', '')
          if n and n ~= '' then
            if not doc_mod().valid(n) then
              util.notify('aic: bad template name ' .. n .. ' (letters, digits, _ . -)', vim.log.levels.ERROR)
              return
            end
            place(n, SKELETON)
          end
        end)
      elseif t.source == 'built-in' then -- read-only: edit a copy that overrides it
        place(t.name, util.read_file(t.path) or SKELETON)
      else
        edit_file(t.path)
      end
    end)
  end)
end

return M
