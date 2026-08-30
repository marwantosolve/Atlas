"""Retry detection and retry-waste attribution (Phase 3).

Retries are the one execution pattern a span tree records only implicitly: the
trace shows two ``lookup_customer`` spans under the same parent, and *somebody*
has to decide the second was a retry of the first rather than a second,
legitimate call. The rule here is deliberately narrow and deterministic:

a span is a retry of its predecessor when the two are

1. siblings under the same parent span,
2. owned by the same agent,
3. the same operation (tool name, else span name),
4. non-overlapping in time (the earlier ended before the later started), and
5. the earlier attempt reported ``ERROR``.

Every condition is observable in the trace; nothing is guessed. The price of
the narrow rule is recall -- a retry pattern it cannot see (overlapping
attempts, a framework that re-parents retries) is simply not reported -- and
that trade is the right one for a tool whose every claim has to be defensible
(docs/decisions.md ADR-002).

Retry *waste* is the one impact figure Atlas may claim (ADR-003): the duration
and carried cost of attempts that failed **and were superseded**. The final
failed attempt of an exhausted chain is a failure (Phase 4's
``EXHAUSTED_RETRIES``), not waste -- it produced no replacement, so nothing
about it was "wasted" relative to the run's outcome.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from atlas.models import Node, Run, SpanStatus

_UNATTRIBUTED = "<unattributed>"


def _sort_key(node: Node) -> tuple[datetime, str]:
    return (node.started_at or datetime.min, node.id)


def _operation(node: Node) -> str:
    """What the span is an attempt *of*: the tool when the trace names one,
    else the span name. Tool wrappers and their inner spans share a tool name,
    which is the identity a retry preserves."""
    return node.tool or node.name


def _failed(node: Node) -> bool:
    return node.status is SpanStatus.ERROR


def _supersedes(earlier: Node, later: Node) -> bool:
    """True when ``later`` can be a retry of ``earlier``.

    Overlap means the "attempts" ran concurrently and are therefore two
    parallel invocations, not a retry; missing timestamps mean ordering cannot
    be established, and guessing it is what this module exists not to do.
    """
    if not (earlier.ended_at and later.started_at):
        return False
    return earlier.ended_at <= later.started_at


class RetryGroup(BaseModel):
    """One operation's attempts, chained by the retry rule.

    ``attempt_ids`` is chronological. Every attempt except the last failed --
    that is an invariant of the chaining rule, not an observation -- so
    ``attempt_ids[:-1]`` is exactly the set of superseded attempts.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: str
    agent: str | None
    attempt_ids: list[str] = Field(min_length=2)
    exhausted: bool = Field(
        description="True when the final attempt also failed; the failure is "
        "then permanent, not recovered."
    )

    @property
    def first_attempt_id(self) -> str:
        return self.attempt_ids[0]

    @property
    def final_attempt_id(self) -> str:
        return self.attempt_ids[-1]

    @property
    def superseded_ids(self) -> list[str]:
        """Attempts that failed and were replaced: the waste, precisely."""
        return self.attempt_ids[:-1]


class GroupWaste(BaseModel):
    """Retry waste attributed to one group."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: str
    agent: str | None
    superseded_ids: list[str]
    wasted_ms: float = Field(ge=0)
    wasted_cost_usd: float | None = Field(
        default=None,
        ge=0,
        description="Sum of the superseded attempts' carried cost_usd. None "
        "when no superseded attempt carried one -- Atlas never computes cost "
        "(ADR-003), so absent input means absent output.",
    )


class RetryWasteReport(BaseModel):
    """Retry waste for a whole run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    groups: list[GroupWaste] = Field(
        description="One entry per retry group, earliest group first. Groups "
        "with no superseded attempt are absent by construction."
    )
    total_wasted_ms: float = Field(default=0.0, ge=0)
    total_wasted_cost_usd: float | None = Field(
        default=None,
        ge=0,
        description="None when no group could attribute cost, so the report "
        "never invents a dollar figure the trace did not carry.",
    )
    by_agent_ms: dict[str, float] = Field(
        default_factory=dict,
        description="Agent -> wasted milliseconds. Unattributed groups key "
        "under '<unattributed>'.",
    )


def retry_groups(run: Run) -> list[RetryGroup]:
    """Detect retry groups in ``run``, earliest group first."""
    # Parentless spans cannot be siblings of anything, and grouping by parent
    # is what keeps two agents calling the same tool from merging.
    siblings: dict[tuple[str | None, str | None, str], list[Node]] = {}
    for node in run.nodes:
        if node.parent_id is None:
            continue
        key = (node.parent_id, node.agent, _operation(node))
        siblings.setdefault(key, []).append(node)

    groups: list[tuple[Node, RetryGroup]] = []
    for nodes in siblings.values():
        nodes.sort(key=_sort_key)
        chain: list[Node] = []

        def flush() -> None:
            if len(chain) >= 2:
                groups.append(
                    (
                        chain[0],
                        RetryGroup(
                            operation=_operation(chain[0]),
                            agent=chain[0].agent,
                            attempt_ids=[node.id for node in chain],
                            exhausted=_failed(chain[-1]),
                        ),
                    )
                )

        for node in nodes:
            if chain and _failed(chain[-1]) and _supersedes(chain[-1], node):
                chain.append(node)
            else:
                flush()
                chain = [node]
        flush()

    groups.sort(key=lambda pair: _sort_key(pair[0]))
    return [group for _, group in groups]


def apply_retries(run: Run) -> Run:
    """Return a copy of ``run`` with ``attempt``/``retry_of`` set from detection.

    Detection is a read-only pass; this is the only place the detected groups
    are written back onto nodes, and it writes onto a *copy* so the ingested
    run keeps saying exactly what the trace said. The graph layer reads
    ``retry_of``, so ``build_execution_graph(apply_retries(run))`` is the
    pipeline that produces RETRY edges.
    """
    updates: dict[str, dict[str, Any]] = {}
    for group in retry_groups(run):
        for index, node_id in enumerate(group.attempt_ids):
            update: dict[str, Any] = {"attempt": index + 1}
            if index > 0:
                update["retry_of"] = group.attempt_ids[index - 1]
            updates[node_id] = update

    if not updates:
        return run

    nodes = [
        node.model_copy(update=updates.get(node.id, {})) for node in run.nodes
    ]
    return run.model_copy(update={"nodes": nodes})


def retry_waste(run: Run) -> RetryWasteReport:
    """Attribute retry waste across ``run``.

    Call this on the *ingested* run (with or without ``apply_retries``): it
    works from the same detection pass and does not read ``retry_of``.
    """
    by_id = run.node_index
    groups: list[GroupWaste] = []
    by_agent_ms: dict[str, float] = {}

    for group in retry_groups(run):
        superseded = [by_id[node_id] for node_id in group.superseded_ids]
        wasted_ms = sum(node.duration_ms or 0.0 for node in superseded)
        costs = [node.cost_usd for node in superseded if node.cost_usd is not None]
        wasted_cost = sum(costs) if costs else None

        groups.append(
            GroupWaste(
                operation=group.operation,
                agent=group.agent,
                superseded_ids=list(group.superseded_ids),
                wasted_ms=wasted_ms,
                wasted_cost_usd=wasted_cost,
            )
        )
        agent_key = group.agent or _UNATTRIBUTED
        by_agent_ms[agent_key] = by_agent_ms.get(agent_key, 0.0) + wasted_ms

    total_cost = sum(g.wasted_cost_usd for g in groups if g.wasted_cost_usd is not None)

    return RetryWasteReport(
        groups=groups,
        total_wasted_ms=sum(g.wasted_ms for g in groups),
        total_wasted_cost_usd=(
            total_cost if any(g.wasted_cost_usd is not None for g in groups) else None
        ),
        by_agent_ms=dict(sorted(by_agent_ms.items())),
    )
