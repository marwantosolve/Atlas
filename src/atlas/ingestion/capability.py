"""Atlas's L1 capability gate.

MASEF grades traces L0-L3 (``dashboard/app/validation/capability.py``). Atlas
reimplements only the L1 predicate, and deliberately implements it to the same
rule as MASEF's ``has_span_tree``: every span carries a non-empty ``span_id``,
a *present* ``parent_span_id`` key (null is fine -- that is a root), and both
``start_time`` and ``end_time``.

Why duplicate ~30 lines instead of importing MASEF: importing would make MASEF
a hard runtime dependency of Atlas ingestion, and MASEF is a Streamlit
application, not a library on PyPI. The predicate is small, stable and
versioned by the schema. Atlas does not reimplement the L2/L3 predicates
because it does not gate on them -- missing token counts reduce what Atlas can
say, they do not make a trace unusable.
"""

from __future__ import annotations

from typing import Any

from atlas.ingestion.errors import CapabilityError


def _spans(trace: dict[str, Any]):
    for session in trace.get("sessions") or []:
        if not isinstance(session, dict):
            continue
        for span in session.get("spans") or []:
            if isinstance(span, dict):
                yield span


def l1_gaps(trace: dict[str, Any]) -> list[str]:
    """Return the reasons ``trace`` falls short of L1, empty if it reaches it.

    Reported as at most one reason per kind rather than one per span: a trace
    exported without ``end_time`` is missing it on all 52 spans, and 52
    identical lines bury the finding instead of stating it.
    """
    gaps: list[str] = []
    saw_span = False
    missing_id = missing_parent_key = missing_timing = 0

    for span in _spans(trace):
        saw_span = True
        if not span.get("span_id"):
            missing_id += 1
        if "parent_span_id" not in span:
            missing_parent_key += 1
        if not (span.get("start_time") and span.get("end_time")):
            missing_timing += 1

    if not saw_span:
        gaps.append("the trace contains no spans, so there is no call tree to build")
        return gaps

    if missing_id:
        gaps.append(f"{missing_id} span(s) have no span_id; nodes cannot be identified")
    if missing_parent_key:
        gaps.append(
            f"{missing_parent_key} span(s) omit the parent_span_id key entirely; "
            "an absent key is not the same as a null parent and Atlas will not "
            "assume either"
        )
    if missing_timing:
        gaps.append(
            f"{missing_timing} span(s) lack start_time or end_time; ordering and "
            "duration would have to be guessed"
        )
    return gaps


def require_l1(trace: dict[str, Any], *, source: str) -> None:
    """Raise :class:`CapabilityError` unless ``trace`` reaches L1."""
    gaps = l1_gaps(trace)
    if not gaps:
        return
    detail = "; ".join(gaps)
    raise CapabilityError(
        f"{source}: trace is below Atlas's minimum capability level (needs "
        f"MASEF L1, a reconstructable call tree): {detail}",
        level="L0",
        missing=gaps,
    )
