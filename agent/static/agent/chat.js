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

    // ---------- clipboard (copy answer) ----------

    function fallbackCopy(text) {
        // execCommand is deprecated but is the only in-page path when
        // navigator.clipboard is unavailable (insecure context).
        try {
            var area = document.createElement('textarea');
            area.value = text;
            area.setAttribute('readonly', '');
            area.style.position = 'fixed';
            area.style.opacity = '0';
            document.body.appendChild(area);
            area.select();
            var ok = document.execCommand('copy');
            document.body.removeChild(area);
            return ok;
        } catch (err) {
            return false;
        }
    }

    function copyToClipboard(text) {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            return navigator.clipboard.writeText(text).then(
                function () { return true; },
                function () { return fallbackCopy(text); }
            );
        }
        return Promise.resolve(fallbackCopy(text));
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
                    var data;
                    try {
                        data = JSON.parse(dataLines.join('\n'));
                    } catch (err) {
                        onError('Malformed stream frame received.');
                        continue;
                    }
                    // A handler exception is a client rendering bug, not a
                    // wire problem -- label it honestly so the real cause
                    // surfaces instead of blaming the stream framing.
                    try {
                        onEvent(type, data);
                    } catch (err) {
                        onError('Turn event handling failed: ' + (err && err.message ? err.message : err));
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

    // fixed-decimal formatter shared by the SPC card and its run chart
    function fmt(value, digits) {
        var num = Number(value);
        return isNaN(num) ? 'n/a' : num.toFixed(digits === undefined ? 2 : digits);
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

    // ---------- wafer die-grid SVG (spec v1.1 feature 1) ----------

    var SVG_NS = 'http://www.w3.org/2000/svg';
    // Mirrors fabtools.wafer_map.PASS_BIN: bin 1 passes, every other valid
    // bin is a fail. Fail fills shade by bin code (0 = untested gray).
    var PASS_BIN = 1;
    var PASS_FILL = '#2f9e44';
    var FAIL_FILLS = { 0: '#9aa5b1', 2: '#d9480f', 3: '#e8590c', 4: '#f76707', 5: '#e03131' };
    var FAIL_FILL_DEFAULT = '#c92a2a';

    function svgEl(tag, attrs) {
        var node = document.createElementNS(SVG_NS, tag);
        if (attrs) {
            Object.keys(attrs).forEach(function (key) {
                node.setAttribute(key, attrs[key]);
            });
        }
        return node;
    }

    function dieFill(bin) {
        var code = Number(bin);
        if (code === PASS_BIN) return PASS_FILL;
        return FAIL_FILLS[code] || FAIL_FILL_DEFAULT;
    }

    function renderWaferDieMap(block) {
        // Returns the figure element, or null when the block predates the
        // die payload (old blocks keep rendering stats-only, unchanged).
        var dies = Array.isArray(block.dies) ? block.dies : [];
        if (!dies.length) return null;

        var minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
        dies.forEach(function (die) {
            if (die.x < minX) minX = die.x;
            if (die.x > maxX) maxX = die.x;
            if (die.y < minY) minY = die.y;
            if (die.y > maxY) maxY = die.y;
        });
        var cols = maxX - minX + 1;
        var rows = maxY - minY + 1;
        var CELL = 12;
        var width = cols * CELL;
        var height = rows * CELL;
        // Wafer orientation: +y is up, so svg rows flip.
        var cellX = function (x) { return (x - minX) * CELL; };
        var cellY = function (y) { return (maxY - y) * CELL; };

        var passCount = dies.filter(function (die) {
            return Number(die.bin) === PASS_BIN;
        }).length;
        var svg = svgEl('svg', {
            viewBox: '0 0 ' + width + ' ' + height,
            width: width,
            height: height,
            role: 'img',
            'aria-label': 'Wafer die grid: ' + dies.length + ' dies, ' +
                passCount + ' pass, ' + (dies.length - passCount) + ' fail',
            class: 'wafer-map-svg'
        });

        dies.forEach(function (die) {
            var rect = svgEl('rect', {
                x: cellX(die.x),
                y: cellY(die.y),
                width: CELL - 1,
                height: CELL - 1,
                fill: dieFill(die.bin),
                'shape-rendering': 'crispEdges',
                'data-die-cell': '1',
                'data-die-bin': die.bin
            });
            // Native tooltip: bin and coordinates of the hovered die.
            var tip = svgEl('title');
            tip.textContent = 'bin ' + die.bin + ' @ (' + die.x + ', ' + die.y + ')';
            rect.appendChild(tip);
            svg.appendChild(rect);
        });

        // Pattern overlays follow the backend's thresholded patterns list --
        // the JS never re-derives thresholds (single source of truth).
        // Radii come from the analyzer's own geometry: rMax is the largest
        // die distance from the lattice center, the edge band starts at 90%
        // of it, the hotspot region ends at 25% (fabtools.wafer_map).
        var patterns = Array.isArray(block.patterns) ? block.patterns : [];
        if (patterns.length) {
            var centerX = (minX + maxX) / 2;
            var centerY = (minY + maxY) / 2;
            var rMaxUnits = 0;
            dies.forEach(function (die) {
                var d = Math.hypot(die.x - centerX, die.y - centerY);
                if (d > rMaxUnits) rMaxUnits = d;
            });
            var overlayCircle = function (radiusUnits, pattern) {
                return svgEl('circle', {
                    cx: (centerX - minX) * CELL + CELL / 2,
                    cy: (maxY - centerY) * CELL + CELL / 2,
                    r: radiusUnits * CELL,
                    fill: 'none',
                    stroke: '#e03131',
                    'stroke-dasharray': '4 3',
                    'stroke-width': 1.5,
                    'data-pattern-overlay': pattern
                });
            };
            if (patterns.indexOf('edge_ring') !== -1) {
                svg.appendChild(overlayCircle(rMaxUnits, 'edge_ring_outer'));
                svg.appendChild(overlayCircle(rMaxUnits * 0.9, 'edge_ring_inner'));
            }
            if (patterns.indexOf('center_hotspot') !== -1) {
                svg.appendChild(overlayCircle(rMaxUnits * 0.25, 'center_hotspot'));
            }
        }

        var figure = el('figure', 'wafer-map-figure');
        figure.appendChild(svg);
        var legend = el('div', 'wafer-legend');
        function legendItem(color, label) {
            var item = el('span', 'wafer-legend-item');
            var swatch = el('span', 'wafer-swatch');
            swatch.style.background = color;
            item.appendChild(swatch);
            item.appendChild(el('span', null, label));
            return item;
        }
        legend.appendChild(legendItem(PASS_FILL, 'pass (bin 1)'));
        legend.appendChild(legendItem(FAIL_FILL_DEFAULT, 'fail (shaded by bin)'));
        figure.appendChild(legend);
        var omitted = Number(block.dies_omitted) || 0;
        if (omitted > 0) {
            figure.appendChild(el('div', 'wafer-map-note',
                'Showing ' + dies.length + ' of ' + (dies.length + omitted) +
                ' dies — ' + omitted + ' omitted by downsampling.'
            ));
        } else {
            figure.appendChild(el('div', 'wafer-map-caption muted small',
                dies.length + ' dies — hover for bin and coordinates.'
            ));
        }
        return figure;
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
        var dieMap = renderWaferDieMap(block);
        if (dieMap) {
            // Die grid beside the stats; flex-wraps to stacked on narrow screens.
            var body = el('div', 'wafer-body');
            body.appendChild(dieMap);
            body.appendChild(grid);
            card.appendChild(body);
        } else {
            card.appendChild(grid);
        }

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

    function renderSpcChartBlock(block) {
        var card = el('div', 'tool-card spc-card');
        card.appendChild(el('div', 'tool-card-title', block.title || 'SPC control-chart check'));
        if (block.summary) card.appendChild(el('div', 'tool-card-summary', block.summary));

        var hasLimits = typeof block.ucl === 'number' && typeof block.lcl === 'number';
        var sigmaLabel = block.sigma_source === 'provided' ? 'known' : 'estimated';
        var grid = el('div', 'spc-grid');
        function stat(label, value) {
            var cell = el('div', 'spc-stat');
            cell.appendChild(el('div', 'spc-stat-value', value));
            cell.appendChild(el('div', 'spc-stat-label', label));
            return cell;
        }
        grid.appendChild(stat('Mean', fmt(block.mean)));
        grid.appendChild(stat('Sigma (' + sigmaLabel + ')', fmt(block.sigma)));
        grid.appendChild(stat('UCL', hasLimits ? fmt(block.ucl) : 'n/a'));
        grid.appendChild(stat('LCL', hasLimits ? fmt(block.lcl) : 'n/a'));
        grid.appendChild(stat('Verdict', block.verdict === 'out_of_control' ? 'OUT OF CONTROL' : 'in control'));
        card.appendChild(grid);

        var values = Array.isArray(block.values) ? block.values : [];
        var flags = Array.isArray(block.point_flags) ? block.point_flags : [];
        if (values.length > 1 && hasLimits) {
            card.appendChild(spcRunChart(values, flags, block));
        } else if (values.length) {
            card.appendChild(el('div', 'muted small',
                'No control chart drawn: ' + (hasLimits
                    ? 'the series needs at least 2 points.'
                    : 'the limits are indeterminate for this series.')));
        }

        var violations = block.violations || [];
        if (violations.length) {
            var vList = el('ul', 'spc-violations');
            violations.forEach(function (violation) {
                var item = el('li');
                item.appendChild(el('div', 'spc-violation-label', violation.label || violation.rule || 'Rule violation'));
                if (violation.detail) item.appendChild(el('div', 'spc-violation-detail', violation.detail));
                vList.appendChild(item);
            });
            card.appendChild(vList);
        } else if (values.length) {
            card.appendChild(el('div', 'muted small', 'No Nelson-rule violations detected.'));
        }

        var issues = block.issues || [];
        if (issues.length) {
            var issueList = el('ul', 'spc-issues');
            issues.forEach(function (issue) {
                issueList.appendChild(el('li', null, issue));
            });
            card.appendChild(issueList);
        }

        var references = block.references || [];
        if (references.length) {
            var refBox = el('div', 'spc-references');
            references.forEach(function (reference) {
                refBox.appendChild(el('div', 'spc-reference', 'Reference: ' + reference));
            });
            card.appendChild(refBox);
        }
        return card;
    }

    // SVG run chart for the spc_chart block: series line, dashed UCL/center/LCL
    // guides, and highlighted markers on points that violate a Nelson rule.
    function spcRunChart(values, flags, block) {
        var svgNs = 'http://www.w3.org/2000/svg';
        var width = 720, height = 240;
        var margin = { left: 52, right: 14, top: 12, bottom: 24 };
        var plotWidth = width - margin.left - margin.right;
        var plotHeight = height - margin.top - margin.bottom;

        function svgNode(tag, attrs) {
            var node = document.createElementNS(svgNs, tag);
            Object.keys(attrs || {}).forEach(function (key) {
                node.setAttribute(key, String(attrs[key]));
            });
            return node;
        }

        var ucl = Number(block.ucl), lcl = Number(block.lcl), mean = Number(block.mean);
        var sigma = Number(block.sigma);
        var low = Math.min(lcl, Math.min.apply(null, values));
        var high = Math.max(ucl, Math.max.apply(null, values));
        if (!isFinite(low) || !isFinite(high)) { low = 0; high = 1; }
        if (high === low) { high += 1; low -= 1; }
        var pad = (high - low) * 0.08;
        low -= pad; high += pad;

        function xPixel(index) {
            return values.length === 1
                ? margin.left + plotWidth / 2
                : margin.left + (plotWidth * index) / (values.length - 1);
        }
        function yPixel(value) {
            return margin.top + plotHeight * (1 - (value - low) / (high - low));
        }

        var svg = svgNode('svg', {
            viewBox: '0 0 ' + width + ' ' + height,
            class: 'spc-chart',
            role: 'img',
            'aria-label': 'SPC run chart of ' + values.length + ' points with control limits'
        });

        // limit guides first, so the series paints on top
        function guide(value, cssClass, label) {
            if (!isFinite(value)) return;
            var y = yPixel(value);
            svg.appendChild(svgNode('line', {
                x1: margin.left, x2: width - margin.right, y1: y, y2: y,
                class: cssClass
            }));
            var text = svgNode('text', {
                x: margin.left - 6, y: y + 3.5,
                class: 'spc-chart-guide-label', 'text-anchor': 'end'
            });
            text.textContent = label;
            svg.appendChild(text);
        }
        guide(ucl, 'spc-chart-limit', 'UCL ' + fmt(ucl));
        guide(mean, 'spc-chart-center', 'x̄ ' + fmt(mean));
        guide(lcl, 'spc-chart-limit', 'LCL ' + fmt(lcl));

        var points = values.map(function (value, index) {
            return xPixel(index) + ',' + yPixel(value);
        }).join(' ');
        svg.appendChild(svgNode('polyline', {
            points: points, class: 'spc-chart-line', fill: 'none'
        }));

        var dotRadius = values.length > 300 ? 1.5 : 3;
        values.forEach(function (value, index) {
            var flagged = (flags[index] || []).length > 0;
            var circle = svgNode('circle', {
                cx: xPixel(index), cy: yPixel(value), r: dotRadius,
                class: flagged ? 'spc-chart-point flagged' : 'spc-chart-point'
            });
            var zScore = isFinite(sigma) && sigma > 0 ? (value - mean) / sigma : null;
            var tooltip = svgNode('title');
            tooltip.textContent = 'point ' + (index + 1) + ' · ' + fmt(value)
                + (zScore === null ? '' : ' (' + (zScore >= 0 ? '+' : '') + zScore.toFixed(1) + 'σ)')
                + ((flags[index] || []).length ? ' — ' + flags[index].join(', ') : '');
            circle.appendChild(tooltip);
            svg.appendChild(circle);
        });

        // axis labels: first and last point numbers
        var firstLabel = svgNode('text', {
            x: margin.left, y: height - 6, class: 'spc-chart-axis-label'
        });
        firstLabel.textContent = '1';
        var lastLabel = svgNode('text', {
            x: width - margin.right, y: height - 6,
            class: 'spc-chart-axis-label', 'text-anchor': 'end'
        });
        lastLabel.textContent = String(values.length);
        svg.appendChild(firstLabel);
        svg.appendChild(lastLabel);
        return svg;
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
            case 'spc_chart': return renderSpcChartBlock(block);
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

    // ---------- per-message feedback (the trust loop's write path) ----------

    function renderFeedbackControls(article, messageId, savedValue, feedbackUrl, csrfToken, getText) {
        // Thumbs (and the copy button) on assistant messages. History
        // passes the persisted verdict (data-feedback); a fresh turn
        // calls this once its turn_saved event has delivered the
        // message id. Clicking the active thumb clears the verdict;
        // failures revert the toggle and say so -- never silent.
        if (!feedbackUrl || !messageId || article.querySelector('.feedback')) return;
        var row = el('div', 'feedback');
        row.setAttribute('data-feedback-for', messageId);
        var statusText = el('span', 'feedback-status muted small');

        function value() {
            return row.getAttribute('data-value') || '';
        }
        function setActive(next) {
            upButton.classList.toggle('on', next === 'up');
            downButton.classList.toggle('on', next === 'down');
            upButton.setAttribute('aria-pressed', next === 'up' ? 'true' : 'false');
            downButton.setAttribute('aria-pressed', next === 'down' ? 'true' : 'false');
            row.classList.toggle('has-value', next === 'up' || next === 'down');
            row.setAttribute('data-value', next);
        }
        function setStatus(message) {
            statusText.textContent = message || '';
            statusText.hidden = !message;
        }
        function send(next) {
            var previous = value();
            setActive(next);
            setStatus('');
            var body = new FormData();
            body.append('message_id', messageId);
            body.append('value', next);
            fetch(feedbackUrl, {
                method: 'POST',
                headers: { 'X-CSRFToken': csrfToken },
                body: body
            }).then(function (response) {
                if (!response.ok) throw new Error('HTTP ' + response.status);
                return response.json();
            }).then(function (payload) {
                setActive(payload.value || '');
            }).catch(function () {
                setActive(previous);
                setStatus('Could not save feedback — try again.');
            });
        }

        var upButton = el('button', 'feedback-btn feedback-up', '\ud83d\udc4d');
        upButton.type = 'button';
        upButton.title = 'Helpful';
        upButton.setAttribute('aria-label', 'Mark this answer helpful');
        var downButton = el('button', 'feedback-btn feedback-down', '\ud83d\udc4e');
        downButton.type = 'button';
        downButton.title = 'Not helpful';
        downButton.setAttribute('aria-label', 'Mark this answer not helpful');

        upButton.addEventListener('click', function () {
            send(value() === 'up' ? 'none' : 'up');
        });
        downButton.addEventListener('click', function () {
            send(value() === 'down' ? 'none' : 'down');
        });

        var copyButton = el('button', 'feedback-btn feedback-copy', '\ud83d\udccb');
        copyButton.type = 'button';
        copyButton.title = 'Copy answer';
        copyButton.setAttribute('aria-label', 'Copy this answer to the clipboard');
        copyButton.addEventListener('click', function () {
            var text = getText ? String(getText() || '') : '';
            if (!text) {
                setStatus('Nothing to copy yet.');
                return;
            }
            copyToClipboard(text).then(function (ok) {
                if (ok) {
                    setStatus('Copied.');
                    window.setTimeout(function () { setStatus(''); }, 2000);
                } else {
                    setStatus('Could not copy — select the text instead.');
                }
            });
        });

        row.appendChild(statusText);
        row.appendChild(upButton);
        row.appendChild(downButton);
        row.appendChild(copyButton);
        setActive(savedValue || '');
        article.appendChild(row);
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

    function rawAnswerText(article) {
        // The answer's original markdown: the json_script payload for
        // hydrated history; the rendered text is the honest fallback
        // when a fresh turn never got a payload node.
        var contentFor = article.querySelector('[data-content-for]');
        if (contentFor) {
            var stored = jsonFromScript(contentFor.getAttribute('data-content-for'));
            if (typeof stored === 'string') return stored;
        }
        var contentEl = article.querySelector('.msg-content');
        return contentEl ? contentEl.textContent : '';
    }

    function hydrateHistory(thread, feedbackUrl, csrfToken) {
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
            if (role === 'assistant') {
                renderFeedbackControls(
                    article,
                    article.getAttribute('data-message-id'),
                    article.getAttribute('data-feedback') || '',
                    feedbackUrl,
                    csrfToken,
                    function () { return rawAnswerText(article); }
                );
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
        var exampleButton = $('#example-wafer-button');
        var fileInput = $('#kb-file-input');
        var uploadStatus = $('#upload-status');
        var conversationList = $('#conversation-list');
        var tutorToggle = $('#tutor-toggle');
        var modeStatus = $('#mode-status');
        var notebookSelect = $('#notebook-select');
        var regenerateButton = $('#regenerate-button');

        // Raw answer text per rendered assistant article, for the copy
        // button (fresh turns have no json_script payload to read).
        var rawAnswers = new WeakMap();

        var state = {
            conversationId: root.getAttribute('data-conversation-id') || '',
            streamUrl: root.getAttribute('data-stream-url'),
            uploadUrl: root.getAttribute('data-upload-url'),
            exampleWaferUrl: root.getAttribute('data-example-wafer-url') || '',
            chatHomeUrl: root.getAttribute('data-chat-home-url'),
            modeUrl: root.getAttribute('data-mode-url') || '',
            feedbackUrl: root.getAttribute('data-feedback-url') || '',
            renameUrl: root.getAttribute('data-rename-url') || '',
            deleteUrlTemplate: root.getAttribute('data-delete-url-template') || '',
            regenerateUrlTemplate: root.getAttribute('data-regenerate-url-template') || '',
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
            // The notebook scope is creation-time only: locked once the
            // conversation exists, disabled while a turn streams.
            if (notebookSelect) {
                notebookSelect.disabled = streaming || Boolean(state.conversationId);
            }
            if (regenerateButton) regenerateButton.disabled = streaming;
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
                complete: false,
                savedMessageId: ''
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

        function replaceToolCard(turn, name, block, callId) {
            var rendered = renderToolBlock(block);
            var running = turn.blocks.querySelectorAll('.tool-running');
            if (callId) {
                // The tool_call card carries the call id; the result event
                // now carries it too, so same-name calls each replace their
                // own card. Attribute read, not a selector -- call ids are
                // model-generated and never query-interpolated.
                for (var i = 0; i < running.length; i++) {
                    if (running[i].getAttribute('data-call-id') === callId) {
                        turn.blocks.replaceChild(rendered, running[i]);
                        return;
                    }
                }
            }
            for (var j = 0; j < running.length; j++) {
                // Fallback: results without a call id (older streams) match
                // the first still-running card of the same tool, in order.
                if (!name || running[j].textContent.indexOf(name) !== -1) {
                    turn.blocks.replaceChild(rendered, running[j]);
                    return;
                }
            }
            turn.blocks.appendChild(rendered);
        }

        function addTurnFooter(turn, latencyMs) {
            if (turn.article.querySelector('.turn-footer')) return;
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

        function notebookBadge(name) {
            var badge = el('span', 'notebook-badge');
            badge.setAttribute('data-notebook-badge', '');
            badge.setAttribute('title', 'Notebook-scoped: ' + name);
            badge.textContent = name;
            return badge;
        }

        function deleteFormFor(id) {
            // Fresh rows get the same working delete control the server
            // renders: a POST form with its CSRF token and the confirm
            // guard (the document-level submit listener honors it).
            var form = el('form', 'inline');
            form.method = 'post';
            form.action = state.deleteUrlTemplate.replace('/0/', '/' + id + '/');
            form.setAttribute('data-confirm', 'Delete this conversation and its messages?');
            var token = el('input');
            token.type = 'hidden';
            token.name = 'csrfmiddlewaretoken';
            token.value = csrf;
            form.appendChild(token);
            var button = el('button', 'icon-btn delete-btn', '\u00d7');
            button.type = 'submit';
            button.setAttribute('aria-label', 'Delete conversation');
            button.setAttribute('title', 'Delete conversation');
            form.appendChild(button);
            return form;
        }

        function bumpSidebarCap() {
            // The indicator counts conversations; a new row makes it one more.
            var cap = conversationList.querySelector('[data-sidebar-cap]');
            if (!cap) return;
            var limit = cap.getAttribute('data-sidebar-limit') || '50';
            var total = Number(cap.getAttribute('data-sidebar-total')) + 1;
            cap.setAttribute('data-sidebar-total', String(total));
            cap.textContent = 'Showing the ' + limit + ' most recent of ' + total +
                ' conversations — older ones are hidden.';
        }

        function addSidebarConversation(id, title, mode, notebookName) {
            if (sidebarConversationExists(id)) return;
            var item = el('div', 'conversation-item');
            item.setAttribute('data-conversation-item', id);
            var link = el('a', 'conversation-link', title || 'New conversation');
            link.href = state.chatHomeUrl + '?c=' + id;
            link.title = title || 'New conversation';
            item.appendChild(link);
            if (mode === 'tutor') item.appendChild(modeBadge());
            if (notebookName) item.appendChild(notebookBadge(notebookName));
            var renameButton = el('button', 'icon-btn rename-btn', '\u270e');
            renameButton.type = 'button';
            renameButton.setAttribute('data-rename-btn', '');
            renameButton.setAttribute('aria-label', 'Rename conversation');
            renameButton.setAttribute('title', 'Rename conversation');
            item.appendChild(renameButton);
            item.appendChild(deleteFormFor(id));
            conversationList.prepend(item);
            bumpSidebarCap();
        }

        function updateSidebarBadge(id, mode) {
            var item = conversationList.querySelector('[data-conversation-item="' + id + '"]');
            if (!item) return;
            var existing = item.querySelector('[data-mode-badge]');
            if (mode === 'tutor' && !existing) {
                // Server row order: link, badge, rename, delete. The rename
                // button is the badge's left neighbor on fresh rows.
                var anchor = item.querySelector('[data-rename-btn]') || item.querySelector('form');
                if (anchor) item.insertBefore(modeBadge(), anchor);
                else item.appendChild(modeBadge());
            } else if (mode !== 'tutor' && existing) {
                existing.remove();
            }
        }

        function selectedNotebookName() {
            if (!notebookSelect || !notebookSelect.value) return '';
            var option = notebookSelect.options[notebookSelect.selectedIndex];
            return option ? option.textContent : '';
        }

        function setConversation(id, title) {
            state.conversationId = String(id);
            root.setAttribute('data-conversation-id', state.conversationId);
            addSidebarConversation(id, title, state.mode, selectedNotebookName());
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
                    replaceToolCard(turn, data.name, data.block, data.call_id);
                    scrollBottom();
                    break;
                case 'sources':
                    renderChips(turn.chips, data.sources || []);
                    scrollBottom();
                    break;
                case 'turn_saved':
                    // Emitted after persistence; carries the assistant
                    // message's pk -- the per-message feedback hook.
                    turn.savedMessageId = data.message_id ? String(data.message_id) : '';
                    break;
                case 'done':
                    turn.complete = true;
                    turn.article.classList.remove('streaming');
                    hideIndicator(turn);
                    addTurnFooter(turn, data.latency_ms);
                    rawAnswers.set(turn.article, turn.text);
                    if (turn.savedMessageId) {
                        renderFeedbackControls(
                            turn.article,
                            turn.savedMessageId,
                            '',
                            state.feedbackUrl,
                            csrf,
                            function () {
                                return rawAnswers.get(turn.article) ||
                                    rawAnswerText(turn.article);
                            }
                        );
                    }
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

        function streamTurn(url, body, turn, hooks) {
            // The one fetch/pump loop both sends and regenerations share.
            // hooks.onStart fires once the stream is confirmed live;
            // hooks.onFail fires on a failure that never reached the
            // server (the caller can undo optimistic DOM changes).
            setStreaming(true);
            showIndicator(turn, 'Athena is thinking…');

            var controller = new AbortController();
            state.controller = controller;
            var landed = false;

            fetch(url, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrf,
                    'Accept': 'text/event-stream'
                },
                body: JSON.stringify(body),
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
                landed = true;
                if (hooks && hooks.onStart) hooks.onStart();
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
                    if (hooks && hooks.onFail && !landed) hooks.onFail();
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

        function runTurn(text) {
            var turn = createTurn();
            turn.titleHint = text.length > 60 ? text.slice(0, 60) + '…' : text;
            appendUserMessage(text);
            streamTurn(state.streamUrl, {
                message: text,
                conversation_id: state.conversationId ? Number(state.conversationId) : undefined,
                // Creation-time only: an existing conversation's persisted
                // mode rules, changed through the mode endpoint.
                mode: state.conversationId ? undefined : state.mode,
                // Creation-time only: an existing conversation keeps its
                // own notebook scope; empty means the whole KB.
                notebook_id: state.conversationId || !notebookSelect
                    ? undefined
                    : (notebookSelect.value || undefined)
            }, turn);
        }

        // ----- regenerate the last answer (spec #9) -----

        function lastAssistantArticle() {
            var articles = thread.querySelectorAll('.message[data-role="assistant"]');
            return articles.length ? articles[articles.length - 1] : null;
        }

        function regenerateLastAnswer() {
            if (state.streaming || !state.conversationId) return;
            if (!state.regenerateUrlTemplate) return;
            var previous = lastAssistantArticle();
            if (!previous) return;
            // Regenerating replaces the old answer (the server deletes
            // its row); the confirm keeps an idle click from costing it.
            if (!window.confirm('Replace the last answer with a fresh one?')) return;
            var turn = createTurn();
            previous.remove();
            streamTurn(
                state.regenerateUrlTemplate.replace('/0/', '/' + state.conversationId + '/'),
                {},
                turn,
                {
                    // The request never landed: put the old answer back
                    // where it was instead of pretending it streamed.
                    onFail: function () {
                        thread.insertBefore(previous, turn.article);
                    }
                }
            );
        }

        // ----- wafer CSV handoff (storage-only documents -> analyzer) -----

        function addAnalyzeAffordance(container, filePath) {
            // One click from a stored CSV to the analyzer: the turn asks
            // for the analysis by storage name; the agent calls
            // wafer_map_analyze(path=...) through its schema.
            var btn = el('button', 'upload-analyze-btn', 'Analyze with the wafer tool');
            btn.type = 'button';
            btn.addEventListener('click', function () {
                if (state.streaming) return;
                btn.disabled = true;
                runTurn('Analyze the wafer CSV stored at "' + filePath +
                    '" with the wafer map tool.');
            });
            container.appendChild(btn);
        }

        function loadExampleWafer() {
            if (state.streaming || !state.exampleWaferUrl) return;
            exampleButton.disabled = true;
            uploadStatus.hidden = false;
            uploadStatus.className = 'upload-status';
            uploadStatus.innerHTML = '';
            var line = el('div', 'upload-line');
            var stateText = el('div', 'upload-state muted small', 'Loading example wafer CSV…');
            line.appendChild(stateText);
            uploadStatus.appendChild(line);

            fetch(state.exampleWaferUrl, {
                method: 'POST',
                headers: { 'X-CSRFToken': csrf }
            }).then(function (response) {
                return response.json().catch(function () { return {}; }).then(function (payload) {
                    return { status: response.status, payload: payload };
                });
            }).then(function (result) {
                var payload = result.payload || {};
                var ok = result.status === 201 || (result.status === 200 && payload.duplicate);
                if (ok) {
                    uploadStatus.classList.add('upload-ok');
                    stateText.textContent = (result.status === 200
                        ? '✓ Example wafer CSV is already in your knowledge base.'
                        : '✓ Example wafer CSV loaded into your knowledge base.');
                    if (payload.file_path) addAnalyzeAffordance(line, payload.file_path);
                } else {
                    // The 503 case: the deployment is missing the bundled
                    // example. Say so; never pretend it loaded.
                    uploadStatus.classList.add('upload-error');
                    stateText.textContent = '✗ Could not load the example wafer CSV (HTTP ' +
                        result.status + '): ' + (payload.error || 'unavailable on this deployment.');
                }
            }).catch(function () {
                uploadStatus.classList.add('upload-error');
                stateText.textContent = '✗ Could not load the example wafer CSV: network error.';
            }).finally(function () {
                exampleButton.disabled = false;
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
                    if (payload && payload.file_path) {
                        // CSV: storage-only document, zero chunks by design.
                        // Offer the one-click handoff to the analyzer.
                        stateText.textContent = '✓ ' + file.name +
                            ' ready — stored for the wafer analyzer.';
                        addAnalyzeAffordance(line, payload.file_path);
                    } else {
                        stateText.textContent = '✓ ' + file.name + ' ready — ' +
                            (payload && payload.chunks !== undefined
                                ? payload.chunks + ' chunks embedded.'
                                : 'embedded.');
                    }
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

        // ----- inline rename (per-row pencil) -----

        function startRename(item) {
            if (!item || item.querySelector('.rename-input')) return;
            var id = item.getAttribute('data-conversation-item');
            var link = item.querySelector('.conversation-link');
            if (!id || !link) return;
            var current = link.textContent;
            var input = el('input', 'rename-input');
            input.type = 'text';
            input.value = current;
            input.maxLength = 200;
            input.setAttribute('aria-label', 'Conversation title');
            link.hidden = true;
            item.insertBefore(input, link);
            input.focus();
            input.select();
            var settled = false;
            function finish() {
                settled = true;
                input.remove();
                link.hidden = false;
            }
            function commit() {
                var next = input.value.trim();
                if (!next || next === current) {
                    // A blank or unchanged title is a cancel, not a save.
                    finish();
                    return;
                }
                var body = new FormData();
                body.append('conversation_id', id);
                body.append('title', next);
                fetch(state.renameUrl, {
                    method: 'POST',
                    headers: { 'X-CSRFToken': csrf },
                    body: body
                }).then(function (response) {
                    if (!response.ok) throw new Error('HTTP ' + response.status);
                    return response.json();
                }).then(function (payload) {
                    link.textContent = payload.title;
                    link.title = payload.title;
                    finish();
                }).catch(function () {
                    // Honest failure: keep the editor open, mark it, and
                    // say how to recover -- the old title stays put.
                    input.classList.add('rename-error');
                    input.title = 'Rename failed — press Enter to retry or Escape to cancel';
                    input.focus();
                });
            }
            input.addEventListener('keydown', function (event) {
                if (event.key === 'Enter') {
                    event.preventDefault();
                    commit();
                } else if (event.key === 'Escape') {
                    event.preventDefault();
                    finish();
                }
            });
            input.addEventListener('blur', function () {
                if (!settled) commit();
            });
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

        if (regenerateButton) {
            regenerateButton.addEventListener('click', regenerateLastAnswer);
        }

        conversationList.addEventListener('click', function (event) {
            var button = event.target.closest('[data-rename-btn]');
            if (!button) return;
            event.preventDefault();
            startRename(button.closest('[data-conversation-item]'));
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

        if (exampleButton) {
            exampleButton.addEventListener('click', loadExampleWafer);
        }

        document.addEventListener('submit', function (event) {
            var formEl = event.target;
            if (formEl && formEl.matches && formEl.matches('[data-confirm]')) {
                if (!window.confirm(formEl.getAttribute('data-confirm'))) {
                    event.preventDefault();
                }
            }
        });

        hydrateHistory(thread, state.feedbackUrl, csrf);
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
