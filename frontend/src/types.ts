export type Theme = 'dark' | 'light'
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
  ui: { theme: Theme; scale: number }
  llm: {
    providers: LlmProvider[]
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
  search_queries: string[]
  search_mode: SearchMode
  mode: ResearchMode
  completion_state: string
  elapsed_ms?: number
  timings?: {
    planning_ms: number
    research_ms: number
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
