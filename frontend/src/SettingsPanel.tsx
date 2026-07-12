import { useMemo, useState } from 'react'
import { Check, Database, KeyRound, Moon, Plus, RefreshCw, Server, Settings2, SlidersHorizontal, Sun, Trash2, X } from 'lucide-react'
import { loadModels } from './api'
import type { AppSettings, LlmProvider, SearchProvider } from './types'

type Section = 'models' | 'search' | 'other'

interface Props {
  settings: AppSettings
  onClose: () => void
  onSave: (settings: AppSettings) => Promise<AppSettings>
}

const clone = <T,>(value: T): T => structuredClone(value)

function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (value: boolean) => void; label: string }) {
  return (
    <button className={`toggle ${checked ? 'is-on' : ''}`} type="button" role="switch" aria-checked={checked} aria-label={label} onClick={() => onChange(!checked)}>
      <span />
    </button>
  )
}

export function SettingsPanel({ settings, onClose, onSave }: Props) {
  const [section, setSection] = useState<Section>('models')
  const [draft, setDraft] = useState<AppSettings>(() => clone(settings))
  const [models, setModels] = useState<Record<string, Array<{ id: string; name: string }>>>({})
  const [busy, setBusy] = useState<string | null>(null)
  const [notice, setNotice] = useState('')

  const providerMap = useMemo(
    () => Object.fromEntries(draft.llm.providers.map((provider) => [provider.id, provider])),
    [draft.llm.providers],
  )

  const updateLlmProvider = (id: string, patch: Partial<LlmProvider>) => {
    setDraft((current) => ({
      ...current,
      llm: { ...current.llm, providers: current.llm.providers.map((provider) => (provider.id === id ? { ...provider, ...patch } : provider)) },
    }))
  }

  const updateSearchProvider = (id: string, patch: Partial<SearchProvider>) => {
    setDraft((current) => ({
      ...current,
      search: { ...current.search, providers: { ...current.search.providers, [id]: { ...current.search.providers[id], ...patch } } },
    }))
  }

  const updateCustomSearch = (id: string, patch: Partial<SearchProvider>) => {
    setDraft((current) => ({
      ...current,
      search: { ...current.search, custom: current.search.custom.map((provider) => (provider.id === id ? { ...provider, ...patch } : provider)) },
    }))
  }

  const persist = async (closeAfter = false) => {
    setBusy('save')
    setNotice('儲存設定中')
    try {
      const saved = await onSave(draft)
      setDraft(clone(saved))
      setNotice('儲存設定完成')
      if (closeAfter) onClose()
    } catch (error) {
      setNotice(`儲存設定失敗：${error instanceof Error ? error.message : '未知錯誤'}`)
    } finally {
      setBusy(null)
    }
  }

  const refreshModels = async (providerId: string) => {
    setBusy(`models:${providerId}`)
    setNotice('同步模型中')
    try {
      const saved = await onSave(draft)
      setDraft(clone(saved))
      const list = await loadModels(providerId)
      setModels((current) => ({ ...current, [providerId]: list }))
      setNotice(`同步模型完成，共 ${list.length} 個`)
    } catch (error) {
      setNotice(`同步模型失敗：${error instanceof Error ? error.message : '未知錯誤'}`)
    } finally {
      setBusy(null)
    }
  }

  const addLlmProvider = () => {
    const id = `custom-${crypto.randomUUID().slice(0, 8)}`
    setDraft((current) => ({
      ...current,
      llm: {
        ...current.llm,
        providers: [...current.llm.providers, { id, name: '自定義 Provider', base_url: '', models_path: '/models', chat_endpoint: '/chat/completions', api_key: '', enabled: true, custom: true }],
      },
    }))
  }

  const addSearchProvider = () => {
    const id = `custom-search-${crypto.randomUUID().slice(0, 8)}`
    setDraft((current) => ({
      ...current,
      search: {
        ...current.search,
        custom: [...current.search.custom, { id, name: '自定義搜尋引擎', enabled: true, base_url: '', api_key: '', method: 'GET', query_param: 'q', count_param: 'count', auth_mode: 'bearer', auth_name: 'Authorization', result_path: 'results', per_query: 20, modes: ['web', 'academic', 'social'], fields: { title: 'title', url: 'url', content: 'content', score: 'score', published_date: 'published_date' }, custom: true }],
      },
    }))
  }

  const renderModelPicker = (purpose: 'question' | 'judge', label: string) => {
    const selected = draft.llm[`${purpose}_model`]
    const available = models[selected.provider_id] || []
    const currentModelExists = available.some((model) => model.id === selected.model)
    return (
      <div className="model-picker glass-panel">
        <div><span className="eyebrow">{purpose === 'question' ? '問答與查詢規劃' : '搜尋結果與證據審核'}</span><h3>{label}</h3></div>
        <div className="field-grid two">
          <label>Provider<select value={selected.provider_id} onChange={(event) => setDraft((current) => ({ ...current, llm: { ...current.llm, [`${purpose}_model`]: { provider_id: event.target.value, model: '' } } }))}>{draft.llm.providers.filter((provider) => provider.enabled).map((provider) => <option key={provider.id} value={provider.id}>{provider.name}</option>)}</select></label>
          <label>模型<select value={selected.model} onChange={(event) => setDraft((current) => ({ ...current, llm: { ...current.llm, [`${purpose}_model`]: { ...selected, model: event.target.value } } }))}><option value="">請選擇模型</option>{selected.model && !currentModelExists && <option value={selected.model}>{selected.model}</option>}{available.map((model) => <option key={model.id} value={model.id}>{model.name}</option>)}</select></label>
        </div>
        <button className="text-button" type="button" disabled={busy !== null} onClick={() => refreshModels(selected.provider_id)}><RefreshCw size={15} className={busy === `models:${selected.provider_id}` ? 'spin' : ''} />從 {providerMap[selected.provider_id]?.name || 'Provider'} 同步模型</button>
      </div>
    )
  }

  return (
    <div className="settings-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="settings-shell" role="dialog" aria-modal="true" aria-label="Simplex 設定">
        <header className="settings-header"><div><span className="eyebrow">Simplex</span><h2>設定</h2></div><button className="icon-button" type="button" onClick={onClose} aria-label="關閉設定"><X size={20} /></button></header>
        <div className="settings-layout">
          <nav className="settings-nav" aria-label="設定分類">
            <button className={section === 'models' ? 'active' : ''} onClick={() => setSection('models')}><KeyRound size={17} />模型</button>
            <button className={section === 'search' ? 'active' : ''} onClick={() => setSection('search')}><Database size={17} />搜尋服務</button>
            <button className={section === 'other' ? 'active' : ''} onClick={() => setSection('other')}><SlidersHorizontal size={17} />其他項目</button>
          </nav>
          <main className="settings-content">
            {section === 'models' && <>
              <div className="section-heading"><div><span className="eyebrow">LLM routing</span><h3>問答與 Judge 分工</h3><p>API Key 只會加密保存在這台機器。同步模型後可分別指定兩個工作模型。</p></div></div>
              <div className="picker-stack">{renderModelPicker('question', '問答模型')}{renderModelPicker('judge', 'Judge 模型')}</div>
              <div className="card-list">
                {draft.llm.providers.map((provider) => <article className="provider-card" key={provider.id}>
                  <div className="provider-title"><div><h4>{provider.name}</h4><small>{provider.id}</small></div><Toggle checked={provider.enabled} onChange={(enabled) => updateLlmProvider(provider.id, { enabled })} label={`啟用 ${provider.name}`} /></div>
                  <div className="field-grid two"><label>名稱<input value={provider.name} disabled={!provider.custom} onChange={(event) => updateLlmProvider(provider.id, { name: event.target.value })} /></label><label>API Key<input type="password" value={provider.api_key} placeholder={provider.has_api_key ? '已安全儲存，留空不變' : '填入 API Key'} onChange={(event) => updateLlmProvider(provider.id, { api_key: event.target.value })} /></label></div>
                  <label>API Base URL<input value={provider.base_url} onChange={(event) => updateLlmProvider(provider.id, { base_url: event.target.value })} /></label>
                  <div className="field-grid two"><label>模型端點<input value={provider.models_path} onChange={(event) => updateLlmProvider(provider.id, { models_path: event.target.value })} /></label><label>聊天端點<input value={provider.chat_endpoint} onChange={(event) => updateLlmProvider(provider.id, { chat_endpoint: event.target.value })} /></label></div>
                  {provider.custom && <button className="danger-button" type="button" onClick={() => setDraft((current) => ({ ...current, llm: { ...current.llm, providers: current.llm.providers.filter((item) => item.id !== provider.id) } }))}><Trash2 size={15} />移除</button>}
                </article>)}
              </div>
              <button className="add-button" type="button" onClick={addLlmProvider}><Plus size={17} />新增自定義 Provider</button>
            </>}

            {section === 'search' && <>
              <div className="section-heading"><div><span className="eyebrow">Search infrastructure</span><h3>選擇搜尋基建</h3><p>預設使用隨 Simplex 啟動的原生 SearXNG；若已有自己的搜尋 API，可切換後再填入 Provider 與 Key。</p></div></div>
              <div className="engine-mode-grid" role="radiogroup" aria-label="搜尋引擎模式">
                <button className={draft.search.engine_mode === 'searxng' ? 'selected' : ''} type="button" role="radio" aria-checked={draft.search.engine_mode === 'searxng'} onClick={() => setDraft((current) => ({ ...current, search: { ...current.search, engine_mode: 'searxng' } }))}><Server size={21} /><span><strong>原生 SearXNG</strong><small>免 API Key，預設選項</small></span><Check size={17} /></button>
                <button className={draft.search.engine_mode === 'custom' ? 'selected' : ''} type="button" role="radio" aria-checked={draft.search.engine_mode === 'custom'} onClick={() => setDraft((current) => ({ ...current, search: { ...current.search, engine_mode: 'custom' } }))}><Settings2 size={21} /><span><strong>自有搜尋引擎</strong><small>使用自己的 Provider 或 JSON API</small></span><Check size={17} /></button>
              </div>

              {draft.search.engine_mode === 'searxng' && <article className="native-engine-card liquid-card">
                <div className="provider-title"><div><span className="eyebrow">Native search</span><h4>SearXNG</h4><small>一般、學術與社群搜尋都由本機服務承接</small></div><span className="active-pill"><span />使用中</span></div>
                <label>SearXNG 地址<input value={draft.search.providers.searxng?.base_url || ''} onChange={(event) => updateSearchProvider('searxng', { base_url: event.target.value })} /></label>
                <p>一鍵啟動預設為 <code>http://127.0.0.1:8888</code>。Docker 版會自動使用容器內部地址，不需要 API Key。</p>
              </article>}

              {draft.search.engine_mode === 'custom' && <div className="custom-engine-panel">
                <div className="mode-notice"><Settings2 size={17} /><div><strong>自有引擎模式</strong><span>只有下方啟用且具備有效設定的服務會收到查詢；原生 SearXNG 會由後端停用。</span></div></div>
                <div className="card-list">
                  {Object.entries(draft.search.providers).filter(([id]) => id !== 'searxng').map(([id, provider]) => <article className="provider-card" key={id}>
                    <div className="provider-title"><div><h4>{provider.name}</h4><small>內建 API Adapter</small></div><Toggle checked={provider.enabled} onChange={(enabled) => updateSearchProvider(id, { enabled })} label={`啟用 ${provider.name}`} /></div>
                    <label>API 地址<input value={provider.base_url} onChange={(event) => updateSearchProvider(id, { base_url: event.target.value })} /></label>
                    <label>API Key<input type="password" value={provider.api_key} placeholder={provider.has_api_key ? '已安全儲存，留空不變' : '填入 API Key'} onChange={(event) => updateSearchProvider(id, { api_key: event.target.value })} /></label>
                  </article>)}
                  {draft.search.custom.map((provider) => <article className="provider-card" key={provider.id}>
                    <div className="provider-title"><div><input className="title-input" value={provider.name} onChange={(event) => updateCustomSearch(provider.id || '', { name: event.target.value })} /><small>自定義 JSON 搜尋 API</small></div><Toggle checked={provider.enabled} onChange={(enabled) => updateCustomSearch(provider.id || '', { enabled })} label={`啟用 ${provider.name}`} /></div>
                    <div className="field-grid two"><label>API 地址<input value={provider.base_url} onChange={(event) => updateCustomSearch(provider.id || '', { base_url: event.target.value })} /></label><label>API Key<input type="password" value={provider.api_key} placeholder={provider.has_api_key ? '已安全儲存，留空不變' : '可留空'} onChange={(event) => updateCustomSearch(provider.id || '', { api_key: event.target.value })} /></label></div>
                    <div className="field-grid three"><label>方法<select value={provider.method} onChange={(event) => updateCustomSearch(provider.id || '', { method: event.target.value as 'GET' | 'POST' })}><option>GET</option><option>POST</option></select></label><label>Query 欄位<input value={provider.query_param} onChange={(event) => updateCustomSearch(provider.id || '', { query_param: event.target.value })} /></label><label>Results 路徑<input value={provider.result_path} onChange={(event) => updateCustomSearch(provider.id || '', { result_path: event.target.value })} /></label></div>
                    <details><summary>回傳欄位與授權設定</summary><div className="field-grid three"><label>標題欄位<input value={provider.fields?.title || ''} onChange={(event) => updateCustomSearch(provider.id || '', { fields: { ...provider.fields, title: event.target.value } })} /></label><label>URL 欄位<input value={provider.fields?.url || ''} onChange={(event) => updateCustomSearch(provider.id || '', { fields: { ...provider.fields, url: event.target.value } })} /></label><label>摘要欄位<input value={provider.fields?.content || ''} onChange={(event) => updateCustomSearch(provider.id || '', { fields: { ...provider.fields, content: event.target.value } })} /></label></div><div className="field-grid two"><label>授權模式<select value={provider.auth_mode} onChange={(event) => updateCustomSearch(provider.id || '', { auth_mode: event.target.value as SearchProvider['auth_mode'] })}><option value="bearer">Bearer</option><option value="header">自定義 Header</option><option value="query">Query parameter</option><option value="none">不需授權</option></select></label><label>授權欄位<input value={provider.auth_name} onChange={(event) => updateCustomSearch(provider.id || '', { auth_name: event.target.value })} /></label></div></details>
                    <button className="danger-button" type="button" onClick={() => setDraft((current) => ({ ...current, search: { ...current.search, custom: current.search.custom.filter((item) => item.id !== provider.id) } }))}><Trash2 size={15} />移除</button>
                  </article>)}
                </div>
                <button className="add-button" type="button" onClick={addSearchProvider}><Plus size={17} />新增自定義搜尋引擎</button>
              </div>}
            </>}

            {section === 'other' && <>
              <div className="section-heading"><div><span className="eyebrow">Appearance</span><h3>其他項目</h3><p>主題和縮放會作用於完整排版系統，容器、間距與控制元件會同步調整。</p></div></div>
              <article className="appearance-card glass-panel"><h4>外觀主題</h4><div className="theme-options"><button className={draft.ui.theme === 'dark' ? 'selected' : ''} onClick={() => setDraft((current) => ({ ...current, ui: { ...current.ui, theme: 'dark' } }))}><Moon size={19} /><span>深色</span><small>清透 Liquid Glass</small></button><button className={draft.ui.theme === 'light' ? 'selected' : ''} onClick={() => setDraft((current) => ({ ...current, ui: { ...current.ui, theme: 'light' } }))}><Sun size={19} /><span>淺色</span><small>Apple 首頁式留白</small></button></div></article>
              <article className="appearance-card glass-panel"><div className="scale-header"><div><h4>介面縮放</h4><p>不是只放大文字；網格、間距、圖示和控制元件會一起縮放。</p></div><strong>{Math.round(draft.ui.scale * 100)}%</strong></div><input className="scale-range" type="range" min="0.8" max="1.35" step="0.05" value={draft.ui.scale} onChange={(event) => setDraft((current) => ({ ...current, ui: { ...current.ui, scale: Number(event.target.value) } }))} /><div className="range-labels"><span>緊湊</span><span>標準</span><span>寬大</span></div></article>
            </>}
          </main>
        </div>
        <footer className="settings-footer"><span className={notice.includes('失敗') ? 'error-text' : ''}>{notice || '變更尚未儲存'}</span><div><button className="secondary-button" onClick={onClose}>取消</button><button className="primary-button" disabled={busy !== null} onClick={() => persist(true)}>{busy === 'save' ? <RefreshCw size={16} className="spin" /> : <Check size={16} />}儲存設定</button></div></footer>
      </section>
    </div>
  )
}
