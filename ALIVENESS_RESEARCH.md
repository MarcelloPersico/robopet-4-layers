# Making a desk pet feel *alive*: state of the art → Jarvis 1.0

> Deep-research report, 2026-06-02. Question: *how do you make a robotic desk pet
> feel genuinely "alive" — capable of learning, holding a persistent and
> ever-changing memory, running an internal monologue (private thought), and
> acting in a lifelike way driven by what it is thinking — rather than behaving
> like a reactive answering machine?*
>
> Method: 5 search angles, 22 sources fetched, 105 claims extracted, top 25
> adversarially fact-checked (3-vote, 2/3-to-kill) → 23 confirmed, 2 refuted →
> synthesized to 12 verified findings. Confidence levels and refuted claims are
> preserved below. All Jarvis code line numbers were read from the working tree
> on 2026-06-02 — verify against the current commit before editing.

## Bottom line

The literature converges on one answer: a machine reads as *alive* rather than
reactive when it runs a **layered cognitive architecture doing four things at
once** —

1. keeps a **persistent natural-language memory stream**,
2. periodically **synthesizes that stream into higher-level reflections**,
3. runs a **private deliberation loop decoupled from a fast reflexive path**, and
4. carries a **persistent affective/needs state that biases behavior**.

The striking finding: **Jarvis 1.0 already implements 3 of these 4 faithfully** —
`memory.py` + `cognition.py` + `mood.py` is a near-textbook local port of
Stanford's *Generative Agents*. The single highest-leverage gap is **#4's
"needs/drives" half and a missing planning/intention layer** — and an ablation
study gives direct, quantified evidence that adding planning is what most
increases lifelike, non-reactive behavior.

---

## Part A — State-of-the-art survey

### The canonical architecture: Generative Agents (Park et al., 2023)

This is the spine of the whole field, and it *is* your design's lineage. Verified
verbatim from the paper:

- **Memory stream** = "a list of memory objects, where each object contains a
  natural language description, a creation timestamp, and a most recent access
  timestamp." The system "synthesize[s] those memories over time into
  higher-level reflections, and retrieve[s] them dynamically to plan behavior."
  *(high confidence, 3-0)*
- **Retrieval scoring** = weighted sum of **recency × importance × relevance**,
  each min-max normalized, all weights = 1. Recency is exponential decay (factor
  0.995/hr); importance is an LLM-rated 1–10 "poignancy" assigned *at creation*;
  relevance is embedding cosine similarity. *(high, 3-0)*
- **Reflection trigger**: generated "when the sum of the importance scores for the
  latest events … exceeds a threshold (150 in our implementation)" (~2–3/day).
  Reflections are stored as memories and retrieved alongside observations, forming
  **trees** of increasingly abstract inference. *(high, 3-0)*
- **The ablation that matters most**: full architecture scored TrueSkill μ=**29.89**,
  vs 26.88 (no reflection), 25.64 (no reflection + no planning), and **21.21 fully
  ablated — *below* the 22.95 human-crowdworker baseline.** The paper states
  "observation, planning, and reflection — each contribute critically to
  believability." Crucially, memory **+ planning** (26.88) beat memory **alone**
  (25.64), isolating planning's distinct contribution. *(high, 3-0)* → **direct
  evidence that adding planning to a memory+reflection loop measurably increases
  believability.**

Sources: <https://arxiv.org/abs/2304.03442>,
<https://ar5iv.labs.arxiv.org/html/2304.03442>,
<https://dl.acm.org/doi/fullHtml/10.1145/3586183.3606763>

### Lifelong learning *without* fine-tuning — Voyager (Wang et al., NeurIPS 2023)

Validates the no-cloud / no-fine-tuning constraint as a *principle*: accumulate
capability in an **external, ever-growing store** queried against a **frozen**
LLM. Voyager "bypasses the need for model parameter fine-tuning" using "an
ever-growing skill library of executable code … temporally extended,
interpretable, and compositional, which compounds the agent's abilities rapidly
and alleviates catastrophic forgetting." *(high on the mechanism, 3-0; 2-1 on
transfer)*

**Caveat (the dissent):** Voyager ran on cloud GPT-4 in Minecraft, which gives
instant error feedback and clean resets a real desk pet lacks. So this validates
the *architecture*, not a proven real-world local result.

Source: <https://arxiv.org/abs/2305.16291>

### Timing & attention — LIDA / Global Workspace Theory (Baars & Franklin, 2009)

Grounds the **tiered fast-reflex-vs-slow-deliberation** split you already have:

- A LIDA cognitive cycle is **~300 ms** ("consciously mediated actions are
  selected roughly five to ten times every second"), while "alarm mechanisms seem
  to operate in the sub-50 ms range." *(high, 3-0)* — the Teensy 1 kHz reflex loop
  + ~300 ms ack budget + 30 s background cognition tick maps onto this almost
  exactly.
- Attention is a **winner-take-all competition**: per moment one coalition is "the
  most salient, the most relevant, the most important, the most urgent" and is
  broadcast — and **the action-selection phase *is* the learning phase**, so what
  gets written to memory is gated by what wins attention, *not* indiscriminate
  logging. *(high on structure, 3-0)* This validates writing thoughts during a
  deliberate tick and *not* storing raw vision.

Source: <https://cse.buffalo.edu/~rapaport/Papers/Papers.by.Others/baars-franklin09.pdf>

### Richer memory mechanics — SOAR / ACT-R

The "classical" cognitive architectures add concrete retrieval machinery the
current scheme doesn't have yet:

- **Base-level activation**: declarative memory is gated by metadata encoding
  **recency *and* frequency of use** to estimate future usefulness — maintained
  *automatically* by the architecture, not written by the agent. *(high, 3-0)*
- ACT-R adds **spreading activation** (from current working-memory context to
  related memories) and **retrieval inhibition** ("chunks recently retrieved …
  can be inhibited to avoid repeated retrievals"). *(high, 3-0)* → retrieval
  inhibition is a direct anti-repetition mechanism for spontaneity.
- **SOAR splits declarative memory** into **semantic** (timeless facts) and
  **episodic** ("snapshots of the complete top state of WM … stored
  automatically"). *(high, 3-0)* This maps cleanly onto an episodic dialogue/thought
  stream vs. semantic `resolved_knowledge`.

Sources:
<https://advancesincognitivesystems.github.io/acs2021/data/ACS-21_paper_6.pdf>,
<https://soar.eecs.umich.edu/soar_manual/07_EpisodicMemory/>

### Persistent memory store & consolidation — Mem0 (2025), FadeMem (Jan 2026)

- **Why a store, not a bigger context window**: fixed context windows "pose
  fundamental challenges for maintaining consistency over prolonged multi-session
  dialogues." *(high, 3-0)* — validates SQLite-stream-over-prompt-stuffing.
- **Importance-weighted forgetting** (FadeMem): adaptive exponential decay
  `v(t)=v(0)·exp(−λ(t−τ)^β)` with rate `λ=λ_base·exp(−μ·I)` so important memories
  decay slower, plus **access-reinforcement** `v(t+)=v(t)+Δv·(1−v(t))`.
  *(medium, single recent preprint)*

> ⚠️ **Two claims were REFUTED** (don't rely on them): (1) that Mem0's specific
> extract→consolidate→retrieve pipeline is *the* production standard (1-2 — the
> general motivation survives, the specific pipeline does not); (2) that selective
> forgetting is a net *performance benefit* with FadeMem's "45% storage cut /
> superior reasoning" headline (1-2 — only the decay **mechanism** survived). So if
> you add forgetting, justify it by **storage/latency**, not accuracy.

Sources: <https://arxiv.org/abs/2504.19413>, <https://arxiv.org/pdf/2601.18642>

### Persistent affect & "wanting" — AIBO's needs-based architecture (ACE 2005)

The affective insight that most directly breaks the answering-machine pattern:
AIBO's lifelike behavior comes from **nPME — needs(n), Personality, Mood,
Emotions** — where "**needs play the most important role and influence the
behavior … in every situation.**" PAD-style mood is the standard *substrate*, but
**needs/drives are what make affect proactive rather than reactive.** *(high, 3-0)*

Source: <https://www.academia.edu/819875/AIBO_as_a_needs_based_companion_dog>

---

## Part B — Mapped onto Jarvis 1.0

### ✅ What the design already does well (validated against SOTA)

| Component | SOTA principle it satisfies | Verdict |
|---|---|---|
| `memory.py` — dialogue/resolution/**thought/reflection** rows, ts + importance + embedding | Generative-Agents memory stream + distinct higher-abstraction reflection class | Faithful port |
| `memory.py retrieve()` — `recency × importance × relevance`, min-max normalized, weights 1.0 | Exact three-factor recall of Park et al. | Faithful (params adapted: 6 h half-life vs 0.995/hr) |
| `cognition.py _maybe_reflect()` — accumulated-importance vs `reflect_threshold` | Reflection-on-importance-threshold (150 in paper; 12.0 is a local scaling) | **Exact mechanism match** |
| Teensy 1 kHz reflex + ~300 ms ack vs. 30 s background `think()` tick that yields to live turns | LIDA tiered timing (~300 ms conscious cycle, sub-50 ms reflex); deliberation off the latency-critical path | Validated, not a gap |
| Not storing raw vision; writing during the deliberate tick | GWT "learning is attention-gated," not indiscriminate logging | Validated |
| `mood.py` — PAD decaying to a circadian baseline → OLED expression | PAD affect substrate (AIBO's M & E layers) | Solid substrate |
| SQLite stream over prompt-stuffing; `resolved_knowledge` (semantic) vs dialogue stream (episodic) | Mem0 multi-session-consistency motivation; SOAR semantic/episodic split | Architecturally correct |

The core loop is **state-of-the-art-faithful and runs entirely locally.** The gaps
below are additive, not corrective.

### ⚠️ Where it falls short

1. **No planning / intention layer.** `think()` produces a one-off private thought
   with no daily agenda or persisted intentions. The *single highest-leverage gap*
   — the ablation puts a number on it.
2. **Heuristic importance-by-kind.** The `IMPORTANCE` dict (`memory.py` ~lines
   56-61) is self-described as "a cheap stand-in for the Generative-Agents LLM
   importance rating" — so all dialogue rows get the same weight regardless of
   actual salience.
3. **No forgetting/consolidation curve.** `evict()` (~lines 194-210) is a crude
   oldest+least-important hard delete; the reserved `last_access` field (~line 41)
   is unused, so **frequency-of-use, access-reinforcement, spreading activation,
   and retrieval inhibition are all absent.**
4. **No skill/preference/habit learning that reshapes behavior across sessions.**
   `resolved_knowledge` + recent-answers buffer is a primitive *fact* store;
   nothing accumulates *behavioral* learning (routines, preferences).
5. **No needs/drives layer.** Mood exists, but there's nothing the pet *wants*
   (attention, novelty, rest, interaction) to proactively bias what it thinks about
   or whether it speaks. This is what keeps it feeling reactive.
6. **Single-level reflection.** Reflection runs on raw rows but not *on
   reflections* — abstraction depth is capped at one level (the paper builds trees).

### 🎯 Ranked, concrete upgrades

Ordered by **(believability leverage ÷ implementation cost)**, all respecting
fully-local / tight-VRAM / ~300 ms / zero-paid-call constraints:

**1. Add a minimal planning/intention layer.** *(highest leverage — the ablation
proves it)*
Don't build a heavyweight planner. Persist a small set of **self-set intentions /
a one-line daily agenda** that `think()` writes and retrieves into its own prompt
("I want to check whether the human is back at the desk this afternoon"). Store
intentions as a new memory `kind`, retrieve them alongside observations, and let
reflection update them. *Cost: one new row type + a few lines in the think() prompt
builder. Risk: low.*

**2. Add an AIBO-style needs/drives vector.** *(most directly breaks the
"answering machine" feel)*
A tiny vector — e.g. `attention`, `novelty`, `rest`, `interaction` — that
**decays/replenishes over time**, nudges `mood.py`'s PAD target, and gates the
speak-probability in `cognition.py`. When `novelty` is starved, bias what the pet
*thinks about* toward exploration; when `interaction` is high after a long
silence, raise spontaneous-speech probability. *Cost: ~a small module + hooks into
mood target and the speak gate. Risk: medium — must tune so it's "never inert but
not a nag" (open question below).*

**3. Replace heuristic importance with a lightweight salience signal.** *(improves
every downstream retrieval/reflection)*
Two options. Cheapest local: derive importance from **utterance length + sentiment
+ novelty-vs-recent-embeddings** (bge-small embeddings already computed, so novelty
is nearly free). More faithful: one extra small-model call for a 1–10 poignancy
rating per *significant* write only (not every frame). *Cost: low-to-moderate.
Risk: low. (See open question on the cost/benefit tradeoff.)*

**4. Add retrieval inhibition + frequency to recall.** *(cheap anti-repetition →
better spontaneity)*
Finally use the reserved `last_access` field: down-weight recently-retrieved
memories (ACT-R retrieval inhibition) and fold **frequency-of-use into base-level
activation**. This directly cuts the desk pet's tendency to surface the same
thought repeatedly. *Cost: a few lines in `retrieve()`. Risk: very low — a strict
improvement to perceived liveliness.*

**5. Add a graded forgetting/consolidation pass.** *(scales the ever-growing
stream gracefully)*
Replace the hard `evict()` with a **FadeMem-style retention score** (`λ` modulated
by importance, reinforced on access). Add a periodic **consolidation** step that
dedups/merges near-duplicate salient facts (the surviving piece of the Mem0
motivation). **Justify it by storage/latency, not accuracy** (per the refuted
claim). *Cost: moderate. Risk: low.*

**6. Build a preference/habit/routine library (local Voyager analog).** *(true
cross-session learning)*
A curated **semantic table** written *by reflection* and retrieved as memory —
e.g. "human usually arrives ~9am," "dislikes being addressed by name." This is the
realistic desk-pet substitute for Voyager's executable skills (no clean success
signal, so learn *preferences*, not code). Gate a learned preference behind a
validation step before it reshapes behavior. *Cost: moderate. Risk: medium —
validation policy is an open question.*

**7. Let reflection reflect on reflections (multi-level trees).** *(deepens the
sense of an inner life — lower priority)*
Allow `_reflect()` to take prior reflections as input, building the abstraction
tree Park et al. describe. *Cost: low. Risk: low. Do after 1–6.*

**8. (Optional polish) Fast mood→eyes reflex before the LLM tick completes.** Fire
an OLED expression change off the fast path on salient input, ahead of the slow
cognition tick — pure LIDA sub-50 ms alarm behavior. *Cost: low.*

---

## Caveats & open questions

**Source-access caveats:** the LIDA/GWT PDF, ACS-21 PDF, and AIBO Academia.edu page
returned 403/binary to direct fetch, so several quotes were corroborated via
independent search extracts and the official SOAR manual / mirrors rather than full
primary-text renders — the **quotes verified verbatim** but the primary PDFs
weren't rendered in-environment. The long-term-memory production literature (Mem0
2025, FadeMem Jan 2026) is recent and partly self-reported; the foundational
sources (Generative Agents, Voyager, LIDA, SOAR/ACT-R) are stable primaries. Code
line numbers were read from the working tree on 2026-06-02 — verify against the
current commit before editing. Param differences (6 h recency half-life /
`reflect_threshold` 12.0 / heuristic importance) are intentional local-scale
adaptations, not bugs.

**Open questions to decide:**

- **Importance rating**: is a per-memory LLM 1–10 rating worth an extra
  small-model call under the VRAM/latency budget, or is the lightweight
  length+sentiment+novelty signal the better local compromise?
- **Planning representation**: what's the *minimal* intention/plan form that
  captures the believability gain without a heavyweight planner, and how does it
  interact with the reflection loop?
- **Preference learning validation**: with no clean success signal, how is a
  learned preference validated before it's allowed to reshape behavior?
- **Needs tuning**: how to tune needs-driven proactivity so the pet is "never
  visibly inert" yet not annoyingly demanding?
- **Forgetting policy**: what retention curve keeps multi-session consistency while
  bounding the SQLite stream — justified by storage/latency, since "forgetting
  improves accuracy" was refuted?

---

## Refuted claims (do not rely on)

- **Mem0's three-stage extract/consolidate/retrieve pipeline is *the* production
  standard for agent long-term memory.** Vote 1-2. The general motivation (a
  persistent store beats a bigger context window) survives; the specific pipeline
  as best practice does not. — <https://arxiv.org/abs/2504.19413>
- **Selective forgetting is a net performance *benefit* (FadeMem's 45%-storage-cut
  / superior-reasoning headline).** Vote 1-2. Only the decay *mechanism* survived
  verification, not the benefit framing. — <https://arxiv.org/pdf/2601.18642>

---

## Primary sources

- **Generative Agents** — Park et al. 2023 — <https://arxiv.org/abs/2304.03442>
  *(the spine; 5 findings)*
- **Voyager** — Wang et al. 2023 — <https://arxiv.org/abs/2305.16291>
  *(lifelong learning without fine-tuning)*
- **LIDA / Global Workspace Theory** — Baars & Franklin 2009 —
  <https://cse.buffalo.edu/~rapaport/Papers/Papers.by.Others/baars-franklin09.pdf>
  *(timing + attention-gated learning)*
- **SOAR / ACT-R** — ACS-21 —
  <https://advancesincognitivesystems.github.io/acs2021/data/ACS-21_paper_6.pdf>
  + <https://soar.eecs.umich.edu/soar_manual/07_EpisodicMemory/>
- **Mem0** — Chhikara et al. 2025 — <https://arxiv.org/abs/2504.19413>
  *(store-over-context motivation; pipeline-as-standard refuted)*
- **FadeMem** — Jan 2026 — <https://arxiv.org/pdf/2601.18642>
  *(decay mechanism; benefit-framing refuted)*
- **AIBO nPME** — ACE 2005 —
  <https://www.academia.edu/819875/AIBO_as_a_needs_based_companion_dog>
  *(needs/drives layer)*

### Additional sources surveyed (not individually cited above)

- Cozmo animation pipeline (GDC) — <https://gdcvault.com/play/1024221/Cozmo-Animation-Pipeline-for-a>
- "Meet Cozmo, the Pixar-inspired robot that feels" (Fast Company) — <https://www.fastcompany.com/3061276/meet-cozmo-the-pixar-inspired-ai-powered-robot-that-feels>
- HRI / animacy-perception set — <https://www.nature.com/articles/s41598-025-17140-9>,
  <https://ir.library.oregonstate.edu/downloads/h415pk244>,
  <https://www.sciencedirect.com/science/article/abs/pii/S107158191600032X>,
  <https://dl.acm.org/doi/10.1145/3757279.3788656>,
  <https://link.springer.com/article/10.1007/s12369-008-0001-3>,
  <https://dl.acm.org/doi/10.1145/3470742>
- Affect / internal-monologue & proactivity set — <https://arxiv.org/abs/2501.00383>,
  <https://arxiv.org/abs/2508.18167>,
  <https://www.frontiersin.org/journals/robotics-and-ai/articles/10.3389/frobt.2016.00021/full>,
  <https://link.springer.com/chapter/10.1007/978-3-642-24603-6_19>

---

*Generated by the deep-research workflow (run `wf_4eace127-918`): 5 angles, 22
sources fetched, 105 claims extracted, 25 adversarially verified (23 confirmed, 2
killed), 12 synthesized findings. 104 agents.*
