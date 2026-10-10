  function syncModalBodyState() {
    document.body.classList.toggle(
      'modal-open',
      !!(
        state.taskSummaryModalOpen
        || state.quickTagModalOpen
      ),
    );
  }

  function setTaskSummaryModalOpen(open) {
    state.taskSummaryModalOpen = !!open;
    if (els.taskSummaryModal) {
      els.taskSummaryModal.classList.toggle('hidden', !state.taskSummaryModalOpen);
      els.taskSummaryModal.setAttribute('aria-hidden', state.taskSummaryModalOpen ? 'false' : 'true');
    }
    syncModalBodyState();
    if (state.taskSummaryModalOpen) {
      setTimeout(() => els.closeTaskSummaryModalBtn?.focus(), 0);
    }
  }

  function setQuickTagModalOpen(open) {
    state.quickTagModalOpen = !!open;
    if (els.quickTagModal) {
      els.quickTagModal.classList.toggle('hidden', !state.quickTagModalOpen);
      els.quickTagModal.setAttribute('aria-hidden', state.quickTagModalOpen ? 'false' : 'true');
    }
    syncModalBodyState();
    if (state.quickTagModalOpen) {
      renderQuickTagModal();
      setTimeout(() => els.quickTagInput?.focus(), 0);
      return;
    }
    state.quickTagModalTaskId = null;
    state.quickTagDrafts = [];
    state.quickTagInput = '';
    state.quickTagSaving = false;
    renderQuickTagModal();
  }
