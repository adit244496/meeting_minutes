"""Audio normalisation and slicing.

Everything downstream assumes 16 kHz mono PCM WAV:
  - it is what ECAPA-TDNN expects, and
  - it is the smallest format that keeps ASR quality intact, which matters when
    you are paying a hosted provider per minute of upload.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

TARGET_SAMPLE_RATE = 16_000


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg not found on PATH - it ships in the backend image")
    return exe


def _ffprobe() -> str:
    exe = shutil.which("ffprobe")
    if not exe:
        raise RuntimeError("ffprobe not found on PATH - it ships in the backend image")
    return exe


def to_wav16k_mono(src: Path, dest: Path) -> Path:
    """Transcode anything ffmpeg can read into 16 kHz mono PCM WAV."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            _ffmpeg(), "-nostdin", "-y",
            "-i", str(src),
            "-ac", "1",
            "-ar", str(TARGET_SAMPLE_RATE),
            "-c:a", "pcm_s16le",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )
    return dest


def to_flac(src: Path, dest: Path) -> Path:
    """Losslessly compress to FLAC for upload.

    16 kHz mono WAV runs ~115 MB per hour, which is a slow upload and well past
    Gemini's 20 MB inline limit. FLAC roughly halves it with no quality loss -
    worth doing before shipping audio to any provider that takes a file upload.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            _ffmpeg(), "-nostdin", "-y",
            "-i", str(src),
            "-ac", "1",
            "-ar", str(TARGET_SAMPLE_RATE),
            "-c:a", "flac",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )
    return dest


def duration_seconds(path: Path) -> float:
    out = subprocess.run(
        [
            _ffprobe(), "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(json.loads(out.stdout)["format"]["duration"])


def extract_clip(src: Path, dest: Path, start_s: float, end_s: float) -> Path:
    """Cut [start_s, end_s) out of `src` as 16 kHz mono WAV."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            _ffmpeg(), "-nostdin", "-y",
            "-i", str(src),
            "-ss", f"{max(0.0, start_s):.3f}",
            "-to", f"{max(0.0, end_s):.3f}",
            "-ac", "1",
            "-ar", str(TARGET_SAMPLE_RATE),
            "-c:a", "pcm_s16le",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )
    return dest
