"""Export a pack's WhatsApp message templates for submission to Meta.

    python scripts/export_whatsapp_templates.py bakery
    python scripts/export_whatsapp_templates.py bakery --curl > submit.sh

Templates have to be approved before anything can be sent outside the 24h service
window, and approval takes time — submit early. Each printed payload is the body of
POST /{waba_id}/message_templates; see docs/whatsapp-templates.md for the wiring.
"""

from __future__ import annotations

import argparse
import json
import sys

from localpulse.orchestrator.templates import SLOT_CATEGORY, meta_payload
from localpulse.packs.base import PackLoadError, load_pack


def examples(business: str, city: str, offer: str) -> dict[str, str]:
    """Sample parameter values shown to Meta's reviewer. They should look like the
    real business the templates are being submitted for — a reviewer rejects
    placeholder-looking examples."""
    return {
        "business_name": business,
        "customer_name": "Priya",
        "city": city,
        "offer": offer,
        "summary": "3 post(s) waiting for approval",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pack_ref", help="vertical pack to export, e.g. bakery")
    parser.add_argument("--business", default="Your Business", help="example business name")
    parser.add_argument("--city", default="Pune", help="example city")
    parser.add_argument(
        "--offer", default="this week's special, ₹550", help="example weekly offer line"
    )
    parser.add_argument(
        "--curl",
        action="store_true",
        help="emit curl commands instead of raw JSON (set WABA_ID and WHATSAPP_TOKEN)",
    )
    args = parser.parse_args()

    try:
        pack = load_pack(args.pack_ref)
    except PackLoadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not pack.message_templates:
        print(f"pack {pack.ref!r} declares no message templates", file=sys.stderr)
        return 1

    samples = examples(args.business, args.city, args.offer)
    for template in pack.message_templates:
        payload = meta_payload(template, samples)
        category = SLOT_CATEGORY[template.slot].value
        if args.curl:
            print(f"# {template.slot.value} — charged as {category}")
            print(
                'curl -X POST "https://graph.facebook.com/v20.0/$WABA_ID/message_templates" \\\n'
                '  -H "Authorization: Bearer $WHATSAPP_TOKEN" \\\n'
                '  -H "Content-Type: application/json" \\\n'
                f"  -d '{json.dumps(payload, ensure_ascii=False)}'\n"
            )
        else:
            print(f"--- {template.slot.value} ({category}) ---")
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
