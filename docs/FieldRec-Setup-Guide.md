# FieldRec — Full Setup Guide

FieldRec is a headless multi-channel field audio recorder running on a Raspberry Pi 5. USB microphones (XIAO nRF52840 Sense) are bridged through JACK's adaptive resampler so all channels share identical latency. Control is via a mobile-friendly web interface served from the Pi itself.

---

## Table of Contents

1. [Hardware Requirements](#1-hardware-requirements)
2. [Architecture Overview](#2-architecture-overview)
3. [Installation](#3-installation)
4. [Post-Install Configuration](#4-post-install-configuration)
5. [Microphone Identification](#5-microphone-identification)
6. [Services & Boot Behaviour](#6-services--boot-behaviour)
7. [Web Interface](#7-web-interface)
8. [Study Protocol Workflow](#8-study-protocol-workflow)
9. [Configuration Reference](#9-configuration-reference)
10. [Troubleshooting](#10-troubleshooting)
11. [Directory Layout](#11-directory-layout)
12. [API Reference](#12-api-reference)

---

## 1. Hardware Requirements

| Component | Details |
|---|---|
| **Computer** | Raspberry Pi 5 (4 GB RAM recommended) |
| **OS** | Raspberry Pi OS Bookworm or Bullseye (64-bit) |
| **Microphones** | Seeed XIAO nRF52840 Sense (USB audio) — 1 to N units |
| **Storage** | NVMe SSD via M.2 HAT (recommended); falls back to SD card |
| **Speaker/output** | Any ALSA-accessible output for headless fallback tones (optional) |
| **Network** | Built-in Wi-Fi (for hotspot) or Ethernet |

### Microphone Specifications

The XIAO nRF52840 Sense presents as a USB audio class device with:

- Format: `S16_LE` (16-bit signed little-endian)
- Channels: `1` (mono)
- Sample rate: `16000 Hz` (only supported rate)

Any number of mics is supported; the installer auto-detects them.

---

## 2. Architecture Overview

```
┌──────────────────────────────────────────────────────────┐
│ Raspberry Pi 5                                           │
│                                                          │
│  USB Mic (id Mic)   ──► zita-a2j (Mic)   ──┐            │
│  USB Mic (id Mic_1) ──► zita-a2j (Mic_1) ──┼──► jackd   │
│  USB Mic (id Mic_2) ──► zita-a2j (Mic_2) ──┤  (dummy)   │
│  USB Mic (id Mic_3) ──► zita-a2j (Mic_3) ──┘            │
│                                    │                     │
│                              jack_capture                 │
│                                    │                     │
│                       RECORDINGS_DIR/*.wav               │
│                                                          │
│  FastAPI (port 8080) ◄──── browser / mobile app          │
└──────────────────────────────────────────────────────────┘
```

All USB audio capture devices are auto-detected at boot; each is bridged into JACK by a `zita-a2j` client named after its ALSA card id.

**Key design decisions:**

- **JACK dummy driver** — jackd runs with no capture hardware; all mics enter through zita-a2j bridges. This gives every channel exactly the same adaptive-resampling latency path, eliminating inter-channel timing offsets.
- **Auto-detected mics** — every USB audio capture device is discovered at boot (no config lists ports). Each bridge is named after the ALSA card **id** (`/sys/class/sound/cardN/id`, sanitised to `[A-Za-z0-9_]`), so ports become `Mic:capture_1`, `Mic_1:capture_1`, etc. These names are stable regardless of which physical USB port a mic occupies — re-plugging never requires editing config.
- **zita-a2j** — bridges each USB mic's ALSA stream into JACK with sample-rate-adaptive resampling (`-c 1` for mono XIAO mics).
- **jack_capture** — records from selected JACK ports to a single multi-channel WAV file.
- **Sync tone & flash** — the synchronisation tone and full-screen flash are generated **in the browser** (Web Audio API) the moment recording goes live, so they are captured by the mics for cross-correlation and video alignment. Server-side `aplay` tones remain only as a headless fallback.

---

## 3. Installation

### 3.1 Clone the repository

```bash
git clone https://github.com/didymos/xiao_nrf54820_sense_usb_audio_streaming.git
cd xiao_nrf54820_sense_usb_audio_streaming/fieldrec
```

### 3.2 Run the installer

```bash
sudo ./install.sh
```

The installer is **idempotent** — safe to run again to update or repair an existing installation.

> **Note:** Mic enrollment is gone. Microphones are auto-detected at runtime, so there is no longer any USB-port assignment step. The `--enroll` flag still exists but now only prints a note that enrollment is unnecessary.

### 3.3 What the installer does

| Step | Action |
|---|---|
| 0 | Pre-flight: detect non-root target user, check OS |
| 1 | Install apt packages (jackd2, zita-ajbridge, alsa-utils, python3, …) |
| 2 | Mask/disable PipeWire and PulseAudio (they conflict with JACK) |
| 3 | Configure real-time scheduling limits (`/etc/security/limits.d/`) |
| 4 | Install & enable `cpu-performance.service` (sets CPU governor to performance) |
| 5 | Microphone detection (count USB capture devices; no enrollment) |
| 6 | Sample rate (fixed at `16000`; no per-port detection) |
| 7 | Set up NVMe SSD mount at `/mnt/ssd`; fall back to `~/recordings` |
| 8 | Detect ALSA output device for tone playback |
| 9 | Create tones directory |
| 10 | Write `/etc/fieldrec/fieldrec.conf` |
| 11 | Deploy app to `/opt/fieldrec/`, create Python venv |
| 12 | Install `audio-sync.service` |
| 13 | Install `fieldrec-web.service` |
| 14 | Add sudoers rule for `date` command (clock sync from browser) |
| 15 | Configure Wi-Fi hotspot via NetworkManager |
| 16 | Enable and start services |
| 17 | Self-test |

Install log is at `/var/log/fieldrec-install.log`.

---

## 4. Post-Install Configuration

### 4.1 Verify services are running

```bash
systemctl status audio-sync fieldrec-web
```

### 4.2 Verify microphone detection

Mics are auto-detected at boot — there is no config to edit for mic ports. Confirm the bridges came up:

```bash
jack_lsp
# Should list Mic:capture_1, Mic_1:capture_1, … one per detected mic
```

If the list is empty, check that the mics are visible to ALSA and inspect the audio service logs, then restart:

```bash
arecord -l                       # should list the USB capture cards
journalctl -u audio-sync -n 80 --no-pager
sudo systemctl restart audio-sync
```

### 4.3 Connect to the web interface

| Method | URL |
|---|---|
| Same network | `http://<pi-ip>:8080` |
| Via hotspot | Connect to `FieldRec` Wi-Fi, then `http://10.42.0.1:8080` |

### 4.4 Add to Android / iOS home screen

**Android (Chrome):** Menu → *Add to Home screen*

**iOS (Safari):** Share → *Add to Home Screen*

The app installs as a standalone PWA with a dark-themed icon.

---

## 5. Microphone Identification & Channel Assignment

Every USB audio capture device is auto-detected at boot and bridged into JACK by a `zita-a2j` client named after its ALSA card id (`/sys/class/sound/cardN/id`, sanitised to `[A-Za-z0-9_]`). For XIAO nRF52840 Sense mics these ids are `Mic`, `Mic_1`, `Mic_2`, `Mic_3`. There is no enrollment step and no `MIC_PORTS`/`JACK_PORTS` config (both are ignored at runtime).

### Channel = physical USB hub socket

The XIAO firmware reports an **identical USB product string and serial** (`XIAO-MIC-001`) on every board, so the kernel cannot tell the units apart. It assigns the `Mic` / `Mic_1` / `Mic_2` / `Mic_3` suffixes in **enumeration order**, which can differ from one boot to the next — the *name* of a given physical mic is therefore not stable.

To make recording channels deterministic, FieldRec orders channels by the **USB port token** (the kernel's topology address, e.g. `3-1.1`), not by the card-id name. The token's leaf (`.1`, `.2`, …) is the physical hub socket and is fixed by the hardware. So:

```
USB 3-1.1  → channel 1
USB 3-1.2  → channel 2
USB 3-1.3  → channel 3
USB 3-1.4  → channel 4
```

Channel N is **always the same physical socket**, regardless of which card-id name the kernel happened to assign that boot. Label the four hub sockets `1`–`4` and the mapping never moves. The web UI leads each mic row with its `USB <token>` (the stable identifier) and shows the current card-id name as a small secondary label.

> **Tip:** If you need a specific *microphone* (not socket) always on a given channel, keep each mic in its labelled hub socket, or flash the boards with unique USB serials so they can be matched by device identity instead.

### Listing detected capture devices

```bash
jack_lsp                         # JACK client names (Mic, Mic_1, …)
arecord -l                       # underlying ALSA capture cards
# USB port token per capture card:
for c in /sys/class/sound/card[0-9]*/device; do
  echo "$(basename $(readlink -f $c) | cut -d: -f1)  $(cat $(dirname $c)/id)"
done
```

---

## 6. Services & Boot Behaviour

Both services start automatically at boot under `multi-user.target` (non-graphical). They require no display or desktop session.

### audio-sync.service

Runs `/opt/fieldrec/audio/start-audio.sh` as `TARGET_USER` in the `audio` group.

1. Kills any existing jackd / zita-a2j processes
2. Starts `jackd -d dummy -r SAMPLE_RATE -p JACK_FRAMES`
3. Polls `jack_lsp` until JACK is ready (up to 15 s)
4. Auto-discovers every USB audio capture card (any card with a `/sys/class/sound/pcmC<n>D*c` capture PCM) and starts one `zita-a2j -c 1` per card, named after the card id
5. Polls until each `<CardId>:capture_1` port appears in JACK; a bridge that fails to appear warns and is skipped (the stack keeps running)
6. Waits on jackd PID; restarts if jackd exits

### fieldrec-web.service

Runs uvicorn as `TARGET_USER`:

```
/opt/fieldrec/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 1
```

Depends on `audio-sync.service` via `Wants=` (starts even if audio-sync is unavailable).

### Useful commands

```bash
# View live audio logs
journalctl -u audio-sync -f

# View live web logs
journalctl -u fieldrec-web -f

# Restart everything
sudo systemctl restart audio-sync fieldrec-web

# Check JACK ports
jack_lsp
```

---

## 7. Web Interface

### Main recording card

| State | Button label | Behaviour |
|---|---|---|
| IDLE | **START** | Runs pre-flight checks and starts recording within ~0.5 s (no countdown) |
| RECORDING | **STOP** | Sends SIGINT to jack_capture, plays stop tone, saves sidecar JSON |
| STOPPING | (spinner) | Saving in progress |
| ERROR | **Reset** | Clears error state, returns to IDLE |

The moment recording goes live, the **browser** plays a synchronisation tone (an 8 ms white-noise burst with a sharp onset for cross-correlation, followed by a 72 ms 1000 Hz sine) and fires a white full-screen flash for video sync. This tone is captured by the mics and appears in the recording. State-transition tones are also browser-side: an ascending double-beep on save, a descending double-beep on error.

**Session name / suffix** — appended to the UTC timestamp in the filename:
`20240618T123456Z_baseline.wav`

### Channels card

Lists all auto-detected capture inputs, ordered by physical USB hub socket and led by their `USB <token>` label. Inputs whose JACK client name matches `^Mic(_\d+)?$` (case-insensitive) are pre-selected by default. Channel numbers follow the USB port order, so channel N is always the same hub socket. Each row:
- **Checkbox** — include/exclude from recording
- **CH badge** — assigned channel number in the output file
- **Presence dot** — green if the JACK port is live, dark if offline
- **Port name** (card id) and **USB port token** (secondary label, from `/sys`, for reference)

Changes are staged locally; press **Apply** to send to the server. The **All** button toggles all channels on/off.

### Study Protocol card

Upload a Markdown file to define a sequence of recordings. See [Study Protocol Workflow](#8-study-protocol-workflow).

### Recordings list

Lists saved WAV files sorted by timestamp. Each entry shows duration, file size, recording time, channel count, and protocol entry (if any). Download and delete buttons are provided.

---

## 8. Study Protocol Workflow

A study protocol is a Markdown file that defines a sequence of recordings with instructions for each.

### File format

```markdown
# Optional overall title

## baseline_sitting
Participant sits still, eyes forward. Breathe normally for 30 seconds.

## walking_slow
Walk at a relaxed pace across the room and back. Repeat 3 times.

## speaking_numbers
Count aloud from 1 to 20 at a normal conversational pace.
```

Rules:
- Each `## heading` defines one recording entry
- The heading text becomes the WAV filename suffix (sanitised to safe characters)
- The body text (until the next `##`) is displayed as instructions
- `# H1 lines` are ignored (can be used for a document title)
- Any number of entries is supported

### Workflow

1. **Upload** — tap *Upload .md* in the Study Protocol card
2. **Entry 1 is shown** — progress indicator, title, and instructions appear
3. **Session name is auto-filled** from the current entry's suffix (editable)
4. **Record** — press START; the description stays visible while recording
5. **Stop** — press STOP; after saving the server automatically advances to entry 2
6. **Repeat** until all entries are recorded
7. **Clear** — tap *Clear* to unload the protocol

The protocol state persists in server memory (resets on service restart). It is also saved in each recording's sidecar JSON under `protocol_entry`.

---

## 9. Configuration Reference

Config file: `/etc/fieldrec/fieldrec.conf`  
Format: shell `key=value`; strings with spaces must be quoted.  
Permissions: `root:root 644` (readable by all, writable by root only).

| Key | Default | Description |
|---|---|---|
| `TARGET_USER` | _(detected)_ | System user that runs the services |
| `TARGET_HOME` | _(detected)_ | Home directory of TARGET_USER |
| `RECORDINGS_DIR` | `/mnt/ssd/recordings` | Where WAV files are saved |
| `TONES_DIR` | `/opt/fieldrec/tones` | Legacy; not used at runtime |
| `MIC_PORTS` | `""` | **Ignored/legacy** — mics are auto-detected; kept empty for reference only |
| `JACK_PORTS` | `""` | **Ignored/legacy** — JACK ports are named by card id; kept empty for reference only |
| `CHANNELS` | `4` | **Ignored/legacy** — runtime channel count is derived from selection |
| `SAMPLE_RATE` | `16000` | Mic sample rate in Hz (XIAO mics support only `16000`) |
| `JACK_FRAMES` | `512` | JACK buffer size in frames |
| `JACK_NPERIODS` | `3` | JACK periods (not used by dummy driver; informational) |
| `OUT_DEVICE` | `plughw:CARD=Speaker` | ALSA device for the headless fallback tones |
| `BIT_DEPTH` | `16` | Recording bit depth (16 or 24) |
| `COUNTDOWN_BEEPS` | `0` | No longer used — countdown removed; sync tone is browser-side |
| `MIN_FREE_MB` | `500` | Minimum free disk space required to start recording |
| `HTTP_HOST` | `0.0.0.0` | Web server bind address |
| `HTTP_PORT` | `8080` | Web server port |
| `HOTSPOT_SSID` | `FieldRec` | Wi-Fi hotspot SSID |
| `HOTSPOT_PASS` | `fieldrecpass` | Wi-Fi hotspot password |

After editing, restart the affected service:

```bash
sudo systemctl restart audio-sync   # for audio/mic settings
sudo systemctl restart fieldrec-web # for web/path settings
```

---

## 10. Troubleshooting

### Service fails to start

```bash
journalctl -u fieldrec-web --boot -n 80 --no-pager
journalctl -u audio-sync   --boot -n 80 --no-pager
```

### No mics appear

Mics are auto-detected, so there are no port paths to fix. Check that the hardware is visible and that the bridges came up, then restart:

```bash
arecord -l                       # should list the USB capture cards
jack_lsp                         # should list Mic:capture_1, Mic_1:capture_1, …
sudo systemctl restart audio-sync
```

### JACK fails to start or no ports appear

```bash
jack_lsp                        # should list Mic, Mic_1, …:capture_1
journalctl -u audio-sync -f     # watch for errors
```

Common causes:
- Sample rate mismatch — set `SAMPLE_RATE=16000` for XIAO mics
- PipeWire/PulseAudio still running — re-run `install.sh` to mask them

### Recording error: "JACK server is not running"

audio-sync.service hasn't started or has crashed:

```bash
sudo systemctl restart audio-sync
sleep 5
sudo systemctl restart fieldrec-web
```

### Web service crashes on startup: `RuntimeError: Form data requires "python-multipart"`

```bash
/opt/fieldrec/.venv/bin/pip install python-multipart
sudo systemctl restart fieldrec-web
```

(This package is now in `requirements.txt`; only affects installs from before this was added.)

### Recordings have inter-channel time offset

All mics go through zita-a2j (not directly through the jackd ALSA driver), which gives every channel the same resampling latency path. Bridges are created automatically per card id, so there are no port names to configure. Confirm every mic shows up as a `<CardId>:capture_1` port in `jack_lsp`; if one is missing, check `journalctl -u audio-sync` for a skipped-bridge warning.

### Browser sync tone or flash doesn't fire

Mobile browsers (iOS/Android) require a user gesture before playing audio. Tap **START** once to unlock browser audio; the tone and flash will fire on subsequent recordings.

### Check xruns

```bash
journalctl -u audio-sync --boot | grep -i xrun
```

Increase `JACK_FRAMES` in the config (512 → 1024) if xruns are frequent.

### Clock drift

The web interface automatically syncs the Pi clock from the browser on page load (requires sudoers rule for `/usr/bin/date`, installed by `install.sh`).

---

## 11. Directory Layout

```
/opt/fieldrec/
├── app/
│   ├── main.py               # FastAPI application
│   ├── requirements.txt
│   └── static/
│       ├── index.html        # Single-page web UI
│       ├── icon-192.png      # PWA icon
│       └── icon-512.png      # PWA icon
├── audio/
│   └── start-audio.sh        # JACK + zita-a2j startup script
├── tones/
│   └── make_tones.py         # Reference script (tones generated at runtime)
└── .venv/                    # Python virtual environment

/etc/fieldrec/
└── fieldrec.conf             # Runtime configuration (root:root 644)

/etc/systemd/system/
├── audio-sync.service
├── fieldrec-web.service
└── cpu-performance.service

/var/log/
└── fieldrec-install.log      # Install log
```

---

## 12. API Reference

All endpoints are on `http://<pi>:8080`. Responses are JSON.

### Status & Control

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | Service health check |
| `GET` | `/api/status` | Full recorder state (polls at 1 Hz from UI) |
| `POST` | `/api/start` | Start recording. Body: `{"session": "name"}`. Synchronous: blocks ~0.5 s until `jack_capture` is confirmed running, then returns `{"state": "RECORDING", "file": …}`. Returns 422 on pre-flight failure, 500 on capture failure. |
| `POST` | `/api/stop` | Stop recording (sends SIGINT to jack_capture) |
| `POST` | `/api/reset` | Clear error state, return to IDLE |
| `POST` | `/api/time` | Sync Pi clock. Body: `{"client_ms": <epoch_ms>}` |

### Microphone selection

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/mics` | Available JACK capture ports + current selection |
| `POST` | `/api/mics/select` | Set active ports. Body: `{"ports": ["Mic:capture_1", …]}` |
| `DELETE` | `/api/mics/select` | Reset to default (auto-selected mic-named ports) |

### Study protocol

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/protocol` | Upload Markdown file (multipart) |
| `GET` | `/api/protocol/current` | Current entry + progress |
| `POST` | `/api/protocol/advance` | Advance to next entry |
| `POST` | `/api/protocol/reset` | Restart from entry 1 |
| `DELETE` | `/api/protocol` | Clear loaded protocol |

### Recordings

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/recordings` | List all WAV files with metadata |
| `GET` | `/api/recordings/{name}/download` | Download a WAV file |
| `DELETE` | `/api/recordings/{name}` | Delete a recording and its sidecar |

### Sidecar JSON

Every recording produces a `<name>.json` alongside the WAV. `go_tone_utc` marks the moment `jack_capture` was confirmed running (recording live):

```json
{
  "file": "20240618T123456Z_baseline.wav",
  "start_time_utc": "2024-06-18T12:34:56+00:00",
  "go_tone_utc": "2024-06-18T12:34:56.5+00:00",
  "go_tone_offset_s": 0.5,
  "sample_rate": "16000",
  "channels": 4,
  "jack_ports": ["Mic:capture_1", "Mic_1:capture_1", "Mic_2:capture_1", "Mic_3:capture_1"],
  "usb_ports": ["3-1.1", "3-1.2", "3-1.3", "3-1.4"],
  "channel_peaks": [0.42, 0.38, 0.0, 0.51],
  "silent_channels": [{"channel": 3, "jack_port": "Mic_2:capture_1", "usb_port": "3-1.3"}],
  "bit_depth": "16",
  "downloaded": false,
  "protocol_entry": {
    "number": 1,
    "suffix": "baseline",
    "title": "baseline_sitting",
    "description": "Participant sits still…"
  }
}
```

### Mock mode

Set `FIELDREC_MOCK=1` to run without hardware (full state machine, silent tones, temporary recordings directory):

```bash
FIELDREC_MOCK=1 /opt/fieldrec/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
```
