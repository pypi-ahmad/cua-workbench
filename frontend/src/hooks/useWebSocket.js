import { useEffect, useRef, useState, useCallback } from 'react'

const WS_PROTOCOL = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
const WS_BASE = `${WS_PROTOCOL}//${window.location.host}/ws`

async function fetchWsToken() {
  // Short-lived, single-use token; /ws rejects connections without one.
  const res = await fetch('/api/session/ws-token', { method: 'POST' })
  if (!res.ok) throw new Error(`ws-token request failed: ${res.status}`)
  const data = await res.json()
  if (!data.token) throw new Error('ws-token response missing token')
  return data.token
}

export default function useWebSocket() {
  const wsRef = useRef(null)
  const [connected, setConnected] = useState(false)
  const [lastScreenshot, setLastScreenshot] = useState(null)
  const [lastScreenshotFormat, setLastScreenshotFormat] = useState('png')
  const [logs, setLogs] = useState([])
  const [steps, setSteps] = useState([])
  const [agentFinished, setAgentFinished] = useState(null)
  const [safetyPrompt, setSafetyPrompt] = useState(null)
  const [activeSessionId, setActiveSessionId] = useState(null)
  const reconnectTimer = useRef(null)

  const connect = useCallback(async () => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return

    let token
    try {
      token = await fetchWsToken()
    } catch (e) {
      // Backend down; retry after delay.  Do NOT open ws with empty token —
      // the server will reject it and we'd spin in a tight reconnect loop.
      reconnectTimer.current = setTimeout(() => connect(), 2000)
      return
    }

    const ws = new WebSocket(`${WS_BASE}?token=${encodeURIComponent(token)}`)
    wsRef.current = ws

    ws.onopen = () => {
      setConnected(true)
      // Heartbeat
      const ping = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'ping' }))
        }
      }, 15000)
      ws._pingInterval = ping
    }

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data)

        switch (msg.event) {
          case 'screenshot':
          case 'screenshot_stream':
            setLastScreenshot(msg.screenshot)
            setLastScreenshotFormat(msg.format || 'png')
            break
          case 'log':
            setLogs((prev) => [...prev.slice(-200), msg.log])
            break
          case 'step':
            setSteps((prev) => [...prev, msg.step])
            break
          case 'agent_finished':
            setAgentFinished(msg)
            break
          case 'safety_confirmation':
            setSafetyPrompt({ sessionId: msg.session_id, explanation: msg.explanation })
            break
          case 'pong':
            break
          default:
            break
        }
      } catch {
        // ignore parse errors
      }
    }

    ws.onclose = () => {
      setConnected(false)
      clearInterval(ws._pingInterval)
      // Reconnect after 2s
      reconnectTimer.current = setTimeout(connect, 2000)
    }

    ws.onerror = () => {
      ws.close()
    }
  }, [])

  useEffect(() => {
    connect()
    return () => {
      clearTimeout(reconnectTimer.current)
      if (wsRef.current) {
        wsRef.current.close()
      }
    }
  }, [connect])

  const clearLogs = useCallback(() => setLogs([]), [])
  const clearSteps = useCallback(() => setSteps([]), [])
  const clearFinished = useCallback(() => setAgentFinished(null), [])
  const clearSafetyPrompt = useCallback(() => setSafetyPrompt(null), [])

  // Subscribe the WS connection to a specific session_id so the backend
  // only fans out that session's events to this tab (prevents multi-tab
  // cross-talk).  The ambient `screenshot_stream` continues regardless.
  const subscribeSession = useCallback((sessionId) => {
    setActiveSessionId(sessionId)
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN && sessionId) {
      ws.send(JSON.stringify({ type: 'subscribe', session_id: sessionId }))
    }
  }, [])

  const unsubscribeSession = useCallback((sessionId) => {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN && sessionId) {
      ws.send(JSON.stringify({ type: 'unsubscribe', session_id: sessionId }))
    }
    setActiveSessionId((cur) => (cur === sessionId ? null : cur))
  }, [])

  return {
    connected, lastScreenshot, lastScreenshotFormat, logs, steps, agentFinished, safetyPrompt,
    clearLogs, clearSteps, clearFinished, clearSafetyPrompt,
    subscribeSession, unsubscribeSession, activeSessionId,
  }
}
