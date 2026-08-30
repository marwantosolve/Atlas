"""Run-level graph integrity rules.

These are the checks that make Phase 2 safe to write: once a `Run` exists, the
nodes are known to be uniquely identified, fully resolvable and acyclic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from atlas.models import SpanStatus

from .conftest import make_node, make_run


def test_valid_run() -> None:
    run = make_run()
    assert [node.id for node in run.roots] == ["s1"]
    assert run.communication.edges == []


def test_empty_node_list_rejected() -> None:
    with pytest.raises(ValidationError, match="at least 1 item"):
        make_run(nodes=[])


def test_empty_id_rejected() -> None:
    with pytest.raises(ValidationError, match="at least 1 character"):
        make_run(id="")


def test_duplicate_node_id_rejected_and_names_the_id() -> None:
    nodes = [make_node("s1"), make_node("s1", name="other")]
    with pytest.raises(ValidationError, match=r"nodes\[1\].id 's1' is a duplicate"):
        make_run(nodes=nodes)


def test_dangling_parent_rejected_and_names_the_parent() -> None:
    nodes = [make_node("s1"), make_node("s2", parent_id="ghost")]
    with pytest.raises(ValidationError, match="parent_id 'ghost'"):
        make_run(nodes=nodes)


def test_dangling_retry_of_rejected() -> None:
    nodes = [make_node("s1"), make_node("s2", parent_id="s1", retry_of="ghost")]
    with pytest.raises(ValidationError, match="retry_of 'ghost'"):
        make_run(nodes=nodes)


def test_parent_cycle_rejected_and_shows_the_cycle() -> None:
    """No node here is a root, which is exactly how a cycle presents."""
    nodes = [
        make_node("a", parent_id="b"),
        make_node("b", parent_id="c"),
        make_node("c", parent_id="a"),
    ]
    with pytest.raises(ValidationError, match="parent_id cycle detected"):
        make_run(nodes=nodes)


def test_two_node_cycle_rejected() -> None:
    nodes = [make_node("a", parent_id="b"), make_node("b", parent_id="a")]
    with pytest.raises(ValidationError, match="parent_id cycle detected"):
        make_run(nodes=nodes)


def test_children_may_be_listed_before_parents() -> None:
    """The reference MASEF trace does exactly this, so order must not matter."""
    nodes = [
        make_node("child", parent_id="parent"),
        make_node("grandchild", parent_id="child"),
        make_node("parent"),
    ]
    run = make_run(nodes=nodes)
    assert [node.id for node in run.roots] == ["parent"]


def test_multiple_roots_allowed() -> None:
    """A trace may hold disconnected fragments; rejecting them would lose data."""
    run = make_run(nodes=[make_node("a"), make_node("b")])
    assert len(run.roots) == 2


def test_deep_chain_does_not_false_positive_as_a_cycle() -> None:
    nodes = [make_node("n0")]
    nodes += [make_node(f"n{i}", parent_id=f"n{i - 1}") for i in range(1, 200)]
    assert len(make_run(nodes=nodes).nodes) == 200


def test_shared_ancestor_visited_once() -> None:
    """Two branches over one root exercise the already-visited path in the
    cycle detector."""
    nodes = [
        make_node("root"),
        make_node("a", parent_id="root"),
        make_node("b", parent_id="root"),
        make_node("a1", parent_id="a"),
    ]
    assert len(make_run(nodes=nodes).roots) == 1


def test_node_index_keys_by_id() -> None:
    run = make_run(nodes=[make_node("a"), make_node("b")])
    assert set(run.node_index) == {"a", "b"}
    assert run.node_index["a"].id == "a"


def test_failed_nodes_filters_error_status() -> None:
    nodes = [
        make_node("ok"),
        make_node("bad", status=SpanStatus.ERROR),
        make_node("unset", status=SpanStatus.UNSET),
    ]
    assert [node.id for node in make_run(nodes=nodes).failed_nodes] == ["bad"]


def test_run_duration_derived_when_absent() -> None:
    run = make_run(
        started_at=datetime(2026, 4, 17, 0, 0, 0),
        ended_at=datetime(2026, 4, 17, 0, 0, 30),
    )
    assert run.duration_ms == pytest.approx(30_000.0)


def test_run_duration_not_inferred_from_node_extents() -> None:
    """A run whose own timestamps are absent has no duration, even though its
    nodes span ten seconds. Deriving one would be latency analysis, which
    MASEF owns -- and would invent a number the trace never stated."""
    run = make_run()
    assert run.nodes[0].duration_ms == pytest.approx(10_000.0)
    assert run.duration_ms is None


def test_supplied_run_duration_preserved() -> None:
    run = make_run(
        started_at=datetime(2026, 4, 17, 0, 0, 0),
        ended_at=datetime(2026, 4, 17, 0, 0, 30),
        duration_ms=12_345.0,
    )
    assert run.duration_ms == 12_345.0


def test_negative_run_duration_rejected() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        make_run(duration_ms=-1)


def test_run_timestamps_normalized_to_naive_utc() -> None:
    run = make_run(
        started_at=datetime(2026, 4, 17, 14, 0, 0, tzinfo=timezone(timedelta(hours=2))),
    )
    assert run.started_at == datetime(2026, 4, 17, 12, 0, 0)


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        make_run(sessions=[])


def test_metadata_and_provenance_round_trip() -> None:
    run = make_run(
        trace_id="tr_1",
        schema_version="1.0",
        input_query="what is O-RAN?",
        final_output="a report",
        metadata={"framework": "langgraph"},
    )
    assert run.metadata["framework"] == "langgraph"
    assert run.schema_version == "1.0"
