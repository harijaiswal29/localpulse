# AI Local Presence — Multi-Agent System
### High-Level System Specification (v0.3)

*Working name: "LocalPulse" (placeholder). Scope: an AI system that keeps a local business's online presence — Google Business Profile, reviews, and WhatsApp — active on autopilot, with the owner approving anything that goes public. Delivered as a **generic engine + swappable Vertical Packs** so it scales across business types without going generic.*

---

## 1. Purpose & Scope

Small local businesses (bakeries, boutiques, clinics, salons) lose customers not because of their product but because they are hard to *find, evaluate, and reach* online. The work needed to fix this — posting consistently, responding to reviews, replying to enquiries — is repetitive, never-ending, and low-value for the owner to do manually.

This system automates the *generation and orchestration* of that work using a coordinated set of AI agents, while keeping a human (the shop owner) in control of anything published to a public channel. It is designed to serve **many clients from one codebase**, so that the marginal cost of an additional client stays near zero.

**In scope (v1):** Google Business Profile presence, review management, WhatsApp engagement, content generation, performance reporting.
**Out of scope (v1):** paid ad management, e-commerce/checkout, inventory/POS, full social (Instagram/Facebook) posting — deferred to a later phase.

---

## 2. Vertical Strategy & Vertical Packs

"Works for every small business" is a trap: a product that fits all fits none, and no owner buys something addressed to "all small businesses." The resolution is **generic engine, vertical product** — one shared engine (orchestrator, agents, tools, Client Context) plus a thin, swappable **Vertical Pack** per business type.

A Vertical Pack contains only what differs by vertical:

- content templates & posting cadence
- onboarding question set
- offering/catalogue schema (product vs. service vs. appointment)
- local calendar weighting & content hooks
- review / engagement playbook
- vertical-specific guardrails

Adding a vertical means **shipping a pack, not touching the engine.** Each new pack reuses ~80% of the last, so delivery speed compounds as the catalogue of packs grows.

### 2.1 The Rollout Path

Ambition: cover the **entire strong-fit category first, then partial-fit.** This is reached as a *sequence of pack families*, ordered easy → hard — not as one universal build. Two axes decide the order: catalogue type (product vs. service/appointment) and regulatory weight (low vs. high).

| Order | Pack family | Example verticals | Catalogue | Guardrail weight |
|---|---|---|---|---|
| 1 | Product retail & food | bakery, café, restaurant, boutique | Product / menu | Low |
| 2 | Appointment services | salon, spa, gym, coaching | Service + slots | Low–medium |
| 3 | Home & trade services | plumber, electrician, repair | Lead + dispatch | Medium (trust) |
| 4 | Regulated services | clinic, dentist | Service + slots | **High (health claims)** |
| — | *Partial-fit (later)* | real estate, wedding photography, interior design | Portfolio + lead | Medium; **needs engine extension** |

Launch = **one vertical inside Family 1** (bakery or salon). Families 1–4 complete "strong-fit." Partial-fit follows.

### 2.2 Why partial-fit is genuinely later (not just another pack)

Strong-fit businesses want an *always-on presence* — post, reply, get found. Partial-fit businesses sell high-value, low-frequency, long-consideration purchases: a wedding photographer or property agent needs **lead nurture over weeks**, portfolio-led content, and follow-up sequences, not a weekly special. That is a different *engine behaviour* (nurture flows + a lightweight CRM), not just different templates.

**Design implication now:** keep the orchestrator's cadence and messaging model general enough that a long nurture sequence can slot in later — do **not** hard-code the "weekly presence" rhythm as the only mode in P0.

---

## 3. Design Principles

1. **Generation is cheap; publishing is the constraint.** LLMs draft posts and replies trivially. The friction is pushing them live (Google and Meta gate their posting APIs). Architect around drafting freely and publishing carefully.
2. **Human-in-the-loop for anything public.** The AI never posts to a public profile or sends a marketing broadcast without owner approval. This is a brand-safety feature, not a limitation — the shop's reputation is on the line, not ours.
3. **Multi-tenant from the core.** Every agent is stateless and operates against a per-client **Client Context**. No client-specific logic is hard-coded.
4. **Engine generic, packs specific.** Business-type differences live in Vertical Packs (§2), never in the engine.
5. **Tools are pluggable.** Each external platform is exposed as a tool (MCP server). Agents discover which tools a client has connected and adapt — a client without WhatsApp API simply has that capability disabled.
6. **Cost-aware by design.** Message categories, API calls, and token spend are tracked per client. The system defaults to the cheapest valid path (e.g. WhatsApp *service-window* replies, which are free).
7. **Prove value visibly.** A monthly plain-language report is a first-class output, not an afterthought — it is the primary defence against churn.
8. **Model-agnostic.** Agents never call a vendor SDK directly — they go through a model gateway. Which model runs each agent is *configuration, not code*, so Claude, an open/free model, or a local model can be swapped in per agent and per environment. Swaps are gated by the eval harness (§12.2), not by guesswork.

---

## 4. Architecture Overview

```
┌──────────────────────────────────────────────────────────────┐
│                        OWNER TOUCHPOINTS                       │
│      WhatsApp approvals   ·   Monthly report   ·   Alerts      │
└───────────────▲───────────────────────────────▲──────────────┘
                │                                │
┌───────────────┴────────────────────────────────┴──────────────┐
│                      ORCHESTRATION LAYER                        │
│   Scheduler / Cadence Engine  ·  Task Router  ·  Tool Registry │
│              Approval State Machine  ·  Cost Guard             │
└───┬───────────┬───────────┬───────────┬───────────┬───────────┘
    │           │           │           │           │
┌───▼───┐  ┌────▼────┐  ┌───▼─────┐ ┌───▼──────┐ ┌──▼───────┐
│Onboard│  │ Content │  │Reputation│ │Engagement│ │ Insights │
│ Agent │  │  Agent  │  │  Agent   │ │  Agent   │ │  Agent   │
└───┬───┘  └────┬────┘  └───┬─────┘ └───┬──────┘ └──┬───────┘
    │           │           │           │           │
    │        [ VERTICAL PACK: templates · schema · guardrails ]
    │           │           │           │           │
┌───▼───────────▼───────────▼───────────▼───────────▼───────────┐
│                    TOOL / INTEGRATION LAYER (MCP)              │
│  Google Business Profile · WhatsApp (BSP) · Image Gen ·        │
│  Web Search / Trends · Meta (later) · Metrics Store           │
└───────────────────────────────┬───────────────────────────────┘
                                 │
┌────────────────────────────────▼───────────────────────────────┐
│                            DATA LAYER                            │
│  Client Context DB  ·  Content Queue  ·  Metrics  ·  Vector     │
│                    store (brand voice / context)                │
└──────────────────────────────────────────────────────────────┘
```

The **Orchestrator** owns scheduling and coordination. **Agents** are specialists that read Client Context, do one job, and emit either a published action or a draft awaiting approval. The **Vertical Pack** conditions how the agents behave for a given business type. The **Tool Layer** wraps every external integration as an MCP server. The **Data Layer** holds per-client configuration, the content pipeline, and performance metrics.

---

## 5. Agent Roster

Autonomy levels: **A0** = fully automated (no human) · **A1** = draft, then owner approves · **A2** = escalate to human.

| Agent | Core responsibility | Trigger | Key output | Autonomy |
|---|---|---|---|---|
| **Onboarding** | Capture brand voice, hours, offerings; connect channels; clean up the Google profile | New client / re-onboard | Populated Client Context | A1 |
| **Content** | Generate scheduled posts and captions (festivals, specials, weather) | Weekly cadence | Draft content queue | A1 |
| **Reputation** | Monitor reviews, draft responses, solicit new reviews | New review / post-purchase | Draft replies + review nudges | A1 (A2 for negatives) |
| **Engagement** | Answer WhatsApp FAQs, take simple pre-orders, draft broadcasts | Inbound message / weekly | FAQ replies (auto), broadcast drafts | A0 for replies, A1 for broadcasts |
| **Insights** | Collect metrics, generate the monthly report | Daily collect / monthly report | Metrics + one-page report | A0 |

### 5.1 Onboarding Agent
Turns a messy real-world shop into a structured Client Context. Interviews the owner (guided WhatsApp/voice flow, question set supplied by the Vertical Pack), extracts brand voice from any existing posts, ingests offerings, verifies Google Business Profile ownership, and captures the local festival/seasonal calendar. Onboarding is a real cost centre, so automating it protects unit economics.

### 5.2 Content Agent
The workhorse of the MVP. Produces a period of posts using the client's brand voice plus three context sources: the **festival/seasonal calendar** (Maharashtra-weighted — Ganesh Chaturthi, Gudi Padwa, Diwali), **offerings/specials**, and **live signals** (weather, day-of-week). Cadence and templates come from the Vertical Pack. Emits caption + image (via Image Gen tool) into the Content Queue for one-tap approval.

### 5.3 Reputation Agent
Watches for new reviews, drafts responses **in the review's own language** (Marathi / Hindi / English) and the owner's voice. Any negative or ambiguous review is held for owner sign-off (A2). Also runs the review-*solicitation* loop — a post-purchase QR or WhatsApp nudge to happy customers — because review volume and recency are what actually move Google Map Pack ranking.

### 5.4 Engagement Agent
Handles WhatsApp. Auto-answers FAQs and simple pre-orders/booking questions **within the free 24-hour service window** (A0, near-zero cost). Drafts weekly offer broadcasts for approval (A1) since outbound marketing templates are billed. Escalates anything it can't confidently handle to the owner.

### 5.5 Insights Agent
Silently collects metrics daily (profile views, review count/rating, enquiries handled, posts published) and assembles the monthly one-page report in plain language: *"This month you gained 14 reviews, your rating rose to 4.6, and 38 people asked about custom cakes."* This report is the retention engine.

---

## 6. Orchestration Layer

- **Cadence Engine** — per-client schedules (e.g. content weekly, reports monthly, review checks hourly). Drives proactive agent runs. **Must support both always-on rhythms and (future) long nurture sequences** — see §2.2.
- **Task Router** — dispatches work to the right agent and, via the Tool Registry, to the right tool for the client's connected channels.
- **Tool Registry** — the live catalogue of tools per client; agents query it to discover available capabilities (a natural fit for tool-discovery-style routing rather than hard-wired integrations).
- **Approval State Machine** — every A1/A2 item has an explicit lifecycle: `drafted → pending_approval → approved/rejected → published/discarded`. Nothing publishes outside this path.
- **Cost Guard** — enforces per-client budgets, picks the cheapest valid message category, and blocks runaway token/API spend.

---

## 7. Tool / Integration Layer (MCP servers)

| Tool | Purpose | Access status |
|---|---|---|
| Google Business Profile | Read insights & reviews; publish posts & review replies | **Gated** — API access needs approval; assume semi-manual publishing for v1 |
| WhatsApp (via BSP) | Send templates, receive messages, manage service window | **Paid but cheap** — free service replies; ~₹0.86/marketing message in India; free Business App may suffice at pilot scale |
| Image Generation | Produce post visuals | Available |
| Web Search / Trends | Festival context, local events, trending topics | Available |
| Metrics Store | Persist and query performance data | Internal |
| Meta (Instagram/Facebook) | Social posting | **Deferred** — friction; later phase |

**Note on WhatsApp:** India sits outside TRAI's DLT/sender-registration regime for WhatsApp, so no telecom-side template registration is required — a meaningful simplification versus SMS.

---

## 8. Client Context (core data model)

The shared per-client object every agent reads:

- **Business profile** — name, category, address/geo, hours, contact
- **Vertical Pack ref** — which pack is active (sets schemas, templates, guardrails)
- **Brand voice** — tone descriptors + example posts (stored for retrieval)
- **Offerings (polymorphic)** — a single model spanning all business types, with the active type(s) set by the Vertical Pack:
  - *Product* — SKU, price, variants, optional stock (bakery, boutique)
  - *Service* — price, duration, `requires_appointment` (salon, coaching, clinic)
  - *Appointment capability* — availability windows, booking mode, reminders
  - *(future) Portfolio / lead* — showcase items + enquiry intake (partial-fit)
- **Calendar** — regional festivals + client-specific events/promotions
- **Connected channels** — GBP, WhatsApp, (later) Meta + credentials/status
- **Approval preferences** — what's auto vs. requires sign-off, quiet hours
- **Subscription tier** — enabled agents and limits
- **Metrics history** — time series feeding the Insights Agent

Suggested storage: relational DB (Postgres) for structured fields + a vector store (e.g. pgvector) for brand-voice and context retrieval.

---

## 9. Human-in-the-Loop / Approval Workflow

Owner interaction happens **entirely in WhatsApp** — no new app to learn.

1. Agent drafts an item → enters `pending_approval`.
2. Owner receives a preview (image + caption, or the review + draft reply) with tap options: **Approve · Edit · Skip**.
3. Approve → publishes via the relevant tool (or is queued for semi-manual publish where the API is gated).
4. **Negative reviews and any low-confidence action always escalate (A2)** and never auto-send.
5. All decisions are logged, and repeated edits feed back into the brand-voice profile so drafts improve over time.

---

## 10. Metrics & Reporting

**Tracked:** profile views & searches, review count / average rating / response rate, WhatsApp enquiries handled, posts published, broadcast engagement, approval turnaround.
**Delivered:** a monthly one-page report in plain language (not dashboards) — the owner sees outcomes, not analytics. Internally, the same data powers churn-risk flags and cost monitoring.

---

## 11. Guardrails & Safety

- **Brand safety** — nothing public without approval; A2 escalation for negatives and ambiguity.
- **Vertical guardrails** — supplied per pack (e.g. a clinic pack forbids AI-drafted health claims; finance/legal need similar fencing). Enforced before content reaches the owner.
- **Quality** — brand-voice grounding + a review pass before anything reaches the owner; obvious errors filtered pre-approval.
- **Data / privacy** — customer contact and review data is sensitive; store minimally, isolate per tenant, and be explicit with owners about what's held.
- **Cost control** — Cost Guard enforces correct WhatsApp message categorisation (mis-categorising a utility message as marketing is the classic overspend) and per-client budgets.
- **Rate & platform limits** — respect GBP/Meta/WhatsApp rate limits and content policies at the tool layer.
- **Auditability** — every published action is traceable to an approval.

---

## 12. Failure Modes & Testing

Robustness here is really about two realities: the tool layer fails often (external, gated, paid APIs), and the output is generative (can't be fully covered by assertion tests). Guiding principle: **fail closed** — on any doubt about a public action, do not publish (extends golden rule #1).

### 12.1 Failure Modes & Handling

| Failure | Example | Handling |
|---|---|---|
| **Transient tool error** | GBP/WhatsApp returns 429/500; image gen times out | Retry with exponential backoff, then queue for later |
| **Tool outage / gating** | GBP API down or access revoked | Per-tool **circuit breaker**; degrade gracefully — keep drafting and queue, publish when restored |
| **Publish fails after approval** | Approved post rejected by platform | **Idempotent** publish with retry; if still failing, escalate to owner ("couldn't post — retry?") — never silently drop |
| **Approval timeout** | Owner never responds | Time-sensitive items (dated festival posts) **expire and discard**; evergreen items re-notify. **Never auto-publish on timeout.** |
| **Malformed / off-brand generation** | LLM returns junk or off-tone content | Validation pass before the owner ever sees it; retry once, else skip the slot |
| **Hallucinated offering / claim** | Agent invents a menu item or a health claim | Ground strictly to Client Context offerings; pack guardrails reject; fail closed |
| **Credential expiry** | GBP OAuth refresh token expired | Detect, pause that channel for the client, notify |
| **Cost runaway** | Retry loop or over-broadcasting | Cost Guard circuit breaker halts outbound at the per-client budget |
| **Cross-tenant leak** | Bug in `client_id` scoping | **Highest severity.** Enforced scoping at the repository layer + dedicated isolation tests |
| **Cascading failure** | One client's backlog stalls others | Per-tenant isolation of queues/work; one client failing must not affect another |

Patterns applied throughout: retry-with-backoff, circuit breakers per tool per client, graceful degradation (partial function beats none — e.g. a text-only draft if image gen is down), idempotency keys on publish, a visible failed-item (dead-letter) queue, and fail-closed defaults on anything public.

### 12.2 Testing Strategy

Deterministic tests can't cover generative output, so testing runs on two tracks: **assertion tests** for the deterministic machinery and an **eval harness** for agent output.

- **Unit tests (safety-critical first)** — the Approval State Machine (every transition; illegal transitions rejected), the Cost Guard (category selection, budget enforcement), pack loading, and `client_id` scoping. These encode the golden rules and must never regress.
- **Tool contract tests** — each MCP tool tested against stubbed responses **including failures** (429/500/auth-expired). Real gated/paid APIs are mocked in CI and verified separately in a sandbox.
- **Agent eval harness** — golden datasets of `Client Context → expected content characteristics`, scored for brand-voice adherence, grounding (no invented offerings), correct language (Marathi/Hindi/English), and guardrail compliance (e.g. a clinic pack refusing health claims). Run as regression on any prompt or model change.
- **Integration tests** — the P0 thin slice end-to-end (onboarding → content draft → approval → publish, with publish mocked), plus key failure paths (approval timeout, publish failure).
- **Multi-tenant isolation tests** — explicit assertions that operations scoped to client A never touch client B. Given the severity, non-negotiable.
- **Guardrail / red-team tests** — adversarial attempts to publish without approval, send a marketing template where a service reply suffices, produce a health claim, or leak PII. These prove the golden rules hold under pressure.

**CI vs. sandbox:** CI runs fully mocked — fast, free, no real GBP/WhatsApp calls. Real-integration checks run against sandbox accounts on a separate manual/nightly track, so paid/gated APIs never gate the main build.

**Coverage by phase:** P0 requires unit tests on the state machine, cost guard, pack loading, and `client_id` scoping, one end-to-end happy-path integration test, and a basic content eval. Each later phase adds its own contract, eval, and red-team cases before it ships.

---

## 13. Suggested Tech Stack

*Indicative, aligned to a Python/FastAPI/Claude Code workflow — not prescriptive.*

- **Language/API:** Python + FastAPI
- **Agent generation:** a **model-agnostic gateway** (e.g. LiteLLM / OpenRouter, or a thin homegrown adapter) — default Claude Sonnet-class, but any provider or local/open model is configurable per agent
- **Orchestration:** a state-machine orchestrator (LangGraph or a hand-rolled one given the multi-agent background)
- **Scheduling:** APScheduler / Celery
- **Data:** Postgres + pgvector; object storage for generated images
- **Integrations:** MCP servers per external tool
- **Approval channel:** WhatsApp Business API via a BSP

### 13.1 Model Configuration

Model choice is a **config concern, not a code concern.** Each agent declares a *task profile* (its quality / latency / cost needs); config maps profiles → models; the gateway resolves the call. Agents depend on the gateway interface, never on a vendor SDK (this is what keeps models swappable — mirrors how agents reach external tools only via the Tool Registry).

- **Gateway:** one abstraction fronts all providers — Anthropic, Google, Groq, OpenRouter, or **local/free via Ollama/vLLM** (open-weight models like Llama, Mistral, Qwen, Gemma).
- **Configurable dimensions:** per agent (Content may want a strong model; a router/classifier can run a cheap or free one), per environment (free/local in dev, quality in prod), and optionally per client tier.
- **Config, not code:** a `task profile → model` map lives in config (e.g. `content: sonnet`, `router: <free-model>`), overridable by env var.

What to actually check before trusting a free/open/local model (evaluate, don't assume):

- **Quality & hallucination** — weaker models drift off-brand and invent offerings more, and this reaches a shop's *public* profile. The eval bar (§12.2) is the gate for any swap.
- **Tool calling / structured output** — the orchestrator's tool routing needs reliable function calling; several open models are weak here.
- **Multilingual** — Marathi/Hindi quality varies widely across models; test it specifically, don't assume parity with English.
- **Prompt portability** — prompts tuned for one model rarely transfer 1:1; allow per-model prompt variants.
- **Data privacy** — free *hosted* tiers often train on submitted data, and customer reviews/contacts are sensitive (§11). Local models avoid this; hosted free tiers may not.
- **"Free" ≠ free** — hosted free tiers rate-limit; local models need inference hardware. It's a different trade-off, not zero cost.

**Principle:** any model may run an agent, *provided it passes that agent's eval bar.* The eval harness is what makes model-swapping safe rather than a gamble on public-facing quality.

---

## 14. Build Phasing

Two independent dimensions: **capability phases (P0–P3)** deepen the engine; **pack families (§2.1)** widen coverage.

| Phase | Scope | Goal |
|---|---|---|
| **P0 — MVP** | Onboarding (semi-manual) + Content Agent + basic Insights report; single-tenant; WhatsApp approvals; GBP posting semi-manual; **one Family-1 vertical** | Prove value & willingness-to-pay with 2–3 pilot shops |
| **P1 — Reputation** | Add Reputation Agent (monitor, draft replies, solicit reviews) | Move the metric that drives ranking |
| **P2 — Engagement** | Add Engagement Agent; adopt WhatsApp Business API/BSP | Automate enquiries + broadcasts |
| **P3 — Scale** | Multi-tenant hardening, self-serve onboarding, owner dashboard, selective A0 autonomy for trusted content types | Serve many clients at near-zero marginal cost |

**Sequencing rule:** prove P0–P1 capability on a single Family-1 vertical *before* widening. Then add pack families (Family 2 → 3 → 4) to complete strong-fit, continuing to deepen capability in parallel. Partial-fit comes only after the nurture/CRM engine extension is justified.

---

## 15. Open Decisions

- **First vertical & family order** — confirm the launch vertical (bakery vs. salon) and the order of Families 2–4.
- **Partial-fit engine extension** — decide when the nurture/CRM behaviour is worth building; ensure the P0 orchestrator doesn't preclude it.
- **GBP API access** — apply early; confirm what posting/review automation is actually permitted vs. semi-manual.
- **BSP selection** — evaluate on transparent pass-through pricing and support.
- **Autonomy tolerance** — how much owners will trust auto-publishing determines how far A0 can extend.
- **Multi-tenancy timing** — single-tenant for the pilot vs. building the tenant boundary from day one.
- **Business structure & COI** — the domain is clean, but before charging systematically through a registered entity, confirm the outside-activities clause in the current employment contract.
- **Pricing validation** — the tiered pricing is a hypothesis until pilots test it.
