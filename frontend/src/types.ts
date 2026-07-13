export type Theme = 'dark' | 'light'
export type Language = 'en' | 'zh-TW'
export type SearchMode = 'web' | 'academic' | 'social'
export type ResearchMode = 'instant' | 'fast' | 'full'

export interface LlmProvider {
  id: string
  name: string
  base_url: string
  models_path: string
  chat_endpoint: string
  api_key: string
  has_api_key?: boolean
  enabled: boolean
  custom: boolean
}

export interface ModelPoolEntry {
  provider_id: string
  model: string
  name: string
}

export interface SearchProvider {
  id?: string
  name: string
  enabled: boolean
  base_url: string
  api_key: string
  has_api_key?: boolean
  method?: 'GET' | 'POST'
  query_param?: string
  count_param?: string
  auth_mode?: 'bearer' | 'header' | 'query' | 'none'
  auth_name?: string
  result_path?: string
  per_query?: number
  modes?: SearchMode[]
  fields?: Record<string, string>
  custom?: boolean
}

export interface AppSettings {
  ui: { theme: Theme; scale: number; language: Language }
  llm: {
    providers: LlmProvider[]
    model_pool: ModelPoolEntry[]
    question_model: { provider_id: string; model: string }
    judge_model: { provider_id: string; model: string }
  }
  search: {
    engine_mode: 'searxng' | 'custom'
    providers: Record<string, SearchProvider>
    custom: SearchProvider[]
  }
}

export interface SearchResult {
  answer: string
  question: string
  standalone_question?: string
  research_strategy?: 'reuse' | 'direct' | 'hybrid' | 'research'
  context_capsule?: string
  search_queries: string[]
  search_mode: SearchMode
  mode: ResearchMode
  completion_state: string
  elapsed_ms?: number
  timings?: {
    planning_ms: number
    research_ms: number
    direct_crawl_ms?: number
    direct_judge_ms?: number
    answer_first_token_ms: number | null
    answer_ms: number | null
    total_ms: number | null
  }
  summary: Record<string, number | string | null>
  sources: Array<{ source_index?: number; citation_id?: string; title?: string; url: string; citation_marker?: string }>
  evidence_bundle: unknown[]
  error?: string | null
}

export interface Health {
  status: string
  version: string
  searxng: { status: string; latency_ms?: number; message: string }
  crawler: { status: string; chromium_command?: string | null; tesseract?: string | null }
}

export type ResearchTraceStage = 'planning' | 'direct_crawl' | 'searching' | 'url_judge' | 'crawling' | 'chunk_judge' | 'evidence' | 'answering' | 'complete'

export interface ResearchTraceSource {
  title: string
  url: string
}

export interface ResearchTraceChunk {
  chunk_id: string
  title: string
  source_url: string
  from_query: string
  preview: string
}

export interface ResearchTraceQuery {
  query: string
  sources: ResearchTraceSource[]
  selected_sources: ResearchTraceSource[]
  final_chunks: ResearchTraceChunk[]
}

export interface ResearchTrace {
  stage: ResearchTraceStage
  queries: ResearchTraceQuery[]
  direct_sources: ResearchTraceSource[]
  direct_chunks: ResearchTraceChunk[]
}

export interface ResearchTraceEvent {
  type: 'plan' | 'search_results' | 'url_selection' | 'crawl_complete' | 'final_evidence' | 'direct_sources' | 'direct_evidence' | 'refresh_sources' | 'stage'
  stage?: ResearchTraceStage
  round?: number
  queries?: Array<{ query: string; sources?: ResearchTraceSource[] }>
  sources?: ResearchTraceSource[]
  chunks?: ResearchTraceChunk[]
}

export type ResearchHistoryStatus = 'running' | 'complete' | 'stopped' | 'error'

export interface ResearchHistoryItem {
  id: string
  title: string
  question: string
  search_mode: SearchMode
  mode: ResearchMode
  model_selection?: ModelPoolEntry
  created_at: string
  updated_at: string
  status: ResearchHistoryStatus
  result?: SearchResult
  trace?: ResearchTrace
}

export interface ConversationMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface ConversationTurn {
  id: string
  question: string
  search_mode: SearchMode
  mode: ResearchMode
  model_selection?: ModelPoolEntry
  force_research?: boolean
  created_at: string
  updated_at: string
  status: ResearchHistoryStatus
  status_message?: string
  warning?: string
  error?: string
  result?: SearchResult
  trace?: ResearchTrace
}

export interface ResearchConversation {
  id: string
  title: string
  created_at: string
  updated_at: string
  turns: ConversationTurn[]
}
