import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { startAgent, stopAgent, startContainer, getPreflight, confirmSafety } from '../api'
import { ENGINE_HELP, SAMPLE_TASKS, ENGINES_WITH_TARGET, getDefaultTarget, estimateCost } from '../shared'
import useAgentConfig from '../hooks/useAgentConfig'

export default function ControlPanel({
  containerRunning,
  agentRunning,
  setAgentRunning,
  sessionId,
  setSessionId,
  steps,
  logs,
  lastScreenshot,
  clearSteps,
  onRefreshContainer,
  agentFinished,
  clearFinished,
  safetyPrompt,
  clearSafetyPrompt,
}) {
  // B-27: shared config hook replaces duplicated model/engine/key state
  const {
    provider, setProvider: handleProviderChange,
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

  const [task, setTask] = useState('')
  const [maxSteps, setMaxSteps] = useState(50)
  const [engine, setEngine] = useState('playwright_mcp')
  const [executionTarget, setExecutionTarget] = useState('local')
  const [error, setError] = useState('')
  const [preflightWarnings, setPreflightWarnings] = useState(null)
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [sessionResult, setSessionResult] = useState(null)
  const [showWelcome, setShowWelcome] = useState(() => !localStorage.getItem('cua_welcomed'))

  // Auto-stop when agent finishes (done/error/max-steps) — mirrors Workbench behavior
  useEffect(() => {
    if (agentFinished && agentRunning) {
      setSessionResult({ status: agentFinished.status, steps: agentFinished.steps })
      setAgentRunning(false)
      setSessionId(null)
      if (clearFinished) clearFinished()
    }
  }, [agentFinished, agentRunning, setAgentRunning, setSessionId, clearFinished])

  const handleStart = async () => {
    const providerLabel = provider === 'google' ? 'Google' : 'Anthropic'
    if (keySource === 'ui' && !apiKey.trim()) {
      setError(`Enter your ${providerLabel} API key above, or switch to "Saved key" if one is already configured.`)
      return
    }
    if (!task.trim()) {
      setError('Describe what the agent should do.')
      return
    }
    setError('')
    setSessionResult(null)
    setPreflightWarnings(null)
    clearSteps()

    try {
      // Auto-start container if needed (matches Workbench behavior)
      if (!containerRunning) {
        try {
          await startContainer()
          if (onRefreshContainer) onRefreshContainer()
        } catch { /* will fail at agent start if container can't start */ }
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

      // Derive agent service mode from engine selection
      const engineMode = engine === 'omni_accessibility' || engine === 'computer_use' ? 'desktop' : 'browser'
      const res = await startAgent({
        task: task.trim(),
        apiKey: keySource === 'ui' ? apiKey.trim() : '',
        model,
        maxSteps: Number(maxSteps),
        mode: engineMode,
        engine,
        provider,
        executionTarget,
      })
      if (res.error) {
        setError(res.error)
        return
      }
      setSessionId(res.session_id)
      setAgentRunning(true)
    } catch (e) {
      setError(`Failed to start: ${e.message}`)
    }
  }

  const handleStop = async () => {
    if (!sessionId) return
    if (!window.confirm('Stop the agent? Progress from this session cannot be recovered.')) return
    try {
      await stopAgent(sessionId)
    } catch {
      // ignore
    }
    setAgentRunning(false)
    setSessionId(null)
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && e.ctrlKey && !agentRunning) {
      handleStart()
    }
  }

  // B-24: Export session as JSON
  const handleExportSession = () => {
    if (steps.length === 0) return
    const now = new Date()
    const pad = (n, w = 2) => String(n).padStart(w, '0')
    const filename = `CUA_session_${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}.json`
    const session = {
      exported_at: now.toISOString(),
      config: { provider, model, engine, executionTarget, maxSteps: Number(maxSteps) },
      steps,
      logs: logs || [],
      final_screenshot: lastScreenshot || null,
    }
    const blob = new Blob([JSON.stringify(session, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="left-panel">
      {/* First-visit welcome card */}
      {showWelcome && (
        <div className="panel-section" style={{ background: 'var(--bg-secondary)', border: '1px solid var(--accent)', borderRadius: 8, padding: 14, marginBottom: 10 }}>
          <h3 style={{ margin: '0 0 6px', fontSize: 15 }}>👋 Welcome to CUA Workbench</h3>
          <p style={{ margin: '0 0 8px', fontSize: 12, lineHeight: 1.5, color: 'var(--text-secondary)' }}>
            This app lets an AI agent control a browser or desktop inside a secure sandbox. To get started:
          </p>
          <ol style={{ margin: '0 0 10px', paddingLeft: 18, fontSize: 12, lineHeight: 1.7, color: 'var(--text-secondary)' }}>
            <li>Choose a provider and paste your API key</li>
            <li>Type a task (e.g. "Search Google for the weather")</li>
            <li>Click <strong>Start Agent</strong> and watch it work</li>
          </ol>
          <button
            onClick={() => { localStorage.setItem('cua_welcomed', '1'); setShowWelcome(false) }}
            style={{ fontSize: 12, padding: '5px 14px', borderRadius: 6, border: 'none', background: 'var(--accent)', color: '#fff', cursor: 'pointer' }}
          >Got it</button>
        </div>
      )}
      {/* API Config */}
      <div className="panel-section">
        <h3>API Configuration</h3>
        <select className="model-select" value={provider} onChange={(e) => handleProviderChange(e.target.value)} disabled={agentRunning} title="Which AI provider to use for the agent">
          <option value="google">Google Gemini</option>
          <option value="anthropic">Anthropic Claude</option>
        </select>

        {/* Key Source Toggle */}
        <div className="key-source-row" role="radiogroup" aria-label="API key source" style={{ display: 'flex', gap: 4, marginBottom: 6 }}>
          <button
            role="radio"
            aria-checked={keySource === 'ui'}
            className={`key-src-btn ${keySource === 'ui' ? 'active' : ''}`}
            onClick={() => { setKeySource('ui'); setKeyValid(null) }}
            disabled={agentRunning}
            style={{ flex: 1, padding: '4px 6px', fontSize: 11, borderRadius: 4, border: '1px solid var(--border)', cursor: 'pointer', background: keySource === 'ui' ? 'var(--accent)' : 'var(--bg-secondary)', color: keySource === 'ui' ? '#fff' : 'var(--text-primary)' }}
          >
            Enter manually
          </button>
          <button
            role="radio"
            aria-checked={keySource !== 'ui'}
            className={`key-src-btn ${keySource !== 'ui' ? 'active' : ''}`}
            onClick={() => { const src = keyStatuses[provider]?.source; if (src) setKeySource(src) }}
            disabled={agentRunning || !keyStatuses[provider]?.available}
            style={{ flex: 1, padding: '4px 6px', fontSize: 11, borderRadius: 4, border: '1px solid var(--border)', cursor: 'pointer', background: keySource !== 'ui' ? 'var(--accent)' : 'var(--bg-secondary)', color: keySource !== 'ui' ? '#fff' : 'var(--text-primary)', opacity: keyStatuses[provider]?.available ? 1 : 0.6 }}
            title={keyStatuses[provider]?.available ? `Key found (${keyStatuses[provider]?.masked_key})` : 'No saved key found for this provider'}
          >
            Saved key {keyStatuses[provider]?.available ? '✓' : ''}
          </button>
        </div>

        {keySource !== 'ui' && keyStatuses[provider]?.available && (
          <div style={{ fontSize: 11, color: 'var(--success, #4caf50)', marginBottom: 6, display: 'flex', alignItems: 'center', gap: 4 }}>
            🔑 <span>{keyStatuses[provider]?.masked_key}</span>
          </div>
        )}
        {keySource !== 'ui' && !keyStatuses[provider]?.available && (
          <div style={{ fontSize: 11, color: 'var(--error, #f44336)', marginBottom: 6 }}>
            ⚠️ No saved key found for this provider.
          </div>
        )}

        {keySource === 'ui' && (
          <div style={{ position: 'relative' }}>
            <input
              type="password"
              className="api-key-input"
              placeholder={provider === 'anthropic' ? 'Anthropic API Key' : 'Gemini API Key'}
              value={apiKey}
              onChange={(e) => { setApiKey(e.target.value); setKeyValid(null) }}
              onBlur={() => { if (apiKey.trim().length >= 8) handleValidateKey(apiKey.trim(), provider) }}
              autoComplete="off"
              title="Paste your API key here"
            />
            {keyValid === true && <span style={{ position: 'absolute', right: 10, top: '50%', transform: 'translateY(-50%)', color: 'var(--success)', fontSize: 14 }} aria-label="Key valid">✓</span>}
            {keyValid === false && <span style={{ position: 'absolute', right: 10, top: '50%', transform: 'translateY(-50%)', color: 'var(--error)', fontSize: 14 }} aria-label="Key invalid">✗</span>}
            {keyValidating && <span style={{ position: 'absolute', right: 10, top: '50%', transform: 'translateY(-50%)', color: 'var(--text-secondary)', fontSize: 11 }}>checking…</span>}
          </div>
        )}

        <select className="model-select" value={model} onChange={(e) => setModel(e.target.value)} disabled={models.length === 0} title="The specific AI model — larger models are slower but more capable">
          {models.length > 0 ? models.map((m) => (
            <option key={m.value} value={m.value}>{m.label}</option>
          )) : (
            <option value="">{backendReachable ? 'Loading models…' : 'Backend offline'}</option>
          )}
        </select>
        {!backendReachable && (
          <p style={{ color: 'var(--warning)', fontSize: 11, margin: '4px 0 0' }}>Cannot reach the server — run <code style={{ fontSize: 11, background: 'var(--bg-tertiary)', padding: '1px 4px', borderRadius: 3 }}>start.bat</code> (Windows) or <code style={{ fontSize: 11, background: 'var(--bg-tertiary)', padding: '1px 4px', borderRadius: 3 }}>./start.sh</code> (Mac/Linux) to start it.</p>
        )}
        {backendReachable && modelsLoaded && models.length === 0 && (
          <p style={{ color: 'var(--error)', fontSize: 11, margin: '4px 0 0' }}>No models available for this provider.</p>
        )}
        <select className="model-select" value={engine} onChange={(e) => { setEngine(e.target.value); setExecutionTarget(getDefaultTarget(e.target.value)) }} disabled={agentRunning} title="How the agent interacts with the computer">
          {engineList.length > 0 ? engineList.map(e => {
            const selectedModel = fetchedModels.find(m => m.model_id === model)
            const supported = !selectedModel || (
              (e.value === 'playwright_mcp' && selectedModel.supports_playwright_mcp !== false) ||
              (e.value === 'omni_accessibility' && selectedModel.supports_accessibility !== false) ||
              (e.value === 'computer_use' && selectedModel.supports_computer_use !== false)
            )
            return <option key={e.value} value={e.value} disabled={!supported}>{e.label}{!supported ? ' (not supported by this model)' : ''}</option>
          }) : (
            <>
              <option value="playwright_mcp">Browser (Semantic)</option>
              <option value="omni_accessibility">Desktop (Accessibility)</option>
              <option value="computer_use">Computer Use (Native)</option>
            </>
          )}
        </select>
        {ENGINE_HELP[engine] && (
          <p style={{ color: 'var(--text-secondary)', fontSize: 11, margin: '4px 0 0', lineHeight: 1.4 }}>{ENGINE_HELP[engine]}</p>
        )}

        {/* B-26: Progressive disclosure — advanced settings */}
        <button onClick={() => setShowAdvanced(!showAdvanced)}
          style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-secondary)', fontSize: 11, padding: 0, display: 'flex', alignItems: 'center', gap: 4, width: '100%', textAlign: 'left', marginTop: 8 }}
          aria-expanded={showAdvanced}
        >
          <span style={{ transform: showAdvanced ? 'rotate(90deg)' : 'rotate(0)', transition: 'transform 0.15s', display: 'inline-block' }}>▶</span>
          Advanced Settings
        </button>
        {showAdvanced && (
          <>
        {ENGINES_WITH_TARGET.includes(engine) && (
          <>
            <label style={{ display: 'block', fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.5px', color: 'var(--text-secondary)', marginTop: 8, marginBottom: 4 }}>Run location</label>
            <select
              className="model-select"
              style={{ marginTop: 0 }}
              value={executionTarget}
              onChange={(e) => setExecutionTarget(e.target.value)}
              disabled={agentRunning}
              title="Where to execute the automation — your machine or the Docker sandbox"
            >
              <option value="local">This machine</option>
              <option value="docker">Docker container</option>
            </select>
          </>
        )}
          </>
        )}
        <Link to="/workbench" className="btn btn-secondary" style={{ textAlign: 'center', marginTop: 6, display: 'block', textDecoration: 'none' }}>
          Open Workbench →
        </Link>
      </div>

      {/* Task Input */}
      <div className="panel-section">
        <h3>Task</h3>
        <textarea
          className="task-input"
          placeholder="Describe what the agent should do...&#10;&#10;e.g., Open Chrome and search for 'weather in New York'"
          value={task}
          onChange={(e) => setTask(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={agentRunning}
          title="Plain English description of the task for the agent"
        />
        {!task.trim() && !agentRunning && (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 6 }}>
            {SAMPLE_TASKS.map((sample, i) => (
              <button
                key={i}
                onClick={() => setTask(sample)}
                style={{
                  padding: '3px 8px', fontSize: 11, borderRadius: 4,
                  border: '1px solid var(--border)', cursor: 'pointer',
                  background: 'var(--bg-tertiary)', color: 'var(--text-secondary)',
                  whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: '100%',
                }}
                title={sample}
              >
                {sample}
              </button>
            ))}
          </div>
        )}
        <div className="step-info" style={{ marginTop: 8 }}>
          <span>
            Max steps: <strong>{maxSteps}</strong>
            {estimateCost(model, Number(maxSteps)) && (
              <span style={{ color: 'var(--text-secondary)', fontSize: 11, marginLeft: 8 }} title="Rough API cost estimate">
                (~${estimateCost(model, Number(maxSteps))})
              </span>
            )}
          </span>
          <input
            type="number"
            className="max-steps-input"
            min={1}
            max={200}
            value={maxSteps}
            onChange={(e) => setMaxSteps(e.target.value)}
            disabled={agentRunning}
            title="Maximum number of actions the agent can take before stopping"
          />
        </div>
        {error && <p style={{ color: 'var(--error)', fontSize: 12, marginTop: 6 }}>{error}</p>}
        {preflightWarnings && preflightWarnings.length > 0 && (
          <div style={{ background: 'var(--bg-tertiary)', border: '1px solid var(--warning, #fbbf24)', borderRadius: 4, padding: '6px 8px', marginTop: 6, fontSize: 11 }}>
            <strong style={{ color: 'var(--warning, #fbbf24)' }}>Pre-flight warnings:</strong>
            <ul style={{ margin: '4px 0 0', paddingLeft: 16 }}>
              {preflightWarnings.map((w, i) => <li key={i} style={{ color: 'var(--text-secondary)' }}>{w}</li>)}
            </ul>
          </div>
        )}
        <div className="btn-row">
          <button
            className="btn btn-primary"
            disabled={agentRunning || models.length === 0}
            onClick={handleStart}
            title="Start the agent (Ctrl+Enter)"
          >
            {agentRunning ? 'Running...' : !backendReachable ? 'Backend Offline' : models.length === 0 ? 'No Models Loaded' : 'Start Agent (Ctrl+Enter)'}
          </button>
          <button className="btn btn-danger" disabled={!agentRunning} onClick={handleStop}>
            Stop
          </button>
          {steps.length > 0 && !agentRunning && (
            <button className="btn btn-secondary" onClick={handleExportSession} title="Export session as JSON" aria-label="Export session" style={{ flex: 'none', padding: '10px 12px' }}>
              📦
            </button>
          )}
        </div>

        {/* Session result card */}
        {sessionResult && !agentRunning && (
          <div style={{ background: 'var(--bg-tertiary)', border: `1px solid ${sessionResult.status === 'error' ? 'var(--error, #f44336)' : 'var(--success, #34d399)'}`, borderRadius: 6, padding: '10px 12px', marginTop: 8 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <strong style={{ fontSize: 13, color: sessionResult.status === 'error' ? 'var(--error, #f44336)' : 'var(--success, #34d399)' }}>
                {sessionResult.status === 'error' ? '❌ Task failed' : '✅ Task completed'}
              </strong>
              <button onClick={() => setSessionResult(null)} style={{ background: 'none', border: 'none', color: 'var(--text-secondary)', cursor: 'pointer', fontSize: 14, padding: '0 2px', lineHeight: 1 }} aria-label="Dismiss result" title="Dismiss">✕</button>
            </div>
            <p style={{ fontSize: 12, color: 'var(--text-secondary)', margin: '6px 0 0' }}>
              {sessionResult.steps} step{sessionResult.steps !== 1 ? 's' : ''}
              {estimateCost(model, sessionResult.steps) && ` · ~$${estimateCost(model, sessionResult.steps)}`}
            </p>
            {steps.length > 0 && (
              <button onClick={handleExportSession} style={{ marginTop: 8, padding: '5px 10px', fontSize: 11, borderRadius: 4, border: '1px solid var(--border)', cursor: 'pointer', background: 'var(--bg-secondary)', color: 'var(--text-primary)' }} title="Export session as JSON">
                📦 Export Session
              </button>
            )}
          </div>
        )}

        {/* Safety confirmation dialog */}
        {safetyPrompt && agentRunning && (
          <div style={{ background: 'var(--bg-tertiary)', border: '1px solid var(--warning, #fbbf24)', borderRadius: 6, padding: '10px 12px', marginTop: 8 }}>
            <strong style={{ fontSize: 13, color: 'var(--warning, #fbbf24)', display: 'block', marginBottom: 6 }}>⚠️ Action requires approval</strong>
            <p style={{ fontSize: 12, color: 'var(--text-secondary)', margin: '0 0 8px', lineHeight: 1.5 }}>{safetyPrompt.explanation}</p>
            <div style={{ display: 'flex', gap: 6 }}>
              <button
                onClick={async () => { await confirmSafety(safetyPrompt.sessionId, true); clearSafetyPrompt() }}
                style={{ padding: '5px 12px', fontSize: 12, borderRadius: 4, border: '1px solid var(--success, #34d399)', cursor: 'pointer', background: 'var(--success, #34d399)', color: '#000', fontWeight: 600 }}
              >Allow</button>
              <button
                onClick={async () => { await confirmSafety(safetyPrompt.sessionId, false); clearSafetyPrompt() }}
                style={{ padding: '5px 12px', fontSize: 12, borderRadius: 4, border: '1px solid var(--border)', cursor: 'pointer', background: 'var(--bg-secondary)', color: 'var(--text-primary)' }}
              >Deny</button>
            </div>
            <p style={{ fontSize: 10, color: 'var(--text-secondary)', margin: '6px 0 0' }}>The action will be automatically denied if not approved within 30 seconds.</p>
          </div>
        )}
      </div>

      {/* Action History */}
      <div className="panel-section" style={{ flexShrink: 0 }}>
        <h3>Action History ({steps.length}){maxSteps > 0 && agentRunning && ` / ${maxSteps}`}</h3>
        {agentRunning && steps.length > 0 && (
          <div className="progress-bar">
            <div
              className="progress-fill"
              style={{ width: `${Math.min((steps.length / maxSteps) * 100, 100)}%` }}
            />
          </div>
        )}
        {!agentRunning && steps.length > 0 && estimateCost(model, steps.length) && (
          <p style={{ color: 'var(--text-secondary)', fontSize: 11, marginTop: 4 }}>
            ~{steps.length} steps · est. ~${estimateCost(model, steps.length)}
          </p>
        )}
      </div>
      <div className="action-list">
        {steps.length === 0 && (
          <p style={{ color: 'var(--text-secondary)', fontSize: 13, padding: '8px 0' }}>
            No steps yet. Start the agent to begin.
          </p>
        )}
        {steps.map((step, i) => (
          <div key={i} className={`action-item ${step.error ? 'has-error' : ''}`}>
            <span className="action-step">#{step.step_number}</span>
            <div className="action-details">
              <div className="action-header">
                <span className={`action-badge action-badge--${step.action?.action || 'unknown'}`}>
                  {step.action?.action || 'unknown'}
                </span>
                {step.action?.coordinates && (
                  <span className="action-coords">
                    ({step.action.coordinates[0]}, {step.action.coordinates[1]})
                  </span>
                )}
                {step.action?.text && step.action.action !== 'done' && (
                  <span className="action-text" title={step.action.text}>
                    &ldquo;{step.action.text.length > 30 ? step.action.text.slice(0, 30) + '...' : step.action.text}&rdquo;
                  </span>
                )}
              </div>
              {step.action?.reasoning && (
                <p className="action-reasoning">{step.action.reasoning}</p>
              )}
              {step.error && (
                <p className="action-reasoning" style={{ color: 'var(--error)' }}>
                  {step.error}
                </p>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
