  function getCurrentPerformanceKey(task = state.selectedTaskSnapshot) {
    if (!task) return '';
    if (task.kind !== 'eval') return '';
    const entry = selectedEvalRunEntry();
    return entry ? getEvalRunPerformanceKey(entry) : '';
  }

  const EVAL_RUNS_TOOLTIP_DELAY_MS = 0;
  const EVAL_RUN_COST_LOAD_CONCURRENCY = 6;
  let evalRunsCountTooltipEl = null;
  let evalRunsCountTooltipTimer = null;
  let evalRunsCountTooltipAnchor = null;

  function normalizeTaskIdForCompare(taskId) {
    return String(taskId || '').trim();
  }

  function nextTaskSelectionGeneration() {
    state.taskSelectionGeneration = Math.max(0, Number(state.taskSelectionGeneration) || 0) + 1;
    return state.taskSelectionGeneration;
  }

  function currentTaskSelectionGeneration() {
    return Math.max(0, Number(state.taskSelectionGeneration) || 0);
  }

  function isCurrentTaskSelection(taskId, generation = currentTaskSelectionGeneration()) {
    const expectedTaskId = normalizeTaskIdForCompare(taskId);
    if (!expectedTaskId || normalizeTaskIdForCompare(state.selectedTaskId) !== expectedTaskId) return false;
    if (generation == null) return true;
    return Number(generation) === currentTaskSelectionGeneration();
  }

  function invalidateEvalRunCostSummaryBatch() {
    state.evalRunCostSummariesToken = Math.max(0, Number(state.evalRunCostSummariesToken) || 0) + 1;
    state.evalRunCostSummariesPromise = null;
    state.evalRunCostSummariesTaskId = '';
    state.evalRunCostSummariesGeneration = 0;
    state.evalRunCostSummariesRefreshPending = false;
  }

  function isEvalRunsLoading(taskId = state.selectedTaskId) {
    const loadingTaskId = normalizeTaskIdForCompare(state.evalRunsLoadingTaskId);
    return !!loadingTaskId && loadingTaskId === normalizeTaskIdForCompare(taskId || state.selectedTaskId);
  }

  function evalRunBelongsToTask(run, task = state.selectedTaskSnapshot) {
    if (!run || typeof run !== 'object') return false;
    const taskId = normalizeTaskIdForCompare(task?.task_id);
    if (!taskId) return true;
    const ownerTaskId = normalizeTaskIdForCompare(run.owner_task_id || run.task_id);
    return !ownerTaskId || ownerTaskId === taskId;
  }

  function isCurrentEvalRunSelection(taskId, generation, runKey = state.selectedEvalRunDir) {
    if (!isCurrentTaskSelection(taskId, generation)) return false;
    const normalizedRunKey = normalizePathForCompare(runKey);
    if (!normalizedRunKey) return true;
    return normalizePathForCompare(state.selectedEvalRunDir) === normalizedRunKey;
  }

  function setEvalRunContentLoading(taskId, runKey) {
    const normalizedTaskId = normalizeTaskIdForCompare(taskId);
    const normalizedRunKey = normalizePathForCompare(runKey);
    state.evalRunContentLoadingKey = normalizedTaskId && normalizedRunKey ? `${normalizedTaskId}::${normalizedRunKey}` : '';
  }

  function clearEvalRunContentLoading(taskId, runKey) {
    const normalizedTaskId = normalizeTaskIdForCompare(taskId);
    const normalizedRunKey = normalizePathForCompare(runKey);
    const key = normalizedTaskId && normalizedRunKey ? `${normalizedTaskId}::${normalizedRunKey}` : '';
    if (!key || state.evalRunContentLoadingKey === key) state.evalRunContentLoadingKey = '';
  }

  function isEvalRunContentLoading(taskId = state.selectedTaskId, runKey = state.selectedEvalRunDir) {
    const normalizedTaskId = normalizeTaskIdForCompare(taskId);
    const normalizedRunKey = normalizePathForCompare(runKey);
    return !!normalizedTaskId && !!normalizedRunKey && state.evalRunContentLoadingKey === `${normalizedTaskId}::${normalizedRunKey}`;
  }

  function getCurrentRunPerformance(task = state.selectedTaskSnapshot) {
    const key = getCurrentPerformanceKey(task);
    if (!key) return null;
    return state.runPerformanceByKey[key] || null;
  }

  function normalizeEvalRunsPayload(taskId, runs) {
    return sortEvalRuns(
      (Array.isArray(runs) ? runs : []).map((x) => ({
        ...x,
        owner_task_id: taskId,
        run_dir: normalizePathForCompare(x?.run_dir),
        repeat_dir: normalizePathForCompare(x?.repeat_dir),
      })),
    );
  }

  function hasPerformanceData(perf) {
    if (!perf || typeof perf !== 'object') return false;
    if (perf.wall_time_seconds != null) return true;
    const hasSectionData = (section) => section && typeof section === 'object'
      && Object.values(section).some((value) => value != null && value !== '');
    return hasSectionData(perf.analysis) || hasSectionData(perf.end_to_end);
  }

  function formatNumberCompact(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) return '';
    try {
      return new Intl.NumberFormat('zh-CN').format(num);
    } catch {
      return String(num);
    }
  }

  function formatUsd(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) return '';
    return `$${num.toFixed(4)}`;
  }

  function formatUsdDelta(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) return '';
    const sign = num > 0 ? '+' : num < 0 ? '-' : '';
    return `${sign}${formatUsd(Math.abs(num))}`;
  }

  function finiteNonnegativeNumber(value) {
    if (value == null || value === '') return null;
    const num = Number(value);
    return Number.isFinite(num) && num >= 0 ? num : null;
  }

  function getEvalTaskOpenCodeAverageCostValue(task = state.selectedTaskSnapshot) {
    const progress = task?.metadata?.eval_progress;
    const cost = task?.metadata?.eval_open_code_cost;
    if (!progress || typeof progress !== 'object') return null;
    if (!cost || typeof cost !== 'object') return null;
    const completed = Math.trunc(Number(progress.completed_count));
    const totalCost = finiteNonnegativeNumber(cost.completed_end_to_end_cost_usd);
    if (!Number.isFinite(completed) || completed <= 0) return null;
    if (totalCost == null) return null;
    return totalCost / completed;
  }

  function getEvalRunPerformanceKey(run) {
    const key = evalRunKey(run);
    if (!key) return '';
    const artifactDir = resolveEvalRunArtifactDir(run);
    return artifactDir ? `${key}::artifact:${artifactDir}` : key;
  }

  function clearEvalRunPerformanceCache(runKey) {
    const key = normalizePathForCompare(runKey);
    if (!key) return;
    const run = state.evalRuns.find((item) => evalRunKey(item) === key) || null;
    const performanceKey = run ? getEvalRunPerformanceKey(run) : key;
    if (performanceKey) delete state.runPerformanceByKey[performanceKey];
    delete state.runPerformanceByKey[key];
  }

  function findEvalRunsByArtifactPath(path) {
    const normalizedPath = normalizePathForCompare(path);
    if (!normalizedPath) return [];
    return (Array.isArray(state.evalRuns) ? state.evalRuns : []).filter((run) => {
      const dirs = [
        resolveEvalRunArtifactDir(run),
        run?.repeat_dir,
      ].map((item) => normalizePathForCompare(item)).filter(Boolean);
      return dirs.some((dir) => isPathInsideDir(normalizedPath, dir));
    });
  }

  function clearEvalRunPerformanceCachesForArtifactPaths(paths) {
    const keys = new Set();
    for (const path of Array.isArray(paths) ? paths : []) {
      for (const run of findEvalRunsByArtifactPath(path)) {
        const key = evalRunKey(run);
        if (key) keys.add(key);
      }
    }
    keys.forEach((key) => clearEvalRunPerformanceCache(key));
    return keys.size;
  }

  function getEvalRunOpenCodeEndToEndCostValue(run) {
    const key = getEvalRunPerformanceKey(run);
    const perf = key ? state.runPerformanceByKey[key] : null;
    const candidates = [
      perf?.end_to_end?.total_cost_usd,
      run?.metrics?.end_to_end?.total_cost_usd_sum,
      run?.metrics?.end_to_end?.total_cost_usd,
      run?.performance?.end_to_end?.total_cost_usd,
    ];
    for (const candidate of candidates) {
      const value = finiteNonnegativeNumber(candidate);
      if (value != null) return value;
    }
    return null;
  }

  function getEvalRunDisplayEndToEndCostValue(run) {
    const key = getEvalRunPerformanceKey(run);
    const perf = key ? state.runPerformanceByKey[key] : null;
    const candidates = [
      perf?.end_to_end?.total_with_mem0_cost_usd,
      run?.metrics?.end_to_end?.total_with_mem0_cost_usd_sum,
      getEvalRunOpenCodeEndToEndCostValue(run),
    ];
    for (const candidate of candidates) {
      const value = finiteNonnegativeNumber(candidate);
      if (value != null) return value;
    }
    return null;
  }

  function isEvalRunCostEligible(run) {
    const statusKey = getEvalRunStatusKey(run);
    return ['success', 'warning', 'error', 'timeout', 'cancelled', 'skipped', 'harness_error'].includes(statusKey);
  }

  function buildEvalRunCostStats(runs) {
    return {
      average: getEvalTaskOpenCodeAverageCostValue(),
    };
  }

  function buildEvalRunCostTitle(costValue, stats) {
    const lines = [
      `OpenCode end-to-end cost: ${formatUsd(costValue)}`,
    ];
    const average = finiteNonnegativeNumber(stats?.average);
    if (average != null) {
      const delta = costValue - average;
      const percentText = average > 0 ? ` (${((delta / average) * 100).toFixed(1)}%)` : '';
      lines.push(`Eval average: ${formatUsd(average)}`);
      lines.push(`Delta vs average: ${formatUsdDelta(delta)}${percentText}`);
    }
    return lines.join('\n');
  }

  function formatMsAsSeconds(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) return '';
    return `${(num / 1000).toFixed(2)}s`;
  }

  function formatSeconds(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) return '';
    return `${num.toFixed(2)}s`;
  }

  function extractPerformanceSummaryFromResultPayload(payload) {
    if (!payload || typeof payload !== 'object') return null;
    const metrics = payload.metrics && typeof payload.metrics === 'object' ? payload.metrics : {};
    const analysis = metrics.analysis && typeof metrics.analysis === 'object' ? metrics.analysis : {};
    const endToEnd = metrics.end_to_end && typeof metrics.end_to_end === 'object' ? metrics.end_to_end : {};
    const hasAny = Object.keys(analysis).length || Object.keys(endToEnd).length || payload.wall_time_seconds != null;
    if (!hasAny) return null;
    return {
      source: 'result.json',
      wall_time_seconds: payload.wall_time_seconds,
      analysis: {
        react_turns: analysis.react_turns,
        input_tokens: analysis.input_tokens,
        output_tokens: analysis.output_tokens,
        cache_read_input_tokens: analysis.cache_read_input_tokens,
        cache_creation_input_tokens: analysis.cache_creation_input_tokens,
        total_cost_usd: analysis.total_cost_usd,
        duration_ms: analysis.duration_ms,
        tool_use_block_count: analysis.tool_use_block_count,
      },
      end_to_end: {
        react_turns: endToEnd.react_turns,
        input_tokens: endToEnd.input_tokens,
        output_tokens: endToEnd.output_tokens,
        cache_read_input_tokens: endToEnd.cache_read_input_tokens,
        cache_creation_input_tokens: endToEnd.cache_creation_input_tokens,
        mem0_total_cost_usd: endToEnd.mem0_total_cost_usd_sum,
        total_with_mem0_cost_usd: endToEnd.total_with_mem0_cost_usd_sum,
        mem0_llm_total_tokens: endToEnd.mem0_llm_total_tokens,
        mem0_embedding_total_tokens: endToEnd.mem0_embedding_total_tokens,
        mem0_total_tokens: endToEnd.mem0_total_tokens,
        total_cost_usd: endToEnd.total_cost_usd_sum ?? endToEnd.total_cost_usd,
        duration_ms: endToEnd.duration_ms_sum ?? endToEnd.duration_ms,
        tool_use_block_count: endToEnd.tool_use_block_count_total ?? endToEnd.tool_use_block_count,
      },
    };
  }

  function extractPerformanceSummaryFromCostPayload(payload) {
    if (!payload || typeof payload !== 'object') return null;
    const phases = Array.isArray(payload.phases) ? payload.phases : [];
    const analysisPhases = phases.filter((x) => x && typeof x === 'object' && String(x.name || x.phase_name || '').startsWith('analysis'));
    const sumNumber = (items, pick) => items.reduce((acc, item) => {
      const value = Number(pick(item));
      return Number.isFinite(value) ? acc + value : acc;
    }, 0);
    const analysisUsage = {
      input_tokens: sumNumber(analysisPhases, (phase) => phase?.result?.usage?.input_tokens),
      output_tokens: sumNumber(analysisPhases, (phase) => phase?.result?.usage?.output_tokens),
      cache_read_input_tokens: sumNumber(analysisPhases, (phase) => phase?.result?.usage?.cache_read_input_tokens),
      cache_creation_input_tokens: sumNumber(analysisPhases, (phase) => phase?.result?.usage?.cache_creation_input_tokens),
    };
    const analysisResult = {
      num_turns: sumNumber(analysisPhases, (phase) => phase?.result?.num_turns),
      total_cost_usd: sumNumber(analysisPhases, (phase) => phase?.result?.total_cost_usd),
      duration_ms: sumNumber(analysisPhases, (phase) => phase?.result?.duration_ms),
    };
    const aggregated = payload.aggregated_metrics?.main_agent && typeof payload.aggregated_metrics.main_agent === 'object'
      ? payload.aggregated_metrics.main_agent
      : {};
    const mem0Backend = payload.aggregated_metrics?.mem0_backend && typeof payload.aggregated_metrics.mem0_backend === 'object'
      ? payload.aggregated_metrics.mem0_backend
      : {};
    const totalWithMem0 = payload.aggregated_metrics?.total_with_mem0 && typeof payload.aggregated_metrics.total_with_mem0 === 'object'
      ? payload.aggregated_metrics.total_with_mem0
      : {};
    const aggregatedUsage = aggregated.usage && typeof aggregated.usage === 'object' ? aggregated.usage : {};
    const mem0Usage = mem0Backend.usage && typeof mem0Backend.usage === 'object' ? mem0Backend.usage : {};
    const hasAny = analysisPhases.length || Object.keys(aggregated).length;
    if (!hasAny) return null;
    return {
      source: 'cost_summary.json',
      analysis: {
        react_turns: analysisResult.num_turns,
        input_tokens: analysisUsage.input_tokens,
        output_tokens: analysisUsage.output_tokens,
        cache_read_input_tokens: analysisUsage.cache_read_input_tokens,
        cache_creation_input_tokens: analysisUsage.cache_creation_input_tokens,
        total_cost_usd: analysisResult.total_cost_usd,
        duration_ms: analysisResult.duration_ms,
        tool_use_block_count: sumNumber(analysisPhases, (phase) => phase?.tool_use_block_count),
      },
      end_to_end: {
        react_turns: aggregated.num_turns_sum,
        input_tokens: aggregatedUsage.input_tokens,
        output_tokens: aggregatedUsage.output_tokens,
        cache_read_input_tokens: aggregatedUsage.cache_read_input_tokens,
        cache_creation_input_tokens: aggregatedUsage.cache_creation_input_tokens,
        mem0_total_cost_usd: mem0Backend.total_cost_usd_sum,
        total_with_mem0_cost_usd: totalWithMem0.total_cost_usd_sum,
        mem0_llm_total_tokens: mem0Usage.llm_total_tokens,
        mem0_embedding_total_tokens: mem0Usage.embedding_tokens,
        mem0_total_tokens: mem0Usage.total_tokens,
        total_cost_usd: aggregated.total_cost_usd_sum,
        duration_ms: aggregated.duration_ms_sum,
        tool_use_block_count: payload.tool_use_block_count_total,
      },
    };
  }

  function mergePerformanceSummary(base, incoming) {
    if (!incoming) return base || null;
    if (!base) return incoming;
    const merged = { ...base, ...incoming };
    const mergeSection = (name) => {
      merged[name] = { ...(base[name] || {}), ...(incoming[name] || {}) };
    };
    mergeSection('analysis');
    mergeSection('end_to_end');
    if (merged.wall_time_seconds == null && base.wall_time_seconds != null) {
      merged.wall_time_seconds = base.wall_time_seconds;
    }
    return merged;
  }

  function getEvalRunCostSummaryCandidatePaths(run) {
    const artifactDir = resolveEvalRunArtifactDir(run);
    return Array.from(new Set([
      buildEvalRunFilePath(artifactDir, 'cost_summary.json'),
      buildEvalRunFilePath(run?.repeat_dir, 'cost_summary.json'),
    ].filter(Boolean)));
  }

  async function ensureEvalRunCostSummary(task, run, { taskId = '', generation = currentTaskSelectionGeneration(), token = state.evalRunCostSummariesToken } = {}) {
    if (!task || task.kind !== 'eval' || !run) return false;
    const expectedTaskId = normalizeTaskIdForCompare(taskId || task.task_id);
    const isCurrentLoad = () => token === state.evalRunCostSummariesToken && isCurrentTaskSelection(expectedTaskId, generation);
    if (!isCurrentLoad()) return false;
    const key = getEvalRunPerformanceKey(run);
    if (!key) return false;
    if (hasPerformanceData(state.runPerformanceByKey[key])) return false;
    if (state.runPerformanceLoadingByKey[key]) return false;
    const candidates = getEvalRunCostSummaryCandidatePaths(run);
    if (!candidates.length) return false;
    state.runPerformanceLoadingByKey[key] = true;
    try {
      let merged = null;
      for (const path of candidates) {
        if (!isCurrentLoad()) return false;
        try {
          const data = await api(`/api/tasks/${task.task_id}/artifact?path=${encodeURIComponent(path)}`);
          if (!isCurrentLoad()) return false;
          const payload = data?.json && typeof data.json === 'object' ? data.json : null;
          if (!payload) continue;
          const summary = extractPerformanceSummaryFromCostPayload(payload);
          if (summary) {
            merged = mergePerformanceSummary(merged, summary);
            break;
          }
        } catch {
          // Try next candidate.
        }
      }
      if (merged && isCurrentLoad()) {
        state.runPerformanceByKey[key] = merged;
        return true;
      }
    } finally {
      if (token === state.evalRunCostSummariesToken) {
        state.runPerformanceLoadingByKey[key] = false;
      } else {
        delete state.runPerformanceLoadingByKey[key];
      }
    }
    return false;
  }

  async function loadEvalRunCostSummaryBatch(task, { generation = currentTaskSelectionGeneration(), token = state.evalRunCostSummariesToken } = {}) {
    if (!task || task.kind !== 'eval') return;
    const taskId = String(task.task_id || '').trim();
    const isCurrentLoad = () => token === state.evalRunCostSummariesToken && isCurrentTaskSelection(taskId, generation);
    if (!isCurrentLoad()) return;
    const candidates = (Array.isArray(state.evalRuns) ? state.evalRuns : []).filter((run) => {
      const key = getEvalRunPerformanceKey(run);
      return key
        && isEvalRunCostEligible(run)
        && !hasPerformanceData(state.runPerformanceByKey[key])
        && !state.runPerformanceLoadingByKey[key]
        && getEvalRunCostSummaryCandidatePaths(run).length;
    });
    if (!taskId || !candidates.length) return;
    let cursor = 0;
    let changed = false;
    const workerCount = Math.min(EVAL_RUN_COST_LOAD_CONCURRENCY, candidates.length);
    const workers = Array.from({ length: workerCount }, async () => {
      while (cursor < candidates.length && isCurrentLoad()) {
        const run = candidates[cursor];
        cursor += 1;
        const loaded = await ensureEvalRunCostSummary(task, run, { taskId, generation, token });
        changed = changed || loaded;
      }
    });
    await Promise.all(workers);
    if (changed && isCurrentLoad()) {
      renderEvalRunsList();
    }
  }

  function ensureEvalRunCostSummaries(task = state.selectedTaskSnapshot, { generation = currentTaskSelectionGeneration() } = {}) {
    if (!task || task.kind !== 'eval') return Promise.resolve();
    const taskId = String(task.task_id || '').trim();
    if (!taskId) return Promise.resolve();
    if (!isCurrentTaskSelection(taskId, generation)) return Promise.resolve();
    if (state.evalRunCostSummariesPromise) {
      if (
        state.evalRunCostSummariesTaskId === taskId
        && Number(state.evalRunCostSummariesGeneration) === Number(generation)
      ) {
        state.evalRunCostSummariesRefreshPending = true;
        return state.evalRunCostSummariesPromise;
      }
      invalidateEvalRunCostSummaryBatch();
    }
    const token = Math.max(0, Number(state.evalRunCostSummariesToken) || 0) + 1;
    state.evalRunCostSummariesToken = token;
    state.evalRunCostSummariesTaskId = taskId;
    state.evalRunCostSummariesGeneration = generation;
    state.evalRunCostSummariesPromise = (async () => {
      try {
        do {
          state.evalRunCostSummariesRefreshPending = false;
          await loadEvalRunCostSummaryBatch(task, { generation, token });
        } while (
          token === state.evalRunCostSummariesToken
          && state.evalRunCostSummariesRefreshPending
          && isCurrentTaskSelection(taskId, generation)
        );
      } finally {
        if (token === state.evalRunCostSummariesToken) {
          state.evalRunCostSummariesPromise = null;
          state.evalRunCostSummariesTaskId = '';
          state.evalRunCostSummariesGeneration = 0;
          state.evalRunCostSummariesRefreshPending = false;
        }
      }
    })();
    return state.evalRunCostSummariesPromise;
  }

  async function ensurePerformanceForCurrentSelection(task = state.selectedTaskSnapshot) {
    if (!task || task.kind !== 'eval') return;
    const entry = selectedEvalRunEntry();
    if (!entry) return;
    await ensureEvalRunCostSummary(task, entry);
  }

  function evalRunEntryId(entry) {
    if (!entry || typeof entry !== 'object') return '';
    const rawEntryId = String(entry.entry_id || '').trim();
    if (rawEntryId) return rawEntryId;
    const taskIndex = entry.task_index;
    if (taskIndex != null && taskIndex !== '') {
      const parsed = Number.parseInt(String(taskIndex), 10);
      if (Number.isFinite(parsed)) return `task-${parsed}`;
    }
    const repeatDir = normalizePathForCompare(entry.repeat_dir);
    if (repeatDir) return `repeat:${repeatDir}`;
    const runDir = normalizePathForCompare(entry.run_dir);
    if (runDir) return `run:${runDir}`;
    const sourceId = entry.source_id ? String(entry.source_id) : '';
    const repeatIdx = entry.repeat_idx != null ? String(entry.repeat_idx) : '';
    return `unknown:${sourceId}:${repeatIdx}`;
  }

  function evalRunKey(entry) {
    if (!entry || typeof entry !== 'object') return '';
    const entryId = evalRunEntryId(entry);
    if (!entryId) return '';
    const ownerTaskId = String(entry.owner_task_id || entry.task_id || '').trim();
    return ownerTaskId ? `${ownerTaskId}::${entryId}` : entryId;
  }

  function selectedEvalRunEntry() {
    const selected = normalizePathForCompare(state.selectedEvalRunDir);
    if (!selected) return null;
    const entry = state.evalRuns.find((x) => evalRunKey(x) === selected) || null;
    return evalRunBelongsToTask(entry) ? entry : null;
  }

  function getEvalRunExecState(run) {
    const explicit = String(run?.exec_state || '').trim().toLowerCase();
    if (explicit === 'running' || explicit === 'completed' || explicit === 'pending' || explicit === 'interrupted') return explicit;
    const status = String(run?.status || '').trim().toLowerCase();
    if (status === 'interrupted') return 'interrupted';
    if (['running', 'starting', 'queued', 'finishing'].includes(status)) return 'running';
    if (['success', 'warning', 'error', 'timeout', 'cancelled', 'skipped', 'harness_error'].includes(status)) return 'completed';
    return 'pending';
  }

  function getEvalRunDisplayState(run) {
    const explicit = String(run?.exec_state || '').trim().toLowerCase();
    if (explicit) return explicit;
    return getEvalRunExecState(run);
  }

  function getEvalRunStatusKey(run) {
    const status = String(run?.status || '').trim().toLowerCase();
    if (status) return status;
    const execState = getEvalRunExecState(run);
    if (execState === 'completed' || execState === 'running' || execState === 'interrupted') return execState;
    return 'pending';
  }

  function statusCssKey(value) {
    const raw = String(value || 'unknown').trim().toLowerCase();
    return raw.replace(/[^a-z0-9_-]+/g, '-') || 'unknown';
  }

  function getEvalRunStatusLabel(statusKey) {
    const key = String(statusKey || '').trim().toLowerCase();
    const labels = {
      success: 'success',
      warning: 'warning',
      error: 'error',
      timeout: 'timeout',
      cancelled: 'cancelled',
      skipped: 'skipped',
      harness_error: 'harness_error',
      paused: 'paused',
      running: 'running',
      pending: 'pending',
      interrupted: 'interrupted',
      completed: 'completed',
    };
    return labels[key] || key || 'unknown';
  }

  function compactEvalRunIdentifier(value) {
    const text = String(value || '').trim();
    if (!text) return '';
    const normalized = text.replace(/[\\/]+/g, '.');
    const parts = normalized.split('.').map((item) => item.trim()).filter(Boolean);
    if (parts.length >= 2) return parts.slice(-2).join('.');
    return text;
  }

  function extractEvalRunSourceSummary(run) {
    const raw = String(
      run?.source
        || run?.source_description
        || run?.generated_source
        || '',
    ).trim();
    if (!raw) return '';
    const pick = (label) => {
      const match = raw.match(new RegExp(`${label}[:：]\\s*([^；;\\n]+)`));
      return match ? match[1].trim() : '';
    };
    const method = pick('source方法');
    const statement = pick('source语句/调用');
    const classname = pick('source类');
    const parts = [method || classname, statement].filter(Boolean);
    return parts.length ? parts.join(' · ') : raw;
  }

  function formatEvalRunSinkSummary(run) {
    const raw = run?.sink_types ?? run?.sink_type;
    if (Array.isArray(raw)) {
      return raw.map((item) => String(item || '').trim()).filter(Boolean).join(',');
    }
    return String(raw || '').trim();
  }

  function pushUniqueEvalRunMetaLine(lines, text) {
    const normalized = String(text || '').trim();
    if (!normalized) return;
    const key = normalized.toLowerCase();
    if (lines.some((line) => line.toLowerCase() === key)) return;
    lines.push(normalized);
  }

  function buildEvalRunCaseLine(run) {
    const sourceId = String(run?.source_id || '').trim();
    const flowId = String(run?.flow_id || '').trim();
    if (sourceId && flowId && sourceId !== flowId) {
      return `case: ${compactEvalRunIdentifier(sourceId)} -> ${compactEvalRunIdentifier(flowId)}`;
    }
    const caseId = sourceId || flowId;
    return caseId ? `case: ${compactEvalRunIdentifier(caseId)}` : '';
  }

  function buildEvalRunMetaLines(run, runKey) {
    const lines = [];
    const appParts = [
      run?.app_name ? String(run.app_name).trim() : '',
      run?.dataset ? String(run.dataset).trim() : '',
      run?.benchmark_family ? String(run.benchmark_family).trim() : '',
    ].filter(Boolean);
    if (appParts.length) pushUniqueEvalRunMetaLine(lines, `app: ${appParts.join(' · ')}`);

    const sourceSummary = extractEvalRunSourceSummary(run);
    if (sourceSummary) pushUniqueEvalRunMetaLine(lines, `source: ${sourceSummary}`);

    const sinkSummary = formatEvalRunSinkSummary(run);
    if (sinkSummary) pushUniqueEvalRunMetaLine(lines, `sink: ${sinkSummary}`);

    const caseLine = buildEvalRunCaseLine(run);
    if (!sourceSummary || lines.length < 2) pushUniqueEvalRunMetaLine(lines, caseLine);

    const dirName = basenameOfPath(run?.run_dir || run?.repeat_dir || runKey);
    if (!lines.length && dirName) pushUniqueEvalRunMetaLine(lines, `dir: ${dirName}`);
    return lines.slice(0, 3);
  }

  function buildEvalRunMetaTitle(run, runKey) {
    return [
      run?.source_id ? `source_id: ${run.source_id}` : '',
      run?.flow_id ? `flow_id: ${run.flow_id}` : '',
      run?.source ? `source: ${run.source}` : '',
      formatEvalRunSinkSummary(run) ? `sink: ${formatEvalRunSinkSummary(run)}` : '',
      run?.run_dir ? `run_dir: ${run.run_dir}` : '',
      run?.repeat_dir ? `repeat_dir: ${run.repeat_dir}` : '',
      runKey ? `key: ${runKey}` : '',
    ].filter(Boolean).join('\n');
  }

  function buildEvalRunsHeaderSummary(runs) {
    const list = Array.isArray(runs) ? runs : [];
    const total = list.length;
    let completed = 0;
    const statusCounts = new Map();
    for (const run of list) {
      if (getEvalRunExecState(run) === 'completed') completed += 1;
      const statusKey = getEvalRunStatusKey(run);
      statusCounts.set(statusKey, (statusCounts.get(statusKey) || 0) + 1);
    }
    const preferredOrder = [
      'success',
      'warning',
      'error',
      'timeout',
      'cancelled',
      'skipped',
      'harness_error',
      'paused',
      'running',
      'pending',
      'interrupted',
      'completed',
      'unknown',
    ];
    const orderedStatusKeys = Array.from(statusCounts.keys()).sort((a, b) => {
      const aIdx = preferredOrder.indexOf(a);
      const bIdx = preferredOrder.indexOf(b);
      const aRank = aIdx >= 0 ? aIdx : preferredOrder.length;
      const bRank = bIdx >= 0 ? bIdx : preferredOrder.length;
      if (aRank !== bRank) return aRank - bRank;
      return a.localeCompare(b);
    });
    const titleLines = [`completed: ${completed}/${total}`];
    for (const statusKey of orderedStatusKeys) {
      titleLines.push(`${statusKey}: ${statusCounts.get(statusKey) || 0}`);
    }
    const visibleStatusKeys = orderedStatusKeys.filter((key) => {
      const count = statusCounts.get(key) || 0;
      return count > 0 && !['completed', 'unknown'].includes(key);
    });
    const countText = visibleStatusKeys
      .map((key) => `${getEvalRunStatusLabel(key)} ${statusCounts.get(key) || 0}`)
      .join(' · ');
    return {
      text: `${completed}/${total}`,
      compactText: countText ? `${completed}/${total} · ${countText}` : `${completed}/${total}`,
      counts: visibleStatusKeys.map((key) => ({
        key,
        label: getEvalRunStatusLabel(key),
        count: statusCounts.get(key) || 0,
      })),
      title: titleLines.join('\n'),
    };
  }

  function ensureEvalRunsCountTooltipElement() {
    if (evalRunsCountTooltipEl?.isConnected) return evalRunsCountTooltipEl;
    const tooltip = document.createElement('div');
    tooltip.className = 'hover-tooltip';
    document.body.appendChild(tooltip);
    evalRunsCountTooltipEl = tooltip;
    return tooltip;
  }

  function clearEvalRunsCountTooltipTimer() {
    if (evalRunsCountTooltipTimer) {
      clearTimeout(evalRunsCountTooltipTimer);
      evalRunsCountTooltipTimer = null;
    }
  }

  function positionEvalRunsCountTooltip(anchor) {
    const tooltip = ensureEvalRunsCountTooltipElement();
    if (!anchor || !tooltip.textContent) return;
    tooltip.style.left = '0px';
    tooltip.style.top = '0px';
    tooltip.style.visibility = 'hidden';
    tooltip.classList.add('visible');
    const anchorRect = anchor.getBoundingClientRect();
    const tooltipRect = tooltip.getBoundingClientRect();
    const margin = 12;
    let left = anchorRect.left + ((anchorRect.width - tooltipRect.width) / 2);
    left = Math.max(margin, Math.min(left, window.innerWidth - tooltipRect.width - margin));
    let top = anchorRect.bottom + 8;
    if (top + tooltipRect.height > window.innerHeight - margin) {
      top = Math.max(margin, anchorRect.top - tooltipRect.height - 8);
    }
    tooltip.style.left = `${Math.round(left)}px`;
    tooltip.style.top = `${Math.round(top)}px`;
    tooltip.style.visibility = 'visible';
  }

  function showEvalRunsCountTooltip(anchor) {
    const text = String(anchor?.dataset?.tooltipText || '').trim();
    if (!anchor || !text) return;
    const tooltip = ensureEvalRunsCountTooltipElement();
    evalRunsCountTooltipAnchor = anchor;
    tooltip.textContent = text;
    positionEvalRunsCountTooltip(anchor);
  }

  function hideEvalRunsCountTooltip() {
    clearEvalRunsCountTooltipTimer();
    evalRunsCountTooltipAnchor = null;
    if (!evalRunsCountTooltipEl) return;
    evalRunsCountTooltipEl.classList.remove('visible');
    evalRunsCountTooltipEl.style.visibility = 'hidden';
  }

  function scheduleEvalRunsCountTooltip(anchor) {
    clearEvalRunsCountTooltipTimer();
    if (!anchor || !String(anchor.dataset.tooltipText || '').trim()) return;
    evalRunsCountTooltipTimer = setTimeout(() => {
      evalRunsCountTooltipTimer = null;
      showEvalRunsCountTooltip(anchor);
    }, EVAL_RUNS_TOOLTIP_DELAY_MS);
  }

  function bindEvalRunsCountTooltip() {
    const anchor = els.evalRunsCount;
    if (!anchor || anchor.dataset.tooltipBound === '1') return;
    anchor.dataset.tooltipBound = '1';
    anchor.addEventListener('mouseenter', () => scheduleEvalRunsCountTooltip(anchor));
    anchor.addEventListener('mouseleave', hideEvalRunsCountTooltip);
    anchor.addEventListener('pointerdown', hideEvalRunsCountTooltip);
    window.addEventListener('scroll', () => {
      if (evalRunsCountTooltipAnchor === anchor && evalRunsCountTooltipEl?.classList.contains('visible')) {
        positionEvalRunsCountTooltip(anchor);
      }
    }, true);
    window.addEventListener('resize', () => {
      if (evalRunsCountTooltipAnchor === anchor && evalRunsCountTooltipEl?.classList.contains('visible')) {
        positionEvalRunsCountTooltip(anchor);
      }
    });
  }

  function resolveEvalRunArtifactDir(run) {
    if (!run || typeof run !== 'object') return '';
    const runDir = normalizePathForCompare(run.run_dir);
    if (runDir) return runDir;
    return normalizePathForCompare(run.repeat_dir);
  }

  function findEvalRunByTranscriptPath(transcriptPath) {
    const runDir = normalizePathForCompare(toEvalRunDirFromTranscriptPath(transcriptPath));
    if (!runDir) return null;
    let direct = null;
    for (let i = 0; i < state.evalRuns.length; i += 1) {
      const item = state.evalRuns[i];
      if (normalizePathForCompare(item?.run_dir) === runDir) {
        direct = item;
        break;
      }
    }
    if (direct) return direct;
    for (let i = 0; i < state.evalRuns.length; i += 1) {
      const item = state.evalRuns[i];
      const repeatDir = normalizePathForCompare(item?.repeat_dir);
      if (repeatDir && isPathInsideDir(runDir, repeatDir)) {
        return item;
      }
    }
    return null;
  }

  function findEvalRunByKnowledgePath(path) {
    const runDir = normalizePathForCompare(toRunDirFromKnowledgePath(path));
    if (!runDir) return null;
    let direct = null;
    for (let i = 0; i < state.evalRuns.length; i += 1) {
      const item = state.evalRuns[i];
      if (normalizePathForCompare(item?.run_dir) === runDir) {
        direct = item;
        break;
      }
    }
    if (direct) return direct;
    for (let i = 0; i < state.evalRuns.length; i += 1) {
      const item = state.evalRuns[i];
      const repeatDir = normalizePathForCompare(item?.repeat_dir);
      if (repeatDir && isPathInsideDir(runDir, repeatDir)) {
        return item;
      }
    }
    return null;
  }

  function sortEvalRuns(list) {
    const runs = Array.isArray(list) ? [...list] : [];
    runs.sort((a, b) => {
      const ai = Number(a?.task_index ?? Number.MAX_SAFE_INTEGER);
      const bi = Number(b?.task_index ?? Number.MAX_SAFE_INTEGER);
      if (ai !== bi) return ai - bi;
      const ar = Number(a?.repeat_idx ?? Number.MAX_SAFE_INTEGER);
      const br = Number(b?.repeat_idx ?? Number.MAX_SAFE_INTEGER);
      if (ar !== br) return ar - br;
      const am = String(a?.mode || '');
      const bm = String(b?.mode || '');
      if (am !== bm) return am.localeCompare(bm);
      const as = String(a?.source_id || '');
      const bs = String(b?.source_id || '');
      if (as !== bs) return as.localeCompare(bs);
      return evalRunKey(a).localeCompare(evalRunKey(b));
    });
    return runs;
  }

  function setEvalRunsPaneVisibility(visible) {
    state.evalRunsPaneVisible = !!visible;
    if (els.evalRunsPane) {
      els.evalRunsPane.classList.toggle('hidden', !state.evalRunsPaneVisible);
    }
    applySidebarLayout();
  }

  function mergeEvalRunEntry(entry) {
    if (!entry || typeof entry !== 'object') return;
    const key = evalRunKey(entry);
    if (!key) return;
    const idx = state.evalRuns.findIndex((x) => evalRunKey(x) === key);
    if (idx >= 0) {
      const prevPerformanceKey = getEvalRunPerformanceKey(state.evalRuns[idx]);
      const prevRunDir = normalizePathForCompare(state.evalRuns[idx]?.run_dir);
      state.evalRuns[idx] = { ...state.evalRuns[idx], ...entry };
      const nextPerformanceKey = getEvalRunPerformanceKey(state.evalRuns[idx]);
      const nextRunDir = normalizePathForCompare(state.evalRuns[idx]?.run_dir);
      if (prevRunDir && nextRunDir && prevRunDir !== nextRunDir) {
        if (prevPerformanceKey) delete state.runPerformanceByKey[prevPerformanceKey];
        if (nextPerformanceKey) delete state.runPerformanceByKey[nextPerformanceKey];
      }
      if (!isEvalRunCostEligible(state.evalRuns[idx])) {
        if (prevPerformanceKey) delete state.runPerformanceByKey[prevPerformanceKey];
        if (nextPerformanceKey) delete state.runPerformanceByKey[nextPerformanceKey];
      }
    } else {
      state.evalRuns.push({ ...entry });
    }
    state.evalRuns = sortEvalRuns(state.evalRuns);
  }

  function clearEvalRunsState() {
    invalidateEvalRunCostSummaryBatch();
    state.evalRuns = [];
    state.evalRunsLoadingTaskId = '';
    state.evalRunContentLoadingKey = '';
    state.lastEvalRunsRenderSignature = '';
    state.selectedEvalRunDir = null;
    state.evalRunTranscriptBuffers = {};
    state.evalRunMetaByDir = {};
    state.evalRunMetaLoadingByDir = {};
    state.runPerformanceByKey = {};
    state.runPerformanceLoadingByKey = {};
    state.knowledgeInjectionByRunDir = {};
    state.knowledgeSkillContentByRunDir = {};
    state.knowledgeSkillContentLoadingByRunDir = {};
    state.transcriptParseCache = {};
    state.transcriptParseCacheOrder = [];
    if (els.evalRunsList) els.evalRunsList.innerHTML = '';
    if (els.evalRunsCount) els.evalRunsCount.textContent = '';
  }

  function renderEvalRunSummaryLoadingPlaceholder() {
    if (els.runSummaryCard) els.runSummaryCard.classList.remove('hidden');
    if (els.runSummaryTitle) els.runSummaryTitle.textContent = 'Sub-run details';
    if (els.runSummaryMode) {
      els.runSummaryMode.textContent = '';
      els.runSummaryMode.classList.add('hidden');
    }
    if (els.runSummary) {
      els.runSummary.innerHTML = '<div class="summary-empty muted">Loading eval runs...</div>';
    }
  }

  function beginEvalTaskDetailLoading(taskId, generation = currentTaskSelectionGeneration()) {
    if (!isCurrentTaskSelection(taskId, generation)) return;
    invalidateEvalRunCostSummaryBatch();
    state.evalRuns = [];
    state.evalRunsLoadingTaskId = normalizeTaskIdForCompare(taskId);
    state.evalRunContentLoadingKey = '';
    state.lastEvalRunsRenderSignature = '';
    state.selectedEvalRunDir = null;
    state.evalRunTranscriptBuffers = {};
    state.evalRunMetaByDir = {};
    state.evalRunMetaLoadingByDir = {};
    state.runPerformanceByKey = {};
    state.runPerformanceLoadingByKey = {};
    state.knowledgeInjectionByRunDir = {};
    state.knowledgeSkillContentByRunDir = {};
    state.knowledgeSkillContentLoadingByRunDir = {};
    state.transcriptParseCache = {};
    state.transcriptParseCacheOrder = [];
    state.costSummaryData = null;
    state.costSummaryPath = null;
    state.costSummaryViewMode = 'card';
    if (typeof resetEvalRunContentPanes === 'function') resetEvalRunContentPanes();
    renderEvalRunsList({ force: true });
    renderEvalRunSummaryLoadingPlaceholder();
  }

  function stableEvalRunsSignatureValue(value) {
    if (value == null) return '';
    if (Array.isArray(value)) {
      return `[${value.map((item) => stableEvalRunsSignatureValue(item)).join(',')}]`;
    }
    if (typeof value === 'object') {
      return `{${Object.keys(value).sort().map((key) => `${key}:${stableEvalRunsSignatureValue(value[key])}`).join(',')}}`;
    }
    return String(value);
  }

  function buildEvalRunsRenderSignature(runs, loading) {
    if (loading) return `loading:${normalizeTaskIdForCompare(state.evalRunsLoadingTaskId)}`;
    const task = state.selectedTaskSnapshot || {};
    const taskParams = task.params || {};
    const taskPaths = task.paths || {};
    const taskMetadata = task.metadata || {};
    const parts = [
      normalizeTaskIdForCompare(task.task_id),
      String(task.status || ''),
      task?.metadata?.historical ? 'historical' : '',
      normalizePathForCompare(taskPaths.eval_root || taskPaths.eval_dir || taskMetadata.eval_dir || ''),
      stableEvalRunsSignatureValue(taskParams),
      stableEvalRunsSignatureValue({
        completed_count: taskMetadata?.eval_progress?.completed_count,
        total_count: taskMetadata?.eval_progress?.total_count,
        completed_end_to_end_cost_usd: taskMetadata?.eval_open_code_cost?.completed_end_to_end_cost_usd,
        completed_total_with_mem0_cost_usd: taskMetadata?.eval_open_code_cost?.completed_total_with_mem0_cost_usd,
      }),
      String(getEvalTaskOpenCodeAverageCostValue(task) ?? ''),
      normalizePathForCompare(state.selectedEvalRunDir),
      String(runs.length),
    ];
    for (const run of runs) {
      const runKey = evalRunKey(run);
      const cost = getEvalRunOpenCodeEndToEndCostValue(run);
      const totalWithMem0Cost = getEvalRunDisplayEndToEndCostValue(run);
      const issues = Array.isArray(run.health_issues) ? run.health_issues : [];
      parts.push([
        runKey,
        run.task_index ?? '',
        run.task_total ?? '',
        run.repeat_idx ?? '',
        run.mode || '',
        run.status || '',
        run.exec_state || '',
        run.run_dir || '',
        run.repeat_dir || '',
        run.source_id || '',
        run.flow_id || '',
        run.app_name || '',
        run.dataset || '',
        run.benchmark_family || '',
        run.source || '',
        run.source_description || '',
        run.generated_source || '',
        stableEvalRunsSignatureValue(run.sink_types ?? run.sink_type),
        run.started_at || '',
        run.finished_at || '',
        cost == null ? '' : String(cost),
        totalWithMem0Cost == null ? '' : String(totalWithMem0Cost),
        stableEvalRunsSignatureValue(issues.map((issue) => ({
          severity: issue?.severity,
          code: issue?.code,
          message: issue?.message,
          artifact_path: issue?.artifact_path,
        }))),
      ].join('~'));
    }
    return parts.join('\n');
  }

  function renderEvalRunsList({ force = false } = {}) {
    if (!els.evalRunsList || !els.evalRunsCount) return;
    const runs = Array.isArray(state.evalRuns) ? state.evalRuns : [];
    const loading = isEvalRunsLoading();
    const renderSignature = buildEvalRunsRenderSignature(runs, loading);
    if (!force && renderSignature && state.lastEvalRunsRenderSignature === renderSignature) {
      return;
    }
    state.lastEvalRunsRenderSignature = renderSignature;
    if (loading) {
      els.evalRunsCount.textContent = 'Loading';
      delete els.evalRunsCount.dataset.tooltipText;
      els.evalRunsCount.removeAttribute('title');
      els.evalRunsList.innerHTML = '<div class="eval-runs-loading summary-empty muted">Loading eval runs...</div>';
      return;
    }
    const headerSummary = buildEvalRunsHeaderSummary(runs);
    const costStats = buildEvalRunCostStats(runs);
    bindEvalRunsCountTooltip();
    els.evalRunsCount.textContent = headerSummary.compactText || headerSummary.text;
    els.evalRunsCount.dataset.tooltipText = headerSummary.title;
    els.evalRunsCount.removeAttribute('title');
    if (evalRunsCountTooltipAnchor === els.evalRunsCount && evalRunsCountTooltipEl?.classList.contains('visible')) {
      showEvalRunsCountTooltip(els.evalRunsCount);
    }
    els.evalRunsList.innerHTML = '';
    for (let i = 0; i < runs.length; i += 1) {
      const run = runs[i];
      const runKey = evalRunKey(run);
      const taskParams = state.selectedTaskSnapshot?.params || {};
      const row = document.createElement('div');
      row.className = 'eval-run-item';
      if (runKey) row.dataset.runKey = runKey;
      if (runKey && normalizePathForCompare(state.selectedEvalRunDir) === runKey) {
        row.classList.add('active');
      }
      row.addEventListener('click', () => { selectEvalRun(runKey); });

      const titleRow = document.createElement('div');
      titleRow.className = 'eval-run-title-row';

      const title = document.createElement('div');
      title.className = 'title';
      const indexText = (run.task_index != null && run.task_total != null)
        ? `#${run.task_index}/${run.task_total}`
        : `run ${i + 1}`;
      const modeText = String(run.mode || '').trim();
      const presetLabel = typeof inferTaskExperimentPreset === 'function' && typeof formatTaskExperimentPreset === 'function'
        ? formatTaskExperimentPreset(inferTaskExperimentPreset('eval', taskParams))
        : '';
      title.textContent = [
        indexText,
        presetLabel,
        modeText,
      ].filter(Boolean).join(' · ');
      titleRow.appendChild(title);
      row.appendChild(titleRow);

      const meta = document.createElement('div');
      meta.className = 'meta';
      const detailParts = buildEvalRunMetaLines(run, runKey);
      for (const detail of detailParts) {
        const line = document.createElement('div');
        line.className = 'meta-row';
        line.textContent = detail;
        meta.appendChild(line);
      }
      meta.title = buildEvalRunMetaTitle(run, runKey);
      row.appendChild(meta);

      const pillRow = document.createElement('div');
      pillRow.className = 'run-pill-row';

      const statusKey = getEvalRunStatusKey(run);
      if (statusKey) {
        const statusPill = document.createElement('div');
        statusPill.className = `run-state run-status ${statusCssKey(statusKey)}`;
        statusPill.textContent = getEvalRunStatusLabel(statusKey);
        pillRow.appendChild(statusPill);
      }

      const healthIssues = Array.isArray(run.health_issues) ? run.health_issues : [];
      for (const issue of healthIssues.slice(0, 2)) {
        const severity = statusCssKey(issue?.severity || 'warning');
        const issuePill = document.createElement('div');
        issuePill.className = `run-state run-health-issue ${severity}`;
        issuePill.textContent = String(issue?.code || 'health_issue');
        issuePill.title = [
          issue?.severity || '',
          issue?.message || '',
          issue?.artifact_path || '',
        ].filter(Boolean).join(' · ');
        pillRow.appendChild(issuePill);
      }
      if (healthIssues.length > 2) {
        const morePill = document.createElement('div');
        morePill.className = 'run-state run-health-issue warning';
        morePill.textContent = `+${healthIssues.length - 2} issues`;
        pillRow.appendChild(morePill);
      }

      const runCostValue = isEvalRunCostEligible(run)
        ? getEvalRunOpenCodeEndToEndCostValue(run)
        : null;
      if (runCostValue != null) {
        const costPill = document.createElement('div');
        costPill.className = 'run-state run-opencode-cost';
        costPill.textContent = formatUsd(runCostValue);
        costPill.title = buildEvalRunCostTitle(runCostValue, costStats);
        pillRow.appendChild(costPill);
      }
      const totalWithMem0CostValue = isEvalRunCostEligible(run)
        ? getEvalRunDisplayEndToEndCostValue(run)
        : null;
      if (totalWithMem0CostValue != null && totalWithMem0CostValue !== runCostValue) {
        const mem0CostPill = document.createElement('div');
        mem0CostPill.className = 'run-state run-opencode-cost';
        mem0CostPill.textContent = `mem0 ${formatUsd(totalWithMem0CostValue)}`;
        mem0CostPill.title = 'OpenCode plus tracked Mem0 backend cost';
        pillRow.appendChild(mem0CostPill);
      }

      row.appendChild(pillRow);

      els.evalRunsList.appendChild(row);
    }
  }

  function upsertEvalRunFromProgress(data) {
    if (!data || typeof data !== 'object') return;
    const entry = {
      ...data,
      owner_task_id: String(state.selectedTaskId || data.owner_task_id || data.task_id || '').trim(),
    };
    mergeEvalRunEntry(entry);
    if (!state.selectedEvalRunDir) {
      state.selectedEvalRunDir = evalRunKey(entry);
    }
    renderEvalRunsList();
    ensureEvalRunCostSummaries(state.selectedTaskSnapshot, { generation: currentTaskSelectionGeneration() }).catch(() => {});
  }

  function hasActiveTextSelection() {
    try {
      const sel = window.getSelection ? window.getSelection() : null;
      if (!sel || sel.isCollapsed) return false;
      const text = String(sel.toString() || '').trim();
      if (!text) return false;
      const anchorNode = sel.anchorNode;
      const focusNode = sel.focusNode;
      const anchorEl = anchorNode?.nodeType === Node.ELEMENT_NODE ? anchorNode : anchorNode?.parentElement;
      const focusEl = focusNode?.nodeType === Node.ELEMENT_NODE ? focusNode : focusNode?.parentElement;
      const inApp = (el) => !!el?.closest?.('.app-shell');
      return inApp(anchorEl) || inApp(focusEl);
    } catch {
      return false;
    }
  }
