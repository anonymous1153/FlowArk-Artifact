  function normalizeArtifactEventPaths(items) {
    const rawItems = Array.isArray(items) ? items : [items];
    return rawItems
      .map((item) => normalizePathForCompare(typeof item === 'string' ? item : item?.path))
      .filter(Boolean);
  }

  const EVAL_RUN_TRANSCRIPT_BUFFER_MAX_RUNS = 8;
  const EVAL_RUN_TRANSCRIPT_BUFFER_MAX_CHARS = 1_200_000;

  function pruneEvalRunTranscriptBuffers(activeRunKey = '') {
    const buffers = state.evalRunTranscriptBuffers || {};
    const protectedKeys = new Set(
      [activeRunKey, state.selectedEvalRunDir]
        .map((key) => normalizePathForCompare(key))
        .filter(Boolean),
    );
    const bufferLength = (key) => String(buffers[key] || '').length;
    let keys = Object.keys(buffers);
    let totalChars = keys.reduce((sum, key) => sum + bufferLength(key), 0);
    while (keys.length > EVAL_RUN_TRANSCRIPT_BUFFER_MAX_RUNS || totalChars > EVAL_RUN_TRANSCRIPT_BUFFER_MAX_CHARS) {
      const dropKey = keys.find((key) => !protectedKeys.has(normalizePathForCompare(key)));
      if (!dropKey) break;
      totalChars -= bufferLength(dropKey);
      delete buffers[dropKey];
      keys = Object.keys(buffers);
    }
  }

  function refreshEvalRunCostSummariesAfterArtifactChange(task, paths) {
    if (!task || task.kind !== 'eval') return;
    const changedPaths = normalizeArtifactEventPaths(paths);
    if (!changedPaths.some((path) => path.endsWith('/cost_summary.json') || path.endsWith('/result.json'))) return;
    const taskId = String(task.task_id || state.selectedTaskId || '').trim();
    const generation = currentTaskSelectionGeneration();
    clearEvalRunPerformanceCachesForArtifactPaths(changedPaths);
    const reload = changedPaths.some((path) => path.endsWith('/result.json'))
      ? loadEvalRuns(taskId, { generation })
      : ensureEvalRunCostSummaries(task, { generation });
    reload
      .then(() => {
        if (!isCurrentTaskSelection(taskId, generation)) return;
        renderEvalRunsList();
        renderTaskSummary(state.selectedTaskSnapshot || task);
      })
      .catch((err) => console.warn('eval-run-cost refresh error', err));
  }

  function connectTaskStream(taskId, generation = currentTaskSelectionGeneration()) {
    const es = new EventSource(`/api/tasks/${encodeURIComponent(taskId)}/events`);
    state.eventSource = es;
    const isCurrentStream = () => isCurrentTaskSelection(taskId, generation);
    const handle = (type, fn) => es.addEventListener(type, (ev) => {
      try {
        const payload = JSON.parse(ev.data);
        fn(payload);
      } catch (err) {
        console.warn('event parse error', type, err, ev.data);
      }
    });

    handle('run_transcript_append', (e) => {
      const text = e.data?.text || '';
      const transcriptPath = e.data?.path ? String(e.data.path) : '';
      if (!isCurrentStream()) return;
      const selectedTask = state.selectedTaskSnapshot;
      if (selectedTask?.kind === 'eval') {
        const matchedEntry = findEvalRunByTranscriptPath(transcriptPath);
        if (matchedEntry) {
          const runKey = evalRunKey(matchedEntry);
          const runDir = normalizePathForCompare(toEvalRunDirFromTranscriptPath(transcriptPath));
          const prev = state.evalRunTranscriptBuffers[runKey] || '';
          const next = (prev + text).slice(-400_000);
          delete state.evalRunTranscriptBuffers[runKey];
          state.evalRunTranscriptBuffers[runKey] = next;
          pruneEvalRunTranscriptBuffers(runKey);
          mergeEvalRunEntry({ ...matchedEntry, run_dir: runDir || matchedEntry.run_dir });
          if (!state.selectedEvalRunDir) {
            state.selectedEvalRunDir = runKey;
            renderEvalRunsList();
            scheduleArtifactsRefresh(600, { generation });
            renderTaskSummary(selectedTask);
            ensureEvalRunMetadata(selectedTask, runKey).then(() => ensurePerformanceForCurrentSelection(selectedTask)).catch(() => {});
          }
          if (normalizePathForCompare(state.selectedEvalRunDir) === runKey) {
            if (transcriptPath) setPanePath('transcript', transcriptPath);
            appendLog('transcript', text);
            renderTaskSummary(selectedTask);
          }
          return;
        }
      }
      if (transcriptPath) setPanePath('transcript', transcriptPath);
      appendLog('transcript', text);
    });
    handle('knowledge_injection_append', (e) => {
      const path = e.data?.path ? String(e.data.path) : '';
      if (!isCurrentStream()) return;
      const selectedTask = state.selectedTaskSnapshot;
      let aliasKeys = [];
      if (selectedTask?.kind === 'eval') {
        const matchedEntry = findEvalRunByKnowledgePath(path);
        if (matchedEntry) {
          const runDir = normalizePathForCompare(toRunDirFromKnowledgePath(path));
          const runKey = evalRunKey(matchedEntry);
          aliasKeys = runKey ? [runKey] : [];
          mergeEvalRunEntry({ ...matchedEntry, run_dir: runDir || matchedEntry.run_dir });
          if (!state.selectedEvalRunDir) {
            state.selectedEvalRunDir = runKey;
            renderEvalRunsList();
            scheduleArtifactsRefresh(600, { generation });
            renderTaskSummary(selectedTask);
          }
        }
      }
      appendKnowledgeInjectionByPath(path, e.data?.item || {}, aliasKeys);
      ensureKnowledgeSkillContentsForCurrentRun().then(() => {
        if (!isCurrentStream()) return;
        rerenderTranscriptPane({ autoScroll: false });
        if (state.selectedTaskSnapshot?.kind === 'eval') {
          renderTaskSummary(state.selectedTaskSnapshot);
        }
      }).catch(() => {});
      const currentRunDir = getCurrentKnowledgeRunKey(state.selectedTaskSnapshot);
      if (currentRunDir && normalizePathForCompare(toRunDirFromKnowledgePath(path)) === normalizePathForCompare(currentRunDir)) {
        rerenderTranscriptPane({ autoScroll: false });
      }
    });
    handle('eval_progress_start', (e) => {
      if (!isCurrentStream()) return;
      if (state.selectedTaskSnapshot?.kind === 'eval') {
        upsertEvalRunFromProgress(e.data);
        renderTaskSummary(state.selectedTaskSnapshot);
      }
    });
    handle('eval_progress_finish', (e) => {
      if (!isCurrentStream()) return;
      if (state.selectedTaskSnapshot?.kind === 'eval') {
        upsertEvalRunFromProgress(e.data);
        scheduleArtifactsRefresh(600, { generation });
        renderTaskSummary(state.selectedTaskSnapshot);
      }
    });
    handle('artifact_created', (e) => {
      if (!isCurrentStream()) return;
      scheduleArtifactsRefresh(600, { generation });
      const path = String(e.data?.path || '');
      const task = state.selectedTaskSnapshot;
      if (path.endsWith('/cost_summary.json')) {
        loadCostSummaryPanel({ generation }).catch(() => {});
      }
      if (task?.kind === 'eval' && (path.endsWith('/cost_summary.json') || path.endsWith('/result.json') || path.endsWith('/summary.json'))) {
        refreshEvalRunCostSummariesAfterArtifactChange(task, [path]);
        ensurePerformanceForCurrentSelection(task).then(() => renderTaskSummary(task)).catch(() => {});
      }
    });
    handle('artifact_updated', (e) => {
      if (!isCurrentStream()) return;
      if (state.selectedArtifactPath === e.data?.path) openArtifact(e.data.path, { generation });
      const path = String(e.data?.path || '');
      const task = state.selectedTaskSnapshot;
      if (path.endsWith('/cost_summary.json')) {
        loadCostSummaryPanel({ generation }).catch(() => {});
      }
      if (task?.kind === 'eval' && (path.endsWith('/cost_summary.json') || path.endsWith('/result.json') || path.endsWith('/summary.json'))) {
        refreshEvalRunCostSummariesAfterArtifactChange(task, [path]);
        ensurePerformanceForCurrentSelection(task).then(() => renderTaskSummary(task)).catch(() => {});
      }
    });
    handle('artifacts_changed', (e) => {
      if (!isCurrentStream()) return;
      const count = Number(e.data?.count) || 0;
      scheduleArtifactsRefresh(600, { generation });
      const paths = Array.isArray(e.data?.paths) ? e.data.paths : [];
      const task = state.selectedTaskSnapshot;
      const selectedPath = normalizePathForCompare(state.selectedArtifactPath);
      const selectedChanged = !!selectedPath && paths.some((item) => normalizePathForCompare(item?.path) === selectedPath);
      if (selectedChanged) openArtifact(state.selectedArtifactPath, { generation });
      if (!task?.kind) return;
      if (paths.some((item) => String(item?.path || '').endsWith('/cost_summary.json'))) {
        loadCostSummaryPanel({ generation }).catch(() => {});
      }
      if (task.kind === 'eval' && paths.some((item) => {
        const path = String(item?.path || '');
        return path.endsWith('/cost_summary.json') || path.endsWith('/result.json') || path.endsWith('/summary.json');
      })) {
        refreshEvalRunCostSummariesAfterArtifactChange(task, paths);
        ensurePerformanceForCurrentSelection(task).then(() => renderTaskSummary(task)).catch(() => {});
      }
    });
    handle('task_status', (e) => {
      const t = state.tasks.find((x) => x.task_id === taskId);
      if (t) {
        t.metadata = t.metadata || {};
        if (Object.prototype.hasOwnProperty.call(e.data || {}, 'pause_requested')) {
          t.metadata.pause_requested = !!e.data.pause_requested;
        }
        if (Object.prototype.hasOwnProperty.call(e.data || {}, 'pause_confirmed')) {
          t.metadata.pause_confirmed = !!e.data.pause_confirmed;
        }
        if (typeof e.data?.pause_mode === 'string' && e.data.pause_mode) {
          t.metadata.pause_mode_requested = e.data.pause_mode;
        }
        if (typeof e.data?.dispatch_mode === 'string' && e.data.dispatch_mode) {
          t.metadata.dispatch_mode = e.data.dispatch_mode;
        }
        if (Object.prototype.hasOwnProperty.call(e.data || {}, 'queue_waiting')) {
          t.metadata.queue_waiting = !!e.data.queue_waiting;
        }
        if (Object.prototype.hasOwnProperty.call(e.data || {}, 'pause_reason')) {
          if (e.data.pause_reason == null || e.data.pause_reason === '') {
            delete t.metadata.pause_reason;
          } else {
            t.metadata.pause_reason = e.data.pause_reason;
          }
        }
        if (Object.prototype.hasOwnProperty.call(e.data || {}, 'queue_position')) {
          if (e.data.queue_position == null || e.data.queue_position === '') {
            delete t.metadata.queue_position;
          } else {
            t.metadata.queue_position = e.data.queue_position;
          }
        }
        if (Object.prototype.hasOwnProperty.call(e.data || {}, 'tags') || Object.prototype.hasOwnProperty.call(e.data || {}, 'group')) {
          const nextTags = Object.prototype.hasOwnProperty.call(e.data || {}, 'tags')
            ? normalizeTaskTags(e.data.tags)
            : normalizeTaskTags(e.data.group == null ? [] : [e.data.group]);
          delete t.metadata.group;
          if (nextTags.length) {
            t.metadata.tags = nextTags;
          } else {
            delete t.metadata.tags;
          }
        }
        for (const key of ['workspace_git_submitted', 'workspace_git_started', 'workspace_git_last_started', 'workspace_git_launch_history']) {
          if (!Object.prototype.hasOwnProperty.call(e.data || {}, key)) continue;
          if (e.data[key] == null || e.data[key] === '') {
            delete t.metadata[key];
          } else {
            t.metadata[key] = e.data[key];
          }
        }
        for (const key of ['eval_progress', 'eval_open_code_cost']) {
          if (!Object.prototype.hasOwnProperty.call(e.data || {}, key)) continue;
          if (e.data[key] == null || e.data[key] === '') {
            delete t.metadata[key];
          } else {
            t.metadata[key] = e.data[key];
          }
        }
        if (e.data?.status) {
          t.status = e.data.status;
        }
        const listSig = taskListSignature(state.tasks);
        if (listSig !== state.lastTaskListSignature) {
          state.lastTaskListSignature = listSig;
          renderTaskList();
        }
        if (isCurrentStream()) {
          const selected = mergeSelectedTaskSnapshotFromSummary(t);
          renderTaskTagEditor(selected);
          state.lastTaskSummarySignature = taskSummarySignature(selected);
          renderTaskSummary(selected);
          refreshStopButtonsForTask(selected);
          if (t.kind === 'eval' && state.evalRunsPaneVisible) {
            renderEvalRunsList();
          }
        }
      }
      if (e.data?.run_dir || e.data?.eval_root) {
        if (isCurrentStream() && state.selectedTaskSnapshot?.kind === 'eval' && e.data?.run_dir) {
          upsertEvalRunFromProgress(e.data);
        }
        if (!isCurrentStream()) return;
        scheduleArtifactsRefresh(600, { generation });
        if (state.selectedTaskSnapshot?.kind === 'eval') {
          loadEvalRuns(taskId, { generation }).then(() => {
            if (isCurrentStream() && state.selectedTaskSnapshot) renderTaskSummary(state.selectedTaskSnapshot);
          }).catch(() => {});
        }
      }
    });
    handle('task_error', (e) => {
      if (!isCurrentStream()) return;
      renderTaskSummary(state.selectedTaskSnapshot);
    });
    handle('task_finished', async (e) => {
      if (!isCurrentStream()) return;
      await refreshTasks();
      if (!isCurrentStream()) return;
      await loadArtifacts({ generation });
      const listedTask = state.tasks.find((t) => t.task_id === taskId);
      const task = listedTask ? mergeSelectedTaskSnapshotFromSummary(listedTask) : state.selectedTaskSnapshot;
      if (task && isCurrentStream()) {
        if (task.kind === 'eval') {
          await loadEvalRuns(task.task_id, { generation });
          if (state.selectedEvalRunDir) {
            await ensureEvalRunMetadata(task, state.selectedEvalRunDir);
            await ensurePerformanceForCurrentSelection(task);
            await loadSelectedEvalRunSnapshot(task, state.selectedEvalRunDir, { generation });
          }
        }
        renderTaskSummary(task);
        ensurePerformanceForCurrentSelection(task).catch(() => {});
      }
    });

    es.onerror = () => {
      // Browser will auto reconnect. No-op.
    };
  }

  function disconnectTaskStream() {
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
  }

  async function stopSelectedTask() {
    if (!state.selectedTaskId) return;
    const task = state.selectedTaskSnapshot;
    if (!task) return;
    const message = task.kind === 'eval'
      ? 'Stop the current eval task? This will terminate the whole eval process.'
      : 'Stop the current task?';
    if (!confirmTrackedAction({
      title: 'Stop task',
      message,
      detail: `Task ID: ${state.selectedTaskId}`,
      cancelMessage: 'Stop cancelled',
    })) return;
    try {
      await runTrackedOperation({
        title: 'Stop task',
        detail: `Task ID: ${state.selectedTaskId}`,
        successMessage: 'Stop request sent',
        successDetail: () => 'Task list refreshed; waiting for the backend to update final status.',
        toastSuccess: true,
        errorMessagePrefix: 'Stop failed',
      }, async () => {
        await api(`/api/tasks/${state.selectedTaskId}/stop`, { method: 'POST', body: '{}' });
        await refreshTasks({ throwOnError: true });
      });
    } catch (err) {
      // Error feedback is already routed to toast.
    }
  }

  async function pauseSelectedEval() {
    const task = state.selectedTaskSnapshot;
    if (!task || task.kind !== 'eval' || !state.selectedTaskId) return;
    if (!confirmTrackedAction({
      title: 'Pause eval',
      message: 'Pause the whole eval after the active sub-run finishes?',
      detail: `Task ID: ${state.selectedTaskId}`,
      cancelMessage: 'Pause cancelled',
    })) return;
    try {
      await runTrackedOperation({
        title: 'Pause eval',
        detail: `Task ID: ${state.selectedTaskId}`,
        successMessage: 'Pause request sent',
        successDetail: () => 'The eval will pause after the active sub-run finishes.',
        toastSuccess: true,
        errorMessagePrefix: 'Pause failed',
      }, async () => {
        await api(`/api/tasks/${state.selectedTaskId}/eval/pause`, { method: 'POST', body: '{}' });
        await refreshTasks({ throwOnError: true });
      });
    } catch (err) {
      // Error feedback is already routed to toast.
    }
  }

  async function resumeSelectedEval() {
    const task = state.selectedTaskSnapshot;
    if (!task || task.kind !== 'eval' || !state.selectedTaskId) return;
    try {
      await runTrackedOperation({
        title: 'Resume eval',
        detail: `Task ID: ${state.selectedTaskId}`,
        successMessage: 'Resume request sent',
        successDetail: () => 'Task list refreshed; waiting for the eval to resume.',
        toastSuccess: true,
        errorMessagePrefix: 'Resume failed',
      }, async () => {
        await api(`/api/tasks/${state.selectedTaskId}/eval/resume`, { method: 'POST', body: '{}' });
        await refreshTasks({ throwOnError: true });
      });
    } catch (err) {
      // Error feedback is already routed to toast.
    }
  }

  function formatProgressLine(obj) {
    if (!obj || typeof obj !== 'object') return '';
    const parts = [];
    for (const key of ['task_index', 'task_total', 'mode', 'repeat_idx', 'source_id', 'flow_id', 'status']) {
      if (obj[key] != null && obj[key] !== '') parts.push(`${key}=${obj[key]}`);
    }
    return parts.join(' ');
  }
