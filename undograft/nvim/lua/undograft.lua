-- undograft: an AI's file edits become normal undo steps in Neovim.
--
-- Driven by the `undograft` Claude Code hook over RPC (see ../../bin/undograft):
--   prepare(path, id)  PreToolUse of tool call `id`: guard the loaded,
--                   unmodified buffer of `path`, so 'autoread' can't reload it
--                   (a reload is one whole-buffer step, and above 'undoreload'
--                   lines it clears the history). A change Neovim notices
--                   while guarded is synced shortly after, in case no
--                   PostToolUse comes.
--   graft(path, id)    PostToolUse: sync the buffer with the file, then
--                   release the call's hold on the guard.
--   graft_file(path, snap, undolevels)   headless, for a file no Neovim has
--                   loaded: load the undofile against the pre-edit snapshot,
--                   add the edit as a step, write the undofile back.
-- Syncing applies what changed on disk as diff hunks, one undo step, then
-- writes the (now identical) buffer back, so Neovim knows the file as read
-- (no "changed since reading" on the next :w) and an undo marks the buffer
-- modified. What it can't express as hunks (encoding, line endings, a final
-- newline appearing or going) gets the usual reload; a deleted file is left
-- to Neovim (E211).

local M = {}
local uv = vim.uv or vim.loop
local diff = (vim.text and vim.text.diff) or vim.diff

-- A tool call whose PostToolUse never comes (denied, interrupted, Neovim
-- busy) lets go of the guard after this long.
M.timeout = 2 * 60 * 1000
-- A change Neovim notices itself is synced this long after, unless the
-- PostToolUse has done it by then.
M.notice_delay = 1000

-- bufnr -> { ids = {id = token}, autocmd, autoread, stat }; survives a reload
-- of this module.
M.guards = {}
for b, g in pairs((package.loaded.undograft or {}).guards or {}) do
  if g.ids then -- (an older version's guards lift themselves)
    M.guards[b] = g
  end
end

local function find_buf(path)
  local real = uv.fs_realpath(path) or path
  for _, b in ipairs(vim.api.nvim_list_bufs()) do
    if vim.api.nvim_buf_is_loaded(b) and vim.bo[b].buftype == "" then
      local name = vim.api.nvim_buf_get_name(b)
      if name == path or (name ~= "" and uv.fs_realpath(name) == real) then
        return b
      end
    end
  end
end

local function unguard(b)
  local g = M.guards[b]
  if not g then
    return
  end
  M.guards[b] = nil
  pcall(vim.api.nvim_del_autocmd, g.autocmd)
  if vim.api.nvim_buf_is_valid(b) then
    if g.autoread == nil then
      vim.api.nvim_buf_call(b, function()
        vim.cmd("set autoread<")
      end)
    else
      vim.bo[b].autoread = g.autoread
    end
  end
end

-- With 'autoread' off for the buffer, a changed file goes to FileChangedShell;
-- there an empty v:fcs_choice makes Neovim only record the new mtime.
local function guard(b, id)
  local g = M.guards[b]
  if not g then
    g = { ids = {}, autoread = vim.bo[b].autoread }
    g.autocmd = vim.api.nvim_create_autocmd("FileChangedShell", {
      buffer = b,
      callback = function()
        package.loaded.undograft.changed(b)
      end,
    })
    vim.bo[b].autoread = false
    M.guards[b] = g
  end
  g.stat = uv.fs_stat(vim.api.nvim_buf_get_name(b)) -- the file as the buffer has it
  local token = {}
  g.ids[id] = token
  vim.defer_fn(function()
    if M.guards[b] == g and g.ids[id] == token then
      g.ids[id] = nil
      if next(g.ids) == nil then
        pcall(M.settle, b)
        unguard(b)
      end
    end
  end, M.timeout)
end

local function release(b, id)
  local g = M.guards[b]
  if g then
    g.ids[id] = nil
    if next(g.ids) == nil then
      unguard(b)
    end
  end
end

local function status(state)
  return vim.json.encode({ state = state, undodir = vim.o.undodir, undolevels = vim.o.undolevels })
end

-- Turn the buffer's lines into `new` with one nvim_buf_set_lines per hunk
-- (one undo step: they all run inside one RPC request or callback).
local function apply(b, new)
  local old = vim.api.nvim_buf_get_lines(b, 0, -1, false)
  local hunks = diff(table.concat(old, "\n") .. "\n", table.concat(new, "\n") .. "\n", { result_type = "indices" })
  for i = #hunks, 1, -1 do
    local a, ac, s, sc = unpack(hunks[i])
    local start = ac == 0 and a or a - 1
    vim.api.nvim_buf_set_lines(b, start, start + ac, false, vim.list_slice(new, s, s + sc - 1))
  end
  if not vim.deep_equal(vim.api.nvim_buf_get_lines(b, 0, -1, false), new) then
    vim.api.nvim_buf_set_lines(b, 0, -1, false, new)
  end
  return #hunks
end

local function read(path)
  local fd = uv.fs_open(path, "r", 438)
  if not fd then
    return nil
  end
  local stat = uv.fs_fstat(fd)
  local data = stat and uv.fs_read(fd, stat.size, 0)
  uv.fs_close(fd)
  if not data or #data ~= stat.size then
    return nil
  end
  return data, stat
end

local function same_file(a, b)
  return a and b and a.size == b.size and a.ino == b.ino and a.mtime.sec == b.mtime.sec
    and a.mtime.nsec == b.mtime.nsec
end

-- The file's bytes split the way the buffer holds lines, or nil when the bytes
-- can't be mapped onto the buffer's lines without a real reload.
local function lines_of(b, data)
  local bo = vim.bo[b]
  if bo.binary or bo.bomb or not (bo.fileencoding == "" or bo.fileencoding == "utf-8") then
    return nil
  end
  if (bo.fileformat ~= "unix" and bo.fileformat ~= "dos") or data == "" or data:sub(1, 3) == "\239\187\191" then
    return nil
  end
  local lines = vim.split(data, "\n", { plain = true })
  local eol = lines[#lines] == ""
  if eol then
    lines[#lines] = nil
  end
  if eol ~= bo.eol then
    return nil
  end
  if bo.fileformat == "dos" then
    for i, l in ipairs(lines) do
      if l:sub(-1) ~= "\r" and (i < #lines or eol) then
        return nil
      end
      lines[i] = l:gsub("\r$", "")
    end
  end
  return lines
end

-- Sync as hunks + a write of identical bytes; nil when it has to be a reload.
local function sync(b)
  local bo = vim.bo[b]
  if not bo.modifiable or bo.readonly then
    return nil
  end
  local path = vim.api.nvim_buf_get_name(b)
  local data, stat = read(path)
  local new = data and lines_of(b, data)
  if not new then
    return nil
  end
  local g = M.guards[b]
  local same = vim.deep_equal(vim.api.nvim_buf_get_lines(b, 0, -1, false), new)
  -- Rewritten with the same text: still write back, Neovim saw a new mtime.
  if same and not (g and g.stat and not same_file(stat, g.stat)) then
    return "same"
  end
  -- The write-back must produce the file's bytes exactly.
  local nl = bo.fileformat == "dos" and "\r\n" or "\n"
  if table.concat(new, nl) .. (bo.eol and nl or "") ~= data then
    return nil
  end
  if not same then
    bo.modified = true -- so that undoing the step marks the buffer modified even if the write fails
    apply(b, new)
    bo.modified = false
  end
  if not same_file(uv.fs_stat(path), stat) then
    return nil -- written again meanwhile: reload whatever is there now
  end
  local fixeol = bo.fixeol
  bo.fixeol = false -- write back exactly what was read
  pcall(vim.api.nvim_buf_call, b, function()
    vim.cmd("silent noautocmd keepalt write!")
  end)
  bo.fixeol = fixeol
  if read(path) ~= data then
    return nil -- another writer got in (or the write failed): take what is there now
  end
  if g then
    g.stat = uv.fs_stat(path)
  end
  return same and "same" or "grafted"
end

-- The usual reload, but keeping the undo history whatever the file's size.
local function reload(b)
  vim.bo[b].modified = false
  vim.api.nvim_buf_call(b, function()
    local ur = vim.o.undoreload
    vim.o.undoreload = -1
    pcall(vim.cmd, "silent edit")
    vim.o.undoreload = ur
  end)
end

-- Bring buffer `b` in line with its file as one undo step.
function M.settle(b)
  if not vim.api.nvim_buf_is_loaded(b) then
    return "absent"
  end
  local exists = function()
    return uv.fs_stat(vim.api.nvim_buf_get_name(b)) ~= nil
  end
  if not exists() then
    return "deleted" -- never reload a missing file (it would empty the buffer)
  end
  if vim.bo[b].modified then
    return "modified" -- the human's unsaved edits: Neovim's own W12 handles it
  end
  local ok, res = pcall(sync, b)
  if ok and res then
    return res
  end
  if not exists() then
    return "deleted"
  end
  reload(b)
  return "fallback"
end

-- FileChangedShell on a guarded buffer.
function M.changed(b)
  local reason = vim.v.fcs_reason
  if reason == "conflict" or reason == "deleted" then
    vim.v.fcs_choice = "ask" -- Neovim's own warning, as if we weren't here
    vim.schedule(function()
      unguard(b)
    end)
    return
  end
  vim.defer_fn(function() -- after the tool's write is surely done; usually the PostToolUse has synced by then
    if M.guards[b] then
      pcall(M.settle, b)
    end
  end, M.notice_delay)
end

function M.prepare(path, id)
  local b = find_buf(path)
  if not b then
    return status("absent")
  end
  if vim.bo[b].modified then
    return status("modified")
  end
  guard(b, id or "")
  return status("guarded")
end

function M.graft(path, id)
  local b = find_buf(path)
  if not b then
    return status("absent")
  end
  local ok, res = pcall(M.settle, b)
  release(b, id or "")
  return status(ok and res or "error")
end

function M.graft_file(path, snap, undolevels)
  vim.cmd("silent edit " .. vim.fn.fnameescape(path))
  local uf = vim.fn.undofile(vim.fn.expand("%:p"))
  if vim.fn.filereadable(uf) == 0 then
    return "no-undofile"
  end
  local new = vim.api.nvim_buf_get_lines(0, 0, -1, false)
  -- The pre-edit text, read the way Neovim reads files, so the undofile's
  -- hash of it matches.
  vim.bo.undolevels = -1
  vim.cmd("silent %delete _")
  vim.cmd("silent 0read ++edit " .. vim.fn.fnameescape(snap))
  vim.cmd("silent $delete _")
  vim.bo.undolevels = math.max(tonumber(undolevels) or 0, 1000)
  pcall(vim.cmd, "silent rundo " .. vim.fn.fnameescape(uf))
  if vim.fn.undotree().seq_last == 0 then
    return "stale" -- the undofile no longer matched the file before this edit
  end
  if apply(0, new) == 0 then
    return "same"
  end
  vim.cmd("silent wundo " .. vim.fn.fnameescape(uf))
  return "grafted"
end

package.loaded.undograft = M
return M
