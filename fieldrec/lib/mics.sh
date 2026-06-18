#!/usr/bin/env bash
# lib/mics.sh — USB mic enrollment and sample rate detection
# Source after lib/log.sh and lib/audio.sh (for resolve_card, detect_sample_rate)

# USB port path → ALSA card number resolver
# Matches /sys/class/sound/cardN/device iface path prefix against USB port token
resolve_card() {
    local want="$1"
    local c iface
    for c in /sys/class/sound/card[0-9]*; do
        [[ -d "$c" ]] || continue
        iface=$(basename "$(readlink -f "$c/device" 2>/dev/null)")
        [ "${iface%%:*}" = "$want" ] && { basename "$c" | tr -dc '0-9'; return 0; }
    done
    return 1
}

# List all currently connected USB audio capture devices with their port paths
list_usb_audio_devices() {
    log_info "USB audio capture devices:"
    local c iface card_num
    local found=0
    for c in /sys/class/sound/card[0-9]*; do
        [[ -d "$c" ]] || continue
        card_num="$(basename "$c" | tr -dc '0-9')"
        iface="$(basename "$(readlink -f "$c/device" 2>/dev/null)")"
        # Check if it has capture capability
        if arecord -l 2>/dev/null | grep -q "card $card_num"; then
            local port="${iface%%:*}"
            local name
            name="$(cat "$c/id" 2>/dev/null || echo 'unknown')"
            log_info "  Card $card_num | USB port: $port | Name: $name"
            found=1
        fi
    done
    if [[ "$found" -eq 0 ]]; then
        log_warn "  No USB audio capture devices found"
    fi
}

# Interactive enrollment: user plugs in mics one at a time
# Writes MIC_PORTS array to stdout in space-separated form
enroll_mics_interactive() {
    local num_mics="${1:-4}"
    local enrolled=()

    echo ""
    echo "=== Microphone Enrollment ==="
    echo "You will be prompted to plug in each microphone one at a time."
    echo "This identifies mics by USB port path (not card number)."
    echo ""

    for (( i=1; i<=num_mics; i++ )); do
        echo -n "Unplug MIC${i} (if connected), then press Enter ..."
        read -r

        # Snapshot current devices
        local before=()
        for c in /sys/class/sound/card[0-9]*; do
            [[ -d "$c" ]] || continue
            before+=( "$(basename "$(readlink -f "$c/device" 2>/dev/null)")" )
        done

        echo -n "Now plug in MIC${i} and press Enter ..."
        read -r
        sleep 1

        # Find new device
        local found_port=""
        for c in /sys/class/sound/card[0-9]*; do
            [[ -d "$c" ]] || continue
            local iface
            iface="$(basename "$(readlink -f "$c/device" 2>/dev/null)")"
            local port="${iface%%:*}"
            local is_new=1
            for prev in "${before[@]}"; do
                [[ "$prev" = "$iface" ]] && { is_new=0; break; }
            done
            if [[ "$is_new" -eq 1 ]]; then
                # Verify it's a capture device
                local card_num
                card_num="$(basename "$c" | tr -dc '0-9')"
                if arecord -l 2>/dev/null | grep -q "card $card_num"; then
                    found_port="$port"
                    local name
                    name="$(cat "$c/id" 2>/dev/null || echo 'unknown')"
                    echo "  ✓ MIC${i} detected: USB port=$port card=$card_num name='$name'"
                    break
                fi
            fi
        done

        if [[ -z "$found_port" ]]; then
            echo "  WARNING: No new USB audio device detected for MIC${i}"
            echo -n "  Enter USB port path manually (e.g. 1-1.2), or leave blank to skip: "
            read -r found_port
        fi

        enrolled+=( "$found_port" )
    done

    # Output space-separated list
    echo "${enrolled[*]}"
}

# Non-interactive enrollment: scan and assign ports in detection order
enroll_mics_auto() {
    local num_mics="${1:-4}"
    local ports=()

    log_info "Auto-detecting USB microphones (taking first $num_mics capture devices) ..."

    for c in /sys/class/sound/card[0-9]*; do
        [[ -d "$c" ]] || continue
        local card_num
        card_num="$(basename "$c" | tr -dc '0-9')"
        local iface
        iface="$(basename "$(readlink -f "$c/device" 2>/dev/null)")"
        local port="${iface%%:*}"

        if arecord -l 2>/dev/null | grep -q "card $card_num"; then
            local name
            name="$(cat "$c/id" 2>/dev/null || echo 'unknown')"
            log_info "  Found: card $card_num port=$port name='$name'"
            ports+=( "$port" )
            if [[ ${#ports[@]} -ge "$num_mics" ]]; then
                break
            fi
        fi
    done

    if [[ ${#ports[@]} -eq 0 ]]; then
        log_warn "No USB audio capture devices found"
    fi

    echo "${ports[*]}"
}

# Detect sample rate for a given USB port token
detect_sample_rate_for_port() {
    local port="$1"
    local card_num

    card_num="$(resolve_card "$port")" || {
        log_warn "Cannot resolve card for port $port — defaulting to 48000"
        echo "48000"
        return 0
    }

    local preferred_rates=(48000 44100 96000 88200 32000 22050 16000 8000)
    local hw_params
    hw_params="$(arecord -D "hw:${card_num}" --dump-hw-params 2>&1 || true)"

    local detected=""
    for rate in "${preferred_rates[@]}"; do
        if echo "$hw_params" | grep -qE "RATE.*\b${rate}\b"; then
            detected="$rate"
            break
        fi
    done

    if [[ -z "$detected" ]]; then
        # Fallback: try probing with arecord
        for rate in "${preferred_rates[@]}"; do
            if timeout 2 arecord -D "hw:${card_num}" -r "$rate" -c 1 -f S16_LE \
                    -d 0 /dev/null 2>/dev/null; then
                detected="$rate"
                break
            fi
        done
    fi

    if [[ -z "$detected" ]]; then
        detected="48000"
        log_warn "Could not detect sample rate for port $port/card $card_num; using $detected"
    else
        log_ok "Sample rate for port $port/card $card_num: $detected"
    fi

    echo "$detected"
}

# Validate that all enrolled mic ports can be resolved to ALSA cards
validate_mic_ports() {
    local ports_str="$1"   # space-separated port tokens
    local -a ports
    read -ra ports <<< "$ports_str"

    local all_ok=1
    for port in "${ports[@]}"; do
        if resolve_card "$port" &>/dev/null; then
            log_ok "  Port $port — found ALSA card $(resolve_card "$port")"
        else
            log_warn "  Port $port — NOT found (mic may not be plugged in)"
            all_ok=0
        fi
    done
    return $((1 - all_ok))
}
