"""Checks shared by agents whose model-generated text goes to a human unreviewed
by the Content Agent's offering-grounding path (review replies, nudges, broadcasts)."""

from __future__ import annotations

from localpulse.packs.base import VerticalPack


def check_text_guardrails(text: str, pack: VerticalPack) -> str | None:
    """Return a rejection reason, or None if the text is safe to show the owner."""
    if not text.strip():
        return "empty text"
    if len(text) > pack.guardrails.max_caption_chars:
        return "text too long"
    lowered = text.lower()
    for term in pack.guardrails.banned_terms:
        if term.lower() in lowered:
            return f"banned term: {term}"
    return None
