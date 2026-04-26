import { useEffect, useRef, useState, useCallback } from 'react'
import { issueWsToken } from '../api'

const WS_PROTOCOL = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
const WS_BASE = `${WS_PROTOCOL}//${window.location.host}/ws`

export default function useWebSocket() {
  const wsRef = useRef(null)
  const [connected, setConnected] = useState(true)
  const [lastScreenshot, setLastScreenshot] = useState(null)
  const [lastScreenshotFormat, setLastScreenshotFormat] = useState('png')
  const [logs, setLogs] = useState([])
  const [steps, setSteps] = useState([])
  const [agentFinished, setAgentFinished] = useState(null)
  const [safetyPrompt, setSafetyPrompt] = useState(null)
  const [activeSessionId, setActiveSessionId] = useState(null)
  const reconnectTimer = useRef(null)
  const activeSessionIdRef = useRef(null)

  const connect = useCallback(async (sessionId) => {
    if (!sessionId) return
    if (wsRef.current && (wsRef.current.readyState === WebSocket.OPEN || wsRef.current.readyState === WebSocket.CONNECTING)) {
      return
    }

    let token
    try {
      const data = await issueWsToken(sessionId)
      token = data.token
      if (!token) throw new Error('ws-token response missing token')
    } catch (e) {
      if (activeSessionIdRef.current !== sessionId) return
      // The session is live but the per-session token mint failed. Retry after
      // delay with a fresh token request instead of reusing a stale credential.
      setConnected(false)
      reconnectTimer.current = setTimeout(() => connect(sessionId), 2000)
      return
    }

    if (activeSessionIdRef.current !== sessionId) return

    const ws = new WebSocket(`${WS_BASE}?token=${encodeURIComponent(token)}`)
    ws._sessionId = sessionId
    wsRef.current = ws

    ws.onopen = () => {
      if (activeSessionIdRef.current !== sessionId) {
        ws.close()
        return
      }
      setConnected(true)
      ws.send(JSON.stringify({ type: 'subscribe', session_id: sessionId }))
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
      clearInterval(ws._pingInterval)
      if (wsRef.current === ws) {
        wsRef.current = null
      }
      const nextSessionId = activeSessionIdRef.current
      if (nextSessionId) {
        setConnected(false)
        reconnectTimer.current = setTimeout(() => connect(nextSessionId), 2000)
      } else {
        setConnected(true)
      }
    }

    ws.onerror = () => {
      ws.close()
    }
  }, [])

  useEffect(() => {
    activeSessionIdRef.current = activeSessionId
  }, [activeSessionId])

  useEffect(() => {
    clearTimeout(reconnectTimer.current)

    if (!activeSessionId) {
      const ws = wsRef.current
      if (ws) {
        wsRef.current = null
        ws.close()
      }
      setConnected(true)
      return
    }

    const ws = wsRef.current
    if (ws && ws.readyState !== WebSocket.CLOSED) {
      if (ws._sessionId === activeSessionId) {
        return
      }
      wsRef.current = null
      ws.close()
      return
    }

    setConnected(false)
    connect(activeSessionId)
  }, [activeSessionId, connect])

  useEffect(() => {
    return () => {
      clearTimeout(reconnectTimer.current)
      if (wsRef.current) {
        wsRef.current.close()
      }
    }
  }, [])

  const clearLogs = useCallback(() => setLogs([]), [])
  const clearSteps = useCallback(() => setSteps([]), [])
  const clearFinished = useCallback(() => setAgentFinished(null), [])
  const clearSafetyPrompt = useCallback(() => setSafetyPrompt(null), [])

  // Subscribe the WS connection to a specific session_id so the backend
  // only fans out that session's events to this tab (prevents multi-tab
  // cross-talk). Tokens are bound to exactly one session, so a new session
  // means a fresh token and, if needed, a fresh underlying connection.
  const subscribeSession = useCallback((sessionId) => {
    if (sessionId) {
      setActiveSessionId(sessionId)
    }
  }, [])

  const unsubscribeSession = useCallback((sessionId) => {
    setActiveSessionId((cur) => (cur === sessionId ? null : cur))
  }, [])

  return {
    connected, lastScreenshot, lastScreenshotFormat, logs, steps, agentFinished, safetyPrompt,
    clearLogs, clearSteps, clearFinished, clearSafetyPrompt,
    subscribeSession, unsubscribeSession, activeSessionId,
  }
}
