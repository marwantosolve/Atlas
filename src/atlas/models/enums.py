"""Enumerations for the Atlas domain model.

Values mirror the MASEF trace vocabulary so that ingestion is a rename,
not a translation. See docs/event-schema.md.
"""

from __future__ import annotations

from enum import Enum


class SpanKind(str, Enum):
    """Role of a span, from ``attributes["openinference.span.kind"]``.

    ``UNKNOWN`` is a real and common case, not a defect: in the reference
    MASEF trace 13 of 52 spans carry no kind attribute (framework-internal
    duplicates emitted by LangGraph). Atlas keeps them so the call tree stays
    complete rather than dropping them and creating orphans.
    """

    LLM = "LLM"
    TOOL = "TOOL"
    CHAIN = "CHAIN"
    RETRIEVER = "RETRIEVER"
    EMBEDDING = "EMBEDDING"
    UNKNOWN = "UNKNOWN"


class SpanStatus(str, Enum):
    """OpenTelemetry status. Mirrors MASEF's ``status`` enum exactly."""

    OK = "OK"
    ERROR = "ERROR"
    UNSET = "UNSET"


class EdgeType(str, Enum):
    """Why two nodes are connected.

    Atlas keeps edge provenance explicit because §22.8 of the implementation
    plan requires every conclusion to be traceable to observable evidence:
    a propagation claim that rests on a CALL edge is a different claim from
    one resting on a HANDOFF edge.
    """

    CALL = "call"
    """Parent span invoked the child span (from ``parent_span_id``)."""

    HANDOFF = "handoff"
    """Agent-to-agent transfer (from a traversed ``communication_graph`` edge)."""

    RETRY = "retry"
    """Failed attempt superseded by a later attempt of the same operation."""

    DATA = "data"
    """Consumer depends on a value produced upstream. Not yet populated."""


class AgentSource(str, Enum):
    """How ``Node.agent`` was resolved.

    Attribution is a *derived* claim, and the members below are not equally
    strong: ``OPENINFERENCE`` is the instrumentation stating the agent
    outright, while ``ANCESTOR`` is Atlas inferring it from the call tree.
    Phase 4/5 propagation claims rest on attribution, so the provenance has to
    survive ingestion -- otherwise a blast-radius report cannot distinguish a
    fact from an inference.

    Members are ordered by the precedence in which they are attempted, which
    mirrors MASEF's ``_extract_agent_name`` (``layers/performance/parser.py``)
    step for step.
    """

    OPENINFERENCE = "openinference"
    """``openinference.agent_name`` on the span. Empty in the reference trace."""

    ATTRIBUTES = "attributes"
    """``attributes.agent_name`` or ``attributes["openinference.agent.name"]``."""

    LANGGRAPH_METADATA = "langgraph_metadata"
    """``langgraph_node`` inside the JSON string at ``attributes.metadata``."""

    CROSS_LINK = "cross_link"
    """A caller-supplied ``span_id -> agent`` map from MASEF's evaluation output."""

    SPAN_NAME = "span_name"
    """Span ``name`` joined against ``AgentSpec.node_name``."""

    ANCESTOR = "ancestor"
    """Inherited from the nearest ancestor that resolved by one of the above."""


class FailureKind(str, Enum):
    """Deterministic classification of an observed failure.

    Every member must be decidable from the trace alone, with no model in the
    loop -- that is the property that distinguishes Atlas's attribution from
    MASEF's LLM-judged MAST taxonomy (see docs/decisions.md ADR-002).
    """

    ERROR_STATUS = "error_status"
    """Span reported ``status == ERROR``."""

    TIMEOUT = "timeout"
    """Error whose message matches a timeout signature."""

    EXHAUSTED_RETRIES = "exhausted_retries"
    """Final attempt in a retry chain still failed."""

    MISSING_OUTPUT = "missing_output"
    """Span completed but produced no output value."""

    UNKNOWN = "unknown"
    """Failure detected but not classifiable from available evidence."""
