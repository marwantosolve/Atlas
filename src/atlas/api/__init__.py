"""The Atlas HTTP API (Phase 6).

Import :func:`create_app` and hand it a :class:`~atlas.store.RunStore`. The
UI's static files live in ``static/`` and are served by the same app.
"""

from atlas.api.app import create_app

__all__ = ["create_app"]
