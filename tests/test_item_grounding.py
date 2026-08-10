"""Item grounding — the other half of "the model invented something".

A price is a number and settles as a set-membership test (see test_price_grounding).
An item is a noun phrase: detecting an arbitrary invented one needs NER or a judge
model, so instead each pack declares the vocabulary of its vertical and the engine
rejects any of those nouns the client's own offerings don't cover.

The pilot bakery sells: Chocolate truffle cake, Modak box, Multigrain bread.
So `cake`, `modak` and `bread` are covered; `croissant` and `cookie` are not.
"""

from __future__ import annotations

import pytest

from localpulse.agents.common import find_unstocked_item
from localpulse.agents.content import ContentTrigger, Slot, check_guardrails, plan_week
from localpulse.context.models import ApprovalState
from localpulse.packs.base import load_pack

from .conftest import PILOT_ANSWERS, opt_in_customer
from .test_content_agent import WEEK_START
from .test_price_grounding import scripted_container


@pytest.fixture
def bakery():
    return load_pack("bakery")


class TestFindUnstockedItem:
    def test_a_noun_the_shop_covers_passes(self, bakery, pilot_context):
        assert find_unstocked_item("Fresh bread every morning.", bakery, pilot_context) is None
        assert find_unstocked_item("Our chocolate cake is back.", bakery, pilot_context) is None

    def test_a_noun_it_does_not_sell_is_returned(self, bakery, pilot_context):
        found = find_unstocked_item("Cake and warm croissants today.", bakery, pilot_context)
        assert found == "croissants"

    def test_plurals_are_matched(self, bakery, pilot_context):
        assert find_unstocked_item("Try our cookies!", bakery, pilot_context) == "cookies"
        assert find_unstocked_item("One cookie, freshly baked.", bakery, pilot_context) == "cookie"

    def test_word_boundaries_hold(self, bakery, pilot_context):
        """`cake` is covered here, but the check must not fire on a *different* word
        that merely contains a lexicon term — that is why the pack lists cheesecake
        and cupcake separately."""
        assert find_unstocked_item("Our pancakes are not a cake.", bakery, pilot_context) is None
        assert find_unstocked_item("New York cheesecake now in.", bakery, pilot_context) == (
            "cheesecake"
        )

    def test_the_business_name_is_not_scanned(self, bakery, pilot_context):
        """A shop may be named after something it doesn't sell. Rename the pilot to
        prove the caption's own shop name can never trip its lexicon."""
        ctx = pilot_context.model_copy(deep=True)
        ctx.business.name = "The Croissant House"
        assert find_unstocked_item(f"Fresh bread at {ctx.business.name}.", bakery, ctx) is None
        # the same word outside the name still counts
        assert (
            find_unstocked_item(f"{ctx.business.name} — warm croissants today.", bakery, ctx)
            == "croissants"
        )

    def test_an_empty_lexicon_disables_the_check(self, pilot_context):
        """A pack that declares no vocabulary gets no item protection — stated
        plainly rather than pretending otherwise."""
        pack = load_pack("bakery").model_copy(deep=True)
        pack.guardrails.item_lexicon = []
        assert find_unstocked_item("Warm croissants today.", pack, pilot_context) is None

    def test_a_shop_that_does_sell_it_is_unaffected(self, bakery, pilot_context):
        """Same pack, different client. The lexicon is the vertical's vocabulary;
        what is *stocked* comes from the Client Context."""
        ctx = pilot_context.model_copy(deep=True)
        ctx.offerings[0].name = "Butter croissant"
        assert find_unstocked_item("Warm croissants today.", bakery, ctx) is None


class TestCaptionGuardrail:
    @pytest.fixture
    def slot(self, pilot_context):
        pack = load_pack("bakery")
        template = next(t for t in pack.templates if t.requires_offering)
        return Slot(WEEK_START, template, pilot_context.offerings[0], None)

    def test_a_padded_caption_is_rejected(self, slot, bakery, pilot_context):
        """The exact caption that used to pass everything: right item, right price,
        no banned term, within length — plus a croissant this shop never baked."""
        caption = f"{slot.offering.name} ₹550 and fresh butter croissants."
        reason = check_guardrails(caption, slot, bakery, pilot_context)
        assert reason is not None and "does not sell" in reason

    def test_an_honest_caption_still_passes(self, slot, bakery, pilot_context):
        caption = f"{slot.offering.name} ₹550, baked fresh this morning."
        assert check_guardrails(caption, slot, bakery, pilot_context) is None

    def test_both_packs_declare_a_lexicon(self):
        for ref in ("bakery", "salon"):
            assert load_pack(ref).guardrails.item_lexicon, ref


class TestReviewRepliesAreExempt:
    def test_a_reply_may_echo_an_item_the_shop_does_not_stock(self, pilot_context):
        """A reviewer asking for croissants should get an answer, not silence. The
        item check is for copy the shop authors; a reply quoting a customer is not
        that. Price grounding still applies to replies.
        """
        from localpulse.agents.reputation import check_reply_guardrails

        pack = load_pack("bakery")
        reply = "Thanks! We don't bake croissants yet, but we'll pass it on. 🙏"
        assert check_reply_guardrails(reply, pack, pilot_context) is None


class TestEndToEnd:
    def test_content_drops_a_slot_that_invents_an_item(self):
        container, provider = scripted_container(
            ["Chocolate truffle cake ₹550 and fresh butter croissants at Mane's Bakehouse."]
        )
        session = container.session()
        try:
            ctx = container.onboarding_agent(session).run("ix-1", "bakery", PILOT_ANSWERS)
            container.ensure_client_tools(ctx)
            services = container.services(session, "ix-1")
            drafts = services.content_agent.run(ctx, ContentTrigger(week_start=WEEK_START))

            assert drafts == []
            assert services.queue.list(state=ApprovalState.PENDING_APPROVAL) == []
            # retried once per slot before giving up, not silently re-rolled
            assert len(provider.calls) == 2 * len(plan_week(load_pack("bakery"), ctx, WEEK_START))
        finally:
            session.close()

    def test_broadcast_offer_line_is_checked_too(self):
        """The broadcast has no manual paste step and costs money per recipient, so
        it needs the check at least as much as a GBP post does."""
        container, _ = scripted_container(["free croissants with every cake"])
        session = container.session()
        try:
            ctx = container.onboarding_agent(session).run("ix-2", "bakery", PILOT_ANSWERS)
            container.ensure_client_tools(ctx)
            services = container.services(session, "ix-2")
            opt_in_customer(services, ctx, "+919900112233", "Priya")
            assert services.engagement_agent.draft_weekly_broadcast(ctx) is None
        finally:
            session.close()


class TestOwnerSeesWhatThePostIsAbout:
    def test_the_draft_records_its_offering(self, container, session, pilot_context):
        services = container.services(session, "pilot-1")
        drafts = services.content_agent.run(pilot_context, ContentTrigger(week_start=WEEK_START))
        grounded = [d for d in drafts if d.meta.get("offering")]
        assert grounded, "a slot built around an offering must record which one"
        assert grounded[0].about

    def test_the_preview_names_it_above_the_caption(self, container, session, pilot_context):
        """The lexicon only knows the nouns someone listed, so the owner's read stays
        the backstop — and an owner cannot spot copy that drifted off the brief
        without being told what the brief was."""
        from tests.conftest import open_owner_window

        services = container.services(session, "pilot-1")
        open_owner_window(services)
        services.content_agent.run(pilot_context, ContentTrigger(week_start=WEEK_START))

        body = container.registry.get("pilot-1", "whatsapp").sent[-1].body
        assert pilot_context.offerings[0].name in body
