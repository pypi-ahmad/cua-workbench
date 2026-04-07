/**
 * useAgentConfig — shared hook for model, engine, and key-status fetching.
 * B-27: Eliminates duplicated mount-time setup between ControlPanel and Workbench.
 */
import { useState, useEffect, useCallback, useMemo } from 'react'
import { getKeyStatuses, getEngines, getModels, validateApiKey } from '../api'

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
  const toOption = (m) => ({ value: m.model_id, label: `${m.display_name} (${m.model_id})` })
  const googleModels = useMemo(() => fetchedModels.filter(m => m.provider === 'google').map(toOption), [fetchedModels])
  const anthropicModels = useMemo(() => fetchedModels.filter(m => m.provider === 'anthropic').map(toOption), [fetchedModels])
  const models = provider === 'anthropic' ? anthropicModels : googleModels

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
    const list = newProvider === 'anthropic' ? anthropicModels : googleModels
    setModel(list.length > 0 ? list[0].value : '')
    const status = keyStatuses[newProvider]
    if (status?.available) {
      setKeySource(status.source)
    } else {
      setKeySource('ui')
    }
    setKeyValid(null)
  }, [anthropicModels, googleModels, keyStatuses])

  // Key validation
  const handleValidateKey = useCallback(async (keyToValidate, prov) => {
    if (!keyToValidate || keyToValidate.length < 8) { setKeyValid(null); return }
    setKeyValidating(true)
    try {
      const res = await validateApiKey(prov || provider, keyToValidate)
      setKeyValid(res.valid !== false)
    } catch { setKeyValid(null) }
    finally { setKeyValidating(false) }
  }, [provider])

  return {
    // Provider / model
    provider, setProvider: changeProvider,
    model, setModel,
    models,
    fetchedModels,
    modelsLoaded,
    backendReachable,
    googleModels,
    anthropicModels,
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
