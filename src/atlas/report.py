"""Text rendering of a :class:`RunAnalysis` -- what ``atlas analyze`` prints.

The report is the product demo: an engineer who has never seen Atlas should
read it once and understand what failed, where it started, and what it
contaminated. Every claim carries the span ids and evidence behind it, because
a diagnosis you cannot click through to is a rumor (plan §22.8).
"""

from __future__ import annotations

from atlas.analysis import RunAnalysis

_KIND_LABELS = {"failed": "FAILED", "contaminated": "CONTAMINATED", "at_risk": "AT RISK"}


def render_analysis(analysis: RunAnalysis) -> str:
    summary = analysis.summary
    lines: list[str] = []

    lines.append(f"Run {summary.run_id} -- {summary.status.upper()}")
    if summary.input_query:
        lines.append(f"  query: {_truncate(summary.input_query, 90)}")
    lines.append(
        f"  {summary.node_count} spans, {summary.agent_count} agents, "
        f"{_ms(summary.duration_ms)}"
        + (f", {summary.retry_wasted_ms:.0f} ms lost to retries" if summary.retry_wasted_ms else "")
    )
    lines.append("")

    if analysis.unjoined_handoffs:
        lines.append("Unjoined handoffs (dataflow coverage gaps):")
        for handoff in analysis.unjoined_handoffs:
            lines.append(f"  {handoff}")
        lines.append("")

    lines.append("Root causes (strongest first):")
    if not analysis.root_causes.candidates:
        lines.append("  none -- no failure was detected in this run")
    for candidate in analysis.root_causes.candidates:
        lines.extend(_render_candidate(candidate))
    lines.append("")

    lines.append("Failures:")
    if not analysis.failures.failures:
        lines.append("  none")
    for failure, radius in zip(analysis.failures.failures, analysis.failures.radii):
        lines.append(f"  {failure.kind.value} at {failure.node_id}")
        if failure.message:
            lines.append(f"    message: {_truncate(failure.message, 90)}")
        for line in failure.evidence:
            lines.append(f"    evidence: {_truncate(line, 100)}")
        lines.append("    blast radius:")
        for entry in radius.affected:
            via = (
                " via " + "+".join(edge.value for edge in entry.via)
                if entry.via
                else ""
            )
            agent = f" ({entry.agent})" if entry.agent else ""
            lines.append(
                f"      {_KIND_LABELS[entry.severity.value]} {entry.node_id}{agent}{via}"
            )
        lines.append("")

    if analysis.retry_waste.groups:
        lines.append("Retry waste:")
        for group in analysis.retry_waste.groups:
            agent = f" (agent {group.agent})" if group.agent else ""
            lines.append(
                f"  {group.operation}{agent}: {group.wasted_ms:.0f} ms wasted on "
                f"{len(group.superseded_ids)} superseded attempt(s)"
                + (
                    f", ${group.wasted_cost_usd:.4f} carried cost"
                    if group.wasted_cost_usd is not None
                    else ""
                )
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _render_candidate(candidate) -> list[str]:
    failure = candidate.failure
    out: list[str] = []
    marker = "" if candidate.is_root else "  [downstream of another failure]"
    out.append(
        f"  #{candidate.rank} {failure.kind.value} at {failure.node_id}"
        + (f" (agent {candidate.agent})" if candidate.agent else "")
        + marker
    )
    if failure.message:
        out.append(f"       message: {_truncate(failure.message, 90)}")
    path = " -> ".join(candidate.propagation_path)
    out.append(f"       propagation: {path}")
    for reason in candidate.reasons:
        out.append(f"       why: {_truncate(reason, 100)}")
    return out


def _ms(value: float | None) -> str:
    if value is None:
        return "duration unknown"
    return f"{value / 1000:.1f}s"


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"
