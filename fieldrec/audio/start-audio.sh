#!/usr/bin/env bash
# audio/start-audio.sh — Start JACK (dummy) + zita-a2j bridges for all mics
#
# Architecture: jackd runs on the dummy driver (no capture device). Every mic
# is bridged via zita-a2j so all channels go through the same adaptive-
# resampling path and arrive with equal latency — zero inter-channel offset.
#
# Reads /etc/fieldrec/fieldrec.conf
# Runs as: TARGET_USER (via audio-sync.service)
set -euo pipefail

CONF="/etc/fieldrec/fieldrec.conf"

# ── Logging — journald captures stdout/stderr via audio-sync.service ──────────
log()  { printf '[%s] [INFO]  %s\n'  "$(date '+%H:%M:%S')" "$*"; }
ok()   { printf '[%s] [OK]    %s\n'  "$(date '+%H:%M:%S')" "$*"; }
warn() { printf '[%s] [WARN]  %s\n'  "$(date '+%H:%M:%S')" "$*" >&2; }
die()  { printf '[%s] [ERROR] %s\n'  "$(date '+%H:%M:%S')" "$*" >&2; exit 1; }

# ── Load config ───────────────────────────────────────────────────────────────
[[ -f "$CONF" ]] || die "Config file not found: $CONF"
# shellcheck source=/dev/null
source "$CONF"

SAMPLE_RATE="${SAMPLE_RATE:-16000}"
JACK_FRAMES="${JACK_FRAMES:-512}"
JACK_NPERIODS="${JACK_NPERIODS:-3}"
MIC_PORTS="${MIC_PORTS:-}"

# ── USB port → ALSA card resolver ────────────────────────────────────────────
resolve_card() {
    local want="$1" c iface
    for c in /sys/class/sound/card[0-9]*; do
        [[ -d "$c" ]] || continue
        iface=$(basename "$(readlink -f "$c/device" 2>/dev/null)")
        [ "${iface%%:*}" = "$want" ] && { basename "$c" | tr -dc '0-9'; return 0; }
    done
    return 1
}

[[ -n "$MIC_PORTS" ]] || die "MIC_PORTS not set in $CONF"
read -ra MIC_PORT_ARRAY <<< "$MIC_PORTS"

# ── Kill any existing JACK / bridges ─────────────────────────────────────────
log "Stopping any existing JACK server and zita bridges ..."
pkill -x jackd     2>/dev/null || true
pkill -x zita-a2j  2>/dev/null || true
sleep 0.5

# ── Start jackd on dummy driver ───────────────────────────────────────────────
# All mics go through zita-a2j so they share the same latency path.
log "Starting jackd (dummy driver, rate=${SAMPLE_RATE} frames=${JACK_FRAMES} periods=${JACK_NPERIODS}) ..."
jackd \
    -R -P70 \
    -d dummy \
    -r "$SAMPLE_RATE" \
    -p "$JACK_FRAMES" \
    -n "$JACK_NPERIODS" \
    2>&1 &
JACKD_PID=$!

# Wait for JACK to become ready
log "Waiting for JACK server ..."
for (( i=0; i<15; i++ )); do
    if jack_lsp &>/dev/null; then
        ok "JACK server is running (PID=$JACKD_PID)"
        break
    fi
    sleep 1
    if (( i == 14 )); then
        die "JACK server did not start within 15s"
    fi
done

# ── Bridge ALL mics via zita-a2j (mic1 … micN) ───────────────────────────────
mic_index=1
for port in "${MIC_PORT_ARRAY[@]}"; do
    bridge_name="mic${mic_index}"
    card_num="$(resolve_card "$port")" || {
        warn "Cannot resolve card for port=$port (${bridge_name}) — skipping"
        (( mic_index++ ))
        continue
    }

    log "Starting zita-a2j bridge '${bridge_name}' for port=${port} card=${card_num} ..."
    zita-a2j \
        -j "$bridge_name" \
        -d "hw:${card_num}" \
        -r "$SAMPLE_RATE" \
        -p "$JACK_FRAMES" \
        -c 1 \
        2>&1 &

    local_port="${bridge_name}:capture_1"
    log "  Waiting for JACK port: ${local_port} ..."
    found=0
    for (( j=0; j<10; j++ )); do
        sleep 1
        if jack_lsp 2>/dev/null | grep -qF "$local_port"; then
            ok "  Port ${local_port} registered in JACK"
            found=1
            break
        fi
    done
    if [[ "$found" -eq 0 ]]; then
        die "JACK port '${local_port}' did not appear after 10s — zita-a2j failed for ${bridge_name}"
    fi

    (( mic_index++ ))
done

ok "Audio stack ready — ${#MIC_PORT_ARRAY[@]} mic(s) via zita-a2j:"
ok "  Sample rate: ${SAMPLE_RATE} Hz | Frames: ${JACK_FRAMES} x ${JACK_NPERIODS}"
jack_lsp 2>/dev/null | grep 'capture' | while read -r line; do ok "  ${line}"; done

log "start-audio.sh complete — waiting on jackd (PID=${JACKD_PID})"
wait "$JACKD_PID" || true
log "jackd exited — audio service stopping"
