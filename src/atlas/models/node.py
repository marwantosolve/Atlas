"""The Atlas execution node.

A node is one span. Atlas deliberately does *not* introduce a separate
"Event" entity alongside "Node" the way the original plan sketched in §8:
a MASEF span already carries identity, causality, timing and status, so a
second representation would only create two things to keep in sync.
See docs/decisions.md ADR-004.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from atlas.models.enums import AgentSource, SpanKind, SpanStatus

# Optional string fields where an empty string carries no more information
# than absence, and collapsing the two keeps downstream checks single-branch.
_BLANK_TO_NONE = (
    "status_message",
    "agent",
    "tool",
    "model",
    "input_value",
    "output_value",
)


def to_naive_utc(value: datetime) -> datetime:
    """Return ``value`` as a naive UTC datetime.

    MASEF traces emit naive local ISO-8601 (``2026-04-17T00:34:02.246968``),
    but adapters for other frameworks routinely emit offset-aware timestamps.
    Mixing the two raises ``TypeError`` on the first comparison, which would
    surface as a crash inside a latency calculation rather than at ingestion.
    Atlas therefore normalizes everything to one representation up front.
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


class Node(BaseModel):
    """A single unit of work within a run."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, description="Span id. Unique within a run.")
    parent_id: str | None = Field(
        default=None,
        description="Parent span id, or None for a root node.",
    )
    name: str = Field(min_length=1, description="Span name, e.g. 'search_web'.")

    kind: SpanKind = SpanKind.UNKNOWN
    status: SpanStatus = SpanStatus.UNSET
    status_message: str | None = None

    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: float | None = Field(default=None, ge=0)

    agent: str | None = Field(
        default=None,
        description=(
            "Owning agent, resolved during ingestion. Never read directly from "
            "openinference.agent_name -- that field is empty in every span of "
            "the reference MASEF trace."
        ),
    )
    agent_source: AgentSource | None = Field(
        default=None,
        description=(
            "How `agent` was resolved. None exactly when `agent` is None. "
            "Kept because an inherited attribution is weaker evidence than a "
            "declared one, and propagation claims must be able to say which "
            "they rest on."
        ),
    )
    tool: str | None = None
    model: str | None = None

    tokens_prompt: int | None = Field(default=None, ge=0)
    tokens_completion: int | None = Field(default=None, ge=0)
    tokens_total: int | None = Field(default=None, ge=0)

    cost_usd: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Carried through when a trace supplies it. Atlas never computes "
            "cost -- MASEF owns pricing (docs/decisions.md ADR-003)."
        ),
    )

    attempt: int = Field(default=1, ge=1, description="1 for a first try.")
    retry_of: str | None = Field(
        default=None,
        description="Id of the attempt this node retries, if any.",
    )

    input_value: str | None = None
    output_value: str | None = None

    raw: dict[str, Any] = Field(
        default_factory=dict,
        description="The original span, unmodified. Required by plan §22.7.",
    )

    @field_validator("started_at", "ended_at")
    @classmethod
    def _normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else to_naive_utc(value)

    @field_validator(*_BLANK_TO_NONE)
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value or None

    @model_validator(mode="after")
    def _check_consistency(self) -> Node:
        if self.parent_id == self.id:
            raise ValueError(f"node {self.id!r} lists itself as its parent")
        if self.retry_of == self.id:
            raise ValueError(f"node {self.id!r} lists itself as its retried attempt")

        # An attribution with no provenance cannot be audited, and a provenance
        # with no attribution describes nothing. Neither half is useful alone.
        if (self.agent is None) != (self.agent_source is None):
            raise ValueError(
                f"node {self.id!r} has agent={self.agent!r} but "
                f"agent_source={self.agent_source!r}; the two must be set or "
                f"unset together"
            )

        if (
            self.started_at is not None
            and self.ended_at is not None
            and self.ended_at < self.started_at
        ):
            raise ValueError(
                f"node {self.id!r} ends before it starts "
                f"({self.ended_at.isoformat()} < {self.started_at.isoformat()})"
            )

        # Derive duration only when the trace omitted it. A supplied value is
        # evidence and is never overwritten, even if it disagrees with the
        # timestamps -- see `duration_disagreement_ms`.
        if self.duration_ms is None and self.started_at and self.ended_at:
            delta = (self.ended_at - self.started_at).total_seconds() * 1000
            object.__setattr__(self, "duration_ms", delta)

        return self

    @property
    def failed(self) -> bool:
        """True when the span itself reported an error."""
        return self.status is SpanStatus.ERROR

    @property
    def is_root(self) -> bool:
        return self.parent_id is None

    @property
    def duration_disagreement_ms(self) -> float | None:
        """Gap between the reported duration and the timestamp span.

        A non-trivial gap means clock skew or a mis-instrumented exporter, and
        makes any latency attribution for this node suspect. Returns None when
        there is nothing to compare.
        """
        if self.duration_ms is None or not (self.started_at and self.ended_at):
            return None
        measured = (self.ended_at - self.started_at).total_seconds() * 1000
        return self.duration_ms - measured
