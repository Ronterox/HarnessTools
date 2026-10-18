-- Cached statusline string (no IO in get()).
local state = require('aic.state')

local M = { cache = '' }

function M.update()
  local s = ''
  if state.root then
    local parts = { 'aic' }
    if state.dir then
      local unseen = state.unseen_count()
      if unseen > 0 then
        table.insert(parts, unseen .. '●')
      end
      local d = state.diagnostics[require('aic.doc').current()] -- the doc being worked on
      if type(d) == 'table' then
        if d.error then
          table.insert(parts, 'E!')
        else
          local e = 0
          for _, it in ipairs(d.items or {}) do
            if it.severity == 'error' then
              e = e + 1
            end
          end
          if e > 0 then
            table.insert(parts, e .. 'E')
          end
          -- code changed since that compile: the diagnostics may no longer hold
          local upto = tonumber(d.upto_seq) or 0
          for i = #state.events, 1, -1 do
            local ev = state.events[i]
            if (tonumber(ev.seq) or 0) <= upto then
              break
            end
            if (ev.kind == 'tool' and ev.hunks and #ev.hunks > 0) or ev.kind == 'human' then
              table.insert(parts, 'stale')
              break
            end
          end
        end
      end
      local w = state.status and type(state.status.worker) == 'table' and state.status.worker or {}
      if w.state == 'held' then
        table.insert(parts, 'held')
      elseif w.state == 'asking' then
        table.insert(parts, 'asks!') -- a permission dialog is waiting in the worker's pane
      elseif state.hold then
        table.insert(parts, 'hold…') -- armed: stops at the next tool call
      elseif w.state == 'busy' then
        table.insert(parts, '⚙')
      end
      if state.compile_queued then
        table.insert(parts, 'compiling+queued')
      elseif state.compiling or state.local_compiling then
        table.insert(parts, 'compiling')
      end
    end
    if not state.dir or (state.status and state.status.worker == nil) then
      table.insert(parts, 'no-worker')
    end
    s = table.concat(parts, ' ')
  end
  if s ~= M.cache then
    M.cache = s
    pcall(vim.cmd, 'redrawstatus')
  end
end

function M.get()
  return M.cache
end

return M
