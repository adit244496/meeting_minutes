#!/usr/bin/env python
"""Measure transcription accuracy.

Two modes, because in practice you have ground truth twice a year and never on
the meeting you actually care about.

**Scored mode** needs a human reference transcript. Reports WER and CER overall
and split by script, so you can see whether Bengali is dragging down a number
that looks acceptable in aggregate.

**Compare mode** needs no reference at all. It runs two providers against each
other and reports how much they disagree. Disagreement is not error - both can
be wrong the same way, and both can be right in different words - but it is a
strong smell, it costs nothing, and it works on real meetings where no reference
will ever exist. This is the signal you can actually run continuously.

Usage
-----
    # scored against a human reference
    python scripts/evaluate.py --hyp /tmp/out-gemini.json --ref /samples/reference.txt

    # two providers against each other, no reference
    python scripts/evaluate.py --compare /tmp/out-gemini.json /tmp/out-elevenlabs.json

The --hyp/--compare files are the JSON the spike writes with --json.

Reference format: plain UTF-8 text, one utterance per line. A leading
"NAME:" speaker prefix is stripped, so you can transcribe in the same shape you
read the recording script.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jiwer  # noqa: E402

from app.lang import analyse, summarise  # noqa: E402

# Danda and double danda end sentences in Devanagari and Bengali; Latin
# punctuation appears throughout code-switched text. None of it is spoken, so
# none of it should count against a transcript.
PUNCTUATION = re.compile(r"[।॥.,!?;:\"'`()\[\]{}<>…—–\-/\\|@#$%^&*_+=~]")
SPEAKER_PREFIX = re.compile(r"^\s*[A-Z][A-Za-z ]{0,30}:\s*")


def normalise(text: str) -> str:
    """Fold away differences that are not transcription errors.

    NFC normalisation matters more here than in English work: Devanagari and
    Bengali both have composed and decomposed forms of the same grapheme, and
    two providers can emit different byte sequences for identical text. Without
    this you measure Unicode encoding, not accuracy.
    """
    text = unicodedata.normalize("NFC", text)
    text = PUNCTUATION.sub(" ", text)
    text = text.lower()
    return " ".join(text.split())


def load_hypothesis(path: Path) -> tuple[str, list[str], str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    segments = data.get("segments", [])
    texts = [s.get("text", "") for s in segments]
    provider = data.get("provider", path.stem)
    return " ".join(texts), texts, provider


def load_reference(path: Path) -> tuple[str, list[str]]:
    lines = [
        SPEAKER_PREFIX.sub("", line).strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return " ".join(lines), lines


def by_script(texts: list[str]) -> dict[str, list[str]]:
    """Bucket utterances by dominant script, so per-language WER is visible."""
    buckets: dict[str, list[str]] = {}
    for text in texts:
        profile = analyse(text)
        key = profile.label or "unknown"
        buckets.setdefault(key, []).append(text)
    return buckets


def score(reference: str, hypothesis: str) -> dict:
    ref, hyp = normalise(reference), normalise(hypothesis)
    if not ref.strip():
        return {}
    words = jiwer.process_words(ref, hyp)
    return {
        "wer": words.wer,
        "cer": jiwer.cer(ref, hyp),
        "substitutions": words.substitutions,
        "deletions": words.deletions,
        "insertions": words.insertions,
        "hits": words.hits,
        "ref_words": len(ref.split()),
    }


def bar(value: float, width: int = 24) -> str:
    filled = min(width, int(round(value * width)))
    return "#" * filled + "." * (width - filled)


def run_scored(hyp_path: Path, ref_path: Path) -> int:
    hypothesis, hyp_segments, provider = load_hypothesis(hyp_path)
    reference, _ = load_reference(ref_path)

    print(f"\nProvider   : {provider}")
    print(f"Reference  : {ref_path.name}")

    overall = score(reference, hypothesis)
    if not overall:
        sys.exit("Reference is empty after normalisation")

    print("\n--- Overall " + "-" * 50)
    print(f"  WER  {overall['wer']:6.1%}   {bar(overall['wer'])}")
    print(f"  CER  {overall['cer']:6.1%}   {bar(overall['cer'])}")
    print(
        f"  {overall['hits']} correct, {overall['substitutions']} substituted, "
        f"{overall['deletions']} deleted, {overall['insertions']} inserted "
        f"({overall['ref_words']} reference words)"
    )
    print(
        "\n  CER is the number to trust for Hindi and Bengali. Word boundaries in\n"
        "  those scripts are partly a transcription convention, so WER punishes\n"
        "  differences that are not really errors."
    )

    # Script mix is a proxy for transliteration behaviour: if the reference
    # keeps English terms in Latin and the hypothesis does not, the shares move.
    ref_mix = summarise([reference])["scripts"]
    hyp_mix = summarise(hyp_segments)["scripts"]
    print("\n--- Script distribution " + "-" * 38)
    print(f"  {'script':<8} {'reference':>10} {'output':>10}   drift")
    for key in sorted(set(ref_mix) | set(hyp_mix)):
        r, h = ref_mix.get(key, 0.0), hyp_mix.get(key, 0.0)
        flag = "  <-- transliteration?" if h - r > 0.08 else ""
        print(f"  {key:<8} {r:>9.1%} {h:>9.1%}   {h - r:+.1%}{flag}")

    return 0


def run_compare(a_path: Path, b_path: Path) -> int:
    a_text, a_segments, a_name = load_hypothesis(a_path)
    b_text, b_segments, b_name = load_hypothesis(b_path)

    print(f"\nComparing  : {a_name}  vs  {b_name}")
    print("No reference transcript - measuring disagreement, not error.")

    result = score(a_text, b_text)
    if not result:
        sys.exit("One of the transcripts is empty")

    print("\n--- Disagreement " + "-" * 45)
    print(f"  Word-level  {result['wer']:6.1%}   {bar(result['wer'])}")
    print(f"  Char-level  {result['cer']:6.1%}   {bar(result['cer'])}")
    print(f"  {result['ref_words']} words in {a_name}, {len(normalise(b_text).split())} in {b_name}")

    a_mix = summarise(a_segments)["scripts"]
    b_mix = summarise(b_segments)["scripts"]
    print("\n--- Script distribution " + "-" * 38)
    print(f"  {'script':<8} {a_name[:9]:>10} {b_name[:9]:>10}")
    for key in sorted(set(a_mix) | set(b_mix)):
        print(f"  {key:<8} {a_mix.get(key, 0.0):>9.1%} {b_mix.get(key, 0.0):>9.1%}")

    print(
        "\n  Higher Latin share on code-switched audio usually means English terms\n"
        "  were kept in Latin rather than transliterated - normally the better\n"
        "  transcript, and the thing to check by eye before trusting the numbers."
    )

    if result["cer"] > 0.35:
        print(
            "\n  Disagreement above ~35% CER means at least one provider is doing\n"
            "  something badly wrong. Read both transcripts before drawing any\n"
            "  conclusion from the percentages."
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--hyp", type=Path, help="Spike JSON to score")
    parser.add_argument("--ref", type=Path, help="Human reference transcript (.txt)")
    parser.add_argument(
        "--compare", type=Path, nargs=2, metavar=("A", "B"),
        help="Two spike JSON files to diff against each other",
    )
    args = parser.parse_args()

    if args.compare:
        return run_compare(*args.compare)
    if args.hyp and args.ref:
        return run_scored(args.hyp, args.ref)

    parser.error("give either --hyp with --ref, or --compare A B")


if __name__ == "__main__":
    raise SystemExit(main())
