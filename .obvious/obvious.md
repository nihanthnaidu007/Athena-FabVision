# Athena-FabVision — Agent Guidance

Repo: `nihanthnaidu007/Athena-FabVision` ("Athena – AI Voice & Web Assistant for Semiconductor Analysis").
A Django 5 web app + REST endpoint for a text agent, plus an optional LiveKit/OpenAI voice-assistant worker.

## Stack
- Python 3.10 (pinned deps target 3.10; sandbox default 3.13 has no wheels for several pins — see Gotchas)
- Django 5.1.3 + Django REST Framework 3.15.2 (installed from `requirements.txt`)
- Database: SQLite (`db.sqlite3`, committed; no external DB service)
- LiveKit agents 0.11.1 + OpenAI plugins (voice worker only; requires external LiveKit server + OpenAI keys)
- Production: gunicorn via `Procfile` (no Dockerfile/Compose/Makefile in repo)

## Commands
```bash
# Setup (sandbox has uv-managed CPython 3.10.21)
uv venv --python 3.10 .venv
uv pip install --python .venv/bin/python -r requirements.txt

# Required env (settings.py boots without these via decouple defaults only for SECRET_KEY/DEBUG;
# the six keys below have NO default and crash startup) — see .env.example note: repo has none, use local .env (gitignored)
# SECRET_KEY, DEBUG, OPENAI_API_KEY, LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET,
# GOOGLE_API_KEY, GOOGLE_SEARCH_ENGINE_ID

.venv/bin/python manage.py migrate      # apply migrations (SQLite)
.venv/bin/python manage.py runserver 0.0.0.0:8000   # dev server, port 8000
.venv/bin/python manage.py test         # test suite (currently empty placeholder)
.venv/bin/python manage.py check        # system checks
```

## Codebase map
See [.obvious/codebase-map.md](codebase-map.md). Short version:
- `django_agent/` — Django project (settings, root URLs, wsgi/asgi); `/` home, `/agent/` app, `/admin/`
- `agent/` — app: `/agent/` UI, `/agent/ask/` DRF POST API, canned text logic in `agent/agent_logic.py`
- `main.py`, `api.py`, `instructions.txt` — LiveKit voice worker and its LLM tools
- `ai/` — committed macOS pyenv 3.10.9 venv; ignore on Linux, never use it

## Local verification
Verified working (2026-09-17 onboarding):
- `manage.py check` → "System check identified no issues"
- `manage.py migrate` → 18 migrations applied
- Dev server on 0.0.0.0:8000 → `GET /` 200, `GET /agent/` 200 (UI renders)
- `POST /agent/ask/ {"query":"tell me a joke about chips"}` → 200 with joke; `{}` → 400 error JSON
- `manage.py test` → OK (0 tests, matches README)

## Sandbox snapshot
- Snapshot ID: `l3e6wjz4kay7d3qc3xmk:default` (captured 2026-09-17T19:00:23Z with dev stack running)
- Restores: Python 3.10 venv at `.venv/`, deps installed, migrations applied, `.env` present

## Gotchas
- **Python version**: on CPython 3.13, `pillow==10.3.0` (and other pins) have no wheels and fail to build — always use the uv-managed 3.10 venv.
- **Committed venv `ai/`**: macOS-only (pyvenv.cfg points to `/Users/kalisettinihanthnaidu/...`). Do not use; do not delete without owner confirmation.
- **`.env` is mandatory at boot**: `settings.py` reads six keys via `config()` with no default; missing keys raise `UndefinedValueError`. Dummy values are fine for web dev — they are only consumed by the optional voice worker.
- **db.sqlite3 is committed**: migrations apply on top of it; watch for stray `db.sqlite3-journal`/`-wal` before starting the server.
- **Voice worker (`python main.py`)** TODO(confirm): needs a reachable LiveKit server + real OpenAI/Google keys; not exercisable in local-only dev, not covered by the Procfile.
- **No lint/typecheck config** in repo; `manage.py check` + `manage.py test` are the only automated gates.

## Policies
- Default branch: `main` (from GitHub repo settings)
- Merge method: merge commit (squash and rebase also allowed); do not auto-delete branch on merge
