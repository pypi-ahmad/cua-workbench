import { useState, useEffect, useRef } from 'react'
import { Link } from 'react-router-dom'
import useWebSocket from '../hooks/useWebSocket'
import useAgentConfig from '../hooks/useAgentConfig'
import useContainerStatus from '../hooks/useContainerStatus'
import { startAgent, stopAgent, startContainer, getPreflight, getContainerLogs } from '../api'
import { ENGINE_HELP, SAMPLE_TASKS, ENGINES_WITH_TARGET, getDefaultTarget, estimateCost, DEFAULT_BROWSER_ENGINES, DEFAULT_DESKTOP_ENGINES } from '../shared'
import ScreenView from '../components/ScreenView'
import './Workbench.css'

// No hardcoded fallback — models come exclusively from GET /api/models.

export default function Workbench() {
  const { connected, lastScreenshot, logs, steps, agentFinished, clearLogs, clearSteps, clearFinished } = useWebSocket()

  // B-27: shared config hook replaces duplicated model/engine/key state
  const {
    provider, setProvider,
    model, setModel,
    models,
    fetchedModels,
    modelsLoaded,
    backendReachable,
    engineList,
    keyStatuses,
    keySource, setKeySource,
    apiKey, setApiKey,
    keyValid, setKeyValid,
    keyValidating,
    handleValidateKey,
  } = useAgentConfig('google')

  // B-27: shared container status hook
  const { containerRunning, agentServiceUp, refreshContainer } = useContainerStatus()

  // Agent state
  const [agentRunning, setAgentRunning] = useState(false)
  const [sessionId, setSessionId] = useState(null)

  // Config (Workbench-specific)
  const [runMode, setRunMode] = useState('browser') // 'browser' | 'desktop'
  const [engine, setEngine] = useState('playwright_mcp')
  const [task, setTask] = useState('')
  const [maxSteps, setMaxSteps] = useState(50)
  const [executionTarget, setExecutionTarget] = useState('local')
  const [error, setError] = useState('')
  const [preflightWarnings, setPreflightWarnings] = useState(null)
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [logLevelFilter, setLogLevelFilter] = useState({ info: true, warning: true, error: true, debug: true })
  const [logTab, setLogTab] = useState('agent') // 'agent' | 'container'
  const [containerLogs, setContainerLogs] = useState('')

  // Smart default: docker for CU/a11y, local for playwright_mcp

  // Timeline expansion
  const [expandedStep, setExpandedStep] = useState(null)

  // Refs
  const timelineRef = useRef(null)
  const logRef = useRef(null)

  // Browser/Desktop engine split (Workbench-specific layout)
  const browserEngines = engineList.filter(e => e.category === 'browser')
  const desktopEngines = engineList.filter(e => e.category === 'desktop')
  const engines = runMode === 'browser'
    ? (browserEngines.length > 0 ? browserEngines : DEFAULT_BROWSER_ENGINES)
    : (desktopEngines.length > 0 ? desktopEngines : DEFAULT_DESKTOP_ENGINES)

  // Auto-stop frontend when agent finishes (done/error/max-steps)
  useEffect(() => {
    if (agentFinished && agentRunning) {
      setAgentRunning(false)
      setSessionId(null)
      clearFinished()
    }
  }, [agentFinished, agentRunning, clearFinished])

  // Auto-scroll timeline
  useEffect(() => {
    if (timelineRef.current) {
      timelineRef.current.scrollTop = timelineRef.current.scrollHeight
    }
  }, [steps])

  // Auto-scroll logs
  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight
    }
  }, [logs])

  // Sync engine when runMode changes
  useEffect(() => {
    const newEngine = runMode === 'browser' ? 'playwright_mcp' : 'computer_use'
    setEngine(newEngine)
    setExecutionTarget(getDefaultTarget(newEngine))
  }, [runMode])

  // handleValidateKey provided by useAgentConfig hook

  const handleStart = async () => {
    const providerLabel = provider === 'google' ? 'Google' : 'Anthropic'
    const envVar = provider === 'google' ? 'GOOGLE_API_KEY' : 'ANTHROPIC_API_KEY'
    if (keySource === 'ui' && !apiKey.trim()) return setError(`Enter your ${providerLabel} API key, or add ${envVar} to your .env file.`)
    if (!task.trim()) return setError('Describe what the agent should do.')
    setError('')
    setPreflightWarnings(null)
    clearSteps()
    clearLogs()

    try {
      if (!containerRunning) {
        await startContainer()
        await refreshContainer()
      }

      // B-12: Pre-flight check — warn but never block
      try {
        const pf = await getPreflight(engine, provider)
        if (pf.checks) {
          const failed = pf.checks.filter(c => !c.ok)
          if (failed.length > 0) {
            setPreflightWarnings(failed.map(c => `${c.label}: ${c.message}`))
          }
        }
      } catch { /* preflight itself failed — don't block start */ }

      const engineMode = engine === 'omni_accessibility' || engine === 'computer_use' ? 'desktop' : 'browser'

      const res = await startAgent({
        task: task.trim(),
        apiKey: keySource === 'ui' ? apiKey.trim() : '', // empty = backend resolves from env
        model,
        maxSteps: Number(maxSteps),
        mode: engineMode,
        engine,
        provider,
        executionTarget,
      })
      if (res.error) return setError(res.error)
      setSessionId(res.session_id)
      setAgentRunning(true)
    } catch (e) {
      setError(`Failed to start: ${e.message}`)
    }
  }

  const handleStop = async () => {
    if (!sessionId) return
    try { await stopAgent(sessionId) } catch { /* ignore */ }
    setAgentRunning(false)
    setSessionId(null)
  }

  const handleDownloadLogs = () => {
    if (logs.length === 0) return
    const now = new Date()
    const pad = (n, w = 2) => String(n).padStart(w, '0')
    const filename = `CUA_logs_${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}.txt`
    const lines = logs.map(log => {
      const ts = formatTime(log.timestamp)
      return `[${ts}] [${(log.level || '').toUpperCase()}] ${log.message}`
    })
    const blob = new Blob([lines.join('\n')], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)
  }

  const handleExportSession = () => {
    if (steps.length === 0) return
    const now = new Date()
    const pad = (n, w = 2) => String(n).padStart(w, '0')
    const filename = `CUA_session_${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}.json`
    const session = {
      exported_at: now.toISOString(),
      config: { provider, model, engine, executionTarget, maxSteps: Number(maxSteps), runMode },
      steps,
      logs,
    }
    const blob = new Blob([JSON.stringify(session, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)
  }

  const handleFetchContainerLogs = async () => {
    try {
      const data = await getContainerLogs(200)
      setContainerLogs(data.logs || data.stderr || 'No logs available.')
    } catch {
      setContainerLogs('Could not fetch container logs.')
    }
  }

  const formatTime = (ts) => {
    try { return new Date(ts).toLocaleTimeString('en-US', { hour12: false }) }
    catch { return '--:--:--' }
  }

  const getActionIcon = (action) => {
    const icons = {
      click: '🖱️', double_click: '🖱️', right_click: '🖱️', hover: '👆',
      type: '⌨️', fill: '📝', key: '⌨️', hotkey: '⌨️', paste: '📋', copy: '📋',
      open_url: '🌐', reload: '🔄', go_back: '◀', go_forward: '▶',
      new_tab: '➕', close_tab: '✖', switch_tab: '🔀',
      scroll: '📜', scroll_to: '📜',
      get_text: '📖', find_element: '🔍', evaluate_js: '💻',
      focus_window: '🪟', open_app: '🚀',
      wait: '⏳', wait_for: '⏳', screenshot_region: '📸',
      done: '✅', error: '❌',
    }
    return icons[action] || '⚡'
  }

  return (
    <div className="wb">
      {/* Header */}
      <header className="wb-header">
        <div className="wb-header-left">
          <Link to="/" className="wb-back">← Back</Link>
          <h1>CUA Workbench</h1>
          <span className={`wb-status-pill ${containerRunning ? 'up' : 'down'}`} aria-label={containerRunning ? 'Container is up' : 'Container is down'}>
            {containerRunning ? '● Container Up' : '✕ Container Down'}
          </span>
          <span className={`wb-status-pill ${connected ? 'up' : 'down'}`} aria-label={connected ? 'WebSocket connected' : 'WebSocket disconnected'}>
            {connected ? 'WS Connected' : 'WS Disconnected'}
          </span>
          {agentRunning && <span className="wb-status-pill running" aria-label="Agent is running">Agent Running</span>}
        </div>
        <div className="wb-header-right">
          <span className="wb-step-counter">Steps: {steps.length}/{maxSteps}</span>
          {steps.length > 0 && estimateCost(model, steps.length) && (
            <span className="wb-step-counter" title="Rough API cost estimate based on model and steps">~${estimateCost(model, steps.length)}</span>
          )}
        </div>
      </header>

      <div className="wb-body">
        {/* Left: Config */}
        <aside className="wb-sidebar">
          {/* Run Mode Toggle */}
          <div className="wb-section">
            <label className="wb-label">Run Mode</label>
            <div className="wb-toggle-group" role="radiogroup" aria-label="Run mode">
              <button role="radio" aria-checked={runMode === 'browser'} className={`wb-toggle ${runMode === 'browser' ? 'active' : ''}`} onClick={() => setRunMode('browser')} disabled={agentRunning}>Browser</button>
              <button role="radio" aria-checked={runMode === 'desktop'} className={`wb-toggle ${runMode === 'desktop' ? 'active' : ''}`} onClick={() => setRunMode('desktop')} disabled={agentRunning}>Desktop</button>
            </div>
          </div>

          {/* Provider & Model */}
          <div className="wb-section">
            <label className="wb-label">Provider</label>
            <select className="wb-select" value={provider} onChange={(e) => setProvider(e.target.value)} disabled={agentRunning} title="Which AI provider to use for the agent">
              <option value="google">Google Gemini</option>
              <option value="anthropic">Anthropic Claude</option>
            </select>
            <label className="wb-label">Model</label>
            <select className="wb-select" value={model} onChange={(e) => setModel(e.target.value)} disabled={agentRunning || models.length === 0} title="The specific AI model — larger models are slower but more capable">
              {models.length > 0 ? models.map(m => <option key={m.value} value={m.value}>{m.label}</option>) : (
                <option value="">Loading models…</option>
              )}
            </select>
            {modelsLoaded && models.length === 0 && (
              <p className="wb-error" style={{ margin: '4px 0 0', fontSize: 11 }}>No models available for this provider.</p>
            )}
            <label className="wb-label">API Key Source</label>
            <div className="wb-key-source-group" role="radiogroup" aria-label="API key source">
              <button role="radio" aria-checked={keySource === 'ui'} className={`wb-key-src-btn ${keySource === 'ui' ? 'active' : ''}`} onClick={() => { setKeySource('ui'); setKeyValid(null) }} disabled={agentRunning} title="Enter key manually">
                Enter key
              </button>
              <button
                role="radio" aria-checked={keySource === 'dotenv'}
                className={`wb-key-src-btn ${keySource === 'dotenv' ? 'active' : ''} ${keyStatuses[provider]?.source === 'dotenv' ? 'available' : ''}`}
                onClick={() => setKeySource('dotenv')}
                disabled={agentRunning || keyStatuses[provider]?.source !== 'dotenv'}
                title={keyStatuses[provider]?.source === 'dotenv' ? `Found in .env (${keyStatuses[provider]?.masked_key})` : 'No key in .env file'}
              >
                From .env file {keyStatuses[provider]?.source === 'dotenv' && '✓'}
              </button>
              <button
                role="radio" aria-checked={keySource === 'env'}
                className={`wb-key-src-btn ${keySource === 'env' ? 'active' : ''} ${keyStatuses[provider]?.source === 'env' ? 'available' : ''}`}
                onClick={() => setKeySource('env')}
                disabled={agentRunning || keyStatuses[provider]?.source !== 'env'}
                title={keyStatuses[provider]?.source === 'env' ? `Found in system env (${keyStatuses[provider]?.masked_key})` : 'No environment variable set'}
              >
                Environment variable {keyStatuses[provider]?.source === 'env' && '✓'}
              </button>
            </div>
            {keySource !== 'ui' && keyStatuses[provider]?.available && (
              <div className="wb-key-status">
                <span className="wb-key-badge ok">🔑 {keyStatuses[provider]?.masked_key}</span>
                <span className="wb-key-source-label">from {keySource === 'env' ? 'system variable' : '.env file'}</span>
              </div>
            )}
            {keySource !== 'ui' && !keyStatuses[provider]?.available && (
              <div className="wb-key-status">
                <span className="wb-key-badge missing">⚠️ No key found</span>
                <span className="wb-key-source-label">
                  {provider === 'google' ? 'Set GOOGLE_API_KEY' : 'Set ANTHROPIC_API_KEY'}
                </span>
              </div>
            )}
            {keySource === 'ui' && (
              <>
                <label className="wb-label">API Key</label>
                <div style={{ position: 'relative' }}>
                  <input type="password" className="wb-input" placeholder={provider === 'anthropic' ? 'sk-ant-...' : 'AI...'} value={apiKey}
                    onChange={(e) => { setApiKey(e.target.value); setKeyValid(null) }}
                    onBlur={() => { if (apiKey.trim().length >= 8) handleValidateKey(apiKey.trim(), provider) }}
                    autoComplete="off" />
                  {keyValid === true && <span style={{ position: 'absolute', right: 8, top: '50%', transform: 'translateY(-50%)', color: 'var(--success)', fontSize: 14 }} aria-label="Key valid">✓</span>}
                  {keyValid === false && <span style={{ position: 'absolute', right: 8, top: '50%', transform: 'translateY(-50%)', color: 'var(--error)', fontSize: 14 }} aria-label="Key invalid">✗</span>}
                  {keyValidating && <span style={{ position: 'absolute', right: 8, top: '50%', transform: 'translateY(-50%)', color: 'var(--text-secondary)', fontSize: 10 }}>checking…</span>}
                </div>
              </>
            )}
          </div>

          {/* Engine — B-26: Progressive disclosure */}
          <div className="wb-section">
            <button onClick={() => setShowAdvanced(!showAdvanced)}
              style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-secondary)', fontSize: 10, padding: 0, display: 'flex', alignItems: 'center', gap: 4, width: '100%', textAlign: 'left' }}
              aria-expanded={showAdvanced}
            >
              <span style={{ transform: showAdvanced ? 'rotate(90deg)' : 'rotate(0)', transition: 'transform 0.15s', display: 'inline-block' }}>▶</span>
              Advanced Settings
            </button>
            {showAdvanced && (
              <>
            <label className="wb-label" style={{ marginTop: 6 }}>Engine</label>
            <select className="wb-select" value={engine} onChange={(e) => { setEngine(e.target.value); setExecutionTarget(getDefaultTarget(e.target.value)) }} disabled={agentRunning} title="How the agent interacts with the computer">
              {engines.map(e => {
                const selectedModel = fetchedModels.find(m => m.model_id === model)
                const supported = !selectedModel || (
                  (e.value === 'playwright_mcp' && selectedModel.supports_playwright_mcp !== false) ||
                  (e.value === 'omni_accessibility' && selectedModel.supports_accessibility !== false) ||
                  (e.value === 'computer_use' && selectedModel.supports_computer_use !== false)
                )
                return <option key={e.value} value={e.value} disabled={!supported}>{e.label}{!supported ? ' (not supported by this model)' : ''}</option>
              })}
            </select>
            {ENGINE_HELP[engine] && (
              <p style={{ color: 'var(--text-secondary)', fontSize: 10, margin: '3px 0 0', lineHeight: 1.4 }}>{ENGINE_HELP[engine]}</p>
            )}
            {!backendReachable && (
              <p className="wb-error" style={{ margin: '4px 0 0', fontSize: 10 }}>Cannot reach backend — start it with <code style={{ fontSize: 10 }}>python -m backend.main</code></p>
            )}
            {ENGINES_WITH_TARGET.includes(engine) && (
              <>
                <label className="wb-label">Run Location</label>
                <select className="wb-select" value={executionTarget} onChange={(e) => setExecutionTarget(e.target.value)} disabled={agentRunning} title="Where to execute the automation">
                  <option value="local">This machine</option>
                  <option value="docker">Docker container</option>
                </select>
              </>
            )}
            <label className="wb-label">Max Steps</label>
            <input type="number" className="wb-input wb-input-sm" min={1} max={200} value={maxSteps} onChange={(e) => setMaxSteps(e.target.value)} disabled={agentRunning} title="Maximum number of actions the agent can take before stopping" />
            {estimateCost(model, Number(maxSteps)) && (
              <p style={{ color: 'var(--text-secondary)', fontSize: 10, margin: '3px 0 0' }}>
                Est. max cost: ~${estimateCost(model, Number(maxSteps))}
              </p>
            )}
              </>
            )}
          </div>

          {/* Task */}
          <div className="wb-section wb-section-grow">
            <label className="wb-label">Task</label>
            <textarea className="wb-textarea" placeholder="Describe what the agent should do..." value={task} onChange={(e) => setTask(e.target.value)} disabled={agentRunning}
              onKeyDown={(e) => { if (e.key === 'Enter' && e.ctrlKey && !agentRunning) handleStart() }}
              title="Plain English description of the task for the agent"
            />
            {!task.trim() && !agentRunning && (
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 3, marginTop: 4 }}>
                {SAMPLE_TASKS.map((sample, i) => (
                  <button key={i} onClick={() => setTask(sample)} title={sample}
                    style={{ padding: '2px 6px', fontSize: 10, borderRadius: 3, border: '1px solid var(--border)', cursor: 'pointer', background: 'var(--bg-primary)', color: 'var(--text-secondary)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: '100%' }}>
                    {sample}
                  </button>
                ))}
              </div>
            )}
            {error && <p className="wb-error">{error}</p>}
            {preflightWarnings && preflightWarnings.length > 0 && (
              <div style={{ background: 'var(--bg-secondary)', border: '1px solid var(--warning, #fbbf24)', borderRadius: 4, padding: '4px 6px', marginTop: 4, fontSize: 10 }}>
                <strong style={{ color: 'var(--warning, #fbbf24)' }}>Pre-flight warnings:</strong>
                <ul style={{ margin: '2px 0 0', paddingLeft: 14 }}>
                  {preflightWarnings.map((w, i) => <li key={i} style={{ color: 'var(--text-secondary)' }}>{w}</li>)}
                </ul>
              </div>
            )}
            <div className="wb-btn-row">
              <button className="wb-btn wb-btn-primary" onClick={handleStart} disabled={agentRunning || models.length === 0} title="Start the agent (Ctrl+Enter)">
                {agentRunning ? 'Running...' : !backendReachable ? 'Backend Offline' : models.length === 0 ? 'No Models Loaded' : 'Start Agent (Ctrl+Enter)'}
              </button>
              <button className="wb-btn wb-btn-danger" onClick={handleStop} disabled={!agentRunning}>Stop</button>
              <button className="wb-btn wb-btn-secondary" onClick={() => { clearSteps(); clearLogs() }} disabled={agentRunning} aria-label="Clear steps and logs">Clear</button>
            </div>
          </div>
        </aside>

        {/* Center: Live Screen */}
        <main className="wb-screen-area">
          <ScreenView screenshot={lastScreenshot} containerRunning={containerRunning} agentServiceUp={agentServiceUp} />

          {/* Progress bar */}
          {agentRunning && steps.length > 0 && (
            <div className="wb-progress">
              <div className="wb-progress-fill" style={{ width: `${Math.min((steps.length / maxSteps) * 100, 100)}%` }} />
            </div>
          )}
        </main>

        {/* Right: Timeline + Logs */}
        <aside className="wb-right-panel">
          {/* Timeline */}
          <div className="wb-timeline-section">
            <div className="wb-panel-header">
              <h3>Timeline ({steps.length})</h3>
            </div>
            <div className="wb-timeline" ref={timelineRef}>
              {steps.length === 0 && <p className="wb-empty">No steps yet.</p>}
              {steps.map((step, i) => (
                <div key={i} className={`wb-timeline-item ${step.error ? 'has-error' : ''} ${expandedStep === i ? 'expanded' : ''}`} onClick={() => setExpandedStep(expandedStep === i ? null : i)} role="button" tabIndex={0} aria-expanded={expandedStep === i} aria-label={`Step ${step.step_number}: ${step.action?.action || 'unknown'}`} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setExpandedStep(expandedStep === i ? null : i) } }}>
                  <div className="wb-timeline-head">
                    <span className="wb-step-num">#{step.step_number}</span>
                    <span className="wb-action-icon">{getActionIcon(step.action?.action)}</span>
                    <span className="wb-action-name">{step.action?.action || 'unknown'}</span>
                    {step.action?.target && <span className="wb-action-target" title={step.action.target}>{step.action.target.length > 20 ? step.action.target.slice(0, 20) + '…' : step.action.target}</span>}
                    {step.action?.text && step.action.action !== 'done' && (
                      <span className="wb-action-text" title={step.action.text}>"{step.action.text.length > 20 ? step.action.text.slice(0, 20) + '…' : step.action.text}"</span>
                    )}
                    <span className="wb-step-time">{formatTime(step.timestamp)}</span>
                  </div>
                  {expandedStep === i && (
                    <div className="wb-timeline-detail">
                      {step.action?.reasoning && <p className="wb-reasoning">{step.action.reasoning}</p>}
                      {step.action?.coordinates && <p className="wb-coords">Coords: [{step.action.coordinates.join(', ')}]</p>}
                      {step.error && <p className="wb-step-error">Error: {step.error}</p>}
                      <pre className="wb-json">{JSON.stringify(step.action, null, 2)}</pre>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>

          {/* Logs */}
          <div className="wb-log-section">
            <div className="wb-panel-header">
              <div style={{ display: 'flex', gap: 0 }}>
                <button onClick={() => setLogTab('agent')}
                  style={{ padding: '2px 8px', fontSize: 11, fontWeight: logTab === 'agent' ? 700 : 400, background: logTab === 'agent' ? 'var(--bg-primary)' : 'transparent', border: '1px solid var(--border)', borderBottom: logTab === 'agent' ? 'none' : '1px solid var(--border)', borderRadius: '4px 4px 0 0', cursor: 'pointer', color: 'var(--text-primary)' }}>
                  Logs ({logs.filter(l => logLevelFilter[l.level] !== false).length})
                </button>
                <button onClick={() => { setLogTab('container'); handleFetchContainerLogs() }}
                  style={{ padding: '2px 8px', fontSize: 11, fontWeight: logTab === 'container' ? 700 : 400, background: logTab === 'container' ? 'var(--bg-primary)' : 'transparent', border: '1px solid var(--border)', borderBottom: logTab === 'container' ? 'none' : '1px solid var(--border)', borderRadius: '4px 4px 0 0', cursor: 'pointer', color: 'var(--text-primary)', marginLeft: -1 }}>
                  Container
                </button>
              </div>
              {logTab === 'agent' && (
              <div className="wb-log-actions">
                {['info', 'warning', 'error', 'debug'].map(level => (
                  <button key={level} onClick={() => setLogLevelFilter(prev => ({ ...prev, [level]: !prev[level] }))}
                    aria-label={`${logLevelFilter[level] ? 'Hide' : 'Show'} ${level} logs`}
                    aria-pressed={logLevelFilter[level]}
                    style={{ padding: '1px 4px', fontSize: 9, borderRadius: 3, border: '1px solid var(--border)', cursor: 'pointer', textTransform: 'uppercase', fontWeight: 600, background: logLevelFilter[level] ? 'var(--bg-primary)' : 'transparent', opacity: logLevelFilter[level] ? 1 : 0.6, color: level === 'info' ? 'var(--accent)' : level === 'error' ? 'var(--error)' : level === 'warning' ? 'var(--warning,#fbbf24)' : 'var(--text-secondary)' }}>
                    {level}
                  </button>
                ))}
                <button className="wb-download-btn" onClick={handleDownloadLogs} disabled={logs.length === 0} title="Download logs as .txt" aria-label="Download logs">⬇ Download</button>
                <button className="wb-download-btn" onClick={handleExportSession} disabled={steps.length === 0} title="Export session as JSON" aria-label="Export session">📦 Export</button>
                <button className="wb-clear-btn" onClick={clearLogs} aria-label="Clear logs">Clear</button>
              </div>
              )}
              {logTab === 'container' && (
              <div className="wb-log-actions">
                <button className="wb-clear-btn" onClick={handleFetchContainerLogs} aria-label="Refresh container logs">Refresh</button>
              </div>
              )}
            </div>
            {logTab === 'agent' && (
            <div className="wb-logs" ref={logRef} role="log" aria-live="polite">
              {(() => { const filtered = logs.filter(l => logLevelFilter[l.level] !== false); return filtered.length === 0 ? (
                <p className="wb-empty">{logs.length === 0 ? 'Waiting for logs...' : 'No logs match the current filter.'}</p>
              ) : filtered.map((log, i) => (
                <div key={i} className="wb-log-entry">
                  <span className="wb-log-time">{formatTime(log.timestamp)}</span>
                  <span className={`wb-log-level ${log.level}`}>{log.level}</span>
                  <span className="wb-log-msg">{log.message}</span>
                </div>
              )); })()}
            </div>
            )}
            {logTab === 'container' && (
            <div className="wb-logs" style={{ whiteSpace: 'pre-wrap', fontFamily: 'monospace', fontSize: 11 }}>
              {containerLogs || <p className="wb-empty">Click the Container tab to load logs.</p>}
            </div>
            )}
          </div>
        </aside>
      </div>
    </div>
  )
}
