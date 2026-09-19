# Athena FabVision User Guide

Athena FabVision is a fab-process assistant for students and process
engineers: ask questions in plain language, get streaming answers
grounded in your own documents, and run deterministic analysis on wafer
and process data — with citations you can click.

This guide covers the v1.1 features. Every behavior described here was
verified against a running instance of the release; where the product
shows an honest failure instead of a happy path, the failure is
documented too.

## Where everything lives

| Surface | URL | What it is |
|---|---|---|
| Chat | `/` | Streaming conversation with tools and citations |
| Usage dashboard | `/dashboard/` | Metering, answer-feedback ratios, per-tool breakdowns |
| Documents | `/dashboard/documents/` | Your knowledge base: upload, status, delete, re-ingest |
| Notebooks | `/dashboard/notebooks/` | Course notebooks that scope conversations |
| API keys | `/dashboard/keys/` | REST access keys |
| Health | `/healthz/`, `/healthz/ready/` | Service liveness and readiness |

---

## 1. Creating your account and your first-run knowledge base

### What it does

Self-service signup creates a fully isolated account — no admin, no
invite. Every new account is seeded with two starter documents so the
product demonstrates itself on first visit:

- **welcome-to-athena-fabvision.md** — a short how-to note that flows
  through the exact same ingestion pipeline as a manual upload.
- **wafer_map_example.csv** — the bundled example wafer, stored as a
  storage-only document the wafer analyzer can read.

Seeding is best-effort and idempotent: it never blocks account
creation, and re-running it (or uploading the identical file) never
duplicates documents — the guard is the file's content hash. Seeded
documents are ordinary documents, private to your account.

### How to use it

1. Open the app root. If signups are enabled you will see a
   **"Create an account"** link on the login page.
2. Fill **Username**, **Password**, and **Confirm password**, then
   submit. You are logged in immediately and land on the chat page.
3. Open **Documents** in the dashboard navigation: your two starter
   documents are already there.

Deployments control this with the `SIGNUPS_ENABLED` environment
variable (default `true`). When it is `false`, the "Create an account"
link disappears from the login page and the signup route itself is
removed — it returns 404 rather than a hidden form.

### Failure modes you may see

- **The welcome note shows status "failed".** The note is ingested
  like any upload, which needs an embedding provider. If embeddings
  are unavailable (misconfigured provider, no network), the document
  row shows **failed** and the **Failure reason** column shows the
  real provider error — nothing is hidden or silently dropped. Use
  **Re-ingest** to retry once the provider works. The wafer CSV is
  storage-only and is unaffected by embedding availability.
- **You see no signup link.** The deployment set `SIGNUPS_ENABLED=false`.
  Ask your administrator for an account.

---

## 2. Chat: asking questions and reading answers

### What it does

The chat page streams answers token by token. Answers can cite your
documents, run deterministic tools, or both. When a tool runs you see
a **tool card** in the transcript; when citations back an answer, the
sources are listed; when an answer used no document context at all,
Athena says so explicitly ("No document context was used for this
answer") instead of implying grounding that isn't there.

### How to use it

1. Type in the composer ("Ask Athena about your fab data…") and press
   Enter or **Send**.
2. Watch the status line while the turn streams; tool cards appear
   inline as tools run.
3. Start a fresh thread with **+ New chat**. Conversations are listed
   in the sidebar; each row has a **✎ rename** button and a **×**
   delete button, and long histories are capped (see §10).

---

## 3. Documents: building your knowledge base

### What it does

The Documents page is your personal knowledge base. Upload `.txt`,
`.md`, `.pdf`, or `.csv` files. Text documents are chunked and
embedded so answers can cite them. Wafer CSVs are stored as-is
(storage-only) — the analyzer reads them from disk, so there is no
text to embed and they never block on an embedding provider.

### How to use it

1. From the chat page, click the **📎** button in the composer and pick
   a file; the status line above the composer reports the upload
   result. You can also upload from the Documents page or
   programmatically via `POST /kb/documents/`.
2. On the Documents page, each row shows **File, Type, Notebook,
   Status, Chunks, Uploaded, Failure reason**, and actions.
3. Status meanings:
   - **pending** — stored but not yet ingested (e.g. no embedding
     provider configured).
   - **ready** — ingestion succeeded. Text documents show their chunk
     count; wafer CSVs are storage-only and legitimately show 0 chunks.
   - **failed** — ingestion errored; the **Failure reason** column
     names exactly what happened.
4. **Re-ingest** (offered for pending and failed rows) retries
   ingestion. **Delete** removes the document from future answers;
   answers you already received keep the citations they showed.

### Failure modes you may see

- **"Embedding failed: …" with a provider error.** The embedding
  provider rejected or could not serve the request. The row goes to
  **failed** with the provider's real error message; fix the provider
  and click **Re-ingest**. Your file is not lost.
- **Nothing to cite.** Until a text document reaches **ready**, its
  content cannot back an answer. Answers will say no document context
  was used rather than pretending.

---

## 4. Wafer analysis: uploading a wafer CSV and handing it to the analyzer

### What it does

A wafer-bin CSV (columns `wafer_id, x, y, bin`) is analyzed by a
deterministic tool — the numbers come from code, not from a language
model. The result reports total/passing/failing dies, yield, bin
distribution, and spatial pattern scores (edge ring, center hotspots),
with a recommendation.

### How to use it

- **One-click example:** click the **🧪** button in the composer. If
  the example wafer is not in your knowledge base yet it is loaded
  instantly; if it already is, you will see
  *"✓ Example wafer CSV is already in your knowledge base."* plus an
  **Analyze with the wafer tool** button that starts the analysis for
  you.
- **Your own wafer:** click **📎** and upload your CSV, then ask
  Athena to analyze it — e.g. "Analyze the wafer CSV I just uploaded".
  Wafer CSVs upload fine even without an embedding provider (they are
  storage-only).
- Ask naturally: "What's the yield?", "Is there an edge ring?", "Which
  dies failed?"

Verified example: the bundled example returns *81 dies, 69 passing
(Bin 1), 12 failing (Bin 2), yield 85.2%, edge ring score 0.85*, and
the answer explains likely edge causes (edge bead removal, edge
exclusion zone, clamping stress) with recommendations.

### Failure modes you may see

- **"Wafer map analysis failed — no wafer CSV named '…' in this
  user's knowledge base."** The analyzer resolves files by the exact
  stored path (like `documents/2026/09/your-file.csv`), not by a bare
  filename. Upload the CSV (paper-clip button or `POST
  /kb/documents/`) and analyze the path from the upload response, or
  ask Athena to search your knowledge base for the file. When the
  model guesses a path that does not exist, you will see this error
  card — it is the tool refusing to guess, not a lost file.
- **"Wafer map analysis failed" with schema detail.** A structurally
  broken CSV uploads fine (storage-only) but fails at analysis time
  with a row-and-column-precise error, e.g. *"row 2: column 'x' must
  be an integer, got 'not-an-int'"*, and Athena relays the expected
  schema and how to fix it. Malformed input never raises a raw stack
  trace into the chat.

---

## 5. The die-grid visualization

### What it does

Successful wafer analysis includes a per-die heat map: an inline SVG
grid of the wafer where each die is colored by its bin, so spatial
patterns (edge rings, hotspots) are visible at a glance.

### How to use it

Just run a wafer analysis (previous section). The die grid renders in
the answer next to the tool card. Each die carries an accessible label
(e.g. "bin 2 @ (0, -5)") for screen readers.

Large wafers are handled honestly: grids above 2,500 dies are
downsampled by an integer stride that keeps the map's shape (a 200×200
map lands around 2,500 points), and the result reports exactly how
many dies were omitted (`dies_omitted`) rather than silently hiding
them.

---

## 6. The SPC rules checker

### What it does

Paste a measurement series and Athena checks it against the Nelson
rules for detecting out-of-control processes — deterministic logic,
not model judgment. You get a control-chart verdict: center line,
sigma (estimated from the moving range unless you supply a known
sigma), UCL/LCL, the specific rule violations, and per-point flags,
with references for the rules used.

### How to use it

Ask Athena to check a series, giving the numbers explicitly — e.g.
"Use the SPC rules check tool on this series: [100.2, 99.8, 100.1, …,
104.5]" or "Check this series against SPC rules: 100.2, 99.8, …". If
you have a known process sigma, include it ("sigma = 0.5").

Verified example: the series above returns verdict **out_of_control**
with mean 100.3, sigma 0.479 (estimated from moving range),
UCL 101.74 / LCL 98.86, and two violations — Nelson rule 1 (1 point
beyond 3σ: the 104.5 excursion) and Nelson rule 2 (9 points in a row
on one side of the center line). The card advises investigating the
flagged points before trusting the process.

### Failure modes you may see

- **"SPC rules check failed — no measurement series provided."** The
  tool runs only when the series actually reaches it. If the request
  is phrased loosely, the tool can be invoked without its input; you
  will see the error card (it names exactly what it needs: CSV or JSON
  numbers, 2–1000 points, optional sigma), and Athena will typically
  fall back to explaining the series itself. That fallback is the
  model's arithmetic, not the deterministic checker — if you need the
  auditable verdict card, re-ask naming the tool or formatting the
  series as a bracketed list.

---

## 7. Tutor mode: learning, not just answering

### What it does

Tutor mode changes the conversation's teaching style: instead of
handing over complete answers, the tutor asks guiding questions, gives
starter hints, and poses thinking exercises — Socratic, not
encyclopedic. Mode is per conversation: an open chat keeps its own
mode.

### How to use it

- Click the **Tutor mode** button above the composer before starting a
  new conversation (or on a new thread). Send your question as usual.
- Verified example: asking *"What causes the edge-ring failure pattern
  on wafers?"* in tutor mode returns two diagnostic questions ("Is a
  wafer perfectly flat? How uniform is it from center to edge?"), a
  starter hint ("non-uniformities amplified at the periphery"), and a
  thinking exercise (spin-coating analogy) — rather than the full
  explanation the same question earns in assistant mode.
- Tutor mode works best when you engage: answer its questions and it
  builds on your reasoning.

### Failure modes you may see

- **You wanted the answer, not a lesson.** Toggle Tutor mode off and
  re-ask, or start a new conversation in assistant mode — an existing
  conversation's persisted mode always rules.
- **You switched mode mid-conversation.** Mode changes apply from the
  next turn; earlier answers keep the style they were written in.

---

## 8. Per-message feedback and the answer-quality dashboard

### What it does

Every assistant message can be rated helpful or not helpful. Your
verdicts — and everyone's in aggregate, on your account — surface on
the Usage dashboard as answer-quality ratios, per conversation and per
tool, so you can see where answers are and are not landing.

### How to use it

1. In a conversation, rate an assistant message with the thumbs-up /
   thumbs-down control.
2. Change your mind? Rate again — the verdict updates. Remove it
   entirely and the dashboard reflects that too (a cleared verdict
   stops counting).
3. Open **Usage**: the **Answer feedback** panel shows the totals —
   verified example: *"1 helpful · 1 not helpful — 50% positive across
   2 rated answers"* — plus per-conversation and per-tool positive
   percentages.

Feedback is scoped to your account: ratings apply to your messages
only, and another user's messages are unreachable by id.

### Failure modes you may see

- **Rating does nothing.** Feedback only attaches to assistant
  messages — rating your own or a system message returns an error
  rather than a silent no-op.
- **The ratio looks brutal.** It reflects current verdicts, including
  on turns where a tool failed. That is the point: failed-tool turns
  show up in the per-tool breakdown.

---

## 9. Course notebooks: grounding answers to one course

### What it does

A notebook is a named bucket of documents — "Lithography", "Yield
Learning" — that a conversation can be scoped to. A scoped
conversation grounds only in that notebook's documents: nothing else
in your knowledge base can leak in.

### How to use it

1. Open **Notebooks** and create one (name it, e.g. "Lithography").
2. On the **Documents** page, assign a document to the notebook with
   the per-row notebook selector and **Save**. Empty value unassigns.
3. Start a scoped conversation: on the chat page, pick the notebook in
   the **scope** dropdown ("Answers then ground only in its
   documents") before sending your first message. Scope is fixed at
   creation — an existing conversation keeps its own scope, and a new
   message to a scoped conversation stays scoped.
4. Ask as usual. If the notebook's documents can't ground an answer
   (for example the only assigned document is a wafer CSV, which has
   no text chunks), Athena says it has no access to course material in
   this conversation — verified behavior — instead of quietly pulling
   from your whole knowledge base.
5. Deleting a notebook keeps its documents and conversations (they
   become unscoped); the page asks for confirmation first.

### Failure modes you may see

- **"I don't have access to …" in a scoped conversation.** Scope is
  working as designed: the notebook's documents cannot back the
  answer. Assign the relevant text document to the notebook (and make
  sure it reached **ready** status), or open the question in an
  unscoped conversation.
- **Answer arrived ungrounded in a scoped chat.** A wafer CSV assigned
  to the notebook makes wafer *analysis* available, but contributes no
  citable text. Text documents are what ground cited answers.

---

## 10. Conversation management: rename, export, copy, regenerate

### What it does

The sidebar and transcript give you full control over your
conversations:

- **Rename** any conversation inline (✎ button in the sidebar) so the
  list stays navigable.
- **Copy** any assistant answer to your clipboard with the 📋 button
  on the message.
- **Regenerate** the last answer: the user's question stays, the stale
  answer (and its feedback rating) is replaced by a fresh streamed
  response.
- **Export** the whole conversation as Markdown — including the
  sources each answer cited and the tool results that ran — as a
  download.
- Long histories are handled honestly: the sidebar shows your 50 most
  recent conversations and, if there are more, says so ("Showing the
  50 most recent of N conversations — older ones are hidden") instead
  of silently hiding them.

### How to use it

- **Rename:** click ✎ on a conversation row, edit the title, confirm.
  The new title is saved immediately (verified: the rename API
  persists it and exports use the new name).
- **Copy:** click 📋 on an assistant message. On restricted contexts
  (non-HTTPS deployments) the copy falls back to a hidden-textarea
  mechanism automatically.
- **Regenerate:** use the regenerate control on the last exchange —
  the fresh answer streams in place; your question is not duplicated.
- **Export:** open the conversation and use the export action; you get
  a Markdown file titled with the conversation name, with every turn
  plus `Sources` and `Tool result` sections.

### Failure modes you may see

- **"A non-empty title is required."** Renaming to a blank title is
  rejected — a rename is a deliberate edit.
- **Nothing to regenerate.** Regenerating a conversation whose last
  turn has no completed answer returns an error rather than a silent
  no-op. Send the message first, or ask again.
- **Regenerate replaced a rating.** A regenerated turn discards the
  old answer *and its feedback rating* — rate the fresh answer on its
  own merits.
- **Someone else's conversation is not yours to touch.** Rename,
  export, copy, and regenerate all operate only on your own
  conversations; another user's conversation id is unreachable (404).

---

## 11. Coming soon

These v1.1 features are not merged yet; this guide will grow real
sections when they land.

### TODO: Practice & spaced review (SM-2)

Validated practice generation and SM-2 scheduling are specified but
not merged. **TODO: document creating practice sets from a notebook
and reviewing due cards at promotion.**

### TODO: Root-cause analysis / 8D

Guided 8D/RCA artifacts are specified but not merged. **TODO:
document starting an RCA from an excursion triage and sharing the 8D
artifact at promotion.**
