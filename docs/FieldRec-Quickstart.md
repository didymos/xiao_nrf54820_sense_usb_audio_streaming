# FieldRec — Quick Reference

Headless multi-channel field recorder on Raspberry Pi 5 + XIAO nRF52840 Sense mics.  
Full documentation: [[FieldRec-Setup-Guide]]

---

## Hardware

- Raspberry Pi 5 (4 GB), Raspberry Pi OS Bookworm 64-bit
- Seeed XIAO nRF52840 Sense USB mics — S16_LE, mono, **16000 Hz only**
- NVMe SSD via M.2 HAT (recommended), or SD card fallback

---

## Install

```bash
git clone https://github.com/didymos/xiao_nrf54820_sense_usb_audio_streaming.git
cd xiao_nrf54820_sense_usb_audio_streaming/fieldrec
sudo ./install.sh          # idempotent — safe to re-run to update
sudo ./install.sh --enroll # interactive: plug mics in channel order to assign ports
```

---

## Post-install: microphones

Mics are **auto-detected** — every USB audio capture device is bridged into JACK
at boot. No `MIC_PORTS` config to edit. The web UI lists all detected inputs and
pre-selects the mics.

**Channels follow the physical USB hub socket**, not the mic name: `3-1.1`→ch1,
`3-1.2`→ch2, `3-1.3`→ch3, `3-1.4`→ch4. The XIAO boards all report the same USB
serial, so the kernel names (`Mic`, `Mic_1`, …) can shuffle between boots — but
channel N is always the same socket. Label your four hub sockets `1`–`4`.

Confirm they came up:

```bash
jack_lsp    # should list Mic:capture_1  Mic_1:capture_1  …
```

If nothing appears, check the service and that the mics enumerate:

```bash
journalctl -u audio-sync --boot -n 40 --no-pager
arecord -l
sudo systemctl restart audio-sync
```

---

## Access the UI

| Network | URL |
|---|---|
| Same LAN | `http://<pi-ip>:8080` |
| Built-in hotspot | Connect to **FieldRec** Wi-Fi → `http://10.42.0.1:8080` |
| Hotspot password | `fieldrecpass` |

**Add to home screen:** Android Chrome → Menu → *Add to Home screen* · iOS Safari → Share → *Add to Home Screen*

---

## Recording workflow

1. **Channels** card — select mics, press **Apply**
2. (Optional) **Study Protocol** card — upload a `.md` file to define a sequence
3. **Session name** — type a suffix for the filename (auto-filled from protocol)
4. **START** → countdown beeps → recording begins
5. **STOP** → file saved to `RECORDINGS_DIR` with a sidecar `.json`
6. Protocol auto-advances to the next entry after each save

---

## Study protocol format

```markdown
## baseline_sitting
Participant sits still for 30 s. Breathe normally.

## walking_slow
Walk across the room and back, 3 times.
```

`## heading` = entry title and WAV suffix · body = on-screen instructions

---

## Key config (`/etc/fieldrec/fieldrec.conf`)

| Key | XIAO value | Notes |
|---|---|---|
| `SAMPLE_RATE` | `16000` | Must match mic hardware |
| `RECORDINGS_DIR` | `/mnt/ssd/recordings` | WAV output directory |
| `OUT_DEVICE` | `plughw:CARD=Speaker` | ALSA device for fallback tones |

Mics are auto-detected — there is no `MIC_PORTS` to set. Recording start,
the sync tone, and screen flash are handled in the browser; `COUNTDOWN_BEEPS`
is no longer used.

---

## Service commands

```bash
systemctl status audio-sync fieldrec-web  # check both services
sudo systemctl restart audio-sync         # after mic/audio config change
sudo systemctl restart fieldrec-web       # after web/path config change
journalctl -u audio-sync -f               # live audio logs
journalctl -u fieldrec-web -f             # live web logs
jack_lsp                                  # list active JACK ports
```

---

## Common fixes

| Symptom | Fix |
|---|---|
| No mics in web UI / `jack_lsp` empty | Check `arecord -l`, then `sudo systemctl restart audio-sync` |
| Sample rate mismatch / JACK won't start | Set `SAMPLE_RATE=16000` in conf |
| "JACK server is not running" | `sudo systemctl restart audio-sync` then `fieldrec-web` |
| Frequent xruns | Increase `JACK_FRAMES` (512 → 1024) in conf |
| No sync tone / flash on phone | Tap START once to unlock browser audio (iOS/Android gesture) |
| `RuntimeError: Form data requires "python-multipart"` | `/opt/fieldrec/.venv/bin/pip install python-multipart && sudo systemctl restart fieldrec-web` |
