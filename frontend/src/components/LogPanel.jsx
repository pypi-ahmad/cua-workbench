import { useEffect, useRef, useState } from 'react'

export default function LogPanel({ logs, onClear }) {
  const scrollRef = useRef(null)
  const [levelFilter, setLevelFilter] = useState({ info: true, warning: true, error: true, debug: true })

  const filteredLogs = logs.filter(log => levelFilter[log.level] !== false)

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [filteredLogs])

  const formatTime = (ts) => {
    try {
      const d = new Date(ts)
      return d.toLocaleTimeString('en-US', { hour12: false })
    } catch {
      return '--:--:--'
    }
  }

  const toggleLevel = (level) => {
    setLevelFilter(prev => ({ ...prev, [level]: !prev[level] }))
  }

  return (
    <div className="bottom-panel">
      <div className="bottom-panel-header">
        <h3>Logs ({filteredLogs.length})</h3>
        <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
          {['info', 'warning', 'error', 'debug'].map(level => (
            <button
              key={level}
              onClick={() => toggleLevel(level)}
              aria-label={`${levelFilter[level] ? 'Hide' : 'Show'} ${level} logs`}
              aria-pressed={levelFilter[level]}
              style={{
                padding: '1px 6px', fontSize: 10, borderRadius: 3,
                border: '1px solid var(--border)', cursor: 'pointer',
                textTransform: 'uppercase', fontWeight: 600,
                background: levelFilter[level] ? 'var(--bg-tertiary)' : 'transparent',
                opacity: levelFilter[level] ? 1 : 0.6,
                color: level === 'info' ? 'var(--info)' : level === 'warning' ? 'var(--warning)' : level === 'error' ? 'var(--error)' : 'var(--text-secondary)',
              }}
            >
              {level}
            </button>
          ))}
          <button className="clear-logs-btn" onClick={onClear} aria-label="Clear all logs">
            Clear
          </button>
        </div>
      </div>
      <div className="log-container" ref={scrollRef} role="log" aria-live="polite">
        {filteredLogs.length === 0 && (
          <div className="log-entry">
            <span className="log-message" style={{ color: 'var(--text-secondary)' }}>
              {logs.length === 0 ? 'Waiting for logs...' : 'No logs match the current filter.'}
            </span>
          </div>
        )}
        {filteredLogs.map((log, i) => (
          <div key={i} className="log-entry">
            <span className="log-time">{formatTime(log.timestamp)}</span>
            <span className={`log-level ${log.level}`}>{log.level}</span>
            <span className="log-message">{log.message}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
