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

from fastapi import FastAPI, HTTPException, Request
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
    "JACK_PORTS": "system:capture_1 mic2:capture_1 mic3:capture_1 mic4:capture_1",
    "MIC_PORTS": "1-1.1 1-1.2 1-1.3 1-1.4",
    "SAMPLE_RATE": "48000",
    "BIT_DEPTH": "16",
    "COUNTDOWN_BEEPS": "3",
    "MIN_FREE_MB": "500",
    "OUT_DEVICE": "plughw:CARD=Device",
    "TONES_DIR": "",
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


def _mock_tones_dir() -> str:
    """Generate minimal silent WAV tone stubs in tmpdir."""
    d = os.path.join(_get_mock_tmpdir(), "tones")
    os.makedirs(d, exist_ok=True)
    for name in (
        "go", "stop", "saved", "error",
        *[f"countdown_{i}" for i in range(8)],
    ):
        path = os.path.join(d, f"{name}.wav")
        if not os.path.exists(path):
            _write_silent_wav(path, duration_ms=200)
    return d


def _write_silent_wav(path: str, duration_ms: int = 200, sr: int = 48000) -> None:
    n_frames = sr * duration_ms // 1000
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(b"\x00\x00" * n_frames)


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


def play_tone_sync(tone_path: str) -> None:
    """Play a WAV tone via aplay (blocking). Never overlaps."""
    if not os.path.exists(tone_path):
        log.warning("Tone missing: %s", tone_path)
        return
    if MOCK_MODE:
        log.info("MOCK aplay: %s", tone_path)
        time.sleep(0.1)
        return
    out_device = cfg("OUT_DEVICE", "plughw:0")
    try:
        subprocess.run(
            ["aplay", "-D", out_device, tone_path],
            capture_output=True,
            timeout=10,
        )
    except Exception as exc:
        log.warning("aplay failed (%s): %s", os.path.basename(tone_path), exc)


def play_tone_bg(tone_name: str) -> None:
    """Play a named tone in a background daemon thread."""
    tones = _get_tones_dir()
    path = os.path.join(tones, f"{tone_name}.wav")
    t = threading.Thread(target=play_tone_sync, args=(path,), daemon=True)
    t.start()


def _get_recordings_dir() -> str:
    if MOCK_MODE:
        return _mock_recordings_dir()
    return cfg("RECORDINGS_DIR", "/mnt/ssd/recordings")


def _get_tones_dir() -> str:
    if MOCK_MODE:
        return _mock_tones_dir()
    return cfg("TONES_DIR", "/opt/fieldrec/tones")


def _safe_filename(name: str) -> str:
    """Sanitize session name to safe filesystem characters."""
    name = re.sub(r"[^\w\-\.]", "_", name)
    name = name.strip("._")
    return (name[:80] if name else "session")


def _path_traversal_check(base: str, filename: str) -> Path:
    """Verify filename resolves inside base dir. Raises HTTPException on attack."""
    # Strip any path components — only the final filename is accepted
    bare = Path(filename).name
    if not bare:
        raise HTTPException(status_code=400, detail="Invalid filename")
    safe = Path(base).resolve()
    target = (safe / bare).resolve()
    if not str(target).startswith(str(safe) + os.sep) and target != safe:
        raise HTTPException(status_code=400, detail="Invalid filename")
    return target


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

    def _play_countdown(self, n: int) -> None:
        tones_dir = _get_tones_dir()
        for i in range(n):
            # countdown_0 is first (lowest pitch at 660 Hz)
            path = os.path.join(tones_dir, f"countdown_{i}.wav")
            play_tone_sync(path)
            if i < n - 1:
                time.sleep(0.8)

    def _build_capture_cmd(self, out_path: str) -> list[str]:
        """
        Build jack_capture command per constraints:
        - duration via SIGINT (no -d flag)
        - -f wav only for <=2 channels; omit for >2 (auto-selects wavex)
        - no --duration flag
        """
        try:
            channels = int(cfg("CHANNELS", "1"))
        except ValueError:
            channels = 1
        bit_depth = cfg("BIT_DEPTH", "16")
        jack_ports_str = cfg("JACK_PORTS", "system:capture_1")
        ports = jack_ports_str.split()

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
        ports = set(cached_jack_lsp())
        jack_ports_str = cfg("JACK_PORTS", "system:capture_1")
        required = jack_ports_str.split()
        missing = [p for p in required if p not in ports]
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

    def start(self, session: str = "take") -> None:
        with self._lock:
            if self._state not in (State.IDLE, State.ERROR):
                raise HTTPException(409, f"Cannot start: state is {self._state.value}")
            self._start_time = time.time()
            self._go_tone_time = None
            self._current_file = None
            self._current_path = None
            self._set_state(State.ARMING)
            safe_session = _safe_filename(session) if session else "take"

        threading.Thread(
            target=self._arm_sequence,
            args=(safe_session,),
            daemon=True,
        ).start()

    def _arm_sequence(self, session: str) -> None:
        try:
            err = self._pre_flight()
            if err:
                with self._lock:
                    self._set_state(State.ERROR, f"Pre-flight: {err}")
                play_tone_bg("error")
                return

            # Countdown beeps
            try:
                beeps = int(cfg("COUNTDOWN_BEEPS", "3"))
            except ValueError:
                beeps = 3
            if beeps > 0:
                self._play_countdown(beeps)

            # Build output path
            ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            rec_dir = _get_recordings_dir()
            os.makedirs(rec_dir, exist_ok=True)
            out_filename = f"{ts}_{session}.wav"
            out_path = os.path.join(rec_dir, out_filename)

            if MOCK_MODE:
                # Write a small mock WAV, run in a thread that waits for stop signal
                _write_silent_wav(out_path, duration_ms=1000)
                proc = None
            else:
                cmd = self._build_capture_cmd(out_path)
                log.info("jack_capture: %s", " ".join(shlex.quote(c) for c in cmd))
                # Hold stdin open with a pipe: jack_capture stops on Return/EOF,
                # and under systemd stdin would be /dev/null (instant EOF). An
                # open pipe we never write to keeps it recording until SIGINT.
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                # Brief wait to detect immediate failure
                time.sleep(0.5)
                if proc.poll() is not None:
                    stderr = b""
                    try:
                        stderr = proc.stderr.read() if proc.stderr else b""
                    except Exception:
                        pass
                    raise RuntimeError(
                        f"jack_capture failed to start (exit={proc.returncode}): "
                        f"{stderr.decode(errors='replace')[:200]}"
                    )
                # Start monitor thread
                threading.Thread(
                    target=self._monitor_proc,
                    args=(proc,),
                    daemon=True,
                ).start()

            # Record go-tone time and play it
            go_time = time.time()

            with self._lock:
                self._proc = proc
                self._current_file = out_filename
                self._current_path = Path(out_path)
                self._go_tone_time = go_time
                self._recording_start_time = time.time()
                self._set_state(State.RECORDING)

            # Play go tone (blocks briefly — time offset is negligible at this sample rate)
            tones_dir = _get_tones_dir()
            play_tone_sync(os.path.join(tones_dir, "go.wav"))

        except Exception as exc:
            log.exception("Arm sequence failed: %s", exc)
            with self._lock:
                self._set_state(State.ERROR, str(exc))
                self._proc = None
            play_tone_bg("error")

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

            # Play stop tone
            tones_dir = _get_tones_dir()
            play_tone_sync(os.path.join(tones_dir, "stop.wav"))

            # Flush OS buffers
            try:
                os.sync()
            except Exception:
                pass

            # Validate output file
            if MOCK_MODE:
                # Mock: file is already written
                pass
            elif out_path is None or not out_path.exists() or out_path.stat().st_size == 0:
                raise RuntimeError(f"Output file missing or empty: {out_path}")

            # Write sidecar JSON
            if out_path is not None:
                go_offset = (
                    (go_tone_time - start_time)
                    if go_tone_time is not None and start_time is not None
                    else None
                )
                sidecar = {
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
                    "channels": cfg("CHANNELS", "1"),
                    "bit_depth": cfg("BIT_DEPTH", "16"),
                    "downloaded": False,
                }
                sidecar_path = out_path.with_suffix(".json")
                sidecar_path.write_text(json.dumps(sidecar, indent=2))
                log.info("Sidecar: %s", sidecar_path)

            # Play saved tone
            play_tone_sync(os.path.join(tones_dir, "saved.wav"))

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
            # Rename partial file
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
        jack_ports_str = conf.get("JACK_PORTS", "system:capture_1")
        required_ports = jack_ports_str.split()
        mic_ports_str = conf.get("MIC_PORTS", "")
        mic_ports = mic_ports_str.split() if mic_ports_str else []
        active_ports = set(cached_jack_lsp())
        is_jack_alive = bool(active_ports) or MOCK_MODE

        # If MIC_PORTS not configured, synthesise one entry per JACK port
        if not mic_ports and required_ports:
            mic_ports = [f"mock-{i+1}" for i in range(len(required_ports))]

        mics: list[dict[str, Any]] = []
        for i, usb_port in enumerate(mic_ports, 1):
            jack_port = required_ports[i - 1] if i - 1 < len(required_ports) else ""
            mics.append({
                "index": i,
                "usb_port": usb_port,
                "jack_port": jack_port,
                "present": jack_port in active_ports if not MOCK_MODE else True,
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

    # Sync clock if drift > 5 s
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
async def api_start(req: StartReq) -> dict[str, Any]:
    session = (req.session or "take").strip() or "take"
    controller.start(session)
    return {"status": "arming", "session": session}


@app.post("/api/stop")
async def api_stop() -> dict[str, str]:
    controller.stop()
    return {"status": "stopping"}


@app.post("/api/reset")
async def api_reset() -> dict:
    controller.reset()
    return controller.get_status()


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
    # Estimate duration from file size (WAV: size / (sr * ch * bytes_per_sample))
    try:
        sr = int(sc.get("sample_rate") or cfg("SAMPLE_RATE", "48000"))
        ch = int(sc.get("channels") or cfg("CHANNELS", "1"))
        bd = int(sc.get("bit_depth") or cfg("BIT_DEPTH", "16"))
        data_bytes = max(0, stat.st_size - 44)  # subtract WAV header
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
        # Mark as downloaded in sidecar
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
