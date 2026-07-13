import { useMemo, useState } from 'react'
import { Check, Database, KeyRound, Moon, Plus, RefreshCw, Server, Settings2, SlidersHorizontal, Sun, Trash2, X } from 'lucide-react'
import { loadModels } from './api'
import { createTranslator, normalizeLanguage } from './i18n'
import type { AppSettings, LlmProvider, ModelPoolEntry, SearchProvider } from './types'

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
  const [noticeError, setNoticeError] = useState(false)

  const language = normalizeLanguage(draft.ui.language)
  const t = useMemo(() => createTranslator(language), [language])
  const providerMap = useMemo(
    () => Object.fromEntries(draft.llm.providers.map((provider) => [provider.id, provider])),
    [draft.llm.providers],
  )

  const errorMessage = (error: unknown) => error instanceof Error ? error.message : t('unknownError')

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

  const isInModelPool = (providerId: string, model: string) => draft.llm.model_pool.some((item) => item.provider_id === providerId && item.model === model)

  const toggleModelPool = (providerId: string, model: { id: string; name: string }) => {
    setDraft((current) => {
      const existing = current.llm.model_pool || []
      const inPool = existing.some((item) => item.provider_id === providerId && item.model === model.id)
      const modelPool: ModelPoolEntry[] = inPool
        ? existing.filter((item) => item.provider_id !== providerId || item.model !== model.id)
        : [...existing, { provider_id: providerId, model: model.id, name: model.name || model.id }]
      return { ...current, llm: { ...current.llm, model_pool: modelPool } }
    })
  }

  const persist = async (closeAfter = false) => {
    setBusy('save')
    setNotice(t('saving'))
    setNoticeError(false)
    try {
      const saved = await onSave(draft)
      setDraft(clone(saved))
      setNotice(t('saved'))
      if (closeAfter) onClose()
    } catch (error) {
      setNotice(t('saveFailed', { message: errorMessage(error) }))
      setNoticeError(true)
    } finally {
      setBusy(null)
    }
  }

  const refreshModels = async (providerId: string) => {
    setBusy(`models:${providerId}`)
    setNotice(t('syncModels'))
    setNoticeError(false)
    try {
      const saved = await onSave(draft)
      setDraft(clone(saved))
      const list = await loadModels(providerId)
      setModels((current) => ({ ...current, [providerId]: list }))
      setNotice(t('syncComplete', { count: list.length }))
    } catch (error) {
      setNotice(t('syncFailed', { message: errorMessage(error) }))
      setNoticeError(true)
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
        providers: [...current.llm.providers, { id, name: language === 'zh-TW' ? '自定義 Provider' : 'Custom provider', base_url: '', models_path: '/models', chat_endpoint: '/chat/completions', api_key: '', enabled: true, custom: true }],
      },
    }))
  }

  const addSearchProvider = () => {
    const id = `custom-search-${crypto.randomUUID().slice(0, 8)}`
    setDraft((current) => ({
      ...current,
      search: {
        ...current.search,
        custom: [...current.search.custom, { id, name: language === 'zh-TW' ? '自定義搜尋引擎' : 'Custom search engine', enabled: true, base_url: '', api_key: '', method: 'GET', query_param: 'q', count_param: 'count', auth_mode: 'bearer', auth_name: 'Authorization', result_path: 'results', per_query: 20, modes: ['web', 'academic', 'social'], fields: { title: 'title', url: 'url', content: 'content', score: 'score', published_date: 'published_date' }, custom: true }],
      },
    }))
  }

  const renderModelPicker = (purpose: 'question' | 'judge') => {
    const selected = draft.llm[`${purpose}_model`]
    const available = models[selected.provider_id] || []
    const currentModelExists = available.some((model) => model.id === selected.model)
    return (
      <div className="model-picker glass-panel">
        <div><span className="eyebrow">{purpose === 'question' ? t('questionPlanning') : t('evidenceReview')}</span><h3>{purpose === 'question' ? t('questionModel') : t('judgeModel')}</h3></div>
        <div className="field-grid two">
          <label>{t('provider')}<select value={selected.provider_id} onChange={(event) => setDraft((current) => ({ ...current, llm: { ...current.llm, [`${purpose}_model`]: { provider_id: event.target.value, model: '' } } }))}>{draft.llm.providers.filter((provider) => provider.enabled).map((provider) => <option key={provider.id} value={provider.id}>{provider.name}</option>)}</select></label>
          <label>{t('model')}<select value={selected.model} onChange={(event) => setDraft((current) => ({ ...current, llm: { ...current.llm, [`${purpose}_model`]: { ...selected, model: event.target.value } } }))}><option value="">{t('selectModel')}</option>{selected.model && !currentModelExists && <option value={selected.model}>{selected.model}</option>}{available.map((model) => <option key={model.id} value={model.id}>{model.name}</option>)}</select></label>
        </div>
        <button className="text-button" type="button" disabled={busy !== null} onClick={() => refreshModels(selected.provider_id)}><RefreshCw size={15} className={busy === `models:${selected.provider_id}` ? 'spin' : ''} />{t('syncModelsFrom', { provider: providerMap[selected.provider_id]?.name || t('provider') })}</button>
      </div>
    )
  }

  return (
    <div className="settings-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="settings-shell" role="dialog" aria-modal="true" aria-label={t('settings')}>
        <header className="settings-header"><div><span className="eyebrow">Simplex</span><h2>{t('settings')}</h2></div><button className="icon-button" type="button" onClick={onClose} aria-label={t('settingsClose')}><X size={20} /></button></header>
        <div className="settings-layout">
          <nav className="settings-nav" aria-label={t('settingsCategories')}>
            <button type="button" className={section === 'models' ? 'active' : ''} onClick={() => setSection('models')}><KeyRound size={17} />{t('settingsModels')}</button>
            <button type="button" className={section === 'search' ? 'active' : ''} onClick={() => setSection('search')}><Database size={17} />{t('settingsSearchServices')}</button>
            <button type="button" className={section === 'other' ? 'active' : ''} onClick={() => setSection('other')}><SlidersHorizontal size={17} />{t('settingsOther')}</button>
          </nav>
          <main className="settings-content">
            {section === 'models' && <>
              <div className="section-heading"><div><span className="eyebrow">{t('llmRouting')}</span><h3>{t('llmHeading')}</h3><p>{t('llmDescription')}</p></div></div>
              <div className="picker-stack">{renderModelPicker('question')}{renderModelPicker('judge')}</div>
              <article className="model-pool-card glass-panel"><div><span className="eyebrow">{t('answerModel')}</span><h4>{t('modelPool')}</h4><p>{t('modelPoolDescription')}</p></div>{draft.llm.model_pool.length ? <div className="model-pool-chips">{draft.llm.model_pool.map((model) => <span key={`${model.provider_id}:${model.model}`}>{providerMap[model.provider_id]?.name || model.provider_id} · {model.name || model.model}<button type="button" aria-label={t('removeFromPool')} onClick={() => toggleModelPool(model.provider_id, { id: model.model, name: model.name })}><X size={13} /></button></span>)}</div> : <p className="model-pool-empty">{t('modelPoolEmpty')}</p>}</article>
              <div className="card-list">
                {draft.llm.providers.map((provider) => <article className="provider-card" key={provider.id}>
                  <div className="provider-title"><div><h4>{provider.name}</h4><small>{provider.id}</small></div><Toggle checked={provider.enabled} onChange={(enabled) => updateLlmProvider(provider.id, { enabled })} label={t('enable', { name: provider.name })} /></div>
                  <div className="field-grid two"><label>{t('name')}<input value={provider.name} disabled={!provider.custom} onChange={(event) => updateLlmProvider(provider.id, { name: event.target.value })} /></label><label>{t('apiKey')}<input type="password" value={provider.api_key} placeholder={provider.has_api_key ? t('apiKeyStored') : t('apiKeyFill')} onChange={(event) => updateLlmProvider(provider.id, { api_key: event.target.value })} /></label></div>
                  <label>{t('apiBaseUrl')}<input value={provider.base_url} onChange={(event) => updateLlmProvider(provider.id, { base_url: event.target.value })} /></label>
                  <div className="field-grid two"><label>{t('modelsEndpoint')}<input value={provider.models_path} onChange={(event) => updateLlmProvider(provider.id, { models_path: event.target.value })} /></label><label>{t('chatEndpoint')}<input value={provider.chat_endpoint} onChange={(event) => updateLlmProvider(provider.id, { chat_endpoint: event.target.value })} /></label></div>
                  <div className="provider-model-pool"><button className="text-button" type="button" disabled={busy !== null} onClick={() => refreshModels(provider.id)}><RefreshCw size={15} className={busy === `models:${provider.id}` ? 'spin' : ''} />{t('syncModelsFrom', { provider: provider.name })}</button>{models[provider.id]?.length ? <details><summary>{t('availableModels')}</summary><div className="available-model-list">{models[provider.id].map((model) => <button type="button" key={model.id} className={isInModelPool(provider.id, model.id) ? 'in-pool' : ''} onClick={() => toggleModelPool(provider.id, model)}><span>{model.name}</span>{isInModelPool(provider.id, model.id) ? t('removeFromPool') : t('addToPool')}</button>)}</div></details> : null}</div>
                  {provider.custom && <button className="danger-button" type="button" onClick={() => setDraft((current) => ({ ...current, llm: { ...current.llm, providers: current.llm.providers.filter((item) => item.id !== provider.id), model_pool: current.llm.model_pool.filter((item) => item.provider_id !== provider.id) } }))}><Trash2 size={15} />{t('remove')}</button>}
                </article>)}
              </div>
              <button className="add-button" type="button" onClick={addLlmProvider}><Plus size={17} />{t('addCustomProvider')}</button>
            </>}

            {section === 'search' && <>
              <div className="section-heading"><div><span className="eyebrow">{t('searchInfrastructure')}</span><h3>{t('searchHeading')}</h3><p>{t('searchDescription')}</p></div></div>
              <div className="engine-mode-grid" role="radiogroup" aria-label={t('searchMode')}>
                <button className={draft.search.engine_mode === 'searxng' ? 'selected' : ''} type="button" role="radio" aria-checked={draft.search.engine_mode === 'searxng'} onClick={() => setDraft((current) => ({ ...current, search: { ...current.search, engine_mode: 'searxng' } }))}><Server size={21} /><span><strong>{t('nativeSearxng')}</strong><small>{t('nativeSearxngDescription')}</small></span><Check size={17} /></button>
                <button className={draft.search.engine_mode === 'custom' ? 'selected' : ''} type="button" role="radio" aria-checked={draft.search.engine_mode === 'custom'} onClick={() => setDraft((current) => ({ ...current, search: { ...current.search, engine_mode: 'custom' } }))}><Settings2 size={21} /><span><strong>{t('customSearch')}</strong><small>{t('customSearchDescription')}</small></span><Check size={17} /></button>
              </div>

              {draft.search.engine_mode === 'searxng' && <article className="native-engine-card liquid-card">
                <div className="provider-title"><div><span className="eyebrow">{t('nativeSearch')}</span><h4>SearXNG</h4><small>{t('nativeSearchDescription')}</small></div><span className="active-pill"><span />{t('inUse')}</span></div>
                <label>{t('searxngAddress')}<input value={draft.search.providers.searxng?.base_url || ''} onChange={(event) => updateSearchProvider('searxng', { base_url: event.target.value })} /></label>
                <p>{t('searxngDefaultDescription')}</p>
              </article>}

              {draft.search.engine_mode === 'custom' && <div className="custom-engine-panel">
                <div className="mode-notice"><Settings2 size={17} /><div><strong>{t('customMode')}</strong><span>{t('customModeDescription')}</span></div></div>
                <div className="card-list">
                  {Object.entries(draft.search.providers).filter(([id]) => id !== 'searxng').map(([id, provider]) => <article className="provider-card" key={id}>
                    <div className="provider-title"><div><h4>{provider.name}</h4><small>{t('builtInAdapter')}</small></div><Toggle checked={provider.enabled} onChange={(enabled) => updateSearchProvider(id, { enabled })} label={t('enable', { name: provider.name })} /></div>
                    <label>{t('apiBaseUrl')}<input value={provider.base_url} onChange={(event) => updateSearchProvider(id, { base_url: event.target.value })} /></label>
                    <label>{t('apiKey')}<input type="password" value={provider.api_key} placeholder={provider.has_api_key ? t('apiKeyStored') : t('apiKeyFill')} onChange={(event) => updateSearchProvider(id, { api_key: event.target.value })} /></label>
                  </article>)}
                  {draft.search.custom.map((provider) => <article className="provider-card" key={provider.id}>
                    <div className="provider-title"><div><input className="title-input" value={provider.name} onChange={(event) => updateCustomSearch(provider.id || '', { name: event.target.value })} /><small>{t('customJsonApi')}</small></div><Toggle checked={provider.enabled} onChange={(enabled) => updateCustomSearch(provider.id || '', { enabled })} label={t('enable', { name: provider.name })} /></div>
                    <div className="field-grid two"><label>{t('apiBaseUrl')}<input value={provider.base_url} onChange={(event) => updateCustomSearch(provider.id || '', { base_url: event.target.value })} /></label><label>{t('apiKey')}<input type="password" value={provider.api_key} placeholder={provider.has_api_key ? t('apiKeyStored') : t('apiKeyOptional')} onChange={(event) => updateCustomSearch(provider.id || '', { api_key: event.target.value })} /></label></div>
                    <div className="field-grid three"><label>{t('method')}<select value={provider.method} onChange={(event) => updateCustomSearch(provider.id || '', { method: event.target.value as 'GET' | 'POST' })}><option>GET</option><option>POST</option></select></label><label>{t('queryField')}<input value={provider.query_param} onChange={(event) => updateCustomSearch(provider.id || '', { query_param: event.target.value })} /></label><label>{t('resultsPath')}<input value={provider.result_path} onChange={(event) => updateCustomSearch(provider.id || '', { result_path: event.target.value })} /></label></div>
                    <details><summary>{t('responseAuth')}</summary><div className="field-grid three"><label>{t('titleField')}<input value={provider.fields?.title || ''} onChange={(event) => updateCustomSearch(provider.id || '', { fields: { ...provider.fields, title: event.target.value } })} /></label><label>{t('urlField')}<input value={provider.fields?.url || ''} onChange={(event) => updateCustomSearch(provider.id || '', { fields: { ...provider.fields, url: event.target.value } })} /></label><label>{t('summaryField')}<input value={provider.fields?.content || ''} onChange={(event) => updateCustomSearch(provider.id || '', { fields: { ...provider.fields, content: event.target.value } })} /></label></div><div className="field-grid two"><label>{t('authMode')}<select value={provider.auth_mode} onChange={(event) => updateCustomSearch(provider.id || '', { auth_mode: event.target.value as SearchProvider['auth_mode'] })}><option value="bearer">Bearer</option><option value="header">{t('customHeader')}</option><option value="query">{t('queryParameter')}</option><option value="none">{t('noAuth')}</option></select></label><label>{t('authField')}<input value={provider.auth_name} onChange={(event) => updateCustomSearch(provider.id || '', { auth_name: event.target.value })} /></label></div></details>
                    <button className="danger-button" type="button" onClick={() => setDraft((current) => ({ ...current, search: { ...current.search, custom: current.search.custom.filter((item) => item.id !== provider.id) } }))}><Trash2 size={15} />{t('remove')}</button>
                  </article>)}
                </div>
                <button className="add-button" type="button" onClick={addSearchProvider}><Plus size={17} />{t('addCustomSearch')}</button>
              </div>}
            </>}

            {section === 'other' && <>
              <div className="section-heading"><div><span className="eyebrow">{t('appearance')}</span><h3>{t('otherHeading')}</h3><p>{t('otherDescription')}</p></div></div>
              <article className="appearance-card glass-panel"><h4>{t('language')}</h4><p>{t('languageDescription')}</p><div className="theme-options"><button type="button" className={language === 'en' ? 'selected' : ''} onClick={() => setDraft((current) => ({ ...current, ui: { ...current.ui, language: 'en' } }))}><span>{t('english')}</span></button><button type="button" className={language === 'zh-TW' ? 'selected' : ''} onClick={() => setDraft((current) => ({ ...current, ui: { ...current.ui, language: 'zh-TW' } }))}><span>{t('traditionalChinese')}</span></button></div></article>
              <article className="appearance-card glass-panel"><h4>{t('theme')}</h4><div className="theme-options"><button type="button" className={draft.ui.theme === 'dark' ? 'selected' : ''} onClick={() => setDraft((current) => ({ ...current, ui: { ...current.ui, theme: 'dark' } }))}><Moon size={19} /><span>{t('dark')}</span><small>{t('darkDescription')}</small></button><button type="button" className={draft.ui.theme === 'light' ? 'selected' : ''} onClick={() => setDraft((current) => ({ ...current, ui: { ...current.ui, theme: 'light' } }))}><Sun size={19} /><span>{t('light')}</span><small>{t('lightDescription')}</small></button></div></article>
              <article className="appearance-card glass-panel"><div className="scale-header"><div><h4>{t('scale')}</h4><p>{t('scaleDescription')}</p></div><strong>{Math.round(draft.ui.scale * 100)}%</strong></div><input aria-label={t('scale')} className="scale-range" type="range" min="0.8" max="1.35" step="0.05" value={draft.ui.scale} onChange={(event) => setDraft((current) => ({ ...current, ui: { ...current.ui, scale: Number(event.target.value) } }))} /><div className="range-labels"><span>{t('compact')}</span><span>{t('standard')}</span><span>{t('spacious')}</span></div></article>
            </>}
          </main>
        </div>
        <footer className="settings-footer"><span className={noticeError ? 'error-text' : ''}>{notice || t('unsaved')}</span><div><button className="secondary-button" type="button" onClick={onClose}>{t('cancel')}</button><button className="primary-button" type="button" disabled={busy !== null} onClick={() => persist(true)}>{busy === 'save' ? <RefreshCw size={16} className="spin" /> : <Check size={16} />}{t('save')}</button></div></footer>
      </section>
    </div>
  )
}
