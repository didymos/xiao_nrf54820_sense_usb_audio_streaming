#!/usr/bin/env python3
"""Field Recording Controller — FastAPI app + RecorderController + tone engine."""

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("fieldrec")

# ─── Mock mode ───────────────────────────────────────────────────────────────
MOCK = (
    os.environ.get("FIELDREC_MOCK", "").lower() in ("1", "true", "yes")
    or "--mock" in sys.argv
)
if MOCK:
    log.info("MOCK MODE ENABLED — JACK and ALSA calls are stubbed")

# ─── Config ──────────────────────────────────────────────────────────────────
_DEFAULTS: dict = {
    "sample_rate": 16000,
    "channels": 4,
    "jack_ports": [
        "system:capture_1",
        "mic2:capture_1",
        "mic3:capture_1",
        "mic4:capture_1",
    ],
    "out_device": "plughw:CARD=Device",
    "recordings_dir": "/mnt/ssd/recordings",
    "bit_depth": 16,
    "host": "0.0.0.0",
    "port": 8080,
    "countdown_beeps": 3,
    "min_free_mb": 500,
}

_REQUIRED_KEYS = list(_DEFAULTS.keys())


def _load_config() -> dict:
    p = Path(__file__).parent.parent / "config.yaml"
    if not p.exists():
        log.warning("config.yaml not found — using built-in defaults")
        return _DEFAULTS.copy()
    with open(p) as f:
        raw = yaml.safe_load(f) or {}
    cfg = {**_DEFAULTS, **raw}
    missing = [k for k in _REQUIRED_KEYS if k not in cfg]
    if missing:
        raise SystemExit(f"FATAL: missing required config keys: {missing}")
    if not cfg["jack_ports"]:
        raise SystemExit("FATAL: jack_ports must not be empty")
    log.info("Config loaded: sr=%s ch=%s dir=%s", cfg["sample_rate"], cfg["channels"], cfg["recordings_dir"])
    return cfg


CFG = _load_config()

RECORDINGS_DIR: Path
if MOCK:
    _mock_dir = Path(tempfile.mkdtemp(prefix="fieldrec_mock_"))
    RECORDINGS_DIR = _mock_dir
    log.info("Mock recordings dir: %s", RECORDINGS_DIR)
else:
    RECORDINGS_DIR = Path(CFG["recordings_dir"])
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

# ─── Tone engine ─────────────────────────────────────────────────────────────
_TONE_DIR = Path(tempfile.mkdtemp(prefix="fieldrec_tones_"))
_TONE_SR = 16000


def _sine(freq: float, duration: float, amplitude: float = 0.7, fade_ms: int = 20) -> np.ndarray:
    n = int(_TONE_SR * duration)
    t = np.linspace(0, duration, n, endpoint=False)
    w = amplitude * np.sin(2 * np.pi * freq * t).astype(np.float32)
    fade = int(_TONE_SR * fade_ms / 1000)
    if fade > 0 and 2 * fade < n:
        w[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
        w[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
    return w


def _write_wav(path: Path, data: np.ndarray) -> None:
    sf.write(str(path), data, _TONE_SR, subtype="PCM_16")


def _generate_tones() -> None:
    silence = np.zeros(int(_TONE_SR * 0.05), dtype=np.float32)
    # Per-beep countdown tones (ascending 660, 770, 880 Hz …); generated lazily too
    for i in range(8):
        _write_wav(_TONE_DIR / f"countdown_{i}.wav", _sine(660 + i * 110, 0.2))
    # GO: high, long — captured by mics as sync marker
    _write_wav(_TONE_DIR / "go.wav", _sine(1320, 0.7))
    # Stop: mid
    _write_wav(_TONE_DIR / "stop.wav", _sine(880, 0.4))
    # Saved: two-note rising chime
    _write_wav(_TONE_DIR / "saved.wav", np.concatenate([_sine(880, 0.3), silence, _sine(1100, 0.5)]))
    # Error: descending low pair
    _write_wav(_TONE_DIR / "error.wav", np.concatenate([_sine(300, 0.4, 0.8), silence, _sine(200, 0.5, 0.8)]))
    log.info("Tones generated in %s", _TONE_DIR)


_generate_tones()

# ─── System helpers ───────────────────────────────────────────────────────────

def _run(cmd: list, timeout: int = 8) -> subprocess.CompletedProcess:
    log.debug("run: %s", " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _jack_lsp() -> list[str]:
    if MOCK:
        return list(CFG["jack_ports"])
    try:
        r = _run(["jack_lsp"], timeout=5)
        return [l.strip() for l in r.stdout.splitlines() if l.strip()] if r.returncode == 0 else []
    except Exception as e:
        log.warning("jack_lsp failed: %s", e)
        return []


def _jack_samplerate() -> int:
    if MOCK:
        return CFG["sample_rate"]
    try:
        r = _run(["jack_samplerate"], timeout=5)
        return int(r.stdout.strip())
    except Exception:
        return -1


def _aplay(wav: Path) -> None:
    if MOCK:
        try:
            info = sf.info(str(wav))
            time.sleep(info.duration)
        except Exception:
            time.sleep(0.3)
        return
    try:
        subprocess.run(
            ["aplay", "-D", CFG["out_device"], str(wav)],
            capture_output=True, timeout=15,
        )
    except Exception as e:
        log.warning("aplay failed for %s: %s", wav.name, e)


def _out_device_exists() -> bool:
    if MOCK:
        return True
    try:
        r = _run(["aplay", "-L"], timeout=5)
        return CFG["out_device"] in r.stdout
    except Exception:
        return False


def _disk_info() -> tuple[float, float]:
    try:
        st = shutil.disk_usage(str(RECORDINGS_DIR))
        return st.free / 1_048_576, st.total / 1_048_576
    except Exception:
        return 0.0, 0.0


_xrun_cache: tuple[float, int] = (0.0, 0)


def _xrun_count() -> int:
    global _xrun_cache
    if MOCK:
        return 0
    ts, val = _xrun_cache
    if time.time() - ts < 15:
        return val
    try:
        r = subprocess.run(
            ["journalctl", "-u", "audio-sync", "--boot", "-o", "cat", "--no-pager"],
            capture_output=True, text=True, timeout=5,
        )
        val = r.stdout.lower().count("xrun")
    except Exception:
        val = -1
    _xrun_cache = (time.time(), val)
    return val


# Cached port list so status endpoint stays fast
_port_cache: tuple[float, list[str]] = (0.0, [])


def _cached_jack_lsp() -> list[str]:
    global _port_cache
    ts, ports = _port_cache
    if time.time() - ts < 2:
        return ports
    ports = _jack_lsp()
    _port_cache = (time.time(), ports)
    return ports


# ─── Mock jack_capture ────────────────────────────────────────────────────────
_MOCK_CAPTURE_SCRIPT = _TONE_DIR / "mock_capture.py"
_MOCK_CAPTURE_SCRIPT.write_text(
    "#!/usr/bin/env python3\n"
    "import sys, signal, time, numpy as np, soundfile as sf\n"
    "outfile = sys.argv[-1]\n"
    "done = False\n"
    "def _h(s, f): global done; done = True\n"
    "signal.signal(signal.SIGINT, _h)\n"
    "signal.signal(signal.SIGTERM, _h)\n"
    "chunks = []\n"
    "while not done:\n"
    "    chunks.append(np.random.randn(16000, 4).astype('float32') * 0.001)\n"
    "    time.sleep(1.0)\n"
    "data = np.concatenate(chunks) if chunks else np.zeros((16000, 4), dtype='float32')\n"
    "sf.write(outfile, data, 16000, subtype='PCM_16')\n"
    "print('mock_capture done:', outfile, flush=True)\n"
)
_MOCK_CAPTURE_SCRIPT.chmod(0o755)

# ─── State machine ────────────────────────────────────────────────────────────


class State(str, Enum):
    IDLE = "IDLE"
    ARMING = "ARMING"
    RECORDING = "RECORDING"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


class RecorderController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = State.IDLE
        self._error_msg: Optional[str] = None
        self._current_file: Optional[Path] = None
        self._start_time: Optional[float] = None        # set at start() — used for ARMING countdown
        self._recording_start_time: Optional[float] = None  # set at RECORDING entry — used for mm:ss timer
        self._go_tone_time: Optional[float] = None
        self._proc: Optional[subprocess.Popen] = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _set_state(self, s: State, error: str = "") -> None:
        self._state = s
        if s == State.ERROR:
            self._error_msg = error
            log.error("State → ERROR: %s", error)
        else:
            if s == State.IDLE:
                self._error_msg = None
            log.info("State → %s", s.value)

    def _play(self, name: str) -> None:
        p = _TONE_DIR / f"{name}.wav"
        if p.exists():
            _aplay(p)
        else:
            log.warning("Tone missing: %s", name)

    def _play_countdown(self, n: int) -> None:
        for i in range(n):
            tone_p = _TONE_DIR / f"countdown_{i}.wav"
            if not tone_p.exists():
                _write_wav(tone_p, _sine(660 + i * 110, 0.2))
            _aplay(tone_p)
            if i < n - 1:
                time.sleep(0.8)  # gap so each beat ≈ 1 s total

    def _pre_flight(self) -> Optional[str]:
        ports = set(_jack_lsp())
        missing = [p for p in CFG["jack_ports"] if p not in ports]
        if missing:
            return f"JACK ports not live: {missing}"
        sr = _jack_samplerate()
        if sr != CFG["sample_rate"]:
            log.warning("Sample-rate mismatch: JACK=%s config=%s", sr, CFG["sample_rate"])
        free_mb, _ = _disk_info()
        if free_mb < CFG["min_free_mb"]:
            return f"Disk too full: {free_mb:.0f} MB free, need {CFG['min_free_mb']} MB"
        if not _out_device_exists():
            log.warning("Output device '%s' not found — cues disabled", CFG["out_device"])
        return None

    def _monitor_proc(self, proc: subprocess.Popen) -> None:
        proc.wait()
        with self._lock:
            if self._state == State.RECORDING:
                self._set_state(State.ERROR, "jack_capture exited unexpectedly")
                threading.Thread(target=self._play, args=("error",), daemon=True).start()

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self, session: str = "take", countdown_beeps: Optional[int] = None) -> dict:
        with self._lock:
            if self._state != State.IDLE:
                raise HTTPException(409, f"Cannot start: state is {self._state.value}")
            err = self._pre_flight()
            if err:
                threading.Thread(target=self._play, args=("error",), daemon=True).start()
                raise HTTPException(409, f"Pre-flight failed: {err}")
            self._current_file = None
            self._start_time = time.time()
            self._go_tone_time = None
            self._set_state(State.ARMING)
            beeps = countdown_beeps if countdown_beeps is not None else CFG["countdown_beeps"]
            session = (session or "take").strip() or "take"

        threading.Thread(target=self._arm, args=(session, beeps), daemon=True).start()
        return self.get_status()

    def _arm(self, session: str, beeps: int) -> None:
        try:
            self._play_countdown(beeps)

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{ts}_{session}.wav"
            out_path = RECORDINGS_DIR / filename

            if MOCK:
                cmd = [sys.executable, str(_MOCK_CAPTURE_SCRIPT), str(out_path)]
            else:
                port_args: list = []
                for p in CFG["jack_ports"]:
                    port_args += ["-p", p]
                cmd = [
                    "jack_capture",
                    "-c", str(CFG["channels"]),
                    *port_args,
                    "-f", "wav",
                    "-b", str(CFG["bit_depth"]),
                    str(out_path),
                ]

            log.info("Spawning capture: %s", " ".join(str(c) for c in cmd))
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            time.sleep(0.5)
            if proc.poll() is not None:
                stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                raise RuntimeError(f"jack_capture failed to start (exit={proc.returncode}): {stderr[:200]}")

            with self._lock:
                self._proc = proc
                self._current_file = out_path
                self._go_tone_time = time.time()

            self._play("go")

            threading.Thread(target=self._monitor_proc, args=(proc,), daemon=True).start()

            with self._lock:
                self._recording_start_time = time.time()
                self._set_state(State.RECORDING)

        except Exception as exc:
            log.exception("ARM sequence failed: %s", exc)
            with self._lock:
                self._set_state(State.ERROR, str(exc))
                self._proc = None
            threading.Thread(target=self._play, args=("error",), daemon=True).start()

    def stop(self) -> dict:
        with self._lock:
            if self._state != State.RECORDING:
                raise HTTPException(409, f"Cannot stop: state is {self._state.value}")
            self._set_state(State.STOPPING)
            proc = self._proc
            out_path = self._current_file
            start_time = self._recording_start_time or self._start_time
            go_tone_time = self._go_tone_time

        threading.Thread(
            target=self._stop_seq,
            args=(proc, out_path, start_time, go_tone_time),
            daemon=True,
        ).start()
        return self.get_status()

    def _stop_seq(
        self,
        proc: Optional[subprocess.Popen],
        out_path: Optional[Path],
        start_time: Optional[float],
        go_tone_time: Optional[float],
    ) -> None:
        try:
            if proc and proc.poll() is None:
                log.info("Sending SIGINT to jack_capture (pid=%s)", proc.pid)
                proc.send_signal(signal.SIGINT)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    log.warning("jack_capture timeout — sending SIGTERM")
                    proc.terminate()
                    proc.wait(timeout=5)

            self._play("stop")
            os.sync()

            if not out_path or not out_path.exists() or out_path.stat().st_size == 0:
                raise RuntimeError(f"Output file missing or empty: {out_path}")

            info = sf.info(str(out_path))
            if info.channels != CFG["channels"]:
                raise RuntimeError(f"Channel mismatch: got {info.channels}, expected {CFG['channels']}")
            if info.duration <= 0:
                raise RuntimeError(f"WAV has zero duration")

            go_offset = (go_tone_time - start_time) if go_tone_time and start_time else None
            sidecar = {
                "start_time": datetime.fromtimestamp(start_time).isoformat() if start_time else None,
                "duration_s": round(info.duration, 3),
                "channels": info.channels,
                "sample_rate": info.samplerate,
                "go_tone_wall_time": datetime.fromtimestamp(go_tone_time).isoformat() if go_tone_time else None,
                "go_tone_offset_note": (
                    f"GO tone played ~{go_offset:.2f}s after capture started. "
                    "Use in-band audio transient for precise per-recorder sync."
                ) if go_offset is not None else None,
                "downloaded": False,
            }
            out_path.with_suffix(".json").write_text(json.dumps(sidecar, indent=2))

            self._play("saved")

            with self._lock:
                self._set_state(State.IDLE)
                self._proc = None
                self._current_file = None
                self._start_time = None
                self._recording_start_time = None
                self._go_tone_time = None

        except Exception as exc:
            log.exception("STOP sequence failed: %s", exc)
            if out_path and out_path.exists():
                failed = out_path.with_name(out_path.name + ".FAILED")
                out_path.rename(failed)
                log.error("Partial file kept as: %s", failed)
            with self._lock:
                self._set_state(State.ERROR, str(exc))
                self._proc = None
                self._current_file = None
            threading.Thread(target=self._play, args=("error",), daemon=True).start()

    def reset_error(self) -> None:
        with self._lock:
            if self._state != State.ERROR:
                return
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
            self._proc = None
            self._current_file = None
            self._start_time = None
            self._recording_start_time = None
            self._go_tone_time = None
            self._set_state(State.IDLE)

    def get_status(self) -> dict:
        with self._lock:
            state = self._state
            now = time.time()
            # During ARMING use start_time (for countdown math); during RECORDING use recording_start_time
            if state == State.RECORDING and self._recording_start_time:
                elapsed = now - self._recording_start_time
            elif self._start_time:
                elapsed = now - self._start_time
            else:
                elapsed = 0.0
            current_file = self._current_file.name if self._current_file else None
            error = self._error_msg

        ports = set(_cached_jack_lsp())
        jack_alive = bool(ports)
        mic_status = [{"port": p, "present": p in ports} for p in CFG["jack_ports"]]
        free_mb, total_mb = _disk_info()

        return {
            "state": state.value,
            "elapsed_seconds": round(elapsed, 1),
            "current_file": current_file,
            "disk_free_mb": round(free_mb, 1),
            "disk_total_mb": round(total_mb, 1),
            "mics": mic_status,
            "jack_alive": jack_alive,
            "sample_rate": CFG["sample_rate"],
            "xruns": _xrun_count(),
            "pi_time": datetime.now().isoformat(),
            "error": error,
            "countdown_beeps": CFG["countdown_beeps"],
        }


controller = RecorderController()

# ─── FastAPI ──────────────────────────────────────────────────────────────────
app = FastAPI(title="Field Recording Controller", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _no_cache(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


_STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index():
    return FileResponse(str(_STATIC / "index.html"))


@app.get("/api/status")
async def api_status():
    return controller.get_status()


class StartReq(BaseModel):
    session: Optional[str] = "take"
    countdown_beeps: Optional[int] = None


@app.post("/api/start")
async def api_start(req: StartReq):
    return controller.start(req.session or "take", req.countdown_beeps)


@app.post("/api/stop")
async def api_stop():
    return controller.stop()


@app.post("/api/reset")
async def api_reset():
    controller.reset_error()
    return controller.get_status()


class TimeReq(BaseModel):
    epoch_ms: int


@app.post("/api/time")
async def api_time(req: TimeReq):
    epoch_s = req.epoch_ms / 1000.0
    try:
        r = subprocess.run(
            ["sudo", "/usr/bin/date", "-s", f"@{epoch_s:.3f}"],
            capture_output=True, text=True, timeout=5,
        )
        ok = r.returncode == 0
        if not ok:
            log.warning("date -s failed: %s", r.stderr.strip())
    except Exception as e:
        log.warning("Time set failed: %s", e)
        ok = False
    return {"ok": ok, "pi_time": datetime.now().isoformat()}


def _read_sidecar(wav: Path) -> dict:
    sc = wav.with_suffix(".json")
    if sc.exists():
        try:
            return json.loads(sc.read_text())
        except Exception:
            pass
    return {}


def _recording_info(f: Path) -> dict:
    sc = _read_sidecar(f)
    try:
        info = sf.info(str(f))
        dur = round(info.duration, 1)
        ch = info.channels
    except Exception:
        dur, ch = 0.0, 0
    return {
        "name": f.name,
        "size_mb": round(f.stat().st_size / 1_048_576, 2),
        "duration_s": dur,
        "channels": ch,
        "created": sc.get("start_time") or datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
        "downloaded": sc.get("downloaded", False),
    }


@app.get("/api/recordings")
async def api_recordings():
    if not RECORDINGS_DIR.exists():
        return []
    files = sorted(RECORDINGS_DIR.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [_recording_info(f) for f in files]


def _safe_path(name: str) -> Path:
    safe = Path(name).name
    p = (RECORDINGS_DIR / safe).resolve()
    rec = RECORDINGS_DIR.resolve()
    if not str(p).startswith(str(rec)):
        raise HTTPException(400, "Invalid filename")
    if not p.exists():
        raise HTTPException(404, "Recording not found")
    return p


@app.get("/api/recordings/{name}/download")
async def api_download(name: str):
    path = _safe_path(name)
    size = path.stat().st_size

    def _iter():
        with open(path, "rb") as fh:
            while chunk := fh.read(65536):
                yield chunk
        sc = _read_sidecar(path)
        sc["downloaded"] = True
        try:
            path.with_suffix(".json").write_text(json.dumps(sc, indent=2))
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
async def api_delete(name: str):
    path = _safe_path(name)
    sc = path.with_suffix(".json")
    path.unlink()
    if sc.exists():
        sc.unlink()
    return {"deleted": name}


@app.get("/api/health")
async def api_health():
    ports = set(_cached_jack_lsp())
    jack = bool(ports)
    mics = sum(1 for p in CFG["jack_ports"] if p in ports)
    free_mb, _ = _disk_info()
    return {
        "ok": jack and free_mb >= CFG["min_free_mb"],
        "jack": jack,
        "mics_present": mics,
        "disk_free_mb": round(free_mb, 1),
        "mock": MOCK,
    }
