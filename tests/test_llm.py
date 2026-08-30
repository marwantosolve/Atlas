"""The thin optional LLM layer (Phase 7).

The tests run without the SDK, a network, or an API key: they pin the contract
(tools are deterministic reads; ask fails with an actionable message when
unavailable) and simulate the Anthropic tool-use loop with a stub client.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from atlas.ingestion import load_trace
from atlas.llm import AskUnavailable, ask
from atlas.llm.tools import build_tools
from atlas.store import RunStore
from tests.conftest import EXAMPLES


@pytest.fixture
def store(tmp_path) -> RunStore:
    run = load_trace(EXAMPLES / "refund_run.json")[0]
    store = RunStore(tmp_path / "runs")
    store.add_run(run)
    return store


@pytest.fixture
def stored(store) -> Any:
    return store.get("session_refund_001")


# ── the deterministic tool surface ────────────────────────────────────


def test_every_tool_is_a_read_over_the_analysis(stored) -> None:
    tools = build_tools(stored)
    assert sorted(tools) == [
        "get_agents",
        "get_failures",
        "get_final_output",
        "get_retry_waste",
        "get_root_cause",
        "get_run",
        "get_unjoined_handoffs",
    ]
    # The root-cause tool returns the pipeline's ranking, byte for byte.
    payload = tools["get_root_cause"]()
    assert payload["candidates"][0]["failure"]["node_id"] == "293a4b5c6d7e8f90"
    # Tools carry the documentation the model reads to choose them.
    for name, fn in tools.items():
        assert fn.__doc__ and fn.__doc__.strip(), name


def test_tools_return_serializable_payloads(stored) -> None:
    for fn in build_tools(stored).values():
        json.dumps(fn(), default=str)


# ── availability ──────────────────────────────────────────────────────


def test_ask_without_key_is_actionable(monkeypatch, store) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(AskUnavailable, match="ANTHROPIC_API_KEY"):
        ask("why did this run fail?", run_id="session_refund_001", store=store)


def test_ask_for_unknown_run(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    store = RunStore(tmp_path / "runs")
    with pytest.raises(AskUnavailable, match="no run with id 'ghost'"):
        ask("why?", run_id="ghost", store=store)


# ── the tool-use loop, with the client stubbed ────────────────────────


class _Block:
    def __init__(self, kind: str, **kw: Any) -> None:
        self.type = kind
        for key, value in kw.items():
            setattr(self, key, value)


class _Response:
    def __init__(self, content: list, stop_reason: str) -> None:
        self.content = content
        self.stop_reason = stop_reason


class _StubMessages:
    """Replays a scripted conversation: one tool call, then one answer."""

    def __init__(self, tool_name: str, answer: str, calls: list) -> None:
        self._tool_name = tool_name
        self._answer = answer
        self._calls = calls

    def create(self, *, model: str, messages: list[dict], **kw: Any) -> _Response:
        self._calls.append(messages)
        if len(messages) == 1:  # first turn: the model asks for a tool
            return _Response(
                [_Block("tool_use", id="tu_1", name=self._tool_name, input={})],
                "tool_use",
            )
        return _Response([_Block("text", text=self._answer)], "end_turn")


def _install_anthropic_stub(monkeypatch, messages_api) -> None:
    """Make ``import anthropic`` inside atlas.llm yield our stub."""

    class _FakeAnthropic:
        def __init__(self, **kw: Any) -> None:
            self.messages = messages_api

    class _FakeSDK:
        Anthropic = _FakeAnthropic

    monkeypatch.setitem(sys.modules, "anthropic", _FakeSDK)


def test_ask_answers_from_tool_output(monkeypatch, store) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    calls: list[list[dict]] = []
    stub = _StubMessages(
        tool_name="get_root_cause",
        answer="The root cause was the CRM lookup timing out twice.",
        calls=calls,
    )
    _install_anthropic_stub(monkeypatch, stub)

    answer = ask(
        "why did this run fail?", run_id="session_refund_001", store=store
    )
    assert answer == "The root cause was the CRM lookup timing out twice."

    # The tool result the model saw was the pipeline's own JSON.
    second_turn = calls[-1]
    tool_result = second_turn[-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    payload = json.loads(tool_result["content"])
    assert payload["candidates"][0]["failure"]["node_id"] == "293a4b5c6d7e8f90"


def test_ask_uses_the_system_prompt(monkeypatch, store) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    captured: dict[str, Any] = {}

    class _CapturingMessages:
        def create(self, *, model: str, system: str, **kw: Any) -> _Response:
            captured["system"] = system
            captured["model"] = model
            return _Response([_Block("text", text="done")], "end_turn")

    _install_anthropic_stub(monkeypatch, _CapturingMessages())
    answer = ask("anything", run_id="session_refund_001", store=store)
    assert answer == "done"
    assert "deterministic" in captured["system"]
    assert "Never invent span ids" in captured["system"]
    assert captured["model"]
