"""The Atlas run: one execution of an agent system, normalized.

A `Run` is self-contained. MASEF keeps the agent/tool/communication registries
at trace level and shares them across sessions, but Atlas analyzes one run at
a time, so ingestion copies the registries onto each run rather than making
every analyzer carry a second object around.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from atlas.models.node import Node, to_naive_utc
from atlas.models.registry import AgentSpec, CommunicationGraph, ToolSpec


def _find_parent_cycle(nodes_by_id: dict[str, Node]) -> list[str] | None:
    """Return one parent-pointer cycle, or None if the nodes form a forest.

    Assumes every ``parent_id`` resolves -- callers validate that first.
    """
    done: set[str] = set()

    for start in nodes_by_id:
        if start in done:
            continue
        path: list[str] = []
        on_path: set[str] = set()
        current: str | None = start

        while current is not None and current not in done:
            if current in on_path:
                return path[path.index(current) :] + [current]
            path.append(current)
            on_path.add(current)
            current = nodes_by_id[current].parent_id

        done.update(path)

    return None


class Run(BaseModel):
    """One normalized execution, ready for graph reconstruction."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, description="MASEF session_id.")
    trace_id: str | None = None
    schema_version: str | None = Field(
        default=None,
        description="Version of the source trace spec, when the trace declared one.",
    )

    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: float | None = Field(default=None, ge=0)

    input_query: str | None = None
    final_output: str | None = None

    nodes: list[Node] = Field(
        min_length=1,
        description="Every span in the run. Order is not significant: real "
        "traces list children before parents.",
    )

    agents: list[AgentSpec] = Field(default_factory=list)
    tools: list[ToolSpec] = Field(default_factory=list)
    communication: CommunicationGraph = Field(default_factory=CommunicationGraph)

    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Source provenance (framework, execution_id, dataset, ...).",
    )

    @field_validator("started_at", "ended_at")
    @classmethod
    def _normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else to_naive_utc(value)

    @model_validator(mode="after")
    def _check_graph_integrity(self) -> Run:
        # Error messages name the offending path, following the MASEF trace
        # spec's rule that a validator must never emit a bare "invalid file".
        seen: set[str] = set()
        for index, node in enumerate(self.nodes):
            if node.id in seen:
                raise ValueError(
                    f"run {self.id!r}: nodes[{index}].id {node.id!r} is a duplicate; "
                    "span ids must be unique within a run"
                )
            seen.add(node.id)

        nodes_by_id = {node.id: node for node in self.nodes}

        for index, node in enumerate(self.nodes):
            if node.parent_id is not None and node.parent_id not in nodes_by_id:
                raise ValueError(
                    f"run {self.id!r}: nodes[{index}] ({node.id!r}) has "
                    f"parent_id {node.parent_id!r}, which is not present in this "
                    "run; the node cannot be placed in the call tree"
                )
            if node.retry_of is not None and node.retry_of not in nodes_by_id:
                raise ValueError(
                    f"run {self.id!r}: nodes[{index}] ({node.id!r}) has "
                    f"retry_of {node.retry_of!r}, which is not present in this run"
                )

        cycle = _find_parent_cycle(nodes_by_id)
        if cycle is not None:
            raise ValueError(
                f"run {self.id!r}: parent_id cycle detected: "
                f"{' -> '.join(cycle)}; the call tree must be acyclic"
            )

        if not any(node.is_root for node in self.nodes):  # pragma: no cover
            # Unreachable, and kept only as a tripwire: `nodes` is non-empty,
            # every parent_id resolves, and the parent pointers are acyclic, so
            # walking parents from any node must terminate at a root. If this
            # ever fires, one of those three checks above has regressed.
            raise ValueError(
                f"run {self.id!r}: no root node despite acyclic resolved parents"
            )

        if self.duration_ms is None and self.started_at and self.ended_at:
            delta = (self.ended_at - self.started_at).total_seconds() * 1000
            object.__setattr__(self, "duration_ms", delta)

        return self

    @property
    def node_index(self) -> dict[str, Node]:
        """Nodes keyed by id."""
        return {node.id: node for node in self.nodes}

    @property
    def roots(self) -> list[Node]:
        """Nodes with no parent. Usually exactly one."""
        return [node for node in self.nodes if node.is_root]

    @property
    def failed_nodes(self) -> list[Node]:
        """Nodes that reported ERROR status.

        This is a filter, not failure analysis: it says which spans reported an
        error, not which failure was the root cause.
        """
        return [node for node in self.nodes if node.failed]
