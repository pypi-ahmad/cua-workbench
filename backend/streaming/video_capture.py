
import asyncio
import logging
import time
from typing import AsyncGenerator

try:
    import av
except ImportError:
    av = None  # WebRTC video capture unavailable

import numpy as np

from backend.config import config

logger = logging.getLogger(__name__)

# Target frame interval for 30 FPS
_FRAME_INTERVAL = 1.0 / 30


class VideoCapture:
    """Captures raw video frames from the Docker container using ffmpeg."""

    def __init__(self):
        """Initialise capture dimensions from global config."""
        self.process: asyncio.subprocess.Process | None = None
        self._stop_event = asyncio.Event()
        self.width = config.screen_width
        self.height = config.screen_height
        self.channels = 3  # BGR
        self.frame_size = self.width * self.height * self.channels

    async def start(self) -> AsyncGenerator:
        """Start capturing frames and yield them as av.VideoFrame objects.

        Requires the ``av`` package — raises ``RuntimeError`` if not installed.
        """
        if av is None:
            raise RuntimeError(
                "Video capture requires PyAV — install with: pip install av"
            )

        container_name = config.container_name

        # Run ffmpeg inside the container, piping raw BGR24 frames to stdout.
        # -nostdin: prevent ffmpeg from reading stdin
        # -draw_mouse 1: include cursor in capture
        cmd = [
            "docker", "exec", "-i", container_name,
            "ffmpeg", "-nostdin",
            "-f", "x11grab", "-draw_mouse", "1",
            "-framerate", "30",
            "-video_size", f"{self.width}x{self.height}",
            "-i", ":99",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "pipe:1",
        ]

        logger.info("Starting video capture: %s", " ".join(cmd))

        try:
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=self.frame_size * 4,  # ~4 frames buffer
            )

            while not self._stop_event.is_set():
                if self.process.stdout is None or self.process.stdout.at_eof():
                    break

                try:
                    data = await asyncio.wait_for(
                        self.process.stdout.readexactly(self.frame_size),
                        timeout=5.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Frame read timeout — ffmpeg may be stalled")
                    break
                except asyncio.IncompleteReadError:
                    logger.warning("Incomplete frame read (EOF)")
                    break

                frame_array = np.frombuffer(data, dtype=np.uint8).reshape(
                    (self.height, self.width, self.channels)
                )
                frame = av.VideoFrame.from_ndarray(frame_array, format="bgr24")
                yield frame

        except FileNotFoundError:
            logger.error("docker command not found — is Docker installed and in PATH?")
        except Exception as e:
            logger.error("Video capture error: %s", e)
        finally:
            self.stop()

    def stop(self):
        """Stop the capture process."""
        self._stop_event.set()
        if self.process and self.process.returncode is None:
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
        self.process = None
        logger.info("Video capture stopped")
