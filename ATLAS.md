# ATLAS — a plain-language guide

This document assumes you know nothing about the project. It explains what Atlas
is for, what it takes in, what it produces, how it fits next to MASEF, how it
will be built, and where the work stands today.

If you only read one paragraph:

> When a team of AI agents produces a bad answer, MASEF tells you *the answer is
> bad, and here is the score.* Atlas tells you **which step broke, and which
> later steps were poisoned by that break.** Atlas does this by rebuilding the
> run as a graph and reasoning over it — no second AI opinion, no guessing.

---

## 1. The problem, with a story

Suppose you build a research assistant out of six AI agents:

```
      orchestrator          plans the work
       /        \
researcher_1  researcher_2   gather facts, in parallel
       \        /
        writer               drafts the answer
          |
        critic               reviews the draft
          |
       db_saver              stores the result
```

You run it. It answers. The answer looks fine. You put it through MASEF and get:

```
Artifact Layer
  Consistency   0.71
  Feasibility   0.62      <- low
Agent Layer
  Coherence     0.68
```

Feasibility is low. **Now what?** MASEF has told you the truth, and it is a
useful truth — but it is a verdict on the *output*, not on the *run*. To fix
anything you need to know:

- Which of the six agents introduced the problem?
- Did anything actually fail, or did a step quietly return something wrong?
- Once that happened, which later steps used the bad material?
- Is the critic's approval meaningless because it reviewed already-contaminated text?

Today, answering those questions means a human reading a 4 MB JSON trace by hand.
**Atlas is the tool that answers them mechanically.** That is the entire point of
the project.

---

## 2. What Atlas is

Atlas is a **diagnostic investigation layer** for agentic AI systems. Three
capabilities, in order of importance:

| # | Capability | In plain words |
|---|---|---|
| 1 | **Execution graph reconstruction** | Turn a flat list of recorded steps back into the shape of the run: who called whom, what ran in parallel, what handed work to what. |
| 2 | **Failure propagation / blast radius** | Given a step that went wrong, determine exactly which later steps were affected by it — and which were *not*. |
| 3 | **Deterministic root-cause localization** | Point at the earliest step that explains the failure, and show the evidence for that claim. Same trace in, same answer out, every time. |

Plus two smaller ones: **retry detection** (which attempts failed and were
redone, and what that waste cost) and **cross-run comparison** (this run took a
different path through the graph than the last twelve).

### The word "deterministic" is doing real work here

Most tools in this space ask a large language model *"what do you think went
wrong?"* That is useful, and MASEF already does it. But it is not reproducible —
ask twice, get two answers — and it cannot be tested against a known-correct
answer.

Atlas commits to the opposite: **every conclusion is derived from facts in the
trace, and every conclusion carries the evidence it rests on.** A failure Atlas
cannot justify from the trace is a failure Atlas does not report. This is
enforced in the code, not just in the docs: the `Failure` object literally cannot
be constructed with an empty evidence list.

That constraint is also what makes Atlas *measurable* — you can build traces with
known root causes and score Atlas against them. You cannot meaningfully score a
judge that gives a different answer each run.

---

## 3. How Atlas relates to MASEF

MASEF is the companion framework (a separate repo, already working). The two
split by **question**, not by feature:

> **MASEF** — evaluation, scoring, observability: *how good was this run?*
> **Atlas** — investigation, structural diagnosis, root cause: *where did it
> break, and what did that break?*

| Atlas owns | MASEF owns |
|---|---|
| Span-level execution graph | Quality scores (consistency, feasibility, coherence) |
| Failure propagation / blast radius | Cost analysis and pricing |
| Deterministic root-cause localization | Latency percentiles, critical-path timing |
| Retry detection and retry waste | Reasoning/artifact evaluation, MAST taxonomy |
| Cross-run structural comparison | Framework adapters, the trace format, the dashboard |

**The hard rule of this project:** Atlas never rebuilds something MASEF already
has. Not cost. Not latency percentiles. Not quality metrics. Not an evaluation
report. Not a second dashboard. This is not a style preference — an audit found
that the original Atlas plan would have rebuilt roughly 5,900 lines of working
MASEF code, and the plan was cut down because of it.

Where the two must *agree* on a definition — who owns a given step, what the
critical path is — Atlas adopts MASEF's definition verbatim rather than inventing
a rival one. Two tools that blame different agents for the same step would make
both untrustworthy, and MASEF shipped first.

### Together they produce the sentence you actually want

```
MASEF:  "The final result was weak."

Atlas:  "The result became weak because researcher_1's search returned
         nothing usable, the writer drafted from the remaining partial
         evidence, and the critic reviewed that already-degraded draft —
         so its approval means nothing here."
```

Neither half is that sentence on its own. That combination is the contribution.

---

## 4. What goes in: a trace

Atlas's input is a **MASEF trace** — a JSON file MASEF already knows how to
produce from LangGraph, CrewAI, AutoGen, and the OpenAI Agents SDK. Atlas
deliberately defines **no format of its own**, so it inherits every framework
MASEF supports without writing a single adapter.

A trace has two parts:

**1. A description of the system** — which agents exist, which tools exist, and
which agent is *allowed* to hand off to which. Note "allowed": the trace also
records which of those handoffs actually happened, and the difference between
possible and taken is diagnostic gold.

**2. One or more sessions**, each a list of **spans**.

### What a span is

A span is one recorded step, with a start and an end. Real example, trimmed:

```json
{
  "span_id":        "a1b2c3d4",
  "parent_span_id": "9f8e7d6c",
  "name":           "ChatGoogleGenerativeAI",
  "start_time":     "2026-04-17T00:34:02.246968",
  "end_time":       "2026-04-17T00:34:04.891204",
  "status":         "OK",
  "attributes":     { "openinference.span.kind": "LLM" },
  "openinference":  { "llm_model_name": "gemma-4-26b", "llm_token_count_total": 180 }
}
```

Two fields carry the structure: `span_id` names this step, and `parent_span_id`
names the step that started it. Chain those together and you get the tree of the
run back. **This is the raw material Atlas works from.**

The reference trace this project is validated against is a real run of the
six-agent research team above: **52 spans, ~184 seconds, 56,002 tokens.**

### A word of caution that shapes the whole design

`parent_span_id` means *"this step was started by that step."* It does **not**
mean *"this step used that step's output."* Those are different relationships,
and Atlas needs the second one. §8 explains why this is the project's biggest
technical risk.

---

## 5. What comes out: a diagnosis

The finished output is a **diagnosis** — a small, structured object, renderable
as text, JSON, or a graph view:

```
RUN         e097d42289108b2d          184.3s     52 steps     6 agents

ROOT CAUSE
  researcher_1 · search_web  (step 17)
  confidence: high

  evidence:
    - returned an empty result set while its sibling returned 11 sources
    - retried once, and the retry returned empty as well
    - it is the earliest step consistent with every downstream symptom

BLAST RADIUS                                  4 of 52 steps affected
  researcher_1  ──▶  writer  ──▶  critic  ──▶  db_saver
   FAILED           DEGRADED     DEGRADED      DEGRADED
                    (drafted     (reviewed     (persisted
                     from half    degraded      degraded
                     the evidence)  text)        text)

  NOT affected: researcher_2 and its 16 steps — it ran in parallel and
  consumed nothing from researcher_1.

  each link above is labelled with why it is a link:
    researcher_1 ▸ writer   traversed handoff   (recorded in the trace)
    writer ▸ critic         traversed handoff   (recorded in the trace)

RETRY WASTE
  1 retry, 12.4s, 1,840 tokens, no recovery

MASEF SAYS (imported, not recomputed)
  feasibility 0.62 · consistency 0.71
```

Three things about this output are worth pointing at:

- **"NOT affected" is as important as "affected."** A tool that flags all 52
  steps as suspicious has told you nothing. Correctly *excluding* researcher_2 is
  what makes the report worth reading.
- **Every link says why it is a link.** "Affected via a recorded handoff" is a
  much stronger claim than "affected via a guess based on overlapping text," and
  a diagnostic tool is not allowed to blur the two.
- **MASEF's scores appear but were not computed here.** They are imported.

---

## 6. How it works: the pipeline

```
   MASEF trace (JSON)
          │
          ▼
   ┌──────────────┐   reject anything that cannot support a graph;
   │  1 INGEST    │   normalize; resolve which agent owns each step;
   │   ✅ built   │   keep every original field, untouched
   └──────────────┘
          │  Run: 52 validated Nodes
          ▼
   ┌──────────────┐   rebuild call edges from parent/child, handoff edges
   │  2 GRAPH     │   from the recorded transitions, and data edges — who
   │   next       │   consumed whose output
   └──────────────┘
          │  a graph with typed edges
          ▼
   ┌──────────────┐   spot the same operation attempted twice; group the
   │  3 RETRIES   │   attempts; measure the waste
   └──────────────┘
          │
          ▼
   ┌──────────────┐   start from failures (MASEF's verdicts + structural
   │  4 PROPAGATE │   ones), walk data edges forward, assign each reached
   │              │   step a severity: FAILED / DEGRADED / UNAFFECTED
   └──────────────┘
          │
          ▼
   ┌──────────────┐   walk backwards to the earliest step that explains
   │  5 ROOT CAUSE│   everything downstream; attach the evidence
   └──────────────┘
          │
          ▼
   ┌──────────────┐   store runs, serve them over HTTP, render the graph
   │  6-7 API/UI  │   and the diagnosis panel
   └──────────────┘
          │
          ▼
   ┌──────────────┐   traces with known root causes; score Atlas against
   │  8 BENCHMARK │   them; report accuracy honestly
   └──────────────┘
```

### Design rules that hold at every stage

1. **Nothing is invented.** Where a fact is absent, the field stays empty. A
   missing status becomes `UNSET`, never `OK` — assuming success from silence is
   exactly how a debugging tool hides the bug it exists to find.
2. **The original is always kept.** Every node carries the complete original span
   in `raw`, so no future analyzer is ever blocked by a field ingestion skipped.
3. **Derived data is never stored beside its source.** Edges are recomputed from
   nodes, not saved. Stored-and-recomputed copies drift apart, and a graph that
   contradicts its own trace can prove nothing.
4. **Errors name the thing that is wrong.** Not "invalid file" but
   `sessions[0].spans[2].end_time: 'not-a-time' is not an ISO-8601 timestamp`.
5. **Dependencies arrive with the phase that needs them.** Right now the whole
   project depends on one library (`pydantic`). No web framework, no graph
   library, no dataframe library — none of it is needed yet, so none of it is
   installed.

---

## 7. Where the work stands

**Phase 0 ✅ · Phase 1 ✅ · Phase 2 next.**  186 tests, all passing.

### Phase 0 — the domain model

Six object types, all self-validating: `Run`, `Node`, `Edge`, `Failure`, and the
registry objects. "Self-validating" means the guarantees are enforced by the
objects themselves and cannot be bypassed — step IDs unique, every parent
reference resolving to a real step, no cycles, no step its own parent, no end
time before a start time, no negative durations, and any unrecognized field
rejected loudly so a renamed upstream field can never be silently dropped.

Phase 2 can therefore *assume* a well-formed graph instead of re-checking.

### Phase 1 — ingestion

The loader that turns a real MASEF trace into those objects, plus the piece that
decides **which agent owns each step**. That second part matters more than it
sounds: a step named `ChatGoogleGenerativeAI` doesn't say who called it, and
without an owner you cannot say "researcher_1 caused this."

Atlas resolves ownership using MASEF's own five-step precedence, then inherits
from the nearest ancestor for anything still unresolved. Results on the real
52-span trace:

| | |
|---|---|
| Steps loaded | **52 of 52**, all connected to a single root |
| Ownership resolved | **50 of 52** — up from 12 before this phase |
| How | recorded metadata 26 · inherited from parent 18 · matched by name 6 |
| The 2 left over | the session root and the framework's own wrapper — neither is an agent's work, so Atlas leaves them unowned rather than fabricating an owner |
| Fidelity | 56,002 tokens, 6 agents, 3 tools, 7 possible handoffs of which 6 were taken, every original span preserved |

Note the last row of that table. **Refusing to guess is a feature.** Two steps
have no owner because they genuinely have no owner, and a test pins that as
correct behaviour rather than as a gap to close.

One quirk found in the real data, worth knowing about because it will bite
anyone counting things: **LangGraph records every agent step twice** — once
properly, once as a duplicate hung off the root. Any per-agent tally that counts
steps naively will double all six agents. Atlas keeps both copies (they are what
the trace says) and documents which one is the real one.

### Still to build

| Phase | What it adds | Blocked on |
|---|---|---|
| 2 | Execution graph with typed edges | choosing the data-dependency signal (§8) |
| 3 | Retry detection, retry waste | Phase 2 |
| 4 | Propagation and blast radius | needs a severity definition |
| 5 | Root-cause localization | Phase 4 |
| 6 | Run store and HTTP API | Phase 5 |
| 7 | Graph + diagnosis UI | Phase 6 |
| 8 | Benchmark and accuracy numbers | needs a ground-truth file format |

---

## 8. The two hard problems, in plain language

Both were found by running the real trace rather than reasoning about it. Both
change the design. Neither is a reason to stop.

### Problem 1 — the call tree cannot answer the question Atlas exists to answer

Atlas's headline feature is *"this step used the bad output, so it's affected
too."* The obvious way to compute that is to follow the parent/child links
forward from the broken step.

**It does not work.** In the real trace, `researcher_1` and `writer` are
*siblings* — both started by the same framework wrapper. The writer clearly
consumed the researcher's output, but that fact appears **nowhere** in the
parent/child links, because those links record *who started whom*, not *who used
whose output*.

So following parent/child links would produce a doubly wrong answer: it would
**miss the writer entirely** (the one node that really was contaminated) while
**flagging every internal bookkeeping step** under the failure (which nobody
cares about).

Three real signals in the trace *can* supply the missing relationship:

| | Signal | Strength | Weakness |
|---|---|---|---|
| 1 | The recorded handoffs (which transitions were actually taken) | explicitly recorded, not inferred | agent-level, not step-level |
| 2 | LangGraph's step numbering and trigger fields | precise ordering | only in 27 of 52 steps, LangGraph-only |
| 3 | Matching one step's output text against another's input text | works on any framework | a heuristic |

Phase 2 must pick, and must publish how much of the real trace its choice
covers. Until then Atlas leaves data edges empty rather than shipping a wrong
one. There is a test in the suite whose only job is to fail if anyone later
assumes the call tree is enough.

### Problem 2 — nothing in the trace says "this failed"

The real 52-step trace contains **zero error statuses.** 39 OK, 13 unset. And yet
it is precisely the kind of run Atlas exists to investigate.

Why: in agent systems the interesting failures are *semantic* — a contradiction,
an unsupported claim, a fabricated citation. A step that confidently returns
something wrong returns it with status OK. **Semantics do not set a status code.**

A failure detector keyed on error status would therefore find nothing at all on
the motivating trace. So Atlas's real input is not "a trace" but **a trace plus
MASEF's verdicts**, joined step by step. MASEF says *what* is wrong; Atlas says
*where it started and what it contaminated.*

This makes the division of labour tighter rather than looser, and it is already
wired: ingestion accepts an optional verdict map. Atlas still detects the
*structural* failures on its own (an error status, an exhausted retry chain, a
step that produced no output) because those need no judge.

---

## 9. Using it today

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev     # create the environment
uv run pytest           # 186 tests
```

Loading a trace:

```python
from atlas.ingestion import load_trace

runs = load_trace("examples/traces/minimal_run.json")
run = runs[0]

print(run.id, len(run.nodes), len(run.roots))

for node in run.nodes:
    print(f"{node.name:30} {node.kind.value:8} {node.agent}  ({node.agent_source})")
```

`node.agent_source` says *how* the owner was determined — whether the
instrumentation stated it outright or Atlas inherited it from a parent. Later
phases need that distinction, because a blast-radius claim resting on an inferred
owner is weaker than one resting on a recorded fact.

The integration test runs against the real MASEF trace when it can find it, and
**skips instead of failing** on a machine without a MASEF checkout:

```bash
ATLAS_MASEF_TRACE=/path/to/trace.json uv run pytest
# otherwise looked up at: ../../GRADUATION PROJECT/MASEF/traces/research_team_trace_1783093171.json
```

---

## 10. Repository map

```
ATLAS.md                       this file
README.md                      short version, for the repo front page
ATLAS_IMPLEMENTATION_PLAN.md   the original strategic plan (partly superseded)

docs/decisions.md              why the project looks the way it does — ten
                               decisions, each with the evidence behind it.
                               Read this before changing anything structural.
docs/event-schema.md           the trace format, field by field, and exactly
                               what ingestion keeps, drops, and normalizes

src/atlas/models/              the domain objects and their validation   (Phase 0)
src/atlas/ingestion/           the MASEF loader and agent attribution     (Phase 1)

tests/                         186 tests, one module per area
examples/traces/               a small hand-written trace in MASEF format
```

`docs/decisions.md` is the important one. Each entry records a decision, what it
supersedes in the plan, and the evidence — including the two problems in §8 and
what closing them will require.

---

## 11. Glossary

| Term | Meaning |
|---|---|
| **span** | one recorded step of a run, with a start and an end |
| **trace** | a JSON file: the system description plus every span of one or more runs |
| **session / run** | one execution of the agent system, start to finish |
| **node** | Atlas's internal object for one span |
| **call edge** | "this step started that step" — from parent/child links |
| **handoff edge** | "this agent passed work to that agent" — a recorded transition |
| **data edge** | "this step used that step's output" — the one Atlas needs and cannot yet build (§8) |
| **attribution** | deciding which agent owns a given step |
| **blast radius** | the set of steps affected by a failure, each with a severity |
| **root cause** | the earliest step that explains every downstream symptom |
| **critical path** | the longest chain of steps by wall-clock time — MASEF's definition, adopted verbatim, cited as context and never as evidence |
| **L1** | MASEF's capability level meaning "the spans are complete enough to rebuild the tree." Atlas requires it and rejects anything lower, saying what is missing. |
| **determinism** | same trace in, same diagnosis out, always — no model in the loop |
