  function parsePythonRepr(str, pos = 0) {
    const len = str.length;
    let i = pos;

    function skipWhitespace() {
      while (i < len && /\s/.test(str[i])) i++;
    }

    function parseValue() {
      skipWhitespace();
      if (i >= len) return { value: null, pos: i };

      const char = str[i];

      // String (single or double quotes)
      if (char === "'" || char === '"') {
        const quote = char;
        i++;
        let result = '';
        while (i < len) {
          if (str[i] === '\\' && i + 1 < len) {
            const next = str[i + 1];
            if (next === 'n') { result += '\n'; i += 2; }
            else if (next === 't') { result += '\t'; i += 2; }
            else if (next === 'r') { result += '\r'; i += 2; }
            else if (next === '\\') { result += '\\'; i += 2; }
            else if (next === quote) { result += quote; i += 2; }
            else { result += str[i] + str[i + 1]; i += 2; }
          } else if (str[i] === quote) {
            i++;
            break;
          } else {
            result += str[i];
            i++;
          }
        }
        return { value: result, pos: i };
      }

      // Number
      if (char === '-' || /\d/.test(char)) {
        let numStr = '';
        if (char === '-') { numStr += char; i++; }
        while (i < len && /\d/.test(str[i])) { numStr += str[i]; i++; }
        if (i < len && str[i] === '.') {
          numStr += str[i]; i++;
          while (i < len && /\d/.test(str[i])) { numStr += str[i]; i++; }
        }
        return { value: parseFloat(numStr), pos: i };
      }

      // Boolean/None
      if (str.slice(i, i + 4) === 'True') { i += 4; return { value: true, pos: i }; }
      if (str.slice(i, i + 5) === 'False') { i += 5; return { value: false, pos: i }; }
      if (str.slice(i, i + 4) === 'None') { i += 4; return { value: null, pos: i }; }

      // List
      if (char === '[') {
        i++;
        const arr = [];
        skipWhitespace();
        if (str[i] === ']') { i++; return { value: arr, pos: i }; }
        while (i < len) {
          const result = parseValue();
          arr.push(result.value);
          i = result.pos;
          skipWhitespace();
          if (str[i] === ']') { i++; break; }
          if (str[i] === ',') { i++; skipWhitespace(); }
          else break;
        }
        return { value: arr, pos: i };
      }

      // Dict
      if (char === '{') {
        i++;
        const obj = {};
        skipWhitespace();
        if (str[i] === '}') { i++; return { value: obj, pos: i }; }
        while (i < len) {
          const keyResult = parseValue();
          const key = keyResult.value;
          i = keyResult.pos;
          skipWhitespace();
          if (str[i] === ':') { i++; skipWhitespace(); }
          const valResult = parseValue();
          obj[key] = valResult.value;
          i = valResult.pos;
          skipWhitespace();
          if (str[i] === '}') { i++; break; }
          if (str[i] === ',') { i++; skipWhitespace(); }
          else break;
        }
        return { value: obj, pos: i };
      }

      // Class instance: ClassName(field=value, ...)
      if (/[A-Za-z_]/.test(char)) {
        let className = '';
        while (i < len && /[A-Za-z0-9_]/.test(str[i])) { className += str[i]; i++; }
        skipWhitespace();
        if (str[i] === '(') {
          i++;
          const obj = { __class__: className };
          skipWhitespace();
          if (str[i] === ')') { i++; return { value: obj, pos: i }; }
          while (i < len) {
            // Parse keyword argument: key=value
            let key = '';
            while (i < len && /[A-Za-z0-9_]/.test(str[i])) { key += str[i]; i++; }
            skipWhitespace();
            if (str[i] === '=') { i++; skipWhitespace(); }
            const valResult = parseValue();
            obj[key] = valResult.value;
            i = valResult.pos;
            skipWhitespace();
            if (str[i] === ')') { i++; break; }
            if (str[i] === ',') { i++; skipWhitespace(); }
            else break;
          }
          return { value: obj, pos: i };
        }
        return { value: className, pos: i };
      }

      // Unknown - skip one char
      i++;
      return { value: null, pos: i };
    }

    return parseValue();
  }

  // Format a parsed object as readable text
  function formatParsedObject(obj, indent = 0) {
    const spaces = '  '.repeat(indent);
    const nextSpaces = '  '.repeat(indent + 1);

    if (obj === null) return 'null';
    if (typeof obj === 'boolean') return obj ? 'true' : 'false';
    if (typeof obj === 'number') return String(obj);
    if (typeof obj === 'string') {
      // Escape and quote string
      const escaped = obj.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\n/g, '\\n');
      return `"${escaped}"`;
    }

    if (Array.isArray(obj)) {
      if (obj.length === 0) return '[]';
      const items = obj.map(item => nextSpaces + formatParsedObject(item, indent + 1));
      return '[\n' + items.join(',\n') + '\n' + spaces + ']';
    }

    if (typeof obj === 'object') {
      const keys = Object.keys(obj);
      if (keys.length === 0) return '{}';

      // Put __class__ first
      keys.sort((a, b) => {
        if (a === '__class__') return -1;
        if (b === '__class__') return 1;
        return a.localeCompare(b);
      });

      const items = keys.map(key => {
        const val = formatParsedObject(obj[key], indent + 1);
        return `${nextSpaces}${key}: ${val}`;
      });
      return '{\n' + items.join(',\n') + '\n' + spaces + '}';
    }

    return String(obj);
  }

  function splitTranscriptPrefix(raw) {
    const text = String(raw || '');
    const prefixMatch = text.match(/^\[([^\]]+)\]\s+([\s\S]*)$/);
    if (!prefixMatch) return { prefix: null, body: text };
    return { prefix: prefixMatch[1], body: prefixMatch[2] };
  }

  function isOpenCodeTranscriptHeaderLine(line) {
    const { body } = splitTranscriptPrefix(line);
    return /^OpenCode\s+[A-Za-z_-]+(?:\s+\S+)?\s*$/.test(String(body || '').trim());
  }

  function isOpenCodeTranscriptContinuationLine(line) {
    const text = String(line || '').trim();
    return /^\[(?:tool:[^\]]+|tool-input|tool-output|step-finish)\]/.test(text);
  }

  function isRunnerTranscriptBoundaryLine(line) {
    const text = String(line || '').trim();
    if (!text.startsWith('[')) return false;
    if (isOpenCodeTranscriptHeaderLine(text) || isOpenCodeTranscriptContinuationLine(text)) return false;
    const match = text.match(/^\[([^\]]+)\]/);
    if (!match) return false;
    const tag = String(match[1] || '').trim().toLowerCase();
    return (
      tag.startsWith('phase:')
      || ['error', 'warning', 'warn', 'info', 'debug', 'trace', 'flowark', 'runner'].includes(tag)
    );
  }

  function splitTranscriptEntries(text) {
    const lines = String(text || '').split('\n');
    const entries = [];
    let current = null;

    const flushCurrent = () => {
      if (!current) return;
      current.text = current.lines.join('\n').replace(/\n+$/g, '');
      current.endLine = current.startLine + current.lines.length - 1;
      entries.push(current);
      current = null;
    };

    for (let i = 0; i < lines.length; i += 1) {
      const line = lines[i];
      if (isOpenCodeTranscriptHeaderLine(line)) {
        flushCurrent();
        current = {
          type: 'opencode',
          startLine: i,
          endLine: i,
          lines: [line],
          text: line,
        };
        continue;
      }
      if (current && isRunnerTranscriptBoundaryLine(line)) {
        flushCurrent();
        entries.push({
          type: 'line',
          startLine: i,
          endLine: i,
          text: line,
        });
        continue;
      }
      if (current) {
        current.lines.push(line);
        continue;
      }
      entries.push({
        type: 'line',
        startLine: i,
        endLine: i,
        text: line,
      });
    }
    flushCurrent();
    return entries;
  }

  function parseJsonMaybe(value) {
    const text = String(value ?? '').trim();
    if (!text) return '';
    if (!/^[{["0-9tfn-]/.test(text)) return value;
    try { return JSON.parse(text); } catch { return value; }
  }

  function formatOpenCodeBlockValue(value) {
    if (value == null) return '';
    if (typeof value === 'string') return normalizeMultilineText(value);
    return formatParsedObject(value);
  }

  function buildTranscriptTextPreview(text, { maxChars = 4000, maxLines = 80 } = {}) {
    const raw = String(text || '');
    const lines = raw.split('\n');
    let previewLines = lines;
    let truncatedByLines = false;
    if (lines.length > maxLines) {
      previewLines = lines.slice(0, maxLines);
      truncatedByLines = true;
    }
    let preview = previewLines.join('\n');
    let truncatedByChars = false;
    if (preview.length > maxChars) {
      preview = preview.slice(0, maxChars);
      truncatedByChars = true;
    }
    const truncated = truncatedByLines || truncatedByChars || raw.length > preview.length;
    const omittedLines = Math.max(0, lines.length - preview.split('\n').length);
    const omittedChars = Math.max(0, raw.length - preview.length);
    if (truncated) {
      const notes = [];
      if (omittedLines) notes.push(`${omittedLines} lines`);
      if (omittedChars) notes.push(`${omittedChars} chars`);
      preview += `\n[studio-preview-truncated: ${notes.join(', ') || 'content'} omitted; open raw artifact for full text]`;
    }
    return {
      text: preview,
      truncated,
      lineCount: lines.length,
      charCount: raw.length,
      omittedLines,
      omittedChars,
    };
  }

  function utf8ByteLength(text) {
    const raw = String(text || '');
    try {
      return new TextEncoder().encode(raw).length;
    } catch {
      return raw.length;
    }
  }

  function formatTranscriptPreviewValue(value, options = {}) {
    return buildTranscriptTextPreview(formatOpenCodeBlockValue(value), options).text;
  }

  function buildOpenCodeToolFieldPreview(rawValue, options = {}) {
    const raw = String(rawValue ?? '');
    const rawStats = buildTranscriptTextPreview(raw, options);
    const meta = {
      line_count: raw.split('\n').length,
      byte_count: utf8ByteLength(raw),
      char_count: raw.length,
      truncated: rawStats.truncated,
    };
    if (rawStats.truncated) {
      return {
        value: rawStats.text,
        content: rawStats.text,
        meta,
      };
    }
    const parsed = parseJsonMaybe(raw);
    const formattedStats = buildTranscriptTextPreview(formatOpenCodeBlockValue(parsed), options);
    return {
      value: formattedStats.truncated ? formattedStats.text : parsed,
      content: formattedStats.text,
      meta: {
        ...meta,
        truncated: formattedStats.truncated,
      },
    };
  }

  function isOpenCodeStepFinishBlock(block) {
    if (!block || typeof block !== 'object') return false;
    const blockType = String(block.__class__ || block.type || '').toLowerCase();
    return blockType.includes('opencodestepfinish') || block.type === 'step-finish';
  }

  function extractOpenCodeStepFinish(block) {
    if (!isOpenCodeStepFinishBlock(block)) return null;
    const tokens = block.tokens && typeof block.tokens === 'object' ? block.tokens : null;
    return {
      reason: block.reason == null ? '' : String(block.reason),
      cost: block.cost,
      tokens,
    };
  }

  function parseOpenCodeTranscriptBlock(blockText) {
    const raw = String(blockText || '').replace(/\n+$/g, '');
    if (!raw.trim()) return null;
    const lines = raw.split('\n');
    const first = splitTranscriptPrefix(lines[0] || '');
    const headerMatch = String(first.body || '').trim().match(/^OpenCode\s+([A-Za-z_-]+)(?:\s+(\S+))?\s*$/);
    if (!headerMatch) return null;

    const role = headerMatch[1] || 'message';
    const messageId = headerMatch[2] || '';
    const obj = {
      __class__: `OpenCode ${role.charAt(0).toUpperCase()}${role.slice(1)}Message`,
      adapter: 'opencode',
      role,
      id: messageId,
      content: [],
    };
    if (first.prefix) obj.prefix = first.prefix;

    let textLines = [];
    let currentTool = null;
    let activeToolField = '';

    const flushText = () => {
      const text = textLines.join('\n').trim();
      if (text) {
        obj.content.push({ __class__: 'TextBlock', type: 'text', text });
      }
      textLines = [];
    };

    const flushTool = () => {
      if (!currentTool) return;
      if (typeof currentTool._inputRaw === 'string') {
        const inputPreview = buildOpenCodeToolFieldPreview(currentTool._inputRaw, { maxChars: 2000, maxLines: 40 });
        currentTool.input = inputPreview.value;
        currentTool.input_meta = inputPreview.meta;
      }
      if (typeof currentTool._outputRaw === 'string') {
        const outputPreview = buildOpenCodeToolFieldPreview(currentTool._outputRaw, { maxChars: 5000, maxLines: 90 });
        currentTool.output = outputPreview.value;
        currentTool.content = outputPreview.content;
        currentTool.output_meta = outputPreview.meta;
      }
      delete currentTool._inputRaw;
      delete currentTool._outputRaw;
      obj.content.push(currentTool);
      currentTool = null;
      activeToolField = '';
    };

    const ensureTool = () => {
      if (!currentTool) {
        currentTool = {
          __class__: 'OpenCodeToolBlock',
          type: 'tool',
          name: 'unknown',
          tool: 'unknown',
        };
      }
      return currentTool;
    };

    for (let i = 1; i < lines.length; i += 1) {
      const line = lines[i];
      const toolMatch = line.match(/^\[tool:([^\]]+)\]\s*([A-Za-z_-]+)?\s*(.*)$/);
      if (toolMatch) {
        flushText();
        flushTool();
        const toolName = String(toolMatch[1] || '').trim();
        currentTool = {
          __class__: 'OpenCodeToolBlock',
          type: 'tool',
          name: toolName,
          tool: toolName,
          status: String(toolMatch[2] || '').trim(),
          title: String(toolMatch[3] || '').trim(),
        };
        activeToolField = '';
        continue;
      }

      const inputMatch = line.match(/^\[tool-input\]\s*([\s\S]*)$/);
      if (inputMatch) {
        flushText();
        const tool = ensureTool();
        tool._inputRaw = String(inputMatch[1] || '');
        activeToolField = 'input';
        continue;
      }

      const outputMatch = line.match(/^\[tool-output\]\s*([\s\S]*)$/);
      if (outputMatch) {
        flushText();
        const tool = ensureTool();
        tool._outputRaw = String(outputMatch[1] || '');
        activeToolField = 'output';
        continue;
      }

      const stepMatch = line.match(/^\[step-finish\]\s*([\s\S]*)$/);
      if (stepMatch) {
        flushText();
        flushTool();
        const payload = parseJsonMaybe(stepMatch[1] || '');
        const step = {
          __class__: 'OpenCodeStepFinish',
          type: 'step-finish',
        };
        if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
          Object.assign(step, payload);
        } else if (payload) {
          step.content = String(payload);
        }
        obj.content.push(step);
        activeToolField = '';
        continue;
      }

      if (currentTool && activeToolField === 'input') {
        currentTool._inputRaw = `${currentTool._inputRaw || ''}\n${line}`;
        continue;
      }
      if (currentTool && activeToolField === 'output') {
        currentTool._outputRaw = `${currentTool._outputRaw || ''}\n${line}`;
        continue;
      }
      textLines.push(line);
    }

    flushText();
    flushTool();
    return { prefix: first.prefix, body: raw, parsed: obj };
  }

  function tryParseTranscriptLine(line) {
    const raw = String(line || '');
    if (!raw.trim()) return null;
    const openCodeParsed = parseOpenCodeTranscriptBlock(raw);
    if (openCodeParsed) return openCodeParsed;
    const { prefix, body } = splitTranscriptPrefix(raw);
    try {
      const result = parsePythonRepr(body);
      if (result && result.value && typeof result.value === 'object') {
        return { prefix, body, parsed: result.value };
      }
    } catch {
      // fall through
    }
    return { prefix, body, parsed: null };
  }

  function normalizeMultilineText(value) {
    if (value == null) return '';
    let text = '';
    if (typeof value === 'string') text = value;
    else {
      try { text = JSON.stringify(value, null, 2); } catch { text = String(value); }
    }
    return text.replace(/\\n/g, '\n');
  }

  function summarizeContentBlock(block) {
    if (!block || typeof block !== 'object') return String(block ?? '');
    const blockType = String(block.__class__ || block.type || '').toLowerCase();
    const toolFieldMetaText = (meta) => {
      if (!meta || typeof meta !== 'object') return '';
      const parts = [];
      if (meta.line_count != null) parts.push(`lines=${meta.line_count}`);
      if (meta.byte_count != null) parts.push(`bytes=${meta.byte_count}`);
      if (meta.truncated) parts.push('truncated=true');
      return parts.join(' ');
    };
    if (blockType.includes('opencodetool') || (block.type === 'tool' && (block.name || block.tool))) {
      const toolName = block.name || block.tool || 'unknown';
      const rows = [`OpenCodeTool: ${toolName}`];
      if (block.status) rows.push(`status: ${block.status}`);
      if (block.title) rows.push(`title: ${block.title}`);
      const inputMeta = toolFieldMetaText(block.input_meta);
      const outputMeta = toolFieldMetaText(block.output_meta);
      if (inputMeta) rows.push(`input_meta: ${inputMeta}`);
      if (block.input != null && block.input !== '') {
        rows.push(`input:\n${formatTranscriptPreviewValue(block.input, { maxChars: 2000, maxLines: 40 })}`);
      }
      if (outputMeta) rows.push(`output_meta: ${outputMeta}`);
      if (block.output != null && block.output !== '') {
        rows.push(`output:\n${formatTranscriptPreviewValue(block.output, { maxChars: 5000, maxLines: 90 })}`);
      } else if (block.content) {
        rows.push(`output:\n${formatTranscriptPreviewValue(block.content, { maxChars: 5000, maxLines: 90 })}`);
      }
      return rows.join('\n');
    }
    if (isOpenCodeStepFinishBlock(block)) return '';
    if (typeof block.text === 'string' && block.text.trim()) {
      return buildTranscriptTextPreview(block.text, { maxChars: 8000, maxLines: 120 }).text;
    }
    if (typeof block.content === 'string' && block.content.trim()) {
      return buildTranscriptTextPreview(block.content, { maxChars: 8000, maxLines: 120 }).text;
    }
    if (typeof block.name === 'string' && block.name.trim()) {
      let body = `${block.__class__ || 'ToolUse'}: ${block.name}`;
      if (block.input && typeof block.input === 'object') {
        body += `\ninput:\n${formatTranscriptPreviewValue(block.input, { maxChars: 2000, maxLines: 40 })}`;
      }
      return body;
    }
    if (block.tool_use_id && block.tool_use_result) {
      const resultValue = block.content != null ? block.content : block.tool_use_result;
      const content = typeof block.content === 'string'
        ? buildTranscriptTextPreview(resultValue, { maxChars: 5000, maxLines: 90 }).text
        : formatTranscriptPreviewValue(resultValue, { maxChars: 5000, maxLines: 90 });
      return `ToolResult(${block.tool_use_id})\n${content}`;
    }
    return formatTranscriptPreviewValue(block, { maxChars: 5000, maxLines: 90 });
  }

  function extractTranscriptPriorityFields(obj) {
    const cls = typeof obj.__class__ === 'string' ? obj.__class__ : '';
    const model = (typeof obj.model === 'string' && obj.model)
      || (typeof obj.model_id === 'string' && obj.model_id)
      || (obj.data && typeof obj.data.model === 'string' ? obj.data.model : '')
      || '';
    const error = obj.error == null ? '' : normalizeMultilineText(obj.error);

    // Extract tool call ID for pairing (from content blocks)
    let toolCallId = '';
    let toolName = '';
    let hasToolUse = false;
    let hasToolResult = false;
    let stepFinish = null;

    if (typeof obj.id === 'string' && obj.id.startsWith('call_')) {
      toolCallId = obj.id;
      if (typeof obj.name === 'string') toolName = obj.name;
      hasToolUse = true;
    } else if (typeof obj.tool_use_id === 'string') {
      toolCallId = obj.tool_use_id;
      hasToolResult = true;
    }

    // Also check content blocks for tool info
    if (Array.isArray(obj.content)) {
      for (const block of obj.content) {
        if (block && typeof block === 'object') {
          if (!stepFinish && isOpenCodeStepFinishBlock(block)) {
            stepFinish = extractOpenCodeStepFinish(block);
          }
          if (block.type === 'tool' || String(block.__class__ || '').toLowerCase().includes('opencodetool')) {
            const candidateId = block.call_id || block.callID || block.id || '';
            if (candidateId && !toolCallId) toolCallId = String(candidateId);
            if (!toolName && (block.name || block.tool)) toolName = String(block.name || block.tool || '');
            hasToolUse = true;
            if (block.output != null || block.content != null) hasToolResult = true;
          }
          if (typeof block.id === 'string' && block.id.startsWith('call_')) {
            if (!toolCallId) {
              toolCallId = block.id;
              toolName = block.name || '';
            }
            hasToolUse = true;
          }
          if (typeof block.tool_use_id === 'string') {
            if (!toolCallId) {
              toolCallId = block.tool_use_id;
            }
            hasToolResult = true;
          }
        }
      }
    }
    if (!stepFinish && (obj.cost != null || obj.reason != null || obj.tokens != null)) {
      stepFinish = extractOpenCodeStepFinish({ type: 'step-finish', cost: obj.cost, reason: obj.reason, tokens: obj.tokens });
    }

    let content = '';
    if (typeof obj.result === 'string' && obj.result.trim()) {
      content = obj.result;
    } else if (Array.isArray(obj.content)) {
      const parts = obj.content.map(summarizeContentBlock).filter((x) => String(x || '').trim());
      content = parts.join('\n\n');
    } else if (typeof obj.content === 'string' && obj.content.trim()) {
      content = obj.content;
    } else if (obj.data && typeof obj.data === 'object' && obj.data.subtype === 'init') {
      const modelPart = typeof obj.data.model === 'string' ? `model=${obj.data.model}` : '';
      const cwdPart = typeof obj.data.cwd === 'string' ? `cwd=${obj.data.cwd}` : '';
      content = [modelPart, cwdPart].filter(Boolean).join('\n');
    }

    const rest = { ...obj };
    delete rest.__class__;
    delete rest.model;
    delete rest.model_id;
    delete rest.content;
    delete rest.result;
    delete rest.error;
    delete rest.id;
    delete rest.role;
    delete rest.tool_use_id;
    if (stepFinish) {
      delete rest.cost;
      delete rest.reason;
      delete rest.tokens;
    }
    if (rest.data && typeof rest.data === 'object') {
      const shallowData = { ...rest.data };
      if (shallowData.model === model) delete shallowData.model;
      if (Object.keys(shallowData).length === 0) delete rest.data;
      else rest.data = shallowData;
    }

    return {
      className: cls || 'Object',
      model: model || '',
      content: normalizeMultilineText(content),
      error,
      toolCallId,
      toolName,
      hasToolUse,
      hasToolResult,
      stepFinish,
      rest,
    };
  }
