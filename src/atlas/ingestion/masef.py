"""MASEF trace -> Atlas ``Run`` projection.

This is the only place in Atlas that knows the MASEF wire format. Everything
downstream is written against :class:`~atlas.models.run.Run`, so a schema change
lands here and nowhere else (plan §9).

The mapping is deliberately a rename plus a normalization, not a
transformation: one MASEF session becomes one ``Run``, one span becomes one
``Node``, and the original span is retained verbatim in ``Node.raw`` so no
analyzer is ever blocked by a field ingestion chose not to promote
(plan §22.7, ADR-004).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from atlas.ingestion.attribution import build_node_agent_map, resolve_all
from atlas.ingestion.capability import require_l1
from atlas.ingestion.errors import TraceFormatError
from atlas.models import (
    AgentSpec,
    CommEdge,
    CommunicationGraph,
    Node,
    Run,
    SpanKind,
    SpanStatus,
    ToolSpec,
)

_SPAN_KIND_ATTRIBUTE = "openinference.span.kind"


def _parse_timestamp(value: Any, *, where: str) -> datetime | None:
    """Parse a MASEF ISO-8601 timestamp.

    A malformed timestamp is an error, not a silent None: dropping it would
    move a span to an unknown position on the timeline while still letting the
    run load, and every ordering conclusion drawn from it would be wrong.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise TraceFormatError(
            f"{where}: expected an ISO-8601 timestamp string, got "
            f"{type(value).__name__}"
        )
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TraceFormatError(
            f"{where}: {value!r} is not an ISO-8601 timestamp ({exc})"
        ) from exc


def _span_kind(span: dict[str, Any]) -> SpanKind:
    """Map ``attributes["openinference.span.kind"]`` onto :class:`SpanKind`.

    An unrecognized or absent value becomes ``UNKNOWN`` rather than raising:
    13 of the reference trace's 52 spans carry no kind at all, and dropping
    them would orphan their children and break the call tree Atlas is built to
    walk.
    """
    attributes = span.get("attributes")
    raw = attributes.get(_SPAN_KIND_ATTRIBUTE) if isinstance(attributes, dict) else None
    if not isinstance(raw, str) or not raw:
        return SpanKind.UNKNOWN
    try:
        return SpanKind(raw.strip().upper())
    except ValueError:
        return SpanKind.UNKNOWN


def _span_status(span: dict[str, Any]) -> SpanStatus:
    """Map ``status`` onto :class:`SpanStatus`, defaulting to ``UNSET``.

    Never defaults to ``OK``. Inferring success from silence is how a debugging
    tool hides the bug it exists to find.
    """
    raw = span.get("status")
    if not isinstance(raw, str) or not raw:
        return SpanStatus.UNSET
    try:
        return SpanStatus(raw.strip().upper())
    except ValueError:
        return SpanStatus.UNSET


def _as_str(value: Any) -> str | None:
    """Coerce a promoted scalar to text, leaving structure to ``Node.raw``.

    MASEF's ``input_value``/``output_value`` are usually JSON strings but an
    adapter may emit an object. Serializing keeps the field a searchable string
    without losing the content.
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(value)


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _agent_specs(trace: dict[str, Any]) -> list[AgentSpec]:
    registry = trace.get("agents_registry")
    agents = registry.get("agents") if isinstance(registry, dict) else None
    specs: list[AgentSpec] = []
    for entry in agents or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("agent_name")
        if not name:
            continue
        # `system_prompt` and `factory_function` are read and dropped on
        # purpose: prompt-level evaluation is MASEF's domain (ADR-002) and
        # copying multi-kilobyte prompts onto every Run costs memory for data
        # no Atlas analyzer reads.
        specs.append(
            AgentSpec(
                name=name,
                node_name=entry.get("node_name") or None,
                description=entry.get("description") or None,
                tools_bound=list(entry.get("tools_bound") or []),
                can_communicate_with=list(entry.get("can_communicate_with") or []),
                participated=entry.get("participated"),
            )
        )
    return specs


def _tool_specs(trace: dict[str, Any]) -> list[ToolSpec]:
    registry = trace.get("tools_registry")
    tools = registry.get("tools") if isinstance(registry, dict) else None
    specs: list[ToolSpec] = []
    for entry in tools or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("tool_name")
        if not name:
            continue
        count = _as_int(entry.get("invocation_count"))
        specs.append(
            ToolSpec(
                name=name,
                description=entry.get("description") or None,
                bound_to_agents=list(entry.get("bound_to_agents") or []),
                invoked=entry.get("invoked"),
                invocation_count=count if count is None or count >= 0 else None,
            )
        )
    return specs


def _communication_graph(trace: dict[str, Any]) -> CommunicationGraph:
    graph = trace.get("communication_graph")
    if not isinstance(graph, dict):
        return CommunicationGraph()
    edges: list[CommEdge] = []
    for entry in graph.get("edges") or []:
        if not isinstance(entry, dict):
            continue
        source, target = entry.get("source"), entry.get("target")
        if not source or not target:
            continue
        edges.append(
            CommEdge(
                source=source,
                target=target,
                edge_type=entry.get("edge_type") or None,
                condition=entry.get("condition") or None,
                traversed=entry.get("traversed"),
            )
        )
    return CommunicationGraph(
        entry_point=graph.get("entry_point") or None,
        terminal_agents=list(graph.get("terminal_agents") or []),
        edges=edges,
    )


def _build_node(
    span: dict[str, Any],
    attribution: dict[str, tuple[str, Any]],
    *,
    where: str,
) -> Node:
    span_id = span.get("span_id")
    if not span_id:  # pragma: no cover - require_l1 rejects this first
        raise TraceFormatError(f"{where}: span has no span_id")

    openinference = span.get("openinference")
    if not isinstance(openinference, dict):
        openinference = {}

    resolved = attribution.get(span_id)
    agent, agent_source = resolved if resolved else (None, None)

    return Node(
        id=span_id,
        parent_id=span.get("parent_span_id") or None,
        # A span with no name still belongs in the tree; naming it after its id
        # keeps `name` non-empty without inventing a role for it.
        name=span.get("name") or f"<unnamed span {span_id}>",
        kind=_span_kind(span),
        status=_span_status(span),
        status_message=_as_str(span.get("status_message")),
        started_at=_parse_timestamp(span.get("start_time"), where=f"{where}.start_time"),
        ended_at=_parse_timestamp(span.get("end_time"), where=f"{where}.end_time"),
        duration_ms=_as_float(span.get("duration_ms")),
        agent=agent,
        agent_source=agent_source,
        tool=_as_str(openinference.get("tool_name")),
        model=_as_str(openinference.get("llm_model_name")),
        tokens_prompt=_as_int(openinference.get("llm_token_count_prompt")),
        tokens_completion=_as_int(openinference.get("llm_token_count_completion")),
        tokens_total=_as_int(openinference.get("llm_token_count_total")),
        # Never derived. MASEF owns pricing (ADR-003); traces do not carry cost
        # today, so this stays None until one does.
        cost_usd=_as_float(span.get("cost_usd")),
        # `attempt` and `retry_of` stay at their defaults: retry detection is
        # Phase 3, and guessing here would seed later phases with fiction.
        input_value=_as_str(openinference.get("input_value")),
        output_value=_as_str(openinference.get("output_value")),
        raw=span,
    )


def load_trace_dict(
    trace: Any,
    *,
    source: str = "<trace>",
    span_agent_map: dict[str, str] | None = None,
) -> list[Run]:
    """Project an already-parsed MASEF trace into one :class:`Run` per session.

    ``span_agent_map`` is MASEF's ``cross_link_index`` (``span_id -> agent``)
    when the caller has a MASEF evaluation output for this trace. Supplying it
    slots into the attribution precedence at MASEF's step 4; omitting it simply
    means that step is skipped.
    """
    if not isinstance(trace, dict):
        raise TraceFormatError(
            f"{source}: a MASEF trace must be a JSON object, got {type(trace).__name__}"
        )

    sessions = trace.get("sessions")
    if sessions is None:
        raise TraceFormatError(
            f"{source}: no 'sessions' key; this does not look like a MASEF trace"
        )
    if not isinstance(sessions, list) or not sessions:
        raise TraceFormatError(
            f"{source}: 'sessions' must be a non-empty list of session objects"
        )

    require_l1(trace, source=source)

    node_agent_map = build_node_agent_map(
        (trace.get("agents_registry") or {}).get("agents") or []
        if isinstance(trace.get("agents_registry"), dict)
        else []
    )
    agents = _agent_specs(trace)
    tools = _tool_specs(trace)
    communication = _communication_graph(trace)
    schema_version = trace.get("schema_version")

    runs: list[Run] = []
    for index, session in enumerate(sessions):
        where = f"{source}: sessions[{index}]"
        if not isinstance(session, dict):
            raise TraceFormatError(
                f"{where} is a {type(session).__name__}, not a session object"
            )

        session_id = session.get("session_id")
        if not session_id:
            raise TraceFormatError(
                f"{where} has no session_id; a Run needs a stable identity to be "
                f"referenced by later analysis"
            )

        spans = [s for s in (session.get("spans") or []) if isinstance(s, dict)]
        if not spans:
            raise TraceFormatError(
                f"{where} ('{session_id}') contains no spans; an empty run has "
                f"nothing to diagnose"
            )

        attribution = resolve_all(spans, node_agent_map, span_agent_map)
        nodes = [
            _build_node(span, attribution, where=f"{where}.spans[{i}]")
            for i, span in enumerate(spans)
        ]

        runs.append(
            Run(
                id=session_id,
                trace_id=session.get("trace_id") or None,
                schema_version=schema_version,
                started_at=_parse_timestamp(
                    session.get("start_time"), where=f"{where}.start_time"
                ),
                ended_at=_parse_timestamp(
                    session.get("end_time"), where=f"{where}.end_time"
                ),
                duration_ms=_as_float(session.get("duration_ms")),
                input_query=_as_str(session.get("input_query")),
                final_output=_as_str(session.get("final_output")),
                nodes=nodes,
                agents=agents,
                tools=tools,
                communication=communication,
                metadata=_run_metadata(trace, session, source=source),
            )
        )

    return runs


def _run_metadata(
    trace: dict[str, Any], session: dict[str, Any], *, source: str
) -> dict[str, Any]:
    """Provenance plus the session fields Atlas does not model.

    ``route_taken`` is kept verbatim rather than promoted: it is a
    framework-specific routing record, and Phase 2 will decide whether it is a
    usable dataflow signal or a LangGraph detail.
    """
    metadata: dict[str, Any] = {"source": source}

    framework = None
    registry = trace.get("agents_registry")
    if isinstance(registry, dict):
        framework = registry.get("framework_name")
    if framework:
        metadata["framework_name"] = framework

    if trace.get("export_time"):
        metadata["export_time"] = trace["export_time"]
    if session.get("route_taken") is not None:
        metadata["route_taken"] = session["route_taken"]

    # Kept because neither is derivable from anything else Atlas carries. The
    # counts (`session_count`, `span_count`, `total_agents`, `adjacency_list`)
    # are dropped on purpose -- they restate what the lists already say, and a
    # stored count that disagrees with its list is a bug waiting to happen.
    trace_metadata = trace.get("metadata")
    if isinstance(trace_metadata, dict) and trace_metadata:
        metadata["trace_metadata"] = trace_metadata

    session_metadata = session.get("metadata")
    if isinstance(session_metadata, dict) and session_metadata:
        metadata["session_metadata"] = session_metadata

    return metadata


def load_trace(
    path: str | Path, *, span_agent_map: dict[str, str] | None = None
) -> list[Run]:
    """Read a MASEF trace from disk and project it into :class:`Run` objects."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TraceFormatError(f"{path}: cannot be read ({exc})") from exc
    try:
        trace = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TraceFormatError(
            f"{path}: not valid JSON (line {exc.lineno}, column {exc.colno}: {exc.msg})"
        ) from exc
    return load_trace_dict(trace, source=str(path), span_agent_map=span_agent_map)


def load_run(path: str | Path, *, span_agent_map: dict[str, str] | None = None) -> Run:
    """Load a single-session trace, raising if it holds more than one run."""
    runs = load_trace(path, span_agent_map=span_agent_map)
    if len(runs) != 1:
        raise TraceFormatError(
            f"{path}: expected exactly one session, found {len(runs)} "
            f"({', '.join(run.id for run in runs)}); use load_trace() instead"
        )
    return runs[0]
