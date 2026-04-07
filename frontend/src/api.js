const API_BASE = '/api'

async function request(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    let message
    try {
      message = await res.text()
    } catch {
      message = res.statusText
    }
    throw new Error(message || res.statusText)
  }
  const ct = res.headers.get('content-type') || ''
  if (ct.includes('application/json')) {
    return res.json()
  }
  return res.text()
}

export async function getHealth() {
  return request('/health')
}

export async function getContainerStatus() {
  return request('/container/status')
}

export async function startContainer() {
  return request('/container/start', { method: 'POST' })
}

export async function stopContainer() {
  return request('/container/stop', { method: 'POST' })
}

export async function buildImage() {
  return request('/container/build', { method: 'POST' })
}

export async function startAgent({ task, apiKey, model, maxSteps, mode, engine, provider, executionTarget }) {
  // Bypass generic request() — validation errors return HTTP 400/429 with JSON body.
  // Always returns { error?: string, ... } so callers can inspect data.error without catching.
  try {
    const res = await fetch(`${API_BASE}/agent/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        task,
        api_key: apiKey,
        model,
        max_steps: maxSteps,
        mode,
        engine,
        provider,
        execution_target: executionTarget || 'local',
      }),
    })

    const ct = res.headers.get('content-type') || ''
    let data
    if (ct.includes('application/json')) {
      data = await res.json()
    } else {
      const text = await res.text()
      data = { error: text || res.statusText }
    }

    // Normalize non-2xx into error if backend didn't already provide one
    if (!res.ok && !data?.error) {
      data = { ...data, error: res.statusText }
    }

    return data
  } catch (e) {
    return { error: String(e?.message || e) }
  }
}

export async function stopAgent(sessionId) {
  return request(`/agent/stop/${sessionId}`, { method: 'POST' })
}

export async function setAgentMode(mode) {
  return request('/agent-service/mode', {
    method: 'POST',
    body: JSON.stringify({ mode }),
  })
}

export async function getAgentStatus(sessionId) {
  return request(`/agent/status/${sessionId}`)
}

export async function getScreenshot() {
  return request('/screenshot')
}

export async function getAgentServiceHealth() {
  return request('/agent-service/health')
}

export async function getKeyStatuses() {
  return request('/keys/status')
}

export async function getEngines() {
  return request('/engines')
}

export async function getModels() {
  return request('/models')
}

export async function validateApiKey(provider, apiKey) {
  try {
    return await request('/keys/validate', {
      method: 'POST',
      body: JSON.stringify({ provider, api_key: apiKey }),
    })
  } catch {
    return { valid: null, error: 'Could not validate key' }
  }
}

export async function getHealthDetailed() {
  return request('/health/detailed')
}

export async function getContainerLogs(lines = 100) {
  return request(`/container/logs?lines=${lines}`)
}

export async function getPreflight(engine, provider) {
  return request(`/preflight?engine=${encodeURIComponent(engine)}&provider=${encodeURIComponent(provider)}`)
}
