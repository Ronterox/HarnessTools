#!/bin/sh
here=$(cd "$(dirname "$0")" && pwd)
. "$here/../lib/common.sh"
check() {
    [ -f "$here/../config.toml" ] || return 3
    log "config ok"
}
check || exit
mkdir -p "$here/../build"
if [ -z "${CC:-}" ]; then
    echo "no CC" >&2
    exit 2
fi
export BUILD_ID=42
"$CC" -o "$here/../build/app" missing.c > "$here/../build/out.txt" 2>&1
helper_not_here
