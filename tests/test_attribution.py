"""Agent attribution.

The precedence order is a contract with MASEF (ADR-007), so each step is pinned
individually: a reordering would make Atlas and MASEF blame different agents for
the same span, which is worse than either being wrong alone.
"""

from __future__ import annotations

import json
from typing import Any

from atlas.ingestion import (
    build_node_agent_map,
    canonicalize,
    resolve_all,
    resolve_direct,
)
from atlas.models import AgentSource

REGISTRY = [
    {"agent_name": "writer_agent", "node_name": "writer"},
    {"agent_name": "critic_agent", "node_name": "critic"},
]
NODE_MAP = build_node_agent_map(REGISTRY)


def span(span_id: str = "s1", **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "span_id": span_id,
        "parent_span_id": None,
        "name": "GenerateContent",
        "openinference": {},
        "attributes": {},
    }
    base.update(overrides)
    return base


def metadata_attr(node: str) -> dict[str, Any]:
    """`attributes.metadata` as MASEF emits it: a JSON *string*, not an object."""
    return {"metadata": json.dumps({"langgraph_node": node, "langgraph_step": 3})}


# --- the join table --------------------------------------------------------


def test_node_name_maps_to_agent_name() -> None:
    assert NODE_MAP["writer"] == "writer_agent"


def test_agent_name_minus_suffix_is_also_a_key() -> None:
    """MASEF adds this short form; a span may be named either way."""
    assert build_node_agent_map([{"agent_name": "solo_agent"}]) == {"solo": "solo_agent"}


def test_declared_node_name_wins_over_the_derived_short_form() -> None:
    mapping = build_node_agent_map(
        [{"agent_name": "writer_agent", "node_name": "writer"}]
    )
    assert mapping["writer"] == "writer_agent"


def test_agents_without_an_agent_name_are_skipped() -> None:
    assert build_node_agent_map([{"node_name": "ghost"}, "not-a-dict"]) == {}


def test_unknown_names_pass_through_canonicalization() -> None:
    """An agent the registry never declared is an observation, not a defect."""
    assert canonicalize("mystery", NODE_MAP) == "mystery"


# --- precedence, step by step ---------------------------------------------


def test_step_1_openinference_agent_name() -> None:
    resolved = resolve_direct(
        span(openinference={"agent_name": "writer"}), NODE_MAP
    )
    assert resolved == ("writer_agent", AgentSource.OPENINFERENCE)


def test_step_2_attributes_agent_name() -> None:
    resolved = resolve_direct(span(attributes={"agent_name": "critic"}), NODE_MAP)
    assert resolved == ("critic_agent", AgentSource.ATTRIBUTES)


def test_step_2_accepts_the_dotted_openinference_spelling() -> None:
    resolved = resolve_direct(
        span(attributes={"openinference.agent.name": "critic"}), NODE_MAP
    )
    assert resolved == ("critic_agent", AgentSource.ATTRIBUTES)


def test_step_3_langgraph_node_from_metadata_json() -> None:
    resolved = resolve_direct(span(attributes=metadata_attr("writer")), NODE_MAP)
    assert resolved == ("writer_agent", AgentSource.LANGGRAPH_METADATA)


def test_step_3_strips_the_tools_suffix() -> None:
    """`writer_tools` is the writer's tool-call node, not a seventh agent."""
    resolved = resolve_direct(span(attributes=metadata_attr("writer_tools")), NODE_MAP)
    assert resolved == ("writer_agent", AgentSource.LANGGRAPH_METADATA)


def test_step_4_cross_link_map() -> None:
    resolved = resolve_direct(span("s9"), NODE_MAP, {"s9": "writer_agent"})
    assert resolved == ("writer_agent", AgentSource.CROSS_LINK)


def test_step_5_span_name_joined_to_node_name() -> None:
    resolved = resolve_direct(span(name="writer"), NODE_MAP)
    assert resolved == ("writer_agent", AgentSource.SPAN_NAME)


def test_step_5_span_named_after_the_agent_itself() -> None:
    resolved = resolve_direct(span(name="critic_agent"), NODE_MAP)
    assert resolved == ("critic_agent", AgentSource.SPAN_NAME)


def test_unresolvable_span_returns_none() -> None:
    assert resolve_direct(span(), NODE_MAP) is None


def test_earlier_steps_win_over_later_ones() -> None:
    """Ordering matters: a span can satisfy several steps at once."""
    contested = span(
        "s1",
        name="critic",
        openinference={"agent_name": "writer"},
        attributes=metadata_attr("critic"),
    )
    assert resolve_direct(contested, NODE_MAP, {"s1": "critic_agent"}) == (
        "writer_agent",
        AgentSource.OPENINFERENCE,
    )


# --- malformed input -------------------------------------------------------


def test_unparseable_metadata_falls_through_instead_of_raising() -> None:
    resolved = resolve_direct(
        span(name="writer", attributes={"metadata": "{not json"}), NODE_MAP
    )
    assert resolved == ("writer_agent", AgentSource.SPAN_NAME)


def test_metadata_without_langgraph_node_falls_through() -> None:
    resolved = resolve_direct(
        span(name="writer", attributes={"metadata": json.dumps({"thread_id": "t1"})}),
        NODE_MAP,
    )
    assert resolved == ("writer_agent", AgentSource.SPAN_NAME)


def test_metadata_that_is_not_an_object_falls_through() -> None:
    resolved = resolve_direct(
        span(name="writer", attributes={"metadata": "[1, 2]"}), NODE_MAP
    )
    assert resolved == ("writer_agent", AgentSource.SPAN_NAME)


def test_non_dict_openinference_and_attributes_are_tolerated() -> None:
    resolved = resolve_direct(
        span(name="writer", openinference="junk", attributes=None), NODE_MAP
    )
    assert resolved == ("writer_agent", AgentSource.SPAN_NAME)


# --- ancestor inheritance --------------------------------------------------


def test_child_inherits_the_nearest_resolving_ancestor() -> None:
    spans = [
        span("root", name="writer"),
        span("mid", parent_span_id="root", name="__pregel_pull"),
        span("leaf", parent_span_id="mid", name="GenerateContent"),
    ]
    resolved = resolve_all(spans, NODE_MAP)
    assert resolved["root"] == ("writer_agent", AgentSource.SPAN_NAME)
    assert resolved["mid"] == ("writer_agent", AgentSource.ANCESTOR)
    assert resolved["leaf"] == ("writer_agent", AgentSource.ANCESTOR)


def test_inheritance_skips_past_unresolvable_intermediates() -> None:
    """Two framework-internal spans between the agent and its LLM call."""
    spans = [
        span("root", name="critic"),
        span("a", parent_span_id="root", name="__pregel_pull"),
        span("b", parent_span_id="a", name="__pregel_push"),
        span("leaf", parent_span_id="b", name="GenerateContent"),
    ]
    assert resolve_all(spans, NODE_MAP)["leaf"] == (
        "critic_agent",
        AgentSource.ANCESTOR,
    )


def test_nearest_ancestor_wins_not_the_furthest() -> None:
    spans = [
        span("root", name="writer"),
        span("mid", parent_span_id="root", name="critic"),
        span("leaf", parent_span_id="mid", name="GenerateContent"),
    ]
    assert resolve_all(spans, NODE_MAP)["leaf"][0] == "critic_agent"


def test_a_direct_resolution_is_never_overwritten_by_an_ancestor() -> None:
    spans = [
        span("root", name="writer"),
        span("child", parent_span_id="root", name="critic"),
    ]
    assert resolve_all(spans, NODE_MAP)["child"] == (
        "critic_agent",
        AgentSource.SPAN_NAME,
    )


def test_unattributable_spans_are_absent_rather_than_guessed() -> None:
    """Assigning an orphan to the root's agent would fabricate attribution."""
    spans = [span("root", name="workflow"), span("child", parent_span_id="root")]
    assert resolve_all(spans, NODE_MAP) == {}


def test_dangling_parent_pointer_stops_the_walk() -> None:
    """Runs before Run validation, so it cannot assume parents resolve."""
    assert resolve_all([span("leaf", parent_span_id="ghost")], NODE_MAP) == {}


def test_cyclic_parent_chain_terminates() -> None:
    """Also runs before the acyclicity guarantee exists."""
    spans = [
        span("a", parent_span_id="b"),
        span("b", parent_span_id="a"),
    ]
    assert resolve_all(spans, NODE_MAP) == {}


def test_self_parenting_span_terminates() -> None:
    assert resolve_all([span("a", parent_span_id="a")], NODE_MAP) == {}


def test_spans_without_ids_are_ignored() -> None:
    assert resolve_all([span(""), span("ok", name="writer")], NODE_MAP) == {
        "ok": ("writer_agent", AgentSource.SPAN_NAME)
    }
