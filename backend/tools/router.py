"""Engine router — pure dispatch only.

The router does NOT select or override engines.
The user-chosen engine is the only engine used for the entire session.
No fallback, no auto-detection, no intelligent routing.
"""

import logging

from backend.engine_capabilities import ALL_ENGINES

logger = logging.getLogger(__name__)

# Canonical set of supported engines (derived from engine_capabilities.json)
SUPPORTED_ENGINES: set[str] = set(ALL_ENGINES)


class InvalidEngineError(ValueError):
    """Raised when an unsupported engine is requested."""


def validate_engine(engine: str) -> str:
    """Validate that *engine* is a supported value.

    Returns the engine string unchanged.
    Raises InvalidEngineError if the engine is not in SUPPORTED_ENGINES.
    """
    if engine not in SUPPORTED_ENGINES:
        raise InvalidEngineError(
            f"Unsupported engine: {engine!r}. "
            f"Must be one of: {', '.join(sorted(SUPPORTED_ENGINES))}"
        )
    return engine
