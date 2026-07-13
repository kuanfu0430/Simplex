import { useEffect, useMemo, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import { ArrowUp, BookOpen, CheckCircle2, CircleStop, Clock3, Globe2, Menu, MessageSquareText, Search, Settings, Sparkles, X } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { loadHealth, loadSettings, runSearch, saveSettings } from './api'
import { createTranslator, normalizeLanguage } from './i18n'
import { ResearchTracePanel } from './ResearchTracePanel'
import { loadConversations, saveConversations } from './researchHistory'
import { emptyResearchTrace, mergeResearchTrace } from './researchTrace'
import { SettingsPanel } from './SettingsPanel'
import type { AppSettings, ConversationMessage, ConversationTurn, Health, ModelPoolEntry, ResearchConversation, ResearchMode, ResearchTrace, SearchMode, SearchResult } from './types'

function citedAnswer(result: SearchResult, fallback: string): string {
  let answer = result.answer
  for (const source of result.sources) {
    if (!source.citation_marker || !source.url) continue
    answer = answer.split(source.citation_marker).join(`[${source.source_index || '?'}](${source.url})`)
  }
  return answer || fallback
}

function sourceHostname(url: string, fallback: string): string {
  if (!url) return fallback
  try {
    return new URL(url).hostname
  } catch {
    return fallback
  }
}

function modelKey(model: ModelPoolEntry): string {
  return `${model.provider_id}\u0000${model.model}`
}

const historyStatusKeys = {
  running: 'historyRunning',
  complete: 'historyComplete',
  stopped: 'historyStopped',
  error: 'historyError',
} as const

function historyTime(value: string, language: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.valueOf())) return ''
  return date.toLocaleString(language === 'zh-TW' ? 'zh-TW' : 'en', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function latestTurn(conversation: ResearchConversation): ConversationTurn | undefined {
  return conversation.turns[conversation.turns.length - 1]
}

function buildConversationContext(turns: ConversationTurn[]): { history: ConversationMessage[]; capsules: string[] } {
  const eligible = turns.filter((turn) => turn.question && turn.result?.answer)
  const selected = eligible.length <= 8 ? eligible : [...eligible.slice(0, 1), ...eligible.slice(-7)]
  return {
    history: selected.flatMap((turn) => [
      { role: 'user' as const, content: turn.question },
      { role: 'assistant' as const, content: turn.result?.answer || '' },
    ]),
    // 後端依此順序優先挑選新近度高、且與追問最相關的已驗證證據。
    capsules: [...selected].reverse().map((turn) => turn.result?.context_capsule || '').filter(Boolean),
  }
}

function App() {
  const [settings, setSettings] = useState<AppSettings | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [health, setHealth] = useState<Health | null>(null)
  const [question, setQuestion] = useState('')
  const [searchMode, setSearchMode] = useState<SearchMode>('web')
  const [researchMode, setResearchMode] = useState<ResearchMode>('fast')
  const [modelSelection, setModelSelection] = useState<ModelPoolEntry | undefined>()
  const [forceResearch, setForceResearch] = useState(false)
  const [busy, setBusy] = useState(false)
  const [conversations, setConversations] = useState<ResearchConversation[]>(() => loadConversations())
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null)
  const [activeTurnId, setActiveTurnId] = useState<string | null>(null)
  const [appError, setAppError] = useState('')
  const [updateReady, setUpdateReady] = useState<ServiceWorker | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const answerStartedRef = useRef(false)
  const resultRef = useRef<SearchResult | undefined>(undefined)
  const traceRef = useRef<ResearchTrace>(emptyResearchTrace())

  const language = normalizeLanguage(settings?.ui.language)
  const t = useMemo(() => createTranslator(language), [language])
  const activeConversation = conversations.find((item) => item.id === activeConversationId)
  const modeOptions = [
    { id: 'web' as SearchMode, label: t('modeWeb'), icon: Globe2 },
    { id: 'academic' as SearchMode, label: t('modeAcademic'), icon: BookOpen },
    { id: 'social' as SearchMode, label: t('modeSocial'), icon: MessageSquareText },
  ]
  const depthOptions = [
    { id: 'instant' as ResearchMode, label: t('depthInstant') },
    { id: 'fast' as ResearchMode, label: t('depthFast') },
    { id: 'full' as ResearchMode, label: t('depthFull') },
  ]
  const answerModels = useMemo(() => {
    if (!settings) return []
    const providers = new Map(settings.llm.providers.map((provider) => [provider.id, provider]))
    const candidates: ModelPoolEntry[] = [...settings.llm.model_pool]
    const defaultModel = settings.llm.question_model
    if (defaultModel.model) candidates.unshift({ provider_id: defaultModel.provider_id, model: defaultModel.model, name: defaultModel.model })
    const seen = new Set<string>()
    return candidates.filter((item) => {
      const provider = providers.get(item.provider_id)
      const key = modelKey(item)
      if (!provider?.enabled || !item.model || seen.has(key)) return false
      seen.add(key)
      return true
    }).map((item) => {
      const provider = providers.get(item.provider_id)
      return { ...item, name: `${provider?.name || item.provider_id} — ${item.name || item.model}` }
    })
  }, [settings])
  const selectedAnswerModel = answerModels.find((item) => modelKey(item) === (modelSelection ? modelKey(modelSelection) : '')) || answerModels[0]

  const patchTurn = (conversationId: string, turnId: string, patch: Partial<ConversationTurn>) => {
    const now = new Date().toISOString()
    setConversations((current) => current
      .map((conversation) => conversation.id === conversationId ? {
        ...conversation,
        updated_at: now,
        turns: conversation.turns.map((turn) => turn.id === turnId ? { ...turn, ...patch, updated_at: now } : turn),
      } : conversation)
      .sort((left, right) => right.updated_at.localeCompare(left.updated_at)))
  }

  useEffect(() => {
    loadSettings().then(setSettings).catch((reason) => setAppError(t('loadSettingsFailed', { message: reason instanceof Error ? reason.message : t('searchFailed') })))
    loadHealth().then(setHealth).catch(() => undefined)
    const timer = window.setInterval(() => loadHealth().then(setHealth).catch(() => undefined), 30000)
    return () => window.clearInterval(timer)
  }, [t])

  useEffect(() => {
    saveConversations(conversations)
  }, [conversations])

  useEffect(() => {
    if (!settings) return
    document.documentElement.dataset.theme = settings.ui.theme
    document.documentElement.lang = language === 'zh-TW' ? 'zh-Hant' : 'en'
    document.documentElement.style.setProperty('--ui-scale', String(settings.ui.scale))
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', settings.ui.theme === 'light' ? '#f5f5f7' : '#090b10')
    document.querySelector('meta[name="description"]')?.setAttribute('content', t('metaDescription'))
  }, [language, settings, t])

  useEffect(() => {
    if (!('serviceWorker' in navigator)) return
    let reloading = false
    const applyUpdate = () => {
      if (reloading) return
      reloading = true
      window.location.reload()
    }
    const register = async () => {
      const registration = await navigator.serviceWorker.register('/service-worker.js', { scope: '/' })
      await registration.update()
      if (registration.waiting) setUpdateReady(registration.waiting)
      registration.addEventListener('updatefound', () => {
        const worker = registration.installing
        worker?.addEventListener('statechange', () => {
          if (worker.state === 'installed' && navigator.serviceWorker.controller) setUpdateReady(worker)
        })
      })
    }
    navigator.serviceWorker.addEventListener('controllerchange', applyUpdate)
    window.addEventListener('load', register, { once: true })
    return () => navigator.serviceWorker.removeEventListener('controllerchange', applyUpdate)
  }, [])

  const healthLabel = useMemo(() => {
    if (!health) return { label: t('healthChecking'), className: 'unknown' }
    if (health.searxng.status === 'ok') return { label: health.searxng.latency_ms == null ? 'SearXNG' : t('healthSearxng', { latency: health.searxng.latency_ms }), className: 'ok' }
    if (health.searxng.status === 'disabled') return { label: t('healthSearxngDisabled'), className: 'unknown' }
    return { label: t('healthDegraded'), className: 'warn' }
  }, [health, t])

  const resetWorkspace = () => {
    if (busy) return
    setActiveConversationId(null)
    setActiveTurnId(null)
    setQuestion('')
    setAppError('')
    setSidebarOpen(false)
  }

  const openConversation = (conversation: ResearchConversation) => {
    if (busy) return
    const latest = latestTurn(conversation)
    setActiveConversationId(conversation.id)
    setActiveTurnId(latest?.id || null)
    setQuestion('')
    setSearchMode(latest?.search_mode || 'web')
    setResearchMode(latest?.mode || 'fast')
    setModelSelection(latest?.model_selection)
    setForceResearch(false)
    setAppError('')
    setSidebarOpen(false)
  }

  const submit = async () => {
    const prompt = question.trim()
    if (!prompt || busy) return
    const controller = new AbortController()
    const now = new Date().toISOString()
    const conversationId = activeConversation?.id || crypto.randomUUID()
    const turnId = crypto.randomUUID()
    const priorTurns = activeConversation?.turns || []
    const initialTrace = emptyResearchTrace()
    const newTurn: ConversationTurn = {
      id: turnId,
      question: prompt,
      search_mode: searchMode,
      mode: researchMode,
      model_selection: selectedAnswerModel,
      force_research: forceResearch,
      created_at: now,
      updated_at: now,
      status: 'running',
      status_message: t('preparingSearch'),
      trace: initialTrace,
    }
    const context = buildConversationContext(priorTurns)
    abortRef.current = controller
    answerStartedRef.current = false
    resultRef.current = undefined
    traceRef.current = initialTrace
    setBusy(true)
    setActiveConversationId(conversationId)
    setActiveTurnId(turnId)
    setQuestion('')
    setAppError('')
    setConversations((current) => {
      const existing = current.find((item) => item.id === conversationId)
      const next = existing
        ? current.map((item) => item.id === conversationId ? { ...item, updated_at: now, turns: [...item.turns, newTurn] } : item)
        : [{ id: conversationId, title: prompt, created_at: now, updated_at: now, turns: [newTurn] }, ...current]
      return next.sort((left, right) => right.updated_at.localeCompare(left.updated_at))
    })
    try {
      await runSearch(prompt, searchMode, researchMode, {
        onStatus: (message) => patchTurn(conversationId, turnId, { status_message: message }),
        onWarning: (message) => patchTurn(conversationId, turnId, { warning: message }),
        onResearchTrace: (event) => {
          const nextTrace = mergeResearchTrace(traceRef.current, event)
          traceRef.current = nextTrace
          patchTurn(conversationId, turnId, { trace: nextTrace })
        },
        onAnswerStart: (value) => {
          answerStartedRef.current = true
          resultRef.current = value
          patchTurn(conversationId, turnId, { result: value, trace: traceRef.current })
        },
        onAnswerDelta: (delta) => {
          if (!resultRef.current) return
          const next = { ...resultRef.current, answer: resultRef.current.answer + delta }
          resultRef.current = next
          flushSync(() => patchTurn(conversationId, turnId, { result: next }))
        },
        onResult: (value) => {
          answerStartedRef.current = false
          resultRef.current = value
          patchTurn(conversationId, turnId, { result: value, trace: traceRef.current, status: 'complete', status_message: t('searchComplete') })
        },
      }, controller.signal, language, selectedAnswerModel, {
        history: context.history,
        capsules: context.capsules,
        forceResearch,
        turnId,
      })
    } catch (reason) {
      if ((reason as Error).name === 'AbortError') {
        patchTurn(conversationId, turnId, {
          result: resultRef.current,
          trace: traceRef.current,
          status: 'stopped',
          status_message: t('searchStopped'),
          warning: answerStartedRef.current ? t('answerStoppedWarning') : t('searchStoppedWarning'),
        })
      } else {
        patchTurn(conversationId, turnId, {
          result: resultRef.current,
          trace: traceRef.current,
          status: 'error',
          error: reason instanceof Error ? reason.message : t('searchFailed'),
        })
      }
    } finally {
      setBusy(false)
      abortRef.current = null
    }
  }

  const stop = () => abortRef.current?.abort()

  const renderTurn = (turn: ConversationTurn) => {
    const result = turn.result
    const hasTrace = Boolean(
      turn.trace?.queries.length
      || turn.trace?.direct_sources?.length
      || turn.trace?.direct_chunks?.length,
    )
    const showSources = Boolean(result && turn.status !== 'running' && result.sources.length)
    const isActive = turn.id === activeTurnId
    return <section className="conversation-turn" key={turn.id}>
      <article className="user-message"><p>{turn.question}</p></article>
      <section className="assistant-turn" aria-live={turn.status === 'running' ? 'polite' : undefined}>
        {turn.status === 'running' && !result && <div className="turn-progress liquid-card"><span className="mini-loader" /><span>{turn.status_message || t('researchInProgress')}</span><button className="stop-button" onClick={stop}><CircleStop size={17} />{t('stop')}</button></div>}
        {turn.status === 'running' && result && <div className="turn-stream-state"><span className="mini-loader" />{turn.status_message || t('researchInProgress')}</div>}
        {hasTrace && <details className="turn-trace" open={turn.status === 'running' || isActive}><summary>{t('researchTrace')}</summary><ResearchTracePanel trace={turn.trace || emptyResearchTrace()} t={t} /></details>}
        {turn.warning && <div className="notice warning">{turn.warning}<button onClick={() => patchTurn(activeConversationId || '', turn.id, { warning: undefined })} aria-label={t('closeNotice')}><X size={15} /></button></div>}
        {turn.error && <div className="notice error">{turn.error}<button onClick={() => patchTurn(activeConversationId || '', turn.id, { error: undefined })} aria-label={t('closeError')}><X size={15} /></button></div>}
        {result && <article className="answer-layout"><section className="answer-card"><div className="answer-meta"><span>{turn.status === 'stopped' ? <CircleStop size={16} /> : <CheckCircle2 size={16} />}{turn.status === 'stopped' ? t('answerStoppedPartial') : result.completion_state === 'complete' ? t('evidenceSufficient') : t('partialComplete')}</span>{result.research_strategy === 'reuse' && <span className="strategy-pill">{t('reusedEvidence')}</span>}{result.research_strategy === 'direct' && <span className="strategy-pill">{t('directEvidence')}</span>}{result.research_strategy === 'hybrid' && <span className="strategy-pill">{t('hybridResearch')}</span>}{result.research_strategy === 'research' && <span className="strategy-pill">{t('newResearch')}</span>}{result.elapsed_ms != null && result.research_strategy !== 'reuse' && <span><Clock3 size={15} />{t('researchSeconds', { seconds: (result.elapsed_ms / 1000).toFixed(1) })}</span>}</div><div className="markdown-body"><ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: ({ href, children }) => <a href={href} target="_blank" rel="noreferrer">{children}</a> }}>{citedAnswer(result, t('answerStreamPreparing'))}</ReactMarkdown></div></section>{showSources && <aside className="sources-panel"><div className="sources-heading"><h3>{t('sources')}</h3><span>{result.sources.length}</span></div>{result.sources.map((source, sourceIndex) => <a className="source-card" href={source.url} target="_blank" rel="noreferrer" key={`${source.citation_id || source.url}-${source.source_index || sourceIndex}`}><span>{source.source_index || sourceIndex + 1}</span><div><strong>{source.title || source.url}</strong><small>{sourceHostname(source.url, t('source'))}</small></div></a>)}</aside>}</article>}
      </section>
    </section>
  }

  return (
    <div className="app-shell">
      <aside className={`sidebar ${sidebarOpen ? 'is-open' : ''}`}>
        <div className="brand"><div className="brand-mark" aria-hidden="true">S</div><div><strong>Simplex</strong><span>{t('brandSubtitle')}</span></div></div>
        <button className="new-search" onClick={resetWorkspace} disabled={busy}><Sparkles size={17} />{t('newSearch')}</button>
        <div className="history-section"><span className="history-heading">{t('historyTitle')}</span><div className="history-list">{conversations.length ? conversations.map((conversation) => {
          const latest = latestTurn(conversation)
          return <button className={`history-item ${activeConversationId === conversation.id ? 'active' : ''}`} disabled={busy} key={conversation.id} onClick={() => openConversation(conversation)}><strong>{conversation.title}</strong><small><span>{t(historyStatusKeys[latest?.status || 'complete'])}</span>{historyTime(conversation.updated_at, language)}</small></button>
        }) : <p>{t('historyEmpty')}</p>}</div></div>
        <div className="sidebar-spacer" />
        <div className="infra-card"><span className={`health-dot ${healthLabel.className}`} /><div><strong>{healthLabel.label}</strong><small>Crawl4AI {health?.crawler.status === 'ok' ? t('crawlerReady') : t('crawlerNeedsCheck')}</small></div></div>
        <button className="sidebar-setting" onClick={() => { setSettingsOpen(true); setSidebarOpen(false) }}><Settings size={18} />{t('settings')}</button>
      </aside>

      {sidebarOpen && <button className="mobile-scrim" aria-label={t('mobileCloseMenu')} onClick={() => setSidebarOpen(false)} />}
      <main className="workspace">
        <header className="topbar"><button className="mobile-menu" aria-label={t('mobileOpenMenu')} onClick={() => setSidebarOpen(true)}><Menu size={20} /></button><div className="topbar-title">{activeConversation?.turns.length ? t('topbarResult') : t('topbarExplore')}</div><button className="topbar-settings" onClick={() => setSettingsOpen(true)}><Settings size={18} /><span>{t('settings')}</span></button></header>

        <div className={`content-stage ${activeConversation?.turns.length ? 'has-result' : ''}`}>
          {!activeConversation?.turns.length && <section className="hero"><div className="hero-symbol"><Search size={24} /></div><h1>{t('heroTitle')}</h1><p>{t('heroDescription')}</p></section>}
          {appError && <div className="notice error">{appError}<button onClick={() => setAppError('')} aria-label={t('closeError')}><X size={15} /></button></div>}
          {activeConversation && <section className="conversation-feed">{activeConversation.turns.map((turn) => renderTurn(turn))}</section>}
        </div>

        <section className="composer-wrap"><div className="composer liquid-card"><textarea value={question} onChange={(event) => setQuestion(event.target.value)} onKeyDown={(event) => { if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') submit() }} placeholder={t('composerPlaceholder')} rows={2} disabled={busy} /><div className="composer-controls"><div className="segmented">{modeOptions.map(({ id, label, icon: Icon }) => <button key={id} className={searchMode === id ? 'active' : ''} onClick={() => setSearchMode(id)} disabled={busy}><Icon size={15} />{label}</button>)}</div><button className={`research-route ${forceResearch ? 'active' : ''}`} title={t('researchRouteHint')} aria-pressed={forceResearch} onClick={() => setForceResearch((value) => !value)} disabled={busy}>{forceResearch ? t('forceResearch') : t('autoResearch')}</button>{answerModels.length > 0 && <div className="model-select"><select aria-label={t('answerModel')} value={selectedAnswerModel ? modelKey(selectedAnswerModel) : ''} onChange={(event) => setModelSelection(answerModels.find((item) => modelKey(item) === event.target.value))} disabled={busy}>{answerModels.map((item) => <option key={modelKey(item)} value={modelKey(item)}>{item.name}</option>)}</select></div>}<div className="depth-select"><select value={researchMode} onChange={(event) => setResearchMode(event.target.value as ResearchMode)} disabled={busy}>{depthOptions.map((option) => <option key={option.id} value={option.id}>{option.label}</option>)}</select></div><button className="send-button" aria-label={t('startSearch')} onClick={submit} disabled={!question.trim() || busy}>{busy ? <span className="mini-loader" /> : <ArrowUp size={19} />}</button></div></div><small className="composer-hint">{t('composerHint')}</small></section>
      </main>

      {settings && settingsOpen && <SettingsPanel settings={settings} onClose={() => setSettingsOpen(false)} onSave={async (value) => { const saved = await saveSettings(value); setSettings(saved); loadHealth().then(setHealth).catch(() => undefined); return saved }} />}
      {updateReady && <div className="update-toast"><div><strong>{t('updateTitle')}</strong><span>{t('updateDescription')}</span></div><button onClick={() => updateReady.postMessage('SKIP_WAITING')}>{t('updateApply')}</button></div>}
    </div>
  )
}

export default App
