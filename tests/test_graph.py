"""Execution graph reconstruction (Phase 2).

Plan §17's synthetic cases, run against both bundled example traces and
hand-built runs, so every traversal guarantee later phases lean on is pinned
here first.
"""

from __future__ import annotations

import pytest

from atlas.graph import ExecutionGraph, build_execution_graph, owning_spans
from atlas.ingestion import load_trace
from atlas.models import (
    CommEdge,
    CommunicationGraph,
    EdgeType,
    SpanKind,
    SpanStatus,
)
from tests.conftest import EXAMPLES, make_node, make_run


@pytest.fixture
def refund_graph() -> ExecutionGraph:
    """The bundled refund scenario: parallel branches, a failed tool, a retry.

    Span ids are stable because the analysis-phase tests share this fixture's
    vocabulary:
    root       a1b2c3d4e5f60708
    orchestrator b2c3d4e5f6070819      LLM c3d4e5f60708192a
    policy_analyst d4e5f60708192a3b    tool e5f60708192a3b4c, LLM f60708192a3b4c5d
    crm        0718293a4b5c6d7e         tool1 18293a4b5c6d7e8f, tool2 293a4b5c6d7e8f90,
                                          LLM 3a4b5c6d7e8f9012
    writer     4b5c6d7e8f901324         LLM 5c6d7e8f90132456
    """
    run = load_trace(EXAMPLES / "refund_run.json")[0]
    return build_execution_graph(run)


# ── shape of the reconstructed graph ──────────────────────────────────


def test_every_span_is_a_node(refund_graph: ExecutionGraph) -> None:
    assert len(refund_graph) == 12


def test_call_edges_mirror_the_span_tree(refund_graph: ExecutionGraph) -> None:
    calls = {
        (e.source, e.target)
        for e in refund_graph.edges_of_type(EdgeType.CALL)
    }
    # Four agents and one root span hang directly off the root; each agent
    # span invokes its own tools and LLM calls.
    root = "a1b2c3d4e5f60708"
    assert calls == {
        (root, "b2c3d4e5f6070819"),
        (root, "d4e5f60708192a3b"),
        (root, "0718293a4b5c6d7e"),
        (root, "4b5c6d7e8f901324"),
        ("b2c3d4e5f6070819", "c3d4e5f60708192a"),
        ("d4e5f60708192a3b", "e5f60708192a3b4c"),
        ("d4e5f60708192a3b", "f60708192a3b4c5d"),
        ("0718293a4b5c6d7e", "18293a4b5c6d7e8f"),
        ("0718293a4b5c6d7e", "293a4b5c6d7e8f90"),
        ("0718293a4b5c6d7e", "3a4b5c6d7e8f9012"),
        ("4b5c6d7e8f901324", "5c6d7e8f90132456"),
    }


def test_handoff_edges_join_owning_spans(refund_graph: ExecutionGraph) -> None:
    """All four traversed communication edges join; siblings in the span tree
    (crm and writer) become connected -- the whole point of the HANDOFF edge
    (ADR-009: the span tree encodes nesting, not dataflow)."""
    handoffs = {
        (e.source, e.target): e.condition
        for e in refund_graph.edges_of_type(EdgeType.HANDOFF)
    }
    assert handoffs == {
        ("b2c3d4e5f6070819", "d4e5f60708192a3b"): "Always (parallel fan-out)",
        ("b2c3d4e5f6070819", "0718293a4b5c6d7e"): "Always (parallel fan-out)",
        ("d4e5f60708192a3b", "4b5c6d7e8f901324"): "Always",
        ("0718293a4b5c6d7e", "4b5c6d7e8f901324"): "Always",
    }
    assert refund_graph.unjoined_handoffs == []


def test_parallel_branches_are_preserved(refund_graph: ExecutionGraph) -> None:
    """Plan §17 case 5: both analysts are children of the root, and the crm
    subtree does not contain the policy subtree."""
    children = refund_graph.children("a1b2c3d4e5f60708", types={EdgeType.CALL})
    assert children == [
        "b2c3d4e5f6070819",  # orchestrator, started first
        "0718293a4b5c6d7e",  # crm, ties with policy_analyst at 05.3; id breaks the tie
        "d4e5f60708192a3b",  # policy_analyst
        "4b5c6d7e8f901324",  # writer
    ]
    crm_subtree = set(refund_graph.subtree("0718293a4b5c6d7e"))
    assert "e5f60708192a3b4c" not in crm_subtree  # policy tool stays in its branch
    assert "293a4b5c6d7e8f90" in crm_subtree  # the second attempt is in crm's


def test_traversal_reaches_across_agents_only_via_handoff(
    refund_graph: ExecutionGraph,
) -> None:
    """Over CALL edges alone, crm's world ends at its own subtree; opening the
    filter to HANDOFF reaches the writer -- which is how a downstream consumer
    shows up in a blast radius."""
    only_calls = refund_graph.descendants("0718293a4b5c6d7e", types={EdgeType.CALL})
    assert "4b5c6d7e8f901324" not in only_calls
    with_handoffs = refund_graph.descendants(
        "0718293a4b5c6d7e", types={EdgeType.CALL, EdgeType.HANDOFF}
    )
    assert "4b5c6d7e8f901324" in with_handoffs


def test_minimal_trace_reconstructs() -> None:
    run = load_trace(EXAMPLES / "minimal_run.json")[0]
    graph = build_execution_graph(run)
    assert len(graph) == 5
    assert len(graph.edges_of_type(EdgeType.CALL)) == 4
    # The one declared communication edge (orchestrator -> researcher) is
    # traversed, so it joins: 4 call edges + 1 handoff.
    assert len(graph.edges_of_type(EdgeType.HANDOFF)) == 1
    assert graph.unjoined_handoffs == []


# ── owning spans and the agent rollup ─────────────────────────────────


def test_agents_rollup_counts_owning_spans(refund_graph: ExecutionGraph) -> None:
    agents = refund_graph.agents()
    assert agents == {
        "crm_agent": ["0718293a4b5c6d7e"],
        "orchestrator_agent": ["b2c3d4e5f6070819"],
        "policy_analyst_agent": ["d4e5f60708192a3b"],
        "writer_agent": ["4b5c6d7e8f901324"],
    }


def test_owning_spans_prefer_chain_over_langgraph_duplicate() -> None:
    """The duplicate-span quirk (docs/event-schema.md §4): LangGraph emits each
    agent step twice -- a CHAIN span under the framework wrapper and an
    UNKNOWN duplicate under the session root. The rollup must count one."""
    wrapper_child = make_node(
        "chain1",
        name="writer",
        kind=SpanKind.CHAIN,
        agent="writer_agent",
        parent_id="wrapper",
    )
    duplicate = make_node(
        "dup1",
        name="writer",
        kind=SpanKind.UNKNOWN,
        agent="writer_agent",
        parent_id="root",
    )
    run = make_run(
        [
            make_node("root"),
            make_node("wrapper", parent_id="root"),
            wrapper_child,
            duplicate,
        ]
    )
    assert [n.id for n in owning_spans(run, "writer_agent")] == ["chain1"]


def test_owning_spans_fall_back_without_chain() -> None:
    """An agent that never got a CHAIN span still owns its tool spans; the
    fallback is detectable through the span's kind."""
    tool = make_node(
        "tool1", name="search_web", kind=SpanKind.TOOL, agent="r_agent", parent_id="root"
    )
    run = make_run([make_node("root"), tool])
    assert [n.id for n in owning_spans(run, "r_agent")] == ["tool1"]


def test_owning_spans_empty_for_unknown_agent(refund_graph: ExecutionGraph) -> None:
    assert refund_graph.owning_spans("nonexistent_agent") == []


# ── handoff joining edges cases ───────────────────────────────────────


def _run_with_comm(
    nodes: list, edges: list[CommEdge]
):
    return make_run(
        nodes,
        communication=CommunicationGraph(
            entry_point="a_agent", terminal_agents=["b_agent"], edges=edges
        ),
    )


def test_untraversed_comm_edge_produces_no_handoff() -> None:
    """A declared-but-not-taken transition must not become an edge: that
    would invent a handoff the run never made."""
    a = make_node("a", name="a", agent="a_agent")
    b = make_node("b", name="b", agent="b_agent")
    run = _run_with_comm([a, b], [CommEdge(source="a_agent", target="b_agent", traversed=False)])
    graph = build_execution_graph(run)
    assert graph.edges_of_type(EdgeType.HANDOFF) == []


def test_unjoinable_handoff_is_recorded_not_dropped() -> None:
    """Coverage of the dataflow signal is evidence (ADR-009): a traversed edge
    whose agent owns no span is reported, not silently skipped."""
    a = make_node("a", name="a", agent="a_agent")
    run = _run_with_comm([a], [CommEdge(source="a_agent", target="ghost_agent", traversed=True)])
    graph = build_execution_graph(run)
    assert graph.edges_of_type(EdgeType.HANDOFF) == []
    assert graph.unjoined_handoffs == ["a_agent -> ghost_agent"]


def test_self_transition_with_single_step_is_not_a_self_loop() -> None:
    """A retry-on-self topology (critic -> critic) where the agent ran once:
    joining would need a self-loop edge, which the Edge model rejects; the
    graph records the unjoined transition instead."""
    a = make_node("a", name="a", agent="a_agent", kind=SpanKind.CHAIN)
    run = _run_with_comm([a], [CommEdge(source="a_agent", target="a_agent", traversed=True)])
    graph = build_execution_graph(run)
    assert graph.edges_of_type(EdgeType.HANDOFF) == []
    assert graph.unjoined_handoffs == ["a_agent -> a_agent"]


def test_self_transition_with_two_steps_joins_them() -> None:
    """The same topology with two steps joins the earlier to the later: the
    loop actually happened."""
    a1 = make_node("a1", name="a", agent="a_agent", kind=SpanKind.CHAIN)
    a2 = make_node(
        "a2",
        name="a",
        agent="a_agent",
        kind=SpanKind.CHAIN,
        started_at=a1.started_at.replace(second=a1.started_at.second + 1),
        ended_at=a1.ended_at.replace(second=a1.ended_at.second + 1),
    )
    run = _run_with_comm([a1, a2], [CommEdge(source="a_agent", target="a_agent", traversed=True)])
    graph = build_execution_graph(run)
    handoffs = graph.edges_of_type(EdgeType.HANDOFF)
    assert [(e.source, e.target) for e in handoffs] == [("a1", "a2")]
    assert graph.unjoined_handoffs == []


# ── retry edges read what detection writes ────────────────────────────


def test_retry_edge_appears_when_retry_of_is_set() -> None:
    """Ingestion never sets ``retry_of`` (schema rule 8); Phase 3 does. The
    graph reads the field, so the RETRY edge appears as soon as it is set."""
    first = make_node(
        "t1", name="lookup", kind=SpanKind.TOOL, status=SpanStatus.ERROR, parent_id="root"
    )
    second = make_node("t2", name="lookup", kind=SpanKind.TOOL, parent_id="root", retry_of="t1")
    graph = build_execution_graph(make_run([make_node("root"), first, second]))
    retries = graph.edges_of_type(EdgeType.RETRY)
    assert [(e.source, e.target) for e in retries] == [("t1", "t2")]


def test_fresh_ingestion_has_no_retry_edges(refund_graph: ExecutionGraph) -> None:
    assert refund_graph.edges_of_type(EdgeType.RETRY) == []


# ── traversal details ─────────────────────────────────────────────────


def test_children_sorted_by_start_time(refund_graph: ExecutionGraph) -> None:
    children = refund_graph.children("0718293a4b5c6d7e", types={EdgeType.CALL})
    assert children == [
        "18293a4b5c6d7e8f",  # 05.5
        "293a4b5c6d7e8f90",  # 10.7
        "3a4b5c6d7e8f9012",  # 16.0
    ]


def test_ancestors_walk_parents(refund_graph: ExecutionGraph) -> None:
    # Earliest-first, matching parents/children: the run root started before
    # the agent span even though it is the *farther* ancestor. Typed to CALL:
    # the orchestrator's handoff into policy_analyst is also an ancestor over
    # untyped edges, which is exactly why the type filter exists.
    assert refund_graph.ancestors(
        "e5f60708192a3b4c", types={EdgeType.CALL}
    ) == [
        "a1b2c3d4e5f60708",
        "d4e5f60708192a3b",
    ]


def test_unknown_node_raises_keyerror(refund_graph: ExecutionGraph) -> None:
    with pytest.raises(KeyError, match="not a node of run"):
        refund_graph.children("ghost")


def test_descendants_exclude_self(refund_graph: ExecutionGraph) -> None:
    assert "a1b2c3d4e5f60708" not in refund_graph.descendants("a1b2c3d4e5f60708")
    assert refund_graph.descendants("a1b2c3d4e5f60708", types={EdgeType.CALL}) == [
        "b2c3d4e5f6070819",  # 00.1
        "c3d4e5f60708192a",  # 00.2
        "0718293a4b5c6d7e",  # 05.3, id before d4e5 at the same start
        "d4e5f60708192a3b",
        "18293a4b5c6d7e8f",  # 05.5, id before e5f6 at the same start
        "e5f60708192a3b4c",
        "f60708192a3b4c5d",  # 10.0
        "293a4b5c6d7e8f90",  # 10.7
        "3a4b5c6d7e8f9012",  # 16.0
        "4b5c6d7e8f901324",  # 30.1
        "5c6d7e8f90132456",  # 30.3
    ]


def test_subtree_includes_self(refund_graph: ExecutionGraph) -> None:
    assert refund_graph.subtree("d4e5f60708192a3b") == [
        "d4e5f60708192a3b",
        "e5f60708192a3b4c",
        "f60708192a3b4c5d",
    ]
