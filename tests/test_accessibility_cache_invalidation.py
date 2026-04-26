from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.engines.accessibility_engine import (
    LinuxATSPIProvider,
    MacAccessibilityProvider,
    WindowsUIAProvider,
)


@pytest.mark.parametrize(
    ("provider_cls", "method_name", "args", "success_setup"),
    [
        (LinuxATSPIProvider, "click_at", (10, 20), lambda provider, monkeypatch: monkeypatch.setattr(
            "backend.engines.accessibility_engine.subprocess.run",
            MagicMock(return_value=SimpleNamespace(stdout="", returncode=0, stderr="")),
        )),
        (LinuxATSPIProvider, "type_text_phys", ("hello",), lambda provider, monkeypatch: monkeypatch.setattr(
            "backend.engines.accessibility_engine.subprocess.run",
            MagicMock(return_value=SimpleNamespace(stdout="", returncode=0, stderr="")),
        )),
        (LinuxATSPIProvider, "press_key", ("ctrl+a",), lambda provider, monkeypatch: monkeypatch.setattr(
            "backend.engines.accessibility_engine.subprocess.run",
            MagicMock(return_value=SimpleNamespace(stdout="", returncode=0, stderr="")),
        )),
        (LinuxATSPIProvider, "activate_window", ("Terminal",), lambda provider, monkeypatch: monkeypatch.setattr(
            "backend.engines.accessibility_engine.subprocess.run",
            MagicMock(side_effect=[
                SimpleNamespace(stdout="123\n", returncode=0, stderr=""),
                SimpleNamespace(stdout="", returncode=0, stderr=""),
            ]),
        )),
        (WindowsUIAProvider, "click_at", (10, 20), lambda provider, monkeypatch: monkeypatch.setattr(
            provider, "_run_ps", MagicMock(return_value="ok")
        )),
        (WindowsUIAProvider, "type_text_phys", ("hello",), lambda provider, monkeypatch: monkeypatch.setattr(
            provider, "_run_ps", MagicMock(return_value="ok")
        )),
        (WindowsUIAProvider, "press_key", ("ctrl+a",), lambda provider, monkeypatch: monkeypatch.setattr(
            provider, "_run_ps", MagicMock(return_value="ok")
        )),
        (WindowsUIAProvider, "activate_window", ("Notepad",), lambda provider, monkeypatch: monkeypatch.setattr(
            provider, "_run_ps", MagicMock(return_value="ok")
        )),
        (MacAccessibilityProvider, "click_at", (10, 20), lambda provider, monkeypatch: monkeypatch.setattr(
            "backend.engines.accessibility_engine.subprocess.run",
            MagicMock(return_value=SimpleNamespace(stdout="", returncode=0, stderr="")),
        )),
        (MacAccessibilityProvider, "type_text_phys", ("hello",), lambda provider, monkeypatch: monkeypatch.setattr(
            "backend.engines.accessibility_engine.subprocess.run",
            MagicMock(return_value=SimpleNamespace(stdout="", returncode=0, stderr="")),
        )),
        (MacAccessibilityProvider, "press_key", ("cmd+a",), lambda provider, monkeypatch: monkeypatch.setattr(
            "backend.engines.accessibility_engine.subprocess.run",
            MagicMock(return_value=SimpleNamespace(stdout="", returncode=0, stderr="")),
        )),
        (MacAccessibilityProvider, "activate_window", ("TextEdit",), lambda provider, monkeypatch: monkeypatch.setattr(
            "backend.engines.accessibility_engine.subprocess.run",
            MagicMock(return_value=SimpleNamespace(stdout="", returncode=0, stderr="")),
        )),
    ],
)
def test_mutating_provider_actions_invalidate_caches_on_success(
    provider_cls,
    method_name,
    args,
    success_setup,
    monkeypatch,
):
    provider = provider_cls()
    invalidate = MagicMock()
    monkeypatch.setattr(provider, "invalidate_caches", invalidate)
    success_setup(provider, monkeypatch)

    assert getattr(provider, method_name)(*args) is True
    invalidate.assert_called_once()


@pytest.mark.parametrize(
    ("provider_cls", "method_name", "args", "failure_setup"),
    [
        (LinuxATSPIProvider, "click_at", (10, 20), lambda provider, monkeypatch: monkeypatch.setattr(
            "backend.engines.accessibility_engine.subprocess.run",
            MagicMock(side_effect=subprocess.CalledProcessError(1, ["xdotool"])),
        )),
        (WindowsUIAProvider, "click_at", (10, 20), lambda provider, monkeypatch: monkeypatch.setattr(
            provider, "_run_ps", MagicMock(side_effect=RuntimeError("boom"))
        )),
        (MacAccessibilityProvider, "click_at", (10, 20), lambda provider, monkeypatch: monkeypatch.setattr(
            "backend.engines.accessibility_engine.subprocess.run",
            MagicMock(side_effect=subprocess.CalledProcessError(1, ["osascript"])),
        )),
    ],
)
def test_mutating_provider_actions_skip_cache_invalidation_on_failure(
    provider_cls,
    method_name,
    args,
    failure_setup,
    monkeypatch,
):
    provider = provider_cls()
    invalidate = MagicMock()
    monkeypatch.setattr(provider, "invalidate_caches", invalidate)
    failure_setup(provider, monkeypatch)

    assert getattr(provider, method_name)(*args) is False
    invalidate.assert_not_called()