  function applySidebarLayout() {
    els.layout.classList.toggle('hide-left', !state.sidebarVisible);
    els.layout.classList.toggle('hide-right', !state.configVisible);
    els.layout.classList.toggle('show-eval-runs', !!state.evalRunsPaneVisible);
    if (els.evalRunsPane) {
      els.evalRunsPane.classList.toggle('hidden', !state.evalRunsPaneVisible);
    }
    els.toggleLeftBtn.textContent = state.sidebarVisible ? '◧ Hide tasks' : '◧ Show tasks';
    els.toggleRightBtn.textContent = state.configVisible ? 'Hide form ◨' : 'Show form ◨';
  }

  function toggleSidebar(which) {
    if (which === 'left') {
      state.sidebarVisible = !state.sidebarVisible;
    } else {
      state.configVisible = !state.configVisible;
    }
    applySidebarLayout();
  }

  function toggleTheme() {
    const html = document.documentElement;
    const currentTheme = html.getAttribute('data-theme');
    const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
    html.setAttribute('data-theme', newTheme);
    localStorage.setItem('flowark-studio-theme', newTheme);
    updateThemeButtonText();
  }

  function updateThemeButtonText() {
    if (els.themeToggleBtn) {
      const isDark = document.documentElement.getAttribute('data-theme') !== 'light';
      els.themeToggleBtn.textContent = isDark ? 'Light' : 'Dark';
    }
  }

  function initTheme() {
    const saved = localStorage.getItem('flowark-studio-theme');
    if (saved) {
      document.documentElement.setAttribute('data-theme', saved);
    }
    updateThemeButtonText();
  }

  function toggleWrap(pane) {
    const next = !state.paneWrap[pane];
    setCodePaneWrap(pane, next);
  }

  function exitFullscreenPanel() {
    if (!state.activeFullscreenPanelId) return;
    const previousPanelId = state.activeFullscreenPanelId;
    const current = document.getElementById(previousPanelId);
    if (current) current.classList.remove('is-fullscreen');
    const btn = document.querySelector(`[data-action="fullscreen"][data-panel="${previousPanelId}"]`);
    if (btn) btn.textContent = 'Fullscreen';
    state.activeFullscreenPanelId = null;
    document.body.classList.remove('has-fullscreen-panel');
    if (previousPanelId === 'transcript-panel' && state.logBuffers.transcript != null) {
      setCodePaneText('transcript', state.logBuffers.transcript, { autoScroll: false });
    }
    refreshTranscriptFormatButtonState();
  }

  function toggleFullscreenPanel(panelId) {
    const panel = document.getElementById(panelId);
    if (!panel) return;
    if (state.activeFullscreenPanelId === panelId) {
      exitFullscreenPanel();
      return;
    }
    exitFullscreenPanel();
    panel.classList.add('is-fullscreen');
    state.activeFullscreenPanelId = panelId;
    document.body.classList.add('has-fullscreen-panel');
    const btn = document.querySelector(`[data-action="fullscreen"][data-panel="${panelId}"]`);
    if (btn) btn.textContent = 'Exit fullscreen';
    if (panelId === 'transcript-panel' && state.logBuffers.transcript != null) {
      setCodePaneText('transcript', state.logBuffers.transcript, { autoScroll: false });
    }
    refreshTranscriptFormatButtonState();
  }

  function bindCodePaneBehavior() {
    for (const pane of CODE_PANES) {
      setCodePaneWrap(pane, true);
    }

    document.body.addEventListener('click', (ev) => {
      const target = ev.target instanceof HTMLElement ? ev.target : null;
      if (!target) return;

      // Handle button clicks with data-action
      const btn = target.closest('button[data-action]');
      if (btn) {
        const action = btn.getAttribute('data-action');
        if (action === 'wrap') {
          const pane = btn.getAttribute('data-pane');
          if (pane) toggleWrap(pane);
        } else if (action === 'fullscreen') {
          const panelId = btn.getAttribute('data-panel');
          if (panelId) toggleFullscreenPanel(panelId);
        }
        return;
      }

      // Handle log-titlebar clicks (fullscreen toggle for log panels)
      const titlebar = target.closest('.log-titlebar');
      if (titlebar) {
        // Ignore clicks on buttons or log-tools area
        if (target.closest('button') || target.closest('.log-tools')) return;
        const panel = titlebar.closest('.log-panel');
        if (panel && panel.id) {
          toggleFullscreenPanel(panel.id);
        }
        return;
      }

      // Handle task-summary-card panel-header clicks (expand/collapse)
      const panelHeader = target.closest('.task-summary-card > .panel-header');
      if (panelHeader) {
        // Ignore clicks on buttons
        if (target.closest('button')) return;
        const card = panelHeader.closest('.task-summary-card');
        if (card && card.id === 'task-summary-card') {
          toggleTaskSummaryPanel();
        } else if (card && card.id === 'run-summary-card') {
          toggleRunSummaryPanel();
        }
        return;
      }
    });

    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape') exitFullscreenPanel();
      if (ev.key === 'Escape' && state.taskSummaryModalOpen) setTaskSummaryModalOpen(false);
    });
    refreshTranscriptFormatButtonState();
  }
