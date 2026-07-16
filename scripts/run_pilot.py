"""Seed and drive one pilot bakery locally, end to end, from the terminal.

    python scripts/run_pilot.py

Onboards a pilot shop, generates a week of drafts, prints the WhatsApp preview
the owner would receive, approves the first draft, and prints the monthly report.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from localpulse.agents.content import ContentTrigger
from localpulse.config import Settings
from localpulse.container import Container
from localpulse.orchestrator.publisher import publish_draft
from localpulse.tools.gbp import Review

ANSWERS = {
    "shop_name": "Mane's Bakehouse",
    "address": "12 FC Road, Shivajinagar",
    "city": "Pune",
    "hours": "8am-9pm, closed Monday",
    "owner_whatsapp": "+919812345678",
    "specialties": "Chocolate truffle cake ₹550, Modak box ₹300, Multigrain bread ₹90",
    "tone": "warm, homely",
    "languages": "English, Marathi",
    "festival_specials": "modaks for Ganesh Chaturthi, faral boxes for Diwali",
}


def main() -> None:
    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")
    container = Container(Settings(database_url="sqlite:///./localpulse.db"))

    with container.session() as session:
        print("\n=== 1. Onboarding ===")
        ctx = container.onboarding_agent(session).run("pilot-1", "bakery", ANSWERS)
        container.ensure_client_tools(ctx)
        print(f"Client Context created for {ctx.business.name} ({len(ctx.offerings)} offerings)")

        print("\n=== 2. Content Agent: a week of drafts ===")
        services = container.services(session, "pilot-1")
        today = datetime.now(UTC).date()
        week_start = today + timedelta(days=(8 - today.isoweekday()) % 7 or 7)
        drafts = services.content_agent.run(ctx, ContentTrigger(week_start=week_start))
        for draft in drafts:
            print(f"  [{draft.short_id}] {draft.scheduled_for} ({draft.state}): {draft.caption}")

        print("\n=== 3. Owner's WhatsApp preview ===")
        whatsapp = container.registry.get("pilot-1", "whatsapp")
        print(whatsapp.sent[-1].body)

        if drafts:
            print("\n=== 4. Owner approves the first draft ===")
            draft, approval_log_id = services.state_machine.approve(drafts[0].id, actor="owner")
            action = publish_draft(
                draft_id=draft.id,
                approval_log_id=approval_log_id,
                queue=services.queue,
                publish_log=services.publish_log,
                state_machine=services.state_machine,
                registry=container.registry,
            )
            print(f"Published as {action.external_ref} (approval #{action.approval_log_id})")
            gbp = container.registry.get("pilot-1", "gbp")
            print(f"Semi-manual GBP queue now holds {len(gbp.queue)} post(s) to push live.")

        print("\n=== 5. Reputation: two new reviews arrive ===")
        gbp = container.registry.get("pilot-1", "gbp")
        gbp.reviews.extend(
            [
                Review(
                    review_id="rev-001",
                    rating=5,
                    text="The modak box was outstanding — fresh and perfectly sweet!",
                    language="en",
                    author="Sneha K.",
                ),
                Review(
                    review_id="rev-002",
                    rating=2,
                    text="Waited 40 minutes and the cake was stale. Disappointed.",
                    language="en",
                    author="Rahul P.",
                ),
            ]
        )
        reply_drafts = services.reputation_agent.check_reviews(ctx)
        print(whatsapp.sent[-1].body)

        if reply_drafts:
            print("\n=== 6. Owner approves the positive reply; the negative one waits ===")
            positive = next(d for d in reply_drafts if not d.meta["escalated"])
            draft, approval_log_id = services.state_machine.approve(positive.id, actor="owner")
            action = publish_draft(
                draft_id=draft.id,
                approval_log_id=approval_log_id,
                queue=services.queue,
                publish_log=services.publish_log,
                state_machine=services.state_machine,
                registry=container.registry,
                cost_guard=services.cost_guard,
                reviews=services.reviews,
            )
            print(f"Reply published as {action.external_ref}")
            print(f"Semi-manual GBP reply queue now holds {len(gbp.reply_queue)} item(s).")

        print("\n=== 7. Engagement: customers message the shop on WhatsApp ===")
        agent = services.engagement_agent
        for number, name, text in [
            ("+919900112233", "Priya", "What time are you open till on Sunday?"),
            ("+919900445566", "Arjun", "I want to order a modak box for Saturday"),
            ("+919900778899", "", "Do you make sugar-free vegan black forest pastries?"),
        ]:
            result = agent.handle_inbound(ctx, number, text, name)
            print(f'  {name or number}: "{text}"')
            print(f"    -> [{result.action}] {result.reply}")

        print("\n=== 8. Engagement: weekly offer broadcast (A1, marketing) ===")
        broadcast = agent.draft_weekly_broadcast(ctx)
        if broadcast is not None:
            print(
                f"  Draft [{broadcast.short_id}] -> {len(broadcast.meta['recipients'])} "
                f"opted-in customer(s):\n  {broadcast.caption}"
            )
            draft, approval_log_id = services.state_machine.approve(broadcast.id, actor="owner")
            action = publish_draft(
                draft_id=draft.id,
                approval_log_id=approval_log_id,
                queue=services.queue,
                publish_log=services.publish_log,
                state_machine=services.state_machine,
                registry=container.registry,
                cost_guard=services.cost_guard,
            )
            print(
                f"  Published as {action.external_ref}; "
                f"month's spend so far: ₹{services.cost_guard.spend_this_month():.2f}"
            )

        print("\n=== 9. Insights ===")
        services.insights_agent.collect_daily(ctx)
        now = datetime.now(UTC)
        print(services.insights_agent.monthly_report(ctx, now.year, now.month))


if __name__ == "__main__":
    main()
