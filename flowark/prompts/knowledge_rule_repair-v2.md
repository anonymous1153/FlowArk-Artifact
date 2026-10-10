【严格输出】只输出 JSON；不要调用工具；不要继续探索；不要 Markdown/解释/代码块。

基于你刚刚在本会话中完成的代码探索与数据流分析，请对下面这条知识候选只做一次“规则边界修复”。

修复目标（必须遵守）：
1. 只修 `match_rules`，必要时可轻微收紧 `entry_condition`。
2. 不要重新分析代码，不要新增新的知识结论，不要改写正文含义，不要使用 Task 或其他 sub-agent。
3. 保留原候选的 `id / name / content / egress_map` 语义不变。
4. 如果无法修出可召回的 `require_all` / `require_any` 组合，就返回 `repairable=false`。
5. 只允许做这些修复：
   - 删除格式错误的 `call`
   - 删除通用框架前缀、通用角色词、过宽或过弱规则
   - 补一个从当前会话代码上下文中可以直接恢复的强规则
   - 轻微收紧 `entry_condition`

当前任务上下文（仅用于理解复用边界，不要重新走完整分析）：
- source_description: {source_desc_json}
- sink_types: {sink_types_json}

待修复候选（JSON）：
{candidate_json}

静态验证给出的失败原因：
{static_reasons_json}

触发本轮修复的规则问题类型：
{issue_types_json}

请仅输出一个 JSON 对象（不要 Markdown、不要代码块围栏），结构必须严格如下：
Rule = {{
  "kind": "exact_symbol" | "symbol_tail" | "package_prefix" | "call",
  "value": string,
  "receiver": string | null,
  "method": string | null
}}

MatchRules = {{
  "require_all": [Rule],
  "require_any": [Rule],
  "exclude": [Rule]
}}

{{
  "schema_version": "flowark-knowledge-rule-repair-v1",
  "candidate_id": string,
  "repairable": boolean,
  "match_rules": MatchRules | null,
  "entry_condition": string | null,
  "notes": [string]
}}

额外要求：
- `candidate_id` 必须与原候选一致。
- 若 `repairable=true`：
  - `require_all` 与 `require_any` 至少有一个非空。
  - `require_all` 用于“泛 API + 具体 key/domain”组合；若包含泛锚点，必须同时包含至少一个非泛、可稳定定位的具体锚点。
  - `require_all` 不能只靠单条 `symbol_tail`、`package_prefix`、`call(method-only)` 或通用框架前缀触发。
  - `require_any` 只能放高度特异、低歧义的主锚点；不允许放 `intent.getStringExtra`、`Intent.EXTRA_TEXT`、`sharedPrefs.get*`、`bundle.putString`、`add`、`onSuccess` 等泛锚点。
  - 不允许只剩 `call(method-only)`。
  - 不允许把 `a|b|c` 塞进 `method`。
  - 不允许把 `receiver.method` 整串塞进 `method`。
  - 不允许只靠通用框架前缀、通用角色词、常见基类名或泛组件名支撑 `require_any`。
- 若 `repairable=false`：
  - `match_rules` 必须为 `null`。
- `notes` 只写简短修复说明，不要重复正文。

## 匹配规则示例

- 场景：Intent extra 的文本只有在随后写入 Amaze 文件 sink 时才适用。预期规则：
  `require_all=[call:intent.getStringExtra, exact_symbol:Intent.EXTRA_TEXT, call:MakeFileOperation.mktextfile]`, `require_any=[]`, `exclude=[]`。
- 场景：SharedPreferences 或 Bundle 的泛读写 API 只有搭配具体配置键或具体下游边界时才适用，并排除测试夹具。预期规则：
  `require_all=[call:sharedPrefs.getInt, exact_symbol:PreferencesConstants.KEY_TRASH_BIN_RETENTION_DAYS, exact_symbol:TrashBinConfig]`,
  `require_any=[exact_symbol:AppConfig.getTrashBinConfig]`,
  `exclude=[package_prefix:com.amaze.filemanager.test]`。
