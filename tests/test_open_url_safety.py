"""Focused tests for the accessibility-engine `_h_open_url` handler.

The audit flagged a previous Windows path that ran
``subprocess.run(["start", url], shell=True)``.  The current code uses
``os.startfile(url)`` (ShellExecuteW — no cmd.exe, no shell parsing)
guarded by strict scheme/character validation.  These tests lock that
contract:

  1) URL is required (rejects empty).
  2) Only ``http`` / ``https`` schemes are allowed.
  3) Shell metacharacters in the URL are rejected.
  4) Bare hostnames are auto-prefixed with ``https://``.
  5) Windows path calls ``os.startfile`` (NOT ``subprocess`` / shell).
  6) Linux/macOS paths use argv-list ``subprocess.run`` with no ``shell=True``.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import MagicMock, patch

from backend.engines.accessibility_engine import _h_open_url


def _run(coro):
    return asyncio.run(coro)


class TestOpenUrlValidation(unittest.TestCase):
    """Input validation must reject hostile URLs before any OS call."""

    def test_empty_url_rejected(self):
        result = _run(_h_open_url(text="", target=""))
        self.assertFalse(result["success"])
        self.assertIn("URL required", result["message"])

    def test_javascript_scheme_rejected(self):
        result = _run(_h_open_url(text="javascript:alert(1)", target=""))
        self.assertFalse(result["success"])
        # javascript URL gets prefixed to https://javascript:alert(1)
        # which fails scheme/netloc validation, OR is detected as
        # containing a scheme already and rejected — either way: fail.
        self.assertIn("open_url", result["message"].lower())

    def test_shell_metachar_ampersand_rejected(self):
        result = _run(_h_open_url(text="https://x & calc.exe & rem", target=""))
        self.assertFalse(result["success"])
        self.assertIn("metacharacter", result["message"].lower())

    def test_shell_metachar_pipe_rejected(self):
        result = _run(_h_open_url(text="https://x|calc.exe", target=""))
        self.assertFalse(result["success"])

    def test_shell_metachar_semicolon_rejected(self):
        result = _run(_h_open_url(text="https://x;calc.exe", target=""))
        self.assertFalse(result["success"])

    def test_shell_metachar_backtick_rejected(self):
        result = _run(_h_open_url(text="https://x`whoami`", target=""))
        self.assertFalse(result["success"])

    def test_shell_metachar_newline_rejected(self):
        result = _run(_h_open_url(text="https://x\ncalc.exe", target=""))
        self.assertFalse(result["success"])

    def test_shell_metachar_null_rejected(self):
        result = _run(_h_open_url(text="https://x\x00calc.exe", target=""))
        self.assertFalse(result["success"])


class TestOpenUrlWindowsPath(unittest.TestCase):
    """Windows must use os.startfile (ShellExecuteW), never shell=True."""

    @patch("backend.engines.accessibility_engine.platform.system", return_value="Windows")
    def test_windows_uses_os_startfile(self, _mock_system):
        startfile_mock = MagicMock()
        with patch("os.startfile", startfile_mock, create=True), \
             patch("backend.engines.accessibility_engine.subprocess.run") as run_mock:
            result = _run(_h_open_url(text="https://example.com", target=""))
        self.assertTrue(result["success"], result["message"])
        startfile_mock.assert_called_once_with("https://example.com")
        # No subprocess on the Windows path — ever.
        run_mock.assert_not_called()

    @patch("backend.engines.accessibility_engine.platform.system", return_value="Windows")
    def test_windows_auto_prefixes_https(self, _mock_system):
        startfile_mock = MagicMock()
        with patch("os.startfile", startfile_mock, create=True):
            result = _run(_h_open_url(text="example.com", target=""))
        self.assertTrue(result["success"])
        startfile_mock.assert_called_once_with("https://example.com")

    @patch("backend.engines.accessibility_engine.platform.system", return_value="Windows")
    def test_windows_blocks_metachars_before_startfile(self, _mock_system):
        startfile_mock = MagicMock()
        with patch("os.startfile", startfile_mock, create=True):
            result = _run(_h_open_url(text="https://x & calc.exe", target=""))
        self.assertFalse(result["success"])
        startfile_mock.assert_not_called()


class TestOpenUrlPosixPath(unittest.TestCase):
    """Posix paths use argv-list subprocess.run — never shell=True."""

    @patch("backend.engines.accessibility_engine.platform.system", return_value="Linux")
    def test_linux_uses_xdg_open_argv(self, _mock_system):
        with patch("backend.engines.accessibility_engine.subprocess.run") as run_mock:
            run_mock.return_value = MagicMock(returncode=0)
            result = _run(_h_open_url(text="https://example.com", target=""))
        self.assertTrue(result["success"], result["message"])
        run_mock.assert_called_once()
        args, kwargs = run_mock.call_args
        # First positional arg is the argv list; must start with xdg-open and the URL.
        self.assertEqual(args[0], ["xdg-open", "https://example.com"])
        self.assertNotIn("shell", kwargs)  # default is shell=False

    @patch("backend.engines.accessibility_engine.platform.system", return_value="Darwin")
    def test_darwin_uses_open_argv(self, _mock_system):
        with patch("backend.engines.accessibility_engine.subprocess.run") as run_mock:
            run_mock.return_value = MagicMock(returncode=0)
            result = _run(_h_open_url(text="https://example.com", target=""))
        self.assertTrue(result["success"], result["message"])
        args, kwargs = run_mock.call_args
        self.assertEqual(args[0], ["open", "https://example.com"])
        self.assertNotIn("shell", kwargs)


if __name__ == "__main__":
    unittest.main()
