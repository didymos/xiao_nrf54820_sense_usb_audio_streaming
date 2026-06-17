#!/usr/bin/env bash
# lib/selftest.sh — post-install self-test suite
# Source after lib/log.sh; call run_self_tests

SELFTEST_PASS=0
SELFTEST_FAIL=0
SELFTEST_WARN=0

_test_pass() {
    SELFTEST_PASS=$(( SELFTEST_PASS + 1 ))
    log_ok "  [PASS] $*"
}

_test_fail() {
    SELFTEST_FAIL=$(( SELFTEST_FAIL + 1 ))
    log_error "  [FAIL] $*"
}

_test_warn() {
    SELFTEST_WARN=$(( SELFTEST_WARN + 1 ))
    log_warn "  [WARN] $*"
}

_check() {
    # Usage: _check "description" command [args...]
    local desc="$1"; shift
    if "$@" &>/dev/null; then
        _test_pass "$desc"
        return 0
    else
        _test_fail "$desc"
        return 1
    fi
}

run_self_tests() {
    log_section "Self-Test Suite"
    SELFTEST_PASS=0; SELFTEST_FAIL=0; SELFTEST_WARN=0

    # --- Binary checks ---
    log_step "Checking required binaries"
    for bin in jackd jack_capture jack_lsp zita-a2j arecord aplay python3; do
        _check "$bin in PATH" command -v "$bin"
    done

    # --- Config file ---
    log_step "Checking config file"
    _check "/etc/fieldrec/fieldrec.conf exists" test -f /etc/fieldrec/fieldrec.conf

    if [[ -f /etc/fieldrec/fieldrec.conf ]]; then
        source /etc/fieldrec/fieldrec.conf 2>/dev/null || true

        _check "SAMPLE_RATE set" test -n "${SAMPLE_RATE:-}"
        _check "RECORDINGS_DIR set" test -n "${RECORDINGS_DIR:-}"
        _check "MIC_PORTS set" test -n "${MIC_PORTS:-}"
    fi

    # --- Directories ---
    log_step "Checking directories"
    _check "/opt/fieldrec exists" test -d /opt/fieldrec
    _check "/opt/fieldrec/tones exists" test -d /opt/fieldrec/tones

    if [[ -n "${RECORDINGS_DIR:-}" ]]; then
        _check "RECORDINGS_DIR exists" test -d "$RECORDINGS_DIR"
    fi

    # --- Tone files ---
    log_step "Checking tone files"
    for tone in go stop saved error; do
        _check "tone: ${tone}.wav" test -f "/opt/fieldrec/tones/${tone}.wav"
    done
    for i in $(seq 0 7); do
        _check "tone: countdown_${i}.wav" test -f "/opt/fieldrec/tones/countdown_${i}.wav"
    done

    # --- Python venv ---
    log_step "Checking Python venv"
    _check "/opt/fieldrec/.venv exists" test -d /opt/fieldrec/.venv
    _check "venv has uvicorn" test -x /opt/fieldrec/.venv/bin/uvicorn

    # --- RT limits ---
    log_step "Checking RT limits"
    _check "/etc/security/limits.d/95-audio.conf" test -f /etc/security/limits.d/95-audio.conf
    _check "audio group exists" getent group audio

    # --- Systemd services ---
    log_step "Checking systemd services"
    for svc in audio-sync fieldrec-web cpu-performance; do
        if systemctl list-unit-files "${svc}.service" 2>/dev/null | grep -q "${svc}.service"; then
            local state
            state="$(systemctl is-enabled "${svc}.service" 2>/dev/null || echo disabled)"
            if [[ "$state" = "enabled" ]]; then
                _test_pass "$svc.service enabled"
            else
                _test_warn "$svc.service not enabled (state: $state)"
            fi
        else
            _test_warn "$svc.service not found"
        fi
    done

    # --- JACK test (non-destructive) ---
    log_step "Checking JACK availability"
    if [[ -f /etc/fieldrec/fieldrec.conf ]]; then
        source /etc/fieldrec/fieldrec.conf 2>/dev/null || true
        if [[ -n "${JACK_MASTER_PORT:-}" ]]; then
            # Try to resolve MIC1 card
            local card1
            if card1="$(resolve_card "${JACK_MASTER_PORT}" 2>/dev/null)"; then
                _test_pass "MIC1 port ${JACK_MASTER_PORT} resolves to card $card1"
            else
                _test_warn "MIC1 port ${JACK_MASTER_PORT} not currently connected"
            fi
        fi
    fi

    # --- Network ---
    log_step "Checking network"
    if nmcli connection show "FieldRec-Hotspot" &>/dev/null; then
        _test_pass "FieldRec-Hotspot connection exists"
    else
        _test_warn "FieldRec-Hotspot connection not configured"
    fi

    # --- Sudoers ---
    log_step "Checking sudoers"
    if grep -r 'date' /etc/sudoers.d/ 2>/dev/null | grep -q NOPASSWD; then
        _test_pass "sudoers date rule present"
    else
        _test_warn "sudoers date rule missing"
    fi

    # --- CPU governor service ---
    log_step "Checking CPU governor"
    if [[ -f /etc/systemd/system/cpu-performance.service ]]; then
        _test_pass "cpu-performance.service installed"
    else
        _test_warn "cpu-performance.service not installed"
    fi

    # --- Summary ---
    echo ""
    log_section "Self-Test Summary"
    log_ok   "  PASSED:  $SELFTEST_PASS"
    if [[ "$SELFTEST_WARN" -gt 0 ]]; then
        log_warn "  WARNED:  $SELFTEST_WARN"
    fi
    if [[ "$SELFTEST_FAIL" -gt 0 ]]; then
        log_error "  FAILED:  $SELFTEST_FAIL"
        return 1
    fi
    return 0
}

# resolve_card is needed by the test; define fallback if audio.sh not sourced
if ! declare -f resolve_card &>/dev/null; then
    resolve_card() {
        local want="$1" c iface
        for c in /sys/class/sound/card[0-9]*; do
            [[ -d "$c" ]] || continue
            iface=$(basename "$(readlink -f "$c/device" 2>/dev/null)")
            [ "${iface%%:*}" = "$want" ] && { basename "$c" | tr -dc '0-9'; return 0; }
        done
        return 1
    }
fi
