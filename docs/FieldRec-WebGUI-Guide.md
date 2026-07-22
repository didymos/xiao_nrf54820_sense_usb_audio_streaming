# FieldRec — Web GUI & Operating Guide

A practical guide to connecting to the FieldRec recorder (Raspberry Pi + USB
microphones) and operating it through the web interface: making recordings,
assigning microphones to channels, checking results, and handling errors.

---

## 1. Overview

FieldRec is a multi-channel field recorder. The Raspberry Pi runs the audio
stack (JACK + per-microphone bridges) and a web interface. You control
everything — start/stop, channel assignment, downloads, power — from a phone,
tablet, or laptop browser. No app to install.

- **Recorder:** Raspberry Pi with the microphones on a powered USB hub.
- **Control:** any device with a web browser on the same network.
- **Output:** multi-channel WAV files, downloadable from the interface.

---

## 2. Connecting to the Recorder

You reach the interface over the network in one of two ways.

### Option A — Connect to the recorder's own Wi-Fi hotspot (field use)

The Pi broadcasts its own Wi-Fi network. Use this when there is no other
network around.

| Setting | Default value |
|---|---|
| Wi-Fi name (SSID) | `FieldRec` |
| Wi-Fi password | `fieldrecpass` |
| Interface address | `http://10.42.0.1:8080` |

1. On your phone/laptop, open Wi-Fi settings and join **`FieldRec`**.
2. Open a browser and go to **`http://10.42.0.1:8080`**.

> ℹ️ The SSID and password can be changed in the recorder's configuration
> (`/etc/fieldrec/fieldrec.conf`). Use whatever your unit is labelled with.

### Option B — Connect over an existing Wi-Fi network (lab/bench use)

If the Pi is joined to a normal Wi-Fi network, open:

- **`http://piste-recorder.local:8080`** (hostname), or
- **`http://<PI-IP-ADDRESS>:8080`** (find the IP in your router, or run
  `hostname -I` on the Pi).

> ⚠️ The Pi has a single Wi-Fi radio: it can run **either** its own hotspot
> **or** be a client on another network — not both at once.

### Opening the interface

When the page loads you should see the **FieldRec** title and a green
**Start / Record** button. If the page will not load, the recorder may still
be booting — wait ~30–40 seconds after power-on and refresh.

---

## 3. The Web Interface

From top to bottom, the interface is organised into cards:

| Card | Purpose |
|---|---|
| **Study Protocol** | (Optional) A list of takes/instructions loaded from a Markdown file; auto-advances after each recording. |
| **Record button** | The large button: **Start** (green) → **Recording** (red) → **Stop**. A short sync tone and a white screen flash mark the exact start. |
| **Session name** | Text appended to the recording's filename. |
| **Channels** | The list of detected microphones and their channel assignment (see §5). |
| **Disk / JACK** | Free disk space, JACK audio status, sample rate, xrun counter. |
| **Recordings** | List of finished files: download, delete, and any warnings. |
| **System** | Restart, reboot, shutdown (see §7–§9). Pinned at the very bottom. |

---

## 4. Making a Recording

1. (Optional) Enter a **session name**.
2. Confirm the microphones are assigned to channels (see §5).
3. Press the big green **Start** button.
   - A **sync tone** plays and the screen **flashes white** — this is the
     shared timing reference captured on every channel.
4. The button turns red and the timer counts up.
5. Press **Stop** when finished.
6. The file appears in the **Recordings** list. Tap **Download** to save it.

> 💡 Keep the browser tab open during a recording. Closing it does **not** stop
> the recording (the Pi keeps recording), but you lose the live timer/controls
> until you reconnect.

---

## 5. Channel Assignment (Microphone → Channel)

Because the microphones are electrically identical, FieldRec identifies each
one by its **physical position / device ID**, not by name. In the **Channels**
card, every detected microphone has a **channel dropdown** (`—`, `CH1`, `CH2`,
…).

- Pick a channel for each microphone. The assignment is **saved on the Pi** and
  **survives reboots**.
- **Auto** assigns channels `1…N` automatically in device order.
- **Clear** removes the assignment (falls back to automatic order).
- Choosing a channel another microphone already uses **swaps** the two, so no
  two microphones ever share a channel.
- Microphones set to **`—`** are **not recorded**.

### 5.1 Verifying the order is correct (Mic 1 = CH1)

Each microphone shows a **hexadecimal ID**, for example `3A129AD622BD`.

**Quick check:** the recording order is correct when, going down the list, the
**first two hex digits of each microphone ID** *and* the **assigned channel
numbers** both run in **ascending order**.

> 📷 **[Insert screenshot here: the Channels list with ascending IDs and
> channels]**

In other words — read the list top to bottom:

- Assigned channels should read `CH1, CH2, CH3, …` in order.
- The first two hex digits of the IDs (e.g. `3A…`, `4B…`, `7C…`) should also
  increase in the same order.

If both columns ascend together, **Mic 1 → CH1, Mic 2 → CH2, …** is correct.

> ⚠️ **Ignore the `system` and `jackp` entries.** These are **dummy inputs**
> that are required internally for multi-channel recording — they are **not**
> microphones and must not be assigned or counted.

---

## 6. Checking a Recording — Dead Channel Detector

After each recording, FieldRec inspects every channel and flags any that came
out **silent** (for example, a microphone that dropped off the USB bus). A
warning banner appears and the affected recording is marked in the list.

> ⚠️ **Note:** the Dead Channel Detector is currently **unreliable (buggy)**.
> Treat its warnings as an **indicator only** — not as a definitive result.
> Always confirm by listening to / inspecting the downloaded file.

---

## 7. Troubleshooting

### 7.1 The recording is silent / the sound file is muted

Work through these steps in order:

1. Scroll down to the **System** section.
2. Click **“Restart audio & backend.”**
   - This restarts the audio stack and re-detects the microphones **without
     rebooting the Pi**. Your channel assignment is preserved. The page
     reconnects automatically after a few seconds.
3. Make a short test recording and check it again.

**If the problem persists → reboot the Pi:**

4. In the **System** section, press the red **“Reboot Pi”** button.
5. You must **type `reboot`** to confirm before anything happens.
6. Wait ~40 seconds, reconnect, and try again.

### 7.2 General checklist

- **Disk** card not red (enough free space).
- **JACK** shows online; **xruns** not climbing rapidly.
- All expected microphones appear in the **Channels** list (use **Refresh** to
  re-scan).
- Microphones are on the **powered** USB hub.

---

## 8. Powering Off Safely

> 🛑 **Always shut the Pi down before removing the power supply.** Pulling power
> from a running Pi can corrupt the SD card and lose recordings.

1. Download any recordings you need first.
2. Scroll to the **System** section.
3. Press the red **“Shutdown Pi”** button.
4. **Type `shutdown`** to confirm.
5. Wait until activity stops (LED off), then remove power.

---

## 9. System Panel Reference

The **System** card is pinned at the very bottom of the interface. Destructive
actions require confirmation so they cannot be triggered by accident.

| Button | What it does | Confirmation |
|---|---|---|
| **Restart audio & backend** (amber) | Restarts the audio stack + web backend; re-detects mics; **no reboot**. Assignment kept. | Single click-confirm dialog |
| **Reboot Pi** (red) | Full restart of the Raspberry Pi (~40 s). | Type **`reboot`** |
| **Shutdown Pi** (red) | Powers the Pi off so it is safe to unplug. | Type **`shutdown`** |

---

## 10. Quick Reference

| I want to… | Do this |
|---|---|
| Connect in the field | Join Wi-Fi **`FieldRec`** → open **`http://10.42.0.1:8080`** |
| Connect on a network | Open **`http://piste-recorder.local:8080`** |
| Record | Enter session name → **Start** → **Stop** → **Download** |
| Assign mics to channels | **Channels** card → set each dropdown, or **Auto** |
| Verify order (Mic 1 = CH1) | First 2 hex digits of IDs **and** channels both ascend (ignore `system`/`jackp`) |
| Fix a silent recording | **System → Restart audio & backend**, retry; if it persists, **Reboot Pi** (type `reboot`) |
| Power off | **System → Shutdown Pi** (type `shutdown`), then unplug |

---

*FieldRec operating guide. Values shown (SSID, password, hostname, address) are
defaults — use the ones your unit is labelled with.*
