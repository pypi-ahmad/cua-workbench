"""Tests for the model-policy allowlist (backend/allowed_models.json + API).

Covers:
- /api/models returns exactly the 4 allowed models
- POST /api/agent/start rejects unknown models
- POST /api/agent/start accepts each allowed model
- Anthropic tool-spec selection for allowed models
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# ── Allowlist file integrity ────────────────────────────────────────────────

_ALLOWED_MODELS_PATH = Path(__file__).resolve().parent.parent / "backend" / "allowed_models.json"

EXPECTED_MODELS = [
    ("google", "gemini-3-flash-preview"),
    ("google", "gemini-3.1-pro-preview"),
    ("anthropic", "claude-sonnet-4-6"),
    ("anthropic", "claude-opus-4-6"),
]


class TestAllowedModelsFile:
    """Verify allowed_models.json is valid and contains exactly the 4 models."""

    def test_file_exists(self):
        assert _ALLOWED_MODELS_PATH.exists(), "backend/allowed_models.json not found"

    def test_valid_json(self):
        data = json.loads(_ALLOWED_MODELS_PATH.read_text(encoding="utf-8"))
        assert "models" in data
        assert isinstance(data["models"], list)

    def test_exactly_four_models(self):
        data = json.loads(_ALLOWED_MODELS_PATH.read_text(encoding="utf-8"))
        assert len(data["models"]) == 4

    def test_model_ids_match(self):
        data = json.loads(_ALLOWED_MODELS_PATH.read_text(encoding="utf-8"))
        actual = [(m["provider"], m["model_id"]) for m in data["models"]]
        assert actual == EXPECTED_MODELS

    def test_anthropic_models_have_cu_metadata(self):
        data = json.loads(_ALLOWED_MODELS_PATH.read_text(encoding="utf-8"))
        for m in data["models"]:
            if m["provider"] == "anthropic":
                assert "cu_tool_version" in m, f"{m['model_id']} missing cu_tool_version"
                assert "cu_betas" in m, f"{m['model_id']} missing cu_betas"
                assert m["cu_tool_version"] == "computer_20251124"
                assert m["cu_betas"] == ["computer-use-2025-11-24"]

    def test_all_models_have_required_fields(self):
        data = json.loads(_ALLOWED_MODELS_PATH.read_text(encoding="utf-8"))
        for m in data["models"]:
            assert "provider" in m
            assert "model_id" in m
            assert "display_name" in m
            assert "supports_computer_use" in m

    def test_no_display_number_in_metadata(self):
        """display_number must never appear in model metadata."""
        raw = _ALLOWED_MODELS_PATH.read_text(encoding="utf-8")
        assert "display_number" not in raw


# ── Backend server: _VALID_MODELS_BY_PROVIDER built from allowlist ───────────

class TestServerModelValidation:
    """Verify server.py builds its validator from allowed_models.json."""

    def _get_valid_models(self):
        """Import the server module and return _VALID_MODELS_BY_PROVIDER."""
        from backend.api.server import _VALID_MODELS_BY_PROVIDER
        return _VALID_MODELS_BY_PROVIDER

    def test_google_models_in_validator(self):
        valid = self._get_valid_models()
        assert "gemini-3-flash-preview" in valid["google"]
        assert "gemini-3.1-pro-preview" in valid["google"]

    def test_anthropic_models_in_validator(self):
        valid = self._get_valid_models()
        assert "claude-sonnet-4-6" in valid["anthropic"]
        assert "claude-opus-4-6" in valid["anthropic"]

    def test_old_model_ids_not_in_validator(self):
        """Stale model IDs from before the fix must not be present."""
        valid = self._get_valid_models()
        all_ids = set()
        for s in valid.values():
            all_ids.update(s)
        assert "claude-4.6-sonnet" not in all_ids, "Stale claude-4.6-sonnet still in validator"

    def test_exactly_four_models_total(self):
        valid = self._get_valid_models()
        total = sum(len(s) for s in valid.values())
        assert total == 4


# ── /api/models endpoint ─────────────────────────────────────────────────────

class TestApiModelsEndpoint:
    """Verify GET /api/models returns the canonical list."""

    @pytest.fixture()
    def client(self):
        from fastapi.testclient import TestClient
        from backend.api.server import app
        return TestClient(app)

    def test_returns_four_models(self, client):
        resp = client.get("/api/models")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["models"]) == 4

    def test_model_ids(self, client):
        resp = client.get("/api/models")
        ids = [m["model_id"] for m in resp.json()["models"]]
        assert ids == [e[1] for e in EXPECTED_MODELS]

    def test_providers(self, client):
        resp = client.get("/api/models")
        providers = [m["provider"] for m in resp.json()["models"]]
        assert providers == [e[0] for e in EXPECTED_MODELS]

    def test_display_names_present(self, client):
        resp = client.get("/api/models")
        for m in resp.json()["models"]:
            assert m.get("display_name"), f"Missing display_name for {m['model_id']}"


# ── POST /api/agent/start: reject unknown models ────────────────────────────

class TestAgentStartModelRestriction:
    """POST /api/agent/start must reject models not in the allowlist."""

    @pytest.fixture()
    def client(self):
        from fastapi.testclient import TestClient
        from backend.api.server import app
        return TestClient(app)

    def _start_payload(self, provider="google", model="gemini-3-flash-preview"):
        return {
            "task": "test task",
            "api_key": "fake-key-123456789",
            "model": model,
            "max_steps": 5,
            "mode": "browser",
            "engine": "playwright_mcp",
            "provider": provider,
        }

    def test_reject_unknown_google_model(self, client):
        resp = client.post("/api/agent/start", json=self._start_payload(model="gemini-unknown"))
        assert resp.status_code == 400
        data = resp.json()
        assert "error" in data
        assert "not allowed" in data["error"].lower() or "supported" in data["error"].lower()

    def test_reject_unknown_anthropic_model(self, client):
        resp = client.post("/api/agent/start", json=self._start_payload(provider="anthropic", model="claude-unknown"))
        assert resp.status_code == 400
        data = resp.json()
        assert "error" in data

    def test_reject_stale_claude_id(self, client):
        """The old 'claude-4.6-sonnet' ID must be rejected."""
        resp = client.post("/api/agent/start", json=self._start_payload(provider="anthropic", model="claude-4.6-sonnet"))
        assert resp.status_code == 400
        data = resp.json()
        assert "error" in data

    def test_error_lists_allowed_models(self, client):
        resp = client.post("/api/agent/start", json=self._start_payload(model="nope"))
        assert resp.status_code == 400
        err = resp.json()["error"]
        # The error message should list at least the allowed model IDs
        for _, mid in EXPECTED_MODELS:
            assert mid in err, f"Error message should list '{mid}'"

    @pytest.mark.parametrize("provider,model", EXPECTED_MODELS)
    def test_accept_allowed_model_passes_validation(self, client, provider, model):
        """Allowed models must pass model validation (may fail later at container/key)."""
        resp = client.post("/api/agent/start", json=self._start_payload(provider=provider, model=model))
        data = resp.json()
        # Must NOT be rejected by the model allowlist check
        assert "not allowed" not in data.get("error", "").lower()


# ── Anthropic CU tool-spec for allowed models ──────────────────────────────

class TestAnthropicToolSpecForAllowedModels:
    """Verify ClaudeCUClient uses tool metadata from the allowlist."""

    def _entry(self, model: str) -> dict:
        data = json.loads(_ALLOWED_MODELS_PATH.read_text(encoding="utf-8"))
        for entry in data["models"]:
            if entry["provider"] == "anthropic" and entry["model_id"] == model:
                return entry
        raise AssertionError(f"No anthropic allowlist entry for {model}")

    def _make_client(self, model: str):
        entry = self._entry(model)
        with patch.dict("sys.modules", {"anthropic": MagicMock()}):
            from backend.engines.computer_use_engine import ClaudeCUClient
            return ClaudeCUClient(
                api_key="fake",
                model=model,
                tool_version=entry["cu_tool_version"],
                beta_flag=entry["cu_betas"],
            )

    def test_sonnet_46_tool_version(self):
        c = self._make_client("claude-sonnet-4-6")
        assert c._tool_version == "computer_20251124"
        assert c._beta_flag == "computer-use-2025-11-24"

    def test_opus_46_tool_version(self):
        c = self._make_client("claude-opus-4-6")
        assert c._tool_version == "computer_20251124"
        assert c._beta_flag == "computer-use-2025-11-24"

    def test_build_tools_no_display_number(self):
        c = self._make_client("claude-sonnet-4-6")
        tools = c._build_tools(1440, 900)
        assert "display_number" not in tools[0]

    def test_build_tools_uses_correct_type(self):
        c = self._make_client("claude-opus-4-6")
        tools = c._build_tools(1440, 900)
        assert tools[0]["type"] == "computer_20251124"


# ── Frontend "no hardcoded fallback" guardrails ─────────────────────────────

class TestNoHardcodedFrontendFallback:
    """Frontend source files must NOT contain hardcoded model arrays.

    The UI must render only from GET /api/models; any hidden fallback
    list that survives a backend-down scenario would violate the contract.
    """

    _FRONTEND_SRC = Path(__file__).resolve().parent.parent / "frontend" / "src"
    _FORBIDDEN_PATTERNS = [
        "FALLBACK_GOOGLE",
        "FALLBACK_ANTHROPIC",
        "FALLBACK_GOOGLE_MODELS",
        "FALLBACK_ANTHROPIC_MODELS",
    ]

    def _scan_jsx_files(self):
        """Return all .jsx/.js source file paths (excluding dist)."""
        return list(self._FRONTEND_SRC.rglob("*.jsx")) + list(self._FRONTEND_SRC.rglob("*.js"))

    def test_no_fallback_arrays_in_source(self):
        """No FALLBACK_*_MODELS or FALLBACK_{GOOGLE,ANTHROPIC} constants."""
        for fp in self._scan_jsx_files():
            content = fp.read_text(encoding="utf-8")
            for pattern in self._FORBIDDEN_PATTERNS:
                # Allow comment references like "no hardcoded fallback"
                for line in content.splitlines():
                    if pattern in line and not line.lstrip().startswith("//"):
                        pytest.fail(
                            f"Forbidden constant '{pattern}' found in "
                            f"{fp.relative_to(self._FRONTEND_SRC)}: {line.strip()}"
                        )

    def test_no_model_id_literals_in_source(self):
        """Source files must not embed model IDs as string literals."""
        import re
        model_ids = [mid for _, mid in EXPECTED_MODELS]
        for fp in self._scan_jsx_files():
            content = fp.read_text(encoding="utf-8")
            for mid in model_ids:
                # Match quoted string literals: "model-id" or 'model-id'
                if re.search(rf"""['"]({re.escape(mid)})['"]""", content):
                    pytest.fail(
                        f"Model ID literal '{mid}' found in "
                        f"{fp.relative_to(self._FRONTEND_SRC)}"
                    )
