"""Trace ingestion (Phase 1).

The boundary between the MASEF wire format and the Atlas domain model. Import
:func:`load_trace` for files, :func:`load_trace_dict` for parsed JSON.
"""

from atlas.ingestion.attribution import (
    Attribution,
    build_node_agent_map,
    canonicalize,
    resolve_all,
    resolve_direct,
)
from atlas.ingestion.capability import l1_gaps, require_l1
from atlas.ingestion.errors import CapabilityError, TraceError, TraceFormatError
from atlas.ingestion.masef import load_run, load_trace, load_trace_dict

__all__ = [
    "Attribution",
    "CapabilityError",
    "TraceError",
    "TraceFormatError",
    "build_node_agent_map",
    "canonicalize",
    "l1_gaps",
    "load_run",
    "load_trace",
    "load_trace_dict",
    "require_l1",
    "resolve_all",
    "resolve_direct",
]
