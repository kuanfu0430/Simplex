import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowUp, BookOpen, CheckCircle2, CircleStop, Clock3, Globe2, Menu, MessageSquareText, Search, Settings, Sparkles, X } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import { loadHealth, loadSettings, runSearch, saveSettings } from './api'
import { SettingsPanel } from './SettingsPanel'
import type { AppSettings, Health, ResearchMode, SearchMode, SearchResult } from './types'

const modeOptions: Array<{ id: SearchMode; label: string; icon: typeof Globe2 }> = [
  { id: 'web', label: '網路', icon: Globe2 },
  { id: 'academic', label: '學術', icon: BookOpen },
  { id: 'social', label: '社群', icon: MessageSquareText },
]

const depthOptions: Array<{ id: ResearchMode; label: string }> = [
  { id: 'instant', label: '即時' },
  { id: 'fast', label: '快速' },
  { id: 'full', label: '完整' },
]

function citedAnswer(result: SearchResult): string {
  let answer = result.answer
  for (const source of result.sources) {
    if (!source.citation_marker || !source.url) continue
    const index = source.source_index || '?'
    answer = answer.split(source.citation_marker).join(`[${index}](${source.url})`)
  }
  return answer
}

function sourceHostname(url: string): string {
  if (!url) return '來源'
  try {
    return new URL(url).hostname
  } catch {
    return '來源'
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
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState('')
  const [queries, setQueries] = useState<string[]>([])
  const [warning, setWarning] = useState('')
  const [error, setError] = useState('')
  const [result, setResult] = useState<SearchResult | null>(null)
  const [answerCancelled, setAnswerCancelled] = useState(false)
  const [updateReady, setUpdateReady] = useState<ServiceWorker | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const answerStartedRef = useRef(false)

  useEffect(() => {
    loadSettings().then(setSettings).catch((reason) => setError(`載入設定失敗：${reason.message}`))
    loadHealth().then(setHealth).catch(() => undefined)
    const timer = window.setInterval(() => loadHealth().then(setHealth).catch(() => undefined), 30000)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    if (!settings) return
    document.documentElement.dataset.theme = settings.ui.theme
    document.documentElement.style.setProperty('--ui-scale', String(settings.ui.scale))
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', settings.ui.theme === 'light' ? '#f5f5f7' : '#090b10')
  }, [settings])

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
    if (!health) return { label: '檢查中', className: 'unknown' }
    if (health.searxng.status === 'ok') return { label: `SearXNG ${health.searxng.latency_ms ?? ''}ms`, className: 'ok' }
    if (health.searxng.status === 'disabled') return { label: 'SearXNG 已停用', className: 'unknown' }
    return { label: '搜尋基建降級', className: 'warn' }
  }, [health])

  const submit = async () => {
    const prompt = question.trim()
    if (!prompt || busy) return
    const controller = new AbortController()
    abortRef.current = controller
    setBusy(true)
    setResult(null)
    setAnswerCancelled(false)
    answerStartedRef.current = false
    setError('')
    setWarning('')
    setQueries([])
    setStatus('準備搜尋中')
    try {
      await runSearch(prompt, searchMode, researchMode, {
        onStatus: (message, payload) => {
          setStatus(message)
          if (Array.isArray(payload.queries)) setQueries(payload.queries.map(String))
        },
        onWarning: setWarning,
        onAnswerStart: (value) => {
          answerStartedRef.current = true
          setResult(value)
        },
        onAnswerDelta: (delta) => {
          setResult((current) => current ? { ...current, answer: current.answer + delta } : current)
        },
        onResult: (value) => {
          answerStartedRef.current = false
          setResult(value)
        },
      }, controller.signal)
      setStatus('搜尋完成')
    } catch (reason) {
      if ((reason as Error).name === 'AbortError') {
        setStatus('搜尋已停止')
        if (answerStartedRef.current) {
          setAnswerCancelled(true)
          setWarning('回答已停止；目前顯示的是已收到的部分內容。')
        } else {
          setWarning('搜尋已停止。')
        }
      }
      else setError(reason instanceof Error ? reason.message : '搜尋失敗')
    } finally {
      setBusy(false)
      abortRef.current = null
    }
  }

  const stop = () => abortRef.current?.abort()

  return (
    <div className="app-shell">
      <aside className={`sidebar ${sidebarOpen ? 'is-open' : ''}`}>
        <div className="brand"><div className="brand-mark" aria-hidden="true">S</div><div><strong>Simplex</strong><span>Research search</span></div></div>
        <button className="new-search" onClick={() => { setResult(null); setAnswerCancelled(false); setQuestion(''); setSidebarOpen(false) }}><Sparkles size={17} />新的搜尋</button>
        <div className="sidebar-spacer" />
        <div className="infra-card"><span className={`health-dot ${healthLabel.className}`} /><div><strong>{healthLabel.label}</strong><small>Crawl4AI {health?.crawler.status === 'ok' ? '已就緒' : '需檢查'}</small></div></div>
        <button className="sidebar-setting" onClick={() => { setSettingsOpen(true); setSidebarOpen(false) }}><Settings size={18} />設定</button>
      </aside>

      {sidebarOpen && <button className="mobile-scrim" aria-label="關閉選單" onClick={() => setSidebarOpen(false)} />}
      <main className="workspace">
        <header className="topbar"><button className="mobile-menu" aria-label="開啟選單" onClick={() => setSidebarOpen(true)}><Menu size={20} /></button><div className="topbar-title">{result ? '研究結果' : '探索問題'}</div><button className="topbar-settings" onClick={() => setSettingsOpen(true)}><Settings size={18} /><span>設定</span></button></header>

        <div className={`content-stage ${result ? 'has-result' : ''}`}>
          {!result && !busy && <section className="hero"><div className="hero-symbol"><Search size={24} /></div><h1>問一個值得查清楚的問題。</h1><p>Simplex 以多路搜尋、Judge 選址和深度爬取，快速整理成可追溯的答案。</p></section>}

          {busy && <section className="progress-card liquid-card" aria-live="polite"><div className="progress-heading"><div className="orb-loader" /><div><span className="eyebrow">Research in progress</span><h2>{status}</h2></div><button className="stop-button" onClick={stop}><CircleStop size={17} />停止</button></div>{queries.length > 0 && <div className="query-list">{queries.map((query) => <span key={query}>{query}</span>)}</div>}<div className="progress-line"><span /></div></section>}

          {warning && <div className="notice warning">{warning}<button onClick={() => setWarning('')} aria-label="關閉提示"><X size={15} /></button></div>}
          {error && <div className="notice error">{error}<button onClick={() => setError('')} aria-label="關閉錯誤"><X size={15} /></button></div>}

          {result && <article className="answer-layout">
            <section className="answer-card"><div className="answer-meta"><span>{answerCancelled ? <CircleStop size={16} /> : <CheckCircle2 size={16} />}{answerCancelled ? '回答已停止（部分內容）' : result.completion_state === 'complete' ? '證據充分' : '部分完成'}</span>{result.elapsed_ms != null && <span><Clock3 size={15} />研究 {(result.elapsed_ms / 1000).toFixed(1)} 秒</span>}</div><div className="markdown-body"><ReactMarkdown components={{ a: ({ href, children }) => <a href={href} target="_blank" rel="noreferrer">{children}</a> }}>{citedAnswer(result) || '回答串流準備中…'}</ReactMarkdown></div></section>
            <aside className="sources-panel"><div className="sources-heading"><h3>來源</h3><span>{result.sources.length}</span></div>{result.sources.map((source, index) => <a className="source-card" href={source.url} target="_blank" rel="noreferrer" key={`${source.citation_id || source.url}-${source.source_index || index}`}><span>{source.source_index || index + 1}</span><div><strong>{source.title || source.url}</strong><small>{sourceHostname(source.url)}</small></div></a>)}</aside>
          </article>}
        </div>

        <section className="composer-wrap"><div className="composer liquid-card"><textarea value={question} onChange={(event) => setQuestion(event.target.value)} onKeyDown={(event) => { if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') submit() }} placeholder="輸入你的問題…" rows={2} disabled={busy} /><div className="composer-controls"><div className="segmented">{modeOptions.map(({ id, label, icon: Icon }) => <button key={id} className={searchMode === id ? 'active' : ''} onClick={() => setSearchMode(id)} disabled={busy}><Icon size={15} />{label}</button>)}</div><div className="depth-select"><select value={researchMode} onChange={(event) => setResearchMode(event.target.value as ResearchMode)} disabled={busy}>{depthOptions.map((option) => <option key={option.id} value={option.id}>{option.label}</option>)}</select></div><button className="send-button" aria-label="開始搜尋" onClick={submit} disabled={!question.trim() || busy}>{busy ? <span className="mini-loader" /> : <ArrowUp size={19} />}</button></div></div><small className="composer-hint">⌘ Enter 搜尋 · snippets 只供 Judge 選址，回答僅使用深爬證據</small></section>
      </main>

      {settings && settingsOpen && <SettingsPanel settings={settings} onClose={() => setSettingsOpen(false)} onSave={async (value) => { const saved = await saveSettings(value); setSettings(saved); loadHealth().then(setHealth).catch(() => undefined); return saved }} />}
      {updateReady && <div className="update-toast"><div><strong>Simplex 有新版本</strong><span>重新整理即可套用。</span></div><button onClick={() => updateReady.postMessage('SKIP_WAITING')}>更新</button></div>}
    </div>
  )
}

export default App
