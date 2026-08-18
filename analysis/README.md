# FieldRec Recording Analysis

Offline tool to analyse FieldRec multi-channel recordings: map each channel to a
physical position (left/right + mounting area such as helmet), quantify
**recording quality** (noise, SNR, clipping) and, optionally, **speech
intelligibility** (how well spoken words are understood).

## Install

```bash
cd analysis
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # numpy required; whisper/matplotlib optional
```

The acoustic metrics need only **numpy**. Speech intelligibility (`--transcribe`)
needs **faster-whisper** (or openai-whisper). Charts (`--plots`) need matplotlib.

## Positions file

Copy `positions.example.json` to `positions.json` and map each **1-based
recording channel** to a position and mounting area:

```json
{
  "channels": {
    "1": { "position": "left",  "mount": "helmet" },
    "2": { "position": "right", "mount": "helmet" }
  }
}
```

The channel order matches the recording (CH1 = first channel). If a recording
has a sidecar `.json` (FieldRec writes one next to each WAV), the tool also picks
up each channel's device serial / USB socket automatically.

## Usage

```bash
# Acoustic quality only (fast, numpy only)
python analyze_recordings.py take_001.wav --positions positions.json

# + speech intelligibility (transcribes each channel with Whisper)
python analyze_recordings.py take_001.wav --positions positions.json --transcribe --model base

# Whole folder, with charts, results in ./results/
python analyze_recordings.py ./recordings/ --positions positions.json --transcribe --plots --out results/
```

Outputs: a console table + per-position summary, `analysis.csv` (all metrics for
every channel/file), and — with `--plots` — an SNR/score bar chart per file.

## What the numbers mean

| Metric | Meaning | Good |
|---|---|---|
| **noise floor (dBFS)** | level of the quiet passages (background noise) | low (very negative) |
| **speech level (dBFS)** | level during speech | around −20 to −12 dBFS |
| **SNR (dB)** | speech level − noise floor | higher (≥ ~15–20 dB is clear) |
| **activity %** | fraction of the take containing speech | context-dependent |
| **clipping %** | samples at full scale (distortion) | 0 |
| **centroid (Hz)** | spectral brightness (muffled vs. crisp) | context-dependent |
| **quality score** | heuristic 0–100 combining SNR + level − clipping | higher |
| **words / conf** | recognised words and mean Whisper confidence | higher |
| **WER** | word-error-rate vs. the clearest channel (relative) | lower |

### On intelligibility

There is no reference "clean" signal, so intelligibility is measured
**non-intrusively**:

- **Whisper transcription** per channel → recognised word count and confidence.
  A position that yields more words at higher confidence is more intelligible.
- **Cross-channel consensus WER**: the channel with the most confident speech is
  taken as the reference; every other channel's word-error-rate against it is a
  relative intelligibility ranking (0 = identical to the best channel).

These are indicators, not calibrated scores (e.g. not STOI/PESQ, which need a
reference recording). For a formal standard, record a known reference phrase.

## Notes

- Reads standard PCM and multichannel `WAVE_FORMAT_EXTENSIBLE` (16/24-bit) — the
  format FieldRec writes for >2 channels.
- `system` / `jackp` dummy inputs are not part of a recorded file's channels, so
  they never appear here — only the assigned microphone channels are analysed.
