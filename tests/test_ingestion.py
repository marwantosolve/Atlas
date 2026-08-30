"""The MASEF trace loader.

Two things are under test: that the bundled fixture projects into a `Run`
without loss, and that every rejection names the offending path. The second
matters as much as the first — a debugger that reports "invalid trace" has
failed at the one job that distinguishes it from a log viewer.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from atlas.ingestion import (
    CapabilityError,
    TraceFormatError,
    l1_gaps,
    load_run,
    load_trace,
    load_trace_dict,
)
from atlas.models import AgentSource, SpanKind, SpanStatus

FIXTURE = Path(__file__).resolve().parent.parent / "examples" / "traces" / "minimal_run.json"


@pytest.fixture
def trace(example_trace: dict[str, Any]) -> dict[str, Any]:
    """A mutable deep copy, so a test can corrupt one field in isolation."""
    return copy.deepcopy(example_trace)


# --- the happy path --------------------------------------------------------


def test_one_run_per_session(trace: dict[str, Any]) -> None:
    runs = load_trace_dict(trace)
    assert len(runs) == 1
    assert runs[0].id == "session_example_001"


def test_load_from_disk() -> None:
    run = load_run(FIXTURE)
    assert len(run.nodes) == 5
    assert len(run.roots) == 1
    assert run.failed_nodes == []


def test_run_level_fields(trace: dict[str, Any]) -> None:
    run = load_trace_dict(trace)[0]
    assert run.trace_id == "b7f1c2d3e4a5b6c7"
    assert run.schema_version == "1.0"
    assert run.started_at == datetime(2026, 4, 17, 0, 34, 2)
    assert run.ended_at == datetime(2026, 4, 17, 0, 34, 5)
    assert run.duration_ms == 3000.0
    assert run.input_query is not None and "rApps" in run.input_query
    assert run.final_output is not None and run.final_output.startswith("rApps attach")


def test_run_duration_is_never_inferred_from_node_extents(
    trace: dict[str, Any],
) -> None:
    """Rolling node durations up would be latency analysis, MASEF's (ADR-002).

    `Run` derives its duration from its *own* timestamps when the session omits
    it, and otherwise leaves it None -- even though the nodes plainly span three
    seconds.
    """
    session = trace["sessions"][0]
    session["duration_ms"] = None
    session["start_time"] = None
    session["end_time"] = None

    run = load_trace_dict(trace)[0]
    assert run.duration_ms is None
    assert any(node.duration_ms for node in run.nodes)


def test_run_duration_falls_back_to_its_own_timestamps(trace: dict[str, Any]) -> None:
    trace["sessions"][0]["duration_ms"] = None
    assert load_trace_dict(trace)[0].duration_ms == pytest.approx(3000.0)


def test_span_fields_are_promoted(trace: dict[str, Any]) -> None:
    by_name = {node.name: node for node in load_trace_dict(trace)[0].nodes}

    tool = by_name["search_web"]
    assert tool.kind is SpanKind.TOOL
    assert tool.status is SpanStatus.OK
    assert tool.tool == "search_web"
    assert tool.parent_id == "4d5e6f708192a3b4"
    assert tool.duration_ms == 1100.0

    llm = by_name["ChatGoogleGenerativeAI"]
    assert llm.kind is SpanKind.LLM
    assert llm.model == "gemma-4-26b-a4b-it"
    assert (llm.tokens_prompt, llm.tokens_completion, llm.tokens_total) == (120, 60, 180)
    assert llm.input_value is not None and llm.input_value.startswith("Plan the")

    assert by_name["research_query"].is_root
    assert by_name["research_query"].parent_id is None


def test_every_span_keeps_its_original_form(trace: dict[str, Any]) -> None:
    """Plan §22.7: no analyzer is ever blocked by a field ingestion skipped."""
    nodes = load_trace_dict(trace)[0].nodes
    assert all(node.raw for node in nodes)
    by_name = {node.name: node for node in nodes}
    # `service_name`, `kind` (the OTel one) and `attributes` have no promoted
    # field, so `raw` is the only place they survive.
    assert by_name["search_web"].raw["service_name"] == "atlas-example"
    assert by_name["search_web"].raw["kind"] == "INTERNAL"
    assert (
        by_name["search_web"].raw["attributes"]["session.id"] == "session_example_001"
    )


def test_no_node_claims_a_cost(trace: dict[str, Any]) -> None:
    """ADR-003: MASEF owns pricing, and traces do not carry cost today."""
    assert all(node.cost_usd is None for node in load_trace_dict(trace)[0].nodes)


def test_retries_are_not_guessed_at_ingestion(trace: dict[str, Any]) -> None:
    """Retry detection is Phase 3; guessing here would seed it with fiction."""
    for node in load_trace_dict(trace)[0].nodes:
        assert node.attempt == 1
        assert node.retry_of is None


def test_registries_are_copied_onto_the_run(trace: dict[str, Any]) -> None:
    run = load_trace_dict(trace)[0]
    assert [agent.name for agent in run.agents] == [
        "orchestrator_agent",
        "researcher_agent",
    ]
    assert [agent.node_name for agent in run.agents] == ["orchestrator", "researcher"]
    assert [tool.name for tool in run.tools] == ["search_web"]
    assert run.tools[0].invocation_count == 1


def test_communication_graph_is_carried_with_traversal(trace: dict[str, Any]) -> None:
    graph = load_trace_dict(trace)[0].communication
    assert graph.entry_point == "orchestrator_agent"
    assert graph.terminal_agents == ["researcher_agent"]
    assert len(graph.edges) == 1
    assert graph.traversed_edges == graph.edges
    assert graph.edges[0].edge_type == "sequential"
    assert graph.edges[0].condition == "Always"


def test_system_prompts_are_not_carried(trace: dict[str, Any]) -> None:
    """ADR-002: prompt-level evaluation is MASEF's, and prompts are large."""
    trace["agents_registry"]["agents"][0]["system_prompt"] = "x" * 5000
    run = load_trace_dict(trace)[0]
    assert "system_prompt" not in run.agents[0].model_dump()


def test_metadata_records_provenance(trace: dict[str, Any]) -> None:
    run = load_trace_dict(trace, source="unit-test")[0]
    assert run.metadata["source"] == "unit-test"
    assert run.metadata["framework_name"] == "langgraph"
    assert run.metadata["export_time"] == "2026-04-17T00:34:20.113402"
    assert run.metadata["route_taken"] == "orchestrator -> researcher"
    assert run.metadata["trace_metadata"]["execution_id"] == "atlas_example_001"


def test_empty_session_metadata_is_omitted_rather_than_stored(
    trace: dict[str, Any],
) -> None:
    assert "session_metadata" not in load_trace_dict(trace)[0].metadata


# --- attribution through the loader ---------------------------------------


def test_attribution_covers_the_fixture(trace: dict[str, Any]) -> None:
    by_name = {node.name: node for node in load_trace_dict(trace)[0].nodes}

    assert by_name["orchestrator"].agent == "orchestrator_agent"
    assert by_name["orchestrator"].agent_source is AgentSource.SPAN_NAME
    assert by_name["researcher"].agent == "researcher_agent"

    # The LLM and tool spans are named after neither agent; they inherit.
    assert by_name["ChatGoogleGenerativeAI"].agent == "orchestrator_agent"
    assert by_name["ChatGoogleGenerativeAI"].agent_source is AgentSource.ANCESTOR
    assert by_name["search_web"].agent == "researcher_agent"
    assert by_name["search_web"].agent_source is AgentSource.ANCESTOR

    # The graph root belongs to no agent and is not assigned to one.
    assert by_name["research_query"].agent is None
    assert by_name["research_query"].agent_source is None


def test_a_supplied_cross_link_map_is_used(trace: dict[str, Any]) -> None:
    """ADR-008: the caller passes MASEF's cross_link_index; Atlas never reads it."""
    root = trace["sessions"][0]["spans"][-1]
    assert root["name"] == "research_query"
    run = load_trace_dict(trace, span_agent_map={root["span_id"]: "orchestrator_agent"})[
        0
    ]
    root_node = run.node_index[root["span_id"]]
    assert root_node.agent == "orchestrator_agent"
    assert root_node.agent_source is AgentSource.CROSS_LINK


# --- normalization ---------------------------------------------------------


def test_unknown_span_kind_becomes_unknown_not_an_error(trace: dict[str, Any]) -> None:
    """13 of 52 reference spans have no kind; dropping them would orphan children."""
    trace["sessions"][0]["spans"][0]["attributes"]["openinference.span.kind"] = "GUARDRAIL"
    node = load_trace_dict(trace)[0].node_index["5e6f708192a3b4c5"]
    assert node.kind is SpanKind.UNKNOWN


def test_missing_span_kind_becomes_unknown(trace: dict[str, Any]) -> None:
    del trace["sessions"][0]["spans"][0]["attributes"]["openinference.span.kind"]
    assert load_trace_dict(trace)[0].node_index["5e6f708192a3b4c5"].kind is SpanKind.UNKNOWN


def test_span_kind_is_case_insensitive(trace: dict[str, Any]) -> None:
    trace["sessions"][0]["spans"][0]["attributes"]["openinference.span.kind"] = "tool"
    assert load_trace_dict(trace)[0].node_index["5e6f708192a3b4c5"].kind is SpanKind.TOOL


def test_missing_status_becomes_unset_never_ok(trace: dict[str, Any]) -> None:
    """Inferring success from silence is how a debugger hides the bug."""
    trace["sessions"][0]["spans"][0]["status"] = None
    assert load_trace_dict(trace)[0].node_index["5e6f708192a3b4c5"].status is SpanStatus.UNSET


def test_unrecognized_status_becomes_unset(trace: dict[str, Any]) -> None:
    trace["sessions"][0]["spans"][0]["status"] = "DEGRADED"
    assert load_trace_dict(trace)[0].node_index["5e6f708192a3b4c5"].status is SpanStatus.UNSET


def test_error_status_is_recognized(trace: dict[str, Any]) -> None:
    trace["sessions"][0]["spans"][0]["status"] = "ERROR"
    trace["sessions"][0]["spans"][0]["status_message"] = "rate limited"
    run = load_trace_dict(trace)[0]
    assert [node.id for node in run.failed_nodes] == ["5e6f708192a3b4c5"]
    assert run.node_index["5e6f708192a3b4c5"].status_message == "rate limited"


def test_offset_aware_timestamps_are_normalized_to_naive_utc(
    trace: dict[str, Any],
) -> None:
    """Mixing aware and naive stamps raises TypeError on the first comparison."""
    session = trace["sessions"][0]
    session["start_time"] = "2026-04-17T02:34:02+02:00"
    assert load_trace_dict(trace)[0].started_at == datetime(2026, 4, 17, 0, 34, 2)


def test_trailing_z_is_accepted(trace: dict[str, Any]) -> None:
    trace["sessions"][0]["start_time"] = "2026-04-17T00:34:02Z"
    assert load_trace_dict(trace)[0].started_at == datetime(2026, 4, 17, 0, 34, 2)


def test_duration_is_derived_only_when_absent(trace: dict[str, Any]) -> None:
    span = trace["sessions"][0]["spans"][0]
    span["duration_ms"] = None
    node = load_trace_dict(trace)[0].node_index[span["span_id"]]
    assert node.duration_ms == pytest.approx(1100.0)


def test_a_supplied_duration_is_never_overwritten(trace: dict[str, Any]) -> None:
    """A disagreeing duration is evidence of clock skew, not noise to correct."""
    span = trace["sessions"][0]["spans"][0]
    span["duration_ms"] = 9999.0
    node = load_trace_dict(trace)[0].node_index[span["span_id"]]
    assert node.duration_ms == 9999.0
    assert node.duration_disagreement_ms == pytest.approx(8899.0)


def test_structured_output_value_is_serialized_not_dropped(
    trace: dict[str, Any],
) -> None:
    span = trace["sessions"][0]["spans"][0]
    span["openinference"]["output_value"] = {"content": "ok"}
    node = load_trace_dict(trace)[0].node_index[span["span_id"]]
    assert node.output_value is not None
    assert json.loads(node.output_value) == {"content": "ok"}


def test_blank_strings_collapse_to_none(trace: dict[str, Any]) -> None:
    span = trace["sessions"][0]["spans"][0]
    span["status_message"] = ""
    span["openinference"]["tool_name"] = ""
    node = load_trace_dict(trace)[0].node_index[span["span_id"]]
    assert node.status_message is None
    assert node.tool is None


def test_unnamed_span_is_kept_with_a_placeholder_name(trace: dict[str, Any]) -> None:
    """It still belongs in the tree; naming it after its id invents no role."""
    span = trace["sessions"][0]["spans"][0]
    span["name"] = ""
    node = load_trace_dict(trace)[0].node_index[span["span_id"]]
    assert node.name == "<unnamed span 5e6f708192a3b4c5>"


def test_string_token_counts_are_coerced(trace: dict[str, Any]) -> None:
    span = trace["sessions"][0]["spans"][2]
    span["openinference"]["llm_token_count_total"] = "180"
    assert load_trace_dict(trace)[0].node_index[span["span_id"]].tokens_total == 180


def test_uncoercible_token_count_becomes_none_rather_than_zero(
    trace: dict[str, Any],
) -> None:
    """Zero would be a claim about usage; None is the absence of one."""
    span = trace["sessions"][0]["spans"][2]
    span["openinference"]["llm_token_count_total"] = "many"
    assert load_trace_dict(trace)[0].node_index[span["span_id"]].tokens_total is None


# --- rejection -------------------------------------------------------------


def test_non_object_trace_is_rejected() -> None:
    with pytest.raises(TraceFormatError, match="must be a JSON object"):
        load_trace_dict([{"sessions": []}])


def test_missing_sessions_key_is_rejected() -> None:
    with pytest.raises(TraceFormatError, match="does not look like a MASEF trace"):
        load_trace_dict({"agents_registry": {"agents": []}})


def test_empty_sessions_list_is_rejected() -> None:
    with pytest.raises(TraceFormatError, match="non-empty list"):
        load_trace_dict({"sessions": []})


def test_session_without_an_id_is_rejected(trace: dict[str, Any]) -> None:
    del trace["sessions"][0]["session_id"]
    with pytest.raises(TraceFormatError, match=r"sessions\[0\] has no session_id"):
        load_trace_dict(trace)


def test_session_without_spans_is_rejected(trace: dict[str, Any]) -> None:
    trace["sessions"][0]["spans"] = []
    with pytest.raises(CapabilityError, match="contains no spans"):
        load_trace_dict(trace)


def test_non_object_session_is_rejected(trace: dict[str, Any]) -> None:
    trace["sessions"].append("oops")
    with pytest.raises(TraceFormatError, match=r"sessions\[1\] is a str"):
        load_trace_dict(trace)


def test_one_empty_session_among_several_is_rejected(trace: dict[str, Any]) -> None:
    """The trace clears L1 on its other spans, so the per-session check fires."""
    empty = copy.deepcopy(trace["sessions"][0])
    empty["session_id"] = "session_example_002"
    empty["spans"] = []
    trace["sessions"].append(empty)
    with pytest.raises(TraceFormatError, match="contains no spans; an empty run"):
        load_trace_dict(trace)


def test_malformed_timestamp_is_rejected_not_silently_dropped(
    trace: dict[str, Any],
) -> None:
    """A None here would move the span to an unknown point on the timeline."""
    trace["sessions"][0]["spans"][0]["start_time"] = "17/04/2026 00:34"
    with pytest.raises(TraceFormatError, match="is not an ISO-8601 timestamp"):
        load_trace_dict(trace)


def test_error_message_names_the_offending_span_path(trace: dict[str, Any]) -> None:
    trace["sessions"][0]["spans"][2]["end_time"] = "not-a-time"
    with pytest.raises(TraceFormatError, match=r"sessions\[0\]\.spans\[2\]\.end_time"):
        load_trace_dict(trace, source="fixture.json")


def test_phase_0_graph_validation_still_applies(trace: dict[str, Any]) -> None:
    """Ingestion must not be a way around Run's integrity checks."""
    trace["sessions"][0]["spans"][0]["parent_span_id"] = "ghost"
    with pytest.raises(ValueError, match="which is not present in this run"):
        load_trace_dict(trace)


def test_duplicate_span_ids_are_rejected(trace: dict[str, Any]) -> None:
    spans = trace["sessions"][0]["spans"]
    # Two spans that are not each other's parent, so the duplicate-id check is
    # what fires rather than the self-parent one.
    assert spans[2]["parent_span_id"] != spans[0]["span_id"]
    spans[2]["span_id"] = spans[0]["span_id"]
    with pytest.raises(ValueError, match="is a duplicate"):
        load_trace_dict(trace)


def test_missing_file_is_reported_with_its_path(tmp_path: Path) -> None:
    with pytest.raises(TraceFormatError, match="cannot be read"):
        load_trace(tmp_path / "absent.json")


def test_invalid_json_is_reported_with_its_position(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text('{"sessions": [', encoding="utf-8")
    with pytest.raises(TraceFormatError, match="not valid JSON"):
        load_trace(path)


def test_load_run_rejects_a_multi_session_trace(trace: dict[str, Any], tmp_path: Path) -> None:
    second = copy.deepcopy(trace["sessions"][0])
    second["session_id"] = "session_example_002"
    trace["sessions"].append(second)
    path = tmp_path / "two.json"
    path.write_text(json.dumps(trace), encoding="utf-8")
    with pytest.raises(TraceFormatError, match="expected exactly one session, found 2"):
        load_run(path)


def test_multi_session_traces_load_as_separate_runs(trace: dict[str, Any]) -> None:
    second = copy.deepcopy(trace["sessions"][0])
    second["session_id"] = "session_example_002"
    trace["sessions"].append(second)
    runs = load_trace_dict(trace)
    assert [run.id for run in runs] == ["session_example_001", "session_example_002"]
    # The registries are shared, so both runs see the same static description.
    assert runs[0].agents == runs[1].agents


# --- the L1 capability gate -----------------------------------------------


def test_fixture_reaches_l1(trace: dict[str, Any]) -> None:
    assert l1_gaps(trace) == []


def test_missing_timing_falls_below_l1(trace: dict[str, Any]) -> None:
    for span in trace["sessions"][0]["spans"]:
        span["end_time"] = None
    with pytest.raises(CapabilityError, match="lack start_time or end_time") as caught:
        load_trace_dict(trace)
    assert caught.value.level == "L0"


def test_absent_parent_key_falls_below_l1(trace: dict[str, Any]) -> None:
    """An absent key is not a null parent, and Atlas will not assume either."""
    del trace["sessions"][0]["spans"][0]["parent_span_id"]
    with pytest.raises(CapabilityError, match="omit the parent_span_id key"):
        load_trace_dict(trace)


def test_missing_span_id_falls_below_l1(trace: dict[str, Any]) -> None:
    trace["sessions"][0]["spans"][0]["span_id"] = ""
    with pytest.raises(CapabilityError, match="have no span_id"):
        load_trace_dict(trace)


def test_capability_error_reports_every_gap_once(trace: dict[str, Any]) -> None:
    """One line per kind of gap, not one per span."""
    for span in trace["sessions"][0]["spans"]:
        span["span_id"] = ""
        span["start_time"] = None
    gaps = l1_gaps(trace)
    assert len(gaps) == 2
    assert "5 span(s) have no span_id" in gaps[0]


def test_capability_error_names_the_source(trace: dict[str, Any]) -> None:
    trace["sessions"][0]["spans"][0]["start_time"] = None
    with pytest.raises(CapabilityError, match="^my-trace.json: trace is below"):
        load_trace_dict(trace, source="my-trace.json")
