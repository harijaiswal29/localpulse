"""Price grounding — the deterministic half of "the model invented something".

Whether an *item* exists needs NER or a judge model, and the eval harness carries
that as a known limit. Whether a *price* is real is a set-membership test, and a
wrong price is the expensive kind of wrong: the customer arrives at the counter
expecting it, and the owner either eats the difference or has the argument.

The pilot shop charges ₹550 (truffle cake), ₹300 (modak box) and ₹90 (bread).
"""

from __future__ import annotations

import pytest

from localpulse.agents.common import (
    check_text_guardrails,
    find_ungrounded_price,
    grounded_prices,
    prices_in,
)
from localpulse.agents.content import ContentTrigger, Slot, check_guardrails, plan_week
from localpulse.container import Container
from localpulse.context.models import ApprovalState
from localpulse.evals.providers import ScriptedProvider
from localpulse.llm.gateway import ModelGateway
from localpulse.packs.base import load_pack

from .conftest import PILOT_ANSWERS, make_test_settings, opt_in_customer
from .test_content_agent import WEEK_START


class TestPriceParsing:
    def test_recognises_the_forms_a_model_actually_writes(self):
        assert prices_in("Chocolate truffle cake ₹550") == {550.0}
        assert prices_in("just ₹ 550 today") == {550.0}
        assert prices_in("a tier cake at ₹1,100") == {1100.0}
        assert prices_in("half a modak box ₹99.50") == {99.5}
        assert prices_in("Rs. 550 only") == {550.0}
        assert prices_in("rs 90 for the loaf") == {90.0}

    def test_bare_numbers_are_not_prices(self):
        """A false positive silently drops the shop's post, so anything that is not
        a rupee amount must not be read as one."""
        assert prices_in("Open till 9, over 20 varieties daily") == set()
        assert prices_in("Call us on +919812345678") == set()
        assert prices_in("Baked fresh since 1998") == set()
        assert prices_in("20% off this Friday") == set()


class TestFindUngroundedPrice:
    def test_a_price_the_shop_charges_is_fine(self):
        assert find_ungrounded_price("Truffle cake ₹550 today", [550.0, 300.0]) is None

    def test_a_price_it_does_not_charge_is_returned(self):
        found = find_ungrounded_price("Truffle cake ₹450 today", [550.0, 300.0])
        assert found == "₹450"

    def test_formatting_differences_are_not_inventions(self):
        """₹1,100 and 1100.0 are the same number; the check must not fire on commas
        or trailing zeros, or it would reject correct copy."""
        assert find_ungrounded_price("₹1,100 for the tier cake", [1100.0]) is None
        assert find_ungrounded_price("₹550.00 flat", [550.0]) is None
        assert find_ungrounded_price("Rs 90", [90.0]) is None

    def test_copy_without_any_price_passes(self):
        assert find_ungrounded_price("Fresh bread, every morning.", [550.0]) is None

    def test_grounded_prices_reads_the_shop_and_the_authorisation(self, pilot_context):
        assert grounded_prices(pilot_context) == {550.0, 300.0, 90.0}
        assert 50.0 in grounded_prices(pilot_context, extra=[50.0])


class TestCaptionGuardrail:
    @pytest.fixture
    def slot(self, pilot_context):
        pack = load_pack("bakery")
        template = next(t for t in pack.templates if t.requires_offering)
        return Slot(WEEK_START, template, pilot_context.offerings[0], None)

    def test_the_real_price_passes(self, slot, pilot_context):
        pack = load_pack("bakery")
        caption = f"{slot.offering.name} — ₹{slot.offering.price_inr:g} today only."
        assert check_guardrails(caption, slot, pack, pilot_context) is None

    def test_an_invented_price_is_rejected(self, slot, pilot_context):
        pack = load_pack("bakery")
        caption = f"{slot.offering.name} — just ₹399 today!"
        reason = check_guardrails(caption, slot, pack, pilot_context)
        assert reason is not None and "does not charge" in reason

    def test_another_offerings_price_is_still_grounded(self, slot, pilot_context):
        """The check is per shop, not per slot — a caption may legitimately mention
        the ₹90 loaf while the slot is about the ₹550 cake."""
        pack = load_pack("bakery")
        caption = f"{slot.offering.name} ₹550, and our bread is ₹90."
        assert check_guardrails(caption, slot, pack, pilot_context) is None

    def test_a_caption_naming_no_price_passes(self, slot, pilot_context):
        pack = load_pack("bakery")
        caption = f"Fresh {slot.offering.name} out of the oven this morning."
        assert check_guardrails(caption, slot, pack, pilot_context) is None

    def test_a_pack_can_opt_out(self, slot, pilot_context):
        pack = load_pack("bakery").model_copy(deep=True)
        pack.guardrails.require_price_grounding = False
        caption = f"{slot.offering.name} — just ₹399 today!"
        assert check_guardrails(caption, slot, pack, pilot_context) is None

    def test_default_is_on_so_a_new_pack_inherits_it(self):
        """This is an engine concern, not a vertical preference — a pack author
        should not have to know the flag exists to be protected by it."""
        for ref in ("bakery", "salon"):
            assert load_pack(ref).guardrails.require_price_grounding is True


def scripted_container(replies: list[str]) -> tuple[Container, ScriptedProvider]:
    """A container whose generative agents are driven by fixed text, through the
    ordinary gateway path — what is under test is the engine, not a stub."""
    provider = ScriptedProvider(replies)
    gateway = ModelGateway(
        {"content": "fixture", "engagement": "fixture", "reputation": "fixture"},
        providers={"fixture": provider},
    )
    return Container(make_test_settings(), gateway=gateway), provider


class TestInventedPriceNeverReachesTheOwner:
    def test_content_drops_a_slot_whose_caption_invents_a_price(self):
        """End to end: the whole point is that the owner's queue stays clean without
        anyone reading it."""
        container, provider = scripted_container(
            ["Chocolate truffle cake — an unbeatable ₹399 this week!"]
        )
        session = container.session()
        try:
            ctx = container.onboarding_agent(session).run("px-1", "bakery", PILOT_ANSWERS)
            container.ensure_client_tools(ctx)
            services = container.services(session, "px-1")
            drafts = services.content_agent.run(ctx, ContentTrigger(week_start=WEEK_START))
            assert drafts == []
            assert services.queue.list(state=ApprovalState.PENDING_APPROVAL) == []
            # asked twice before giving up on the slot, not silently re-rolled
            assert len(provider.calls) == 2 * len(plan_week(load_pack("bakery"), ctx, WEEK_START))
        finally:
            session.close()


class TestBroadcast:
    def seed(self, services, ctx):
        opt_in_customer(services, ctx, "+919900112233", "Priya")

    def test_a_discount_the_owner_asked_for_is_authorised(self, container, session, pilot_context):
        """₹50 off is not on the menu, and must still be allowed — the owner named
        it. Rejecting this would make the check useless for real marketing."""
        services = container.services(session, "pilot-1")
        self.seed(services, pilot_context)
        draft = services.engagement_agent.draft_weekly_broadcast(
            pilot_context, offer_text="₹50 off every cake"
        )
        assert draft is not None
        assert "₹50" in draft.caption

    def test_a_price_the_model_added_on_top_is_rejected(self):
        """The owner authorised ₹50 off; the model also quoted ₹250 for a cake that
        costs ₹550. That is the send that has to not happen."""
        container, _ = scripted_container(["₹50 off every cake — now just ₹250!"])
        session = container.session()
        try:
            ctx = container.onboarding_agent(session).run("px-2", "bakery", PILOT_ANSWERS)
            container.ensure_client_tools(ctx)
            services = container.services(session, "px-2")
            self.seed(services, ctx)
            draft = services.engagement_agent.draft_weekly_broadcast(
                ctx, offer_text="₹50 off every cake"
            )
            assert draft is None
        finally:
            session.close()


class TestReviewReply:
    def test_a_reply_inventing_a_price_is_rejected(self, pilot_context):
        pack = load_pack("bakery")
        reason = check_text_guardrails(
            "So sorry! Here's ₹200 off your next order.", pack, pilot_context
        )
        assert reason is not None and "does not charge" in reason
