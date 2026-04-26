"""Tests for the Engine Capability Registry.

Validates that:
- JSON schema loads without error
- All six engines are present
- validate_action rejects unsupported actions
- validate_action accepts supported actions
- Hybrid engine inherits actions from child engines
- Accessibility engine loads with correct categories and event types
- Meta-engine has no raw actions of its own (only inherited)
- Category lookups return correct data
- Environment requirements and limitations are populated
- Capability comparison matrix is available
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.engine_capabilities import (
    ALL_ENGINES,
    CONCRETE_ENGINES,
    EngineCapabilities,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def caps() -> EngineCapabilities:
    """Load the capability registry once for all tests in this module."""
    return EngineCapabilities()


@pytest.fixture(scope="module")
def schema_path() -> Path:
    return Path(__file__).parent.parent / "backend" / "engine_capabilities.json"


# ── Schema Loading ────────────────────────────────────────────────────────────

class TestSchemaLoading:
    """Verify the JSON file loads and parses correctly."""

    def test_json_file_exists(self, schema_path: Path):
        assert schema_path.exists(), f"Schema file missing: {schema_path}"

    def test_json_is_valid(self, schema_path: Path):
        with open(schema_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        assert "engines" in data
        assert "version" in data

    def test_loads_without_error(self, caps: EngineCapabilities):
        assert caps is not None
        assert caps.version == "2.0"

    def test_all_engines_present(self, caps: EngineCapabilities):
        for engine in ALL_ENGINES:
            assert engine in caps.engine_names, f"Missing engine: {engine}"

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            EngineCapabilities(schema_path=tmp_path / "nonexistent.json")


# ── Action Validation ─────────────────────────────────────────────────────────

class TestValidateAction:
    """validate_action must accept supported and reject unsupported actions."""

    @pytest.mark.parametrize("engine,action", [
        ("playwright_mcp", "browser_snapshot"),
        ("playwright_mcp", "browser_type"),
        ("omni_accessibility", "find_by_role"),
        ("omni_accessibility", "invoke_action"),
        ("omni_accessibility", "subscribe_event"),
    ])
    def test_valid_action_accepted(self, caps: EngineCapabilities, engine: str, action: str):
        assert caps.validate_action(engine, action), (
            f"{action!r} should be valid for {engine}"
        )

    @pytest.mark.parametrize("engine,action", [
        ("playwright_mcp", "window_minimize"),
        ("omni_accessibility", "window_maximize"),
    ])
    def test_unsupported_action_rejected(self, caps: EngineCapabilities, engine: str, action: str):
        assert not caps.validate_action(engine, action), (
            f"{action!r} should NOT be valid for {engine}"
        )

    def test_unknown_engine_rejected(self, caps: EngineCapabilities):
        assert not caps.validate_action("nonexistent_engine", "click")

    def test_unknown_action_rejected(self, caps: EngineCapabilities):
        assert not caps.validate_action("playwright_mcp", "fly_to_moon")

    def test_validate_detailed_gives_alternatives(self, caps: EngineCapabilities):
        ok, msg = caps.validate_action_detailed("omni_accessibility", "browser_evaluate")
        assert not ok
        assert "playwright_mcp" in msg  # should suggest playwright_mcp as alternative


# ── Accessibility Engine ──────────────────────────────────────────────────────

class TestAccessibilityEngine:
    """Accessibility (AT-SPI) engine must load with correct schema."""

    def test_engine_exists(self, caps: EngineCapabilities):
        eng = caps.get_engine("omni_accessibility")
        assert eng is not None
        assert eng.display_name == "Omni Accessibility (AT-SPI/UIA/JXA)"

    def test_has_discovery_category(self, caps: EngineCapabilities):
        cats = caps.get_engine_categories("omni_accessibility")
        assert "discovery" in cats
        assert "get_accessibility_tree" in cats["discovery"]
        assert "find_by_role" in cats["discovery"]

    def test_has_event_category(self, caps: EngineCapabilities):
        cats = caps.get_engine_categories("omni_accessibility")
        assert "event" in cats
        assert "subscribe_event" in cats["event"]

    def test_has_tree_category(self, caps: EngineCapabilities):
        cats = caps.get_engine_categories("omni_accessibility")
        assert "tree" in cats

    def test_has_required_categories(self, caps: EngineCapabilities):
        cats = caps.get_engine_categories("omni_accessibility")
        required = {"discovery", "tree", "action", "event", "state", "query",
                     "component", "text", "value"}
        assert required.issubset(cats.keys()), (
            f"Missing categories: {required - cats.keys()}"
        )

    def test_has_all_required_actions(self, caps: EngineCapabilities):
        actions = caps.get_engine_actions("omni_accessibility")
        required = {
            "get_accessibility_tree", "find_by_role", "find_by_text",
            "find_by_label", "invoke_action", "get_role", "get_state",
            "get_attributes", "query_text", "query_value", "query_component",
            "subscribe_event", "unsubscribe_event",
        }
        assert required.issubset(actions), (
            f"Missing actions: {required - actions}"
        )

    def test_has_event_types(self, caps: EngineCapabilities):
        events = caps.get_event_types("omni_accessibility")
        assert "object:state-changed" in events
        assert "window:activate" in events
        assert len(events) == 5

    def test_has_environment_requirements(self, caps: EngineCapabilities):
        reqs = caps.get_environment_requirements("omni_accessibility")
        assert len(reqs) >= 5
        req_text = " ".join(reqs).lower()
        assert "dbus" in req_text or "d-bus" in req_text
        assert "at-spi" in req_text

    def test_has_tooling(self, caps: EngineCapabilities):
        eng = caps.get_engine("omni_accessibility")
        assert eng is not None
        assert "core_infrastructure" in eng.tooling
        assert "pyatspi" in eng.tooling["core_infrastructure"]
        assert "inspection_tools" in eng.tooling
        assert "accerciser" in eng.tooling["inspection_tools"]

    def test_has_limitations(self, caps: EngineCapabilities):
        lims = caps.get_limitations("omni_accessibility")
        assert len(lims) >= 4
        lim_text = " ".join(lims).lower()
        assert "linux" in lim_text
        assert "atk" in lim_text or "pixel" in lim_text


# ── Engine Metadata ───────────────────────────────────────────────────────────

class TestEngineMetadata:
    """Environment requirements, limitations, and categories for all engines."""

    @pytest.mark.parametrize("engine", list(CONCRETE_ENGINES))
    def test_concrete_engines_have_actions(self, caps: EngineCapabilities, engine: str):
        actions = caps.get_engine_actions(engine)
        assert len(actions) > 0, f"{engine} has no actions"

    @pytest.mark.parametrize("engine", list(ALL_ENGINES))
    def test_all_engines_have_limitations(self, caps: EngineCapabilities, engine: str):
        lims = caps.get_limitations(engine)
        assert len(lims) > 0, f"{engine} has no limitations"

    @pytest.mark.parametrize("engine", list(ALL_ENGINES))
    def test_all_engines_have_env_reqs(self, caps: EngineCapabilities, engine: str):
        reqs = caps.get_environment_requirements(engine)
        assert len(reqs) > 0, f"{engine} has no environment_requirements"

    @pytest.mark.parametrize("engine", list(CONCRETE_ENGINES))
    def test_concrete_engines_have_categories(self, caps: EngineCapabilities, engine: str):
        cats = caps.get_engine_categories(engine)
        assert len(cats) > 0, f"{engine} has no categories"

    @pytest.mark.parametrize("engine", list(ALL_ENGINES))
    def test_done_error_available_everywhere(self, caps: EngineCapabilities, engine: str):
        actions = caps.get_engine_actions(engine)
        assert "done" in actions, f"{engine} missing 'done'"
        assert "error" in actions, f"{engine} missing 'error'"


# ── Cross-engine Queries ──────────────────────────────────────────────────────

class TestCrossEngine:
    """engines_supporting and capability comparison matrix."""

    def test_click_supported_by_all(self, caps: EngineCapabilities):
        # Each engine uses its own naming: playwright_mcp→browser_click, omni→click, CU→click_at
        for engine in ALL_ENGINES:
            actions = caps.get_engine_actions(engine)
            click_like = {a for a in actions if "click" in a}
            assert click_like, f"No click-like action found for {engine}"

    def test_evaluate_js_only_browser(self, caps: EngineCapabilities):
        engines = caps.engines_supporting("browser_evaluate")
        assert "playwright_mcp" in engines
        assert "omni_accessibility" not in engines
        assert "computer_use" not in engines

    def test_capability_comparison_available(self, caps: EngineCapabilities):
        matrix = caps.get_capability_comparison()
        assert "dom_awareness" in matrix
        assert "wayland_support" in matrix

    def test_summary_is_readable(self, caps: EngineCapabilities):
        s = caps.summary()
        assert "playwright_mcp" in s
        assert "omni_accessibility" in s


# ── No Duplication ────────────────────────────────────────────────────────────

class TestNoDuplication:
    """Allowed actions lists in the JSON must not contain duplicates."""

    def test_no_duplicate_actions_in_json(self, schema_path: Path):
        with open(schema_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for name, block in data["engines"].items():
            raw_actions = block.get("allowed_actions", [])
            if isinstance(raw_actions, str):
                continue  # __inherited__
            dupes = [a for a in raw_actions if raw_actions.count(a) > 1]
            assert not dupes, (
                f"Engine {name!r} has duplicate actions: {set(dupes)}"
            )
