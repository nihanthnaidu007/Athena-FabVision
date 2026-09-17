# Codebase Map — Athena-FabVision

Depth-2 map. Code dirs, committed artifacts, and root files only (`node_modules`-style dirs: none).

| Path | Kind | What it is |
|---|---|---|
| `agent/` | Django app | Text agent: `/agent/` UI route, `/agent/ask/` DRF API (`views.py`), canned response logic (`agent_logic.py` imports `livekit.agents.llm`), empty placeholder `models.py`/`tests.py` |
| `agent/migrations/` | Django migrations | Only `__init__.py` — app has no models, no generated migrations |
| `agent/templates/agent/` | Django templates | `index.html` — textarea + "Ask the Agent" button UI |
| `django_agent/` | Django project | `settings.py` (SQLite DB, decouple-based env config, `ALLOWED_HOSTS=['*']`), `urls.py` (routes `/`, `/agent/`, `/admin/`), `wsgi.py`, `asgi.py`, collected static |
| `ai/` | Committed venv (macOS) | pyenv 3.10.9 venv from the original author's Mac; unusable on Linux — ignore, recreate with `uv venv --python 3.10` |
| `staticfiles/` | Collected static | `collectstatic` output (admin, DRF static); regenerated, safe to ignore |
| `__pycache__/` | Bytecode | Committed stray bytecode; ignore |
| `main.py` | Entry point | LiveKit voice worker: Silero VAD + OpenAI STT/LLM/TTS, `VoiceAssistant` loop, loads `instructions.txt` |
| `api.py` | Voice tools | `AssistantFnc` function context: `tell_joke`, `provide_insight`, `get_current_datetime`, `search_web` (Google CSE) |
| `instructions.txt` | Prompt | Domain system prompt for the voice assistant (semiconductor focus) |
| `manage.py` | Django CLI | migrate / runserver / test entry |
| `requirements.txt` | Manifest | 62 pinned deps (Django 5.1.3, DRF 3.15.2, livekit-agents 0.11.1, openai 1.53.0, …) |
| `Procfile` | Proc spec | `web: gunicorn --bind 0.0.0.0:$PORT django_agent.wsgi:application` |
| `db.sqlite3` | SQLite DB | Committed dev database (18 migrations applied at onboarding) |
| `.gitignore` | VCS config | Ignores only `.env` and `__pycache__/` |
| `README.md` | Docs | Full feature/install/run guide (source of truth for this map) |
