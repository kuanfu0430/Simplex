import type { ResearchTrace, ResearchTraceChunk, ResearchTraceEvent, ResearchTraceQuery, ResearchTraceSource } from './types'

export const emptyResearchTrace = (): ResearchTrace => ({ stage: 'planning', queries: [], direct_sources: [], direct_chunks: [] })

function uniqueSources(items: ResearchTraceSource[]): ResearchTraceSource[] {
  const seen = new Set<string>()
  return items.filter((item) => {
    const key = item.url || item.title
    if (!key || seen.has(key)) return false
    seen.add(key)
    return true
  })
}

function uniqueChunks(items: ResearchTraceChunk[]): ResearchTraceChunk[] {
  const seen = new Set<string>()
  return items.filter((item) => {
    const key = item.chunk_id || `${item.source_url}:${item.preview}`
    if (!key || seen.has(key)) return false
    seen.add(key)
    return true
  })
}

function ensureQuery(queries: ResearchTraceQuery[], query: string): ResearchTraceQuery[] {
  if (!query || queries.some((item) => item.query === query)) return queries
  return [...queries, { query, sources: [], selected_sources: [], final_chunks: [] }]
}

function mergeQuerySources(
  queries: ResearchTraceQuery[],
  query: string,
  field: 'sources' | 'selected_sources',
  sources: ResearchTraceSource[],
): ResearchTraceQuery[] {
  const expanded = ensureQuery(queries, query)
  return expanded.map((item) => item.query === query ? { ...item, [field]: uniqueSources([...item[field], ...sources]) } : item)
}

function mergeChunks(
  queries: ResearchTraceQuery[],
  field: 'final_chunks',
  chunks: ResearchTraceChunk[],
): ResearchTraceQuery[] {
  let expanded = queries
  for (const chunk of chunks) expanded = ensureQuery(expanded, chunk.from_query)
  return expanded.map((item) => {
    const additions = chunks.filter((chunk) => chunk.from_query === item.query)
    return additions.length ? { ...item, [field]: uniqueChunks([...item[field], ...additions]) } : item
  })
}

export function mergeResearchTrace(current: ResearchTrace, event: ResearchTraceEvent): ResearchTrace {
  const directSources = current.direct_sources || []
  const directChunks = current.direct_chunks || []
  if (event.type === 'direct_sources' || event.type === 'refresh_sources') {
    return {
      ...current,
      stage: event.stage || 'direct_crawl',
      direct_sources: uniqueSources([...directSources, ...(event.sources || [])]),
      direct_chunks: directChunks,
    }
  }
  if (event.type === 'direct_evidence') {
    return {
      ...current,
      stage: event.stage || 'chunk_judge',
      direct_sources: directSources,
      direct_chunks: uniqueChunks([...directChunks, ...(event.chunks || [])]),
    }
  }
  if (event.type === 'plan') {
    const planned = (event.queries || []).map((item) => item.query).filter(Boolean)
    return { ...current, stage: event.stage || 'searching', queries: planned.reduce(ensureQuery, current.queries), direct_sources: directSources, direct_chunks: directChunks }
  }
  if (event.type === 'search_results') {
    const queries = (event.queries || []).reduce(
      (next, item) => mergeQuerySources(next, item.query, 'sources', item.sources || []),
      current.queries,
    )
    return { ...current, stage: event.stage || 'url_judge', queries, direct_sources: directSources, direct_chunks: directChunks }
  }
  if (event.type === 'url_selection') {
    const queries = (event.queries || []).reduce(
      (next, item) => mergeQuerySources(next, item.query, 'selected_sources', item.sources || []),
      current.queries,
    )
    return { ...current, stage: event.stage || 'crawling', queries, direct_sources: directSources, direct_chunks: directChunks }
  }
  if (event.type === 'final_evidence') {
    return { ...current, stage: event.stage || 'evidence', queries: mergeChunks(current.queries, 'final_chunks', event.chunks || []), direct_sources: directSources, direct_chunks: directChunks }
  }
  return { ...current, stage: event.stage || current.stage, direct_sources: directSources, direct_chunks: directChunks }
}
