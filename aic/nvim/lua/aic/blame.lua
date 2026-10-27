-- AI blame from aigource (`aigource --serve`, aigource/README.md "Serve"): the
-- line number of a line an AI turn wrote takes that turn's colour, the cursor
-- line's blame text (gitsigns' formatter) names the prompt; ]w/[w, the detail
-- float, focus in aigource and the turn's lines in the quickfix list.
local config = require('aic.config')
local util = require('aic.util')

local api = vim.api
local uv = vim.uv or vim.loop
local diff = (vim.text and vim.text.diff) or vim.diff
local M = {}

M.ns = api.nvim_create_namespace('aic_blame')
M.line_ns = api.nvim_create_namespace('aic_blame_line') -- the cursor line's text, untracked buffers
-- Colours are only drawn this many lines around what each window shows.
M.margin = 200
-- aigource's blame palette (sv_blame_col in aigource/src/sview.c), slot 0..7.
M.palette = { '#f2a541', '#56d364', '#79c0ff', '#d2a8ff', '#ff7b72', '#39c5cf', '#e3b341', '#ffa198' }
M.clients = {} -- [root] = client: one connection per repo
local bufs = {} -- [buf] = { root, path (real), rel, rev, status, base_text, base_cid, changes, turns, map, cid, untracked }
M.bufs = bufs
-- A root without transcripts, or a dir in no repo, is asked again after this
-- many ms (on BufEnter): a later `git init` or first session shows up.
M.recheck = 30000
local roots = {} -- [dir] = { root = root | false, at }
local sessions = {} -- [root] = { ok, at, scanning, rejected = { [dir] = mtime } }
M.sessions = sessions
local attached = {} -- [buf] = true while nvim_buf_attach listens
local dirty = {} -- [buf] = true: text changed since the last mapping
local disabled = false

function M.define_highlights()
  for i, c in ipairs(M.palette) do
    api.nvim_set_hl(0, 'AicTurn' .. i, { fg = c, default = true })
  end
end
M.define_highlights()

---------------------------------------------------------------------------
-- client

local function sock_name(root)
  local r = vim.system({ 'sha1sum' }, { stdin = root, text = true }):wait()
  local hex = r.code == 0 and r.stdout:match('^(%x+)')
  local ns = vim.env.AIG_SOCK_NS
  -- the abstract namespace: a leading NUL; pipe_connect2 passes the name's length
  return hex and ('\0' .. ((ns and ns ~= '') and ns or 'aigource') .. '/' .. hex:sub(1, 16))
end

local function spawn(args, root)
  local h
  h = uv.spawn(config.options.aigource, { args = args, cwd = root, detached = true }, function()
    h:close()
  end)
  if h then
    h:unref()
  end
  return h ~= nil
end

local connect, fetch

local NOT_CONNECTED = 'aic: blame server not connected'

-- `user`: a request you made (a key); dropped unanswered, it's notified.
local function send(cl, msg, cb, user)
  if not (cl and cl.pipe) then
    if user then
      util.notify(NOT_CONNECTED)
    end
    return false
  end
  cl.next_id = cl.next_id + 1
  msg.id = cl.next_id
  cl.cbs[msg.id] = cb
  cl.user[msg.id] = user
  cl.pipe:write(vim.json.encode(msg) .. '\n')
  return true
end

local on_focus

-- Each message from the host, in order.
local function handle(cl, line)
  local m = util.decode(line)
  if type(m) ~= 'table' then
    return
  end
  if m.event == 'changed' then
    local named = {}
    for _, f in ipairs(m.files or {}) do
      named[util.abs(cl.root, f)] = true
    end
    for buf, b in pairs(bufs) do
      if b.root == cl.root and named[b.path] and api.nvim_buf_is_loaded(buf) then
        fetch(buf)
      end
    end
  elseif m.event == 'handover' then
    cl.handover = true -- the next host binds as this one goes: wait for it, don't spawn
  elseif m.id and cl.cbs[m.id] then
    local cb = cl.cbs[m.id]
    cl.cbs[m.id], cl.user[m.id] = nil, nil
    cb(m)
  end
end

local function gone(cl, p)
  if cl.pipe ~= p then
    return
  end
  p:close()
  if next(cl.user) then
    util.notify(NOT_CONNECTED .. ' (it went away); try again')
  end
  cl.pipe, cl.cbs, cl.user = nil, {}, {}
  if cl.handover then
    cl.wait_until = uv.now() + 30000
  end
  cl.handover, cl.spawned, cl.tries = nil, nil, 0
  connect(cl)
end

local function up(cl, p)
  cl.pipe, cl.tries, cl.spawned, cl.wait_until, cl.down = p, 0, nil, nil, nil
  local parts = {}
  p:read_start(function(err, data)
    if err or not data then
      return vim.schedule(function()
        gone(cl, p)
      end)
    end
    local last = data:match('.*()\n')
    if not last then
      parts[#parts + 1] = data
      return
    end
    local whole = table.concat(parts) .. data:sub(1, last - 1)
    parts = { data:sub(last + 1) }
    vim.schedule(function()
      for _, line in ipairs(vim.split(whole, '\n', { plain = true })) do
        if cl.pipe == p then
          handle(cl, line)
        end
      end
    end)
  end)
  send(cl, { cmd = 'subscribe' })
  -- maybe another host: revs start over, so every buffer of the root asks again
  for buf, b in pairs(bufs) do
    if b.root == cl.root and api.nvim_buf_is_loaded(buf) then
      b.rev = nil
      fetch(buf)
    end
  end
  local again = cl.refocus
  cl.refocus = nil
  if again then
    send(cl, again, function(r)
      on_focus(cl, again, r)
    end)
  end
end

-- Refused: during a handover keep asking (30 s); else start a server once and
-- ask again 50 ms .. 1 s apart.
local function retry(cl)
  cl.tries = cl.tries + 1
  if cl.wait_until and uv.now() < cl.wait_until then
    return vim.defer_fn(function()
      connect(cl)
    end, 100)
  end
  cl.wait_until = nil
  if not cl.spawned then
    cl.spawned, cl.tries = true, 0
    spawn({ '--serve', '-r', cl.root }, cl.root)
  end
  if cl.tries > 40 then
    cl.down = true -- the next buffer that asks starts over
    if not cl.noticed then
      cl.noticed = true
      util.notify('aic: no aigource server for ' .. cl.root, vim.log.levels.WARN)
    end
    return
  end
  vim.defer_fn(function()
    connect(cl)
  end, math.min(1000, 25 * 2 ^ cl.tries))
end

connect = function(cl)
  if cl.pipe or cl.connecting then
    return
  end
  local p = uv.new_pipe(false)
  cl.connecting = true
  local req = uv.pipe_connect2(p, cl.name, {}, function(err)
    vim.schedule(function()
      cl.connecting = nil
      if err then
        p:close()
        retry(cl)
      else
        up(cl, p)
      end
    end)
  end)
  if not req then
    cl.connecting = nil
    p:close()
    retry(cl)
  end
end

-- Claude Code's transcripts for `root`, found as aigource finds them
-- (ingest.c find_projdirs): <projects>/<slug>, or a <slug>-… dir holding a
-- transcript whose cwd (in its first 64 KB) is below root, a session started
-- in root/sub. The exact dir is a stat; the prefix dirs are read off the main
-- loop, each again only when its mtime changed.
local function projects()
  local cfg = vim.env.CLAUDE_CONFIG_DIR
  return (cfg or ((vim.env.HOME or '') .. '/.claude')) .. '/projects'
end

-- Runs in a coroutine: a luv call's async form, its callback's results.
local function await(f, ...)
  local co, args = coroutine.running(), { n = select('#', ...), ... }
  args[args.n + 1] = function(...)
    coroutine.resume(co, ...)
  end
  if not f(unpack(args, 1, args.n + 1)) then
    return 'not queued'
  end
  return coroutine.yield()
end

local function scan_prefix(root, s, done)
  local base, want = projects(), root:gsub('[^A-Za-z0-9]', '-')
  local function scan()
    local found = false
    local _, d = await(uv.fs_scandir, base)
    while d and not found do
      local name = uv.fs_scandir_next(d)
      if not name then
        break
      end
      local dir, st = base .. '/' .. name, nil
      if name:sub(1, #want + 1) == want .. '-' then
        _, st = await(uv.fs_stat, dir)
      end
      local mt = st and (st.mtime.sec .. '.' .. st.mtime.nsec) -- ns: a file added the same second counts
      if st and s.rejected[dir] ~= mt then -- new, or changed since it was rejected
        local _, sd = await(uv.fs_scandir, dir)
        while sd and not found do
          local f = uv.fs_scandir_next(sd)
          if not f then
            break
          end
          local fd
          if f:sub(-6) == '.jsonl' then
            _, fd = await(uv.fs_open, dir .. '/' .. f, 'r', 438)
          end
          if fd then
            local _, head = await(uv.fs_read, fd, 65535, 0)
            await(uv.fs_close, fd)
            local cwd = (head or ''):match('"cwd":"([^"]*)"')
            found = cwd ~= nil and cwd:sub(1, #root + 1) == root .. '/'
          end
        end
        if not found then
          s.rejected[dir] = mt
        end
      end
    end
    return found
  end
  coroutine.wrap(function()
    local ok, found = pcall(scan)
    vim.schedule(function()
      done(ok and found)
    end)
  end)()
end

local function client(root)
  if M.clients[root] or disabled then
    return M.clients[root]
  end
  local s = sessions[root]
  if not s then
    s = { rejected = {} }
    sessions[root] = s
  end
  s.ok = s.ok or uv.fs_stat(projects() .. '/' .. root:gsub('[^A-Za-z0-9]', '-')) ~= nil
  if not s.ok then
    if not s.scanning and (not s.at or uv.now() - s.at >= M.recheck) then
      s.scanning, s.at = true, uv.now()
      scan_prefix(root, s, function(found)
        s.scanning, s.ok = nil, found
        for buf, b in pairs(bufs) do
          if found and b.root == root and api.nvim_buf_is_loaded(buf) then
            fetch(buf)
          end
        end
      end)
    end
    return -- no AI worked here (as far as we know yet): no server
  end
  local bin = config.options.aigource
  if not bin or vim.fn.executable(bin) ~= 1 then
    disabled = true
    util.notify(('aic: AI blame off: %s not found (option `aigource`)'):format(tostring(bin)), vim.log.levels.WARN)
    return
  end
  local name = sock_name(root)
  if not name then
    return
  end
  local cl = { root = root, name = name, next_id = 0, cbs = {}, user = {}, tries = 0 }
  M.clients[root] = cl
  connect(cl)
  return cl
end

---------------------------------------------------------------------------
-- per buffer: base, mapping, drawing

-- The change, turn and colour of buffer line `lnum`, or nil (not an AI's).
local function at(buf, lnum)
  local b = bufs[buf]
  local cid = b and b.cid and b.cid[lnum]
  local ch = cid and cid >= 0 and b.changes[tostring(cid)]
  if not ch then
    return
  end
  local turn = ch.turn_key and b.turns[ch.turn_key]
  return { cid = cid, change = ch, key = ch.turn_key, turn = turn, slot = turn and tonumber(turn.slot), base = b.map and b.map[lnum] }
end
M.at = at

-- Buffer lines -> base lines (nil for lines added since), and their changes.
local function remap(buf)
  local b = bufs[buf]
  local n = api.nvim_buf_line_count(buf)
  if n > config.options.blame_max_lines then
    b.cid, b.map = nil, nil
    if not b.too_big then
      b.too_big = true
      util.notify(('aic: AI blame off for this buffer: %d lines (option `blame_max_lines`)'):format(n))
    end
    return
  end
  local lines = api.nvim_buf_get_lines(buf, 0, -1, false)
  local text = table.concat(lines, '\n') .. '\n'
  local map = {}
  if text == b.base_text then
    for l = 1, #lines do
      map[l] = l
    end
  else
    local a, l = 1, 1
    for _, h in ipairs(diff(b.base_text, text, { result_type = 'indices' })) do
      local ca, sb, cb = h[2], h[3], h[4]
      for _ = l, (cb == 0 and sb + 1 or sb) - 1 do
        map[l], a, l = a, a + 1, l + 1
      end
      l, a = l + cb, a + ca
    end
    for _ = l, #lines do
      map[l], a, l = a, a + 1, l + 1
    end
  end
  b.map, b.cid = map, {}
  for i = 1, #lines do
    b.cid[i] = map[i] and b.base_cid[map[i]] or -1
  end
end

-- Lines (1-based, inclusive) around the visible part of each window on `buf`.
local function spans(buf)
  local out = {}
  for _, w in ipairs(vim.fn.win_findbuf(buf)) do
    local top, bot = unpack(api.nvim_win_call(w, function()
      return { vim.fn.line('w0'), vim.fn.line('w$') }
    end))
    table.insert(out, { top - M.margin, bot + M.margin })
  end
  return out
end

local function draw(buf)
  api.nvim_buf_clear_namespace(buf, M.ns, 0, -1)
  local b = bufs[buf]
  if not (b and b.cid) then
    return
  end
  local n, done = api.nvim_buf_line_count(buf), {}
  for _, sp in ipairs(spans(buf)) do
    for l = math.max(1, sp[1]), math.min(n, sp[2]) do
      local i = not done[l] and at(buf, l)
      done[l] = true
      if i and i.slot then
        api.nvim_buf_set_extmark(buf, M.ns, l - 1, 0, { number_hl_group = 'AicTurn' .. (i.slot % 8 + 1), priority = 5 })
      end
    end
  end
end

local function ago(ts)
  return util.ago(ts) .. ' ago'
end

-- The cursor line's blame text for an AI line `i`.
local function chunks(i)
  local what = i.turn and util.first_line(i.turn.prompt) or (i.change.agent or i.change.tool or 'AI')
  if vim.fn.strchars(what) > 60 then
    what = vim.fn.strcharpart(what, 0, 59) .. '…'
  end
  local hl = 'GitSignsCurrentLineBlame'
  return { { '◆ ', i.slot and ('AicTurn' .. (i.slot % 8 + 1)) or hl }, { what .. ' · ' .. ago(i.change.ts), hl } }
end

-- gitsigns has no cursor-line blame in an untracked buffer: draw ours there.
local function cursor_text(buf)
  if not api.nvim_buf_is_valid(buf) then
    return
  end
  api.nvim_buf_clear_namespace(buf, M.line_ns, 0, -1)
  local b = bufs[buf]
  if not (b and b.untracked) or buf ~= api.nvim_get_current_buf() or api.nvim_get_mode().mode:sub(1, 1) == 'i' then
    return
  end
  local l = api.nvim_win_get_cursor(0)[1]
  local i = at(buf, l)
  if i then
    api.nvim_buf_set_extmark(buf, M.line_ns, l - 1, 0, { virt_text = chunks(i), virt_text_pos = 'eol', hl_mode = 'combine' })
  end
end

local function refresh_gitsigns(buf)
  local clb = package.loaded['gitsigns.current_line_blame']
  if type(clb) == 'table' and clb.update and buf == api.nvim_get_current_buf() then
    pcall(clb.update, buf)
  end
end

local function keys(buf)
  if vim.b[buf].aic_blame_maps or not config.options.keymaps then
    return
  end
  vim.b[buf].aic_blame_maps = true
  vim.keymap.set('n', ']w', function()
    M.jump(1)
  end, { buffer = buf, desc = 'aic: next AI run' })
  vim.keymap.set('n', '[w', function()
    M.jump(-1)
  end, { buffer = buf, desc = 'aic: previous AI run' })
end

-- Tracked by git? gitsigns draws the cursor-line blame only then.
local function track(buf)
  local b = bufs[buf]
  if not b then
    return
  end
  vim.system({ 'git', 'ls-files', '--error-unmatch', '--', b.path }, { cwd = b.root }, function(o)
    vim.schedule(function()
      if bufs[buf] == b then
        b.untracked = o.code ~= 0
        cursor_text(buf)
      end
    end)
  end)
end

local function on_blame(buf, b, r)
  if bufs[buf] ~= b or not api.nvim_buf_is_loaded(buf) or (b.rev and r.rev and r.rev < b.rev) then
    return
  end
  if r.status == 'building' then
    return -- the first build's `changed` push names every file
  end
  b.rev, b.status = r.rev, r.status
  b.base_text, b.base_cid, b.cid, b.map = nil, nil, nil, nil
  local cids, any = {}, false
  for _, run in ipairs(r.status == 'ok' and r.runs or {}) do
    any = any or run[2] >= 0
    for _ = 1, run[1] do
      cids[#cids + 1] = run[2]
    end
  end
  if any then -- else no base: nothing to map while you type
    local text = r.text or ''
    b.base_text = (text == '' or text:sub(-1) == '\n') and text or text .. '\n'
    b.base_cid, b.changes, b.turns = cids, r.changes or {}, r.turns or {}
    remap(buf)
    keys(buf)
    track(buf)
  end
  draw(buf)
  cursor_text(buf)
  refresh_gitsigns(buf)
end

-- Ask for `buf`'s blame; `after` runs once its reply is in.
fetch = function(buf, after)
  local b = bufs[buf]
  local cl = b and client(b.root)
  if not cl then
    return
  end
  if cl.down then
    cl.down, cl.tries, cl.spawned = nil, 0, nil
    connect(cl)
  end
  send(cl, { cmd = 'blame', file = b.rel }, function(r)
    on_blame(buf, b, r)
    if after then
      after()
    end
  end) -- not connected: the connection asks for every buffer when it's up
end

local remap_dirty = util.debounce(150, function()
  for buf in pairs(dirty) do
    dirty[buf] = nil
    if bufs[buf] and bufs[buf].base_text and api.nvim_buf_is_loaded(buf) then
      remap(buf)
      draw(buf)
      cursor_text(buf)
    end
  end
end)

-- Every change (typing, undo, an RPC edit, a reload) maps the buffer again.
local function listen(buf)
  if attached[buf] then
    return
  end
  attached[buf] = true
  local function changed()
    if not bufs[buf] then
      attached[buf] = nil
      return true -- detach
    end
    dirty[buf] = true
    remap_dirty()
  end
  api.nvim_buf_attach(buf, false, {
    on_lines = changed,
    on_reload = changed,
    on_detach = function()
      attached[buf] = nil
    end,
  })
end

local function root_of(path)
  local dir = vim.fs.dirname(path)
  local c = roots[dir]
  if not c or (not c.root and uv.now() - c.at >= M.recheck) then
    local r = vim.fs.root(path, '.git')
    c = { root = r and uv.fs_realpath(r) or false, at = uv.now() }
    roots[dir] = c
  end
  return c.root or nil
end

-- Start following `buf` (a file in a git repo), and ask for its blame.
function M.attach(buf)
  if disabled or not api.nvim_buf_is_loaded(buf) or vim.bo[buf].buftype ~= '' then
    return
  end
  local name = api.nvim_buf_get_name(buf)
  if name:sub(1, 1) ~= '/' or name:find('/.git/', 1, true) then
    return
  end
  local path = uv.fs_realpath(name) or name -- a link into a repo: the repo's file
  local root = root_of(path)
  local rel = root and util.rel(root, path)
  if not rel then
    return
  end
  bufs[buf] = { root = root, path = path, rel = rel }
  listen(buf)
  fetch(buf)
end

-- The file changed on disk: the next build reads it.
local function saved(buf)
  local b = bufs[buf]
  if b then
    send(M.clients[b.root], { cmd = 'saved', file = b.rel })
  end
end

---------------------------------------------------------------------------
-- gitsigns' current_line_blame_formatter(_nc)

-- gitsigns' own default text for a line no AI wrote.
function M.git_default(username, info)
  local nc = info.author == 'Not Committed Yet'
  local ok, s = pcall(function()
    local key = nc and 'current_line_blame_formatter_nc' or 'current_line_blame_formatter'
    local fmt = require('gitsigns.config').schema[key].default
    return require('gitsigns.blame_formatter').expand_string(fmt, username, info, { self_author_text = 'You' })
  end)
  if not ok or type(s) ~= 'string' then -- gitsigns moved it (an internal module)
    local author = info.author == username and 'You' or tostring(info.author or '?')
    s = nc and (' ' .. author) or (' %s, %s - %s '):format(author, ago(info.author_time), info.summary or '')
  end
  return { { s, 'GitSignsCurrentLineBlame' } }
end

-- An AI line: `◆ <prompt> · <when>` in its turn's colour; else gitsigns' text.
function M.format(username, info)
  local i = type(info) == 'table' and at(api.nvim_get_current_buf(), tonumber(info.final_lnum) or -1)
  if i then
    return chunks(i)
  end
  return M.git_default(username, info or {})
end

---------------------------------------------------------------------------
-- actions

-- Go to the start of the next/previous AI run (lines of one turn in a row).
function M.jump(dir)
  local buf = api.nvim_get_current_buf()
  if not (bufs[buf] and bufs[buf].cid) then
    util.notify('aic: no AI blame here')
    return
  end
  local starts, prev = {}, nil
  for l = 1, api.nvim_buf_line_count(buf) do
    local i = at(buf, l)
    local k = i and (i.key or ('c' .. i.cid))
    if k and k ~= prev then
      starts[#starts + 1] = l
    end
    prev = k
  end
  local cur = api.nvim_win_get_cursor(0)[1]
  for n = dir > 0 and 1 or #starts, dir > 0 and #starts or 1, dir > 0 and 1 or -1 do
    if (dir > 0 and starts[n] > cur) or (dir < 0 and starts[n] < cur) then
      api.nvim_win_set_cursor(0, { starts[n], 0 })
      return starts[n]
    end
  end
  util.notify('aic: no ' .. (dir > 0 and 'next' or 'previous') .. ' AI run')
end

local function here()
  local buf = api.nvim_get_current_buf()
  local l = api.nvim_win_get_cursor(0)[1]
  local i = at(buf, l)
  if not i then
    util.notify('aic: no AI wrote this line')
  end
  return i, bufs[buf], l
end

local function when(ts)
  ts = tonumber(ts)
  return ts and (os.date('%Y-%m-%d %H:%M', math.floor(ts)) .. ' (' .. ago(ts) .. ')') or '?'
end

-- The float's lines: the blame reply's change and turn, and the `detail`
-- reply `d` when the host gave one (the full prompt, the agent's comment, the hunks).
local function detail_lines(i, b, l, d)
  local c = d or i.change
  local prompt = util.split(vim.trim((d and d.prompt) or (i.turn and i.turn.prompt) or '(no turn)'))
  local lines = { '# ' .. (prompt[1] or '') }
  vim.list_extend(lines, vim.list_slice(prompt, 2))
  if d and d.prompt_truncated then
    table.insert(lines, '(truncated)')
  end
  vim.list_extend(lines, {
    '',
    '- tool: ' .. ((c.tool and c.tool ~= '') and c.tool or '?'),
    '- agent: ' .. (c.agent or '?'),
    '- changed: ' .. when(c.ts),
  })
  if i.turn then
    table.insert(lines, '- prompt sent: ' .. when(i.turn.ts))
  end
  table.insert(lines, ('- line %d%s · change %d at rev %s'):format(l,
    i.base ~= l and (' (line ' .. i.base .. ' as aigource read it)') or '', i.cid, tostring(b.rev)))
  if d and type(d.comment) == 'string' and vim.trim(d.comment) ~= '' then
    table.insert(lines, '')
    vim.list_extend(lines, util.split(vim.trim(d.comment)))
    if d.comment_truncated then
      table.insert(lines, '(truncated)')
    end
  end
  if d and type(d.hunks) == 'table' and #d.hunks > 0 then
    vim.list_extend(lines, { '', '```diff' })
    for _, h in ipairs(d.hunks) do
      table.insert(lines, ('@@ -%d,%d +%d,%d @@'):format(h.old_start or 0, h.old_lines or 0, h.new_start or 0, h.new_lines or 0))
      for _, t in ipairs(h.lines or {}) do
        vim.list_extend(lines, util.split(t))
      end
    end
    table.insert(lines, '```')
    if d.truncated then
      table.insert(lines, '(truncated)')
    end
  end
  return lines
end

-- Float: the line's turn and change, with aigource's `detail` when it has it.
-- A stale rev fetches the blame again and asks once more.
function M.detail(retried)
  local i, b, l = here()
  if not i then
    return
  end
  local buf = api.nvim_get_current_buf()
  local function show(d)
    local lines = detail_lines(i, b, l, d)
    util.float(lines, { title = ' aic blame ', filetype = 'markdown', height = #lines + 1 })
  end
  local ok = send(M.clients[b.root], { cmd = 'detail', file = b.rel, line = i.base, rev = b.rev }, function(r)
    if r.status == 'ok' then
      show(r)
    elseif r.status == 'stale' and not retried then
      fetch(buf, function()
        if api.nvim_get_current_buf() == buf then
          M.detail(true)
        end
      end)
    elseif r.status == 'none' then
      util.notify('aic: no AI wrote this line')
    else
      show() -- a host without `detail`, or stale twice: what the blame has
    end
  end, true)
  if not ok then
    show() -- what the blame has
  end
end

on_focus = function(cl, msg, r)
  if r.status == 'ok' and r.host == 'headless' and not msg.started then
    -- a GUI that hosts takes the socket over after its first build; it gets
    -- the focus again once it's up (refocus)
    msg.started, cl.refocus = true, msg
    if spawn({ '-a', '-r', cl.root, '--structure', 'icicle::' .. msg.file }, cl.root) then
      util.notify('aic: starting aigource; it shows the turn once its replay is in')
    end
  elseif r.status == 'building' then
    util.notify('aic: aigource is still building')
  elseif r.status ~= 'ok' then
    util.notify('aic: aigource: ' .. tostring(r.error or r.status), vim.log.levels.WARN)
  end
end

-- Show the line's turn and file in aigource's structure screen.
function M.show()
  local i, b = here()
  if not i then
    return
  end
  if not i.key then
    util.notify('aic: this change has no turn')
    return
  end
  local cl = M.clients[b.root]
  local msg = { cmd = 'focus', turn_key = i.key, file = b.rel, line = i.base or 0 }
  send(cl, msg, function(r)
    on_focus(cl, msg, r)
  end, true)
end

-- The quickfix list: every range of the line's turn, in every file (lines as
-- aigource read the files). Never opens a window.
function M.lines()
  local i, b = here()
  if not i then
    return
  end
  if not i.key then
    util.notify('aic: this change has no turn')
    return
  end
  local cl, prompt = M.clients[b.root], i.turn and i.turn.prompt or ''
  send(cl, { cmd = 'turn_lines', turn_key = i.key }, function(r)
    if r.status ~= 'ok' then
      util.notify('aic: aigource: ' .. tostring(r.error or r.status))
      return
    end
    local items = {}
    for _, f in ipairs(r.files or {}) do
      local l = 1
      for _, run in ipairs(f.runs or {}) do
        if run[2] >= 0 then
          table.insert(items, { filename = util.abs(cl.root, f.file), lnum = l, end_lnum = l + run[1] - 1, text = prompt })
        end
        l = l + run[1]
      end
    end
    vim.fn.setqflist({}, ' ', { title = 'aic turn: ' .. prompt, items = items })
    util.notify(('aic: %d ranges of this turn in the quickfix list'):format(#items))
  end, true)
end

---------------------------------------------------------------------------

local redraw_visible = util.debounce(100, function()
  for _, w in ipairs(api.nvim_tabpage_list_wins(0)) do
    local b = api.nvim_win_get_buf(w)
    if bufs[b] and api.nvim_buf_is_loaded(b) then
      draw(b)
    end
  end
end)

function M.setup()
  local g = api.nvim_create_augroup('aic_blame', { clear = true })
  local au = function(ev, fn)
    api.nvim_create_autocmd(ev, { group = g, callback = fn })
  end
  au('BufReadPost', function(a)
    M.attach(a.buf)
  end)
  au('BufWinEnter', function(a)
    if bufs[a.buf] then
      draw(a.buf)
    else
      M.attach(a.buf)
    end
  end)
  au({ 'BufWritePost', 'FileChangedShellPost' }, function(a)
    saved(a.buf)
    if bufs[a.buf] and bufs[a.buf].base_text then
      track(a.buf) -- a write may follow a `git add`
    end
  end)
  -- a dir that wasn't in a repo, a root without sessions: asked again (M.recheck)
  au('BufEnter', function(a)
    local b = bufs[a.buf]
    if not b then
      M.attach(a.buf)
    elseif not M.clients[b.root] then
      fetch(a.buf)
    end
  end)
  api.nvim_create_autocmd('User', {
    group = g,
    pattern = 'GitSignsUpdate',
    callback = function(a)
      local buf = type(a.data) == 'table' and a.data.buffer
      if buf and bufs[buf] and bufs[buf].untracked then
        track(buf) -- `git add`: gitsigns draws it now
      end
    end,
  })
  au({ 'BufUnload', 'BufWipeout' }, function(a)
    bufs[a.buf], dirty[a.buf] = nil, nil
  end)
  au('WinScrolled', redraw_visible)
  au({ 'CursorMoved', 'BufEnter', 'InsertLeave' }, function(a)
    if bufs[a.buf] and bufs[a.buf].untracked then
      cursor_text(a.buf)
    end
  end)
  au({ 'InsertEnter', 'BufLeave' }, function(a)
    if bufs[a.buf] then
      api.nvim_buf_clear_namespace(a.buf, M.line_ns, 0, -1)
    end
  end)
  for _, w in ipairs(api.nvim_list_wins()) do
    local b = api.nvim_win_get_buf(w)
    if not bufs[b] then
      M.attach(b)
    end
  end
end

return M
