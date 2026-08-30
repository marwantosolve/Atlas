"""Edge, registry and failure model rules."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from atlas.models import (
    AgentSpec,
    CommEdge,
    CommunicationGraph,
    Edge,
    EdgeType,
    Failure,
    FailureKind,
    ToolSpec,
)


# ── Edge ──────────────────────────────────────────────────────────────


def test_edge_carries_its_provenance() -> None:
    edge = Edge(source="a", target="b", type=EdgeType.CALL)
    assert edge.type is EdgeType.CALL
    assert str(edge) == "a -[call]-> b"


def test_edge_self_loop_rejected() -> None:
    with pytest.raises(ValidationError, match="points 'a' at itself"):
        Edge(source="a", target="a", type=EdgeType.CALL)


@pytest.mark.parametrize("field", ["source", "target"])
def test_edge_empty_endpoint_rejected(field: str) -> None:
    kwargs = {"source": "a", "target": "b", "type": EdgeType.CALL, field: ""}
    with pytest.raises(ValidationError, match="at least 1 character"):
        Edge(**kwargs)


def test_edge_is_immutable() -> None:
    """Edges are conclusions about the run; nothing should rewrite one in place."""
    edge = Edge(source="a", target="b", type=EdgeType.HANDOFF)
    with pytest.raises(ValidationError):
        edge.target = "c"


def test_edge_type_accepts_wire_value() -> None:
    assert Edge(source="a", target="b", type="retry").type is EdgeType.RETRY


def test_handoff_edge_keeps_routing_condition() -> None:
    edge = Edge(
        source="orchestrator_agent",
        target="researcher_1_agent",
        type=EdgeType.HANDOFF,
        condition="Always (parallel fan-out)",
    )
    assert edge.condition == "Always (parallel fan-out)"


# ── Registries ────────────────────────────────────────────────────────


def test_agent_spec_minimal() -> None:
    agent = AgentSpec(name="writer_agent")
    assert agent.node_name is None
    assert agent.tools_bound == []


def test_agent_spec_node_name_is_the_attribution_join_key() -> None:
    agent = AgentSpec(
        name="researcher_1_agent",
        node_name="researcher_1",
        tools_bound=["search_web"],
        can_communicate_with=["writer_agent"],
        participated=True,
    )
    assert agent.node_name == "researcher_1"
    assert agent.name != agent.node_name


def test_agent_spec_empty_name_rejected() -> None:
    with pytest.raises(ValidationError, match="at least 1 character"):
        AgentSpec(name="")


def test_agent_spec_rejects_system_prompt() -> None:
    """Deliberately not carried: prompt-level evaluation belongs to MASEF."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AgentSpec(name="writer_agent", system_prompt="You are...")


def test_tool_spec_records_invocation_counts() -> None:
    tool = ToolSpec(name="search_web", invoked=True, invocation_count=5)
    assert tool.invocation_count == 5


def test_tool_spec_negative_invocation_count_rejected() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        ToolSpec(name="search_web", invocation_count=-1)


def test_communication_graph_defaults_are_empty() -> None:
    graph = CommunicationGraph()
    assert graph.edges == []
    assert graph.traversed_edges == []
    assert graph.entry_point is None


def test_traversed_edges_separates_taken_from_declared() -> None:
    """The critic -> writer loopback exists in the topology but was not taken
    in the reference trace; conflating the two would invent a handoff."""
    graph = CommunicationGraph(
        entry_point="orchestrator_agent",
        terminal_agents=["db_saver_agent"],
        edges=[
            CommEdge(source="writer_agent", target="critic_agent", traversed=True),
            CommEdge(source="critic_agent", target="writer_agent", traversed=False),
            CommEdge(source="critic_agent", target="db_saver_agent"),
        ],
    )
    assert [(e.source, e.target) for e in graph.traversed_edges] == [
        ("writer_agent", "critic_agent")
    ]


def test_comm_edge_type_stays_free_form() -> None:
    """MASEF leaves edge_type open, so Atlas must not reject unseen values."""
    assert CommEdge(source="a", target="b", edge_type="loopback").edge_type == "loopback"


def test_comm_edge_allows_self_loop() -> None:
    """A retry-on-self transition is legitimate topology, unlike a graph edge."""
    assert CommEdge(source="critic_agent", target="critic_agent").source == "critic_agent"


# ── Failure ───────────────────────────────────────────────────────────


def test_failure_requires_evidence() -> None:
    with pytest.raises(ValidationError, match="at least 1 item"):
        Failure(node_id="s1", kind=FailureKind.TIMEOUT, evidence=[])


def test_failure_records_its_justification() -> None:
    failure = Failure(
        node_id="tool_x",
        kind=FailureKind.TIMEOUT,
        message="Read timed out after 30s",
        evidence=["span status == ERROR", "status_message matches /timed out/"],
    )
    assert len(failure.evidence) == 2
    assert str(failure) == "timeout at tool_x"


def test_failure_is_immutable() -> None:
    failure = Failure(node_id="s1", kind=FailureKind.UNKNOWN, evidence=["x"])
    with pytest.raises(ValidationError):
        failure.kind = FailureKind.TIMEOUT


def test_failure_kind_accepts_wire_value() -> None:
    failure = Failure(node_id="s1", kind="error_status", evidence=["x"])
    assert failure.kind is FailureKind.ERROR_STATUS
