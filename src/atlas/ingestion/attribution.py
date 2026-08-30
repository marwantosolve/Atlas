"""Agent attribution.

Which agent owns a span is the single most load-bearing derived fact in Atlas:
without it a propagation report can say "span 8a11 failed" but not "the writer
failed", and per-agent blast radius is the whole point.

The trace does not state it. ``openinference.agent_name`` is empty in all 52
spans of the reference trace, so attribution is inferred -- and Atlas infers it
with **the same precedence MASEF uses**
(``layers/performance/parser.py::_extract_agent_name``), step for step, so that
Atlas and MASEF never disagree about who owned a span. A shared definition is
the point of ADR-007; two tools blaming different agents for the same span
would make both untrustworthy.

Resolution is two passes:

1. **Direct.** Try each source in precedence order on the span itself.
2. **Inherit.** Any span still unresolved takes the agent of its nearest
   ancestor that resolved directly, tagged :attr:`AgentSource.ANCESTOR`.

Names are canonicalized to the registry's ``agent_name`` (``writer_agent``),
never the graph's ``node_name`` (``writer``). That is MASEF's canonical form and
it is also the form ``communication_graph`` edges use, so handoff edges join to
node attribution without a second mapping.
"""

from __future__ import annotations

import json
from typing import Any

from atlas.models.enums import AgentSource

_TOOLS_SUFFIX = "_tools"
_AGENT_SUFFIX = "_agent"

# One resolved attribution: the canonical agent name and how it was found.
Attribution = tuple[str, AgentSource]


def build_node_agent_map(agents: list[dict[str, Any]]) -> dict[str, str]:
    """Build the ``node_name -> agent_name`` join table.

    LangGraph labels spans with the graph node (``researcher_1``) while the
    registry keys on the agent (``researcher_1_agent``). Mirrors MASEF's
    ``_build_node_to_agent_map``, including the ``agent_name``-minus-``_agent``
    short form, so a span named after either resolves.
    """
    mapping: dict[str, str] = {}
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        agent_name = agent.get("agent_name") or ""
        node_name = agent.get("node_name") or ""
        if agent_name and node_name:
            mapping[node_name] = agent_name
        if agent_name.endswith(_AGENT_SUFFIX):
            mapping.setdefault(agent_name[: -len(_AGENT_SUFFIX)], agent_name)
    return mapping


def canonicalize(name: str, node_agent_map: dict[str, str]) -> str:
    """Map any known alias of an agent to its registry ``agent_name``.

    Unknown names pass through unchanged rather than being dropped: a span
    labelled with an agent the registry never declared is a real observation,
    and discarding it would hide an incomplete registry.
    """
    if name in node_agent_map:
        return node_agent_map[name]
    return name


def _langgraph_node(attributes: dict[str, Any]) -> str | None:
    """Read ``langgraph_node`` out of the JSON string at ``attributes.metadata``.

    This is where 26 of the reference trace's 52 attributions actually come
    from -- the span-name join alone reaches only 12.
    """
    raw = attributes.get("metadata")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        meta = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    node = meta.get("langgraph_node") or ""
    if not isinstance(node, str) or not node:
        return None
    # LangGraph emits a companion `<node>_tools` node for a node's tool calls.
    # It is the same agent, so the suffix is stripped rather than treated as a
    # seventh agent that appears in no registry.
    if node.endswith(_TOOLS_SUFFIX):
        node = node[: -len(_TOOLS_SUFFIX)]
    return node or None


def resolve_direct(
    span: dict[str, Any],
    node_agent_map: dict[str, str],
    span_agent_map: dict[str, str] | None = None,
) -> Attribution | None:
    """Resolve ``span``'s agent from the span alone, or return None.

    Precedence matches MASEF's ``_extract_agent_name``. ``span_agent_map`` is
    MASEF's ``cross_link_index`` (``span_id -> agent``) when the caller has a
    MASEF evaluation output to hand; Atlas never reads it from disk itself.
    """
    openinference = span.get("openinference")
    if isinstance(openinference, dict):
        declared = openinference.get("agent_name")
        if declared:
            return canonicalize(declared, node_agent_map), AgentSource.OPENINFERENCE

    attributes = span.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {}

    from_attributes = attributes.get("agent_name") or attributes.get(
        "openinference.agent.name"
    )
    if from_attributes:
        return canonicalize(from_attributes, node_agent_map), AgentSource.ATTRIBUTES

    node = _langgraph_node(attributes)
    if node:
        return canonicalize(node, node_agent_map), AgentSource.LANGGRAPH_METADATA

    span_id = span.get("span_id") or ""
    if span_agent_map and span_id in span_agent_map:
        return span_agent_map[span_id], AgentSource.CROSS_LINK

    name = span.get("name") or ""
    if name and node_agent_map:
        if name in node_agent_map:
            return node_agent_map[name], AgentSource.SPAN_NAME
        if name in node_agent_map.values():
            return name, AgentSource.SPAN_NAME

    return None


def resolve_all(
    spans: list[dict[str, Any]],
    node_agent_map: dict[str, str],
    span_agent_map: dict[str, str] | None = None,
) -> dict[str, Attribution]:
    """Resolve every span, inheriting from the nearest resolving ancestor.

    Returns a ``span_id -> (agent, source)`` map covering only the spans that
    resolved. Spans absent from the result are genuinely unattributable and
    Atlas leaves them that way rather than assigning them to the root's agent.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for span in spans:
        span_id = span.get("span_id")
        if span_id:
            by_id[span_id] = span

    resolved: dict[str, Attribution] = {}
    for span_id, span in by_id.items():
        direct = resolve_direct(span, node_agent_map, span_agent_map)
        if direct is not None:
            resolved[span_id] = direct

    # Second pass: walk each unresolved span's parent chain. `seen` guards
    # against a cyclic parent chain -- Run validation rejects cycles, but this
    # runs *before* Run construction, so it cannot rely on that guarantee.
    for span_id, span in by_id.items():
        if span_id in resolved:
            continue
        seen = {span_id}
        current = span.get("parent_span_id")
        while current and current not in seen:
            seen.add(current)
            inherited = resolved.get(current)
            if inherited is not None:
                # Record the agent, not the ancestor's source: this span's
                # attribution is an inference regardless of how solid the
                # ancestor's own attribution was.
                resolved[span_id] = (inherited[0], AgentSource.ANCESTOR)
                break
            parent = by_id.get(current)
            if parent is None:
                break
            current = parent.get("parent_span_id")

    return resolved
