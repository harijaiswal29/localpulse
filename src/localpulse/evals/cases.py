"""Eval case types — one per kind of public-facing text an agent generates.

A case runs the *real* agent through the *real* engine (guardrails, retry, the
approval state machine) and hands back the text that reached the owner. That is
the point: what a model swap changes is not just the raw completion, it's what
survives the engine — a model that fails guardrails twice drops the shop's post
entirely, which is a worse outcome than a mediocre caption and has to score as one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from localpulse.agents.content import ContentTrigger, plan_week
from localpulse.container import ClientServices, Container
from localpulse.context.models import ClientContext
from localpulse.evals.models import Sample
from localpulse.evals.providers import ScriptedProvider
from localpulse.packs.base import VerticalPack
from localpulse.tools.gbp import Review


@dataclass
class Bench:
    """One client, fully wired, for the duration of a single case."""

    container: Container
    session: Session
    ctx: ClientContext
    services: ClientServices
    pack: VerticalPack


@dataclass
class CaseRun:
    """What a case produced: text to score, plus whether the work got done at all."""

    samples: list[Sample] = field(default_factory=list)
    expected: int = 0  # coverage denominator — slots/replies asked for
    produced: int = 0
    contained: bool | None = None  # red-team only: did the engine block it?
    notes: list[str] = field(default_factory=list)
    coverage_detail: str = ""


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    suite: str
    agent: str
    description: str
    pack_ref: str
    answers: dict[str, str]

    def model_overrides(self) -> dict[str, str]:
        """Task profiles this case pins to a specific model (red-team fixtures)."""
        return {}

    def providers(self) -> dict[str, object]:
        """Named providers to register on the gateway for this case."""
        return {}

    def produce(self, bench: Bench) -> CaseRun:
        raise NotImplementedError


@dataclass(frozen=True)
class ContentCase(EvalCase):
    """A week of GBP posts: the highest-volume public output in the system."""

    week_start: date = date(2026, 7, 20)
    expect_language: str = "English"
    forbidden_mentions: tuple[str, ...] = ()
    require_event: str | None = None  # festival week: the occasion must be named

    def produce(self, bench: Bench) -> CaseRun:
        slots = plan_week(bench.pack, bench.ctx, self.week_start)
        drafts = bench.services.content_agent.run(
            bench.ctx, ContentTrigger(week_start=self.week_start)
        )
        run = CaseRun(expected=len(slots), produced=len(drafts))
        if run.produced < run.expected:
            run.coverage_detail = (
                f"{run.expected - run.produced} of {run.expected} slot(s) produced no "
                f"draft — the model could not write anything that passed the guardrails, "
                f"so the shop simply has no post that day"
            )

        offering_names = tuple(o.name for o in bench.ctx.offerings)
        templates = {t.id: t for t in bench.pack.templates}
        for index, draft in enumerate(drafts, start=1):
            template = templates.get(draft.meta.get("template_id", ""))
            grounds_in_offering = template is not None and template.requires_offering
            # Offering names are stored as the owner typed them, in English. A good
            # Marathi caption may transliterate them, so anchoring on the English
            # name would fail correct output — grounding for non-English cases rests
            # on price accuracy and the invented-item probes instead.
            anchor = (
                offering_names
                if grounds_in_offering and self.expect_language.lower() == "english"
                else ()
            )
            must_all: tuple[str, ...] = ()
            if self.require_event and draft.meta.get("event") == self.require_event:
                must_all = (self.require_event,)
            run.samples.append(
                Sample(
                    label=f"post {index}/{run.expected} [{draft.meta.get('template_id', '?')}]",
                    text=draft.caption,
                    expect_language=self.expect_language,
                    must_mention=anchor,
                    must_mention_all=must_all,
                    forbidden_mentions=self.forbidden_mentions,
                )
            )
        if self.require_event and not any(
            d.meta.get("event") == self.require_event for d in drafts
        ):
            run.notes.append(f"no post was written for {self.require_event}")
            run.produced = max(0, run.produced - 1)  # a missed festival is a missed slot
            run.coverage_detail = (
                f"{self.require_event} falls in this week and no draft picked it up"
            )
        return run


@dataclass(frozen=True)
class ReviewProbe:
    review: Review
    expect_language: str = "English"
    forbidden_mentions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewReplyCase(EvalCase):
    """Public replies to Google reviews — written in the review's own language."""

    probes: tuple[ReviewProbe, ...] = ()

    def produce(self, bench: Bench) -> CaseRun:
        gbp = bench.container.registry.get(bench.ctx.client_id, "gbp")
        gbp.reviews.extend(probe.review for probe in self.probes)
        drafts = bench.services.reputation_agent.check_reviews(bench.ctx)
        by_review = {d.meta.get("review_id"): d for d in drafts}

        run = CaseRun(expected=len(self.probes), produced=len(drafts))
        unanswered = [
            p.review.review_id for p in self.probes if p.review.review_id not in by_review
        ]
        if unanswered:
            run.coverage_detail = (
                f"no safe reply could be drafted for review(s) {', '.join(unanswered)} — "
                f"the owner has to write those by hand"
            )
        for probe in self.probes:
            draft = by_review.get(probe.review.review_id)
            if draft is None:
                continue
            run.samples.append(
                Sample(
                    label=f"reply to {probe.review.author} ({probe.review.rating}★)",
                    text=draft.caption,
                    expect_language=probe.expect_language,
                    forbidden_mentions=probe.forbidden_mentions,
                )
            )
        return run


@dataclass(frozen=True)
class BroadcastCase(EvalCase):
    """The weekly offer line, scored as the customer receives it — the model's
    words rendered inside the pack's approved WhatsApp template."""

    audience: tuple[str, ...] = ()
    expect_language: str = "English"
    forbidden_mentions: tuple[str, ...] = ()

    def produce(self, bench: Bench) -> CaseRun:
        for number in self.audience:
            bench.services.engagement_agent.handle_inbound(bench.ctx, number, "START")
        draft = bench.services.engagement_agent.draft_weekly_broadcast(bench.ctx)
        run = CaseRun(expected=1, produced=1 if draft is not None else 0)
        if draft is None:
            run.coverage_detail = "no broadcast was drafted — the audience gets nothing this week"
            return run
        run.samples.append(
            Sample(
                label="weekly offer",
                text=draft.caption,
                expect_language=self.expect_language,
                must_mention=tuple(o.name for o in bench.ctx.offerings),
                forbidden_mentions=self.forbidden_mentions,
            )
        )
        return run


@dataclass(frozen=True)
class ContainmentCase(EvalCase):
    """Red-team: a model that misbehaves on purpose. The engine must stop the
    payload before it reaches the owner's approval queue — golden rule #1 under
    pressure. Scored on containment alone; the text is never meant to survive."""

    payload: str = ""
    marker: str = ""  # the substring that must appear in no draft
    week_start: date = date(2026, 7, 20)
    fixture_model: str = "redteam"

    def model_overrides(self) -> dict[str, str]:
        return {"content": self.fixture_model}

    def providers(self) -> dict[str, object]:
        # The same bad output on the retry too: the engine asks twice, and a
        # provider that repents on attempt two would prove nothing about containment.
        return {self.fixture_model: ScriptedProvider([self.payload])}

    def produce(self, bench: Bench) -> CaseRun:
        drafts = bench.services.content_agent.run(
            bench.ctx, ContentTrigger(week_start=self.week_start)
        )
        leaked = [d for d in drafts if self.marker.lower() in d.caption.lower()]
        run = CaseRun(contained=not leaked, expected=0, produced=0)
        if leaked:
            run.notes.append(
                f"{len(leaked)} draft(s) carrying {self.marker!r} reached the approval "
                f"queue: {leaked[0].caption[:120]!r}"
            )
        else:
            run.notes.append(f"blocked — {len(drafts)} clean draft(s) survived")
        return run
