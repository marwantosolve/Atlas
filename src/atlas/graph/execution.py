"""Execution graph reconstruction (Phase 2).

Turns a :class:`~atlas.models.run.Run` into an
:class:`ExecutionGraph`: one node per span, typed edges between them.

Three edge types are populated here, in order of how directly the trace
states them (see docs/decisions.md ADR-006 and ADR-009):

- ``CALL`` -- from ``parent_span_id``. Invocation nesting; always available
  because ingestion requires MASEF L1.
- ``RETRY`` -- from ``Node.retry_of``. Empty on a freshly ingested run (retry
  detection is Phase 3 and only it may set the field), populated once
  detection has run.
- ``HANDOFF`` -- from traversed ``communication_graph`` edges, joined to the
  agents' *owning spans*. This is the ADR-009 dataflow decision: cross-agent
  dependency rides the one signal the trace states outright, and every edge
  keeps its provenance so a propagation claim can say what it rests on.

``DATA`` edges stay unpopulated (ADR-009).
"""

from __future__ import annotations

from datetime import datetime

import networkx as nx

from atlas.models import Edge, EdgeType, Node, Run, SpanKind

_EdgeTypes = frozenset[EdgeType] | set[EdgeType] | None


def _sort_key(node: Node) -> tuple[datetime, str]:
    """Deterministic ordering: by start time, then id.

    A node without timestamps sorts first but still stably; real traces give
    every span a start time (ingestion requires it), so this is a defensive
    path, not a normal one.
    """
    return (node.started_at or datetime.min, node.id)


def owning_spans(run: Run, agent: str) -> list[Node]:
    """The spans that represent ``agent``'s own steps, earliest first.

    LangGraph emits each agent step twice: a ``CHAIN`` span under the
    framework wrapper carrying the real metadata, and an ``UNKNOWN`` duplicate
    parented straight to the session root (docs/event-schema.md §4). Counting
    both would double every agent's step count, so the ``CHAIN`` span is the
    owning span when one exists; the duplicates are still in the graph, they
    are just not what an agent-level rollup counts.

    An agent with no ``CHAIN`` span falls back to every span attributed to it
    -- an approximation, and one callers can detect because the owning spans
    carry ``kind``.
    """
    attributed = [node for node in run.nodes if node.agent == agent]
    chains = [node for node in attributed if node.kind is SpanKind.CHAIN]
    return sorted(chains or attributed, key=_sort_key)


class ExecutionGraph:
    """A run's spans as a directed graph with typed edges.

    The graph is a *view* of the run: nodes are the run's ``Node`` models and
    edges are derived, never stored back on the run, so the two cannot drift
    apart (ADR-006). Traversal helpers take an optional edge-type filter
    because the types are not interchangeable evidence: reaching a node over
    a ``CALL`` edge says the failing span's own subtree contains it, while
    reaching it over a ``HANDOFF`` says a downstream agent consumed the
    failing agent's output.
    """

    def __init__(self, run: Run, edges: list[Edge]) -> None:
        self.run = run
        self._nodes: dict[str, Node] = run.node_index
        self.edges = edges
        self.unjoined_handoffs: list[str] = []
        self._digraph = nx.MultiDiGraph()
        self._digraph.add_nodes_from(self._nodes)
        for edge in edges:
            self._digraph.add_edge(edge.source, edge.target, type=edge.type)

    # ── lookups ─────────────────────────────────────────────────────────

    def node(self, node_id: str) -> Node:
        return self._nodes[node_id]

    def __contains__(self, node_id: object) -> bool:
        return node_id in self._nodes

    def __len__(self) -> int:
        return len(self._nodes)

    def edges_of_type(self, edge_type: EdgeType) -> list[Edge]:
        return [edge for edge in self.edges if edge.type is edge_type]

    def owning_spans(self, agent: str) -> list[Node]:
        """See :func:`owning_spans`."""
        return owning_spans(self.run, agent)

    def agents(self) -> dict[str, list[str]]:
        """Agent name -> ids of that agent's owning spans, earliest first.

        This is the per-agent rollup that does not double-count the LangGraph
        duplicate spans; count these, not every attributed span.
        """
        rollup: dict[str, list[str]] = {}
        for agent in {node.agent for node in self.run.nodes if node.agent}:
            rollup[agent] = [node.id for node in self.owning_spans(agent)]
        return dict(sorted(rollup.items()))

    # ── traversal ───────────────────────────────────────────────────────

    def parents(self, node_id: str, *, types: _EdgeTypes = None) -> list[str]:
        """Nodes with an edge pointing at ``node_id``, earliest first."""
        self._require(node_id)
        found = [
            edge.source
            for edge in self.edges
            if edge.target == node_id and self._type_ok(edge.type, types)
        ]
        return self._sorted_ids(found)

    def children(self, node_id: str, *, types: _EdgeTypes = None) -> list[str]:
        """Nodes ``node_id`` points at, earliest first."""
        self._require(node_id)
        found = [
            edge.target
            for edge in self.edges
            if edge.source == node_id and self._type_ok(edge.type, types)
        ]
        return self._sorted_ids(found)

    def descendants(self, node_id: str, *, types: _EdgeTypes = None) -> list[str]:
        """Everything reachable from ``node_id`` over the allowed edge types.

        Excludes ``node_id`` itself. A cycle is impossible over ``CALL``
        (ingestion rejects parent cycles) and over ``HANDOFF`` (each joined
        edge moves between distinct agents' spans), but the walk tracks seen
        nodes anyway so a malformed future edge type cannot hang it.
        """
        return self._walk(node_id, forward=True, types=types)

    def ancestors(self, node_id: str, *, types: _EdgeTypes = None) -> list[str]:
        """Everything ``node_id`` is reachable from, over the allowed types."""
        return self._walk(node_id, forward=False, types=types)

    def subtree(self, node_id: str) -> list[str]:
        """``node_id`` plus everything it invoked, in call order."""
        return [node_id, *self.descendants(node_id, types={EdgeType.CALL})]

    # ── internals ───────────────────────────────────────────────────────

    def _join_handoff(
        self, source_agent: str, target_agent: str, condition: str | None
    ) -> None:
        """Add the ``HANDOFF`` edge for one traversed communication-graph edge.

        Joined span-to-span through the agents' owning spans: the source
        agent's last step hands its output to the target agent's first step,
        which is the only pairing a chronologically linear trace supports. A
        communication edge that cannot be joined -- an agent that owns no
        span, or a self-transition whose agent ran once -- is recorded in
        ``unjoined_handoffs`` instead of silently dropped: coverage of the
        dataflow signal is itself evidence (ADR-009).
        """
        sources = self.owning_spans(source_agent)
        targets = self.owning_spans(target_agent)
        if not sources or not targets:
            self.unjoined_handoffs.append(f"{source_agent} -> {target_agent}")
            return
        if source_agent == target_agent:
            # A self-transition is a loop the topology allows: the dataflow it
            # records is an agent's own later step consuming its earlier one,
            # so the edge runs first -> second in time. Joining last -> first
            # (the cross-agent rule) would point backwards through the run.
            if len(sources) < 2:
                self.unjoined_handoffs.append(f"{source_agent} -> {target_agent}")
                return
            source, target = sources[0], sources[1]
        else:
            source, target = sources[-1], targets[0]
        if source.id == target.id:
            # A self-transition by an agent that ran once: the topology says
            # the agent could loop, the run says it did not produce two steps
            # to loop between. Nothing to join.
            self.unjoined_handoffs.append(f"{source_agent} -> {target_agent}")
            return
        self.edges.append(
            Edge(
                source=source.id,
                target=target.id,
                type=EdgeType.HANDOFF,
                condition=condition,
            )
        )
        self._digraph.add_edge(source.id, target.id, type=EdgeType.HANDOFF)

    def _require(self, node_id: str) -> None:
        if node_id not in self._nodes:
            raise KeyError(
                f"{node_id!r} is not a node of run {self.run.id!r}; "
                f"known nodes: {len(self._nodes)}"
            )

    @staticmethod
    def _type_ok(edge_type: EdgeType, types: _EdgeTypes) -> bool:
        return types is None or edge_type in types

    def _sorted_ids(self, node_ids: list[str]) -> list[str]:
        unique = dict.fromkeys(node_ids)
        return sorted(unique, key=lambda nid: _sort_key(self._nodes[nid]))

    def _walk(
        self, node_id: str, *, forward: bool, types: _EdgeTypes
    ) -> list[str]:
        self._require(node_id)
        seen: set[str] = set()
        frontier = [node_id]
        while frontier:
            current = frontier.pop()
            for neighbor in (
                self.children(current, types=types)
                if forward
                else self.parents(current, types=types)
            ):
                if neighbor not in seen:
                    seen.add(neighbor)
                    frontier.append(neighbor)
        return self._sorted_ids(list(seen))


def build_execution_graph(run: Run) -> ExecutionGraph:
    """Reconstruct ``run``'s execution graph.

    Edge population order mirrors evidence strength: ``CALL`` (the trace
    states it), ``RETRY`` (detection set it), ``HANDOFF`` (joined through
    attribution onto owning spans).
    """
    edges: list[Edge] = []

    for node in run.nodes:
        if node.parent_id is not None:
            edges.append(Edge(source=node.parent_id, target=node.id, type=EdgeType.CALL))

    for node in run.nodes:
        if node.retry_of is not None:
            edges.append(Edge(source=node.retry_of, target=node.id, type=EdgeType.RETRY))

    graph = ExecutionGraph(run, edges)

    for comm in run.communication.traversed_edges:
        graph._join_handoff(comm.source, comm.target, comm.condition)

    return graph
