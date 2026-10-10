【严格输出】只输出 JSON；不要调用工具；不要继续探索；不要 Markdown/解释/代码块。

基于当前会话已经完成的代码探索与数据流分析，生成普通结构化知识摘要。
不要再探索代码，直接给出符合 schema 的知识摘要输出。

## 任务目标：
- 最多输出 2 条；可输出 0 条。
- 总结当前会话中观察到的局部程序分析知识，不总结本次完整 source-to-sink 答案。
- 不编造当前会话没有支持的代码事实。
- 若没有值得记录的局部知识，输出空数组。

## 知识内容要求：
- 候选类型只能是 `note`。
- `name` 用简短机制名或代码边界名。
- `entry_condition` 用 1 到 2 句话写适用条件。
- `content` 写简洁程序事实摘要；不强制包含“可跳过 / 优先检查 / 回退条件”。
- 不要把 query 原文、source 描述或 sink 类型改写成知识正文。

### 匹配规则：

- 规则类型只允许 `exact_symbol`、`call`、`symbol_tail`、`package_prefix`，这四类规则需要进一步组织到 `require_all`、`require_any`、`exclude` 三个规则桶中；
  - `symbol_tail`、`package_prefix` 只能作为谨慎使用的弱锚点：仅当它们足够项目内特有、足够长、歧义低时才使用。
  - 正向召回规则至少要满足一类：`require_all` 非空 或 `require_any` 非空。
  - 字段、常量、配置 key、类名和属性名使用 `exact_symbol`，不要写成 `call`。

- 规则桶 `require_all` 用于表达“多个锚点必须同时命中才允许召回”的组合：
  - 适合“泛 API + 具体 key/domain”这类场景。
  - 必须包含至少一个非泛、可稳定定位的具体 key/domain 锚点。
  - 不能只靠单条 `symbol_tail`、`package_prefix`、`call(method-only)` 或通用框架前缀触发。

- 规则桶 `require_any` 用于表达“任一锚点命中即可召回”的主锚点：
  - 只能放高度特异、低歧义的锚点，不要放泛 API / 泛符号。
  - 禁止示例：`intent.getStringExtra`、`Intent.EXTRA_TEXT`、`sharedPrefs.get*`、`bundle.putString`、`add`、`onSuccess`。

- 规则桶 `exclude` 用于表达“排除”，任一 `exclude` 规则命中时，该知识不应被召回。适合排除测试路径、无关模块、反例分支或容易误命中的上下文。

- 找不到稳定匹配规则时，不输出该候选。

#### 匹配规则示例：
- 场景：Intent extra 的文本只有在随后进入 Amaze 文件创建边界时才适用。预期规则：
```json
{{
  "require_all": [
    {{"kind": "call", "value": "", "receiver": "intent", "method": "getStringExtra"}},
    {{"kind": "exact_symbol", "value": "Intent.EXTRA_TEXT", "receiver": null, "method": null}},
    {{"kind": "call", "value": "", "receiver": "MakeFileOperation", "method": "mktextfile"}}
  ],
  "require_any": [],
  "exclude": []
}}
```
- 场景：SharedPreferences 或 Bundle 的泛读写 API 只有搭配具体配置键或具体下游边界时才适用，并排除测试夹具。预期规则：
```json
{{
  "require_all": [
    {{"kind": "call", "value": "", "receiver": "sharedPrefs", "method": "getInt"}},
    {{"kind": "exact_symbol", "value": "PreferencesConstants.KEY_TRASH_BIN_RETENTION_DAYS", "receiver": null, "method": null}},
    {{"kind": "exact_symbol", "value": "TrashBinConfig", "receiver": null, "method": null}}
  ],
  "require_any": [
    {{"kind": "exact_symbol", "value": "AppConfig.getTrashBinConfig", "receiver": null, "method": null}}
  ],
  "exclude": [
    {{"kind": "package_prefix", "value": "com.amaze.filemanager.test", "receiver": null, "method": null}}
  ]
}}
```

### `evidence_refs`：
- 使用当前会话已读证据；有文件、符号、行号则填写。
- 没有稳定行号时，`line` 可为 null；不要编造引用。

## 已有知识：
- `validated_catalog_block` 表示已通过验证的知识，帮助你判断哪些知识已经被覆盖了。
  - 它是简化版，不包含完整知识正文；不要因其内容简短就误判已有知识覆盖范围。
- `repairable_catalog_block` 只作修复参考；若高度相似，优先复用旧 `id` 输出修正版。
- 不要把这些信息写入 `evidence_refs`。

`validated_catalog_block`
{validated_catalog_block}

`repairable_catalog_block`
{repairable_catalog_block}

## 输出要求

仅输出一个 JSON 对象，结构如下：

Rule = {{
  "kind": "exact_symbol" | "symbol_tail" | "package_prefix" | "call",
  "value": string,
  "receiver": string | null,
  "method": string | null
}}

EvidenceRef = {{
  "file": string,
  "line": number | null,
  "symbol": string | null,
  "reason": string | null
}}

{{
  "schema_version": "flowark-knowledge-synth-v5",
  "reason": string,
  "candidates": [
    {{
      "id": string,
      "type": "note",
      "name": string,
      "match_rules": {{
        "require_all": [Rule],
        "require_any": [Rule],
        "exclude": [Rule]
      }},
      "entry_condition": string,
      "content": string,
      "evidence_refs": [EvidenceRef]
    }}
  ]
}}

## 补充要求：
- `reason` 必填，用一句话解释输出这些候选或输出空数组的原因。
- 当前任务上下文只用于判断相关性，不要直接抄进知识正文。

当前任务上下文：
- source_description: {source_desc_json}
- sink_types: {sink_types_json}
