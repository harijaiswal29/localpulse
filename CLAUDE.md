# CLAUDE.md — LocalPulse

Operational guide for building this project in Claude Code. **Read `docs/multi-agent-system-spec.md` for the full design.** This file is the how-we-build-it companion: structure, rules, commands, and the current milestone.

---

## What this is (one paragraph)

An AI system that keeps a local business's online presence — Google Business Profile, reviews, WhatsApp — active on autopilot, with the owner approving anything that goes public. It's a **generic engine + swappable Vertical Packs**, multi-tenant, serving many clients from one codebase. Coordinated AI agents *draft* work; a human approves anything public.

---

## Golden rules (never violate these)

1. **Nothing publishes to a public channel without an approved item** that has passed the Approval State Machine (`drafted → pending_approval → approved → published`).
2. **Keep the engine generic.** No vertical-specific logic anywhere except `src/localpulse/packs/`. If a bakery assumption leaks into the orchestrator or an agent, that's a bug.
3. **Multi-tenant always.** Every operation is scoped by `client_id`. No global/shared mutable state across clients.
4. **Cost-aware by default.** All outbound messaging routes through the Cost Guard. Prefer free WhatsApp *service-window* replies; never send a marketing template where a service reply works.
5. **Negatives and low-confidence actions escalate (A2)** to the owner — never auto-send.
6. **Agents are stateless.** They read/write only via Client Context repositories.
7. **Model-agnostic.** Agents call the **model gateway**, never a vendor SDK directly. Which model runs an agent is config, not code — swappable per agent and per environment, gated by evals.

---

## Architecture at a glance

Orchestrator (cadence · router · tool registry · approval state machine · cost guard) coordinates five stateless agents — **Onboarding, Content, Reputation, Engagement, Insights** — which act through an MCP **tool layer** (GBP, WhatsApp/BSP, image gen, web search, metrics) and read a per-client **Client Context**. A **Vertical Pack** conditions agent behaviour per business type. Full detail + diagram in the spec.

---

## Repo structure

```
localpulse/
├── CLAUDE.md
├── README.md
├── pyproject.toml
├── .env.example
├── docs/
│   └── multi-agent-system-spec.md      # the design spec
├── src/localpulse/
│   ├── orchestrator/    # cadence engine, task router, tool registry,
│   │                    #   approval state machine, cost guard
│   ├── agents/          # onboarding, content, reputation, engagement, insights
│   ├── tools/           # MCP clients: gbp, whatsapp, imagegen, websearch, metrics
│   ├── llm/             # model gateway + per-agent model config (provider-agnostic)
│   ├── packs/           # vertical packs — bakery/, salon/, ...  (ALL vertical logic here)
│   ├── context/         # Client Context pydantic models + repositories
│   ├── data/            # db models, migrations, vector store access
│   └── api/             # FastAPI app: WhatsApp inbound webhook, approval endpoints
├── tests/
└── scripts/
```

---

## Tech stack & commands

- **Python 3.12**, FastAPI, pydantic (models + settings)
- **Agent generation:** a **model gateway** (LiteLLM / OpenRouter / thin adapter) — model per agent is configurable; default Claude Sonnet-class, but any provider or local/open model (Ollama) can be swapped in
- **Postgres + pgvector**; object storage for generated images
- **Scheduler:** APScheduler (or Celery at scale)
- **Tools** exposed as MCP servers with typed interfaces
- Lint/format: **ruff**; tests: **pytest**

```bash
# setup
cp .env.example .env        # then fill in secrets
pip install -e ".[dev]"

# run
uvicorn localpulse.api.main:app --reload    # API + webhooks
python -m localpulse.orchestrator.worker     # scheduler / agent runs

# quality
pytest
ruff check . && ruff format .
```

---

## Config & secrets (`.env`)

Never hard-code these; load via pydantic-settings.

```
DATABASE_URL=postgresql://...
OBJECT_STORAGE_URL=
OBJECT_STORAGE_KEY=

# Model gateway — model per agent is configurable; providers below are optional
LLM_GATEWAY=litellm                 # or openrouter / custom
ANTHROPIC_API_KEY=                  # default provider
OPENROUTER_API_KEY=                 # optional — many models via one key
GEMINI_API_KEY=                     # optional
GROQ_API_KEY=                       # optional — fast free tier
OLLAMA_BASE_URL=http://localhost:11434   # optional — local/free models
# per-agent model map (task profile → model), e.g.:
MODEL_CONTENT=claude-sonnet
MODEL_ROUTER=                        # e.g. a cheap/free model
MODEL_INSIGHTS=
MODEL_REPUTATION=
MODEL_ENGAGEMENT=

# WhatsApp (via BSP) — leave both empty to use the offline mock transport;
# set both to switch every client to the WhatsApp Cloud API adapter
WHATSAPP_BSP_API_KEY=
WHATSAPP_PHONE_NUMBER_ID=

# Google Business Profile (OAuth) — access is gated; see spec §7
GBP_OAUTH_CLIENT_ID=
GBP_OAUTH_CLIENT_SECRET=
# per-client refresh tokens stored encrypted in DB, not here

APP_ENV=dev
LOG_LEVEL=INFO
```

---

## Interface contracts (high level)

- **Agent I/O:** every agent takes `(client_context, trigger_payload)` and returns either a `PublishedAction` or a `DraftItem` (which enters the approval queue). Model both as pydantic types in `context/`.
- **Tools:** each MCP tool exposes typed methods (e.g. `gbp.post(...)`, `gbp.list_reviews(...)`, `gbp.reply_review(...)`, `whatsapp.send(to, body, category)`). Agents call tools only via the Tool Registry, never directly — and never call `whatsapp.send` themselves: all outbound WhatsApp goes through `orchestrator.messaging.send_whatsapp`, the single choke point where the Cost Guard picks the category and checks the budget.
- **Vertical Pack:** a pack is a directory exporting `templates`, `onboarding_questions`, `offering_schema`, `calendar_weights`, `playbook`, and `guardrails`. The engine loads a pack by `client_context.vertical_pack_ref`.

---

## Milestone status

**P0 — MVP: done** (2026-07-16). All DoD items met: `packs/bakery/` drives Content + Onboarding; Onboarding produces a valid `ClientContext`; Content generates a week of drafts (caption + image) into the Content Queue; owner Approve/Edit/Skip works over WhatsApp with semi-manual GBP publish; Insights produces the monthly report; `client_id` scoping is in the data model; core paths are tested.

**P1 — Reputation: done** (2026-07-16). Reputation Agent runs hourly review checks (cadence from the pack playbook), drafts replies in the review's own language, escalates negative/ambiguous reviews (A2 — rating ≤ 3 or complaint cues; flagged, never auto-sent), and runs the review-solicitation nudge loop through the Cost Guard. The publisher dispatches by `DraftKind` (GBP post / review reply / WhatsApp nudge); the monthly report includes review response rate. GBP reviews and replies stay semi-manual (seedable list + reply queue in `tools/gbp.py`) until API access is granted (spec §7).

**P2 — Engagement: done** (2026-07-16). All DoD items met: the Engagement Agent auto-answers FAQs and simple pre-order questions (A0) using **deterministic pack templates filled from the Client Context** — the model never improvises a customer-facing answer, so it can never guess one; unmatched or ambiguous messages (including pre-orders naming only a `vague_terms` word like "cake") escalate to the owner (A2) with a pack-defined holding reply; the weekly offer broadcast is drafted (A1, `engagement.weekly_broadcast` Friday cadence), carries an engine-enforced STOP opt-out footer, and publishes to opted-in customers only, priced as marketing with an **all-or-nothing** budget precheck (a blocked batch sends nothing and stays retryable); `CloudApiWhatsAppTool` (WhatsApp Business Cloud API) sits behind the `WhatsAppTool` interface and is selected only when `WHATSAPP_BSP_API_KEY` + `WHATSAPP_PHONE_NUMBER_ID` are set — the mock stays the default offline transport; all FAQ/pre-order/broadcast wording lives in the pack's `EngagementPlaybook`. Conversations track the 24h service window and STOP opt-outs; every enquiry is audit-logged and the monthly report counts enquiries handled.

## Current milestone: P3 (scale-out — see spec §14)

Grow beyond the single pilot: second Vertical Pack (salon — Family 2, appointments) to prove the pack contract; multi-client worker hardening; GBP API integration once access is granted; owner-configurable approval preferences (A1→A0 promotions for trusted draft kinds, never for A2-escalated items).

**Carry-over open items:** apply for GBP API access (spec §15); confirm the approximate 2026 festival dates in `context/regional_calendar.py` before real pilots; real deployments must collect explicit marketing opt-in (pilot treats an inbound message as implied consent, STOP revokes).

---

## Conventions

- Type hints everywhere; pydantic for all Client Context and agent I/O.
- Keep vertical logic out of the engine (rule #2). When in doubt, it goes in `packs/`.
- Small, testable functions for the approval state machine and cost guard — these are the safety-critical paths.
- Log every published action with the approval that authorised it (auditability).

## Start here

1. Read `docs/multi-agent-system-spec.md`.
2. The repo is scaffolded and P0 + P1 + P2 are built (see Milestone status). Verify with `pytest`, `ruff check .`, and `python scripts/run_pilot.py` — everything runs offline via the mock model provider and mock tools; `src/localpulse/container.py` is the composition root.
3. Work the P3 items above, keeping all vertical logic in `packs/`.
