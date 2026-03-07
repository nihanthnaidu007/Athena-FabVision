## Athena – AI Voice & Web Assistant for Semiconductor Analysis

Athena is an end-to-end AI assistant that combines a Django web application with a real-time voice assistant powered by LiveKit and OpenAI.  
It is designed primarily for semiconductor and chip-design use cases, but can be extended to general technical Q&A.

Athena provides:

- A simple web UI for text-based queries
- A REST API endpoint for programmatic access
- A LiveKit-based voice assistant that uses OpenAI for STT, LLM reasoning, and TTS
- Optional web search via Google Custom Search for fresh, external information

---

## Features

- **Text-based agent (Web + API)**
  - Home page with a link to the agent UI
  - Agent UI at `/agent/` with a textarea and “Ask the Agent” button
  - REST endpoint at `/agent/ask/` that accepts JSON `{ "query": "..." }` and returns JSON `{ "response": "..." }`
  - Simple pluggable logic in `agent_logic.py` (e.g., telling jokes, basic canned responses)

- **Real-time voice assistant**
  - LiveKit worker defined in `main.py`
  - Uses:
    - Silero VAD for voice activity detection
    - OpenAI for:
      - Speech-to-Text (STT)
      - LLM reasoning
      - Text-to-Speech (TTS)
  - Guided by a rich domain-specific prompt in `instructions.txt` focused on semiconductor and chip design
  - Can:
    - Answer time/date questions
    - Tell jokes
    - Provide domain-specific insights
    - Perform web searches and summarize top results

- **Assistant tools**
  - Defined in `api.py` as methods on an `AssistantFnc` function context:
    - `tell_joke()`
    - `provide_insight(topic)`
    - `get_current_datetime()`
    - `search_web(query)` (Google Custom Search API)
  - Exposed to the LLM through LiveKit’s tools/function-calling interface

---

## Tech Stack

- **Backend**
  - Python 3
  - Django 5.x
  - Django REST framework

- **Real-time & AI**
  - LiveKit agents framework
  - LiveKit OpenAI plugins
  - Silero VAD
  - OpenAI (STT, LLM, TTS)

- **External APIs**
  - Google Custom Search API

- **Database & Storage**
  - SQLite (default, via `db.sqlite3`)
  - Django static files collected into `staticfiles/`

- **Configuration & Deployment**
  - `python-decouple` and `python-dotenv` for configuration via environment variables / `.env`
  - Gunicorn (via `Procfile`) for production serving of the Django app

---

## Project Structure

High-level structure:

- `manage.py`  
  Django management script (migrations, dev server, tests).

- `main.py`  
  Entry point for the LiveKit-based voice assistant worker.

- `api.py`  
  Assistant function context, exposing tools (jokes, insights, datetime, web search).

- `instructions.txt`  
  Domain-specific system prompt for the voice assistant (semiconductor and chip design focus).

- `requirements.txt`  
  Python dependencies for the entire project.

- `Procfile`  
  Process definition for production:
  - `web: gunicorn --bind 0.0.0.0:$PORT django_agent.wsgi:application`

- `db.sqlite3`  
  SQLite database used by Django (default/dev).

- `staticfiles/`  
  Collected static files (admin, CSS/JS, etc).

- `django_agent/` (Django project)
  - `settings.py` – Django configuration (DB, installed apps, static files, env-based keys)
  - `urls.py` – routes (`/admin/`, `/agent/`, `/`)
  - `wsgi.py`, `asgi.py` – WSGI/ASGI entry points

- `agent/` (Django app)
  - `views.py` – `AgentAPIView` and `agent_home`
  - `urls.py` – routes for `/agent/` and `/agent/ask/`
  - `agent_logic.py` – core logic for the text agent (e.g., jokes)
  - `templates/agent/index.html` – simple web UI for interacting with the agent
  - `models.py`, `tests.py` – placeholders for future models and tests

- `ai/`  
  A local Python virtual environment directory (committed in this repo but typically ignored in production setups).

---

## Prerequisites

- Python 3 (e.g., 3.10 or compatible)
- OpenAI account and API key
- LiveKit server (self-hosted or cloud), with API key and secret
- Google Custom Search Engine (CSE) and API key
- pip / virtualenv (recommended)

---

## Installation

1. **Clone the repository**

```bash
git clone <YOUR_REPO_URL>
cd <YOUR_REPO_NAME>
```

2. **Create and activate a virtual environment** (recommended)

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. **Install dependencies**

```bash
pip install -r requirements.txt
```

---

## Configuration

Create a `.env` file in the project root (or set these as environment variables) with at least:

```env
SECRET_KEY=your_django_secret_key
DEBUG=True

OPENAI_API_KEY=your_openai_api_key

LIVEKIT_URL=your_livekit_url
LIVEKIT_API_KEY=your_livekit_api_key
LIVEKIT_API_SECRET=your_livekit_api_secret

GOOGLE_API_KEY=your_google_api_key
GOOGLE_SEARCH_ENGINE_ID=your_search_engine_id
```

Notes:

- For **development**, `DEBUG=True` is fine.  
  For **production**, set `DEBUG=False` and configure `ALLOWED_HOSTS` in `django_agent/settings.py`.
- `SECRET_KEY` must be a strong, unique value in production.
- `GOOGLE_API_KEY` and `GOOGLE_SEARCH_ENGINE_ID` are used by `api.py` for web search.

---

## Running the Django Web App (Development)

1. **Apply migrations**

```bash
python manage.py migrate
```

2. **Run the development server**

```bash
python manage.py runserver
```

3. **Access the app**

- Root page:  
  `http://127.0.0.1:8000/`  
  (simple welcome page with a link to the agent UI)

- Agent UI:  
  `http://127.0.0.1:8000/agent/`  
  (textarea and button for sending queries)

- REST API endpoint:  
  `POST http://127.0.0.1:8000/agent/ask/`  
  with JSON body:

  ```json
  {
    "query": "Tell me a joke about chips"
  }
  ```

---

## Running the LiveKit Voice Assistant

In a separate terminal (with the same virtualenv and `.env`):

```bash
python main.py
```

This will:

- Load environment variables
- Connect to your LiveKit server (`LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`)
- Initialize:
  - Silero VAD
  - OpenAI STT/LLM/TTS via LiveKit’s OpenAI plugins
- Load system instructions from `instructions.txt`
- Run a `VoiceAssistant` loop that:
  - Listens for audio in the configured room
  - Transcribes user speech
  - Uses the LLM plus tools from `api.py` to decide how to respond
  - Sends synthesized speech back to the user

You typically run **both**:

- Django app: `python manage.py runserver`
- Voice assistant worker: `python main.py`

at the same time in development.

---

## Production Deployment

This project is set up for a typical PaaS-style deployment with Gunicorn:

- `Procfile`:

```procfile
web: gunicorn --bind 0.0.0.0:$PORT django_agent.wsgi:application
```

Production steps (high-level):

1. Set environment variables:
   - `SECRET_KEY` (secure)
   - `DEBUG=False`
   - `OPENAI_API_KEY`, `LIVEKIT_*`, `GOOGLE_*`, etc.
2. Adjust `ALLOWED_HOSTS` in `django_agent/settings.py` to your domain(s).
3. Apply migrations:

   ```bash
   python manage.py migrate
   ```

4. Collect static files:

   ```bash
   python manage.py collectstatic
   ```

5. Start Gunicorn via the `Procfile` (your platform may handle this automatically).

### Running the voice worker in production

The LiveKit worker (`main.py`) is not part of the Procfile by default.  
You should run it as a separate long-lived process, for example:

```bash
python main.py
```

Use your hosting platform or process manager (systemd, supervisor, etc.) to keep it running.

---

## Testing

- A `tests.py` file exists in the `agent` app but does not currently contain tests.
- You can add Django tests there and run:

```bash
python manage.py test
```

---

## Roadmap / Possible Extensions

- Replace the placeholder text logic in `agent_logic.py` with a real LLM-powered backend.
- Add authentication and user-specific context to queries.
- Extend `AssistantFnc` in `api.py` with more specialized tools (e.g., analytics, data retrieval, dashboards).
- Add proper CI tests and coverage.
- Improve the `/agent/` UI with more polished frontend components.


MIT License

Copyright (c) 2024 Nihanth Naidu K

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the “Software”), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
