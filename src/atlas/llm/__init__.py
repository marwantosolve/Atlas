"""The thin optional LLM layer: natural-language questions over deterministic facts.

The architecture this module implements is the one Atlas is pitched with: a
deterministic engine underneath, and an LLM above it as a *query and
explanation interface* -- never the analyst. The model never sees raw logs and
never decides what failed; it asks the query tools below for facts, then
explains them. Every fact in its answer comes from the pipeline, so the answer
is only as good as the graph -- which is the point: hallucination has nothing
to grow in.

Requires the ``llm`` extra (the Anthropic SDK) and ``ANTHROPIC_API_KEY``.
Without either, ``ask`` raises :class:`AskUnavailable` and every other Atlas
surface is unaffected.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from .tools import build_tools

_MODEL = "claude-sonnet-5"
_MAX_TURNS = 8

_SYSTEM_PROMPT = """\
You are the query interface for Atlas, a deterministic execution-intelligence
engine for agentic AI systems. You are answering an engineer's question about
one stored agent run.

Rules you must not break:
- Every factual claim comes from the tools. Never invent span ids, agents,
  latencies, or causes. If the tools do not answer the question, say so.
- Cite span ids and agent names exactly as the tools return them.
- The engine's root-cause ranking is dependency-based evidence, not certain
  truth: present the top candidate with its evidence, not as a verdict.
- Be concise. The engineer wants the answer, the evidence, and the span ids.
"""


class AskUnavailable(RuntimeError):
    """Raised when the LLM layer cannot run: missing key or missing SDK.

    Callers surface this as an actionable message, not a stack trace.
    """


def ask(question: str, *, run_id: str, store: Any) -> str:
    """Answer ``question`` about ``run_id`` using the deterministic tools.

    ``store`` is a :class:`~atlas.store.RunStore` (duck-typed so tests can
    pass a fake without importing the LLM stack).
    """
    stored = store.get(run_id)
    if stored is None:
        raise AskUnavailable(
            f"no run with id {run_id!r} in the store; known runs: {len(store)}"
        )

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise AskUnavailable(
            "the ask command needs ANTHROPIC_API_KEY in the environment; "
            "set it or use `atlas analyze` for the deterministic report"
        )
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on extras
        raise AskUnavailable(
            f"the ask command needs the llm extras (`uv sync --extra llm`): {exc}"
        ) from exc

    tools = build_tools(stored)
    tool_schemas = [
        {
            "name": name,
            "description": fn.__doc__.strip(),
            "input_schema": {"type": "object", "properties": {}},
        }
        for name, fn in tools.items()
    ]

    client = anthropic.Anthropic(api_key=key)
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]

    for _ in range(_MAX_TURNS):
        response = client.messages.create(
            model=_MODEL,
            max_tokens=1500,
            system=_SYSTEM_PROMPT,
            tools=tool_schemas,
            messages=messages,
        )
        if response.stop_reason != "tool_use":
            return _text(response)
        messages.append({"role": "assistant", "content": response.content})
        results: list[dict[str, Any]] = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            fn = tools.get(block.name)
            payload = fn() if fn is not None else {"error": f"unknown tool {block.name}"}
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(payload, default=str),
                }
            )
        messages.append({"role": "user", "content": results})

    return (
        "The question could not be answered within the tool-call budget; "
        "the deterministic report (`atlas analyze`) has the full picture."
    )


def _text(response: Any) -> str:
    parts = [
        block.text for block in response.content if getattr(block, "type", "") == "text"
    ]
    return "\n".join(parts).strip() or "(the model returned no text)"
