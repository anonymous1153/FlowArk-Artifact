# Source-first v3.2 Top50 消融与 LLM 对比子集抽样方案

- 生成时间：2026-06-22
- 参考分布文档：`260618-source-first-v3.2-top50-source-count-distribution.md`
- 目标用途：在已经完成 Top50 端到端主实验的基础上，为模块消融实验与不同 LLM 型号对比实验提供一个成本可控、客观可复现的 app 子集。

## 抽样动机

Top50 app 共包含 4685 个 source occurrence。直接使用 Top10 app 会保留 2223 个 source，占 Top50 source 的 47.4%，但 app 数只有 Top50 的 20%。这会导致成本下降不充分，同时 app 覆盖面看起来偏窄。

因此，本子集不采用 Top-k 前缀，而采用 Top50 内的分层系统抽样：保留较多 app 数量，同时显著降低 source 总量。

## 抽样规则

1. 以 `260618-source-first-v3.2-top50-source-count-distribution.md` 中 Top50 表格的 `Rank` 为固定排序。
2. 将 Top50 按 rank 划分为 5 个等宽层：
   - Rank 1-10
   - Rank 11-20
   - Rank 21-30
   - Rank 31-40
   - Rank 41-50
3. 每层固定选取该层内第 3、6、9 个 app。
4. 得到全局 rank：
   - `3, 6, 9, 13, 16, 19, 23, 26, 29, 33, 36, 39, 43, 46, 49`
5. 抽样后不再根据 FlowArk/naive 表现、运行成本、模型表现或人工偏好调整 app 列表。
6. 对被选中的 app，保留该 app 内全部 source occurrence，不再进行 app 内 source 抽样。

该规则是一个 deterministic rank-stratified systematic sample，可由 Top50 分布表完全复现。

## 子集规模

| 指标 | 数值 |
|---|---:|
| App 数 | 15 |
| 占 Top50 app 比例 | 30.0% |
| Source occurrence 数 | 1284 |
| 占 Top50 source 比例 | 27.4% |
| 占全量 inventory source 比例 | 21.7% |

## Source kind 分布

| Source kind | 子集 count | 子集 share | Top50 share |
|---|---:|---:|---:|
| `persistent_storage` | 592 | 46.1% | 49.2% |
| `ui_input` | 308 | 24.0% | 23.0% |
| `icc_payload` | 119 | 9.3% | 10.4% |
| `platform_api` | 149 | 11.6% | 7.3% |
| `remote_payload` | 116 | 9.0% | 10.0% |
| **Total** | **1284** | **100.0%** | **100.0%** |

该子集覆盖全部 5 类 source kind，且整体分布与 Top50 分布保持在同一量级。

## 抽样 App 列表

`数据集标准名称` 使用参考分布文档中 Top50 明细表的 `应用名称` 列。

| Stratum | Rank | 数据集标准名称 | App ID / 源码目录 | Source count | Kind coverage | Top rule |
|---|---:|---|---|---:|---:|---|
| 1-10 | 3 | DuckDuckGo Privacy Browser | `com.duckduckgo.mobile.android_52702000` | 236 | 5/5 | `local.dao.return.v1` |
| 1-10 | 6 | InviZible Pro: 增强您的安全，保护您的隐私 | `pan.alexander.tordnscrypt.stable_26603` | 183 | 4/5 | `local.preferences.getter.v1` |
| 1-10 | 9 | SCEE | `de.westnordost.streetcomplete.expert_6302` | 137 | 5/5 | `local.dao.return.v1` |
| 11-20 | 13 | Kreate | `me.knighthat.kreate_133` | 111 | 4/5 | `remote.response.body.v1` |
| 11-20 | 16 | AndBible: 研经工具 | `net.bible.android.activity_910` | 91 | 3/5 | `ui.code.text_getter.v1` |
| 11-20 | 19 | Quicksy | `im.quicksy.client_4217104` | 79 | 5/5 | `ui.code.text_getter.v1` |
| 21-30 | 23 | Etar - 开源日历 | `ws.xsoh.etar_53` | 68 | 4/5 | `local.cursor.getter.v1` |
| 21-30 | 26 | KeePassDX 密码库 | `com.kunzisoft.keepass.libre_153` | 61 | 4/5 | `ui.code.checked_value.v1` |
| 21-30 | 29 | NewPipe | `org.schabi.newpipe_1009` | 58 | 5/5 | `local.preferences.getter.v1` |
| 31-40 | 33 | idTech4A++ | `com.karin.idTech4Amm_11071` | 53 | 4/5 | `local.preferences.getter.v1` |
| 31-40 | 36 | KDE Connect | `org.kde.kdeconnect_tp_13505` | 50 | 4/5 | `app_entry.intent_extra.v1` |
| 31-40 | 39 | 质感文件 | `me.zhanghai.android.files_39` | 47 | 3/5 | `local.preferences.getter.v1` |
| 41-50 | 43 | Password Store | `app.passwordstore.agrahn_11602` | 40 | 4/5 | `local.preferences.getter.v1` |
| 41-50 | 46 | Feeder | `com.nononsenseapps.feeder_3922` | 36 | 5/5 | `local.dao.return.v1` |
| 41-50 | 49 | baresip | `com.tutpro.baresip_483` | 34 | 4/5 | `ui.compose.on_value_change.v1` |

## 使用建议

- Top50 仍作为端到端主实验集合。
- 本 15-app 子集用于成本敏感的模块消融实验和 LLM 型号对比实验。
- 所有被比较的条件必须使用同一 app 子集、同一 app 顺序、同一 source 顺序、同一 knowledge-base reset policy。
- 不建议在该子集内进一步抽 source，因为 FlowArk 的知识复用依赖同一 app 内跨 source 的历史顺序和知识积累。

## 论文表述建议

可描述为：

> For cost-sensitive ablation and model-comparison experiments, we use a deterministic stratified subset of the Top50 source-first benchmark. We divide the Top50 apps into five rank strata and select the 3rd, 6th, and 9th app from each stratum, yielding 15 apps and 1284 source occurrences. The subset preserves all five source categories and keeps the same per-app source order as the full Top50 benchmark.
