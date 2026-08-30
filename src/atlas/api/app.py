"""The HTTP API (Phase 6) and the UI it serves (Phase 7).

The API is a serialization surface, not a logic one: every response is a
:class:`RunAnalysis` (or a direct projection of the stored run), computed by
the deterministic pipeline. That is the discipline that keeps the API, the
CLI and the LLM query layer from disagreeing with each other.

Run the server with ``atlas serve``; POST a MASEF trace to ``/api/runs`` and
read the diagnosis back.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from atlas.analysis import apply_retries
from atlas.graph import build_execution_graph
from atlas.ingestion import TraceError
from atlas.store import RunStore, StoredRun

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(store: RunStore) -> FastAPI:
    app = FastAPI(
        title="Atlas",
        description="Execution intelligence for agentic AI systems: deterministic "
        "failure propagation and root-cause localization over reconstructed "
        "execution graphs.",
        version="0.1.0",
    )
    app.state.store = store

    @app.get("/api/runs")
    def list_runs() -> dict:
        return {"runs": [analysis.summary.model_dump() for analysis in store.summaries()]}

    @app.post("/api/runs", status_code=201)
    def add_runs(trace: dict) -> dict:
        try:
            analyses = store.add_trace(trace)
        except TraceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            # Verdict validation and similar caller errors.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"runs": [analysis.summary.model_dump() for analysis in analyses]}

    def _stored(run_id: str) -> StoredRun:
        stored = store.get(run_id)
        if stored is None:
            raise HTTPException(
                status_code=404,
                detail=f"no run with id {run_id!r} in the store; "
                f"known runs: {len(store)}",
            )
        return stored

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        return _stored(run_id).analysis.model_dump()

    @app.get("/api/runs/{run_id}/graph")
    def get_graph(run_id: str) -> dict:
        stored = _stored(run_id)
        return _graph_payload(stored)

    @app.get("/api/runs/{run_id}/failures")
    def get_failures(run_id: str) -> dict:
        return _stored(run_id).analysis.failures.model_dump()

    @app.get("/api/runs/{run_id}/diagnosis")
    def get_diagnosis(run_id: str) -> dict:
        analysis = _stored(run_id).analysis
        return {
            "summary": analysis.summary.model_dump(),
            "root_causes": analysis.root_causes.model_dump(),
            "failures": analysis.failures.model_dump(),
            "retry_waste": analysis.retry_waste.model_dump(),
            "unjoined_handoffs": analysis.unjoined_handoffs,
        }

    # ── UI ──────────────────────────────────────────────────────────

    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(_STATIC_DIR / "index.html")

    return app


def _graph_payload(stored: StoredRun) -> dict:
    """Nodes and edges for rendering, derived from the stored run.

    Rebuilt rather than persisted: edges are derived data (ADR-006), and
    rebuilding keeps the graph payload from ever disagreeing with the run it
    claims to depict.
    """
    run = stored.run
    graph = build_execution_graph(apply_retries(run))
    nodes = [
        {
            "id": node.id,
            "name": node.name,
            "kind": node.kind.value,
            "status": node.status.value,
            "agent": node.agent,
            "agent_source": node.agent_source.value if node.agent_source else None,
            "tool": node.tool,
            "model": node.model,
            "attempt": node.attempt,
            "started_at": node.started_at.isoformat() if node.started_at else None,
            "ended_at": node.ended_at.isoformat() if node.ended_at else None,
            "duration_ms": node.duration_ms,
        }
        for node in run.nodes
    ]
    return {
        "run_id": run.id,
        "nodes": nodes,
        "edges": [edge.model_dump() for edge in graph.edges],
        "agents": graph.agents(),
        "unjoined_handoffs": graph.unjoined_handoffs,
    }
