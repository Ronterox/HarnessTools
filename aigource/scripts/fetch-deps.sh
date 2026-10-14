#!/bin/sh
# Fetch pinned third-party sources into vendor/ (gitignored), verified by sha256.
set -eu
cd "$(dirname "$0")/.."
mkdir -p vendor
fetch() { # url file sha256 — one retry when a cached or fresh download is bad
    for attempt in 1 2; do
        if [ ! -f "vendor/$2" ]; then
            curl -fsSL -o "vendor/$2.part" "$1"
            mv "vendor/$2.part" "vendor/$2"
        fi
        if echo "$3  vendor/$2" | sha256sum -c --quiet - 2>/dev/null; then return 0; fi
        rm -f "vendor/$2"
    done
    echo "checksum mismatch: $2" >&2
    exit 1
}
fetch https://github.com/raysan5/raylib/releases/download/5.5/raylib-5.5_linux_amd64.tar.gz \
      raylib.tar.gz 3d95ef03d5b38dfa55c0a16ca122d382134b078f0e5b270b52fe7eae0549c000
fetch https://github.com/ibireme/yyjson/archive/refs/tags/0.10.0.tar.gz \
      yyjson.tar.gz 0d901cb2c45c5586e3f3a4245e58c2252d6b24bf4b402723f6179523d389b165
tar xzf vendor/raylib.tar.gz -C vendor
tar xzf vendor/yyjson.tar.gz -C vendor
touch vendor/raylib-5.5_linux_amd64/lib/libraylib.a vendor/yyjson-0.10.0/src/yyjson.c
