#!/usr/bin/env bash
# Build 8 XIAO mic firmware images, each with a unique USB serial number
# (XIAO-MIC-001 … XIAO-MIC-008).
#
# The USB *product* string is kept constant on purpose: the Linux ALSA card id
# is derived from the product string, so keeping it "XIAO BLE Sense Mic" means
# the cards stay named Mic / Mic_1 / … and the FieldRec host-side auto-select
# (^Mic(_\d+)?$) and USB-socket channel ordering keep working unchanged. Only
# the *serial* varies — it shows up at /sys/bus/usb/devices/*/serial and gives
# each physical board a unique identity for optional serial-based mapping.
#
# Run from the firmware app directory (the one containing prj.conf), e.g.:
#   cd ~/workspace/app && ./build-all-uf2.sh
#
# Output: ./uf2-out/XIAO-MIC-00N.uf2
set -euo pipefail

BOARD="xiao_ble/nrf52840/sense"
PRODUCT="XIAO BLE Sense Mic"
OUT="$(pwd)/uf2-out"
mkdir -p "$OUT"

for n in 001 002 003 004 005 006 007 008; do
    sn="XIAO-MIC-$n"
    echo "======================================================================"
    echo "  Building $sn"
    echo "======================================================================"
    west build -p always -b "$BOARD" -- \
        -DCONFIG_USB_DEVICE_PRODUCT="\"$PRODUCT\"" \
        -DCONFIG_USB_DEVICE_SN="\"$sn\""
    cp build/zephyr/zephyr.uf2 "$OUT/$sn.uf2"
    echo "  -> $OUT/$sn.uf2"
done

echo
echo "All 8 images built in: $OUT"
ls -l "$OUT"
