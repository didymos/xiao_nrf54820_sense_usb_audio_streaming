#!/usr/bin/env bash
# uninstall.sh — FieldRec clean removal
# Usage: sudo ./uninstall.sh [--purge]
#
# Flags:
#   --purge   Also remove recordings directory and /etc/fieldrec (destructive!)
#
# This script:
#   1. Stops and disables systemd services
#   2. Removes /etc/systemd/system/{audio-sync,fieldrec-web,cpu-performance}.service
#   3. Removes /opt/fieldrec/
#   4. Removes /etc/security/limits.d/95-audio.conf
#   5. Removes /etc/sudoers.d/fieldrec-date
#   6. Removes Wi-Fi hotspot connection
#   7. Optionally removes /etc/fieldrec/ and RECORDINGS_DIR (--purge only)
#   8. Does NOT remove apt packages (jackd2, etc.) — they may be used by other software

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Minimal logging (log.sh may be gone if run after partial install)
info()  { printf '\033[0;34m[INFO]  %s\033[0m\n'  "$*"; }
ok()    { printf '\033[0;32m[OK]    %s\033[0m\n'  "$*"; }
warn()  { printf '\033[1;33m[WARN]  %s\033[0m\n'  "$*" >&2; }
die()   { printf '\033[0;31m[ERROR] %s\033[0m\n'  "$*" >&2; exit 1; }
step()  { echo ""; printf '\033[1;36m==> %s\033[0m\n' "$*"; }

# ── Parse flags ───────────────────────────────────────────────────────────────
PURGE=0
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        --help|-h)
            echo "Usage: sudo $0 [--purge]"
            echo "  --purge  Also delete recordings and /etc/fieldrec (irreversible!)"
            exit 0
            ;;
    esac
done

[[ $EUID -eq 0 ]] || die "Must run as root: sudo $0"

if [[ "$PURGE" -eq 1 ]]; then
    echo ""
    warn "WARNING: --purge will delete /etc/fieldrec/ and RECORDINGS_DIR"
    warn "         This is IRREVERSIBLE."
    echo -n "Type 'yes' to confirm: "
    read -r confirm
    [[ "$confirm" = "yes" ]] || { info "Aborted."; exit 0; }
fi

# ── 1. Stop + disable services ────────────────────────────────────────────────
step "Stopping and disabling services"
for svc in fieldrec-web audio-sync cpu-performance; do
    if systemctl is-active --quiet "${svc}.service" 2>/dev/null; then
        systemctl stop "${svc}.service" 2>/dev/null && ok "  Stopped $svc" || warn "  Could not stop $svc"
    fi
    if systemctl is-enabled --quiet "${svc}.service" 2>/dev/null; then
        systemctl disable "${svc}.service" 2>/dev/null && ok "  Disabled $svc" || true
    fi
done

# ── 2. Remove service files ───────────────────────────────────────────────────
step "Removing systemd service files"
for f in audio-sync fieldrec-web cpu-performance; do
    target="/etc/systemd/system/${f}.service"
    if [[ -f "$target" ]]; then
        rm -f "$target"
        ok "  Removed $target"
    fi
done
systemctl daemon-reload
ok "systemd daemon reloaded"

# ── 3. Remove /opt/fieldrec ───────────────────────────────────────────────────
step "Removing /opt/fieldrec"
if [[ -d /opt/fieldrec ]]; then
    rm -rf /opt/fieldrec
    ok "Removed /opt/fieldrec"
else
    info "/opt/fieldrec not found (already removed?)"
fi

# ── 4. Remove RT limits config ────────────────────────────────────────────────
step "Removing RT limits config"
if [[ -f /etc/security/limits.d/95-audio.conf ]]; then
    rm -f /etc/security/limits.d/95-audio.conf
    ok "Removed /etc/security/limits.d/95-audio.conf"
fi

# ── 5. Remove sudoers rule ────────────────────────────────────────────────────
step "Removing sudoers rule"
if [[ -f /etc/sudoers.d/fieldrec-date ]]; then
    rm -f /etc/sudoers.d/fieldrec-date
    ok "Removed /etc/sudoers.d/fieldrec-date"
fi

# ── 6. Remove Wi-Fi hotspot ───────────────────────────────────────────────────
step "Removing Wi-Fi hotspot"
if command -v nmcli &>/dev/null && nmcli connection show "FieldRec-Hotspot" &>/dev/null; then
    nmcli connection down   "FieldRec-Hotspot" 2>/dev/null || true
    nmcli connection delete "FieldRec-Hotspot" 2>/dev/null && ok "Hotspot connection removed" \
        || warn "Could not remove hotspot connection"
else
    info "Hotspot connection not found (already removed?)"
fi

# ── 7. Restore CPU governor (ondemand) ───────────────────────────────────────
step "Restoring CPU governor to ondemand"
for gov in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    [[ -w "$gov" ]] && echo ondemand > "$gov" 2>/dev/null || true
done
ok "CPU governor restored"

# ── 8. Optional: purge config + recordings ───────────────────────────────────
if [[ "$PURGE" -eq 1 ]]; then
    step "PURGE: removing /etc/fieldrec"
    if [[ -f /etc/fieldrec/fieldrec.conf ]]; then
        # Read recordings dir before removing config
        RECORDINGS_DIR="$(grep '^RECORDINGS_DIR=' /etc/fieldrec/fieldrec.conf \
            | cut -d= -f2 | tr -d '"' || echo '')"
    fi
    rm -rf /etc/fieldrec
    ok "Removed /etc/fieldrec"

    if [[ -n "${RECORDINGS_DIR:-}" ]] && [[ -d "$RECORDINGS_DIR" ]]; then
        step "PURGE: removing recordings dir: $RECORDINGS_DIR"
        rm -rf "$RECORDINGS_DIR"
        ok "Removed $RECORDINGS_DIR"
    fi
else
    info "Config at /etc/fieldrec/ preserved (use --purge to delete)"
    info "Recordings preserved (use --purge to delete)"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
ok "FieldRec uninstalled successfully"
if [[ "$PURGE" -eq 0 ]]; then
    info "Config and recordings were NOT deleted."
    info "Run with --purge to remove everything."
fi
echo ""
info "Note: apt packages (jackd2, zita-ajbridge, etc.) were not removed."
info "To remove them: sudo apt remove jackd2 zita-ajbridge alsa-utils"
