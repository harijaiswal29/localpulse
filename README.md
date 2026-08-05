# LocalPulse

An AI system that keeps a local business's online presence — Google Business Profile,
reviews, WhatsApp — active on autopilot, with the owner approving anything that goes
public. Built as a **generic engine + swappable Vertical Packs**, multi-tenant from the
data model up. Coordinated AI agents *draft* work; a human approves anything public.

Full design: [`docs/multi-agent-system-spec.md`](docs/multi-agent-system-spec.md) ·
Build guide: [`CLAUDE.md`](CLAUDE.md)

## Status: P3 in progress (P0 MVP + Reputation + Engagement + second vertical)

The full slice runs for two verticals — **bakery** (Family 1, products) and
**salon** (Family 2, appointment services) — on one generic engine:

- **Onboarding Agent** — pack question set → validated `ClientContext`
- **Content Agent** — a week of drafts (caption + image ref) into the Content Queue,
  grounded in real offerings, festival-calendar aware (Maharashtra-weighted),
  guardrail-checked before the owner ever sees them
- **Approval flow** — owner replies `LIST` / `APPROVE <id>` / `EDIT <id> <text>` /
  `SKIP <id>` on WhatsApp; approved items publish through the Approval State Machine
  (`drafted → pending_approval → approved → published`); GBP publishing is
  semi-manual in P0 (approved posts land in a per-client publish queue)
- **Reputation Agent** (P1) — hourly review checks; drafts public replies in the
  review's own language and the owner's voice; negative or ambiguous reviews are
  escalated (A2) and never auto-send; runs the post-purchase review-solicitation
  nudge loop through the Cost Guard
- **Engagement Agent** (P2) — answers customer WhatsApp messages: FAQs and simple
  pre-orders are auto-answered (A0) from **deterministic pack templates** filled with
  Client Context — free inside the 24h service window, and never a guessed reply;
  anything not confidently matched escalates to the owner (A2) with a polite holding
  message; drafts the weekly offer broadcast (A1) to opted-in customers, priced as
  marketing and budget-checked per recipient (`STOP` opt-out honoured)
- **Insights Agent** — daily metric collection + plain-language monthly report,
  including review response rate and WhatsApp enquiries handled
- **Cost Guard** — every outbound WhatsApp message is categorised (free service-window
  reply preferred) and budget-checked before sending
- **WhatsApp transport** — offline mock by default; setting `WHATSAPP_BSP_API_KEY` +
  `WHATSAPP_PHONE_NUMBER_ID` switches to the real WhatsApp Business Cloud API adapter
  behind the same `WhatsAppTool` interface
- **Salon Vertical Pack** (P3) — proves the pack contract beyond product retail:
  offerings are appointment services (typed by the pack's offering schema, no engine
  change per vertical), booking enquiries quote and alert the owner, walk-in/hours/price
  FAQs auto-answer, vague requests ("hair treatment") escalate instead of guessing
- **Hardened multi-client worker** (P3) — the cadence schedule tracks the tenant
  directory live (new clients scheduled, deleted clients unscheduled, broken packs
  skipped — no restart needed), and dispatch is isolated per client/task with a
  circuit breaker so one failing tenant never starves the rest
- **Approval preferences** (P3) — owners promote trusted draft kinds to publish on
  autopilot (`AUTO ON gbp_post` over WhatsApp, or the REST endpoint); every
  auto-publish is still a logged pass through the Approval State Machine, and
  anything escalated (A2) always waits for a human, whatever the preference says
- **WhatsApp message templates** (P3) — outside the 24h window WhatsApp delivers
  approved templates only, so every paid send resolves to one: packs own the wording
  for three engine-owned slots (review nudge, weekly offer, owner alert), the choke
  point refuses a paid send without one *before* charging, and inside the window the
  same words still go free-form. What the owner approves is byte-for-byte what each
  recipient receives ([submission runbook](docs/whatsapp-templates.md))
- **Explicit marketing consent** (P3) — customers join the broadcast audience only by
  replying `START`, recorded with its basis and timestamp; the pack asks once, on a
  first contact, inside the free window; `STOP` always wins
- **Resumable delivery** (P3) — temporary tool failures (429, 5xx, dropped connection)
  are retried with exponential backoff while a permanently rejected message fails fast;
  every broadcast send is recorded per recipient, so a batch that dies halfway resumes
  on the next attempt instead of re-sending — nobody is messaged, or charged for,
  twice
- **Price grounding** (P3) — no generated caption, review reply or offer line may
  quote a rupee amount the shop doesn't actually charge; a discount the owner asks
  for is authorised by them naming it. Invented *prices* are a set-membership test
  and are blocked at runtime; invented *items* need the eval harness, which is why
  auto-publish is a considered choice rather than a default
- **Eval harness** (P3) — the gate for changing a model or a prompt: a golden dataset
  runs the real agents through the real engine and scores what reaches the owner for
  grounding (no invented items or prices), language (Marathi/Hindi/English), guardrail
  compliance, brand register, coverage, and containment of a deliberately misbehaving
  model. Deterministic — no judge model — and it exits non-zero, so a bad swap stops a
  release instead of reaching a shop's profile ([runbook](docs/evals.md))

Next up: multi-tenant scale-out and GBP API onboarding (see spec §14–15).

## Setup

```bash
cp .env.example .env        # then fill in secrets (runs offline with defaults)
pip install -e ".[dev]"
```

Defaults run fully offline: SQLite database and the `mock` model provider (no API keys
needed). Which model runs each agent is config (`MODEL_CONTENT=claude-...` etc.), never
code — agents only talk to the model gateway.

## Run

```bash
uvicorn localpulse.api.main:app --reload    # API + WhatsApp webhook
python -m localpulse.orchestrator.worker     # cadence engine / scheduled agent runs
python scripts/run_pilot.py                  # seeded end-to-end demo in the terminal
python scripts/export_whatsapp_templates.py bakery   # message templates to submit to Meta
```

Try the flow with curl:

```bash
# onboard a pilot shop (answers keyed by the bakery pack's question ids)
curl -X POST localhost:8000/clients/pilot-1/onboard \
  -H 'content-type: application/json' \
  -d '{"pack_ref": "bakery", "answers": {"shop_name": "Mane'\''s Bakehouse", "address": "12 FC Road", "city": "Pune", "hours": "8am-9pm", "owner_whatsapp": "+919812345678", "specialties": "Chocolate truffle cake ₹550, Modak box ₹300", "tone": "warm, homely", "languages": "English"}}'

# generate a week of drafts
curl -X POST localhost:8000/clients/pilot-1/content/run \
  -H 'content-type: application/json' -d '{"week_start": "2026-07-20"}'

# owner approves over WhatsApp (webhook simulation)
curl -X POST localhost:8000/webhooks/whatsapp \
  -H 'content-type: application/json' \
  -d '{"from_number": "+919812345678", "text": "LIST"}'

# check for new reviews and draft replies (also runs hourly via the worker)
curl -X POST localhost:8000/clients/pilot-1/reputation/check

# draft a post-purchase review nudge for a happy customer
curl -X POST localhost:8000/clients/pilot-1/reputation/nudge \
  -H 'content-type: application/json' \
  -d '{"customer_number": "+919900112233", "customer_name": "Priya"}'

# a customer messages the shop — auto-answered or escalated, never guessed
curl -X POST localhost:8000/clients/pilot-1/engagement/inbound \
  -H 'content-type: application/json' \
  -d '{"customer_number": "+919900112233", "customer_name": "Priya", "text": "What time do you close?"}'

# a customer opts in to offers — nothing else puts them in the broadcast audience
curl -X POST localhost:8000/clients/pilot-1/engagement/inbound \
  -H 'content-type: application/json' \
  -d '{"customer_number": "+919900112233", "text": "START"}'

# draft this week's offer broadcast for the opted-in audience (owner approves before send)
curl -X POST localhost:8000/clients/pilot-1/engagement/broadcast \
  -H 'content-type: application/json' -d '{}'

# promote a trusted draft kind to auto-publish (A1 -> A0; A2 escalations always wait)
# owners can do the same over WhatsApp: AUTO ON gbp_post / AUTO OFF gbp_post
curl -X PUT localhost:8000/clients/pilot-1/approval-preferences \
  -H 'content-type: application/json' -d '{"auto_publish_kinds": ["gbp_post"]}'
```

## Quality

```bash
pytest                        # 234 tests: state machine, cost guard, packs, tenant
                              # isolation, content eval, reputation, engagement,
                              # salon pack, worker hardening, approval prefs,
                              # WhatsApp templates + consent, retry/partial
                              # delivery, eval harness, price grounding, e2e
ruff check . && ruff format .

python scripts/run_evals.py                            # score the configured models
python scripts/run_evals.py --model claude-sonnet-4-5  # score a candidate before swapping
python scripts/run_evals.py --suite core               # offline smoke run
```

The eval harness is the gate for a model or prompt change: `0` pass, `1` below the
bar, `2` passed but regressed against a `--baseline`. Suites are `core` (English —
every provider must clear it), `multilingual` (Marathi/Hindi — the offline mock
fails it by design) and `redteam` (a model misbehaving on purpose; scored on whether
the engine contained it). See [`docs/evals.md`](docs/evals.md).

## Repo map

```
src/localpulse/
├── orchestrator/   # approval state machine, cost guard, tool registry, cadence worker
├── agents/         # onboarding, content, reputation, engagement, insights (stateless)
├── tools/          # GBP (semi-manual), WhatsApp (mock + Cloud API), image gen — typed clients
├── llm/            # model gateway (provider-agnostic; per-agent model config)
├── packs/          # vertical packs — ALL vertical logic lives here (bakery/, salon/)
├── context/        # Client Context pydantic models + client_id-scoped repositories
├── data/           # SQLAlchemy models — every table carries client_id
├── evals/          # agent eval harness — golden dataset, scorers, pass bar
└── api/            # FastAPI: WhatsApp inbound webhook + approval endpoints
```

**Golden rules** live in [`CLAUDE.md`](CLAUDE.md) — nothing publishes without an
approval, the engine stays generic, agents are stateless, and all spend routes
through the Cost Guard.
