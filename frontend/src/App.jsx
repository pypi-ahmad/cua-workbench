import { useState, useEffect, useCallback } from 'react'
import useWebSocket from './hooks/useWebSocket'
import ControlPanel from './components/ControlPanel'
import ScreenView from './components/ScreenView'
import LogPanel from './components/LogPanel'
import Header from './components/Header'
import { getContainerStatus } from './api'

export default function App() {
  const { connected, lastScreenshot, logs, steps, agentFinished, clearLogs, clearSteps, clearFinished } = useWebSocket()

  const [containerRunning, setContainerRunning] = useState(false)
  const [agentServiceUp, setAgentServiceUp] = useState(false)
  const [agentRunning, setAgentRunning] = useState(false)
  const [sessionId, setSessionId] = useState(null)

  const [backendReachable, setBackendReachable] = useState(true)
  const pollIntervalRef = useCallback(() => backendReachable ? 5000 : 15000, [backendReachable])

  // Poll container status (backs off when backend is unreachable)
  const refreshContainerStatus = useCallback(async () => {
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
  }, [])

  useEffect(() => {
    refreshContainerStatus()
    const id = setInterval(refreshContainerStatus, pollIntervalRef())
    return () => clearInterval(id)
  }, [refreshContainerStatus, pollIntervalRef])

  return (
    <div className="app">
      <Header
        connected={connected}
        containerRunning={containerRunning}
        agentServiceUp={agentServiceUp}
        agentRunning={agentRunning}
        onRefreshContainer={refreshContainerStatus}
      />
      <div className="main-content">
        <ControlPanel
          containerRunning={containerRunning}
          agentRunning={agentRunning}
          setAgentRunning={setAgentRunning}
          sessionId={sessionId}
          setSessionId={setSessionId}
          steps={steps}
          clearSteps={clearSteps}
          onRefreshContainer={refreshContainerStatus}
          agentFinished={agentFinished}
          clearFinished={clearFinished}
        />
        <div className="right-panel">
          <ScreenView screenshot={lastScreenshot} containerRunning={containerRunning} agentServiceUp={agentServiceUp} />
          <LogPanel logs={logs} onClear={clearLogs} />
        </div>
      </div>
    </div>
  )
}
