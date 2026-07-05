  const I18N_STORAGE_KEY = 'flowark-studio-language';
  const I18N_DEFAULT_LANGUAGE = 'en';
  const I18N_LABELS = {
    en: {
      app_ready: 'ready',
      app_connecting: 'connecting...',
      language_button: 'ZH',
    },
    zh: {
      app_ready: 'ready',
      app_connecting: 'connecting...',
      language_button: 'EN',
    },
  };
  const I18N_TEXT_EN = new Map(Object.entries({
    '◧ 左侧栏': '◧ Sidebar',
    '◧ 隐藏任务列表': '◧ Hide tasks',
    '◧ 显示任务列表': '◧ Show tasks',
    '右侧栏 ◨': 'Form ◨',
    '隐藏表单 ◨': 'Hide form ◨',
    '显示表单 ◨': 'Show form ◨',
    '收起/展开任务列表': 'Collapse/expand task list',
    '收起/展开表单侧栏': 'Collapse/expand launch form',
    '查看当前任务详情': 'View current task details',
    '切换明暗主题': 'Toggle light/dark theme',
    '任务详情': 'Task details',
    '停止任务': 'Stop',
    '暂停评测': 'Pause eval',
    '断点续跑': 'Resume',
    '刷新产物': 'Refresh artifacts',
    '刷新任务列表': 'Refresh tasks',
    '主题': 'Theme',
    '任务列表': 'Tasks',
    '新建': 'New',
    '全部展开': 'Expand all',
    '全部折叠': 'Collapse all',
    '添加': 'Add',
    '清空': 'Clear',
    '选择已有标签': 'Select existing tag',
    '运行详情': 'Run details',
    '收起': 'Collapse',
    '展开': 'Expand',
    'Agent Transcript': 'Agent transcript',
    'Cost Summary': 'Cost summary',
    '卡片模式': 'Cards',
    '源码模式': 'Source',
    '格式化: 开': 'Format: on',
    '格式化: 关': 'Format: off',
    '卡片视图: 开': 'Card view: on',
    '卡片视图: 关': 'Card view: off',
    '自动配对: 开': 'Pairing: on',
    '自动配对: 关': 'Pairing: off',
    '自动换行: 开': 'Wrap: on',
    '自动换行: 关': 'Wrap: off',
    '全屏': 'Fullscreen',
    '退出全屏': 'Exit fullscreen',
    '关键产物': 'Key artifacts',
    '产物内容': 'Artifact content',
    'Eval 启动': 'Launch eval',
    '启动任务': 'Start eval',
    '恢复默认值': 'Reset defaults',
    '任务详情（Eval）': 'Eval details',
    '运行详情（子 Run）': 'Sub-run details',
    '快速编辑标签': 'Edit tags',
    '请选择一个任务。': 'Select a task.',
    '关闭': 'Close',
    '复制这个标签': 'Copy this tag',
    '未标记': 'Untagged',
    '无标签': 'No tags',
    '暂无标签': 'No tags',
    '加载中': 'Loading',
    '浅色': 'Light',
    '深色': 'Dark',
    '显示任务列表': 'Show tasks',
    '隐藏任务列表': 'Hide tasks',
    '显示表单': 'Show form',
    '隐藏表单': 'Hide form',
    '状态': 'Status',
    '模式': 'Mode',
    '耗时': 'Elapsed',
    '输入': 'Input',
    '基础信息': 'Basics',
    '评测输入': 'Eval input',
    '实验方案': 'Experiment preset',
    '输入与采样': 'Input and sampling',
    'FlowArk 参数': 'FlowArk parameters',
    'Agent / OpenCode': 'Agent / OpenCode',
    '后端与 Judge': 'Backends',
    '调试与兼容': 'Debug and compatibility',
    '评估输入文件': 'Eval input file',
    'App 名称过滤': 'App name filter',
    'Studio 便捷入口；每次 eval 只代表一个干净实验条件。': 'Studio shortcut; each eval starts one clean experimental condition.',
    '支持聚合 sources[] JSON / 单条 JSON / JSONL。': 'Supports aggregated sources[] JSON, single JSON, or JSONL.',
    '逗号分隔，仅运行指定 app_name（大小写不敏感精确匹配）。': 'Comma-separated app_name filter (case-insensitive exact match).',
    '模式设置（聚合）': 'Preset settings',
    '分析输入（query/source/sink）': 'Analysis input (query/source/sink)',
    '正在加载 Eval Runs...': 'Loading eval runs...',
    '请先在左侧 Eval Runs 中选择一个子 run': 'Select a sub-run from Eval Runs.',
    '正在读取该 run 的 query/source/sink 元数据...': 'Loading query/source/sink metadata for this run...',
    '暂无': 'None',
    '暂无模式参数': 'No preset parameters',
    '暂无性能信息': 'No performance data',
    '正在读取性能信息...': 'Loading performance data...',
    '暂无指标': 'No metrics',
    '未命名卡片': 'Untitled card',
    '暂无 Transcript 内容': 'No transcript content',
    '暂无 cost_summary.json': 'No cost_summary.json',
    'Cost Summary 概览': 'Cost summary overview',
    '本次运行的总体开销与消息规模': 'Run cost and message scale',
    'OpenCode 聚合': 'OpenCode aggregate',
    'Mem0 后端': 'Mem0 backend',
    'OpenCode + Mem0': 'OpenCode + Mem0',
    '聚合视图': 'Aggregate',
    '阶段开销': 'Phase costs',
    '暂无 phase 数据': 'No phase data',
    '缺少必填字段：': 'Missing required fields: ',
    '启动 Eval 任务': 'Start eval task',
    '启动失败': 'Start failed',
    '任务列表已刷新': 'Task list refreshed',
    '刷新任务列表失败': 'Refresh failed',
    '刷新关键产物': 'Refresh key artifacts',
    '关键产物已刷新': 'Key artifacts refreshed',
    '刷新产物失败': 'Refresh artifacts failed',
    '打开产物': 'Open artifact',
    '产物已打开': 'Artifact opened',
    '打开产物失败': 'Open artifact failed',
    '已有任务列表刷新进行中，本次请求已忽略': 'A refresh is already running; this request was ignored.',
    '已有关键产物刷新进行中，本次请求已忽略': 'An artifact refresh is already running; this request was ignored.',
    '请等待当前刷新完成后再试。': 'Wait for the current refresh to finish.',
    '主动拉取最新任务列表，并刷新任务摘要与并发状态。': 'Fetch the latest tasks and refresh task summaries.',
    '公开版 Studio 只展示 Eval 任务。': 'The public Studio only shows eval tasks.',
    '请先选择任务': 'Select a task first.',
    '没有可复制的内容': 'Nothing to copy',
    '浏览器不支持复制到剪贴板': 'This browser cannot copy to the clipboard',
    '操作失败': 'Operation failed',
    '操作成功': 'Operation succeeded',
    '未命名操作': 'Untitled operation',
    '确认继续吗？': 'Continue?',
    '用户取消了操作': 'Operation cancelled by user',
    '停止失败': 'Stop failed',
    '暂停失败': 'Pause failed',
    '断点续跑失败': 'Resume failed',
    '已发送停止任务请求': 'Stop request sent',
    '已发送暂停评测请求': 'Pause request sent',
    '已发送断点续跑请求': 'Resume request sent',
    '任务列表已刷新，等待后端更新最终状态。': 'Task list refreshed; waiting for the backend to update final status.',
    '当前活跃 sub-run 完成后，评测会进入暂停状态。': 'The eval will pause after the active sub-run finishes.',
    '任务列表已刷新，等待评测恢复执行。': 'Task list refreshed; waiting for the eval to resume.',
    '确认停止当前评测任务吗？这会终止整个 eval 进程。': 'Stop the current eval task? This will terminate the whole eval process.',
    '确认停止当前任务吗？': 'Stop the current task?',
    '确认在当前活跃 sub-run 结束后暂停整个评测吗？': 'Pause the whole eval after the active sub-run finishes?',
    '已取消停止任务': 'Stop cancelled',
    '已取消暂停评测': 'Pause cancelled',
    '断点续跑评测': 'Resume eval',
    '知识注入': 'Knowledge injection',
    '无显式匹配理由': 'No explicit match reason',
    '来源：日志中的文件快照': 'Source: file snapshot in log',
    '来源：当前磁盘文件': 'Source: current file on disk',
    '来源：日志记录的实际注入文本': 'Source: actual injected text in log',
    '查看知识原文（日志中的文件快照）': 'View raw knowledge (file snapshot in log)',
    '查看知识原文（当前磁盘文件）': 'View raw knowledge (current file on disk)',
    '查看实际注入到 prompt 的文本（日志）': 'View actual text injected into the prompt (log)',
    '注入关键依据': 'Injection rationale',
    '展开注入匹配详情': 'Expand injection match details',
    '本次未实际注入任何知识。': 'No knowledge was injected in this run.',
    '无仅匹配未注入知识。': 'No matched-only knowledge.',
    '暂无聚合项。': 'No aggregate entries.',
    '工具调用配对': 'Tool-call pairing',
    '已配对': 'Paired',
    '等待结果': 'Waiting for result',
    '调用未找到': 'Call not found',
    '未知': 'Unknown',
  }));
  const I18N_PHRASE_EN = [
    ['。Studio 将按所选实验方案启动 eval。', '. Studio will start the eval with the selected experiment preset.'],
    ['Studio 将按所选实验方案启动 eval。', 'Studio will start the eval with the selected experiment preset.'],
    ['实验方案', 'Experiment preset'],
    ['输入与采样', 'Input and sampling'],
    ['评估输入文件', 'Eval input file'],
    ['App 名称过滤', 'App name filter'],
    ['详情渲染失败:', 'Failed to render details:'],
    ['刷新任务列表失败:', 'Refresh failed:'],
    ['刷新产物失败:', 'Refresh artifacts failed:'],
    ['缺少必填字段：', 'Missing required fields: '],
    ['缺失字段:', 'Missing fields:'],
    ['任务 ID:', 'Task ID:'],
    ['当前产物数:', 'Current artifact count:'],
    ['请求:', 'Request:'],
    ['错误:', 'Error:'],
    ['响应:', 'Response:'],
    ['确认提示:', 'Confirmation prompt:'],
    ['卡片列表格式展示 Transcript', 'card-list transcript view'],
    ['普通视图与全屏都会使用卡片模式', 'card mode applies in normal and fullscreen views'],
    ['仅在卡片视图开启时生效', 'only works when card view is enabled'],
    ['卡片模式下自动按 tool_call_id 成对展示 ToolUse/ToolResult', 'auto-pair ToolUse/ToolResult by tool_call_id in card mode'],
    ['已截断', 'Truncated'],
    ['完整内容请打开 raw artifact。', 'Open the raw artifact for the full content.'],
    ['展开其余字段', 'Expand remaining fields'],
    ['结果 #', 'Result #'],
    ['调用 #', 'Call #'],
    ['按 skill 聚合', 'Aggregate by skill'],
    ['查看匹配但未注入的知识', 'View matched but not injected knowledge'],
    ['source方法', 'source method'],
    ['source语句/调用', 'source statement/call'],
    ['source类', 'source class'],
  ];
  const I18N_REGEX_EN = [
    {
      pattern: /(\d+)\s*项/g,
      replacement: (_match, count) => `${count} ${Number(count) === 1 ? 'item' : 'items'}`,
    },
    {
      pattern: /(\d+)\s*行/g,
      replacement: (_match, count) => `${count} ${Number(count) === 1 ? 'line' : 'lines'}`,
    },
    {
      pattern: /injected:\s*(\d+)\s*\/\s*matched-only:\s*(\d+)/g,
      replacement: 'injected: $1 / matched-only: $2',
    },
    {
      pattern: /。/g,
      replacement: '.',
    },
  ];

  function i18nLabel(key) {
    const lang = state.language === 'zh' ? 'zh' : 'en';
    return I18N_LABELS[lang]?.[key] || I18N_LABELS.en[key] || key;
  }

  function shouldSkipI18nNode(node) {
    const parent = node?.parentElement;
    if (!parent) return true;
    return !!parent.closest(
      'pre, code, .code-text, .line-gutter, #transcript-log, #artifact-content, #cost-summary-log, .artifact-path',
    );
  }

  function translateText(text) {
    if (state.language !== 'en') return text;
    const trimmed = String(text || '').trim();
    if (!trimmed) return text;
    if (I18N_TEXT_EN.has(trimmed)) {
      return String(text).replace(trimmed, I18N_TEXT_EN.get(trimmed));
    }
    let next = String(text);
    for (const [source, target] of [...I18N_PHRASE_EN].sort((a, b) => b[0].length - a[0].length)) {
      if (next.includes(source)) next = next.split(source).join(target);
    }
    for (const { pattern, replacement } of I18N_REGEX_EN) {
      next = next.replace(pattern, replacement);
    }
    return next;
  }

  function applyI18n(root = document.body) {
    if (!root) return;
    document.documentElement.lang = state.language === 'zh' ? 'zh-CN' : 'en';
    if (els.languageToggleBtn) {
      els.languageToggleBtn.textContent = i18nLabel('language_button');
      els.languageToggleBtn.title = state.language === 'en' ? 'Switch to Chinese' : '切换到英文';
    }
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const textNodes = [];
    while (walker.nextNode()) textNodes.push(walker.currentNode);
    for (const node of textNodes) {
      if (shouldSkipI18nNode(node)) continue;
      const next = translateText(node.nodeValue);
      if (next !== node.nodeValue) node.nodeValue = next;
    }
    const attrNames = ['title', 'placeholder', 'aria-label'];
    const elements = root.querySelectorAll ? root.querySelectorAll('[title], [placeholder], [aria-label], option') : [];
    for (const el of elements) {
      if (el.closest?.('pre, code, .code-text, .line-gutter')) continue;
      for (const attr of attrNames) {
        if (!el.hasAttribute?.(attr)) continue;
        const current = el.getAttribute(attr);
        const next = translateText(current);
        if (next !== current) el.setAttribute(attr, next);
      }
    }
  }

  let i18nObserver = null;

  function initLanguage() {
    const saved = localStorage.getItem(I18N_STORAGE_KEY);
    state.language = saved === 'zh' ? 'zh' : I18N_DEFAULT_LANGUAGE;
    if (i18nObserver) i18nObserver.disconnect();
    i18nObserver = new MutationObserver((mutations) => {
      if (state.language !== 'en') return;
      for (const mutation of mutations) {
        for (const node of mutation.addedNodes || []) {
          if (node.nodeType === Node.TEXT_NODE && !shouldSkipI18nNode(node)) {
            const next = translateText(node.nodeValue);
            if (next !== node.nodeValue) node.nodeValue = next;
          } else if (node.nodeType === Node.ELEMENT_NODE) {
            applyI18n(node);
          }
        }
        if (mutation.type === 'characterData' && mutation.target?.nodeType === Node.TEXT_NODE && !shouldSkipI18nNode(mutation.target)) {
          const next = translateText(mutation.target.nodeValue);
          if (next !== mutation.target.nodeValue) mutation.target.nodeValue = next;
        }
      }
    });
    i18nObserver.observe(document.body, { childList: true, characterData: true, subtree: true });
    applyI18n();
  }

  function toggleLanguage() {
    state.language = state.language === 'en' ? 'zh' : 'en';
    localStorage.setItem(I18N_STORAGE_KEY, state.language);
    window.location.reload();
  }
