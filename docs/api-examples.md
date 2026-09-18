# API examples

Every endpoint below requires authentication (browser session or API key)
except the two health probes. Examples assume the server on
`http://127.0.0.1:8000`.

## Authentication

```bash
BASE=http://127.0.0.1:8000

# Option A: browser session (cookie jar + CSRF token)
curl -s -c cookies.txt $BASE/dashboard/accounts/login/ -o /dev/null
CSRF=$(awk '/csrftoken/{print $7}' cookies.txt)
curl -s -b cookies.txt -c cookies.txt -o /dev/null \
  -H "Referer: $BASE/" \
  -d "username=engineer&password=<your-password>&csrfmiddlewaretoken=$CSRF" \
  $BASE/dashboard/accounts/login/

# Option B: API key (create one in the dashboard at /dashboard/keys/)
# Accepted headers:  X-API-Key: <key>   |   Authorization: Bearer <key>
#                    Authorization: ApiKey <key>
```

Session-authenticated (non-GET) requests must send the CSRF token in the
`X-CSRFToken` header with `Referer` set.

## Streaming chat — `POST /agent/stream/`

One agent turn as a `text/event-stream`. Body: `{"message": "...",
"conversation_id": <optional id>}` (continues that conversation when given,
starts a new one otherwise). `mode` (optional, `"assistant"` or `"tutor"`)
sets a *newly created* conversation's mode; an existing conversation's
persisted mode always rules — `POST /agent/chat/mode/` with form fields
`conversation_id` and `mode` changes it (session login or API key
required; form-encoded like the other chat CRUD routes). A tutor
conversation responds with hints and guiding questions instead of final
answers.

```bash
curl -N -b cookies.txt \
  -H "Content-Type: application/json" \
  -H "X-CSRFToken: $CSRF" \
  -d '{"message": "What typically causes edge-ring yield loss?"}' \
  $BASE/agent/stream/
```

The stream emits `event:` / `data:` frames. Event types and payloads:

| Event | Data | Meaning |
| --- | --- | --- |
| `status` | `{"stage": "started", "conversation_id": 12, "message_id": 345}` | Turn accepted and persisted |
| `status` | `{"stage": "retrieving"}` | Searching the knowledge base |
| `status` | `{"stage": "generating"}` | Model streaming begins |
| `delta` | `{"text": "Edge-ring loss usually..."}` | One chunk of the answer (render incrementally) |
| `tool_call` | `{"name": "wafer_map_analyze", "arguments": "{...}", "call_id": "..."}` | Model invoked a fab tool |
| `tool_result` | `{"name": "wafer_map_analyze", "block": {"type": "wafer_map", ...}}` | Structured tool result block |
| `sources` | `{"sources": [{"kind": "doc", "title": "...", "chunk_index": 3, "score": 0.82}, {"kind": "tool", "tool": "wafer_map_analyze", "summary": "..."}]}` | Citations for this turn |
| `done` | `{"latency_ms": 1240, "message_id": 346}` | Turn complete and persisted |
| `error` | `{"code": "llm_unavailable", "error": "The model is not configured on this deployment."}` | Turn failed honestly (e.g. no `OPENAI_API_KEY`) |
| `error` | `{"code": "agent_error", "error": "The assistant turn failed: ..."}` | Turn failed mid-flight |

A `request_id` field accompanies payloads for support correlation. Failures
are `error` events on the same stream — the stream itself never breaks.
Example session with a configured model:

```text
event: status
data: {"stage": "started", "conversation_id": 12, "message_id": 345, "request_id": "..."}

event: status
data: {"stage": "retrieving", "request_id": "..."}

event: status
data: {"stage": "generating", "request_id": "..."}

event: delta
data: {"text": "Edge-ring yield loss usually indicates ", "request_id": "..."}

event: delta
data: {"text": "a process step problem at the wafer perimeter.", "request_id": "..."}

event: sources
data: {"sources": [{"kind": "doc", "title": "process_notes.md", "chunk_index": 3, "score": 0.82}], "request_id": "..."}

event: done
data: {"latency_ms": 1240, "message_id": 346, "request_id": "..."}
```

## Document upload — `POST /kb/documents/`

Multipart form, `file` field. Accepted: `.txt`, `.md`, `.csv`, `.pdf` (≤10 MiB
by default). Ingestion is synchronous; the response reports the honest
resulting state.

```bash
curl -s -b cookies.txt -H "X-CSRFToken: $CSRF" -H "Referer: $BASE/" \
  -F "file=@process_notes.md" \
  $BASE/kb/documents/
# {"id": 7, "status": "ready", "chunks": 12}        (with OPENAI_API_KEY)
# {"id": 7, "status": "pending", ...}               (degraded: no embeddings key)
```

Errors follow the JSON error contract:

```json
{"error": "Unsupported file type \".docx\"; accepted: .txt, .md, .csv, .pdf.",
 "code": "validation_error", "request_id": "..."}
```

## Retired endpoint — `POST /agent/ask/`

The legacy one-shot shim (which answered canned keyword-routed responses
without the model) was retired in v1.1. It now answers a permanent
`410 Gone` for every method and meters nothing, following the JSON
error contract:

```json
{"error": "This endpoint was retired; use POST /agent/stream/ for agent turns.",
 "code": "endpoint_retired", "request_id": "..."}
```

Use `POST /agent/stream/` for agent turns.

## LiveKit voice token — `POST /voice/token/`

Mints a LiveKit token scoped to the requesting user's own room (throttled).
Requires all three `LIVEKIT_*` variables server-side; otherwise answers the
JSON error contract.

```bash
curl -s -H "X-API-Key: $ATHENA_KEY" -X POST $BASE/voice/token/
# {"token": "<jwt>", "url": "wss://your-livekit-host"}
```

## Health probes — no auth

```bash
curl -s $BASE/healthz/
# {"status": "ok"}

curl -s $BASE/healthz/ready/
# {"status": "ok", "checks": {"database": "ok"},
#  "features": {"openai": true, "livekit": false}}
```

Readiness answers `503` when the database is unreachable (route traffic away;
the process itself is fine) and exposes only booleans — never key material.
