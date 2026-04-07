import { startContainer, stopContainer } from '../api'

export default function Header({ connected, containerRunning, agentServiceUp, agentRunning, onRefreshContainer, systemHealth }) {

  const handleStartContainer = async () => {
    await startContainer()
    onRefreshContainer()
  }

  const handleStopContainer = async () => {
    await stopContainer()
    onRefreshContainer()
  }

  // B-13: Derive health badge color from systemHealth
  const healthStatus = systemHealth?.status
  const healthColor = healthStatus === 'healthy' ? 'var(--success, #34d399)' : healthStatus === 'degraded' ? 'var(--warning, #fbbf24)' : healthStatus === 'unhealthy' ? 'var(--error, #f44336)' : 'var(--text-secondary)'
  const healthLabel = healthStatus === 'healthy' ? 'All systems healthy' : healthStatus === 'degraded' ? 'System degraded' : healthStatus === 'unhealthy' ? 'System unhealthy' : 'Health unknown'

  // B-31: VNC warning
  const vncUnprotected = systemHealth && containerRunning && systemHealth.vnc_protected === false

  return (
    <header className="header">
      <h1>
        <span>CUA</span> — Computer Using Agent
      </h1>
      <div className="header-status">
        <div className="container-controls">
          {systemHealth && (
            <span
              style={{ display: 'inline-block', width: 8, height: 8, borderRadius: '50%', background: healthColor, marginRight: 6, flexShrink: 0 }}
              aria-label={healthLabel}
              title={healthLabel}
            />
          )}
          <span className={`container-status ${containerRunning ? 'running' : 'stopped'}`} aria-label={containerRunning ? 'Sandbox is ready' : 'Sandbox is offline'}>
            {containerRunning ? '● Sandbox Ready' : '✕ Sandbox Offline'}
          </span>
          {containerRunning && (
            <span className={`service-badge ${agentServiceUp ? 'up' : 'down'}`} aria-label={agentServiceUp ? 'Automation is ready' : 'Automation is starting'}>
              Automation {agentServiceUp ? 'Ready' : 'Starting…'}
            </span>
          )}
          {vncUnprotected && (
            <span
              style={{ fontSize: 10, color: 'var(--warning, #fbbf24)', marginLeft: 6 }}
              title="The desktop view is not password-protected. Set VNC_PASSWORD in your configuration for security."
              aria-label="Desktop view is not password-protected"
            >
              ⚠ Desktop view unprotected
            </span>
          )}
          {!containerRunning && (
            <button className="btn-sm" onClick={handleStartContainer} aria-label="Start the sandbox environment">
              Start Sandbox
            </button>
          )}
          {containerRunning && !agentRunning && (
            <button className="btn-sm" onClick={handleStopContainer} aria-label="Stop the sandbox environment">
              Stop Sandbox
            </button>
          )}
        </div>
        <span className={`status-dot ${connected ? 'connected' : ''} ${agentRunning ? 'running' : ''}`} aria-label={connected ? (agentRunning ? 'Agent is running' : 'Connected to server') : 'Reconnecting to server'} />
        <span>{connected ? (agentRunning ? 'Agent Running' : 'Connected') : 'Reconnecting…'}</span>
      </div>
    </header>
  )
}
