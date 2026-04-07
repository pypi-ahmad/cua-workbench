import { startContainer, stopContainer } from '../api'

export default function Header({ connected, containerRunning, agentServiceUp, agentRunning, onRefreshContainer }) {

  const handleStartContainer = async () => {
    await startContainer()
    onRefreshContainer()
  }

  const handleStopContainer = async () => {
    await stopContainer()
    onRefreshContainer()
  }

  return (
    <header className="header">
      <h1>
        <span>CUA</span> — Computer Using Agent
      </h1>
      <div className="header-status">
        <div className="container-controls">
          <span className={`container-status ${containerRunning ? 'running' : 'stopped'}`} aria-label={containerRunning ? 'Container is running' : 'Container is stopped'}>
            {containerRunning ? '● Container Running' : '✕ Container Stopped'}
          </span>
          {containerRunning && (
            <span className={`service-badge ${agentServiceUp ? 'up' : 'down'}`} aria-label={agentServiceUp ? 'Agent service is ready' : 'Agent service is down'}>
              Agent Service {agentServiceUp ? 'Ready' : 'Down'}
            </span>
          )}
          {!containerRunning && (
            <button className="btn-sm" onClick={handleStartContainer} aria-label="Start Docker container">
              Start Container
            </button>
          )}
          {containerRunning && !agentRunning && (
            <button className="btn-sm" onClick={handleStopContainer} aria-label="Stop Docker container">
              Stop Container
            </button>
          )}
        </div>
        <span className={`status-dot ${connected ? 'connected' : ''} ${agentRunning ? 'running' : ''}`} aria-label={connected ? (agentRunning ? 'Agent is running' : 'WebSocket connected') : 'WebSocket disconnected'} />
        <span>{connected ? (agentRunning ? 'Agent Running' : 'Connected') : 'Disconnected'}</span>
      </div>
    </header>
  )
}
