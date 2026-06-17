#!/usr/bin/env python3
"""
make_tones.py — Generate WAV cue tones for FieldRec.

Usage:
    python3 make_tones.py /opt/fieldrec/tones

Tones generated:
    countdown_0..countdown_7  — ascending pitches 660+i*110 Hz, 200 ms each
    go.wav                    — 1320 Hz, 700 ms
    stop.wav                  — 880 Hz, 400 ms
    saved.wav                 — 880 Hz then 1100 Hz with 50 ms gap
    error.wav                 — 300 Hz then 200 Hz (descending, gap between)

All tones have 20 ms fade-in and fade-out to prevent clicks.
Requires: numpy, soundfile
"""

import sys
import os
import numpy as np

try:
    import soundfile as sf
except ImportError:
    sys.exit("soundfile not installed — run: pip install soundfile")


SAMPLE_RATE = 48000
FADE_MS = 20


def make_sine(freq_hz: float, duration_ms: float, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Generate a mono sine wave array."""
    n = int(sr * duration_ms / 1000)
    t = np.linspace(0.0, duration_ms / 1000.0, n, endpoint=False)
    return 0.8 * np.sin(2.0 * np.pi * freq_hz * t).astype(np.float32)


def apply_fade(samples: np.ndarray, fade_ms: float = FADE_MS, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Apply linear fade-in and fade-out to prevent clicks."""
    fade_n = int(sr * fade_ms / 1000)
    fade_n = min(fade_n, len(samples) // 2)
    fade_in  = np.linspace(0.0, 1.0, fade_n, endpoint=False).astype(np.float32)
    fade_out = np.linspace(1.0, 0.0, fade_n, endpoint=False).astype(np.float32)
    result = samples.copy()
    result[:fade_n] *= fade_in
    result[-fade_n:] *= fade_out
    return result


def silence(duration_ms: float, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Return a silent segment."""
    n = int(sr * duration_ms / 1000)
    return np.zeros(n, dtype=np.float32)


def write_wav(path: str, samples: np.ndarray, sr: int = SAMPLE_RATE) -> None:
    """Write mono float32 array as a 16-bit WAV file."""
    # Clip to [-1, 1] then convert to int16
    clipped = np.clip(samples, -1.0, 1.0)
    int16 = (clipped * 32767).astype(np.int16)
    sf.write(path, int16, sr, subtype="PCM_16")
    size_kb = os.path.getsize(path) / 1024
    print(f"  Written: {os.path.basename(path)}  ({len(samples)/sr*1000:.0f} ms, {size_kb:.1f} KB)")


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <output_dir>")
        sys.exit(1)

    out_dir = sys.argv[1]
    os.makedirs(out_dir, exist_ok=True)
    print(f"Generating tones → {out_dir}")

    # ── Countdown tones (countdown_0 .. countdown_7) ─────────────────────────
    # Ascending pitches: 660 + i*110 Hz, 200 ms each
    for i in range(8):
        freq = 660 + i * 110
        tone = apply_fade(make_sine(freq, 200))
        write_wav(os.path.join(out_dir, f"countdown_{i}.wav"), tone)

    # ── Go tone ───────────────────────────────────────────────────────────────
    # 1320 Hz, 700 ms — high and punchy
    go = apply_fade(make_sine(1320, 700))
    write_wav(os.path.join(out_dir, "go.wav"), go)

    # ── Stop tone ─────────────────────────────────────────────────────────────
    # 880 Hz, 400 ms
    stop = apply_fade(make_sine(880, 400))
    write_wav(os.path.join(out_dir, "stop.wav"), stop)

    # ── Saved tone ────────────────────────────────────────────────────────────
    # 880 Hz (300 ms), 50 ms silence, 1100 Hz (300 ms)
    saved = np.concatenate([
        apply_fade(make_sine(880,  300)),
        silence(50),
        apply_fade(make_sine(1100, 300)),
    ])
    write_wav(os.path.join(out_dir, "saved.wav"), saved)

    # ── Error tone ────────────────────────────────────────────────────────────
    # 300 Hz (400 ms), 50 ms silence, 200 Hz (600 ms) — low descending
    error = np.concatenate([
        apply_fade(make_sine(300, 400)),
        silence(50),
        apply_fade(make_sine(200, 600)),
    ])
    write_wav(os.path.join(out_dir, "error.wav"), error)

    print(f"\nAll tones generated successfully in: {out_dir}")
    print(f"Total files: {len(os.listdir(out_dir))}")


if __name__ == "__main__":
    main()
