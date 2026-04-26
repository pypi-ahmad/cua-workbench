"""Docker container lifecycle management."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile

from backend.config import config
from backend.utils import agent_auth

logger = logging.getLogger(__name__)

# Only allow safe characters in container/image names
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:/-]*$")

# Track tempfiles used for secret bind mounts so stop_container can shred them.
_tracked_secret_files: set[str] = set()

# Per-container path of the host-side copy of the agent_service bearer token.
# Populated by ``start_container`` after a successful ``docker cp``.
_agent_service_token_files: dict[str, str] = {}


def _validate_name(name: str, label: str = "name") -> None:
    """Reject names containing shell metacharacters."""
    if not name or len(name) > 128 or not _SAFE_NAME_RE.match(name):
        raise ValueError(f"Invalid {label}: {name!r}")


async def _run(args: list[str]) -> tuple[int, str, str]:
    """Run a command as an explicit argument list (no shell interpretation)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout.decode(), stderr.decode()


async def build_image() -> bool:
    """Build the CUA Docker image from docker/Dockerfile."""
    _validate_name(config.container_image, "container_image")
    logger.info("Building Docker image: %s", config.container_image)
    rc, out, err = await _run(
        ["docker", "build", "-t", config.container_image, "-f", "docker/Dockerfile", "."]
    )
    if rc != 0:
        logger.error("Docker build failed: %s", err)
        return False
    logger.info("Docker image built successfully")
    return True


async def is_container_running(name: str | None = None) -> bool:
    """Return True if the named container is currently running."""
    container = name or config.container_name
    _validate_name(container, "container_name")
    rc, out, _ = await _run(
        ["docker", "ps", "--filter", f"name=^/{container}$", "--format", "{{.Names}}"]
    )
    return container in out


async def start_container(name: str | None = None) -> bool:
    """Start the CUA Docker container with Xvfb + agent service."""
    container = name or config.container_name
    _validate_name(container, "container_name")
    _validate_name(config.container_image, "container_image")

    # Check if container is running
    if await is_container_running(container):
        logger.info("Container %s is already running", container)
        return True

    # Check if container exists but is stopped
    rc, _, _ = await _run(["docker", "inspect", container])
    if rc == 0:
        logger.info("Container %s exists but is stopped. Starting...", container)
        rc, _, err = await _run(["docker", "start", container])
        if rc != 0:
            logger.error("Failed to start existing container: %s", err)
            # Try to remove and recreate if start fails
            await _run(["docker", "rm", "-f", container])
        else:
            # Container started successfully, skip creation
            logger.info("Existing container started")
            return await _wait_for_service(container)

    # Remove any stopped container with the same name (if inspect failed or start failed)
    await _run(["docker", "rm", "-f", container])

    args = [
        "docker", "run", "-d",
        "--name", container,
        "-e", "DISPLAY=:99",
        "-e", f"SCREEN_WIDTH={config.screen_width}",
        "-e", f"SCREEN_HEIGHT={config.screen_height}",
        "-e", f"AGENT_SERVICE_PORT={config.agent_service_port}",
        "-p", "127.0.0.1:5900:5900",
        "-p", "127.0.0.1:6080:6080",
        "-p", f"127.0.0.1:{config.playwright_mcp_port}:{config.playwright_mcp_port}",
        "-p", f"127.0.0.1:{config.agent_service_port}:{config.agent_service_port}",
        "--shm-size=2g",
    ]

    # B-31: Pass VNC password into the container as a bind-mounted file.
    # Previously this was injected via -e VNC_PASSWORD=..., which leaks to
    # ``docker inspect``, /proc/<pid>/environ, and orchestration logs.
    # Instead, write the secret to a 0600 temp file on the host and bind it
    # read-only at /run/secrets/vnc_password; the entrypoint reads it from
    # that path and then shreds the source file.
    vnc_secret_path: str | None = None
    if config.vnc_password:
        fd, vnc_secret_path = tempfile.mkstemp(prefix="cua-vnc-", suffix=".secret")
        try:
            os.write(fd, config.vnc_password.encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(vnc_secret_path, 0o600)
        _tracked_secret_files.add(vnc_secret_path)
        args.extend([
            "-v", f"{vnc_secret_path}:/run/secrets/vnc_password:ro",
            "-e", "VNC_PASSWORD_FILE=/run/secrets/vnc_password",
        ])

    args.append(config.container_image)
    logger.info("Starting container: %s", container)
    rc, out, err = await _run(args)

    if rc != 0:
        logger.error("Failed to start container: %s", err)
        return False

    return await _wait_for_service(container)


async def _wait_for_service(container: str) -> bool:
    """Wait for the agent service to become ready."""
    logger.info("Waiting for container environment...")
    for attempt in range(10):
        await asyncio.sleep(2)
        # Check if agent service is responding
        try:
            import httpx
            async with httpx.AsyncClient(timeout=3.0) as client:
                # /health is intentionally unauthenticated (I-002).
                resp = await client.get(f"{config.agent_service_url}/health")
                if resp.status_code == 200:
                    logger.info("Container %s is ready (agent service up)", container)
                    if not await _extract_agent_service_token(container):
                        logger.error(
                            "agent_service token extraction failed for %s — "
                            "host-side calls will be rejected by the bearer-"
                            "token check (see I-002).", container,
                        )
                        return False
                    return True
        except Exception:
            pass
        logger.debug("Waiting for agent service... (attempt %d)", attempt + 1)

    # Even if agent service isn't responding, container may still be usable
    if await is_container_running(container):
        logger.warning("Container running but agent service not confirmed healthy")
        return True

    logger.error("Container failed to become ready")
    return False


async def _extract_agent_service_token(container: str) -> bool:
    """Copy ``/run/secrets/agent_service_token`` out to a host tempfile.

    The token is generated inside the container by ``entrypoint.sh``
    (see I-002).  We ``docker cp`` it to a 0600 tempfile on the host
    and register the path with ``agent_auth`` so every host-side
    httpx caller can attach ``Authorization: Bearer <token>``.

    Tracks the path in ``_tracked_secret_files`` so the existing
    cleanup path in ``stop_container`` shreds the plaintext after the
    container is gone.
    """
    fd, host_path = tempfile.mkstemp(prefix="cua-agent-token-", suffix=".secret")
    os.close(fd)
    rc, _, err = await _run([
        "docker", "cp",
        f"{container}:/run/secrets/agent_service_token",
        host_path,
    ])
    if rc != 0:
        logger.error("docker cp of agent_service token failed: %s", err.strip())
        try:
            os.unlink(host_path)
        except OSError:
            pass
        return False
    try:
        os.chmod(host_path, 0o600)
    except OSError as oe:
        logger.warning("chmod 0600 on agent token tempfile failed: %s", oe)
    _tracked_secret_files.add(host_path)
    _agent_service_token_files[container] = host_path
    try:
        agent_auth.set_token_path(host_path)
    except OSError as oe:
        logger.error("agent_auth.set_token_path(%s) failed: %s", host_path, oe)
        return False
    logger.info("agent_service bearer token registered (%s)", host_path)
    return True


async def stop_container(name: str | None = None) -> bool:
    """Force-remove the CUA Docker container + shred any bound secrets."""
    container = name or config.container_name
    _validate_name(container, "container_name")
    logger.info("Stopping container: %s", container)
    rc, _, err = await _run(["docker", "rm", "-f", container])
    success = rc == 0
    if not success:
        logger.error("Failed to stop container: %s", err)

    # Shred tracked secret tempfiles regardless of docker rm success so we
    # don't leave plaintext credentials on disk between runs.
    for path in list(_tracked_secret_files):
        try:
            if os.path.exists(path):
                os.unlink(path)
        except OSError as oe:
            logger.warning("Failed to remove secret tempfile %s: %s", path, oe)
        _tracked_secret_files.discard(path)

    _agent_service_token_files.pop(container, None)
    agent_auth.clear_token()

    return success


async def get_container_status(name: str | None = None) -> dict:
    """Return a dict with container running state and service health."""
    container = name or config.container_name
    running = await is_container_running(container)

    # Check agent service health if running
    service_healthy = False
    if running:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{config.agent_service_url}/health")
                service_healthy = resp.status_code == 200
        except Exception:
            pass

    return {
        "name": container,
        "running": running,
        "image": config.container_image,
        "agent_service": service_healthy,
    }
