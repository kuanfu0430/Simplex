# Simplex Technical and Operations Guide

[Back to the entry README](../README.en.md) · [繁體中文技術文件](TECHNICAL_GUIDE.zh-TW.md)

This document covers installation, configuration, the research pipeline, conversation context, PDF handling, APIs, security, and testing. Start with the entry README if you are new to the project.

## 1. Design goals

Simplex is designed around one trade-off: speed comes first, but evidence quality must not depend on model guesswork.

- Search, Judges, crawling, and answering have separate responsibilities; search snippets are never treated as facts by themselves.
- Without an explicit URL, ordinary follow-up questions do not automatically add crawls or unlimited agent loops.
- With an explicit URL, the system reads the source first, then lets the query planner and Judge decide whether a direct answer is safe or more search is needed.
- Multi-turn conversations keep controlled history and an evidence capsule instead of accumulating every old tool output in every prompt.
- Citable content must come from deep-crawled source chunks. The source registry assigns citation numbers again after evidence is merged.

## 2. Installation and startup

### Native installation

Requirements: Python 3.11+, Git, and Node.js/npm. Run:

```bash
./simplex install
./simplex start
```

The launcher creates and prepares:

- The Simplex Python environment: `.venv/`
- The SearXNG Python environment: `.runtime/searxng-venv/`
- The Playwright/Patchright Chromium runtime
- Dependencies for PDF extraction and Tesseract OCR

Services bind only to the local loopback interface:

| Service | Address | Purpose |
|---|---|---|
| Simplex Web | `http://127.0.0.1:8787` | FastAPI, SSE, and frontend |
| SearXNG | `http://127.0.0.1:8888` | Local search infrastructure |

Check the installation with:

```bash
./simplex doctor
```

Stop the foreground launcher with `Ctrl-C`. Startup checks the ports first; do not replace the loopback binding with `0.0.0.0`.

### Docker

```bash
docker compose up --build
```

Then open `http://127.0.0.1:8787/`. Compose maps the host's local port to the Web service and keeps SearXNG inside the container network. Set a random `SEARXNG_SECRET` in `.env` before long-running deployments.

### Environment variables

`.env.example` contains examples only and must not contain secrets. Common settings include:

```dotenv
SEARXNG_URL=http://127.0.0.1:8888
PDF_ENABLE_OCR=true
PDF_OCR_LANGUAGES=eng+chi_tra+chi_sim+jpn
```

Provider API keys entered through the Web Settings page are stored in the local SQLite database, encrypted with Fernet, and never returned to the frontend in plaintext.

## 3. First-time configuration

The Settings panel is divided into three groups.

### Models

1. Enter and save a Provider API key.
2. Use **Sync models** to retrieve available models.
3. Add models that should be selectable for an individual question to the **Model pool**.
4. Select the **Question model** and **Judge model** separately.

The Question model plans queries, routes follow-up questions, and writes the final cited answer. The Judge model selects URLs, selects chunks, and evaluates evidence sufficiency. The Model pool is only the list of models available for a specific question; it is not a separate “research model” role in the pipeline.

### Search services

Simplex supports two mutually exclusive modes:

- **Native SearXNG** uses the local SearXNG service and does not require commercial search API keys.
- **Custom search engines** disables built-in SearXNG and sends queries only to enabled custom JSON or commercial providers with complete configuration.

Available external providers include Brave, Tavily, Exa, SerpApi, and custom JSON APIs. A custom API can configure GET/POST, authorization, query/count fields, the result-array path, and field mappings.

### Other settings

- **UI language:** English by default, with Traditional Chinese available.
- **Theme:** Dark or Light.
- **Interface scale:** Scales the interface as a whole rather than enlarging text alone.

Language settings affect the interface, status and error messages, research trace, and generated answer. The answer model always answers in the language of the user's original question, even when search uses another language.

## 4. Research pipeline

```mermaid
flowchart LR
  Q[Question or follow-up] --> U{HTTP(S) URL in this turn?}
  U -->|Yes| DC[Read the URL directly]
  DC --> DJ[Judge the provided chunks]
  DJ --> DR{Evidence sufficient?}
  DR -->|Yes| A[Answer model]
  DR -->|No| P[Query planning]
  U -->|No| R{Previous evidence sufficient?}
  R -->|Yes| A
  R -->|No| P
  P --> S[SearXNG or search provider]
  S --> F[URL Judge]
  F --> C[Crawl4AI, HTTP, or PDF]
  C --> K[Split into chunks]
  K --> J[Chunk Judge]
  J --> E[Merge evidence and citations]
  E --> A
```

### 4.1 Query planning

The planner first creates a `standalone_question` and then chooses a `strategy`:

- `reuse`: the question can be answered completely from previously validated evidence.
- `direct`: the URL(s) provided in this turn are sufficient, and the user did not request freshness, external verification, or additional sources.
- `research`: new facts, current information, verification, broader coverage, or planner uncertainty requires a new search.

When the strategy is `research`, the planner prepares three complementary queries. Search language is chosen from the subject matter rather than mechanically following the user's language. Time-sensitive questions follow the freshness requested by the user and search for the latest available dates.

### 4.2 Search routing and budgets

Current search routing is:

| Mode | SearXNG routing | Purpose |
|---|---|---|
| Web | `general` | General web information |
| Academic | `general` + `science` | Academic sources plus general context |
| Social | `general` + `social media` | Social perspectives plus general context |

Results from multiple providers are canonicalized, deduplicated, and recorded in the source trace before they reach the URL Judge. If a specialized provider is unavailable, enabled providers can still handle the query.

### 4.3 URL Judge

The URL Judge sees search candidate titles, snippets, and source metadata. It selects pages worth deep crawling; it is not the final answer model, and snippets are not answer facts. Selection preserves source diversity and conflicting viewpoints instead of assuming that one position is correct.

### 4.4 Deep crawling and PDFs

The single-page crawl order is:

1. Extract the main text through a fast HTTP path.
2. If the page is an SPA, interactive, or too low quality, use Playwright/Patchright.
3. Prefer PyMuPDF for PDFs, with pypdf as a fallback.
4. When a PDF is scanned or the extracted text is insufficient, use Tesseract OCR according to the configured languages.

The default OCR languages are `eng+chi_tra+chi_sim+jpn`. PDF extraction preserves page boundaries, removes repeated headers/footers and layout noise, and joins short CJK layout lines into main-text chunks that can be reviewed by the Judge. See the [PDF chunk diagnosis](pdf-chunk-diagnosis-jas-hkbu.md) for a concrete extraction case.

### 4.5 Chunk Judge and answer generation

The Chunk Judge selects a minimally sufficient evidence set from deep-crawled text and reports:

- `verdict`: `sufficient` or `insufficient`.
- `coverage`: covered aspects and remaining gaps.
- `selected_chunks`: source excerpts that may be cited.
- `next_search_queries`: queries needed to fill gaps.

The final answer model receives only the merged evidence bundle of source chunks. Conversation answers, search snippets, query plans, content maps, and source registries are not evidence by themselves. Citation markers are placed immediately after the sentences that use a source.

## 5. Direct URL mode

When the user includes HTTP(S) URLs in the current question:

1. URLs are extracted and canonicalized. A single turn accepts at most five; exceeding the limit is reported explicitly rather than silently dropping URLs.
2. The sources are read through the same safe HTTP/JS/PDF pipeline used for search results.
3. Bounded relevant chunks are sent to the query planner, while the URL-content Judge runs in parallel with planning.
4. Normal search is skipped only when every URL was read successfully, the Judge says the evidence is sufficient, and the planner selects `direct`.
5. Insufficient content, crawl errors, a request for current information or external verification, or forced research switches to `hybrid` or normal `research`.

Direct URLs are added to the current search exclusion set so the same page is not deep-crawled twice in one turn. Every direct URL goes through DNS, private-address, loopback-address, and redirect safety checks.

This is a bounded preflight decision. It does not let an LLM freely choose unlimited `instant`, `fast`, or `full` loops, and it does not turn Simplex into unrestricted agentic search.

## 6. Multi-turn conversations and evidence capsules

### 6.1 What enters the next turn

The next turn keeps:

- Controlled user/assistant history.
- An encrypted, validated evidence capsule.
- A small set of prior evidence selected by query terms and source-diversity rules.
- No unbounded replay of all previous snippets, tool traces, or full crawl bodies into the planner prompt.

This preserves references and discussion context while preventing token cost and lost-in-the-middle effects from growing with every turn.

### 6.2 Reuse, research, and refresh

An ordinary follow-up that is fully supported by prior evidence uses `reuse` and does not start a deep search. Current-information requests, verification requests, explicit requests for new sources, or evidence gaps use `research`.

The planner can request a refresh only through a `source_ref` from the evidence ledger, and only when the user explicitly asks to reread, recheck, update, or extract details from an earlier source. The backend accepts at most two validated URLs and refreshes them in parallel with the first research loop. Refreshed chunks replace old evidence from the same URL so both versions do not contaminate the answer.

## 7. Execution depth

| Mode | Behavior | Suitable for |
|---|---|---|
| `instant` | One search/evidence pass with no gap-filling | A clear question where first-token speed matters most |
| `fast` | One lightweight gap-filling pass when the first pass is insufficient | Default research and follow-ups |
| `full` | Up to three passes that fill Judge-reported gaps step by step | More complete multi-angle reports |

Frontend Fast/Full are research budget ceilings. URL or chunk Judges may mark evidence insufficient, but they cannot expand the pipeline without a bound.

## 8. Web API and SSE

### 8.1 Main endpoints

- `GET /api/health`: SearXNG, crawler, Chromium, and OCR status.
- `GET /api/ready`: process readiness.
- `GET/PUT /api/settings`: read or save masked settings.
- `GET /api/llm/providers/{id}/models`: sync provider models.
- `POST /api/search-engines/{id}/test`: test a search-service configuration.
- `POST /api/search/stream`: start a research and answer stream.

### 8.2 Search request

```json
{
  "question": "Compare the main positions in these sources",
  "search_mode": "academic",
  "mode": "fast",
  "conversation_history": [
    {"role": "user", "content": "The previous question"},
    {"role": "assistant", "content": "The previous answer"}
  ],
  "context_capsules": ["A capsule issued and encrypted by the backend"],
  "force_research": false
}
```

`model_selection` may select the default answer model or a model from the Model pool. `context_capsules` are backend-issued encrypted values; arbitrary client-created evidence is not accepted.

### 8.3 SSE events

Typical order:

1. `status`: planning, direct crawl, searching, answering, and other states.
2. `research_trace`: queries, sources, URL selection, direct sources, chunks, and stage details.
3. `answer_start`: evidence, sources, capsule, and initial timings before answer generation.
4. Multiple `answer_delta` events: streamed answer fragments.
5. `result`: complete answer, `research_strategy`, sources, evidence bundle, and timings.
6. `done`: end of the turn.

`research_strategy` can be:

- `reuse`: answer from previous evidence.
- `direct`: use only URLs provided in the current turn.
- `hybrid`: combine provided/refreshed sources with a new search.
- `research`: normal search research.

`timings` records `planning_ms`, `research_ms`, `direct_crawl_ms`, `direct_judge_ms`, `answer_first_token_ms`, `answer_ms`, and `total_ms`.

## 9. Frontend and code structure

```text
simplex_app/                  FastAPI, encrypted settings, model settings, SSE routes
frontend/                     React + Vite + TypeScript PWA
deep_search_tool.py           Search, budgets, Judges, crawling, chunks, orchestration
pro_search_crawl_backend.py   HTTP/JS/PDF crawling core
crawl4ai_pdf.py               PDF extraction and OCR
searxng/settings.yml          SearXNG general/science/social configuration
scripts/                      Installer, doctor, and two-service supervisor
docs/                         Technical documents and diagnosis reports
tests/                        Offline regression tests
```

The frontend `ResearchTracePanel` aggregates SSE trace events into expandable queries, sources, direct URLs, and chunks. Conversation and research history stay in browser-local storage; model API keys and evidence capsules are not stored as plaintext in frontend history.

## 10. Security boundaries

- Web and MCP services bind to `127.0.0.1` only and should not be exposed directly to the public internet.
- Search results, user-provided URLs, and controlled refresh URLs are DNS-resolved before crawling; private, loopback, link-local, and reserved addresses are rejected.
- Every HTTP redirect is checked again to prevent a public URL from being used to reach an internal network.
- `.env`, `data/`, `.runtime/`, `.venv/`, frontend node_modules/dist, database files, and key files must not be committed.
- API settings return empty values and `has_api_key`, never plaintext secrets.
- Evidence capsules are encrypted and authenticated with a local Fernet key; tampered, expired, or undecryptable capsules are discarded.
- The service rejects untrusted Host values, cross-site Origins, and requests that do not meet local-request metadata requirements.

## 11. Development and testing

After installing development dependencies:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
npm --prefix frontend run lint
npm --prefix frontend run build
bash -n scripts/*.sh simplex "Simplex Search.command"
./simplex doctor
```

For a fast backend syntax check:

```bash
.venv/bin/python -m py_compile \
  simplex_app/main.py simplex_app/conversation.py \
  simplex_app/llm.py deep_search_tool.py
```

New or modified research routing should cover at least: a normal follow-up without URLs, a direct URL route, a URL-insufficient hybrid route, controlled refresh of an earlier source, URL safety, and evidence citations.

## 12. Troubleshooting

### The service starts but search returns nothing

Run `./simplex doctor` and confirm that SearXNG is available at `127.0.0.1:8888`. Then confirm that Settings contains at least one valid search provider. External search-engine rate limits may leave SearXNG with partial results; that does not necessarily mean the local Simplex service is broken.

### Why did an explicit URL not use direct mode?

`direct` is deliberately conservative. One failed URL, insufficient text, a Judge-reported gap, planner uncertainty, a freshness request, or a request for external verification is enough to switch to `hybrid` or `research`.

### Why did a follow-up not refresh an earlier source?

The planner emits a valid `source_ref` only when the user explicitly asks to reread, verify, update, or extract details from an earlier source. Ordinary follow-ups reuse existing evidence or run a new search; they do not recrawl every old URL automatically.

### PDF chunks are poor

Check the OCR language packages and the PyMuPDF/Tesseract installation first. Then consult the [PDF chunk diagnosis](pdf-chunk-diagnosis-jas-hkbu.md) for extraction, cleanup, page-boundary, and fallback recommendations.
