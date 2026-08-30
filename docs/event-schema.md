# Atlas Event Schema

Atlas has **no event schema of its own.** Its input format is the MASEF trace
(`tracing/masef_trace.schema.json` in the MASEF repository), and its internal
representation is a normalized projection of that format. This document
describes both sides and the mapping between them.

See [decisions.md](decisions.md) ADR-001 for why.

---

## 1. Input: the MASEF trace

A trace is a **static system description** plus one or more **runtime sessions**.

| Key | Required | Used by Atlas |
|---|---|---|
| `agents_registry.agents[]` | yes | agent attribution, `Run.agents` |
| `sessions[]` | yes | one `Run` per session |
| `tools_registry.tools[]` | no | `Run.tools` |
| `communication_graph` | no | `HANDOFF` edges, `Run.communication` |
| `schema_version` | recommended | `Run.schema_version` |
| `metadata` | no | `Run.metadata` |
| `export_time`, `session_count`, `span_count` | no | ignored (provenance only) |

Atlas requires the trace to reach MASEF's **L1 (execution)** capability level:
spans must carry `span_id`, a *present* `parent_span_id` key (null means root),
and `start_time`/`end_time`. An L0-only trace is a valid MASEF document but
Atlas cannot build a call tree from it, so ingestion rejects it — naming which
of the three is missing and on how many spans — rather than guessing.

Atlas does **not** require L2 or L3. Missing token counts or a missing
communication graph reduce what Atlas can say; they do not make a trace invalid.

`atlas/ingestion/capability.py` reimplements MASEF's `has_span_tree` predicate
rather than importing it. MASEF is a Streamlit application, not a library on
PyPI, so importing would make it a hard runtime dependency of Atlas ingestion
for the sake of thirty lines. The L2/L3 predicates are not reimplemented,
because Atlas does not gate on them.

### What ingestion drops

Everything else survives in `Node.raw` (spans) or `Run.metadata` (trace and
session). Three things are dropped on purpose:

| Dropped | Why |
|---|---|
| `agents[].system_prompt`, `factory_function` | Prompt-level evaluation is MASEF's (ADR-002), and prompts are kilobytes per run that no Atlas analyzer reads. |
| `session_count`, `span_count`, `total_agents`, `total_tools`, `total_edges` | They restate what the lists already say. A stored count that disagrees with its list is a bug waiting to happen. |
| `communication_graph.adjacency_list` | Fully derivable from `edges`. |

---

## 1a. Loading

```python
from atlas.ingestion import load_trace, load_run

runs = load_trace("trace.json")          # one Run per session
run = load_run("trace.json")             # single-session traces; raises otherwise
```

`load_trace_dict(trace, source=..., span_agent_map=...)` takes already-parsed
JSON. `span_agent_map` is MASEF's `cross_link_index` (`span_id → agent`) when
the caller has a MASEF evaluation output to hand; it feeds attribution step 4
below. Atlas never reads MASEF's output directory itself, so it takes no
dependency on MASEF's on-disk layout (ADR-008).

Failures raise `TraceFormatError` (not a MASEF trace, or a malformed field) or
`CapabilityError` (structurally valid but below L1), both subclasses of
`TraceError` and of `ValueError`.

---

## 2. Internal model

### `Run` — one execution

Self-contained: ingestion copies the trace-level registries onto each run so
analyzers never need a second object. One MASEF session becomes one `Run`.

| Field | Type | Notes |
|---|---|---|
| `id` | `str` | MASEF `session_id`. Non-empty. |
| `trace_id` | `str \| None` | |
| `schema_version` | `str \| None` | Version of the source trace spec. |
| `started_at` / `ended_at` | `datetime \| None` | Naive UTC (§3). |
| `duration_ms` | `float \| None` | `>= 0`. Derived from the run's own timestamps if absent — **never** inferred from node extents. |
| `input_query` | `str \| None` | Top-level user input. |
| `final_output` | `str \| None` | Final system answer. |
| `nodes` | `list[Node]` | At least one. Order not significant. |
| `agents` | `list[AgentSpec]` | |
| `tools` | `list[ToolSpec]` | |
| `communication` | `CommunicationGraph` | |
| `metadata` | `dict` | Source provenance. |

Properties: `node_index`, `roots`, `failed_nodes`.

### `Node` — one span

| Field | Type | From |
|---|---|---|
| `id` | `str` | `span_id` |
| `parent_id` | `str \| None` | `parent_span_id` |
| `name` | `str` | `name` |
| `kind` | `SpanKind` | `attributes["openinference.span.kind"]` |
| `status` | `SpanStatus` | `status` |
| `status_message` | `str \| None` | `status_message` |
| `started_at` / `ended_at` | `datetime \| None` | `start_time` / `end_time` |
| `duration_ms` | `float \| None` | `duration_ms` |
| `agent` | `str \| None` | **resolved**, see §4 |
| `agent_source` | `AgentSource \| None` | how `agent` was resolved, see §4 |
| `tool` | `str \| None` | `openinference.tool_name` |
| `model` | `str \| None` | `openinference.llm_model_name` |
| `tokens_prompt` | `int \| None` | `openinference.llm_token_count_prompt` |
| `tokens_completion` | `int \| None` | `openinference.llm_token_count_completion` |
| `tokens_total` | `int \| None` | `openinference.llm_token_count_total` |
| `cost_usd` | `float \| None` | carried only; Atlas never computes it |
| `attempt` | `int` | `1` unless a retry is detected. `>= 1` |
| `retry_of` | `str \| None` | id of the attempt this supersedes |
| `input_value` / `output_value` | `str \| None` | `openinference.*` |
| `raw` | `dict` | the entire original span, unmodified |

Properties: `failed`, `is_root`, `duration_disagreement_ms`.

The OTel `kind` (`INTERNAL`/`CLIENT`/…), `service_name`, `events` and
`eval_metadata` have no dedicated field. They are preserved in `raw`; no Atlas
analyzer reads them today, and `events`/`eval_metadata` are empty in every span
of the reference trace.

### `Edge` — a derived relationship

Edges are **not** stored on `Run`. Phase 2 reconstructs them from nodes, which
is what keeps derived data from drifting out of sync with its source.

| `EdgeType` | Meaning | Derived from |
|---|---|---|
| `call` | parent invoked child | `parent_id` |
| `handoff` | agent-to-agent transfer | traversed `communication_graph` edge |
| `retry` | failed attempt superseded | `retry_of` |
| `data` | consumer depends on upstream value | not yet populated |

Self-loops are rejected. `condition` carries the routing text for handoffs.

### `Failure`

`node_id`, `kind` (`FailureKind`), optional `message`, and a **non-empty**
`evidence` list. The list is mandatory by design: a failure Atlas cannot
justify from observable trace facts is one it should not report (plan §22.8).

Detection (`atlas.analysis.analyze_failures`) reports four kinds: seeded
verdicts localized verbatim (ADR-008), `EXHAUSTED_RETRIES` on the final
attempt of a detected retry chain (which subsumes that attempt's
`ERROR_STATUS`), `TIMEOUT` / `ERROR_STATUS` for unexplained error spans, and
`MISSING_OUTPUT` for spans that ended `OK` with no recorded output.

### Seeded verdicts: the `atlas_verdicts` key

Evaluator verdicts travel *inside* the trace body under `atlas_verdicts` — a
list of `{node_id, kind, message, source}` objects — when a caller has an
evaluator's judgments to hand:

```json
{
  "schema_version": "1.0",
  "atlas_verdicts": [
    {"node_id": "f60708192a3b4c5d", "kind": "unknown",
     "message": "Contradicts the policy text", "source": "masef"}
  ],
  "sessions": []
}
```

The store (`RunStore.add_trace`, `POST /api/runs`) extracts the key, seeds
detection with it, and strips it before ingestion proper, so the stored `Run`
is always a pure MASEF document. The CLI takes the same list as a standalone
file via `atlas analyze --verdicts verdicts.json`.

### Enums

- `SpanKind`: `LLM`, `TOOL`, `CHAIN`, `RETRIEVER`, `EMBEDDING`, `UNKNOWN`
- `SpanStatus`: `OK`, `ERROR`, `UNSET`
- `EdgeType`: `call`, `handoff`, `retry`, `data`
- `AgentSource`: `openinference`, `attributes`, `langgraph_metadata`, `cross_link`, `span_name`, `ancestor`
- `FailureKind`: `error_status`, `timeout`, `exhausted_retries`, `missing_output`, `unknown`

`UNKNOWN` span kind is a normal case, not a defect: **13 of 52 spans** in the
reference MASEF trace carry no kind attribute (framework-internal duplicates
emitted by LangGraph — see §4). Atlas keeps them so the call tree stays complete
rather than dropping them and orphaning their children. An *unrecognized* kind
also becomes `UNKNOWN` rather than raising, for the same reason.

---

## 3. Normalization rules

1. **Timestamps → naive UTC.** MASEF emits naive local ISO-8601
   (`2026-04-17T00:34:02.246968`). Adapters for other frameworks often emit
   offset-aware stamps. Mixing the two raises `TypeError` on the first
   comparison, so offset-aware inputs are converted to UTC and stripped of
   `tzinfo`. Naive inputs pass through untouched.
2. **Blank strings → `None`** for `status_message`, `agent`, `tool`, `model`,
   `input_value`, `output_value`. An empty string carries no more information
   than absence, and collapsing them keeps downstream checks single-branch.
3. **Durations are derived only when absent.** A supplied `duration_ms` is
   evidence and is never overwritten, even when it disagrees with the
   timestamps. `Node.duration_disagreement_ms` exposes the gap, which is the
   signal for clock skew or a mis-instrumented exporter. (No span in the
   reference trace disagrees by more than 1 ms.)
4. **Missing `status` → `UNSET`**, missing kind → `UNKNOWN`. Never `OK`:
   assuming success from silence is how a debugging tool hides the bug it
   exists to find.
5. **`raw` always holds the original span** (plan §22.7).
6. **A malformed timestamp is an error, not a `None`.** Silently dropping it
   would move the span to an unknown point on the timeline while still letting
   the run load, and every ordering conclusion drawn from it would be wrong.
7. **A structured `input_value`/`output_value` is serialized, not dropped.**
   MASEF emits JSON strings, but another adapter may emit an object; the field
   stays searchable text either way and the original is in `raw`.
8. **Retries are not guessed at ingestion.** `attempt` stays `1` and `retry_of`
   stays `None` for every node. Retry detection is Phase 3, and inventing it
   here would seed later phases with fiction.

## 4. Agent attribution

Attribution is **derived**, not read: `openinference.agent_name` is empty in all
52 spans of the reference trace. Atlas resolves it with the same precedence
MASEF uses (`layers/performance/parser.py::_extract_agent_name`), step for step,
so the two tools never disagree about who owned a span — see ADR-007.

| # | Source | `AgentSource` | Spans in the reference trace |
|---|---|---|---|
| 1 | `openinference.agent_name` | `OPENINFERENCE` | 0 |
| 2 | `attributes.agent_name` / `attributes["openinference.agent.name"]` | `ATTRIBUTES` | 0 |
| 3 | `langgraph_node` in the JSON string at `attributes.metadata` | `LANGGRAPH_METADATA` | 26 |
| 4 | caller-supplied `span_id → agent` map | `CROSS_LINK` | 0 (none supplied) |
| 5 | span `name` joined to `AgentSpec.node_name` | `SPAN_NAME` | 6 |
| — | nearest ancestor that resolved by 1–5 | `ANCESTOR` | 18 |

**Coverage: 50 of 52 spans.** The two that resolve to nothing are
`research_team_query` (the session root) and `LangGraph` (the framework's own
graph-execution wrapper). Neither is an agent's work, so Atlas leaves them
unowned rather than assigning them to the root's agent.

Two details are load-bearing:

- Names canonicalize to the registry's `agent_name` (`writer_agent`), never the
  graph's `node_name` (`writer`). That is MASEF's canonical form and it is also
  the form `communication_graph` edges use, so handoff edges join to node
  attribution without a second mapping.
- A LangGraph `<node>_tools` node is the same agent with its `_tools` suffix
  stripped, not a seventh agent absent from every registry.

`Node.agent_source` has no MASEF equivalent. It exists because Phase 4/5
propagation claims rest on attribution, and a blast-radius report has to be able
to say whether a span's owner was declared by the instrumentation or inferred
from its parent. Without the provenance the two are indistinguishable. `agent`
and `agent_source` are set or unset together; a validator enforces it.

### The duplicate-span quirk

LangGraph emits **each agent step twice**: a `CHAIN` span under the `LangGraph`
wrapper carrying the real metadata, and an `UNKNOWN` duplicate parented straight
to the session root. Both resolve to the same agent, by different sources:

```
research_team_query  UNKNOWN  (root, unowned)
├── LangGraph        CHAIN    (framework wrapper, unowned)
│   ├── writer       CHAIN    writer_agent  <- langgraph_metadata
│   └── critic       CHAIN    critic_agent  <- langgraph_metadata
├── writer           UNKNOWN  writer_agent  <- span_name
└── critic           UNKNOWN  critic_agent  <- span_name
```

Atlas keeps both, because both are what the trace says. But **any per-agent
rollup that counts spans will double every agent's step count**, so Phase 2 must
pick the `CHAIN` span under the wrapper as an agent's owning span. This accounts
for 6 of the 13 `UNKNOWN` spans; another 6 are `tool:*` invocation wrappers and
the last is the session root.

## 5. Validation

Ingestion guarantees these before any analyzer runs, so Phase 2 can assume a
well-formed forest:

- span ids unique within a run
- every `parent_id` and `retry_of` resolves to a node in the same run
- parent pointers acyclic
- no node is its own parent or its own retried attempt
- `ended_at >= started_at`
- `duration_ms`, `cost_usd`, token counts `>= 0`; `attempt >= 1`
- `agent` and `agent_source` set or unset together
- unknown fields rejected (`extra="forbid"`) so a renamed upstream field fails
  loudly instead of being silently dropped

Multiple roots are **allowed** — a trace may contain disconnected fragments,
and rejecting them would discard evidence. (The reference trace has exactly one,
and its integration test pins that.)

Error messages name the offending path, following MASEF's rule that a validator
must never emit a bare "invalid file":

```
run 'session_1': nodes[14].id 's3' is a duplicate; span ids must be unique within a run
run 'session_1': nodes[3] ('s4') has parent_id 'ghost', which is not present in
this run; the node cannot be placed in the call tree
run 'session_1': parent_id cycle detected: a -> b -> c -> a; the call tree must be acyclic
trace.json: sessions[0].spans[2].end_time: 'not-a-time' is not an ISO-8601 timestamp
trace.json: trace is below Atlas's minimum capability level (needs MASEF L1, a
reconstructable call tree): 5 span(s) lack start_time or end_time; ordering and
duration would have to be guessed
```

Attribution runs *before* `Run` construction, so it cannot rely on any of these
guarantees — the ancestor walk therefore guards against dangling and cyclic
parent pointers itself.
