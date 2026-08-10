"""Checks shared by agents whose model-generated text goes to a human unreviewed
by the Content Agent's offering-grounding path (review replies, nudges, broadcasts)."""

from __future__ import annotations

import re
from collections.abc import Collection
from functools import lru_cache

from localpulse.context.models import ClientContext
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


# A rupee amount, in the forms a model actually writes: ₹550, ₹ 550, ₹1,100,
# ₹99.50, Rs. 550, rs 550. Bare numbers are deliberately not matched — a caption
# saying "20 varieties" or "open till 9" is not quoting a price.
PRICE_PATTERN = re.compile(r"(?:₹|\bRs\.?)\s*([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)


def prices_in(text: str) -> set[float]:
    """Every rupee amount named in `text`, normalised to 2dp."""
    found: set[float] = set()
    for match in PRICE_PATTERN.finditer(text):
        try:
            found.add(round(float(match.group(1).replace(",", "")), 2))
        except ValueError:  # pragma: no cover — the pattern only matches numerals
            continue
    return found


def grounded_prices(ctx: ClientContext, extra: Collection[float] = ()) -> set[float]:
    """What this shop may be quoted as charging: its own offering prices, plus any
    amount the owner authorised for this piece of work (a discount they asked for)."""
    prices = {round(o.price_inr, 2) for o in ctx.offerings if o.price_inr is not None}
    prices.update(round(p, 2) for p in extra)
    return prices


def find_ungrounded_price(text: str, allowed_inr: Collection[float]) -> str | None:
    """The first rupee amount in `text` the shop does not actually charge, or None.

    This is the one half of invention that can be checked deterministically. Whether
    an *item* exists needs NER or a judge model, but a price is a number, and a wrong
    one is the expensive kind of wrong — the customer arrives expecting it.
    """
    allowed = {round(p, 2) for p in allowed_inr}
    for match in PRICE_PATTERN.finditer(text):
        try:
            amount = round(float(match.group(1).replace(",", "")), 2)
        except ValueError:  # pragma: no cover — the pattern only matches numerals
            continue
        if amount not in allowed:
            return match.group(0).strip()
    return None


@lru_cache(maxsize=512)
def _item_pattern(term: str) -> re.Pattern[str]:
    """Word-boundary matcher for one lexicon term, tolerating a plural and loose
    spacing in a multi-word term. Boundaries matter: `cake` must not fire inside
    "cheesecake", so a pack lists both words separately."""
    words = r"\s+".join(re.escape(word) for word in term.split())
    return re.compile(rf"\b{words}(?:s|es)?\b", re.IGNORECASE)


def find_unstocked_item(text: str, pack: VerticalPack, ctx: ClientContext) -> str | None:
    """The first item noun in `text` that none of this shop's offerings cover, or None.

    The other half of invention, and the half a number can't settle: the Content
    Agent's grounding check asks that the intended offering *is named*, never that
    nothing else was added, so "Chocolate truffle cake ₹550 and fresh butter
    croissants" is otherwise clean. Detecting an arbitrary invented noun needs NER or
    a judge model; a pack-declared vocabulary catches the plausible ones for free.

    Deliberately lenient — a false positive silently drops a shop's post, which is
    worse than a miss the owner still gets to see.
    """
    lexicon = pack.guardrails.item_lexicon
    if not lexicon:
        return None
    sold = " | ".join(offering.name.lower() for offering in ctx.offerings)
    haystack = _without_business_name(text, ctx)
    for term in lexicon:
        if term.lower() in sold:
            continue  # the shop really does sell this
        match = _item_pattern(term).search(haystack)
        if match is not None:
            return match.group(0).strip()
    return None


def _without_business_name(text: str, ctx: ClientContext) -> str:
    """A shop may be named after something it doesn't sell — "The Nail Bar" must not
    trip its own lexicon."""
    name = ctx.business.name.strip()
    if not name:
        return text
    return re.sub(re.escape(name), " ", text, flags=re.IGNORECASE)


def check_text_guardrails(
    text: str,
    pack: VerticalPack,
    ctx: ClientContext,
    noun: str = "text",
    extra_prices: Collection[float] = (),
) -> str | None:
    """Return a rejection reason, or None if the text is safe to show the owner.

    `ctx` is required rather than optional because the price check is only as good
    as its list of real prices — a call site that could quietly omit it would lose
    the check silently, which is the failure mode this exists to prevent.
    """
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
    if pack.guardrails.require_price_grounding:
        price = find_ungrounded_price(text, grounded_prices(ctx, extra_prices))
        if price is not None:
            return f"price the shop does not charge: {price}"
    return None
