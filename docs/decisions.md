# Decision Record

Decisions that changed `ATLAS_IMPLEMENTATION_PLAN.md`, with the evidence behind
them. The plan remains the strategic document; where the two disagree, this file
is current.

---

## ADR-000: The plan's premise about MASEF was out of date

**Status:** accepted, 2026-08-23

Plan §2 divides the world as *"MASEF asks: was the agent system good? Atlas
asks: what exactly happened during execution?"* — and states MASEF *"is not
primarily designed to explain the complete execution path."*

An audit of the MASEF working tree found that most of what the plan assigns to
Atlas already exists there:

| Plan section | Already in MASEF |
|---|---|
| §11 `CriticalPathAnalyzer` | `layers/performance/metrics/latency.py` — `critical_path`, `critical_path_ms`, parallelism exploitation, hotspots |
| §11 `CostAnalyzer` / `TokenAnalyzer` | `layers/performance/metrics/cost_analysis.py`, `layers/performance/pricing.py` |
| §11 `LatencyAnalyzer` | per-agent / per-tool / per-model decomposition with p50/p95 (`docs/latency_analysis_metric.md`) |
| §12 failure propagation | `dashboard/app/reports/diagnostics/failure_chain.py` (267 lines) |
| §13 root cause + impact + confidence | `dashboard/app/reports/diagnostics/root_cause.py` (615 lines) |
| §15 graph / timeline UI | `execution_view.py`, `timeline.py`, `workflow.py`, `graph_view.py` (networkx), Gantt + Sankey |
| §21 V5 optimization advice | `recommendations.py` (502 lines) |
| §8 framework-agnostic canonical schema | `tracing/masef_trace.schema.json` + `docs/trace_specs.md`, with L0–L3 capability levels and an adapter pattern |

That diagnostics package totals ~5,900 lines; commit `c934bd8` is
*"Dashboard V4 (Diagnostics Report)"*. `layers/common/mast.py` (479 lines)
implements the MAST failure taxonomy.

**Consequence:** building the plan as written would have rebuilt three of its
five MVP capabilities (§5) alongside a working implementation.

*Confidence:* module docstrings, signatures, the latency/cost metric docs and
the trace schema were read in full; the 5,900 lines were not read line by line.

---

## ADR-001: MASEF's trace format is Atlas's native input

**Status:** accepted, 2026-08-23 — supersedes plan §8

Atlas defines no event schema of its own.

The plan's §8 sketch (8 flat fields) could not express a graph: no causality
field, a point `timestamp` instead of an interval, no `status`, no error object,
no retry grouping, no token counts, no model name, and no schema version. Every
one of those already exists in `masef_trace.schema.json`, which uses
`span_id` / `parent_span_id` / `start_time` / `end_time` / `status` /
`llm_token_count_*` / `llm_model_name`.

Maintaining a second canonical schema describing the same thing would mean two
formats to keep in sync and two adapter suites to write. MASEF already owns the
adapter story for LangGraph, CrewAI, AutoGen and OpenAI Agents SDK.

**Consequences**

- Atlas ingests MASEF traces directly and requires MASEF **L1** capability.
- Plan §6 (Phase 6, MASEF integration) becomes near-trivial: the boundary is the
  format, not a translation layer.
- Atlas's own "framework agnostic" claim (§22.3) is inherited from MASEF rather
  than reimplemented.
- Trade-off accepted: Atlas is coupled to MASEF's schema. It is mitigated by the
  schema being versioned and stable, and by ingestion being isolated from
  analysis (plan §9).

---

## ADR-002: Atlas is narrowed to the genuine gap

**Status:** accepted, 2026-08-23 — supersedes plan §5, §11

Atlas is a **diagnostic investigation layer**, not a second evaluator. The split
with MASEF is by question:

- **MASEF** — evaluation, scoring, observability: *how good was this run?*
- **Atlas** — failure investigation, structural diagnosis, root cause: *where
  did it break, and what did that break?*

Atlas builds only what MASEF lacks:

1. **Span-level DAG propagation.** MASEF's `failure_chain.py` propagates across
   *evaluation layers* ("Format/Tool failures → Reasoning issues → Global metric
   degradation"). Nothing walks `tool_x → researcher → synthesizer → final_answer`
   through the actual span graph. This is plan §12, and it does not exist yet.
2. **Deterministic root-cause localization.** MASEF's is LLM-judged (MAST calls
   `OpenAI` with judge prompts) plus metric thresholds. A reproducible,
   graph-derived localization is a real contribution and is what plan §22.2
   argues for.
3. **Retry detection and retry-waste attribution.** Not present in MASEF.
4. **Cross-run structural comparison.** MASEF is single-trace oriented.
5. **A run store and HTTP API.** MASEF is a Streamlit app with no `POST /runs`.

**Explicitly out of scope**, and not to be added back merely because it would be
useful: cost analysis, latency percentiles, critical-path *timing*, pricing
tables, quality metrics, prompt-level evaluation, evaluation reports, a second
dashboard, generic "evaluate my trace" workflows, and visualization that adds no
diagnostic capability. Atlas consumes MASEF's numbers for these or does without
them.

---

## ADR-003: Atlas never computes cost

**Status:** accepted, 2026-08-23 — narrows plan §11

`Node.cost_usd` is carried through when a trace supplies it and is otherwise
`None`. MASEF owns `pricing.py`; a second pricing table would drift from it and
produce two different dollar figures for one run — the worst possible outcome for
a debugging tool.

This also removes a design smell in plan §8, where `cost` was an *input* field:
a system handed its cost is summing someone else's arithmetic, not attributing.

The one impact figure Atlas may legitimately claim is **retry waste** — the cost
and latency of attempts that failed and were superseded. That is directly
observable. Plan §19's `+4.2s / +$0.08` framing implies a counterfactual baseline
that no trace contains, and is not adopted.

---

## ADR-004: A span is a node; there is no separate Event entity

**Status:** accepted, 2026-08-23 — supersedes plan §4, §8

The plan is ambiguous about granularity: §4 says a node may be an agent, an LLM
call, a tool, a retry, an artifact or an error; §10 nests calls inside agents;
§12–13 use agent-level ids while §19 mixes levels freely.

Resolution: **the base graph is spans, one node per span.** Agent-level views
are derived rollups computed on top, never a second stored graph. The §8 "Event"
entity is dropped — a MASEF span already carries identity, causality, timing and
status, so a parallel representation would only be a second thing to keep in
sync.

`Artifact` is deferred until a data-dependency source exists in the trace;
`EdgeType.DATA` is reserved but unpopulated.

---

## ADR-005: Dependencies arrive with the phase that needs them

**Status:** accepted, 2026-08-23 — implements plan §22.5

Phase 0 depends on `pydantic` alone. NetworkX arrives with Phase 2, FastAPI with
the API phase, UI dependencies last. `uv` manages the environment.

Note for the UI phase: Streamlit renders graphs poorly. Plan on graphviz or
pyvis for the graph view specifically, as MASEF's `graph_view.py` already does.

---

## ADR-006: Edges are derived, never stored on a Run

**Status:** accepted, 2026-08-23

`Run` holds nodes; Phase 2 reconstructs edges from them. Storing derived data
next to its source invites the two to disagree, and an execution graph that
contradicts its own trace cannot support plan §22.8 ("every conclusion
traceable").

`Edge` carries an explicit `EdgeType` for the same reason: a propagation claim
resting on a `call` edge is a different claim from one resting on a `handoff`.

---

## ADR-007: Shared definitions come from MASEF, not from Atlas

**Status:** accepted, 2026-08-23 — closes the "critical path is never defined"
open question

Where Atlas and MASEF must mean the same thing by a term, MASEF's existing
implementation is the definition. Two tools that blame different agents for the
same span, or draw different critical paths through the same run, would make
both untrustworthy — and MASEF shipped first.

**Agent attribution** — adopted and implemented in `atlas/ingestion/attribution.py`,
matching `layers/performance/parser.py::_extract_agent_name` step for step:

| # | Source | Atlas `AgentSource` |
|---|---|---|
| 1 | `openinference.agent_name` | `OPENINFERENCE` |
| 2 | `attributes.agent_name` / `attributes["openinference.agent.name"]` | `ATTRIBUTES` |
| 3 | `langgraph_node` inside the JSON at `attributes.metadata`, `_tools` suffix stripped | `LANGGRAPH_METADATA` |
| 4 | caller-supplied `span_id → agent` map (MASEF's `cross_link_index`) | `CROSS_LINK` |
| 5 | span `name` joined to `AgentSpec.node_name` | `SPAN_NAME` |
| — | nearest ancestor that resolved by 1–5 | `ANCESTOR` |

Two details are load-bearing and were adopted verbatim: names canonicalize to
the registry's `agent_name` (`writer_agent`), never the graph's `node_name`
(`writer`) — which is also the form `communication_graph` edges use, so handoff
edges join to node attribution without a second mapping; and the `_tools`
suffix on a LangGraph node is stripped rather than treated as a separate agent.

Atlas adds `Node.agent_source`, which MASEF has no equivalent of. Attribution is
an *inference*, and Phase 4/5 propagation claims rest on it: a blast-radius
report has to be able to say whether a span's owner was declared by the
instrumentation or guessed from its parent. Without the provenance the two are
indistinguishable.

**Critical path** — adopted by definition, deferred by implementation. MASEF's
`_compute_critical_path` (`layers/performance/metrics/latency.py`) reports
`critical_path_method: "span_dag"`: a DP over sibling spans that chains
`i → j` when `end_time(i) <= start_time(j) + 10ms`, taking the longest chain.
Atlas will not compute a second one. Two consequences worth recording:

- The path is a **longest-wall-clock** path, not a longest-*dependency* path.
- Sibling chaining is inferred from **timing adjacency within a 10 ms
  tolerance** — a heuristic, not an observed dependency. Any Atlas conclusion
  that leans on the critical path inherits that heuristic, which sits awkwardly
  beside Atlas's determinism claim (plan §22.2). Atlas should therefore cite
  MASEF's critical path as *context*, and never as the evidence under a
  root-cause claim.

---

## ADR-008: MASEF verdicts are an input, not an optional extra

**Status:** accepted, 2026-08-23

The reference 52-span trace contains **zero `ERROR` spans** — every status is
`OK` or `UNSET`. Yet it is exactly the kind of run Atlas exists to investigate:
the interesting failures in agent systems are *semantic* (a contradiction, an
unsupported claim, a hallucinated citation), and semantics do not set an OTel
status code.

So a failure detector keyed on `status == ERROR` would find nothing on the very
trace that motivates the project. Atlas's real input is therefore **trace +
MASEF verdicts**, joined on `span_id` through MASEF's `cross_link_index`
(`span_id → {type, tool, agent}`, built in `layers/agents/main.py` and
`layers/artifact/main.py`). MASEF says *what* is wrong; Atlas says *where it
started and what it contaminated*.

**Consequences**

- `FailureKind` stays deterministic (ADR-002) but is **seeded**, not
  discovered: Atlas localizes and propagates the failures MASEF flags, and
  additionally detects the structural ones (`ERROR_STATUS`,
  `EXHAUSTED_RETRIES`, `MISSING_OUTPUT`) that need no judge.
- Ingestion accepts an optional `span_agent_map` for precisely this reason. It
  slots into attribution at MASEF's step 4. Atlas never reads MASEF's output
  directory itself — the caller supplies the map, so Atlas takes no dependency
  on MASEF's on-disk layout.
- Atlas must not re-judge. Re-running an LLM judge would be the duplication
  ADR-002 exists to prevent.

---

## ADR-009: Data dependency is not derivable from `parent_span_id`

**Status:** accepted, 2026-08-23 — constrains Phase 2 and Phase 4

The motivating example for blast radius is *"node 23 consumed the affected
artifact, so it is affected too."* The span tree cannot answer that. It encodes
**invocation nesting**, not dataflow: in the reference trace `researcher_1` and
`writer` are *siblings* under the graph root, so the fact that the writer
consumed the researcher's output appears nowhere in `parent_span_id`.

Reachability over `call` edges is therefore the wrong propagation relation. It
would miss the writer entirely while over-reporting every framework-internal
child of a failed span.

Three signals in the trace *can* supply dataflow, in decreasing strength:

1. **`communication_graph.edges` with `traversed: true`** — agent-level,
   explicitly recorded, 7 edges in the reference trace with `traversed`
   distinguishing taken from merely possible. Strongest, but coarse: agent
   granularity, not span.
2. **`langgraph_triggers` and `langgraph_step`** in `attributes.metadata` —
   these encode real execution ordering (`orchestrator` step 1 →
   `researcher_1`/`researcher_2` step 2 in parallel → `writer` 3 → `critic` 4 →
   `db_saver` 5). Precise, but LangGraph-specific, and present in only 27 of 52
   spans.
3. **Value matching** between an upstream `output_value` and a downstream
   `input_value`. Framework-agnostic and span-level, but a heuristic.

**Consequences**

- `EdgeType.DATA` stays unpopulated until Phase 2 chooses among these, and the
  choice must be recorded here with its coverage on the reference trace.
- `Edge.type` must be reported alongside any propagation claim. "Affected via a
  traversed handoff" and "affected via value overlap" are different claims and
  a diagnostic tool may not conflate them.
- Blast radius is not a flat reachable set. It needs severity — see the open
  question below, which this ADR does not resolve.

---

## ADR-010: v1 dataflow rides traversed handoff edges; DATA stays unpopulated

**Status:** accepted, 2026-08-29 — resolves ADR-009's open choice

ADR-009 listed three candidate dataflow signals. v1 takes signal 1 —
`communication_graph.edges` with `traversed: true` — and joins each traversed
edge (last owning span of the source agent → first owning span of the target
agent) into the graph as a `HANDOFF` edge.

**Consequences**

- Coverage is agent-granularity: a handoff says *the writer consumed something
  the researcher produced*, not *which artifact*. Every propagation claim that
  crosses a handoff says so (`via handoff`), so the coarser claim is never
  laundered into a span-level one.
- Signals 2 and 3 (`langgraph_step` ordering, value matching) remain future
  work; `EdgeType.DATA` stays reserved and unpopulated. When a span-level
  signal lands, it must record its coverage the way this ADR does.
- Joins that fail — no owning span on either side, or a self-loop — are not
  silently dropped: the analysis carries an `unjoined_handoffs` list, which is
  the honest coverage statement for the dataflow signal (and is exposed to the
  LLM query layer as a tool of its own).

---

## ADR-011: Retry detection is a structural rule; waste is superseded attempts only

**Status:** accepted, 2026-08-29

A retry group is identified deterministically: **sibling spans, same agent,
same operation, non-overlapping time intervals** — the earlier attempts must
have ended `ERROR`. The chaining invariant means a group of N attempts has N-1
superseded failures and one final attempt.

**Waste** is the duration (and carried cost, if the trace supplies it) of the
*superseded attempts only*. The final attempt is never waste, whatever its
outcome: if it failed, that is a failure (see ADR-012), not a cost of retrying.

**Consequences**

- Superseded attempts do not appear in the failure report as `ERROR_STATUS`
  failures; they appear in the retry-waste report. Reporting both would count
  one timeout twice.
- A final failed attempt is reported once, as `EXHAUSTED_RETRIES` on the last
  span, subsuming its `ERROR_STATUS`. One event, one failure kind.
- Detection annotates a copy of the run (`retry_of` pointers, `EdgeType.RETRY`
  edges); the ingested `Run` object is never mutated.

---

## ADR-012: Blast radius is graded, not flat

**Status:** accepted, 2026-08-29 — resolves the first open question

Propagation severity is three grades:

- **FAILED** — the node itself failed.
- **CONTAMINATED** — the node did not fail but consumed the failure: it is a
  call-descendant of the failed span's agent region, or reachable across a
  traversed handoff from the failing agent (plus that target's call
  descendants).
- **AT_RISK** — the node sits on the far side of a communication edge that was
  *not* traversed, so it may have consumed the failure but the trace does not
  say it did. `MISSING_OUTPUT` failures are capped at `AT_RISK` — with no
  output recorded, Atlas cannot claim contamination of anything.

Every affected node carries *how* it was reached — the edge types on the
propagation path — because (ADR-009) "contaminated via a call edge" and
"contaminated via a traversed handoff" are different claims with different
confidence.

---

## ADR-013: The LLM is a query interface, never the analyst

**Status:** accepted, 2026-08-29

The natural-language layer ("why did this run fail?") is a **thin, optional,
key-gated** extra (`pip install atlas[llm]` + `ANTHROPIC_API_KEY`). The
deterministic engine remains the source of truth.

The honesty rules live in the tool surface, not the prompt: the model may only
call seven **read-only, zero-argument** tools over the already-computed
analysis (`get_run`, `get_agents`, `get_failures`, `get_root_cause`,
`get_retry_waste`, `get_unjoined_handoffs`, `get_final_output`). It cannot
steer a query toward invented inputs, cannot re-derive anything, and every
fact in its answer is pipeline output. The system prompt additionally forbids
inventing span ids and requires presenting the root-cause ranking as evidence,
not verdict.

**Consequences**

- Without the extra or the key, `atlas ask` exits with an actionable message;
  every other surface is unaffected.
- The layer is a demonstration of the pitch — "deterministic engine
  underneath, LLM as explanation interface" — not a dependency of it.

---

## ADR-014: v1 surfaces are CLI, HTTP API, and a dependency-free read-only UI

**Status:** accepted, 2026-08-29

The public-repo v1 ships three surfaces over one pipeline: `atlas analyze`
(CLI, exit code 1 when failures are found — CI-friendly), a FastAPI HTTP API
(`GET/POST /api/runs`, subresources `/graph`, `/failures`, `/diagnosis`), and
a static single-page UI served by `atlas serve` with **no JavaScript
dependencies and no build step** — fetch + hash routing + DOM only.

The store is a directory of JSON files, one `StoredRun` document (run +
analysis) per run. No database, no server-side state beyond the directory;
single-process by design.

**Consequences**

- The UI renders the run graph as a depth-banded schematic (columns per depth,
  severity-colored nodes) rather than a force layout: it must stay legible and
  dependency-free, and a schematic is honest about being a summary.
- ADR-005's "plan on graphviz or pyvis for the graph view" is therefore *not*
  adopted for v1; it remains the option if an interactive graph view is ever
  wanted badly enough to accept a dependency.

---

## Open questions

- **Benchmark ground truth has no format.** Plan §18 wants root-cause and
  propagation accuracy, which requires each synthetic trace to ship an expected
  -diagnosis file (`root_cause`, `failure_type`, `affected_nodes`,
  `critical_path`). That format should be designed alongside the traces. Note
  that `critical_path` in the ground truth must mean MASEF's `span_dag` path
  (ADR-007) or the benchmark will measure agreement with a definition Atlas
  never adopted.
- **Multi-session traces are untested.** Every trace to hand holds exactly one
  session. The loader returns a list and is written for many, but the path has
  no real-world coverage.
- **Span-level dataflow.** ADR-010's agent-granularity handoff signal is the
  accepted v1 ceiling; `langgraph_step` ordering and value matching remain the
  candidate upgrades, and each must record its coverage when adopted.
