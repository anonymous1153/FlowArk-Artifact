  function setActiveForm(kind) {
    state.activeForm = 'eval';
    if (els.tabEval) els.tabEval.classList.toggle('active', true);
    els.formTitle.textContent = 'Launch eval';
    renderForm();
  }

  function newTask(kind = 'eval') {
    kind = 'eval';
    // Create a local draft task so the user sees it immediately in the list.
    const draft = createDraftTask(kind);
    upsertDraftTask(draft);
    state.tasks = mergeServerTasksWithDrafts(state.tasks.filter((t) => !isDraftTask(t)));
    state.lastTaskListSignature = taskListSignature(state.tasks);
    state.selectedTaskId = draft.task_id;
    state.selectedArtifactPath = null;
    clearEvalRunsState();
    setEvalRunsPaneVisibility(false);
    disconnectTaskStream();
    resetCodePanes();
    renderTaskList();

    // Reset form to defaults
    const schema = state.schemas[kind];
    if (schema) {
      state.formValues[kind] = structuredClone(schema.defaults || {});
      draft.params = structuredClone(state.formValues[kind]);
      upsertDraftTask(draft);
    }

    // Switch to the form
    setActiveForm(kind);

    // Reset buttons
    refreshStopButtonsForTask(null);
    els.reloadArtifactsBtn.disabled = true;
    renderTaskSummary(draft);
    syncTaskTagEditor(draft);
  }

  const EXPERIMENT_PRESET_LABELS = {
    naive: 'Standard opencode',
    flowark_full: 'FlowArk-enabled opencode',
    m1_generic: 'M1 Generic',
    m2_embedding: 'M2 Embedding',
    m3_start_only: 'M3 Start-only',
    mem0_enabled_opencode: 'Mem0-enabled opencode',
    analysis_log_rag: 'Analysis-Log RAG Baseline',
  };

  function normalizeExperimentPreset(value) {
    const normalized = String(value || '').trim().toLowerCase();
    return Object.prototype.hasOwnProperty.call(EXPERIMENT_PRESET_LABELS, normalized)
      ? normalized
      : 'flowark_full';
  }

  function inferExperimentPresetFromValues(kind, values) {
    const explicit = String(values?.experiment_preset || '').trim();
    if (explicit) return normalizeExperimentPreset(explicit);
    const modes = normalizeModeValues(values?.modes || '');
    if (modes.has('naive')) return 'naive';
    const packaging = String(values?.knowledge_packaging_mode || '').trim().toLowerCase();
    if (packaging === 'analysis_log_rag') return 'analysis_log_rag';
    if (packaging === 'embedding') return 'm2_embedding';
    const distill = String(values?.knowledge_distillation_mode || '').trim().toLowerCase();
    if (distill === 'generic') return 'm1_generic';
    const runtime = String(values?.runtime_injection_mode || '').trim().toLowerCase();
    if (runtime === 'start_only') return 'm3_start_only';
    return 'flowark_full';
  }

  function syncExperimentPresetWithValues(kind, values, { force = false } = {}) {
    if (!values || typeof values !== 'object') return;
    if (force || !values.experiment_preset) {
      values.experiment_preset = inferExperimentPresetFromValues(kind, values);
    } else {
      values.experiment_preset = normalizeExperimentPreset(values.experiment_preset);
    }
  }

  function normalizeStudioEvalModeForForm(values) {
    if (!values || typeof values !== 'object' || values.modes == null) return [];
    const modes = Array.from(normalizeModeValues(values.modes));
    if (modes.length <= 1) {
      if (modes.length === 1) values.modes = modes[0];
      return [];
    }
    const selected = modes.includes('flowark') ? 'flowark' : modes[0];
    values.modes = selected;
    return [`The Studio eval form keeps one mode at a time; imported modes=${modes.join(',')} were reduced to ${selected}.`];
  }

  function getPresetSummary(values) {
    const preset = normalizeExperimentPreset(values?.experiment_preset || inferExperimentPresetFromValues(state.activeForm, values));
    return `${EXPERIMENT_PRESET_LABELS[preset] || preset} · standard eval`;
  }

  function handleFormFieldChanged(name, values, { rerender = false } = {}) {
    if (name === 'experiment_preset') {
      values[name] = normalizeExperimentPreset(values[name]);
    }
    if (rerender) {
      renderForm();
      return;
    }
    renderFormConditionalHint();
  }

  function shouldUseEnumPills(field) {
    const name = String(field?.name || '');
    if (name === 'experiment_preset') return true;
    if (name === 'classification_filter') return false;
    const options = Array.isArray(field?.enum) ? field.enum : [];
    const labels = field?.enum_labels && typeof field.enum_labels === 'object' ? field.enum_labels : {};
    if (options.length < 2 || options.length > 6) return false;
    return options.every((opt) => {
      const value = String(opt || '');
      const display = String(labels[value] || value);
      return display.length <= 14;
    });
  }

  function shouldSpanTwoColumns(field) {
    const name = String(field?.name || '');
    const type = String(field?.type || '');
    if (name === 'experiment_preset') return true;
    if (type === 'textarea' || type === 'path' || type === 'multienum') return true;
    if (
      name === 'input_path'
      || name === 'runtime_backend_base_url'
      || name === 'runtime_backend_auth_token'
      || name === 'opencode_model'
      || name === 'out_dir'
      || name === 'app_names'
      || name === 'classification_filter'
    ) return true;
    if (type === 'bool' && String(field?.label || '').length >= 14) return true;
    if (type === 'enum' && !shouldUseEnumPills(field)) return true;
    return false;
  }

  function shouldRenderFieldHelp(field) {
    return !!String(field?.help || '').trim();
  }

  function createFieldHelpTooltip(field) {
    const text = String(field?.help || '').trim();
    if (!text || !shouldRenderFieldHelp(field)) return null;

    const tip = document.createElement('button');
    tip.type = 'button';
    tip.className = 'field-help-tooltip';
    tip.setAttribute('aria-label', `Help: ${text}`);
    tip.addEventListener('mousedown', (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
    });
    tip.addEventListener('click', (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
    });

    const mark = document.createElement('span');
    mark.className = 'field-help-tooltip-mark';
    mark.textContent = '?';
    tip.appendChild(mark);

    const bubble = document.createElement('span');
    bubble.className = 'field-help-tooltip-bubble';
    bubble.setAttribute('aria-hidden', 'true');
    bubble.textContent = text;
    tip.appendChild(bubble);
    return tip;
  }

  function getOrderedFieldsForRender(fields) {
    return Array.isArray(fields) ? fields : [];
  }

  function sanitizeFormValues(kind, rawValues) {
    const schema = state.schemas[kind];
    if (!schema) return {};
    const allowed = new Set((schema.fields || []).map((field) => String(field?.name || '').trim()).filter(Boolean));
    const values = rawValues && typeof rawValues === 'object' ? rawValues : {};
    const sanitized = {};
    for (const [key, value] of Object.entries(values)) {
      if (allowed.has(String(key || '').trim())) {
        sanitized[key] = value;
      }
    }
    return sanitized;
  }

  function renderEnumPills({ field, values, current, multi = false }) {
    const name = field.name;
    const input = document.createElement('div');
    input.className = `enum-pill-group${multi ? ' multi' : ''}`;
    input.dataset.fieldName = name;
    const selected = new Set(
      multi
        ? (Array.isArray(current) ? current.map((x) => String(x)) : [])
        : [String(current ?? '')],
    );
    const syncValue = ({ notify = true } = {}) => {
      if (multi) {
        values[name] = Array.from(selected);
      } else {
        values[name] = selected.size ? Array.from(selected)[0] : '';
      }
      if (notify) handleFormFieldChanged(name, values, { rerender: true });
    };

    const options = Array.isArray(field.enum) ? field.enum : [];
    const labels = field?.enum_labels && typeof field.enum_labels === 'object' ? field.enum_labels : {};
    for (const option of options) {
      const value = String(option);
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'enum-pill';
      btn.setAttribute('data-value', value);
      btn.setAttribute('aria-pressed', selected.has(value) ? 'true' : 'false');
      btn.setAttribute('data-active', selected.has(value) ? '1' : '0');
      btn.textContent = String(labels[value] || value);
      btn.classList.toggle('active', selected.has(value));
      btn.addEventListener('click', () => {
        if (multi) {
          if (selected.has(value)) selected.delete(value);
          else selected.add(value);
        } else {
          selected.clear();
          selected.add(value);
        }
        input.querySelectorAll('.enum-pill').forEach((node) => {
          const nodeVal = node.getAttribute('data-value') || '';
          const isActive = selected.has(nodeVal);
          node.classList.toggle('active', isActive);
          node.setAttribute('aria-pressed', isActive ? 'true' : 'false');
          node.setAttribute('data-active', isActive ? '1' : '0');
        });
        syncValue();
      });
      input.appendChild(btn);
    }
    syncValue({ notify: false });
    return input;
  }

  function getFormSectionKey(kind, section) {
    return `${kind}:${String(section || 'Other')}`;
  }

  function isSectionOpenByDefault(section, fields, values) {
    for (const field of fields) {
      if (field?.default_open === true) return true;
    }
    return false;
  }

  function getFormSectionOpen(kind, section, fields, values) {
    if (!state.formSectionOpen || typeof state.formSectionOpen !== 'object') {
      state.formSectionOpen = {};
    }
    const key = getFormSectionKey(kind, section);
    if (Object.prototype.hasOwnProperty.call(state.formSectionOpen, key)) {
      return !!state.formSectionOpen[key];
    }
    return isSectionOpenByDefault(section, fields, values);
  }

  function shouldSkipFieldForValues(kind, field, values) {
    return false;
  }

  function groupFieldsForRender(kind, fields, values) {
    const groups = [];
    const index = new Map();
    for (const field of fields) {
      if (shouldSkipFieldForValues(kind, field, values)) continue;
      const section = String(field?.section || 'Public eval');
      if (!index.has(section)) {
        const group = { section, fields: [] };
        index.set(section, group);
        groups.push(group);
      }
      index.get(section).fields.push(field);
    }
    return groups;
  }

  function renderFormField(field, values) {
    const wrap = document.createElement('div');
    wrap.className = `form-field ${shouldSpanTwoColumns(field) ? 'field-span-2' : 'field-span-1'}`;
    if (field?.ui_variant === 'preset') {
      wrap.classList.add('experiment-preset-field');
    }

    const name = field.name;
    const type = field.type;
    const current = values[name] ?? field.default ?? '';
    let input;

    if (type !== 'bool') {
      const label = document.createElement('label');
      label.className = 'form-label';
      label.appendChild(document.createTextNode(`${field.label}${field.required ? ' *' : ''}`));
      const helpTip = createFieldHelpTooltip(field);
      if (helpTip) label.appendChild(helpTip);
      wrap.appendChild(label);
    }

    if (type === 'textarea') {
      input = document.createElement('textarea');
      input.value = String(current ?? '');
      input.addEventListener('input', () => { values[name] = input.value; });
    } else if (type === 'enum') {
      if (shouldUseEnumPills(field)) {
        input = renderEnumPills({ field, values, current, multi: false });
        if (name !== 'experiment_preset') {
          wrap.classList.remove('field-span-2');
          wrap.classList.add('field-span-1');
        }
      } else {
        const allowCustom = !!field?.allow_custom;
        const select = document.createElement('select');
        const labels = field?.enum_labels && typeof field.enum_labels === 'object' ? field.enum_labels : {};
        const options = (field.enum || []).map((option) => String(option));
        const currentText = String(current ?? '');
        const customValue = '__custom__';
        if (allowCustom) {
          const opt = document.createElement('option');
          opt.value = customValue;
          opt.textContent = currentText && !options.includes(currentText) ? `${currentText} (current)` : 'Custom...';
          opt.selected = !!currentText && !options.includes(currentText);
          select.appendChild(opt);
        } else if (currentText && !options.includes(currentText)) {
          const opt = document.createElement('option');
          opt.value = currentText;
          opt.textContent = `${currentText} (current)`;
          opt.selected = true;
          select.appendChild(opt);
        }
        for (const option of options) {
          const opt = document.createElement('option');
          opt.value = option;
          opt.textContent = String(labels[String(option)] || option);
          if (currentText === String(option)) opt.selected = true;
          select.appendChild(opt);
        }
        if (!allowCustom) {
          input = select;
          input.addEventListener('change', () => {
            values[name] = input.value;
            handleFormFieldChanged(name, values);
          });
        } else {
          input = document.createElement('div');
          input.className = 'enum-custom-field';
          const customInput = document.createElement('input');
          customInput.type = 'text';
          customInput.placeholder = 'Model id';
          customInput.value = currentText && !options.includes(currentText) ? currentText : '';
          customInput.style.display = select.value === customValue ? '' : 'none';
          const syncCustom = () => {
            values[name] = select.value === customValue ? customInput.value : select.value;
          };
          select.addEventListener('change', () => {
            customInput.style.display = select.value === customValue ? '' : 'none';
            syncCustom();
            handleFormFieldChanged(name, values);
          });
          customInput.addEventListener('input', () => {
            syncCustom();
            renderFormConditionalHint();
          });
          input.appendChild(select);
          input.appendChild(customInput);
        }
      }
    } else if (type === 'multienum') {
      input = renderEnumPills({ field, values, current, multi: true });
    } else if (type === 'bool') {
      wrap.classList.add('bool-field');
      input = document.createElement('div');
      input.className = 'toggle-row';
      const toggleControl = document.createElement('label');
      toggleControl.className = 'toggle-control';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.className = 'toggle-checkbox';
      cb.checked = !!current;
      cb.addEventListener('change', () => {
        values[name] = cb.checked;
        handleFormFieldChanged(name, values);
      });
      toggleControl.appendChild(cb);
      const txt = document.createElement('span');
      txt.className = 'toggle-text';
      txt.textContent = `${field.label}${field.required ? ' *' : ''}`;
      toggleControl.appendChild(txt);
      input.appendChild(toggleControl);
      const helpTip = createFieldHelpTooltip(field);
      if (helpTip) {
        input.classList.add('has-help');
        input.appendChild(helpTip);
      }
      values[name] = !!current;
    } else {
      input = document.createElement('input');
      input.type = type === 'int' ? 'number' : type === 'password' ? 'password' : 'text';
      input.value = current == null ? '' : String(current);
      if (field.placeholder) input.placeholder = field.placeholder;
      input.addEventListener('input', () => {
        if (type === 'int') {
          values[name] = input.value === '' ? '' : Number(input.value);
        } else {
          values[name] = input.value;
        }
        renderFormConditionalHint();
      });
    }

    wrap.appendChild(input);
    return wrap;
  }

  function renderForm() {
    const schema = state.schemas[state.activeForm];
    if (!schema) return;
    const values = state.formValues[state.activeForm] || {};
    els.form.innerHTML = '';
    const orderedFields = getOrderedFieldsForRender(schema.fields || []);
    const groups = groupFieldsForRender(state.activeForm, orderedFields, values);

    for (const group of groups) {
      const details = document.createElement('details');
      details.className = 'form-section';
      details.dataset.section = group.section;
      details.open = getFormSectionOpen(state.activeForm, group.section, group.fields, values);
      details.addEventListener('toggle', () => {
        if (!state.formSectionOpen || typeof state.formSectionOpen !== 'object') {
          state.formSectionOpen = {};
        }
        state.formSectionOpen[getFormSectionKey(state.activeForm, group.section)] = details.open;
      });
      const summary = document.createElement('summary');
      summary.className = 'form-section-summary';
      const title = document.createElement('span');
      title.className = 'form-section-title';
      title.textContent = group.section;
      const meta = document.createElement('span');
      meta.className = 'form-section-meta';
      meta.textContent = `${group.fields.length} ${group.fields.length === 1 ? 'item' : 'items'}`;
      summary.appendChild(title);
      summary.appendChild(meta);
      details.appendChild(summary);
      const body = document.createElement('div');
      body.className = 'form-section-body';
      for (const field of group.fields) {
        body.appendChild(renderFormField(field, values));
      }
      details.appendChild(body);
      els.form.appendChild(details);
    }
    renderFormConditionalHint();
  }

  function renderFormConditionalHint() {
    const values = state.formValues[state.activeForm] || {};
    const presetSummary = getPresetSummary(values);
    els.hint.textContent = `${presetSummary}. Studio will start the eval with the selected experiment preset.`;
  }

  function getNormalizationWarnings(payload) {
    if (!payload || typeof payload !== 'object') return [];
    const warnings = Array.isArray(payload.normalization_warnings)
      ? payload.normalization_warnings
      : payload.parameter_warnings;
    if (!Array.isArray(warnings)) return [];
    return warnings.map((item) => String(item || '').trim()).filter(Boolean);
  }

  function formatNormalizationWarningDetail(payload) {
    const warnings = getNormalizationWarnings(payload);
    if (!warnings.length) return '';
    return ['Parameter normalization warnings:', ...warnings.map((item) => `- ${item}`)].join('\n');
  }

  function normalizeModeValues(value) {
    const rawItems = Array.isArray(value) ? value : String(value || '').split(',');
    const modes = new Set();
    for (const item of rawItems) {
      const text = String(item || '').trim().toLowerCase();
      if (!text) continue;
      modes.add(text === 'native' ? 'naive' : text);
    }
    return modes;
  }

  function resetForm() {
    const schema = state.schemas[state.activeForm];
    if (!schema) return;
    state.formValues[state.activeForm] = structuredClone(schema.defaults || {});
    syncExperimentPresetWithValues(state.activeForm, state.formValues[state.activeForm], { force: true });
    renderForm();
  }

  function validateCurrentFormBeforeSubmit() {
    const kind = state.activeForm;
    const schema = state.schemas[kind];
    if (!schema) return [];
    const values = state.formValues[kind] || {};
    const missing = [];
    for (const field of (schema.fields || [])) {
      if (!field?.required) continue;
      const value = values[field.name];
      const empty = (
        value == null
        || (typeof value === 'string' && value.trim() === '')
        || (Array.isArray(value) && value.length === 0)
      );
      if (empty) missing.push(String(field.label || field.name || '').trim() || String(field.name || ''));
    }
    return missing;
  }

  async function submitForm() {
    const kind = state.activeForm;
    const missingFields = validateCurrentFormBeforeSubmit();
    if (missingFields.length) {
      recordOperationResult({
        title: 'Start eval task',
        status: 'error',
        message: `Missing required fields: ${missingFields.join(', ')}`,
        detail: joinOperationDetails(
          `Missing fields: ${missingFields.join(', ')}`,
        ),
      });
      return;
    }
    const params = structuredClone(sanitizeFormValues(kind, state.formValues[kind] || {}));
    for (const [k, v] of Object.entries(params)) {
      if (typeof v === 'string' && v.trim() === '') delete params[k];
    }
    try {
      const endpoint = '/api/eval';
      await runTrackedOperation({
        title: 'Start eval task',
        detail: joinOperationDetails(
          `Parameter count: ${Object.keys(params).length}`,
        ),
        successStatus: (result) => getNormalizationWarnings(result).length ? 'warning' : 'success',
        successMessage: (result) => getNormalizationWarnings(result).length
          ? 'Eval task submitted with normalized parameters'
          : 'Eval task submitted',
        successDetail: (result) => joinOperationDetails(
          result?.task_id ? `Task ID: ${result.task_id}` : '',
          formatNormalizationWarningDetail(result),
        ),
        toastVariant: (result) => getNormalizationWarnings(result).length ? 'warning' : 'success',
        toastSuccess: true,
        toastSuccessDurationMs: (result) => getNormalizationWarnings(result).length ? 4200 : 2000,
        errorMessagePrefix: 'Start failed',
      }, async () => {
        const data = await api(endpoint, {
          method: 'POST',
          body: JSON.stringify({ params }),
        });
        if (data && data.task_id) {
          if (String(state.selectedTaskId || '').startsWith(`draft-${kind}-`)) {
            removeDraftTask(state.selectedTaskId);
            state.lastTaskListSignature = '';
          }
          await refreshTasks({ background: false, throwOnError: true });
          await selectTask(data.task_id);
        }
        return data;
      });
    } catch (err) {
      // Error feedback is already routed to toast.
    }
  }
