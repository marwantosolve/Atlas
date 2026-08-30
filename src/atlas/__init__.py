"""Atlas -- execution intelligence for agentic AI systems.

Atlas reconstructs an agent run as a span-level execution graph and explains,
deterministically, where a failure started and what it affected downstream.

Scope note: Atlas does not compute cost, latency percentiles, or critical-path
timing. MASEF already implements those, and Atlas consumes them rather than
reimplementing them. See docs/decisions.md.
"""

__version__ = "0.1.0"
