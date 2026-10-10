-- Project/session state: root + session dir resolution, file watching and
-- cached contents of the session files. Other modules subscribe via M.on().
local backend = require('aic.backend')
local config = require('aic.config')
local util = require('aic.util')

local uv = vim.uv or vim.loop

local M = {
  root = nil, -- project root (contains .aic/)
  dir = nil, -- absolute session dir
  sid = nil,
  status = nil, -- last `aic status --json`
  events = {}, -- trace events in file order
  by_id = {},
  touched = {},
  diagnostics = nil,
  seen = {}, -- set: id -> true
  hold = false,
  compiling = false, -- `compiling` file exists
  local_compiling = false, -- a compile we started is running
  compile_queued = false, -- backend queued one more pass after the running one
  asking = 0,
  usage = nil, -- usage.json of the session
  quota = nil, -- account quota from `aic usage --json` (nil when unknown)
}

local listeners = {}
local stats = {}
local trace_offset = 0
local trace_ino = nil -- the watcher rebuilds trace.jsonl as a new file (new inode)
local handles = {} -- fs_event / timer handles for the session dir
local root_handles = {}
-- debounce/throttle closures own a uv timer: create them once and reuse them
-- across sessions and roots instead of leaking one per set_dir/activate
local refresh_soon, resolve_soon

function M.on(name, fn)
  listeners[name] = listeners[name] or {}
  table.insert(listeners[name], fn)
end

function M.emit(name, ...)
  for _, fn in ipairs(listeners[name] or {}) do
    local ok, err = pcall(fn, ...)
    if not ok then
      util.notify(('aic: %s handler failed: %s'):format(name, err), vim.log.levels.WARN)
    end
  end
end

-- Nearest ancestor of `path` (default cwd) containing .aic/.
function M.find_root(path)
  return vim.fs.root(path or vim.fn.getcwd(), '.aic')
end

function M.fallback_root(path)
  return vim.fs.root(path or vim.fn.getcwd(), '.git') or vim.fn.getcwd()
end

function M.active()
  return M.root ~= nil and M.dir ~= nil
end

local function close_handles(list)
  for _, h in ipairs(list) do
    if not h:is_closing() then
      h:stop()
      h:close()
    end
  end
  for i = #list, 1, -1 do
    list[i] = nil
  end
end

-- Watch `path` with fs_event; fall back to a polling timer calling `on_poll`.
local function watch(path, list, cb, poll_cb)
  local h = uv.new_fs_event()
  local ok = h and h:start(path, {}, function(err)
    if not err then
      cb()
    end
  end)
  if ok then
    table.insert(list, h)
    return
  end
  if h then
    h:close()
  end
  if poll_cb then
    local t = uv.new_timer()
    t:start(config.options.poll_interval, config.options.poll_interval, function()
      vim.schedule(poll_cb)
    end)
    table.insert(list, t)
  end
end

local function reset_cache()
  M.events, M.by_id, M.touched, M.diagnostics, M.seen = {}, {}, {}, nil, {}
  M.usage = nil
  M.hold, M.compiling = false, false
  stats = {}
  trace_offset = 0
end

local function set_dir(dir)
  close_handles(handles)
  reset_cache()
  M.dir = dir
  if dir then
    refresh_soon = refresh_soon or util.debounce(config.options.debounce, function()
      M.refresh()
    end)
    watch(dir, handles, refresh_soon, function()
      M.refresh()
    end)
    M.refresh(true)
  end
  M.emit('session')
  M.emit('refresh')
end

-- Run `aic status --json` and (re)point at the session dir it names.
function M.resolve(cb)
  if not M.root then
    if cb then
      cb()
    end
    return
  end
  backend.run({ 'status', '--json' }, M.root, function(ok, out, err)
    local st = ok and util.decode(out)
    if type(st) ~= 'table' then
      if not M.status_warned then
        M.status_warned = true
        util.notify('aic status failed: ' .. vim.trim(err ~= '' and err or out), vim.log.levels.WARN)
      end
      if cb then
        cb()
      end
      return
    end
    M.status_warned = false
    M.status = st
    if type(st.root) == 'string' and st.root ~= '' then
      M.root = st.root
    end
    local dir = type(st.dir) == 'string' and st.dir ~= '' and util.abs(M.root, st.dir) or nil
    M.sid = st.session_id
    if dir ~= M.dir then
      set_dir(dir)
    else
      M.emit('refresh')
    end
    if cb then
      cb()
    end
  end)
end

function M.deactivate()
  close_handles(handles)
  close_handles(root_handles)
  reset_cache()
  M.root, M.dir, M.sid, M.status = nil, nil, nil, nil
end

-- Activate aic for `root` (no-op if already active there).
function M.activate(root, cb)
  if M.root == root and M.dir then
    if cb then
      cb()
    end
    return
  end
  if M.root ~= root then
    M.deactivate()
    M.root = root
    -- attachment changes (.aic/current, new workers) => re-resolve the session
    resolve_soon = resolve_soon or util.throttle(1000, function()
      M.resolve()
    end)
    watch(root .. '/.aic', root_handles, resolve_soon)
    watch(root .. '/.aic/workers', root_handles, resolve_soon)
  end
  M.resolve(cb)
end

---------------------------------------------------------------------------
-- loading session files

local function load_trace(st)
  if not st then
    M.events, M.by_id, trace_offset = {}, {}, 0
    return
  end
  if st.size < trace_offset or (trace_ino and st.ino ~= trace_ino) then
    M.events, M.by_id, trace_offset = {}, {}, 0
  end
  trace_ino = st.ino
  local f = io.open(M.dir .. '/trace.jsonl', 'rb')
  if not f then
    return
  end
  f:seek('set', trace_offset)
  local data = f:read('*a') or ''
  f:close()
  local last_nl = data:match('.*()\n')
  if not last_nl then
    return -- no complete line yet
  end
  trace_offset = trace_offset + last_nl
  for line in data:sub(1, last_nl):gmatch('[^\n]+') do
    local ev = util.decode(line)
    if type(ev) == 'table' and ev.id then
      ev.id = tostring(ev.id)
      local old = M.by_id[ev.id]
      if old then
        for k in pairs(old) do
          old[k] = nil
        end
        for k, v in pairs(ev) do
          old[k] = v
        end
      else
        table.insert(M.events, ev)
        M.by_id[ev.id] = ev
      end
    end
  end
end

local function load_seen()
  local s = util.read_json(M.dir .. '/seen.json')
  M.seen = {}
  if type(s) == 'table' and type(s.seen) == 'table' then
    for _, id in ipairs(s.seen) do
      M.seen[tostring(id)] = true
    end
  end
end

local files = { 'trace.jsonl', 'seen.json', 'touched.json', 'diagnostics.json', 'chat.md', 'hold', 'compiling', 'usage.json' }

-- Re-stat the session files and reload the ones that changed.
-- `initial` is passed to listeners (true on the first load of a session).
function M.refresh(initial)
  if not M.dir then
    return
  end
  local changed = {}
  for _, name in ipairs(files) do
    local st = uv.fs_stat(M.dir .. '/' .. name)
    local sig = st and (st.mtime.sec .. '.' .. st.mtime.nsec .. ':' .. st.size) or 'none'
    if initial or sig ~= stats[name] then
      stats[name] = sig
      changed[name] = st or false
    end
  end
  if next(changed) == nil then
    return
  end
  if changed['trace.jsonl'] ~= nil then
    load_trace(changed['trace.jsonl'])
  end
  if changed['seen.json'] ~= nil then
    load_seen()
  end
  if changed['trace.jsonl'] ~= nil or changed['seen.json'] ~= nil then
    M.emit('trace')
  end
  if changed['touched.json'] ~= nil then
    local t = util.read_json(M.dir .. '/touched.json')
    M.touched = type(t) == 'table' and t or {}
    M.emit('touched')
  end
  if changed['diagnostics.json'] ~= nil then
    M.diagnostics = util.read_json(M.dir .. '/diagnostics.json')
    M.emit('diagnostics', M.diagnostics, initial)
  end
  if changed['chat.md'] ~= nil then
    M.emit('chat')
  end
  if changed['usage.json'] ~= nil then
    local u = util.read_json(M.dir .. '/usage.json')
    M.usage = type(u) == 'table' and u or nil
    M.emit('usage', initial)
  end
  if changed['hold'] ~= nil then
    M.hold = changed['hold'] ~= false
  end
  if changed['compiling'] ~= nil then
    M.compiling = changed['compiling'] ~= false
    if not M.compiling then
      M.compile_queued = false
    end
  end
  M.emit('refresh')
end

---------------------------------------------------------------------------
-- seen state

-- Events that count as inbox items (worker activity).
function M.seenable(ev)
  return ev and (ev.kind == 'tool' or ev.kind == 'text')
end

function M.is_unseen(ev)
  return M.seenable(ev) and not M.seen[ev.id]
end

function M.unseen_count()
  local n = 0
  for _, ev in ipairs(M.events) do
    if M.is_unseen(ev) then
      n = n + 1
    end
  end
  return n
end

local function save_seen()
  if not M.dir then
    return
  end
  local ids = vim.tbl_keys(M.seen)
  table.sort(ids)
  vim.fn.mkdir(M.dir, 'p')
  local ok, err = pcall(util.write_atomic, M.dir .. '/seen.json', vim.json.encode({ seen = ids }))
  if not ok then
    util.notify('aic: cannot write seen.json: ' .. tostring(err), vim.log.levels.ERROR)
  end
end

-- Mark event ids seen (val=true) or unseen (val=false).
function M.set_seen(ids, val)
  local changed = false
  for _, id in ipairs(ids) do
    if (M.seen[id] or false) ~= val then
      M.seen[id] = val or nil
      changed = true
    end
  end
  if changed then
    save_seen()
    M.emit('trace')
    M.emit('refresh')
  end
end

function M.set_local_compiling(v)
  M.local_compiling = v
  M.emit('refresh')
end

return M
