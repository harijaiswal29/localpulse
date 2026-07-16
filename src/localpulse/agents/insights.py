"""Insights Agent (A0) — collects metrics daily and assembles the monthly
one-page report in plain language. The report is the retention engine (spec §5.5)."""

from __future__ import annotations

import calendar as _calendar
from datetime import UTC, datetime

from localpulse.context.models import ClientContext, DraftKind
from localpulse.context.repositories import (
    EnquiryRepository,
    MetricsRepository,
    PublishLogRepository,
    ReviewRepository,
)
from localpulse.orchestrator.tool_registry import ToolRegistry


class InsightsAgent:
    task_profile = "insights"

    def __init__(
        self,
        metrics: MetricsRepository,
        publish_log: PublishLogRepository,
        registry: ToolRegistry,
        reviews: ReviewRepository | None = None,
        enquiries: EnquiryRepository | None = None,
    ):
        self._metrics = metrics
        self._publish_log = publish_log
        self._registry = registry
        self._reviews = reviews
        self._enquiries = enquiries

    def collect_daily(self, ctx: ClientContext) -> dict[str, float]:
        """Silently pull platform metrics and persist them (daily cadence)."""
        gbp = self._registry.get(ctx.client_id, "gbp")
        insights = gbp.fetch_insights()
        for metric, value in insights.items():
            self._metrics.record(metric, value)
        return insights

    def monthly_report(self, ctx: ClientContext, year: int, month: int) -> str:
        """Plain-language one-pager — outcomes, not analytics (spec §10)."""
        start = datetime(year, month, 1, tzinfo=UTC)
        last_day = _calendar.monthrange(year, month)[1]
        end = datetime(year, month, last_day, 23, 59, 59, tzinfo=UTC)

        views = [m.value for m in self._metrics.series("profile_views", start, end)]
        ratings = [m.value for m in self._metrics.series("avg_rating", start, end)]
        review_counts = [m.value for m in self._metrics.series("review_count", start, end)]
        posts_published = self._publish_log.count_between(start, end, kind=DraftKind.GBP_POST.value)

        month_name = start.strftime("%B %Y")
        lines = [f"📈 {ctx.business.name} — your month in review ({month_name})", ""]

        if views:
            lines.append(f"• {int(sum(views))} people viewed your Google profile this month.")
        if review_counts:
            gained = int(review_counts[-1] - review_counts[0])
            if gained > 0:
                lines.append(f"• You gained {gained} new review(s).")
            lines.append(f"• You now have {int(review_counts[-1])} reviews in total.")
        if ratings:
            lines.append(f"• Your average rating is {ratings[-1]:.1f} stars.")
        if self._reviews is not None:
            new_reviews = self._reviews.count_between(start, end)
            replied = self._reviews.replied_count_between(start, end)
            if new_reviews:
                lines.append(
                    f"• {new_reviews} new review(s) came in and {replied} got a public reply."
                )
        if self._enquiries is not None:
            handled = self._enquiries.count_between(start, end)
            instant = self._enquiries.auto_answered_between(start, end)
            if handled:
                lines.append(
                    f"• {handled} customer enquirie(s) came in on WhatsApp — "
                    f"{instant} answered instantly, {handled - instant} passed to you."
                )
        lines.append(f"• {posts_published} post(s) went live on your profile.")

        if not views and not ratings and posts_published == 0:
            lines.append("• We're still collecting your first month of data — more next month!")

        lines.append("")
        lines.append("Keep approving those drafts — consistency is what moves the needle. 🙌")
        return "\n".join(lines)
