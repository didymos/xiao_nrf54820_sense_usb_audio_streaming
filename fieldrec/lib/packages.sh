#!/usr/bin/env bash
# lib/packages.sh — per-package apt installation + jack_capture apt-or-source build
# Source after lib/log.sh

# Install a single apt package. Returns 0 on success, 1 on failure (non-fatal by design).
install_pkg() {
    local pkg="$1"
    if dpkg -s "$pkg" &>/dev/null; then
        log_info "  $pkg — already installed"
        return 0
    fi
    log_info "  Installing $pkg ..."
    if DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$pkg" \
            >> "$LOGFILE" 2>&1; then
        log_ok "  $pkg installed"
        return 0
    else
        log_warn "  $pkg — apt install failed (skipping)"
        return 1
    fi
}

# Install all required packages one at a time
install_all_packages() {
    log_step "Installing required packages (one at a time)"

    local pkgs=(
        # Core audio
        jackd2
        libjack-jackd2-dev
        zita-ajbridge
        alsa-utils
        # Build tools (needed for jack_capture source build)
        build-essential
        pkg-config
        libsndfile1-dev
        git
        # Python
        python3
        python3-pip
        python3-venv
        # System utilities
        curl
        wget
        lsof
        pciutils
        usbutils
        nvme-cli
        smartmontools
        # Network
        network-manager
        # Scheduling
        rtkit
    )

    local failed=()
    for pkg in "${pkgs[@]}"; do
        install_pkg "$pkg" || failed+=("$pkg")
    done

    if [[ ${#failed[@]} -gt 0 ]]; then
        log_warn "The following packages could not be installed: ${failed[*]}"
        log_warn "Continuing — some features may be unavailable"
    fi
}

# Try apt first; fall back to building from source.
install_jack_capture() {
    log_step "Installing jack_capture"

    if command -v jack_capture &>/dev/null; then
        log_ok "jack_capture already in PATH: $(command -v jack_capture)"
        return 0
    fi

    # --- Attempt 1: apt ---
    log_info "Trying: apt install jack-capture ..."
    if DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends jack-capture \
            >> "$LOGFILE" 2>&1; then
        if command -v jack_capture &>/dev/null; then
            log_ok "jack_capture installed from apt"
            return 0
        fi
    fi
    log_warn "apt install jack-capture failed or binary missing — building from source"

    # --- Ensure build deps ---
    for dep in build-essential pkg-config libjack-jackd2-dev libsndfile1-dev git; do
        install_pkg "$dep" || true
    done

    # --- Attempt 2: source build ---
    local build_dir="/tmp/jack_capture_build"
    rm -rf "$build_dir"
    log_info "Cloning jack_capture source ..."
    if ! git clone https://github.com/kmatheussen/jack_capture "$build_dir" >> "$LOGFILE" 2>&1; then
        log_error "Failed to clone jack_capture repository"
        return 1
    fi

    log_info "Building jack_capture ..."
    if ! (cd "$build_dir" && make >> "$LOGFILE" 2>&1); then
        log_error "jack_capture make failed"
        return 1
    fi

    log_info "Installing jack_capture ..."
    if ! (cd "$build_dir" && sudo make install >> "$LOGFILE" 2>&1); then
        log_error "jack_capture sudo make install failed"
        return 1
    fi

    rm -rf "$build_dir"

    if command -v jack_capture &>/dev/null; then
        log_ok "jack_capture installed from source: $(command -v jack_capture)"
        return 0
    else
        log_error "jack_capture not found in PATH after source build"
        return 1
    fi
}

# Install Python venv with app dependencies
install_python_venv() {
    local app_dir="$1"          # e.g. /opt/fieldrec/app
    local venv_dir="$2"         # e.g. /opt/fieldrec/.venv
    local requirements="$3"     # path to requirements.txt

    log_step "Creating Python venv at $venv_dir"
    python3 -m venv "$venv_dir" >> "$LOGFILE" 2>&1 || die "Failed to create venv"

    log_info "Installing Python dependencies ..."
    "$venv_dir/bin/pip" install --upgrade pip >> "$LOGFILE" 2>&1 || true
    "$venv_dir/bin/pip" install -r "$requirements" >> "$LOGFILE" 2>&1 \
        || die "pip install failed — check $LOGFILE"
    log_ok "Python venv ready"
}
