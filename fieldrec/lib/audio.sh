#!/usr/bin/env bash
# lib/audio.sh — neutralize audio servers, RT tuning, output device detection
# Source after lib/log.sh

# Mask all PipeWire / PulseAudio / WirePlumber user services for TARGET_USER
neutralize_audio_servers() {
    local user="$1"
    log_step "Neutralizing PipeWire / PulseAudio / WirePlumber for user: $user"

    local services=(
        pipewire
        pipewire.socket
        wireplumber
        pulseaudio
        pulseaudio.socket
    )

    for svc in "${services[@]}"; do
        log_info "  Masking $svc ..."
        # systemctl --user requires XDG_RUNTIME_DIR; use machinectl or su trick
        if systemctl --user -M "${user}@.host" mask "$svc" >> "$LOGFILE" 2>&1 \
           || sudo -u "$user" \
                XDG_RUNTIME_DIR="/run/user/$(id -u "$user")" \
                DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u "$user")/bus" \
                systemctl --user mask "$svc" >> "$LOGFILE" 2>&1; then
            log_ok "  $svc masked"
        else
            log_warn "  Could not mask $svc (may not exist — OK)"
        fi

        # Stop if currently active
        sudo -u "$user" \
            XDG_RUNTIME_DIR="/run/user/$(id -u "$user")" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u "$user")/bus" \
            systemctl --user stop "$svc" >> "$LOGFILE" 2>&1 || true
    done

    log_ok "Audio server neutralization complete"
}

# Write RT scheduling limits config
setup_rt_limits() {
    log_step "Configuring RT scheduling limits"

    local limits_file="/etc/security/limits.d/95-audio.conf"
    cat > "$limits_file" <<'EOF'
# RT scheduling for audio — written by fieldrec installer
@audio   -  rtprio     95
@audio   -  memlock    unlimited
@audio   -  nice       -19
EOF
    log_ok "Written $limits_file"

    # Ensure audio group exists and TARGET_USER is a member
    if ! getent group audio &>/dev/null; then
        groupadd audio
        log_ok "Created group: audio"
    fi

    if [[ -n "${TARGET_USER:-}" ]] && ! id -nG "$TARGET_USER" | grep -qw audio; then
        usermod -aG audio "$TARGET_USER"
        log_ok "Added $TARGET_USER to audio group"
    fi

    # Enable rtkit if present
    if systemctl list-unit-files rtkit-daemon.service &>/dev/null; then
        systemctl enable --now rtkit-daemon.service >> "$LOGFILE" 2>&1 || true
        log_ok "rtkit-daemon enabled"
    fi
}

# Auto-detect sample rate supported by an ALSA capture device
detect_sample_rate() {
    local card_num="$1"   # numeric card number
    local preferred_rates=(48000 44100 96000 88200 32000 22050 16000)

    log_info "Probing hw:${card_num} for supported sample rates ..."

    local hw_params
    hw_params="$(arecord -D "hw:${card_num}" --dump-hw-params 2>&1 || true)"

    # Extract rate range from RATE line, e.g.:  RATE: [ 8000 96000 ]
    # or pick from a list like: RATE: 48000
    local detected=""
    for rate in "${preferred_rates[@]}"; do
        if echo "$hw_params" | grep -qE "RATE.*\b${rate}\b"; then
            detected="$rate"
            break
        fi
    done

    # Fallback: try arecord -D hw:<card> -r <rate> for short duration
    if [[ -z "$detected" ]]; then
        for rate in "${preferred_rates[@]}"; do
            if timeout 2 arecord -D "hw:${card_num}" -r "$rate" -c 1 -f S16_LE \
                    -d 0 /dev/null >> "$LOGFILE" 2>&1; then
                detected="$rate"
                break
            fi
        done
    fi

    if [[ -z "$detected" ]]; then
        detected="48000"
        log_warn "Could not detect sample rate for card ${card_num}; defaulting to $detected"
    else
        log_ok "Detected sample rate for card ${card_num}: $detected"
    fi

    echo "$detected"
}

# Detect a suitable ALSA output/playback device
detect_output_device() {
    log_step "Detecting output (playback) device"

    # Prefer a USB audio device that has playback
    local device=""

    # Try HDMI / analog / USB in order
    while read -r line; do
        local card
        card="$(echo "$line" | awk '{print $2}' | tr -d '[]')"
        if aplay -D "plughw:${card}" -d 0 -f S16_LE -r 48000 -c 2 /dev/zero \
                >> "$LOGFILE" 2>&1 & pid=$!; then
            sleep 0.2
            kill "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
            device="plughw:${card}"
            log_ok "Output device: $device"
            break
        fi
    done < <(aplay -l 2>/dev/null | grep '^card')

    if [[ -z "$device" ]]; then
        # Check for card named Speaker, Headphones, HDMI
        while read -r line; do
            local name
            name="$(echo "$line" | grep -oP '(?<=\[)[^\]]+' | head -1)"
            case "${name,,}" in
                *speaker*|*headphone*|*hdmi*|*output*)
                    local card
                    card="$(echo "$line" | awk '{print $2}' | tr -d '[]')"
                    device="plughw:CARD=${name}"
                    log_ok "Output device by name: $device"
                    break
                    ;;
            esac
        done < <(aplay -l 2>/dev/null | grep '^card')
    fi

    if [[ -z "$device" ]]; then
        device="plughw:0"
        log_warn "No output device detected; defaulting to $device"
    fi

    echo "$device"
}

# USB port path → ALSA card number resolver
resolve_card() {
    local want="$1"
    local c iface
    for c in /sys/class/sound/card[0-9]*; do
        iface=$(basename "$(readlink -f "$c/device")")
        [ "${iface%%:*}" = "$want" ] && { basename "$c" | tr -dc '0-9'; return 0; }
    done
    return 1
}
