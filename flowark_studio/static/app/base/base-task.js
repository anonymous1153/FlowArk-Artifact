  function nowIsoString() {
    return new Date().toISOString();
  }

  function isDraftTask(task) {
    return !!(task && task.metadata && task.metadata.draft);
  }

  function createDraftTask(kind = 'eval') {
    const schema = state.schemas[kind];
    const defaults = structuredClone(schema?.defaults || {});
    const taskId = `draft-${kind}-${Date.now().toString(36)}`;
    return {
      task_id: taskId,
      kind,
      status: 'draft',
      created_at: nowIsoString(),
      params: defaults,
      metadata: {
        draft: true,
      },
      paths: {},
    };
  }

  function upsertDraftTask(task) {
    if (!task || !task.task_id) return;
    const idx = state.draftTasks.findIndex((t) => t.task_id === task.task_id);
    if (idx >= 0) state.draftTasks[idx] = task;
    else state.draftTasks.unshift(task);
  }

  function removeDraftTask(taskId) {
    if (!taskId) return;
    state.draftTasks = state.draftTasks.filter((t) => t.task_id !== taskId);
  }

  function findTaskById(taskId) {
    if (!taskId) return null;
    return state.tasks.find((t) => t.task_id === taskId)
      || state.draftTasks.find((t) => t.task_id === taskId)
      || null;
  }

  function normalizeEvalModes(value) {
    const rawItems = Array.isArray(value) ? value : String(value || '').split(',');
    const out = [];
    const seen = new Set();
    for (const item of rawItems) {
      let text = String(item || '').trim().toLowerCase();
      if (!text) continue;
      if (text === 'native') text = 'naive';
      if (seen.has(text)) continue;
      seen.add(text);
      out.push(text);
    }
    return out;
  }

  function inferTaskExperimentPreset(kind, params) {
    const p = params || {};
    const explicit = String(p.experiment_preset || '').trim();
    if (explicit) return normalizePublicExperimentPreset(explicit);
    const mode = normalizeEvalModes(p.modes).includes('flowark') ? 'flowark' : 'naive';
    if (mode === 'naive') return 'naive';
    const adapter = String(p.agent_adapter || 'opencode').trim().toLowerCase().replace('-', '_');
    const knowledge = String(p.knowledge_mode || 'warm').trim().toLowerCase();
    const cycleRaw = p.auto_knowledge_cycle;
    const cycle = cycleRaw == null
      ? true
      : !['0', 'false', 'off', 'no'].includes(String(cycleRaw).trim().toLowerCase());
    const runtime = String(p.runtime_injection_mode || 'context_aware').trim().toLowerCase();
    const distill = String(p.knowledge_distillation_mode || 'with_selection_rules').trim().toLowerCase();
    const packaging = String(p.knowledge_packaging_mode || 'dsl_rule').trim().toLowerCase();
    const validate = String(p.auto_knowledge_validate_mode || 'static').trim().toLowerCase();
    const digest = String(p.knowledge_reuse_digest_mode || 'live_corridor_v2').trim().toLowerCase();
    if (adapter !== 'opencode' || knowledge !== 'warm') return 'flowark_full';
    if (distill === 'with_selection_rules' && packaging === 'dsl_rule' && runtime === 'context_aware' && validate === 'static' && digest === 'live_corridor_v2') return 'flowark_full';
    if (distill === 'generic' && packaging === 'dsl_rule' && runtime === 'context_aware' && validate === 'off' && digest === 'off') return 'm1_generic';
    if (distill === 'with_selection_rules' && packaging === 'embedding' && runtime === 'context_aware' && validate === 'off' && digest === 'live_corridor_v2') return 'm2_embedding';
    if (distill === 'with_selection_rules' && packaging === 'dsl_rule' && runtime === 'start_only' && validate === 'static' && digest === 'live_corridor_v2') return 'm3_start_only';
    if (distill === 'with_selection_rules' && packaging === 'analysis_log_rag' && runtime === 'context_aware' && !cycle && validate === 'off' && digest === 'off') return 'analysis_log_rag';
    if (distill === 'with_selection_rules' && packaging === 'analysis_log_rag_initial' && runtime === 'start_only' && !cycle && validate === 'off' && digest === 'off') return 'analysis_log_rag';
    return 'flowark_full';
  }

  function normalizePublicExperimentPreset(value) {
    const normalized = String(value || '').trim().toLowerCase();
    const allowed = {
      naive: true,
      flowark_full: true,
      m1_generic: true,
      m2_embedding: true,
      m3_start_only: true,
      mem0_enabled_opencode: true,
      analysis_log_rag: true,
    };
    if (allowed[normalized]) return normalized;
    if (normalized === 'analysis_log_rag_initial') return 'analysis_log_rag';
    return 'flowark_full';
  }

  function formatTaskExperimentPreset(value) {
    const labels = {
      naive: 'Standard opencode',
      flowark_full: 'FlowArk-enabled opencode',
      m1_generic: 'M1 Generic',
      m2_embedding: 'M2 Embedding',
      m3_start_only: 'M3 Start-only',
      mem0_enabled_opencode: 'Mem0-enabled opencode',
      analysis_log_rag: 'Analysis-Log RAG Baseline',
    };
    const normalized = normalizePublicExperimentPreset(value);
    return labels[normalized] || labels.flowark_full;
  }

  function getTaskDisplayLabel(task) {
    if (!task) return '';
    const kind = String(task.kind || '').trim();
    const params = task.params || {};
    if (kind === 'eval') {
      const modes = normalizeEvalModes(params.modes);
      const modeLabel = modes.join('+');
      const presetLabel = formatTaskExperimentPreset(inferTaskExperimentPreset(kind, params));
      const parts = ['eval', presetLabel, modeLabel];
      if (!parts[parts.length - 1]) parts.pop();
      if (!parts.length) return task.task_id ? `eval ${task.task_id}` : 'eval';
      return parts.join(' · ');
    }
    return task.task_id ? `${kind || 'task'} ${task.task_id}` : (kind || 'task');
  }

  function normalizeTaskTags(values) {
    const rawValues = Array.isArray(values) ? values : (values == null ? [] : [values]);
    const next = [];
    const seen = new Set();
    for (const item of rawValues) {
      const text = String(item || '').trim();
      if (!text || seen.has(text)) continue;
      next.push(text);
      seen.add(text);
    }
    return next;
  }

  function getTaskTags(task) {
    const normalized = normalizeTaskTags(task?.metadata?.tags);
    if (normalized.length) return normalized;
    const legacy = String(task?.metadata?.group || '').trim();
    return legacy ? [legacy] : [];
  }

  function getTaskGroupName(task) {
    const tags = getTaskTags(task);
    return tags.length === 1 ? tags[0] : '';
  }

  function getDispatchModeLabel(_task) {
    return '';
  }

  function getQueueStateLabel(_task) {
    return '';
  }

  function getTaskStatusInfo(task) {
    const rawStatus = String(task?.status || 'unknown').trim().toLowerCase() || 'unknown';
    const pauseConfirmed = !!task?.metadata?.pause_confirmed;
    if (String(task?.kind || '').trim().toLowerCase() === 'eval' && rawStatus === 'warning') {
      return {
        rawStatus,
        displayText: 'completed',
        displayClass: 'success',
      };
    }
    if (rawStatus === 'pausing') {
      if (pauseConfirmed) {
        return {
          rawStatus,
          displayText: 'paused-finalizing',
          displayClass: 'paused',
        };
      }
      return {
        rawStatus,
        displayText: 'pausing',
        displayClass: 'pausing',
      };
    }
    return {
      rawStatus,
      displayText: rawStatus,
      displayClass: rawStatus,
    };
  }

  function mergeServerTasksWithDrafts(serverTasks) {
    const list = Array.isArray(serverTasks) ? serverTasks : [];
    const serverIds = new Set(list.map((t) => t.task_id));
    state.draftTasks = state.draftTasks.filter((t) => !serverIds.has(t.task_id));
    return [...state.draftTasks, ...list];
  }

  function isTaskListSummary(task) {
    return !!task?.metadata?._list_summary;
  }

  const TASK_LIST_METADATA_KEYS = [
    'historical',
    'draft',
    'tags',
    'group',
    'run_dir',
    'eval_dir',
    'out_dir',
    'dispatch_mode',
    'queue_waiting',
    'queue_position',
    'pause_requested',
    'pause_confirmed',
    'pause_mode',
    'pause_mode_requested',
    'pause_reason',
    'eval_status_counts',
    'eval_progress',
    'eval_open_code_cost',
  ];

  function mergeTaskDetailWithSummary(detail, summary) {
    if (!detail || typeof detail !== 'object') return summary;
    if (!summary || typeof summary !== 'object') return detail;
    const summaryMetadata = summary.metadata || {};
    const metadata = isTaskListSummary(summary)
      ? { ...(detail.metadata || {}) }
      : { ...(detail.metadata || {}), ...summaryMetadata };
    if (isTaskListSummary(summary)) {
      for (const key of TASK_LIST_METADATA_KEYS) {
        delete metadata[key];
      }
      for (const [key, value] of Object.entries(summaryMetadata)) {
        if (key !== '_list_summary') metadata[key] = value;
      }
    }
    delete metadata._list_summary;
    return {
      ...detail,
      status: summary.status || detail.status,
      started_at: summary.started_at ?? detail.started_at,
      finished_at: summary.finished_at ?? detail.finished_at,
      pid: summary.pid ?? detail.pid,
      return_code: summary.return_code ?? detail.return_code,
      error: summary.error ?? detail.error,
      last_seq: summary.last_seq ?? detail.last_seq,
      paths: {
        ...(detail.paths || {}),
        ...(summary.paths || {}),
      },
      metadata,
    };
  }

  function mergeSelectedTaskSnapshotFromSummary(summary) {
    const current = state.selectedTaskSnapshot;
    const selected = current?.task_id === summary?.task_id
      ? mergeTaskDetailWithSummary(current, summary)
      : summary;
    state.selectedTaskSnapshot = selected;
    return selected;
  }

  function taskListSignature(tasks) {
    const compact = (Array.isArray(tasks) ? tasks : []).map((t) => ({
      id: t.task_id,
      kind: t.kind,
      status: t.status,
      created_at: t.created_at,
      historical: !!t?.metadata?.historical,
      draft: !!t?.metadata?.draft,
      tags: getTaskTags(t),
      dispatch_mode: t?.metadata?.dispatch_mode || '',
      queue_waiting: !!t?.metadata?.queue_waiting,
      queue_position: t?.metadata?.queue_position ?? '',
      pause_requested: !!t?.metadata?.pause_requested,
      pause_confirmed: !!t?.metadata?.pause_confirmed,
      pause_mode: t?.metadata?.pause_mode || '',
      pause_mode_requested: t?.metadata?.pause_mode_requested || '',
      pause_reason: t?.metadata?.pause_reason || '',
      eval_status_counts: t?.metadata?.eval_status_counts || null,
      eval_progress: t?.metadata?.eval_progress || null,
      eval_open_code_cost: t?.metadata?.eval_open_code_cost || null,
      input_path: t?.params?.input_path || t?.params?.input || '',
      modes: Array.isArray(t?.params?.modes) ? t.params.modes.join(',') : (t?.params?.modes || ''),
      knowledge_mode: t?.params?.knowledge_mode || '',
      runtime_injection_mode: t?.params?.runtime_injection_mode || '',
      knowledge_distillation_mode: t?.params?.knowledge_distillation_mode || '',
      knowledge_packaging_mode: t?.params?.knowledge_packaging_mode || '',
      auto_knowledge_validate_mode: t?.params?.auto_knowledge_validate_mode || '',
      knowledge_reuse_digest_mode: t?.params?.knowledge_reuse_digest_mode || '',
      parallel: t?.params?.parallel ?? '',
    }));
    try { return JSON.stringify(compact); } catch { return String(compact.length); }
  }

  function taskSummarySignature(task) {
    if (!task) return '';
    const payload = {
      task_id: task.task_id,
      kind: task.kind,
      status: task.status,
      created_at: task.created_at,
      started_at: task.started_at,
      finished_at: task.finished_at,
      pid: task.pid,
      return_code: task.return_code,
      metadata: {
        draft: !!task?.metadata?.draft,
        historical: !!task?.metadata?.historical,
        tags: getTaskTags(task),
        dispatch_mode: task?.metadata?.dispatch_mode || '',
        queue_waiting: !!task?.metadata?.queue_waiting,
        queue_position: task?.metadata?.queue_position ?? '',
        eval_status_counts: task?.metadata?.eval_status_counts || null,
        eval_progress: task?.metadata?.eval_progress || null,
        eval_open_code_cost: task?.metadata?.eval_open_code_cost || null,
      },
      paths: task.paths || {},
    };
    try { return JSON.stringify(payload); } catch { return `${task.task_id}:${task.status}`; }
  }

  function normalizePathForCompare(path) {
    return String(path || '')
      .replace(/\\/g, '/')
      .replace(/\/+$/, '');
  }

  function basenameOfPath(path) {
    const norm = normalizePathForCompare(path);
    const idx = norm.lastIndexOf('/');
    if (idx < 0) return norm;
    return norm.slice(idx + 1);
  }

  function isPathInsideDir(path, dir) {
    const p = normalizePathForCompare(path);
    const d = normalizePathForCompare(dir);
    if (!p || !d) return false;
    return p === d || p.startsWith(`${d}/`);
  }

  function toEvalRunDirFromTranscriptPath(path) {
    const norm = normalizePathForCompare(path);
    const suffix = '/raw_transcript.txt';
    if (norm.endsWith(suffix)) {
      return norm.slice(0, -suffix.length);
    }
    return '';
  }

  function toRunDirFromKnowledgePath(path) {
    const norm = normalizePathForCompare(path);
    const suffix = '/knowledge_injection.jsonl';
    if (norm.endsWith(suffix)) {
      return norm.slice(0, -suffix.length);
    }
    return '';
  }

  function buildEvalRunFilePath(runDir, fileName) {
    const base = normalizePathForCompare(runDir);
    if (!base) return null;
    return `${base}/${fileName}`;
  }

  function parseJsonlObjects(text) {
    const lines = String(text || '').split('\n');
    const items = [];
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        const obj = JSON.parse(trimmed);
        if (obj && typeof obj === 'object') items.push(obj);
      } catch {
        // Ignore malformed lines.
      }
    }
    return items;
  }

  function upsertKnowledgeInjections(runDir, items) {
    const key = normalizePathForCompare(runDir);
    if (!key) return;
    if (!Array.isArray(items) || !items.length) {
      state.knowledgeInjectionByRunDir[key] = [];
      return;
    }
    state.knowledgeInjectionByRunDir[key] = items;
  }

  function appendKnowledgeInjectionForKey(key, item) {
    const normalized = normalizePathForCompare(key);
    if (!normalized || !item || typeof item !== 'object') return;
    const current = Array.isArray(state.knowledgeInjectionByRunDir[normalized]) ? state.knowledgeInjectionByRunDir[normalized] : [];
    const next = [...current, item];
    const limit = 200;
    state.knowledgeInjectionByRunDir[normalized] = next.length > limit ? next.slice(-limit) : next;
  }

  function appendKnowledgeInjectionByPath(path, item, aliasKeys = []) {
    const runDir = toRunDirFromKnowledgePath(path);
    const key = normalizePathForCompare(runDir);
    if (!key || !item || typeof item !== 'object') return;
    appendKnowledgeInjectionForKey(key, item);
    for (const aliasKey of (Array.isArray(aliasKeys) ? aliasKeys : [])) {
      const normalizedAlias = normalizePathForCompare(aliasKey);
      if (!normalizedAlias || normalizedAlias === key) continue;
      appendKnowledgeInjectionForKey(normalizedAlias, item);
    }
  }

  function splitKnowledgeEventsBySession(events) {
    const mainEvents = [];
    const forkEvents = [];
    let seenInitialPrompt = false;
    let inFork = false;
    for (const ev of (Array.isArray(events) ? events : [])) {
      const hook = String(ev?.hook_event_name || '').trim();
      if (hook === 'UserPromptSubmit') {
        if (!seenInitialPrompt) {
          seenInitialPrompt = true;
          mainEvents.push(ev);
        } else {
          inFork = true;
          forkEvents.push(ev);
        }
        continue;
      }
      if (inFork) forkEvents.push(ev);
      else mainEvents.push(ev);
    }
    return { mainEvents, forkEvents };
  }

  function getSkillIdsFromKnowledgeEvents(events) {
    const ids = new Set();
    const push = (value) => {
      const s = String(value || '').trim();
      if (s) ids.add(s);
    };
    for (const ev of (events || [])) {
      for (const id of (Array.isArray(ev?.injected_skill_ids) ? ev.injected_skill_ids : [])) push(id);
      for (const id of (Array.isArray(ev?.matched_skill_ids) ? ev.matched_skill_ids : [])) push(id);
      for (const d of (Array.isArray(ev?.details) ? ev.details : [])) {
        if (d && typeof d === 'object' && String(d.type || '') === 'skill') {
          push(d.skill_id);
        }
      }
    }
    return [...ids];
  }

  function normalizeKnowledgeAppName(value) {
    const text = String(value || '').trim();
    return text || '';
  }

  function scopeDirNameForApp(value) {
    const appName = normalizeKnowledgeAppName(value);
    if (!appName) return '_global';
    const sanitized = appName.replace(/[^0-9A-Za-z._-]+/g, '_').replace(/^[._-]+|[._-]+$/g, '');
    return sanitized || '_global';
  }

  function pushScopedSkillPathCandidates(out, rootDir, skillId, appName) {
    const base = normalizePathForCompare(rootDir);
    const sid = String(skillId || '').trim();
    if (!base || !sid) return;
    const push = (path) => {
      const norm = normalizePathForCompare(path);
      if (norm) out.push(norm);
    };
    const scopedDir = scopeDirNameForApp(appName);
    if (scopedDir && scopedDir !== '_global') {
      push(`${base}/${scopedDir}/${sid}.md`);
    }
    push(`${base}/_global/${sid}.md`);
    push(`${base}/${sid}.md`);
  }

  function getKnowledgeAppNameForRun(task, runDir) {
    const targetRunDir = normalizePathForCompare(runDir);
    if (task?.kind === 'eval') {
      const matchedEntry = (state.evalRuns || []).find((item) => {
        const artifactDir = normalizePathForCompare(resolveEvalRunArtifactDir(item));
        const repeatDir = normalizePathForCompare(item?.repeat_dir);
        return artifactDir === targetRunDir || repeatDir === targetRunDir || evalRunKey(item) === targetRunDir;
      }) || null;
      if (matchedEntry) {
        const metaKey = normalizePathForCompare(evalRunKey(matchedEntry));
        const meta = metaKey ? (state.evalRunMetaByDir[metaKey] || {}) : {};
        return normalizeKnowledgeAppName(meta.app_name || matchedEntry.app_name);
      }
      const selectedMeta = normalizePathForCompare(state.selectedEvalRunDir)
        ? (state.evalRunMetaByDir[normalizePathForCompare(state.selectedEvalRunDir)] || {})
        : {};
      return normalizeKnowledgeAppName(selectedMeta.app_name);
    }
    return normalizeKnowledgeAppName(task?.params?.app_name);
  }

  function resolveSkillPathCandidates(task, runDir, skillId) {
    const out = [];
    const push = (path) => {
      const norm = normalizePathForCompare(path);
      if (norm) out.push(norm);
    };
    const inferEvalRootFromRunDir = (dir) => {
      const norm = normalizePathForCompare(dir);
      if (!norm) return '';
      const marker = norm.match(/^(.*)\/(?:flowark|naive)\/[^/]+\/repeat-[^/]+\/runs\/[^/]+$/);
      if (marker && marker[1]) return normalizePathForCompare(marker[1]);
      const alt = norm.match(/^(.*)\/(?:flowark|naive)\/[^/]+\/repeat-[^/]+$/);
      if (alt && alt[1]) return normalizePathForCompare(alt[1]);
      return '';
    };
    const inferredEvalRoot = inferEvalRootFromRunDir(runDir || task?.paths?.run_dir || '');
    const appName = getKnowledgeAppNameForRun(task, runDir);
    if (task?.kind === 'eval') {
      const selectedRunKey = normalizePathForCompare(state.selectedEvalRunDir);
      const selectedMeta = selectedRunKey ? (state.evalRunMetaByDir[selectedRunKey] || {}) : {};
      // Highest priority: eval-scoped skills generated for the current evaluation.
      if (inferredEvalRoot) {
        pushScopedSkillPathCandidates(out, `${inferredEvalRoot}/knowledge_scope/skills`, skillId, appName);
      }
      if (task?.paths?.eval_root) {
        pushScopedSkillPathCandidates(out, `${task.paths.eval_root}/knowledge_scope/skills`, skillId, appName);
      }
      if (task?.paths?.eval_dir) {
        pushScopedSkillPathCandidates(out, `${task.paths.eval_dir}/knowledge_scope/skills`, skillId, appName);
      }
      if (selectedMeta?.skills_dir) {
        pushScopedSkillPathCandidates(out, selectedMeta.skills_dir, skillId, appName || selectedMeta.app_name);
      }
    }
    return [...new Set(out)];
  }

  async function ensureKnowledgeSkillContentsForRun(task, runDir) {
    const key = normalizePathForCompare(runDir);
    if (!key || !task) return;
    const allEvents = state.knowledgeInjectionByRunDir[key] || [];
    const { mainEvents: events } = splitKnowledgeEventsBySession(allEvents);
    const skillIds = getSkillIdsFromKnowledgeEvents(events);
    if (!skillIds.length) return;
    const existing = state.knowledgeSkillContentByRunDir[key] || {};
    const pendingIds = skillIds.filter((id) => !(id in existing));
    if (!pendingIds.length) return;
    if (state.knowledgeSkillContentLoadingByRunDir[key]) return;
    state.knowledgeSkillContentLoadingByRunDir[key] = true;
    try {
      const nextMap = { ...existing };
      const cap = 30;
      for (const skillId of pendingIds.slice(0, cap)) {
        const candidates = resolveSkillPathCandidates(task, key, skillId);
        let resolved = null;
        for (const path of candidates) {
          try {
            const text = await readArtifactAsText(task.task_id, path);
            if (text && String(text).trim()) {
              resolved = { path, content: String(text) };
              break;
            }
          } catch {
            // try next candidate
          }
        }
        if (resolved) nextMap[skillId] = resolved;
        else nextMap[skillId] = { path: '', content: '' };
      }
      state.knowledgeSkillContentByRunDir[key] = nextMap;
    } finally {
      state.knowledgeSkillContentLoadingByRunDir[key] = false;
    }
  }

  async function ensureKnowledgeSkillContentsForCurrentRun(task = state.selectedTaskSnapshot) {
    const runDir = getCurrentKnowledgeRunKey(task);
    if (!runDir) return;
    await ensureKnowledgeSkillContentsForRun(task, runDir);
  }

  function getCurrentRunSkillContentMap() {
    const runDir = getCurrentKnowledgeRunKey(state.selectedTaskSnapshot);
    if (!runDir) return {};
    return state.knowledgeSkillContentByRunDir[runDir] || {};
  }

  function getCurrentRunDirForTask(task) {
    if (!task) return '';
    if (task.kind === 'eval') {
      const entry = selectedEvalRunEntry();
      return normalizePathForCompare(resolveEvalRunArtifactDir(entry) || evalRunKey(entry));
    }
    return '';
  }

  function getTaskOutputRootDir(task) {
    if (!task) return '';
    if (task.kind === 'eval') {
      return normalizePathForCompare(task?.paths?.eval_root || task?.paths?.eval_dir || task?.metadata?.eval_dir || '');
    }
    return '';
  }

  function getKnowledgeRunKeyForEvalEntry(entry) {
    if (!entry || typeof entry !== 'object') return '';
    const artifactDir = normalizePathForCompare(resolveEvalRunArtifactDir(entry));
    if (artifactDir) return artifactDir;
    const runKey = normalizePathForCompare(evalRunKey(entry));
    if (runKey && state.knowledgeInjectionByRunDir[runKey]) return runKey;
    const repeatDir = normalizePathForCompare(entry.repeat_dir);
    if (repeatDir) {
      const match = Object.keys(state.knowledgeInjectionByRunDir).find((key) => isPathInsideDir(key, repeatDir));
      if (match) return normalizePathForCompare(match);
    }
    return runKey;
  }

  function getCurrentKnowledgeRunKey(task = state.selectedTaskSnapshot) {
    if (!task) return '';
    if (task.kind === 'eval') {
      return getKnowledgeRunKeyForEvalEntry(selectedEvalRunEntry());
    }
    return getCurrentRunDirForTask(task);
  }

  function getCurrentKnowledgeInjectionEvents() {
    const runDir = getCurrentKnowledgeRunKey(state.selectedTaskSnapshot);
    if (!runDir) return [];
    const items = state.knowledgeInjectionByRunDir[runDir];
    const { mainEvents } = splitKnowledgeEventsBySession(Array.isArray(items) ? items : []);
    return mainEvents;
  }
