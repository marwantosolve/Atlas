"""Root-cause localization (Phase 5) and the composed pipeline."""

from __future__ import annotations

from datetime import datetime, timedelta

from atlas.analysis import (
    Verdict,
    analyze_root_causes,
    analyze_run,
    detect_failures,
)
from atlas.ingestion import load_trace
from atlas.models import (
    CommEdge,
    CommunicationGraph,
    FailureKind,
    SpanKind,
    SpanStatus,
)
from tests.conftest import EXAMPLES, make_node, make_run

_T0 = datetime(2026, 4, 17, 0, 34, 2)
CRM_TOOL_2 = "293a4b5c6d7e8f90"
CRM_SPAN = "0718293a4b5c6d7e"
WRITER_SPAN = "4b5c6d7e8f901324"


# ── ranking on the bundled refund trace ───────────────────────────────


def test_refund_root_cause_is_the_crm_tool() -> None:
    """The canonical scenario: the root cause is the CRM tool whose retries
    were exhausted -- not the writer that merely consumed the degraded
    output, and not the run's final answer."""
    run = load_trace(EXAMPLES / "refund_run.json")[0]
    report = analyze_root_causes(run)
    primary = report.primary
    assert primary is not None
    assert primary.failure.node_id == CRM_TOOL_2
    assert primary.failure.kind is FailureKind.EXHAUSTED_RETRIES
    assert primary.is_root is True
    assert primary.agent == "crm_agent"
    assert primary.rank == 1
    assert primary.upstream_failure_ids == []
    assert primary.downstream_reach == 4
    # The propagation path names exact span ids: failure -> crm's owning
    # span -> the writer's owning span (plan §22.8).
    assert primary.propagation_path == [CRM_TOOL_2, CRM_SPAN, WRITER_SPAN]
    assert primary.reasons


def test_clean_run_reports_nothing() -> None:
    run = load_trace(EXAMPLES / "minimal_run.json")[0]
    report = analyze_root_causes(run)
    assert report.candidates == []
    assert report.primary is None


# ── a downstream failure is demoted ───────────────────────────────────


def _upstream_failure_run():
    """a_agent's tool fails, then b_agent's tool fails after consuming a's
    output over a traversed handoff. The second failure is propagation."""
    a_span = make_node("a", name="a", agent="a_agent")
    a_tool = make_node(
        "at",
        name="tool",
        kind=SpanKind.TOOL,
        status=SpanStatus.ERROR,
        status_message="connection refused",
        parent_id="a",
        agent="a_agent",
        started_at=_T0 + timedelta(seconds=1),
        ended_at=_T0 + timedelta(seconds=2),
    )
    b_span = make_node(
        "b", name="b", agent="b_agent", started_at=_T0 + timedelta(seconds=3)
    )
    b_tool = make_node(
        "bt",
        name="tool",
        kind=SpanKind.TOOL,
        status=SpanStatus.ERROR,
        status_message="connection refused",
        parent_id="b",
        agent="b_agent",
        started_at=_T0 + timedelta(seconds=4),
        ended_at=_T0 + timedelta(seconds=5),
    )
    communication = CommunicationGraph(
        entry_point="a_agent",
        edges=[CommEdge(source="a_agent", target="b_agent", traversed=True)],
    )
    return make_run([a_span, a_tool, b_span, b_tool], communication=communication)


def test_downstream_failure_is_not_the_root() -> None:
    report = analyze_root_causes(_upstream_failure_run())
    assert [c.failure.node_id for c in report.candidates] == ["at", "bt"]
    assert report.candidates[0].is_root is True
    downstream = report.candidates[1]
    assert downstream.is_root is False
    assert downstream.upstream_failure_ids == ["at"]
    assert "upstream failures ['at'] precede it" in downstream.reasons


# ── the composed pipeline ─────────────────────────────────────────────


def test_analyze_run_composes_every_phase() -> None:
    run = load_trace(EXAMPLES / "refund_run.json")[0]
    analysis = analyze_run(run)

    assert analysis.summary.run_id == "session_refund_001"
    assert analysis.summary.status == "degraded"  # failed tools, final output present
    assert analysis.summary.failure_count == 1
    assert analysis.summary.retry_wasted_ms == 5000.0
    assert analysis.summary.node_count == 12
    assert analysis.summary.agent_count == 4

    assert analysis.agents["crm_agent"] == [CRM_SPAN]
    assert analysis.unjoined_handoffs == []
    assert analysis.retry_waste.total_wasted_ms == 5000.0
    assert analysis.root_causes.primary is not None
    assert analysis.root_causes.primary.failure.node_id == CRM_TOOL_2

    # The analysis is serializable end to end -- the API layer depends on it.
    payload = analysis.model_dump()
    assert payload["summary"]["status"] == "degraded"
    assert payload["root_causes"]["candidates"][0]["failure"]["node_id"] == CRM_TOOL_2


def test_analyze_run_without_final_output_is_failed() -> None:
    run = load_trace(EXAMPLES / "refund_run.json")[0]
    stripped = run.model_copy(update={"final_output": None})
    analysis = analyze_run(stripped)
    assert analysis.summary.status == "failed"


def test_analyze_run_accepts_seeded_verdicts() -> None:
    run = load_trace(EXAMPLES / "refund_run.json")[0]
    analysis = analyze_run(
        run,
        verdicts=[
            Verdict(
                node_id="f60708192a3b4c5d",
                kind=FailureKind.UNKNOWN,
                message="Policy summary contradicts the policy text",
                source="masef",
            )
        ],
    )
    assert analysis.summary.failure_count == 2
    assert analysis.root_causes.candidates[0].failure.node_id == CRM_TOOL_2


def test_pipeline_leaves_the_ingested_run_untouched() -> None:
    run = load_trace(EXAMPLES / "refund_run.json")[0]
    analyze_run(run)
    assert all(node.retry_of is None for node in run.nodes)
    assert detect_failures(run)  # still works on the original
