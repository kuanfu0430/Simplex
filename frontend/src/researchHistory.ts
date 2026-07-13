import type { ConversationTurn, ResearchConversation, ResearchHistoryItem, SearchResult } from './types'

const V1_STORAGE_KEY = 'simplex:research-history:v1'
const STORAGE_KEY = 'simplex:conversations:v2'
const MAX_CONVERSATIONS = 24

function compactResult(result: SearchResult): SearchResult {
  // 原始 evidence 僅在目前頁面記憶體存在；重新開啟時使用後端簽發的膠囊。
  return { ...result, evidence_bundle: [] }
}

function isConversation(value: unknown): value is ResearchConversation {
  if (!value || typeof value !== 'object') return false
  const item = value as Partial<ResearchConversation>
  return typeof item.id === 'string' && typeof item.title === 'string' && Array.isArray(item.turns)
}

function legacyToConversation(item: ResearchHistoryItem): ResearchConversation {
  const turn: ConversationTurn = {
    id: item.id,
    question: item.question,
    search_mode: item.search_mode,
    mode: item.mode,
    model_selection: item.model_selection,
    created_at: item.created_at,
    updated_at: item.updated_at,
    status: item.status,
    result: item.result ? compactResult(item.result) : undefined,
    trace: item.trace,
  }
  return {
    id: item.id,
    title: item.title || item.question,
    created_at: item.created_at,
    updated_at: item.updated_at,
    turns: [turn],
  }
}

function compactConversation(conversation: ResearchConversation, dropHeavy = false): ResearchConversation {
  return {
    ...conversation,
    turns: conversation.turns.map((turn) => ({
      ...turn,
      trace: dropHeavy ? undefined : turn.trace,
      result: turn.result
        ? {
          ...compactResult(turn.result),
          context_capsule: dropHeavy ? undefined : turn.result.context_capsule,
        }
        : undefined,
    })),
  }
}

function sortConversations(items: ResearchConversation[]): ResearchConversation[] {
  return [...items].sort((left, right) => right.updated_at.localeCompare(left.updated_at)).slice(0, MAX_CONVERSATIONS)
}

export function loadConversations(): ResearchConversation[] {
  if (typeof window === 'undefined') return []
  try {
    const saved = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || 'null')
    if (Array.isArray(saved)) return sortConversations(saved.filter(isConversation))
  } catch {
    // 改讀舊格式，讓既有研究至少能以單輪對話回看。
  }
  try {
    const legacy = JSON.parse(window.localStorage.getItem(V1_STORAGE_KEY) || '[]')
    if (!Array.isArray(legacy)) return []
    return sortConversations(
      legacy
        .filter((item): item is ResearchHistoryItem => Boolean(item && typeof item.id === 'string' && typeof item.question === 'string'))
        .map(legacyToConversation),
    )
  } catch {
    return []
  }
}

export function saveConversations(items: ResearchConversation[]): void {
  if (typeof window === 'undefined') return
  const compact = sortConversations(items).map((item) => compactConversation(item))
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(compact))
    return
  } catch {
    // 儲存空間不足時，先丟棄可重建的軌跡與證據膠囊，保留問答歷史。
  }
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(compact.map((item) => compactConversation(item, true))))
    return
  } catch {
    // 最後才減少最舊的對話，避免一次搜尋使整份本機歷史失效。
  }
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(compact.slice(0, 8).map((item) => compactConversation(item, true))))
  } catch {
    // 瀏覽器封鎖本機儲存時，當前工作階段仍可繼續使用。
  }
}
