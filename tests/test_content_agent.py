"""Content Agent: a week of grounded, guardrail-compliant drafts lands in the
queue as pending_approval. Doubles as the basic P0 content eval (spec §12.2)."""

from datetime import date

import pytest

from localpulse.agents.content import ContentTrigger, Slot, check_guardrails, plan_week
from localpulse.container import Container
from localpulse.context.models import ApprovalState, DraftKind
from localpulse.packs.base import load_pack
from tests.conftest import open_owner_window

WEEK_START = date(2026, 7, 20)  # a plain week (no festivals)
GANESH_WEEK = date(2026, 9, 14)  # Ganesh Chaturthi falls on this Monday


@pytest.fixture
def services(container: Container, session, pilot_context):
    return container.services(session, "pilot-1")


def test_week_of_drafts_enters_queue_pending(services, pilot_context):
    drafts = services.content_agent.run(pilot_context, ContentTrigger(week_start=WEEK_START))
    pack = load_pack("bakery")
    assert len(drafts) == pack.playbook.posts_per_week
    for draft in drafts:
        assert draft.state == ApprovalState.PENDING_APPROVAL
        assert draft.kind == DraftKind.GBP_POST
        assert draft.caption.strip()
        assert draft.image_ref is not None  # caption + image per P0 DoD
        assert WEEK_START <= draft.scheduled_for < date(2026, 7, 27)


def test_captions_are_grounded_in_real_offerings(services, pilot_context):
    drafts = services.content_agent.run(pilot_context, ContentTrigger(week_start=WEEK_START))
    offering_names = {o.name.lower() for o in pilot_context.offerings}
    grounded = [d for d in drafts if d.meta["template_id"] not in {"custom_order_cta"}]
    for draft in grounded:
        assert any(name in draft.caption.lower() for name in offering_names), draft.caption


def test_festival_week_produces_festival_post(services, pilot_context):
    drafts = services.content_agent.run(pilot_context, ContentTrigger(week_start=GANESH_WEEK))
    festival_drafts = [d for d in drafts if d.meta.get("event") == "Ganesh Chaturthi"]
    assert festival_drafts, "Ganesh Chaturthi week must produce a festival post"
    assert all(d.time_sensitive for d in festival_drafts)
    assert all(d.expires_at is not None for d in festival_drafts)


def test_owner_receives_approval_preview_on_whatsapp(services, pilot_context, container):
    open_owner_window(services)
    services.content_agent.run(pilot_context, ContentTrigger(week_start=WEEK_START))
    whatsapp = container.registry.get("pilot-1", "whatsapp")
    assert whatsapp.sent, "owner must get a preview message"
    preview = whatsapp.sent[-1]
    assert preview.to == pilot_context.business.owner_whatsapp
    assert preview.category == "service_reply"  # free — never marketing for approvals
    assert "APPROVE" in preview.body


def test_guardrails_reject_banned_terms_and_ungrounded_captions(pilot_context):
    pack = load_pack("bakery")
    template = next(t for t in pack.templates if t.requires_offering)
    offering = pilot_context.offerings[0]
    slot = Slot(WEEK_START, template, offering, None)

    assert check_guardrails("", slot, pack) == "empty caption"
    assert "banned term" in check_guardrails(f"{offering.name} cures all ills!", slot, pack)
    assert check_guardrails("Try our amazing mystery cake!", slot, pack) is not None
    assert check_guardrails(f"Fresh {offering.name} today!", slot, pack) is None
    assert check_guardrails("x" * 601 + f" {offering.name}", slot, pack) == "caption too long"


def test_guardrails_reject_health_claims_the_pack_forbids(pilot_context):
    """`forbid_health_claims` is a pack flag, so the engine has to honour it — a
    claim phrase gets past a banned-term list every time."""
    pack = load_pack("bakery")
    template = next(t for t in pack.templates if t.requires_offering)
    offering = pilot_context.offerings[0]
    slot = Slot(WEEK_START, template, offering, None)

    for claim in [
        f"{offering.name} — clinically proven to boost immunity!",
        f"{offering.name}: no side effects, 100% safe.",
        f"Our {offering.name} heals a bad day.",
    ]:
        reason = check_guardrails(claim, slot, pack)
        assert reason is not None and reason.startswith("health claim"), claim

    # "weight loss" is also on the bakery pack's banned-term list — either gate
    # rejecting it is fine, what matters is that neither lets it through.
    assert check_guardrails(f"Our {offering.name} helps with weight loss.", slot, pack)


def test_ordinary_bakery_wording_is_not_mistaken_for_a_health_claim(pilot_context):
    """A false positive here silently drops the shop's post, so normal trade
    language must survive."""
    pack = load_pack("bakery")
    template = next(t for t in pack.templates if t.requires_offering)
    offering = pilot_context.offerings[0]
    slot = Slot(WEEK_START, template, offering, None)

    for innocent in [
        f"Treat yourself to a fresh {offering.name} this morning.",
        f"A healthy start to the day with our {offering.name}.",
        f"{offering.name} — baked fresh, every single day.",
    ]:
        assert check_guardrails(innocent, slot, pack) is None, innocent


def test_plan_week_fails_closed_without_offerings(pilot_context):
    pack = load_pack("bakery")
    bare = pilot_context.model_copy(deep=True)
    bare.offerings = []
    slots = plan_week(pack, bare, WEEK_START)
    for slot in slots:
        assert not slot.template.requires_offering  # never a slot it can't ground
