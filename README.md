# XIAO nRF52840 Sense — USB Microphone Firmware

Streams the onboard **MSM261D3526H1CPM** MEMS microphone over USB as a
standard **USB Audio Class 1** device. Plug it in; the OS enumerates it
as a microphone with no drivers required. Works with Audacity, OBS, REAPER,
and any UAC1-capable application on Windows / macOS / Linux.

## Hardware

| Item | Detail |
|------|--------|
| Board | Seeed Studio XIAO nRF52840 Sense |
| MCU | nRF52840 — ARM Cortex-M4F @ 64 MHz, 256 KB RAM, 1 MB flash |
| Microphone | MSM261D3526H1CPM — PDM, mono, −26 dBFS sensitivity |
| PDM CLK | P1.00 |
| PDM DIN | P0.16 |
| Mic power | P0.09 (active-high) |
| USB | Full-Speed 12 Mbps (nRF52840 built-in USBD) |

## Audio Specifications

| Parameter | Value |
|-----------|-------|
| Sample rate | **48 000 Hz** |
| Bit depth | **16-bit** signed PCM |
| Channels | **Mono** |
| USB frame size | 96 bytes (48 samples × 2 bytes) per 1 ms SOF |
| End-to-end latency | ~2 ms (1 ms DMA capture + 1 ms USB transfer) |
| PDM clock | 3.072 MHz (PCLK32M, 64× decimation, hardware CIC+FIR) |

> The nRF52840 PDM peripheral performs all decimation and filtering in
> hardware — no software DSP, no CPU overhead, minimal noise.

## Audio pipeline

```
MSM261D3526H1CPM
  │  PDM bitstream @ 3.072 MHz
  ▼
nRF52840 PDM peripheral
  │  Hardware CIC + compensation FIR (64× decimation)
  │  DMA → k_mem_slab (8 × 96-byte blocks)
  ▼
audio_stream thread (priority 2)
  │  dmic_read() → net_buf_alloc() → net_buf_add_mem()
  ▼
Zephyr USB Audio Class 1 (UAC1)
  │  ISO IN endpoint, 96 bytes / 1 ms frame
  ▼
Host OS audio stack → Audacity / DAW
```

## Build & Flash

### Prerequisites

```sh
# Install Zephyr SDK and west (https://docs.zephyrproject.org/latest/develop/getting_started/)
pip install west
west init -m https://github.com/didymos/xiao_nrf54820_sense_usb_audio_streaming --mr main workspace
cd workspace
west update
```

### Build

```sh
cd app
west build -b xiao_ble/nrf52840/sense -- \
    -DCONFIG_USB_DEVICE_PRODUCT=\"XIAO\ BLE\ Sense\ Mic\"
```
Edit ```-DCONFIG_USB_DEVICE_PRODUCT´´´to chnage the USB identifier of your XIAO mic..  
### Flash

```sh
west flash
# or via UF2 bootloader (double-tap reset):
west build -t uf2
# copy build/zephyr/zephyr.uf2 to the XIAO USB drive
```

### Verify

1. Connect XIAO via USB.
2. Check OS audio inputs — "XIAO BLE Sense Mic" should appear.
3. Open Audacity → select the device → Record.

## Project structure

```
.
├── CMakeLists.txt
├── prj.conf                         # Kconfig: USB audio + DMIC
├── west.yml                         # Zephyr workspace manifest
├── boards/
│   └── xiao_ble_nrf52840_sense.overlay   # DT: PDM pins, mic power, UAC1 node
└── src/
    └── main.c                       # Audio capture + USB streaming
```

## Key design decisions

### Why UAC1 over UAC2?

UAC1 (USB Audio Class 1) works on **every OS without drivers**, including
older Windows and iOS. UAC2 requires a kernel driver on Windows < 10 and
adds complexity (explicit/implicit feedback synchronisation). For a 48 kHz
mono source, UAC1 at full-speed USB is more than sufficient.

### Why 48 kHz?

The MSM261D3526H1CPM's SNR is rated at the top of its usable bandwidth
(up to ~10 kHz audio). 48 kHz captures the full acoustic response and is
the native rate of most DAWs, avoiding resampling artefacts. The nRF52840
PDM peripheral generates an exact 3.072 MHz PDM clock from PCLK32M at
this rate.

### Why 1 ms blocks?

USB full-speed SOF fires every 1 ms. Matching the DMA block size to one
SOF period means each captured block is dispatched in the very next frame,
giving ~2 ms end-to-end latency. Larger blocks trade latency for robustness;
edit `BLOCK_SAMPLES` in `main.c` if you need more headroom.

### Double-buffering

The `k_mem_slab` holds 8 DMA buffers. The nrfx_pdm driver DMA-fills one
while the audio thread consumes another — the pipeline never stalls.

## Tuning

| Goal | Change |
|------|--------|
| More headroom (less xrun risk) | Increase `DMIC_SLAB_BLOCKS` in `main.c` |
| Lower latency | Decrease `DMIC_SLAB_BLOCKS` to 4 |
| Adjust mic gain | Change `left-gain` in the DTS overlay (0–80) |
| 16 kHz for voice-only | Set `SAMPLE_RATE_HZ 16000` and adjust PDM clock range |

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Device not enumerated | VBUS not detected | Check USB cable, nRF52840 USB regulator |
| Silent audio | Mic power not enabled | Verify P0.09 GPIO in overlay |
| Distorted audio | PDM clock out of range | Check `clock-source = "PCLK32M"` in overlay |
| Audacity shows device but no levels | Wrong channel map | Set `req_chan_map_lo` to `PDM_CHAN_LEFT` |
| `dmic_configure()` fails | Pins not in pinctrl | Check `pdm0_default` pinctrl group in overlay |
