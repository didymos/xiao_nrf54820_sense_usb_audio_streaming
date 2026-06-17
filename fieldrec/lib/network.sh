#!/usr/bin/env bash
# lib/network.sh — Wi-Fi hotspot management via nmcli
# Source after lib/log.sh

# Create or ensure Wi-Fi hotspot is configured
setup_hotspot() {
    local ssid="${1:-FieldRec}"
    local pass="${2:-fieldrecpass}"
    local iface="${3:-}"   # empty = auto-detect

    log_step "Configuring Wi-Fi hotspot: SSID=$ssid"

    # Ensure NetworkManager is running
    if ! systemctl is-active --quiet NetworkManager; then
        log_info "Starting NetworkManager ..."
        systemctl enable --now NetworkManager >> "$LOGFILE" 2>&1 || die "Cannot start NetworkManager"
    fi

    # Auto-detect wireless interface if not supplied
    if [[ -z "$iface" ]]; then
        iface="$(nmcli -t -f DEVICE,TYPE device status 2>/dev/null \
                 | grep ':wifi$' | head -1 | cut -d: -f1)"
        if [[ -z "$iface" ]]; then
            log_warn "No Wi-Fi interface found — skipping hotspot setup"
            return 0
        fi
    fi
    log_info "Using Wi-Fi interface: $iface"

    local conn_name="FieldRec-Hotspot"

    # Remove existing connection if present
    if nmcli connection show "$conn_name" &>/dev/null; then
        log_info "Removing existing hotspot connection ..."
        nmcli connection delete "$conn_name" >> "$LOGFILE" 2>&1 || true
    fi

    # Create hotspot connection
    log_info "Creating hotspot connection ..."
    nmcli connection add \
        type wifi \
        ifname "$iface" \
        con-name "$conn_name" \
        autoconnect yes \
        ssid "$ssid" \
        >> "$LOGFILE" 2>&1 || die "nmcli connection add failed"

    nmcli connection modify "$conn_name" \
        802-11-wireless.mode ap \
        802-11-wireless.band bg \
        ipv4.method shared \
        wifi-sec.key-mgmt wpa-psk \
        wifi-sec.psk "$pass" \
        >> "$LOGFILE" 2>&1 || die "nmcli connection modify failed"

    log_info "Bringing up hotspot ..."
    nmcli connection up "$conn_name" >> "$LOGFILE" 2>&1 \
        && log_ok "Hotspot '$ssid' is up on $iface" \
        || log_warn "Failed to bring up hotspot (will try on next boot)"
}

# Bring hotspot up (idempotent)
start_hotspot() {
    local conn_name="FieldRec-Hotspot"
    if nmcli connection show "$conn_name" &>/dev/null; then
        nmcli connection up "$conn_name" 2>/dev/null || true
    fi
}

# Bring hotspot down
stop_hotspot() {
    local conn_name="FieldRec-Hotspot"
    if nmcli connection show --active "$conn_name" &>/dev/null; then
        nmcli connection down "$conn_name" 2>/dev/null || true
    fi
}
