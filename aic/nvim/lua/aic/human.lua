-- Human edits: diff what is on disk just before a write against what was
-- written, append to human.jsonl. Reading the old content from disk (not a
-- read-time snapshot) works for buffers loaded before setup(), and it also
-- records it honestly when your write overwrites something the worker changed.
local state = require('aic.state')
local util = require('aic.util')

local api = vim.api
local M = {}

M.snap = {} -- [buf] = lines at last read/write

local diff = (vim.text and vim.text.diff) or vim.diff

-- Project-relative path of `buf` if it is a trackable project file.
local function rel_path(buf, root)
  if vim.bo[buf].buftype ~= '' then
    return nil
  end
  local rel = util.rel(root, api.nvim_buf_get_name(buf))
  if not rel or rel:match('^%.aic/') or rel:match('^%.git/') then
    return nil
  end
  return rel
end

-- BufReadPost/BufNewFile: remember content for files inside an aic project.
function M.snapshot(buf, new_file)
  local name = api.nvim_buf_get_name(buf)
  if name == '' or vim.bo[buf].buftype ~= '' then
    return
  end
  local root = state.root or state.find_root(vim.fs.dirname(name))
  if not root or not rel_path(buf, root) then
    return
  end
  M.snap[buf] = new_file and {} or api.nvim_buf_get_lines(buf, 0, -1, false)
end

-- BufWritePre: the file as it is on disk right now is the "before".
function M.pre_write(buf)
  local name = api.nvim_buf_get_name(buf)
  if name == '' or vim.bo[buf].buftype ~= '' then
    return
  end
  local root = state.root or state.find_root(vim.fs.dirname(name))
  if not root or not rel_path(buf, root) then
    return
  end
  M.snap[buf] = vim.fn.filereadable(name) == 1 and vim.fn.readfile(name) or {}
end

-- Hunks (PROTOCOL format) turning `old` into `new`.
function M.diff(old, new)
  local a = #old > 0 and (table.concat(old, '\n') .. '\n') or ''
  local b = #new > 0 and (table.concat(new, '\n') .. '\n') or ''
  local hunks = {}
  for _, h in ipairs(diff(a, b, { result_type = 'indices' })) do
    local sa, ca, sb, cb = h[1], h[2], h[3], h[4]
    local lines = {}
    for i = sa, sa + ca - 1 do
      table.insert(lines, '-' .. old[i])
    end
    for i = sb, sb + cb - 1 do
      table.insert(lines, '+' .. new[i])
    end
    -- pure deletion: vim.diff reports the line before; use the first line after
    table.insert(hunks, { start = cb > 0 and sb or sb + 1, count = cb, lines = lines })
  end
  return hunks
end

-- BufWritePost: append a human event if the content changed.
function M.on_write(buf)
  if not state.active() then
    M.snapshot(buf)
    return
  end
  local rel = rel_path(buf, state.root)
  if not rel then
    return
  end
  local new = api.nvim_buf_get_lines(buf, 0, -1, false)
  local old = M.snap[buf]
  M.snap[buf] = new
  if not old then
    return
  end
  local hunks = M.diff(old, new)
  if #hunks == 0 then
    return
  end
  vim.fn.mkdir(state.dir, 'p')
  local ok, err = pcall(
    util.append_file,
    state.dir .. '/human.jsonl',
    vim.json.encode({ ts = util.now(), file = rel, hunks = hunks }) .. '\n'
  )
  if not ok then
    util.notify('aic: cannot write human.jsonl: ' .. tostring(err), vim.log.levels.ERROR)
  end
end

function M.forget(buf)
  M.snap[buf] = nil
end

return M
