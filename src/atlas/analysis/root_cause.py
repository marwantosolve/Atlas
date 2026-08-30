"""Deterministic root-cause localization (Phase 5).

The question is not "which spans failed" -- Phase 4 answered that -- but
"which failure should an engineer look at first". The answer here is derived
from observable graph properties only:

- **Is it a root?** A failure is *explained* when another failure sits
  upstream of it -- among its call-tree ancestors, or in the subtree of an
  agent whose traversed handoff fed the failing agent -- and started no later.
  An explained failure is propagation, not cause.
- **How it ranks.** Roots first, then by kind weight (an exhausted retry chain
  says more than an absent output), then by downstream reach (blast-radius
  size), then by depth (deeper is more specific: a tool inside an agent beats
  the agent's wrapper).

There are no confidence percentages. Plan §13 allows "dependency-based causal
attribution" and nothing stronger, and a number like 0.92 would imply a
calibration Atlas does not have. Ranking position plus the evidence each
candidate carries *is* the claim (plan §22.8).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from atlas.analysis.failures import (
    FailureReport,
    _KIND_WEIGHT,
    _failure_agent,
    _topmost_span_of_agent,
    analyze_failures,
)
from atlas.graph import ExecutionGraph, build_execution_graph
from atlas.models import EdgeType, Failure, Node, Run

_MIN_TIME = datetime.min


class RootCauseCandidate(BaseModel):
    """One failure, ranked as a candidate root cause."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    failure: Failure
    rank: int = Field(ge=1, description="1-based; 1 is the strongest candidate.")
    agent: str | None = None
    is_root: bool = Field(
        description="False when an upstream failure already explains this one; "
        "the candidate is then propagation, not cause."
    )
    upstream_failure_ids: list[str] = Field(
        default_factory=list,
        description="Failures upstream of this node that started no later.",
    )
    downstream_reach: int = Field(
        ge=0, description="Blast-radius size excluding the failed node itself."
    )
    depth: int = Field(
        ge=0, description="Call-tree distance from the run's root span(s)."
    )
    propagation_path: list[str] = Field(
        description="Node ids: the failure, its agent's owning span, then the "
        "owning spans of agents the failure reached over traversed handoffs."
    )
    reasons: list[str] = Field(
        min_length=1,
        description="Why the candidate ranks where it does, in the same "
        "evidence style as Failure.evidence.",
    )


class RootCauseReport(BaseModel):
    """Ranked root-cause candidates for a run. Empty when nothing failed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidates: list[RootCauseCandidate] = Field(
        description="Strongest first. A clean run yields an empty report, not a "
        "fabricated one."
    )

    @property
    def primary(self) -> RootCauseCandidate | None:
        """The strongest candidate, when any failure was detected."""
        return self.candidates[0] if self.candidates else None


def _depth(run: Run, node: Node) -> int:
    by_id = run.node_index
    depth = 0
    current = node.parent_id
    while current is not None:
        depth += 1
        current = by_id[current].parent_id
    return depth


def _upstream_failure_ids(
    run: Run, graph: ExecutionGraph, node: Node, failures: list[Failure]
) -> list[str]:
    """Failures that sit upstream of ``node`` and started no later.

    Upstream means: call-tree ancestors, or anything inside the subtree of an
    agent whose *traversed* handoff fed the failing node's agent. Both are
    edges data can actually have flowed along; a declared-but-not-taken
    transition explains nothing (ADR-009).
    """
    agent = _failure_agent(run, node)
    upstream: set[str] = set(graph.ancestors(node.id, types={EdgeType.CALL}))
    if agent is not None:
        for comm in run.communication.traversed_edges:
            if comm.target != agent:
                continue
            for owner in graph.owning_spans(comm.source):
                upstream.update(graph.subtree(owner.id))

    node_start = node.started_at or _MIN_TIME
    by_id = run.node_index
    return sorted(
        failure.node_id
        for failure in failures
        if failure.node_id in upstream
        and (by_id[failure.node_id].started_at or _MIN_TIME) <= node_start
    )


def _propagation_path(
    run: Run, graph: ExecutionGraph, node: Node, agent: str | None
) -> list[str]:
    """The failure's route through agent space: its own span, its agent's
    owning span, then the owning spans of agents reached over traversed
    handoffs, in the order the run handed work along."""
    if agent is None:
        return [node.id]

    path: list[str] = [node.id]
    top = _topmost(run, node, agent)
    if top is not None and top.id != node.id:
        path.append(top.id)

    seen_agents = {agent}
    frontier = [agent]
    while frontier:
        current = frontier.pop(0)
        targets = []
        for edge in graph.edges_of_type(EdgeType.HANDOFF):
            if graph.node(edge.source).agent != current:
                continue
            targets.append(edge.target)
            target_agent = graph.node(edge.target).agent
            if target_agent and target_agent not in seen_agents:
                seen_agents.add(target_agent)
                frontier.append(target_agent)
        for target in sorted(
            targets,
            key=lambda nid: (graph.node(nid).started_at or _MIN_TIME, nid),
        ):
            if target not in path:
                path.append(target)
    return path


def _topmost(run: Run, node: Node, agent: str) -> Node | None:
    return _topmost_span_of_agent(run, node, agent)


def analyze_root_causes(
    run: Run,
    *,
    failure_report: FailureReport | None = None,
    graph: ExecutionGraph | None = None,
) -> RootCauseReport:
    """Rank the run's failures as root-cause candidates."""
    if graph is None:
        graph = build_execution_graph(run)
    if failure_report is None:
        failure_report = analyze_failures(run, graph=graph)

    by_id = run.node_index
    failures = failure_report.failures

    scored: list[tuple[tuple[int, int, int, int], RootCauseCandidate]] = []
    for failure, radius in zip(failures, failure_report.radii):
        node = by_id[failure.node_id]
        agent = _failure_agent(run, node)
        upstream = _upstream_failure_ids(run, graph, node, failures)
        reach = sum(
            1 for entry in radius.affected if entry.node_id != failure.node_id
        )
        reasons = [
            f"failure kind {failure.kind.value} "
            f"(weight {_KIND_WEIGHT[failure.kind]})",
            f"downstream reach: {reach} affected node(s)",
            "no upstream failure explains it"
            if not upstream
            else f"upstream failures {upstream} precede it",
            f"depth {_depth(run, node)} from the root span",
        ]
        score = (
            0 if upstream else 1,
            _KIND_WEIGHT[failure.kind],
            reach,
            _depth(run, node),
        )
        candidate = RootCauseCandidate(
            failure=failure,
            rank=1,  # placeholder: the real rank is assigned after ordering
            agent=agent,
            is_root=not upstream,
            upstream_failure_ids=upstream,
            downstream_reach=reach,
            depth=_depth(run, node),
            propagation_path=_propagation_path(run, graph, node, agent),
            reasons=reasons,
        )
        scored.append((score, candidate))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    candidates = [
        candidate.model_copy(update={"rank": index + 1})
        for index, (_, candidate) in enumerate(scored)
    ]
    return RootCauseReport(candidates=candidates)
