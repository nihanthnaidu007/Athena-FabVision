# Athena FabVision

A streaming AI assistant for semiconductor fab engineers and students. Ask a
question in chat and watch the answer stream in, grounded in your own uploaded
fab documents with inline citations, with a wafer-level analysis toolkit one
tool call away and an optional voice mode.

- **Streaming chat** — every answer arrives live over Server-Sent Events, with
  conversation history, sanitized markdown, and a stop button.
- **Cited document Q&A** — upload `.txt`/`.md`/`.csv`/`.pdf` files into your
  personal knowledge base; answers cite the document chunks they used, and the
  citation chip reveals the exact chunk and retrieval score.
- **Wafer analytics** — attach a wafer-bin CSV (`wafer_id,x,y,bin`) and the
  assistant analyzes it as a tool call: yield, bin distribution, edge-ring and
  center-hotspot spatial patterns. Excursion triage turns lot metrics into a
  structured checklist.
- **Usage dashboard** — messages per day, token counts, latency p50/p95, and
  tool-call counts for your account, plus self-service API-key management.
- **API access** — the same agent loop behind a hashed API key with scoped
  hourly throttles and logged usage events.
- **Optional voice** — a LiveKit-powered voice satellite that reuses the exact
  same agent core. Voice is feature-gated: without LiveKit configured, the
  product is simply a (complete) text product.

## Quickstart

Requires Python 3.10+ and Git. The following takes a fresh clone to a running
product in about a minute (install time excluded):

```bash
git clone https://github.com/nihanthnaidu007/Athena-FabVision.git
cd Athena-FabVision

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.lock

cp .env.example .env
# Put a generated secret into DJANGO_SECRET_KEY in .env:
python -c "import secrets; print(secrets.token_urlsafe(64))"

python manage.py migrate
python manage.py createsuperuser
python manage.py collectstatic --noinput
python manage.py runserver
```

Open <http://127.0.0.1:8000/> in a browser. Log in with the account you just
created and you are in the chat — type a question and the answer streams in.

The app boots with **zero integration keys**: with no `OPENAI_API_KEY`, chat
turns answer with an honest `llm_unavailable` error event and the upload panel
reports ingested documents as `pending`. Set `OPENAI_API_KEY` and restart to
turn on real model answers, embeddings, and citations (see
[Configuration](#configuration)).

Prefer the terminal? One agent turn from the command line, using your browser
session:

```bash
# Log in and keep the session cookies (after `createsuperuser`).
curl -s -c cookies.txt http://127.0.0.1:8000/dashboard/accounts/login/ -o /dev/null
CSRF=$(awk '/csrftoken/{print $7}' cookies.txt)
curl -s -b cookies.txt -c cookies.txt -o /dev/null \
  -H "Referer: http://127.0.0.1:8000/" \
  -d "username=engineer&password=<your-password>&csrfmiddlewaretoken=$CSRF" \
  http://127.0.0.1:8000/dashboard/accounts/login/

# Stream one chat turn (session auth; the CSRF token also goes in the header).
CSRF=$(awk '/csrftoken/{print $7}' cookies.txt)
curl -N -b cookies.txt \
  -H "Content-Type: application/json" \
  -H "X-CSRFToken: $CSRF" \
  -d '{"message": "What typically causes edge-ring yield loss?"}' \
  http://127.0.0.1:8000/agent/stream/
```

The response is a `text/event-stream` of `event:`/`data:` frames:

```text
event: status
data: {"stage": "started", ...}

event: delta
data: {"text": "Edge-ring loss usually points to ..."}

event: done
data: {"latency_ms": 1240, ...}
```

(Exact frames, including `tool_call`, `tool_result`, `sources`, and `error`,
are documented in [`docs/api-examples.md`](docs/api-examples.md).)

### First document and first wafer analysis

Upload a document (or use the paper-clip button in the chat composer):

```bash
CSRF=$(awk '/csrftoken/{print $7}' cookies.txt)
curl -s -b cookies.txt -H "X-CSRFToken: $CSRF" \
  -H "Referer: http://127.0.0.1:8000/" \
  -F "file=@path/to/process_notes.md" \
  http://127.0.0.1:8000/kb/documents/
```

Then try the wafer analyzer on the shipped example CSV — it is deterministic,
so it runs even in a no-key deployment:

```bash
python manage.py shell -c "import asyncio; from fabtools.tools import wafer_map_analyze; print(asyncio.run(wafer_map_analyze(None, csv_content=open('fabtools/examples/wafer_map_example.csv').read()))['summary'])"
```

In chat, attach a wafer CSV to a message and just ask — the model invokes the
same tool and the result renders as a wafer-map block.

## Architecture

One Django service, five layers, one shared agent core. The voice worker is a
thin satellite over the same tools — never a second brain.

```text
┌──────────────────────────────────────────────────────────────────┐
│ 5 · Web surface        chat UI (SSE) · dashboard · voice page    │
├──────────────────────────────────────────────────────────────────┤
│ 2 · Shared agent core  one LLM loop: prompt → retrieval → tools   │
│                        → streamed answer (status/delta/tool_*/   │
│                        sources/done/error events)                │
│        ▲                      ▲                      ▲           │
│   REST + SSE API         voice worker                  │
├──────────────────────────────────────────────────────────────────┤
│ 3 · Retrieval (RAG)    upload → extract → chunk → embed → cosine │
├──────────────────────────────────────────────────────────────────┤
│ 4 · Fab tools          wafer_map_analyze · excursion_triage ·    │
│                        kb_search · web_search (optional)         │
├──────────────────────────────────────────────────────────────────┤
│ 1 · Config core        12-factor settings; boots with zero keys  │
└──────────────────────────────────────────────────────────────────┘
```

1. **Config core** — `django_agent/settings.py` reads every variable from the
   environment (`.env` works the same). The app boots with no integration keys
   and reports features that need a key as unavailable, never crashes.
2. **Shared agent core** — `agent/loop.py` + `agent/registry.py`: one async
   LLM loop that retrieves context, calls fab tools, and streams the answer.
   The SSE endpoint, the REST surface, and the voice worker all consume it.
3. **Retrieval (RAG)** — `rag/`: uploads are extracted (pypdf for PDFs),
   chunked, embedded with OpenAI `text-embedding-3-small` (cached by content
   hash), and retrieved by cosine similarity. Documents are user-scoped —
   another user's documents are unreachable by construction.
4. **Fab tools** — `fabtools/`: a typed tool registry. `wafer_map_analyze`
   and `excursion_triage` are always on; `kb_search` is on when embeddings
   are possible; `web_search` registers only when its Google keys exist. The
   model is told what is available — no silent tool failures.
5. **Web surface** — server-rendered Django templates plus one vanilla-JS
   chat client (no node toolchain). Auth, API keys, and scoped throttling sit
   underneath every route.

**Voice satellite** — `voice/`: a livekit-agents 1.x worker
(`python -m voice.worker`) wrapping the same registry with `@function_tool`.
Django mints room-scoped tokens at `POST /voice/token/`; the browser connects
via LiveKit JS. Only exists when all three `LIVEKIT_*` variables are set.

A deeper dive lives in [`docs/architecture.md`](docs/architecture.md).

## Configuration

Copy `.env.example` to `.env` (never commit `.env`). Every variable:

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `DJANGO_SECRET_KEY` | **Yes** | — | Cryptographic signing key. Generate with `python -c "import secrets; print(secrets.token_urlsafe(64))"` |
| `DJANGO_DEBUG` | No | `false` | Debug mode. Keep `false` in production (security headers key off it) |
| `DJANGO_ALLOWED_HOSTS` | No | `localhost,127.0.0.1` | Comma-separated hostnames the site may be served from |
| `DJANGO_SECURE_SSL_REDIRECT` | No | `false` | Redirect plain HTTP to HTTPS. Set `true` **only** when TLS terminates at the app itself (e.g. a directly exposed container serving its own certificate). Behind an edge proxy / load balancer that terminates TLS, leave it `false` — the app would redirect the already-decrypted request and loop. |
| `OPENAI_API_KEY` | No | — | Chat + embeddings. **Optional with degraded mode:** unset, the app boots and serves; chat answers `llm_unavailable`, KB search reports unavailable, uploads ingest as `pending`. |
| `LIVEKIT_URL` | No | — | **Feature-gates voice.** Voice activates only when all three `LIVEKIT_*` variables are set; otherwise the voice page renders an honest setup guide and nothing else changes. |
| `LIVEKIT_API_KEY` | No | — | (with `LIVEKIT_URL` + `LIVEKIT_API_SECRET`) |
| `LIVEKIT_API_SECRET` | No | — | (with `LIVEKIT_URL` + `LIVEKIT_API_KEY`) |
| `GOOGLE_API_KEY` | No | — | Google Programmable Search. Both `GOOGLE_*` variables together enable the `web_search` tool. |
| `GOOGLE_SEARCH_ENGINE_ID` | No | — | (with `GOOGLE_API_KEY`) |

A few more variables exist for less common tuning (all optional, all with
sane defaults): `OPENAI_CHAT_MODEL` (default `gpt-4o-mini`),
`LIVEKIT_TOKEN_TTL` (default `3600`), `DJANGO_HSTS_SECONDS` (default
`31536000`), `DJANGO_LOGIN_URL` (default `/dashboard/accounts/login/`), and
`DJANGO_ENV_FILE` (default `.env`).

The database is SQLite (`db.sqlite3`, git-ignored) — zero-config for a
self-hosted single-container deployment.

## HTTP surface

| Route | Method | Auth | Purpose |
| --- | --- | --- | --- |
| `/` | GET | session | Chat UI (login required) |
| `/agent/stream/` | POST | session or API key | One agent turn as an SSE stream |
| `/agent/ask/` | POST | session or API key | Legacy one-shot REST shim |
| `/kb/documents/` | POST | session or API key | Knowledge-base upload (multipart `file`) |
| `/voice/` | GET | session | Voice page (both configured/unconfigured states) |
| `/voice/token/` | POST | session or API key | Mint a LiveKit room token (throttled) |
| `/dashboard/` | GET | session | Usage analytics (7/30-day windows) |
| `/dashboard/keys/` | GET/POST | session | API-key management |
| `/admin/` | — | staff | Django admin, moderation, CSV export |
| `/healthz/` | GET | none | Liveness (process only) |
| `/healthz/ready/` | GET | none | Readiness (database + feature booleans) |

API-key authentication accepts `X-API-Key: <key>`, `Authorization: Bearer
<key>`, or `Authorization: ApiKey <key>`. Keys are stored only as SHA-256
hashes; revocation is checked on every request. Full request/response
examples, including the SSE frame contract, are in
[`docs/api-examples.md`](docs/api-examples.md).

## Deployment

The repository ships a single-container production image: Gunicorn serves the
Procfile command, a boot migration makes the container self-sufficient, and
static files are collected at build time.

```bash
docker build -t athena-fabvision .

docker run --rm -p 8000:8000 \
  -e DJANGO_SECRET_KEY='<generated-secret>' \
  -e DJANGO_DEBUG=false \
  athena-fabvision
```

For local compose-based verification, `docker compose up` builds the same
image and serves on port 8000.

- **Health probes** — point orchestrators at `GET /healthz/` (liveness:
  process-only, restarts a broken container) and `GET /healthz/ready/`
  (readiness: verifies the database, reports which optional integrations are
  configured; answers 503 so traffic routes away without restarting). The
  Dockerfile's `HEALTHCHECK` uses `/healthz/`.
- **TLS** — terminate TLS at your edge (load balancer / reverse proxy) and
  leave `DJANGO_SECURE_SSL_REDIRECT=false`; only set it `true` when the
  container itself serves TLS. HSTS and other security headers engage when
  `DJANGO_DEBUG=false`.
- **SSE through proxies** — the app already sends `X-Accel-Buffering: no`. If
  you front it with nginx, disable buffering on the location
  (`proxy_buffering off;`) so deltas arrive live instead of coalescing.
- **Voice worker** — run `python -m voice.worker` as a separate long-lived
  process next to the web container; it only connects when the `LIVEKIT_*`
  variables are configured.
- **Render-style PaaS** — `render.yaml` and the `Procfile` (`web`, `release`,
  `voice`) cover platforms that build from the repo.

## Development

```bash
pip install -r requirements-dev.txt   # pytest, ruff, pip-tools

ruff check .                          # lint (E/F/W/I/B/UP, line length 100)
pytest                                # the full test suite
```

Dependencies are managed with [pip-tools](https://github.com/jazzband/pip-tools):
`requirements.txt` states the runtime dependencies (with security-audited
minimums explained in its comments), and `requirements.lock` is the compiled,
reproducible pin set that CI and the Dockerfile install. After changing
`requirements.txt`, recompile with:

```bash
pip-compile requirements.in -o requirements.lock   # or: pip-compile requirements.txt -o requirements.lock
```

`requirements-dev.txt` includes `requirements.txt`, so dev installs always
match the runtime floor set.

## License

MIT — see the notice below.

```
MIT License

Copyright (c) 2024 Nihanth Naidu K

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the “Software”), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```
