"""Engine Certification Test Suite.

Comprehensive pytest-based validation of every engine and tool declared in
``engine_capabilities.json``.  Tests are **data-driven** — engine names and
action lists are read dynamically from the schema so the suite auto-extends
when new engines are added.

Run:
    pytest tests/test_engine_certification.py -v

Deep execution probes (requires live container environment):
    pytest tests/test_engine_certification.py -v -m integration
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Set

import pytest

from backend.health.engine_certifier import (
    CertificationReport,
    EngineCertifier,
    EngineReport,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def schema_path() -> Path:
    """Resolve the canonical schema path."""
    return Path(__file__).parent.parent / "backend" / "engine_capabilities.json"


@pytest.fixture(scope="module")
def schema_raw(schema_path: Path) -> Dict[str, Any]:
    """Load the raw JSON schema once for the module."""
    with open(schema_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def certifier(schema_path: Path) -> EngineCertifier:
    """Instantiate the certifier once for the module."""
    return EngineCertifier(schema_path=schema_path)


@pytest.fixture(scope="module")
def full_report(certifier: EngineCertifier) -> CertificationReport:
    """Run full (non-deep) certification once for the module."""
    return certifier.run_full_certification(deep=False)


@pytest.fixture(scope="module")
def engine_names(schema_raw: Dict[str, Any]) -> List[str]:
    """All engine names from the schema, sorted."""
    return sorted(schema_raw.get("engines", {}).keys())


@pytest.fixture(scope="module")
def concrete_engines(schema_raw: Dict[str, Any]) -> List[str]:
    """Concrete (non-meta) engine names from the schema."""
    engines = schema_raw.get("engines", {})
    return sorted(
        name for name, block in engines.items()
        if not block.get("is_meta_engine", False)
    )


@pytest.fixture(scope="module")
def meta_engines(schema_raw: Dict[str, Any]) -> List[str]:
    """Meta-engine names from the schema."""
    engines = schema_raw.get("engines", {})
    return sorted(
        name for name, block in engines.items()
        if block.get("is_meta_engine", False)
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _flatten_categories(categories: Dict[str, List[str]]) -> Set[str]:
    """Flatten a categories dict into a flat set of action names."""
    result: Set[str] = set()
    for actions in categories.values():
        if isinstance(actions, list):
            result.update(actions)
    return result


def _get_engine_report(report: CertificationReport, engine_name: str) -> EngineReport:
    """Look up a single engine's report from the aggregate."""
    for eng in report.engines:
        if eng.engine == engine_name:
            return eng
    raise KeyError(f"No report for engine '{engine_name}'")


# ══════════════════════════════════════════════════════════════════════════════
# Phase 1 — Schema Integrity
# ══════════════════════════════════════════════════════════════════════════════

class TestSchemaIntegrity:
    """Verify top-level schema structure and required fields."""

    def test_schema_file_exists(self, schema_path: Path) -> None:
        assert schema_path.exists(), f"Schema file missing: {schema_path}"

    def test_schema_valid_json(self, schema_path: Path) -> None:
        with open(schema_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        assert isinstance(data, dict)

    def test_schema_has_version(self, schema_raw: Dict[str, Any]) -> None:
        assert "version" in schema_raw, "Missing 'version' field"

    def test_schema_has_engines(self, schema_raw: Dict[str, Any]) -> None:
        assert "engines" in schema_raw, "Missing 'engines' field"
        assert len(schema_raw["engines"]) > 0, "No engines defined"

    def test_validate_schema_integrity_passes(self, certifier: EngineCertifier) -> None:
        issues = certifier.validate_schema_integrity()
        assert issues == [], f"Schema integrity issues: {issues}"

    def test_every_engine_has_display_name(
        self, schema_raw: Dict[str, Any], engine_names: List[str]
    ) -> None:
        for name in engine_names:
            block = schema_raw["engines"][name]
            assert "display_name" in block, f"[{name}] missing display_name"
            assert block["display_name"].strip(), f"[{name}] empty display_name"

    def test_every_engine_has_categories(
        self, schema_raw: Dict[str, Any], engine_names: List[str]
    ) -> None:
        for name in engine_names:
            block = schema_raw["engines"][name]
            cats = block.get("categories")
            assert cats is not None, f"[{name}] missing categories"
            assert isinstance(cats, (dict, str)), f"[{name}] categories is {type(cats)}"

    def test_every_engine_has_allowed_actions(
        self, schema_raw: Dict[str, Any], engine_names: List[str]
    ) -> None:
        for name in engine_names:
            block = schema_raw["engines"][name]
            actions = block.get("allowed_actions")
            assert actions is not None, f"[{name}] missing allowed_actions"

    def test_fallback_priority_unique(
        self, schema_raw: Dict[str, Any], engine_names: List[str]
    ) -> None:
        seen: Dict[int, str] = {}
        for name in engine_names:
            block = schema_raw["engines"][name]
            prio = block.get("fallback_priority")
            if prio is not None and prio != 0:
                assert prio not in seen, (
                    f"Duplicate fallback_priority {prio}: "
                    f"'{name}' and '{seen.get(prio)}'"
                )
                seen[prio] = name


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2 — Engine Registration
# ══════════════════════════════════════════════════════════════════════════════

class TestEngineRegistration:
    """Verify all engines are properly registered and well-formed."""

    def test_all_engines_registered(self, certifier: EngineCertifier) -> None:
        issues = certifier.validate_engine_registration()
        assert issues == [], f"Registration issues: {issues}"

    def test_engine_count_at_least_one(self, engine_names: List[str]) -> None:
        assert len(engine_names) >= 1

    def test_no_meta_engines(self, meta_engines: List[str]) -> None:
        assert len(meta_engines) == 0, f"Unexpected meta-engines: {meta_engines}"

    def test_at_least_one_concrete_engine(self, concrete_engines: List[str]) -> None:
        assert len(concrete_engines) >= 1, "No concrete engines found"

    def test_concrete_engines_have_dict_categories(
        self, schema_raw: Dict[str, Any], concrete_engines: List[str]
    ) -> None:
        for name in concrete_engines:
            cats = schema_raw["engines"][name].get("categories", {})
            assert isinstance(cats, dict), f"[{name}] categories should be dict, got {type(cats)}"

    def test_meta_engines_have_inherited_or_dict_categories(
        self, schema_raw: Dict[str, Any], meta_engines: List[str]
    ) -> None:
        for name in meta_engines:
            cats = schema_raw["engines"][name].get("categories")
            assert cats == "__inherited__" or isinstance(cats, dict), (
                f"[{name}] meta-engine categories should be '__inherited__' or dict"
            )

    def test_every_concrete_engine_has_environment_requirements(
        self, schema_raw: Dict[str, Any], concrete_engines: List[str]
    ) -> None:
        for name in concrete_engines:
            block = schema_raw["engines"][name]
            reqs = block.get("environment_requirements", [])
            assert isinstance(reqs, list), f"[{name}] environment_requirements not a list"
            assert len(reqs) > 0, f"[{name}] no environment_requirements"


# ══════════════════════════════════════════════════════════════════════════════
# Phase 3 — Environment Requirements
# ══════════════════════════════════════════════════════════════════════════════

class TestEnvironmentRequirements:
    """Validate binary, env-var, and filesystem dependencies.

    These tests verify the *certifier's ability to detect* missing deps.
    They pass regardless of whether deps are installed — the certifier must
    not crash and must produce a well-formed report.
    """

    def test_environment_validation_returns_report(
        self, certifier: EngineCertifier, engine_names: List[str]
    ) -> None:
        for name in engine_names:
            report = certifier.validate_environment_requirements(name)
            assert isinstance(report, EngineReport)
            assert report.engine == name

    def test_binary_validation_returns_list(
        self, certifier: EngineCertifier, engine_names: List[str]
    ) -> None:
        for name in engine_names:
            missing = certifier.validate_binary_dependencies(name)
            assert isinstance(missing, list), f"[{name}] expected list, got {type(missing)}"

    def test_unknown_engine_returns_unhealthy(
        self, certifier: EngineCertifier
    ) -> None:
        report = certifier.validate_environment_requirements("nonexistent_engine_xyz")
        assert not report.healthy
        assert len(report.schema_issues) > 0

    def test_env_report_fields_are_lists(
        self, certifier: EngineCertifier, engine_names: List[str]
    ) -> None:
        for name in engine_names:
            report = certifier.validate_environment_requirements(name)
            assert isinstance(report.missing_dependencies, list)
            assert isinstance(report.missing_env_vars, list)
            assert isinstance(report.missing_fs_paths, list)

    def test_no_duplicate_missing_deps(
        self, certifier: EngineCertifier, engine_names: List[str]
    ) -> None:
        for name in engine_names:
            report = certifier.validate_environment_requirements(name)
            assert len(report.missing_dependencies) == len(set(report.missing_dependencies)), (
                f"[{name}] duplicate missing_dependencies"
            )


# ══════════════════════════════════════════════════════════════════════════════
# Phase 4 — Action Consistency
# ══════════════════════════════════════════════════════════════════════════════

class TestActionConsistency:
    """Ensure categories ↔ allowed_actions parity."""

    def test_action_consistency_all_engines(
        self, certifier: EngineCertifier, engine_names: List[str]
    ) -> None:
        for name in engine_names:
            issues = certifier.validate_allowed_actions(name)
            assert issues == [], f"[{name}] action issues: {issues}"

    def test_no_empty_categories(
        self, schema_raw: Dict[str, Any], concrete_engines: List[str]
    ) -> None:
        for name in concrete_engines:
            cats = schema_raw["engines"][name].get("categories", {})
            for cat_name, actions in cats.items():
                assert len(actions) > 0, f"[{name}] empty category '{cat_name}'"

    def test_no_duplicate_actions_in_allowed(
        self, schema_raw: Dict[str, Any], concrete_engines: List[str]
    ) -> None:
        for name in concrete_engines:
            actions = schema_raw["engines"][name].get("allowed_actions", [])
            if isinstance(actions, list):
                assert len(actions) == len(set(actions)), (
                    f"[{name}] duplicate actions: "
                    f"{[a for a in actions if actions.count(a) > 1]}"
                )

    def test_categories_union_equals_allowed_actions(
        self, schema_raw: Dict[str, Any], concrete_engines: List[str]
    ) -> None:
        for name in concrete_engines:
            block = schema_raw["engines"][name]
            cats = block.get("categories", {})
            allowed = set(block.get("allowed_actions", []))

            cat_actions = _flatten_categories(cats)

            in_cats_not_allowed = cat_actions - allowed
            in_allowed_not_cats = allowed - cat_actions

            assert not in_cats_not_allowed, (
                f"[{name}] in categories but not allowed_actions: {in_cats_not_allowed}"
            )
            assert not in_allowed_not_cats, (
                f"[{name}] in allowed_actions but no category: {in_allowed_not_cats}"
            )

    def test_get_text_in_correct_engines(
        self, schema_raw: Dict[str, Any]
    ) -> None:
        """Spot-check: get_text should be in accessibility and playwright."""
        engines_with_get_text: Set[str] = set()
        for name, block in schema_raw["engines"].items():
            actions = block.get("allowed_actions", [])
            if isinstance(actions, list) and "get_text" in actions:
                engines_with_get_text.add(name)
        assert len(engines_with_get_text) >= 2, (
            f"Expected get_text in >=2 engines, found: {engines_with_get_text}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Phase 5 — Meta-Engine Inheritance
# ══════════════════════════════════════════════════════════════════════════════

class TestMetaEngineInheritance:
    """Validate meta-engine inheritance resolution."""

    def test_meta_engine_inheritance_valid(
        self, certifier: EngineCertifier, meta_engines: List[str]
    ) -> None:
        for name in meta_engines:
            issues = certifier.validate_meta_engine_inheritance(name)
            assert issues == [], f"[{name}] inheritance issues: {issues}"

    def test_meta_engine_inherits_from_known_engines(
        self, schema_raw: Dict[str, Any], meta_engines: List[str]
    ) -> None:
        all_names = set(schema_raw["engines"].keys())
        for name in meta_engines:
            parents = schema_raw["engines"][name].get("inherit_actions_from", [])
            for parent in parents:
                assert parent in all_names, (
                    f"[{name}] inherits from unknown engine '{parent}'"
                )

    def test_meta_engine_resolved_actions_is_superset(
        self, schema_raw: Dict[str, Any], meta_engines: List[str]
    ) -> None:
        """Resolved meta-engine actions must contain all parent actions."""
        from backend.engine_capabilities import EngineCapabilities
        caps = EngineCapabilities()

        for name in meta_engines:
            parents = schema_raw["engines"][name].get("inherit_actions_from", [])
            resolved = caps.get_engine_actions(name)

            for parent_name in parents:
                parent_actions = set(
                    schema_raw["engines"].get(parent_name, {}).get("allowed_actions", [])
                )
                missing = parent_actions - resolved
                assert not missing, (
                    f"[{name}] missing inherited actions from '{parent_name}': {missing}"
                )

    def test_meta_engine_resolved_actions_is_exact_union(
        self, schema_raw: Dict[str, Any], meta_engines: List[str]
    ) -> None:
        """Resolved meta-engine actions must be exactly the union of parents."""
        from backend.engine_capabilities import EngineCapabilities
        caps = EngineCapabilities()

        for name in meta_engines:
            parents = schema_raw["engines"][name].get("inherit_actions_from", [])
            expected_union: Set[str] = set()
            for parent_name in parents:
                parent_actions = schema_raw["engines"].get(parent_name, {}).get("allowed_actions", [])
                if isinstance(parent_actions, list):
                    expected_union.update(parent_actions)

            resolved = caps.get_engine_actions(name)
            assert resolved == expected_union, (
                f"[{name}] resolved != union. "
                f"Extra: {resolved - expected_union}, Missing: {expected_union - resolved}"
            )

    def test_no_circular_inheritance(
        self, schema_raw: Dict[str, Any]
    ) -> None:
        """Walk the inheritance graph and ensure no cycles."""
        engines = schema_raw.get("engines", {})

        def _walk(name: str, visited: Set[str]) -> None:
            assert name not in visited, f"Circular inheritance: {visited} -> {name}"
            visited.add(name)
            for parent in engines.get(name, {}).get("inherit_actions_from", []):
                _walk(parent, visited.copy())

        for name in engines:
            _walk(name, set())

    def test_concrete_engines_not_meta(
        self, schema_raw: Dict[str, Any], concrete_engines: List[str]
    ) -> None:
        for name in concrete_engines:
            block = schema_raw["engines"][name]
            assert not block.get("is_meta_engine", False), f"[{name}] should not be meta"
            assert not block.get("inherit_actions_from"), f"[{name}] should not inherit"


# ══════════════════════════════════════════════════════════════════════════════
# Phase 5b — Fallback Chain
# ══════════════════════════════════════════════════════════════════════════════

class TestFallbackChain:
    """Validate fallback chain structure and references."""

    def test_fallback_chain_valid(
        self, certifier: EngineCertifier, engine_names: List[str]
    ) -> None:
        for name in engine_names:
            issues = certifier.validate_fallback_chain(name)
            assert issues == [], f"[{name}] fallback issues: {issues}"

    def test_fallback_chain_references_known_engines(
        self, schema_raw: Dict[str, Any], engine_names: List[str]
    ) -> None:
        all_names = set(schema_raw["engines"].keys())
        for name in engine_names:
            chain = schema_raw["engines"][name].get("fallback_chain", [])
            for entry in chain:
                assert entry in all_names, (
                    f"[{name}] fallback_chain references unknown '{entry}'"
                )

    def test_fallback_chain_no_self_reference(
        self, schema_raw: Dict[str, Any], engine_names: List[str]
    ) -> None:
        for name in engine_names:
            chain = schema_raw["engines"][name].get("fallback_chain", [])
            assert name not in chain, f"[{name}] fallback_chain contains self-reference"

    def test_fallback_chain_no_duplicates(
        self, schema_raw: Dict[str, Any], engine_names: List[str]
    ) -> None:
        for name in engine_names:
            chain = schema_raw["engines"][name].get("fallback_chain", [])
            assert len(chain) == len(set(chain)), (
                f"[{name}] duplicate entries in fallback_chain"
            )

    def test_meta_engines_have_fallback_chain(
        self, schema_raw: Dict[str, Any], meta_engines: List[str]
    ) -> None:
        for name in meta_engines:
            chain = schema_raw["engines"][name].get("fallback_chain", [])
            assert len(chain) > 0, f"[{name}] meta-engine has no fallback_chain"

    def test_concrete_engines_no_fallback_chain(
        self, schema_raw: Dict[str, Any], concrete_engines: List[str]
    ) -> None:
        for name in concrete_engines:
            chain = schema_raw["engines"][name].get("fallback_chain", [])
            assert len(chain) == 0, (
                f"[{name}] concrete engine should not have fallback_chain"
            )


# ══════════════════════════════════════════════════════════════════════════════
# Phase 7 — Structured Report
# ══════════════════════════════════════════════════════════════════════════════

class TestCertificationReport:
    """Verify the full certification report structure and content."""

    def test_report_is_well_formed(self, full_report: CertificationReport) -> None:
        assert isinstance(full_report, CertificationReport)
        assert full_report.schema_version != "unknown"
        assert full_report.engine_count > 0
        assert len(full_report.engines) == full_report.engine_count

    def test_report_contains_all_engines(
        self, full_report: CertificationReport, engine_names: List[str]
    ) -> None:
        reported_names = sorted(e.engine for e in full_report.engines)
        assert reported_names == engine_names

    def test_report_serialisable(self, full_report: CertificationReport) -> None:
        data = full_report.to_dict()
        serialised = json.dumps(data)
        recovered = json.loads(serialised)
        assert recovered["schema_version"] == full_report.schema_version
        assert len(recovered["engines"]) == full_report.engine_count

    def test_engine_reports_have_all_fields(
        self, full_report: CertificationReport
    ) -> None:
        expected_keys = {
            "engine", "healthy", "schema_issues", "missing_dependencies",
            "missing_env_vars", "missing_fs_paths", "invalid_actions",
            "inheritance_issues", "fallback_issues", "execution_probe",
        }
        for eng in full_report.engines:
            data = eng.to_dict()
            assert set(data.keys()) == expected_keys, (
                f"[{eng.engine}] missing keys: {expected_keys - set(data.keys())}"
            )

    def test_no_schema_validation_failures(
        self, full_report: CertificationReport
    ) -> None:
        """Schema validation (action consistency, inheritance, fallback) all pass."""
        for eng in full_report.engines:
            assert eng.invalid_actions == [], (
                f"[{eng.engine}] invalid_actions: {eng.invalid_actions}"
            )
            assert eng.inheritance_issues == [], (
                f"[{eng.engine}] inheritance_issues: {eng.inheritance_issues}"
            )
            assert eng.fallback_issues == [], (
                f"[{eng.engine}] fallback_issues: {eng.fallback_issues}"
            )

    def test_all_engines_schema_healthy(
        self, full_report: CertificationReport
    ) -> None:
        """Every engine should be schema-healthy (deps are env-dependent)."""
        for eng in full_report.engines:
            assert eng.healthy, (
                f"[{eng.engine}] unhealthy: "
                f"actions={eng.invalid_actions}, "
                f"inherit={eng.inheritance_issues}, "
                f"fallback={eng.fallback_issues}"
            )

    def test_execution_probes_skipped_in_shallow(
        self, full_report: CertificationReport
    ) -> None:
        for eng in full_report.engines:
            assert eng.execution_probe == "skipped", (
                f"[{eng.engine}] probe should be 'skipped' in shallow mode"
            )

    def test_report_platform_populated(
        self, full_report: CertificationReport
    ) -> None:
        assert full_report.platform, "Platform string is empty"


# ══════════════════════════════════════════════════════════════════════════════
# Cross-Engine Validation
# ══════════════════════════════════════════════════════════════════════════════

class TestCrossEngineValidation:
    """Validate relationships across all engines."""

    def test_done_and_error_in_every_concrete_engine(
        self, schema_raw: Dict[str, Any], concrete_engines: List[str]
    ) -> None:
        """Every concrete engine should support 'done' and 'error' meta-actions."""
        for name in concrete_engines:
            actions = set(schema_raw["engines"][name].get("allowed_actions", []))
            assert "done" in actions, f"[{name}] missing 'done' action"
            assert "error" in actions, f"[{name}] missing 'error' action"

    def test_screenshot_in_every_concrete_engine(
        self, schema_raw: Dict[str, Any], concrete_engines: List[str]
    ) -> None:
        """Every concrete engine should support at least one screenshot action."""
        # computer_use takes screenshots via the native CU protocol, not an explicit action
        standard_engines = [e for e in concrete_engines if e != "computer_use"]
        for name in standard_engines:
            actions = set(schema_raw["engines"][name].get("allowed_actions", []))
            screenshot_actions = {a for a in actions if "screenshot" in a}
            assert screenshot_actions, f"[{name}] no screenshot actions found"

    def test_click_in_every_concrete_engine(
        self, schema_raw: Dict[str, Any], concrete_engines: List[str]
    ) -> None:
        """Every concrete engine should support 'click'."""
        # computer_use uses model-native 'click_at' instead of 'click'
        standard_engines = [e for e in concrete_engines if e != "computer_use"]
        for name in standard_engines:
            actions = set(schema_raw["engines"][name].get("allowed_actions", []))
            assert "click" in actions, f"[{name}] missing 'click' action"

    def test_type_in_every_concrete_engine(
        self, schema_raw: Dict[str, Any], concrete_engines: List[str]
    ) -> None:
        """Every concrete engine should support 'type'."""
        # computer_use uses model-native 'type_text_at' instead of 'type'
        standard_engines = [e for e in concrete_engines if e != "computer_use"]
        for name in standard_engines:
            actions = set(schema_raw["engines"][name].get("allowed_actions", []))
            assert "type" in actions, f"[{name}] missing 'type' action"

    def test_capability_comparison_covers_all_engines(
        self, schema_raw: Dict[str, Any], engine_names: List[str]
    ) -> None:
        """The capability_comparison matrix should reference all engines."""
        features = schema_raw.get("capability_comparison", {}).get("features", {})
        if not features:
            pytest.skip("No capability_comparison in schema")
        for feature_name, engine_map in features.items():
            for name in engine_names:
                assert name in engine_map, (
                    f"Engine '{name}' missing from capability_comparison.{feature_name}"
                )

    def test_no_action_in_zero_engines(
        self, schema_raw: Dict[str, Any], concrete_engines: List[str]
    ) -> None:
        """Every action mentioned in a category must appear in allowed_actions."""
        for name in concrete_engines:
            block = schema_raw["engines"][name]
            cats = block.get("categories", {})
            allowed = set(block.get("allowed_actions", []))
            for cat_name, cat_actions in cats.items():
                for action in cat_actions:
                    assert action in allowed, (
                        f"[{name}] action '{action}' in category '{cat_name}' "
                        f"but not in allowed_actions"
                    )


# ══════════════════════════════════════════════════════════════════════════════
# Certifier Construction & Edge Cases
# ══════════════════════════════════════════════════════════════════════════════

class TestCertifierEdgeCases:
    """Verify certifier handles edge cases gracefully."""

    def test_missing_schema_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            EngineCertifier(schema_path=tmp_path / "nonexistent.json")

    def test_malformed_schema(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not json at all", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            EngineCertifier(schema_path=bad_file)

    def test_empty_schema(self, tmp_path: Path) -> None:
        empty_file = tmp_path / "empty.json"
        empty_file.write_text("{}", encoding="utf-8")
        certifier = EngineCertifier(schema_path=empty_file)
        issues = certifier.validate_schema_integrity()
        assert any("version" in i.lower() for i in issues)

    def test_validate_unknown_engine_actions(
        self, certifier: EngineCertifier
    ) -> None:
        issues = certifier.validate_allowed_actions("nonexistent_engine")
        assert len(issues) > 0
        assert "not in schema" in issues[0].lower()

    def test_validate_unknown_engine_fallback(
        self, certifier: EngineCertifier
    ) -> None:
        issues = certifier.validate_fallback_chain("nonexistent_engine")
        assert len(issues) > 0

    def test_probe_unknown_engine(self, certifier: EngineCertifier) -> None:
        result = certifier.probe_execution("nonexistent_engine")
        assert result.startswith("skip")

    def test_schema_with_missing_fields(self, tmp_path: Path) -> None:
        """Schema with an engine missing required fields should report issues."""
        data = {
            "version": "test",
            "engines": {
                "broken_engine": {
                    "fallback_priority": 99,
                }
            },
        }
        schema_file = tmp_path / "partial.json"
        schema_file.write_text(json.dumps(data), encoding="utf-8")

        certifier = EngineCertifier(schema_path=schema_file)
        issues = certifier.validate_schema_integrity()
        assert any("display_name" in i for i in issues)
        assert any("categories" in i for i in issues)
        assert any("allowed_actions" in i for i in issues)

    def test_schema_with_duplicate_fallback_priority(self, tmp_path: Path) -> None:
        data = {
            "version": "test",
            "engines": {
                "engine_a": {
                    "display_name": "A",
                    "fallback_priority": 1,
                    "categories": {},
                    "allowed_actions": [],
                },
                "engine_b": {
                    "display_name": "B",
                    "fallback_priority": 1,
                    "categories": {},
                    "allowed_actions": [],
                },
            },
        }
        schema_file = tmp_path / "dup_prio.json"
        schema_file.write_text(json.dumps(data), encoding="utf-8")

        certifier = EngineCertifier(schema_path=schema_file)
        issues = certifier.validate_schema_integrity()
        assert any("duplicate fallback_priority" in i.lower() for i in issues)

    def test_meta_engine_with_bad_parent(self, tmp_path: Path) -> None:
        data = {
            "version": "test",
            "engines": {
                "meta": {
                    "display_name": "Meta",
                    "is_meta_engine": True,
                    "fallback_priority": 0,
                    "inherit_actions_from": ["ghost_engine"],
                    "fallback_chain": ["ghost_engine"],
                    "categories": "__inherited__",
                    "allowed_actions": "__inherited__",
                },
            },
        }
        schema_file = tmp_path / "bad_parent.json"
        schema_file.write_text(json.dumps(data), encoding="utf-8")

        certifier = EngineCertifier(schema_path=schema_file)
        issues = certifier.validate_fallback_chain("meta")
        assert any("unknown engine" in i.lower() for i in issues)


# ══════════════════════════════════════════════════════════════════════════════
# Phase 6 — Deep Execution Probes (integration marker)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
class TestExecutionProbes:
    """Run deep execution probes — requires live container environment.

    These tests are skipped by default.  Run with:
        pytest tests/test_engine_certification.py -v -m integration
    """

    def test_deep_certification_runs(self, certifier: EngineCertifier) -> None:
        report = certifier.run_full_certification(deep=True)
        assert isinstance(report, CertificationReport)
        for eng in report.engines:
            assert eng.execution_probe != "skipped", (
                f"[{eng.engine}] probe should not be 'skipped' in deep mode"
            )

    def test_probe_results_are_valid_strings(
        self, certifier: EngineCertifier, engine_names: List[str]
    ) -> None:
        for name in engine_names:
            result = certifier.probe_execution(name)
            assert isinstance(result, str)
            assert result.startswith(("pass", "fail:", "skip:"))
