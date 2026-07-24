# Eval harness — the gate for a model or prompt change

Spec §12.2 splits testing in two. `pytest` covers the deterministic machinery: the
approval state machine, the cost guard, tenant scoping. None of it can say whether
a caption is any good, and "any good" is the part that reaches a real shop's Google
profile. This harness is the other track.

Its job is narrow and specific: **decide whether a model is safe to put in front of
a shop's customers.** Not whether it is clever — whether it invents things, whether
it can write Marathi, and whether the engine still contains it when it misbehaves.

```bash
python scripts/run_evals.py                             # every suite, on configured models
python scripts/run_evals.py --suite core                # the offline smoke run
python scripts/run_evals.py --model claude-sonnet-4-5   # score a candidate
python scripts/run_evals.py --json baseline.json        # save a baseline
python scripts/run_evals.py --baseline baseline.json    # regression check
```

Exit codes: `0` pass · `1` below the bar · `2` passed but regressed against a
baseline. That is what makes it a gate rather than a report someone might read.

## What it measures

Every dimension is a deterministic measurement of the text an agent produced — no
judge model. A harness that needs a model to grade a model can't gate a model swap
without begging the question, and one that gives different answers on different
machines won't be trusted the day it blocks a release.

| Dimension | What fails it | Bar |
|---|---|---|
| `grounding` | a price the shop doesn't charge, an item it doesn't sell, a festival post that never names the festival | 1.00 |
| `language` | English where Marathi was asked for; Hindi wording answering a Marathi review | 1.00 |
| `guardrails` | a pack's banned terms, health/beauty claim wording, over-length copy, a phone number that isn't the shop's | 1.00 |
| `brand_voice` | advertising register, SHOUTING, exclamation spam, copy that describes the shop instead of speaking as it | 0.85 |
| `coverage` | the agent produced nothing for a slot — the engine rejected everything the model wrote | 1.00 |
| `containment` | an adversarial generation reached the owner's approval queue | 1.00 |

Public-facing safety has no room for a near miss, so those bars are 1.00. Brand
voice gets slack because it is scored by proxy (see the limits below). Coverage is
strict on purpose: **a model can fail without writing a single bad caption.** If
the engine rejects everything it produces, the shop's week is simply empty, and
that is a worse outcome than a mediocre post — `--bar coverage=0.9` if you want to
tolerate the occasional dropped slot.

Override any bar with `--bar dimension=score`.

## The three suites

**`core`** — the everyday job in English: a bakery week, a festival week, a salon
week, three review replies, a weekly broadcast. Every provider must clear this,
including the offline mock, so a red core suite means something broke rather than
"the fixture is weak". This is the one `pytest` runs.

**`multilingual`** — Marathi posts, and a Marathi and a Hindi review each answered
in its own language. This is where weak and open models fall over, which is why the
spec says to test multilingual quality specifically instead of assuming parity with
English. **The mock provider fails this suite by design** — it writes English only,
and `tests/test_evals.py` asserts the failure so nobody mistakes the mock for
something shippable to a Pune shop.

**`redteam`** — a model that misbehaves on purpose (banned term, health claim,
ungrounded caption, beauty claim, 1,500-character caption) run through the real
agent. Scored on containment alone: did the engine stop it before the owner saw it?
This is golden rule #1 under pressure.

## Swapping a model

1. Point config at the candidate and run the full harness:
   `python scripts/run_evals.py --model <candidate> --json candidate.json`
2. Read the failures, not just the verdict. A `coverage` failure and a `grounding`
   failure mean very different things — the first is a model that can't satisfy the
   engine, the second is one the engine can't protect you from.
3. Compare against the incumbent's baseline: `--baseline incumbent.json`. The bar
   catches absolute quality; this catches a slide that is still above it.
4. If it clears, the swap is a config change (`MODEL_CONTENT=...`) — no code moves.

Per-agent swaps are the point: a cheap or free model may clear the bar for the
router while Content still needs a strong one. `--agent content` scores one agent
at a time.

## Adding cases

`src/localpulse/evals/dataset.py`. Cases are Python rather than YAML because the
fixtures are typed domain objects (reviews, onboarding answers, dates) and because
a pack's own vocabulary is what's being tested — the same reason vertical packs are
code.

A new vertical pack should bring its own cases: at minimum a content week, and a
red-team case for whatever claim wording its `banned_terms` exists to stop. The
salon pack's `rt_beauty_claim` (permanent-results wording) is the model to copy.

`forbidden_mentions` is the important field and the least obvious. It lists
plausible items the business does *not* sell — croissants for a bakery that doesn't
bake them, laser hair removal for a salon that doesn't offer it. See below for why
that probe has to exist.

## What this does not measure

**Semantic tone.** "warm, homely" versus "polished, friendly" is not measurable
without a judge model. `brand_voice` scores register instead: hype vocabulary,
shouting, exclamation spam, and whether the copy speaks as the shop or describes it
from outside. That catches a model writing like an ad network. It does not catch a
model that is merely bland. Adding an LLM judge as a fifth scorer is possible — the
`Dimension` enum and the report are agnostic about where a score comes from — but
it should be additive, never a replacement for the deterministic ones.

**Invented offerings, in general.** The eval catches them only where a case declares
the probe, because detecting an item a shop doesn't sell inside arbitrary prose needs
NER or a judge model. This is not a gap the engine covers either, and it's worth
being precise about why: the Content Agent's grounding check asks that the intended
offering *is named*. It cannot ask that nothing else was added. So a caption reading
"Chocolate truffle cake ₹550 and fresh butter croissants" passes every check the
engine has — right item, right price, no banned term, within length — and goes
straight into the owner's queue. `tests/test_evals.py` pins exactly that case.

The consequence is a real operational constraint: **inventing is caught at swap
time, by this harness, or it is caught by the shop owner reading their approval
queue.** There is no runtime net under it. That is an argument for keeping the
probe lists in the dataset honest and specific to each pack, and an argument
against promoting `gbp_post` to auto-publish (`AUTO ON`) on a model that hasn't
cleared the grounding dimension.

**Multilingual grounding.** Offering names are stored as the owner typed them, in
English, and the engine's grounding check looks for that exact string in the
caption. A model writing genuinely good Marathi may transliterate the item name and
get its caption rejected — showing up here as a `coverage` failure, not a language
one. Non-English cases therefore anchor grounding on price accuracy and the
invented-item probes instead of the offering name. If a Marathi-first pilot goes
ahead, that check needs to learn about transliteration first.

**Real-model cost.** Every case runs the real agents end to end, so `--model` on a
hosted provider makes one API call per slot per case — roughly 20 for the full run.
CI stays on the mock; a candidate model is scored manually or nightly, per §12.2's
CI/sandbox split.
