# WhatsApp message templates — how LocalPulse sends paid messages

WhatsApp delivers free-form text **only inside the 24h service window** that a
customer's own message opens. Outside it, the platform accepts nothing but a
**pre-approved message template**. That single rule shapes every paid send in
LocalPulse, so it is worth stating plainly:

| Where | What is delivered | What it costs |
|---|---|---|
| Inside the 24h window | free-form text | ₹0 (service reply) |
| Outside it | an approved template, parameters only | utility or marketing rate |

The Cost Guard already picks the cheapest legal category for every message; the
template rule is enforced in the same place (`orchestrator/messaging.py`): a paid
send without a template raises `TemplateRequiredError` **before** anything is
charged or sent, rather than failing at the BSP.

## The three slots

The engine owns the slots (where a template is sent from, and what it costs); each
Vertical Pack owns the wording. Adding a vertical means writing three bodies, not
touching the engine.

| Slot | Sent when | Meta category |
|---|---|---|
| `review_nudge` | post-purchase "please review us" | UTILITY |
| `weekly_offer` | the weekly broadcast to the opted-in audience | MARKETING |
| `owner_alert` | the owner needs telling and their window is cold | UTILITY |

Two consequences worth knowing:

- **Nudges and broadcasts are not free-form model output any more.** A nudge is the
  pack template, rendered — identical every time, because that is what Meta
  approved. For a broadcast the model writes only the offer line that fills
  `{offer}`; the rest of the sentence is fixed.
- **The owner's digest cannot be sent cold.** A template parameter may not contain
  newlines, so a multi-line draft digest physically cannot go out as one. When the
  owner's window is closed they get the short `owner_alert` instead ("…3 post(s)
  waiting for approval. Reply LIST to see everything waiting."), which re-opens the
  window; their reply brings the full detail free-form.

## Writing a template (what Meta will reject)

`MessageTemplate` enforces these at pack load, so a bad body fails in `pytest`
rather than at submission:

- lowercase snake_case name (`bakery_weekly_offer_v1`)
- the body may not **start or end** with a placeholder, and may not put two
  placeholders next to each other — the approved part must be the fixed text
- no placeholder twice in one body
- only placeholders the engine can fill for that slot (`SLOT_PARAMS` in
  `context/models.py`)
- a `weekly_offer` body must tell the reader to reply **STOP** — the opt-out lives
  inside the approved wording, because the engine cannot append a footer to a
  template send

## Submitting them

Templates take time to review, so submit as soon as the WhatsApp Business Account
exists — that wait can run in parallel with the GBP application
(`docs/gbp-api-access.md`).

```bash
# see what would be submitted
python scripts/export_whatsapp_templates.py bakery --business "Mane's Bakehouse"

# or emit ready-to-run curl calls
export WABA_ID=... WHATSAPP_TOKEN=...
python scripts/export_whatsapp_templates.py bakery --business "Mane's Bakehouse" --curl
```

Each payload is the body of `POST /{waba_id}/message_templates`. You can equally
paste the text into WhatsApp Manager → Message Templates; the `category` in the
payload is the one to pick there, and it must match — a marketing message sent on a
utility template is the classic overspend, and Meta re-categorises (or blocks)
templates that misdeclare.

Use example values that look like the real business. Reviewers reject
placeholder-looking samples.

## After approval

Nothing in the code changes. The template names in the pack must match the approved
names, `WHATSAPP_BSP_API_KEY` + `WHATSAPP_PHONE_NUMBER_ID` switch the transport to
`CloudApiWhatsAppTool`, and paid sends start going out as
`type: "template"` with positional parameters. Until then the mock transport records
exactly the same payloads offline.

## Marketing consent

Meta requires opt-in before marketing templates. LocalPulse defaults to
`MARKETING_OPT_IN_MODE=explicit`: a customer joins the broadcast audience only by
replying **START** (recorded with its source and timestamp), and the pack's
`opt_in_invite` asks for it once, on a first contact, inside the free window. STOP
revokes and always wins — a later message never revives consent.

`MARKETING_OPT_IN_MODE=implied` is the looser pilot basis (messaging the business
counts as consent, applied on first contact only). Real deployments should stay on
`explicit`.
