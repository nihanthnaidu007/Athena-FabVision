/*
 * Athena FabVision chat client -- vanilla JS, no build step.
 *
 * Consumes POST /agent/stream/ as an SSE stream over fetch: frames of
 * ``event: <type>`` / ``data: <json>`` for status, delta, tool_call,
 * tool_result, sources, done, and error (the contract in agent/loop.py).
 * Markdown renders through the vendored marked, always sanitized by the
 * vendored DOMPurify -- unsanitized HTML is never inserted. History
 * messages hydrate from the json_script payloads the chat template
 * embeds; uploads go to the knowledge-base endpoint with XHR progress.
 *
 * window.AthenaChat exposes the pure helpers for browser-console smoke
 * checks; the page contract itself is pinned by agent/tests/test_chat_ui.py.
 */
(function () {
    'use strict';

    // ---------- tiny DOM helpers (no dependencies) ----------

    function $(selector, root) {
        return (root || document).querySelector(selector);
    }

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    // ---------- markdown (marked) + sanitization (DOMPurify) ----------

    function renderMarkdown(text, target) {
        if (window.DOMPurify && window.marked) {
            try {
                target.innerHTML = window.DOMPurify.sanitize(window.marked.parse(text));
                return;
            } catch (err) {
                // A renderer crash must not wedge the turn; fall through to text.
            }
        }
        // Honest fallback: plain text -- HTML is never inserted unsanitized.
        target.textContent = text;
    }

    // ---------- SSE frame parser (incremental, tolerant of chunk splits) ----------

    function createSseParser(onEvent, onError) {
        var buffer = '';
        return {
            push: function (chunkText) {
                buffer += chunkText;
                var frameEnd;
                while ((frameEnd = buffer.indexOf('\n\n')) !== -1) {
                    var frame = buffer.slice(0, frameEnd);
                    buffer = buffer.slice(frameEnd + 2);
                    var type = 'message';
                    var dataLines = [];
                    var lines = frame.split('\n');
                    for (var i = 0; i < lines.length; i++) {
                        var line = lines[i].replace(/\r$/, '');
                        if (line.indexOf('event: ') === 0) type = line.slice(7);
                        else if (line.indexOf('data: ') === 0) dataLines.push(line.slice(6));
                    }
                    if (!dataLines.length) continue;
                    try {
                        onEvent(type, JSON.parse(dataLines.join('\n')));
                    } catch (err) {
                        onError('Malformed stream frame received.');
                    }
                }
            }
        };
    }

    // ---------- tool blocks (the fabtools/registry contract) ----------

    function formatScore(value, suffix) {
        if (value === null || value === undefined) return 'n/e';
        var num = Number(value);
        if (isNaN(num)) return String(value);
        return (suffix === '%' ? (num * 100).toFixed(1) + '%' : num.toFixed(2));
    }

    function renderTableBlock(block) {
        var card = el('div', 'tool-card');
        card.appendChild(el('div', 'tool-card-title', block.title || 'Tool result'));
        if (block.summary) card.appendChild(el('div', 'tool-card-summary', block.summary));
        var table = el('table', 'tool-table');
        var head = el('thead');
        var headRow = el('tr');
        (block.columns || []).forEach(function (column) {
            headRow.appendChild(el('th', null, column));
        });
        head.appendChild(headRow);
        table.appendChild(head);
        var body = el('tbody');
        (block.rows || []).forEach(function (row) {
            var tr = el('tr');
            (row || []).forEach(function (cell) {
                tr.appendChild(el('td', null, cell === null || cell === undefined ? '' : cell));
            });
            body.appendChild(tr);
        });
        table.appendChild(body);
        card.appendChild(table);
        return card;
    }

    function renderWaferBlock(block) {
        var card = el('div', 'tool-card wafer-card');
        card.appendChild(el('div', 'tool-card-title', block.title || 'Wafer map analysis'));
        if (block.summary) card.appendChild(el('div', 'tool-card-summary', block.summary));

        var grid = el('div', 'wafer-grid');
        function stat(label, value) {
            var cell = el('div', 'wafer-stat');
            cell.appendChild(el('div', 'wafer-stat-value', value));
            cell.appendChild(el('div', 'wafer-stat-label', label));
            return cell;
        }
        grid.appendChild(stat('Total dies', block.total_dies));
        grid.appendChild(stat('Pass', block.pass_count));
        grid.appendChild(stat('Fail', block.fail_count));
        grid.appendChild(stat('Yield', formatScore(block.yield_pct, '%')));
        grid.appendChild(stat('Edge-ring score', formatScore(block.edge_ring_score)));
        grid.appendChild(stat('Center hotspot', formatScore(block.center_hotspot_score)));
        card.appendChild(grid);

        var patterns = block.patterns || [];
        if (patterns.length) {
            var badgeRow = el('div', 'wafer-patterns');
            patterns.forEach(function (pattern) {
                badgeRow.appendChild(el('span', 'pattern-badge', pattern));
            });
            card.appendChild(badgeRow);
        } else {
            card.appendChild(el('div', 'muted small', 'No spatial patterns above threshold.'));
        }

        var bins = block.bin_counts || {};
        var binCodes = Object.keys(bins);
        if (binCodes.length) {
            var binTable = el('table', 'tool-table bin-table');
            var headRow = el('tr');
            var valueRow = el('tr');
            binCodes.forEach(function (code) {
                headRow.appendChild(el('th', null, 'bin ' + code));
                valueRow.appendChild(el('td', null, bins[code]));
            });
            binTable.appendChild(headRow);
            binTable.appendChild(valueRow);
            card.appendChild(binTable);
        }

        var issues = block.issues || [];
        if (issues.length) {
            var issueList = el('ul', 'wafer-issues');
            issues.forEach(function (issue) {
                issueList.appendChild(el('li', null, issue));
            });
            card.appendChild(issueList);
        }
        return card;
    }

    function renderErrorBlock(block) {
        var alert = el('div', 'tool-alert');
        alert.appendChild(el('div', 'tool-alert-title', block.title || 'Tool error'));
        alert.appendChild(el('div', 'tool-alert-detail', block.detail || block.error || ''));
        return alert;
    }

    function renderTextBlock(block) {
        var card = el('div', 'tool-card');
        if (block.title) card.appendChild(el('div', 'tool-card-title', block.title));
        var body = el('div', 'tool-text');
        renderMarkdown(block.summary || block.text || '', body);
        card.appendChild(body);
        return card;
    }

    function renderToolBlock(block) {
        if (!block || typeof block !== 'object') {
            return renderErrorBlock({ title: 'Tool block', detail: 'Invalid tool result.' });
        }
        switch (block.type) {
            case 'table': return renderTableBlock(block);
            case 'wafer_map': return renderWaferBlock(block);
            case 'error': return renderErrorBlock(block);
            case 'text': return renderTextBlock(block);
            default:
                return renderErrorBlock({
                    title: 'Tool block',
                    detail: 'Unknown block type "' + block.type + '".'
                });
        }
    }

    // ---------- citation chips ----------

    function renderChips(container, sources) {
        container.innerHTML = '';
        var docs = (sources || []).filter(function (source) {
            return source && source.kind === 'doc';
        });
        if (!docs.length) {
            container.appendChild(el(
                'div', 'chips-empty muted small',
                'No document context was used for this answer.'
            ));
            return;
        }
        docs.forEach(function (source) {
            var chip = el('button', 'chip');
            chip.type = 'button';
            chip.appendChild(el('span', 'chip-title', source.title || 'Untitled document'));
            if (source.score !== null && source.score !== undefined) {
                chip.appendChild(el('span', 'chip-score', formatScore(source.score)));
            }
            var panel = el('div', 'chip-panel');
            panel.appendChild(el('div', 'chip-snippet', source.snippet || '(empty snippet)'));
            chip.addEventListener('click', function () {
                var open = container.querySelector('.chip-panel.open');
                if (open === panel) {
                    panel.classList.remove('open');
                    return;
                }
                if (open) open.classList.remove('open');
                panel.classList.add('open');
            });
            container.appendChild(chip);
            container.appendChild(panel);
        });
    }

    // ---------- history hydration (json_script payloads from the template) ----------

    function jsonFromScript(id) {
        var node = document.getElementById(id);
        if (!node) return null;
        try {
            return JSON.parse(node.textContent);
        } catch (err) {
            return null;
        }
    }

    function hydrateHistory(thread) {
        var messages = thread.querySelectorAll('.message');
        Array.prototype.forEach.call(messages, function (article) {
            var role = article.getAttribute('data-role');
            var contentEl = article.querySelector('.msg-content');
            var contentFor = article.querySelector('[data-content-for]');
            var sourcesFor = article.querySelector('[data-chips-for]');
            var blocksFor = article.querySelector('[data-blocks-for]');
            if (contentEl && contentFor) {
                var content = jsonFromScript(contentFor.getAttribute('data-content-for'));
                if (typeof content === 'string') {
                    if (role === 'assistant') renderMarkdown(content, contentEl);
                    else contentEl.textContent = content;
                }
            }
            if (sourcesFor) {
                renderChips(sourcesFor, jsonFromScript(sourcesFor.getAttribute('data-chips-for')));
            }
            if (blocksFor) {
                var blocks = jsonFromScript(blocksFor.getAttribute('data-blocks-for'));
                (Array.isArray(blocks) ? blocks : []).forEach(function (block) {
                    blocksFor.appendChild(renderToolBlock(block));
                });
            }
        });
    }

    // ---------- the chat controller ----------

    function init() {
        var root = $('#chat-root');
        if (!root || root.getAttribute('data-chat-initialized') === 'true') return;
        root.setAttribute('data-chat-initialized', 'true');

        var csrfMeta = $('meta[name="csrf-token"]');
        var csrf = csrfMeta ? csrfMeta.getAttribute('content') : '';
        var thread = $('#thread');
        var form = $('#composer-form');
        var input = $('#composer-input');
        var sendButton = $('#send-button');
        var stopButton = $('#stop-button');
        var uploadButton = $('#upload-button');
        var fileInput = $('#kb-file-input');
        var uploadStatus = $('#upload-status');
        var conversationList = $('#conversation-list');
        var tutorToggle = $('#tutor-toggle');
        var modeStatus = $('#mode-status');

        var state = {
            conversationId: root.getAttribute('data-conversation-id') || '',
            streamUrl: root.getAttribute('data-stream-url'),
            uploadUrl: root.getAttribute('data-upload-url'),
            chatHomeUrl: root.getAttribute('data-chat-home-url'),
            modeUrl: root.getAttribute('data-mode-url') || '',
            // The server renders the active conversation's mode; a fresh
            // page starts in assistant mode until the student toggles.
            mode: tutorToggle && tutorToggle.getAttribute('aria-pressed') === 'true'
                ? 'tutor'
                : 'assistant',
            controller: null,
            streaming: false
        };

        function nearBottom() {
            return thread.scrollHeight - thread.scrollTop - thread.clientHeight < 140;
        }

        function scrollBottom(force) {
            if (force || nearBottom()) thread.scrollTop = thread.scrollHeight;
        }

        function setStreaming(streaming) {
            state.streaming = streaming;
            sendButton.disabled = streaming;
            input.disabled = streaming;
            uploadButton.disabled = streaming;
            tutorToggle.disabled = streaming;
            stopButton.hidden = !streaming;
        }

        function removeEmptyState() {
            var empty = thread.querySelector('[data-thread-empty]');
            if (empty) empty.remove();
        }

        function appendUserMessage(text) {
            var article = el('article', 'message msg-user');
            article.setAttribute('data-role', 'user');
            var bubble = el('div', 'msg-bubble');
            var content = el('div', 'msg-content');
            content.textContent = text;
            bubble.appendChild(content);
            article.appendChild(bubble);
            thread.appendChild(article);
            removeEmptyState();
            scrollBottom(true);
        }

        function createTurn() {
            var article = el('article', 'message msg-assistant streaming');
            article.setAttribute('data-role', 'assistant');
            var bubble = el('div', 'msg-bubble');
            var content = el('div', 'msg-content');
            bubble.appendChild(content);
            var chips = el('div', 'chips');
            bubble.appendChild(chips);
            var blocks = el('div', 'tool-blocks');
            bubble.appendChild(blocks);
            article.appendChild(bubble);
            thread.appendChild(article);
            removeEmptyState();
            return {
                article: article,
                content: content,
                chips: chips,
                blocks: blocks,
                text: '',
                indicator: null,
                pendingFrame: false,
                complete: false
            };
        }

        function showIndicator(turn, stageText) {
            if (!turn.indicator) {
                turn.indicator = el('div', 'typing-indicator');
                var dots = el('span', 'typing-dots');
                for (var i = 0; i < 3; i++) dots.appendChild(el('span', 'typing-dot'));
                turn.indicator.appendChild(dots);
                turn.indicator.appendChild(el('span', 'typing-stage', stageText));
                turn.article.querySelector('.msg-bubble').appendChild(turn.indicator);
                scrollBottom();
            } else {
                turn.indicator.querySelector('.typing-stage').textContent = stageText;
                turn.indicator.classList.remove('hidden');
            }
        }

        function hideIndicator(turn) {
            if (turn.indicator) turn.indicator.classList.add('hidden');
        }

        function renderTurnText(turn) {
            if (turn.pendingFrame) return;
            turn.pendingFrame = true;
            requestAnimationFrame(function () {
                turn.pendingFrame = false;
                renderMarkdown(turn.text, turn.content);
                scrollBottom();
            });
        }

        function toolRunningCard(toolName, callId) {
            var card = el('div', 'tool-card tool-running');
            if (callId) card.setAttribute('data-call-id', callId);
            card.appendChild(el('div', 'tool-card-title', 'Running ' + (toolName || 'tool') + '…'));
            var spinner = el('span', 'spinner');
            card.appendChild(spinner);
            return card;
        }

        function replaceToolCard(turn, name, block) {
            var rendered = renderToolBlock(block);
            var running = turn.blocks.querySelectorAll('.tool-running');
            for (var i = 0; i < running.length; i++) {
                // tool_result carries no call id; calls run in order, so the
                // first still-running card matching the tool name is ours.
                if (!name || running[i].textContent.indexOf(name) !== -1) {
                    turn.blocks.replaceChild(rendered, running[i]);
                    return;
                }
            }
            turn.blocks.appendChild(rendered);
        }

        function addTurnFooter(turn, latencyMs) {
            if (turn.querySelector('.turn-footer')) return;
            var footer = el('div', 'turn-footer muted small');
            footer.textContent = latencyMs ? 'Completed in ' + latencyMs + ' ms' : 'Completed';
            turn.article.appendChild(footer);
        }

        function surfaceTurnError(turn, message) {
            var alert = el('div', 'turn-alert');
            alert.appendChild(el('div', 'turn-alert-title', 'Something went wrong'));
            alert.appendChild(el('div', 'turn-alert-detail', message || 'The assistant turn failed.'));
            turn.article.appendChild(alert);
            scrollBottom();
        }

        function sidebarConversationExists(id) {
            return !!conversationList.querySelector(
                '[data-conversation-item="' + id + '"]'
            );
        }

        function modeBadge() {
            var badge = el('span', 'mode-badge');
            badge.setAttribute('data-mode-badge', '');
            badge.setAttribute('title', 'Tutor mode');
            badge.textContent = 'tutor';
            return badge;
        }

        function addSidebarConversation(id, title, mode) {
            if (sidebarConversationExists(id)) return;
            var item = el('div', 'conversation-item');
            item.setAttribute('data-conversation-item', id);
            var link = el('a', 'conversation-link', title || 'New conversation');
            link.href = state.chatHomeUrl + '?c=' + id;
            item.appendChild(link);
            if (mode === 'tutor') item.appendChild(modeBadge());
            conversationList.prepend(item);
        }

        function updateSidebarBadge(id, mode) {
            var item = conversationList.querySelector('[data-conversation-item="' + id + '"]');
            if (!item) return;
            var existing = item.querySelector('[data-mode-badge]');
            if (mode === 'tutor' && !existing) {
                var deleteForm = item.querySelector('form');
                if (deleteForm) item.insertBefore(modeBadge(), deleteForm);
                else item.appendChild(modeBadge());
            } else if (mode !== 'tutor' && existing) {
                existing.remove();
            }
        }

        function setConversation(id, title) {
            state.conversationId = String(id);
            root.setAttribute('data-conversation-id', state.conversationId);
            addSidebarConversation(id, title, state.mode);
            var nextUrl = state.chatHomeUrl + '?c=' + id;
            if (window.history && window.history.replaceState) {
                window.history.replaceState(null, '', nextUrl);
            }
        }

        function handleEvent(turn, type, data) {
            if (!data || typeof data !== 'object') return;
            switch (type) {
                case 'status':
                    if (data.conversation_id && !state.conversationId) {
                        setConversation(data.conversation_id, turn.titleHint);
                    }
                    if (data.stage === 'retrieving') showIndicator(turn, 'Searching the knowledge base…');
                    else if (data.stage === 'generating') showIndicator(turn, 'Athena is thinking…');
                    break;
                case 'delta':
                    turn.text += data.text || '';
                    hideIndicator(turn);
                    renderTurnText(turn);
                    break;
                case 'tool_call':
                    turn.blocks.appendChild(toolRunningCard(data.name, data.call_id));
                    showIndicator(turn, 'Running ' + (data.name || 'tool') + '…');
                    scrollBottom();
                    break;
                case 'tool_result':
                    replaceToolCard(turn, data.name, data.block);
                    scrollBottom();
                    break;
                case 'sources':
                    renderChips(turn.chips, data.sources || []);
                    scrollBottom();
                    break;
                case 'done':
                    turn.complete = true;
                    turn.article.classList.remove('streaming');
                    hideIndicator(turn);
                    addTurnFooter(turn, data.latency_ms);
                    break;
                case 'error':
                    hideIndicator(turn);
                    surfaceTurnError(turn, data.error);
                    break;
                default:
                    // Forward compatibility: unknown event types are ignored.
                    break;
            }
        }

        function runTurn(text) {
            var turn = createTurn();
            turn.titleHint = text.length > 60 ? text.slice(0, 60) + '…' : text;
            appendUserMessage(text);
            setStreaming(true);
            showIndicator(turn, 'Athena is thinking…');

            var controller = new AbortController();
            state.controller = controller;

            fetch(state.streamUrl, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrf,
                    'Accept': 'text/event-stream'
                },
                body: JSON.stringify({
                    message: text,
                    conversation_id: state.conversationId ? Number(state.conversationId) : undefined,
                    // Creation-time only: an existing conversation's persisted
                    // mode rules, changed through the mode endpoint.
                    mode: state.conversationId ? undefined : state.mode
                }),
                signal: controller.signal
            }).then(function (response) {
                if (!response.ok || !response.body) {
                    return response.json().catch(function () { return {}; })
                        .then(function (payload) {
                            throw new Error(
                                (payload && payload.error) ||
                                'The assistant turn failed (HTTP ' + response.status + ').'
                            );
                        });
                }
                var reader = response.body.getReader();
                var decoder = new TextDecoder();
                var parser = createSseParser(
                    function (type, data) { handleEvent(turn, type, data); },
                    function (message) { surfaceTurnError(turn, message); }
                );
                function pump() {
                    return reader.read().then(function (result) {
                        if (result.done) return null;
                        parser.push(decoder.decode(result.value, { stream: true }));
                        return pump();
                    });
                }
                return pump();
            }).catch(function (err) {
                if (err && err.name === 'AbortError') {
                    // Stop button: keep the partial answer; the server
                    // persists the partial text for this turn too.
                    var stopped = el('div', 'turn-footer muted small', 'Stopped — partial answer kept.');
                    turn.article.appendChild(stopped);
                } else {
                    surfaceTurnError(turn, err && err.message ? err.message : String(err));
                }
            }).finally(function () {
                turn.article.classList.remove('streaming');
                hideIndicator(turn);
                setStreaming(false);
                state.controller = null;
                input.focus();
            });
        }

        // ----- uploads (XHR: fetch has no upload progress events) -----

        function uploadFile(file) {
            uploadStatus.hidden = false;
            uploadStatus.className = 'upload-status';
            uploadStatus.innerHTML = '';

            var line = el('div', 'upload-line');
            line.appendChild(el('div', 'upload-name', file.name));
            var bar = el('div', 'upload-progress');
            var fill = el('div', 'upload-progress-fill');
            fill.style.width = '0%';
            bar.appendChild(fill);
            line.appendChild(bar);
            var stateText = el('div', 'upload-state muted small', 'Uploading… 0%');
            line.appendChild(stateText);
            uploadStatus.appendChild(line);

            var xhr = new XMLHttpRequest();
            xhr.open('POST', state.uploadUrl);
            xhr.setRequestHeader('X-CSRFToken', csrf);
            xhr.upload.addEventListener('progress', function (event) {
                if (event.lengthComputable) {
                    var pct = Math.round((event.loaded / event.total) * 100);
                    fill.style.width = pct + '%';
                    stateText.textContent = 'Uploading… ' + pct + '%';
                }
            });
            xhr.addEventListener('load', function () {
                var payload = null;
                try { payload = JSON.parse(xhr.responseText); } catch (err) { payload = null; }
                fill.style.width = '100%';
                if (xhr.status === 201) {
                    uploadStatus.classList.add('upload-ok');
                    stateText.textContent = '✓ ' + file.name + ' ready — ' +
                        (payload && payload.chunks !== undefined
                            ? payload.chunks + ' chunks embedded.'
                            : 'embedded.');
                } else if (xhr.status === 202) {
                    // Degraded mode: stored, embeddings unavailable. Quote the
                    // server's honest detail instead of pretending success.
                    uploadStatus.classList.add('upload-pending');
                    stateText.textContent = '⏳ ' + file.name + ' stored — ' +
                        ((payload && payload.detail) || 'will be processed once embeddings are configured.');
                } else {
                    uploadStatus.classList.add('upload-error');
                    stateText.textContent = '✗ Upload failed (HTTP ' + xhr.status + '): ' +
                        ((payload && payload.error) || 'unknown error.');
                }
            });
            xhr.addEventListener('error', function () {
                uploadStatus.classList.add('upload-error');
                stateText.textContent = '✗ Upload failed: network error.';
            });

            var formData = new FormData();
            formData.append('file', file);
            xhr.send(formData);
        }

        // ----- tutor mode (per-conversation preset) -----

        function renderModeToggle() {
            var tutor = state.mode === 'tutor';
            tutorToggle.setAttribute('aria-pressed', tutor ? 'true' : 'false');
            tutorToggle.classList.toggle('on', tutor);
        }

        function setModeStatus(message) {
            if (message) {
                modeStatus.textContent = message;
                modeStatus.hidden = false;
            } else {
                modeStatus.hidden = true;
                modeStatus.textContent = '';
            }
        }

        function applyMode(next, persist) {
            var previous = state.mode;
            state.mode = next;
            renderModeToggle();
            if (!persist) return;
            if (state.conversationId) {
                var body = new FormData();
                body.append('conversation_id', state.conversationId);
                body.append('mode', next);
                fetch(state.modeUrl, {
                    method: 'POST',
                    headers: { 'X-CSRFToken': csrf },
                    body: body
                }).then(function (response) {
                    if (!response.ok) throw new Error('HTTP ' + response.status);
                    return response.json();
                }).then(function (payload) {
                    updateSidebarBadge(String(payload.conversation_id), payload.mode);
                    setModeStatus('');
                }).catch(function () {
                    // Honest failure: revert the toggle and say what happened.
                    state.mode = previous;
                    renderModeToggle();
                    setModeStatus('Could not save tutor mode — try again.');
                });
            }
            // No conversation yet: the mode rides the next stream request
            // and is persisted when the conversation is created.
        }

        // ----- wiring -----

        form.addEventListener('submit', function (event) {
            event.preventDefault();
            var text = input.value.trim();
            if (!text || state.streaming) return;
            input.value = '';
            input.style.height = '';
            runTurn(text);
        });

        input.addEventListener('keydown', function (event) {
            if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                form.dispatchEvent(new Event('submit', { cancelable: true }));
            }
        });

        input.addEventListener('input', function () {
            input.style.height = 'auto';
            input.style.height = Math.min(input.scrollHeight, 160) + 'px';
        });

        stopButton.addEventListener('click', function () {
            if (state.controller) state.controller.abort();
        });

        tutorToggle.addEventListener('click', function () {
            if (state.streaming) return;
            applyMode(state.mode === 'tutor' ? 'assistant' : 'tutor', true);
        });

        uploadButton.addEventListener('click', function () { fileInput.click(); });
        fileInput.addEventListener('change', function () {
            var file = fileInput.files && fileInput.files[0];
            if (file) uploadFile(file);
            fileInput.value = '';
        });

        document.addEventListener('submit', function (event) {
            var formEl = event.target;
            if (formEl && formEl.matches && formEl.matches('[data-confirm]')) {
                if (!window.confirm(formEl.getAttribute('data-confirm'))) {
                    event.preventDefault();
                }
            }
        });

        hydrateHistory(thread);
        scrollBottom(true);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // Smoke-check surface (browser console / manual QA).
    window.AthenaChat = {
        createSseParser: createSseParser,
        renderMarkdown: renderMarkdown,
        renderToolBlock: renderToolBlock,
        renderChips: renderChips
    };
})();
