"""WhatsApp message templates — the only thing the platform delivers outside the
24h service window, and therefore the shape of every paid send (spec §7, §11).

The split follows golden rule #2: a pack supplies the wording for a slot, the
engine owns which slots exist, what each one costs, and how it reaches the wire.
Rendering happens once, at draft time, so what the owner approves is exactly the
text (and exactly the parameters) that go out at publish time.
"""

from __future__ import annotations

from localpulse.context.models import MessageTemplate, TemplateSlot
from localpulse.orchestrator.cost_guard import MessageCategory
from localpulse.tools.whatsapp import OutboundTemplate

# What WhatsApp charges for each slot. Meta prices by the category the template was
# *approved* under, so this mapping is also what the submitted template must declare
# — mis-declaring a marketing template as utility is the classic overspend (§11).
SLOT_CATEGORY: dict[TemplateSlot, MessageCategory] = {
    TemplateSlot.REVIEW_NUDGE: MessageCategory.UTILITY,
    TemplateSlot.WEEKLY_OFFER: MessageCategory.MARKETING,
    TemplateSlot.OWNER_ALERT: MessageCategory.UTILITY,
}

# Which parameter an owner edit rewrites. A nudge and an owner alert are fixed
# wording end to end; a broadcast's editable content is the offer line.
SLOT_CONTENT_PARAM: dict[TemplateSlot, str | None] = {
    TemplateSlot.REVIEW_NUDGE: None,
    TemplateSlot.WEEKLY_OFFER: "offer",
    TemplateSlot.OWNER_ALERT: None,
}


class TemplateRequiredError(Exception):
    """A paid send was attempted without a template — WhatsApp would reject it."""

    def __init__(self, category: MessageCategory, purpose: str):
        super().__init__(
            f"a {category.value} message ({purpose}) needs an approved template: "
            "free-form text is only delivered inside the 24h service window"
        )


class TemplateRenderError(Exception):
    """The pack template could not be filled — never send a half-filled message."""


def render(template: MessageTemplate, mapping: dict[str, str]) -> OutboundTemplate:
    """Fill a pack template into something sendable. Parameters are squeezed onto one
    line because Meta rejects newlines, tabs and runs of spaces inside a parameter."""
    values: dict[str, str] = {}
    for name in template.params:
        raw = mapping.get(name)
        if raw is None:
            raise TemplateRenderError(f"template {template.name!r} needs {{{name}}}")
        value = " ".join(str(raw).split())
        if not value:
            raise TemplateRenderError(f"template {template.name!r} got an empty {{{name}}}")
        values[name] = value
    return OutboundTemplate(
        name=template.name,
        language=template.language,
        params=[values[name] for name in template.params],
        body=template.body.format_map(values),
    )


def rerender(
    template: MessageTemplate, approved: OutboundTemplate, param: str, value: str
) -> OutboundTemplate:
    """Apply an owner edit to a templated draft. The fixed wording is what Meta
    approved and cannot change, so an edit rewrites the one parameter that carries
    the content — everything else is reused from the draft as approved."""
    mapping = dict(zip(template.params, approved.params, strict=True))
    mapping[param] = value
    return render(template, mapping)


def meta_payload(template: MessageTemplate, examples: dict[str, str]) -> dict:
    """The submission payload for POST /{waba_id}/message_templates — the named
    placeholders compile down to Meta's positional {{1}}, {{2}} form."""
    body = template.body
    sample: list[str] = []
    for index, name in enumerate(template.params, start=1):
        body = body.replace(f"{{{name}}}", f"{{{{{index}}}}}")
        sample.append(examples.get(name, name.replace("_", " ")))
    component: dict = {"type": "BODY", "text": body}
    if sample:
        component["example"] = {"body_text": [sample]}
    return {
        "name": template.name,
        "language": template.language,
        "category": SLOT_CATEGORY[template.slot].value.upper(),
        "components": [component],
    }
