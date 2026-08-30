"""The analysis pipeline: one call from ``Run`` to every Phase 2-5 result.

This is the seam the CLI, the API and the LLM query layer all sit on, so
those surfaces stay serialization and never re-implement analysis.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from atlas.analysis.failures import FailureReport, Verdict, analyze_failures
from atlas.analysis.retries import RetryWasteReport, apply_retries, retry_waste
from atlas.analysis.root_cause import RootCauseReport, analyze_root_causes
from atlas.graph import ExecutionGraph, build_execution_graph
from atlas.models import Run


class RunSummary(BaseModel):
    """The run-list view of a run: identity, outcome, scale."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    status: str = Field(
        description="'ok' (nothing failed), 'degraded' (failures detected but "
        "the run still produced a final output), or 'failed' (failures and no "
        "final output)."
    )
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: float | None = None
    node_count: int
    agent_count: int
    failure_count: int
    retry_wasted_ms: float
    input_query: str | None = None


class RunAnalysis(BaseModel):
    """Everything Atlas concludes about one run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: RunSummary
    agents: dict[str, list[str]] = Field(
        description="Agent -> owning span ids (the rollup that does not "
        "double-count LangGraph duplicate spans)."
    )
    unjoined_handoffs: list[str] = Field(
        description="Traversed communication edges that could not be joined to "
        "spans -- the honest coverage statement for the dataflow signal "
        "(ADR-009)."
    )
    retry_waste: RetryWasteReport
    failures: FailureReport
    root_causes: RootCauseReport


def _summary(run: Run, failure_count: int, wasted_ms: float) -> RunSummary:
    if failure_count == 0:
        status = "ok"
    elif run.final_output is None:
        status = "failed"
    else:
        status = "degraded"
    return RunSummary(
        run_id=run.id,
        status=status,
        started_at=run.started_at.isoformat() if run.started_at else None,
        ended_at=run.ended_at.isoformat() if run.ended_at else None,
        duration_ms=run.duration_ms,
        node_count=len(run.nodes),
        agent_count=len({node.agent for node in run.nodes if node.agent}),
        failure_count=failure_count,
        retry_wasted_ms=wasted_ms,
        input_query=run.input_query,
    )


def analyze_run(run: Run, *, verdicts: list[Verdict] | None = None) -> RunAnalysis:
    """Run the full deterministic pipeline over one run.

    The verdicts, when supplied, seed failure detection with an evaluator's
    judgments (ADR-008); everything else is derived from the trace alone.
    """
    annotated = apply_retries(run)
    graph: ExecutionGraph = build_execution_graph(annotated)

    failures = analyze_failures(annotated, verdicts=verdicts, graph=graph)
    root_causes = analyze_root_causes(annotated, failure_report=failures, graph=graph)
    waste = retry_waste(annotated)

    return RunAnalysis(
        summary=_summary(run, len(failures.failures), waste.total_wasted_ms),
        agents=graph.agents(),
        unjoined_handoffs=graph.unjoined_handoffs,
        retry_waste=waste,
        failures=failures,
        root_causes=root_causes,
    )
