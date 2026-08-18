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

# ── Step 5: Microphone detection ──────────────────────────────────────────────
log_section "Step 5: Microphone detection"
list_usb_audio_devices
# Mics are auto-detected at runtime by start-audio.sh — every USB audio capture
# device is bridged into JACK and named after its ALSA card id (Mic, Mic_1, …).
# No enrollment or MIC_PORTS config is needed; re-plugging into a different USB
# port no longer requires editing the config.
if [[ "$ENROLL" -eq 1 ]]; then
    log_info "Mic enrollment is no longer required — inputs are auto-detected at runtime."
fi
# Count capture-capable cards for the log only.
DETECTED_MICS=0
for c in /sys/class/sound/card[0-9]*; do
    [[ -d "$c" ]] || continue
    cn=$(basename "$c" | tr -dc '0-9')
    compgen -G "/sys/class/sound/pcmC${cn}D*c" >/dev/null 2>&1 && (( DETECTED_MICS++ )) || true
done
log_ok "Detected ${DETECTED_MICS} USB audio capture device(s) — all will be bridged at boot."

# ── Step 6: Sample rate ───────────────────────────────────────────────────────
log_section "Step 6: Sample rate"
# XIAO nRF52840 Sense mics support 16000 Hz only. jackd + all zita-a2j bridges
# run at this rate; bridges adaptively resample if a device differs.
SAMPLE_RATE="16000"
log_info "Sample rate: ${SAMPLE_RATE} Hz (XIAO nRF52840 Sense)"

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

# ── Step 9: Tones directory ───────────────────────────────────────────────────
log_section "Step 9: Preparing tones directory"
# Tones are now generated in Python at runtime (numpy sine waves piped to aplay).
# The tones/ directory is still copied to /opt/fieldrec/ for make_tones.py reference.
TONES_DIR="/opt/fieldrec/tones"
mkdir -p "$TONES_DIR"
log_ok "Tones dir ready (runtime generation — no WAV files needed)"

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
# Mics are auto-detected at runtime (named Mic, Mic_1, … after their ALSA card
# id). MIC_PORTS/JACK_PORTS are IGNORED at runtime — kept for reference only.
MIC_PORTS=""
JACK_PORTS=""
CHANNELS=4
SAMPLE_RATE=${SAMPLE_RATE}
JACK_FRAMES=512
JACK_NPERIODS=3
OUT_DEVICE=${OUT_DEVICE}
BIT_DEPTH=16
COUNTDOWN_BEEPS=0
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

# ── Step 14: Sudoers rules (set clock + power/restart from web UI) ────────────
log_section "Step 14: Sudoers rules"
SYSTEMCTL="$(command -v systemctl || echo /usr/bin/systemctl)"
SUDOERS_FILE="/etc/sudoers.d/fieldrec-date"
cat > "$SUDOERS_FILE" <<SUDEOF
# Allow fieldrec web service to set the system clock
${TARGET_USER} ALL=(root) NOPASSWD: /usr/bin/date
# Allow the web UI's System panel to reboot / shut down / restart services
${TARGET_USER} ALL=(root) NOPASSWD: ${SYSTEMCTL} reboot
${TARGET_USER} ALL=(root) NOPASSWD: ${SYSTEMCTL} poweroff
${TARGET_USER} ALL=(root) NOPASSWD: ${SYSTEMCTL} restart --no-block audio-sync fieldrec-web
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
log_info "Mics are auto-detected at boot — no enrollment needed. Just plug them in."
