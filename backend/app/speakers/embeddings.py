"""Speaker embeddings via ECAPA-TDNN (SpeechBrain).

This is the piece no hosted ASR gives you: they return anonymous SPEAKER_0/1/2,
never "this is Priya". The model is ~20MB and runs on CPU at many times
real-time, and we only ever feed it ~30s per speaker rather than the whole
meeting - so adding speaker identification does not drag a GPU into the stack.

The model is loaded lazily and cached process-wide; the first call in a fresh
container downloads it into /models (a named docker volume).
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path

import numpy as np

from app.audio import TARGET_SAMPLE_RATE

log = logging.getLogger(__name__)

MODEL_SOURCE = "speechbrain/spkrec-ecapa-voxceleb"
MODEL_CACHE = "/models/speechbrain/spkrec-ecapa-voxceleb"
EMBEDDING_DIM = 192


NOT_INSTALLED = (
    "Speaker identification is not available in this build. The image was built "
    "without torch and speechbrain to keep it small. Rebuild with "
    "`docker compose build --build-arg WITH_SPEAKER_ID=true api` to enable it."
)


@functools.lru_cache(maxsize=1)
def _encoder():
    try:
        from speechbrain.inference.speaker import EncoderClassifier
    except ImportError as exc:
        raise RuntimeError(NOT_INSTALLED) from exc

    log.info("Loading ECAPA-TDNN speaker encoder (first call may download ~20MB)")
    return EncoderClassifier.from_hparams(
        source=MODEL_SOURCE,
        savedir=MODEL_CACHE,
        run_opts={"device": "cpu"},
    )


def warmup() -> None:
    """Pre-load the encoder so the first real request is not slow."""
    _encoder()


def _l2_normalize(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    return vec if norm == 0.0 else vec / norm


def embed_waveform(waveform: np.ndarray) -> np.ndarray:
    """Embed a mono 16 kHz float32 waveform into a unit-length 192-dim vector."""
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError(NOT_INSTALLED) from exc

    if waveform.ndim > 1:
        waveform = waveform.mean(axis=1)

    tensor = torch.from_numpy(np.ascontiguousarray(waveform, dtype=np.float32)).unsqueeze(0)
    with torch.no_grad():
        out = _encoder().encode_batch(tensor)

    vec = out.squeeze().cpu().numpy().astype(np.float32)
    return _l2_normalize(vec)


def embed_file(path: Path) -> np.ndarray:
    import soundfile as sf

    waveform, sample_rate = sf.read(str(path), dtype="float32", always_2d=False)
    if sample_rate != TARGET_SAMPLE_RATE:
        raise ValueError(
            f"expected {TARGET_SAMPLE_RATE} Hz audio, got {sample_rate} Hz - "
            "normalise with audio.to_wav16k_mono first"
        )
    return embed_waveform(waveform)


def embed_chunks(paths: list[Path]) -> np.ndarray:
    """Average several clips into one unit-length embedding.

    Averaging across a few short clips is more robust than one long clip: a
    single clip can be dominated by one phrase, one noise condition or one
    moment of unusual prosody.
    """
    vectors = [embed_file(p) for p in paths if p.exists()]
    if not vectors:
        raise ValueError("no usable audio chunks to embed")
    return _l2_normalize(np.mean(np.stack(vectors), axis=0))


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity of two already-normalised vectors, clamped to [-1, 1]."""
    return float(np.clip(np.dot(_l2_normalize(a), _l2_normalize(b)), -1.0, 1.0))
