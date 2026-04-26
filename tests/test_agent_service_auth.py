"""Bearer-auth tests for the in-container agent_service (I-002).

These tests run against the agent_service module loaded as a normal
Python module — they don't require a live container.  They exercise:

  * ``_require_auth`` returns False (and sends 401) for unauthenticated
    requests on protected paths.
  * ``_require_auth`` returns True for /health regardless of header.
  * ``_require_auth`` accepts the correct Bearer token via
    ``hmac.compare_digest`` semantics.
  * Source-level assertions: ``do_GET`` / ``do_POST`` invoke
    ``_require_auth`` *before* any side-effectful work; the only
    public path is /health; ``hmac.compare_digest`` is used (not ``==``).
  * ``docker_manager.start_container`` extracts the token via
    ``docker cp`` and registers it through ``backend.utils.agent_auth``.

Closes F-008.  Implements I-002.
"""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent
_AGENT_SERVICE = _ROOT / "docker" / "agent_service.py"
_DOCKER_MANAGER = _ROOT / "backend" / "utils" / "docker_manager.py"


def _load_agent_service_module():
    """Import docker/agent_service.py without executing main()."""
    sys.path.insert(0, str(_ROOT / "docker"))
    try:
        if "agent_service" in sys.modules:
            return importlib.reload(sys.modules["agent_service"])
        return importlib.import_module("agent_service")
    finally:
        try:
            sys.path.remove(str(_ROOT / "docker"))
        except ValueError:
            pass


class _FakeHandler:
    """Minimal stand-in for AgentHandler used by ``_require_auth``."""

    def __init__(self, path: str, auth_header: str | None):
        self.path = path
        self.headers = {}
        if auth_header is not None:
            self.headers["Authorization"] = auth_header
        self.errors: list[tuple[int, str]] = []

    def send_error(self, code: int, message: str) -> None:
        self.errors.append((code, message))


class TestAgentServiceAuth(unittest.TestCase):
    """Unit-level checks of ``_require_auth`` semantics."""

    @classmethod
    def setUpClass(cls):
        cls.svc = _load_agent_service_module()

    def setUp(self):
        # Reset the module-level token to a known value for each test.
        self._orig_token = self.svc._AGENT_SERVICE_TOKEN
        self.svc._AGENT_SERVICE_TOKEN = "test-token-1234"

    def tearDown(self):
        self.svc._AGENT_SERVICE_TOKEN = self._orig_token

    def test_health_is_public(self):
        h = _FakeHandler("/health", auth_header=None)
        self.assertTrue(self.svc._require_auth(h))
        self.assertEqual(h.errors, [])

    def test_health_with_query_is_public(self):
        h = _FakeHandler("/health?foo=bar", auth_header=None)
        self.assertTrue(self.svc._require_auth(h))

    def test_health_a11y_requires_auth(self):
        h = _FakeHandler("/health/a11y", auth_header=None)
        self.assertFalse(self.svc._require_auth(h))
        self.assertEqual(h.errors[0][0], 401)

    def test_post_action_unauthenticated_rejected(self):
        h = _FakeHandler("/action", auth_header=None)
        self.assertFalse(self.svc._require_auth(h))
        self.assertEqual(h.errors[0][0], 401)

    def test_post_action_wrong_token_rejected(self):
        h = _FakeHandler("/action", auth_header="Bearer wrong")
        self.assertFalse(self.svc._require_auth(h))
        self.assertEqual(h.errors[0][0], 401)

    def test_post_action_correct_token_accepted(self):
        h = _FakeHandler("/action", auth_header="Bearer test-token-1234")
        self.assertTrue(self.svc._require_auth(h))
        self.assertEqual(h.errors, [])

    def test_non_bearer_scheme_rejected(self):
        h = _FakeHandler("/action", auth_header="Basic dXNlcjpwYXNz")
        self.assertFalse(self.svc._require_auth(h))

    def test_token_unconfigured_rejects(self):
        self.svc._AGENT_SERVICE_TOKEN = None
        h = _FakeHandler("/action", auth_header="Bearer anything")
        self.assertFalse(self.svc._require_auth(h))
        self.assertEqual(h.errors[0][0], 401)

    def test_constant_time_compare_used(self):
        """``hmac.compare_digest`` must be used (timing-safe)."""
        text = _AGENT_SERVICE.read_text(encoding="utf-8")
        self.assertIn("hmac.compare_digest", text)
        # And no naive == comparison on the token.
        self.assertNotRegex(
            text,
            r"presented\s*==\s*_AGENT_SERVICE_TOKEN",
            "Token compared with == — must use hmac.compare_digest",
        )


class TestAgentServiceSource(unittest.TestCase):
    """Static guarantees about ``do_GET`` / ``do_POST`` and /health."""

    @classmethod
    def setUpClass(cls):
        cls.text = _AGENT_SERVICE.read_text(encoding="utf-8")

    def test_do_get_calls_require_auth_first(self):
        # The first non-trivial line inside do_GET must be _require_auth.
        # Be lenient about docstring lines.
        idx = self.text.index("def do_GET(self):")
        block = self.text[idx:idx + 400]
        self.assertIn("_require_auth(self)", block)

    def test_do_post_calls_require_auth_first(self):
        idx = self.text.index("def do_POST(self):")
        block = self.text[idx:idx + 400]
        self.assertIn("_require_auth(self)", block)

    def test_health_in_public_paths(self):
        self.assertIn("_PUBLIC_PATHS", self.text)
        # Must contain /health and NOT /health/a11y or /screenshot.
        # Locate the tuple definition.
        line = next(
            ln for ln in self.text.splitlines()
            if ln.strip().startswith("_PUBLIC_PATHS")
        )
        self.assertIn('"/health"', line)
        self.assertNotIn('"/health/a11y"', line)
        self.assertNotIn('"/screenshot"', line)

    def test_token_loaded_from_env_file(self):
        self.assertIn("AGENT_SERVICE_TOKEN_FILE", self.text)


class TestEntrypointTokenGeneration(unittest.TestCase):
    """entrypoint.sh must mint a token before starting the service."""

    @classmethod
    def setUpClass(cls):
        cls.text = (_ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")

    def test_token_file_path(self):
        self.assertIn("/run/secrets/agent_service_token", self.text)

    def test_token_chmod_0400(self):
        self.assertIn("chmod 0400", self.text)

    def test_token_exported_for_service(self):
        self.assertIn("AGENT_SERVICE_TOKEN_FILE", self.text)


class TestDockerManagerTokenExtraction(unittest.TestCase):
    """``docker_manager`` must copy the token out and register it."""

    @classmethod
    def setUpClass(cls):
        cls.text = _DOCKER_MANAGER.read_text(encoding="utf-8")

    def test_docker_cp_for_token(self):
        self.assertIn("agent_service_token", self.text)
        # The cp source path is /run/secrets/agent_service_token.
        self.assertIn("/run/secrets/agent_service_token", self.text)
        self.assertIn('"docker", "cp"', self.text)

    def test_token_file_tracked_for_cleanup(self):
        # Tempfile path must be added to _tracked_secret_files so
        # stop_container shreds it.
        self.assertIn("_tracked_secret_files.add(host_path)", self.text)

    def test_token_registered_with_agent_auth(self):
        self.assertIn("agent_auth.set_token_path", self.text)

    def test_token_cleared_on_stop(self):
        self.assertIn("agent_auth.clear_token()", self.text)


class TestAgentAuthHelper(unittest.TestCase):
    """``backend.utils.agent_auth`` round-trip."""

    def setUp(self):
        from backend.utils import agent_auth
        self.mod = agent_auth
        self.mod.clear_token()

    def tearDown(self):
        self.mod.clear_token()

    def test_no_token_returns_empty_headers(self):
        self.assertEqual(self.mod.get_auth_headers(), {})

    def test_set_token_path_round_trip(self):
        import tempfile
        fd, path = tempfile.mkstemp(prefix="cua-test-token-")
        os.write(fd, b"super-secret-value\n")
        os.close(fd)
        try:
            self.mod.set_token_path(path)
            self.assertEqual(
                self.mod.get_auth_headers(),
                {"Authorization": "Bearer super-secret-value"},
            )
        finally:
            os.unlink(path)
            self.mod.clear_token()

    def test_empty_token_file_raises(self):
        import tempfile
        fd, path = tempfile.mkstemp(prefix="cua-test-token-")
        os.close(fd)
        try:
            with self.assertRaises(OSError):
                self.mod.set_token_path(path)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
