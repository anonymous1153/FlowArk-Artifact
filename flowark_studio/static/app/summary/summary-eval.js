  function findEvalRunEntry(runDir) {
    const target = normalizePathForCompare(runDir);
    if (!target) return null;
    return state.evalRuns.find((x) => evalRunKey(x) === target) || null;
  }

  function normalizeSinkTypesDisplay(value) {
    if (Array.isArray(value)) return value.filter((x) => String(x || '').trim()).join(',');
    if (typeof value === 'string') return value.trim();
    return '';
  }

  function extractEvalRunMetaFromPayload(payload) {
    if (!payload || typeof payload !== 'object') return {};
    const out = {};
    const sourceObj = payload.source && typeof payload.source === 'object' ? payload.source : null;
    const dataflows = Array.isArray(payload.dataflows) ? payload.dataflows : [];
    const pick = (targetKey, candidateKeys) => {
      for (const key of candidateKeys) {
        const value = payload[key];
        if (value == null) continue;
        if (typeof value === 'string' && !value.trim()) continue;
        if (Array.isArray(value) && !value.length) continue;
        out[targetKey] = value;
        return;
      }
    };
    pick('query', ['query', 'generated_query']);
    if (sourceObj && typeof sourceObj.description === 'string' && sourceObj.description.trim()) {
      out.source = sourceObj.description;
    } else {
      pick('source', ['source_description', 'generated_source', 'source']);
    }
    if (dataflows.length) {
      const sinkTypes = [];
      dataflows.forEach((item) => {
        const sink = item && typeof item === 'object' ? item.sink : null;
        const sinkType = sink && typeof sink === 'object' ? sink.sink_type : null;
        if (typeof sinkType === 'string' && sinkType.trim()) sinkTypes.push(sinkType.trim());
      });
      if (sinkTypes.length) out.sink_types = Array.from(new Set(sinkTypes));
    }
    if (out.sink_types == null) pick('sink_types', ['sink_types', 'sink_type']);
    pick('app_name', ['app_name']);
    pick('agent_mode', ['agent_mode']);
    pick('knowledge_mode', ['knowledge_mode']);
    pick('cwd', ['cwd']);
    if (Array.isArray(payload.health_issues)) {
      out.health_issues = payload.health_issues;
    }
    return out;
  }

  function formatEvalSummaryUsd(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) return '';
    return `$${num.toFixed(4)}`;
  }


  async function ensureEvalRunMetadata(task, runDir) {
    if (!task || task.kind !== 'eval') return;
    const normalized = normalizePathForCompare(runDir);
    if (!normalized) return;
    if (state.evalRunMetaByDir[normalized]) return;
    if (state.evalRunMetaLoadingByDir[normalized]) return;
    state.evalRunMetaLoadingByDir[normalized] = true;
    try {
      const entry = findEvalRunEntry(normalized) || {};
      const merged = { ...entry };
      const artifactDir = resolveEvalRunArtifactDir(entry);
      const candidates = [
        buildEvalRunFilePath(entry.repeat_dir, 'result.json'),
        buildEvalRunFilePath(artifactDir, 'run_meta.json'),
        buildEvalRunFilePath(entry.repeat_dir, 'normalized_case.json'),
        buildEvalRunFilePath(artifactDir, 'final_report.json'),
      ].filter(Boolean);
      if (!candidates.length) {
        state.evalRunMetaByDir[normalized] = merged;
        return;
      }
      for (const path of candidates) {
        try {
          const data = await api(`/api/tasks/${task.task_id}/artifact?path=${encodeURIComponent(path)}`);
          const payload = data?.json && typeof data.json === 'object' ? data.json : null;
          if (payload) {
            Object.assign(merged, extractEvalRunMetaFromPayload(payload));
          }
        } catch {
          // Try next candidate.
        }
      }
      if (merged.sink_types != null) {
        merged.sink_types = normalizeSinkTypesDisplay(merged.sink_types);
      }
      state.evalRunMetaByDir[normalized] = merged;
    } finally {
      state.evalRunMetaLoadingByDir[normalized] = false;
      if (task.task_id === state.selectedTaskId) {
        renderTaskSummary(task);
        ensureKnowledgeSkillContentsForCurrentRun(task).then(() => {
          rerenderTranscriptPane({ autoScroll: false });
        }).catch(() => {});
      }
    }
  }
