【严格输出】只输出 JSON；不要调用工具；不要继续探索；不要 Markdown/解释/代码块。

## 任务目标：
直接复用刚才的结论与证据，输出固定格式最终报告。

## 要求：
- 基于当前会话已经完成的分析结果，不要重新做代码探索。
- 字段缺失时使用 null 或 []，不要省略固定字段。
- 若未确认到目标 sink 的数据流，`dataflows` 输出 []，并在 `uncertainties` 写明缺口。
- `location` 使用项目根目录相对路径：`"path/to/File.kt@128"` 或 `"path/to/File.kt@128-129"`；未知写 null。
- `knowledge_used.notes` 填本轮实际影响分析的知识 id；未使用则 []。
- `knowledge_used.flow_facts` 是兼容字段，当前 note-only 运行必须填 []。

## 字段说明：

### `dataflows.path`：
- 按 source 到 sink 顺序写已确认的关键污点数据传递证据。
- 只保留关键 carrier、字段、参数、回调或调用边界。
- 不确定或未展开的传播不要强行补入 path，写入 `uncertainties`。

### `sink`：
- `sink_type` 使用当前任务要求的 sink 类型或当前会话明确采用的 sink 分类。
- `statement` 写直接 sink 调用语句或关键表达式；未知写 null。
- `method` 写包含 sink 调用的方法或最接近的边界方法；未知写 null。
- `location` 写 sink 调用位置；若只确认到外部边界，写该边界调用位置。

### `method_call_chain`：
- 写你确认的 source-to-sink 路径上对应的方法调用链。
- 按照以下格式，在列表中按顺序列出方法调用链
  - 例如：`Receiver.source_method1(...)`、`Class.method(...)`、`Receiver.sink_method2(...)`。
  - 不要写完整方法签名，只写到其上级对象或类。
  - 形参省略为 `...`
- 未确认到方法调用链时写 []，不要猜测。

### `uncertainties` 与 `skipped_branches`：
- `uncertainties` 写会影响结论可信度的缺口。
- `skipped_branches` 写本轮明确未展开的相关分支及原因。
- 已由代码证据确认的事实不要写成不确定性。

### `from` / `to`：
- 能明确写出相邻代码锚点时填写；无法可靠拆成相邻锚点时可写 null，并用 `description` 简述。
- 优先使用简短代码表达式，如 `Receiver.method(...)`、`Receiver.field`、`name`、`"key"`。
- 不要编造不存在的数据流；文件路径和行号只写在 `location`。

## 输出格式：
{{
  "schema_version": "flowark-final-report-v2",
  "query": string,
  "source": {{
    "description": string,
    "method": string|null,
    "location": string|null
  }},
  "dataflows": [
    {{
      "explain": string,
      "confidence": string|null,
      "sink": {{
        "sink_type": string,
        "statement": string|null,
        "method": string|null,
        "location": string|null
      }},
      "method_call_chain": [string],
      "path": [
        {{
          "description": string,
          "from": string|null,
          "to": string|null,
          "location": string|null
        }}
      ]
    }}
  ],
  "uncertainties": [string],
  "skipped_branches": [string],
  "knowledge_used": {{
    "notes": [string],
    "flow_facts": []
  }}
}}

## 当前任务上下文（供校验，不要照抄）：
- source_description: {source_desc_json}
- sink_types: {sink_types_json}
