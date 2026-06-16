# Field Recording Controller

A robust, field-deployable 4-channel audio recording controller for Raspberry Pi 5.
Self-contained web UI served over the Pi's Wi-Fi AP at `http://10.42.0.1:8080`.

## Features

- 4-channel JACK capture (`jack_capture`) with drift-locked clock
- Synchronized GO-tone sync marker captured in-band by all mics
- Countdown beeps → GO tone → recording (state machine, never double-spawns)
- Mobile-first web UI: large touch targets, dark theme, no external CDN
- Automatic time sync from browser (no RTC required)
- Download & delete recordings from the UI
- Mock mode for development without hardware

---

## Prerequisites (on the Pi, already provided by the setup tutorial)

- Raspberry Pi OS Bookworm (64-bit), user `rec` in group `audio`
- `audio-sync.service` running with JACK ports:
  `system:capture_1`, `mic2:capture_1`, `mic3:capture_1`, `mic4:capture_1`
- `jack_capture`, `jack_lsp`, `jack_samplerate`, `aplay` installed
- USB speaker at `plughw:CARD=Device` (optional — cues disabled if absent)
- SSD mounted at `/mnt/ssd/recordings`
- Python 3.11+

---

## Installation on the Pi

### 1. Clone / copy the project

```bash
sudo mkdir -p /opt/fieldrec
sudo chown rec:rec /opt/fieldrec
# copy this directory to /opt/fieldrec
rsync -av fieldrec/ rec@10.42.0.1:/opt/fieldrec/
```

### 2. Create the venv and install dependencies

```bash
cd /opt/fieldrec
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. Grant the time-setting sudo rule

```bash
sudo tee /etc/sudoers.d/fieldrec-time <<'EOF'
rec ALL=(root) NOPASSWD: /usr/bin/date -s *
EOF
sudo chmod 440 /etc/sudoers.d/fieldrec-time
```

### 4. Install and enable the systemd service

```bash
sudo cp /opt/fieldrec/deploy/fieldrec-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable fieldrec-web.service
sudo systemctl start fieldrec-web.service
```

### 5. Check it's running

```bash
sudo systemctl status fieldrec-web.service
journalctl -u fieldrec-web.service -f
```

Open `http://10.42.0.1:8080` on any phone or laptop connected to the Pi's Wi-Fi AP.

---

## Running manually

### Production (on the Pi)

```bash
cd /opt/fieldrec
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### Development / mock mode (any machine)

```bash
cd fieldrec
pip install -r requirements.txt        # or use the venv
FIELDREC_MOCK=1 uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

Or with the flag:

```bash
FIELDREC_MOCK=1 python -m uvicorn app.main:app --port 8080
```

In mock mode:
- `jack_lsp`, `jack_capture`, and `aplay` are all stubbed
- Recordings are written to a temp directory printed at startup
- The full start → countdown → record → stop → saved cycle works end-to-end
- A **MOCK** badge appears in the UI header

---

## Configuration (`config.yaml`)

| Key | Default | Description |
|---|---|---|
| `sample_rate` | `16000` | Recording sample rate (must match JACK) |
| `channels` | `4` | Number of capture channels |
| `jack_ports` | see below | Ordered list → WAV channel order |
| `out_device` | `plughw:CARD=Device` | ALSA device for cue tones |
| `recordings_dir` | `/mnt/ssd/recordings` | Output directory |
| `bit_depth` | `16` | WAV bit depth |
| `host` | `0.0.0.0` | Bind address |
| `port` | `8080` | Listen port |
| `countdown_beeps` | `3` | Number of pre-start beeps |
| `min_free_mb` | `500` | Minimum free disk (MB) to allow start |

---

## API Reference

All `/api/*` routes return JSON and include `Cache-Control: no-store`.
CORS is open (closed AP network).

### `GET /`
Serves the single-page UI (`app/static/index.html`).

---

### `GET /api/status`
Returns the full recorder status (polled by UI every second).

**Response:**
```json
{
  "state": "IDLE",
  "elapsed_seconds": 0.0,
  "current_file": null,
  "disk_free_mb": 12345.6,
  "disk_total_mb": 238475.0,
  "mics": [
    {"port": "system:capture_1", "present": true},
    {"port": "mic2:capture_1",   "present": true},
    {"port": "mic3:capture_1",   "present": true},
    {"port": "mic4:capture_1",   "present": true}
  ],
  "jack_alive": true,
  "sample_rate": 16000,
  "xruns": 0,
  "pi_time": "2024-11-15T14:32:01",
  "error": null,
  "countdown_beeps": 3
}
```

**States:** `IDLE` → `ARMING` → `RECORDING` → `STOPPING` → `IDLE` (+ `ERROR`)

---

### `POST /api/start`
Start a recording. Only allowed from `IDLE`.

**Body:**
```json
{ "session": "scene01", "countdown_beeps": 3 }
```
Both fields optional. `session` becomes the filename suffix (default: `take`).

**Returns:** status object

**Errors:**
- `409` if not IDLE or pre-flight failed (body contains `detail` with reason)

---

### `POST /api/stop`
Stop the current recording. Only allowed from `RECORDING`.

**Returns:** status object  
**Error:** `409` if not RECORDING

---

### `POST /api/reset`
Force-clear an `ERROR` state back to `IDLE`.

**Returns:** status object

---

### `POST /api/time`
Set the Pi system clock from client time (called automatically by UI on load).

**Body:** `{ "epoch_ms": 1700000000000 }`

**Returns:** `{ "ok": true, "pi_time": "..." }`

Requires sudoers rule: `rec ALL=(root) NOPASSWD: /usr/bin/date -s *`

---

### `GET /api/recordings`
List all recordings, newest first.

**Returns:**
```json
[
  {
    "name": "20241115_143201_scene01.wav",
    "size_mb": 7.32,
    "duration_s": 120.5,
    "channels": 4,
    "created": "2024-11-15T14:32:01",
    "downloaded": false
  }
]
```

---

### `GET /api/recordings/{name}/download`
Stream a WAV file with correct `Content-Length` (browser shows progress bar).
Marks `downloaded: true` in the sidecar JSON after a complete transfer.

---

### `DELETE /api/recordings/{name}`
Delete a WAV and its `.json` sidecar. Path-traversal-safe.

**Returns:** `{ "deleted": "filename.wav" }`

---

### `GET /api/health`
Quick health check (suitable for monitoring / load-balancer probes).

**Returns:**
```json
{ "ok": true, "jack": true, "mics_present": 4, "disk_free_mb": 12345.6, "mock": false }
```

---

## Sidecar JSON format

Each recording `NAME.wav` has a `NAME.json` sidecar:

```json
{
  "start_time": "2024-11-15T14:32:01.123",
  "duration_s": 120.456,
  "channels": 4,
  "sample_rate": 16000,
  "go_tone_wall_time": "2024-11-15T14:32:05.234",
  "go_tone_offset_note": "GO tone played ~4.11s after capture started. Use in-band audio transient for precise per-recorder sync.",
  "downloaded": false
}
```

Use the in-band GO tone (1320 Hz, ~700 ms) to align multiple recorders in post.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Pre-flight: "JACK ports not live" | `systemctl status audio-sync.service` and `jack_lsp` |
| Pre-flight: "Disk too full" | `df -h /mnt/ssd` |
| No cue tones | Check `out_device` in `config.yaml` vs `aplay -L` |
| Time set fails | Verify sudoers rule and that `/usr/bin/date` path is correct |
| Service won't start | `journalctl -u fieldrec-web.service -n 50` |

---

## Development notes

- The process never crashes on recording/tone errors — captures them, sets `ERROR`, logs, keeps serving.
- Path traversal is prevented by stripping all directory components from `{name}` route params.
- `jack_lsp` output is cached for 2 s; `xruns` (journalctl) cached for 15 s to keep the status endpoint cheap.
- The UI recovers gracefully from connection drops (retries every second).
