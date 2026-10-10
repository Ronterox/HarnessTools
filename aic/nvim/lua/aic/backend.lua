-- Running the `aic` CLI (one-shot commands and the `aic watch` job).
local config = require('aic.config')
local util = require('aic.util')

local M = {}
local uv = vim.uv or vim.loop

M.watch_jobs = {} -- project root -> vim.SystemObj of the `aic watch` we started there

local function cmd(args)
  local c = { config.options.bin }
  vim.list_extend(c, args)
  return c
end

-- Run `aic <args>` asynchronously in `cwd`.
-- cb(ok, stdout, stderr, code) is called on the main loop.
function M.run(args, cwd, cb)
  cb = cb or function() end
  local ok, obj = pcall(vim.system, cmd(args), { cwd = cwd, text = true }, function(res)
    vim.schedule(function()
      cb(res.code == 0, res.stdout or '', res.stderr or '', res.code)
    end)
  end)
  if not ok then
    vim.schedule(function()
      cb(false, '', tostring(obj), -1)
    end)
    return nil
  end
  return obj
end

-- Callback that reports a command's result via vim.notify.
function M.report(label, after)
  return function(ok, out, err, code)
    out, err = vim.trim(out or ''), vim.trim(err or '')
    if ok then
      local msg = out ~= '' and out or (label .. ': ok')
      if err ~= '' then
        msg = msg .. '\n' .. err
      end
      util.notify(msg)
    else
      util.notify(
        ('aic %s failed (%s): %s'):format(label, tostring(code), err ~= '' and err or out),
        vim.log.levels.ERROR
      )
    end
    if after then
      after(ok, out, err, code)
    end
  end
end

local function pid_alive(pid)
  if not pid then
    return false
  end
  local ok, r = pcall(uv.kill, pid, 0)
  return ok and r == 0
end

-- Start `aic watch` for `root` unless we already run one there or
-- .aic/watch.pid names a live process. One watcher per project root.
function M.ensure_watch(root)
  if not root then
    return
  end
  local job = M.watch_jobs[root]
  if job and not job:is_closing() then
    return
  end
  local pid = tonumber(vim.trim(util.read_file(root .. '/.aic/watch.pid') or ''))
  if pid_alive(pid) then
    return
  end
  local log
  local ok, obj
  ok, obj = pcall(vim.system, cmd({ 'watch' }), {
    cwd = root,
    stdin = true, -- `aic watch` exits when stdin closes
    text = true,
    stdout = false,
    stderr = function(_, data)
      if data and data:match('%S') then
        log = data
      end
    end,
  }, function(res)
    vim.schedule(function()
      if M.watch_jobs[root] == obj then
        M.watch_jobs[root] = nil
      end
      if not M.stopping and res.code ~= 0 and res.signal == 0 then
        util.notify(
          ('aic watch exited (%d): %s'):format(res.code, vim.trim(log or '')),
          vim.log.levels.WARN
        )
      end
    end)
  end)
  if ok then
    M.watch_jobs[root] = obj
  else
    util.notify('aic: cannot start `aic watch`: ' .. tostring(obj), vim.log.levels.ERROR)
  end
end

-- Stop every `aic watch` we started (VimLeavePre).
function M.stop_watch()
  M.stopping = true
  for root, job in pairs(M.watch_jobs) do
    pcall(job.write, job, nil) -- close stdin
    pcall(job.kill, job, 'sigterm')
    M.watch_jobs[root] = nil
  end
end

return M
