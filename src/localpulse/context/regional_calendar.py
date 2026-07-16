"""Regional festival calendar (Maharashtra-weighted, spec §5.2).

Regional — not vertical — data, so it lives in the engine; packs weight it via
`calendar_weights`. Dates are approximate for lunar festivals; confirm each year.
"""

from datetime import date

from localpulse.context.models import CalendarEvent

MAHARASHTRA_2026: list[CalendarEvent] = [
    CalendarEvent(name="Makar Sankranti", date=date(2026, 1, 14), hooks=["tilgul", "sweets"]),
    CalendarEvent(name="Holi", date=date(2026, 3, 4), hooks=["colours", "gujiya"]),
    CalendarEvent(name="Gudi Padwa", date=date(2026, 3, 19), hooks=["new year", "shrikhand"]),
    CalendarEvent(name="Raksha Bandhan", date=date(2026, 8, 28), hooks=["siblings", "gifting"]),
    CalendarEvent(name="Ganesh Chaturthi", date=date(2026, 9, 14), hooks=["modak", "Bappa"]),
    CalendarEvent(name="Navratri", date=date(2026, 10, 11), hooks=["nine nights", "fasting"]),
    CalendarEvent(name="Diwali", date=date(2026, 11, 8), hooks=["faral", "gifting", "lights"]),
    CalendarEvent(name="Christmas", date=date(2026, 12, 25), hooks=["cake", "gifting"]),
]


def regional_calendar(_region: str = "maharashtra") -> list[CalendarEvent]:
    return [event.model_copy(deep=True) for event in MAHARASHTRA_2026]
