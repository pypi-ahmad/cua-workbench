import { Link } from 'react-router-dom'
import useContainerStatus from './hooks/useContainerStatus'
import Header from './components/Header'

export default function App() {
  const { containerRunning, agentServiceUp, systemHealth, refreshContainer } = useContainerStatus()

  return (
    <div className="app">
      <Header
        connected={false}
        containerRunning={containerRunning}
        agentServiceUp={agentServiceUp}
        agentRunning={false}
        onRefreshContainer={refreshContainer}
        systemHealth={systemHealth}
      />

      <div className="landing">
        <div className="landing-hero">
          <h2 className="landing-title">Let AI control a browser or desktop for you</h2>
          <p className="landing-subtitle">
            Describe a task in plain language and watch the agent carry it out inside a secure sandbox — no scripting required.
          </p>
          <Link to="/workbench" className="landing-cta">Open Workbench →</Link>
        </div>

        <div className="landing-features">
          <div className="landing-card">
            <span className="landing-icon">🌐</span>
            <h3>Browser Automation</h3>
            <p>Navigate sites, fill forms, and extract data using semantic page understanding.</p>
          </div>
          <div className="landing-card">
            <span className="landing-icon">🖥️</span>
            <h3>Desktop Automation</h3>
            <p>Interact with native apps through accessibility APIs — click, type, and read the screen.</p>
          </div>
          <div className="landing-card">
            <span className="landing-icon">🔒</span>
            <h3>Sandboxed Execution</h3>
            <p>Everything runs in an isolated sandbox. Your host machine is never touched by the agent.</p>
          </div>
        </div>

        <div className="landing-steps">
          <h3>How it works</h3>
          <ol>
            <li><strong>Choose a provider</strong> — Google Gemini, Anthropic Claude, or OpenAI</li>
            <li><strong>Paste your API key</strong> — or use a saved key from your environment</li>
            <li><strong>Describe a task</strong> — e.g. "Search Google for the weather in Tokyo"</li>
            <li><strong>Watch the agent work</strong> — live screenshots, action timeline, and logs</li>
          </ol>
        </div>
      </div>
    </div>
  )
}
