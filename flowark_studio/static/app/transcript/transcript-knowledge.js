  function renderSyntheticTranscriptCard({
    title,
    badges = [],
    metaRows = [],
    content = '',
    detailsHtml = '',
    cardTypeClass = 'card-type-system',
  }) {
    const badgeHtml = (badges || [])
      .filter((x) => String(x || '').trim())
      .map((x) => `<span class="badge">${escapeHtml(String(x))}</span>`)
      .join('');
    const metaHtml = (metaRows || [])
      .filter((row) => row && String(row.k || '').trim() && String(row.v ?? '').trim())
      .map((row) => `<div class="kv-row"><span class="k">${escapeHtml(String(row.k))}</span><span class="v prewrap">${escapeHtml(String(row.v))}</span></div>`)
      .join('');
    const contentBlock = String(content || '').trim()
      ? `<div class="transcript-card-section"><div class="section-label">content</div><div class="transcript-card-content prewrap">${escapeHtml(String(content))}</div></div>`
      : '';
    return `
      <article class="transcript-card ${escapeHtml(cardTypeClass)}">
        <div class="transcript-card-head">
          <div class="transcript-card-title">${escapeHtml(String(title || 'Synthetic'))}</div>
          <div class="transcript-card-badges">${badgeHtml}</div>
        </div>
        <div class="transcript-card-meta">${metaHtml}</div>
        ${contentBlock}
        ${detailsHtml || ''}
      </article>
    `;
  }

  function buildKnowledgeSkillEntries(knowledgeEvents) {
    const entries = [];
    for (const ev of (knowledgeEvents || [])) {
      const injectedSet = new Set((Array.isArray(ev?.injected_skill_ids) ? ev.injected_skill_ids : []).map((x) => String(x)));
      const matchedSet = new Set((Array.isArray(ev?.matched_skill_ids) ? ev.matched_skill_ids : []).map((x) => String(x)));
      const detailById = {};
      for (const detail of (Array.isArray(ev?.details) ? ev.details : [])) {
        if (!detail || typeof detail !== 'object') continue;
        if (String(detail.type || '') !== 'skill') continue;
        const sid = String(detail.skill_id || '').trim();
        if (!sid) continue;
        detailById[sid] = detail;
      }
      const idSet = new Set([...Object.keys(detailById), ...injectedSet, ...matchedSet]);
      for (const skillId of idSet) {
        const detail = detailById[skillId] || null;
        entries.push({
          skill_id: skillId,
          injected: injectedSet.has(skillId),
          matched: matchedSet.has(skillId),
          node_type: String(detail?.node_type || ''),
          validation_status: String(detail?.validation_status || ''),
          injection_mode: String(detail?.mode || ''),
          hook_event_name: String(ev?.hook_event_name || ''),
          mode: String(ev?.mode || ''),
          delta: !!ev?.delta,
          timestamp: String(ev?.timestamp || ''),
          knowledge_ref: String(detail?.knowledge_ref || `knowledge://${skillId}`),
          score: detail?.score,
          constraint_mode: String(detail?.constraint_mode || ''),
          original_constraint_mode: String(detail?.original_constraint_mode || ''),
          match_fields: Array.isArray(detail?.match_fields) ? detail.match_fields : [],
          reasons: Array.isArray(detail?.reasons) ? detail.reasons : [],
          skill_file_path: String(detail?.skill_file_path || ''),
          skill_file_snapshot: String(detail?.skill_file_snapshot || ''),
          injected_prompt_block: String(detail?.injected_prompt_block || ''),
          detail_raw: detail,
          event_raw: ev,
        });
      }
    }
    entries.sort((a, b) => {
      if (a.injected !== b.injected) return a.injected ? -1 : 1;
      const as = Number(a.score ?? -1);
      const bs = Number(b.score ?? -1);
      if (as !== bs) return bs - as;
      return a.skill_id.localeCompare(b.skill_id);
    });
    return entries;
  }

  function buildKnowledgeSkillAggregate(entries) {
    const bySkill = new Map();
    for (const item of (entries || [])) {
      const sid = String(item?.skill_id || '').trim();
      if (!sid) continue;
      const prev = bySkill.get(sid) || {
        skill_id: sid,
        total: 0,
        injected_count: 0,
        matched_only_count: 0,
        latest_timestamp: '',
        latest_hook: '',
      };
      prev.total += 1;
      if (item.injected) prev.injected_count += 1;
      else prev.matched_only_count += 1;
      const ts = String(item.timestamp || '');
      if (!prev.latest_timestamp || ts > prev.latest_timestamp) {
        prev.latest_timestamp = ts;
        prev.latest_hook = String(item.hook_event_name || '');
      }
      bySkill.set(sid, prev);
    }
    return Array.from(bySkill.values()).sort((a, b) => {
      if (a.injected_count !== b.injected_count) return b.injected_count - a.injected_count;
      if (a.total !== b.total) return b.total - a.total;
      return a.skill_id.localeCompare(b.skill_id);
    });
  }

  function renderKnowledgeEventSection(knowledgeEvents, options = {}) {
    if (!Array.isArray(knowledgeEvents) || !knowledgeEvents.length) return '';
    const entries = buildKnowledgeSkillEntries(knowledgeEvents);
    if (!entries.length) return '';
    const sectionSummary = String(options.summary || '').trim()
      || `Knowledge injection (injected: ${entries.filter((x) => x.injected).length} / matched-only: ${entries.filter((x) => !x.injected).length})`;
    const skillContentMap = getCurrentRunSkillContentMap();
    const injectedEntries = entries.filter((x) => x.injected);
    const matchedOnlyEntries = entries.filter((x) => !x.injected);
    const aggregateEntries = buildKnowledgeSkillAggregate(entries);

    const renderKnowledgeCard = (item) => {
      const scoreText = Number.isFinite(Number(item.score)) ? Number(item.score).toFixed(1) : '';
      const reasonPreview = item.reasons.slice(0, 2);
      const cardState = item.injected ? 'injected' : 'matched';
      const reasonInline = reasonPreview.length
        ? reasonPreview.map((x) => escapeHtml(String(x))).join('; ')
        : 'No explicit match reason';
      const skillRaw = item.skill_file_snapshot || skillContentMap[item.skill_id]?.content || '';
      const skillPath = item.skill_file_path || skillContentMap[item.skill_id]?.path || '';
      const injectedBlock = item.injected_prompt_block || '';
      const skillSourceType = item.skill_file_snapshot
        ? 'log_snapshot'
        : (skillRaw ? 'current_file' : '');
      const skillSourceLabel = skillSourceType === 'log_snapshot'
        ? 'Source: file snapshot in log'
        : (skillSourceType === 'current_file' ? 'Source: current file on disk' : '');
      const injectedBlockSourceLabel = injectedBlock ? 'Source: actual injected text in log' : '';
      const rawSkillBlock = skillRaw
        ? `
          <details class="transcript-card-details">
            <summary>${item.skill_file_snapshot ? 'View raw knowledge (file snapshot in log)' : 'View raw knowledge (current file on disk)'}</summary>
            ${skillSourceLabel ? `<div class="muted knowledge-source-note">${escapeHtml(skillSourceLabel)}</div>` : ''}
            ${skillPath ? `<pre>${escapeHtml(`skill_path: ${skillPath}`)}</pre>` : ''}
            <pre>${escapeHtml(skillRaw)}</pre>
          </details>
        `
        : '<div class="knowledge-raw-missing muted">Raw knowledge content was not loaded.</div>';
      const injectedBlockDetails = injectedBlock
        ? `
          <details class="transcript-card-details">
            <summary>View actual text injected into the prompt (log)</summary>
            ${injectedBlockSourceLabel ? `<div class="muted knowledge-source-note">${escapeHtml(injectedBlockSourceLabel)}</div>` : ''}
            <pre>${escapeHtml(injectedBlock)}</pre>
          </details>
        `
        : '';
      const matchDetails = {
        detail: item.detail_raw,
        event: item.event_raw,
      };
      return `
        <div class="knowledge-injection-card knowledge-injection-card-${escapeHtml(cardState)}">
          <div class="knowledge-injection-head">
            <div class="knowledge-injection-title-wrap">
              <span class="knowledge-state-chip knowledge-state-chip-${escapeHtml(cardState)}">${item.injected ? 'INJECTED' : 'MATCHED'}</span>
              <div class="knowledge-injection-title">${escapeHtml(item.skill_id)}</div>
            </div>
            <div class="transcript-card-badges">
              ${item.node_type ? `<span class="badge">${escapeHtml(item.node_type)}</span>` : ''}
              ${item.validation_status ? `<span class="badge">${escapeHtml(item.validation_status)}</span>` : ''}
              ${item.injection_mode ? `<span class="badge">${escapeHtml(item.injection_mode)}</span>` : ''}
              ${item.delta ? '<span class="badge">delta</span>' : '<span class="badge">full</span>'}
              ${item.mode ? `<span class="badge">${escapeHtml(item.mode)}</span>` : ''}
              ${scoreText ? `<span class="badge">score=${escapeHtml(scoreText)}</span>` : ''}
            </div>
          </div>
          <div class="knowledge-injection-meta">
            <div class="knowledge-meta-row"><span class="mk">knowledge_ref</span><span class="mv prewrap">${escapeHtml(item.knowledge_ref)}</span></div>
            <div class="knowledge-meta-row"><span class="mk">hook</span><span class="mv">${escapeHtml(item.hook_event_name || '-')}</span></div>
            ${skillSourceLabel ? `<div class="knowledge-meta-row"><span class="mk">knowledge_source</span><span class="mv">${escapeHtml(skillSourceLabel)}</span></div>` : ''}
            ${injectedBlockSourceLabel ? `<div class="knowledge-meta-row"><span class="mk">prompt_block_source</span><span class="mv">${escapeHtml(injectedBlockSourceLabel)}</span></div>` : ''}
            ${item.node_type ? `<div class="knowledge-meta-row"><span class="mk">node_type</span><span class="mv">${escapeHtml(item.node_type)}</span></div>` : ''}
            ${item.validation_status ? `<div class="knowledge-meta-row"><span class="mk">validation</span><span class="mv">${escapeHtml(item.validation_status)}</span></div>` : ''}
            ${item.constraint_mode ? `<div class="knowledge-meta-row"><span class="mk">constraint</span><span class="mv">${escapeHtml(item.constraint_mode)}${item.original_constraint_mode ? ` (orig:${escapeHtml(item.original_constraint_mode)})` : ''}</span></div>` : ''}
          </div>
          <div class="knowledge-reason-block">
            <div class="section-label">Injection rationale</div>
            <div class="knowledge-reason-inline">${reasonInline}</div>
          </div>
          <details class="transcript-card-details">
            <summary>Expand injection match details</summary>
            ${skillPath ? `<pre>${escapeHtml(`skill_path: ${skillPath}`)}</pre>` : ''}
            <pre>${escapeHtml(formatParsedObject(matchDetails))}</pre>
          </details>
          ${injectedBlockDetails}
          ${rawSkillBlock}
        </div>
      `;
    };

    const injectedCards = injectedEntries.length
      ? injectedEntries.map((item) => renderKnowledgeCard(item)).join('')
      : '<div class="muted">No knowledge was injected in this run.</div>';
    const matchedCards = matchedOnlyEntries.length
      ? matchedOnlyEntries.map((item) => renderKnowledgeCard(item)).join('')
      : '<div class="muted">No matched-only knowledge.</div>';
    const aggregateCards = aggregateEntries.length
      ? aggregateEntries.map((item) => `
          <div class="knowledge-injection-card">
            <div class="knowledge-injection-head">
              <div class="knowledge-injection-title-wrap">
                <div class="knowledge-injection-title">${escapeHtml(item.skill_id)}</div>
              </div>
              <div class="transcript-card-badges">
                <span class="badge">total=${escapeHtml(String(item.total))}</span>
                <span class="badge">injected=${escapeHtml(String(item.injected_count))}</span>
                <span class="badge">matched-only=${escapeHtml(String(item.matched_only_count))}</span>
              </div>
            </div>
            <div class="knowledge-injection-meta">
              <div class="knowledge-meta-row"><span class="mk">latest_hook</span><span class="mv">${escapeHtml(item.latest_hook || '-')}</span></div>
              <div class="knowledge-meta-row"><span class="mk">latest_timestamp</span><span class="mv">${escapeHtml(item.latest_timestamp || '-')}</span></div>
            </div>
          </div>
        `).join('')
      : '<div class="muted">No aggregate entries.</div>';

    return `
      <div class="transcript-card-section">
        <details class="knowledge-section-details">
          <summary>${escapeHtml(sectionSummary)}</summary>
          <details class="knowledge-secondary-details">
            <summary>Aggregate by skill (unique: ${aggregateEntries.length})</summary>
            <div class="knowledge-injection-list">${aggregateCards}</div>
          </details>
          <div class="knowledge-injection-list">${injectedCards}</div>
          ${matchedOnlyEntries.length ? `
            <details class="knowledge-secondary-details">
              <summary>View matched but not injected knowledge (${matchedOnlyEntries.length})</summary>
              <div class="knowledge-injection-list">${matchedCards}</div>
            </details>
          ` : ''}
        </details>
      </div>
    `;
  }

  function collectKnowledgeEventTerms(ev) {
    const terms = [];
    const pushTerms = (value) => {
      if (!value) return;
      if (Array.isArray(value)) {
        value.forEach((item) => pushTerms(item));
        return;
      }
      const s = String(value).trim();
      if (!s) return;
      terms.push(s.toLowerCase());
    };
    pushTerms(ev?.matched_rules);
    pushTerms(ev?.new_terms);
    pushTerms(ev?.injected_skill_ids);
    pushTerms(ev?.matched_skill_ids);
    return [...new Set(terms.filter((x) => x.length >= 3))].slice(0, 24);
  }

  function buildKnowledgeInjectionAssociations(lines, parsedLines) {
    const events = getCurrentKnowledgeInjectionEvents();
    const lineEventMap = new Map();
    const initialEvents = [];
    if (!Array.isArray(events) || !events.length) {
      return { lineEventMap, initialEvents };
    }

    const toolResultEntries = [];
    for (let i = 0; i < lines.length; i += 1) {
      const parsedLine = parsedLines[i];
      const obj = parsedLine && parsedLine.parsed && typeof parsedLine.parsed === 'object' ? parsedLine.parsed : null;
      if (!obj) continue;
      const p = extractTranscriptPriorityFields(obj);
      if (!p.hasToolResult) continue;
      const haystack = `${String(lines[i] || '')}\n${String(p.content || '')}`.toLowerCase();
      toolResultEntries.push({ lineIndex: i, haystack });
    }

    const pickTargetLine = (ev) => {
      if (!toolResultEntries.length) return -1;
      const terms = collectKnowledgeEventTerms(ev);
      let best = toolResultEntries[toolResultEntries.length - 1];
      let bestScore = 0;
      if (terms.length) {
        for (const entry of toolResultEntries) {
          let score = 0;
          for (const term of terms) {
            if (entry.haystack.includes(term)) score += 1;
          }
          if (score > bestScore) {
            bestScore = score;
            best = entry;
          }
        }
      }
      return best?.lineIndex ?? -1;
    };

    let consumedInitialUserPrompt = false;
    for (const ev of events) {
      const hook = String(ev?.hook_event_name || '');
      if (hook === 'UserPromptSubmit') {
        if (!consumedInitialUserPrompt) {
          initialEvents.push(ev);
          consumedInitialUserPrompt = true;
        }
        continue;
      }
      const target = pickTargetLine(ev);
      if (target < 0) continue;
      const arr = lineEventMap.get(target) || [];
      arr.push(ev);
      lineEventMap.set(target, arr);
    }
    return { lineEventMap, initialEvents };
  }

  function buildTranscriptInitialQueryCard(initialKnowledgeEvents = []) {
    const task = state.selectedTaskSnapshot;
    if (!task) return '';
    let query = '';
    let source = '';
    let sink = '';
    let appName = '';
    let mode = '';
    let sourceId = '';

    if (task.kind !== 'eval') return '';
    const selectedRunKey = normalizePathForCompare(state.selectedEvalRunDir);
    const selectedRunMeta = selectedRunKey ? (state.evalRunMetaByDir[selectedRunKey] || {}) : {};
    const selectedEntry = selectedEvalRunEntry() || {};
    query = String(selectedRunMeta.query || '').trim();
    source = String(selectedRunMeta.source || '').trim();
    sink = normalizeSinkTypesDisplay(selectedRunMeta.sink_types || '');
    appName = String(selectedRunMeta.app_name || '').trim();
    mode = String(selectedEntry.mode || selectedRunMeta.agent_mode || '').trim();
    sourceId = String(selectedEntry.source_id || '').trim();

    if (!query && !source && !sink && !appName && !sourceId) return '';
    return renderSyntheticTranscriptCard({
      title: 'Initial Query',
      badges: ['flowark-studio', 'query-context'],
      metaRows: [
        { k: 'app', v: appName || '-' },
        { k: 'mode', v: mode || '-' },
        { k: 'source_id', v: sourceId || '-' },
        { k: 'sink_types', v: sink || '-' },
        { k: 'source', v: source || '-' },
      ],
      content: query || '(empty query)',
      detailsHtml: renderKnowledgeEventSection(initialKnowledgeEvents),
      cardTypeClass: 'card-type-user',
    });
  }
