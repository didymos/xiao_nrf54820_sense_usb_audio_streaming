#!/usr/bin/env bash
# install.sh — FieldRec idempotent installer
# Usage: sudo ./install.sh [--enroll]
#
# Flags:
#   --enroll   Interactive USB mic enrollment (plug in one at a time)
#
# Requirements: Raspberry Pi OS (Bookworm/Bullseye), internet access for first run.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOGFILE=/var/log/fieldrec-install.log
source "$SCRIPT_DIR/lib/log.sh"
source "$SCRIPT_DIR/lib/packages.sh"
source "$SCRIPT_DIR/lib/audio.sh"
source "$SCRIPT_DIR/lib/network.sh"
source "$SCRIPT_DIR/lib/storage.sh"
source "$SCRIPT_DIR/lib/mics.sh"
source "$SCRIPT_DIR/lib/selftest.sh"

# ── Parse flags ───────────────────────────────────────────────────────────────
ENROLL=0
for arg in "$@"; do
    case "$arg" in
        --enroll) ENROLL=1 ;;
        --help|-h)
            echo "Usage: sudo $0 [--enroll]"
            echo "  --enroll  Interactive USB mic enrollment"
            exit 0
            ;;
    esac
done

# ── Step 0: Pre-flight ─────────────────────────────────────────────────────────
log_section "Step 0: Pre-flight checks"

[[ $EUID -eq 0 ]] || die "Must run as root: sudo $0"

# Detect target user (never hardcode)
TARGET_USER="${SUDO_USER:-$(logname 2>/dev/null || echo '')}"
if [[ -z "$TARGET_USER" ]] || [[ "$TARGET_USER" = "root" ]]; then
    log_warn "Cannot detect non-root SUDO_USER; attempting to detect from active sessions"
    TARGET_USER="$(who | awk '{print $1}' | grep -v root | head -1 || true)"
    [[ -n "$TARGET_USER" ]] || die "Could not determine target user. Run: sudo -E SUDO_USER=<username> $0"
fi
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
[[ -n "$TARGET_HOME" ]] || die "Could not determine home for user $TARGET_USER"
log_ok "Target user: $TARGET_USER ($TARGET_HOME)"
export TARGET_USER TARGET_HOME

# Log init
log_init

# OS check
if [[ -f /etc/os-release ]]; then
    source /etc/os-release
    log_info "OS: ${PRETTY_NAME:-unknown}"
    if ! echo "${ID:-} ${ID_LIKE:-}" | grep -qi debian; then
        log_warn "This installer is tested on Debian/Raspberry Pi OS. Proceeding anyway."
    fi
fi

log_ok "Pre-flight passed"

# ── Step 1: Packages ──────────────────────────────────────────────────────────
log_section "Step 1: Installing packages"
log_info "Updating apt cache ..."
apt-get update -qq >> "$LOGFILE" 2>&1 || log_warn "apt update returned non-zero (continuing)"
install_all_packages
install_jack_capture

# ── Step 2: Neutralize audio servers ─────────────────────────────────────────
log_section "Step 2: Neutralizing PipeWire / PulseAudio"
neutralize_audio_servers "$TARGET_USER"

# ── Step 3: RT scheduling ─────────────────────────────────────────────────────
log_section "Step 3: Real-time scheduling"
setup_rt_limits

# ── Step 4: CPU governor service ─────────────────────────────────────────────
log_section "Step 4: CPU performance governor (sysfs oneshot)"
install -m 644 "$SCRIPT_DIR/systemd/cpu-performance.service" \
    /etc/systemd/system/cpu-performance.service
systemctl daemon-reload
systemctl enable cpu-performance.service >> "$LOGFILE" 2>&1
systemctl start  cpu-performance.service >> "$LOGFILE" 2>&1 || log_warn "cpu-performance.service start failed (non-fatal)"

# Verify governor
for gov in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    [[ -f "$gov" ]] && cur="$(cat "$gov")" && break || cur="unknown"
done
log_info "CPU governor: ${cur:-unknown}"

# ── Step 5: Mic enrollment ────────────────────────────────────────────────────
log_section "Step 5: Microphone enrollment"
list_usb_audio_devices

CHANNELS=4

if [[ "$ENROLL" -eq 1 ]]; then
    log_info "Running interactive mic enrollment ..."
    MIC_PORTS_STR="$(enroll_mics_interactive "$CHANNELS")"
else
    log_info "Auto-detecting mics (use --enroll for interactive) ..."
    MIC_PORTS_STR="$(enroll_mics_auto "$CHANNELS")"
fi

read -ra MIC_PORT_ARRAY <<< "$MIC_PORTS_STR"
if [[ ${#MIC_PORT_ARRAY[@]} -eq 0 ]]; then
    log_warn "No mics detected; using placeholder ports"
    MIC_PORT_ARRAY=(1-1.1 1-1.2 1-1.3 1-1.4)
fi
CHANNELS="${#MIC_PORT_ARRAY[@]}"
JACK_MASTER_PORT="${MIC_PORT_ARRAY[0]}"

log_ok "Enrolled ${#MIC_PORT_ARRAY[@]} mic(s): ${MIC_PORT_ARRAY[*]}"

# Build JACK_PORTS string: first is system:capture_1, rest are mic2:capture_1 etc
JACK_PORTS="system:capture_1"
for (( i=1; i<${#MIC_PORT_ARRAY[@]}; i++ )); do
    JACK_PORTS+=" mic$(( i+1 )):capture_1"
done
log_info "JACK_PORTS: $JACK_PORTS"

# ── Step 6: Sample rate detection ─────────────────────────────────────────────
log_section "Step 6: Sample rate detection"
SAMPLE_RATE="48000"

if resolve_card "$JACK_MASTER_PORT" &>/dev/null; then
    SAMPLE_RATE="$(detect_sample_rate_for_port "$JACK_MASTER_PORT")"
else
    log_warn "MIC1 port $JACK_MASTER_PORT not connected — using default $SAMPLE_RATE Hz"
fi

# ── Step 7: Storage setup ─────────────────────────────────────────────────────
log_section "Step 7: Storage"
NVME_DEV="$(detect_nvme)"
MOUNTPOINT="/mnt/ssd"
RECORDINGS_DIR="${MOUNTPOINT}/recordings"

if [[ -n "$NVME_DEV" ]]; then
    setup_nvme_mount "$NVME_DEV" "$MOUNTPOINT" || {
        log_warn "NVMe mount failed — using local recordings dir"
        RECORDINGS_DIR="/home/${TARGET_USER}/recordings"
    }
else
    log_warn "No NVMe found — recordings will go to /home/${TARGET_USER}/recordings"
    RECORDINGS_DIR="/home/${TARGET_USER}/recordings"
fi
setup_recordings_dir "$RECORDINGS_DIR" "$TARGET_USER"

# ── Step 8: Output device detection ──────────────────────────────────────────
log_section "Step 8: Output device detection"
OUT_DEVICE="$(detect_output_device)"

# ── Step 9: Generate tones ────────────────────────────────────────────────────
log_section "Step 9: Generating tone files"
TONES_DIR="/opt/fieldrec/tones"
mkdir -p "$TONES_DIR"

if python3 -c "import numpy, soundfile" &>/dev/null; then
    python3 "$SCRIPT_DIR/tones/make_tones.py" "$TONES_DIR" \
        && log_ok "Tones generated in $TONES_DIR" \
        || log_warn "Tone generation failed — trying pip install first"
fi

if ! ls "$TONES_DIR"/go.wav &>/dev/null; then
    log_info "Installing tone deps with pip ..."
    pip3 install -q numpy soundfile >> "$LOGFILE" 2>&1 || true
    python3 "$SCRIPT_DIR/tones/make_tones.py" "$TONES_DIR" >> "$LOGFILE" 2>&1 \
        || log_warn "Tone generation failed — tones dir may be incomplete"
fi

# ── Step 10: Write /etc/fieldrec/fieldrec.conf ───────────────────────────────
log_section "Step 10: Writing configuration"
mkdir -p /etc/fieldrec

# Back up existing config
if [[ -f /etc/fieldrec/fieldrec.conf ]]; then
    cp /etc/fieldrec/fieldrec.conf "/etc/fieldrec/fieldrec.conf.bak.$(date +%Y%m%dT%H%M%S)"
    log_info "Backed up existing config"
fi

cat > /etc/fieldrec/fieldrec.conf <<CONFEOF
# FieldRec configuration — written by install.sh on $(date)
TARGET_USER=${TARGET_USER}
TARGET_HOME=${TARGET_HOME}
RECORDINGS_DIR=${RECORDINGS_DIR}
TONES_DIR=${TONES_DIR}
JACK_MASTER_PORT=${JACK_MASTER_PORT}
MIC_PORTS="${MIC_PORT_ARRAY[*]}"
JACK_PORTS="${JACK_PORTS}"
CHANNELS=${CHANNELS}
SAMPLE_RATE=${SAMPLE_RATE}
JACK_FRAMES=512
JACK_NPERIODS=3
OUT_DEVICE=${OUT_DEVICE}
BIT_DEPTH=16
COUNTDOWN_BEEPS=3
MIN_FREE_MB=500
HTTP_HOST=0.0.0.0
HTTP_PORT=8080
HOTSPOT_SSID=FieldRec
HOTSPOT_PASS=fieldrecpass
CONFEOF

chmod 644 /etc/fieldrec/fieldrec.conf
chown root:root /etc/fieldrec/fieldrec.conf
log_ok "Config written to /etc/fieldrec/fieldrec.conf"

# ── Step 11: Deploy application files ────────────────────────────────────────
log_section "Step 11: Deploying application"
OPT_DIR="/opt/fieldrec"
mkdir -p "$OPT_DIR"

# Copy files
cp -r "$SCRIPT_DIR/app"   "$OPT_DIR/"
cp -r "$SCRIPT_DIR/tones" "$OPT_DIR/"
cp -r "$SCRIPT_DIR/audio" "$OPT_DIR/"
chmod +x "$OPT_DIR/audio/start-audio.sh"

# Tones were already generated above; no need to regenerate
# Fix ownership
chown -R "$TARGET_USER":"$TARGET_USER" "$OPT_DIR"
chmod -R u+rX,go+rX "$OPT_DIR"

# Python venv
install_python_venv "$OPT_DIR/app" "$OPT_DIR/.venv" "$OPT_DIR/app/requirements.txt"
chown -R "$TARGET_USER":"$TARGET_USER" "$OPT_DIR/.venv"

# ── Step 12: Install audio-sync.service ──────────────────────────────────────
log_section "Step 12: Installing audio-sync.service"
sed \
    -e "s|\$TARGET_USER|${TARGET_USER}|g" \
    -e "s|\$TARGET_HOME|${TARGET_HOME}|g" \
    "$SCRIPT_DIR/systemd/audio-sync.service.tmpl" \
    > /etc/systemd/system/audio-sync.service
log_ok "audio-sync.service installed"

# ── Step 13: Install fieldrec-web.service ────────────────────────────────────
log_section "Step 13: Installing fieldrec-web.service"
sed \
    -e "s|\$TARGET_USER|${TARGET_USER}|g" \
    -e "s|\$TARGET_HOME|${TARGET_HOME}|g" \
    "$SCRIPT_DIR/systemd/fieldrec-web.service.tmpl" \
    > /etc/systemd/system/fieldrec-web.service
log_ok "fieldrec-web.service installed"

# ── Step 14: Sudoers rule for date ───────────────────────────────────────────
log_section "Step 14: Sudoers rule for date command"
SUDOERS_FILE="/etc/sudoers.d/fieldrec-date"
cat > "$SUDOERS_FILE" <<SUDEOF
# Allow fieldrec web service to set system clock
${TARGET_USER} ALL=(root) NOPASSWD: /usr/bin/date
SUDEOF
chmod 440 "$SUDOERS_FILE"
visudo -cf "$SUDOERS_FILE" && log_ok "Sudoers rule valid: $SUDOERS_FILE" \
    || { rm -f "$SUDOERS_FILE"; log_warn "Sudoers rule invalid — removed"; }

# ── Step 15: Wi-Fi hotspot ────────────────────────────────────────────────────
log_section "Step 15: Wi-Fi hotspot"
source /etc/fieldrec/fieldrec.conf 2>/dev/null || true
setup_hotspot "${HOTSPOT_SSID:-FieldRec}" "${HOTSPOT_PASS:-fieldrecpass}" || true

# ── Step 16: Enable and start services ───────────────────────────────────────
log_section "Step 16: Enabling systemd services"
systemctl daemon-reload

for svc in cpu-performance audio-sync fieldrec-web; do
    log_info "Enabling $svc ..."
    systemctl enable "${svc}.service" >> "$LOGFILE" 2>&1 && log_ok "  $svc enabled"
done

log_info "Starting services ..."
systemctl restart audio-sync.service   >> "$LOGFILE" 2>&1 \
    && log_ok "  audio-sync started" \
    || log_warn "  audio-sync failed to start (mics may not be connected)"

# Wait a moment for JACK to settle, then start web
sleep 2
systemctl restart fieldrec-web.service >> "$LOGFILE" 2>&1 \
    && log_ok "  fieldrec-web started" \
    || log_warn "  fieldrec-web failed to start (check: journalctl -u fieldrec-web)"

# ── Step 17: Self-test ────────────────────────────────────────────────────────
log_section "Step 17: Self-test"
run_self_tests || log_warn "Some self-tests failed — see output above"

# ── Step 18: Summary ──────────────────────────────────────────────────────────
log_section "Installation Complete"
log_ok "FieldRec installed successfully"
echo ""
log_info "Configuration:  /etc/fieldrec/fieldrec.conf"
log_info "Application:    /opt/fieldrec/"
log_info "Recordings:     ${RECORDINGS_DIR}"
log_info "Tones:          ${TONES_DIR}"
log_info "Web interface:  http://$(hostname -I | awk '{print $1}'):${HTTP_PORT:-8080}/"
log_info "Hotspot:        SSID=${HOTSPOT_SSID:-FieldRec} PASS=${HOTSPOT_PASS:-fieldrecpass}"
echo ""
log_info "Logs:"
log_info "  Install log:  $LOGFILE"
log_info "  Audio:        journalctl -u audio-sync -f"
log_info "  Web:          journalctl -u fieldrec-web -f"
echo ""
log_info "To re-enroll microphones: sudo $0 --enroll"
