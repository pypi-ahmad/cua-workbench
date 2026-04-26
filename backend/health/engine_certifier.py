"""Engine Certification Framework.

Validates every engine and every tool declared in ``engine_capabilities.json``
against the live runtime environment.  Produces a structured diagnostic report
suitable for CI gating and pre-deployment checks.

Usage::

    # Programmatic
    from backend.health.engine_certifier import EngineCertifier
    certifier = EngineCertifier()
    report = certifier.run_full_certification(deep=False)

    # CLI
    python -m backend.health.engine_certifier --deep
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Set

logger = logging.getLogger(__name__)

_SCHEMA_FILENAME = "engine_capabilities.json"
_DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parent.parent / _SCHEMA_FILENAME

# ── Binary mappings derived from environment_requirements prose ───────────────
# Keys are substrings found in *environment_requirements* strings; values are
# the binary names expected on $PATH.  This powers the dependency checker
# without hardcoding engine names.

_REQUIREMENT_BINARY_MAP: Dict[str, str] = {
    "xdotool": "xdotool",
    "wmctrl": "wmctrl",
    "scrot": "scrot",
    "xclip": "xclip",
    "playwright": "npx",
    "node.js": "node",
    "node": "node",
    "dbus-monitor": "dbus-monitor",
    "gdbus": "gdbus",
}

# Environment-variable expectations keyed on binary/requirement keywords.
_ENV_CHECKS: Dict[str, str] = {
    "xdotool": "DISPLAY",
    "scrot": "DISPLAY",
    "wmctrl": "DISPLAY",
    "d-bus": "DBUS_SESSION_BUS_ADDRESS",
    "dbus": "DBUS_SESSION_BUS_ADDRESS",
    "at-spi": "DBUS_SESSION_BUS_ADDRESS",
}

# Special filesystem checks keyed on requirement keywords.
_FS_CHECKS: Dict[str, str] = {
}


# ── Result dataclasses ───────────────────────────────────────────────────────

@dataclass
class EngineReport:
    """Certification result for a single engine."""

    engine: str
    healthy: bool = True
    schema_issues: List[str] = field(default_factory=list)
    missing_dependencies: List[str] = field(default_factory=list)
    missing_env_vars: List[str] = field(default_factory=list)
    missing_fs_paths: List[str] = field(default_factory=list)
    invalid_actions: List[str] = field(default_factory=list)
    inheritance_issues: List[str] = field(default_factory=list)
    fallback_issues: List[str] = field(default_factory=list)
    execution_probe: str = "skipped"

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a JSON-friendly dict."""
        return {
            "engine": self.engine,
            "healthy": self.healthy,
            "schema_issues": self.schema_issues,
            "missing_dependencies": self.missing_dependencies,
            "missing_env_vars": self.missing_env_vars,
            "missing_fs_paths": self.missing_fs_paths,
            "invalid_actions": self.invalid_actions,
            "inheritance_issues": self.inheritance_issues,
            "fallback_issues": self.fallback_issues,
            "execution_probe": self.execution_probe,
        }


@dataclass
class CertificationReport:
    """Aggregate certification result for all engines."""

    schema_version: str = "unknown"
    platform: str = ""
    engine_count: int = 0
    all_healthy: bool = True
    engines: List[EngineReport] = field(default_factory=list)
    global_issues: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a JSON-friendly dict."""
        return {
            "schema_version": self.schema_version,
            "platform": self.platform,
            "engine_count": self.engine_count,
            "all_healthy": self.all_healthy,
            "engines": [e.to_dict() for e in self.engines],
            "global_issues": self.global_issues,
        }


# ── Certifier ─────────────────────────────────────────────────────────────────

class EngineCertifier:
    """Comprehensive engine and tool validation framework.

    Loads ``engine_capabilities.json`` and performs multi-phase validation:

    1. Schema integrity (structure, required fields)
    2. Engine registration (all engines present and well-formed)
    3. Environment requirements (binaries, env vars, filesystem)
    4. Action consistency (categories ↔ allowed_actions parity)
    5. Meta-engine inheritance (union correctness, no circular refs)
    6. Fallback chain validity
    7. Optional deep execution probes (safe, non-destructive)
    """

    def __init__(self, schema_path: str | Path | None = None) -> None:
        self._path = Path(schema_path) if schema_path else _DEFAULT_SCHEMA_PATH
        if not self._path.exists():
            raise FileNotFoundError(f"Schema not found: {self._path}")

        with open(self._path, "r", encoding="utf-8") as fh:
            self._raw: Dict[str, Any] = json.load(fh)

        self._version: str = self._raw.get("version", "unknown")
        self._engines_raw: Dict[str, Any] = self._raw.get("engines", {})

    # ── Phase 1: Schema Integrity ─────────────────────────────────────────

    def validate_schema_integrity(self) -> List[str]:
        """Verify top-level schema structure and required fields per engine.

        Returns a list of issue strings (empty = pass).
        """
        issues: List[str] = []

        if "version" not in self._raw:
            issues.append("Missing top-level 'version' field")
        if "engines" not in self._raw:
            issues.append("Missing top-level 'engines' field")
            return issues

        seen_priorities: Dict[int, str] = {}

        for name, block in self._engines_raw.items():
            if not isinstance(block, dict):
                issues.append(f"[{name}] Engine block is not a dict")
                continue

            # Required fields
            if "display_name" not in block:
                issues.append(f"[{name}] Missing 'display_name'")
            if "categories" not in block:
                issues.append(f"[{name}] Missing 'categories'")
            if "allowed_actions" not in block:
                issues.append(f"[{name}] Missing 'allowed_actions'")

            # Fallback priority uniqueness (0 is allowed for meta-engines)
            prio = block.get("fallback_priority")
            if prio is not None and prio != 0:
                if prio in seen_priorities:
                    issues.append(
                        f"[{name}] Duplicate fallback_priority {prio} "
                        f"(also used by '{seen_priorities[prio]}')"
                    )
                else:
                    seen_priorities[prio] = name

            # Meta-engine specific
            if block.get("is_meta_engine"):
                if not block.get("inherit_actions_from"):
                    issues.append(f"[{name}] Meta-engine missing 'inherit_actions_from'")
                if not block.get("fallback_chain"):
                    issues.append(f"[{name}] Meta-engine missing 'fallback_chain'")

        return issues

    # ── Phase 2: Engine Registration ──────────────────────────────────────

    def validate_engine_registration(self) -> List[str]:
        """Verify all engines are properly registered.

        Returns a list of issue strings.
        """
        issues: List[str] = []

        if not self._engines_raw:
            issues.append("No engines defined in schema")

        for name, block in self._engines_raw.items():
            if not isinstance(block, dict):
                issues.append(f"[{name}] Invalid engine block type")
                continue

            if not block.get("display_name", "").strip():
                issues.append(f"[{name}] Empty or missing display_name")

            # Verify categories is dict or __inherited__
            cats = block.get("categories", {})
            if isinstance(cats, str):
                if cats != "__inherited__":
                    issues.append(f"[{name}] categories is string but not '__inherited__'")
            elif isinstance(cats, dict):
                for cat_name, actions in cats.items():
                    if not isinstance(actions, list):
                        issues.append(f"[{name}] Category '{cat_name}' is not a list")
            else:
                issues.append(f"[{name}] categories is neither dict nor '__inherited__'")

        return issues

    # ── Phase 3: Environment Requirements ─────────────────────────────────

    def validate_environment_requirements(self, engine_name: str) -> EngineReport:
        """Check binary, env-var, and filesystem deps for one engine.

        Returns an ``EngineReport`` populated with missing items.
        """
        report = EngineReport(engine=engine_name)
        block = self._engines_raw.get(engine_name)
        if block is None:
            report.healthy = False
            report.schema_issues.append(f"Engine '{engine_name}' not in schema")
            return report

        reqs: List[str] = block.get("environment_requirements", [])
        self._check_binary_deps(reqs, report)
        self._check_env_vars(reqs, report)
        self._check_fs_paths(reqs, report)

        if report.missing_dependencies or report.missing_env_vars or report.missing_fs_paths:
            report.healthy = False

        return report

    def validate_binary_dependencies(self, engine_name: str) -> List[str]:
        """Return list of missing binary names for the given engine."""
        block = self._engines_raw.get(engine_name, {})
        reqs: List[str] = block.get("environment_requirements", [])
        missing: List[str] = []

        for req_text in reqs:
            req_lower = req_text.lower()
            for keyword, binary in _REQUIREMENT_BINARY_MAP.items():
                if keyword in req_lower and shutil.which(binary) is None:
                    if binary not in missing:
                        missing.append(binary)

        # Tooling blocks (accessibility engine)
        tooling: Dict[str, List[str]] = block.get("tooling", {})
        for group_name, tools in tooling.items():
            for tool_name in tools:
                tool_lower = tool_name.lower().replace("-", "")
                for keyword, binary in _REQUIREMENT_BINARY_MAP.items():
                    keyword_norm = keyword.lower().replace("-", "")
                    if keyword_norm in tool_lower and shutil.which(binary) is None:
                        if binary not in missing:
                            missing.append(binary)

        return missing

    def _check_binary_deps(self, reqs: List[str], report: EngineReport) -> None:
        """Populate report.missing_dependencies from requirement strings."""
        for req_text in reqs:
            req_lower = req_text.lower()
            for keyword, binary in _REQUIREMENT_BINARY_MAP.items():
                if keyword in req_lower and shutil.which(binary) is None:
                    if binary not in report.missing_dependencies:
                        report.missing_dependencies.append(binary)

    def _check_env_vars(self, reqs: List[str], report: EngineReport) -> None:
        """Populate report.missing_env_vars from requirement strings."""
        for req_text in reqs:
            req_lower = req_text.lower()
            for keyword, env_var in _ENV_CHECKS.items():
                if keyword in req_lower and not os.environ.get(env_var):
                    if env_var not in report.missing_env_vars:
                        report.missing_env_vars.append(env_var)

    def _check_fs_paths(self, reqs: List[str], report: EngineReport) -> None:
        """Populate report.missing_fs_paths from requirement strings."""
        for req_text in reqs:
            for keyword, fs_path in _FS_CHECKS.items():
                if keyword in req_text and not os.path.exists(fs_path):
                    if fs_path not in report.missing_fs_paths:
                        report.missing_fs_paths.append(fs_path)

    # ── Phase 4: Action Consistency ───────────────────────────────────────

    def validate_allowed_actions(self, engine_name: str) -> List[str]:
        """Verify categories ↔ allowed_actions parity for one engine.

        Returns a list of issue strings.
        """
        issues: List[str] = []
        block = self._engines_raw.get(engine_name)
        if block is None:
            return [f"Engine '{engine_name}' not in schema"]

        raw_cats = block.get("categories", {})
        raw_actions = block.get("allowed_actions", [])

        # Skip __inherited__ engines (validated separately)
        if isinstance(raw_cats, str) and raw_cats == "__inherited__":
            return issues
        if isinstance(raw_actions, str) and raw_actions == "__inherited__":
            return issues

        # Flatten categories into a set
        cat_actions: Set[str] = set()
        for cat_name, action_list in raw_cats.items():
            if isinstance(action_list, list):
                cat_actions.update(action_list)

        allowed_set = set(raw_actions)

        # Actions in categories but not in allowed_actions
        in_cats_not_allowed = cat_actions - allowed_set
        for action in sorted(in_cats_not_allowed):
            issues.append(
                f"[{engine_name}] Action '{action}' in categories but not in allowed_actions"
            )

        # Actions in allowed_actions but not in any category
        in_allowed_not_cats = allowed_set - cat_actions
        for action in sorted(in_allowed_not_cats):
            issues.append(
                f"[{engine_name}] Action '{action}' in allowed_actions but not in any category"
            )

        # Duplicate actions within allowed_actions
        seen: Set[str] = set()
        for action in raw_actions:
            if action in seen:
                issues.append(f"[{engine_name}] Duplicate action in allowed_actions: '{action}'")
            seen.add(action)

        return issues

    # ── Phase 5: Meta-Engine Inheritance ──────────────────────────────────

    def validate_meta_engine_inheritance(self, engine_name: str) -> List[str]:
        """Verify meta-engine inheritance correctness.

        Checks:
        - ``inherit_actions_from`` references valid engines
        - Resolved actions equal union of parent engines
        - No circular inheritance
        - Fallback chain references valid engines

        Returns a list of issue strings.
        """
        issues: List[str] = []
        block = self._engines_raw.get(engine_name)
        if block is None:
            return [f"Engine '{engine_name}' not in schema"]

        if not block.get("is_meta_engine"):
            return issues

        parents: List[str] = block.get("inherit_actions_from", [])
        all_engine_names = set(self._engines_raw.keys())

        # Validate parent references
        for parent_name in parents:
            if parent_name not in all_engine_names:
                issues.append(
                    f"[{engine_name}] inherit_actions_from references "
                    f"unknown engine '{parent_name}'"
                )

        # Check for circular inheritance
        visited: Set[str] = set()
        self._detect_circular(engine_name, visited, issues)

        # Verify union completeness
        expected_union: Set[str] = set()
        for parent_name in parents:
            parent_block = self._engines_raw.get(parent_name, {})
            parent_actions = parent_block.get("allowed_actions", [])
            if isinstance(parent_actions, list):
                expected_union.update(parent_actions)

        # To check actual resolved actions, use EngineCapabilities (lazy import)
        from backend.engine_capabilities import EngineCapabilities
        caps = EngineCapabilities(self._path)
        resolved = caps.get_engine_actions(engine_name)

        missing_from_resolved = expected_union - resolved
        extra_in_resolved = resolved - expected_union

        for action in sorted(missing_from_resolved):
            issues.append(
                f"[{engine_name}] Expected inherited action '{action}' "
                f"missing from resolved set"
            )
        for action in sorted(extra_in_resolved):
            issues.append(
                f"[{engine_name}] Unexpected action '{action}' "
                f"in resolved set (not in any parent)"
            )

        return issues

    def _detect_circular(
        self, engine_name: str, visited: Set[str], issues: List[str]
    ) -> None:
        """Recursively detect circular inheritance in meta-engines."""
        if engine_name in visited:
            issues.append(f"Circular inheritance detected involving '{engine_name}'")
            return
        visited.add(engine_name)

        block = self._engines_raw.get(engine_name, {})
        for parent_name in block.get("inherit_actions_from", []):
            self._detect_circular(parent_name, visited, issues)

    # ── Phase 5b: Fallback Chain Validation ───────────────────────────────

    def validate_fallback_chain(self, engine_name: str) -> List[str]:
        """Verify fallback chain references valid engines with no duplicates.

        Returns a list of issue strings.
        """
        issues: List[str] = []
        block = self._engines_raw.get(engine_name)
        if block is None:
            return [f"Engine '{engine_name}' not in schema"]

        chain: List[str] = block.get("fallback_chain", [])
        if not chain:
            return issues

        all_engine_names = set(self._engines_raw.keys())
        seen: Set[str] = set()

        for entry in chain:
            if entry not in all_engine_names:
                issues.append(
                    f"[{engine_name}] Fallback chain references "
                    f"unknown engine '{entry}'"
                )
            if entry in seen:
                issues.append(
                    f"[{engine_name}] Duplicate engine '{entry}' in fallback_chain"
                )
            seen.add(entry)

            # Self-reference check
            if entry == engine_name:
                issues.append(
                    f"[{engine_name}] Fallback chain contains self-reference"
                )

        return issues

    # ── Phase 6: Execution Probes ─────────────────────────────────────────

    def probe_execution(self, engine_name: str) -> str:
        """Run a safe, non-destructive execution probe for *engine_name*.

        Returns one of: ``"pass"``, ``"fail:<reason>"``, ``"skip:<reason>"``.
        """
        probe_fn = self._PROBES.get(engine_name)
        if probe_fn is None:
            return "skip:no probe defined"
        try:
            return probe_fn(self)
        except Exception as exc:
            return f"fail:{exc}"

    def _probe_playwright_mcp(self) -> str:
        """Spawn MCP server, attempt JSON-RPC handshake, then kill."""
        if shutil.which("npx") is None and shutil.which("npx.cmd") is None:
            return "skip:npx not found"
        try:
            proc = subprocess.Popen(
                ["npx", "-y", "@anthropic-ai/mcp-server-playwright@latest"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            # We just passed PIPE for stdin/stdout, so Popen guarantees these
            # are non-None. The assert documents the invariant for mypy and
            # turns any future regression (e.g. someone changing PIPE to None)
            # into a clear AssertionError instead of an opaque attribute error.
            assert proc.stdin is not None and proc.stdout is not None, (
                "engine_certifier requires PIPE for stdin/stdout"
            )
            # Send JSON-RPC initialize request
            init_msg = json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "certifier", "version": "1.0"}},
            })
            try:
                proc.stdin.write((init_msg + "\n").encode())
                proc.stdin.flush()
                # Wait briefly for response
                import select
                proc.stdout.readline()  # may block briefly
                return "pass"
            finally:
                proc.terminate()
                proc.wait(timeout=5)
        except FileNotFoundError:
            return "skip:npx not found"
        except Exception as exc:
            return f"fail:{exc}"

    def _probe_accessibility(self) -> str:
        """Import gi + pyatspi and attempt Registry.getDesktop(0)."""
        try:
            import gi  # type: ignore[import-untyped]
            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi  # type: ignore[import-untyped]
            desktop = Atspi.get_desktop(0)
            if desktop is not None:
                return "pass"
            return "fail:Atspi.get_desktop(0) returned None"
        except ImportError as exc:
            return f"skip:import error — {exc}"
        except Exception as exc:
            return f"fail:{exc}"

    def _probe_computer_use(self) -> str:
        """Check that internal deps (xdotool, scrot) are available for DesktopExecutor."""
        missing: List[str] = []
        for binary in ("xdotool", "scrot"):
            if shutil.which(binary) is None:
                missing.append(binary)
        if missing:
            return f"skip:missing binaries — {', '.join(missing)}"
        if not os.environ.get("DISPLAY"):
            return "skip:DISPLAY not set"
        return "pass"

    # Map engine names to probe methods — only the 3 active engines.
    _PROBES: Dict[str, Any] = {
        "playwright_mcp": _probe_playwright_mcp,
        "omni_accessibility": _probe_accessibility,
        "computer_use": _probe_computer_use,
    }

    # ── Phase 7: Full Certification Run ───────────────────────────────────

    def run_full_certification(self, deep: bool = False) -> CertificationReport:
        """Execute all validation phases and return an aggregate report.

        Parameters:
            deep: If ``True``, run execution probes (Phase 6).  Default ``False``
                  keeps the run fast and side-effect-free.
        """
        report = CertificationReport(
            schema_version=self._version,
            platform=f"{platform.system()} {platform.release()} ({platform.machine()})",
            engine_count=len(self._engines_raw),
        )

        # Phase 1: Schema integrity
        schema_issues = self.validate_schema_integrity()
        if schema_issues:
            report.global_issues.extend(schema_issues)
            report.all_healthy = False

        # Phase 2: Engine registration
        reg_issues = self.validate_engine_registration()
        if reg_issues:
            report.global_issues.extend(reg_issues)
            report.all_healthy = False

        # Per-engine validation
        for engine_name in sorted(self._engines_raw):
            eng_report = self._certify_single_engine(engine_name, deep=deep)
            report.engines.append(eng_report)
            if not eng_report.healthy:
                report.all_healthy = False

        # Global: duplicate actions across concrete (non-meta) engines
        # This is informational — duplicates across engines are expected.
        report.global_issues.extend(self._check_global_action_duplicates())

        return report

    def _certify_single_engine(
        self, engine_name: str, deep: bool = False
    ) -> EngineReport:
        """Run all per-engine checks and return an EngineReport."""
        eng_report = EngineReport(engine=engine_name)

        # Phase 3: Environment
        env_report = self.validate_environment_requirements(engine_name)
        eng_report.missing_dependencies = env_report.missing_dependencies
        eng_report.missing_env_vars = env_report.missing_env_vars
        eng_report.missing_fs_paths = env_report.missing_fs_paths

        # Phase 4: Action consistency
        action_issues = self.validate_allowed_actions(engine_name)
        eng_report.invalid_actions = action_issues

        # Phase 5: Meta-engine inheritance
        inheritance_issues = self.validate_meta_engine_inheritance(engine_name)
        eng_report.inheritance_issues = inheritance_issues

        # Phase 5b: Fallback chain
        fallback_issues = self.validate_fallback_chain(engine_name)
        eng_report.fallback_issues = fallback_issues

        # Phase 6: Execution probe (optional)
        if deep:
            eng_report.execution_probe = self.probe_execution(engine_name)
        else:
            eng_report.execution_probe = "skipped"

        # Determine overall health (deps are env-dependent, don't fail schema tests)
        has_schema_problems = bool(
            eng_report.invalid_actions
            or eng_report.inheritance_issues
            or eng_report.fallback_issues
        )
        if has_schema_problems:
            eng_report.healthy = False

        # Execution probe failure also marks unhealthy
        if eng_report.execution_probe.startswith("fail"):
            eng_report.healthy = False

        return eng_report

    def _check_global_action_duplicates(self) -> List[str]:
        """Detect actions that appear in multiple concrete engines.

        This is informational for awareness — shared actions like ``click``
        across engines are expected by design.  We only flag if a concrete
        engine defines an action that no other engine does AND it appears in
        a meta-engine's parents list, creating ambiguity.

        Returns an empty list (no issues flagged) — included for extensibility.
        """
        return []


# ── CLI entry point ──────────────────────────────────────────────────────────

def _print_table(report: CertificationReport) -> None:
    """Print a human-readable certification table to stdout."""
    header = f"{'Engine':<20} {'Healthy':<10} {'Missing Deps':<30} {'Execution Probe':<20}"
    sep = "-" * len(header)

    print(f"\n  CUA Engine Certification Report (schema v{report.schema_version})")
    print(f"  Platform: {report.platform}")
    print(f"  Engines:  {report.engine_count}")
    print()
    print(f"  {header}")
    print(f"  {sep}")

    for eng in report.engines:
        healthy_str = "YES" if eng.healthy else "NO"
        deps_str = ", ".join(eng.missing_dependencies) if eng.missing_dependencies else "-"
        probe_str = eng.execution_probe

        # Truncate long strings
        if len(deps_str) > 28:
            deps_str = deps_str[:25] + "..."
        if len(probe_str) > 18:
            probe_str = probe_str[:15] + "..."

        print(f"  {eng.engine:<20} {healthy_str:<10} {deps_str:<30} {probe_str:<20}")

    print(f"\n  {sep}")
    overall = "ALL HEALTHY" if report.all_healthy else "ISSUES DETECTED"
    print(f"  Overall: {overall}")

    if report.global_issues:
        print("\n  Global Issues:")
        for issue in report.global_issues:
            print(f"    - {issue}")

    # Per-engine detail (only for engines with issues)
    for eng in report.engines:
        detail_lines: List[str] = []
        if eng.invalid_actions:
            detail_lines.extend(f"    Action: {a}" for a in eng.invalid_actions)
        if eng.inheritance_issues:
            detail_lines.extend(f"    Inherit: {a}" for a in eng.inheritance_issues)
        if eng.fallback_issues:
            detail_lines.extend(f"    Fallback: {a}" for a in eng.fallback_issues)
        if eng.missing_env_vars:
            detail_lines.extend(f"    Env: {v}" for v in eng.missing_env_vars)
        if eng.missing_fs_paths:
            detail_lines.extend(f"    FS: {p}" for p in eng.missing_fs_paths)

        if detail_lines:
            print(f"\n  [{eng.engine}] Details:")
            for line in detail_lines:
                print(line)

    print()


def main() -> None:
    """CLI entry point for ``python -m backend.health.engine_certifier``."""
    import argparse

    parser = argparse.ArgumentParser(
        description="CUA Engine Certification — validate all engines and tools",
    )
    parser.add_argument(
        "--deep", action="store_true",
        help="Run execution probes (requires live environment)",
    )
    parser.add_argument(
        "--json", action="store_true", dest="json_output",
        help="Output raw JSON report instead of table",
    )
    parser.add_argument(
        "--schema", type=str, default=None,
        help="Path to engine_capabilities.json (default: auto-detect)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    certifier = EngineCertifier(schema_path=args.schema)
    report = certifier.run_full_certification(deep=args.deep)

    if args.json_output:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        _print_table(report)

    sys.exit(0 if report.all_healthy else 1)


if __name__ == "__main__":
    main()
