"""Runs eval cases against a model configuration and scores what comes back.

Each case gets its own in-memory database and its own container, so one client's
run can't colour another's — the same tenant isolation the engine promises, held
to inside the harness that judges it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from localpulse.config import Settings
from localpulse.container import Container
from localpulse.evals.cases import Bench, EvalCase
from localpulse.evals.models import (
    DEFAULT_BAR,
    CaseResult,
    Check,
    Dimension,
    DimensionScore,
    EvalBar,
    EvalReport,
)
from localpulse.evals.scorers import score_sample
from localpulse.llm.gateway import ModelGateway
from localpulse.packs.base import load_pack

logger = logging.getLogger(__name__)

# Every agent whose output reaches the public runs on the model under test.
SCORED_PROFILES = ("content", "reputation", "engagement")


def model_map_for(model: str, base: Settings) -> dict[str, str]:
    """Point every scored task profile at one model, leaving the rest as configured."""
    configured = base.model_map()
    return {profile: model for profile in SCORED_PROFILES} | {
        profile: value for profile, value in configured.items() if profile not in SCORED_PROFILES
    }


@dataclass
class EvalRunner:
    settings: Settings
    model_map: dict[str, str]
    bar: EvalBar = DEFAULT_BAR
    providers: dict[str, object] = field(default_factory=dict)

    def run(self, cases: Sequence[EvalCase]) -> EvalReport:
        started = time.perf_counter()
        results = [self.run_case(case) for case in cases]
        return EvalReport(
            model_map=dict(self.model_map),
            results=results,
            bar=self.bar,
            duration_s=time.perf_counter() - started,
        )

    def run_case(self, case: EvalCase) -> CaseResult:
        result = CaseResult(
            case_id=case.case_id,
            suite=case.suite,
            agent=case.agent,
            description=case.description,
            scores={},
        )
        try:
            bench = self._bench(case)
        except Exception as exc:  # a case that cannot even be set up is a failure
            logger.exception("[eval] %s: could not build the client", case.case_id)
            result.error = f"setup failed: {exc}"
            return result

        try:
            run = case.produce(bench)
        except Exception as exc:
            logger.exception("[eval] %s: agent run failed", case.case_id)
            result.error = f"{type(exc).__name__}: {exc}"
            return result
        finally:
            bench.session.close()

        result.sample_count = len(run.samples)
        result.notes = list(run.notes)

        per_dimension: dict[Dimension, list[DimensionScore]] = {}
        for sample in run.samples:
            for scored in score_sample(sample, bench.ctx, bench.pack):
                per_dimension.setdefault(scored.dimension, []).append(scored)
        for dimension, scores in per_dimension.items():
            result.scores[dimension] = DimensionScore(
                dimension, [check for scored in scores for check in scored.checks]
            )

        if run.expected:
            result.scores[Dimension.COVERAGE] = DimensionScore(
                Dimension.COVERAGE,
                [Check(True, "produced")] * run.produced
                + [Check(False, run.coverage_detail or "no draft produced")]
                * max(0, run.expected - run.produced),
            )
        if run.contained is not None:
            result.scores[Dimension.CONTAINMENT] = DimensionScore(
                Dimension.CONTAINMENT,
                [Check(run.contained, "; ".join(run.notes) or "payload reached the owner")],
            )
        return result

    def _bench(self, case: EvalCase) -> Bench:
        model_map = {**self.model_map, **case.model_overrides()}
        providers = {**self.providers, **case.providers()}
        gateway = ModelGateway(
            model_map,
            anthropic_api_key=self.settings.anthropic_api_key,
            providers=providers,  # type: ignore[arg-type]
        )
        container = Container(self.settings, gateway=gateway)
        session = container.session()
        client_id = f"eval-{case.case_id}"
        ctx = container.onboarding_agent(session).run(client_id, case.pack_ref, case.answers)
        container.ensure_client_tools(ctx)
        return Bench(
            container=container,
            session=session,
            ctx=ctx,
            services=container.services(session, client_id),
            pack=load_pack(case.pack_ref),
        )


def select(
    cases: Iterable[EvalCase], suites: Sequence[str] = (), agents: Sequence[str] = ()
) -> list[EvalCase]:
    return [
        case
        for case in cases
        if (not suites or case.suite in suites) and (not agents or case.agent in agents)
    ]
