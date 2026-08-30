"""Shared fixtures.

Helpers keep the tests focused on the rule under test: `make_node()` supplies
valid defaults so each test varies exactly one field.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from atlas.models import AgentSource, Node, Run, SpanKind, SpanStatus

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "traces"

# The real MASEF export the loader is validated against. It lives outside this
# repository (it is 4 MB of someone else's run), so the integration test
# resolves it solely through the ATLAS_MASEF_TRACE environment variable and
# skips when it is unset -- a public clone has no way to guess where such a
# trace might be, and hardcoding a path would leak a machine's layout.
REAL_TRACE_ENV = "ATLAS_MASEF_TRACE"


def find_real_trace() -> Path | None:
    override = os.environ.get(REAL_TRACE_ENV)
    if override:
        path = Path(override)
        return path if path.is_file() else None
    return None


def make_node(node_id: str = "s1", **overrides: Any) -> Node:
    """A valid root LLM node, overridable field by field."""
    fields: dict[str, Any] = {
        "id": node_id,
        "name": "root_span",
        "kind": SpanKind.CHAIN,
        "status": SpanStatus.OK,
        "started_at": datetime(2026, 4, 17, 0, 34, 2),
        "ended_at": datetime(2026, 4, 17, 0, 34, 12),
    }
    fields.update(overrides)
    if fields.get("agent") and not fields.get("agent_source"):
        # The model requires the pair; tests that set an agent without saying
        # how it was attributed get the strongest source by default.
        fields["agent_source"] = AgentSource.ATTRIBUTES
    return Node(**fields)


def make_run(nodes: list[Node] | None = None, **overrides: Any) -> Run:
    """A valid single-node run."""
    fields: dict[str, Any] = {
        "id": "session_1",
        "nodes": nodes if nodes is not None else [make_node()],
    }
    fields.update(overrides)
    return Run(**fields)


@pytest.fixture
def example_trace() -> dict[str, Any]:
    """The bundled minimal MASEF-format trace."""
    return json.loads((EXAMPLES / "minimal_run.json").read_text(encoding="utf-8"))
