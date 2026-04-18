import { useState, useEffect } from 'react'

export default function ScreenView({ screenshot, screenshotFormat = 'png', containerRunning, agentServiceUp }) {
  // Default to VNC (interactive) when container is running
  const [useVnc, setUseVnc] = useState(true)
  const [vncUrl, setVncUrl] = useState(null)

  // Route noVNC through the backend reverse proxy (same origin).  The
  // ws-token must be appended to ``path=`` so noVNC's internal WebSocket
  // passes the ``/ws`` auth gate.  Tokens are 30-second single-use, so
  // we re-fetch each time the iframe is (re)mounted.
  useEffect(() => {
    if (!containerRunning || !useVnc) return
    let cancelled = false
    ;(async () => {
      try {
        const res = await fetch('/api/session/ws-token', { method: 'POST' })
        if (!res.ok) throw new Error(`ws-token ${res.status}`)
        const data = await res.json()
        if (cancelled || !data.token) return
        const path = `vnc/websockify?token=${encodeURIComponent(data.token)}`
        setVncUrl(`/vnc/vnc.html?autoconnect=true&resize=scale&path=${encodeURIComponent(path)}`)
      } catch {
        // Fall back to screenshot view on token failure
        setUseVnc(false)
      }
    })()
    return () => { cancelled = true }
  }, [containerRunning, useVnc])

  // Loading state: container running but agent service not yet ready
  if (containerRunning && !agentServiceUp && !screenshot) {
    return (
      <div className="screen-container" style={{ position: 'relative' }}>
        <div className="screen-placeholder">
          <div style={{ width: 32, height: 32, border: '3px solid var(--border)', borderTop: '3px solid var(--accent)', borderRadius: '50%', animation: 'spin 1s linear infinite' }} />
          <span>Waiting for agent service to start…</span>
          <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>The container is running. Services are initializing.</span>
        </div>
      </div>
    )
  }

  // When container is running and VNC mode enabled, show interactive desktop
  if (containerRunning && useVnc && vncUrl) {
    return (
      <div className="screen-container" style={{ position: 'relative' }}>
        <iframe
          src={vncUrl}
          title="Live Desktop (noVNC)"
          style={{ width: '100%', height: '100%', border: 'none' }}
          sandbox="allow-scripts allow-same-origin allow-forms"
          allow="clipboard-read; clipboard-write"
          onError={() => {
            console.warn("VNC iframe failed to load, falling back to screenshot")
            setUseVnc(false)
          }}
        />
        <div className="screen-overlay">
          <span className="screen-badge" aria-label="Interactive VNC mode active">Interactive</span>
        </div>
      </div>
    )
  }

  // Screenshot fallback view
  return (
    <div className="screen-container" style={{ position: 'relative' }}>
        {/* Screenshot layer */}
        {screenshot && (
            <img
            src={`data:image/${screenshotFormat};base64,${screenshot}`}
            alt="Agent screen capture"
            draggable={false}
            style={{
                width: '100%', height: '100%', objectFit: 'contain',
                display: 'block'
            }}
            />
        )}

        {/* Empty state */}
        {!screenshot && (
            <div className="screen-placeholder">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
                <rect x="2" y="3" width="20" height="14" rx="2" />
                <path d="M8 21h8M12 17v4" />
            </svg>
            <span>No screen capture available</span>
            <span style={{ fontSize: 12 }}>Start the container to see the live view</span>
            </div>
        )}

        {/* Overlay */}
        {screenshot && (
            <div className="screen-overlay">
                <span className="screen-badge">Screenshot</span>
                {containerRunning && (
                    <button
                        onClick={() => setUseVnc(true)}
                        aria-label="Switch to interactive VNC view"
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
