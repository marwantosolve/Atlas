"""Run store, HTTP API and CLI (Phases 6-7)."""

from __future__ import annotations

import json

import pytest

from atlas.cli import main as cli_main
from atlas.ingestion import load_trace
from atlas.store import RunStore, VERDICTS_KEY
from tests.conftest import EXAMPLES

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from atlas.api import create_app  # noqa: E402


@pytest.fixture
def refund_trace() -> dict:
    return json.loads((EXAMPLES / "refund_run.json").read_text(encoding="utf-8"))


@pytest.fixture
def store(tmp_path) -> RunStore:
    return RunStore(tmp_path / "runs")


# ── run store ─────────────────────────────────────────────────────────


def test_store_persists_and_reloads(tmp_path, refund_trace) -> None:
    first = RunStore(tmp_path / "runs")
    first.add_trace(refund_trace)
    assert len(first) == 1

    reloaded = RunStore(tmp_path / "runs")
    assert len(reloaded) == 1
    stored = reloaded.get("session_refund_001")
    assert stored is not None
    assert stored.analysis.summary.status == "degraded"
    assert stored.analysis.root_causes.primary is not None


def test_store_survives_foreign_json(tmp_path, refund_trace) -> None:
    """A directory with someone else's JSON in it still loads."""
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "not-a-run.json").write_text('{"hello": "world"}')
    store = RunStore(tmp_path / "runs")
    store.add_trace(refund_trace)
    reloaded = RunStore(tmp_path / "runs")
    assert len(reloaded) == 1


def test_store_verdicts_key_is_stripped_from_the_trace(store, refund_trace) -> None:
    refund_trace[VERDICTS_KEY] = [
        {
            "node_id": "f60708192a3b4c5d",
            "kind": "unknown",
            "message": "Contradicts the policy text",
            "source": "masef",
        }
    ]
    analyses = store.add_trace(refund_trace)
    assert analyses[0].summary.failure_count == 2
    # The stored run is a pure MASEF document: no atlas_verdicts key survived.
    assert VERDICTS_KEY not in (refund_trace)


def test_store_summaries_most_recent_first(tmp_path) -> None:
    import datetime as dt

    from tests.conftest import make_node, make_run

    store = RunStore(tmp_path / "runs")
    older = make_run(
        [make_node("s1")],
        started_at=dt.datetime(2026, 1, 1),
        ended_at=dt.datetime(2026, 1, 1, 0, 0, 5),
    )
    newer = make_run(
        [make_node("s2")],
        id="session_2",
        started_at=dt.datetime(2026, 2, 1),
        ended_at=dt.datetime(2026, 2, 1, 0, 0, 5),
    )
    store.add_run(older)
    store.add_run(newer)
    assert [a.summary.run_id for a in store.summaries()] == ["session_2", "session_1"]


def test_safe_filename_never_escapes_the_directory(tmp_path) -> None:
    from tests.conftest import make_node, make_run

    evil = make_run([make_node("s1")], id="../../etc/passwd")
    store = RunStore(tmp_path / "runs")
    store.add_run(evil)
    files = list((tmp_path / "runs").glob("*.json"))
    assert len(files) == 1
    assert files[0].parent == tmp_path / "runs"
    assert store.get("../../etc/passwd") is not None


# ── HTTP API ──────────────────────────────────────────────────────────


@pytest.fixture
def client(store, refund_trace) -> TestClient:
    store.add_trace(refund_trace)
    return TestClient(create_app(store))


def test_list_runs(client) -> None:
    res = client.get("/api/runs")
    assert res.status_code == 200
    assert res.json()["runs"][0]["run_id"] == "session_refund_001"


def test_post_trace_roundtrip(store) -> None:
    client = TestClient(create_app(store))
    trace = json.loads((EXAMPLES / "minimal_run.json").read_text())
    res = client.post("/api/runs", json=trace)
    assert res.status_code == 201
    assert res.json()["runs"][0]["status"] == "ok"


def test_post_invalid_trace_is_a_400(store) -> None:
    client = TestClient(create_app(store))
    res = client.post("/api/runs", json={"not": "a trace"})
    assert res.status_code == 400
    assert "does not look like a MASEF trace" in res.json()["detail"]


def test_get_run_and_subresources(client) -> None:
    run_id = "session_refund_001"
    full = client.get(f"/api/runs/{run_id}")
    assert full.status_code == 200
    assert full.json()["summary"]["status"] == "degraded"

    graph = client.get(f"/api/runs/{run_id}/graph").json()
    assert len(graph["nodes"]) == 12
    assert len({(e["source"], e["target"]) for e in graph["edges"]}) >= 11

    diagnosis = client.get(f"/api/runs/{run_id}/diagnosis").json()
    assert diagnosis["root_causes"]["candidates"][0]["failure"]["node_id"] == (
        "293a4b5c6d7e8f90"
    )

    failures = client.get(f"/api/runs/{run_id}/failures").json()
    assert len(failures["failures"]) == 1


def test_unknown_run_is_a_404_with_named_ids(client) -> None:
    res = client.get("/api/runs/ghost")
    assert res.status_code == 404
    assert "ghost" in res.json()["detail"]


def test_graph_payload_has_no_retry_edges_before_detection(store, refund_trace) -> None:
    """The graph endpoint rebuilds from the stored run -- retry edges appear
    because it applies detection first, matching ``atlas analyze``."""
    store.add_trace(refund_trace)
    client = TestClient(create_app(store))
    edges = client.get("/api/runs/session_refund_001/graph").json()["edges"]
    retry_edges = [e for e in edges if e["type"] == "retry"]
    assert [(e["source"], e["target"]) for e in retry_edges] == [
        ("18293a4b5c6d7e8f", "293a4b5c6d7e8f90")
    ]


# ── CLI ───────────────────────────────────────────────────────────────


def test_cli_analyze_text(capsys) -> None:
    code = cli_main(["analyze", str(EXAMPLES / "refund_run.json")])
    out = capsys.readouterr().out
    assert code == 1  # failures were found
    assert "Run session_refund_001 -- DEGRADED" in out
    assert "exhausted_retries" in out
    assert "293a4b5c6d7e8f90" in out
    assert "blast radius" in out
    assert "propagation:" in out


def test_cli_analyze_json(capsys) -> None:
    code = cli_main(["analyze", str(EXAMPLES / "refund_run.json"), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["summary"]["run_id"] == "session_refund_001"
    assert payload["root_causes"]["candidates"][0]["failure"]["node_id"] == (
        "293a4b5c6d7e8f90"
    )


def test_cli_analyze_clean_run_exits_zero(capsys) -> None:
    code = cli_main(["analyze", str(EXAMPLES / "minimal_run.json")])
    out = capsys.readouterr().out
    assert code == 0
    assert "no failure was detected" in out


def test_cli_analyze_missing_file(capsys) -> None:
    assert cli_main(["analyze", "/nonexistent/trace.json"]) == 2
    assert "error:" in capsys.readouterr().err


def test_cli_analyze_with_verdicts_file(tmp_path, capsys) -> None:
    verdicts = [
        {
            "node_id": "f60708192a3b4c5d",
            "kind": "unknown",
            "message": "Contradicts the policy text",
            "source": "masef",
        }
    ]
    path = tmp_path / "verdicts.json"
    path.write_text(json.dumps(verdicts))
    code = cli_main(
        ["analyze", str(EXAMPLES / "refund_run.json"), "--verdicts", str(path)]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert out.count("at f60708192a3b4c5d") >= 1


def test_cli_analyze_persists_to_store(tmp_path, capsys) -> None:
    store_dir = tmp_path / "runs"
    cli_main(["analyze", str(EXAMPLES / "refund_run.json"), "--store", str(store_dir)])
    capsys.readouterr()
    store = RunStore(store_dir)
    assert len(store) == 1
    assert store.get("session_refund_001") is not None
