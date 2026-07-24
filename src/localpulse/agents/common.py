"""Checks shared by agents whose model-generated text goes to a human unreviewed
by the Content Agent's offering-grounding path (review replies, nudges, broadcasts)."""

from __future__ import annotations

import re

from localpulse.packs.base import VerticalPack

# Claim-shaped wording a small shop must not publish: health, medical and
# permanence claims are what draw ASCI and consumer-protection complaints, and a
# pack opts in to this check with `guardrails.forbid_health_claims`.
#
# Blocking here silently drops a slot, so these are deliberately phrases rather
# than words — ordinary trade language ("treat yourself", "fresh", "healthy
# breakfast") must never trip it. A pack adds its own vertical wording to
# `banned_terms`; this list stays generic (golden rule #2).
HEALTH_CLAIM_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bcures?\b|\bcuring\b", "claims to cure"),
    (r"\bheals?\b|\bhealing\b", "claims to heal"),
    (r"clinically\s+proven|medically\s+proven|scientifically\s+proven", "claims proof"),
    (r"doctor[- ]recommended|dermatologist[- ]approved", "claims professional endorsement"),
    (r"boosts?\s+(your\s+)?immunity|immunity[- ](booster|boosting)", "claims an immunity effect"),
    (r"no\s+side\s+effects|100%\s+safe|completely\s+safe", "claims safety"),
    (r"guaranteed\s+(results|weight|growth)", "guarantees an outcome"),
    (r"permanent\s+(results|solution|cure)", "claims permanence"),
    (r"\bweight\s+loss\b|\bfat\s+loss\b|\bdetox(ify|ifies)?\b", "claims a body effect"),
    (r"anti[- ]ag(e)?ing|removes?\s+wrinkles", "claims an anti-ageing effect"),
    (r"\bregrows?\b|\bregrowth\b", "claims regrowth"),
)


def find_health_claim(text: str) -> str | None:
    """The first claim-shaped phrase in `text`, or None. Reason strings are written
    to be shown to a shop owner, not just logged."""
    for pattern, description in HEALTH_CLAIM_PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return f"{description} ({match.group(0).strip()!r})"
    return None


def check_text_guardrails(text: str, pack: VerticalPack, noun: str = "text") -> str | None:
    """Return a rejection reason, or None if the text is safe to show the owner."""
    if not text.strip():
        return f"empty {noun}"
    if len(text) > pack.guardrails.max_caption_chars:
        return f"{noun} too long"
    lowered = text.lower()
    for term in pack.guardrails.banned_terms:
        if term.lower() in lowered:
            return f"banned term: {term}"
    if pack.guardrails.forbid_health_claims:
        claim = find_health_claim(text)
        if claim is not None:
            return f"health claim: {claim}"
    return None
