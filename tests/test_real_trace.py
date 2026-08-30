"""Integration test over a real MASEF export.

The bundled fixture is 5 hand-written spans; this is 52 spans from an actual
LangGraph run, and it is the only test that exercises the parts of the format a
fixture author would never think to write: framework-internal `__pregel_pull`
spans with no kind, `attributes.metadata` as a JSON string, six agents whose
node names differ from their registry names, and a graph root that belongs to no
agent at all.

The trace lives outside this repository, so the test skips when it is absent
(see `conftest.find_real_trace`). Set `ATLAS_MASEF_TRACE` to point at another
export.

The numbers below are pinned deliberately. They are not arbitrary: each one is a
fact about this run that Atlas must keep reproducing, and a diff in any of them
is either a real regression or a change that needs a decision recorded.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from atlas.ingestion import load_trace
from atlas.models import AgentSource, Run, SpanKind, SpanStatus
from tests.conftest import find_real_trace

TRACE_PATH = find_real_trace()

pytestmark = pytest.mark.skipif(
    TRACE_PATH is None,
    reason="the real MASEF trace is not available; set ATLAS_MASEF_TRACE to run",
)


@pytest.fixture(scope="module")
def real_run() -> Run:
    assert TRACE_PATH is not None
    runs = load_trace(TRACE_PATH)
    assert len(runs) == 1, "the reference export holds exactly one session"
    return runs[0]


@pytest.fixture(scope="module")
def raw_spans() -> list[dict]:
    """The spans as they appear on disk, for cross-checking the projection."""
    assert TRACE_PATH is not None
    trace = json.loads(Path(TRACE_PATH).read_text(encoding="utf-8"))
    return trace["sessions"][0]["spans"]


# --- shape -----------------------------------------------------------------


def test_run_identity(real_run: Run) -> None:
    assert real_run.id == "e097d42289108b2d"
    assert real_run.trace_id == "e097d42289108b2d7d0f6d3c8fca71ee"
    # This export predates `schema_version`; ingestion must not invent one.
    assert real_run.schema_version is None


def test_all_52_spans_become_nodes(real_run: Run, raw_spans: list[dict]) -> None:
    assert len(real_run.nodes) == 52
    assert len(raw_spans) == 52
    assert {node.id for node in real_run.nodes} == {s["span_id"] for s in raw_spans}


def test_exactly_one_root(real_run: Run) -> None:
    assert [node.name for node in real_run.roots] == ["research_team_query"]


def test_the_call_tree_is_connected_to_that_root(real_run: Run) -> None:
    """Every node reaches the root, so propagation has one graph to walk."""
    index = real_run.node_index
    root_id = real_run.roots[0].id
    for node in real_run.nodes:
        hops, current = 0, node
        while current.parent_id is not None:
            current = index[current.parent_id]
            hops += 1
            assert hops <= len(real_run.nodes), f"node {node.id} does not terminate"
        assert current.id == root_id


def test_no_span_reports_an_error_status(real_run: Run) -> None:
    """ADR-008: the interesting failures here are semantic, not OTel statuses.

    A failure detector keyed on `status == ERROR` would find nothing on the very
    trace that motivates the project, which is why MASEF's verdicts are an input
    rather than an optional extra.
    """
    assert real_run.failed_nodes == []
    assert Counter(node.status for node in real_run.nodes) == {
        SpanStatus.OK: 39,
        SpanStatus.UNSET: 13,
    }


def test_unknown_kinds_are_kept_not_dropped(real_run: Run) -> None:
    """13 framework-internal spans carry no kind. Dropping them would orphan
    their children and cut the call tree in half."""
    assert Counter(node.kind for node in real_run.nodes) == {
        SpanKind.LLM: 24,
        SpanKind.UNKNOWN: 13,
        SpanKind.CHAIN: 9,
        SpanKind.TOOL: 6,
    }
    unknown = [node for node in real_run.nodes if node.kind is SpanKind.UNKNOWN]
    assert all(node.name for node in unknown)
    # And they are load-bearing: most have children that would be orphaned.
    parents = {node.parent_id for node in real_run.nodes}
    assert any(node.id in parents for node in unknown)


# --- agent attribution -----------------------------------------------------


def test_attribution_reaches_50_of_52_spans(real_run: Run) -> None:
    """The span-name join alone reached 12. The metadata source plus ancestor
    inheritance (ADR-007) take it to 50."""
    attributed = [node for node in real_run.nodes if node.agent]
    assert len(attributed) == 50

    assert Counter(node.agent_source for node in attributed) == {
        AgentSource.LANGGRAPH_METADATA: 26,
        AgentSource.ANCESTOR: 18,
        AgentSource.SPAN_NAME: 6,
    }


def test_the_two_unattributed_spans_belong_to_no_agent(real_run: Run) -> None:
    """Not a coverage gap: neither span is an agent's work.

    `research_team_query` is the session root and `LangGraph` is the framework's
    own graph-execution wrapper. Assigning either to an agent would fabricate
    attribution, so Atlas leaves them unowned.
    """
    unattributed = sorted(node.name for node in real_run.nodes if not node.agent)
    assert unattributed == ["LangGraph", "research_team_query"]
    assert all(
        node.agent_source is None for node in real_run.nodes if node.agent is None
    )


def test_work_is_distributed_across_all_six_agents(real_run: Run) -> None:
    assert Counter(node.agent for node in real_run.nodes if node.agent) == {
        "researcher_2_agent": 16,
        "researcher_1_agent": 12,
        "db_saver_agent": 8,
        "orchestrator_agent": 5,
        "critic_agent": 5,
        "writer_agent": 4,
    }


def test_every_resolved_agent_exists_in_the_registry(real_run: Run) -> None:
    """Attribution canonicalizes to `agent_name`, so the join must close."""
    declared = {agent.name for agent in real_run.agents}
    assert declared == {
        "orchestrator_agent",
        "researcher_1_agent",
        "researcher_2_agent",
        "writer_agent",
        "critic_agent",
        "db_saver_agent",
    }
    for node in real_run.nodes:
        if node.agent:
            assert node.agent in declared, f"{node.name} -> unknown agent {node.agent}"


def test_no_node_is_attributed_to_a_bare_node_name(real_run: Run) -> None:
    """`writer`, not `writer_agent`, would silently break the handoff join."""
    node_names = {agent.node_name for agent in real_run.agents}
    assert node_names == {
        "orchestrator",
        "researcher_1",
        "researcher_2",
        "writer",
        "critic",
        "db_saver",
    }
    assert not [node for node in real_run.nodes if node.agent in node_names]


def test_inherited_attribution_agrees_with_its_ancestor(real_run: Run) -> None:
    """An ANCESTOR node must name the same agent its nearest owner does."""
    index = real_run.node_index
    inherited = [
        node for node in real_run.nodes if node.agent_source is AgentSource.ANCESTOR
    ]
    assert inherited
    for node in inherited:
        current = index[node.parent_id] if node.parent_id else None
        while current is not None and current.agent is None:
            current = index[current.parent_id] if current.parent_id else None
        assert current is not None, f"{node.id} inherited from nothing"
        assert current.agent == node.agent


# --- promoted fields -------------------------------------------------------


def test_llm_spans_carry_model_and_token_counts(real_run: Run) -> None:
    with_model = [node for node in real_run.nodes if node.model]
    assert len(with_model) == 24
    assert sum(node.tokens_total or 0 for node in real_run.nodes) == 56_002
    for node in with_model:
        assert node.tokens_total is not None
        assert node.tokens_prompt is not None
        assert node.tokens_completion is not None


def test_tool_calls_are_visible(real_run: Run) -> None:
    """12 spans name a tool while only 6 are kind TOOL -- the LLM span that
    requested the call carries `tool_name` too. Both are real, and Phase 2 will
    need to tell them apart."""
    named = [node for node in real_run.nodes if node.tool]
    assert len(named) == 12
    assert len([n for n in real_run.nodes if n.kind is SpanKind.TOOL]) == 6
    assert {node.tool for node in named} <= {tool.name for tool in real_run.tools}


def test_no_node_claims_a_cost(real_run: Run) -> None:
    """ADR-003. MASEF owns pricing; this trace carries no cost field."""
    assert all(node.cost_usd is None for node in real_run.nodes)


def test_timings_are_internally_consistent(real_run: Run) -> None:
    """No span disagrees with its own timestamps by more than a millisecond, so
    nothing in this run is evidence of clock skew."""
    for node in real_run.nodes:
        assert node.started_at is not None
        assert node.ended_at is not None
        gap = node.duration_disagreement_ms
        assert gap is not None and abs(gap) <= 1.0, f"{node.name}: {gap} ms"


def test_run_duration_is_the_session_value(real_run: Run) -> None:
    assert real_run.duration_ms == pytest.approx(184_272.628)
    assert real_run.started_at is not None and real_run.ended_at is not None
    assert real_run.ended_at > real_run.started_at


def test_every_span_survives_verbatim(real_run: Run, raw_spans: list[dict]) -> None:
    """Plan §22.7. `raw` is what lets a later phase read a field ingestion did
    not promote -- `langgraph_triggers`, for one (ADR-009)."""
    by_id = {span["span_id"]: span for span in raw_spans}
    for node in real_run.nodes:
        assert node.raw == by_id[node.id]


def test_metadata_records_where_the_run_came_from(real_run: Run) -> None:
    assert real_run.metadata["source"] == str(TRACE_PATH)
    assert real_run.metadata["framework_name"] == "research_team"
    assert real_run.metadata["export_time"] == "2026-04-17T00:37:06.508010"


# --- the static system description ----------------------------------------


def test_the_communication_graph_distinguishes_taken_from_possible(
    real_run: Run,
) -> None:
    """ADR-009 rests on this: 7 declared transitions, 6 actually traversed. The
    untaken one is the critic's revision loopback, and a propagation claim must
    not walk it."""
    graph = real_run.communication
    assert graph.entry_point == "orchestrator_agent"
    assert graph.terminal_agents == ["db_saver_agent"]
    assert len(graph.edges) == 7
    assert len(graph.traversed_edges) == 6

    untaken = [edge for edge in graph.edges if not edge.traversed]
    assert [(edge.source, edge.target) for edge in untaken] == [
        ("critic_agent", "writer_agent")
    ]
    assert untaken[0].edge_type == "conditional"


def test_langgraph_emits_each_agent_step_twice(real_run: Run) -> None:
    """A structural quirk any per-agent rollup has to know about.

    Every agent appears as two spans with the same name: a `CHAIN` one under the
    `LangGraph` wrapper carrying the real metadata, and an `UNKNOWN` duplicate
    parented straight to the session root. Counting spans per agent would double
    the step count for all six.

    Atlas keeps both -- they are what the trace says -- but records the
    difference so Phase 2 can pick the `CHAIN` one as the agent's owning span.
    """
    index = real_run.node_index
    agent_names = {agent.node_name for agent in real_run.agents}

    duplicated = {
        name: [node for node in real_run.nodes if node.name == name]
        for name in agent_names
    }
    for name, nodes in duplicated.items():
        assert len(nodes) == 2, f"{name}: expected 2 spans, got {len(nodes)}"
        kinds = {node.kind for node in nodes}
        assert kinds == {SpanKind.CHAIN, SpanKind.UNKNOWN}, name

        chain = next(node for node in nodes if node.kind is SpanKind.CHAIN)
        shadow = next(node for node in nodes if node.kind is SpanKind.UNKNOWN)
        assert index[chain.parent_id].name == "LangGraph"
        assert index[shadow.parent_id].name == "research_team_query"

        # Both land on the same agent, from different sources.
        assert chain.agent == shadow.agent
        assert chain.agent_source is AgentSource.LANGGRAPH_METADATA
        assert shadow.agent_source is AgentSource.SPAN_NAME


def owning_span(run: Run, agent: str):
    """The agent's real entry span: the CHAIN one under the LangGraph wrapper."""
    index = run.node_index
    found = [
        node
        for node in run.nodes
        if node.agent == agent
        and node.kind is SpanKind.CHAIN
        and node.parent_id is not None
        and index[node.parent_id].name == "LangGraph"
    ]
    assert len(found) == 1, f"{agent}: expected 1 owning span, got {len(found)}"
    return found[0]


def test_the_writer_is_a_sibling_of_the_researchers_not_their_child(
    real_run: Run,
) -> None:
    """The structural fact behind ADR-009.

    The writer consumed both researchers' output, but `parent_span_id` says
    nothing about it: all three hang off the same graph wrapper. Reachability
    over call edges would therefore miss the writer entirely, which is why
    `EdgeType.DATA` cannot be derived from the call tree.
    """
    index = real_run.node_index
    researcher = owning_span(real_run, "researcher_1_agent")
    writer = owning_span(real_run, "writer_agent")

    def ancestors(node) -> list[str]:
        seen = []
        while node.parent_id:
            node = index[node.parent_id]
            seen.append(node.id)
        return seen

    assert writer.id not in ancestors(researcher)
    assert researcher.id not in ancestors(writer)
    # They share a parent instead, so the handoff between them is declared only
    # in the communication graph -- nowhere in the call tree.
    assert writer.parent_id == researcher.parent_id
    assert ("researcher_1_agent", "writer_agent") in [
        (edge.source, edge.target) for edge in real_run.communication.traversed_edges
    ]


def test_the_researchers_ran_in_parallel(real_run: Run) -> None:
    """Overlapping siblings, which a sequential reading of the tree would miss."""
    first = owning_span(real_run, "researcher_1_agent")
    second = owning_span(real_run, "researcher_2_agent")
    assert first.started_at is not None and first.ended_at is not None
    assert second.started_at is not None and second.ended_at is not None
    assert first.started_at < second.ended_at
    assert second.started_at < first.ended_at
