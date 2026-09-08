#!/usr/bin/env python
"""Pipeline spike - the step that de-risks the whole project.

No database, no auth, no UI, no queue. Point it at an audio file and it runs
exactly the pipeline the production worker runs, printing a speaker-labelled
transcript. Run this against real Bengali and Hindi audio BEFORE building
anything on top: if the transcript quality is not acceptable here, no amount of
application code will fix it.

Usage
-----
    # Transcribe and diarize
    python scripts/spike.py samples/meeting.m4a

    # ...and identify speakers against enrolled samples
    python scripts/spike.py samples/meeting.m4a \\
        --enroll "Priya Sharma=samples/priya.wav" \\
        --enroll "Rahul Das=samples/rahul.wav"

    # ...and generate the minutes
    python scripts/spike.py samples/meeting.m4a --minutes

    # Compare providers on the same audio - this is the benchmark that should
    # pick your default, not vendor marketing.
    python scripts/spike.py samples/meeting.m4a --provider elevenlabs --json out-11l.json
    python scripts/spike.py samples/meeting.m4a --provider sarvam     --json out-sarvam.json
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.asr import get_provider  # noqa: E402
from app.audio import duration_seconds, to_wav16k_mono  # noqa: E402
from app.lang import analyse, summarise  # noqa: E402
from app.speakers.embeddings import embed_file  # noqa: E402
from app.speakers.identify import (  # noqa: E402
    EnrolledVoice,
    identify_speakers,
    label_speakers_only,
)


def timestamp(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def load_enrollments(pairs: list[str], tmpdir: Path) -> list[EnrolledVoice]:
    voices: dict[str, EnrolledVoice] = {}
    for i, pair in enumerate(pairs):
        if "=" not in pair:
            raise SystemExit(f"--enroll expects 'Name=path/to/sample.wav', got {pair!r}")
        name, raw_path = pair.split("=", 1)
        name = name.strip()
        source = Path(raw_path.strip())
        if not source.exists():
            raise SystemExit(f"Enrollment sample not found: {source}")

        wav = to_wav16k_mono(source, tmpdir / f"enroll_{i:02d}.wav")
        embedding = embed_file(wav)
        # Repeating a name accumulates multiple samples for that person, which
        # is exactly what you want - 3 x 20s beats 1 x 60s.
        voices.setdefault(name, EnrolledVoice(user_id=name, display_name=name, embeddings=[]))
        voices[name].embeddings.append(embedding)
        print(f"  enrolled {name}: {duration_seconds(wav):.1f}s from {source.name}")
    return list(voices.values())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("audio", type=Path, help="Meeting audio (any format ffmpeg reads)")
    parser.add_argument("--provider", default=None, help="elevenlabs | sarvam | gemini")
    parser.add_argument(
        "--language",
        default=None,
        help="Optional hint (en/hi/bn). Omit for code-switched meetings - forcing "
        "one language is what produces garbage transliteration.",
    )
    parser.add_argument(
        "--enroll",
        action="append",
        default=[],
        metavar="NAME=FILE",
        help="Enrollment sample; repeatable, and repeat a name for multiple samples",
    )
    parser.add_argument("--threshold", type=float, default=None, help="Cosine match threshold")
    parser.add_argument("--minutes", action="store_true", help="Also generate minutes via Claude")
    parser.add_argument("--json", type=Path, default=None, help="Write full results to a JSON file")
    args = parser.parse_args()

    if not args.audio.exists():
        raise SystemExit(f"Audio file not found: {args.audio}")

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)

        print(f"[1/5] Normalising {args.audio.name} to 16 kHz mono...")
        wav = to_wav16k_mono(args.audio, tmpdir / "audio.wav")
        seconds = duration_seconds(wav)
        print(f"      {seconds / 60:.1f} minutes of audio")

        provider = get_provider(args.provider)
        print(f"[2/5] Transcribing via {provider.name} (this is the slow, paid step)...")
        started = time.monotonic()
        result = provider.transcribe(wav, language_hint=args.language)
        elapsed = time.monotonic() - started
        print(
            f"      {len(result.segments)} segments, "
            f"{len(result.speaker_labels)} speakers, "
            f"language={result.language}, {elapsed:.1f}s wall clock"
        )
        if not result.segments:
            raise SystemExit("ASR returned nothing - check the API key and audio file")

        print(f"[3/5] Loading {len(args.enroll)} enrollment sample(s)...")
        enrolled = load_enrollments(args.enroll, tmpdir) if args.enroll else []

        # With nothing enrolled there is nothing to match against, so skip the
        # embedding work entirely rather than burning CPU to produce Unknowns.
        if enrolled:
            print("[4/5] Identifying speakers...")
            resolutions = identify_speakers(
                wav, result.segments, enrolled, threshold=args.threshold
            )
        else:
            print("[4/5] No enrollments given - separating speakers only")
            resolutions = label_speakers_only(result.segments)
        names = {r.speaker_label: r.display_name for r in resolutions}

        print("\n--- Speakers " + "-" * 55)
        for r in sorted(resolutions, key=lambda r: -r.speaking_seconds):
            share = (r.speaking_seconds / seconds * 100) if seconds else 0.0
            match = f"confidence {r.confidence:.3f}" if r.user_id else "no voiceprint match"
            print(
                f"  {r.speaker_label:<14} -> {r.display_name:<24} "
                f"{r.speaking_seconds:>7.1f}s ({share:4.1f}%)  {match}"
            )

        print("\n--- Transcript " + "-" * 53)
        for seg in result.segments:
            speaker = names.get(seg.speaker, seg.speaker)
            lang = f" [{seg.language}]" if seg.language else ""
            print(f"[{timestamp(seg.start)}] {speaker}{lang}: {seg.text}")

        payload = {
            "audio": str(args.audio),
            "duration_seconds": round(seconds, 2),
            "provider": result.provider,
            "language": result.language,
            "asr_seconds": round(elapsed, 1),
            "code_switching": stats,
            "speakers": [
                {
                    "label": r.speaker_label,
                    "name": r.display_name,
                    "matched": bool(r.user_id),
                    "confidence": r.confidence,
                    "speaking_seconds": r.speaking_seconds,
                }
                for r in resolutions
            ],
            "segments": [
                {
                    "start": round(s.start, 2),
                    "end": round(s.end, 2),
                    "speaker": names.get(s.speaker, s.speaker),
                    "language": s.language,
                    "scripts": analyse(s.text).label,
                    "text": s.text,
                }
                for s in result.segments
            ],
        }

        if args.minutes:
            print("\n[5/5] Generating minutes via Claude...")
            from app.minutes.generate import generate_minutes

            generated = generate_minutes(
                title=args.audio.stem.replace("_", " ").title(),
                segments=[
                    {
                        "start_ms": int(s.start * 1000),
                        "speaker": names.get(s.speaker, s.speaker),
                        "text": s.text,
                        "language": s.language,
                    }
                    for s in result.segments
                ],
            )
            payload["minutes"] = generated.model_dump()

            print("\n--- Summary " + "-" * 56)
            print(generated.summary)
            if generated.decisions:
                print("\n--- Decisions " + "-" * 54)
                for d in generated.decisions:
                    print(f"  * {d.decision}" + (f"  ({d.decided_by})" if d.decided_by else ""))
            if generated.action_items:
                print("\n--- Action items " + "-" * 51)
                for a in generated.action_items:
                    due = f" - due {a.due}" if a.due else ""
                    print(f"  [ ] {a.task}  ({a.owner}, {a.priority}{due})")
            if generated.open_questions:
                print("\n--- Open questions " + "-" * 49)
                for q in generated.open_questions:
                    print(f"  ? {q}")
        else:
            print("\n[5/5] Skipping minutes (pass --minutes to generate them)")

        if args.json:
            args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\nWrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
