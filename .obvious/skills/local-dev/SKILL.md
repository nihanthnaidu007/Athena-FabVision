---
name: local-dev
description: Proven recipe for booting Athena-FabVision local dev in the repo sandbox
---

# local-dev — Athena-FabVision

Recorded during automated onboarding (2026-09-17). Result: `dev_stack_healthy: true`.

## What the stack is
Django 5.1.3 web app (SQLite, no external services) + optional LiveKit voice worker (`main.py`).
The web app is the primary dev flow and the only thing the Procfile runs.

## Recipe (verified)
1. **Python 3.10, not the sandbox default 3.13.** Pinned deps (`pillow==10.3.0`, `av==13.1.0`,
   `livekit==0.18.0`) have no cp313 wheels and their sdist builds fail
   (`KeyError: '__version__'` from pillow's setup). Use uv:
   ```bash
   export PATH="$HOME/.local/bin:$PATH"
   uv venv --python 3.10 .venv
   uv pip install --python .venv/bin/python -r requirements.txt   # all 62 pins, wheel-only, ~5s
   ```
2. **Write `.env` before anything imports settings.** `django_agent/settings.py` reads
   `OPENAI_API_KEY`, `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`,
   `GOOGLE_API_KEY`, `GOOGLE_SEARCH_ENGINE_ID` via `decouple.config()` with **no defaults**
   → startup crashes with `UndefinedValueError` without them. Dummy values are safe for web
   dev (only the optional voice worker consumes them); `.env` is gitignored.
3. **Migrate**: `.venv/bin/python manage.py migrate` → applies to the committed `db.sqlite3`
   (18 migrations). Check for stray `db.sqlite3-journal` / `-wal` first.
4. **Run**: `.venv/bin/python manage.py runserver 0.0.0.0:8000 --noreload` (dev port 8000
   per README; confirm with `ss -ltnp | grep 8000`).
5. **Verify primary flow**:
   - `GET /` → 200; `GET /agent/` → 200 with "Ask the Agent" UI
   - `POST /agent/ask/` `{"query":"tell me a joke about chips"}` → 200 `{"response": "...joke..."}`
   - `POST /agent/ask/` `{}` → 400 `{"error":"Query parameter is required"}` (validation path)
   - `GET /admin/` → 302 to login (expected)
   - `.venv/bin/python manage.py test` → OK (0 tests — placeholder suite per README)
   - `.venv/bin/python manage.py check` → no issues

## Known limitations
- `main.py` voice worker needs a real LiveKit server + OpenAI keys — not exercisable offline;
  treat as optional and out of scope for web dev health.
- No lint/typecheck config in the repo; no non-empty test suite. `manage.py check`/`test` are the gates.
- The committed `ai/` venv is macOS-only — never activate it on Linux.

## Gotchas for future workers
- Regenerate the venv on snapshot restore only if `.venv/` is missing; uv recreates it in seconds.
- If port 8000 is taken, the startup log + `ss` output is the source of truth for the actual port — don't assume.
