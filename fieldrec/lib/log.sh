#!/usr/bin/env bash
# lib/log.sh — colored logging helpers
# Source this file; do not execute directly.

# Color codes
_RED='\033[0;31m'
_GREEN='\033[0;32m'
_YELLOW='\033[1;33m'
_BLUE='\033[0;34m'
_CYAN='\033[0;36m'
_BOLD='\033[1m'
_NC='\033[0m'  # No Color

# LOGFILE may be set by the caller before sourcing; default to stderr only
LOGFILE="${LOGFILE:-}"

_log_raw() {
    local level="$1"; shift
    local msg="$*"
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    # Always write to stderr — keeps stdout clean for $() capture in install.sh
    printf "%s\n" "$msg" >&2
    # Write plain text to logfile if set
    if [[ -n "$LOGFILE" ]]; then
        local plain
        plain="$(printf "%s" "$msg" | sed 's/\x1b\[[0-9;]*m//g')"
        printf "[%s] [%s] %s\n" "$ts" "$level" "$plain" >> "$LOGFILE" 2>/dev/null || true
    fi
}

log_info() {
    _log_raw "INFO" "${_BLUE}[INFO]${_NC}  $*"
}

log_ok() {
    _log_raw "OK" "${_GREEN}[OK]${_NC}    $*"
}

log_warn() {
    _log_raw "WARN" "${_YELLOW}[WARN]${_NC}  $*" >&2
}

log_error() {
    _log_raw "ERROR" "${_RED}[ERROR]${_NC} $*" >&2
}

log_step() {
    _log_raw "STEP" "${_CYAN}${_BOLD}==> $*${_NC}"
}

log_section() {
    local bar="=================================================="
    _log_raw "SECTION" ""
    _log_raw "SECTION" "${_BOLD}${_CYAN}${bar}${_NC}"
    _log_raw "SECTION" "${_BOLD}${_CYAN}  $*${_NC}"
    _log_raw "SECTION" "${_BOLD}${_CYAN}${bar}${_NC}"
}

die() {
    log_error "$*"
    exit 1
}

# Ensure logfile directory exists and is writable
log_init() {
    if [[ -n "$LOGFILE" ]]; then
        local logdir
        logdir="$(dirname "$LOGFILE")"
        mkdir -p "$logdir" 2>/dev/null || true
        touch "$LOGFILE" 2>/dev/null || { LOGFILE=""; log_warn "Cannot write to $LOGFILE — logging to console only"; }
    fi
}
