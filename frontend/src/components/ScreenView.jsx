import { useState, useEffect, useRef, useCallback } from 'react'

// Derive the backend API base from the current page location (protocol-safe)
const API_BASE = window.location.origin

export default function ScreenView({ screenshot, containerRunning }) {
  // Default to VNC (interactive) when container is running, as requested by audit
  const [useVnc, setUseVnc] = useState(true)
  const [videoStreamActive, setVideoStreamActive] = useState(false)
  const videoRef = useRef(null)
  const pcRef = useRef(null)
  const retryTimerRef = useRef(null)

  const cleanup = useCallback(() => {
    if (retryTimerRef.current) {
      clearTimeout(retryTimerRef.current)
      retryTimerRef.current = null
    }
    if (pcRef.current) {
      pcRef.current.close()
      pcRef.current = null
    }
    setVideoStreamActive(false)
  }, [])

  // WebRTC initialization
  useEffect(() => {
    if (!containerRunning || useVnc) {
      cleanup()
      return
    }

    let cancelled = false

    const startWebRTC = async () => {
      cleanup()

      const pc = new RTCPeerConnection()
      pcRef.current = pc

      pc.addTransceiver('video', { direction: 'recvonly' })

      pc.ontrack = (event) => {
        if (cancelled) return
        console.log('WebRTC track received')
        if (videoRef.current) {
          videoRef.current.srcObject = event.streams[0]
          setVideoStreamActive(true)
        }
      }

      pc.onconnectionstatechange = () => {
        if (cancelled) return
        const state = pc.connectionState
        console.log('WebRTC state:', state)
        if (state === 'failed' || state === 'disconnected') {
          setVideoStreamActive(false)
          // Auto-reconnect after 3 seconds
          retryTimerRef.current = setTimeout(() => {
            if (!cancelled) startWebRTC()
          }, 3000)
        }
        if (state === 'closed') {
          setVideoStreamActive(false)
        }
      }

      try {
        const offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        const response = await fetch(`${API_BASE}/webrtc/offer`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            sdp: pc.localDescription.sdp,
            type: pc.localDescription.type,
          }),
        })

        if (!response.ok) throw new Error(`WebRTC offer failed: ${response.status}`)

        const answer = await response.json()
        if (answer.error) throw new Error(answer.error)

        if (!cancelled && pcRef.current === pc) {
          await pc.setRemoteDescription(answer)
        }
      } catch (err) {
        console.warn('WebRTC error:', err.message)
        if (!cancelled) {
          setVideoStreamActive(false)
          // Retry after 5 seconds on initial connection failure
          retryTimerRef.current = setTimeout(() => {
            if (!cancelled) startWebRTC()
          }, 5000)
        }
      }
    }

    // Small delay to let the container fully start
    const initTimer = setTimeout(startWebRTC, 1000)

    return () => {
      cancelled = true
      clearTimeout(initTimer)
      cleanup()
    }
  }, [containerRunning, useVnc, cleanup])

  // Route noVNC through the backend reverse proxy (same origin) so the
  // browser never needs direct access to Docker-mapped port 6080.
  const vncUrl = `/vnc/vnc.html?autoconnect=true&resize=scale&path=vnc/websockify`

  // When container is running and VNC mode enabled, show interactive desktop
  if (containerRunning && useVnc) {
    return (
      <div className="screen-container" style={{ position: 'relative' }}>
        <iframe
          src={vncUrl}
          title="Live Desktop (noVNC)"
          style={{ width: '100%', height: '100%', border: 'none' }}
          allow="clipboard-read; clipboard-write"
          onError={() => {
            console.warn("VNC iframe failed to load, falling back to video")
            setUseVnc(false)
          }}
        />
        <div className="screen-overlay">
          <span className="screen-badge">Interactive</span>
          <button
            onClick={() => setUseVnc(false)}
            style={{
              marginLeft: 8, padding: '2px 8px', fontSize: 11,
              background: 'rgba(0,0,0,0.5)', color: '#fff', border: '1px solid rgba(255,255,255,0.3)',
              borderRadius: 4, cursor: 'pointer',
            }}
          >
            Video View
          </button>
        </div>
      </div>
    )
  }

  // Combined Video + Screenshot Fallback View
  return (
    <div className="screen-container" style={{ position: 'relative' }}>
        {/* Video Layer */}
        {containerRunning && (
            <video
            ref={videoRef}
            autoPlay
            playsInline
            muted
            style={{
                width: '100%', height: '100%', objectFit: 'contain',
                display: videoStreamActive ? 'block' : 'none',
            }}
            />
        )}
        
        {/* Screenshot Layer (Fallback) - Only show if video is NOT active */}
        {(!videoStreamActive && screenshot) && (
            <img
            src={`data:image/png;base64,${screenshot}`}
            alt="Agent screen"
            draggable={false}
            style={{
                width: '100%', height: '100%', objectFit: 'contain',
                display: 'block'
            }}
            />
        )}
        
        {/* Empty State */}
        {(!videoStreamActive && !screenshot) && (
            <div className="screen-placeholder">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                <rect x="2" y="3" width="20" height="14" rx="2" />
                <path d="M8 21h8M12 17v4" />
            </svg>
            <span>No screen capture available</span>
            <span style={{ fontSize: 12 }}>Start the container to see the live view</span>
            </div>
        )}

        {/* Overlay */}
        {(screenshot || videoStreamActive) && (
            <div className="screen-overlay">
                <span className="screen-badge">
                    {videoStreamActive ? 'Live Video' : 'Screenshot'}
                </span>
                {containerRunning && (
                    <button
                        onClick={() => setUseVnc(true)}
                        style={{
                            marginLeft: 8, padding: '2px 8px', fontSize: 11,
                            background: 'rgba(0,0,0,0.5)', color: '#fff', border: '1px solid rgba(255,255,255,0.3)',
                            borderRadius: 4, cursor: 'pointer',
                        }}
                    >
                        Interactive View
                    </button>
                )}
            </div>
        )}
    </div>
  )
}
