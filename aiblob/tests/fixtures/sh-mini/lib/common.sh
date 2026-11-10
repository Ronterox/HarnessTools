# Shared helpers: sourced, never run.
log() {
    echo "[$(date +%T)] $*" >> run.log
}

die() {
    log "error: $*"
    exit 1
}

cleanup() {
    rm -f "$TMPDIR/run.lock"
}
