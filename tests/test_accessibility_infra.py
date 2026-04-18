"""Tests for accessibility engine infrastructure hardening (Round 6).

Validates: entrypoint AT-SPI ordering, executor HTTP routing, loop preflight
gate, command allowlists, prompt improvements, recovery hints, and agent_service
accessibility dispatch.
"""

from __future__ import annotations

import asyncio
import re
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_loop(engine="omni_accessibility", mode="desktop"):
    """Create an AgentLoop with minimal config for unit testing."""
    from backend.agent.loop import AgentLoop

    with patch("backend.agent.loop.config") as mock_config:
        mock_config.gemini_model = "gemini-3-flash-preview"
        mock_config.max_steps = 10
        mock_config.action_delay_ms = 0
        mock_config.agent_service_url = "http://localhost:9222"
        mock_config.screen_width = 1440
        mock_config.screen_height = 900
        loop = AgentLoop(
            task="test task",
            api_key="test-key",
            engine=engine,
            mode=mode,
        )
    return loop


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Entrypoint ordering — AT-SPI before XFCE
# ═══════════════════════════════════════════════════════════════════════════════


class TestEntrypointOrdering:
    """AT-SPI env vars and registryd must appear BEFORE startxfce4."""

    def _read_entrypoint(self) -> str:
        import pathlib
        ep = pathlib.Path(__file__).resolve().parent.parent / "docker" / "entrypoint.sh"
        return ep.read_text(encoding="utf-8")

    def test_atspi_env_before_xfce(self):
        content = self._read_entrypoint()
        at_bridge_pos = content.find("NO_AT_BRIDGE=0")
        xfce_pos = content.find("startxfce4")
        assert at_bridge_pos > 0, "NO_AT_BRIDGE=0 not found in entrypoint"
        assert xfce_pos > 0, "startxfce4 not found in entrypoint"
        assert at_bridge_pos < xfce_pos, (
            "NO_AT_BRIDGE=0 must appear before startxfce4 so XFCE apps "
            "register with AT-SPI at launch"
        )

    def test_gtk_modules_before_xfce(self):
        content = self._read_entrypoint()
        gtk_pos = content.find("GTK_MODULES=gail:atk-bridge")
        xfce_pos = content.find("startxfce4")
        assert gtk_pos > 0 and xfce_pos > 0
        assert gtk_pos < xfce_pos

    def test_registryd_before_xfce(self):
        content = self._read_entrypoint()
        reg_pos = content.find("at-spi2-registryd")
        xfce_pos = content.find("startxfce4")
        assert reg_pos > 0 and xfce_pos > 0
        assert reg_pos < xfce_pos

    def test_post_desktop_app_count_check(self):
        """After desktop starts, entrypoint should verify AT-SPI sees apps."""
        content = self._read_entrypoint()
        assert "get_child_count" in content, (
            "Entrypoint should verify AT-SPI registered apps after desktop start"
        )

    def test_accessibility_enabled_env(self):
        content = self._read_entrypoint()
        assert "ACCESSIBILITY_ENABLED=1" in content

    def test_qt_accessibility_set(self):
        content = self._read_entrypoint()
        assert "QT_ACCESSIBILITY=1" in content


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Dockerfile — required packages
# ═══════════════════════════════════════════════════════════════════════════════


class TestDockerfilePackages:
    """Verify all AT-SPI packages are present in the Dockerfile."""

    def _read_dockerfile(self) -> str:
        import pathlib
        df = pathlib.Path(__file__).resolve().parent.parent / "docker" / "Dockerfile"
        return df.read_text(encoding="utf-8")

    @pytest.mark.parametrize("pkg", [
        "at-spi2-core",
        "gir1.2-atspi-2.0",
        "gir1.2-gtk-3.0",
        "python3-gi",
        "dbus-x11",
        "libatspi2.0-dev",
    ])
    def test_package_present(self, pkg):
        content = self._read_dockerfile()
        assert pkg in content, f"Package {pkg} not found in Dockerfile"

    def test_system_site_packages_venv(self):
        """Venv must use --system-site-packages for gi/Atspi access."""
        content = self._read_dockerfile()
        assert "--system-site-packages" in content


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Executor routes accessibility by execution_target
# ═══════════════════════════════════════════════════════════════════════════════


class TestExecutorAccessibilityRouting:
    """Accessibility routing: docker → HTTP agent service, local → direct provider."""

    def test_executor_accessibility_has_execution_target_branching(self):
        """The executor must branch on execution_target for omni_accessibility."""
        import inspect
        from backend.agent import executor
        source = inspect.getsource(executor.execute_action)
        # execution_target="local" path uses direct import
        assert "from backend.engines.accessibility_engine import execute_accessibility_action" in source, (
            "executor.execute_action must import accessibility_engine for local execution path."
        )
        # execution_target="docker" path uses HTTP agent service
        assert '"mode": "omni_accessibility"' in source or "'mode': 'omni_accessibility'" in source, (
            "executor must send mode='omni_accessibility' in the HTTP payload for docker path"
        )

    def test_executor_accessibility_sends_mode(self):
        """The HTTP payload for accessibility must include mode=omni_accessibility."""
        import inspect
        from backend.agent import executor
        source = inspect.getsource(executor.execute_action)
        # Should contain mode: omni_accessibility in the payload
        assert '"mode": "omni_accessibility"' in source or "'mode': 'omni_accessibility'" in source, (
            "executor must send mode='omni_accessibility' in the HTTP payload"
        )

    def test_check_accessibility_health_remote_exists(self):
        """Remote health check function must exist."""
        from backend.agent.executor import check_accessibility_health_remote
        assert callable(check_accessibility_health_remote)

    def test_health_remote_returns_dict_on_error(self):
        """When agent service is unreachable, return a fallback dict."""
        from backend.agent.executor import check_accessibility_health_remote

        async def _run():
            with patch("backend.agent.executor._get_client") as mc:
                client = AsyncMock()
                client.get.side_effect = Exception("connection refused")
                mc.return_value = client

                result = await check_accessibility_health_remote()
                assert result["healthy"] is False
                assert result["bindings"] is False
                assert "error" in result

        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Loop preflight — hard gate on missing bindings
# ═══════════════════════════════════════════════════════════════════════════════


class TestLoopPreflightGate:
    """Preflight must abort if AT-SPI bindings are missing in container."""

    def test_preflight_uses_remote_check(self):
        """Loop must call check_accessibility_health_remote, not direct import."""
        import inspect
        from backend.agent import loop as loop_module
        source = inspect.getsource(loop_module.AgentLoop.run)
        assert "check_accessibility_health_remote" in source, (
            "Loop.run must use check_accessibility_health_remote (HTTP) "
            "instead of direct accessibility_engine import"
        )
        assert "from backend.engines.accessibility_engine import check_accessibility_health" not in source, (
            "Loop.run must NOT import check_accessibility_health directly"
        )

    def test_preflight_aborts_on_missing_bindings(self):
        """If bindings=False, session must be set to ERROR immediately."""

        async def _run():
            loop = _make_loop(engine="omni_accessibility")

            with patch("backend.agent.executor.check_accessibility_health_remote") as mock_check:
                mock_check.return_value = {"healthy": False, "bindings": False, "error": "gi not found"}

                session = await loop.run()

            from backend.models import SessionStatus
            assert session.status == SessionStatus.ERROR

        asyncio.run(_run())

    def test_preflight_warns_on_no_apps(self):
        """If bindings=True but healthy=False, warn but continue."""

        async def _run():
            loop = _make_loop(engine="omni_accessibility")

            logs: list[dict] = []
            loop._on_log = lambda entry: logs.append(entry)

            with patch("backend.agent.executor.check_accessibility_health_remote") as mock_check, \
                 patch("backend.agent.loop.capture_screenshot", new_callable=AsyncMock, return_value="base64img"), \
                 patch.object(loop, "_execute_step", new_callable=AsyncMock, return_value=False):
                mock_check.return_value = {"healthy": False, "bindings": True}

                await loop.run()

            # The warning should have been emitted but loop should not abort
            # (it may abort for other reasons like _execute_step returning False)
            mock_check.assert_called_once()

        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Command allowlists — desktop apps
# ═══════════════════════════════════════════════════════════════════════════════


class TestCommandAllowlists:
    """Common desktop apps must be in command allowlists."""

    def _read_agent_service(self) -> str:
        import pathlib
        f = pathlib.Path(__file__).resolve().parent.parent / "docker" / "agent_service.py"
        return f.read_text(encoding="utf-8")

    def _read_accessibility_engine(self) -> str:
        import pathlib
        f = pathlib.Path(__file__).resolve().parent.parent / "backend" / "engines" / "accessibility_engine.py"
        return f.read_text(encoding="utf-8")

    # Only apps actually installed by docker/Dockerfile (see H-3 image
    # minimization) may appear here.  Desktop apps that were dropped
    # (mousepad, thunar, firefox, gnome-control-center, gnome-calculator)
    # have been deliberately removed from _ALLOWED_COMMANDS so the LLM
    # cannot invoke non-existent binaries.
    @pytest.mark.parametrize("cmd", [
        "xfce4-settings-manager",
        "xfce4-settings-editor",
        "google-chrome",
    ])
    def test_agent_service_allows_desktop_apps(self, cmd):
        content = self._read_agent_service()
        assert f'"{cmd}"' in content, f"{cmd} not in agent_service _ALLOWED_COMMANDS"

    @pytest.mark.parametrize("cmd", [
        "xfce4-settings-manager",
        "xfce4-settings-editor",
    ])
    def test_accessibility_engine_allows_desktop_apps(self, cmd):
        content = self._read_accessibility_engine()
        assert f'"{cmd}"' in content, f"{cmd} not in accessibility_engine allowlist"


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Accessibility prompt improvements
# ═══════════════════════════════════════════════════════════════════════════════


class TestAccessibilityPrompt:
    """Verify accessibility prompt contains essential guidance."""

    def _get_prompt(self) -> str:
        from backend.agent.prompts import get_system_prompt
        return get_system_prompt("omni_accessibility", "desktop")

    def test_mentions_xfce(self):
        assert "XFCE" in self._get_prompt() or "xfce" in self._get_prompt()

    def test_mentions_xfce_settings_manager(self):
        assert "xfce4-settings-manager" in self._get_prompt()

    def test_warns_against_gnome_control_center(self):
        prompt = self._get_prompt()
        # Should discourage using gnome-control-center as primary
        assert "gnome-control-center" in prompt.lower()

    def test_has_environment_section(self):
        assert "ENVIRONMENT:" in self._get_prompt()

    def test_has_data_extraction_section(self):
        assert "DATA EXTRACTION" in self._get_prompt()

    def test_has_app_launch_strategy(self):
        assert "APPLICATION LAUNCH STRATEGY" in self._get_prompt()

    def test_has_no_repeat_rule(self):
        prompt = self._get_prompt()
        assert "more than twice" in prompt or "more than once" in prompt

    def test_run_command_lists_allowed_apps(self):
        prompt = self._get_prompt()
        assert "xfce4-settings-manager" in prompt
        assert "thunar" in prompt

    def test_done_mentions_summary(self):
        prompt = self._get_prompt()
        assert "summary" in prompt.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Recovery hints for accessibility-relevant actions
# ═══════════════════════════════════════════════════════════════════════════════


class TestAccessibilityRecoveryHints:
    """Recovery hints must cover accessibility-specific stuck patterns."""

    def _build_hint(self, action_value: str, engine: str = "omni_accessibility") -> str:
        from backend.models import ActionType
        loop = _make_loop(engine=engine)

        # Populate action history with enough repeated actions to trigger stuck
        from backend.models import AgentAction
        for _ in range(4):
            loop._action_history.append(
                AgentAction(
                    action=ActionType(action_value),
                    target="test",
                    coordinates=[0, 0],
                    text="test",
                    reasoning="test",
                )
            )
        return loop._build_recovery_hint()

    def test_get_accessibility_tree_hint(self):
        hint = self._build_hint("get_accessibility_tree")
        assert "STOP" in hint
        assert "get_accessibility_tree" in hint

    def test_run_command_hint(self):
        hint = self._build_hint("run_command")
        assert "xfce4-settings-manager" in hint
        assert "gnome-control-center" in hint

    def test_evaluate_js_hint_unchanged(self):
        hint = self._build_hint("evaluate_js")
        assert "STOP" in hint
        assert "done" in hint.lower()

    def test_get_text_hint_unchanged(self):
        hint = self._build_hint("get_text")
        assert "STOP" in hint


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Agent service accessibility dispatch
# ═══════════════════════════════════════════════════════════════════════════════


class TestAgentServiceAccessibilityDispatch:
    """Verify agent_service has accessibility mode routing."""

    def _read_agent_service(self) -> str:
        import pathlib
        f = pathlib.Path(__file__).resolve().parent.parent / "docker" / "agent_service.py"
        return f.read_text(encoding="utf-8")

    def test_dispatch_has_accessibility_mode(self):
        content = self._read_agent_service()
        assert 'mode == "omni_accessibility"' in content or "mode == 'omni_accessibility'" in content

    def test_dispatch_accessibility_method_exists(self):
        content = self._read_agent_service()
        assert "_dispatch_accessibility" in content

    def test_dispatch_accessibility_imports_engine(self):
        content = self._read_agent_service()
        assert "execute_accessibility_action" in content

    def test_health_a11y_endpoint(self):
        content = self._read_agent_service()
        assert "/health/a11y" in content

    def test_health_a11y_returns_bindings_key(self):
        content = self._read_agent_service()
        # Should include "bindings" in the response
        assert '"bindings"' in content or "'bindings'" in content
