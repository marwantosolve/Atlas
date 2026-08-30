"""Static system description carried alongside a run.

These mirror MASEF's ``agents_registry``, ``tools_registry`` and
``communication_graph``. Atlas needs them for two things the span tree alone
cannot supply: resolving which agent owns a span (via ``AgentSpec.node_name``)
and knowing which agent-to-agent handoffs were *possible* versus *taken*.

``system_prompt`` is intentionally not carried. Prompt-level evaluation is
MASEF's domain, and copying multi-kilobyte prompts into every Atlas run would
cost memory for data no Atlas analyzer reads.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AgentSpec(BaseModel):
    """An agent that exists in the system, whether or not it ran."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="Registry name, e.g. 'writer_agent'.")
    node_name: str | None = Field(
        default=None,
        description=(
            "Graph node label, e.g. 'writer'. This is the join key for agent "
            "attribution: span names match node_name, not name."
        ),
    )
    description: str | None = None
    tools_bound: list[str] = Field(default_factory=list)
    can_communicate_with: list[str] = Field(default_factory=list)
    participated: bool | None = None


class ToolSpec(BaseModel):
    """A tool available to the system."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str | None = None
    bound_to_agents: list[str] = Field(default_factory=list)
    invoked: bool | None = None
    invocation_count: int | None = Field(default=None, ge=0)


class CommEdge(BaseModel):
    """A declared agent-to-agent transition.

    ``edge_type`` stays a free string because MASEF leaves it open
    ('parallel', 'conditional', 'sequential', 'loopback', ...) and Atlas has
    no reason to reject a value it merely does not recognize.
    """

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    edge_type: str | None = None
    condition: str | None = None
    traversed: bool | None = Field(
        default=None,
        description="True when this transition was actually taken in the run.",
    )


class CommunicationGraph(BaseModel):
    """Declared topology of the agent system."""

    model_config = ConfigDict(extra="forbid")

    entry_point: str | None = None
    terminal_agents: list[str] = Field(default_factory=list)
    edges: list[CommEdge] = Field(default_factory=list)

    @property
    def traversed_edges(self) -> list[CommEdge]:
        """Only the transitions the run actually took."""
        return [edge for edge in self.edges if edge.traversed]
