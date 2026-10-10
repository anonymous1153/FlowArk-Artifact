  function isTranscriptCardModeEnabled() {
    return state.transcriptFormatted;
  }

  function refreshTranscriptFormatButtonState() {
    const inFullscreen = state.activeFullscreenPanelId === 'transcript-panel';
    const btn = els.transcriptFormatBtn;
    if (!btn) return;
    btn.classList.toggle('active', state.transcriptFormatted);
    btn.textContent = `Card view: ${state.transcriptFormatted ? 'on' : 'off'}`;
    btn.title = inFullscreen ? 'Show transcript as cards' : 'Card mode applies in normal and fullscreen views';
    refreshTranscriptPairingButtonState();
  }

  function refreshTranscriptPairingButtonState() {
    const inFullscreen = state.activeFullscreenPanelId === 'transcript-panel';
    const btn = els.transcriptPairingBtn;
    if (!btn) return;
    btn.classList.toggle('active', state.transcriptAutoPairing);
    const enabled = state.transcriptFormatted;
    btn.textContent = `Pairing: ${state.transcriptAutoPairing ? 'on' : 'off'}`;
    btn.title = enabled ? 'Auto-pair ToolUse/ToolResult by tool_call_id in card mode' : 'Only available when card view is on';
  }
  function getTranscriptCardTypeClass(className) {
    const cls = String(className || '').toLowerCase();
    if (cls.includes('system')) return 'card-type-system';
    if (cls.includes('assistant')) return 'card-type-assistant';
    if (cls.includes('user')) return 'card-type-user';
    if (cls.includes('tool')) return 'card-type-tool';
    if (cls.includes('result')) return 'card-type-result';
    if (cls.includes('error')) return 'card-type-error';
    return '';
  }

  function formatTranscriptMetricValue(value) {
    if (value == null || value === '') return '';
    const num = Number(value);
    if (Number.isFinite(num)) return String(value);
    return String(value);
  }

  function renderTranscriptTokenSummary(tokens) {
    if (!tokens || typeof tokens !== 'object') return '';
    const cache = tokens.cache && typeof tokens.cache === 'object' ? tokens.cache : {};
    const parts = [
      ['input', tokens.input],
      ['output', tokens.output],
      ['reasoning', tokens.reasoning],
      ['total', tokens.total],
      ['cache.read', cache.read],
      ['cache.write', cache.write],
    ]
      .filter(([, value]) => value != null && value !== '')
      .map(([key, value]) => `${key}=${formatTranscriptMetricValue(value)}`);
    return parts.join(' · ');
  }

  function buildTranscriptContentPreview(content) {
    const raw = String(content || '');
    const MAX_LINES = 80;
    const PREVIEW_LINES = 12;
    const MAX_CHARS = 8000;
    const lines = raw.split('\n');
    const needsPreview = lines.length > MAX_LINES || raw.length > MAX_CHARS;
    if (!needsPreview) {
      return {
        text: raw,
        truncated: false,
        lineCount: lines.length,
        charCount: raw.length,
        omittedLines: 0,
        omittedChars: 0,
      };
    }
    let preview = lines.slice(0, PREVIEW_LINES).join('\n');
    if (preview.length > MAX_CHARS) preview = preview.slice(0, MAX_CHARS);
    return {
      text: preview,
      truncated: true,
      lineCount: lines.length,
      charCount: raw.length,
      omittedLines: Math.max(0, lines.length - PREVIEW_LINES),
      omittedChars: Math.max(0, raw.length - preview.length),
    };
  }

  function renderTranscriptCardHtml(line, index, toolCallIndex, parsedLine, knowledgeEvents = []) {
    const parsed = parsedLine || tryParseTranscriptLine(line);
    if (!parsed) return '';
    const { prefix, body, parsed: obj } = parsed;
    if (!obj) {
      const rawPreview = buildTranscriptContentPreview(body);
      return `
        <article class="transcript-card transcript-card-raw">
          <div class="transcript-card-head">
            <div class="transcript-card-title">Raw Line</div>
            <div class="transcript-card-badges">
              ${prefix ? `<span class="badge">${escapeHtml(prefix)}</span>` : ''}
              <span class="badge muted">#${index + 1}</span>
            </div>
          </div>
          <div class="transcript-card-content prewrap">${escapeHtml(rawPreview.text)}</div>
          ${rawPreview.truncated ? `<div class="muted transcript-preview-note">Truncated ${rawPreview.omittedLines} lines / ${rawPreview.omittedChars} chars; open the raw artifact for the full content.</div>` : ''}
        </article>
      `;
    }

    const p = extractTranscriptPriorityFields(obj);
    const stepFinish = p.stepFinish && typeof p.stepFinish === 'object' ? p.stepFinish : null;
    const tokenSummary = stepFinish ? renderTranscriptTokenSummary(stepFinish.tokens) : '';
    const restKeys = Object.keys(p.rest || {}).filter((k) => p.rest[k] != null && !(typeof p.rest[k] === 'string' && p.rest[k] === ''));
    const restBlock = restKeys.length
      ? `<details class="transcript-card-details"><summary>Expand remaining fields (${restKeys.length})</summary><pre>${escapeHtml(formatParsedObject(p.rest))}</pre></details>`
      : '';
    const knowledgeBlock = renderKnowledgeEventSection(knowledgeEvents);

    let contentHtml = '';
    if (p.content) {
      const preview = buildTranscriptContentPreview(p.content);
      if (preview.truncated) {
        contentHtml = `
          <div class="transcript-card-section">
            <div class="section-label">content <span class="line-count-hint">(${preview.lineCount} lines · ${preview.charCount} chars)</span></div>
            <div class="transcript-card-content-collapsible">
              <div class="transcript-card-content prewrap transcript-content-preview">${escapeHtml(preview.text)}</div>
              <div class="muted transcript-preview-note">Truncated ${preview.omittedLines} lines / ${preview.omittedChars} chars; open the raw artifact for the full content.</div>
            </div>
          </div>
        `;
      } else {
        contentHtml = `
          <div class="transcript-card-section">
            <div class="section-label">content</div>
            <div class="transcript-card-content prewrap">${escapeHtml(preview.text)}</div>
          </div>
        `;
      }
    }

    // Determine card type class for color coding
    const cardTypeClass = getTranscriptCardTypeClass(p.className);

    // Build tool call pairing info
    const cardId = `transcript-card-${index}`;
    let toolPairHtml = '';
    if (p.toolCallId && toolCallIndex) {
      const pairInfo = toolCallIndex.get(p.toolCallId);

      if (p.hasToolUse && pairInfo) {
        const hasResult = pairInfo.toolResultIndex >= 0;
        const resultBadge = hasResult
          ? `<span class="badge badge-paired badge-clickable" data-target-card="transcript-card-${pairInfo.toolResultIndex}">Result #${pairInfo.toolResultIndex + 1}</span>`
          : `<span class="badge badge-unpaired">Waiting for result</span>`;
        toolPairHtml = `
          <div class="kv-row">
            <span class="k">tool_id</span>
            <span class="v">${escapeHtml(p.toolCallId)} ${resultBadge}</span>
          </div>
        `;
      } else if (p.hasToolResult && pairInfo) {
        const hasCall = pairInfo.toolUseIndex >= 0;
        const toolNameDisplay = pairInfo.toolUseName || p.toolName || '(unknown)';
        const callBadge = hasCall
          ? `<span class="badge badge-paired badge-clickable" data-target-card="transcript-card-${pairInfo.toolUseIndex}">Call #${pairInfo.toolUseIndex + 1}</span>`
          : `<span class="badge badge-unpaired">Call not found</span>`;
        toolPairHtml = `
          <div class="kv-row">
            <span class="k">tool_use_id</span>
            <span class="v">${escapeHtml(p.toolCallId)} ${callBadge}</span>
          </div>
          <div class="kv-row">
            <span class="k">tool_name</span>
            <span class="v">${escapeHtml(toolNameDisplay)}</span>
          </div>
        `;
      } else if (p.hasToolResult) {
        toolPairHtml = `
          <div class="kv-row">
            <span class="k">tool_use_id</span>
            <span class="v">${escapeHtml(p.toolCallId)} <span class="badge badge-unpaired">Call not found</span></span>
          </div>
        `;
      }
    }

    return `
      <article id="${cardId}" class="transcript-card ${cardTypeClass}">
        <div class="transcript-card-head">
          <div class="transcript-card-title">${escapeHtml(p.className || 'Message')}</div>
          <div class="transcript-card-badges">
            ${p.model ? `<span class="badge">${escapeHtml(p.model)}</span>` : ''}
            ${stepFinish?.reason ? `<span class="badge">reason=${escapeHtml(stepFinish.reason)}</span>` : ''}
            ${stepFinish?.cost != null ? `<span class="badge">cost=${escapeHtml(formatTranscriptMetricValue(stepFinish.cost))}</span>` : ''}
            ${prefix ? `<span class="badge">${escapeHtml(prefix)}</span>` : ''}
            <span class="badge muted">#${index + 1}</span>
          </div>
        </div>
        <div class="transcript-card-meta">
          ${toolPairHtml}
          ${stepFinish?.reason ? `<div class="kv-row"><span class="k">finish_reason</span><span class="v">${escapeHtml(stepFinish.reason)}</span></div>` : ''}
          ${stepFinish?.cost != null ? `<div class="kv-row"><span class="k">cost</span><span class="v">${escapeHtml(formatTranscriptMetricValue(stepFinish.cost))}</span></div>` : ''}
          ${tokenSummary ? `<div class="kv-row"><span class="k">tokens</span><span class="v">${escapeHtml(tokenSummary)}</span></div>` : ''}
          ${p.error ? `<div class="kv-row error"><span class="k">error</span><span class="v prewrap">${escapeHtml(p.error)}</span></div>` : ''}
        </div>
        ${contentHtml}
        ${knowledgeBlock}
        ${restBlock}
      </article>
    `;
  }

  /**
   * Build an index mapping tool_call_id -> { toolUseIndex, toolUseName, toolResultIndex }
   */
  function buildToolCallIndex(entries) {
    const index = new Map();
    const parsedLines = [];

    for (let i = 0; i < entries.length; i += 1) {
      const entry = entries[i];
      const line = typeof entry === 'string' ? entry : String(entry?.text || '');
      if (!line || !line.trim()) {
        parsedLines.push(null);
        continue;
      }
      const parsed = entry && typeof entry === 'object' && entry.parsed
        ? entry.parsed
        : tryParseTranscriptLine(line);
      parsedLines.push(parsed);

      if (!parsed || !parsed.parsed) continue;
      const obj = parsed.parsed;
      const cls = String(obj.__class__ || '').toLowerCase();

      // Check for ToolUse at top level
      if (typeof obj.id === 'string' && obj.id.startsWith('call_')) {
        const toolCallId = obj.id;
        const toolName = obj.name || '';
        if (toolCallId) {
          if (!index.has(toolCallId)) {
            index.set(toolCallId, { toolUseIndex: i, toolUseName: toolName, toolResultIndex: -1 });
          } else {
            index.get(toolCallId).toolUseIndex = i;
            index.get(toolCallId).toolUseName = toolName || index.get(toolCallId).toolUseName;
          }
        }
      }

      // Check content array for ToolUseBlock
      if (Array.isArray(obj.content)) {
        for (const block of obj.content) {
          if (block && typeof block === 'object') {
            // ToolUseBlock
            if (typeof block.id === 'string' && block.id.startsWith('call_')) {
              const toolCallId = block.id;
              const toolName = block.name || '';
              if (toolCallId) {
                if (!index.has(toolCallId)) {
                  index.set(toolCallId, { toolUseIndex: i, toolUseName: toolName, toolResultIndex: -1 });
                } else {
                  index.get(toolCallId).toolUseIndex = i;
                  index.get(toolCallId).toolUseName = toolName || index.get(toolCallId).toolUseName;
                }
              }
            }
            // ToolResultBlock
            if (typeof block.tool_use_id === 'string') {
              const toolCallId = block.tool_use_id;
              if (toolCallId) {
                if (!index.has(toolCallId)) {
                  index.set(toolCallId, { toolUseIndex: -1, toolUseName: '', toolResultIndex: i });
                } else {
                  index.get(toolCallId).toolResultIndex = i;
                }
              }
            }
          }
        }
      }

      // Check for ToolResult at top level
      if (typeof obj.tool_use_id === 'string') {
        const toolCallId = obj.tool_use_id;
        if (toolCallId) {
          if (!index.has(toolCallId)) {
            index.set(toolCallId, { toolUseIndex: -1, toolUseName: '', toolResultIndex: i });
          } else {
            index.get(toolCallId).toolResultIndex = i;
          }
        }
      }
    }

    return { index, parsedLines };
  }

  function transcriptParseCacheKey(text) {
    const raw = String(text || '');
    const path = normalizePathForCompare(state.panePaths?.transcript || '');
    const head = raw.slice(0, 512);
    const tail = raw.slice(-512);
    return `${path}::${raw.length}::${head}::${tail}`;
  }

  function getCachedTranscriptParse(text) {
    const raw = String(text || '');
    const cacheKey = transcriptParseCacheKey(raw);
    if (!state.transcriptParseCache || typeof state.transcriptParseCache !== 'object') {
      state.transcriptParseCache = {};
      state.transcriptParseCacheOrder = [];
    }
    const cached = state.transcriptParseCache[cacheKey];
    if (cached) return cached;
    const entries = splitTranscriptEntries(raw)
      .filter((entry) => String(entry?.text || '').trim());
    const lines = entries.map((entry) => String(entry?.text || ''));
    const { index: toolCallIndex, parsedLines } = buildToolCallIndex(entries);
    const parsed = { entries, lines, toolCallIndex, parsedLines };
    state.transcriptParseCache[cacheKey] = parsed;
    state.transcriptParseCacheOrder = [...(state.transcriptParseCacheOrder || []), cacheKey];
    while (state.transcriptParseCacheOrder.length > 12) {
      const oldKey = state.transcriptParseCacheOrder.shift();
      if (oldKey) delete state.transcriptParseCache[oldKey];
    }
    return parsed;
  }

  function renderTranscriptPairGroupHtml({
    toolCallId,
    pairInfo,
    useLine,
    useIndex,
    useParsed,
    resultLine,
    resultIndex,
    resultParsed,
    toolCallIndex,
    useKnowledgeEvents,
    resultKnowledgeEvents,
  }) {
    const toolName = (pairInfo && pairInfo.toolUseName) || '';
    const title = toolName ? `Tool-call pairing · ${toolName}` : 'Tool-call pairing';
    const left = renderTranscriptCardHtml(useLine, useIndex, toolCallIndex, useParsed, useKnowledgeEvents);
    const right = renderTranscriptCardHtml(resultLine, resultIndex, toolCallIndex, resultParsed, resultKnowledgeEvents);
    const knowledgeCount = (Array.isArray(useKnowledgeEvents) ? useKnowledgeEvents.length : 0)
      + (Array.isArray(resultKnowledgeEvents) ? resultKnowledgeEvents.length : 0);
    return `
      <section class="transcript-pair-group" data-tool-call-id="${escapeHtml(toolCallId || '')}">
        <div class="transcript-pair-group-head">
          <div class="transcript-pair-group-title">${escapeHtml(title)}</div>
          <div class="transcript-pair-group-badges">
            <span class="badge badge-paired">Paired</span>
            ${knowledgeCount ? `<span class="badge">knowledge=${knowledgeCount}</span>` : ''}
            ${toolCallId ? `<span class="badge">${escapeHtml(toolCallId)}</span>` : ''}
          </div>
        </div>
        <div class="transcript-pair-grid">
          <div class="transcript-pair-col">
            <div class="transcript-pair-col-label">ToolUse</div>
            ${left}
          </div>
          <div class="transcript-pair-col">
            <div class="transcript-pair-col-label">ToolResult</div>
            ${right}
          </div>
        </div>
      </section>
    `;
  }

  function renderTranscriptCards(text) {
    const { lines, toolCallIndex, parsedLines } = getCachedTranscriptParse(text);
    const knowledgeAssoc = buildKnowledgeInjectionAssociations(lines, parsedLines);
    const cards = [];
    const queryCard = buildTranscriptInitialQueryCard(knowledgeAssoc.initialEvents);
    if (queryCard) cards.push(queryCard);
    const consumed = new Set();
    const enablePairing = !!state.transcriptAutoPairing;
    for (let i = 0; i < lines.length; i += 1) {
      if (consumed.has(i)) continue;
      const line = lines[i];
      if (!line || !line.trim()) continue;
      const parsedLine = parsedLines[i];
      const parsedObj = parsedLine && parsedLine.parsed && typeof parsedLine.parsed === 'object'
        ? parsedLine.parsed
        : null;

      if (enablePairing && parsedObj) {
        const p = extractTranscriptPriorityFields(parsedObj);
        if (p.hasToolUse && p.toolCallId && toolCallIndex.has(p.toolCallId)) {
          const pairInfo = toolCallIndex.get(p.toolCallId);
          const resultIndex = Number(pairInfo?.toolResultIndex ?? -1);
          if (resultIndex > i && resultIndex < lines.length && !consumed.has(resultIndex)) {
            const resultLine = lines[resultIndex];
            const resultParsed = parsedLines[resultIndex];
            const resultObj = resultParsed && resultParsed.parsed && typeof resultParsed.parsed === 'object'
              ? resultParsed.parsed
              : null;
            if (resultObj) {
              const rp = extractTranscriptPriorityFields(resultObj);
              if (rp.hasToolResult && rp.toolCallId === p.toolCallId) {
                cards.push(renderTranscriptPairGroupHtml({
                  toolCallId: p.toolCallId,
                  pairInfo,
                  useLine: line,
                  useIndex: i,
                  useParsed: parsedLine,
                  resultLine,
                  resultIndex,
                  resultParsed,
                  toolCallIndex,
                  useKnowledgeEvents: knowledgeAssoc.lineEventMap.get(i) || [],
                  resultKnowledgeEvents: knowledgeAssoc.lineEventMap.get(resultIndex) || [],
                }));
                consumed.add(i);
                consumed.add(resultIndex);
                continue;
              }
            }
          }
        }
      }

      cards.push(renderTranscriptCardHtml(line, i, toolCallIndex, parsedLine, knowledgeAssoc.lineEventMap.get(i) || []));
    }
    if (!cards.length) {
      return '<div class="transcript-card-empty">No transcript content</div>';
    }
    return `<div class="transcript-card-list">${cards.join('')}</div>`;
  }
