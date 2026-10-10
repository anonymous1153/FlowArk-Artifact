  function renderDetailBody({
    statusClass,
    statusText,
    statusTitle,
    timelineRows,
    compactCells,
    sectionsHtml,
  }, expanded = state.taskSummaryExpanded) {
    if (!expanded) {
      return renderSummaryCompactBar(compactCells || []);
    }
    const rows = Array.isArray(timelineRows) ? timelineRows : [];
    return `
      <div class="summary-top-row">
        <div class="summary-top-status">
          <span class="summary-top-label">Status</span>
          <span class="summary-status-pill ${escapeHtml(String(statusClass || '').toLowerCase())}"${statusTitle ? ` title="${escapeHtml(statusTitle)}"` : ''}>${escapeHtml(String(statusText || 'unknown'))}</span>
        </div>
        ${rows.length ? `
          <div class="summary-top-times">
            ${rows.map((r) => `
              <div class="summary-time-row">
                <span class="summary-time-k">${escapeHtml(String(r.k || ''))}</span>
                <span class="summary-time-v">${escapeHtml(formatSummaryValue(r.v) || '-')}</span>
              </div>
            `).join('')}
          </div>
        ` : '<div class="summary-top-times"><div class="summary-time-row"><span class="summary-time-k">-</span><span class="summary-time-v">-</span></div></div>'}
      </div>
      ${(sectionsHtml || []).join('')}
    `;
  }

  function renderTaskSummary(task, { immediate = false } = {}) {
    if (!task) return;
    state.pendingTaskSummaryTask = task;
    if (immediate || typeof window === 'undefined' || typeof window.requestAnimationFrame !== 'function') {
      state.taskSummaryRenderScheduled = false;
      const pending = state.pendingTaskSummaryTask;
      state.pendingTaskSummaryTask = null;
      if (pending) renderTaskSummaryNow(pending);
      return;
    }
    if (state.taskSummaryRenderScheduled) return;
    state.taskSummaryRenderScheduled = true;
    window.requestAnimationFrame(() => {
      state.taskSummaryRenderScheduled = false;
      const pending = state.pendingTaskSummaryTask;
      state.pendingTaskSummaryTask = null;
      if (pending) renderTaskSummaryNow(pending);
    });
  }

  function isTaskTimingOngoing(task) {
    const status = String(task?.status || '').trim().toLowerCase();
    return ['running', 'queued', 'starting', 'finishing', 'pausing'].includes(status);
  }

  function isEvalRunTimingOngoing(run) {
    return getEvalRunExecState(run) === 'running';
  }

  function summarizeKnowledgeInjectionEvents(events) {
    const rows = Array.isArray(events) ? events : [];
    let delivered = 0;
    let skipped = 0;
    let failed = 0;
    for (const row of rows) {
      const status = String(row?.delivery_status || '').trim().toLowerCase();
      if (status === 'skipped') {
        skipped += 1;
      } else if (status === 'failed') {
        failed += 1;
      } else {
        delivered += 1;
      }
    }
    return { delivered, skipped, failed, total: rows.length };
  }

  function buildTaskSummaryBaseContext(task) {
    const p = task.params || {};
    const taskStatusInfo = getTaskStatusInfo(task);
    const taskElapsed = getSummaryElapsedLabel({
      startedAt: task.started_at || '',
      finishedAt: task.finished_at || '',
      ongoing: isTaskTimingOngoing(task),
    });
    return {
      p,
      taskStatus: taskStatusInfo.displayText || 'unknown',
      taskStatusClass: taskStatusInfo.displayClass || 'unknown',
      taskStatusTitle: taskStatusInfo.displayTitle || '',
      taskTimelineRows: buildSummaryTimelineRows({
        createdAt: task.created_at || '',
        startedAt: task.started_at || '',
        finishedAt: task.finished_at || '',
        ongoing: isTaskTimingOngoing(task),
      }),
      taskElapsed,
      taskBaseRows: [
        { k: 'task_id', v: task.task_id },
        { k: 'kind', v: task.kind },
        { k: 'tags', v: getTaskTags(task).join(', ') || '' },
        { k: 'pid', v: task.pid || '' },
        { k: 'return_code', v: task.return_code ?? '' },
        { k: 'historical', v: !!task?.metadata?.historical },
        { k: 'draft', v: !!task?.metadata?.draft },
      ],
      modeTextEval: [
        formatTaskExperimentPreset(inferTaskExperimentPreset(task.kind, p)),
        normalizeEvalModes(p.modes).join('+') || (p.modes || ''),
      ].filter(Boolean).join(' · '),
    };
  }

  function buildEvalTaskPanelConfig(task, context) {
    const { p, taskStatus, taskStatusClass, taskStatusTitle, taskTimelineRows, taskElapsed, taskBaseRows, modeTextEval } = context;
    const evalInputRows = [
      { k: 'dataset_preset', v: p.dataset_preset || '' },
      { k: 'input_path', v: p.input_path || p.input || '' },
      { k: 'app_names', v: p.app_names || '' },
      { k: 'classification_filter', v: p.classification_filter || '' },
      { k: 'max_sources', v: p.max_sources ?? '' },
      { k: 'max_cases', v: p.max_cases ?? '' },
      { k: 'max_apps', v: p.max_apps ?? '' },
    ];
    const selectedRunKeyRaw = normalizePathForCompare(state.selectedEvalRunDir);
    const selectedRunEntryRaw = findEvalRunEntry(selectedRunKeyRaw);
    const selectedRunMatchesTask = !!selectedRunEntryRaw && evalRunBelongsToTask(selectedRunEntryRaw, task);
    const selectedRunEntry = selectedRunMatchesTask ? selectedRunEntryRaw : {};
    const selectedRunKey = selectedRunMatchesTask ? selectedRunKeyRaw : '';
    const selectedRunMeta = selectedRunKey ? (state.evalRunMetaByDir[selectedRunKey] || {}) : {};
    const selectedRunHealthIssues = Array.isArray(selectedRunEntry.health_issues)
      ? selectedRunEntry.health_issues
      : (Array.isArray(selectedRunMeta.health_issues) ? selectedRunMeta.health_issues : []);
    const selectedRunDir = resolveEvalRunArtifactDir(selectedRunEntry);
    const selectedRunLabel = selectedRunDir
      ? basenameOfPath(selectedRunDir)
      : (selectedRunEntry.source_id || selectedRunKey || '');

    return {
      modeText: modeTextEval,
      taskBody: {
        statusClass: taskStatusClass,
        statusText: taskStatus,
        statusTitle: taskStatusTitle,
        timelineRows: taskTimelineRows,
        compactCells: [
          { k: 'status', v: taskStatus },
          { k: 'mode', v: modeTextEval || '-' },
          { k: 'elapsed', v: taskElapsed || '' },
          { k: 'input', v: p.input_path || p.input || '-' },
          { k: 'selected_run', v: selectedRunLabel || '-' },
        ],
        sectionsHtml: [
          renderSummarySection('Basics', renderSummaryKvTable(taskBaseRows), { open: true }),
          renderSummarySection('Eval input', renderSummaryKvTable(evalInputRows), { open: true }),
          renderSummarySection('Preset settings', renderModeChips(task), { open: true }),
        ],
      },
      selectedRunKey,
      selectedRunEntry,
      selectedRunMeta,
      selectedRunHealthIssues,
      selectedRunDir,
      selectedRunLabel,
    };
  }

  function renderHealthIssueValue(value) {
    if (Array.isArray(value)) {
      if (!value.length) return '<span class="muted">[]</span>';
      return `
        <div class="health-issue-chip-row">
          ${value.map((item) => `<span class="health-issue-chip">${escapeHtml(formatSummaryValue(item))}</span>`).join('')}
        </div>
      `;
    }
    if (value && typeof value === 'object') {
      try {
        return `<code class="health-issue-code">${escapeHtml(JSON.stringify(value))}</code>`;
      } catch {
        return `<span>${escapeHtml(String(value))}</span>`;
      }
    }
    return `<span>${escapeHtml(formatSummaryValue(value))}</span>`;
  }

  function renderHealthIssueContext(context) {
    if (!context || typeof context !== 'object' || Array.isArray(context)) return '';
    const rows = Object.entries(context).filter(([key]) => String(key || '').trim());
    if (!rows.length) return '';
    return `
      <div class="health-issue-context">
        ${rows.map(([key, value]) => `
          <div class="health-issue-context-row">
            <div class="health-issue-context-key">${escapeHtml(String(key))}</div>
            <div class="health-issue-context-value">${renderHealthIssueValue(value)}</div>
          </div>
        `).join('')}
      </div>
    `;
  }

  function renderHealthIssues(issues) {
    const validIssues = (Array.isArray(issues) ? issues : []).filter((issue) => issue && typeof issue === 'object');
    if (!validIssues.length) return '<div class="summary-empty muted">None</div>';
    return `
      <div class="health-issue-list">
        ${validIssues.map((issue, index) => {
          const severity = String(issue.severity || 'issue').trim().toLowerCase() || 'issue';
          const code = String(issue.code || 'unknown').trim() || 'unknown';
          const message = String(issue.message || '').trim();
          const artifactPath = String(issue.artifact_path || '').trim();
          return `
            <div class="health-issue-card ${escapeHtml(severity)}">
              <div class="health-issue-head">
                <span class="health-issue-index">#${index + 1}</span>
                <span class="health-issue-severity ${escapeHtml(severity)}">${escapeHtml(severity)}</span>
                <code class="health-issue-code-name">${escapeHtml(code)}</code>
              </div>
              ${message ? `<div class="health-issue-message">${escapeHtml(message)}</div>` : ''}
              ${artifactPath ? `
                <div class="health-issue-artifact">
                  <span class="health-issue-label">artifact</span>
                  <code>${escapeHtml(artifactPath)}</code>
                </div>
              ` : ''}
              ${renderHealthIssueContext(issue.context)}
            </div>
          `;
        }).join('')}
      </div>
    `;
  }

  function buildEvalRunPanelConfig(task, evalTaskConfig) {
    const {
      selectedRunEntry,
      selectedRunMeta,
      selectedRunHealthIssues = [],
      selectedRunDir,
      selectedRunLabel,
    } = evalTaskConfig;
    const runStatus = String(selectedRunEntry.status || task.status || 'unknown');
    const runQuery = selectedRunMeta.query || '';
    const runSource = selectedRunMeta.source || '';
    const runSink = normalizeSinkTypesDisplay(selectedRunMeta.sink_types || '');
    const runLoading = !!state.evalRunMetaLoadingByDir[evalTaskConfig.selectedRunKey]
      || isEvalRunContentLoading(task.task_id, evalTaskConfig.selectedRunKey);
    const subRunModeText = selectedRunEntry.mode || selectedRunMeta.agent_mode || '';
    const runElapsed = getSummaryElapsedLabel({
      startedAt: selectedRunEntry.started_at || selectedRunMeta.started_at || '',
      finishedAt: selectedRunEntry.finished_at || selectedRunMeta.finished_at || '',
      wallTimeSeconds: selectedRunEntry.wall_time_seconds,
      ongoing: isEvalRunTimingOngoing(selectedRunEntry),
    });
    const knowledgeRunKey = getKnowledgeRunKeyForEvalEntry(selectedRunEntry);
    const runKnowledgeEventStats = summarizeKnowledgeInjectionEvents(
      knowledgeRunKey
        ? (state.knowledgeInjectionByRunDir[normalizePathForCompare(knowledgeRunKey)] || [])
        : [],
    );
    const runBaseRows = [
      { k: 'run_dir', v: selectedRunDir || '' },
      { k: 'repeat_dir', v: selectedRunEntry.repeat_dir || '' },
      { k: 'task_index', v: selectedRunEntry.task_index ?? '' },
      { k: 'task_total', v: selectedRunEntry.task_total ?? '' },
      { k: 'mode', v: subRunModeText },
      { k: 'repeat_idx', v: selectedRunEntry.repeat_idx ?? '' },
      { k: 'source_id', v: selectedRunEntry.source_id || '' },
      { k: 'flow_id', v: selectedRunEntry.flow_id || '' },
      { k: 'started_at', v: selectedRunEntry.started_at || selectedRunMeta.started_at || '' },
      { k: 'finished_at', v: selectedRunEntry.finished_at || selectedRunMeta.finished_at || '' },
      { k: 'wall_time', v: runElapsed || '' },
      { k: 'knowledge_events', v: runKnowledgeEventStats.delivered },
      { k: 'knowledge_skipped_events', v: runKnowledgeEventStats.skipped },
      { k: 'knowledge_failed_events', v: runKnowledgeEventStats.failed },
    ];
    const runInputRows = [
      { k: 'query', v: runQuery },
      { k: 'source', v: runSource },
      { k: 'sink', v: runSink },
      { k: 'app_name', v: selectedRunMeta.app_name || '' },
    ];
    return {
      modeText: subRunModeText,
      runBody: {
        statusClass: runStatus.toLowerCase(),
        statusText: runStatus,
        timelineRows: buildSummaryTimelineRows({
          startedAt: selectedRunEntry.started_at || selectedRunMeta.started_at || '',
          finishedAt: selectedRunEntry.finished_at || selectedRunMeta.finished_at || '',
          wallTimeSeconds: selectedRunEntry.wall_time_seconds,
          ongoing: isEvalRunTimingOngoing(selectedRunEntry),
          includeCreated: false,
        }),
        compactCells: [
          { k: 'run', v: selectedRunLabel || selectedRunDir },
          { k: 'mode', v: subRunModeText || '-' },
          { k: 'elapsed', v: runElapsed || '' },
          { k: 'source_id', v: selectedRunEntry.source_id || '-' },
          { k: 'query', v: runQuery || '-' },
        ],
        sectionsHtml: [
          runLoading ? '<div class="summary-empty muted">Loading query/source/sink metadata for this run...</div>' : '',
          renderSummarySection('Basics', renderSummaryKvTable(runBaseRows), { open: true }),
          renderSummarySection('Analysis input (query/source/sink)', renderSummaryKvTable(runInputRows), { open: true }),
        ].filter(Boolean),
      },
    };
  }

  function renderTaskSummaryNow(task) {
    if (!task) return;
    try {
      const taskExpanded = !!state.taskSummaryExpanded;
      const runExpanded = !!state.runSummaryExpanded;
      if (els.toggleTaskSummaryBtn) {
        els.toggleTaskSummaryBtn.textContent = taskExpanded ? 'Collapse' : 'Expand';
        els.toggleTaskSummaryBtn.title = taskExpanded ? 'Collapse details' : 'Expand details';
      }
      if (els.toggleRunSummaryBtn) {
        els.toggleRunSummaryBtn.textContent = runExpanded ? 'Collapse' : 'Expand';
        els.toggleRunSummaryBtn.title = runExpanded ? 'Collapse details' : 'Expand details';
      }
      if (task.kind !== 'eval') {
        if (els.taskSummaryCard) els.taskSummaryCard.classList.remove('hidden');
        if (els.runSummaryCard) els.runSummaryCard.classList.add('hidden');
        if (els.taskSummaryTitle) els.taskSummaryTitle.textContent = 'Task details';
        if (els.taskSummaryMode) {
          els.taskSummaryMode.textContent = '';
          els.taskSummaryMode.classList.add('hidden');
        }
        if (els.runSummaryMode) {
          els.runSummaryMode.textContent = '';
          els.runSummaryMode.classList.add('hidden');
        }
        if (els.taskSummary) {
          els.taskSummary.innerHTML = '<div class="summary-empty muted">The public Studio only shows eval tasks.</div>';
        }
        if (els.runSummary) {
          els.runSummary.innerHTML = '';
        }
        refreshPaneFileButtons();
        refreshTaskLogDirButtons();
        return;
      }
      const context = buildTaskSummaryBaseContext(task);

      const evalConfig = buildEvalTaskPanelConfig(task, context);
      if (els.taskSummaryCard) els.taskSummaryCard.classList.remove('hidden');
      if (els.runSummaryCard) els.runSummaryCard.classList.remove('hidden');
      if (els.taskSummaryTitle) els.taskSummaryTitle.textContent = 'Eval details';
      if (els.runSummaryTitle) els.runSummaryTitle.textContent = 'Sub-run details';
      if (els.taskSummaryMode) {
        els.taskSummaryMode.textContent = evalConfig.modeText || '';
        els.taskSummaryMode.classList.toggle('hidden', taskExpanded || !evalConfig.modeText);
      }
      if (els.taskSummary) {
        els.taskSummary.innerHTML = renderDetailBody(evalConfig.taskBody, taskExpanded);
      }

      if (!evalConfig.selectedRunKey) {
        if (els.runSummary) {
          els.runSummary.innerHTML = isEvalRunsLoading(task.task_id)
            ? '<div class="summary-empty muted">Loading eval runs...</div>'
            : '<div class="summary-empty muted">Select a sub-run from Eval Runs.</div>';
        }
        if (els.runSummaryMode) {
          els.runSummaryMode.classList.add('hidden');
        }
        refreshPaneFileButtons();
        refreshTaskLogDirButtons();
        return;
      }

      const evalRunConfig = buildEvalRunPanelConfig(task, evalConfig);
      if (els.runSummaryMode) {
        els.runSummaryMode.textContent = evalRunConfig.modeText;
        els.runSummaryMode.classList.toggle('hidden', runExpanded || !evalRunConfig.modeText);
      }
      if (els.runSummary) {
        els.runSummary.innerHTML = renderDetailBody(evalRunConfig.runBody, runExpanded);
      }

      refreshPaneFileButtons();
      refreshTaskLogDirButtons();
    } catch (err) {
      console.error('renderTaskSummary error', err);
      if (els.runSummary) els.runSummary.innerHTML = `<div class="summary-empty muted">Failed to render details: ${escapeHtml(String(err?.message || err))}</div>`;
      if (els.taskSummary) els.taskSummary.innerHTML = '';
      if (els.taskSummaryCard) els.taskSummaryCard.classList.add('hidden');
      if (els.runSummaryCard) els.runSummaryCard.classList.remove('hidden');
    }
  }
