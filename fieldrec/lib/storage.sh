#!/usr/bin/env bash
# lib/storage.sh — NVMe detection, mount management, recordings directory setup
# Source after lib/log.sh

# Detect NVMe device (e.g., /dev/nvme0n1)
detect_nvme() {
    log_step "Detecting NVMe storage"
    local dev=""

    # Check nvme list first
    if command -v nvme &>/dev/null; then
        dev="$(nvme list 2>/dev/null | awk '/^\/dev\/nvme/ {print $1; exit}')"
    fi

    # Fallback: scan /sys/class/nvme
    if [[ -z "$dev" ]]; then
        for d in /sys/class/nvme/nvme*/nvme*n1; do
            [[ -e "$d" ]] && dev="/dev/$(basename "$d")" && break
        done
    fi

    if [[ -n "$dev" ]]; then
        log_ok "NVMe device found: $dev"
    else
        log_warn "No NVMe device detected"
    fi
    echo "$dev"
}

# Ensure NVMe is partitioned and mounted at /mnt/ssd
setup_nvme_mount() {
    local dev="$1"        # e.g. /dev/nvme0n1
    local mountpoint="${2:-/mnt/ssd}"

    log_step "Setting up NVMe mount at $mountpoint"

    if [[ -z "$dev" ]] || [[ ! -b "$dev" ]]; then
        log_warn "NVMe device '$dev' not available — skipping NVMe mount setup"
        return 1
    fi

    mkdir -p "$mountpoint"

    # Check if already mounted
    if mountpoint -q "$mountpoint"; then
        log_ok "$mountpoint is already mounted"
        return 0
    fi

    # Determine partition (prefer p1 suffix for NVMe)
    local part="${dev}p1"
    if [[ ! -b "$part" ]]; then
        # Try without p suffix
        part="${dev}1"
    fi

    # If no partition exists, create one
    if [[ ! -b "$part" ]]; then
        log_info "Creating partition on $dev ..."
        parted -s "$dev" mklabel gpt mkpart primary ext4 0% 100% >> "$LOGFILE" 2>&1 \
            || die "Failed to partition $dev"
        # Re-read partition table
        partprobe "$dev" >> "$LOGFILE" 2>&1 || true
        sleep 1
        part="${dev}p1"
        [[ ! -b "$part" ]] && part="${dev}1"
    fi

    # Format if not formatted
    if ! blkid "$part" | grep -q ext4; then
        log_info "Formatting $part as ext4 ..."
        mkfs.ext4 -F -L ssd_recordings "$part" >> "$LOGFILE" 2>&1 \
            || die "mkfs.ext4 failed on $part"
    fi

    # Add to /etc/fstab if not already present
    local uuid
    uuid="$(blkid -s UUID -o value "$part")"
    if [[ -n "$uuid" ]] && ! grep -q "UUID=$uuid" /etc/fstab; then
        echo "UUID=$uuid  $mountpoint  ext4  defaults,noatime  0  2" >> /etc/fstab
        log_ok "Added $part (UUID=$uuid) to /etc/fstab"
    fi

    mount "$part" "$mountpoint" >> "$LOGFILE" 2>&1 \
        && log_ok "Mounted $part at $mountpoint" \
        || log_warn "Mount failed — will retry on next boot"
}

# Create recordings directory and set ownership
setup_recordings_dir() {
    local dir="$1"        # e.g. /mnt/ssd/recordings
    local owner="$2"      # e.g. rec

    log_step "Setting up recordings directory: $dir"

    mkdir -p "$dir" || die "Cannot create $dir"
    chown "${owner}:${owner}" "$dir" || log_warn "Cannot chown $dir to $owner"
    chmod 755 "$dir"
    log_ok "Recordings directory ready: $dir"
}

# Check available disk space (MB) on a path
check_disk_space() {
    local path="$1"
    local min_mb="${2:-500}"

    local avail_mb
    avail_mb="$(df -BM "$path" 2>/dev/null | awk 'NR==2 {gsub(/M/,""); print $4}')"
    avail_mb="${avail_mb:-0}"

    if (( avail_mb < min_mb )); then
        log_warn "Low disk space on $path: ${avail_mb}MB available (minimum: ${min_mb}MB)"
        return 1
    fi
    log_info "Disk space OK: ${avail_mb}MB available on $path"
    return 0
}
