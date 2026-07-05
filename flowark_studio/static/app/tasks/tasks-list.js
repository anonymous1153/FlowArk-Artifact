  async function refreshTasks({ background = false, showFailureAlert = false, trackOperation = false, throwOnError = false } = {}) {
    if (trackOperation) {
      if (state.tasksRefreshInFlight) {
        recordOperationResult({
          title: 'Refresh tasks',
          status: 'cancelled',
          message: 'A refresh is already running; this request was ignored.',
          detail: 'Wait for the current refresh to finish.',
          toastVariant: 'success',
          toastDurationMs: 1800,
        });
        return;
      }
      try {
        return await runTrackedOperation({
          title: 'Refresh tasks',
          detail: 'Fetch the latest tasks and refresh task summaries.',
          successMessage: 'Task list refreshed',
          successDetail: () => `Current task count: ${Array.isArray(state.tasks) ? state.tasks.length : 0}`,
          toastSuccess: true,
          toastSuccessDurationMs: 1800,
          errorMessagePrefix: 'Refresh tasks failed',
        }, () => refreshTasks({ background, showFailureAlert: false, trackOperation: false, throwOnError: true }));
      } catch {
        return;
      }
    }
    if (background && (document.hidden || hasActiveTextSelection())) {
      return;
    }
    if (state.tasksRefreshInFlight) return;
    state.tasksRefreshInFlight = true;
    try {
      const data = await api('/api/tasks?view=summary');
      const merged = mergeServerTasksWithDrafts(Array.isArray(data.tasks) ? data.tasks : []);
      state.tasks = merged;
      const listSig = taskListSignature(merged);
      if (listSig !== state.lastTaskListSignature) {
        state.lastTaskListSignature = listSig;
        renderTaskList();
      }
      if (state.selectedTaskId) {
        const still = findTaskById(state.selectedTaskId);
        if (still) {
          const selected = mergeSelectedTaskSnapshotFromSummary(still);
          renderTaskTagEditor(selected);
          refreshStopButtonsForTask(selected);
          if (selected.kind === 'eval' && state.evalRunsPaneVisible) {
            renderEvalRunsList();
          }
          const sumSig = taskSummarySignature(selected);
          if (sumSig !== state.lastTaskSummarySignature) {
            state.lastTaskSummarySignature = sumSig;
            renderTaskSummary(selected);
          }
        } else {
          disconnectTaskStream();
          state.selectedTaskId = null;
          state.selectedTaskSnapshot = null;
          state.lastTaskSummarySignature = '';
          syncTaskTagEditor(null);
          refreshStopButtonsForTask(null);
          if (els.reloadArtifactsBtn) els.reloadArtifactsBtn.disabled = true;
          refreshTaskLogDirButtons();
          clearEvalRunsState();
          setEvalRunsPaneVisibility(false);
          if (els.artifactList) els.artifactList.innerHTML = '';
          if (els.taskSummary) els.taskSummary.innerHTML = '';
          if (els.taskSummaryMode) {
            els.taskSummaryMode.textContent = '';
            els.taskSummaryMode.classList.add('hidden');
          }
          if (els.taskSummaryCard) els.taskSummaryCard.classList.add('hidden');
          if (els.runSummary) {
            els.runSummary.innerHTML = '<div class="summary-empty muted">Select a task first.</div>';
          }
        }
      } else {
        state.selectedTaskSnapshot = null;
        syncTaskTagEditor(null);
      }
    } catch (err) {
      console.warn('refreshTasks failed', err);
      if (showFailureAlert) {
        showToast(`Refresh tasks failed: ${err.message}`, { variant: 'error' });
      }
      if (throwOnError) throw err;
    } finally {
      state.tasksRefreshInFlight = false;
    }
  }

  function taskStatusCssKey(value) {
    return String(value || 'unknown').trim().toLowerCase().replace(/[^a-z0-9_-]+/g, '-') || 'unknown';
  }

  function getEvalTaskStatusCountItems(task) {
    const counts = task?.metadata?.eval_status_counts;
    if (!counts || typeof counts !== 'object') return [];
    const specs = [
      ['success_count', 'success'],
      ['error_count', 'error'],
    ];
    const items = [];
    for (const [key, label] of specs) {
      const count = Number(counts[key] || 0);
      if (Number.isFinite(count) && count > 0) {
        items.push({ label, count });
      }
    }
    return items;
  }

  function getEvalTaskProgressText(task) {
    const progress = task?.metadata?.eval_progress;
    if (!progress || typeof progress !== 'object') return '';
    const completed = Number(progress.completed_count);
    const total = Number(progress.total_count);
    if (!Number.isFinite(completed) || !Number.isFinite(total) || total <= 0) return '';
    return `${Math.max(0, Math.trunc(completed))}/${Math.max(0, Math.trunc(total))}`;
  }

  function getEvalTaskOpenCodeCostText(task) {
    const cost = task?.metadata?.eval_open_code_cost;
    if (!cost || typeof cost !== 'object') return '';
    const value = Number(cost.completed_end_to_end_cost_usd);
    if (!Number.isFinite(value) || value < 0) return '';
    return `$${value.toFixed(4)}`;
  }

  function getEvalTaskTotalWithMem0CostText(task) {
    const cost = task?.metadata?.eval_open_code_cost;
    if (!cost || typeof cost !== 'object') return '';
    const value = Number(cost.completed_total_with_mem0_cost_usd);
    if (!Number.isFinite(value) || value < 0) return '';
    return `with mem0 $${value.toFixed(4)}`;
  }

  function getEvalTaskOpenCodeAverageCostText(task) {
    const progress = task?.metadata?.eval_progress;
    const cost = task?.metadata?.eval_open_code_cost;
    if (!progress || typeof progress !== 'object') return '';
    if (!cost || typeof cost !== 'object') return '';
    const completed = Math.trunc(Number(progress.completed_count));
    const value = Number(cost.completed_end_to_end_cost_usd);
    if (!Number.isFinite(completed) || completed <= 0) return '';
    if (!Number.isFinite(value) || value < 0) return '';
    return `avg $${(value / completed).toFixed(4)}`;
  }

  function getEvalTaskTotalWithMem0AverageCostText(task) {
    const progress = task?.metadata?.eval_progress;
    const cost = task?.metadata?.eval_open_code_cost;
    if (!progress || typeof progress !== 'object') return '';
    if (!cost || typeof cost !== 'object') return '';
    const completed = Math.trunc(Number(progress.completed_count));
    const value = Number(cost.completed_total_with_mem0_cost_usd);
    if (!Number.isFinite(completed) || completed <= 0) return '';
    if (!Number.isFinite(value) || value < 0) return '';
    return `avg with mem0 $${(value / completed).toFixed(4)}`;
  }

  function buildTaskTagSections(tasks) {
    const buckets = new Map();
    const tagEntries = new Map();
    for (const task of (Array.isArray(tasks) ? tasks : [])) {
      const tags = getTaskTags(task);
      const keys = tags.length ? tags : [''];
      for (const key of keys) {
        if (!buckets.has(key)) buckets.set(key, []);
        buckets.get(key).push(task);
        if (key) {
          const current = tagEntries.get(key) || { tag: key, createdAtMs: null };
          const createdAtMs = knownTagCreatedAtMs(task, key);
          if (Number.isFinite(createdAtMs) && (current.createdAtMs === null || createdAtMs < current.createdAtMs)) {
            current.createdAtMs = createdAtMs;
          }
          tagEntries.set(key, current);
        }
      }
    }
    return Array.from(buckets.entries())
      .sort(([a], [b]) => {
        if (!a && !b) return 0;
        if (!a) return -1;
        if (!b) return 1;
        return compareKnownTagEntries(
          tagEntries.get(a) || { tag: a, createdAtMs: null },
          tagEntries.get(b) || { tag: b, createdAtMs: null },
        );
      })
      .map(([group, items]) => {
        const key = getTagSectionKey(group);
        return {
          key,
          group,
          label: group || 'Untagged',
          items,
          collapsed: isTagSectionCollapsed(key),
        };
      });
  }

  function setAllTagSectionsCollapsed(collapsed) {
    const next = { ...(state.collapsedTagSections || {}) };
    for (const section of buildTaskTagSections(state.tasks)) {
      next[section.key] = !!collapsed;
    }
    state.collapsedTagSections = next;
    renderTaskList();
  }

  function renderTaskListItem(task) {
    const div = document.createElement('div');
    div.className = 'task-item';
    div.dataset.taskId = task.task_id;
    const isEvalTask = String(task.kind || '').trim().toLowerCase() === 'eval';
    if (isEvalTask) div.classList.add('kind-eval');
    if (task.task_id === state.selectedTaskId) div.classList.add('active');
    if (state.taskContextMenuOpen && task.task_id === state.taskContextMenuTaskId) div.classList.add('context-open');
    if (task.metadata?.historical) div.classList.add('historical');
    if (task.metadata?.draft) div.classList.add('draft');
    div.addEventListener('click', () => selectTask(task.task_id));
    div.addEventListener('contextmenu', async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      await openTaskContextMenu(task.task_id, ev.clientX, ev.clientY);
    });

    const titleRow = document.createElement('div');
    titleRow.className = 'task-title-row';
    const title = document.createElement('div');
    title.className = 'title';
    title.textContent = taskTitle(task);
    titleRow.appendChild(title);
    div.appendChild(titleRow);

    const meta = document.createElement('div');
    meta.className = 'meta';

    const formatMetaTime = (value) => {
      const timestamp = String(value || '').trim();
      if (!timestamp) return '';
      const date = new Date(timestamp);
      if (Number.isNaN(date.getTime())) return '';
      const pad = (n) => String(n).padStart(2, '0');
      return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
    };

    const timeLabel = formatMetaTime(task.created_at);
    const metaParts = isEvalTask ? [
      timeLabel,
    ].filter(Boolean) : [
      `${task.task_id}${task.metadata?.historical ? ' [hist]' : ''}${task.metadata?.draft ? ' [draft]' : ''}`,
      getTaskTags(task).length ? `tags:${getTaskTags(task).join('/')}` : '',
      task.created_at || '',
    ].filter(Boolean);
    meta.textContent = metaParts.join(isEvalTask ? ' - ' : ' · ');
    div.appendChild(meta);

    const statusInfo = getTaskStatusInfo(task);
    const status = document.createElement('div');
    status.className = `status ${statusInfo.displayClass}`;
    const progressText = isEvalTask ? getEvalTaskProgressText(task) : '';
    status.textContent = progressText ? `${statusInfo.displayText} ${progressText}` : statusInfo.displayText;
    if (statusInfo.displayTitle) status.title = statusInfo.displayTitle;
    div.appendChild(status);
    if (isEvalTask) {
      const countItems = getEvalTaskStatusCountItems(task);
      const costText = getEvalTaskOpenCodeCostText(task);
      const totalWithMem0CostText = getEvalTaskTotalWithMem0CostText(task);
      const averageCostText = getEvalTaskOpenCodeAverageCostText(task);
      const totalWithMem0AverageCostText = getEvalTaskTotalWithMem0AverageCostText(task);
      if (countItems.length || costText || totalWithMem0CostText || averageCostText || totalWithMem0AverageCostText) {
        const countRow = document.createElement('div');
        countRow.className = 'task-status-count-row';
        for (const item of countItems) {
          const pill = document.createElement('span');
          pill.className = `task-status-count-pill ${taskStatusCssKey(item.label)}`;
          pill.textContent = `${item.label} ${item.count}`;
          countRow.appendChild(pill);
        }
        if (costText) {
          const costPill = document.createElement('span');
          costPill.className = 'task-status-count-pill cost';
          costPill.textContent = costText;
          costPill.title = 'Completed OpenCode end-to-end cost';
          countRow.appendChild(costPill);
        }
        if (totalWithMem0CostText) {
          const mem0CostPill = document.createElement('span');
          mem0CostPill.className = 'task-status-count-pill cost';
          mem0CostPill.textContent = totalWithMem0CostText;
          mem0CostPill.title = 'Completed OpenCode plus tracked Mem0 backend cost';
          countRow.appendChild(mem0CostPill);
        }
        if (averageCostText) {
          const averageCostPill = document.createElement('span');
          averageCostPill.className = 'task-status-count-pill cost';
          averageCostPill.textContent = averageCostText;
          averageCostPill.title = 'Average completed OpenCode end-to-end cost per case';
          countRow.appendChild(averageCostPill);
        }
        if (totalWithMem0AverageCostText) {
          const mem0AverageCostPill = document.createElement('span');
          mem0AverageCostPill.className = 'task-status-count-pill cost';
          mem0AverageCostPill.textContent = totalWithMem0AverageCostText;
          mem0AverageCostPill.title = 'Average completed OpenCode plus tracked Mem0 backend cost per case';
          countRow.appendChild(mem0AverageCostPill);
        }
        div.appendChild(countRow);
      }
    }
    return div;
  }

  function renderTaskList() {
    els.taskList.innerHTML = '';
    const sections = buildTaskTagSections(state.tasks);
    els.taskCount.textContent = `${state.tasks.length} tasks · ${sections.length} tags`;
    if (els.expandAllTagsBtn) els.expandAllTagsBtn.disabled = !sections.length;
    if (els.collapseAllTagsBtn) els.collapseAllTagsBtn.disabled = !sections.length;
    for (const section of sections) {
      const wrap = document.createElement('section');
      wrap.className = 'task-group-section';

      const header = document.createElement('button');
      header.type = 'button';
      header.className = 'task-group-header';
      header.dataset.tagSectionKey = section.key;
      if (state.tagContextMenuOpen && section.key === state.tagContextMenuSectionKey) {
        header.classList.add('context-open');
      }
      header.setAttribute('aria-expanded', section.collapsed ? 'false' : 'true');
      header.addEventListener('click', () => {
        setTagSectionCollapsed(section.key, !section.collapsed);
        renderTaskList();
      });
      if (section.group) {
        header.addEventListener('contextmenu', (ev) => {
          ev.preventDefault();
          ev.stopPropagation();
          openTagContextMenu(section.group, section.key, ev.clientX, ev.clientY);
        });
      }
      const headLeft = document.createElement('div');
      headLeft.className = 'task-group-header-left';
      const caret = document.createElement('span');
      caret.className = 'task-group-caret';
      caret.textContent = '▶';
      headLeft.appendChild(caret);
      const title = document.createElement('div');
      title.className = 'task-group-title';
      title.textContent = section.label;
      headLeft.appendChild(title);
      header.appendChild(headLeft);
      const count = document.createElement('div');
      count.className = 'task-group-count';
      count.textContent = `${section.items.length}`;
      header.appendChild(count);
      wrap.appendChild(header);

      if (!section.collapsed) {
        for (const task of section.items) {
          wrap.appendChild(renderTaskListItem(task));
        }
      }
      els.taskList.appendChild(wrap);
    }
    renderTaskTagEditor(state.selectedTaskSnapshot);
    renderTaskContextMenu();
    renderTagContextMenu();
    renderQuickTagModal();
  }

  function syncTaskParamsToForm(task) {
    if (!task || !task.params) return;
    if (task.kind !== 'eval') return;
    const kind = 'eval';
    const schema = state.schemas[kind];
    if (!schema) return;

    // Switch to the correct form tab
    setActiveForm(kind);

    // Get default values from schema
    const defaults = schema.defaults || {};
    const current = state.formValues[kind] || {};
    const hasExplicitExperimentPreset = Object.prototype.hasOwnProperty.call(task.params, 'experiment_preset')
      && String(task.params.experiment_preset || '').trim();
    // Merge: defaults < task.params, but only for fields that exist in schema
    const fieldNames = new Set((schema.fields || []).map((f) => f.name));
    const merged = { ...defaults };
    for (const [key, value] of Object.entries(task.params)) {
      if (fieldNames.has(key) && value != null) {
        merged[key] = value;
      }
    }
    if (!hasExplicitExperimentPreset && fieldNames.has('experiment_preset')) {
      if (typeof inferExperimentPresetFromValues === 'function') {
        merged.experiment_preset = inferExperimentPresetFromValues(kind, task.params || {});
      } else {
        merged.experiment_preset = defaults.experiment_preset || 'flowark_full';
      }
    }
    if (typeof syncExperimentPresetWithValues === 'function') {
      syncExperimentPresetWithValues(kind, merged, { force: !hasExplicitExperimentPreset });
    }

    state.formValues[kind] = merged;
    renderForm();
  }

  async function loadHistoricalTaskContent(task, { generation = currentTaskSelectionGeneration() } = {}) {
    if (!task?.metadata?.historical) return;
    if (task.kind !== 'eval') return;
    const taskId = String(task.task_id || '').trim();
    const isStillCurrentSelection = () => isCurrentTaskSelection(taskId, generation);
    const paths = task.paths || {};

    // Load raw_transcript.txt for transcript pane
    if (paths.raw_transcript_txt) {
      try {
        const preview = await readArtifactTailAsText(task.task_id, paths.raw_transcript_txt, {
          maxBytes: 512000,
          fromEnd: true,
        });
        if (!isStillCurrentSelection()) return;
        const text = preview.truncatedHead
          ? `[studio:preview] showing the last ${formatBytes(Math.max(0, preview.size - preview.startOffset))} of ${formatBytes(preview.size)} from raw_transcript.txt. Open the artifact for the full file.\n${preview.text}`
          : preview.text;
        state.logBuffers.transcript = text;
        setPanePath('transcript', paths.raw_transcript_txt);
        setCodePaneText('transcript', text, { autoScroll: false });
      } catch {
        if (!isStillCurrentSelection()) return;
      }
    }

    // Load knowledge injection timeline for transcript synthetic cards.
    const runDir = getCurrentRunDirForTask(task);
    const knowledgePath = paths.knowledge_injection_jsonl
      || paths.knowledge_injection_log
      || (runDir ? buildEvalRunFilePath(runDir, 'knowledge_injection.jsonl') : null);
    if (knowledgePath && runDir) {
      try {
        const text = await readArtifactAsText(task.task_id, knowledgePath);
        if (!isStillCurrentSelection()) return;
        upsertKnowledgeInjections(runDir, parseJsonlObjects(text));
      } catch {
        if (!isStillCurrentSelection()) return;
        upsertKnowledgeInjections(runDir, []);
      }
      await ensureKnowledgeSkillContentsForRun(task, runDir);
      if (!isStillCurrentSelection()) return;
      rerenderTranscriptPane({ autoScroll: false });
    }
  }

  function refreshStopButtonsForTask(task) {
    if (els.taskSummaryModalBtn) {
      const hasTask = !!task;
      els.taskSummaryModalBtn.disabled = !hasTask;
      const label = String(task?.task_id || '').trim();
      els.taskSummaryModalBtn.title = hasTask
        ? `View current task details${label ? `\n${label}` : ''}`
        : 'Select a task first.';
      if (!hasTask && state.taskSummaryModalOpen) {
        setTaskSummaryModalOpen(false);
      }
    }
    if (!task) {
      els.stopTaskBtn.disabled = true;
      if (els.pauseEvalBtn) els.pauseEvalBtn.disabled = true;
      if (els.resumeEvalBtn) els.resumeEvalBtn.disabled = true;
      return;
    }
    const normalizedStatus = String(task?.status || '').trim().toLowerCase();
    const hasEvalRoot = !!normalizePathForCompare(task?.paths?.eval_root || task?.paths?.eval_dir || task?.metadata?.eval_dir || '');
    const canStopTask = !task.metadata?.historical && ['running', 'queued', 'starting', 'finishing', 'pausing'].includes(normalizedStatus);
    const canPauseEval = !task.metadata?.historical
      && task.kind === 'eval'
      && hasEvalRoot
      && ['running', 'queued', 'starting', 'finishing'].includes(normalizedStatus);
    const canResumeEval = task.kind === 'eval'
      && normalizedStatus === 'paused';
    els.stopTaskBtn.disabled = !canStopTask;
    if (els.pauseEvalBtn) els.pauseEvalBtn.disabled = !canPauseEval;
    if (els.resumeEvalBtn) els.resumeEvalBtn.disabled = !canResumeEval;
  }

  function selectDraftTask(task) {
    clearEvalRunsState();
    setEvalRunsPaneVisibility(false);
    state.selectedTaskSnapshot = task;
    syncTaskTagEditor(task);
    renderTaskSummary(task);
    syncTaskParamsToForm(task);
    refreshStopButtonsForTask(null);
    els.reloadArtifactsBtn.disabled = true;
    refreshTaskLogDirButtons();
    els.artifactList.innerHTML = '';
    state.lastTaskSummarySignature = taskSummarySignature(task);
  }

  async function selectPersistedTask(taskId, previousTaskId, generation = currentTaskSelectionGeneration()) {
    const localTask = state.tasks.find((item) => item.task_id === taskId) || null;
    const task = isTaskListSummary(localTask)
      ? mergeTaskDetailWithSummary(await api(`/api/tasks/${taskId}`), localTask)
      : (localTask || await api(`/api/tasks/${taskId}`));
    if (!isCurrentTaskSelection(taskId, generation)) return;
    if (task.kind === 'eval') {
      setEvalRunsPaneVisibility(true);
      if (previousTaskId !== taskId) {
        beginEvalTaskDetailLoading(taskId, generation);
      }
    } else {
      clearEvalRunsState();
      setEvalRunsPaneVisibility(false);
    }
    state.selectedTaskSnapshot = task;
    syncTaskTagEditor(task);
    renderTaskSummary(task);
    state.lastTaskSummarySignature = taskSummarySignature(task);
    if (task.kind === 'eval') {
      ensurePerformanceForCurrentSelection(task).catch(() => {});
    }
    syncTaskParamsToForm(task);
    refreshStopButtonsForTask(task);
    els.reloadArtifactsBtn.disabled = task.kind !== 'eval';
    refreshTaskLogDirButtons();

    if (task.kind === 'eval') {
      await loadEvalRuns(task.task_id, { generation });
      if (!isCurrentTaskSelection(taskId, generation)) return;
    }

    if (task.kind === 'eval' && state.selectedEvalRunDir) {
      const selectedRunKey = state.selectedEvalRunDir;
      setEvalRunContentLoading(task.task_id, selectedRunKey);
      void loadArtifacts({ generation }).catch(() => {});
      void ensureEvalRunMetadata(task, selectedRunKey).catch(() => {});
      void ensurePerformanceForCurrentSelection(task).catch(() => {});
      void loadSelectedEvalRunSnapshot(task, selectedRunKey, { generation }).catch(() => {});
    } else if (task.kind === 'eval') {
      await loadArtifacts({ generation });
      await loadCostSummaryPanel({ generation });
    }
    if (!isCurrentTaskSelection(taskId, generation)) return;

    if (task.metadata?.historical) {
      if (!(task.kind === 'eval' && state.selectedEvalRunDir)) {
        await loadHistoricalTaskContent(task, { generation });
      }
    } else if (['running', 'queued', 'starting', 'pausing', 'finishing'].includes(task.status)) {
      connectTaskStream(taskId, generation);
    }
  }

  async function selectTask(taskId) {
    const previousTaskId = state.selectedTaskId;
    const generation = nextTaskSelectionGeneration();
    closeTaskContextMenu({ rerenderList: false });
    state.selectedTaskId = taskId;
    state.selectedArtifactPath = null;
    resetCodePanes();
    renderTaskList();
    disconnectTaskStream();
    const localTask = findTaskById(taskId);
    if (localTask?.kind === 'eval' && previousTaskId !== taskId) {
      setEvalRunsPaneVisibility(true);
      beginEvalTaskDetailLoading(taskId, generation);
    } else if (localTask && localTask.kind !== 'eval') {
      clearEvalRunsState();
      setEvalRunsPaneVisibility(false);
    }
    if (isDraftTask(localTask)) {
      selectDraftTask(localTask);
      return;
    }
    await selectPersistedTask(taskId, previousTaskId, generation);
  }
