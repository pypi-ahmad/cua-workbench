import asyncio
import logging

try:
    import av
    import numpy as np
    from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
    _WEBRTC_AVAILABLE = True
except ImportError:
    _WEBRTC_AVAILABLE = False
    av = None  # type: ignore[assignment]
    RTCPeerConnection = None  # type: ignore[assignment,misc]
    RTCSessionDescription = None  # type: ignore[assignment,misc]
    VideoStreamTrack = object  # type: ignore[assignment,misc]

from backend.config import config

logger = logging.getLogger(__name__)

# Maximum concurrent WebRTC connections (local-only, single viewer)
_MAX_CONNECTIONS = 2

if _WEBRTC_AVAILABLE:
    import numpy as np
    from backend.streaming.video_capture import VideoCapture

    # Reusable black frame for error/end-of-stream fallback
    _BLACK_FRAME = av.VideoFrame.from_ndarray(
        np.zeros((config.screen_height, config.screen_width, 3), dtype=np.uint8),
        format="bgr24",
    )

    class X11StreamTrack(VideoStreamTrack):
        """Video stream track that yields frames captured from the X11 display."""

        kind = "video"

        def __init__(self):
            """Set up internal capture generator state."""
            super().__init__()
            self._capture = VideoCapture()
            self._generator = None

        async def recv(self):
            """Return the next video frame to the WebRTC peer."""
            if self._generator is None:
                self._generator = self._capture.start()

            try:
                frame = await self._generator.__anext__()
            except (StopAsyncIteration, Exception) as e:
                logger.warning("Stream ended or errored: %s", e)
                self.stop()
                frame = _BLACK_FRAME

            pts, time_base = await self.next_timestamp()
            frame.pts = pts
            frame.time_base = time_base
            return frame

        def stop(self):
            """Stop the underlying video capture and the track itself."""
            if self._capture:
                self._capture.stop()
            super().stop()


class WebRTCManager:
    """Manages WebRTC peer connections for screen streaming.

    When aiortc/av are not installed, all methods degrade gracefully.
    """

    def __init__(self):
        """Initialise the set of active peer connections."""
        self.pcs: set = set()

    async def handle_offer(self, sdp: str, sdp_type: str) -> dict:
        """Accept an SDP offer and return an SDP answer with the video track."""
        if not _WEBRTC_AVAILABLE:
            raise RuntimeError(
                "WebRTC is not available — install aiortc and av: "
                "pip install aiortc av"
            )

        # Enforce connection limit
        active = {pc for pc in self.pcs if pc.connectionState not in ("failed", "closed")}
        self.pcs = active
        if len(active) >= _MAX_CONNECTIONS:
            raise RuntimeError(f"Connection limit reached ({_MAX_CONNECTIONS})")

        offer = RTCSessionDescription(sdp=sdp, type=sdp_type)

        pc = RTCPeerConnection()
        self.pcs.add(pc)

        @pc.on("connectionstatechange")
        async def on_state_change():
            logger.info("WebRTC connection state: %s", pc.connectionState)
            if pc.connectionState in ("failed", "closed"):
                await pc.close()
                self.pcs.discard(pc)

        video_track = X11StreamTrack()
        pc.addTrack(video_track)

        await pc.setRemoteDescription(offer)

        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        return {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
        }

    async def cleanup(self):
        """Close all peer connections."""
        coros = [pc.close() for pc in self.pcs]
        await asyncio.gather(*coros, return_exceptions=True)
        self.pcs.clear()
        logger.info("WebRTC manager: all connections closed")


# Singleton
manager = WebRTCManager()
