"""Guards the bundled example trace.

The fixture is the contract between Atlas and MASEF, so it is checked
structurally here: these tests assert facts about the *trace file*, and fail
when a hand edit drifts it away from the shape a real MASEF export has. What
the loader makes of it is `test_ingestion.py`'s job.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from atlas.models import SpanKind, SpanStatus


def test_required_masef_keys_present(example_trace: dict[str, Any]) -> None:
    assert "agents_registry" in example_trace
    assert "sessions" in example_trace
    assert example_trace["agents_registry"]["agents"]
    assert len(example_trace["sessions"]) >= 1


def test_declared_counts_match_reality(example_trace: dict[str, Any]) -> None:
    sessions = example_trace["sessions"]
    assert example_trace["session_count"] == len(sessions)
    assert example_trace["span_count"] == sum(len(s["spans"]) for s in sessions)


def test_span_ids_unique_and_parents_resolve(example_trace: dict[str, Any]) -> None:
    spans = example_trace["sessions"][0]["spans"]
    ids = [span["span_id"] for span in spans]
    assert len(ids) == len(set(ids))

    known = set(ids)
    dangling = [
        span["span_id"]
        for span in spans
        if span["parent_span_id"] is not None and span["parent_span_id"] not in known
    ]
    assert dangling == []


def test_exactly_one_root(example_trace: dict[str, Any]) -> None:
    spans = example_trace["sessions"][0]["spans"]
    roots = [s["span_id"] for s in spans if s["parent_span_id"] is None]
    assert len(roots) == 1


def test_every_span_kind_is_known_to_atlas(example_trace: dict[str, Any]) -> None:
    """A kind Atlas cannot name would be silently downgraded to UNKNOWN."""
    for span in example_trace["sessions"][0]["spans"]:
        kind = span.get("attributes", {}).get("openinference.span.kind")
        assert kind is None or kind in SpanKind.__members__


def test_every_status_is_known_to_atlas(example_trace: dict[str, Any]) -> None:
    for span in example_trace["sessions"][0]["spans"]:
        assert span["status"] in SpanStatus.__members__


def test_agent_node_names_are_distinct_from_agent_names(
    example_trace: dict[str, Any],
) -> None:
    """Agent attribution joins span.name to node_name, so node_name must exist."""
    for agent in example_trace["agents_registry"]["agents"]:
        assert agent["node_name"]
        assert agent["node_name"] != agent["agent_name"]


def test_span_names_cover_the_participating_agents(
    example_trace: dict[str, Any],
) -> None:
    span_names = {s["name"] for s in example_trace["sessions"][0]["spans"]}
    for agent in example_trace["agents_registry"]["agents"]:
        if agent.get("participated"):
            assert agent["node_name"] in span_names


def test_fixture_carries_the_fields_the_loader_promotes(
    example_trace: dict[str, Any],
) -> None:
    """A fixture that exercises no promotion would let regressions through.

    The loader tests read this file, so it has to keep covering one span of each
    interesting shape: a tool call, an LLM call with token counts, and a root.
    """
    spans = example_trace["sessions"][0]["spans"]
    by_name = {span["name"]: span for span in spans}

    assert by_name["search_web"]["openinference"]["tool_name"] == "search_web"

    llm = by_name["ChatGoogleGenerativeAI"]["openinference"]
    assert llm["llm_model_name"]
    assert llm["llm_token_count_total"] == 180

    assert by_name["research_query"]["parent_span_id"] is None


def test_example_durations_agree_with_timestamps(example_trace: dict[str, Any]) -> None:
    """Guards against hand-edited fixtures drifting out of internal consistency."""
    for span in example_trace["sessions"][0]["spans"]:
        started = datetime.fromisoformat(span["start_time"])
        ended = datetime.fromisoformat(span["end_time"])
        measured = (ended - started).total_seconds() * 1000
        assert span["duration_ms"] == pytest.approx(measured, abs=1.0)
