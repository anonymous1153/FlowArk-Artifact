  function taskTitle(task) {
    return getTaskDisplayLabel(task) || String(task?.task_id || '').trim() || 'task';
  }

  function selectedTaskSupportsTagging(task = state.selectedTaskSnapshot) {
    return !!task && !task?.metadata?.draft;
  }

  function taskTagsLabel(tags) {
    return normalizeTaskTags(tags).join(' / ');
  }

  function tagsEqual(a, b) {
    return JSON.stringify(normalizeTaskTags(a)) === JSON.stringify(normalizeTaskTags(b));
  }

  function knownTagCreatedAtMs(task, tag) {
    const metadata = task?.metadata || {};
    const candidateMaps = [
      metadata.tag_created_at,
      metadata.tag_created_at_map,
      metadata.tags_created_at,
      metadata.tagCreatedAt,
      metadata.tagCreatedAtMap,
    ];
    for (const value of candidateMaps) {
      if (!value) continue;
      if (typeof value === 'object' && !Array.isArray(value)) {
        const ts = Date.parse(String(value[tag] || '').trim());
        if (Number.isFinite(ts)) return ts;
      } else {
        const ts = Date.parse(String(value).trim());
        if (Number.isFinite(ts)) return ts;
      }
    }
    return null;
  }

  function tagDatePrefixSortValue(tag) {
    const match = String(tag || '').trim().match(/^(\d{2})(\d{2})(?:[-_\s]|$)/);
    if (!match) return null;
    const month = Number(match[1]);
    const day = Number(match[2]);
    if (!Number.isInteger(month) || !Number.isInteger(day)) return null;
    if (month < 1 || month > 12 || day < 1 || day > 31) return null;
    return month * 100 + day;
  }

  function compareKnownTagEntries(a, b) {
    const createdA = Number.isFinite(a.createdAtMs) ? a.createdAtMs : null;
    const createdB = Number.isFinite(b.createdAtMs) ? b.createdAtMs : null;
    if (createdA !== null || createdB !== null) {
      if (createdA !== null && createdB !== null && createdA !== createdB) return createdB - createdA;
      if (createdA !== null && createdB === null) return -1;
      if (createdA === null && createdB !== null) return 1;
    }
    const prefixA = tagDatePrefixSortValue(a.tag);
    const prefixB = tagDatePrefixSortValue(b.tag);
    if (prefixA !== null || prefixB !== null) {
      if (prefixA !== null && prefixB !== null && prefixA !== prefixB) return prefixB - prefixA;
      if (prefixA !== null && prefixB === null) return -1;
      if (prefixA === null && prefixB !== null) return 1;
    }
    return a.tag.localeCompare(b.tag, 'zh-Hans-CN');
  }

  function listKnownTags(excludeTags = []) {
    const exclude = new Set(normalizeTaskTags(excludeTags));
    const entries = new Map();
    for (const task of (Array.isArray(state.tasks) ? state.tasks : [])) {
      for (const tag of getTaskTags(task)) {
        if (!tag || exclude.has(tag)) continue;
        const current = entries.get(tag) || { tag, createdAtMs: null };
        const createdAtMs = knownTagCreatedAtMs(task, tag);
        if (Number.isFinite(createdAtMs) && (current.createdAtMs === null || createdAtMs < current.createdAtMs)) {
          current.createdAtMs = createdAtMs;
        }
        entries.set(tag, current);
      }
    }
    return Array.from(entries.values()).sort(compareKnownTagEntries).map((entry) => entry.tag);
  }

  function renderExistingTagSelect(selectEl, excludeTags, {
    disabled = false,
    placeholder = 'Select existing tag',
    emptyPlaceholder = 'No existing tags available',
  } = {}) {
    if (!selectEl) return;
    const options = listKnownTags(excludeTags);
    selectEl.innerHTML = '';
    const first = document.createElement('option');
    first.value = '';
    first.textContent = options.length ? placeholder : emptyPlaceholder;
    selectEl.appendChild(first);
    for (const tag of options) {
      const opt = document.createElement('option');
      opt.value = tag;
      opt.textContent = tag;
      selectEl.appendChild(opt);
    }
    selectEl.disabled = !!disabled || !options.length;
    selectEl.value = '';
  }

  function renderTagChipList(container, tags, {
    canEdit = false,
    saving = false,
    emptyText = 'No tags',
    onRemove = null,
  } = {}) {
    if (!container) return;
    container.innerHTML = '';
    const normalizedTags = normalizeTaskTags(tags);
    if (!normalizedTags.length) {
      const empty = document.createElement('div');
      empty.className = 'task-tag-empty muted';
      empty.textContent = emptyText;
      container.appendChild(empty);
      return;
    }
    for (const tag of normalizedTags) {
      const chip = document.createElement('div');
      chip.className = 'task-tag-chip';
      const text = document.createElement('span');
      text.textContent = tag;
      chip.appendChild(text);
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'task-tag-chip-remove';
      btn.textContent = '×';
      btn.title = `Remove tag ${tag}`;
      btn.disabled = !canEdit || !!saving;
      btn.addEventListener('click', () => {
        if (typeof onRemove === 'function') onRemove(tag);
      });
      chip.appendChild(btn);
      container.appendChild(chip);
    }
  }

  function getTagSectionKey(tag) {
    const text = String(tag || '').trim();
    return text ? `tag:${text}` : 'tag:__untagged__';
  }

  function isTagSectionCollapsed(sectionKey) {
    if (Object.prototype.hasOwnProperty.call(state.collapsedTagSections || {}, sectionKey)) {
      return !!state.collapsedTagSections[sectionKey];
    }
    return true;
  }

  function setTagSectionCollapsed(sectionKey, collapsed) {
    state.collapsedTagSections = {
      ...(state.collapsedTagSections || {}),
      [sectionKey]: !!collapsed,
    };
  }

  function setTaskContextOpenClass(taskId) {
    document.querySelectorAll('.task-item.context-open').forEach((node) => node.classList.remove('context-open'));
    const targetId = String(taskId || '').trim();
    if (!targetId) return;
    document.querySelectorAll('.task-item').forEach((node) => {
      if (node?.dataset?.taskId === targetId) node.classList.add('context-open');
    });
  }

  function setTagContextOpenClass(sectionKey) {
    document.querySelectorAll('.task-group-header.context-open').forEach((node) => node.classList.remove('context-open'));
    const targetKey = String(sectionKey || '').trim();
    if (!targetKey) return;
    document.querySelectorAll('.task-group-header').forEach((node) => {
      if (node?.dataset?.tagSectionKey === targetKey) node.classList.add('context-open');
    });
  }

  function closeTaskContextMenu({ rerenderList = false } = {}) {
    const hadOpenState = !!state.taskContextMenuOpen || !!state.taskContextMenuTaskId;
    state.taskContextMenuOpen = false;
    state.taskContextMenuTaskId = null;
    state.taskContextMenuPosition = null;
    renderTaskContextMenu();
    setTaskContextOpenClass(null);
    if (hadOpenState && rerenderList) {
      renderTaskList();
    }
  }

  function closeTagContextMenu({ rerenderList = false } = {}) {
    const hadOpenState = !!state.tagContextMenuOpen || !!state.tagContextMenuTag || !!state.tagContextMenuSectionKey;
    state.tagContextMenuOpen = false;
    state.tagContextMenuTag = null;
    state.tagContextMenuSectionKey = null;
    state.tagContextMenuPosition = null;
    renderTagContextMenu();
    setTagContextOpenClass(null);
    if (hadOpenState && rerenderList) {
      renderTaskList();
    }
  }

  function getContextMenuTask() {
    if (!state.taskContextMenuTaskId) return null;
    return findTaskByIdPreservingSelectedDetail(state.taskContextMenuTaskId);
  }

  function findTaskByIdPreservingSelectedDetail(taskId) {
    const normalizedTaskId = String(taskId || '').trim();
    if (!normalizedTaskId) return null;
    const listedTask = findTaskById(normalizedTaskId);
    if (state.selectedTaskSnapshot?.task_id === normalizedTaskId) {
      const selectedTask = listedTask
        ? mergeTaskDetailWithSummary(state.selectedTaskSnapshot, listedTask)
        : state.selectedTaskSnapshot;
      state.selectedTaskSnapshot = selectedTask;
      return selectedTask;
    }
    return listedTask || null;
  }

  function renderTaskContextMenu() {
    if (!els.taskContextMenu) return;
    const task = getContextMenuTask();
    const open = !!state.taskContextMenuOpen && !!task;
    els.taskContextMenu.classList.toggle('hidden', !open);
    els.taskContextMenu.setAttribute('aria-hidden', open ? 'false' : 'true');
    if (!open) return;

    const canQuickTag = selectedTaskSupportsTagging(task);
    if (els.taskContextMenuQuickTagBtn) {
      els.taskContextMenuQuickTagBtn.disabled = !canQuickTag;
      els.taskContextMenuQuickTagBtn.title = canQuickTag ? 'Edit tags for the current task' : 'Draft tasks do not persist tags';
    }

    const rawX = Math.max(8, Number(state.taskContextMenuPosition?.x) || 0);
    const rawY = Math.max(8, Number(state.taskContextMenuPosition?.y) || 0);
    els.taskContextMenu.style.left = `${rawX}px`;
    els.taskContextMenu.style.top = `${rawY}px`;
    const width = els.taskContextMenu.offsetWidth || 240;
    const height = els.taskContextMenu.offsetHeight || 168;
    const clampedX = Math.min(rawX, Math.max(8, window.innerWidth - width - 8));
    const clampedY = Math.min(rawY, Math.max(8, window.innerHeight - height - 8));
    els.taskContextMenu.style.left = `${clampedX}px`;
    els.taskContextMenu.style.top = `${clampedY}px`;
  }

  function getTagContextMenuTag() {
    return normalizeTaskTags([state.tagContextMenuTag])[0] || '';
  }

  function renderTagContextMenu() {
    if (!els.tagContextMenu) return;
    const tag = getTagContextMenuTag();
    const open = !!state.tagContextMenuOpen && !!tag;
    els.tagContextMenu.classList.toggle('hidden', !open);
    els.tagContextMenu.setAttribute('aria-hidden', open ? 'false' : 'true');
    if (!open) return;

    if (els.tagContextMenuCopyBtn) {
      els.tagContextMenuCopyBtn.disabled = !tag;
      els.tagContextMenuCopyBtn.title = tag ? `Copy tag ${tag}` : 'The untagged group cannot be copied';
    }

    const rawX = Math.max(8, Number(state.tagContextMenuPosition?.x) || 0);
    const rawY = Math.max(8, Number(state.tagContextMenuPosition?.y) || 0);
    els.tagContextMenu.style.left = `${rawX}px`;
    els.tagContextMenu.style.top = `${rawY}px`;
    const width = els.tagContextMenu.offsetWidth || 200;
    const height = els.tagContextMenu.offsetHeight || 52;
    const clampedX = Math.min(rawX, Math.max(8, window.innerWidth - width - 8));
    const clampedY = Math.min(rawY, Math.max(8, window.innerHeight - height - 8));
    els.tagContextMenu.style.left = `${clampedX}px`;
    els.tagContextMenu.style.top = `${clampedY}px`;
  }

  async function openTaskContextMenu(taskId, clientX, clientY) {
    const normalizedTaskId = String(taskId || '').trim();
    if (!normalizedTaskId) return;
    if (state.tagContextMenuOpen) {
      closeTagContextMenu({ rerenderList: false });
    }
    if (state.taskContextMenuOpen) {
      closeTaskContextMenu({ rerenderList: false });
    }
    if (state.selectedTaskId !== normalizedTaskId) {
      try {
        await selectTask(normalizedTaskId);
      } catch (err) {
        showToast(`Failed to open task context menu: ${err.message}`, { variant: 'error' });
        return;
      }
    }
    const task = findTaskByIdPreservingSelectedDetail(normalizedTaskId);
    if (!task) return;
    state.taskContextMenuTaskId = normalizedTaskId;
    state.taskContextMenuPosition = {
      x: Number(clientX) || 0,
      y: Number(clientY) || 0,
    };
    state.taskContextMenuOpen = true;
    setTaskContextOpenClass(normalizedTaskId);
    renderTaskContextMenu();
  }

  function openTagContextMenu(tag, sectionKey, clientX, clientY) {
    const normalizedTag = normalizeTaskTags([tag])[0] || '';
    if (!normalizedTag) return;
    if (state.taskContextMenuOpen) {
      closeTaskContextMenu({ rerenderList: false });
    }
    if (state.tagContextMenuOpen) {
      closeTagContextMenu({ rerenderList: false });
    }
    state.tagContextMenuTag = normalizedTag;
    state.tagContextMenuSectionKey = String(sectionKey || getTagSectionKey(normalizedTag));
    state.tagContextMenuPosition = {
      x: Number(clientX) || 0,
      y: Number(clientY) || 0,
    };
    state.tagContextMenuOpen = true;
    setTagContextOpenClass(state.tagContextMenuSectionKey);
    renderTagContextMenu();
  }

  async function copyTagFromContextMenu() {
    const tag = getTagContextMenuTag();
    if (!tag) return;
    try {
      await copyTextToClipboard(tag);
      showToast('Tag copied', { variant: 'success', durationMs: 1800 });
      closeTagContextMenu();
    } catch (err) {
      showToast(`Failed to copy tag: ${err.message}`, { variant: 'error' });
    }
  }

  function confirmClearTags(task, scopeLabel) {
    return confirmTrackedAction({
      title: `Clear tags for ${scopeLabel}`,
      message: `Clear all tags for ${scopeLabel}?`,
      detail: joinOperationDetails(
        `Task ID: ${task?.task_id || '--'}`,
        `Scope: ${scopeLabel}`,
      ),
      cancelMessage: `Clearing tags for ${scopeLabel} was cancelled`,
    });
  }

  function renderTaskTagEditor(task = state.selectedTaskSnapshot) {
    const canEdit = selectedTaskSupportsTagging(task);
    const currentTags = getTaskTags(task);
    if (!state.taskTagDirty) {
      state.taskTagDrafts = canEdit ? currentTags.slice() : [];
      state.taskTagInput = canEdit ? state.taskTagInput : '';
    }
    if (els.taskTagMeta) {
      if (!task) {
        els.taskTagMeta.textContent = 'Select a task to edit tags';
      } else if (task?.metadata?.draft) {
        els.taskTagMeta.textContent = 'Draft tasks do not persist tags';
      } else {
        const label = taskTitle(task);
        els.taskTagMeta.textContent = currentTags.length
          ? `${label} · current tags: ${taskTagsLabel(currentTags)}`
          : `${label} · currently untagged`;
      }
    }
    if (els.taskTagInput) {
      if (document.activeElement !== els.taskTagInput || !state.taskTagDirty) {
        els.taskTagInput.value = state.taskTagInput;
      }
      els.taskTagInput.disabled = !canEdit || !!state.taskTagSaving;
    }
    const pendingTag = normalizeTaskTags(state.taskTagInput)[0] || '';
    if (els.taskTagAddBtn) {
      els.taskTagAddBtn.textContent = 'Add';
      els.taskTagAddBtn.disabled = !canEdit || !!state.taskTagSaving || !pendingTag;
    }
    if (els.taskTagClearBtn) {
      els.taskTagClearBtn.disabled = !canEdit || !!state.taskTagSaving || (!state.taskTagDrafts.length && !state.taskTagInput.trim());
    }
    renderExistingTagSelect(
      els.taskTagExistingSelect,
      state.taskTagDrafts,
      {
        disabled: !canEdit || !!state.taskTagSaving,
      },
    );
    renderTagChipList(els.taskTagList, state.taskTagDrafts, {
      canEdit,
      saving: state.taskTagSaving,
      emptyText: canEdit ? 'No tags yet. Add a tag first.' : 'No tags',
      onRemove: (tag) => removeDraftTaskTag(tag),
    });
  }

  function syncTaskTagEditor(task = state.selectedTaskSnapshot) {
    state.taskTagDrafts = getTaskTags(task);
    state.taskTagInput = '';
    state.taskTagDirty = false;
    renderTaskTagEditor(task);
  }

  function updateTaskTagsLocally(taskId, tags) {
    const nextTags = normalizeTaskTags(tags);
    const apply = (task) => {
      if (!task || task.task_id !== taskId) return task;
      task.metadata = task.metadata || {};
      delete task.metadata.group;
      if (nextTags.length) task.metadata.tags = nextTags.slice();
      else delete task.metadata.tags;
      return task;
    };
    state.tasks.forEach(apply);
    state.draftTasks.forEach(apply);
    if (state.selectedTaskSnapshot?.task_id === taskId) {
      apply(state.selectedTaskSnapshot);
    }
    state.lastTaskListSignature = '';
    if (state.selectedTaskSnapshot?.task_id === taskId) {
      state.lastTaskSummarySignature = '';
    }
  }

  function syncTaskTagViewsAfterMutation(taskId, tags) {
    updateTaskTagsLocally(taskId, tags);
    const selectedTask = state.selectedTaskSnapshot?.task_id === taskId ? state.selectedTaskSnapshot : null;
    if (selectedTask) {
      syncTaskTagEditor(selectedTask);
      renderTaskSummary(selectedTask);
    } else {
      renderTaskTagEditor(state.selectedTaskSnapshot);
    }
    if (state.quickTagModalTaskId === taskId) {
      state.quickTagDrafts = normalizeTaskTags(tags);
      state.quickTagInput = '';
      renderQuickTagModal();
    }
    renderTaskList();
  }

  async function persistTaskTags(taskId, tags, {
    title = 'Set task tags',
    errorMessagePrefix = 'Failed to set task tags',
  } = {}) {
    const normalizedTags = normalizeTaskTags(tags);
    return runTrackedOperation({
      title,
      detail: joinOperationDetails(
        `Task ID: ${taskId}`,
        normalizedTags.length ? `Tags: ${taskTagsLabel(normalizedTags)}` : 'Tags: untagged',
      ),
      successMessage: normalizedTags.length ? 'Task tags updated' : 'Task tags cleared',
      successDetail: (payload) => joinOperationDetails(
        payload?.persisted_to === 'task_sidecar' ? 'Written to task directory' : 'Written to transient state',
        Array.isArray(payload?.tags) && payload.tags.length ? `Current tags: ${taskTagsLabel(payload.tags)}` : 'Current tags: untagged',
      ),
      toastSuccess: true,
      toastSuccessDurationMs: 1800,
      errorMessagePrefix,
    }, async () => {
      const payload = await api(`/api/tasks/${encodeURIComponent(taskId)}/tags`, {
        method: 'POST',
        body: JSON.stringify({ tags: normalizedTags }),
      });
      syncTaskTagViewsAfterMutation(taskId, payload?.tags || []);
      return payload;
    });
  }

  function addDraftTaskTag(rawTag = state.taskTagInput) {
    const nextTag = normalizeTaskTags(rawTag)[0] || '';
    if (!nextTag) return false;
    const nextDrafts = normalizeTaskTags([...(state.taskTagDrafts || []), nextTag]);
    if (tagsEqual(nextDrafts, state.taskTagDrafts || [])) {
      state.taskTagInput = '';
      renderTaskTagEditor(state.selectedTaskSnapshot);
      return false;
    }
    const task = state.selectedTaskSnapshot;
    if (!selectedTaskSupportsTagging(task)) return false;
    state.taskTagDrafts = nextDrafts;
    state.taskTagInput = '';
    state.taskTagDirty = true;
    state.taskTagSaving = true;
    renderTaskTagEditor(task);
    void persistTaskTags(task.task_id, nextDrafts)
      .then((result) => {
        state.taskTagDrafts = normalizeTaskTags(result?.tags);
        state.taskTagInput = '';
      })
      .catch(() => {
        state.taskTagDrafts = getTaskTags(task);
        state.taskTagInput = '';
      })
      .finally(() => {
        state.taskTagDirty = false;
        state.taskTagSaving = false;
        renderTaskTagEditor(state.selectedTaskSnapshot);
      });
    return true;
  }

  function removeDraftTaskTag(tag) {
    const task = state.selectedTaskSnapshot;
    if (!selectedTaskSupportsTagging(task)) return;
    const tags = normalizeTaskTags(state.taskTagDrafts).filter((item) => item !== tag);
    state.taskTagDrafts = tags;
    state.taskTagDirty = true;
    state.taskTagSaving = true;
    renderTaskTagEditor(task);
    void persistTaskTags(task.task_id, tags)
      .then((result) => {
        state.taskTagDrafts = normalizeTaskTags(result?.tags);
      })
      .catch(() => {
        state.taskTagDrafts = getTaskTags(task);
      })
      .finally(() => {
        state.taskTagDirty = false;
        state.taskTagSaving = false;
        renderTaskTagEditor(state.selectedTaskSnapshot);
      });
  }

  async function clearSelectedTaskTags() {
    const task = state.selectedTaskSnapshot;
    if (!selectedTaskSupportsTagging(task)) return;
    if (!confirmClearTags(task, 'current task')) return;
    const hadPersistedTags = getTaskTags(task).length > 0;
    state.taskTagDrafts = [];
    state.taskTagInput = '';
    if (!hadPersistedTags) {
      state.taskTagDirty = false;
      renderTaskTagEditor(task);
      return;
    }
    state.taskTagDirty = true;
    state.taskTagSaving = true;
    renderTaskTagEditor(task);
    try {
      const result = await persistTaskTags(task.task_id, []);
      state.taskTagDrafts = normalizeTaskTags(result?.tags);
    } catch {
      state.taskTagDrafts = getTaskTags(task);
      state.taskTagInput = '';
    } finally {
      state.taskTagDirty = false;
      state.taskTagSaving = false;
      renderTaskTagEditor(state.selectedTaskSnapshot);
    }
  }

  function renderQuickTagModal() {
    if (!els.quickTagModal) return;
    const task = state.quickTagModalTaskId
      ? findTaskByIdPreservingSelectedDetail(state.quickTagModalTaskId)
      : null;
    const open = !!state.quickTagModalOpen && !!task;
    els.quickTagModal.classList.toggle('hidden', !open);
    els.quickTagModal.setAttribute('aria-hidden', open ? 'false' : 'true');
    if (!open) return;

    const canEdit = selectedTaskSupportsTagging(task);
    const currentTags = getTaskTags(task);
    const pendingTag = normalizeTaskTags(state.quickTagInput)[0] || '';

    if (els.quickTagTaskTitle) {
      els.quickTagTaskTitle.textContent = taskTitle(task);
      els.quickTagTaskTitle.classList.remove('hidden');
    }
    if (els.quickTagTaskMeta) {
      els.quickTagTaskMeta.textContent = currentTags.length
        ? `Task ID: ${task.task_id} · current tags: ${taskTagsLabel(currentTags)}`
        : `Task ID: ${task.task_id} · currently untagged`;
    }
    if (els.quickTagInput) {
      if (document.activeElement !== els.quickTagInput) {
        els.quickTagInput.value = state.quickTagInput;
      }
      els.quickTagInput.disabled = !canEdit || !!state.quickTagSaving;
    }
    if (els.quickTagAddBtn) {
      els.quickTagAddBtn.textContent = 'Add';
      els.quickTagAddBtn.disabled = !canEdit || !!state.quickTagSaving || !pendingTag;
    }
    if (els.quickTagClearBtn) {
      els.quickTagClearBtn.disabled = !canEdit || !!state.quickTagSaving || (!state.quickTagDrafts.length && !state.quickTagInput.trim());
    }
    renderExistingTagSelect(
      els.quickTagExistingSelect,
      state.quickTagDrafts,
      {
        disabled: !canEdit || !!state.quickTagSaving,
      },
    );
    renderTagChipList(els.quickTagList, state.quickTagDrafts, {
      canEdit,
      saving: state.quickTagSaving,
      emptyText: canEdit ? 'No tags yet. Add a tag first.' : 'No tags',
      onRemove: (tag) => removeQuickTagDraft(tag),
    });
  }

  function openQuickTagModal(taskId = state.taskContextMenuTaskId) {
    const task = taskId
      ? findTaskByIdPreservingSelectedDetail(taskId)
      : null;
    if (!selectedTaskSupportsTagging(task)) return;
    closeTaskContextMenu();
    state.quickTagModalTaskId = task.task_id;
    state.quickTagDrafts = getTaskTags(task);
    state.quickTagInput = '';
    state.quickTagSaving = false;
    setQuickTagModalOpen(true);
  }

  function addQuickTagDraft(rawTag = state.quickTagInput) {
    const nextTag = normalizeTaskTags(rawTag)[0] || '';
    if (!nextTag) return false;
    const nextDrafts = normalizeTaskTags([...(state.quickTagDrafts || []), nextTag]);
    if (tagsEqual(nextDrafts, state.quickTagDrafts || [])) {
      state.quickTagInput = '';
      renderQuickTagModal();
      return false;
    }
    const taskId = String(state.quickTagModalTaskId || '').trim();
    const task = taskId
      ? findTaskByIdPreservingSelectedDetail(taskId)
      : null;
    if (!selectedTaskSupportsTagging(task)) return false;
    state.quickTagDrafts = nextDrafts;
    state.quickTagInput = '';
    state.quickTagSaving = true;
    renderQuickTagModal();
    void persistTaskTags(taskId, nextDrafts, {
      title: 'Edit task tags',
      errorMessagePrefix: 'Failed to edit task tags',
    }).then((result) => {
      state.quickTagDrafts = normalizeTaskTags(result?.tags);
      state.quickTagInput = '';
    }).catch(() => {
      state.quickTagDrafts = getTaskTags(task);
      state.quickTagInput = '';
    }).finally(() => {
      state.quickTagSaving = false;
      renderQuickTagModal();
    });
    return true;
  }

  function removeQuickTagDraft(tag) {
    const taskId = String(state.quickTagModalTaskId || '').trim();
    const task = taskId
      ? findTaskByIdPreservingSelectedDetail(taskId)
      : null;
    if (!selectedTaskSupportsTagging(task)) return;
    const nextTags = normalizeTaskTags(state.quickTagDrafts).filter((item) => item !== tag);
    state.quickTagDrafts = nextTags;
    state.quickTagSaving = true;
    renderQuickTagModal();
    void persistTaskTags(taskId, nextTags, {
      title: 'Edit task tags',
      errorMessagePrefix: 'Failed to edit task tags',
    }).then((result) => {
      state.quickTagDrafts = normalizeTaskTags(result?.tags);
    }).catch(() => {
      state.quickTagDrafts = getTaskTags(task);
    }).finally(() => {
      state.quickTagSaving = false;
      renderQuickTagModal();
    });
  }

  async function clearQuickTagDrafts() {
    const taskId = String(state.quickTagModalTaskId || '').trim();
    const task = taskId
      ? findTaskByIdPreservingSelectedDetail(taskId)
      : null;
    if (!selectedTaskSupportsTagging(task)) return;
    if (!confirmClearTags(task, 'task in the quick-edit dialog')) return;
    const hadPersistedTags = getTaskTags(task).length > 0;
    state.quickTagDrafts = [];
    state.quickTagInput = '';
    if (!hadPersistedTags) {
      renderQuickTagModal();
      return;
    }
    state.quickTagSaving = true;
    renderQuickTagModal();
    try {
      const result = await persistTaskTags(taskId, [], {
        title: 'Edit task tags',
        errorMessagePrefix: 'Failed to edit task tags',
      });
      state.quickTagDrafts = normalizeTaskTags(result?.tags);
      state.quickTagInput = '';
      renderQuickTagModal();
      return result;
    } catch {
      state.quickTagDrafts = getTaskTags(task);
      state.quickTagInput = '';
    } finally {
      state.quickTagSaving = false;
      renderQuickTagModal();
    }
  }
