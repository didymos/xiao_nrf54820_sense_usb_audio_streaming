#!/usr/bin/env bash
# audio/start-audio.sh — Start JACK (dummy) + zita-a2j bridges for all mics
#
# Architecture: jackd runs on the dummy driver (no capture device). Every
# USB audio capture device is auto-detected and bridged via zita-a2j so all
# channels go through the same adaptive-resampling path and arrive with equal
# latency — zero inter-channel offset.
#
# Each JACK client is named after its ALSA card id (e.g. "Mic", "Mic_1"),
# which is stable regardless of which physical USB port the mic is in. No
# MIC_PORTS configuration is needed — the web UI lists every detected input
# and pre-selects the ones named like mics.
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

# ── Sanitize a string into a valid JACK client name ──────────────────────────
sanitize_name() { printf '%s' "$1" | tr -c 'A-Za-z0-9_' '_'; }

# ── Discover all USB audio capture devices ───────────────────────────────────
# Emits one "cardnum<TAB>cardid" line per capture-capable card, sorted by id
# so channel order is stable (Mic, Mic_1, Mic_2, …).
discover_capture_cards() {
    local c cardnum cardid
    for c in /sys/class/sound/card[0-9]*; do
        [[ -d "$c" ]] || continue
        cardnum=$(basename "$c" | tr -dc '0-9')
        # A capture PCM shows up as /sys/class/sound/pcmC<num>D*c
        compgen -G "/sys/class/sound/pcmC${cardnum}D*c" >/dev/null 2>&1 || continue
        cardid=$(cat "$c/id" 2>/dev/null || echo "card${cardnum}")
        printf '%s\t%s\n' "$cardnum" "$cardid"
    done | sort -t $'\t' -k2,2 -V
}

# ── Kill any existing JACK / bridges ─────────────────────────────────────────
log "Stopping any existing JACK server and zita bridges ..."
pkill -x jackd     2>/dev/null || true
pkill -x zita-a2j  2>/dev/null || true
sleep 0.5

# ── Start jackd on dummy driver ───────────────────────────────────────────────
# All mics go through zita-a2j so they share the same latency path.
log "Starting jackd (dummy driver, rate=${SAMPLE_RATE} frames=${JACK_FRAMES}) ..."
# NOTE: the dummy driver has no -n/nperiods option (only -C -P -r -m -p -w).
jackd \
    -R -P70 \
    -d dummy \
    -r "$SAMPLE_RATE" \
    -p "$JACK_FRAMES" \
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

# ── Auto-detect and bridge every capture device via zita-a2j ─────────────────
mapfile -t CAPTURE_CARDS < <(discover_capture_cards)
if [[ "${#CAPTURE_CARDS[@]}" -eq 0 ]]; then
    warn "No USB audio capture devices detected — JACK is up with no inputs."
    warn "Plug in mics and 'sudo systemctl restart audio-sync'."
fi

bridged=0
for entry in "${CAPTURE_CARDS[@]}"; do
    card_num="${entry%%$'\t'*}"
    card_id="${entry#*$'\t'}"
    bridge_name="$(sanitize_name "$card_id")"

    log "Starting zita-a2j bridge '${bridge_name}' for card=${card_num} (id=${card_id}) ..."
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
        # One bad device shouldn't kill the whole stack — warn and continue.
        warn "  JACK port '${local_port}' did not appear after 10s — skipping card ${card_num}"
        continue
    fi
    (( bridged++ ))
done

ok "Audio stack ready — ${bridged} input(s) bridged via zita-a2j:"
ok "  Sample rate: ${SAMPLE_RATE} Hz | Frames: ${JACK_FRAMES} x ${JACK_NPERIODS}"
jack_lsp 2>/dev/null | grep 'capture' | while read -r line; do ok "  ${line}"; done

log "start-audio.sh complete — waiting on jackd (PID=${JACKD_PID})"
wait "$JACKD_PID" || true
log "jackd exited — audio service stopping"
