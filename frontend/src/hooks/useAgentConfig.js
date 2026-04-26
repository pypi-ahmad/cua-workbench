/**
 * useAgentConfig — shared hook for model, engine, and key-status fetching.
 * B-27: Eliminates duplicated mount-time setup between ControlPanel and Workbench.
 */
import { useState, useEffect, useCallback, useMemo } from 'react'
import { getKeyStatuses, getEngines, getModels, validateApiKey } from '../api'

const PROVIDER_LABELS = {
  google: 'Google Gemini',
  anthropic: 'Anthropic Claude',
  openai: 'OpenAI',
}

const PROVIDER_ORDER = ['google', 'anthropic', 'openai']

/**
 * Returns all data needed to populate the agent config form.
 *
 * @param {string} initialProvider - default provider ('google')
 * @returns {Object}
 */
export default function useAgentConfig(initialProvider = 'google') {
  const [provider, setProvider] = useState(initialProvider)
  const [model, setModel] = useState('')
  const [fetchedModels, setFetchedModels] = useState([])
  const [modelsLoaded, setModelsLoaded] = useState(false)
  const [backendReachable, setBackendReachable] = useState(true)
  const [engineList, setEngineList] = useState([])
  const [keyStatuses, setKeyStatuses] = useState({})
  const [keySource, setKeySource] = useState('ui')
  const [apiKey, setApiKey] = useState('')
  const [keyValid, setKeyValid] = useState(null)
  const [keyValidating, setKeyValidating] = useState(false)

  // Derive per-provider model options
  const toOption = (m) => ({ value: m.model_id, label: m.display_name })
  const modelsByProvider = useMemo(() => fetchedModels.reduce((acc, entry) => {
    const providerId = entry.provider
    if (!providerId) return acc
    if (!acc[providerId]) acc[providerId] = []
    acc[providerId].push(toOption(entry))
    return acc
  }, {}), [fetchedModels])
  const providerOptions = useMemo(() => {
    const discovered = Object.keys(modelsByProvider)
    const ordered = [
      ...PROVIDER_ORDER.filter(providerId => discovered.includes(providerId)),
      ...discovered.filter(providerId => !PROVIDER_ORDER.includes(providerId)),
    ]
    const providers = ordered.length > 0 ? ordered : PROVIDER_ORDER
    return providers.map(providerId => ({
      value: providerId,
      label: PROVIDER_LABELS[providerId] || providerId,
    }))
  }, [modelsByProvider])
  const models = modelsByProvider[provider] || []

  // Fetch keys, engines, models on mount
  useEffect(() => {
    (async () => {
      try {
        const data = await getKeyStatuses()
        if (data.keys) {
          const map = {}
          data.keys.forEach(k => { map[k.provider] = k })
          setKeyStatuses(map)
          const current = map[initialProvider]
          if (current?.available) setKeySource(current.source)
        }
      } catch { /* backend not ready */ }
    })();
    (async () => {
      try {
        const data = await getEngines()
        if (data.engines?.length) setEngineList(data.engines)
      } catch { /* backend not ready */ }
    })();
    (async () => {
      try {
        const data = await getModels()
        if (data.models?.length) {
          setFetchedModels(data.models)
          setModelsLoaded(true)
          setBackendReachable(true)
          const first = data.models.find(m => m.provider === initialProvider)
          if (first) setModel(first.model_id)
        }
      } catch {
        setBackendReachable(false)
      }
    })()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Change provider → auto-select first model + best key source
  const changeProvider = useCallback((newProvider) => {
    setProvider(newProvider)
    const list = modelsByProvider[newProvider] || []
    setModel(list.length > 0 ? list[0].value : '')
    const status = keyStatuses[newProvider]
    if (status?.available) {
      setKeySource(status.source)
    } else {
      setKeySource('ui')
    }
    setKeyValid(null)
  }, [keyStatuses, modelsByProvider])

  // Key validation
  const handleValidateKey = useCallback(async (keyToValidate, prov) => {
    if (!keyToValidate || keyToValidate.length < 8) { setKeyValid(null); return }
    setKeyValidating(true)
    try {
      const res = await validateApiKey(prov || provider, keyToValidate)
      setKeyValid(res.valid === true)
    } catch (error) {
      setKeyValid(error?.status === 422 ? false : null)
    }
    finally { setKeyValidating(false) }
  }, [provider])

  return {
    // Provider / model
    provider, setProvider: changeProvider,
    model, setModel,
    models,
    providerOptions,
    fetchedModels,
    modelsLoaded,
    backendReachable,
    // Engine
    engineList,
    // Key
    keyStatuses,
    keySource, setKeySource,
    apiKey, setApiKey,
    keyValid, setKeyValid,
    keyValidating,
    handleValidateKey,
  }
}
