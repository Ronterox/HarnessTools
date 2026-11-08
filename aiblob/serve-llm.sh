#!/usr/bin/env bash
# Start llama-server for label.py (RTX 2060 6GB). Foreground; Ctrl-C stops it.
#   ./serve-llm.sh [--runtime b11960] [MODEL.gguf [PORT [extra llama-server flags...]]]
# Defaults: Qwen3.5-4B-Q4_K_M, port 8080, the system llama-server (MODEL / PORT / LLAMA_RUNTIME
# env vars also work). --runtime b11960 uses the newer build in ~/.cache/remcli-runtimes
# (needed for LFM3, whose architecture the system build 8770 does not know). That build runs
# through a private glibc loader and finds no CUDA device unless cwd is its bin dir, so we cd.
# label.py sends one request at a time (siblings' titles feed each prompt); -np 4 / -c 8192 give
# 2048-token slots, so keep the shared prefix small. --reasoning off: no <think>.
# Qwen3.5 is a hybrid (recurrent) model: llama-server logs "cache_reuse is not supported" and
# ignores --cache-reuse; prefix reuse happens through slot LCP matching + context checkpoints.
set -euo pipefail
RUNTIME="${LLAMA_RUNTIME:-}"
if [ "${1:-}" = "--runtime" ]; then RUNTIME="${2:?--runtime needs a name}"; shift 2; fi
MODEL="${1:-${MODEL:-$HOME/.cache/models/Qwen3.5-4B-Q4_K_M.gguf}}"
PORT="${2:-${PORT:-8080}}"
if [ $# -gt 2 ]; then shift 2; else shift $#; fi
FLAGS=(-ngl 99 -c 8192 -np 4 --cache-reuse 256 -fa on --reasoning off)

if [ -z "$RUNTIME" ]; then
  exec llama-server -m "$MODEL" --host 127.0.0.1 --port "$PORT" "${FLAGS[@]}" "$@"
fi
R="$HOME/.cache/remcli-runtimes"
LIBC="$R/llama-b${RUNTIME#b}/compat-libc/usr/lib/x86_64-linux-gnu"
BIN="$R/llama-b${RUNTIME#b}/bin/llama-b${RUNTIME#b}"
CUDA="$R/prism-b12128-d7b2896/cuda12-libs/nvidia"
MODEL="$(realpath "$MODEL")"
cd "$BIN"
exec "$LIBC/ld-linux-x86-64.so.2" \
  --library-path "$LIBC:$PWD:$CUDA/cuda_runtime/lib:$CUDA/cublas/lib" \
  ./llama-server -m "$MODEL" --host 127.0.0.1 --port "$PORT" "${FLAGS[@]}" "$@"
