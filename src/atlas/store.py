"""The run store (Phase 6): analyzed runs, persisted as plain JSON.

One file per run under a directory, one :class:`StoredRun` document per file:
the normalized ``Run`` plus its ``RunAnalysis``. No database (plan §20) --
a directory of JSON files is inspectable, greppable, and trivially backed up,
which is the right shape for a debugging tool's local store.

The store is single-process by design: no locking, last-writer-wins on a
``run_id`` collision. Concurrent writers are a v2 problem, and pretending
otherwise now would add machinery nobody has asked to exercise.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from atlas.analysis import RunAnalysis, Verdict, analyze_run
from atlas.ingestion import load_trace_dict
from atlas.models import Run

_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")

# Verdicts ride inside the trace body under this key when a caller has an
# evaluator's judgments to seed detection with (ADR-008). Atlas strips the
# key before ingestion so the trace itself stays a pure MASEF document.
VERDICTS_KEY = "atlas_verdicts"


class StoredRun(BaseModel):
    """One run and its analysis, as persisted."""

    model_config = ConfigDict(extra="forbid")

    run: Run
    analysis: RunAnalysis


class RunStore:
    """A directory of analyzed runs with an in-memory index."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._index: dict[str, StoredRun] = {}
        self._load()

    # ── reading ────────────────────────────────────────────────────────

    def _load(self) -> None:
        for path in sorted(self.directory.glob("*.json")):
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                stored = StoredRun.model_validate(document)
            except (OSError, ValueError):
                # A file that is not a stored run does not take the store
                # down; it is someone else's file in the directory.
                continue
            self._index[stored.run.id] = stored

    def get(self, run_id: str) -> StoredRun | None:
        return self._index.get(run_id)

    def summaries(self) -> list[RunAnalysis]:
        """Every stored run's analysis, most recent run first."""
        return [
            stored.analysis
            for stored in sorted(
                self._index.values(),
                key=lambda s: s.run.started_at or datetime.min,
                reverse=True,
            )
        ]

    def __len__(self) -> int:
        return len(self._index)

    def __contains__(self, run_id: object) -> bool:
        return run_id in self._index

    # ── writing ────────────────────────────────────────────────────────

    def add_trace(self, trace: dict, *, source: str = "<api>") -> list[RunAnalysis]:
        """Ingest, analyze and persist every session in a MASEF trace.

        The trace may carry verdicts under ``atlas_verdicts`` (a list of
        ``{"node_id", "kind", "message", "source"}`` objects); they seed
        failure detection and are stripped before ingestion proper.
        """
        verdicts = _extract_verdicts(trace)
        runs = load_trace_dict(trace, source=source)
        return [self.add_run(run, verdicts=verdicts) for run in runs]

    def add_run(self, run: Run, *, verdicts: list[Verdict] | None = None) -> RunAnalysis:
        analysis = analyze_run(run, verdicts=verdicts)
        stored = StoredRun(run=run, analysis=analysis)
        self._index[run.id] = stored
        path = self.directory / f"{_safe_filename(run.id)}.json"
        path.write_text(stored.model_dump_json(indent=2), encoding="utf-8")
        return analysis

    def clear(self) -> None:
        """Remove every stored run. For tests and `atlas serve --fresh`."""
        for path in self.directory.glob("*.json"):
            path.unlink()
        self._index.clear()


def _safe_filename(run_id: str) -> str:
    """Run ids come from session data; only safe characters reach the path.

    The run's true id lives inside the document, so even a collision after
    sanitization is survivable: the index keys on the document's id, and the
    overwritten file is the older run with the same sanitized name.
    """
    cleaned = _SAFE_ID.sub("_", run_id).strip("._") or "run"
    return cleaned[:120]


def _extract_verdicts(trace: dict) -> list[Verdict] | None:
    raw = trace.pop(VERDICTS_KEY, None)
    if not raw:
        return None
    verdicts = [Verdict.model_validate(entry) for entry in raw]
    return verdicts or None
