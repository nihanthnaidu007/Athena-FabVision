# Architecture

One Django service, five layers, one shared agent core. Every surface — the
chat UI, the SSE/REST API, and the optional voice worker — consumes the same
agent loop, so behavior never forks between web and voice.

```text
┌──────────────────────────────────────────────────────────────────┐
│ 5 · Web surface        chat UI (SSE) · dashboard · voice page    │
├──────────────────────────────────────────────────────────────────┤
│ 2 · Shared agent core  one LLM loop: prompt → retrieval → tools   │
│                        → streamed answer                         │
│        ▲                      ▲                      ▲           │
│   REST + SSE API         voice worker                            │
├──────────────────────────────────────────────────────────────────┤
│ 3 · Retrieval (RAG)    upload → extract → chunk → embed → cosine │
├──────────────────────────────────────────────────────────────────┤
│ 4 · Fab tools          wafer_map_analyze · excursion_triage ·    │
│                        spc_rules_check · kb_search ·             │
│                        web_search (optional)                     │
├──────────────────────────────────────────────────────────────────┤
│ 1 · Config core        12-factor settings; boots with zero keys  │
└──────────────────────────────────────────────────────────────────┘
```

## Layer 1 · Config core

`django_agent/settings.py` + `django_agent/config.py`: every variable is read
from the environment (a repo-root `.env` works identically, via
python-decouple). A missing **required** variable fails startup with a named
error (`MissingEnvironmentVariable`) pointing at `.env.example`; every
*integration* key is optional. With zero keys the app boots, serves, and
reports the features that need a key as unavailable — degraded-but-alive.

## Layer 2 · Shared agent core

`agent/loop.py` (the async agent loop), `agent/llm.py` (the injectable
streaming LLM client; OpenAI wrapped behind an `LLMClient` protocol so tests
inject fakes), `agent/registry.py` (tool registry). One turn:

1. Persist the user message.
2. `status` event: `started` → `retrieving`.
3. Retrieve knowledge-base context for the asking user (may be empty).
4. `status` event: `generating`; the model streams text deltas, which are
   forwarded immediately as `delta` events.
5. On a tool call: execute through the registry, emit `tool_call` /
   `tool_result`, feed the result back to the model, and continue.
6. Emit `sources` (documents + tools used), then `done` (latency), persisting
   the assistant message with sources and usage; record a `UsageEvent`.

Failures inside a turn are `error` events on the same stream — the stream
itself never breaks. Without an LLM key the loop answers a single honest
`error` event (`code: "llm_unavailable"`).

## Layer 3 · Retrieval (RAG)

`rag/`: `ingestion.py` (extract — pypdf for PDFs, txt/md/csv as text — chunk,
embed), `embeddings.py` (OpenAI `text-embedding-3-small`, cached by content
hash), `retrieval.py` (cosine similarity over the user's own chunks). Models
live in `assistant/models.py`: `Document` (status `processing` → `ready` /
`failed`, with the honest failure reason) and `Chunk` (text + embedding JSON).
Documents are user-scoped; cross-user access 404s by construction through the
`for_user` querysets.

## Layer 4 · Fab tools

`fabtools/` — the differentiating layer, exposed to the model through the one
typed registry (`agent/registry.py`):

| Tool | Availability | What it does |
| --- | --- | --- |
| `wafer_map_analyze` | always | Wafer-bin CSV (`wafer_id,x,y,bin`) → yield, bin histogram, edge-ring / center-hotspot spatial patterns, rendered as a `wafer_map` block |
| `excursion_triage` | always | Lot metrics → rule-based triage table |
| `spc_rules_check` | always | A measurement series (CSV/JSON text or list, 2–1000 points, optional known sigma) → control limits (`x̄ ± 3σ`, estimated from the mean moving range when sigma is unknown), the eight Nelson rules, an SVG run chart with violation markers, and a plain-language verdict, rendered as an `spc_chart` block |
| `kb_search` | when embeddings are possible | Cited search over the user's knowledge base |
| `web_search` | only when `GOOGLE_API_KEY` + `GOOGLE_SEARCH_ENGINE_ID` are set | Google Programmable Search |

Availability is computed per request and the model is told what it can call —
no silent tool failures. Tool results are structured blocks (`table`,
`wafer_map`, `spc_chart`, `text`, `error`) rendered in the UI and fed back to
the model.

## Layer 5 · Web surface

Server-rendered Django templates plus one vanilla-JS chat client
(`agent/static/agent/chat.js`; markdown via vendored `marked` + `DOMPurify`).
No node toolchain. `dashboard/` renders usage analytics and API-key
management. `assistant/` owns the data model, API-key authentication,
throttling, and usage recording.

## Voice satellite

`voice/`: a livekit-agents 1.x worker (`Agent` / `AgentSession` /
`@function_tool`, `voice/agent.py` + `voice/worker.py`) that reuses layers
2–4. Django mints a room-scoped token at `POST /voice/token/` (auth required,
throttled); the browser connects through LiveKit JS. The worker only exists
when all three `LIVEKIT_*` variables are configured, and reconnects across
LiveKit signal drops. It never blocks the web service.
