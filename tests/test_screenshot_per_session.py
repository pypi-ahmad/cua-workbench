"""Per-session screenshot tempfile tests (I-008 / F-014).

The in-container ``agent_service`` previously wrote every screenshot to
``/tmp/screenshot.png`` (and ``/tmp/full.png``, ``/tmp/region.png``).
With three concurrent sessions, session A could read session B's frame
and feed it to its LLM.  I-008 replaces shared paths with
``tempfile.NamedTemporaryFile(prefix="cua-<sid>-", suffix=".png")`` and
adds a janitor that prunes leaked files older than 60s.

These tests exercise:
  * ``make_tempshot`` returns a unique path under /tmp with the
    expected ``cua-<sid>-...`` prefix.
  * Two distinct session ids produce distinct paths.
  * Same session_id, two calls, still produce distinct paths
    (kernel-side uniquification via tempfile).
  * The janitor matches the documented file-age rule.
  * The host-side ``capture_screenshot`` threads ``session_id`` into
    the agent_service URL.
  * Source-level: no remaining hardcoded ``/tmp/screenshot.png`` /
    ``/tmp/cu_screenshot.png`` references in the screenshot path.
"""

from __future__ import annotations

import importlib
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


_ROOT = Path(__file__).resolve().parent.parent
_AGENT_SERVICE = _ROOT / "docker" / "agent_service.py"
_SCREENSHOT_HOST = _ROOT / "backend" / "agent" / "screenshot.py"
_CU_ENGINE = _ROOT / "backend" / "engines" / "computer_use_engine.py"


def _load_agent_service_module():
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


class TestMakeTempshot(unittest.TestCase):
    """``agent_service.make_tempshot`` is the single source of truth."""

    @classmethod
    def setUpClass(cls):
        cls.svc = _load_agent_service_module()

    def setUp(self):
        # Track files we create so we can clean up even on failure.
        self._paths: list[Path] = []
        # The agent_service is designed to run inside a Linux container
        # where /tmp exists.  Tests run on the host (which on Windows
        # has no /tmp), so redirect the screenshot dir to the platform
        # tempdir for the duration of the test.
        self._tmpdir = tempfile.mkdtemp(prefix="cua-test-")
        self._orig_dir = self.svc._SCREENSHOT_DIR
        self.svc._SCREENSHOT_DIR = self._tmpdir

    def tearDown(self):
        self.svc._SCREENSHOT_DIR = self._orig_dir
        for p in self._paths:
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(self._tmpdir)
        except OSError:
            pass

    def _make(self, sid: str | None) -> Path:
        p = self.svc.make_tempshot(sid)
        self._paths.append(p)
        return p

    def test_path_under_tmp_with_prefix(self):
        p = self._make("alpha")
        self.assertEqual(str(p.parent), self._tmpdir)
        self.assertTrue(p.name.startswith("cua-alpha-"))
        self.assertTrue(p.name.endswith(".png"))
        self.assertTrue(p.exists())

    def test_distinct_sessions_distinct_paths(self):
        a = self._make("alpha")
        b = self._make("beta")
        self.assertNotEqual(a, b)
        self.assertIn("cua-alpha-", a.name)
        self.assertIn("cua-beta-", b.name)

    def test_same_session_two_calls_distinct_paths(self):
        """Even within one session, two consecutive captures must not collide."""
        a = self._make("same")
        b = self._make("same")
        self.assertNotEqual(a, b)

    def test_session_id_is_sanitized(self):
        # Slashes / dots must not break the prefix or escape the dir.
        p = self._make("../etc/passwd")
        self.assertEqual(str(p.parent), self._tmpdir)
        self.assertTrue(p.name.startswith("cua-"))
        self.assertNotIn("..", p.name)

    def test_none_session_uses_default_prefix(self):
        p = self._make(None)
        self.assertTrue(p.name.startswith("cua-default-"))


class TestScreenshotJanitor(unittest.TestCase):
    """The janitor matches /tmp/cua-* and skips unrelated files."""

    @classmethod
    def setUpClass(cls):
        cls.svc = _load_agent_service_module()

    def test_pattern_matches_tempshot_prefix(self):
        prefix_re = self.svc._SCREENSHOT_PREFIX_RE
        self.assertIsNotNone(prefix_re.match("cua-alpha-abcd.png"))
        self.assertIsNotNone(prefix_re.match("cua-default-1234.png"))

    def test_pattern_does_not_match_unrelated_files(self):
        prefix_re = self.svc._SCREENSHOT_PREFIX_RE
        for name in ("screenshot.png", "tmp.txt", "cua.txt", ".X11-unix"):
            self.assertIsNone(prefix_re.match(name), name)

    def test_max_age_documented_60s(self):
        self.assertEqual(self.svc._SCREENSHOT_MAX_AGE_S, 60.0)


class TestSourceLevelGuarantees(unittest.TestCase):
    """Hardcoded shared paths must not creep back in."""

    def test_agent_service_no_shared_screenshot_path(self):
        text = _AGENT_SERVICE.read_text(encoding="utf-8")
        # The literal "/tmp/screenshot.png" must not appear anywhere.
        self.assertNotIn("/tmp/screenshot.png", text)
        self.assertNotIn("/tmp/full.png", text)
        self.assertNotIn("/tmp/region.png", text)

    def test_host_screenshot_no_shared_path(self):
        text = _SCREENSHOT_HOST.read_text(encoding="utf-8")
        self.assertNotIn('"/tmp/screenshot.png"', text)
        # Fallback uses a uuid-based per-call name.
        self.assertIn("uuid", text.lower())

    def test_cu_engine_no_shared_path(self):
        text = _CU_ENGINE.read_text(encoding="utf-8")
        self.assertNotIn('"/tmp/cu_screenshot.png"', text)
        self.assertNotIn("/tmp/cu_screenshot.png", text)

    def test_agent_service_starts_janitor(self):
        text = _AGENT_SERVICE.read_text(encoding="utf-8")
        self.assertIn("_start_screenshot_janitor", text)


class TestCaptureScreenshotThreadsSessionId(unittest.IsolatedAsyncioTestCase):
    """Host-side ``capture_screenshot`` must pass session_id to the URL."""

    async def test_session_id_appears_in_url(self):
        from backend.agent import screenshot as ss

        captured: dict[str, str] = {}

        class _FakeResp:
            def raise_for_status(self):  # noqa: D401
                return None

            def json(self):
                return {"screenshot": "Zm9v", "method": "desktop"}

        class _FakeClient:
            async def get(self, url, headers=None):  # noqa: D401
                captured["url"] = url
                return _FakeResp()

        with mock.patch.object(ss, "_get_client", return_value=_FakeClient()):
            await ss.capture_screenshot(mode="desktop", session_id="abc-123")

        self.assertIn("session_id=abc-123", captured["url"])
        self.assertIn("mode=desktop", captured["url"])

    async def test_no_session_id_omits_param(self):
        from backend.agent import screenshot as ss

        captured: dict[str, str] = {}

        class _FakeResp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"screenshot": "Zm9v"}

        class _FakeClient:
            async def get(self, url, headers=None):
                captured["url"] = url
                return _FakeResp()

        with mock.patch.object(ss, "_get_client", return_value=_FakeClient()):
            await ss.capture_screenshot(mode="browser")

        self.assertNotIn("session_id=", captured["url"])


if __name__ == "__main__":
    unittest.main()
