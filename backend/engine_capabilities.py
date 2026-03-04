"""Engine Capability Registry — structured, machine-readable engine metadata.

Loads ``engine_capabilities.json`` and exposes a typed Python API for:

* Action validation (reject unsupported actions before they hit the container)
* Capability filtering (list what an engine can do)
* Engine-specific schema injection (feed the model only valid actions)
* Hybrid engine routing (resolve ``__inherited__`` dynamically)
* Model action restriction (deterministic capability negotiation)

Usage::

    from backend.engine_capabilities import EngineCapabilities

    caps = EngineCapabilities()                       # auto-discovers JSON
    caps.validate_action("playwright", "click")       # → True
    caps.validate_action("xdotool", "evaluate_js")    # → False
    caps.get_engine_actions("desktop_hybrid")          # → union of child engines

The schema is **future-proof for Wayland** — ydotool and accessibility engines
already declare ``wayland_support: true`` in the comparison matrix.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_SCHEMA_FILENAME = "engine_capabilities.json"
_DEFAULT_SCHEMA_PATH = Path(__file__).parent / _SCHEMA_FILENAME

# Sentinel used in the JSON schema for meta-engines that inherit dynamically.
_INHERITED = "__inherited__"

# All concrete engine names.
CONCRETE_ENGINES: frozenset[str] = frozenset({
    "playwright_mcp",
    "omni_accessibility",
    "computer_use",
})

# Every engine name the system recognises.
ALL_ENGINES: frozenset[str] = CONCRETE_ENGINES


# ── Dataclass-like typed containers ───────────────────────────────────────────

class EngineSchema:
    """Typed representation of a single engine's capability block.

    Attributes:
        name:                     Engine identifier (e.g. ``"playwright"``).
        display_name:             Human-readable label.
        description:              Prose description of the engine.
        fallback_priority:        Lower = preferred.  ``0`` for meta-engines.
        is_meta_engine:           ``True`` for ``desktop_hybrid``.
        fallback_chain:           Ordered list of child engines (meta only).
        inherit_actions_from:     Engines whose actions are unioned (meta only).
        categories:               ``{category: [actions]}`` or ``"__inherited__"``.
        allowed_actions:          Flat set of all valid action strings.
        limitations:              Human-readable limitation list.
        environment_requirements: What the runtime needs.
        event_types:              AT-SPI event strings (accessibility only).
        tooling:                  AT-SPI tooling groups (accessibility only).
        notes:                    Arbitrary engine-level notes dict.
    """

    __slots__ = (
        "name", "display_name", "description", "fallback_priority",
        "is_meta_engine", "fallback_chain", "inherit_actions_from",
        "categories", "allowed_actions", "limitations",
        "environment_requirements", "event_types", "tooling", "notes",
    )

    def __init__(self, name: str, raw: Dict[str, Any]) -> None:
        self.name: str = name
        self.display_name: str = raw.get("display_name", name)
        self.description: str = raw.get("description", "")
        self.fallback_priority: int = raw.get("fallback_priority", 99)
        self.is_meta_engine: bool = raw.get("is_meta_engine", False)
        self.fallback_chain: List[str] = raw.get("fallback_chain", [])
        self.inherit_actions_from: List[str] = raw.get("inherit_actions_from", [])

        # Categories / actions may be ``"__inherited__"`` for meta-engines.
        raw_cats = raw.get("categories", {})
        self.categories: Dict[str, List[str]] | str = raw_cats

        raw_actions = raw.get("allowed_actions", [])
        if isinstance(raw_actions, str) and raw_actions == _INHERITED:
            self.allowed_actions: FrozenSet[str] = frozenset()  # resolved later
        else:
            self.allowed_actions = frozenset(raw_actions)

        self.limitations: List[str] = raw.get("limitations", [])
        self.environment_requirements: List[str] = raw.get("environment_requirements", [])
        self.event_types: List[str] = raw.get("event_types", [])
        self.tooling: Dict[str, List[str]] = raw.get("tooling", {})
        self.notes: Dict[str, Any] = raw.get("notes", {})

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"EngineSchema(name={self.name!r}, actions={len(self.allowed_actions)}, "
            f"meta={self.is_meta_engine})"
        )


# ── Main capability class ────────────────────────────────────────────────────

class EngineCapabilities:
    """Machine-readable engine capability registry.

    Parameters:
        schema_path: Path to ``engine_capabilities.json``.  Defaults to the
            file next to this module.

    Raises:
        FileNotFoundError: If the schema file does not exist.
        json.JSONDecodeError: If the schema file is malformed JSON.

    Example::

        caps = EngineCapabilities()
        assert caps.validate_action("playwright", "click")
        assert not caps.validate_action("xdotool", "evaluate_js")
    """

    def __init__(self, schema_path: str | Path | None = None) -> None:
        path = Path(schema_path) if schema_path else _DEFAULT_SCHEMA_PATH
        if not path.exists():
            raise FileNotFoundError(f"Engine capability schema not found: {path}")

        with open(path, "r", encoding="utf-8") as fh:
            raw: Dict[str, Any] = json.load(fh)

        self._version: str = raw.get("version", "unknown")
        self._raw: Dict[str, Any] = raw

        # Parse each engine block into EngineSchema objects.
        self._engines: Dict[str, EngineSchema] = {}
        for name, block in raw.get("engines", {}).items():
            self._engines[name] = EngineSchema(name, block)

        # Resolve inherited actions for meta-engines.
        self._resolve_inheritance()

        # Materialise a global action → engines reverse index.
        self._action_index: Dict[str, Set[str]] = {}
        for eng_name, eng in self._engines.items():
            for action in eng.allowed_actions:
                self._action_index.setdefault(action, set()).add(eng_name)

        logger.debug(
            "EngineCapabilities loaded: version=%s, engines=%d, total_actions=%d",
            self._version,
            len(self._engines),
            len(self._action_index),
        )

    # ── Inheritance resolution ────────────────────────────────────────────

    def _resolve_inheritance(self) -> None:
        """Resolve ``__inherited__`` markers on meta-engines.

        After this runs every ``EngineSchema.allowed_actions`` is a concrete
        ``frozenset[str]`` and ``categories`` is a merged dict.
        """
        for eng in self._engines.values():
            if not eng.is_meta_engine:
                continue
            parents: Sequence[str] = eng.inherit_actions_from
            if not parents:
                continue

            merged_actions: Set[str] = set()
            merged_cats: Dict[str, List[str]] = {}
            for parent_name in parents:
                parent = self._engines.get(parent_name)
                if parent is None:
                    logger.warning(
                        "Meta-engine %r references unknown parent %r",
                        eng.name, parent_name,
                    )
                    continue
                merged_actions |= parent.allowed_actions
                if isinstance(parent.categories, dict):
                    for cat, actions in parent.categories.items():
                        existing = merged_cats.setdefault(cat, [])
                        for a in actions:
                            if a not in existing:
                                existing.append(a)

            eng.allowed_actions = frozenset(merged_actions)
            if eng.categories == _INHERITED:
                eng.categories = merged_cats

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def version(self) -> str:
        """Schema version string."""
        return self._version

    @property
    def engine_names(self) -> FrozenSet[str]:
        """All registered engine names (including meta-engines)."""
        return frozenset(self._engines.keys())

    def get_engine(self, engine_name: str) -> Optional[EngineSchema]:
        """Return the full ``EngineSchema`` for *engine_name*, or ``None``."""
        return self._engines.get(engine_name)

    def get_engine_actions(self, engine_name: str) -> FrozenSet[str]:
        """Return the set of allowed actions for *engine_name*.

        For meta-engines (``desktop_hybrid``) this returns the **union** of all
        child-engine action sets — inheritance is already resolved.

        Returns an empty frozenset if the engine is unknown.
        """
        eng = self._engines.get(engine_name)
        if eng is None:
            return frozenset()
        return eng.allowed_actions

    def validate_action(self, engine_name: str, action: str) -> bool:
        """Return ``True`` if *action* is valid for *engine_name*.

        This is the primary guard used to reject unsupported actions before
        they reach the container agent service.
        """
        eng = self._engines.get(engine_name)
        if eng is None:
            return False
        return action in eng.allowed_actions

    def validate_action_detailed(
        self, engine_name: str, action: str
    ) -> Tuple[bool, str]:
        """Validate and return a ``(ok, message)`` tuple.

        On failure the message explains *why* and may suggest alternative
        engines that support the action.
        """
        eng = self._engines.get(engine_name)
        if eng is None:
            return False, f"Unknown engine: {engine_name!r}"

        if action in eng.allowed_actions:
            return True, ""

        # Build a helpful hint: which engines *do* support this action?
        alternatives = sorted(self._action_index.get(action, set()))
        if alternatives:
            alt_str = ", ".join(alternatives)
            return (
                False,
                f"Action {action!r} is not supported by {engine_name}. "
                f"Supported by: {alt_str}",
            )
        return (
            False,
            f"Action {action!r} is not supported by any registered engine.",
        )

    def get_engine_categories(self, engine_name: str) -> Dict[str, List[str]]:
        """Return ``{category: [actions]}`` for *engine_name*.

        Returns an empty dict for unknown engines or un-resolved meta-engines.
        """
        eng = self._engines.get(engine_name)
        if eng is None:
            return {}
        if isinstance(eng.categories, str):
            return {}  # still unresolved (should not happen after __init__)
        return dict(eng.categories)

    def get_environment_requirements(self, engine_name: str) -> List[str]:
        """Return the list of environment requirements for *engine_name*."""
        eng = self._engines.get(engine_name)
        if eng is None:
            return []
        return list(eng.environment_requirements)

    def get_limitations(self, engine_name: str) -> List[str]:
        """Return the list of known limitations for *engine_name*."""
        eng = self._engines.get(engine_name)
        if eng is None:
            return []
        return list(eng.limitations)

    def get_fallback_chain(self, engine_name: str) -> List[str]:
        """Return the fallback chain for a meta-engine, or ``[]``."""
        eng = self._engines.get(engine_name)
        if eng is None:
            return []
        return list(eng.fallback_chain)

    def get_event_types(self, engine_name: str) -> List[str]:
        """Return AT-SPI event types (accessibility engine only)."""
        eng = self._engines.get(engine_name)
        if eng is None:
            return []
        return list(eng.event_types)

    def engines_supporting(self, action: str) -> FrozenSet[str]:
        """Return the set of engine names that support *action*."""
        return frozenset(self._action_index.get(action, set()))

    def get_capability_comparison(self) -> Dict[str, Any]:
        """Return the raw capability comparison matrix from the schema."""
        return dict(self._raw.get("capability_comparison", {}).get("features", {}))

    # ── Utility ───────────────────────────────────────────────────────────

    def summary(self) -> str:
        """Return a multi-line human-readable summary string."""
        lines: list[str] = [
            f"Engine Capability Registry v{self._version}",
            f"{'Engine':<20} {'Actions':>8}  {'Meta':>5}  Priority",
            "-" * 55,
        ]
        for name in sorted(self._engines):
            eng = self._engines[name]
            lines.append(
                f"{name:<20} {len(eng.allowed_actions):>8}  "
                f"{'yes' if eng.is_meta_engine else 'no':>5}  "
                f"{eng.fallback_priority}"
            )
        return "\n".join(lines)
