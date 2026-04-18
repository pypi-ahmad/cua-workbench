"""Lock-in tests for Docker image surface minimization.

Pins the negative invariants for the verified-unjustified items
removed in this audit pass:

* No bare ``python`` symlink is installed in the image, so neither
  command allowlist may list ``"python"`` (the LLM would always hit
  "command not found").
* ``pip`` / ``pip3`` live in ``/opt/venv/bin`` and are not on the
  ``_h_run_command`` PATH, so they must not appear in that allowlist.
* Apps NOT installed by docker/Dockerfile (gnome-*, mousepad, firefox,
  xterm, xfce4-taskmanager) must not appear in either allowlist.
* The Dockerfile must not pull the unused ``libatspi2.0-dev`` -dev
  headers package (runtime lib comes via ``at-spi2-core``).
* Active prompts must not advertise applications the image does not
  contain.
"""

from __future__ import annotations

import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
DOCKERFILE  = REPO / "docker" / "Dockerfile"
AGENT_SVC   = REPO / "docker" / "agent_service.py"
A11Y_ENGINE = REPO / "backend" / "engines" / "accessibility_engine.py"


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def _block(src: str, marker: str) -> str:
    """Return the literal ``frozenset({...})`` block following *marker*."""
    idx = src.index(marker)
    open_paren = src.index("frozenset({", idx)
    close = src.index("})", open_paren)
    return src[open_paren:close + 2]


# ── Dockerfile invariants ────────────────────────────────────────────────────

class TestDockerfileMinimal(unittest.TestCase):

    def test_no_libatspi_dev(self):
        # The package must not appear on a non-comment line (i.e. an
        # actual apt install entry).  The audit-trail comment that
        # documents the removal is allowed to mention the name.
        for line in _read(DOCKERFILE).splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertNotIn(
                "libatspi2.0-dev", stripped,
                f"libatspi2.0-dev still installed: {line!r}",
            )

    def test_runtime_a11y_stack_still_present(self):
        # Removing -dev must not drop the runtime stack.
        df = _read(DOCKERFILE)
        for pkg in ("at-spi2-core", "gir1.2-atspi-2.0", "python3-gi"):
            self.assertIn(pkg, df, f"runtime a11y dep {pkg} missing from Dockerfile")


# ── agent_service.py allowlist invariants ────────────────────────────────────

class TestAgentServiceAllowlist(unittest.TestCase):

    def setUp(self) -> None:
        self.block = _block(_read(AGENT_SVC), "_ALLOWED_COMMANDS = frozenset({")

    def test_no_bare_python_symlink_command(self):
        # Must not allow a bare `python` (no python-is-python3 in image).
        self.assertNotRegex(self.block, r'"python"\s*,')

    def test_no_uninstalled_apps(self):
        for cmd in ("xfce4-taskmanager", "gnome-calculator", "firefox", "xterm",
                    "mousepad", "gedit", "gnome-control-center"):
            self.assertNotIn(f'"{cmd}"', self.block,
                             f"{cmd!r} not installed but listed in agent_service allowlist")

    def test_kept_essentials(self):
        for cmd in ("python3", "node", "curl", "xdotool", "scrot",
                    "xfce4-settings-manager", "google-chrome"):
            self.assertIn(f'"{cmd}"', self.block,
                          f"{cmd!r} essential but missing from agent_service allowlist")


# ── accessibility_engine.py allowlist invariants ─────────────────────────────

class TestAccessibilityEngineAllowlist(unittest.TestCase):

    def setUp(self) -> None:
        src = _read(A11Y_ENGINE)
        self.allowed = _block(src, "_ALLOWED_COMMANDS = frozenset({")
        self.gui     = _block(src, "_GUI_COMMANDS = frozenset({")

    def test_no_bare_python_or_pip(self):
        # No `python` symlink; pip lives in /opt/venv (not on _h_run_command PATH).
        self.assertNotRegex(self.allowed, r'"python"\s*,')
        self.assertNotIn('"pip"',  self.allowed)
        self.assertNotIn('"pip3"', self.allowed)

    def test_no_uninstalled_apps_in_either_set(self):
        uninstalled = (
            "xfce4-taskmanager",
            "mousepad",
            "firefox",
            "xterm",
            "gnome-control-center",
            "gnome-settings",
            "gnome-calculator",
            "gnome-text-editor",
            "gedit",
            "gnome-system-monitor",
            "gnome-terminal",
        )
        for cmd in uninstalled:
            self.assertNotIn(f'"{cmd}"', self.allowed,
                             f"{cmd!r} not installed but in _ALLOWED_COMMANDS")
            self.assertNotIn(f'"{cmd}"', self.gui,
                             f"{cmd!r} not installed but in _GUI_COMMANDS")

    def test_kept_essentials(self):
        for cmd in ("python3", "node", "curl", "xdotool", "scrot",
                    "xfce4-settings-manager", "thunar", "google-chrome"):
            self.assertIn(f'"{cmd}"', self.allowed,
                          f"{cmd!r} essential but missing from accessibility allowlist")


# ── Prompt no longer advertises uninstalled apps ─────────────────────────────

class TestPromptMatchesImage(unittest.TestCase):

    def test_active_a11y_prompt_does_not_advertise_uninstalled_apps(self):
        from backend.agent.prompts import get_system_prompt
        prompt = get_system_prompt("omni_accessibility", "desktop")

        # The "Available applications:" inventory section starts after
        # the colon and ends at the first sentence break.  Anything
        # after that (e.g. "Other desktop apps (...) are NOT installed")
        # is negative phrasing and is allowed to mention removed apps.
        m = re.search(r"Available applications:\s*([^.\n]+)", prompt)
        self.assertIsNotNone(m, "prompt must keep an 'Available applications:' line")
        positive = m.group(1)
        for forbidden in ("mousepad", "firefox", "xfce4-taskmanager"):
            self.assertNotIn(
                forbidden, positive,
                f"prompt still positively lists uninstalled app {forbidden!r}: {positive!r}",
            )


if __name__ == "__main__":
    unittest.main()
