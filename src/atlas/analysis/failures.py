"""Failure detection, propagation and blast radius (Phase 4).

**Detection** has two sources (ADR-008). The structural detectors decide from
the trace alone; seeded verdicts come from a caller who ran an evaluator
(MASEF's cross-link join) -- Atlas localizes and propagates them, it never
re-judges. The interesting failures in agent systems are semantic and do not
set an OTel status code, so a detector keyed on ``status == ERROR`` alone
would find nothing on exactly the runs Atlas exists to investigate.

**Propagation** is the ADR-009 relation, with the edge type recorded on every
hop: within an agent, failure spreads over ``CALL`` edges through the subtree
of the agent's owning span; across agents, it spreads over traversed
``HANDOFF`` edges -- which is what catches the writer that consumed the
researcher's bad output even though the two are siblings in the span tree.

**Blast radius is not a flat reachable set** (the open question in
docs/decisions.md, resolved here): every affected node carries a severity --

- ``FAILED`` -- the span the failure was detected at;
- ``CONTAMINATED`` -- reached over CALL edges or a traversed HANDOFF;
- ``AT_RISK`` -- an agent-level neighbour of the contaminated region over a
  declared-but-not-taken communication edge, or anything downstream of a
  merely suspected (``MISSING_OUTPUT``) failure.

-- plus the edge types that implicated it, so "affected via a traversed
handoff" and "affected because it was invoked" remain distinguishable claims.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from atlas.analysis.retries import RetryGroup, retry_groups
from atlas.graph import ExecutionGraph, build_execution_graph
from atlas.models import (
    AgentSource,
    EdgeType,
    Failure,
    FailureKind,
    Node,
    Run,
    SpanKind,
    SpanStatus,
)

_TIMEOUT_PATTERN = re.compile(r"time.?out|timed.?out|deadline", re.IGNORECASE)

# Kind weights for Phase 5 ranking and for structural dedup priority: a more
# specific observation of the same underlying event wins.
_KIND_WEIGHT: dict[FailureKind, int] = {
    FailureKind.EXHAUSTED_RETRIES: 4,
    FailureKind.TIMEOUT: 3,
    FailureKind.ERROR_STATUS: 2,
    FailureKind.MISSING_OUTPUT: 1,
    FailureKind.UNKNOWN: 0,
}

_PROPAGATION_EDGES = {EdgeType.CALL, EdgeType.HANDOFF}


class Severity(str, Enum):
    """How strongly a node is implicated by a failure."""

    FAILED = "failed"
    CONTAMINATED = "contaminated"
    AT_RISK = "at_risk"

    @property
    def rank(self) -> int:
        return {Severity.FAILED: 2, Severity.CONTAMINATED: 1, Severity.AT_RISK: 0}[self]


class Verdict(BaseModel):
    """A failure an external evaluator already judged, keyed to a span.

    This is MASEF's verdict reaching Atlas through the cross-link join
    (ADR-008): the caller supplies ``span_id -> verdict``; Atlas supplies the
    *where it started and what it contaminated* half.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    kind: FailureKind
    message: str | None = None
    source: str = Field(
        default="external",
        description="Who issued the verdict, e.g. 'masef'. Reported in the evidence.",
    )


class AffectedNode(BaseModel):
    """One node inside a blast radius, and why it is there."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    severity: Severity
    via: list[EdgeType] = Field(
        default_factory=list,
        description="Edge types on the paths that implicated this node. Empty "
        "for the failed node itself.",
    )
    agent: str | None = None
    agent_source: AgentSource | None = Field(
        default=None,
        description="Attribution provenance, surfaced so a report can flag "
        "claims resting on inherited (ANCESTOR) attribution.",
    )

    def merged_with(self, other: AffectedNode) -> AffectedNode:
        """Combine two entries for the same node: worst severity, unioned via.

        Kept as an explicit operation rather than silent dict-overwrite so the
        merge rule is a decision that tests can pin.
        """
        severity = self.severity if self.severity.rank >= other.severity.rank else other.severity
        via = list(dict.fromkeys([*self.via, *other.via]))
        return AffectedNode(
            node_id=self.node_id,
            severity=severity,
            via=via,
            agent=self.agent,
            agent_source=self.agent_source,
        )


class BlastRadius(BaseModel):
    """Where one failure reached, and how far."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    failure: Failure
    affected: list[AffectedNode] = Field(
        description="FAILED first, then CONTAMINATED, then AT_RISK; within a "
        "severity, by start time then id."
    )

    @property
    def failed_node_id(self) -> str:
        return self.failure.node_id

    @property
    def agent_severities(self) -> dict[str, Severity]:
        """Agent -> worst severity across its affected nodes."""
        worst: dict[str, Severity] = {}
        for entry in self.affected:
            if entry.agent is None:
                continue
            current = worst.get(entry.agent)
            if current is None or entry.severity.rank > current.rank:
                worst[entry.agent] = entry.severity
        return dict(sorted(worst.items()))


class FailureReport(BaseModel):
    """Every failure detected in a run, each with its blast radius."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    failures: list[Failure] = Field(
        description="Detections, ordered by the failing node's start time then id."
    )
    radii: list[BlastRadius] = Field(
        description="Parallel to ``failures``: radii[i] is failures[i]'s blast radius."
    )


# ── detection ─────────────────────────────────────────────────────────


def detect_failures(
    run: Run,
    *,
    verdicts: list[Verdict] | None = None,
    groups: list[RetryGroup] | None = None,
) -> list[Failure]:
    """Detect failures in ``run`` from both sources, deterministically ordered.

    Superseded retry attempts are deliberately absent: a failed attempt that
    was replaced did not shape the run's outcome -- it is retry waste
    (Phase 3), not a failure. The final attempt of an exhausted chain is
    reported once, as ``EXHAUSTED_RETRIES``, which subsumes its error status.
    """
    if groups is None:
        groups = retry_groups(run)
    by_id = run.node_index

    # Nodes whose outcome a retry group already accounts for.
    accounted = {gid for group in groups for gid in group.attempt_ids}

    failures: list[tuple[Node, Failure]] = []

    for node in sorted(run.nodes, key=lambda n: (n.started_at or datetime.min, n.id)):
        if node.failed and node.id not in accounted:
            failures.append((node, _structural_error_failure(node)))
        elif (
            node.kind in (SpanKind.LLM, SpanKind.TOOL)
            and node.status is not SpanStatus.ERROR
            and node.output_value is None
        ):
            failures.append((node, _missing_output_failure(node)))

    for group in groups:
        if not group.exhausted:
            continue
        final = by_id[group.final_attempt_id]
        attempts = ", ".join(
            f"{node_id}={by_id[node_id].status.value}" for node_id in group.attempt_ids
        )
        failures.append(
            (
                final,
                Failure(
                    node_id=group.final_attempt_id,
                    kind=FailureKind.EXHAUSTED_RETRIES,
                    message=final.status_message,
                    evidence=[
                        f"retry group '{group.operation}'"
                        + (f" (agent {group.agent})" if group.agent else "")
                        + f": attempts {attempts}",
                        f"final attempt {group.final_attempt_id!r} reported ERROR, "
                        "so the operation never succeeded",
                    ],
                ),
            )
        )

    for verdict in verdicts or []:
        if verdict.node_id not in by_id:
            raise ValueError(
                f"verdict from {verdict.source!r} names node {verdict.node_id!r}, "
                f"which is not in run {run.id!r}; a verdict must anchor to a span"
            )
        node = by_id[verdict.node_id]
        evidence = [f"verdict supplied by {verdict.source}: kind={verdict.kind.value}"]
        if verdict.message:
            evidence.append(f"verdict message: {verdict.message!r}")
        failures.append(
            (
                node,
                Failure(
                    node_id=verdict.node_id,
                    kind=verdict.kind,
                    message=verdict.message,
                    evidence=evidence,
                ),
            )
        )

    failures.sort(key=lambda pair: (pair[0].started_at or datetime.min, pair[0].id))
    return [failure for _, failure in failures]


def _structural_error_failure(node: Node) -> Failure:
    if node.status_message and _TIMEOUT_PATTERN.search(node.status_message):
        return Failure(
            node_id=node.id,
            kind=FailureKind.TIMEOUT,
            message=node.status_message,
            evidence=[
                "span status == ERROR",
                f"status_message {node.status_message!r} matches the timeout signature",
            ],
        )
    evidence = ["span status == ERROR"]
    if node.status_message:
        evidence.append(f"status_message: {node.status_message!r}")
    return Failure(
        node_id=node.id, kind=FailureKind.ERROR_STATUS, message=node.status_message, evidence=evidence
    )


def _missing_output_failure(node: Node) -> Failure:
    return Failure(
        node_id=node.id,
        kind=FailureKind.MISSING_OUTPUT,
        message=f"{node.kind.value} span completed without an output value",
        evidence=[
            f"span kind == {node.kind.value}, status == {node.status.value}",
            "output_value is absent",
        ],
    )


# ── propagation ───────────────────────────────────────────────────────


def _failure_agent(run: Run, node: Node) -> str | None:
    """The agent a failure propagates *from*: the node's own attribution, else
    the nearest attributed ancestor's. Per-node provenance stays available on
    each AffectedNode, which is where a report actually reads it."""
    if node.agent is not None:
        return node.agent
    by_id = run.node_index
    current = node.parent_id
    while current is not None:
        ancestor = by_id[current]
        if ancestor.agent is not None:
            return ancestor.agent
        current = ancestor.parent_id
    return None


def _topmost_span_of_agent(run: Run, node: Node, agent: str) -> Node | None:
    """The highest ancestor of ``node`` still inside ``agent``'s region.

    The failure may sit deep in a tool span; what hands output to other agents
    is the agent's owning span above it, so propagation needs the region's
    top, not the failure's immediate parent.
    """
    by_id = run.node_index
    topmost: Node | None = node if node.agent == agent else None
    current = node.parent_id
    while current is not None:
        ancestor = by_id[current]
        if ancestor.agent == agent:
            topmost = ancestor
        current = ancestor.parent_id
    return topmost


def blast_radius(
    run: Run,
    failure: Failure,
    *,
    graph: ExecutionGraph | None = None,
) -> BlastRadius:
    """Compute where ``failure`` reached in ``run``.

    ``graph`` may be supplied when the caller already built one; otherwise a
    fresh graph is built (propagation only reads CALL and HANDOFF edges, so
    whether retries have been applied is irrelevant here).
    """
    if graph is None:
        graph = build_execution_graph(run)
    by_id = run.node_index
    node = by_id[failure.node_id]

    # A merely suspected failure (MISSING_OUTPUT) may not claim more than
    # AT_RISK anywhere: nothing observed actually failed. For every other
    # kind the cap governs propagation only -- the failed node itself is an
    # observation, not a claim, and stays FAILED.
    suspected = failure.kind is FailureKind.MISSING_OUTPUT
    cap = Severity.AT_RISK if suspected else Severity.CONTAMINATED

    entries: dict[str, AffectedNode] = {}

    def add(
        node_id: str, severity: Severity, via: list[EdgeType], *, respect_cap: bool = True
    ) -> None:
        if node_id == node.id:
            # The failed node is in the radius because it failed, not because
            # an edge reached it; propagation loops re-encounter it, and the
            # via list must not claim an edge that merely points at it.
            via = []
        capped = severity if not respect_cap or severity.rank <= cap.rank else cap
        agent_node = by_id[node_id]
        entry = AffectedNode(
            node_id=node_id,
            severity=capped,
            via=sorted(via, key=lambda t: t.value),
            agent=agent_node.agent,
            agent_source=agent_node.agent_source,
        )
        existing = entries.get(node_id)
        entries[node_id] = entry if existing is None else existing.merged_with(entry)

    add(
        node.id,
        Severity.AT_RISK if suspected else Severity.FAILED,
        [],
        respect_cap=False,
    )

    agent = _failure_agent(run, node)

    # Within the failing agent: everything the agent's owning span invoked is
    # downstream of the failure's data, not only the failure's own subtree --
    # the LLM call that reasoned about the bad tool output is a sibling of the
    # tool span, and it is exactly the kind of node a blast radius exists to
    # name.
    if agent is not None:
        region_top = _topmost_span_of_agent(run, node, agent)
        if region_top is not None:
            for descendant in graph.descendants(region_top.id, types={EdgeType.CALL}):
                add(descendant, Severity.CONTAMINATED, [EdgeType.CALL])
    else:
        for descendant in graph.descendants(node.id, types={EdgeType.CALL}):
            add(descendant, Severity.CONTAMINATED, [EdgeType.CALL])

    # Across agents: the traversed handoffs leaving the failing agent's spans
    # contaminate the receiving agent's owning span and everything it invoked.
    if agent is not None:
        for edge in graph.edges_of_type(EdgeType.HANDOFF):
            if graph.node(edge.source).agent != agent:
                continue
            add(edge.target, Severity.CONTAMINATED, [EdgeType.HANDOFF])
            for descendant in graph.descendants(edge.target, types={EdgeType.CALL}):
                add(descendant, Severity.CONTAMINATED, [EdgeType.HANDOFF, EdgeType.CALL])

    # Agent-level neighbours over declared-but-not-taken edges: not
    # contaminated, but the topology says they could have consumed the bad
    # output, so an investigator should see them flagged.
    region_agents = {entry.agent for entry in entries.values() if entry.agent}
    for comm in run.communication.edges:
        if comm.traversed:
            continue
        neighbour = None
        if comm.source in region_agents:
            neighbour = comm.target
        elif comm.target in region_agents:
            neighbour = comm.source
        if neighbour is None or neighbour in region_agents:
            continue
        for owning in graph.owning_spans(neighbour):
            add(owning.id, Severity.AT_RISK, [EdgeType.HANDOFF])

    ordered = sorted(
        entries.values(),
        key=lambda e: (
            -e.severity.rank,
            by_id[e.node_id].started_at or datetime.min,
            e.node_id,
        ),
    )

    return BlastRadius(failure=failure, affected=ordered)


def analyze_failures(
    run: Run,
    *,
    verdicts: list[Verdict] | None = None,
    graph: ExecutionGraph | None = None,
) -> FailureReport:
    """Detect every failure in ``run`` and compute each one's blast radius."""
    if graph is None:
        graph = build_execution_graph(run)
    failures = detect_failures(run, verdicts=verdicts)
    radii = [blast_radius(run, failure, graph=graph) for failure in failures]
    return FailureReport(failures=failures, radii=radii)
