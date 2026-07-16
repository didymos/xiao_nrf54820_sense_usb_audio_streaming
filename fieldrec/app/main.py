"""
app/main.py — FieldRec FastAPI controller

Config: /etc/fieldrec/fieldrec.conf  (shell key=value format)
Environment:
    FIELDREC_MOCK=1   — stub all external commands (full state machine still runs)
    FIELDREC_CONF     — override config path
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import numpy as np
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("fieldrec")

# ── Constants ─────────────────────────────────────────────────────────────────
CONF_PATH = os.environ.get("FIELDREC_CONF", "/etc/fieldrec/fieldrec.conf")
MOCK_MODE = os.environ.get("FIELDREC_MOCK", "0").strip() in ("1", "true", "yes")
APP_DIR = Path(__file__).parent
STATIC_DIR = APP_DIR / "static"

if MOCK_MODE:
    log.info("MOCK MODE ENABLED — external commands are stubbed")


# ── Config loader ─────────────────────────────────────────────────────────────
def load_config(path: str = CONF_PATH) -> dict[str, str]:
    """Parse shell key=value config, strip surrounding quotes."""
    cfg: dict[str, str] = {}
    try:
        with open(path) as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                # Strip surrounding single or double quotes
                if len(val) >= 2 and val[0] in ('"', "'") and val[-1] == val[0]:
                    val = val[1:-1]
                cfg[key] = val
    except FileNotFoundError:
        log.warning("Config not found: %s — using defaults", path)
    return cfg


_MOCK_DEFAULTS: dict[str, str] = {
    "CHANNELS": "4",
    "JACK_PORTS": "Mic:capture_1 Mic_1:capture_1 Mic_2:capture_1 Mic_3:capture_1",
    "MIC_PORTS": "3-1.1 3-1.2 3-1.3 3-1.4",
    "SAMPLE_RATE": "16000",
    "BIT_DEPTH": "16",
    "COUNTDOWN_BEEPS": "0",
    "MIN_FREE_MB": "500",
    "OUT_DEVICE": "plughw:CARD=Device",
}

_cfg: Optional[dict[str, str]] = None
_cfg_lock = threading.Lock()


def get_cfg() -> dict[str, str]:
    global _cfg
    with _cfg_lock:
        if _cfg is None:
            loaded = load_config()
            if MOCK_MODE:
                _cfg = {**_MOCK_DEFAULTS, **loaded}
            else:
                _cfg = loaded
        return dict(_cfg)


def cfg(key: str, default: str = "") -> str:
    return get_cfg().get(key, default)


# ── Mock infrastructure ───────────────────────────────────────────────────────
_mock_tmpdir: Optional[str] = None
_mock_tmpdir_lock = threading.Lock()


def _get_mock_tmpdir() -> str:
    global _mock_tmpdir
    with _mock_tmpdir_lock:
        if _mock_tmpdir is None:
            _mock_tmpdir = tempfile.mkdtemp(prefix="fieldrec_mock_")
            log.info("MOCK: tmpdir=%s", _mock_tmpdir)
    return _mock_tmpdir


def _mock_recordings_dir() -> str:
    d = os.path.join(_get_mock_tmpdir(), "recordings")
    os.makedirs(d, exist_ok=True)
    return d


def _write_silent_wav(path: str, duration_ms: int = 200, sr: int = 48000) -> None:
    n_frames = sr * duration_ms // 1000
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(b"\x00\x00" * n_frames)


# A channel whose peak is below this is treated as dead (dropped mic / no signal).
# A live XIAO mic in a quiet room still reads ~1e-3; a dropped port reads 0.
_SILENCE_PEAK = 5e-4


def _wav_channel_peaks(path: str) -> Optional[list[float]]:
    """Per-channel peak amplitude (0.0..1.0) of a PCM WAV, or None if unreadable.
    Handles standard PCM and WAVE_FORMAT_EXTENSIBLE (multichannel), 16- and 24-bit.
    Python's `wave` module can't read jack_capture's >2ch WAVEX files, so we parse
    the RIFF chunks directly."""
    import struct
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except Exception:
        return None
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    fmt = audio = None
    pos = 12
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        sz = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        chunk = data[pos + 8:pos + 8 + sz]
        if cid == b"fmt ":
            fmt = chunk
        elif cid == b"data":
            audio = chunk
        pos += 8 + sz + (sz & 1)
    if not fmt or audio is None or len(fmt) < 16:
        return None
    tag = struct.unpack("<H", fmt[0:2])[0]
    ch = struct.unpack("<H", fmt[2:4])[0]
    bits = struct.unpack("<H", fmt[14:16])[0]
    if tag == 0xFFFE and len(fmt) >= 26:          # WAVE_FORMAT_EXTENSIBLE
        tag = struct.unpack("<H", fmt[24:26])[0]  # real format from SubFormat
    if ch < 1 or tag != 1:                         # integer PCM only
        return None
    try:
        if bits == 16:
            usable = (len(audio) // (2 * ch)) * (2 * ch)
            arr = np.frombuffer(audio[:usable], dtype="<i2").reshape(-1, ch)
            scale = 32768.0
        elif bits == 24:
            usable = (len(audio) // (3 * ch)) * (3 * ch)
            b = np.frombuffer(audio[:usable], dtype=np.uint8).reshape(-1, 3)
            val = (b[:, 0].astype(np.int32)
                   | (b[:, 1].astype(np.int32) << 8)
                   | (b[:, 2].astype(np.int32) << 16))
            val = np.where(val & 0x800000, val - 0x1000000, val)
            arr = val.reshape(-1, ch)
            scale = 8388608.0
        else:
            return None
        if arr.shape[0] == 0:
            return [0.0] * ch
        peaks = np.max(np.abs(arr.astype(np.float32)), axis=0) / scale
        return [round(float(p), 5) for p in peaks]
    except Exception:
        return None


# ── Tone generation ───────────────────────────────────────────────────────────
# Each entry is a list of (freq_hz, duration_s) segments played in sequence.
_TONE_SPECS: dict[str, list[tuple[float, float]]] = {
    "go":    [(1320.0, 0.70)],
    "stop":  [(880.0,  0.40)],
    "saved": [(880.0,  0.15), (1100.0, 0.15)],
    "error": [(300.0,  0.15), (200.0,  0.15)],
    **{f"countdown_{i}": [(660.0 + i * 110.0, 0.20)] for i in range(8)},
}

_TONE_SR = 48000  # Hz for output device


def _generate_tone_pcm(freq: float, duration_s: float, sr: int = _TONE_SR) -> bytes:
    n = max(1, int(sr * duration_s))
    t = np.linspace(0.0, duration_s, n, endpoint=False)
    wave_arr = np.sin(2.0 * np.pi * freq * t) * 0.65
    # 10ms fade-in/out to avoid clicks
    fade = min(int(sr * 0.010), n // 4)
    if fade > 0:
        wave_arr[:fade] *= np.linspace(0.0, 1.0, fade)
        wave_arr[n - fade:] *= np.linspace(1.0, 0.0, fade)
    return (wave_arr * 32767.0).astype(np.int16).tobytes()


def play_tone_sync(name: str) -> None:
    """Generate and play a named tone via aplay. Blocking."""
    specs = _TONE_SPECS.get(name)
    if not specs:
        log.warning("Unknown tone: %s", name)
        return
    if MOCK_MODE:
        total = sum(d for _, d in specs)
        log.info("MOCK tone: %s (%.2fs total)", name, total)
        time.sleep(min(0.05, total))
        return
    out_device = cfg("OUT_DEVICE", "plughw:0")
    for freq, dur in specs:
        pcm = _generate_tone_pcm(freq, dur)
        try:
            subprocess.run(
                ["aplay", "-D", out_device,
                 "-r", str(_TONE_SR), "-c", "1", "-f", "S16_LE", "-"],
                input=pcm,
                capture_output=True,
                timeout=dur + 5.0,
            )
        except Exception as exc:
            log.warning("aplay failed (tone=%s freq=%.0f Hz): %s", name, freq, exc)


def play_tone_bg(tone_name: str) -> None:
    """Play a named tone in a background daemon thread."""
    t = threading.Thread(target=play_tone_sync, args=(tone_name,), daemon=True)
    t.start()


def _get_recordings_dir() -> str:
    if MOCK_MODE:
        return _mock_recordings_dir()
    return cfg("RECORDINGS_DIR", "/mnt/ssd/recordings")


def _safe_filename(name: str) -> str:
    """Sanitize session name to safe filesystem characters."""
    name = re.sub(r"[^\w\-\.]", "_", name)
    name = name.strip("._")
    return (name[:80] if name else "session")


def _path_traversal_check(base: str, filename: str) -> Path:
    """Verify filename resolves inside base dir. Raises HTTPException on attack."""
    bare = Path(filename).name
    if not bare:
        raise HTTPException(status_code=400, detail="Invalid filename")
    safe = Path(base).resolve()
    target = (safe / bare).resolve()
    if not str(target).startswith(str(safe) + os.sep) and target != safe:
        raise HTTPException(status_code=400, detail="Invalid filename")
    return target


# ── External command wrappers ─────────────────────────────────────────────────
def _run(cmd: list[str], timeout: int = 10) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
    log.debug("run: %s", " ".join(shlex.quote(c) for c in cmd))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def jack_alive() -> bool:
    if MOCK_MODE:
        return True
    try:
        r = _run(["jack_lsp"], timeout=3)
        return r.returncode == 0
    except Exception:
        return False


def jack_lsp() -> list[str]:
    if MOCK_MODE:
        ports = cfg("JACK_PORTS", "system:capture_1")
        return ports.split()
    try:
        r = _run(["jack_lsp"], timeout=3)
        if r.returncode == 0:
            return [l.strip() for l in r.stdout.splitlines() if l.strip()]
        return []
    except Exception:
        return []


# Cache jack_lsp calls to keep /api/status fast
_lsp_cache: tuple[float, list[str]] = (0.0, [])
_lsp_cache_ttl = 2.0


def cached_jack_lsp() -> list[str]:
    global _lsp_cache
    ts, ports = _lsp_cache
    if time.time() - ts < _lsp_cache_ttl:
        return ports
    ports = jack_lsp()
    _lsp_cache = (time.time(), ports)
    return ports


_xrun_cache: tuple[float, int] = (0.0, 0)


def get_xrun_count() -> int:
    global _xrun_cache
    if MOCK_MODE:
        return 0
    ts, val = _xrun_cache
    if time.time() - ts < 15:
        return val
    try:
        r = subprocess.run(
            ["journalctl", "-u", "audio-sync", "--boot", "-o", "cat", "--no-pager"],
            capture_output=True, text=True, timeout=5,
        )
        val = r.stdout.lower().count("xrun") if r.returncode == 0 else -1
    except Exception:
        val = -1
    _xrun_cache = (time.time(), val)
    return val


def get_disk_stats(path: str) -> tuple[float, float]:
    try:
        stat = shutil.disk_usage(path)
        return stat.free / 1024 / 1024, stat.total / 1024 / 1024
    except Exception:
        return 0.0, 0.0


# ── Mic selection ─────────────────────────────────────────────────────────────
# Empty list means "use the auto-detected mic default". Set via /api/mics/select.
_mic_selection: dict[str, Any] = {"ports": []}
_mic_selection_lock = threading.Lock()

# JACK client names for XIAO mics look like "Mic", "Mic_1", "Mic_2", …
_MIC_NAME_RE = re.compile(r"^Mic(_\d+)?$", re.IGNORECASE)


def _natural_key(s: str) -> tuple[Any, ...]:
    """Numeric-aware sort key: '3-1.2' < '3-1.10', 'Mic' < 'Mic_1'."""
    return tuple(
        int(tok) if tok.isdigit() else tok
        for tok in re.split(r"(\d+)", s)
    )


def _read_card_attr(card_dir: Path) -> tuple[str, str, str]:
    """Return (sanitized_card_id, usb_port_token, usb_serial) for one sound card.
    The card's `device` symlink points at the USB *interface* (…/3-1.1:1.0); the
    USB port token is that basename's prefix, and the serial lives on the parent
    USB *device* dir (…/3-1.1/serial). Any field may be "" if unavailable."""
    try:
        card_id = (card_dir / "id").read_text().strip()
    except Exception:
        return "", "", ""
    safe = re.sub(r"[^A-Za-z0-9_]", "_", card_id)
    port = serial = ""
    try:
        iface = os.path.realpath(str(card_dir / "device"))   # …/3-1.1/3-1.1:1.0
        port = os.path.basename(iface).split(":", 1)[0]       # 3-1.1
        sp = os.path.join(os.path.dirname(iface), "serial")   # …/3-1.1/serial
        if os.path.exists(sp):
            with open(sp) as fh:
                serial = fh.read().strip()
    except Exception:
        pass
    return safe, port, serial


def _usb_ports_by_cardid() -> dict[str, str]:
    """Map each sanitized ALSA card id to its stable USB port token (e.g. "3-1.1")."""
    result: dict[str, str] = {}
    try:
        for entry in sorted(Path("/sys/class/sound").glob("card[0-9]*")):
            cid, port, _ = _read_card_attr(entry)
            if cid and port:
                result[cid] = port
    except Exception:
        pass
    return result


def _usb_serials_by_cardid() -> dict[str, str]:
    """Map each sanitized ALSA card id to its USB serial number (e.g.
    "XIAO-MIC-003"). This is the per-physical-device identity: it stays with the
    board no matter which hub socket it is plugged into."""
    result: dict[str, str] = {}
    try:
        for entry in sorted(Path("/sys/class/sound").glob("card[0-9]*")):
            cid, _, serial = _read_card_attr(entry)
            if cid and serial:
                result[cid] = serial
    except Exception:
        pass
    return result


def _channel_sort_key(
    port: str, serials: dict[str, str], usb: dict[str, str]
) -> tuple[Any, ...]:
    """Channel ordering key. Primary: USB serial number (per-device identity, so
    XIAO-MIC-001 → ch1 … regardless of socket). Fallback for a board with no
    serial: USB hub socket, then the JACK client name. Serialed devices always
    sort ahead of non-serialed ones so the numbered boards take the low channels."""
    client = port.split(":", 1)[0]
    ser = serials.get(client)
    if ser:
        return (0,) + _natural_key(ser)
    token = usb.get(client)
    if token:
        return (1,) + _natural_key(token)
    return (2,) + _natural_key(port)


def sort_ports_for_channels(ports: list[str]) -> list[str]:
    """Sort JACK capture ports into stable channel order: by USB serial number,
    falling back to hub socket then name."""
    if MOCK_MODE:
        serials, usb = {}, {}
    else:
        serials, usb = _usb_serials_by_cardid(), _usb_ports_by_cardid()
    return sorted(ports, key=lambda p: _channel_sort_key(p, serials, usb))


def default_mic_ports() -> list[str]:
    """Live capture ports whose JACK client is named like a mic (Mic, Mic_1, …),
    ordered by USB serial number (per-device identity). This is the default
    recording selection when the user hasn't picked channels explicitly."""
    mics = [
        p for p in cached_jack_lsp()
        if "capture" in p.lower() and _MIC_NAME_RE.match(p.split(":", 1)[0])
    ]
    return sort_ports_for_channels(mics)


def get_active_ports() -> list[str]:
    """Return the active recording port list. Falls back to the auto-detected
    mic default (all live Mic* capture ports, ordered by hub socket)."""
    with _mic_selection_lock:
        sel = _mic_selection["ports"]
        if sel:
            return list(sel)
    return default_mic_ports()


# ── Study protocol ────────────────────────────────────────────────────────────
_protocol_state: dict[str, Any] = {"entries": [], "index": 0}
_protocol_lock = threading.Lock()


def parse_protocol(text: str) -> list[dict[str, Any]]:
    """
    Parse a Markdown study protocol into a list of recording entries.

    Format:
        ## Title of recording
        Description / instructions for this recording.
        Multiple lines are fine.

        ## Next recording
        Next instructions.

    Returns list of {"number", "suffix", "title", "description"}.
    """
    entries: list[dict[str, Any]] = []
    current_heading: Optional[str] = None
    current_desc: list[str] = []

    for line in text.splitlines():
        if line.startswith("## "):
            if current_heading is not None:
                entries.append({
                    "number": len(entries) + 1,
                    "suffix": _safe_filename(current_heading),
                    "title": current_heading,
                    "description": "\n".join(current_desc).strip(),
                })
            current_heading = line[3:].strip()
            current_desc = []
        elif current_heading is not None and not line.startswith("# "):
            current_desc.append(line)

    if current_heading is not None:
        entries.append({
            "number": len(entries) + 1,
            "suffix": _safe_filename(current_heading),
            "title": current_heading,
            "description": "\n".join(current_desc).strip(),
        })
    return entries


def _get_protocol_snapshot() -> dict[str, Any]:
    with _protocol_lock:
        entries = _protocol_state["entries"]
        idx = _protocol_state["index"]
        if not entries:
            return {"entry": None, "current_index": 0, "total": 0}
        entry = entries[idx] if idx < len(entries) else None
        return {"entry": entry, "current_index": idx, "total": len(entries)}


# ── State machine ─────────────────────────────────────────────────────────────
class State(str, Enum):
    IDLE      = "IDLE"
    ARMING    = "ARMING"
    RECORDING = "RECORDING"
    STOPPING  = "STOPPING"
    ERROR     = "ERROR"


class RecorderController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = State.IDLE
        self._error_msg: Optional[str] = None
        self._current_file: Optional[str] = None     # basename only
        self._current_path: Optional[Path] = None    # full path
        self._start_time: Optional[float] = None
        self._recording_start_time: Optional[float] = None
        self._go_tone_time: Optional[float] = None
        self._proc: Optional[subprocess.Popen] = None  # type: ignore[type-arg]

    # ── State helpers ─────────────────────────────────────────────────────────

    def _set_state(self, s: State, error: str = "") -> None:
        self._state = s
        if s == State.ERROR:
            self._error_msg = error
            log.error("State -> ERROR: %s", error)
        else:
            if s == State.IDLE:
                self._error_msg = None
            log.info("State -> %s", s.value)

    def _get_state_snapshot(self) -> tuple[State, Optional[str], Optional[str], Optional[float], Optional[float]]:
        with self._lock:
            return (
                self._state,
                self._error_msg,
                self._current_file,
                self._recording_start_time or self._start_time,
                time.time(),
            )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _build_capture_cmd(self, out_path: str) -> list[str]:
        """
        Build jack_capture command per constraints:
        - duration via SIGINT (no -d flag)
        - -f wav only for <=2 channels; omit for >2 (auto-selects wavex)
        - no --duration flag
        """
        ports = get_active_ports()
        channels = len(ports)
        bit_depth = cfg("BIT_DEPTH", "16")

        cmd = ["jack_capture", "-b", bit_depth]

        # Only add -f wav for mono/stereo; for >2ch omit (auto wavex)
        if channels <= 2:
            cmd += ["-f", "wav"]

        for p in ports:
            cmd += ["-p", p]

        cmd.append(out_path)
        return cmd

    def _pre_flight(self) -> Optional[str]:
        """Return error string if not ready to record, else None."""
        if not jack_alive():
            return "JACK server is not running"
        active = set(cached_jack_lsp())
        recording = get_active_ports()
        if not recording:
            return "No recording channels selected"
        missing = [p for p in recording if p not in active]
        if missing:
            return f"JACK ports not live: {missing}"
        rec_dir = _get_recordings_dir()
        os.makedirs(rec_dir, exist_ok=True)
        free_mb, _ = get_disk_stats(rec_dir)
        min_free = float(cfg("MIN_FREE_MB", "500"))
        if free_mb < min_free:
            return f"Disk too full: {free_mb:.0f} MB free (need {min_free:.0f} MB)"
        return None

    def _monitor_proc(self, proc: subprocess.Popen) -> None:  # type: ignore[type-arg]
        """Watch jack_capture; detect unexpected exit."""
        proc.wait()
        with self._lock:
            if self._state == State.RECORDING:
                stderr = b""
                try:
                    if proc.stderr:
                        stderr = proc.stderr.read()
                except Exception:
                    pass
                err_str = stderr.decode(errors="replace")[:300] if stderr else "unknown"
                self._set_state(State.ERROR, f"jack_capture exited unexpectedly: {err_str}")
                self._proc = None
        play_tone_bg("error")

    # ── Public start/stop ─────────────────────────────────────────────────────

    def start_sync(self, session: str = "take") -> str:
        """Block until jack_capture is confirmed running (~0.5 s).
        Returns the output filename. Raises HTTPException on any failure.
        Called from a sync FastAPI route (thread-pool) — safe to block."""
        safe_session = _safe_filename(session) if session else "take"

        with self._lock:
            if self._state not in (State.IDLE, State.ERROR):
                raise HTTPException(409, f"Cannot start: state is {self._state.value}")
            self._start_time = time.time()
            self._go_tone_time = None
            self._current_file = None
            self._current_path = None
            self._set_state(State.ARMING)

        try:
            err = self._pre_flight()
            if err:
                with self._lock:
                    self._set_state(State.ERROR, f"Pre-flight: {err}")
                raise HTTPException(422, f"Pre-flight check failed: {err}")

            ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            rec_dir = _get_recordings_dir()
            os.makedirs(rec_dir, exist_ok=True)
            out_filename = f"{ts}_{safe_session}.wav"
            out_path = os.path.join(rec_dir, out_filename)

            if MOCK_MODE:
                _write_silent_wav(out_path, duration_ms=1000)
                proc = None
            else:
                cmd = self._build_capture_cmd(out_path)
                log.info("jack_capture: %s", " ".join(shlex.quote(c) for c in cmd))
                # Hold stdin open so jack_capture doesn't get EOF under systemd
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                time.sleep(0.5)
                if proc.poll() is not None:
                    stderr_bytes = b""
                    try:
                        stderr_bytes = proc.stderr.read() if proc.stderr else b""
                    except Exception:
                        pass
                    err_detail = stderr_bytes.decode(errors="replace")[:200]
                    with self._lock:
                        self._set_state(
                            State.ERROR,
                            f"jack_capture failed to start (exit={proc.returncode}): {err_detail}",
                        )
                    raise HTTPException(
                        500,
                        f"jack_capture failed to start (exit={proc.returncode}): {err_detail}",
                    )
                threading.Thread(
                    target=self._monitor_proc,
                    args=(proc,),
                    daemon=True,
                ).start()

            go_time = time.time()

            with self._lock:
                self._proc = proc
                self._current_file = out_filename
                self._current_path = Path(out_path)
                self._go_tone_time = go_time
                self._recording_start_time = time.time()
                self._set_state(State.RECORDING)

            # Fallback go-tone on OUT_DEVICE for headless/non-browser operation
            play_tone_bg("go")

            return out_filename

        except HTTPException:
            raise
        except Exception as exc:
            log.exception("Start failed: %s", exc)
            with self._lock:
                self._set_state(State.ERROR, str(exc))
                self._proc = None
            play_tone_bg("error")
            raise HTTPException(500, f"Recording start failed: {exc}")

    def stop(self) -> None:
        with self._lock:
            if self._state != State.RECORDING:
                raise HTTPException(409, f"Cannot stop: state is {self._state.value}")
            self._set_state(State.STOPPING)
            proc = self._proc
            out_path = self._current_path
            start_time = self._recording_start_time or self._start_time
            go_tone_time = self._go_tone_time

        threading.Thread(
            target=self._stop_sequence,
            args=(proc, out_path, start_time, go_tone_time),
            daemon=True,
        ).start()

    def _stop_sequence(
        self,
        proc: Optional[subprocess.Popen],  # type: ignore[type-arg]
        out_path: Optional[Path],
        start_time: Optional[float],
        go_tone_time: Optional[float],
    ) -> None:
        try:
            if proc is not None and proc.poll() is None:
                log.info("Sending SIGINT to jack_capture (pid=%s)", proc.pid)
                proc.send_signal(signal.SIGINT)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    log.warning("jack_capture timeout after SIGINT — SIGTERM")
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()

            play_tone_sync("stop")

            try:
                os.sync()
            except Exception:
                pass

            if MOCK_MODE:
                pass
            elif out_path is None or not out_path.exists() or out_path.stat().st_size == 0:
                raise RuntimeError(f"Output file missing or empty: {out_path}")

            if out_path is not None:
                go_offset = (
                    (go_tone_time - start_time)
                    if go_tone_time is not None and start_time is not None
                    else None
                )
                # Capture current protocol entry before advancing
                proto = _get_protocol_snapshot()
                recorded_ports = get_active_ports()
                # Document the physical device (USB serial) and hub socket behind
                # each channel so the recording is self-describing:
                # channel N ← serial ← usb port.
                usb_lookup = {} if MOCK_MODE else _usb_ports_by_cardid()
                ser_lookup = {} if MOCK_MODE else _usb_serials_by_cardid()
                usb_ports = [
                    usb_lookup.get(p.split(":", 1)[0], "") for p in recorded_ports
                ]
                serials = [
                    ser_lookup.get(p.split(":", 1)[0], "") for p in recorded_ports
                ]
                # Post-record check: per-channel peak level. A dropped mic (e.g.
                # a USB device that fell off the bus mid-session) records as a
                # dead, silent channel — flag those so the UI can warn.
                peaks = None if MOCK_MODE else _wav_channel_peaks(str(out_path))
                silent_channels: list[dict[str, Any]] = []
                if peaks:
                    for i, pk in enumerate(peaks):
                        if pk < _SILENCE_PEAK:
                            silent_channels.append({
                                "channel": i + 1,
                                "jack_port": recorded_ports[i] if i < len(recorded_ports) else "",
                                "usb_port": usb_ports[i] if i < len(usb_ports) else "",
                                "serial": serials[i] if i < len(serials) else "",
                            })
                    if silent_channels:
                        log.warning(
                            "Silent channel(s) in %s: %s",
                            out_path.name,
                            [c["channel"] for c in silent_channels],
                        )
                sidecar: dict[str, Any] = {
                    "file": out_path.name,
                    "start_time_utc": (
                        datetime.fromtimestamp(start_time, tz=timezone.utc).isoformat()
                        if start_time else None
                    ),
                    "go_tone_utc": (
                        datetime.fromtimestamp(go_tone_time, tz=timezone.utc).isoformat()
                        if go_tone_time else None
                    ),
                    "go_tone_offset_s": round(go_offset, 4) if go_offset is not None else None,
                    "sample_rate": cfg("SAMPLE_RATE", "48000"),
                    "channels": len(recorded_ports),
                    "jack_ports": recorded_ports,
                    "usb_ports": usb_ports,
                    "serials": serials,
                    "channel_peaks": peaks,
                    "silent_channels": silent_channels,
                    "bit_depth": cfg("BIT_DEPTH", "16"),
                    "downloaded": False,
                }
                if proto["entry"]:
                    sidecar["protocol_entry"] = proto["entry"]
                sidecar_path = out_path.with_suffix(".json")
                sidecar_path.write_text(json.dumps(sidecar, indent=2))
                log.info("Sidecar: %s", sidecar_path)

            play_tone_sync("saved")

            # Auto-advance protocol to the next entry
            with _protocol_lock:
                entries = _protocol_state["entries"]
                idx = _protocol_state["index"]
                if entries and idx < len(entries) - 1:
                    _protocol_state["index"] = idx + 1
                    log.info(
                        "Protocol advanced to entry %d/%d",
                        _protocol_state["index"] + 1,
                        len(entries),
                    )

            with self._lock:
                self._set_state(State.IDLE)
                self._proc = None
                self._current_file = None
                self._current_path = None
                self._start_time = None
                self._recording_start_time = None
                self._go_tone_time = None

        except Exception as exc:
            log.exception("Stop sequence failed: %s", exc)
            if out_path and out_path.exists():
                failed = out_path.with_name(out_path.name + ".FAILED")
                try:
                    out_path.rename(failed)
                    log.error("Partial file kept: %s", failed)
                except Exception:
                    pass
            with self._lock:
                self._set_state(State.ERROR, str(exc))
                self._proc = None
                self._current_file = None
                self._current_path = None
            play_tone_bg("error")

    def reset(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.send_signal(signal.SIGINT)
                    self._proc.wait(timeout=3)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
            self._proc = None
            self._current_file = None
            self._current_path = None
            self._start_time = None
            self._recording_start_time = None
            self._go_tone_time = None
            self._set_state(State.IDLE)

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            state = self._state
            now = time.time()
            if state == State.RECORDING and self._recording_start_time:
                elapsed = now - self._recording_start_time
            elif self._start_time:
                elapsed = now - self._start_time
            else:
                elapsed = 0.0
            current_file = self._current_file
            error = self._error_msg

        conf = get_cfg()
        all_lsp = cached_jack_lsp()
        active_ports = set(all_lsp)
        is_jack_alive = bool(active_ports) or MOCK_MODE

        recording_ports = get_active_ports()
        recording_ports_set = set(recording_ports)

        # Map each live capture port to its USB serial (device identity) and
        # port token (hub socket) for display.
        usb_lookup = {} if MOCK_MODE else _usb_ports_by_cardid()
        ser_lookup = {} if MOCK_MODE else _usb_serials_by_cardid()

        # All JACK capture ports visible right now, ordered by USB serial so the
        # UI rows and channel numbers follow per-device identity.
        available_capture = sort_ports_for_channels(
            [p for p in active_ports if "capture" in p.lower()]
        )
        if MOCK_MODE and not available_capture:
            available_capture = recording_ports

        usb_by_jack: dict[str, str] = {}
        serial_by_jack: dict[str, str] = {}
        for jp in available_capture:
            client = jp.split(":", 1)[0]
            if client in usb_lookup:
                usb_by_jack[jp] = usb_lookup[client]
            if client in ser_lookup:
                serial_by_jack[jp] = ser_lookup[client]

        # Mic list derived from live ports (no dependency on config MIC_PORTS)
        mics: list[dict[str, Any]] = []
        for i, jack_port in enumerate(available_capture, 1):
            ch = (recording_ports.index(jack_port) + 1) if jack_port in recording_ports_set else None
            mics.append({
                "index": i,
                "usb_port": usb_by_jack.get(jack_port, ""),
                "serial": serial_by_jack.get(jack_port, ""),
                "jack_port": jack_port,
                "present": True,
                "recording": jack_port in recording_ports_set,
                "channel": ch,
            })

        rec_dir = _get_recordings_dir()
        free_mb, total_mb = get_disk_stats(rec_dir if os.path.exists(rec_dir) else "/tmp")

        return {
            "state": state.value,
            "elapsed_seconds": round(elapsed, 1),
            "current_file": current_file,
            "disk_free_mb": round(free_mb, 1),
            "disk_total_mb": round(total_mb, 1),
            "mics": mics,
            "jack_alive": is_jack_alive,
            "sample_rate": int(conf.get("SAMPLE_RATE", "48000")),
            "xruns": get_xrun_count(),
            "pi_time": datetime.now(tz=timezone.utc).isoformat(),
            "error": error,
            "countdown_beeps": int(conf.get("COUNTDOWN_BEEPS", "3")),
            "mock": MOCK_MODE,
            "protocol": _get_protocol_snapshot(),
            "recording_ports": recording_ports,
            "available_capture_ports": available_capture,
            "usb_by_jack": usb_by_jack,
            "serial_by_jack": serial_by_jack,
        }


# ── Singleton controller ───────────────────────────────────────────────────────
controller = RecorderController()


# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(title="FieldRec", version="1.0.0", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_cache_api(request: Request, call_next):  # type: ignore[no-untyped-def]
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


# ── Static / root ──────────────────────────────────────────────────────────────
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def root() -> FileResponse:  # type: ignore[return]
        return FileResponse(str(STATIC_DIR / "index.html"))
else:
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def root_fallback() -> HTMLResponse:
        return HTMLResponse("<h1>FieldRec</h1><p>Static files not found.</p>")


# ── API endpoints ──────────────────────────────────────────────────────────────
@app.get("/manifest.json", include_in_schema=False)
async def web_manifest():
    from fastapi.responses import JSONResponse
    return JSONResponse({
        "name": "FieldRec",
        "short_name": "FieldRec",
        "description": "Multi-channel field audio recorder",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#111111",
        "theme_color": "#111111",
        "orientation": "portrait",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    })


@app.get("/api/health")
async def api_health() -> dict[str, Any]:
    ports = set(cached_jack_lsp())
    is_jack = bool(ports) or MOCK_MODE
    rec_dir = _get_recordings_dir()
    free_mb, _ = get_disk_stats(rec_dir if os.path.exists(rec_dir) else "/tmp")
    return {
        "ok": is_jack and free_mb >= float(cfg("MIN_FREE_MB", "500")),
        "jack": is_jack,
        "disk_free_mb": round(free_mb, 1),
        "mock": MOCK_MODE,
        "version": "1.0.0",
    }


@app.post("/api/time")
async def api_time(request: Request) -> dict[str, Any]:
    """Accept client timestamp; optionally sync Pi clock."""
    try:
        body = await request.json()
        client_ms = int(body.get("client_ms", 0))
    except Exception:
        client_ms = 0

    server_ms = int(time.time() * 1000)
    drift_ms = server_ms - client_ms if client_ms else 0

    if abs(drift_ms) > 5000 and client_ms > 0 and not MOCK_MODE:
        try:
            epoch_s = client_ms / 1000.0
            subprocess.run(
                ["sudo", "/usr/bin/date", "-s", f"@{epoch_s:.3f}"],
                capture_output=True, timeout=5,
            )
            log.info("Clock adjusted by %d ms", drift_ms)
        except Exception as exc:
            log.warning("Clock sync failed: %s", exc)

    return {
        "server_ms": server_ms,
        "drift_ms": drift_ms,
        "pi_time": datetime.now(tz=timezone.utc).isoformat(),
    }


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    return controller.get_status()


class StartReq(BaseModel):
    session: Optional[str] = "take"


@app.post("/api/start")
def api_start(req: StartReq) -> dict[str, Any]:
    """Synchronous route (runs in thread pool). Blocks ~0.5 s until
    jack_capture is confirmed running. Returns state=RECORDING on success."""
    session = (req.session or "take").strip() or "take"
    filename = controller.start_sync(session)
    return {"state": "RECORDING", "file": filename, "session": session}


@app.post("/api/stop")
async def api_stop() -> dict[str, str]:
    controller.stop()
    return {"status": "stopping"}


@app.post("/api/reset")
async def api_reset() -> dict:
    controller.reset()
    return controller.get_status()


# ── Mic selection endpoints ───────────────────────────────────────────────────

@app.get("/api/mics")
async def api_mics() -> dict[str, Any]:
    available = sorted(p for p in cached_jack_lsp() if "capture" in p.lower())
    if MOCK_MODE and not available:
        available = get_active_ports()
    selected = get_active_ports()
    return {"available": available, "selected": selected, "channels": len(selected)}


class MicSelectReq(BaseModel):
    ports: list[str]


@app.post("/api/mics/select")
async def api_mics_select(req: MicSelectReq) -> dict[str, Any]:
    if not req.ports:
        raise HTTPException(status_code=400, detail="Select at least one port")
    if not MOCK_MODE:
        available = set(cached_jack_lsp())
        invalid = [p for p in req.ports if p not in available]
        if invalid:
            raise HTTPException(status_code=400, detail=f"Ports not in JACK: {invalid}")
    with _mic_selection_lock:
        _mic_selection["ports"] = list(req.ports)
    log.info("Mic selection: %s", req.ports)
    return {"selected": req.ports, "channels": len(req.ports)}


@app.delete("/api/mics/select")
async def api_mics_reset() -> dict[str, Any]:
    """Reset to the auto-detected mic default (all live Mic* capture ports)."""
    with _mic_selection_lock:
        _mic_selection["ports"] = []
    default = default_mic_ports()
    log.info("Mic selection reset to auto default: %s", default)
    return {"selected": default, "channels": len(default)}


# ── Protocol endpoints ────────────────────────────────────────────────────────

@app.post("/api/protocol")
async def api_protocol_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    """Upload a Markdown study protocol. Parses ## headings as recording entries."""
    content = await file.read()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("latin-1", errors="replace")
    entries = parse_protocol(text)
    if not entries:
        raise HTTPException(status_code=400, detail="No ## entries found in protocol file")
    with _protocol_lock:
        _protocol_state["entries"] = entries
        _protocol_state["index"] = 0
    log.info("Protocol loaded: %d entries from '%s'", len(entries), file.filename)
    return {"entries": entries, "current_index": 0, "total": len(entries)}


@app.get("/api/protocol/current")
async def api_protocol_current() -> dict[str, Any]:
    return _get_protocol_snapshot()


@app.post("/api/protocol/advance")
async def api_protocol_advance() -> dict[str, Any]:
    with _protocol_lock:
        entries = _protocol_state["entries"]
        idx = _protocol_state["index"]
        if entries and idx < len(entries) - 1:
            _protocol_state["index"] = idx + 1
        return _get_protocol_snapshot()


@app.post("/api/protocol/reset")
async def api_protocol_reset() -> dict[str, Any]:
    with _protocol_lock:
        _protocol_state["index"] = 0
    return _get_protocol_snapshot()


@app.delete("/api/protocol")
async def api_protocol_clear() -> dict[str, Any]:
    with _protocol_lock:
        _protocol_state["entries"] = []
        _protocol_state["index"] = 0
    return {"entries": [], "current_index": 0, "total": 0}


# ── Recordings endpoints ───────────────────────────────────────────────────────

def _read_sidecar(wav_path: Path) -> dict[str, Any]:
    sc = wav_path.with_suffix(".json")
    if sc.exists():
        try:
            return json.loads(sc.read_text())
        except Exception:
            pass
    return {}


def _recording_info(f: Path) -> dict[str, Any]:
    sc = _read_sidecar(f)
    stat = f.stat()
    try:
        sr = int(sc.get("sample_rate") or cfg("SAMPLE_RATE", "48000"))
        ch = int(sc.get("channels") or cfg("CHANNELS", "1"))
        bd = int(sc.get("bit_depth") or cfg("BIT_DEPTH", "16"))
        data_bytes = max(0, stat.st_size - 44)
        duration_s = data_bytes / (sr * ch * (bd // 8)) if sr > 0 else 0.0
    except Exception:
        duration_s = 0.0

    return {
        "name": f.name,
        "size_bytes": stat.st_size,
        "size_mb": round(stat.st_size / 1_048_576, 2),
        "duration_s": round(duration_s, 1),
        "timestamp": (
            sc.get("start_time_utc")
            or datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
        ),
        "downloaded": sc.get("downloaded", False),
        "sidecar": sc,
    }


@app.get("/api/recordings")
async def api_recordings() -> list[dict[str, Any]]:
    rec_dir = _get_recordings_dir()
    if not os.path.exists(rec_dir):
        return []
    try:
        files = sorted(
            Path(rec_dir).glob("*.wav"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return [_recording_info(f) for f in files]
    except Exception as exc:
        log.warning("Error listing recordings: %s", exc)
        return []


@app.get("/api/recordings/{name}/download")
async def api_download(name: str) -> StreamingResponse:
    rec_dir = _get_recordings_dir()
    target = _path_traversal_check(rec_dir, name)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Recording not found")

    size = target.stat().st_size

    def _iter():
        with open(target, "rb") as fh:
            while chunk := fh.read(65536):
                yield chunk
        sc = _read_sidecar(target)
        sc["downloaded"] = True
        try:
            target.with_suffix(".json").write_text(json.dumps(sc, indent=2))
        except Exception:
            pass

    return StreamingResponse(
        _iter(),
        media_type="audio/wav",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Content-Length": str(size),
        },
    )


@app.delete("/api/recordings/{name}")
async def api_delete(name: str) -> dict[str, str]:
    rec_dir = _get_recordings_dir()
    target = _path_traversal_check(rec_dir, name)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Recording not found")
    try:
        target.unlink()
        sc = target.with_suffix(".json")
        if sc.exists():
            sc.unlink()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"status": "deleted", "name": name}
