"""Per-session upload prefix tests (I-015 / F-020).

The previous ``_UPLOAD_ALLOWED_PREFIXES = ("/tmp", "/app", "/home")``
allowed an LLM-driven upload to overwrite ``/home/user/.bashrc`` or
the Chromium preferences file — a clean persistence vector across
container restarts.  I-015 replaces the broad allowlist with a
per-session subdir under ``/tmp/cua-uploads/<session_id>``.  The
session id arrives via the ``X-Session-Id`` request header set by
the host-side executor.

Tests:
  * ``_upload_prefix`` returns a session-scoped path under the base
    and creates the directory.
  * Different session ids return different paths.
  * Source-level: ``_UPLOAD_ALLOWED_PREFIXES`` constant is gone, ``/home``
    is not in the new prefix scheme, ``_pw_upload_file`` accepts
    ``session_id``.
  * ``_pw_upload_file`` rejects writes outside the prefix (e.g.
    ``/home/user/.bashrc``) and accepts writes inside.
  * The host-side executor sends ``X-Session-Id`` when ``session_id``
    is provided.

Closes F-020.  Implements I-015.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


_ROOT = Path(__file__).resolve().parent.parent
_AGENT_SERVICE = _ROOT / "docker" / "agent_service.py"


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


class _FakePage:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def set_input_files(self, selector, file_path):
        self.calls.append((selector, file_path))


class TestUploadPrefix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc = _load_agent_service_module()

    def setUp(self):
        # Redirect the upload base to a host tempdir so tests work on
        # platforms that don't have /tmp (Windows).
        self._orig_base = self.svc._UPLOAD_BASE
        self._tmp = tempfile.mkdtemp(prefix="cua-upload-test-")
        self.svc._UPLOAD_BASE = self._tmp

    def tearDown(self):
        self.svc._UPLOAD_BASE = self._orig_base
        # Best-effort recursive cleanup.
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_prefix_creates_dir(self):
        p = self.svc._upload_prefix("alpha")
        self.assertTrue(os.path.isdir(p))
        self.assertEqual(os.path.basename(p), "alpha")

    def test_prefix_distinct_per_session(self):
        a = self.svc._upload_prefix("one")
        b = self.svc._upload_prefix("two")
        self.assertNotEqual(a, b)

    def test_none_session_uses_default(self):
        p = self.svc._upload_prefix(None)
        self.assertTrue(p.endswith("default"))

    def test_session_id_traversal_sanitized(self):
        p = self.svc._upload_prefix("../etc")
        # Sanitizer drops slashes and dots → some safe form under base.
        self.assertTrue(p.startswith(self._tmp + os.sep))
        self.assertNotIn("..", os.path.basename(p))


class TestPwUploadFileSandbox(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc = _load_agent_service_module()

    def setUp(self):
        self._orig_base = self.svc._UPLOAD_BASE
        self._tmp = tempfile.mkdtemp(prefix="cua-upload-test-")
        self.svc._UPLOAD_BASE = self._tmp
        self._page = _FakePage()
        # Patch _get_page to return the fake.
        self._page_patch = mock.patch.object(self.svc, "_get_page", return_value=self._page)
        self._page_patch.start()

    def tearDown(self):
        self._page_patch.stop()
        self.svc._UPLOAD_BASE = self._orig_base
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_upload_inside_prefix_accepted(self):
        prefix = self.svc._upload_prefix("alpha")
        target = os.path.join(prefix, "foo.txt")
        with open(target, "w", encoding="utf-8") as f:
            f.write("ok")
        result = self.svc._pw_upload_file("input[type=file]", target, "alpha")
        self.assertTrue(result["success"], result)
        self.assertEqual(self._page.calls, [("input[type=file]", target)])

    def test_upload_outside_prefix_rejected(self):
        # Try writing to a path that is NOT under /tmp/cua-uploads/<sid>.
        # Use a sibling tempdir.
        import tempfile as _tf
        other = _tf.mkdtemp(prefix="cua-other-")
        try:
            target = os.path.join(other, "evil.txt")
            with open(target, "w", encoding="utf-8") as f:
                f.write("nope")
            result = self.svc._pw_upload_file("input[type=file]", target, "alpha")
            self.assertFalse(result["success"], result)
            self.assertIn("Upload restricted", result["message"])
            self.assertEqual(self._page.calls, [])
        finally:
            import shutil
            shutil.rmtree(other, ignore_errors=True)

    def test_home_bashrc_rejected(self):
        # Even with no session_id (uses 'default'), /home/user/.bashrc must reject.
        result = self.svc._pw_upload_file(
            "input[type=file]", "/home/user/.bashrc", session_id=None,
        )
        self.assertFalse(result["success"], result)

    def test_neighbour_dir_not_a_prefix_match(self):
        """``/tmp/.../foo`` must not authorize writes to ``/tmp/.../foobar``."""
        # Create the "foo" prefix; then attempt to write into a neighbour
        # called "foobar" with same session-id base.
        prefix_foo = self.svc._upload_prefix("foo")
        # Create a sibling directory adjacent to the prefix.
        sibling = prefix_foo + "bar"
        os.makedirs(sibling, exist_ok=True)
        target = os.path.join(sibling, "x.txt")
        with open(target, "w", encoding="utf-8") as f:
            f.write("x")
        result = self.svc._pw_upload_file("input[type=file]", target, "foo")
        self.assertFalse(result["success"], result)


class TestSourceLevelGuarantees(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = _AGENT_SERVICE.read_text(encoding="utf-8")

    def test_old_constant_removed(self):
        # The exact tuple that contained /home is gone.
        self.assertNotIn(
            '_UPLOAD_ALLOWED_PREFIXES = ("/tmp", "/app", "/home")',
            self.text,
        )

    def test_upload_base_under_tmp_cua_uploads(self):
        self.assertIn('_UPLOAD_BASE = "/tmp/cua-uploads"', self.text)

    def test_pw_upload_file_takes_session_id(self):
        self.assertIn(
            "def _pw_upload_file(selector: str, file_path: str, session_id:",
            self.text,
        )

    def test_dispatcher_threads_session_id(self):
        # do_POST must read X-Session-Id and pass it through.
        self.assertIn("X-Session-Id", self.text)
        self.assertIn("_session_id_from_headers", self.text)

    def test_executor_sends_session_header(self):
        executor_text = (_ROOT / "backend" / "agent" / "executor.py").read_text(encoding="utf-8")
        self.assertIn('headers["X-Session-Id"] = session_id', executor_text)


class TestExecutorSendsSessionHeader(unittest.IsolatedAsyncioTestCase):
    async def test_session_id_propagated_to_request_header(self):
        from backend.agent import executor as ex

        captured: dict = {}

        class _FakeResp:
            status_code = 200

            def json(self):
                return {"success": True, "message": "ok"}

            @property
            def text(self):
                return ""

        class _FakeClient:
            async def post(self, url, json=None, headers=None):
                captured["url"] = url
                captured["headers"] = headers or {}
                return _FakeResp()

        with mock.patch.object(ex, "_get_client", return_value=_FakeClient()):
            result = await ex._send_with_retry(
                {"action": "click"}, retries=0, session_id="sess-xyz",
            )
        self.assertTrue(result["success"])
        self.assertEqual(captured["headers"].get("X-Session-Id"), "sess-xyz")

    async def test_no_session_id_no_header(self):
        from backend.agent import executor as ex

        captured: dict = {}

        class _FakeResp:
            status_code = 200

            def json(self):
                return {"success": True}

        class _FakeClient:
            async def post(self, url, json=None, headers=None):
                captured["headers"] = headers or {}
                return _FakeResp()

        with mock.patch.object(ex, "_get_client", return_value=_FakeClient()):
            await ex._send_with_retry({"action": "click"}, retries=0)
        self.assertNotIn("X-Session-Id", captured["headers"])


if __name__ == "__main__":
    unittest.main()
