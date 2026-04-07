import { useState } from 'react'
import useWebSocket from './hooks/useWebSocket'
import useContainerStatus from './hooks/useContainerStatus'
import ControlPanel from './components/ControlPanel'
import ScreenView from './components/ScreenView'
import LogPanel from './components/LogPanel'
import Header from './components/Header'

export default function App() {
  const { connected, lastScreenshot, logs, steps, agentFinished, clearLogs, clearSteps, clearFinished } = useWebSocket()
  const { containerRunning, agentServiceUp, systemHealth, refreshContainer } = useContainerStatus()

  const [agentRunning, setAgentRunning] = useState(false)
  const [sessionId, setSessionId] = useState(null)

  return (
    <div className="app">
      <Header
        connected={connected}
        containerRunning={containerRunning}
        agentServiceUp={agentServiceUp}
        agentRunning={agentRunning}
        onRefreshContainer={refreshContainer}
        systemHealth={systemHealth}
      />
      <div className="main-content">
        <ControlPanel
          containerRunning={containerRunning}
          agentRunning={agentRunning}
          setAgentRunning={setAgentRunning}
          sessionId={sessionId}
          setSessionId={setSessionId}
          steps={steps}
          logs={logs}
          lastScreenshot={lastScreenshot}
          clearSteps={clearSteps}
          onRefreshContainer={refreshContainer}
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
