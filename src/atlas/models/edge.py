"""Edges of the reconstructed execution graph.

Edges are *derived*, not ingested: `Run` stores nodes only, and Phase 2's
graph reconstruction produces edges from them. Keeping derived data out of
the ingestion model is what stops the two from drifting apart.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from atlas.models.enums import EdgeType


class Edge(BaseModel):
    """A directed relationship between two nodes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    type: EdgeType

    condition: str | None = Field(
        default=None,
        description=(
            "Routing condition for HANDOFF edges, copied verbatim from the "
            "MASEF communication graph (e.g. 'Always (parallel fan-out)')."
        ),
    )

    @model_validator(mode="after")
    def _reject_self_loop(self) -> Edge:
        if self.source == self.target:
            raise ValueError(
                f"edge of type {self.type.value!r} points {self.source!r} at itself"
            )
        return self

    def __str__(self) -> str:
        return f"{self.source} -[{self.type.value}]-> {self.target}"
