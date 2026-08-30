"""A detected failure.

Phase 0 defines the shape only. Detection lands in Phase 4 -- the point of
declaring it now is that `evidence` is not optional, which forces every later
detector to justify itself against observable trace facts (plan §22.8).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from atlas.models.enums import FailureKind


class Failure(BaseModel):
    """A failure observed at a specific node."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(min_length=1)
    kind: FailureKind
    message: str | None = Field(
        default=None,
        description="Operator-facing summary, usually the span's status_message.",
    )
    evidence: list[str] = Field(
        min_length=1,
        description=(
            "Observable facts supporting this classification, e.g. "
            "\"span status == ERROR\". Must never be empty: a failure Atlas "
            "cannot justify is a failure Atlas should not report."
        ),
    )

    def __str__(self) -> str:
        return f"{self.kind.value} at {self.node_id}"
