"""Retry detection and retry-waste attribution (Phase 3)."""

from __future__ import annotations

from datetime import datetime, timedelta

from atlas.analysis import apply_retries, retry_groups, retry_waste
from atlas.graph import build_execution_graph
from atlas.ingestion import load_trace
from atlas.models import EdgeType, SpanKind, SpanStatus
from tests.conftest import EXAMPLES, make_node, make_run

_T0 = datetime(2026, 4, 17, 0, 34, 2)


def _attempt(
    node_id: str,
    *,
    start: datetime,
    seconds: float = 1.0,
    status: SpanStatus = SpanStatus.ERROR,
    parent_id: str = "root",
    agent: str | None = "a_agent",
    name: str = "lookup",
    tool: str | None = "lookup",
    cost_usd: float | None = None,
):
    return make_node(
        node_id,
        name=name,
        kind=SpanKind.TOOL,
        status=status,
        parent_id=parent_id,
        agent=agent,
        started_at=start,
        ended_at=start + timedelta(seconds=seconds),
        tool=tool,
        cost_usd=cost_usd,
    )


# ── detection on the bundled refund trace ─────────────────────────────


def test_refund_trace_has_one_exhausted_group() -> None:
    run = load_trace(EXAMPLES / "refund_run.json")[0]
    groups = retry_groups(run)
    assert len(groups) == 1
    group = groups[0]
    assert group.operation == "lookup_customer"
    assert group.agent == "crm_agent"
    assert group.attempt_ids == ["18293a4b5c6d7e8f", "293a4b5c6d7e8f90"]
    assert group.exhausted is True
    assert group.superseded_ids == ["18293a4b5c6d7e8f"]


def test_refund_trace_waste_is_the_superseded_attempt() -> None:
    """Attempt 1 (5 s, failed, replaced) is waste; attempt 2 (5 s, failed,
    final) is a failure, not waste -- see ADR-003."""
    run = load_trace(EXAMPLES / "refund_run.json")[0]
    report = retry_waste(run)
    assert report.total_wasted_ms == 5000.0
    assert report.total_wasted_cost_usd is None  # the trace carries no cost
    assert report.by_agent_ms == {"crm_agent": 5000.0}
    assert len(report.groups) == 1
    assert report.groups[0].superseded_ids == ["18293a4b5c6d7e8f"]


def test_apply_retries_feeds_the_graph() -> None:
    """The pipeline that produces RETRY edges: apply detection's verdicts,
    then rebuild the graph."""
    run = load_trace(EXAMPLES / "refund_run.json")[0]
    annotated = apply_retries(run)
    by_id = annotated.node_index
    assert by_id["18293a4b5c6d7e8f"].attempt == 1
    assert by_id["18293a4b5c6d7e8f"].retry_of is None
    assert by_id["293a4b5c6d7e8f90"].attempt == 2
    assert by_id["293a4b5c6d7e8f90"].retry_of == "18293a4b5c6d7e8f"

    graph = build_execution_graph(annotated)
    retries = graph.edges_of_type(EdgeType.RETRY)
    assert [(e.source, e.target) for e in retries] == [
        ("18293a4b5c6d7e8f", "293a4b5c6d7e8f90")
    ]

    # The ingested run is untouched: it still says exactly what the trace said.
    assert run.node_index["293a4b5c6d7e8f90"].retry_of is None
    assert build_execution_graph(run).edges_of_type(EdgeType.RETRY) == []


# ── the chaining rule, condition by condition ─────────────────────────


def test_recovered_chain_is_not_exhausted() -> None:
    a1 = _attempt("t1", start=_T0)
    a2 = _attempt("t2", start=_T0 + timedelta(seconds=2), status=SpanStatus.OK)
    groups = retry_groups(make_run([make_node("root"), a1, a2]))
    assert len(groups) == 1
    assert groups[0].exhausted is False
    assert groups[0].superseded_ids == ["t1"]


def test_three_attempt_chain() -> None:
    a1 = _attempt("t1", start=_T0)
    a2 = _attempt("t2", start=_T0 + timedelta(seconds=2))
    a3 = _attempt("t3", start=_T0 + timedelta(seconds=4), status=SpanStatus.OK)
    groups = retry_groups(make_run([make_node("root"), a1, a2, a3]))
    assert groups[0].attempt_ids == ["t1", "t2", "t3"]
    assert groups[0].superseded_ids == ["t1", "t2"]


def test_successful_call_then_failure_is_not_a_retry() -> None:
    """Two invocations where the first succeeded: the second is a new call,
    not a retry -- the trace gives no evidence it replaced anything."""
    a1 = _attempt("t1", start=_T0, status=SpanStatus.OK)
    a2 = _attempt("t2", start=_T0 + timedelta(seconds=2))
    assert retry_groups(make_run([make_node("root"), a1, a2])) == []


def test_overlapping_attempts_are_parallel_calls() -> None:
    a1 = _attempt("t1", start=_T0, seconds=3.0)
    a2 = _attempt("t2", start=_T0 + timedelta(seconds=1))
    assert retry_groups(make_run([make_node("root"), a1, a2])) == []


def test_different_parents_do_not_group() -> None:
    a1 = _attempt("t1", start=_T0, parent_id="p1")
    a2 = _attempt("t2", start=_T0 + timedelta(seconds=2), parent_id="p2")
    run = make_run(
        [
            make_node("root"),
            make_node("p1", parent_id="root"),
            make_node("p2", parent_id="root"),
            a1,
            a2,
        ]
    )
    assert retry_groups(run) == []


def test_different_agents_do_not_group() -> None:
    """Two agents calling the same tool under a shared parent (an
    orchestrator's tool fan-out, say) are not each other's retries."""
    a1 = _attempt("t1", start=_T0, agent="a_agent")
    a2 = _attempt("t2", start=_T0 + timedelta(seconds=2), agent="b_agent")
    assert retry_groups(make_run([make_node("root"), a1, a2])) == []


def test_missing_timestamps_do_not_chain() -> None:
    """Without ordering evidence there is no retry claim to make."""
    a1 = _attempt("t1", start=_T0)
    a2 = make_node(
        "t2",
        name="lookup",
        kind=SpanKind.TOOL,
        status=SpanStatus.ERROR,
        parent_id="root",
        agent="a_agent",
        tool="lookup",
        retry_of=None,
        started_at=None,
        ended_at=None,
    )
    assert retry_groups(make_run([make_node("root"), a1, a2])) == []


def test_operation_identity_is_tool_else_name() -> None:
    """A tool wrapper and an inner span share the tool name; a span with no
    tool falls back to its name."""
    wrapper = _attempt("t1", start=_T0, name="tool:lookup", tool="lookup")
    inner = _attempt("t2", start=_T0 + timedelta(seconds=2), name="lookup", tool=None)
    groups = retry_groups(make_run([make_node("root"), wrapper, inner]))
    assert len(groups) == 1
    assert groups[0].operation == "lookup"


# ── waste arithmetic ──────────────────────────────────────────────────


def test_waste_sums_carried_cost_when_present() -> None:
    a1 = _attempt("t1", start=_T0, seconds=2.0, cost_usd=0.01)
    a2 = _attempt("t2", start=_T0 + timedelta(seconds=3), seconds=2.0, cost_usd=0.02)
    a3 = _attempt(
        "t3", start=_T0 + timedelta(seconds=6), status=SpanStatus.OK, cost_usd=0.02
    )
    report = retry_waste(make_run([make_node("root"), a1, a2, a3]))
    # Only the superseded attempts count: 0.01 + 0.02, never the final's.
    assert report.total_wasted_cost_usd == 0.03
    assert report.total_wasted_ms == 4000.0
    assert report.groups[0].wasted_cost_usd == 0.03


def test_waste_cost_none_when_trace_carries_none() -> None:
    a1 = _attempt("t1", start=_T0)
    a2 = _attempt("t2", start=_T0 + timedelta(seconds=2), status=SpanStatus.OK)
    report = retry_waste(make_run([make_node("root"), a1, a2]))
    assert report.total_wasted_cost_usd is None
    assert report.groups[0].wasted_cost_usd is None


def test_empty_report_when_no_retries() -> None:
    report = retry_waste(make_run())
    assert report.groups == []
    assert report.total_wasted_ms == 0.0
    assert report.total_wasted_cost_usd is None
    assert report.by_agent_ms == {}


def test_unattributed_group_keys_under_marker() -> None:
    a1 = _attempt("t1", start=_T0, agent=None)
    a2 = _attempt("t2", start=_T0 + timedelta(seconds=2), agent=None)
    report = retry_waste(make_run([make_node("root"), a1, a2]))
    assert report.by_agent_ms == {"<unattributed>": 1000.0}
