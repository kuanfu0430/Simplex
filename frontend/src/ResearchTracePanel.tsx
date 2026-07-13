import type { ReactNode } from 'react'
import { CheckCircle2, ChevronRight, FileSearch, Globe2, Search } from 'lucide-react'
import type { ResearchTrace, ResearchTraceChunk, ResearchTraceSource } from './types'
import type { Translator } from './i18n'

interface Props {
  trace: ResearchTrace
  t: Translator
}

const stageKeys = {
  planning: 'tracePlanning',
  direct_crawl: 'traceDirectCrawl',
  searching: 'traceSearching',
  url_judge: 'traceUrlJudge',
  crawling: 'traceCrawling',
  chunk_judge: 'traceChunkJudge',
  evidence: 'traceEvidence',
  answering: 'traceAnswering',
  complete: 'traceComplete',
} as const

function SourceList({ sources }: { sources: ResearchTraceSource[] }) {
  return (
    <ul className="trace-source-list">
      {sources.map((source) => <li key={source.url || source.title}><a href={source.url} target="_blank" rel="noreferrer">{source.title || source.url}</a></li>)}
    </ul>
  )
}

function ChunkList({ chunks }: { chunks: ResearchTraceChunk[] }) {
  return (
    <ul className="trace-chunk-list">
      {chunks.map((chunk) => <li key={chunk.chunk_id}>
        <a href={chunk.source_url} target="_blank" rel="noreferrer"><strong>{chunk.chunk_id}</strong><span>{chunk.title || chunk.source_url}</span></a>
        {chunk.preview && <p>{chunk.preview}</p>}
      </li>)}
    </ul>
  )
}

function TraceGroup({ title, icon, children, collapsible = false }: { title: string; icon: ReactNode; children: ReactNode; collapsible?: boolean }) {
  if (collapsible) {
    return <details className="trace-group trace-group-collapsible"><summary className="trace-group-summary"><span className="trace-group-title">{icon}<span>{title}</span></span><ChevronRight size={14} /></summary><div className="trace-group-content">{children}</div></details>
  }
  return <section className="trace-group"><h4>{icon}{title}</h4>{children}</section>
}

export function ResearchTracePanel({ trace, t }: Props) {
  const directSources = trace.direct_sources || []
  const directChunks = trace.direct_chunks || []
  const hasDirectTrace = directSources.length > 0 || directChunks.length > 0
  if (!trace.queries.length && !hasDirectTrace) return null
  const phaseEntries = Object.keys(stageKeys) as Array<keyof typeof stageKeys>
  const activeIndex = phaseEntries.indexOf(trace.stage)

  return (
    <section className="research-trace liquid-card" aria-live="polite">
      <header className="trace-header"><div><span className="eyebrow">{t('researchTrace')}</span><h2>{t(stageKeys[trace.stage])}</h2></div><span className="trace-live-dot" /></header>
      <ol className="trace-phases" aria-label={t('researchTrace')}>
        {phaseEntries.filter((stage) => !['planning', 'complete'].includes(stage) && (stage !== 'direct_crawl' || hasDirectTrace)).map((stage) => <li className={phaseEntries.indexOf(stage) <= activeIndex ? 'complete' : ''} key={stage}>{t(stageKeys[stage])}</li>)}
      </ol>
      {hasDirectTrace && <div className="trace-direct-context">
        {directSources.length > 0 && <TraceGroup title={t('traceDirectSources')} icon={<Globe2 size={14} />}><SourceList sources={directSources} /></TraceGroup>}
        {directChunks.length > 0 && <TraceGroup title={t('traceDirectChunks')} icon={<CheckCircle2 size={14} />} collapsible><ChunkList chunks={directChunks} /></TraceGroup>}
      </div>}
      <div className="trace-query-list">
        {trace.queries.map((query) => <details className="trace-query" key={query.query}>
          <summary><span className="trace-query-icon"><Search size={15} /></span><strong>{query.query}</strong><span className="trace-query-count">{query.sources.length}</span><ChevronRight size={16} /></summary>
          <div className="trace-query-body">
            <TraceGroup title={t('traceSources')} icon={<Globe2 size={14} />}>
              {query.sources.length ? <SourceList sources={query.sources} /> : <p className="trace-empty">{t('traceNoSources')}</p>}
            </TraceGroup>
            {query.selected_sources.length > 0 && <TraceGroup title={t('traceSelectedSources')} icon={<FileSearch size={14} />}><SourceList sources={query.selected_sources} /></TraceGroup>}
            {query.final_chunks.length > 0 && <TraceGroup title={t('traceFinalChunks')} icon={<CheckCircle2 size={14} />} collapsible><ChunkList chunks={query.final_chunks} /></TraceGroup>}
          </div>
        </details>)}
      </div>
    </section>
  )
}
