"""Failure detection, propagation and blast radius (Phase 4)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from atlas.analysis import (
    Severity,
    Verdict,
    analyze_failures,
    blast_radius,
    detect_failures,
)
from atlas.ingestion import load_trace
from atlas.models import (
    CommEdge,
    CommunicationGraph,
    EdgeType,
    FailureKind,
    SpanKind,
    SpanStatus,
)
from tests.conftest import EXAMPLES, make_node, make_run

_T0 = datetime(2026, 4, 17, 0, 34, 2)

CRM_TOOL_2 = "293a4b5c6d7e8f90"
CRM_TOOL_1 = "18293a4b5c6d7e8f"
CRM_SPAN = "0718293a4b5c6d7e"
CRM_LLM = "3a4b5c6d7e8f9012"
WRITER_SPAN = "4b5c6d7e8f901324"
WRITER_LLM = "5c6d7e8f90132456"


@pytest.fixture
def refund_run():
    return load_trace(EXAMPLES / "refund_run.json")[0]


# ── detection ─────────────────────────────────────────────────────────


def test_refund_trace_detects_exhausted_retry_chain(refund_run) -> None:
    failures = detect_failures(refund_run)
    assert len(failures) == 1
    failure = failures[0]
    assert failure.node_id == CRM_TOOL_2
    assert failure.kind is FailureKind.EXHAUSTED_RETRIES
    # The evidence names the whole chain, not just the final attempt.
    assert any(CRM_TOOL_1 in line for line in failure.evidence)
    assert any(CRM_TOOL_2 in line for line in failure.evidence)


def test_superseded_attempt_is_waste_not_failure(refund_run) -> None:
    """Attempt 1 failed but was replaced: it shaped no outcome, so it must not
    appear as a failure (that is retry waste, Phase 3)."""
    failures = detect_failures(refund_run)
    assert all(failure.node_id != CRM_TOOL_1 for failure in failures)


def test_timeout_signature_is_classified() -> None:
    node = make_node(
        "t1",
        name="lookup",
        kind=SpanKind.TOOL,
        status=SpanStatus.ERROR,
        status_message="CRM API connection timed out after 5000 ms",
    )
    failure = detect_failures(make_run([node]))[0]
    assert failure.kind is FailureKind.TIMEOUT


def test_plain_error_is_error_status() -> None:
    node = make_node(
        "t1",
        name="lookup",
        kind=SpanKind.TOOL,
        status=SpanStatus.ERROR,
        status_message="connection refused",
    )
    failure = detect_failures(make_run([node]))[0]
    assert failure.kind is FailureKind.ERROR_STATUS


def test_missing_output_on_completed_llm_is_suspected() -> None:
    node = make_node(
        "l1", name="llm", kind=SpanKind.LLM, status=SpanStatus.OK, output_value=None
    )
    failure = detect_failures(make_run([node]))[0]
    assert failure.kind is FailureKind.MISSING_OUTPUT
    assert "output_value is absent" in failure.evidence


def test_clean_run_detects_nothing() -> None:
    run = load_trace(EXAMPLES / "minimal_run.json")[0]
    assert detect_failures(run) == []


def test_seeded_verdict_localizes_a_semantic_failure(refund_run) -> None:
    """ADR-008's core case: the run has no ERROR spans a judge would care
    about beyond the structural one -- a verdict seeds a *semantic* failure
    on a span whose status is OK, and Atlas localizes it."""
    ok_span = "f60708192a3b4c5d"  # policy_analyst's LLM call, status OK
    verdicts = [
        Verdict(
            node_id=ok_span,
            kind=FailureKind.UNKNOWN,
            message="Policy summary contradicts the retrieved policy text",
            source="masef",
        )
    ]
    failures = detect_failures(refund_run, verdicts=verdicts)
    assert [f.node_id for f in failures] == [ok_span, CRM_TOOL_2]
    seeded = failures[0]
    assert seeded.kind is FailureKind.UNKNOWN
    assert any("masef" in line for line in seeded.evidence)


def test_verdict_for_unknown_node_is_rejected(refund_run) -> None:
    with pytest.raises(ValueError, match="must anchor to a span"):
        detect_failures(refund_run, verdicts=[Verdict(node_id="ghost", kind=FailureKind.UNKNOWN)])


# ── propagation and blast radius ──────────────────────────────────────


def test_refund_blast_radius_covers_crm_and_writer(refund_run) -> None:
    report = analyze_failures(refund_run)
    assert len(report.radii) == 1
    radius = report.radii[0]

    by_severity: dict[Severity, list[str]] = {}
    for entry in radius.affected:
        by_severity.setdefault(entry.severity, []).append(entry.node_id)

    assert by_severity[Severity.FAILED] == [CRM_TOOL_2]
    # Contaminated: the rest of crm's region, plus the writer that consumed
    # crm's degraded output over the traversed handoff, plus what writer
    # invoked. The policy branch stays clean.
    assert sorted(by_severity[Severity.CONTAMINATED]) == sorted(
        [CRM_TOOL_1, CRM_LLM, WRITER_SPAN, WRITER_LLM]
    )
    assert not by_severity.get(Severity.AT_RISK)
    assert radius.agent_severities == {
        "crm_agent": Severity.FAILED,
        "writer_agent": Severity.CONTAMINATED,
    }


def test_handoff_hop_records_its_edge_type(refund_run) -> None:
    report = analyze_failures(refund_run)
    entry = next(
        e for e in report.radii[0].affected if e.node_id == WRITER_SPAN
    )
    assert entry.via == [EdgeType.HANDOFF]
    assert entry.agent == "writer_agent"
    # Inherited attribution is visible, per ADR-007's provenance requirement.
    llm_entry = next(e for e in report.radii[0].affected if e.node_id == CRM_LLM)
    assert llm_entry.agent_source is not None


def test_policy_branch_stays_clean(refund_run) -> None:
    """Plan §17 case 6: the unaffected branch is not dragged into the blast
    radius just because it shares the root span."""
    report = analyze_failures(refund_run)
    affected_ids = {e.node_id for e in report.radii[0].affected}
    assert "e5f60708192a3b4c" not in affected_ids  # policy tool
    assert "d4e5f60708192a3b" not in affected_ids  # policy_analyst span


def _three_agent_run():
    """a_agent fails and hands off to b_agent (traversed); b_agent could also
    have handed off to c_agent but the run never took that edge."""
    a_span = make_node("a", name="a", agent="a_agent")
    a_tool = make_node(
        "at",
        name="tool",
        kind=SpanKind.TOOL,
        status=SpanStatus.ERROR,
        parent_id="a",
        agent="a_agent",
        started_at=_T0 + timedelta(seconds=1),
        ended_at=_T0 + timedelta(seconds=2),
    )
    b_span = make_node(
        "b",
        name="b",
        agent="b_agent",
        started_at=_T0 + timedelta(seconds=3),
        ended_at=_T0 + timedelta(seconds=4),
    )
    c_span = make_node(
        "c",
        name="c",
        agent="c_agent",
        started_at=_T0 + timedelta(seconds=5),
        ended_at=_T0 + timedelta(seconds=6),
    )
    communication = CommunicationGraph(
        entry_point="a_agent",
        edges=[
            CommEdge(source="a_agent", target="b_agent", traversed=True),
            CommEdge(source="b_agent", target="c_agent", traversed=False),
        ],
    )
    return make_run([a_span, a_tool, b_span, c_span], communication=communication)


def test_at_risk_over_declared_but_untaken_edge() -> None:
    report = analyze_failures(_three_agent_run())
    radius = report.radii[0]
    by_id = {e.node_id: e for e in radius.affected}
    assert by_id["at"].severity is Severity.FAILED
    assert by_id["b"].severity is Severity.CONTAMINATED
    assert by_id["b"].via == [EdgeType.HANDOFF]
    # c_agent never consumed the bad output -- the topology says it could
    # have, so it is flagged, but only as at-risk.
    assert by_id["c"].severity is Severity.AT_RISK


def test_unattributed_failure_propagates_only_within_subtree() -> None:
    """A failure Atlas cannot attribute to an agent cannot cross agent
    boundaries either: no handoff edge starts from its spans."""
    root = make_node("root")
    tool = make_node(
        "t",
        name="tool",
        kind=SpanKind.TOOL,
        status=SpanStatus.ERROR,
        parent_id="root",
        agent=None,
        agent_source=None,
        started_at=_T0 + timedelta(seconds=1),
        ended_at=_T0 + timedelta(seconds=2),
    )
    child = make_node(
        "tc",
        name="child",
        parent_id="t",
        started_at=_T0 + timedelta(seconds=1, microseconds=500),
        ended_at=_T0 + timedelta(seconds=2),
    )
    run = make_run([root, tool, child])
    radius = blast_radius(run, detect_failures(run)[0])
    assert [(e.node_id, e.severity) for e in radius.affected] == [
        ("t", Severity.FAILED),
        ("tc", Severity.CONTAMINATED),
    ]


def test_missing_output_is_severity_capped() -> None:
    """A suspected failure may not claim contamination: it flags risk only."""
    node = make_node(
        "l1",
        name="llm",
        kind=SpanKind.LLM,
        status=SpanStatus.OK,
        output_value=None,
        agent="a_agent",
    )
    run = make_run([node])
    report = analyze_failures(run)
    assert all(
        entry.severity is Severity.AT_RISK for entry in report.radii[0].affected
    )


def test_failures_and_radii_stay_parallel(refund_run) -> None:
    report = analyze_failures(refund_run)
    assert [r.failure for r in report.radii] == report.failures
