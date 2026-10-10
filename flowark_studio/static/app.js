  async function api(path, options = {}) {
    const requestOptions = {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    };
    const method = String(requestOptions.method || 'GET').toUpperCase();
    let res;
    try {
      res = await fetch(path, requestOptions);
    } catch (err) {
      const wrapped = new Error(String(err?.message || 'Network request failed'));
      wrapped.method = method;
      wrapped.path = path;
      wrapped.responseText = '';
      throw wrapped;
    }
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = { raw: text };
    }
    if (!res.ok) {
      const detail = data && (data.detail || data.raw)
        ? (data.detail || data.raw)
        : `${res.status} ${res.statusText}`;
      const err = new Error(String(detail));
      err.status = res.status;
      err.statusText = res.statusText;
      err.method = method;
      err.path = path;
      err.responseData = data;
      err.responseText = text;
      throw err;
    }
    return data;
  }

  const TASK_REFRESH_INTERVAL_MS = 5000;

  async function bootstrap() {
    initTheme();
    initLanguage();
    try {
      const [health, evalSchema] = await Promise.all([
        api('/healthz'),
        api('/api/schema/eval'),
      ]);
      setHealth('ready');
      state.schemas.eval = evalSchema;
      state.formValues.eval = structuredClone(evalSchema.defaults || {});
      state.activeForm = 'eval';
      resetCodePanes();
      renderForm();
      applySidebarLayout();
      await refreshTasks({ background: false });
      setInterval(() => { refreshTasks({ background: true }); }, TASK_REFRESH_INTERVAL_MS);
    } catch (err) {
      setHealth(`error: ${err.message}`, false);
      console.error(err);
    }
  }

  function bindFormAndTaskActions() {
    if (els.tabEval) els.tabEval.addEventListener('click', () => setActiveForm('eval'));
    if (els.newTaskBtn) els.newTaskBtn.addEventListener('click', () => newTask('eval'));
    if (els.transcriptFormatBtn) els.transcriptFormatBtn.addEventListener('click', toggleTranscriptFormat);
    if (els.transcriptPairingBtn) els.transcriptPairingBtn.addEventListener('click', toggleTranscriptPairing);
    if (els.submitFormBtn) els.submitFormBtn.addEventListener('click', submitForm);
    if (els.resetFormBtn) els.resetFormBtn.addEventListener('click', resetForm);
    if (els.refreshTasksBtn) {
      els.refreshTasksBtn.addEventListener('click', () => refreshTasks({
        background: false,
        showFailureAlert: true,
        trackOperation: true,
      }));
    }
    if (els.stopTaskBtn) els.stopTaskBtn.addEventListener('click', stopSelectedTask);
    if (els.pauseEvalBtn) els.pauseEvalBtn.addEventListener('click', pauseSelectedEval);
    if (els.resumeEvalBtn) els.resumeEvalBtn.addEventListener('click', resumeSelectedEval);
    if (els.reloadArtifactsBtn) {
      els.reloadArtifactsBtn.addEventListener('click', () => loadArtifacts({
        showFailureAlert: true,
        trackOperation: true,
      }));
    }
    if (els.toggleLeftBtn) els.toggleLeftBtn.addEventListener('click', () => toggleSidebar('left'));
    if (els.toggleRightBtn) els.toggleRightBtn.addEventListener('click', () => toggleSidebar('right'));
    if (els.languageToggleBtn) els.languageToggleBtn.addEventListener('click', toggleLanguage);
    if (els.themeToggleBtn) els.themeToggleBtn.addEventListener('click', toggleTheme);
  }

  function bindTaskTagActions() {
    if (els.taskTagInput) {
      els.taskTagInput.addEventListener('input', () => {
        state.taskTagInput = els.taskTagInput.value;
        renderTaskTagEditor(state.selectedTaskSnapshot);
      });
      els.taskTagInput.addEventListener('keydown', (ev) => {
        if (ev.key !== 'Enter') return;
        ev.preventDefault();
        addDraftTaskTag();
      });
    }
    if (els.taskTagExistingSelect) {
      els.taskTagExistingSelect.addEventListener('change', () => {
        const tag = String(els.taskTagExistingSelect.value || '').trim();
        if (!tag) return;
        state.taskTagInput = tag;
        renderTaskTagEditor(state.selectedTaskSnapshot);
        els.taskTagInput?.focus();
        els.taskTagInput?.select?.();
        els.taskTagExistingSelect.value = '';
      });
    }
    if (els.expandAllTagsBtn) els.expandAllTagsBtn.addEventListener('click', () => setAllTagSectionsCollapsed(false));
    if (els.collapseAllTagsBtn) els.collapseAllTagsBtn.addEventListener('click', () => setAllTagSectionsCollapsed(true));
    if (els.taskTagAddBtn) {
      els.taskTagAddBtn.addEventListener('click', () => {
        addDraftTaskTag();
      });
    }
    if (els.taskTagClearBtn) els.taskTagClearBtn.addEventListener('click', () => clearSelectedTaskTags());
    if (els.taskContextMenuQuickTagBtn) els.taskContextMenuQuickTagBtn.addEventListener('click', () => openQuickTagModal());
    if (els.tagContextMenuCopyBtn) els.tagContextMenuCopyBtn.addEventListener('click', () => void copyTagFromContextMenu());
    if (els.quickTagInput) {
      els.quickTagInput.addEventListener('input', () => {
        state.quickTagInput = els.quickTagInput.value;
        renderQuickTagModal();
      });
      els.quickTagInput.addEventListener('keydown', (ev) => {
        if (ev.key !== 'Enter') return;
        ev.preventDefault();
        addQuickTagDraft();
      });
    }
    if (els.quickTagExistingSelect) {
      els.quickTagExistingSelect.addEventListener('change', () => {
        const tag = String(els.quickTagExistingSelect.value || '').trim();
        if (!tag) return;
        state.quickTagInput = tag;
        renderQuickTagModal();
        els.quickTagInput?.focus();
        els.quickTagInput?.select?.();
        els.quickTagExistingSelect.value = '';
      });
    }
    if (els.quickTagAddBtn) {
      els.quickTagAddBtn.addEventListener('click', () => {
        addQuickTagDraft();
      });
    }
    if (els.quickTagClearBtn) els.quickTagClearBtn.addEventListener('click', () => clearQuickTagDrafts());
    if (els.quickTagCancelBtn) els.quickTagCancelBtn.addEventListener('click', () => setQuickTagModalOpen(false));
    if (els.closeQuickTagModalBtn) els.closeQuickTagModalBtn.addEventListener('click', () => setQuickTagModalOpen(false));
  }

  function bindStatusPanelActions() {
    if (els.taskSummaryModalBtn) els.taskSummaryModalBtn.addEventListener('click', () => setTaskSummaryModalOpen(true));
  }

  function bindModalAndGlobalActions() {
    if (els.closeTaskSummaryModalBtn) els.closeTaskSummaryModalBtn.addEventListener('click', () => setTaskSummaryModalOpen(false));
    if (els.taskSummaryModal) {
      els.taskSummaryModal.addEventListener('click', (ev) => {
        if (ev.target === els.taskSummaryModal) setTaskSummaryModalOpen(false);
      });
    }
    if (els.quickTagModal) {
      els.quickTagModal.addEventListener('click', (ev) => {
        if (ev.target === els.quickTagModal) setQuickTagModalOpen(false);
      });
    }
    document.addEventListener('pointerdown', (ev) => {
      if (state.taskContextMenuOpen && els.taskContextMenu && !els.taskContextMenu.contains(ev.target)) {
        closeTaskContextMenu();
      }
      if (state.tagContextMenuOpen && els.tagContextMenu && !els.tagContextMenu.contains(ev.target)) {
        closeTagContextMenu();
      }
    });
    document.addEventListener('contextmenu', (ev) => {
      const target = ev.target;
      if (state.taskContextMenuOpen) {
        if (els.taskContextMenu?.contains(target)) return;
        if (target?.closest?.('.task-item')) return;
        closeTaskContextMenu();
      }
      if (state.tagContextMenuOpen) {
        if (els.tagContextMenu?.contains(target)) return;
        if (target?.closest?.('.task-group-header')) return;
        closeTagContextMenu();
      }
    });
    document.addEventListener('keydown', (ev) => {
      if (ev.key !== 'Escape') return;
      if (state.taskContextMenuOpen) {
        closeTaskContextMenu();
        return;
      }
      if (state.tagContextMenuOpen) {
        closeTagContextMenu();
        return;
      }
      if (state.quickTagModalOpen) {
        setQuickTagModalOpen(false);
      }
    });
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) return;
      refreshTasks({ background: true });
    });
    if (els.taskList) {
      els.taskList.addEventListener('scroll', () => {
        closeTaskContextMenu();
        closeTagContextMenu();
      }, { passive: true });
    }
    if (els.sidebar) {
      els.sidebar.addEventListener('scroll', () => {
        closeTaskContextMenu();
        closeTagContextMenu();
      }, { passive: true });
    }
    window.addEventListener('resize', () => requestAnimationFrame(() => {
      closeTaskContextMenu();
      closeTagContextMenu();
    }));
  }

  bindFormAndTaskActions();
  bindTaskTagActions();
  bindStatusPanelActions();
  bindModalAndGlobalActions();

  bindCodePaneBehavior();
  bindResizers();
  applyResizableLayoutState();
  bootstrap();
