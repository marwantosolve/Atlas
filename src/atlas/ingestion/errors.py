"""Ingestion failures.

Every message must name the offending path. MASEF's validator follows the same
rule, and it matters more for Atlas than for most tools: a debugger that says
"invalid trace" has failed at the one job that distinguishes it from a log
viewer.
"""

from __future__ import annotations


class TraceError(ValueError):
    """Base class for anything that stops a trace from becoming Runs."""


class TraceFormatError(TraceError):
    """The document is not a MASEF trace, or a required key is absent."""


class CapabilityError(TraceError):
    """A structurally valid MASEF trace that is below Atlas's minimum level.

    Atlas needs L1 (a call tree). An L0 trace is a legitimate MASEF document --
    it just cannot answer the questions Atlas exists to answer, and guessing a
    tree from span ordering would produce confident nonsense.
    """

    def __init__(self, message: str, *, level: str, missing: list[str]) -> None:
        super().__init__(message)
        self.level = level
        self.missing = missing
