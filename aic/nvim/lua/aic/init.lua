-- aic: Neovim client for the "AI compiler". See README.md.
local backend = require('aic.backend')
local config = require('aic.config')
local state = require('aic.state')
local util = require('aic.util')

local api = vim.api
local M = {}
local configured = false

local function mod(name)
  return require('aic.' .. name)
end

local highlights = {
  AicUnseen = 'DiagnosticWarn',
  AicSeen = 'Comment',
  AicTurn = 'Title',
  AicPrompt = 'Normal',
  AicTime = 'Comment',
  AicTitle = 'Function',
  AicWhy = 'Comment',
  AicHunk = 'Identifier',
  AicWorker = 'DiagnosticInfo',
  AicHuman = 'DiagnosticHint',
  AicSubmit = 'Special',
  AicHold = 'WarningMsg',
  AicCompact = 'Comment',
  AicText = 'String',
  AicError = 'DiagnosticError',
  AicAnchor = 'Underlined',
  AicUsageOk = 'Comment',
  AicUsageWarn = 'WarningMsg',
  AicUsageHot = 'ErrorMsg',
}

local function define_highlights()
  for name, link in pairs(highlights) do
    api.nvim_set_hl(0, name, { link = link, default = true })
  end
end

local function wire()
  state.on('trace', function()
    mod('trace').render()
  end)
  state.on('touched', function()
    mod('signs').apply_all()
  end)
  state.on('diagnostics', function(name, d, initial)
    mod('doc').on_diagnostics(name, d, initial)
  end)
  state.on('chat', function()
    mod('chat').render()
  end)
  state.on('usage', function()
    mod('usage').on_usage()
  end)
  state.on('session', function()
    mod('usage').update()
    mod('view').on_session()
    mod('trace').render()
    mod('chat').render()
    mod('signs').apply_all()
  end)
  state.on('refresh', function()
    mod('status').update()
  end)
end

local function in_aic_project(path)
  return state.find_root(path)
end

-- Activate passively (no prompts, no `aic watch`) if `path` is in an aic project.
local function passive(path)
  if state.root then
    return
  end
  local root = in_aic_project(path)
  if root then
    state.activate(root)
  end
end

local function setup_autocmds()
  local g = api.nvim_create_augroup('aic', { clear = true })
  api.nvim_create_autocmd('ColorScheme', { group = g, callback = define_highlights })
  api.nvim_create_autocmd({ 'BufReadPost', 'BufNewFile' }, {
    group = g,
    callback = function(a)
      if vim.bo[a.buf].buftype ~= '' or a.file == '' then
        return
      end
      local name = api.nvim_buf_get_name(a.buf)
      if name:match('/%.aic/docs/[^/]+%.md$') then
        mod('doc').attach(a.buf)
      end
      mod('human').snapshot(a.buf, a.event == 'BufNewFile')
      if state.active() then
        mod('signs').apply(a.buf, true) -- (re)read: extmarks are gone
      else
        passive(vim.fs.dirname(name))
      end
    end,
  })
  api.nvim_create_autocmd('BufWritePre', {
    group = g,
    callback = function(a)
      mod('human').pre_write(a.buf)
    end,
  })
  api.nvim_create_autocmd('BufWritePost', {
    group = g,
    callback = function(a)
      mod('human').on_write(a.buf)
    end,
  })
  api.nvim_create_autocmd('BufWipeout', {
    group = g,
    callback = function(a)
      mod('human').forget(a.buf)
    end,
  })
  api.nvim_create_autocmd('VimLeavePre', {
    group = g,
    callback = function()
      backend.stop_watch()
    end,
  })
  api.nvim_create_autocmd('DirChanged', {
    group = g,
    callback = function()
      passive()
    end,
  })
end

local function setup_keymaps(prefix)
  local function map(modes, lhs, plug, desc)
    vim.keymap.set(modes, prefix .. lhs, plug, { remap = true, desc = 'aic: ' .. desc })
  end
  map('n', 'z', '<Plug>(aic-toggle)', 'toggle code / last aic view')
  map('n', 't', '<Plug>(aic-history)', 'history (trace)')
  map('n', 'q', '<Plug>(aic-questions)', 'questions')
  map('n', 'd', '<Plug>(aic-docs)', 'doc picker')
  map('n', 'D', '<Plug>(aic-doc-new)', 'new doc')
  map('n', 'p', '<Plug>(aic-prompts)', 'prompt templates')
  map('n', 'e', '<Plug>(aic-diagnostics)', 'compile diagnostics -> quickfix list')
  map({ 'n', 'x' }, 's', '<Plug>(aic-submit)', 'submit')
  map({ 'n', 'x' }, 'S', '<Plug>(aic-submit-now)', 'submit now')
  map('n', 'h', '<Plug>(aic-hold)', 'hold')
  map('n', 'c', '<Plug>(aic-continue)', 'continue')
  map('n', '?', '<Plug>(aic-ask)', 'ask at cursor')
  map('n', 'l', '<Plug>(aic-last-answer)', 'reopen last answer')
  map('n', 'w', '<Plug>(aic-whatif)', 'what-if')
  map('n', 'b', '<Plug>(aic-blame)', 'blame')
  map('n', 'a', '<Plug>(aic-attach)', 'attach worker')
  map('n', 'u', '<Plug>(aic-next-unseen)', 'next unseen')
  map('n', 'U', '<Plug>(aic-usage)', 'usage / compact')
  map('n', 'g', '<Plug>(aic-follow)', 'follow anchor under cursor')
  map('n', ']', '<Plug>(aic-next-touched)', 'next touched range')
  map('n', '[', '<Plug>(aic-prev-touched)', 'previous touched range')
end

function M.setup(opts)
  local o = config.set(opts)
  define_highlights()
  if not configured then
    wire()
    mod('usage').start()
  end
  configured = true
  setup_autocmds()
  mod('view').setup()
  if o.keymaps then
    setup_keymaps(o.prefix)
  end
  if vim.v.vim_did_enter == 1 then
    vim.schedule(passive)
  else
    api.nvim_create_autocmd('VimEnter', { group = 'aic', once = true, callback = function() passive() end })
  end
end

local function ensure_setup()
  if not configured then
    M.setup({})
  end
end

-- Run cb once aic is active. interactive=true offers `aic init` if needed.
local function ensure(cb, interactive)
  ensure_setup()
  if state.active() then
    return cb()
  end
  local name = api.nvim_buf_get_name(0)
  local from = (vim.bo.buftype == '' and name ~= '') and vim.fs.dirname(name) or vim.fn.getcwd()
  local root = state.root or state.find_root(from) or state.find_root()
  if root then
    return state.activate(root, function()
      if state.root then
        cb()
      end
    end)
  end
  if not interactive then
    util.notify('aic: no .aic/ found (run :Aic to initialize)', vim.log.levels.WARN)
    return
  end
  local target = state.fallback_root(from)
  vim.ui.select({ 'Yes', 'No' }, { prompt = 'aic: no .aic/ found. Run `aic init` in ' .. target .. '?' }, function(choice)
    if choice ~= 'Yes' then
      return
    end
    backend.run({ 'init' }, target, backend.report('init', function(ok)
      if ok then
        state.activate(target, cb)
      end
    end))
  end)
end

local function run(args, label)
  ensure(function()
    backend.run(args, state.root, backend.report(label, function()
      state.refresh()
    end))
  end)
end

---------------------------------------------------------------------------
-- public API

function M.open()
  ensure(function()
    backend.ensure_watch(state.root)
    mod('view').open()
    mod('usage').poll()
  end, true)
end

function M.toggle()
  ensure(function()
    backend.ensure_watch(state.root)
    mod('view').toggle()
    mod('usage').poll()
  end, true)
end

-- Show view `name` ('trace' | 'questions' | 'doc') in the current window.
function M.show(name)
  ensure(function()
    backend.ensure_watch(state.root)
    mod('view').show(name)
    mod('usage').poll()
  end, true)
end

-- Pick a prompt template, preview, :w sends. o = { range = n, line1 = a, line2 = b, now = bool }
function M.submit(o)
  o = o or {}
  ensure(function()
    mod('submit').start(o)
  end)
end

-- :AicPrompts: edit a template (built-ins are copied first) or make one.
function M.prompts()
  ensure(function()
    mod('submit').manage()
  end)
end

function M.hold()
  run({ 'hold' }, 'hold')
end

function M.continue(msg)
  local args = { 'continue' }
  if msg and vim.trim(msg) ~= '' then
    vim.list_extend(args, { '--', msg })
  end
  run(args, 'continue')
end

function M.compile()
  ensure(function()
    mod('doc').compile()
  end)
end

function M.ask()
  ensure(function()
    mod('ask').ask()
  end)
end

function M.whatif()
  ensure(function()
    mod('ask').whatif()
  end)
end

function M.chat(text)
  ensure(function()
    mod('chat').ask(text)
  end)
end

-- Docs: picker, show one (default: current), new one.
function M.docs()
  ensure(function()
    mod('doc').pick()
  end, true)
end

function M.doc(name)
  ensure(function()
    mod('doc').show(name and vim.trim(name) ~= '' and vim.trim(name) or nil)
  end, true)
end

function M.doc_new(name)
  ensure(function()
    mod('doc').new(name)
  end, true)
end

function M.blame()
  ensure(function()
    mod('signs').blame()
  end)
end

function M.next_touched(dir)
  ensure(function()
    mod('signs').jump(dir or 1)
  end)
end

-- Load the last compile into the quickfix list. Never opens a window:
-- :copen / ]q are yours.
function M.diagnostics()
  ensure(function()
    if mod('doc').load_qf() then
      util.notify(('aic: %d compile diagnostics in the quickfix list'):format(#vim.fn.getqflist()))
    else
      util.notify('aic: no compile diagnostics yet')
    end
  end)
end

function M.next_unseen(dir)
  ensure(function()
    backend.ensure_watch(state.root)
    mod('view').show('trace')
    mod('trace').next_unseen(dir or 1)
  end, true)
end

function M.refresh()
  ensure(function()
    state.resolve(function()
      state.refresh(true)
    end)
  end)
end

local function age(ts)
  ts = tonumber(ts)
  if not ts then
    return '?'
  end
  local d = os.time() - ts
  if d < 60 then
    return math.floor(d) .. 's'
  elseif d < 3600 then
    return math.floor(d / 60) .. 'm'
  elseif d < 86400 then
    return math.floor(d / 3600) .. 'h'
  end
  return math.floor(d / 86400) .. 'd'
end

function M.attach()
  ensure(function()
    backend.run({ 'sessions', '--json' }, state.root, function(ok, out, err)
      local list = ok and util.decode(out)
      if type(list) ~= 'table' then
        util.notify('aic sessions failed: ' .. vim.trim(err ~= '' and err or out), vim.log.levels.ERROR)
        return
      end
      local items = { { auto = true } }
      vim.list_extend(items, list)
      vim.ui.select(items, {
        prompt = 'aic: attach worker',
        format_item = function(it)
          if it.auto then
            return 'auto (most recent worker)'
          end
          local sid = tostring(it.session_id or '?')
          return ('%s %s  [%s]  %s ago  %s'):format(
            it.attached and '*' or ' ',
            (it.title and it.title ~= '') and it.title or sid:sub(1, 8),
            it.state or '?',
            age(it.updated),
            sid:sub(1, 8)
          )
        end,
      }, function(choice)
        if not choice then
          return
        end
        local args = choice.auto and { 'attach', '--auto' } or { 'attach', tostring(choice.session_id) }
        backend.run(args, state.root, backend.report('attach', function()
          state.resolve()
        end))
      end)
    end)
  end, true)
end

-- :AicUsage float (sessions, quota, last calls; compact/reset keys).
function M.usage()
  ensure(function()
    mod('usage').open()
  end)
end

-- `aic compact <which> [--reset]` (which = compile|ask|worker; reset: compile|ask).
function M.compact(which, reset)
  ensure(function()
    mod('usage').compact(which, reset)
  end)
end

-- Follow the anchor under the cursor (any buffer).
function M.follow()
  ensure_setup()
  mod('anchors').follow_cursor(false)
end

-- Statusline component: cached, never does IO.
function M.status()
  return mod('status').get()
end

return M
