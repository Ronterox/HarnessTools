#!/bin/sh
cd "$(dirname "$0")/.."
expect() {
    "$@" || { echo "FAIL $*"; exit 1; }
}
expect ./run.sh build
expect bash bin/deploy
