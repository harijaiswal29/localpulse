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
7. **Model-agnostic.** Agents call the **model gateway**, never a vendor SDK directly. Which model runs an agent is config, not code — swappable per agent and per environment, gated by evals — `python scripts/run_evals.py --model <candidate>` must clear the bar before the swap (`docs/evals.md`).

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
│   ├── evals/           # agent eval harness — golden dataset, scorers, runner
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
python scripts/run_evals.py                   # agent eval harness — gates a model/prompt change
python scripts/run_evals.py --model <candidate>   # score a candidate model before swapping
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
- **Vertical Pack:** a pack is a directory exporting `templates`, `onboarding_questions`, `offering_schema`, `calendar_weights`, `playbook`, `guardrails`, and `message_templates` (WhatsApp wording per engine-owned slot). The engine loads a pack by `client_context.vertical_pack_ref`.

---

## Milestone status

**P0 — MVP: done** (2026-07-16). All DoD items met: `packs/bakery/` drives Content + Onboarding; Onboarding produces a valid `ClientContext`; Content generates a week of drafts (caption + image) into the Content Queue; owner Approve/Edit/Skip works over WhatsApp with semi-manual GBP publish; Insights produces the monthly report; `client_id` scoping is in the data model; core paths are tested.

**P1 — Reputation: done** (2026-07-16). Reputation Agent runs hourly review checks (cadence from the pack playbook), drafts replies in the review's own language, escalates negative/ambiguous reviews (A2 — rating ≤ 3 or complaint cues; flagged, never auto-sent), and runs the review-solicitation nudge loop through the Cost Guard. The publisher dispatches by `DraftKind` (GBP post / review reply / WhatsApp nudge); the monthly report includes review response rate. GBP reviews and replies stay semi-manual (seedable list + reply queue in `tools/gbp.py`) until API access is granted (spec §7).

**P2 — Engagement: done** (2026-07-16). All DoD items met: the Engagement Agent auto-answers FAQs and simple pre-order questions (A0) using **deterministic pack templates filled from the Client Context** — the model never improvises a customer-facing answer, so it can never guess one; unmatched or ambiguous messages (including pre-orders naming only a `vague_terms` word like "cake") escalate to the owner (A2) with a pack-defined holding reply; the weekly offer broadcast is drafted (A1, `engagement.weekly_broadcast` Friday cadence), carries an engine-enforced STOP opt-out footer, and publishes to opted-in customers only, priced as marketing with an **all-or-nothing** budget precheck (a blocked batch sends nothing and stays retryable); `CloudApiWhatsAppTool` (WhatsApp Business Cloud API) sits behind the `WhatsAppTool` interface and is selected only when `WHATSAPP_BSP_API_KEY` + `WHATSAPP_PHONE_NUMBER_ID` are set — the mock stays the default offline transport; all FAQ/pre-order/broadcast wording lives in the pack's `EngagementPlaybook`. Conversations track the 24h service window and STOP opt-outs; every enquiry is audit-logged and the monthly report counts enquiries handled.

## Current milestone: P3 (scale-out — see spec §14)

Grow beyond the single pilot.

**Salon pack (Family 2, appointments): done** (2026-07-19). `packs/salon/` proves the pack contract beyond product retail with zero salon logic in the engine. Engine changes were pure de-bakery-fication: `OfferingSchema` gained `requires_appointment` and Onboarding now parses any `offerings.*` field, typing offerings from the pack's schema instead of assuming PRODUCT. Booking cues, walk-in/hours/price FAQs, vague-term escalation ("hair treatment" never gets a guessed quote), Thursday-evening broadcast cadence, and beauty-claim guardrails (no whitening/fairness/permanent-results wording) all live in the pack. Verified by the salon pack contract suite incl. a two-pack isolation test (bakery + salon in one engine).

**Multi-client worker hardening: done** (2026-07-19). The schedule is no longer a startup snapshot: a `worker:resync` job (interval from `WORKER_RESYNC_MINUTES`, default 5) reconciles scheduler jobs with the tenant directory — clients onboarded while the worker runs get scheduled, deleted clients get unscheduled, cadence changes re-schedule, and a client whose pack fails to load is skipped without touching the others. `TaskRouter.dispatch` never raises: per-client/task failures are contained and a circuit breaker (3 consecutive failures → 30-min cooldown) stops a broken tenant from burning worker cycles; a vanished client logs a warning and its jobs drop at the next resync.

**Approval preferences: done** (2026-07-19). Owners promote trusted draft kinds from A1 to A0 via `ApprovalPreferences.auto_publish_kinds` — set over WhatsApp (`AUTO` / `AUTO ON <kind>` / `AUTO OFF <kind>`) or `PUT /clients/{id}/approval-preferences`. Promotion happens inside the Approval State Machine at submit: the draft still walks `drafted → pending_approval → approved` with every step logged (actor `owner_preference`), so golden rule #1 holds. **A2-escalated drafts are never auto-approved** — the state machine checks `meta["escalated"]` before the preference. Delivery is `publisher.publish_ready`, which publishes anything sitting APPROVED (called by the router after every dispatch and by the drafting API endpoints); a budget-blocked publish stays approved and retries on later cadence ticks.

**WhatsApp templates + explicit consent: done** (2026-07-24). Outside the 24h service window WhatsApp delivers approved templates only, so every paid send now resolves to one: packs declare the wording for three engine-owned slots (`review_nudge` utility, `weekly_offer` marketing, `owner_alert` utility) and `orchestrator/messaging.send_whatsapp` refuses a paid send without one (`TemplateRequiredError`) before charging anything — inside the window the same words still go free-form, so nothing pays twice. Rendering happens once, at draft time: the rendered template travels on `draft.meta["template"]`, so what the owner approves is byte-for-byte what publishing sends. Consequences: a review nudge is now pack wording, not model output; the broadcast generates only the offer line that fills `{offer}`; the STOP footer lives inside the approved body (engine-validated) since a template send cannot be appended to; owner digests can't be sent cold (parameters can't carry newlines), so a closed owner window gets the short `owner_alert` that re-opens it. `EDIT` on a templated draft rewrites the one content parameter and re-renders; on fixed-wording slots it is refused. Marketing consent is now explicit by default (`MARKETING_OPT_IN_MODE`): customers join by replying `START`, recorded with source + timestamp; the pack's `opt_in_invite` asks once on first contact; STOP always wins. Submission tooling: `scripts/export_whatsapp_templates.py` emits the Meta payloads (runbook in `docs/whatsapp-templates.md`).

**Delivery reliability — resumable broadcasts + retry/backoff: done** (2026-07-24). Transient tool failures are now separated from permanent ones (`tools/retry.py`): a 429/408/5xx or a dropped connection is retried with jittered exponential backoff (3 attempts, honouring `Retry-After`), while a 4xx Meta will never accept fails immediately instead of re-sending. `CloudApiWhatsAppTool` classifies its own responses rather than calling `raise_for_status`. The publish log records a draft as a whole, which was too coarse for a broadcast: a batch that died on recipient 7 of 20 was re-sent in full, so the first six customers got the offer twice and the shop paid twice. Every send is now recorded per recipient in `broadcast_deliveries` as it settles, so a retry resumes — a permanently rejected number is settled as failed and the batch carries on (`wa-broadcast:<sent>/<total>`), a transient failure stops the run and leaves the draft APPROVED for the next cadence tick (`PartialDeliveryError`), and the budget precheck covers only what is still owed. `send_whatsapp` now authorises the budget before the send and charges after it, so the ledger counts messages that actually left.

**Eval harness: done** (2026-07-24). `src/localpulse/evals/` + `python scripts/run_evals.py` — the gate for any prompt or model change (spec §12.2/§13.1); runbook in `docs/evals.md`. A golden dataset of `ClientContext → expected content characteristics` runs the **real** agents through the **real** engine and scores what reaches the owner on six deterministic dimensions (`grounding`, `language`, `guardrails`, `brand_voice`, `coverage`, `containment`) — no judge model, so a swap gate never begs the question. Three suites: **core** (English, must pass on any provider incl. the mock), **multilingual** (Marathi/Hindi — the mock fails it by design, asserted in tests so it's never mistaken for shippable), **redteam** (a rigged provider tries banned terms/health claims/ungrounded/oversized copy; scored on whether the *engine* contained it). Exit codes gate a release: `0` pass / `1` below bar / `2` regressed against `--baseline`. `coverage` is a first-class dimension because a model can fail without writing a bad caption — if the engine rejects everything it writes, the shop's week is empty. Model injection is config, not code: `ModelGateway(providers={...})` registers a named provider and `Container(settings, gateway=...)` accepts it.

**Guardrail fix found by the harness** (2026-07-24): `Guardrails.forbid_health_claims` was declared `True` by both packs and enforced *nowhere* — "clinically proven to boost immunity" passed the banned-term list and reached the approval queue (red-team case `rt_health_claim`). `agents/common.py` now carries generic `HEALTH_CLAIM_PATTERNS` (phrases, not words, so "treat yourself"/"healthy breakfast" never trip it) applied only when a pack opts in; `content.check_guardrails` delegates to `check_text_guardrails` instead of duplicating it. The eval keeps its **own** claim list on purpose — an eval that imports the engine's checks can only ever agree with it.

**Price grounding: done** (2026-08-05). Every rupee amount in generated copy must be one the shop actually charges — `require_price_grounding` on `Guardrails`, **default True** because this is an engine concern, not a vertical preference, so a new pack inherits it without knowing the flag exists. `agents/common.py` owns `PRICE_PATTERN` / `prices_in` / `grounded_prices` / `find_ungrounded_price`, and `check_text_guardrails` now takes `ctx` as a **required** argument — an optional one could be silently omitted at a call site, which is the exact failure this exists to prevent. It covers all four generative paths (captions, review replies, review nudges, broadcast offer lines). The owner can authorise an amount that isn't on the menu by naming it: `draft_weekly_broadcast(offer_text="₹50 off every cake")` passes `extra_prices=prices_in(offer)`, so ₹50 is allowed while a price the model adds on top is not — without that, the check would be useless for real marketing. Only rupee-marked amounts count (`₹`/`Rs`), so "open till 9" and "20 varieties" never trip it. Red-team case `rt_invented_price` pins it, and is deliberately clean on every other axis so only the price check can contain it. Content prompts now format prices `:g` not `:.0f` — showing the model ₹100 for a ₹99.50 item invited a rejection the model couldn't fix.

**Remaining P3 items:** GBP API integration once access is granted.

**Carry-over open items:** apply for GBP API access — runbook with pre-drafted form answers in `docs/gbp-api-access.md` (blocking prerequisite: the applicant email must be a manager on a verified GBP active 60+ days, so arrange pilot-profile access first); submit the WhatsApp templates once the WABA exists (`docs/whatsapp-templates.md` — that review runs in parallel with the GBP wait); confirm the approximate 2026 festival dates in `context/regional_calendar.py` before real pilots. Note there are still no migrations (`create_all` only, spec/P0 decision) — the conversations table gained consent columns and `broadcast_deliveries` is a new table, so an existing dev `localpulse.db` must be recreated.

**Known gaps against the spec** (audited 2026-07-24, none blocking a hand-held pilot): no dead-letter queue, per-tool circuit breaker, or credential-expiry detection (§12.1); a publish failure is logged and retried but never reaches the owner ("couldn't post — retry?"); evergreen approval items never re-notify; `ApprovalPreferences.quiet_hours` is defined but enforced nowhere; brand voice `example_posts` is captured at onboarding and never read (no vector store — §8/§13); broadcast engagement and approval turnaround aren't tracked (§10); `MockWebSearchTool` is a stub nothing calls; spec §14's P3 also lists self-serve onboarding and an owner dashboard, both unbuilt.

**Known limits of the eval harness** (`docs/evals.md` has the full version): semantic tone isn't scored — `brand_voice` measures register (hype, shouting, speaking as the shop), not whether a caption *feels* homely; and **an invented item is caught at swap time or not at all**. Invention splits in two: a price is a number, so the engine checks it at runtime (see "Price grounding" above), but an item is a noun phrase, and spotting one the shop doesn't sell inside arbitrary prose needs NER or a judge model. The grounding check asks that the intended offering is named, never that nothing else was added, so "Chocolate truffle cake ₹550 and fresh butter croissants" still clears every runtime check. That's the argument against `AUTO ON gbp_post` for a model that hasn't cleared the grounding dimension — and it sharpens when GBP publishing stops being semi-manual, because the owner's copy-paste into Google is currently the last human read of a caption. Non-English grounding is also weak: offering names are stored in English and matched as substrings, so a good Marathi caption that transliterates the item name gets rejected by the engine (surfacing as `coverage`, not `language`) — fix before any Marathi-first pilot.

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
