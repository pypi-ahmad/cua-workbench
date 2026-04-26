from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent


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


class TestAgentServiceCommandGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc = _load_agent_service_module()

    def test_ls_allowed(self):
        allowed, reason = self.svc._command_is_allowed("ls /tmp")

        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_rm_blocked_when_program_not_allowlisted(self):
        allowed, reason = self.svc._command_is_allowed("rm -rf /")

        self.assertFalse(allowed)
        self.assertIn("program not in allowlist", reason)
        self.assertIn("'rm'", reason)

    def test_semicolon_chain_blocked(self):
        allowed, reason = self.svc._command_is_allowed("ls; rm -rf /")

        self.assertFalse(allowed)
        self.assertIn("chaining/redirection forbidden", reason)
        self.assertIn("';'", reason)

    def test_obfuscated_rm_blocked_after_shlex(self):
        allowed, reason = self.svc._command_is_allowed("r''m -rf /")

        self.assertFalse(allowed)
        self.assertIn("program not in allowlist", reason)
        self.assertIn("'rm'", reason)

    def test_variable_expansion_blocked(self):
        allowed, reason = self.svc._command_is_allowed("echo $IFS")

        self.assertFalse(allowed)
        self.assertIn("chaining/redirection forbidden", reason)
        self.assertIn("'$'", reason)

    def test_logical_and_blocked(self):
        allowed, reason = self.svc._command_is_allowed("ls && rm -rf /")

        self.assertFalse(allowed)
        self.assertIn("chaining/redirection forbidden", reason)
        self.assertIn("'&&'", reason)

    def test_dispatch_desktop_returns_403_for_blocked_command(self):
        handler = self.svc.AgentHandler.__new__(self.svc.AgentHandler)

        result = handler._dispatch_desktop("run_command", 0, 0, "rm -rf /", [], "")

        self.assertFalse(result["success"])
        self.assertEqual(result["_status_code"], 403)
        self.assertIn("program not in allowlist", result["message"])