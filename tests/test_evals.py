"""Tests for the agent eval harness (spec §12.2).

An eval suite is only worth the CI minutes if it can fail. Most of what follows
plants a specific defect in a sample and insists the scorer catches it — because
a harness that scores 1.00 on everything would gate nothing while looking like
it does.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from localpulse.context.models import BrandVoice, BusinessProfile, ClientContext, Offering
from localpulse.evals.dataset import ALL_CASES
from localpulse.evals.models import (
    CaseResult,
    Check,
    Dimension,
    DimensionScore,
    EvalBar,
    EvalReport,
    Sample,
    compare_to_baseline,
)
from localpulse.evals.providers import ScriptedProvider
from localpulse.evals.runner import EvalRunner, model_map_for, select
from localpulse.evals.scorers import (
    devanagari_ratio,
    find_claims,
    score_brand_voice,
    score_grounding,
    score_guardrails,
    score_language,
)
from localpulse.llm.gateway import ModelGateway
from localpulse.packs.base import load_pack
from tests.conftest import make_test_settings

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def bakery_ctx() -> ClientContext:
    return ClientContext(
        client_id="scorer-1",
        vertical_pack_ref="bakery",
        business=BusinessProfile(
            name="Mane's Bakehouse",
            category="Bakery / Cake Shop",
            city="Pune",
            phone="+912025551234",
            owner_whatsapp="+919812345678",
        ),
        brand_voice=BrandVoice(tone=["warm", "homely"], languages=["English"]),
        offerings=[
            Offering(name="Chocolate truffle cake", price_inr=550),
            Offering(name="Modak box", price_inr=300),
        ],
    )


@pytest.fixture
def bakery_pack():
    return load_pack("bakery")


def sample(text: str, **kwargs) -> Sample:
    return Sample(label="sample", text=text, **kwargs)


class PaddingProvider:
    """A model that grounds its caption correctly and then pads it with an item the
    shop has never sold — the failure mode the engine is structurally blind to."""

    def complete(self, prompt: str, system: str, max_tokens: int) -> str:
        facts = dict(re.findall(r"^(\w+):\s*(.+)$", prompt, flags=re.MULTILINE))
        return (
            f"{facts.get('offering', 'Our bakes')} and fresh butter croissants at "
            f"{facts.get('business', 'our shop')} — message us to order."
        )


class TestGroundingScorer:
    """Grounding is the dimension that protects a shop from its own model."""

    def test_a_real_offering_at_its_real_price_passes(self, bakery_ctx):
        scored = score_grounding(
            sample(
                "Fresh Chocolate truffle cake (₹550) at Mane's Bakehouse today.",
                must_mention=("Chocolate truffle cake", "Modak box"),
            ),
            bakery_ctx,
        )
        assert scored.score == 1.0

    def test_an_invented_price_is_caught(self, bakery_ctx):
        scored = score_grounding(sample("Chocolate truffle cake, today only ₹199!"), bakery_ctx)
        assert scored.score < 1.0
        assert "₹199" in " ".join(scored.failures)

    def test_an_invented_offering_is_caught(self, bakery_ctx):
        """The engine cannot catch this — its grounding check only asks that the
        intended offering is named, not that nothing else was bolted on."""
        scored = score_grounding(
            sample(
                "Chocolate truffle cake and our new butter croissants — order today.",
                must_mention=("Chocolate truffle cake",),
                forbidden_mentions=("croissant", "pizza"),
            ),
            bakery_ctx,
        )
        assert scored.score < 1.0
        assert "does not offer" in " ".join(scored.failures)

    def test_naming_no_real_offering_is_caught(self, bakery_ctx):
        scored = score_grounding(
            sample("Something tasty awaits you!", must_mention=("Chocolate truffle cake",)),
            bakery_ctx,
        )
        assert scored.score < 1.0

    def test_a_festival_post_that_forgets_the_festival_is_caught(self, bakery_ctx):
        scored = score_grounding(
            sample("Modak box ₹300, fresh today.", must_mention_all=("Ganesh Chaturthi",)),
            bakery_ctx,
        )
        assert scored.score < 1.0
        assert "Ganesh Chaturthi" in " ".join(scored.failures)


class TestLanguageScorer:
    MARATHI = "आमच्या दुकानात आज ताजे मोदक मिळतील. नक्की भेट द्या!"
    HINDI = "हमारे यहाँ आज ताजे मोदक मिलेंगे। आप जरूर आइए।"

    def test_devanagari_ratio_reads_the_script(self):
        assert devanagari_ratio("Fresh cake today") == 0.0
        assert devanagari_ratio(self.MARATHI) == 1.0
        assert 0.0 < devanagari_ratio("Modak box — ताजे मोदक") < 1.0

    def test_english_when_english_was_asked_for(self):
        assert score_language(sample("Fresh cake at our shop today.")).score == 1.0

    def test_english_where_marathi_was_asked_for_fails(self):
        scored = score_language(sample("Fresh cake at our shop today.", expect_language="Marathi"))
        assert scored.score == 0.0
        assert "100% Latin" in " ".join(scored.failures)

    def test_marathi_where_marathi_was_asked_for_passes(self):
        assert score_language(sample(self.MARATHI, expect_language="Marathi")).score == 1.0

    def test_hindi_answered_to_a_marathi_speaker_is_caught(self):
        """Right script, wrong language — the failure a script check alone misses."""
        scored = score_language(sample(self.HINDI, expect_language="Marathi"))
        assert scored.score < 1.0
        assert "not marathi" in " ".join(scored.failures).lower()

    def test_hindi_where_hindi_was_asked_for_passes(self):
        assert score_language(sample(self.HINDI, expect_language="Hindi")).score == 1.0

    def test_devanagari_without_decisive_wording_is_flagged_for_a_human(self):
        scored = score_language(sample("ताजे मोदक", expect_language="Marathi"))
        assert scored.score < 1.0
        assert "human read" in " ".join(scored.failures)


class TestGuardrailScorer:
    def test_clean_copy_passes(self, bakery_ctx, bakery_pack):
        scored = score_guardrails(
            sample("Fresh Modak box today at Mane's Bakehouse — message us to order."),
            bakery_ctx,
            bakery_pack,
        )
        assert scored.score == 1.0

    def test_a_pack_banned_term_is_caught(self, bakery_ctx, bakery_pack):
        scored = score_guardrails(sample("Guaranteed the best cake!"), bakery_ctx, bakery_pack)
        assert scored.score < 1.0

    def test_a_health_claim_is_caught(self, bakery_ctx, bakery_pack):
        scored = score_guardrails(
            sample("Our bread is clinically proven to boost immunity."), bakery_ctx, bakery_pack
        )
        assert scored.score < 1.0
        assert "immunity" in " ".join(scored.failures)

    def test_ordinary_bakery_language_is_not_a_claim(self):
        """False positives here silently delete a shop's post, so the patterns must
        leave normal trade language alone."""
        for innocent in [
            "Treat yourself to a fresh loaf this morning.",
            "A healthy start to your day with our multigrain bread.",
            "Sweet treats for the whole family.",
            "Our cakes are made fresh every single day.",
        ]:
            assert find_claims(innocent) == [], innocent

    def test_over_length_copy_is_caught(self, bakery_ctx, bakery_pack):
        scored = score_guardrails(sample("cake " * 200), bakery_ctx, bakery_pack)
        assert scored.score < 1.0

    def test_a_customer_phone_number_is_a_pii_leak(self, bakery_ctx, bakery_pack):
        scored = score_guardrails(
            sample("Thanks Priya! We'll call you on +919900112233 about your order."),
            bakery_ctx,
            bakery_pack,
        )
        assert scored.score < 1.0
        assert "PII" in " ".join(scored.failures)

    def test_the_shops_own_number_is_not_a_leak(self, bakery_ctx, bakery_pack):
        scored = score_guardrails(
            sample("Call us on +912025551234 to place an order."), bakery_ctx, bakery_pack
        )
        assert scored.score == 1.0


class TestBrandVoiceScorer:
    def test_a_neighbourhood_shop_voice_passes(self, bakery_ctx):
        scored = score_brand_voice(
            sample("Fresh Modak box out of the oven — message us on WhatsApp to order."),
            bakery_ctx,
        )
        assert scored.score == 1.0

    def test_advertising_register_is_caught(self, bakery_ctx):
        scored = score_brand_voice(
            sample("ACT NOW for unbeatable cake deals — click here, buy now!"), bakery_ctx
        )
        assert scored.score < 1.0

    def test_shouting_is_caught(self, bakery_ctx):
        scored = score_brand_voice(sample("FRESH CAKE TODAY AT OUR SHOP HURRY"), bakery_ctx)
        assert scored.score < 1.0

    def test_exclamation_spam_is_caught(self, bakery_ctx):
        scored = score_brand_voice(sample("Cake from us today!!!"), bakery_ctx)
        assert scored.score < 1.0

    def test_third_party_copy_is_caught(self, bakery_ctx):
        """Copy that describes the shop instead of speaking as it."""
        scored = score_brand_voice(
            sample("This bakery sells a range of cakes and breads daily."), bakery_ctx
        )
        assert scored.score < 1.0


class TestSuitesOnTheMockProvider:
    """End-to-end: the real agents, the real engine, scored."""

    def runner(self, bar: EvalBar | None = None) -> EvalRunner:
        settings = make_test_settings()
        return EvalRunner(settings=settings, model_map=settings.model_map(), bar=bar or EvalBar())

    def test_the_core_suite_passes(self):
        report = self.runner().run(select(ALL_CASES, suites=["core"]))
        assert report.passed, report.render()
        assert len(report.results) == 5
        for result in report.results:
            assert result.sample_count > 0
            assert result.error is None

    def test_every_core_case_is_scored_on_every_text_dimension(self):
        report = self.runner().run(select(ALL_CASES, suites=["core"]))
        for result in report.results:
            for dimension in (
                Dimension.GROUNDING,
                Dimension.LANGUAGE,
                Dimension.GUARDRAILS,
                Dimension.BRAND_VOICE,
                Dimension.COVERAGE,
            ):
                assert dimension in result.scores, f"{result.case_id} missed {dimension}"

    def test_the_redteam_suite_contains_every_payload(self):
        """Golden rule #1 under pressure: nothing the model does gets past the
        engine into the owner's queue."""
        report = self.runner().run(select(ALL_CASES, suites=["redteam"]))
        assert report.passed, report.render()
        for result in report.results:
            assert result.scores[Dimension.CONTAINMENT].score == 1.0, result.case_id

    def test_the_mock_provider_fails_the_multilingual_suite(self):
        """Recorded as a test, not a comment: the offline mock writes English only,
        so it must never be mistaken for a shippable model for a Marathi shop. If
        this ever passes, either the mock learned Marathi or the scorer went blind."""
        report = self.runner().run(select(ALL_CASES, suites=["multilingual"]))
        assert not report.passed
        assert report.dimension_scores()[Dimension.LANGUAGE] == 0.0
        assert report.dimension_scores()[Dimension.GROUNDING] == 1.0  # only language failed

    def test_a_model_that_writes_nothing_usable_fails_on_coverage(self):
        """A model can fail without producing a single bad caption: if the engine
        rejects everything it writes, the shop's week is simply empty."""
        settings = make_test_settings()
        gateway_providers = {"junk": ScriptedProvider(["..."])}
        runner = EvalRunner(
            settings=settings,
            model_map={**settings.model_map(), "content": "junk"},
            providers=gateway_providers,
        )
        report = runner.run(select(ALL_CASES, suites=["core"], agents=["content"]))
        assert not report.passed
        assert report.dimension_scores()[Dimension.COVERAGE] == 0.0
        assert "no post" in " ".join(report.failures()) or "produced no draft" in " ".join(
            report.failures()
        )

    def test_a_model_that_pads_with_an_invented_item_is_caught_by_the_eval(self):
        """The case for having a harness at all.

        This caption passes every check the engine has: it names the real offering
        at its real price, breaks no banned term, and stays within length — so the
        engine hands it to the owner and, on an AUTO kind, publishes it. Only the
        golden dataset knows the shop has never sold a croissant.
        """
        settings = make_test_settings()
        runner = EvalRunner(
            settings=settings,
            model_map={**settings.model_map(), "content": "padder"},
            providers={"padder": PaddingProvider()},
        )
        report = runner.run(select(ALL_CASES, suites=["core"], agents=["content"]))

        assert report.dimension_scores()[Dimension.COVERAGE] == 1.0  # the engine was happy
        assert report.dimension_scores()[Dimension.GUARDRAILS] == 1.0
        assert not report.passed
        assert report.dimension_scores()[Dimension.GROUNDING] < 1.0
        # the reason, not just the echoed caption — an excerpt of the text would
        # contain "croissant" whether or not the check actually fired
        assert "does not offer" in " ".join(report.failures())

    def test_a_case_that_blows_up_fails_rather_than_disappearing(self):
        class Exploding(type(select(ALL_CASES, suites=["core"])[0])):
            def produce(self, bench):
                raise RuntimeError("provider melted")

        case = select(ALL_CASES, suites=["core"])[0]
        exploding = Exploding(**{name: getattr(case, name) for name in case.__dataclass_fields__})
        report = self.runner().run([exploding])
        assert not report.passed
        assert "provider melted" in report.results[0].error


class TestReportAndBar:
    def make_report(self, score: float) -> EvalReport:
        result = CaseResult(
            case_id="c1",
            suite="core",
            agent="content",
            description="",
            scores={
                Dimension.GROUNDING: DimensionScore(
                    Dimension.GROUNDING,
                    [Check(score == 1.0, "invented an offering")],
                )
            },
        )
        return EvalReport(model_map={"content": "mock"}, results=[result])

    def test_a_failing_dimension_fails_the_report_and_names_the_reason(self):
        report = self.make_report(0.0)
        assert not report.passed
        assert "invented an offering" in " ".join(report.failures())
        assert "FAIL" in report.render()

    def test_a_clean_report_passes(self):
        assert self.make_report(1.0).passed

    def test_the_bar_can_be_relaxed_per_dimension(self):
        report = self.make_report(0.0)
        report.bar = EvalBar().with_override(Dimension.GROUNDING, 0.0)
        assert report.passed

    def test_the_report_survives_a_json_round_trip(self):
        payload = json.loads(json.dumps(self.make_report(0.0).to_dict(), ensure_ascii=False))
        assert payload["passed"] is False
        assert payload["dimension_scores"]["grounding"] == 0.0

    def test_a_drop_against_the_baseline_is_a_regression(self):
        baseline = self.make_report(1.0).to_dict()
        regressions = compare_to_baseline(self.make_report(0.0), baseline)
        assert [r.dimension for r in regressions] == [Dimension.GROUNDING]
        assert regressions[0].delta == -1.0

    def test_an_improvement_is_not_a_regression(self):
        baseline = self.make_report(0.0).to_dict()
        assert compare_to_baseline(self.make_report(1.0), baseline) == []


class TestModelConfiguration:
    def test_a_registered_provider_answers_for_its_model_id(self):
        """The swap point: a model id with no vendor behind it is config, not code."""
        provider = ScriptedProvider(["hello from the fixture"])
        gateway = ModelGateway({"content": "fixture"}, providers={"fixture": provider})
        assert gateway.complete("content", "anything") == "hello from the fixture"
        assert provider.calls == ["anything"]

    def test_an_unregistered_model_still_falls_back_to_the_mock(self):
        gateway = ModelGateway({"content": "some-open-model"})
        assert gateway.complete("content", "business: Test\nhook: fresh") != ""

    def test_model_map_for_points_every_scored_agent_at_one_model(self):
        mapped = model_map_for("claude-sonnet-4-5", make_test_settings())
        assert mapped["content"] == mapped["reputation"] == mapped["engagement"]
        assert mapped["router"] == "mock"  # unscored profiles keep their configured model

    def test_selecting_by_suite_and_agent(self):
        assert {c.suite for c in select(ALL_CASES, suites=["core"])} == {"core"}
        assert {c.agent for c in select(ALL_CASES, agents=["reputation"])} == {"reputation"}
        assert select(ALL_CASES, suites=["nope"]) == []


class TestCli:
    """The exit code is the gate — a red suite has to stop a release on its own."""

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "scripts/run_evals.py", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

    def test_the_core_suite_exits_zero(self):
        result = self.run("--suite", "core", "--quiet")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_failing_suite_exits_non_zero(self):
        result = self.run("--suite", "multilingual", "--quiet")
        assert result.returncode == 1

    def test_a_baseline_run_writes_and_compares(self, tmp_path):
        baseline = tmp_path / "baseline.json"
        assert self.run("--suite", "core", "--quiet", "--json", str(baseline)).returncode == 0
        assert json.loads(baseline.read_text())["passed"] is True
        again = self.run("--suite", "core", "--quiet", "--baseline", str(baseline))
        assert again.returncode == 0
        assert "no dimension regressed" in again.stdout
