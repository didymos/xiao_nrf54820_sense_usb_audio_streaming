# FieldRec — Production Field Audio Recorder for Raspberry Pi 5

A production-grade 4-channel USB audio recording system: JACK-based capture,
synchronized GO-tone sync marker, mobile-first web UI, Wi-Fi hotspot, NVMe storage.

---

## Quick Start

```bash
sudo ./install.sh          # automated install
sudo ./install.sh --enroll # interactive mic enrollment (plug in one at a time)
```

Browse to `http://<pi-ip>:8080` (or the hotspot address) from any device.

---

## Features

- **4-channel JACK capture** via `jack_capture` with per-mic zita-a2j bridges
- **USB port-path mic identification** — survives reboots regardless of enumeration order
- **Synchronized GO-tone** (1320 Hz, in-band) for precise multi-recorder post-sync
- **Countdown beeps** before recording starts
- **Per-recording JSON sidecar** with GO-tone UTC timestamp and offset
- **Mobile-first dark web UI** — vanilla JS, no CDN, works fully offline
- **NVMe SSD auto-detect** with ext4 format and /etc/fstab mount
- **Wi-Fi hotspot** via NetworkManager (SSID: FieldRec)
- **RT scheduling** — JACK runs at priority 70, memlock unlimited
- **CPU performance governor** via sysfs (oneshot systemd service)
- **Mock mode** — full state machine works without any JACK hardware

---

## Directory Layout

```
fieldrec/
  install.sh                  main idempotent installer (sudo ./install.sh)
  uninstall.sh                clean removal
  lib/
    log.sh                    colored logging helpers
    packages.sh               per-package apt + jack_capture source fallback
    audio.sh                  neutralize audio servers, RT tuning, output detect
    network.sh                nmcli hotspot
    storage.sh                NVMe detect + mount, recordings dir
    mics.sh                   USB mic enrollment, sample rate detection
    selftest.sh               post-install self-test suite
  config/
    fieldrec.conf.default     template config (reference only)
  audio/
    start-audio.sh            JACK + zita-a2j startup script
  tones/
    make_tones.py             generates WAV cue tones (numpy + soundfile)
  app/
    main.py                   FastAPI controller
    static/index.html         mobile-first SPA
    requirements.txt
  systemd/
    audio-sync.service.tmpl   JACK stack service template
    fieldrec-web.service.tmpl FastAPI web service template
    cpu-performance.service   sysfs CPU governor oneshot
```

---

## Config File: `/etc/fieldrec/fieldrec.conf`

Shell `key=value` format. Written by `install.sh`; edit to override.

| Key | Example | Description |
|---|---|---|
| `TARGET_USER` | `rec` | System user running services |
| `RECORDINGS_DIR` | `/mnt/ssd/recordings` | Output directory |
| `SAMPLE_RATE` | `48000` | Recording sample rate (Hz) |
| `CHANNELS` | `4` | Number of capture channels |
| `JACK_FRAMES` | `512` | JACK buffer frames |
| `JACK_NPERIODS` | `3` | JACK periods |
| `JACK_MASTER_PORT` | `1-1.1` | USB port path for MIC1 (JACK master) |
| `MIC_PORTS` | `"1-1.1 1-1.2 1-1.3 1-1.4"` | All USB port paths |
| `JACK_PORTS` | `"system:capture_1 mic2:capture_1 ..."` | JACK port names for capture |
| `OUT_DEVICE` | `plughw:CARD=Speaker` | ALSA output for tone playback |
| `BIT_DEPTH` | `16` | Recording bit depth |
| `COUNTDOWN_BEEPS` | `3` | Number of countdown beeps |
| `MIN_FREE_MB` | `500` | Minimum free disk to allow recording |
| `HTTP_PORT` | `8080` | Web interface port |
| `HOTSPOT_SSID` | `FieldRec` | Wi-Fi hotspot SSID |
| `HOTSPOT_PASS` | `fieldrecpass` | Wi-Fi hotspot password |

---

## Audio Stack Architecture

```
USB mic1 ──► jackd (hw:<mic1_card>) ──► system:capture_1 ─┐
USB mic2 ──► zita-a2j (mic2)        ──► mic2:capture_1   ─┤──► jack_capture ──► session.wav
USB mic3 ──► zita-a2j (mic3)        ──► mic3:capture_1   ─┤
USB mic4 ──► zita-a2j (mic4)        ──► mic4:capture_1   ─┘
```

- `jackd` runs capture-only on MIC1 (master clock): `-R -P70 --capture`
- MIC2-4 bridged via `zita-a2j`; each bridge polled via `jack_lsp` before proceeding
- `jack_capture` receives SIGINT to stop (never `-d` duration flag)
- For >2 channels, `-f wav` is omitted (jack_capture auto-selects WAVE_EX format)

---

## USB Port Path Identification

Mics are identified by USB port path (e.g., `1-1.2`), not card number.
This means plugging in the same mic on the same port always maps to the same channel.

```bash
# Find port paths:
ls -la /sys/class/sound/card*/device | grep -v '^total'
# or:
for c in /sys/class/sound/card[0-9]*; do
    echo "Card $(basename $c | tr -dc '0-9'): $(basename $(readlink -f $c/device))"
done
```

---

## API Reference

All `/api/*` routes return JSON with `Cache-Control: no-store`. CORS open.

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Web UI (SPA) |
| `GET` | `/api/health` | Health check |
| `POST` | `/api/time` | Sync Pi clock from browser |
| `GET` | `/api/status` | Full status (polled every 1 s) |
| `POST` | `/api/start` | Start recording (body: `{"session":"take"}`) |
| `POST` | `/api/stop` | Stop recording (SIGINT to jack_capture) |
| `POST` | `/api/reset` | Clear ERROR state |
| `GET` | `/api/recordings` | List recordings (newest first) |
| `GET` | `/api/recordings/{name}/download` | Stream WAV file |
| `DELETE` | `/api/recordings/{name}` | Delete WAV + sidecar |

**State machine:** `IDLE → ARMING → RECORDING → STOPPING → IDLE` (+ `ERROR`)

---

## Mock Mode (Development)

```bash
FIELDREC_MOCK=1 uvicorn app.main:app --port 8080 --reload
```

- All JACK/ALSA/aplay calls stubbed
- Tones generated as silent WAVs in a tmpdir
- Recordings written to tmpdir
- Full state machine cycle works end-to-end
- **MOCK** badge visible in UI header

---

## Sidecar JSON

Each `session.wav` has a `session.json` sidecar:

```json
{
  "file": "20241115T143201Z_take.wav",
  "start_time_utc": "2024-11-15T14:32:01+00:00",
  "go_tone_utc": "2024-11-15T14:32:05.234+00:00",
  "go_tone_offset_s": 4.11,
  "sample_rate": "48000",
  "channels": "4",
  "bit_depth": "16",
  "downloaded": false
}
```

Use `go_tone_offset_s` (seconds into the recording when the GO tone fired) together
with the in-band 1320 Hz transient to align multiple recorders in post.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| JACK ports not live | `systemctl status audio-sync` · `jack_lsp` |
| zita-a2j bridge missing | `journalctl -u audio-sync -n 50` |
| No cue tones | `OUT_DEVICE` in conf · `aplay -L` |
| Disk full warning | `df -h "$RECORDINGS_DIR"` |
| Time sync fails | Sudoers rule: `/etc/sudoers.d/fieldrec-date` |
| Web service won't start | `journalctl -u fieldrec-web -n 50` |
| Mic on wrong channel | `sudo ./install.sh --enroll` to re-enroll |

---

## Uninstall

```bash
sudo ./uninstall.sh           # remove services + /opt/fieldrec
sudo ./uninstall.sh --purge   # also delete recordings and /etc/fieldrec
```
