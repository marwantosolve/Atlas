"""Node validation rules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from atlas.models import Node, SpanKind, SpanStatus, to_naive_utc

from .conftest import make_node


def test_minimal_node_needs_only_id_and_name() -> None:
    node = Node(id="s1", name="root")
    assert node.kind is SpanKind.UNKNOWN
    assert node.status is SpanStatus.UNSET
    assert node.attempt == 1
    assert node.is_root
    assert node.raw == {}


@pytest.mark.parametrize("field", ["id", "name"])
def test_empty_identity_fields_rejected(field: str) -> None:
    with pytest.raises(ValidationError, match="at least 1 character"):
        make_node(**{field: ""})


def test_self_parent_rejected() -> None:
    with pytest.raises(ValidationError, match="lists itself as its parent"):
        make_node("s1", parent_id="s1")


def test_self_retry_rejected() -> None:
    with pytest.raises(ValidationError, match="itself as its retried attempt"):
        make_node("s1", retry_of="s1")


def test_end_before_start_rejected() -> None:
    with pytest.raises(ValidationError, match="ends before it starts"):
        make_node(
            started_at=datetime(2026, 4, 17, 12, 0, 0),
            ended_at=datetime(2026, 4, 17, 11, 0, 0),
        )


def test_equal_start_and_end_allowed() -> None:
    """Sub-microsecond spans legitimately round to a zero-length interval."""
    instant = datetime(2026, 4, 17, 12, 0, 0)
    node = make_node(started_at=instant, ended_at=instant)
    assert node.duration_ms == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("duration_ms", -1.0),
        ("cost_usd", -0.01),
        ("tokens_prompt", -1),
        ("tokens_completion", -1),
        ("tokens_total", -1),
    ],
)
def test_negative_measurements_rejected(field: str, value: float) -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        make_node(**{field: value})


def test_attempt_below_one_rejected() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        make_node(attempt=0)


def test_duration_derived_when_absent() -> None:
    node = make_node(
        started_at=datetime(2026, 4, 17, 0, 0, 0),
        ended_at=datetime(2026, 4, 17, 0, 0, 1, 500_000),
    )
    assert node.duration_ms == pytest.approx(1500.0)


def test_supplied_duration_is_never_overwritten() -> None:
    """A trace's own number is evidence; Atlas preserves it (plan §22.7)."""
    node = make_node(
        started_at=datetime(2026, 4, 17, 0, 0, 0),
        ended_at=datetime(2026, 4, 17, 0, 0, 10),
        duration_ms=9999.0,
    )
    assert node.duration_ms == 9999.0


def test_duration_disagreement_surfaces_bad_instrumentation() -> None:
    node = make_node(
        started_at=datetime(2026, 4, 17, 0, 0, 0),
        ended_at=datetime(2026, 4, 17, 0, 0, 10),
        duration_ms=11_000.0,
    )
    assert node.duration_disagreement_ms == pytest.approx(1000.0)


def test_duration_disagreement_is_none_without_both_timestamps() -> None:
    assert make_node(started_at=None, ended_at=None).duration_disagreement_ms is None
    assert Node(id="s1", name="n", duration_ms=5).duration_disagreement_ms is None


def test_offset_aware_timestamps_normalized_to_naive_utc() -> None:
    """MASEF emits naive stamps; other adapters emit aware ones. Mixing them
    raises TypeError on the first comparison, so Atlas picks one."""
    plus_two = timezone(timedelta(hours=2))
    node = make_node(
        started_at=datetime(2026, 4, 17, 14, 0, 0, tzinfo=plus_two),
        ended_at=datetime(2026, 4, 17, 15, 0, 0, tzinfo=plus_two),
    )
    assert node.started_at == datetime(2026, 4, 17, 12, 0, 0)
    assert node.started_at.tzinfo is None
    assert node.duration_ms == pytest.approx(3_600_000.0)


def test_to_naive_utc_passes_through_naive_values() -> None:
    naive = datetime(2026, 4, 17, 12, 0, 0)
    assert to_naive_utc(naive) is naive


@pytest.mark.parametrize(
    "field",
    ["status_message", "agent", "tool", "model", "input_value", "output_value"],
)
def test_blank_optional_strings_collapse_to_none(field: str) -> None:
    assert getattr(make_node(**{field: ""}), field) is None


def test_iso_string_timestamps_parsed() -> None:
    """Ingestion hands Pydantic the raw MASEF string form."""
    node = make_node(
        started_at="2026-04-17T00:34:02.246968",
        ended_at="2026-04-17T00:34:16.258179",
    )
    assert node.started_at == datetime(2026, 4, 17, 0, 34, 2, 246968)


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        make_node(span_kind="LLM")


def test_failed_reflects_error_status_only() -> None:
    assert make_node(status=SpanStatus.ERROR).failed
    assert not make_node(status=SpanStatus.OK).failed
    assert not make_node(status=SpanStatus.UNSET).failed


def test_raw_span_is_preserved_verbatim() -> None:
    raw = {"name": "orchestrator", "attributes": {"session.id": "abc"}}
    assert make_node(raw=raw).raw == raw


def test_kind_and_status_accept_masef_strings() -> None:
    node = make_node(kind="TOOL", status="ERROR")
    assert node.kind is SpanKind.TOOL
    assert node.status is SpanStatus.ERROR


def test_unrecognized_kind_rejected_rather_than_silently_unknown() -> None:
    with pytest.raises(ValidationError):
        make_node(kind="GUARDRAIL")
