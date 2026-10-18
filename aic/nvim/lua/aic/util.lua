-- Small shared helpers: file IO, JSON, timers, floats.
local M = {}
local uv = vim.uv or vim.loop

-- Floats opened by M.float (answers, blame, usage): win -> true. Jumping to
-- code from one of them closes it; other floats are never closed by aic.
M.floats = setmetatable({}, { __mode = 'k' })

function M.notify(msg, level)
  vim.notify(msg, level or vim.log.levels.INFO, { title = 'aic' })
end

function M.read_file(path)
  local f = path and io.open(path, 'rb')
  if not f then
    return nil
  end
  local s = f:read('*a')
  f:close()
  return s
end

function M.read_json(path)
  local s = M.read_file(path)
  if not s or s:match('^%s*$') then
    return nil
  end
  local ok, v = pcall(vim.json.decode, s, { luanil = { object = true } })
  if ok then
    return v
  end
end

function M.decode(s)
  local ok, v = pcall(vim.json.decode, s or '', { luanil = { object = true } })
  if ok then
    return v
  end
end

function M.split(s)
  if not s or s == '' then
    return {}
  end
  return vim.split(s, '\n', { plain = true })
end

-- Lines of a file (trailing newline does not produce an extra empty line).
function M.read_lines(path)
  local s = M.read_file(path)
  if not s then
    return nil
  end
  s = s:gsub('\n$', '')
  if s == '' then
    return {}
  end
  return M.split(s)
end

function M.first_line(s)
  if type(s) ~= 'string' then
    return ''
  end
  return (s:match('^%s*([^\n]*)') or ''):gsub('%s+$', '')
end

-- `s` as one line no wider than the screen minus 30 (never a hit-enter prompt).
function M.oneline(s)
  s = tostring(s or ''):gsub('%s*\n%s*', ' · ')
  s = vim.trim(s)
  local max = math.max(20, vim.o.columns - 30)
  if vim.fn.strdisplaywidth(s) > max then
    s = vim.fn.strcharpart(s, 0, max - 1) .. '…'
  end
  return s
end

function M.write_file(path, data)
  local f, err = io.open(path, 'wb')
  if not f then
    error(err)
  end
  f:write(data)
  f:close()
end

-- Write via tmp file + rename so readers never see a partial file.
function M.write_atomic(path, data)
  local tmp = path .. '.tmp.' .. uv.os_getpid()
  M.write_file(tmp, data)
  local ok, err = uv.fs_rename(tmp, path)
  if not ok then
    os.remove(tmp)
    error(err)
  end
end

function M.append_file(path, data)
  local f, err = io.open(path, 'ab')
  if not f then
    error(err)
  end
  f:write(data)
  f:close()
end

function M.exists(path)
  return path ~= nil and uv.fs_stat(path) ~= nil
end

function M.now()
  local s, us = uv.gettimeofday()
  return s + us / 1e6
end

-- Path of `abs` relative to `root`, or nil when outside it.
function M.rel(root, abs)
  if not root or not abs or abs == '' then
    return nil
  end
  abs = vim.fs.normalize(abs)
  root = vim.fs.normalize(root)
  if abs:sub(1, #root + 1) == root .. '/' then
    return abs:sub(#root + 2)
  end
end

function M.abs(root, path)
  if not path or path == '' then
    return nil
  end
  if path:sub(1, 1) == '/' then
    return path
  end
  return root .. '/' .. path
end

-- Returns a function that runs `fn` (scheduled) `ms` after the last call.
-- Safe to call from luv callbacks.
function M.debounce(ms, fn)
  local timer = uv.new_timer()
  return function()
    timer:stop()
    timer:start(ms, 0, function()
      vim.schedule(fn)
    end)
  end
end

-- Runs `fn` at most once per `ms` (trailing edge).
function M.throttle(ms, fn)
  local timer = uv.new_timer()
  return function()
    if timer:is_active() then
      return
    end
    timer:start(ms, 0, function()
      vim.schedule(fn)
    end)
  end
end

-- Centered floating window with a scratch buffer. Returns buf, win.
function M.float(lines, opts)
  opts = opts or {}
  local buf = vim.api.nvim_create_buf(false, true)
  vim.bo[buf].bufhidden = 'wipe'
  vim.api.nvim_buf_set_lines(buf, 0, -1, false, lines)
  if opts.filetype then
    vim.bo[buf].filetype = opts.filetype
  end
  local cols, rows = vim.o.columns, vim.o.lines
  local width = math.min(opts.width or 90, cols - 6)
  local height = math.min(opts.height or 20, rows - 6)
  local win = vim.api.nvim_open_win(buf, true, {
    relative = 'editor',
    width = math.max(width, 20),
    height = math.max(height, 3),
    row = math.floor((rows - height) / 2) - 1,
    col = math.floor((cols - width) / 2),
    style = 'minimal',
    border = 'rounded',
    title = opts.title,
    title_pos = opts.title and 'center' or nil,
  })
  M.floats[win] = true
  vim.wo[win].wrap = true
  vim.wo[win].linebreak = true
  vim.wo[win].foldenable = false -- markdown folding (or nvim-ufo) would hide answer lines
  local close = function()
    if vim.api.nvim_win_is_valid(win) then
      vim.api.nvim_win_close(win, true)
    end
  end
  vim.keymap.set('n', 'q', close, { buffer = buf, nowait = true })
  vim.keymap.set('n', '<Esc>', close, { buffer = buf, nowait = true })
  return buf, win
end

function M.set_lines(buf, lines)
  if not vim.api.nvim_buf_is_valid(buf) then
    return
  end
  local mod = vim.bo[buf].modifiable
  vim.bo[buf].modifiable = true
  vim.api.nvim_buf_set_lines(buf, 0, -1, false, lines)
  vim.bo[buf].modifiable = mod
end

-- Detach nvim-ufo from a buffer so our own folds work.
function M.detach_ufo(buf)
  local ok, ufo = pcall(require, 'ufo')
  if ok and type(ufo) == 'table' and ufo.detach then
    pcall(ufo.detach, buf)
  end
end

return M
