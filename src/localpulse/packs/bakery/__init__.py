"""Bakery Vertical Pack (Family 1: product retail & food). The P0 launch vertical."""

from localpulse.context.models import OfferingType
from localpulse.packs.base import (
    CadenceRule,
    ContentTemplate,
    Guardrails,
    OfferingSchema,
    OnboardingQuestion,
    Playbook,
    VerticalPack,
)

PACK = VerticalPack(
    ref="bakery",
    display_name="Bakery / Cake Shop",
    family=1,
    templates=[
        ContentTemplate(
            id="daily_special",
            hook="today's special",
            prompt=(
                "Announce today's special bake. Mention the item by name and price, "
                "say it is freshly made today, and invite orders on WhatsApp."
            ),
            requires_offering=True,
        ),
        ContentTemplate(
            id="festival",
            hook="festival greeting with a festive bake",
            prompt=(
                "Write a warm festival greeting tied to the occasion and feature a "
                "matching item from the menu. Suggest pre-ordering for the festival."
            ),
            occasion="festival",
            requires_offering=True,
        ),
        ContentTemplate(
            id="weekend_treat",
            hook="weekend indulgence",
            prompt=(
                "Tempt readers with a weekend treat. Feature the item by name and "
                "encourage visiting the shop or ordering ahead for the weekend."
            ),
            requires_offering=True,
        ),
        ContentTemplate(
            id="fresh_batch",
            hook="fresh out of the oven this morning",
            prompt=(
                "Paint a sensory picture of the morning's fresh batch — aroma, warmth. "
                "Invite early birds to drop in."
            ),
            requires_offering=True,
        ),
        ContentTemplate(
            id="custom_order_cta",
            hook="custom cake orders",
            prompt=(
                "Remind customers that custom celebration cakes are taken to order. "
                "Ask them to message on WhatsApp with their date and theme."
            ),
        ),
    ],
    onboarding_questions=[
        OnboardingQuestion(
            id="shop_name", question="What is your shop called?", field="business.name"
        ),
        OnboardingQuestion(
            id="address", question="What is the full shop address?", field="business.address"
        ),
        OnboardingQuestion(
            id="city", question="Which city/area are you in?", field="business.city"
        ),
        OnboardingQuestion(
            id="hours",
            question="What are your opening hours (e.g. 8am–9pm, closed Monday)?",
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
            id="specialties",
            question=(
                "List your bestsellers and specialties with prices, comma-separated "
                "(e.g. Chocolate truffle cake ₹550, Modak box ₹300)"
            ),
            field="offerings.products",
        ),
        OnboardingQuestion(
            id="tone",
            question="Pick words that describe your shop's voice (e.g. warm, homely, playful)",
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
            id="festival_specials",
            question="Any festival specials you always make (e.g. modaks for Ganesh Chaturthi)?",
            field="notes.festival_specials",
            required=False,
        ),
    ],
    offering_schema=OfferingSchema(
        allowed_types=[OfferingType.PRODUCT],
        required_fields=["name"],
    ),
    calendar_weights={
        "ganesh chaturthi": 2.0,
        "diwali": 2.0,
        "gudi padwa": 1.8,
        "raksha bandhan": 1.5,
        "holi": 1.4,
        "christmas": 1.6,
        "makar sankranti": 1.3,
        "navratri": 1.2,
    },
    playbook=Playbook(
        posts_per_week=3,
        post_weekdays=[2, 4, 6],  # Tue, Thu, Sat
        cadence=[
            CadenceRule(task="content.generate_week", cron="0 9 * * 1"),
            CadenceRule(task="reputation.check_reviews", cron="15 * * * *"),
            CadenceRule(task="insights.collect", cron="30 21 * * *"),
            CadenceRule(task="insights.monthly_report", cron="0 9 1 * *"),
            CadenceRule(task="approvals.sweep_expired", cron="0 * * * *"),
        ],
        review_reply_style="thank warmly, mention the item they praised, invite them back",
    ),
    guardrails=Guardrails(
        banned_terms=[
            "cure",
            "cures",
            "medicinal",
            "immunity booster",
            "immunity-boosting",
            "weight loss",
            "guaranteed",
        ],
        forbid_health_claims=True,
        max_caption_chars=600,
        require_offering_grounding=True,
    ),
)
