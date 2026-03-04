"""Tests for browser bootstrap improvements in Docker container.

Covers:
- Browser binary resolution (_resolve_browser_binary)
- Chrome launch flags include all first-run suppression flags
- Modal detection & auto-dismiss (_dismiss_known_modals)
- open_url uses direct browser launch instead of blind xdg-open
- xdg-open fallback only when no browser binary found
- Prompt updates for modal handling guidance
"""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import MagicMock, patch, call

import sys
import os

# Ensure the project root is on sys.path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Tests: Browser binary resolution
# ---------------------------------------------------------------------------

class TestResolveBrowserBinary(unittest.TestCase):
    """_resolve_browser_binary returns the first available browser + flags."""

    @patch("shutil.which")
    def test_google_chrome_preferred(self, mock_which):
        """google-chrome is preferred over all others."""
        mock_which.side_effect = lambda name: (
            "/usr/bin/google-chrome" if name == "google-chrome" else None
        )
        from docker.agent_service import _resolve_browser_binary
        result = _resolve_browser_binary()
        self.assertIsNotNone(result)
        binary, flags = result
        self.assertEqual(binary, "/usr/bin/google-chrome")
        self.assertIn("--no-sandbox", flags)
        self.assertIn("--no-first-run", flags)
        self.assertIn("--disable-first-run-ui", flags)
        self.assertIn("--disable-sync", flags)

    @patch("shutil.which")
    def test_chromium_fallback(self, mock_which):
        """Falls back to chromium-browser when chrome is missing."""
        def side_effect(name):
            if name == "chromium-browser":
                return "/usr/bin/chromium-browser"
            return None
        mock_which.side_effect = side_effect
        from docker.agent_service import _resolve_browser_binary
        result = _resolve_browser_binary()
        self.assertIsNotNone(result)
        binary, _ = result
        self.assertEqual(binary, "/usr/bin/chromium-browser")

    @patch("shutil.which")
    def test_firefox_fallback(self, mock_which):
        """Falls back to firefox when no Chrome variant exists."""
        def side_effect(name):
            if name == "firefox":
                return "/usr/bin/firefox"
            return None
        mock_which.side_effect = side_effect
        from docker.agent_service import _resolve_browser_binary
        result = _resolve_browser_binary()
        self.assertIsNotNone(result)
        binary, flags = result
        self.assertEqual(binary, "/usr/bin/firefox")
        self.assertIn("--new-window", flags)
        # Firefox must NOT get Chrome-specific flags
        self.assertNotIn("--no-sandbox", flags)
        self.assertNotIn("--user-data-dir=/tmp/chrome-profile", flags)

    @patch("shutil.which", return_value=None)
    def test_none_when_no_browser(self, _mock_which):
        """Returns None when no browser is installed."""
        from docker.agent_service import _resolve_browser_binary
        result = _resolve_browser_binary()
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Tests: Chrome flags completeness
# ---------------------------------------------------------------------------

class TestChromeFlags(unittest.TestCase):
    """Verify _CHROME_FLAGS suppress all first-run UI."""

    def test_flags_include_essential_suppressions(self):
        from docker.agent_service import _CHROME_FLAGS
        required = [
            "--no-sandbox",
            "--no-first-run",
            "--disable-first-run-ui",
            "--disable-sync",
            "--disable-extensions",
            "--disable-default-apps",
            "--password-store=basic",
            "--no-default-browser-check",
        ]
        for flag in required:
            self.assertIn(flag, _CHROME_FLAGS, f"Missing flag: {flag}")

    def test_user_data_dir_points_to_chrome_profile(self):
        from docker.agent_service import _CHROME_FLAGS
        profile_flags = [f for f in _CHROME_FLAGS if f.startswith("--user-data-dir=")]
        self.assertEqual(len(profile_flags), 1)
        self.assertIn("/tmp/chrome-profile", profile_flags[0])


# ---------------------------------------------------------------------------
# Tests: Modal detection & dismissal
# ---------------------------------------------------------------------------

class TestDismissKnownModals(unittest.TestCase):
    """_dismiss_known_modals detects modals by title and closes them."""

    @patch("subprocess.run")
    def test_dismisses_welcome_modal(self, mock_run):
        """Detects 'Welcome to Google Chrome' and closes via wmctrl -c."""
        # First call: wmctrl -l
        wmctrl_list = MagicMock()
        wmctrl_list.returncode = 0
        wmctrl_list.stdout = (
            "0x04000001  0 host Welcome to Google Chrome\n"
            "0x04000002  0 host Desktop\n"
        )
        # Second call: wmctrl -c
        wmctrl_close = MagicMock()
        wmctrl_close.returncode = 0

        mock_run.side_effect = [wmctrl_list, wmctrl_close]

        from docker.agent_service import _dismiss_known_modals
        dismissed = _dismiss_known_modals()
        self.assertEqual(len(dismissed), 1)
        self.assertIn("Welcome to Google Chrome", dismissed[0])

    @patch("subprocess.run")
    def test_dismisses_keyring_modal(self, mock_run):
        """Detects 'Choose password for new keyring' and closes it."""
        wmctrl_list = MagicMock()
        wmctrl_list.returncode = 0
        wmctrl_list.stdout = (
            "0x04000001  0 host Choose password for new keyring\n"
        )
        wmctrl_close = MagicMock()
        wmctrl_close.returncode = 0
        mock_run.side_effect = [wmctrl_list, wmctrl_close]

        from docker.agent_service import _dismiss_known_modals
        dismissed = _dismiss_known_modals()
        self.assertEqual(len(dismissed), 1)
        self.assertIn("keyring", dismissed[0].lower())

    @patch("subprocess.run")
    def test_no_modals_returns_empty(self, mock_run):
        """When no known modals are present, returns empty list."""
        wmctrl_list = MagicMock()
        wmctrl_list.returncode = 0
        wmctrl_list.stdout = (
            "0x04000001  0 host Desktop\n"
            "0x04000002  0 host My App\n"
        )
        mock_run.side_effect = [wmctrl_list]

        from docker.agent_service import _dismiss_known_modals
        dismissed = _dismiss_known_modals()
        self.assertEqual(len(dismissed), 0)

    @patch("subprocess.run")
    def test_wmctrl_failure_returns_empty(self, mock_run):
        """When wmctrl -l fails, returns empty list gracefully."""
        wmctrl_list = MagicMock()
        wmctrl_list.returncode = 1
        wmctrl_list.stdout = ""
        mock_run.side_effect = [wmctrl_list]

        from docker.agent_service import _dismiss_known_modals
        dismissed = _dismiss_known_modals()
        self.assertEqual(len(dismissed), 0)

    @patch("subprocess.run")
    def test_multiple_modals_dismissed(self, mock_run):
        """Dismisses multiple known modals in one pass."""
        wmctrl_list = MagicMock()
        wmctrl_list.returncode = 0
        wmctrl_list.stdout = (
            "0x04000001  0 host Welcome to Google Chrome\n"
            "0x04000002  0 host Sign in to Chrome\n"
            "0x04000003  0 host Desktop\n"
        )
        close1 = MagicMock(returncode=0)
        close2 = MagicMock(returncode=0)
        mock_run.side_effect = [wmctrl_list, close1, close2]

        from docker.agent_service import _dismiss_known_modals
        dismissed = _dismiss_known_modals()
        self.assertEqual(len(dismissed), 2)


# ---------------------------------------------------------------------------
# Tests: open_url uses direct browser launch
# ---------------------------------------------------------------------------

class TestOpenUrlInBrowser(unittest.TestCase):
    """_open_url_in_browser launches Chrome with flags, not xdg-open."""

    @patch("docker.agent_service._xdo", return_value="")
    @patch("docker.agent_service._xdo_normalize_window", return_value="normalized")
    @patch("docker.agent_service._xdo_search_window_ids", return_value=["0x12345"])
    @patch("docker.agent_service._dismiss_known_modals", return_value=[])
    @patch("subprocess.Popen")
    @patch("docker.agent_service._resolve_browser_binary")
    @patch("time.sleep")
    def test_uses_chrome_not_xdg_open(
        self, _sleep, mock_resolve, mock_popen, _modal, _search, _norm, _xdo
    ):
        """open_url should launch google-chrome, not xdg-open."""
        mock_resolve.return_value = (
            "/usr/bin/google-chrome",
            ["--no-sandbox", "--no-first-run"],
        )
        from docker.agent_service import _open_url_in_browser
        result = _open_url_in_browser("https://example.com")
        self.assertTrue(result["success"])
        self.assertIn("google-chrome", result["message"])

        # Verify the FIRST Popen call was chrome, not xdg-open
        first_call = mock_popen.call_args_list[0]
        cmd = first_call[0][0]
        self.assertEqual(cmd[0], "/usr/bin/google-chrome")
        self.assertIn("--no-sandbox", cmd)
        self.assertIn("https://example.com", cmd)

    @patch("subprocess.Popen")
    @patch("docker.agent_service._resolve_browser_binary", return_value=None)
    @patch("time.sleep")
    def test_xdg_open_fallback_when_no_browser(self, _sleep, mock_resolve, mock_popen):
        """Falls back to xdg-open when no browser binary is found."""
        from docker.agent_service import _open_url_in_browser
        result = _open_url_in_browser("https://example.com")
        self.assertTrue(result["success"])
        self.assertIn("xdg-open fallback", result["message"])

        popen_call = mock_popen.call_args
        cmd = popen_call[0][0]
        self.assertEqual(cmd[0], "xdg-open")

    @patch("docker.agent_service._xdo", return_value="")
    @patch("docker.agent_service._xdo_normalize_window", return_value="normalized")
    @patch("docker.agent_service._xdo_search_window_ids", return_value=["0x12345"])
    @patch("docker.agent_service._dismiss_known_modals", return_value=["Welcome to Google Chrome"])
    @patch("subprocess.Popen")
    @patch("docker.agent_service._resolve_browser_binary")
    @patch("time.sleep")
    def test_dismisses_modals_after_launch(
        self, _sleep, mock_resolve, mock_popen, mock_modal, _search, _norm, _xdo
    ):
        """After browser launch, modals are detected and dismissed."""
        mock_resolve.return_value = (
            "/usr/bin/google-chrome",
            ["--no-sandbox"],
        )
        from docker.agent_service import _open_url_in_browser
        result = _open_url_in_browser("https://example.com")
        self.assertTrue(result["success"])
        self.assertIn("dismissed modals", result["message"])

    @patch("docker.agent_service._xdo", return_value="")
    @patch("docker.agent_service._xdo_normalize_window", return_value="normalized")
    @patch("docker.agent_service._xdo_search_window_ids", return_value=["0x12345"])
    @patch("docker.agent_service._dismiss_known_modals", return_value=[])
    @patch("subprocess.Popen")
    @patch("docker.agent_service._resolve_browser_binary")
    @patch("time.sleep")
    def test_auto_prefix_https(
        self, _sleep, mock_resolve, mock_popen, _modal, _search, _norm, _xdo
    ):
        """URLs without scheme get https:// prepended."""
        mock_resolve.return_value = (
            "/usr/bin/google-chrome",
            ["--no-sandbox"],
        )
        from docker.agent_service import _open_url_in_browser
        _open_url_in_browser("example.com")

        first_call = mock_popen.call_args_list[0]
        cmd = first_call[0][0]
        self.assertIn("https://example.com", cmd)


# ---------------------------------------------------------------------------
# Tests: Known modal titles list
# ---------------------------------------------------------------------------

class TestKnownModalTitles(unittest.TestCase):
    """_KNOWN_MODAL_TITLES covers the common first-run dialogs."""

    def test_covers_chrome_welcome(self):
        from docker.agent_service import _KNOWN_MODAL_TITLES
        titles = " ".join(_KNOWN_MODAL_TITLES).lower()
        self.assertIn("welcome", titles)

    def test_covers_sign_in(self):
        from docker.agent_service import _KNOWN_MODAL_TITLES
        titles = " ".join(_KNOWN_MODAL_TITLES).lower()
        self.assertIn("sign in", titles)

    def test_covers_keyring(self):
        from docker.agent_service import _KNOWN_MODAL_TITLES
        titles = " ".join(_KNOWN_MODAL_TITLES).lower()
        self.assertIn("keyring", titles)


# ---------------------------------------------------------------------------
# Tests: Prompt updates for modal handling
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Tests: _xdo_open_url and _ydo_open_url delegate to shared function
# ---------------------------------------------------------------------------

class TestOpenUrlDelegation(unittest.TestCase):
    """Both engine-specific open_url functions delegate to _open_url_in_browser."""

    @patch("docker.agent_service._open_url_in_browser")
    def test_xdo_open_url_delegates(self, mock_shared):
        mock_shared.return_value = {"success": True, "message": "ok"}
        from docker.agent_service import _xdo_open_url
        result = _xdo_open_url("https://example.com")
        mock_shared.assert_called_once_with("https://example.com")
        self.assertTrue(result["success"])

    @patch("docker.agent_service._open_url_in_browser")
    def test_ydo_open_url_delegates(self, mock_shared):
        mock_shared.return_value = {"success": True, "message": "ok"}
        from docker.agent_service import _ydo_open_url
        result = _ydo_open_url("https://example.com")
        mock_shared.assert_called_once_with("https://example.com")
        self.assertTrue(result["success"])


if __name__ == "__main__":
    unittest.main()
