"""The deterministic tool surface the LLM may call.

The rules that keep the LLM honest live here, not in the prompt:

- every tool is a pure read over one stored run's analysis;
- every tool returns pipeline output, never a re-derivation;
- tools take no arguments, so the model cannot steer a query toward invented
  inputs -- the run is fixed by ``ask --run``.

Adding a tool means adding a function here; the schema generation in
``atlas.llm`` reads ``__doc__`` and assumes no parameters, which is deliberate:
each tool is "give me fact X about this run".
"""

from __future__ import annotations

from typing import Any, Callable

Tools = dict[str, Callable[[], dict]]


def build_tools(stored: Any) -> Tools:
    """Build the read-only tool surface for one stored run."""
    analysis = stored.analysis
    run = stored.run

    def get_run() -> dict:
        """Run identity, outcome status, scale, duration, and input query."""
        return analysis.summary.model_dump()

    def get_agents() -> dict:
        """Every agent in the run and the span ids of its owning steps."""
        return analysis.agents

    def get_failures() -> dict:
        """Every detected failure with its evidence and blast radius."""
        return analysis.failures.model_dump()

    def get_root_cause() -> dict:
        """The ranked root-cause candidates with evidence chains and
        propagation paths."""
        return analysis.root_causes.model_dump()

    def get_retry_waste() -> dict:
        """Retry groups and the latency/cost wasted on superseded attempts."""
        return analysis.retry_waste.model_dump()

    def get_unjoined_handoffs() -> dict:
        """Traversed agent handoffs that could not be joined to spans -- the
        honest coverage statement for the dataflow signal."""
        return {"unjoined_handoffs": analysis.unjoined_handoffs}

    def get_final_output() -> dict:
        """The run's final answer text and the user's original query."""
        return {
            "input_query": run.input_query,
            "final_output": run.final_output,
        }

    return {
        "get_run": get_run,
        "get_agents": get_agents,
        "get_failures": get_failures,
        "get_root_cause": get_root_cause,
        "get_retry_waste": get_retry_waste,
        "get_unjoined_handoffs": get_unjoined_handoffs,
        "get_final_output": get_final_output,
    }
