from __future__ import annotations

import pytest

from backend.config import Config, ConfigError


_ENV_CASES = [
    ("GEMINI_MODEL", "gemini-test", "gemini_model", "gemini-test"),
    ("CONTAINER_NAME", "cua-test-container", "container_name", "cua-test-container"),
    ("AGENT_SERVICE_HOST", "localhost", "agent_service_host", "localhost"),
    ("AGENT_SERVICE_PORT", "9333", "agent_service_port", 9333),
    ("AGENT_MODE", "desktop", "agent_mode", "desktop"),
    ("PLAYWRIGHT_MCP_HOST", "mcp.local", "playwright_mcp_host", "mcp.local"),
    ("PLAYWRIGHT_MCP_PORT", "9010", "playwright_mcp_port", 9010),
    ("PLAYWRIGHT_MCP_PATH", "/rpc", "playwright_mcp_path", "/rpc"),
    ("PLAYWRIGHT_MCP_AUTOSTART", "true", "playwright_mcp_autostart", True),
    ("PLAYWRIGHT_MCP_COMMAND", "node", "playwright_mcp_command", "node"),
    ("PLAYWRIGHT_MCP_ARGS", "server.js --stdio", "playwright_mcp_args", "server.js --stdio"),
    ("PLAYWRIGHT_MCP_DOCKER_TRANSPORT", "stdio", "playwright_mcp_docker_transport", "stdio"),
    ("HOST", "127.0.0.1", "host", "127.0.0.1"),
    ("PORT", "8001", "port", 8001),
    ("SCREEN_WIDTH", "1920", "screen_width", 1920),
    ("SCREEN_HEIGHT", "1080", "screen_height", 1080),
    ("SCREENSHOT_FORMAT", "jpeg", "screenshot_format", "jpeg"),
    ("MAX_STEPS", "75", "max_steps", 75),
    ("ACTION_DELAY_MS", "42", "action_delay_ms", 42),
    ("STEP_TIMEOUT", "12.5", "step_timeout", 12.5),
    ("GEMINI_RETRY_ATTEMPTS", "5", "gemini_retry_attempts", 5),
    ("DEBUG", "1", "debug", True),
    ("VNC_PASSWORD", "secret", "vnc_password", "secret"),
]

_ALL_ENV_NAMES = [env_name for env_name, *_ in _ENV_CASES]


def _clear_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_name in _ALL_ENV_NAMES:
        monkeypatch.delenv(env_name, raising=False)


@pytest.mark.parametrize(("env_name", "raw_value", "attr_name", "expected"), _ENV_CASES)
def test_from_env_loads_documented_env_vars(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    raw_value: str,
    attr_name: str,
    expected: object,
) -> None:
    _clear_config_env(monkeypatch)
    monkeypatch.setenv(env_name, raw_value)

    config = Config.from_env()

    assert getattr(config, attr_name) == expected


def test_from_env_defaults_host_to_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_config_env(monkeypatch)

    config = Config.from_env()

    assert config.host == "127.0.0.1"


@pytest.mark.parametrize(
    "env_name",
    [
        "AGENT_SERVICE_PORT",
        "PLAYWRIGHT_MCP_PORT",
        "SCREEN_WIDTH",
        "SCREEN_HEIGHT",
        "MAX_STEPS",
        "ACTION_DELAY_MS",
        "GEMINI_RETRY_ATTEMPTS",
        "PORT",
    ],
)
def test_from_env_invalid_int_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
) -> None:
    _clear_config_env(monkeypatch)
    monkeypatch.setenv(env_name, "not-an-int")

    with pytest.raises(ConfigError, match=env_name):
        Config.from_env()