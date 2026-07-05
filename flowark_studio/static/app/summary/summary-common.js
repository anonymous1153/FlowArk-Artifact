  function formatSummaryValue(value) {
    if (value == null) return '';
    if (Array.isArray(value)) return value.join(', ');
    if (typeof value === 'boolean') return value ? 'on' : 'off';
    if (typeof value === 'object') {
      try { return JSON.stringify(value); } catch { return String(value); }
    }
    return String(value);
  }

  function renderSummaryKvTable(rows) {
    const validRows = (rows || []).filter((r) => r && String(r.v ?? '').trim() !== '');
    if (!validRows.length) {
      return '<div class="summary-empty muted">None</div>';
    }
    return `
      <div class="summary-kv-table">
        ${validRows.map((r) => `
          <div class="summary-kv-row">
            <div class="k">${escapeHtml(String(r.k))}</div>
            <div class="v">${escapeHtml(formatSummaryValue(r.v))}</div>
          </div>
        `).join('')}
      </div>
    `;
  }

  function parseSummaryTimeMs(value) {
    const text = String(value || '').trim();
    if (!text) return null;
    const ms = Date.parse(text);
    return Number.isFinite(ms) ? ms : null;
  }

  function formatDurationCompact(value) {
    const num = Number(value);
    if (!Number.isFinite(num) || num < 0) return '';
    if (num < 60) {
      if (num >= 10) return `${num.toFixed(1).replace(/\.0$/, '')}s`;
      return `${num.toFixed(2).replace(/0+$/, '').replace(/\.$/, '')}s`;
    }
    const totalSeconds = Math.round(num);
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    const parts = [];
    if (hours) parts.push(`${hours}h`);
    if (minutes || hours) parts.push(`${minutes}m`);
    parts.push(`${seconds}s`);
    return parts.join(' ');
  }

  function formatRatioPercent(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) return '';
    return `${(num * 100).toFixed(1)}%`;
  }

  function computeSummaryDurationSeconds(startValue, finishValue, { ongoing = false } = {}) {
    const startMs = parseSummaryTimeMs(startValue);
    if (startMs == null) return null;
    const finishMs = parseSummaryTimeMs(finishValue);
    const endMs = finishMs ?? (ongoing ? Date.now() : null);
    if (endMs == null || endMs < startMs) return null;
    return (endMs - startMs) / 1000;
  }

  function getSummaryElapsedLabel({
    startedAt = '',
    finishedAt = '',
    wallTimeSeconds = null,
    ongoing = false,
  } = {}) {
    const hasExplicitWallTime = wallTimeSeconds !== null && wallTimeSeconds !== undefined && String(wallTimeSeconds).trim() !== '';
    if (hasExplicitWallTime) {
      const explicit = Number(wallTimeSeconds);
      if (Number.isFinite(explicit) && explicit >= 0) {
        return formatDurationCompact(explicit);
      }
    }
    return formatDurationCompact(computeSummaryDurationSeconds(startedAt, finishedAt, { ongoing }));
  }

  function buildSummaryTimelineRows({
    createdAt = '',
    startedAt = '',
    finishedAt = '',
    wallTimeSeconds = null,
    ongoing = false,
    includeCreated = true,
    includeStartedFinished = true,
  } = {}) {
    const rows = [];
    if (includeCreated) rows.push({ k: 'created', v: createdAt || '' });
    if (includeStartedFinished) {
      rows.push({ k: 'started', v: startedAt || '' });
      rows.push({ k: 'finished', v: finishedAt || '' });
    }
    rows.push({
      k: 'elapsed',
      v: getSummaryElapsedLabel({
        startedAt,
        finishedAt,
        wallTimeSeconds,
        ongoing,
      }),
    });
    return rows;
  }

  const PERFORMANCE_SECTION_SPECS = {
    overview: [
      { label: 'source', key: 'source', formatter: (value) => String(value || ''), comparable: false },
      { label: 'wall_time', key: 'wall_time_seconds', formatter: formatSeconds, comparable: true },
    ],
    analysis: [
      { label: 'turns', key: 'react_turns', formatter: formatNumberCompact, comparable: true },
      { label: 'input_tokens', key: 'input_tokens', formatter: formatNumberCompact, comparable: true },
      { label: 'output_tokens', key: 'output_tokens', formatter: formatNumberCompact, comparable: true },
      { label: 'cache_read_tokens', key: 'cache_read_input_tokens', formatter: formatNumberCompact, comparable: true },
      { label: 'cache_creation_tokens', key: 'cache_creation_input_tokens', formatter: formatNumberCompact, comparable: true },
      { label: 'tool_use_blocks', key: 'tool_use_block_count', formatter: formatNumberCompact, comparable: true },
      { label: 'duration', key: 'duration_ms', formatter: formatMsAsSeconds, comparable: true },
      { label: 'cost_usd', key: 'total_cost_usd', formatter: formatUsd, comparable: true },
    ],
    end_to_end: [
      { label: 'turns', key: 'react_turns', formatter: formatNumberCompact, comparable: true },
      { label: 'input_tokens', key: 'input_tokens', formatter: formatNumberCompact, comparable: true },
      { label: 'output_tokens', key: 'output_tokens', formatter: formatNumberCompact, comparable: true },
      { label: 'cache_read_tokens', key: 'cache_read_input_tokens', formatter: formatNumberCompact, comparable: true },
      { label: 'cache_creation_tokens', key: 'cache_creation_input_tokens', formatter: formatNumberCompact, comparable: true },
      { label: 'mem0_cost', key: 'mem0_total_cost_usd', formatter: formatUsd, comparable: true },
      { label: 'total_with_mem0_cost', key: 'total_with_mem0_cost_usd', formatter: formatUsd, comparable: true },
      { label: 'mem0_tokens', key: 'mem0_total_tokens', formatter: formatNumberCompact, comparable: true },
      { label: 'mem0_embedding_tokens', key: 'mem0_embedding_total_tokens', formatter: formatNumberCompact, comparable: true },
      { label: 'tool_use_blocks', key: 'tool_use_block_count', formatter: formatNumberCompact, comparable: true },
      { label: 'duration', key: 'duration_ms', formatter: formatMsAsSeconds, comparable: true },
      { label: 'cost_usd', key: 'total_cost_usd', formatter: formatUsd, comparable: true },
    ],
  };

  function getPerformanceMetricSpecs(sectionName) {
    return PERFORMANCE_SECTION_SPECS[sectionName] || [];
  }

  function getPerformanceMetricRawValue(perf, sectionName, metricKey) {
    if (!perf || typeof perf !== 'object') return null;
    if (sectionName === 'overview') {
      return perf[metricKey];
    }
    const section = perf[sectionName];
    if (!section || typeof section !== 'object') return null;
    return section[metricKey];
  }

  function formatPerformanceMetricValue(spec, value) {
    if (value == null || value === '') return '';
    return typeof spec?.formatter === 'function' ? spec.formatter(value) : formatSummaryValue(value);
  }

  function buildPerformanceRows(sectionName, perf) {
    const specs = getPerformanceMetricSpecs(sectionName);
    return specs
      .map((spec) => ({
        k: spec.label,
        v: formatPerformanceMetricValue(spec, getPerformanceMetricRawValue(perf, sectionName, spec.key)),
      }))
      .filter((row) => String(row.v || '').trim() !== '');
  }

  function renderSummaryNestedCard(title, bodyHtml) {
    return `
      <section class="summary-nested-card">
        <div class="summary-nested-card-title">${escapeHtml(String(title || ''))}</div>
        <div class="summary-nested-card-body">${bodyHtml}</div>
      </section>
    `;
  }

  function renderPerformanceSection(perf, { loading = false } = {}) {
    if (loading) {
      return '<div class="summary-empty muted">Loading performance data...</div>';
    }
    if (!perf || typeof perf !== 'object' || (!perf.analysis && !perf.end_to_end && perf.wall_time_seconds == null)) {
      return '<div class="summary-empty muted">No performance data</div>';
    }
    const overviewRows = buildPerformanceRows('overview', perf);
    const analysisRows = buildPerformanceRows('analysis', perf);
    const endToEndRows = buildPerformanceRows('end_to_end', perf);
    const blocks = [];
    if (overviewRows.length) {
      blocks.push(renderSummaryNestedCard('overview', renderSummaryKvTable(overviewRows)));
    }
    if (analysisRows.length) {
      blocks.push(renderSummaryNestedCard('analysis', renderSummaryKvTable(analysisRows)));
    }
    if (endToEndRows.length) {
      blocks.push(renderSummaryNestedCard('end_to_end', renderSummaryKvTable(endToEndRows)));
    }
    return `<div class="summary-nested-grid">${blocks.join('')}</div>`;
  }

  function normalizeSummaryModes(value) {
    const rawItems = Array.isArray(value) ? value : String(value || '').split(',');
    const out = [];
    for (const item of rawItems) {
      let mode = String(item || '').trim().toLowerCase();
      if (!mode) continue;
      if (mode === 'native') mode = 'naive';
      if (!out.includes(mode)) out.push(mode);
    }
    return out;
  }

  function inferSummaryExperimentPreset(task) {
    const p = task?.params || {};
    const explicit = String(p.experiment_preset || '').trim();
    if (explicit) return normalizePublicSummaryExperimentPreset(explicit);
    const mode = normalizeSummaryModes(p.modes).includes('flowark') ? 'flowark' : 'naive';
    if (mode === 'naive') return 'naive';
    const adapter = String(p.agent_adapter || 'opencode').trim().toLowerCase().replace('-', '_');
    const knowledge = String(p.knowledge_mode || 'warm').trim().toLowerCase();
    const cycleRaw = p.auto_knowledge_cycle;
    const cycle = cycleRaw == null
      ? true
      : !['0', 'false', 'off', 'no'].includes(String(cycleRaw).trim().toLowerCase());
    const runtime = String(p.runtime_injection_mode || 'context_aware').trim().toLowerCase();
    const distill = String(p.knowledge_distillation_mode || 'with_selection_rules').trim().toLowerCase();
    const packaging = String(p.knowledge_packaging_mode || 'dsl_rule').trim().toLowerCase();
    const validate = String(p.auto_knowledge_validate_mode || 'static').trim().toLowerCase();
    const digest = String(p.knowledge_reuse_digest_mode || 'live_corridor_v2').trim().toLowerCase();
    if (adapter !== 'opencode' || knowledge !== 'warm') return 'flowark_full';
    if (distill === 'with_selection_rules' && packaging === 'dsl_rule' && runtime === 'context_aware' && validate === 'static' && digest === 'live_corridor_v2') return 'flowark_full';
    if (distill === 'generic' && packaging === 'dsl_rule' && runtime === 'context_aware' && validate === 'off' && digest === 'off') return 'm1_generic';
    if (distill === 'with_selection_rules' && packaging === 'embedding' && runtime === 'context_aware' && validate === 'off' && digest === 'live_corridor_v2') return 'm2_embedding';
    if (distill === 'with_selection_rules' && packaging === 'dsl_rule' && runtime === 'start_only' && validate === 'static' && digest === 'live_corridor_v2') return 'm3_start_only';
    if (distill === 'with_selection_rules' && packaging === 'analysis_log_rag' && runtime === 'context_aware' && !cycle && validate === 'off' && digest === 'off') return 'analysis_log_rag';
    if (distill === 'with_selection_rules' && packaging === 'analysis_log_rag_initial' && runtime === 'start_only' && !cycle && validate === 'off' && digest === 'off') return 'analysis_log_rag';
    return 'flowark_full';
  }

  function normalizePublicSummaryExperimentPreset(value) {
    const normalized = String(value || '').trim().toLowerCase();
    const allowed = {
      naive: true,
      flowark_full: true,
      m1_generic: true,
      m2_embedding: true,
      m3_start_only: true,
      mem0_enabled_opencode: true,
      analysis_log_rag: true,
    };
    if (allowed[normalized]) return normalized;
    if (normalized === 'analysis_log_rag_initial') return 'analysis_log_rag';
    return 'flowark_full';
  }

  function renderModeChips(task) {
    const p = task?.params || {};
    const chips = [];
    const pushChip = (k, v) => {
      if (v == null || v === '') return;
      chips.push({ k, v: formatSummaryValue(v) });
    };
    pushChip('kind', task?.kind || '');
    pushChip('preset', inferSummaryExperimentPreset(task));
    pushChip('dataset', p.dataset_preset);
    pushChip('modes', Array.isArray(p.modes) ? p.modes.join('+') : p.modes);
    pushChip('app_names', p.app_names);
    if (!chips.length) return '<div class="summary-empty muted">No preset parameters</div>';
    return `<div class="summary-chip-list">${chips.map((c) => `<span class="summary-chip"><span class="chip-k">${escapeHtml(c.k)}</span><span class="chip-sep">=</span><span class="chip-v">${escapeHtml(c.v)}</span></span>`).join('')}</div>`;
  }

  function renderSummarySection(title, contentHtml, { open = false } = {}) {
    return `
      <details class="summary-section"${open ? ' open' : ''}>
        <summary>${escapeHtml(title)}</summary>
        <div class="summary-section-body">${contentHtml}</div>
      </details>
    `;
  }

  function renderSummaryCompactBar(cells) {
    const safeCells = (cells || []).filter((cell) => cell && String(cell.v ?? '').trim() !== '');
    if (!safeCells.length) return '<div class="summary-empty muted">None</div>';
    return `
      <div class="summary-collapsed-bar">
        ${safeCells.map((cell, idx) => `
          <div class="summary-collapsed-cell${idx === safeCells.length - 1 ? ' summary-collapsed-query' : ''}">
            <span class="k">${escapeHtml(String(cell.k || '-'))}</span>
            <span class="v summary-collapsed-truncate">${escapeHtml(formatSummaryValue(cell.v) || '-')}</span>
          </div>
        `).join('')}
      </div>
    `;
  }

  function renderSummaryDetailCard({ title, statusClass, statusText, statusTitle, timelineRows, sectionsHtml, compactHtml }) {
    const expanded = !!state.taskSummaryExpanded;
    const statusTitleAttr = statusTitle ? ` title="${escapeHtml(statusTitle)}"` : '';
    if (!expanded) {
      return `
        <section class="summary-detail-card">
          <div class="summary-detail-card-head">
            <div class="summary-detail-card-title">${escapeHtml(title)}</div>
            ${statusText ? `<span class="summary-status-pill ${escapeHtml(statusClass || '')}"${statusTitleAttr}>${escapeHtml(statusText)}</span>` : ''}
          </div>
          ${compactHtml || '<div class="summary-empty muted">None</div>'}
        </section>
      `;
    }
    const rows = Array.isArray(timelineRows) ? timelineRows : [];
    return `
      <section class="summary-detail-card">
        <div class="summary-detail-card-head">
          <div class="summary-detail-card-title">${escapeHtml(title)}</div>
          ${statusText ? `<span class="summary-status-pill ${escapeHtml(statusClass || '')}"${statusTitleAttr}>${escapeHtml(statusText)}</span>` : ''}
        </div>
        ${rows.length ? `
          <div class="summary-top-times summary-top-times-inline">
            ${rows.map((r) => `
              <div class="summary-time-row">
                <span class="summary-time-k">${escapeHtml(String(r.k || ''))}</span>
                <span class="summary-time-v">${escapeHtml(formatSummaryValue(r.v) || '-')}</span>
              </div>
            `).join('')}
          </div>
        ` : ''}
        ${(sectionsHtml || []).join('')}
      </section>
    `;
  }
