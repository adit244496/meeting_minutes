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
        raise RuntimeError(
            "ffmpeg not found on PATH. Every meeting is transcoded before transcription, "
            "so install it on the server:  sudo apt install -y ffmpeg  "
            "(the Docker image already ships it), then restart the API and the worker."
        )
    return exe


def _ffprobe() -> str:
    exe = shutil.which("ffprobe")
    if not exe:
        raise RuntimeError(
            "ffprobe not found on PATH. It comes with ffmpeg:  sudo apt install -y ffmpeg  "
            "(the Docker image already ships it), then restart the API and the worker."
        )
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


def to_opus(src: Path, dest: Path, bitrate: str = "32k") -> Path:
    """Compress speech to Ogg Opus for upload.

    Around 14 MB per hour against ~55 MB for FLAC, so the upload is roughly four
    times faster. Gemini downsamples audio to 16 kbps internally anyway, so
    32 kbps Opus - which is designed for speech - loses nothing it would use.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            _ffmpeg(), "-nostdin", "-y",
            "-i", str(src),
            "-ac", "1",
            "-ar", str(TARGET_SAMPLE_RATE),
            "-c:a", "libopus",
            "-b:a", bitrate,
            "-application", "voip",
            # Fastest encoder setting. Measured on a 9-minute meeting: 6.7s at
            # level 0 against 14.6s at the default 10, files within 1% in size.
            "-compression_level", "0",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )
    return dest


def extract_sample(src: Path, dest: Path, spans: list[tuple[float, float]]) -> Path:
    """Stitch a few spans of one speaker into a short clip you can play.

    Several short spans beat one long one for recognising a voice: a single
    stretch can be one unusual sentence, while a few give the listener the
    speaker's normal range. AAC in MP4 because every browser plays it - Opus in
    WebM does not play in Safari at all.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not spans:
        raise ValueError("no spans to sample")

    trims = []
    labels = []
    for i, (start, end) in enumerate(spans):
        trims.append(f"[0:a]atrim=start={max(0.0, start):.3f}:end={max(0.0, end):.3f},asetpts=N/SR/TB[s{i}]")
        labels.append(f"[s{i}]")
    graph = "; ".join(trims) + f"; {''.join(labels)}concat=n={len(spans)}:v=0:a=1[out]"

    subprocess.run(
        [
            _ffmpeg(), "-nostdin", "-y",
            "-i", str(src),
            "-filter_complex", graph,
            "-map", "[out]",
            "-ac", "1",
            "-ar", "16000",
            "-c:a", "aac", "-b:a", "64k",
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
