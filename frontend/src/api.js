const API_BASE = '/api'

function generateRequestId() {
  return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

function buildRequestError(status, payload, fallbackMessage) {
  const message =
    payload && typeof payload === 'object'
      ? payload.error || payload.detail || fallbackMessage
      : payload || fallbackMessage

  const error = new Error(message || fallbackMessage)
  error.status = status
  if (payload && typeof payload === 'object') {
    error.detail = payload.detail
    error.requestId = payload.request_id
  }
  return error
}

async function request(path, options = {}) {
  const requestId = generateRequestId()
  const headers = { 'Content-Type': 'application/json', 'X-Request-ID': requestId, ...options.headers }
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers,
  })
  const ct = res.headers.get('content-type') || ''
  const rawBody = await res.text()
  let payload = rawBody

  if (ct.includes('application/json') && rawBody) {
    try {
      payload = JSON.parse(rawBody)
    } catch {
      payload = rawBody
    }
  }

  if (!res.ok) {
    throw buildRequestError(res.status, payload, res.statusText)
  }
  if (ct.includes('application/json')) {
    return payload || {}
  }
  return rawBody
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

export async function startAgent({ task, apiKey, model, maxSteps, mode, engine, provider, executionTarget }) {
  // Bypass generic request() — validation errors return HTTP 400/429 with JSON body.
  // Always returns { error?: string, ... } so callers can inspect data.error without catching.
  try {
    const requestId = generateRequestId()
    const res = await fetch(`${API_BASE}/agent/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Request-ID': requestId },
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

export async function issueWsToken(sessionId) {
  return request('/session/ws-token', {
    method: 'POST',
    body: JSON.stringify({ session_id: sessionId }),
  })
}

export async function stopAgent(sessionId) {
  return request(`/agent/stop/${sessionId}`, { method: 'POST' })
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
  return request('/keys/validate', {
    method: 'POST',
    body: JSON.stringify({ provider, api_key: apiKey }),
  })
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

export async function confirmSafety(sessionId, confirm) {
  return request('/agent/safety-confirm', {
    method: 'POST',
    body: JSON.stringify({ session_id: sessionId, confirm }),
  })
}
