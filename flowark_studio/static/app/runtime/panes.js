  function resetCodePanes() {
    state.logBuffers = { transcript: '', cost: '', artifact: '' };
    state.paneRenderAutoScroll = {};
    state.panePaths = Object.fromEntries(CODE_PANES.map((p) => [p, null]));
    state.selectedArtifactPath = null;
    state.costSummaryViewMode = 'card';
    state.costSummaryData = null;
    state.costSummaryPath = null;
    for (const pane of CODE_PANES) {
      setCodePaneText(pane, '', { autoScroll: false });
    }
    els.artifactPath.textContent = '';
    els.artifactList.innerHTML = '';
    renderCostSummaryPresentation();
    refreshPaneFileButtons();
  }

  function paneDom(pane) {
    return codePaneEls[pane];
  }

  function setPanePath(pane, path) {
    if (!CODE_PANES.includes(pane)) return;
    const value = path ? String(path) : null;
    state.panePaths[pane] = value;
    refreshPaneFileButtons();
  }

  function currentPanePath(pane) {
    const explicit = state.panePaths?.[pane];
    if (explicit) return explicit;
    if (pane === 'artifact') return state.selectedArtifactPath || null;
    const task = state.selectedTaskSnapshot;
    if (!task || !task.paths) return null;
    if (task.kind !== 'eval') return null;
    if (task.kind === 'eval' && state.selectedEvalRunDir) {
      const selectedRun = selectedEvalRunEntry();
      const runDir = resolveEvalRunArtifactDir(selectedRun);
      if (pane === 'transcript') return runDir ? buildEvalRunFilePath(runDir, 'raw_transcript.txt') : null;
      if (pane === 'cost') return runDir ? buildEvalRunFilePath(runDir, 'cost_summary.json') : null;
    }
    return null;
  }

  function refreshPaneFileButtons() {
    return undefined;
  }

  function refreshTaskLogDirButtons() {
    return undefined;
  }

  function applyResizableLayoutState() {
    if (els.artifactLayout) {
      const w = Math.max(220, Math.min(720, Number(state.artifactLeftWidth || 320)));
      els.artifactLayout.style.setProperty('--artifact-left-width', `${w}px`);
    }
  }

  function toggleTaskSummaryPanel() {
    state.taskSummaryExpanded = !state.taskSummaryExpanded;
    if (els.toggleTaskSummaryBtn) {
      els.toggleTaskSummaryBtn.textContent = state.taskSummaryExpanded ? 'Collapse' : 'Expand';
      els.toggleTaskSummaryBtn.title = state.taskSummaryExpanded ? 'Collapse details' : 'Expand details';
    }
    if (state.selectedTaskSnapshot) {
      renderTaskSummary(state.selectedTaskSnapshot, { immediate: true });
    }
  }

  function toggleRunSummaryPanel() {
    state.runSummaryExpanded = !state.runSummaryExpanded;
    if (els.toggleRunSummaryBtn) {
      els.toggleRunSummaryBtn.textContent = state.runSummaryExpanded ? 'Collapse' : 'Expand';
      els.toggleRunSummaryBtn.title = state.runSummaryExpanded ? 'Collapse details' : 'Expand details';
    }
    if (state.selectedTaskSnapshot) {
      renderTaskSummary(state.selectedTaskSnapshot, { immediate: true });
    }
  }

  function beginDrag(startEvent, onMove, onEnd) {
    const pointerId = startEvent?.pointerId;
    const move = (ev) => onMove(ev);
    const up = (ev) => {
      if (pointerId != null && ev && ev.pointerId != null && ev.pointerId !== pointerId) return;
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      window.removeEventListener('pointercancel', up);
      document.body.classList.remove('is-dragging-splitter');
      if (onEnd) onEnd();
    };
    document.body.classList.add('is-dragging-splitter');
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', up);
  }

  function bindResizers() {
    if (els.toggleTaskSummaryBtn) {
      els.toggleTaskSummaryBtn.addEventListener('click', toggleTaskSummaryPanel);
    }
    if (els.toggleRunSummaryBtn) {
      els.toggleRunSummaryBtn.addEventListener('click', toggleRunSummaryPanel);
    }
    if (els.costSummaryViewCardBtn) {
      els.costSummaryViewCardBtn.addEventListener('click', () => setCostSummaryViewMode('card'));
    }
    if (els.costSummaryViewSourceBtn) {
      els.costSummaryViewSourceBtn.addEventListener('click', () => setCostSummaryViewMode('source'));
    }
    if (els.artifactLayoutResizer && els.artifactLayout) {
      els.artifactLayoutResizer.addEventListener('pointerdown', (ev) => {
        ev.preventDefault();
        els.artifactLayoutResizer.setPointerCapture?.(ev.pointerId);
        const layoutRect = els.artifactLayout.getBoundingClientRect();
        const startX = ev.clientX;
        const startW = Number(state.artifactLeftWidth || 320);
        beginDrag(
          ev,
          (moveEv) => {
            moveEv.preventDefault?.();
            const delta = moveEv.clientX - startX;
            const maxWidth = Math.max(240, layoutRect.width - 280);
            state.artifactLeftWidth = Math.max(220, Math.min(maxWidth, startW + delta));
            applyResizableLayoutState();
          },
          null,
        );
      });
    }
  }

  function setCodePaneWrap(pane, enabled) {
    const dom = paneDom(pane);
    if (!dom) return;
    state.paneWrap[pane] = !!enabled;
    dom.text.classList.toggle('wrap-on', !!enabled);
    dom.text.classList.toggle('wrap-off', !enabled);
    const btn = document.querySelector(`[data-action="wrap"][data-pane="${pane}"]`);
    if (btn) {
      btn.classList.toggle('active', !!enabled);
      btn.textContent = `Wrap: ${enabled ? 'on' : 'off'}`;
    }
  }

  function paneRenderDelayMs(pane) {
    if (pane === 'transcript') return 120;
    return 32;
  }

  function scheduleCodePaneRender(pane, { autoScroll = true } = {}) {
    if (!CODE_PANES.includes(pane)) return;
    if (autoScroll) state.paneRenderAutoScroll[pane] = true;
    if (state.paneRenderTimers[pane]) return;
    state.paneRenderTimers[pane] = window.setTimeout(() => {
      state.paneRenderTimers[pane] = null;
      const shouldAutoScroll = !!state.paneRenderAutoScroll[pane];
      state.paneRenderAutoScroll[pane] = false;
      setCodePaneText(pane, state.logBuffers[pane] || '', { autoScroll: shouldAutoScroll });
    }, paneRenderDelayMs(pane));
  }

  function escapeHtml(text) {
    return text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  function setCodePaneText(pane, text, { autoScroll = true } = {}) {
    const dom = paneDom(pane);
    if (!dom) return;
    const viewport = dom.text.parentElement;
    const nearBottom = viewport
      ? Math.abs((viewport.scrollHeight - viewport.scrollTop) - viewport.clientHeight) < 32
      : false;

    const transcriptCardMode = pane === 'transcript' && isTranscriptCardModeEnabled();
    let html;
    if (transcriptCardMode) {
      html = renderTranscriptCards(text);
      dom.text.classList.add('transcript-card-mode');
      viewport?.classList.add('transcript-card-viewport');
    } else {
      const lines = String(text).split('\n');
      const MAX_LINE_LENGTH = 5000; // Truncate very long lines.
      html = lines.map((line) => {
        const displayLine = line.length > MAX_LINE_LENGTH
          ? escapeHtml(line.substring(0, MAX_LINE_LENGTH)) + '... [truncated]'
          : escapeHtml(line);
        return `<div class="code-line">${displayLine}</div>`;
      }).join('');
      dom.text.classList.remove('transcript-card-mode');
      viewport?.classList.remove('transcript-card-viewport');
    }
    dom.text.innerHTML = html;

    // Bind expand/collapse button events for transcript cards
    if (transcriptCardMode) {
      // Bind tool pair jump links
      dom.text.querySelectorAll('.badge-clickable').forEach((badge) => {
        badge.addEventListener('click', (ev) => {
          ev.stopPropagation();
          const targetId = badge.getAttribute('data-target-card');
          if (targetId) {
            const targetCard = document.getElementById(targetId);
            if (targetCard) {
              targetCard.scrollIntoView({ behavior: 'smooth', block: 'center' });
              // Highlight flash effect
              targetCard.classList.add('card-highlight-flash');
              setTimeout(() => targetCard.classList.remove('card-highlight-flash'), 1500);
            }
          }
        });
      });

      dom.text.querySelectorAll('.transcript-expand-btn').forEach((btn) => {
        btn.addEventListener('click', (ev) => {
          ev.stopPropagation();
          const expanded = btn.getAttribute('data-expanded') === 'true';
          const container = btn.parentElement;
          const preview = container.querySelector('.transcript-content-preview');
          const full = container.querySelector('.transcript-content-hidden');
          const expandText = btn.querySelector('.expand-text');
          const collapseText = btn.querySelector('.collapse-text');

          if (expanded) {
            // Collapse
            preview.style.display = 'block';
            full.style.display = 'none';
            expandText.style.display = 'inline';
            collapseText.style.display = 'none';
            btn.setAttribute('data-expanded', 'false');
          } else {
            // Expand
            preview.style.display = 'none';
            full.style.display = 'block';
            full.classList.add('transcript-card-content', 'prewrap');
            expandText.style.display = 'none';
            collapseText.style.display = 'inline';
            btn.setAttribute('data-expanded', 'true');
          }
        });
      });
    }

    if (autoScroll && viewport && nearBottom) {
      viewport.scrollTop = viewport.scrollHeight;
    }
  }

  function rerenderTranscriptPane({ autoScroll = false } = {}) {
    if (state.logBuffers.transcript == null) return;
    setCodePaneText('transcript', state.logBuffers.transcript || '', { autoScroll });
  }

  function toggleTranscriptFormat() {
    state.transcriptFormatted = !state.transcriptFormatted;
    refreshTranscriptFormatButtonState();
    // Re-render transcript
    if (state.logBuffers.transcript != null) {
      setCodePaneText('transcript', state.logBuffers.transcript, { autoScroll: false });
    }
  }

  function toggleTranscriptPairing() {
    state.transcriptAutoPairing = !state.transcriptAutoPairing;
    refreshTranscriptPairingButtonState();
    if (state.logBuffers.transcript != null) {
      setCodePaneText('transcript', state.logBuffers.transcript, { autoScroll: false });
    }
  }

  function appendLog(pane, text) {
    if (!text) return;
    state.logBuffers[pane] = (state.logBuffers[pane] || '') + text;
    const limit = 400_000;
    if (state.logBuffers[pane].length > limit) {
      state.logBuffers[pane] = state.logBuffers[pane].slice(-limit);
    }
    scheduleCodePaneRender(pane, { autoScroll: true });
  }

  async function readArtifactAsText(taskId, path) {
    const data = await api(`/api/tasks/${taskId}/artifact?path=${encodeURIComponent(path)}`);
    if (data.json != null) return JSON.stringify(data.json, null, 2);
    return data.content || '';
  }

  async function readArtifactTailAsText(taskId, path, { maxBytes = 512000, fromEnd = true } = {}) {
    const params = new URLSearchParams({
      path: String(path || ''),
      offset: '0',
      max_bytes: String(maxBytes),
    });
    if (fromEnd) params.set('from_end', 'true');
    const data = await api(`/api/tasks/${taskId}/tail?${params.toString()}`);
    return {
      text: data?.text || '',
      truncatedHead: !!data?.truncated_head,
      startOffset: Number(data?.start_offset || 0),
      size: Number(data?.size || 0),
      offset: Number(data?.offset || 0),
    };
  }

  function listEvalRootArtifactPaths(task) {
    const paths = task?.paths || {};
    const rootSet = new Set();
    for (const key of ['progress_jsonl', 'summary_json', 'results_jsonl', 'manifest_json', 'config_json']) {
      const v = normalizePathForCompare(paths[key]);
      if (v) rootSet.add(v);
    }
    return rootSet;
  }

  function filterArtifactsBySelectedEvalRun(task, artifacts) {
    if (!task || task.kind !== 'eval') return artifacts;
    const selected = selectedEvalRunEntry();
    const runDir = resolveEvalRunArtifactDir(selected);
    if (!runDir) return artifacts;
    const roots = listEvalRootArtifactPaths(task);
    return artifacts.filter((item) => {
      const p = normalizePathForCompare(item?.path);
      if (!p) return false;
      return isPathInsideDir(p, runDir) || roots.has(p);
    });
  }

  function buildArtifactsUrl(taskId, task = state.selectedTaskSnapshot, selectedRunDir = null) {
    const encodedTaskId = encodeURIComponent(String(taskId || ''));
    const params = new URLSearchParams();
    if (task?.kind === 'eval') {
      const runDir = selectedRunDir || resolveEvalRunArtifactDir(selectedEvalRunEntry());
      if (runDir) params.set('selected_run_dir', runDir);
    }
    const query = params.toString();
    return `/api/tasks/${encodedTaskId}/artifacts${query ? `?${query}` : ''}`;
  }

  async function loadEvalRuns(taskId, { generation = currentTaskSelectionGeneration(), throwOnError = false } = {}) {
    if (!taskId) return;
    if (!isCurrentTaskSelection(taskId, generation)) return;
    const requestSeq = (Number(state.evalRunsLoadSequence) || 0) + 1;
    state.evalRunsLoadSequence = requestSeq;
    const isCurrentEvalRunsRequest = () => isCurrentTaskSelection(taskId, generation)
      && Number(state.evalRunsLoadSequence) === requestSeq;
    try {
      const data = await api(`/api/tasks/${encodeURIComponent(taskId)}/eval-runs`);
      if (!isCurrentEvalRunsRequest()) return;
      const incoming = Array.isArray(data?.runs) ? data.runs : [];
      const prev = normalizePathForCompare(state.selectedEvalRunDir);
      state.evalRuns = normalizeEvalRunsPayload(taskId, incoming);
      state.evalRunsLoadingTaskId = '';
      const hasPrev = !!prev && state.evalRuns.some((x) => evalRunKey(x) === prev);
      if (hasPrev) {
        state.selectedEvalRunDir = prev;
      } else {
        const running = state.evalRuns.find((x) => getEvalRunExecState(x) === 'running');
        const pending = state.evalRuns.find((x) => getEvalRunExecState(x) === 'pending');
        const fallback = running || pending || (state.evalRuns.length ? state.evalRuns[state.evalRuns.length - 1] : null);
        state.selectedEvalRunDir = fallback ? evalRunKey(fallback) : null;
      }
      renderEvalRunsList({ force: true });
      if (state.selectedTaskSnapshot) renderTaskSummary(state.selectedTaskSnapshot, { immediate: true });
      ensureEvalRunCostSummaries(state.selectedTaskSnapshot, { generation }).catch(() => {});
    } catch (err) {
      if (!isCurrentEvalRunsRequest()) return;
      clearEvalRunsState();
      renderEvalRunsList();
      if (throwOnError) throw err;
    }
  }

  function resetEvalRunContentPanes() {
    for (const pane of ['transcript', 'cost', 'artifact']) {
      state.logBuffers[pane] = '';
      setPanePath(pane, null);
      setCodePaneText(pane, '', { autoScroll: false });
    }
    state.selectedArtifactPath = null;
    state.costSummaryData = null;
    state.costSummaryPath = null;
    state.costSummaryViewMode = 'card';
    renderCostSummaryPresentation();
    if (els.artifactPath) els.artifactPath.textContent = '';
  }

  async function refreshSelectedEvalRunLiveViews(task = state.selectedTaskSnapshot, { generation = currentTaskSelectionGeneration() } = {}) {
    if (!task || task.kind !== 'eval' || !state.selectedEvalRunDir) return;
    const selectedRunKey = normalizePathForCompare(state.selectedEvalRunDir);
    setEvalRunContentLoading(task.task_id, selectedRunKey);
    renderTaskSummary(task, { immediate: true });
    await Promise.allSettled([
      ensureEvalRunMetadata(task, selectedRunKey),
      ensurePerformanceForCurrentSelection(task),
      loadArtifacts({ generation }),
      loadSelectedEvalRunSnapshot(task, selectedRunKey, { generation }),
    ]);
    if (isCurrentEvalRunSelection(task.task_id, generation, selectedRunKey)) {
      clearEvalRunContentLoading(task.task_id, selectedRunKey);
      renderTaskSummary(task, { immediate: true });
    }
  }

  async function loadSelectedEvalRunSnapshot(task, runDir, { generation = currentTaskSelectionGeneration() } = {}) {
    if (!task || task.kind !== 'eval') return;
    const selectedRunKey = normalizePathForCompare(runDir);
    if (!selectedRunKey) return;
    const requestedTaskId = String(task.task_id || '').trim();
    const isStillCurrentSelection = () => isCurrentEvalRunSelection(requestedTaskId, generation, selectedRunKey);
    if (!isStillCurrentSelection()) return;
    setEvalRunContentLoading(requestedTaskId, selectedRunKey);
    try {
      const entry = findEvalRunEntry(selectedRunKey);
      const artifactDir = resolveEvalRunArtifactDir(entry);
      const cachedTranscript = state.evalRunTranscriptBuffers[selectedRunKey] || '';
      if (!artifactDir) {
        if (!isStillCurrentSelection()) return;
        if (cachedTranscript) {
          state.logBuffers.transcript = cachedTranscript;
          setCodePaneText('transcript', cachedTranscript, { autoScroll: false });
        }
        await loadCostSummaryPanel({ generation });
        return;
      }

      const transcriptPath = buildEvalRunFilePath(artifactDir, 'raw_transcript.txt');
      if (transcriptPath) {
        setPanePath('transcript', transcriptPath);
        try {
          const preview = await readArtifactTailAsText(task.task_id, transcriptPath, {
            maxBytes: 512000,
            fromEnd: true,
          });
          if (!isStillCurrentSelection()) return;
          const text = preview.truncatedHead
            ? `[studio:preview] showing the last ${formatBytes(Math.max(0, preview.size - preview.startOffset))} of ${formatBytes(preview.size)} from raw_transcript.txt. Open the artifact for the full file.\n${preview.text}`
            : preview.text;
          state.logBuffers.transcript = text;
          setCodePaneText('transcript', text, { autoScroll: false });
        } catch (err) {
          if (!isStillCurrentSelection()) return;
          if (cachedTranscript) {
            state.logBuffers.transcript = cachedTranscript;
            setCodePaneText('transcript', cachedTranscript, { autoScroll: false });
          }
        }
      }

      const knowledgePath = buildEvalRunFilePath(artifactDir, 'knowledge_injection.jsonl');
      if (knowledgePath) {
        try {
          const knowledgeText = await readArtifactAsText(task.task_id, knowledgePath);
          if (!isStillCurrentSelection()) return;
          upsertKnowledgeInjections(artifactDir, parseJsonlObjects(knowledgeText));
        } catch {
          if (!isStillCurrentSelection()) return;
          upsertKnowledgeInjections(artifactDir, []);
        }
        await ensureKnowledgeSkillContentsForRun(task, artifactDir);
        if (!isStillCurrentSelection()) return;
        rerenderTranscriptPane({ autoScroll: false });
      }

      await loadCostSummaryPanel({ generation });
      if (!isStillCurrentSelection()) return;

    } finally {
      if (isStillCurrentSelection()) clearEvalRunContentLoading(requestedTaskId, selectedRunKey);
    }
  }

  async function selectEvalRun(runDir) {
    const task = state.selectedTaskSnapshot;
    if (!task || task.kind !== 'eval') return;
    const normalized = normalizePathForCompare(runDir);
    if (!normalized) return;
    const generation = currentTaskSelectionGeneration();
    if (normalizePathForCompare(state.selectedEvalRunDir) === normalized) {
      renderEvalRunsList();
      renderTaskSummary(task, { immediate: true });
      void refreshSelectedEvalRunLiveViews(task, { generation }).catch(() => {});
      return;
    }
    state.selectedEvalRunDir = normalized;
    renderEvalRunsList();
    resetEvalRunContentPanes();
    setEvalRunContentLoading(task.task_id, normalized);
    renderTaskSummary(task, { immediate: true });
    void refreshSelectedEvalRunLiveViews(task, { generation }).catch(() => {});
  }

  async function loadArtifacts({ showFailureAlert = false, trackOperation = false, throwOnError = false, generation = currentTaskSelectionGeneration() } = {}) {
    if (state.artifactListRefreshTimer) {
      window.clearTimeout(state.artifactListRefreshTimer);
      state.artifactListRefreshTimer = null;
    }
    if (trackOperation) {
      if (state.artifactListLoading) {
        recordOperationResult({
          title: 'Refresh key artifacts',
          status: 'cancelled',
          message: 'An artifact refresh is already running; this request was ignored.',
          detail: 'Wait for the current refresh to finish.',
          toastVariant: 'success',
          toastDurationMs: 1800,
        });
        return;
      }
      try {
        return await runTrackedOperation({
          title: 'Refresh key artifacts',
          detail: `Task ID: ${state.selectedTaskId || '--'}`,
          successMessage: 'Key artifacts refreshed',
          successDetail: () => `Current artifact count: ${els.artifactList?.children?.length || 0}`,
          toastSuccess: true,
          toastSuccessDurationMs: 1800,
          errorMessagePrefix: 'Refresh artifacts failed',
        }, async () => {
          await loadArtifacts({ showFailureAlert: false, trackOperation: false, throwOnError: true });
          return { artifactCount: els.artifactList?.children?.length || 0 };
        });
      } catch {
        return;
      }
    }
    if (!state.selectedTaskId) return;
    if (state.artifactListLoading) {
      state.artifactListRefreshPending = true;
      state.artifactListRefreshPendingGeneration = generation;
      return;
    }
    state.artifactListLoading = true;
    const taskId = state.selectedTaskId;
    try {
      const task = state.selectedTaskSnapshot;
      const requestEvalRunKey = task?.kind === 'eval' ? normalizePathForCompare(state.selectedEvalRunDir) : '';
      const requestArtifactRunDir = task?.kind === 'eval'
        ? normalizePathForCompare(resolveEvalRunArtifactDir(selectedEvalRunEntry()))
        : '';
      const data = await api(buildArtifactsUrl(taskId, task, requestArtifactRunDir));
      if (!isCurrentTaskSelection(taskId, generation)) return;
      if (task?.kind === 'eval') {
        const currentRunKey = normalizePathForCompare(state.selectedEvalRunDir);
        const currentArtifactRunDir = normalizePathForCompare(resolveEvalRunArtifactDir(selectedEvalRunEntry()));
        if (currentRunKey !== requestEvalRunKey || currentArtifactRunDir !== requestArtifactRunDir) return;
      }
      const allArtifacts = Array.isArray(data.artifacts) ? data.artifacts : [];
      const artifacts = filterArtifactsBySelectedEvalRun(task, allArtifacts);
      const artifactPriority = (item) => {
        const name = String(item?.name || '');
        if (name === 'knowledge_injection.jsonl') return 1;
        if (name === 'rag_retrieval.jsonl') return 2;
        if (name === 'rag_injection.jsonl') return 3;
        if (name === 'trimmed_transcript.txt') return 4;
        if (name === 'chunks.jsonl') return 5;
        if (name === 'index_update.json') return 6;
        if (name === 'raw_transcript.txt') return 7;
        if (name === 'final_report.md') return 8;
        if (name === 'final_report.json') return 9;
        if (name === 'cost_summary.json') return 10;
        if (name === 'progress.jsonl') return 11;
        if (name === 'summary.json') return 12;
        return 99;
      };
      artifacts.sort((a, b) => {
        const pa = artifactPriority(a);
        const pb = artifactPriority(b);
        if (pa !== pb) return pa - pb;
        return normalizePathForCompare(a?.path).localeCompare(normalizePathForCompare(b?.path));
      });
      els.artifactList.innerHTML = '';
      for (const item of artifacts) {
        if (!item.exists || item.is_dir) continue;
        const li = document.createElement('li');
        const btn = document.createElement('button');
        btn.disabled = false;
        btn.textContent = `${item.name} (${formatBytes(item.size || 0)})`;
        btn.title = item.path;
        btn.addEventListener('click', () => openArtifact(item.path, { trackOperation: true }));
        li.appendChild(btn);
        els.artifactList.appendChild(li);
      }
      if (state.selectedArtifactPath) {
        const selectedExists = artifacts.some((x) => normalizePathForCompare(x.path) === normalizePathForCompare(state.selectedArtifactPath));
        if (!selectedExists) {
          state.selectedArtifactPath = null;
          setPanePath('artifact', null);
          if (els.artifactPath) els.artifactPath.textContent = '';
          state.costSummaryData = null;
          state.costSummaryPath = null;
          state.costSummaryViewMode = 'card';
          state.logBuffers.artifact = '';
          setCodePaneText('artifact', '', { autoScroll: false });
          renderCostSummaryPresentation();
        }
      }
    } catch (err) {
      if (!isCurrentTaskSelection(taskId, generation)) return;
      if (showFailureAlert) {
        showToast(`Refresh artifacts failed: ${err.message}`, { variant: 'error' });
      }
      if (throwOnError) throw err;
    } finally {
      state.artifactListLoading = false;
      if (state.artifactListRefreshPending) {
        const pendingGeneration = state.artifactListRefreshPendingGeneration ?? currentTaskSelectionGeneration();
        state.artifactListRefreshPending = false;
        state.artifactListRefreshPendingGeneration = null;
        scheduleArtifactsRefresh(600, { showFailureAlert, throwOnError: false, generation: pendingGeneration });
      }
    }
  }

  function scheduleArtifactsRefresh(delayMs = 600, options = {}) {
    if (!state.selectedTaskId) return;
    if (state.artifactListRefreshTimer) {
      window.clearTimeout(state.artifactListRefreshTimer);
    }
    state.artifactListRefreshTimer = window.setTimeout(() => {
      state.artifactListRefreshTimer = null;
      void loadArtifacts({
        showFailureAlert: !!options.showFailureAlert,
        trackOperation: false,
        throwOnError: !!options.throwOnError,
        generation: options.generation ?? currentTaskSelectionGeneration(),
      });
    }, Math.max(0, Number(delayMs) || 0));
  }

  function isArtifactCostSummaryPath(path) {
    return basenameOfPath(path || '') === 'cost_summary.json';
  }

  function formatArtifactInteger(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) return '';
    try {
      return new Intl.NumberFormat('zh-CN').format(num);
    } catch {
      return String(num);
    }
  }

  function formatArtifactDurationMs(value) {
    const num = Number(value);
    if (!Number.isFinite(num) || num < 0) return '';
    if (typeof formatDurationCompact === 'function') {
      return formatDurationCompact(num / 1000);
    }
    return `${(num / 1000).toFixed(2)}s`;
  }

  function formatArtifactUsd(value) {
    if (value == null || value === '') return '';
    const num = Number(value);
    if (!Number.isFinite(num)) return '';
    return `$${num.toFixed(4)}`;
  }

  function formatArtifactPercent(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) return '';
    return `${(num * 100).toFixed(1)}%`;
  }

  function formatArtifactScalar(value) {
    if (value == null || value === '') return '';
    if (typeof value === 'boolean') return value ? 'on' : 'off';
    if (Array.isArray(value)) return value.filter((item) => String(item || '').trim()).join(', ');
    if (typeof value === 'number') return formatArtifactInteger(value);
    return String(value);
  }

  function renderCostSummaryMetricItems(rows) {
    const items = (Array.isArray(rows) ? rows : []).filter((row) => row && String(row.v || '').trim());
    if (!items.length) {
      return '<div class="cost-summary-empty muted">No metrics</div>';
    }
    return `
      <div class="cost-summary-metrics">
        ${items.map((row) => `
          <div class="cost-summary-metric">
            <span class="mk">${escapeHtml(String(row.k || ''))}</span>
            <span class="mv">${escapeHtml(String(row.v || ''))}</span>
          </div>
        `).join('')}
      </div>
    `;
  }

  function renderCostSummaryCard(title, subtitle, rows, { tone = 'normal' } = {}) {
    return `
      <section class="cost-summary-card tone-${escapeHtml(String(tone || 'normal'))}">
        <div class="cost-summary-card-head">
          <div class="cost-summary-card-title-wrap">
            <div class="cost-summary-card-title">${escapeHtml(String(title || 'Untitled card'))}</div>
            ${subtitle ? `<div class="cost-summary-card-subtitle">${escapeHtml(String(subtitle))}</div>` : ''}
          </div>
        </div>
        ${renderCostSummaryMetricItems(rows)}
      </section>
    `;
  }

  function buildCostSummaryUsageRows(usage) {
    if (!usage || typeof usage !== 'object') return [];
    const rows = [];
    const push = (label, value) => {
      const text = formatArtifactInteger(value);
      if (text) rows.push({ k: label, v: text });
    };
    push('input_tokens', usage.input_tokens);
    push('output_tokens', usage.output_tokens);
    push('cache_read_tokens', usage.cache_read_input_tokens);
    push('cache_creation_tokens', usage.cache_creation_input_tokens);
    push('embedding_tokens', usage.embedding_tokens ?? usage.mem0_embedding_tokens);
    push('total_tokens', usage.total_tokens);
    return rows;
  }

  function buildCostSummaryAggregateRows(metrics) {
    if (!metrics || typeof metrics !== 'object') return [];
    const rows = [];
    const pushScalar = (label, value, formatter = formatArtifactScalar) => {
      const text = formatter(value);
      if (text) rows.push({ k: label, v: text });
    };
    pushScalar('react_turns', metrics.num_turns_sum, formatArtifactInteger);
    if (metrics.cost_tracked === false && metrics.total_cost_usd_sum == null) {
      pushScalar('final_cost', 'unavailable');
    } else {
      pushScalar('final_cost', metrics.total_cost_usd_sum, formatArtifactUsd);
    }
    pushScalar('status', metrics.status);
    pushScalar('requests', metrics.request_count, formatArtifactInteger);
    pushScalar('opencode_cost', metrics.opencode_total_cost_usd_sum, formatArtifactUsd);
    pushScalar('mem0_backend_cost', metrics.mem0_backend_total_cost_usd_sum, formatArtifactUsd);
    pushScalar('terminal_errors', metrics.terminal_error_result_count, formatArtifactInteger);
    if (Object.prototype.hasOwnProperty.call(metrics, 'has_terminal_error_result')) pushScalar('has_terminal_error_result', metrics.has_terminal_error_result);
    if (Array.isArray(metrics.terminal_error_phase_names) && metrics.terminal_error_phase_names.length) {
      rows.push({ k: 'terminal_error_phases', v: metrics.terminal_error_phase_names.join(', ') });
    }
    rows.push(...buildCostSummaryUsageRows(metrics.usage));
    return rows;
  }

  function buildCostSummaryPhaseRows(phase) {
    if (!phase || typeof phase !== 'object') return [];
    const rows = [];
    const result = phase.result && typeof phase.result === 'object' ? phase.result : {};
    const pushScalar = (label, value, formatter = formatArtifactScalar) => {
      const text = formatter(value);
      if (text) rows.push({ k: label, v: text });
    };
    pushScalar('react_turns', result.num_turns, formatArtifactInteger);
    pushScalar('cost', result.total_cost_usd, formatArtifactUsd);
    pushScalar('tool_uses', phase.tool_use_block_count, formatArtifactInteger);
    pushScalar('stop_reason', phase.stop_reason);
    if (Object.prototype.hasOwnProperty.call(phase, 'is_error')) pushScalar('error', phase.is_error);
    rows.push(...buildCostSummaryUsageRows(result.usage));
    return rows;
  }

  function renderCostSummaryArtifact(payload) {
    const cost = payload && typeof payload === 'object' ? payload : {};
    const overviewRows = [];
    const pushOverview = (label, value, formatter = formatArtifactScalar) => {
      const text = formatter(value);
      if (text) overviewRows.push({ k: label, v: text });
    };
    pushOverview('agent_mode', cost.agent_mode);
    pushOverview('flowark_extensions', cost.flowark_extensions_enabled);
    pushOverview('analysis_messages', cost.analysis_turn_raw_message_count, formatArtifactInteger);
    pushOverview('tool_uses', cost.tool_use_block_count_total, formatArtifactInteger);

    const aggregated = cost.aggregated_metrics && typeof cost.aggregated_metrics === 'object' ? cost.aggregated_metrics : {};
    const aggregateTitles = {
      main_agent: 'OpenCode aggregate',
      mem0_backend: 'Mem0 backend',
      total_with_mem0: 'OpenCode + Mem0',
    };
    const aggregateCards = Object.entries(aggregated)
      .filter(([name, metrics]) => Object.prototype.hasOwnProperty.call(aggregateTitles, name) && metrics && typeof metrics === 'object')
      .map(([name, metrics]) => {
        let tone = 'normal';
        if (metrics.has_terminal_error_result) tone = 'danger';
        else if (metrics.has_error_result) tone = 'warn';
        return renderCostSummaryCard(
          name,
          aggregateTitles[name] || 'Aggregate',
          buildCostSummaryAggregateRows(metrics),
          { tone },
        );
      });

    const phases = Array.isArray(cost.phases) ? cost.phases : [];
    const phaseCards = phases.map((phase, index) => {
      const title = String(phase?.name || phase?.phase_name || `phase_${index + 1}`);
      const subtitle = [phase?.family, phase?.bucket].filter((item) => String(item || '').trim()).join(' · ');
      return renderCostSummaryCard(title, subtitle, buildCostSummaryPhaseRows(phase), {
        tone: phase?.is_error ? 'danger' : 'normal',
      });
    });

    return `
      <div class="artifact-view-section">
        <div class="artifact-view-section-title">Cost summary overview</div>
        <div class="cost-summary-grid">
          ${renderCostSummaryCard('overview', 'Run cost and message scale', overviewRows)}
          ${aggregateCards.join('')}
        </div>
      </div>
      <div class="artifact-view-section">
        <div class="artifact-view-section-title">Phase costs</div>
        <div class="cost-summary-grid">
          ${phaseCards.length ? phaseCards.join('') : '<div class="cost-summary-empty muted">No phase data</div>'}
        </div>
      </div>
    `;
  }

  function costSummaryWrapButton() {
    return document.querySelector('[data-action="wrap"][data-pane="cost"]');
  }

  function renderCostSummaryPresentation() {
    const hasCostSummary = !!state.costSummaryData;
    const showCard = hasCostSummary && state.costSummaryViewMode === 'card';
    if (els.costSummaryViewCardBtn) {
      els.costSummaryViewCardBtn.classList.toggle('active', showCard);
      els.costSummaryViewCardBtn.disabled = !hasCostSummary;
    }
    if (els.costSummaryViewSourceBtn) {
      els.costSummaryViewSourceBtn.classList.toggle('active', !showCard && hasCostSummary);
      els.costSummaryViewSourceBtn.disabled = !hasCostSummary;
    }
    if (els.costSummaryCardView) {
      els.costSummaryCardView.classList.toggle('hidden', hasCostSummary && !showCard);
      els.costSummaryCardView.innerHTML = showCard
        ? renderCostSummaryArtifact(state.costSummaryData)
        : (hasCostSummary ? '' : '<div class="cost-summary-empty muted">No cost_summary.json</div>');
    }
    if (els.costSummaryCodeViewport) {
      els.costSummaryCodeViewport.classList.toggle('hidden', showCard || !hasCostSummary);
    }
    const wrapBtn = costSummaryWrapButton();
    if (wrapBtn) {
      wrapBtn.disabled = !state.selectedTaskId || !currentPanePath('cost') || showCard;
    }
  }

  function setCostSummaryViewMode(mode) {
    if (mode !== 'card' && mode !== 'source') return;
    if (mode === 'card' && !state.costSummaryData) return;
    state.costSummaryViewMode = mode;
    renderCostSummaryPresentation();
  }

  function setCostSummaryPayload(path, data) {
    let payload = null;
    if (isArtifactCostSummaryPath(path)) {
      if (data?.json != null && typeof data.json === 'object') {
        payload = data.json;
      } else {
        const text = String(data?.content || '').trim();
        if (text) {
          try {
            const parsed = JSON.parse(text);
            if (parsed && typeof parsed === 'object') {
              payload = parsed;
            }
          } catch {
            // Keep source mode only when JSON parsing fails.
          }
        }
      }
    }
    state.costSummaryPath = payload ? path : null;
    state.costSummaryData = payload;
    state.costSummaryViewMode = payload ? 'card' : 'source';
    renderCostSummaryPresentation();
  }

  async function loadCostSummaryPanel({ generation = currentTaskSelectionGeneration() } = {}) {
    if (!state.selectedTaskId) return;
    const taskId = state.selectedTaskId;
    const path = currentPanePath('cost');
    if (!path) {
      if (!isCurrentTaskSelection(taskId, generation)) return;
      state.costSummaryPath = null;
      state.costSummaryData = null;
      state.costSummaryViewMode = 'card';
      state.logBuffers.cost = '';
      setPanePath('cost', null);
      setCodePaneText('cost', '', { autoScroll: false });
      renderCostSummaryPresentation();
      return;
    }
    setPanePath('cost', path);
    try {
      const data = await api(`/api/tasks/${encodeURIComponent(taskId)}/artifact?path=${encodeURIComponent(path)}`);
      if (!isCurrentTaskSelection(taskId, generation) || normalizePathForCompare(path) !== normalizePathForCompare(currentPanePath('cost'))) return;
      const text = data.json != null ? JSON.stringify(data.json, null, 2) : (data.content || '');
      state.logBuffers.cost = text;
      setCodePaneText('cost', text, { autoScroll: false });
      setCostSummaryPayload(path, data);
      const viewport = paneDom('cost')?.text.parentElement;
      if (viewport) viewport.scrollTop = 0;
    } catch (err) {
      if (!isCurrentTaskSelection(taskId, generation)) return;
      state.costSummaryPath = null;
      state.costSummaryData = null;
      state.costSummaryViewMode = 'source';
      renderCostSummaryPresentation();
      const msg = `[read cost_summary error] ${err.message}`;
      state.logBuffers.cost = msg;
      setCodePaneText('cost', msg, { autoScroll: false });
    }
  }

  async function openArtifact(path, { trackOperation = false, generation = currentTaskSelectionGeneration() } = {}) {
    if (!state.selectedTaskId) return;
    const taskId = state.selectedTaskId;
    state.selectedArtifactPath = path;
    setPanePath('artifact', path);
    els.artifactPath.textContent = path;
    try {
      const isRawTranscript = basenameOfPath(path) === 'raw_transcript.txt';
      const artifactUrl = `/api/tasks/${taskId}/artifact?path=${encodeURIComponent(path)}`;
      const loadArtifact = async () => {
        if (!isRawTranscript) return api(artifactUrl);
        const preview = await readArtifactTailAsText(taskId, path, {
          maxBytes: 1_000_000,
          fromEnd: true,
        });
        const text = preview.truncatedHead
          ? `[studio:preview] showing the last ${formatBytes(Math.max(0, preview.size - preview.startOffset))} of ${formatBytes(preview.size)} from raw_transcript.txt. Open the file externally for the full content.\n${preview.text}`
          : preview.text;
        return { content: text };
      };
      const data = trackOperation
        ? await runTrackedOperation({
          title: 'Open artifact',
          detail: path,
          successMessage: 'Artifact opened',
          successDetail: () => path,
          errorMessagePrefix: 'Open artifact failed',
          toastSuccess: false,
        }, loadArtifact)
        : await loadArtifact();
      if (!isCurrentTaskSelection(taskId, generation) || normalizePathForCompare(path) !== normalizePathForCompare(state.selectedArtifactPath)) return;
      let text = '';
      if (data.json != null) {
        text = JSON.stringify(data.json, null, 2);
      } else {
        text = data.content || '';
      }
      state.logBuffers.artifact = text;
      setCodePaneText('artifact', text, { autoScroll: false });
      const viewport = paneDom('artifact')?.text.parentElement;
      if (viewport) viewport.scrollTop = 0;
    } catch (err) {
      if (!isCurrentTaskSelection(taskId, generation) || normalizePathForCompare(path) !== normalizePathForCompare(state.selectedArtifactPath)) return;
      const msg = `[read artifact error] ${err.message}`;
      state.logBuffers.artifact = msg;
      setCodePaneText('artifact', msg, { autoScroll: false });
    }
  }

  function formatBytes(n) {
    const v = Number(n || 0);
    if (!Number.isFinite(v) || v <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    let value = v;
    let idx = 0;
    while (value >= 1024 && idx < units.length - 1) {
      value /= 1024;
      idx += 1;
    }
    return `${value.toFixed(value >= 100 ? 0 : value >= 10 ? 1 : 2)} ${units[idx]}`;
  }
