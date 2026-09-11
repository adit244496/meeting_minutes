#!/usr/bin/env python
"""Generate a synthetic mixed-language test file - FOR SMOKE TESTING ONLY.

    !!  This is NOT a quality benchmark. Do not judge a provider by it.  !!

espeak-ng produces robotic, monotone, perfectly-clean speech with none of the
things that actually make meeting transcription hard: no room reverb, no
far-field attenuation, no overlapping speakers, no natural code-switching
prosody, no accents. A provider can ace this file and still fail on your real
meetings, or vice versa.

What it IS good for: proving the pipeline runs end to end before you gather
three colleagues into a room. It exercises the untested path - upload,
transcribe, diarize, script-detect, persist - and will surface an SDK signature
mismatch or a bad API key in seconds.

For the real benchmark, read RECORDING_SCRIPT.md aloud on your own microphone.

Usage
-----
    # native (needs: sudo apt install -y espeak-ng ffmpeg)
    cd ~/meeting_minutes/meeting_minutes
    backend/venv/bin/python samples/generate_test_audio.py
    cd backend && venv/bin/python scripts/spike.py ../samples/synthetic_mixed.wav

    # docker
    docker compose run --rm api python /samples/generate_test_audio.py
    docker compose run --rm api python scripts/spike.py /samples/synthetic_mixed.wav

The file it writes sits next to this script, whichever way you run it.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

OUT = Path(__file__).resolve().parent / 'synthetic_mixed.wav'

# (espeak voice, pitch, speed, text) - pitch and speed vary per speaker so the
# diarizer has something to separate. Same content as RECORDING_SCRIPT.md.
TURNS = [
    ("en", 55, 165, "Right, let's start. We're here to review the payment gateway "
                    "integration and decide whether we can ship on the twenty-third."),
    ("hi", 30, 150, "हाँ, तो deployment का status ये है कि staging पर सब कुछ काम कर रहा है, "
                    "but production database migration अभी pending है।"),
    ("bn", 75, 155, "আমাদের দিক থেকে API integration টা complete, কিন্তু load testing "
                    "এখনো বাকি আছে।"),
    ("en", 55, 165, "Okay so the blocker is testing. How much time for the migration?"),
    ("hi", 30, 150, "लगभग दो दिन। अगर हम बुधवार को शुरू करें तो शुक्रवार तक हो जाएगा।"),
    ("bn", 75, 155, "কিন্তু তুমি যদি Friday তে finish করো, তাহলে আমার regression suite "
                    "চালানোর জন্য শুধু একটা weekend থাকবে।"),
    ("en", 55, 165, "Let's move the launch to the thirtieth. Rahul owns the migration, "
                    "Ananya owns load testing and regression."),
    ("hi", 30, 150, "ठीक है। मैं Jenkins pipeline भी update कर दूँगा।"),
    ("bn", 75, 155, "একটা প্রশ্ন আছে — Razorpay এর sandbox credentials কে renew করবে?"),
    ("en", 55, 165, "Good question, nobody owns that yet. I'll find out. Thanks everyone."),
]


def require(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        sys.exit(
            f"{binary} not found.\n"
            "  native:  sudo apt install -y espeak-ng ffmpeg\n"
            "  docker:  docker compose build api   (both ship in the image)"
        )
    return path


def main() -> int:
    espeak, ffmpeg = require("espeak-ng"), require("ffmpeg")

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        parts = []

        for i, (voice, pitch, speed, text) in enumerate(TURNS):
            clip = tmpdir / f"turn_{i:02d}.wav"
            subprocess.run(
                [espeak, "-v", voice, "-p", str(pitch), "-s", str(speed),
                 "-w", str(clip), text],
                check=True, capture_output=True,
            )
            parts.append(clip)
            print(f"  [{voice}] turn {i + 1}/{len(TURNS)}")

            # Half a second of silence between turns, so the segmenter has a
            # boundary to find.
            gap = tmpdir / f"gap_{i:02d}.wav"
            subprocess.run(
                [ffmpeg, "-nostdin", "-y", "-f", "lavfi",
                 "-i", "anullsrc=r=22050:cl=mono", "-t", "0.5", str(gap)],
                check=True, capture_output=True,
            )
            parts.append(gap)

        listing = tmpdir / "parts.txt"
        listing.write_text(
            "\n".join(f"file '{p.as_posix()}'" for p in parts), encoding="utf-8"
        )

        OUT.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [ffmpeg, "-nostdin", "-y", "-f", "concat", "-safe", "0",
             "-i", str(listing), "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", str(OUT)],
            check=True, capture_output=True,
        )

    print(f"\nWrote {OUT}")
    print("\nSmoke test only - this proves the pipeline runs, not that a provider")
    print("is accurate. Use RECORDING_SCRIPT.md for the real benchmark.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
