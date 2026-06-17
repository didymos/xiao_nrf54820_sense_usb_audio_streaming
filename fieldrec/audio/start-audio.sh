#!/usr/bin/env bash
# audio/start-audio.sh — Start JACK + zita-a2j bridges
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

SAMPLE_RATE="${SAMPLE_RATE:-48000}"
JACK_FRAMES="${JACK_FRAMES:-512}"
JACK_NPERIODS="${JACK_NPERIODS:-3}"
JACK_MASTER_PORT="${JACK_MASTER_PORT:-}"
MIC_PORTS="${MIC_PORTS:-}"
CHANNELS="${CHANNELS:-1}"

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

# ── Validate master mic ───────────────────────────────────────────────────────
[[ -n "$JACK_MASTER_PORT" ]] || die "JACK_MASTER_PORT not set in $CONF"

MIC1_CARD="$(resolve_card "$JACK_MASTER_PORT")" \
    || die "Cannot find ALSA card for JACK_MASTER_PORT=$JACK_MASTER_PORT"
log "MIC1: USB port $JACK_MASTER_PORT -> ALSA card $MIC1_CARD"

# ── Kill any existing JACK / bridges ─────────────────────────────────────────
log "Stopping any existing JACK server and zita bridges ..."
pkill -x jackd     2>/dev/null || true
pkill -x zita-a2j  2>/dev/null || true
sleep 0.5

# ── Start jackd ───────────────────────────────────────────────────────────────
log "Starting jackd on hw:${MIC1_CARD} (rate=${SAMPLE_RATE} frames=${JACK_FRAMES} periods=${JACK_NPERIODS}) ..."
jackd \
    -R -P70 \
    -d alsa \
    -d "hw:${MIC1_CARD}" \
    -r "$SAMPLE_RATE" \
    -p "$JACK_FRAMES" \
    -n "$JACK_NPERIODS" \
    -i 1 \
    -C \
    2>&1 &
JACKD_PID=$!

# Wait for JACK to become ready
log "Waiting for JACK server ..."
local_timeout=15
for (( i=0; i<local_timeout; i++ )); do
    if jack_lsp &>/dev/null; then
        ok "JACK server is running (PID=$JACKD_PID)"
        break
    fi
    sleep 1
    if (( i == local_timeout - 1 )); then
        die "JACK server did not start within ${local_timeout}s"
    fi
done

# ── Bridge additional mics via zita-a2j ──────────────────────────────────────
read -ra MIC_PORT_ARRAY <<< "$MIC_PORTS"

# MIC_PORT_ARRAY[0] is the master (already in JACK as system:capture_1)
# MIC_PORT_ARRAY[1..] need zita-a2j bridges
mic_index=2
for port in "${MIC_PORT_ARRAY[@]:1}"; do
    bridge_name="mic${mic_index}"
    card_num="$(resolve_card "$port")" || {
        warn "Cannot resolve card for port=$port (MIC${mic_index}) — skipping bridge"
        (( mic_index++ ))
        continue
    }

    log "Starting zita-a2j bridge '$bridge_name' for port=$port card=$card_num ..."
    zita-a2j \
        -j "$bridge_name" \
        -d "hw:${card_num}" \
        -r "$SAMPLE_RATE" \
        -p "$JACK_FRAMES" \
        2>&1 &

    # Poll jack_lsp for the bridge port
    local_port="${bridge_name}:capture_1"
    log "  Waiting for JACK port: $local_port ..."
    found=0
    for (( j=0; j<10; j++ )); do
        sleep 1
        if jack_lsp 2>/dev/null | grep -qF "$local_port"; then
            ok "  Port $local_port is registered in JACK"
            found=1
            break
        fi
    done
    if [[ "$found" -eq 0 ]]; then
        die "JACK port '$local_port' did not appear after 10s — zita-a2j bridge failed for MIC${mic_index}"
    fi

    (( mic_index++ ))
done

ok "Audio stack ready:"
ok "  JACK PID:    $JACKD_PID"
ok "  Sample rate: $SAMPLE_RATE Hz"
ok "  Frames:      $JACK_FRAMES x $JACK_NPERIODS periods"
ok "  Mics bridged: $(( mic_index - 2 )) additional"

jack_lsp 2>/dev/null | sed 's/^/  PORT: /' | while read -r line; do
    ok "$line"
done

log "Audio start-audio.sh complete — JACK running in background"

# Keep the service alive by waiting on jackd
wait "$JACKD_PID" || true
log "jackd exited — audio service stopping"
