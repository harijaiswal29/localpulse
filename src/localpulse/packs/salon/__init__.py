"""Salon Vertical Pack (Family 2: appointment services). The second pack —
proves the pack contract holds beyond product retail: offerings are services
booked by appointment, and every booking cue routes through the same generic
engagement machinery the bakery uses."""

from localpulse.context.models import OfferingType
from localpulse.packs.base import (
    CadenceRule,
    ContentTemplate,
    EngagementPlaybook,
    FaqEntry,
    Guardrails,
    OfferingSchema,
    OnboardingQuestion,
    Playbook,
    VerticalPack,
)

PACK = VerticalPack(
    ref="salon",
    display_name="Salon / Beauty Studio",
    family=2,
    templates=[
        ContentTemplate(
            id="service_spotlight",
            hook="service of the week",
            prompt=(
                "Feature the service by name and price, describe the experience in a "
                "sentence, and invite bookings on WhatsApp."
            ),
            requires_offering=True,
        ),
        ContentTemplate(
            id="festival",
            hook="get festival-ready",
            prompt=(
                "Write a warm festival greeting tied to the occasion and invite readers "
                "to book the featured service to get festival-ready. Mention that slots "
                "fill fast before festivals."
            ),
            occasion="festival",
            requires_offering=True,
        ),
        ContentTemplate(
            id="weekend_slots",
            hook="weekend slots filling fast",
            prompt=(
                "Remind readers that weekend appointments go quickly. Feature the "
                "service by name and nudge them to book their slot on WhatsApp today."
            ),
            requires_offering=True,
        ),
        ContentTemplate(
            id="midweek_selfcare",
            hook="midweek self-care break",
            prompt=(
                "Invite readers to treat themselves to a quieter midweek visit. Feature "
                "the service by name and mention midweek is the calmest time to come in."
            ),
            requires_offering=True,
        ),
        ContentTemplate(
            id="bridal_consult_cta",
            hook="bridal & occasion styling",
            prompt=(
                "Remind customers that bridal and special-occasion styling is planned "
                "over a consultation. Ask them to message on WhatsApp with their date."
            ),
        ),
    ],
    onboarding_questions=[
        OnboardingQuestion(
            id="salon_name", question="What is your salon called?", field="business.name"
        ),
        OnboardingQuestion(
            id="address", question="What is the full salon address?", field="business.address"
        ),
        OnboardingQuestion(
            id="city", question="Which city/area are you in?", field="business.city"
        ),
        OnboardingQuestion(
            id="hours",
            question="What are your working hours (e.g. 10am–8pm, closed Tuesday)?",
            field="business.hours",
        ),
        OnboardingQuestion(
            id="owner_whatsapp",
            question="Which WhatsApp number should approvals go to?",
            field="business.owner_whatsapp",
        ),
        OnboardingQuestion(
            id="phone",
            question="What phone number should customers call?",
            field="business.phone",
            required=False,
        ),
        OnboardingQuestion(
            id="services",
            question=(
                "List your main services with prices, comma-separated "
                "(e.g. Haircut ₹250, Gold facial ₹800, Bridal makeup package ₹5000)"
            ),
            field="offerings.services",
        ),
        OnboardingQuestion(
            id="tone",
            question="Pick words that describe your salon's voice (e.g. polished, friendly)",
            field="brand_voice.tone",
        ),
        OnboardingQuestion(
            id="languages",
            question="Which languages should posts use (English, Marathi, Hindi)?",
            field="brand_voice.languages",
        ),
        OnboardingQuestion(
            id="example_posts",
            question="Paste 1–3 of your past posts you liked (optional)",
            field="brand_voice.example_posts",
            required=False,
        ),
        OnboardingQuestion(
            id="festival_offers",
            question=(
                "Any festival or wedding-season packages you always run "
                "(e.g. bridal packages, Diwali glow facials)?"
            ),
            field="notes.festival_specials",
            required=False,
        ),
    ],
    offering_schema=OfferingSchema(
        allowed_types=[OfferingType.SERVICE],
        required_fields=["name"],
        requires_appointment=True,
    ),
    calendar_weights={
        "diwali": 2.0,  # party & wedding season opener — peak grooming demand
        "navratri": 1.8,  # garba nights
        "gudi padwa": 1.6,
        "christmas": 1.6,  # year-end party season
        "ganesh chaturthi": 1.4,
        "raksha bandhan": 1.4,
        "holi": 1.2,
        "makar sankranti": 1.1,
    },
    playbook=Playbook(
        posts_per_week=3,
        post_weekdays=[3, 5, 7],  # Wed, Fri, Sun — build toward the weekend rush
        cadence=[
            CadenceRule(task="content.generate_week", cron="0 9 * * 1"),
            CadenceRule(task="reputation.check_reviews", cron="15 * * * *"),
            CadenceRule(task="insights.collect", cron="30 21 * * *"),
            CadenceRule(task="insights.monthly_report", cron="0 9 1 * *"),
            CadenceRule(task="approvals.sweep_expired", cron="0 * * * *"),
            # Thursday evening: draft the weekend-slots broadcast for owner approval
            CadenceRule(task="engagement.weekly_broadcast", cron="0 18 * * 4"),
        ],
        review_reply_style=(
            "thank them personally, mention the service or stylist they praised, "
            "invite them to book their next visit"
        ),
        engagement=EngagementPlaybook(
            faqs=[
                FaqEntry(
                    id="hours",
                    patterns=["open", "close", "closing", "timing", "hours", "kiti vajta"],
                    answer="We're open {hours}. See you soon at {business_name}! ✨",
                ),
                FaqEntry(
                    id="location",
                    patterns=["where", "address", "location", "directions", "reach you"],
                    answer=(
                        "You'll find us at {address}, {city} — just search "
                        "{business_name} on Google Maps for directions."
                    ),
                ),
                FaqEntry(
                    id="services",
                    patterns=["price", "prices", "cost", "rate", "services", "how much"],
                    answer=(
                        "Our services at {business_name}: {menu}. "
                        "Message us here to book your slot! 💇"
                    ),
                ),
                FaqEntry(
                    id="walkin",
                    patterns=["walk-in", "walk in", "walkin", "without appointment"],
                    answer=(
                        "Walk-ins are welcome when a chair is free, but booking ahead "
                        "right here on WhatsApp guarantees your slot. We're open {hours}."
                    ),
                ),
            ],
            preorder_cues=["book", "booking", "appointment", "slot", "schedule"],
            # too generic to identify one service — a booking naming only these escalates
            vague_terms=["hair", "package", "treatment", "combo", "style", "styling"],
            preorder_ack=(
                "Great choice, {customer_name}! {offering_name} is {price}. "
                "I've asked the team to confirm your slot — you'll get your "
                "booking time right here shortly. 💇‍♀️"
            ),
            escalation_ack=(
                "Thanks for reaching out! Let me check with the team — "
                "you'll hear back right here shortly."
            ),
            opt_out_ack=(
                "Done — you won't get offers from us anymore. You're always "
                "welcome to message us here anytime."
            ),
            broadcast_prompt=(
                "Write this week's short WhatsApp offer featuring the service. Polished "
                "and friendly, no pushy sales language, invite a reply to book a slot."
            ),
        ),
    ),
    guardrails=Guardrails(
        banned_terms=[
            # beauty-claim territory that invites ASCI/consumer complaints
            "cure",
            "cures",
            "regrow",
            "regrows",
            "whitening",
            "fairness",
            "permanent results",
            "100% safe",
            "no side effects",
            "medically proven",
            "guaranteed",
        ],
        forbid_health_claims=True,
        max_caption_chars=600,
        require_offering_grounding=True,
    ),
)
