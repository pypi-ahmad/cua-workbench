"""Bearer-token plumbing for the in-container ``agent_service``.

The container generates a 32-byte random token at startup
(``docker/entrypoint.sh``) and writes it to
``/run/secrets/agent_service_token``.  ``docker_manager.start_container``
then copies that file to a host-side 0600 tempfile so every call from
the host to ``agent_service`` can attach
``Authorization: Bearer <token>``.

Why a separate helper:
  * httpx clients are instantiated in many places (executor,
    screenshot, computer_use_engine, loop, server).  A single, lock-
    protected source of truth avoids passing the token through every
    constructor.
  * Tests that don't run a real container leave the token unset; the
    host helpers then send no header and the existing mocks keep
    working.

Closes F-008.  Implements I-002.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_token: str | None = None
_token_path: str | None = None


def set_token_path(path: str) -> None:
    """Read the bearer token from ``path`` and remember it for later use.

    Called by ``docker_manager.start_container`` after ``docker cp``
    extracts ``/run/secrets/agent_service_token`` from the running
    container.  Raises ``OSError`` if the file is unreadable so the
    caller can fail fast with a clear message rather than silently
    falling back to unauthenticated calls (which would 401 anyway).
    """
    global _token, _token_path
    with open(path, "r", encoding="utf-8") as fh:
        tok = fh.read().strip()
    if not tok:
        raise OSError(f"agent_service token file is empty: {path}")
    with _lock:
        _token = tok
        _token_path = path


def clear_token() -> None:
    """Forget the cached token (called from ``stop_container``)."""
    global _token, _token_path
    with _lock:
        _token = None
        _token_path = None


def get_auth_headers() -> dict[str, str]:
    """Return ``{"Authorization": "Bearer <token>"}`` if configured.

    Returns an empty dict when no token has been registered — this is
    the test path (no real container) and lets unit tests that mock
    httpx keep working without per-test fixtures.  Production paths
    register the token in ``start_container`` so every live call has
    the header.
    """
    with _lock:
        if _token:
            return {"Authorization": f"Bearer {_token}"}
        return {}
