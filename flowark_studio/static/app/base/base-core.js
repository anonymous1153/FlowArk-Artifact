  function setHealth(text, ok = true) {
    els.health.textContent = text;
    els.health.style.color = ok ? '#0c6b58' : '#b13a2d';
  }

  let toastSeq = 0;

  function showToast(message, { variant = 'error', durationMs = 3200 } = {}) {
    if (!els.toastContainer) return;
    const toast = document.createElement('div');
    toast.className = `toast-item ${variant}`;
    toast.textContent = String(message || '');
    toast.dataset.toastId = `toast-${++toastSeq}`;
    els.toastContainer.appendChild(toast);
    requestAnimationFrame(() => toast.classList.add('show'));
    const removeToast = () => {
      toast.classList.remove('show');
      setTimeout(() => toast.remove(), 180);
    };
    setTimeout(removeToast, Math.max(1200, Number(durationMs) || 3200));
  }

  async function copyTextToClipboard(text) {
    const value = String(text || '');
    if (!value) throw new Error('Nothing to copy');
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return;
    }
    const textarea = document.createElement('textarea');
    textarea.value = value;
    textarea.setAttribute('readonly', 'readonly');
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    textarea.style.pointerEvents = 'none';
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    let copied = false;
    try {
      copied = !!document.execCommand('copy');
    } finally {
      textarea.remove();
    }
    if (!copied) {
      throw new Error('This browser cannot copy to the clipboard');
    }
  }

  function resolveOperationText(value, ...args) {
    if (typeof value === 'function') return value(...args);
    return value;
  }

  function compactOperationText(value) {
    return String(value || '').trim();
  }

  function joinOperationDetails(...parts) {
    return parts
      .map((part) => compactOperationText(part))
      .filter(Boolean)
      .join('\n\n');
  }

  function buildOperationErrorDetail(err, { fallbackMessage = 'Operation failed' } = {}) {
    const lines = [];
    const message = compactOperationText(err?.message) || fallbackMessage;
    if (err?.method || err?.path) {
      lines.push(`Request: ${compactOperationText(err?.method) || 'GET'} ${compactOperationText(err?.path) || ''}`.trim());
    }
    if (err?.status || err?.statusText) {
      lines.push(`HTTP: ${compactOperationText(err?.status)} ${compactOperationText(err?.statusText)}`.trim());
    }
    lines.push(`Error: ${message}`);
    if (err?.responseData && typeof err.responseData === 'object') {
      try {
        lines.push(`Response:\n${JSON.stringify(err.responseData, null, 2)}`);
      } catch {
        // Ignore non-serializable data.
      }
    } else if (compactOperationText(err?.responseText) && compactOperationText(err?.responseText) !== message) {
      lines.push(`Response: ${compactOperationText(err.responseText)}`);
    }
    return {
      message,
      detail: lines.join('\n\n'),
    };
  }

  function recordOperationResult({
    title,
    status = 'error',
    message = '',
    detail = '',
    toastVariant = 'error',
    toastDurationMs = 3200,
    showToastMessage = true,
  }) {
    if (showToastMessage && compactOperationText(message)) {
      showToast(message, { variant: toastVariant, durationMs: toastDurationMs });
    }
    return null;
  }

  async function runTrackedOperation(config, action) {
    const title = compactOperationText(config?.title) || 'Untitled operation';
    try {
      const result = await action(null);
      const successMessage = compactOperationText(resolveOperationText(config?.successMessage, result)) || 'Operation succeeded';
      if (config?.toastSuccess) {
        const toastVariant = compactOperationText(resolveOperationText(config?.toastVariant, result)) || 'success';
        const toastDurationMs = Number(resolveOperationText(config?.toastSuccessDurationMs, result)) || 2200;
        showToast(successMessage, {
          variant: toastVariant,
          durationMs: toastDurationMs,
        });
      }
      return result;
    } catch (err) {
      const errorInfo = buildOperationErrorDetail(err, { fallbackMessage: `${title} failed` });
      const prefix = compactOperationText(resolveOperationText(config?.errorMessagePrefix, err));
      const errorMessage = prefix ? `${prefix}: ${errorInfo.message}` : errorInfo.message;
      if (config?.toastError !== false) {
        showToast(errorMessage, {
          variant: 'error',
          durationMs: Number(config?.toastErrorDurationMs) || 3200,
        });
      }
      throw err;
    }
  }

  function confirmTrackedAction({ title, message, detail = '', cancelMessage = 'Operation cancelled by user' }) {
    const confirmed = window.confirm(String(message || title || 'Continue?'));
    if (!confirmed) {
      recordOperationResult({
        title,
        status: 'cancelled',
        message: cancelMessage,
        detail: joinOperationDetails(detail, `Confirmation prompt: ${message}`),
        toastVariant: 'success',
        toastDurationMs: 1800,
        showToastMessage: false,
      });
    }
    return confirmed;
  }
