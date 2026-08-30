"""Execution graph reconstruction (Phase 2).

Import :func:`build_execution_graph` and :class:`ExecutionGraph` from here.
"""

from atlas.graph.execution import ExecutionGraph, build_execution_graph, owning_spans

__all__ = [
    "ExecutionGraph",
    "build_execution_graph",
    "owning_spans",
]
