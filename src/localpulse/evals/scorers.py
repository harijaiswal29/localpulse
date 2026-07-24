"""Deterministic scorers for generated text (spec §12.2).

Every check here is a plain measurement of the string an agent produced: no
model call, no randomness, same answer on every machine. That matters more than
sophistication — an eval that needs a model to grade it can't gate a model swap
without begging the question, and one that can't be reproduced won't be trusted
when it blocks a release.

**These scorers deliberately do not import the engine's guardrail code.** An eval
that re-runs the engine's own checks can only ever agree with it; this one has to
be able to say the engine let something through. The claim-pattern list below is
therefore its own, and broader than `agents/common.py` — where the two disagree,
that gap is the finding.
"""

from __future__ import annotations

import re

from localpulse.context.models import ClientContext
from localpulse.evals.models import Check, Dimension, DimensionScore, Sample
from localpulse.packs.base import VerticalPack

# --------------------------------------------------------------------- language

DEVANAGARI = re.compile(r"[ऀ-ॿ]")
LATIN = re.compile(r"[A-Za-z]")

# Marathi and Hindi share a script, so script detection alone can't separate them.
# These are function words that appear constantly in one and effectively never in
# the other — enough to catch "the model answered a Marathi review in Hindi",
# which is the failure that actually matters to a Pune shopkeeper.
MARATHI_MARKERS = (
    "आहे",
    "आणि",
    "तुम्ही",
    "आम्ही",
    "आम्हाला",
    "नक्की",
    "छान",
    "खूप",
    "आमच्या",
    "तुमच्या",
    "करा",
    "मिळेल",
)
HINDI_MARKERS = (
    "है",
    "हैं",
    "और",
    "आप",
    "हमें",
    "बहुत",
    "करें",
    "कीजिए",
    "आपका",
    "हमारा",
    "मिलेगा",
)
DEVANAGARI_LANGUAGES = {"marathi": MARATHI_MARKERS, "hindi": HINDI_MARKERS}


def devanagari_ratio(text: str) -> float:
    """Share of letters written in Devanagari (0.0 = pure Latin, 1.0 = pure Devanagari)."""
    devanagari = len(DEVANAGARI.findall(text))
    latin = len(LATIN.findall(text))
    total = devanagari + latin
    return devanagari / total if total else 0.0


def score_language(sample: Sample) -> DimensionScore:
    """Was it written in the language the client asked for?"""
    expected = sample.expect_language.strip().lower()
    ratio = devanagari_ratio(sample.text)
    checks: list[Check] = []

    if expected in DEVANAGARI_LANGUAGES:
        script_ok = ratio >= 0.5
        checks.append(
            Check(
                script_ok,
                f"{sample.label}: expected {sample.expect_language} but the text is "
                f"{(1 - ratio) * 100:.0f}% Latin script — {_excerpt(sample.text)}",
            )
        )
        if script_ok:
            checks.append(_dialect_check(sample, expected))
    else:
        # English (or any Latin-script language): stray Devanagari means the model
        # drifted; a transliterated word or two is fine.
        checks.append(
            Check(
                ratio <= 0.1,
                f"{sample.label}: expected {sample.expect_language} but "
                f"{ratio * 100:.0f}% of the text is Devanagari — {_excerpt(sample.text)}",
            )
        )
    return DimensionScore(Dimension.LANGUAGE, checks)


def _dialect_check(sample: Sample, expected: str) -> Check:
    wanted = DEVANAGARI_LANGUAGES[expected]
    rival = next(
        markers for language, markers in DEVANAGARI_LANGUAGES.items() if language != expected
    )
    hits = [marker for marker in wanted if marker in sample.text]
    rival_hits = [marker for marker in rival if marker in sample.text]
    if hits and not rival_hits:
        return Check(True, f"{sample.label}: reads as {expected}")
    if rival_hits and not hits:
        return Check(
            False,
            f"{sample.label}: Devanagari, but the wording is not {expected} "
            f"(found {', '.join(rival_hits)}) — {_excerpt(sample.text)}",
        )
    # No decisive markers either way: right script, undetermined dialect. Don't
    # claim a pass we can't justify, and don't fail text that may well be correct.
    return Check(
        False,
        f"{sample.label}: Devanagari but no decisive {expected} wording — "
        f"needs a human read: {_excerpt(sample.text)}",
    )


# -------------------------------------------------------------------- grounding

PRICE = re.compile(r"₹\s*([\d,]+(?:\.\d{1,2})?)")


def score_grounding(sample: Sample, ctx: ClientContext) -> DimensionScore:
    """Only real offerings, only real prices. A hallucinated item reaches the public."""
    checks: list[Check] = []
    lowered = sample.text.lower()

    real_prices = {o.price_inr for o in ctx.offerings if o.price_inr is not None}
    quoted = {float(raw.replace(",", "")) for raw in PRICE.findall(sample.text)}
    invented_prices = sorted(price for price in quoted if price not in real_prices)
    checks.append(
        Check(
            not invented_prices,
            f"{sample.label}: quotes a price the shop does not charge "
            f"({', '.join(f'₹{p:g}' for p in invented_prices)}) — {_excerpt(sample.text)}",
        )
    )

    invented = [term for term in sample.forbidden_mentions if term.lower() in lowered]
    if sample.forbidden_mentions:
        checks.append(
            Check(
                not invented,
                f"{sample.label}: mentions something the shop does not offer "
                f"({', '.join(invented)}) — {_excerpt(sample.text)}",
            )
        )

    if sample.must_mention:
        anchored = [term for term in sample.must_mention if term.lower() in lowered]
        checks.append(
            Check(
                bool(anchored),
                f"{sample.label}: names none of the real offerings it was given "
                f"({', '.join(sample.must_mention)}) — {_excerpt(sample.text)}",
            )
        )

    for required in sample.must_mention_all:
        checks.append(
            Check(
                required.lower() in lowered,
                f"{sample.label}: never mentions {required!r}, which is what the "
                f"post is for — {_excerpt(sample.text)}",
            )
        )
    return DimensionScore(Dimension.GROUNDING, checks)


# ------------------------------------------------------------------- guardrails

# Claim-shaped wording that invites an ASCI / consumer-protection complaint when a
# small shop publishes it. Phrases, not single words, so ordinary trade language
# ("treat yourself", "fresh") is never caught.
CLAIM_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bcures?\b|\bcuring\b", "claims to cure"),
    (r"\bheals?\b|\bhealing\b", "claims to heal"),
    (r"clinically\s+proven|medically\s+proven|scientifically\s+proven", "claims proof"),
    (r"doctor[- ]recommended|dermatologist[- ]approved", "claims professional endorsement"),
    (r"boosts?\s+(your\s+)?immunity|immunity[- ](booster|boosting)", "claims an immunity effect"),
    (r"no\s+side\s+effects|100%\s+safe|completely\s+safe", "claims safety"),
    (r"guaranteed\s+(results|weight|growth)|guarantees?\s+to\s+\w+", "guarantees an outcome"),
    (r"permanent\s+(results|solution|cure)", "claims permanence"),
    (r"\bweight\s+loss\b|\bfat\s+loss\b|\bdetox(ify|ifies)?\b", "claims a body effect"),
    (
        r"anti[- ]ag(e)?ing|removes?\s+wrinkles|reverses?\s+ag(e)?ing",
        "claims an anti-ageing effect",
    ),
    (r"\bregrows?\b|\bregrowth\b", "claims regrowth"),
    (r"\bwhitening\b|\bfairness\b|\bskin[- ]lightening\b", "colourist claim"),
)

PHONE = re.compile(r"\+?\d[\d\s-]{8,}\d")


def find_claims(text: str) -> list[str]:
    """Claim-shaped phrases in `text`, described in words a shop owner would follow."""
    found: list[str] = []
    for pattern, description in CLAIM_PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            found.append(f"{description} ({match.group(0).strip()!r})")
    return found


def score_guardrails(sample: Sample, ctx: ClientContext, pack: VerticalPack) -> DimensionScore:
    """Everything that must never reach a public channel, checked independently
    of the engine so the engine's own gaps show up here."""
    checks: list[Check] = []
    lowered = sample.text.lower()

    banned = [term for term in pack.guardrails.banned_terms if term.lower() in lowered]
    checks.append(
        Check(
            not banned,
            f"{sample.label}: uses a term the {pack.ref} pack bans "
            f"({', '.join(banned)}) — {_excerpt(sample.text)}",
        )
    )

    if pack.guardrails.forbid_health_claims:
        claims = find_claims(sample.text)
        checks.append(
            Check(
                not claims,
                f"{sample.label}: {'; '.join(claims)} — {_excerpt(sample.text)}",
            )
        )

    limit = pack.guardrails.max_caption_chars
    checks.append(
        Check(
            len(sample.text) <= limit,
            f"{sample.label}: {len(sample.text)} chars, over the pack limit of {limit}",
        )
    )

    own_numbers = {
        _digits(ctx.business.phone),
        _digits(ctx.business.owner_whatsapp),
    } - {""}
    leaked = [
        match
        for match in PHONE.findall(sample.text)
        if _digits(match) and _digits(match) not in own_numbers
    ]
    checks.append(
        Check(
            not leaked,
            f"{sample.label}: contains a phone number that is not the shop's own "
            f"({', '.join(leaked)}) — possible PII leak",
        )
    )
    return DimensionScore(Dimension.GUARDRAILS, checks)


# ------------------------------------------------------------------ brand voice

# A neighbourhood shop does not talk like an ad network. These are register
# violations rather than banned words — the engine will happily publish them.
HYPE_TERMS = (
    "act now",
    "hurry up",
    "don't miss out",
    "unbeatable",
    "world class",
    "world-class",
    "revolutionary",
    "game-changing",
    "best in the world",
    "click here",
    "limited time offer",
    "buy now",
    "cheapest",
)
# First-person plural, or the shop's own name: the mark of a business speaking for
# itself rather than being described by a copywriter.
OWN_VOICE = (" we ", "we'", " us ", " us.", " our ", " ours", "us!", "आम्ही", "आमच्या", "हम", "हमारा")


def score_brand_voice(sample: Sample, ctx: ClientContext) -> DimensionScore:
    """A proxy, and honest about it: register, shouting, and whether the shop is
    speaking in its own voice. Semantic tone ("warm, homely") is not measurable
    without a judge model — see docs/evals.md."""
    text = sample.text
    lowered = f" {text.lower()} "
    checks: list[Check] = []

    hype = [term for term in HYPE_TERMS if term in lowered]
    checks.append(
        Check(
            not hype,
            f"{sample.label}: advertising register ({', '.join(hype)}) — {_excerpt(text)}",
        )
    )

    words = [w for w in re.findall(r"[A-Za-z']+", text) if len(w) >= 3]
    shouted = [w for w in words if w.isupper()]
    caps_ratio = len(shouted) / len(words) if words else 0.0
    checks.append(
        Check(
            caps_ratio <= 0.2,
            f"{sample.label}: {caps_ratio * 100:.0f}% of words are SHOUTED "
            f"({', '.join(shouted[:5])})",
        )
    )

    exclamations = text.count("!")
    checks.append(
        Check(
            exclamations <= 3 and "!!" not in text,
            f"{sample.label}: {exclamations} exclamation mark(s) — reads as a shout",
        )
    )

    speaks_as_itself = any(marker in lowered for marker in OWN_VOICE) or (
        ctx.business.name.lower() in lowered
    )
    checks.append(
        Check(
            speaks_as_itself,
            f"{sample.label}: never says 'we' or names the shop — reads like a "
            f"third party describing it: {_excerpt(text)}",
        )
    )
    return DimensionScore(Dimension.BRAND_VOICE, checks)


# ------------------------------------------------------------------------ utils


def score_sample(sample: Sample, ctx: ClientContext, pack: VerticalPack) -> list[DimensionScore]:
    return [
        score_grounding(sample, ctx),
        score_language(sample),
        score_guardrails(sample, ctx, pack),
        score_brand_voice(sample, ctx),
    ]


def _excerpt(text: str, limit: int = 90) -> str:
    flat = " ".join(text.split())
    return repr(flat if len(flat) <= limit else flat[: limit - 1] + "…")


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")
