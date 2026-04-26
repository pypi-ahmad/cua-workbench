"""Lock-in tests for model-ID single source of truth.

Source of truth: ``backend/allowed_models.json``.  Loaded by
``backend.api.server`` into ``_VALID_MODELS_BY_PROVIDER`` and used by
``/api/keys/validate`` to gate the model the client wants to call.

These tests catch:
  1) Allowlist file shape (provider, model_id) for every entry.
  2) Default model literals in code (``backend/config.py``,
     ``backend/models.py``, ``backend/engines/computer_use_engine.py``)
     are all members of the allowlist for their provider.
  3) The runtime ``_VALID_MODELS_BY_PROVIDER`` dict matches the JSON file
     exactly — no silent drift.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.api import server as srv


_REPO = Path(__file__).resolve().parent.parent
_ALLOWLIST = _REPO / "backend" / "allowed_models.json"


def _load_allowlist() -> list[dict]:
    with _ALLOWLIST.open(encoding="utf-8") as f:
        return json.load(f)["models"]


def _ids_for(provider: str) -> set[str]:
    return {m["model_id"] for m in _load_allowlist() if m["provider"] == provider}


class TestAllowlistShape(unittest.TestCase):
    """Every allowlist entry must carry the keys the runtime depends on."""

    def test_each_entry_has_provider_and_model_id(self):
        for entry in _load_allowlist():
            self.assertIn("provider", entry)
            self.assertIn("model_id", entry)
            self.assertIsInstance(entry["provider"], str)
            self.assertIsInstance(entry["model_id"], str)
            self.assertTrue(entry["provider"])
            self.assertTrue(entry["model_id"])

    def test_providers_are_recognized(self):
        recognized = {"google", "anthropic"}
        for entry in _load_allowlist():
            self.assertIn(
                entry["provider"], recognized,
                f"Unknown provider in allowlist: {entry['provider']}",
            )

    def test_no_duplicate_model_ids_per_provider(self):
        for provider in ("google", "anthropic"):
            ids = [m["model_id"] for m in _load_allowlist() if m["provider"] == provider]
            self.assertEqual(
                len(ids), len(set(ids)),
                f"Duplicate model_id for provider {provider}: {ids}",
            )

    def test_anthropic_entries_require_cu_metadata_at_startup(self):
        bad_models = [
            {
                "provider": "anthropic",
                "model_id": "claude-test",
                "display_name": "Claude Test",
                "supports_computer_use": True,
            }
        ]

        with self.assertRaisesRegex(ValueError, "claude-test.*cu_tool_version"):
            srv._build_allowed_model_state(bad_models)

    def test_allowlist_metadata_flows_to_claude_client(self):
        from backend.engines.computer_use_engine import ComputerUseEngine, Environment, Provider

        with patch("backend.engines.computer_use_engine.ClaudeCUClient") as mock_client:
            ComputerUseEngine(
                provider=Provider.CLAUDE,
                api_key="fake-key",
                model="claude-sonnet-4-6",
                environment=Environment.DESKTOP,
                tool_version="computer_test_20260101",
                beta_flag=["computer-use-2026-01-01"],
            )

        kwargs = mock_client.call_args.kwargs
        self.assertEqual(kwargs["tool_version"], "computer_test_20260101")
        self.assertEqual(kwargs["beta_flag"], ["computer-use-2026-01-01"])


class TestRuntimeMatchesAllowlist(unittest.TestCase):
    """server._VALID_MODELS_BY_PROVIDER must mirror the JSON file."""

    def test_runtime_dict_matches_file(self):
        expected: dict[str, set[str]] = {}
        for entry in _load_allowlist():
            expected.setdefault(entry["provider"], set()).add(entry["model_id"])
        self.assertEqual(srv._VALID_MODELS_BY_PROVIDER, expected)


class TestCodeDefaultsAreAllowlisted(unittest.TestCase):
    """Default model literals in source must be present in the allowlist."""

    def test_config_default_gemini_is_allowlisted(self):
        from backend.config import config
        self.assertIn(
            config.gemini_model, _ids_for("google"),
            f"config.gemini_model={config.gemini_model!r} not in google allowlist",
        )

    def test_models_py_defaults_are_allowlisted(self):
        # backend/models.py declares two `model: str = "..."` defaults
        # that the API consumes.  Both must be allowlisted entries.
        text = (_REPO / "backend" / "models.py").read_text(encoding="utf-8")
        defaults = re.findall(r'model:\s*str\s*=\s*"([^"]+)"', text)
        self.assertTrue(defaults, "no model defaults found in backend/models.py")
        all_ids = _ids_for("google") | _ids_for("anthropic")
        for d in defaults:
            self.assertIn(d, all_ids, f"backend/models.py default {d!r} not in allowlist")

    def test_computer_use_engine_defaults_are_allowlisted(self):
        # The Gemini and Claude default fallbacks in computer_use_engine.py
        # are reached when the caller passes model=None / "".  They MUST
        # match the allowlist or the request will be rejected one layer up.
        text = (_REPO / "backend" / "engines" / "computer_use_engine.py").read_text(encoding="utf-8")
        # Match the fallback literals: model=model or "<id>" and
        # the ClaudeCUClient default param.
        gemini_defaults = re.findall(r'model\s*=\s*model\s*or\s*"(gemini-[^"]+)"', text)
        claude_defaults = re.findall(r'model\s*=\s*model\s*or\s*"(claude-[^"]+)"', text)
        claude_param_defaults = re.findall(
            r'def __init__\([^)]*?model:\s*str\s*=\s*"(claude-[^"]+)"',
            text, re.DOTALL,
        )

        self.assertTrue(gemini_defaults, "no Gemini fallback default found")
        self.assertTrue(claude_defaults, "no Claude `model or` fallback default found")
        self.assertTrue(claude_param_defaults, "no Claude __init__ default found")

        google_ids = _ids_for("google")
        anthropic_ids = _ids_for("anthropic")
        for d in gemini_defaults:
            self.assertIn(d, google_ids, f"stale Gemini default in code: {d!r}")
        for d in claude_defaults + claude_param_defaults:
            self.assertIn(d, anthropic_ids, f"stale Claude default in code: {d!r}")


class TestSingleSourceLookupSemantics(unittest.TestCase):
    """The runtime lookup helper rejects unknowns and accepts allowlisted IDs."""

    def test_known_models_pass(self):
        for entry in _load_allowlist():
            self.assertIn(
                entry["model_id"],
                srv._VALID_MODELS_BY_PROVIDER.get(entry["provider"], set()),
            )

    def test_stale_claude_id_rejected(self):
        # Specifically: the stale 2025-05-14 Sonnet SKU must NOT pass.
        self.assertNotIn(
            "claude-sonnet-4-20250514",
            srv._VALID_MODELS_BY_PROVIDER.get("anthropic", set()),
        )

    def test_unknown_provider_rejected(self):
        self.assertEqual(
            srv._VALID_MODELS_BY_PROVIDER.get("openai", set()),
            set(),
        )


if __name__ == "__main__":
    unittest.main()
