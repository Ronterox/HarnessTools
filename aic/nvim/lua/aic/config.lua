-- User options.
local M = {}

-- <aic>/nvim/lua/aic/config.lua -> <aic>/nvim
local nvim_dir = vim.fn.fnamemodify(debug.getinfo(1, 'S').source:sub(2), ':p:h:h:h')

M.defaults = {
  -- path to the aic executable (default: <aic>/bin/aic next to the plugin dir)
  bin = vim.fn.fnamemodify(nvim_dir, ':h') .. '/bin/aic',
  -- prefix for the default keymaps
  prefix = '<leader>z',
  -- install default keymaps under `prefix`
  keymaps = true,
  -- usage winbar on the aic views (history, questions, docs)
  winbar = true,
  -- new docs compile when saved: 'ask' (asked on :AicDocNew), true or false
  new_doc_compile = 'ask',
  -- debounce for file-watch refreshes (ms)
  debounce = 150,
  -- fallback polling interval when fs_event is unavailable (ms)
  poll_interval = 500,
}

M.options = vim.deepcopy(M.defaults)

function M.set(opts)
  M.options = vim.tbl_deep_extend('force', vim.deepcopy(M.defaults), opts or {})
  return M.options
end

return M
