/**
 * useContainerStatus — shared hook for polling Docker container + agent service status.
 * B-27: Eliminates duplicated polling between App.jsx and Workbench.
 */
import { useState, useEffect, useCallback } from 'react'
import { getContainerStatus, getHealthDetailed } from '../api'

export default function useContainerStatus() {
  const [containerRunning, setContainerRunning] = useState(false)
  const [agentServiceUp, setAgentServiceUp] = useState(false)
  const [backendReachable, setBackendReachable] = useState(true)
  const [systemHealth, setSystemHealth] = useState(null)

  const refresh = useCallback(async () => {
    try {
      const data = await getContainerStatus()
      setContainerRunning(data.running || false)
      setAgentServiceUp(data.agent_service || false)
      setBackendReachable(true)
    } catch {
      setContainerRunning(false)
      setAgentServiceUp(false)
      setBackendReachable(false)
    }
    // Detailed health (non-blocking, informational)
    try {
      const health = await getHealthDetailed()
      setSystemHealth(health)
    } catch { /* ignore */ }
  }, [])

  useEffect(() => {
    refresh()
    const interval = backendReachable ? 5000 : 15000
    const id = setInterval(refresh, interval)
    return () => clearInterval(id)
  }, [refresh, backendReachable])

  return { containerRunning, agentServiceUp, backendReachable, systemHealth, refreshContainer: refresh }
}
