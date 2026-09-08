"""Script detection for code-switched text.

Every published ASR accuracy number - including ElevenLabs' Bengali WER tier - is
measured on *monolingual* audio. Nobody publishes code-switched numbers, and
code-switching is the dominant mode in Indian corporate meetings: technical
vocabulary stays English while grammar and connectives are Hindi or Bengali.

So we measure it ourselves. Counting Unicode script ranges is cheap, exact and
needs no model, and it answers the question that actually decides your provider:
*is the ASR keeping English terms in Latin script, or transliterating them into
Devanagari?* A provider that renders "deployment" as "डिप्लॉयमेंट" half the time
produces a transcript that is readable but unsearchable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# A secondary script holding at least this share of the letters means the
# segment is genuinely mixed, not just one stray loanword.
MIXED_THRESHOLD = 0.15

SCRIPT_TO_LANGUAGE = {"latn": "en", "deva": "hi", "beng": "bn"}


def _script_of(char: str) -> str | None:
    code = ord(char)
    if 0x0900 <= code <= 0x097F:
        return "deva"
    if 0x0980 <= code <= 0x09FF:
        return "beng"
    if ("a" <= char <= "z") or ("A" <= char <= "Z"):
        return "latn"
    return None  # digits, punctuation, whitespace carry no script signal


@dataclass
class ScriptProfile:
    counts: dict[str, int] = field(default_factory=dict)
    total: int = 0

    @property
    def shares(self) -> dict[str, float]:
        if not self.total:
            return {}
        return {s: n / self.total for s, n in self.counts.items()}

    @property
    def dominant(self) -> str | None:
        return max(self.counts, key=self.counts.get) if self.counts else None

    @property
    def present(self) -> list[str]:
        """Scripts holding a meaningful share, most-used first."""
        return [
            s
            for s, share in sorted(self.shares.items(), key=lambda kv: -kv[1])
            if share >= MIXED_THRESHOLD
        ]

    @property
    def is_mixed(self) -> bool:
        return len(self.present) > 1

    @property
    def label(self) -> str | None:
        """Compact tag for storage and display: 'latn', 'deva+latn', ..."""
        return "+".join(self.present) if self.present else None

    @property
    def language(self) -> str | None:
        """Language implied by the dominant script."""
        return SCRIPT_TO_LANGUAGE.get(self.dominant) if self.dominant else None


def analyse(text: str) -> ScriptProfile:
    counts: dict[str, int] = {}
    total = 0
    for char in text or "":
        script = _script_of(char)
        if script:
            counts[script] = counts.get(script, 0) + 1
            total += 1
    return ScriptProfile(counts=counts, total=total)


def summarise(texts: list[str]) -> dict:
    """Aggregate stats across a whole transcript - used by the spike report."""
    profiles = [analyse(t) for t in texts]
    scored = [p for p in profiles if p.total]
    if not scored:
        return {"segments": 0, "mixed_segments": 0, "mixed_share": 0.0, "scripts": {}}

    letters: dict[str, int] = {}
    for p in scored:
        for script, n in p.counts.items():
            letters[script] = letters.get(script, 0) + n
    grand = sum(letters.values()) or 1

    mixed = sum(1 for p in scored if p.is_mixed)
    return {
        "segments": len(scored),
        "mixed_segments": mixed,
        "mixed_share": mixed / len(scored),
        "scripts": {s: round(n / grand, 4) for s, n in sorted(letters.items(), key=lambda kv: -kv[1])},
    }
