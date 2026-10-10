【严格输出】只输出 JSON；不要调用工具；不要继续探索；不要 Markdown/解释/代码块。

基于当前会话已经完成的代码探索与数据流分析，提炼可复用知识候选。
不要再探索代码，直接给出符合 schema 的知识候选输出。

## 任务目标：
- 最多输出 2 条；可输出 0 条。
- 遵循后面提到的 "知识选择规则"。
- 只输出未来会显著减少阅读量的局部稳定知识，不要总结本次完整答案。
- 优先识别高复用的局部模式，历史高频链路可以帮你提示候选边界。

## 知识总结指导

### 知识选择规则：
- Evidence-backed：只写当前会话已读、已确认的代码事实，并用 `evidence_refs` 支撑。
- Bounded：候选必须有清楚的入口条件、局部机制、下游边界和回退条件。
  - 局部稳定：入口条件、局部序列、边界 API 或出口关系足够稳定。
  - 若当前分析结论中已暴露稳定的 `source-family -> bridge-field -> boundary` 序列，优先总结这段数据流模式为知识候选。
  - 泛 API 不能单独构成知识价值；必须和具体 key/domain/边界 API 形成组合。
- Actionable：候选必须说明后续 agent 可以少读什么、应优先检查什么。

### 什么知识值得总结：
- 高复用：会在多个分析任务（针对同一 app 的不同 source）中反复出现。
  - "历史高频链路" 可以帮你揭示历史中已发现的高复用局部模式，优先总结这些模式为知识候选。
- 高降本：能让后续分析 agent 少读一段局部实现、少展开一个分支树、直达关键 API 或下游边界的知识。

### 不要提炼此类知识：
- 当前分析任务的一次性事实或最终结论。
- 一眼可见、几乎没有复用价值的直接 API 行为。
- 只因单点 API 高频而形成的过宽 bridge-only 知识。
- 已被 `validated_catalog_block` 覆盖的主导模式。

## 知识内容要求：
- 候选类型只能是 `note`。
- `entry_condition` 用 1 到 2 句话写适用条件和主要回退条件。
- `content` 必须包含以下四个标签：
  - `知识摘要：...`
  - `可跳过：...`
  - `优先检查：...`
  - `回退条件：...`
- 正文只保留必要代码锚点；详细证据放入 `evidence_refs`。
- 不要把 query 原文、source 描述或本次 sink 类型改写成知识正文。

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

### `egress_map`：
- 仅当知识的价值来自局部分发或出口映射时填写；否则写 null。
- `selectors` / `negative_selectors` 必须是代码或工具输出中可直接出现的稳定文本。
  - 例如：参数敏感的入口，填写对应参数的值/常量名。基对象敏感的入口，填写对应类名或接口名。分发条件敏感的入口，填写对应条件值或类型。
  - 这些值必须直接决定后续分析的分支走向，且在未来分析中足够稳定。
- 只写已确认分支，不猜未读分支。
- `note_id` 必须等于候选 `id`。

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


## 历史复用线索

{historical_reuse_digest_guidance_block}

### 历史高频链路
{historical_reuse_digest_block}

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

EgressCase = {{
  "selectors": [string],
  "negative_selectors": [string],
  "next_hops": [string],
  "summary": string,
  "evidence_refs": [EvidenceRef]
}}

EgressMap = {{
  "schema_version": "flowark-egress-map-v2",
  "note_id": string,
  "boundary_summary": string,
  "key_apis": [string],
  "cases": [EgressCase]
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
      "evidence_refs": [EvidenceRef],
      "egress_map": EgressMap | null
    }}
  ]
}}

## 补充要求：
- `reason` 必填，用一句话解释输出这些候选或输出空数组的原因。
- 当前任务上下文只用于判断相关性，不要直接抄进知识正文。

当前任务上下文：
- source_description: {source_desc_json}
- sink_types: {sink_types_json}
