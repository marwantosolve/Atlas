# ATLAS — Implementation Plan

> **Revision note (2026-08-23).** This document remains the strategic reference,
> but several sections are superseded. An audit of the MASEF working tree found
> that MASEF already implements cost analysis, latency decomposition, critical
> path, a failure chain, root-cause diagnosis with confidence scores, and a
> canonical framework-agnostic trace schema — so §2's premise about what MASEF
> does is out of date, and §5/§8/§11 would have rebuilt working code.
>
> Atlas has been narrowed to the genuine gap: span-level graph propagation,
> deterministic root-cause attribution, retry waste, cross-run comparison, and a
> run store/API. It now ingests MASEF traces natively instead of defining its own
> event schema.
>
> **Superseded sections:** §4 and §8 (see ADR-001, ADR-004), §5 and §11
> (ADR-002), §13's cost/impact framing (ADR-003), §19's counterfactual impact
> figures (ADR-003).
>
> See [docs/decisions.md](docs/decisions.md) for the evidence and the full
> reasoning, and [docs/event-schema.md](docs/event-schema.md) for the schema that
> replaced §8.

## 1. Purpose

Atlas is an execution intelligence platform for agentic AI systems.

Its purpose is not to replace an evaluation framework such as MASEF. Instead, Atlas focuses on understanding what happened during an agent run, why it happened, where failures originated, how they propagated, and how execution cost and latency were distributed.

The core distinction is:

> **MASEF asks: "Was the agent system good?"**
>
> **Atlas asks: "What exactly happened during execution, why did it happen, and where did it go wrong?"**

Atlas therefore owns **execution intelligence**, while MASEF owns **quality evaluation**.

---

## 2. Atlas vs. MASEF

### MASEF

MASEF is a research-oriented framework for evaluating multi-agent AI systems through modular, framework-agnostic evaluation pipelines. It evaluates dimensions such as agent behavior, reasoning artifacts, and system performance using mechanisms including LLM-based judges, semantic similarity, and NLI-based metrics.

Typical output:

```text
Artifact Layer
  Consistency     0.87
  Compliance      0.91
  Feasibility     0.76

Agent Layer
  Coherence       0.82
  Contradiction   0.14

Performance
  Latency         8.4s
  Cost            $0.31
```

MASEF can tell us that feasibility is low.

It is not primarily designed to explain the complete execution path that caused that score.

### Atlas

Atlas reconstructs an agent run as an execution graph and provides execution-level intelligence:

```text
User Request
     |
     v
Orchestrator
     |
  +--+---------+
  |  |         |
  v  v         v
 A1 A2        A3
 |  |          |
 v  v          v
Tool Tool     A3.1
 |             |
 v             v
Failure       Tool
 |             |
 +------+------+
        |
        v
   Final Answer
```

Atlas can then attribute:

- latency
- token usage
- API/LLM cost
- retries
- failures
- downstream impact
- critical-path contribution
- candidate root causes

Example:

```text
Root Cause:
Tool X timed out inside Agent A3.1

Propagation:
A3.1 -> A3 -> Final Synthesis

Impact:
+4.2s latency
+$0.08 cost
Reduced evidence available to final synthesis
```

### The relationship

The intended relationship is:

```text
                 AGENTIC SYSTEM
                       |
                       v
                 +-----------+
                 |   ATLAS   |
                 | Execution |
                 |Intelligence
                 +-----+-----+
                       |
               Reconstructed Trace
                       |
                       v
                 +-----------+
                 |   MASEF   |
                 | Evaluation|
                 +-----+-----+
                       |
                       v
                  Quality Scores
```

Atlas and MASEF should remain separate systems with a clean integration boundary.

---

# 3. Why Atlas Is Useful

Modern agentic systems are increasingly difficult to debug because a single user request may involve:

- multiple agents
- multiple LLM calls
- tool calls
- retries
- parallel branches
- agent-to-agent dependencies
- intermediate artifacts
- external APIs
- long execution chains

Traditional observability can show a sequence of events, but engineers still need to answer higher-level questions:

- Why did this run fail?
- What was the original failure?
- Which agent caused the downstream degradation?
- Which tool call introduced the latency?
- Where was most of the money spent?
- Which branch was on the critical path?
- How did a local failure affect the final answer?
- Why did one run perform better than another?

Atlas is intended to answer these questions.

The product should therefore be positioned as:

> **Execution intelligence for agentic AI systems.**

It should not initially be positioned as another generic observability dashboard.

---

# 4. Core Product Concept

Atlas should transform:

```text
Raw Agent Trace
      |
      v
Normalized Events
      |
      v
Execution Graph
      |
      +----> Cost Analysis
      |
      +----> Latency Analysis
      |
      +----> Failure Analysis
      |
      +----> Dependency Analysis
      |
      v
Diagnosis
```

The central object in Atlas is the **execution graph**.

A run should be represented as a directed graph containing nodes and edges.

### Nodes

Nodes may represent:

- agent execution
- LLM calls
- tool calls
- orchestration events
- retries
- artifacts
- errors
- external service calls

### Edges

Edges represent relationships such as:

- execution dependency
- parent/child relationship
- data dependency
- agent-to-agent communication
- downstream propagation

---

# 5. MVP Scope

Do not attempt to build a full agent observability platform immediately.

The first version should focus on five capabilities:

1. Trace ingestion
2. Execution graph reconstruction
3. Cost/latency attribution
4. Failure propagation analysis
5. A focused debugging UI

The MVP should prove the following statement:

> Given an agent execution trace, Atlas can reconstruct the execution graph, attribute cost and latency, identify failures, and explain how failures propagate to downstream agents and the final result.

---

# 6. Proposed Architecture

```text
                 +-----------------------+
                 |   Agent Frameworks    |
                 | LangGraph / CrewAI    |
                 | AutoGen / Custom      |
                 +-----------+-----------+
                             |
                             v
                 +-----------------------+
                 |    Atlas Collector    |
                 +-----------+-----------+
                             |
                             v
                 +-----------------------+
                 |   Event Normalizer    |
                 +-----------+-----------+
                             |
                             v
                 +-----------------------+
                 | Execution Graph       |
                 | Reconstruction        |
                 +-----------+-----------+
                             |
              +--------------+--------------+
              |              |              |
              v              v              v
       Cost Analysis   Failure Analysis   Latency
              |              |              |
              +--------------+--------------+
                             |
                             v
                 +-----------------------+
                 |   Diagnosis Engine    |
                 +-----------+-----------+
                             |
                    +--------+--------+
                    |                 |
                    v                 v
                Atlas UI            MASEF
                                      |
                                      v
                               Quality Evaluation
```

---

# 7. Repository Structure

Start with a simple Python repository:

```text
atlas/
├── README.md
├── pyproject.toml
├── docs/
│   ├── architecture.md
│   ├── event-schema.md
│   └── roadmap.md
├── src/
│   └── atlas/
│       ├── __init__.py
│       ├── models/
│       ├── ingestion/
│       ├── graph/
│       ├── analysis/
│       ├── diagnosis/
│       ├── integrations/
│       └── api/
├── tests/
├── examples/
│   └── traces/
└── ui/
```

Keep the architecture modular from the beginning, but avoid premature infrastructure.

For the first version:

- Python
- Pydantic
- NetworkX
- FastAPI if an API layer is needed
- Streamlit for the initial UI

Do not introduce Neo4j, Kubernetes, Kafka, or a distributed tracing backend unless the MVP actually requires them.

---

# 8. Phase 0 — Define the Data Contract

Before implementing the analysis engine, define the canonical Atlas event model.

The initial entities should include:

```text
Run
Event
Node
Edge
Artifact
Failure
Cost
```

A minimal event could look like:

```json
{
  "id": "e2",
  "run_id": "run_001",
  "type": "tool_call",
  "agent": "researcher",
  "tool": "search",
  "timestamp": 1300,
  "latency_ms": 850,
  "cost": 0.02
}
```

Use Pydantic models for validation.

The event schema should be framework-agnostic.

Atlas should not depend directly on LangGraph, CrewAI, AutoGen, or another specific orchestration framework.

---

# 9. Phase 1 — Trace Ingestion

Implement:

```text
TraceLoader
JSONTraceLoader
TraceValidator
EventNormalizer
```

Input:

```text
trace.json
```

Output:

```python
Run(
    id=...,
    events=[...]
)
```

Requirements:

- validate required fields
- normalize timestamps
- normalize event types
- preserve raw event metadata
- reject malformed traces clearly
- support deterministic parsing

The ingestion layer should be isolated from the analysis layer.

This makes it possible to add framework adapters later.

---

# 10. Phase 2 — Execution Graph Reconstruction

Implement an `ExecutionGraph`.

Initial API:

```python
graph.add_node(...)
graph.add_edge(...)
graph.get_children(...)
graph.get_parents(...)
graph.get_critical_path(...)
```

The graph should reconstruct relationships between:

```text
Run
 |
 +-- Agent A
 |    +-- LLM Call
 |    +-- Tool Call
 |
 +-- Agent B
 |    +-- LLM Call
 |    +-- Tool Call
 |
 +-- Agent C
      +-- Agent B output
```

Use NetworkX for the initial implementation.

Do not start with a graph database.

The graph abstraction should be independent enough that the storage implementation can later be replaced.

---

# 11. Phase 3 — Execution Metrics

Implement independent analyzers:

```text
LatencyAnalyzer
CostAnalyzer
TokenAnalyzer
FailureAnalyzer
RetryAnalyzer
CriticalPathAnalyzer
```

Each analyzer should return structured results.

Example:

```json
{
  "agent": "researcher",
  "latency_ms": 4200,
  "cost": 0.21,
  "retries": 2
}
```

The analyzers should not directly depend on the UI.

This allows them to be tested independently and reused through APIs.

---

# 12. Phase 4 — Failure Propagation

This is one of the most important components of Atlas.

Given:

```text
Tool Timeout
     |
     v
Agent Retry
     |
     v
Context Degradation
     |
     v
Wrong Synthesis
```

Atlas should identify:

```text
Root Failure
    |
    v
Affected Nodes
    |
    v
Affected Artifacts
    |
    v
Final Impact
```

Initial API:

```python
get_failure_root()
get_affected_nodes()
get_failure_path()
```

Return a structured `FailureReport`.

Example:

```json
{
  "root_cause": "tool_timeout",
  "root_node": "tool_x",
  "affected_nodes": [
    "agent_b",
    "agent_c",
    "final_synthesis"
  ],
  "propagation_path": [
    "tool_x",
    "agent_b",
    "agent_c",
    "final_synthesis"
  ]
}
```

Start with deterministic dependency-based propagation.

Do not use an LLM to determine root causes in V1.

The system should first establish a deterministic foundation.

---

# 13. Phase 5 — Root-Cause and Impact Attribution

After deterministic failure propagation works, introduce attribution scores.

Potential outputs:

```text
root_cause_score
impact_score
confidence
```

For example:

```json
{
  "node": "tool_x",
  "root_cause_score": 0.92,
  "impact_score": 0.81,
  "confidence": 0.88
}
```

These scores should initially be based on observable graph properties:

- dependency position
- downstream reach
- failure type
- timing
- retries
- artifact dependency
- critical-path membership

Avoid making unsupported claims of formal causal inference.

The first implementation should be described as:

> dependency-based causal attribution

rather than claiming that the system has solved causal inference.

---

# 14. Phase 6 — MASEF Integration

Atlas should integrate with MASEF through a dedicated adapter:

```text
atlas/
└── integrations/
    └── masef.py
```

The adapter should allow:

```text
Atlas Run
    |
    v
MASEF-compatible Trace
    |
    v
MASEF Evaluation
```

The combined result could conceptually look like:

```json
{
  "execution": {
    "latency_ms": 12800,
    "cost": 0.42,
    "critical_path": ["planner", "researcher", "synthesizer"]
  },
  "diagnosis": {
    "root_cause": "tool_timeout",
    "affected_nodes": ["researcher", "synthesizer"]
  },
  "quality": {
    "consistency": 0.71,
    "feasibility": 0.62,
    "coherence": 0.68
  }
}
```

This creates a powerful workflow:

```text
Execution
    ↓
Diagnosis
    ↓
Evaluation
```

Or, more specifically:

```text
"What happened?"
        ↓
Atlas
        ↓
"Why did it happen?"
        ↓
Atlas
        ↓
"How good was the result?"
        ↓
MASEF
```

Atlas should not duplicate MASEF's quality metrics.

---

# 15. Phase 7 — Initial UI

Only build the UI after the backend and analysis primitives work.

The first UI should have three main views.

## Run List

```text
Run ID     Status     Latency     Cost
----------------------------------------
run_1842   Failed     12.8s       $0.42
run_1841   Success     8.4s       $0.31
run_1840   Success     7.9s       $0.28
```

## Run Details

```text
+---------------------------------------------+
| Run #1842                    $0.42   12.8s |
+---------------------------------------------+
|                                             |
|             EXECUTION GRAPH                 |
|                                             |
| Planner -> Researcher -> Synthesizer       |
|                |                            |
|                v                            |
|            Web Search                       |
|                |                            |
|              TIMEOUT                        |
|                                             |
+---------------------------------------------+
```

## Diagnosis Panel

```text
ROOT CAUSE

Web Search timeout inside Researcher

PROPAGATION

Web Search
    ↓
Researcher
    ↓
Synthesizer
    ↓
Final Answer

IMPACT

+4.2s latency
+$0.08 estimated cost
Reduced evidence available to synthesis
```

The UI should prioritize understanding the run over displaying dozens of metrics.

---

# 16. Initial API Design

A minimal API could expose:

```text
POST /runs
GET  /runs
GET  /runs/{run_id}
GET  /runs/{run_id}/graph
GET  /runs/{run_id}/metrics
GET  /runs/{run_id}/failures
GET  /runs/{run_id}/diagnosis
```

Potential response:

```json
{
  "run_id": "run_1842",
  "status": "failed",
  "latency_ms": 12800,
  "cost": 0.42,
  "root_cause": "tool_timeout",
  "critical_path": [
    "planner",
    "researcher",
    "synthesizer"
  ]
}
```

Keep the API small until real use cases require expansion.

---

# 17. Testing Strategy

Atlas should be developed test-first around deterministic examples.

Create synthetic traces covering:

### Case 1 — Simple success

```text
Agent A -> Tool -> Final
```

Expected:

- graph reconstructed
- no failures
- correct latency
- correct cost

### Case 2 — Tool failure

```text
Agent A -> Tool -> Failure
```

Expected:

- tool identified as failed
- failure returned
- downstream impact identified

### Case 3 — Retry

```text
Agent A -> Tool -> Failure
              |
              v
             Retry -> Success
```

Expected:

- retry detected
- total cost calculated
- total latency calculated
- final run classified correctly

### Case 4 — Multi-agent propagation

```text
Agent A
   |
   v
Agent B
   |
   v
Agent C
   |
   v
Final
```

If Agent A fails:

- B and C should be identified as downstream affected nodes
- propagation path should be returned

### Case 5 — Parallel branches

```text
             +-> Agent B --+
Planner -----|             |--> Synthesizer
             +-> Agent C --+
```

Expected:

- graph preserves parallelism
- critical path is correctly identified
- branch-level cost is attributed

### Case 6 — Partial failure

One branch fails while another succeeds.

Expected:

- successful branch remains valid
- failed branch is isolated
- final impact reflects only affected dependencies

---

# 18. Evaluation Benchmark

The first benchmark does not need to be huge.

Start with approximately:

```text
10 successful runs
10 failed runs
10 complex multi-agent runs
```

The benchmark should evaluate:

```text
Graph reconstruction accuracy
Root-cause identification accuracy
Failure propagation accuracy
Cost attribution accuracy
Latency attribution accuracy
Critical-path accuracy
```

The important question is not whether Atlas has many features.

The important question is whether it can reliably reconstruct and explain executions.

---

# 19. Example End-to-End Scenario

Consider:

```text
User
 |
 v
Planner
 |
 +------------------+
 |                  |
 v                  v
Researcher        Calculator
 |                  |
 v                  v
Search Tool        Result
 |
 X
TIMEOUT
 |
 v
Retry
 |
 X
FAIL
 |
 v
Synthesizer
 |
 v
Final Answer
```

Atlas should produce something similar to:

```text
Run:
run_1842

Status:
FAILED / DEGRADED

Latency:
12.8s

Cost:
$0.42

Root Cause:
Search Tool timeout

Propagation:
Search Tool
 -> Researcher
 -> Synthesizer
 -> Final Answer

Critical Path:
Planner
 -> Researcher
 -> Search Tool
 -> Researcher Retry
 -> Synthesizer

Highest Cost Agent:
Researcher

Highest Latency Contributor:
Researcher / Search Tool

Affected Artifact:
Research evidence

Final Impact:
Reduced evidence available to synthesizer
```

MASEF could then evaluate the final result:

```text
Consistency: 0.71
Feasibility: 0.62
Coherence: 0.68
```

Together, the systems answer:

```text
MASEF:
"The final result was weak."

Atlas:
"The result became weak because the search tool failed,
the researcher could not recover the required evidence,
and that degradation propagated into synthesis."
```

That combination is the actual value proposition.

---

# 20. What NOT to Build in V1

Avoid these initially:

- Full distributed tracing infrastructure
- Kubernetes deployment
- Kafka
- Neo4j
- Complex event streaming
- LLM-based root-cause analysis
- Automated remediation
- Autonomous optimization
- Dozens of dashboards
- Support for every agent framework
- Production-scale multi-tenancy
- Complex authentication/authorization
- Formal causal inference claims

These are possible future directions, not MVP requirements.

The goal is to validate the core intelligence layer first.

---

# 21. Future Roadmap

After the MVP is validated, Atlas can evolve toward:

## V2 — Framework Integrations

Support:

```text
LangGraph
CrewAI
AutoGen
OpenAI Agents SDK
Custom agent runtimes
```

Each framework should map into the same Atlas event schema.

## V3 — Production Observability

Potential components:

```text
OpenTelemetry
Prometheus
Grafana
PostgreSQL
Redis
```

Only add infrastructure when scale requires it.

## V4 — Intelligent Diagnosis

Introduce LLM-assisted analysis for:

- ambiguous root causes
- semantic artifact dependencies
- complex failure explanations
- anomaly explanations
- cross-run comparisons

The deterministic graph should remain the source of truth.

## V5 — Optimization

Atlas could eventually recommend:

```text
Remove unnecessary agent
Reduce retry count
Change tool
Reduce context size
Parallelize branches
Use cheaper model
Cache repeated calls
Improve routing
```

This would move Atlas from:

```text
Observe
    ↓
Diagnose
```

to:

```text
Observe
    ↓
Diagnose
    ↓
Optimize
```

---

# 22. Key Design Principles

### 1. Execution first

Atlas is fundamentally about execution intelligence.

### 2. Deterministic before intelligent

Build reliable graph reconstruction and dependency analysis before adding LLM reasoning.

### 3. Framework agnostic

Framework adapters should map into a common event model.

### 4. Separate analysis from presentation

The UI should consume analysis results rather than contain business logic.

### 5. Avoid premature infrastructure

NetworkX and local files are enough for the MVP.

### 6. Do not duplicate MASEF

MASEF evaluates quality.

Atlas explains execution.

### 7. Preserve raw evidence

Never throw away the original trace information during normalization.

### 8. Make every conclusion traceable

Every diagnosis should be backed by observable graph evidence.

---

# 23. Claude Code Implementation Sequence

Claude Code should not be asked to build the entire platform in one step.

Implement sequentially:

```text
Phase 0
Define models and event schema
        ↓
Phase 1
Implement trace ingestion
        ↓
Phase 2
Implement execution graph
        ↓
Phase 3
Implement analyzers
        ↓
Phase 4
Implement failure propagation
        ↓
Phase 5
Implement diagnosis
        ↓
Phase 6
Implement MASEF adapter
        ↓
Phase 7
Implement API
        ↓
Phase 8
Implement UI
        ↓
Phase 9
Build benchmark
        ↓
Phase 10
Evaluate and refine
```

At the end of every phase:

1. Run the test suite.
2. Add tests for the new behavior.
3. Update documentation.
4. Do not proceed if the current phase is unstable.

---

# 24. First Claude Code Task

The first instruction to Claude Code should be narrowly scoped:

> Read this implementation plan and initialize the Atlas repository.
>
> Implement Phase 0 only.
>
> Create the repository structure, Python project configuration, Pydantic domain models, and canonical event schema.
>
> Do not implement graph reconstruction, metrics, diagnosis, UI, or MASEF integration yet.
>
> Add unit tests for every domain model and schema validation rule.
>
> Add a minimal example trace under `examples/traces/`.
>
> Add `docs/event-schema.md` documenting every field.
>
> Run the complete test suite and ensure it passes before finishing.
>
> Do not introduce unnecessary infrastructure or dependencies.

After that succeeds, move to Phase 1.

---

# 25. Definition of Done for the MVP

Atlas MVP is complete when:

- A valid agent trace can be ingested.
- Events are normalized into the Atlas schema.
- An execution graph can be reconstructed.
- Agent/tool/LLM relationships are represented.
- Cost can be attributed to nodes and branches.
- Latency can be attributed to nodes and branches.
- Retries can be detected.
- Failures can be identified.
- Failure propagation can be traced.
- A candidate root cause can be identified deterministically.
- Critical paths can be calculated.
- A run can be inspected through a simple UI.
- Atlas can export a representation compatible with MASEF.
- Synthetic benchmark tests pass.
- The system can explain at least one non-trivial failure end-to-end.

The final MVP should demonstrate:

```text
TRACE
  ↓
EXECUTION GRAPH
  ↓
ATTRIBUTION
  ↓
FAILURE PROPAGATION
  ↓
ROOT-CAUSE DIAGNOSIS
  ↓
MASEF QUALITY EVALUATION
```

That is the core Atlas + MASEF story.
