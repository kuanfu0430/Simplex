import type { AppSettings, ConversationMessage, Health, Language, ModelPoolEntry, ResearchMode, ResearchTraceEvent, SearchMode, SearchResult } from './types'
import { createTranslator, normalizeLanguage } from './i18n'

async function readError(response: Response): Promise<string> {
  try {
    const data = await response.json()
    return data.detail || data.message || `HTTP ${response.status}`
  } catch {
    return `HTTP ${response.status}`
  }
}

export async function loadSettings(): Promise<AppSettings> {
  const response = await fetch('/api/settings')
  if (!response.ok) throw new Error(await readError(response))
  return response.json()
}

export async function saveSettings(settings: AppSettings): Promise<AppSettings> {
  const response = await fetch('/api/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ settings }),
  })
  if (!response.ok) throw new Error(await readError(response))
  return response.json()
}

export async function loadModels(providerId: string): Promise<Array<{ id: string; name: string }>> {
  const response = await fetch(`/api/llm/providers/${encodeURIComponent(providerId)}/models`)
  if (!response.ok) throw new Error(await readError(response))
  const data = await response.json()
  return data.models || []
}

export async function loadHealth(): Promise<Health> {
  const response = await fetch('/api/health')
  if (!response.ok) throw new Error(await readError(response))
  return response.json()
}

export interface SearchCallbacks {
  onStatus: (message: string, payload: Record<string, unknown>) => void
  onWarning: (message: string) => void
  onResearchTrace: (event: ResearchTraceEvent) => void
  onAnswerStart: (result: SearchResult) => void
  onAnswerDelta: (delta: string) => void
  onResult: (result: SearchResult) => void
}

export interface ConversationContext {
  history: ConversationMessage[]
  capsules: string[]
  forceResearch: boolean
  turnId: string
}

function parseEventBlock(block: string): { event: string; data: unknown } | null {
  let event = 'message'
  const dataLines: string[] = []
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
  }
  if (!dataLines.length) return null
  try {
    return { event, data: JSON.parse(dataLines.join('\n')) }
  } catch {
    return { event, data: dataLines.join('\n') }
  }
}

function waitForNextPaint(): Promise<void> {
  return new Promise((resolve) => {
    if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
      window.requestAnimationFrame(() => resolve())
      return
    }
    setTimeout(resolve, 0)
  })
}

export async function runSearch(
  question: string,
  searchMode: SearchMode,
  mode: ResearchMode,
  callbacks: SearchCallbacks,
  signal?: AbortSignal,
  language: Language = 'en',
  modelSelection?: ModelPoolEntry,
  context?: ConversationContext,
): Promise<void> {
  const t = createTranslator(normalizeLanguage(language))
  const response = await fetch('/api/search/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify({
      question,
      search_mode: searchMode,
      mode,
      model_selection: modelSelection,
      conversation_history: context?.history || [],
      context_capsules: context?.capsules || [],
      force_research: context?.forceResearch || false,
      turn_id: context?.turnId || '',
    }),
    signal,
  })
  if (!response.ok || !response.body) throw new Error(await readError(response))

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let pendingAnswerDelta = ''
  const flushAnswerDelta = async () => {
    if (!pendingAnswerDelta) return
    const delta = pendingAnswerDelta
    pendingAnswerDelta = ''
    callbacks.onAnswerDelta(delta)
    // React 18 會合併同一個 reader.read() 裡的 state 更新；等待一個 frame 才能真正逐段繪製。
    await waitForNextPaint()
  }
  while (true) {
    const { value, done } = await reader.read()
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done })
    const blocks = buffer.split('\n\n')
    buffer = blocks.pop() || ''
    for (const block of blocks) {
      const parsed = parseEventBlock(block)
      if (!parsed) continue
      const payload = (parsed.data || {}) as Record<string, unknown>
      if (parsed.event === 'status') callbacks.onStatus(String(payload.message || t('preparingSearch')), payload)
      if (parsed.event === 'warning') callbacks.onWarning(String(payload.message || t('degradedWarning')))
      if (parsed.event === 'error') throw new Error(String(payload.message || t('searchFailed')))
      if (parsed.event === 'research_trace') {
        callbacks.onResearchTrace(payload as unknown as ResearchTraceEvent)
        await waitForNextPaint()
      }
      if (parsed.event === 'answer_start') callbacks.onAnswerStart(payload as unknown as SearchResult)
      if (parsed.event === 'answer_delta') {
        pendingAnswerDelta += String(payload.delta || '')
        if (pendingAnswerDelta.length >= 12 || /[\n。！？.!?]\s*$/.test(pendingAnswerDelta)) {
          await flushAnswerDelta()
        }
      }
      if (parsed.event === 'result') {
        await flushAnswerDelta()
        callbacks.onResult(payload as unknown as SearchResult)
      }
    }
    await flushAnswerDelta()
    if (done) break
  }
}
