"""Focused tests for Docker runtime hardening parity.

Locks the security-relevant parts of the container manifest so a casual
edit cannot regress them silently:

  • docker-compose.yml — port bindings, capabilities, no-new-privileges,
    DNS-rebind protection, pids_limit.
  • docker/Dockerfile — debug ports not in EXPOSE, MCP version pinned.
  • docker/entrypoint.sh — comment block matches the strict ALLOWED_HOSTS.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml


_ROOT = Path(__file__).resolve().parent.parent
_COMPOSE = _ROOT / "docker-compose.yml"
_DOCKERFILE = _ROOT / "docker" / "Dockerfile"
_ENTRYPOINT = _ROOT / "docker" / "entrypoint.sh"


class TestDockerComposeHardening(unittest.TestCase):
    """docker-compose.yml security baseline."""

    @classmethod
    def setUpClass(cls):
        with _COMPOSE.open(encoding="utf-8") as f:
            cls.compose = yaml.safe_load(f)
        cls.svc = cls.compose["services"]["cua-environment"]

    # ── Port exposure ────────────────────────────────────────────────────

    def test_cdp_port_9223_not_published(self):
        """Chrome DevTools port 9223 must NOT appear in compose ports."""
        ports = self.svc.get("ports", []) or []
        for p in ports:
            self.assertNotIn(":9223:", str(p), f"CDP port leaked to host: {p}")
            self.assertFalse(str(p).endswith(":9223"), f"CDP port leaked to host: {p}")

    def test_all_ports_loopback_bound(self):
        """Every published port must bind to 127.0.0.1, never 0.0.0.0."""
        ports = self.svc.get("ports", []) or []
        self.assertGreater(len(ports), 0, "compose has no ports section")
        for p in ports:
            self.assertTrue(
                str(p).startswith("127.0.0.1:"),
                f"Port not loopback-bound: {p!r}",
            )

    # ── Capability hardening ─────────────────────────────────────────────

    def test_cap_drop_all(self):
        """All Linux capabilities must be dropped by default."""
        cap_drop = self.svc.get("cap_drop", []) or []
        self.assertIn("ALL", cap_drop)

    def test_cap_add_minimal_set(self):
        """cap_add must contain only the documented minimal set."""
        cap_add = set(self.svc.get("cap_add", []) or [])
        # The set must be a subset of these — no SYS_ADMIN, NET_ADMIN, etc.
        allowed = {"CHOWN", "SETUID", "SETGID", "DAC_OVERRIDE", "SYS_CHROOT", "KILL"}
        self.assertTrue(
            cap_add.issubset(allowed),
            f"cap_add includes unexpected capabilities: {cap_add - allowed}",
        )

    def test_no_new_privileges(self):
        """no-new-privileges must be set so suid binaries can't escalate."""
        sec = self.svc.get("security_opt", []) or []
        self.assertTrue(
            any("no-new-privileges" in s for s in sec),
            f"no-new-privileges not in security_opt: {sec}",
        )

    def test_pids_limit_set(self):
        """pids_limit must be set to defeat fork-bomb DoS."""
        pids = self.svc.get("pids_limit")
        self.assertIsNotNone(pids, "pids_limit not configured")
        self.assertGreater(int(pids), 0)
        self.assertLessEqual(int(pids), 4096, "pids_limit is too generous")

    # ── DNS-rebind / Host-header protection ──────────────────────────────

    def test_playwright_mcp_allowed_hosts_loopback_only(self):
        """ALLOWED_HOSTS must NOT be '*' — DNS-rebind defense."""
        env = self.svc.get("environment", []) or []
        if isinstance(env, dict):
            allowed = env.get("PLAYWRIGHT_MCP_ALLOWED_HOSTS")
        else:
            allowed = None
            for entry in env:
                if isinstance(entry, str) and entry.startswith("PLAYWRIGHT_MCP_ALLOWED_HOSTS="):
                    allowed = entry.split("=", 1)[1]
                    break
        self.assertIsNotNone(allowed, "PLAYWRIGHT_MCP_ALLOWED_HOSTS not configured")
        self.assertNotEqual(allowed, "*", "ALLOWED_HOSTS=* disables DNS-rebind protection")
        # Must include loopback hostnames only.
        tokens = {t.strip() for t in allowed.split(",")}
        self.assertTrue(
            tokens.issubset({"localhost", "127.0.0.1", "::1"}),
            f"ALLOWED_HOSTS contains non-loopback tokens: {tokens}",
        )

    # ── Container limits ─────────────────────────────────────────────────

    def test_memory_limit_set(self):
        limits = self.svc.get("deploy", {}).get("resources", {}).get("limits", {})
        self.assertIn("memory", limits)


class TestDockerfileHardening(unittest.TestCase):
    """Dockerfile must not EXPOSE the CDP port."""

    @classmethod
    def setUpClass(cls):
        cls.text = _DOCKERFILE.read_text(encoding="utf-8")

    def test_no_expose_cdp_port(self):
        """EXPOSE directive must not include 9223."""
        for line in self.text.splitlines():
            stripped = line.strip()
            if stripped.startswith("EXPOSE"):
                tokens = stripped.split()[1:]
                self.assertNotIn("9223", tokens, f"CDP port 9223 in EXPOSE: {line}")

    def test_mcp_version_pinned(self):
        """@playwright/mcp must be installed at a pinned version, not @latest."""
        self.assertIn("@playwright/mcp@0.0.70", self.text)
        self.assertNotIn("@playwright/mcp@latest", self.text)


class TestEntrypointCommentParity(unittest.TestCase):
    """Comment in entrypoint must reflect the actual ALLOWED_HOSTS value."""

    @classmethod
    def setUpClass(cls):
        cls.text = _ENTRYPOINT.read_text(encoding="utf-8")

    def test_no_stale_wildcard_allowed_hosts_comment(self):
        """The misleading 'ALLOWED_HOSTS=*' comment must not survive."""
        # Match the exact stale-comment pattern only — avoid false positives
        # from genuinely descriptive prose.
        self.assertNotRegex(
            self.text,
            r"PLAYWRIGHT_MCP_ALLOWED_HOSTS=\*",
            "Stale wildcard ALLOWED_HOSTS comment still present",
        )

    def test_comment_documents_loopback_only(self):
        """Comment block should mention loopback-only ALLOWED_HOSTS."""
        self.assertIn("localhost,127.0.0.1", self.text)


if __name__ == "__main__":
    unittest.main()
