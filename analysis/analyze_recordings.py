#!/usr/bin/env python3
"""
analyze_recordings.py — quality & intelligibility analysis for FieldRec
multi-channel recordings.

For each channel of a (multi-channel) WAV it computes:
  • Acoustic quality (numpy only):
      - noise floor (dBFS), active speech level (dBFS), SNR (dB)
      - speech activity (%), clipping (%), spectral centroid (Hz)
      - a heuristic 0–100 quality score
  • Speech intelligibility (optional, needs Whisper — `--transcribe`):
      - recognised word count, mean word confidence, no-speech probability
      - word-error-rate vs. the best channel (cross-channel consensus)

Each channel is mapped to a physical position (left/right) and mounting area
(e.g. helmet) via a positions file, so results are reported per position.

Usage:
    python analyze_recordings.py RECORDING.wav --positions positions.json
    python analyze_recordings.py RECORDING.wav --positions positions.json --transcribe --model base
    python analyze_recordings.py ./recordings/ --positions positions.json --out results/

Dependencies:
    required : numpy
    optional : faster-whisper  (or openai-whisper)  for --transcribe
               matplotlib                            for --plots
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import struct
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
# WAV reading (numpy only) — handles PCM 16/24-bit and WAVE_FORMAT_EXTENSIBLE
# (multichannel), which Python's `wave` module cannot read.
# ─────────────────────────────────────────────────────────────────────────────
def read_wav(path: str) -> tuple[np.ndarray, int]:
    """Return (samples float32 in [-1,1] shaped [n_frames, n_channels], sample_rate)."""
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError(f"Not a WAV file: {path}")
    fmt = audio = None
    pos = 12
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        sz = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        avail = len(data) - (pos + 8)
        if cid == b"data":
            # Streaming/interrupted recorders leave the data size at 0 (or
            # 0xFFFFFFFF); the samples then run to EOF. Trust EOF in that case.
            if sz == 0 or sz > avail:
                audio = data[pos + 8:]
                break
            audio = data[pos + 8:pos + 8 + sz]
            pos += 8 + sz + (sz & 1)
            continue
        if cid == b"fmt ":
            fmt = data[pos + 8:pos + 8 + sz]
        if sz == 0 or sz > avail:
            break
        pos += 8 + sz + (sz & 1)
    if not fmt or audio is None or len(fmt) < 16:
        raise ValueError(f"Malformed WAV (no fmt/data): {path}")
    tag = struct.unpack("<H", fmt[0:2])[0]
    ch = struct.unpack("<H", fmt[2:4])[0]
    sr = struct.unpack("<I", fmt[4:8])[0]
    bits = struct.unpack("<H", fmt[14:16])[0]
    if tag == 0xFFFE and len(fmt) >= 26:
        tag = struct.unpack("<H", fmt[24:26])[0]
    if ch < 1:
        raise ValueError(f"Bad channel count in {path}")

    if tag == 1 and bits == 16:
        usable = (len(audio) // (2 * ch)) * (2 * ch)
        arr = np.frombuffer(audio[:usable], dtype="<i2").astype(np.float32) / 32768.0
    elif tag == 1 and bits == 24:
        usable = (len(audio) // (3 * ch)) * (3 * ch)
        b = np.frombuffer(audio[:usable], dtype=np.uint8).reshape(-1, 3)
        v = (b[:, 0].astype(np.int32)
             | (b[:, 1].astype(np.int32) << 8)
             | (b[:, 2].astype(np.int32) << 16))
        v = np.where(v & 0x800000, v - 0x1000000, v)
        arr = v.astype(np.float32) / 8388608.0
    elif tag == 3 and bits == 32:
        usable = (len(audio) // (4 * ch)) * (4 * ch)
        arr = np.frombuffer(audio[:usable], dtype="<f4").astype(np.float32)
    else:
        raise ValueError(f"Unsupported WAV format tag={tag} bits={bits} in {path}")
    return arr.reshape(-1, ch), sr


# ─────────────────────────────────────────────────────────────────────────────
# Acoustic metrics
# ─────────────────────────────────────────────────────────────────────────────
def _frame(x: np.ndarray, sr: int, frame_ms: float = 25, hop_ms: float = 10) -> np.ndarray:
    flen = max(1, int(sr * frame_ms / 1000))
    hop = max(1, int(sr * hop_ms / 1000))
    if len(x) < flen:
        return x[None, :] if len(x) else np.zeros((1, 1), np.float32)
    n = 1 + (len(x) - flen) // hop
    idx = np.arange(flen)[None, :] + hop * np.arange(n)[:, None]
    return x[idx]


def _dbfs(v: np.ndarray | float) -> np.ndarray | float:
    return 20.0 * np.log10(np.maximum(v, 1e-12))


@dataclass
class ChannelMetrics:
    channel: int
    position: str = ""
    mount: str = ""
    label: str = ""
    device_id: str = ""          # serial / usb socket from sidecar, if any
    duration_s: float = 0.0
    peak_dbfs: float = 0.0
    noise_floor_dbfs: float = 0.0
    speech_level_dbfs: float = 0.0
    snr_db: float = 0.0
    activity_pct: float = 0.0
    clipping_pct: float = 0.0
    centroid_hz: float = 0.0
    quality_score: float = 0.0
    # intelligibility (optional)
    words: Optional[int] = None
    word_conf: Optional[float] = None
    no_speech: Optional[float] = None
    wer_vs_best: Optional[float] = None
    transcript: str = ""


def acoustic_metrics(x: np.ndarray, sr: int) -> dict:
    x = np.asarray(x, np.float64)
    if len(x) == 0:
        return {}
    peak = float(np.max(np.abs(x)))
    clip = float(np.mean(np.abs(x) >= 0.999))
    frames = _frame(x, sr)
    frms = np.sqrt(np.mean(frames ** 2, axis=1))
    fdb = _dbfs(frms)
    noise_floor = float(np.percentile(fdb, 10))
    active = fdb > (noise_floor + 6.0)          # simple energy VAD
    activity = float(np.mean(active))
    if np.any(active):
        speech_level = float(np.median(fdb[active]))
    else:
        speech_level = float(np.percentile(fdb, 90))
    snr = speech_level - noise_floor

    # spectral centroid over active (speech) frames
    win = np.hanning(frames.shape[1])
    spec = np.abs(np.fft.rfft(frames * win, axis=1))
    freqs = np.fft.rfftfreq(frames.shape[1], 1.0 / sr)
    denom = np.maximum(np.sum(spec, axis=1), 1e-12)
    cent = np.sum(spec * freqs[None, :], axis=1) / denom
    centroid = float(np.mean(cent[active])) if np.any(active) else float(np.mean(cent))

    return dict(
        duration_s=len(x) / sr,
        peak_dbfs=round(float(_dbfs(peak)), 1),
        noise_floor_dbfs=round(noise_floor, 1),
        speech_level_dbfs=round(speech_level, 1),
        snr_db=round(snr, 1),
        activity_pct=round(activity * 100, 1),
        clipping_pct=round(clip * 100, 3),
        centroid_hz=round(centroid, 0),
        quality_score=round(quality_score(snr, speech_level, clip), 1),
    )


def quality_score(snr_db: float, speech_dbfs: float, clip_frac: float) -> float:
    """Heuristic 0–100. SNR-dominated, rewards adequate level, penalises
    clipping. NOT a calibrated metric — a relative indicator for ranking.
      • SNR:   up to 60 pts (linear to 40 dB)
      • level: up to 40 pts, full from -15 dBFS, ramping to 0 at -45 dBFS
               (a louder-but-clean channel is NOT penalised; overload is
                caught by the clipping term)
      • clipping: heavy penalty (2 pts per % of clipped samples)"""
    s = min(max(snr_db, 0.0), 40.0) / 40.0 * 60.0
    s += 40.0 * min(1.0, max(0.0, (speech_dbfs + 45.0) / 30.0))
    s -= clip_frac * 100.0 * 2.0
    return float(min(100.0, max(0.0, s)))


# ─────────────────────────────────────────────────────────────────────────────
# Optional intelligibility via Whisper
# ─────────────────────────────────────────────────────────────────────────────
def _resample_to_16k(x: np.ndarray, sr: int) -> np.ndarray:
    if sr == 16000:
        return x.astype(np.float32)
    n_out = int(round(len(x) * 16000 / sr))
    xp = np.linspace(0, 1, len(x), endpoint=False)
    xq = np.linspace(0, 1, n_out, endpoint=False)
    return np.interp(xq, xp, x).astype(np.float32)


class Transcriber:
    """Thin wrapper around faster-whisper (preferred) or openai-whisper."""

    def __init__(self, model_size: str = "base", language: Optional[str] = None):
        self.language = language
        self.backend = None
        try:
            from faster_whisper import WhisperModel
            self.model = WhisperModel(model_size, device="cpu", compute_type="int8")
            self.backend = "faster"
        except Exception:
            try:
                import whisper
                self.model = whisper.load_model(model_size)
                self.backend = "openai"
            except Exception as exc:
                raise RuntimeError(
                    "Transcription needs 'faster-whisper' (pip install faster-whisper) "
                    "or 'openai-whisper'. Import failed: %s" % exc
                )

    def transcribe(self, x: np.ndarray, sr: int) -> dict:
        audio = _resample_to_16k(np.asarray(x, np.float32), sr)
        if self.backend == "faster":
            segments, _info = self.model.transcribe(
                audio, language=self.language, word_timestamps=True, vad_filter=True,
            )
            words, confs, nsp, parts = [], [], [], []
            for seg in segments:
                parts.append(seg.text)
                nsp.append(getattr(seg, "no_speech_prob", 0.0))
                for w in (seg.words or []):
                    words.append(w.word.strip())
                    confs.append(float(w.probability))
            text = "".join(parts).strip()
        else:  # openai-whisper
            res = self.model.transcribe(audio, language=self.language, word_timestamps=True)
            text = res.get("text", "").strip()
            words, confs, nsp = [], [], []
            for seg in res.get("segments", []):
                nsp.append(seg.get("no_speech_prob", 0.0))
                for w in seg.get("words", []):
                    words.append(str(w.get("word", "")).strip())
                    confs.append(float(w.get("probability", 0.0)))
        return dict(
            words=len(words),
            word_conf=round(float(np.mean(confs)), 3) if confs else 0.0,
            no_speech=round(float(np.mean(nsp)), 3) if nsp else 0.0,
            transcript=text,
            tokens=_normalise_tokens(text),
        )


def _normalise_tokens(text: str) -> list[str]:
    return [t for t in re.sub(r"[^\w\s]", " ", text.lower()).split() if t]


def _word_edit_distance(a: list[str], b: list[str]) -> int:
    n, m = len(a), len(b)
    if n == 0:
        return m
    if m == 0:
        return n
    prev = list(range(m + 1))
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        ai = a[i - 1]
        for j in range(1, m + 1):
            cost = 0 if ai == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[m]


def add_consensus_wer(rows: list[ChannelMetrics], tokens_by_ch: dict[int, list[str]]) -> None:
    """Reference = channel with the most confidently recognised speech.
    WER of each channel is computed against that reference (relative measure)."""
    scored = [(r.channel, (r.words or 0) * (r.word_conf or 0.0)) for r in rows if r.words]
    if not scored:
        return
    ref_ch = max(scored, key=lambda t: t[1])[0]
    ref = tokens_by_ch.get(ref_ch, [])
    if not ref:
        return
    for r in rows:
        toks = tokens_by_ch.get(r.channel)
        if toks is None:
            continue
        r.wer_vs_best = round(_word_edit_distance(toks, ref) / max(1, len(ref)), 3)


# ─────────────────────────────────────────────────────────────────────────────
# Positions + sidecar
# ─────────────────────────────────────────────────────────────────────────────
def load_positions(path: Optional[str]) -> dict[int, dict]:
    """positions.json: {"channels": {"1": {"position": "left", "mount": "helmet"}, ...}}"""
    if not path:
        return {}
    with open(path) as fh:
        data = json.load(fh)
    chans = data.get("channels", data)
    out: dict[int, dict] = {}
    for k, v in chans.items():
        try:
            out[int(k)] = v or {}
        except (TypeError, ValueError):
            continue
    return out


def load_sidecar(wav_path: str) -> dict:
    sc = os.path.splitext(wav_path)[0] + ".json"
    if os.path.exists(sc):
        try:
            with open(sc) as fh:
                return json.load(fh)
        except Exception:
            pass
    return {}


def device_id_for_channel(sidecar: dict, ch_index: int) -> str:
    """1-based channel → serial (preferred) or usb port from the sidecar."""
    i = ch_index - 1
    for key in ("serials", "usb_ports"):
        arr = sidecar.get(key)
        if isinstance(arr, list) and 0 <= i < len(arr) and arr[i]:
            return str(arr[i])
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Analysis driver
# ─────────────────────────────────────────────────────────────────────────────
def analyze_file(path: str, positions: dict[int, dict],
                 transcriber: Optional[Transcriber]) -> list[ChannelMetrics]:
    samples, sr = read_wav(path)
    n_ch = samples.shape[1]
    sidecar = load_sidecar(path)
    rows: list[ChannelMetrics] = []
    tokens_by_ch: dict[int, list[str]] = {}

    for c in range(n_ch):
        ch = c + 1
        x = samples[:, c]
        pos = positions.get(ch, {})
        row = ChannelMetrics(
            channel=ch,
            position=str(pos.get("position", "")),
            mount=str(pos.get("mount", "")),
            label=str(pos.get("label", "")),
            device_id=device_id_for_channel(sidecar, ch),
        )
        for k, v in acoustic_metrics(x, sr).items():
            setattr(row, k, v)
        if transcriber is not None:
            try:
                t = transcriber.transcribe(x, sr)
                row.words = t["words"]
                row.word_conf = t["word_conf"]
                row.no_speech = t["no_speech"]
                row.transcript = t["transcript"]
                tokens_by_ch[ch] = t["tokens"]
            except Exception as exc:
                print(f"  [warn] transcription failed on ch{ch}: {exc}", file=sys.stderr)
        rows.append(row)

    if transcriber is not None and tokens_by_ch:
        add_consensus_wer(rows, tokens_by_ch)
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────
def _pos_label(r: ChannelMetrics) -> str:
    parts = [p for p in (r.mount, r.position) if p]
    base = "/".join(parts) if parts else (r.label or r.device_id or "—")
    return base


def print_table(name: str, rows: list[ChannelMetrics], with_asr: bool) -> None:
    print(f"\n=== {name} ===")
    cols = [("CH", 3), ("position", 16), ("SNR", 6), ("noise", 7), ("speech", 7),
            ("clip%", 6), ("act%", 6), ("cent", 6), ("score", 6)]
    if with_asr:
        cols += [("words", 6), ("conf", 6), ("WER", 6)]
    header = "".join(h.ljust(w) for h, w in cols)
    print(header)
    print("-" * len(header))
    for r in sorted(rows, key=lambda r: r.channel):
        cells = [
            str(r.channel).ljust(3),
            _pos_label(r)[:15].ljust(16),
            f"{r.snr_db:.1f}".ljust(6),
            f"{r.noise_floor_dbfs:.0f}".ljust(7),
            f"{r.speech_level_dbfs:.0f}".ljust(7),
            f"{r.clipping_pct:.2f}".ljust(6),
            f"{r.activity_pct:.0f}".ljust(6),
            f"{r.centroid_hz:.0f}".ljust(6),
            f"{r.quality_score:.0f}".ljust(6),
        ]
        if with_asr:
            cells += [
                (str(r.words) if r.words is not None else "-").ljust(6),
                (f"{r.word_conf:.2f}" if r.word_conf is not None else "-").ljust(6),
                (f"{r.wer_vs_best:.2f}" if r.wer_vs_best is not None else "-").ljust(6),
            ]
        print("".join(cells))


def write_csv(path: str, all_rows: list[tuple[str, ChannelMetrics]]) -> None:
    fields = ["file"] + [f for f in asdict(ChannelMetrics(0)).keys() if f != "transcript"] + ["transcript"]
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for fname, r in all_rows:
            d = asdict(r)
            d["file"] = fname
            w.writerow(d)


def summarise(rows: list[ChannelMetrics], with_asr: bool) -> list[str]:
    out: list[str] = []
    ranked = sorted(rows, key=lambda r: r.quality_score, reverse=True)
    best, worst = ranked[0], ranked[-1]
    out.append(f"- Best acoustic quality:  CH{best.channel} ({_pos_label(best)}) "
               f"score {best.quality_score:.0f}, SNR {best.snr_db:.1f} dB")
    out.append(f"- Worst acoustic quality: CH{worst.channel} ({_pos_label(worst)}) "
               f"score {worst.quality_score:.0f}, SNR {worst.snr_db:.1f} dB")
    clipped = [r for r in rows if r.clipping_pct > 0.1]
    if clipped:
        out.append("- ⚠ Clipping detected on: " +
                   ", ".join(f"CH{r.channel} ({r.clipping_pct:.1f}%)" for r in clipped))
    quiet = [r for r in rows if r.snr_db < 10]
    if quiet:
        out.append("- ⚠ Low SNR (<10 dB) on: " +
                   ", ".join(f"CH{r.channel} ({r.snr_db:.0f} dB)" for r in quiet))
    if with_asr:
        asr = [r for r in rows if r.words]
        if asr:
            mi = max(asr, key=lambda r: (r.words or 0) * (r.word_conf or 0))
            out.append(f"- Most intelligible: CH{mi.channel} ({_pos_label(mi)}) "
                       f"{mi.words} words, conf {mi.word_conf:.2f}")
    return out


def aggregate_positions(all_rows: list[tuple[str, ChannelMetrics]], with_asr: bool) -> list[dict]:
    """Group every analysed channel (across all takes) by position/mount and
    average the metrics — the per-position comparison over multiple recordings."""
    from collections import defaultdict
    groups: dict[str, list[ChannelMetrics]] = defaultdict(list)
    for _fname, r in all_rows:
        groups[_pos_label(r)].append(r)

    def mean(rows, attr):
        vals = [getattr(x, attr) for x in rows if getattr(x, attr) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    out = []
    for key, rows in groups.items():
        takes = len({id(r) for r in rows})
        rec = dict(
            position=key,
            takes=len(rows),
            snr_db=mean(rows, "snr_db"),
            quality_score=mean(rows, "quality_score"),
            speech_level_dbfs=mean(rows, "speech_level_dbfs"),
            noise_floor_dbfs=mean(rows, "noise_floor_dbfs"),
            clip_pct_max=round(max((r.clipping_pct for r in rows), default=0.0), 2),
        )
        if with_asr:
            rec.update(
                words=mean(rows, "words"),
                word_conf=mean(rows, "word_conf"),
                wer_vs_best=mean(rows, "wer_vs_best"),
            )
        out.append(rec)
    out.sort(key=lambda d: (d["quality_score"] or 0), reverse=True)
    return out


def print_aggregate(agg: list[dict], with_asr: bool) -> None:
    if not agg:
        return
    print("\n=== Aggregate by position (mean over all takes) ===")
    cols = [("position", 18), ("takes", 6), ("SNR", 7), ("score", 7),
            ("speech", 8), ("noise", 7), ("clipmax", 8)]
    if with_asr:
        cols += [("words", 7), ("conf", 6), ("WER", 6)]
    header = "".join(h.ljust(w) for h, w in cols)
    print(header + "\n" + "-" * len(header))

    def cell(v, fmt="{:.1f}"):
        return "-" if v is None else fmt.format(v)

    for d in agg:
        row = [
            str(d["position"])[:17].ljust(18),
            str(d["takes"]).ljust(6),
            cell(d["snr_db"]).ljust(7),
            cell(d["quality_score"], "{:.0f}").ljust(7),
            cell(d["speech_level_dbfs"], "{:.0f}").ljust(8),
            cell(d["noise_floor_dbfs"], "{:.0f}").ljust(7),
            cell(d["clip_pct_max"], "{:.1f}").ljust(8),
        ]
        if with_asr:
            row += [
                cell(d.get("words"), "{:.0f}").ljust(7),
                cell(d.get("word_conf"), "{:.2f}").ljust(6),
                cell(d.get("wer_vs_best"), "{:.2f}").ljust(6),
            ]
        print("".join(row))
    best = agg[0]
    print(f"\n→ Best position overall: {best['position']} "
          f"(score {cell(best['quality_score'], '{:.0f}')}, SNR {cell(best['snr_db'])} dB, "
          f"{best['takes']} take(s))")


def write_aggregate_csv(path: str, agg: list[dict]) -> None:
    if not agg:
        return
    fields = list(agg[0].keys())
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(agg)


def main() -> int:
    ap = argparse.ArgumentParser(description="Analyse FieldRec recordings: quality & intelligibility per position.")
    ap.add_argument("input", help="A WAV file or a directory of WAV files")
    ap.add_argument("--positions", help="positions.json (channel → position/mount)")
    ap.add_argument("--transcribe", action="store_true", help="Also measure speech intelligibility (needs Whisper)")
    ap.add_argument("--model", default="base", help="Whisper model size (tiny/base/small/medium)")
    ap.add_argument("--language", default=None, help="Force language (e.g. de, en); default auto-detect")
    ap.add_argument("--out", default="analysis_out", help="Output directory")
    ap.add_argument("--plots", action="store_true", help="Write SNR/score bar chart per file (needs matplotlib)")
    args = ap.parse_args()

    positions = load_positions(args.positions)
    files = ([args.input] if os.path.isfile(args.input)
             else sorted(glob.glob(os.path.join(args.input, "*.wav"))))
    if not files:
        print("No WAV files found.", file=sys.stderr)
        return 1

    transcriber = None
    if args.transcribe:
        print(f"Loading Whisper model '{args.model}' …", file=sys.stderr)
        transcriber = Transcriber(args.model, args.language)

    os.makedirs(args.out, exist_ok=True)
    all_rows: list[tuple[str, ChannelMetrics]] = []
    for path in files:
        name = os.path.basename(path)
        try:
            rows = analyze_file(path, positions, transcriber)
        except Exception as exc:
            print(f"[error] {name}: {exc}", file=sys.stderr)
            continue
        print_table(name, rows, args.transcribe)
        print("\n".join(summarise(rows, args.transcribe)))
        all_rows.extend((name, r) for r in rows)
        if args.plots:
            _plot_file(os.path.join(args.out, os.path.splitext(name)[0] + ".png"), name, rows)

    if all_rows:
        agg = aggregate_positions(all_rows, args.transcribe)
        print_aggregate(agg, args.transcribe)

        csv_path = os.path.join(args.out, "analysis.csv")
        write_csv(csv_path, all_rows)
        agg_path = os.path.join(args.out, "aggregate.csv")
        write_aggregate_csv(agg_path, agg)
        print(f"\nWrote {csv_path}  ({len(all_rows)} channel rows)")
        print(f"Wrote {agg_path}  ({len(agg)} position groups)")
    return 0


def _plot_file(out_png: str, name: str, rows: list[ChannelMetrics]) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        print("  [warn] matplotlib not available — skipping plots", file=sys.stderr)
        return
    labels = [f"CH{r.channel}\n{_pos_label(r)}" for r in rows]
    snr = [r.snr_db for r in rows]
    score = [r.quality_score for r in rows]
    fig, ax1 = plt.subplots(figsize=(max(6, len(rows) * 1.3), 4))
    x = np.arange(len(rows))
    ax1.bar(x - 0.2, snr, 0.4, label="SNR (dB)", color="#2980b9")
    ax1.set_ylabel("SNR (dB)")
    ax2 = ax1.twinx()
    ax2.bar(x + 0.2, score, 0.4, label="Quality score", color="#27ae60")
    ax2.set_ylabel("Quality score (0–100)")
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels, fontsize=8)
    ax1.set_title(name)
    fig.tight_layout()
    fig.savefig(out_png, dpi=110)
    plt.close(fig)


if __name__ == "__main__":
    raise SystemExit(main())
