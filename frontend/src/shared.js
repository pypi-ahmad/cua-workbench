/**
 * Shared constants and helpers used by the Workbench.
 */

export const ENGINE_HELP = {
  playwright_mcp: 'Best for web tasks — clicks by element name, not pixel coordinates.',
  omni_accessibility: 'Best for desktop apps — uses the system accessibility tree.',
  computer_use: 'Best when the model needs native screen control.',
}

export const SAMPLE_TASKS = [
  "Search Google for 'latest AI news'",
  'Open the file manager and list files in /tmp',
  'Take a screenshot of the desktop',
  'Go to wikipedia.org and find the main page featured article',
]

export const ENGINES_WITH_TARGET = ['playwright_mcp', 'omni_accessibility', 'computer_use']

export const getDefaultTarget = (eng) => (eng === 'playwright_mcp' ? 'local' : 'docker')

// B-25: Rough per-step cost estimates (USD) by model prefix
const COST_PER_STEP = {
  'gemini-2.5-pro': 0.02,
  'gemini-2.5-flash': 0.003,
  'gemini-3-flash': 0.003,
  'gemini-2.0-flash': 0.003,
  'claude-sonnet': 0.015,
  'claude-opus': 0.075,
  'claude-haiku': 0.003,
}

export function estimateCost(modelId, stepCount) {
  if (!modelId || stepCount <= 0) return null
  for (const [prefix, cost] of Object.entries(COST_PER_STEP)) {
    if (modelId.startsWith(prefix)) return (cost * stepCount).toFixed(3)
  }
  return null
}

export const DEFAULT_BROWSER_ENGINES = [
  { value: 'playwright_mcp', label: 'Browser Automation' },
]

export const DEFAULT_DESKTOP_ENGINES = [
  { value: 'omni_accessibility', label: 'Desktop Automation' },
  { value: 'computer_use', label: 'Full Screen Control' },
]
