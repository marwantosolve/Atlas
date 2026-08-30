# Atlas

Atlas is a **diagnostic investigation layer** for agentic AI systems. It
reconstructs a run into a span-level execution graph, traces how a failure
propagated to everything downstream of it, and localizes candidate root causes
deterministically — so an engineer can see not just *that* a run went wrong,
but *where it started going wrong*.

```
$ uv run atlas analyze examples/traces/refund_run.json

Run session_refund_001 -- DEGRADED
  query: Analyze this customer's issue and check whether they're eligible for a refund.
  12 spans, 4 agents, 42.0s, 5000 ms lost to retries

Root causes (strongest first):
  #1 exhausted_retries at 293a4b5c6d7e8f90 (agent crm_agent)
       message: CRM API connection timed out after 5000 ms
       propagation: 293a4b5c6d7e8f90 -> 0718293a4b5c6d7e -> 4b5c6d7e8f901324
       why: failure kind exhausted_retries (weight 4)
       why: downstream reach: 4 affected node(s)
       why: no upstream failure explains it
       why: depth 2 from the root span

Failures:
  exhausted_retries at 293a4b5c6d7e8f90
    message: CRM API connection timed out after 5000 ms
    evidence: retry group 'lookup_customer' (agent crm_agent): attempts 18293a4b5c6d7e8f=ERROR, 293a4b5c6d7e8f90=ERROR
    evidence: final attempt '293a4b5c6d7e8f90' reported ERROR, so the operation never succeeded
    blast radius:
      FAILED 293a4b5c6d7e8f90 (crm_agent)
      CONTAMINATED 18293a4b5c6d7e8f (crm_agent) via call
      CONTAMINATED 3a4b5c6d7e8f9012 (crm_agent) via call
      CONTAMINATED 4b5c6d7e8f901324 (writer_agent) via handoff
      CONTAMINATED 5c6d7e8f90132456 (writer_agent) via handoff+call

Retry waste:
  lookup_customer (agent crm_agent): 5000 ms wasted on 1 superseded attempt(s)
```

*(abridged — the real report also names the agent's owning spans and any
unjoined handoffs; exit code is 1 because failures were found)*

## Why not just logs or tracing?

Observability tools tell you *that* a run failed and where the error line is.
They do not answer the questions that follow:

- **The error is often not the cause.** A retry-exhausted tool call is where
  the run died; the question is what it contaminated and what started it.
  Atlas walks the reconstructed graph — call edges for nesting, traversed
  handoff edges for dataflow — and grades every downstream node
  `FAILED` / `CONTAMINATED` / `AT_RISK`, with the edge types it was reached
  through.
- **Root cause is a ranking problem, not a search for `status == ERROR`.** In
  real agent traces the interesting failures are semantic — a contradiction, a
  hallucinated citation — and never set an error code. Atlas consumes
  evaluator verdicts as *input*, localizes and propagates them, and never
  re-judges.
- **Retries hide waste.** Superseded attempts are neither visible in a log
  grep nor counted as failures. Atlas detects retry chains structurally and
  attributes the latency (and carried cost) that the wasted attempts burned.
- **Everything is deterministic and traceable.** No LLM decides what failed.
  Every conclusion is derived from the trace by rules you can read in
  [docs/decisions.md](docs/decisions.md), and the optional natural-language
  layer may only *query* those conclusions, never produce them.

## Scope

Atlas complements [MASEF](docs/decisions.md#adr-001-masefs-trace-format-is-atlass-native-input)
(a sibling evaluation framework) rather than duplicating it. The split is by
question, not by feature:

> **MASEF** — evaluation, scoring, observability: *how good was this run?*
> **Atlas** — failure investigation, structural diagnosis, root-cause analysis:
> *where did it break, and what did that break?*

| Atlas owns | The evaluation framework owns |
|---|---|
| Span-level execution graph reconstruction | Quality scores (consistency, feasibility, coherence) |
| Failure propagation and blast radius through the graph | Cost analysis and pricing |
| Deterministic root-cause localization | Latency percentiles, critical-path timing |
| Retry detection and retry-waste attribution | Reasoning / artifact evaluation, failure taxonomies |
| Cross-run structural comparison | Framework adapters, the canonical trace format |

Atlas **never computes cost**, defines no metrics of its own, and produces no
evaluation report. Where a definition should be shared — agent attribution,
critical path — Atlas adopts the evaluation framework's rather than inventing
a rival one; where it has already scored something, Atlas consumes the verdict
instead of recomputing it.

## Quickstart

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev --extra api    # create the environment

uv run pytest                      # run the test suite

# Analyze a trace (the two-minute demo). Exit code 1 when failures are found,
# so it drops straight into CI.
uv run atlas analyze examples/traces/refund_run.json

# Serve the HTTP API and web UI over a run store
uv run atlas serve --store ./runs --seed examples/traces/refund_run.json
# → http://127.0.0.1:8000
```

### The optional natural-language layer

```bash
uv sync --extra llm
export ANTHROPIC_API_KEY=sk-...
uv run atlas ask "why did this run fail?" --run session_refund_001
```

The LLM may only call seven read-only, zero-argument tools over the
already-computed analysis; every fact in its answer is pipeline output. See
[ADR-013](docs/decisions.md#adr-013-the-llm-is-a-query-interface-never-the-analyst).
Without the extra or the key, every other Atlas surface works unchanged.

### Ingesting your own traces

Atlas ingests **MASEF traces** directly — it defines no event schema of its
own. A trace must reach MASEF's L1 capability level (spans carrying
`span_id`, `parent_span_id`, `start_time`, `end_time`); ingestion rejects
anything lower with a message naming what is missing. See
[docs/event-schema.md](docs/event-schema.md).

```python
from atlas.ingestion import load_trace
from atlas.analysis import analyze_run

runs = load_trace("my_trace.json")
analysis = analyze_run(runs[0])          # or runs[0], verdicts=[...]
print(analysis.root_causes.model_dump())
```

```bash
# Or over HTTP
curl -X POST localhost:8000/api/runs --json @my_trace.json
curl localhost:8000/api/runs/<run_id>/diagnosis
```

Evaluator verdicts (LLM-judged failures, etc.) seed detection as an input:
pass them as a JSON list of `{node_id, kind, message, source}` objects via
`atlas analyze --verdicts verdicts.json`, or under the `atlas_verdicts` key
of a POSTed trace. Atlas localizes and propagates them; it never re-judges.

## Status

**v1 complete** — deterministic engine, three surfaces, public-ready.

- [x] Phase 0 — domain models, event schema, validation
- [x] Phase 1 — trace ingestion (MASEF loader, agent attribution)
- [x] Phase 2 — execution graph reconstruction (call / retry / handoff edges)
- [x] Phase 3 — retry detection and retry-waste attribution
- [x] Phase 4 — failure severity, propagation and blast radius
- [x] Phase 5 — deterministic root-cause localization
- [x] Phase 6 — run store and HTTP API
- [x] Phase 7 — investigation UI, CLI, optional LLM query layer
- [ ] Phase 8 — benchmark ground truth and evaluation

The [implementation plan](v1.md) documents how v1 was sequenced and what was
deliberately deferred.

## Development

```bash
uv sync --extra dev --extra api    # environment with test + API dependencies
uv run pytest                      # the suite (no network, no API keys)
```

The integration test over a real 52-span MASEF trace skips unless pointed at
one: `ATLAS_MASEF_TRACE=/path/to/trace.json uv run pytest`.

## Layout

```
src/atlas/models/      domain models (spans, runs, edges)
src/atlas/ingestion/   MASEF trace loader and agent attribution
src/atlas/graph/       execution graph reconstruction
src/atlas/analysis/    retries, failures, blast radius, root cause, pipeline
src/atlas/store.py     the run store (a directory of JSON documents)
src/atlas/api/         FastAPI app and the static web UI
src/atlas/llm/         the optional, key-gated query layer
src/atlas/cli.py       the `atlas` command (analyze / serve / ask)
src/atlas/report.py    the text report renderer
tests/                 unit + integration tests, one module per area
examples/traces/       example traces in MASEF format
docs/                  event schema and decision record
```

## License

[MIT](LICENSE)
