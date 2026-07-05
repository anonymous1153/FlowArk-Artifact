# Source-first v3.2 Top 50 App Source 数量分布

- 生成时间：2026-06-18 18:00:03
- 数据源：`xxx`
- 汇总文件：`xxx`
- 统计口径：按 `inventory.jsonl` 中的 source occurrence 计数；同一 app 内按 `source_kind` 统计分布。
- 全量 inventory：5927 个 source occurrence，152 个 app 至少包含 1 个 source。

## TopN 累计覆盖

| Top N apps | 累计 source 数 | 占全量 source | 本组新增 source 数 | 本组新增占全量 source |
|---:|---:|---:|---:|---:|
| Top 5 | 1487 | 25.1% | 1487 | 25.1% |
| Top 10 | 2223 | 37.5% | 736 | 12.4% |
| Top 15 | 2773 | 46.8% | 550 | 9.3% |
| Top 20 | 3176 | 53.6% | 403 | 6.8% |
| Top 25 | 3514 | 59.3% | 338 | 5.7% |
| Top 30 | 3808 | 64.2% | 294 | 5.0% |
| Top 35 | 4072 | 68.7% | 264 | 4.5% |
| Top 40 | 4312 | 72.8% | 240 | 4.0% |
| Top 45 | 4512 | 76.1% | 200 | 3.4% |
| Top 50 | 4685 | 79.0% | 173 | 2.9% |

## Top 50 App 明细

| Rank | 应用名称 | 源码目录 / App ID | Total | Kind coverage | Storage | Storage % | UI | UI % | ICC | ICC % | Platform | Platform % | Remote | Remote % | Top rule | Top rule count | Top rule share |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|
| 1 | Fedilab | `fr.gouv.etalab.mastodon_549` | 529 | 3/5 | 241 | 45.6% | 64 | 12.1% | 0 | 0.0% | 0 | 0.0% | 224 | 42.3% | `local.preferences.getter.v1` | 226 | 42.7% |
| 2 | 时钟 | `com.best.deskclock_2031` | 301 | 4/5 | 246 | 81.7% | 35 | 11.6% | 17 | 5.6% | 3 | 1.0% | 0 | 0.0% | `local.dao.return.v1` | 204 | 67.8% |
| 3 | DuckDuckGo Privacy Browser | `com.duckduckgo.mobile.android_52702000` | 236 | 5/5 | 131 | 55.5% | 25 | 10.6% | 26 | 11.0% | 28 | 11.9% | 26 | 11.0% | `local.dao.return.v1` | 118 | 50.0% |
| 4 | Gadgetbridge | `nodomain.freeyourgadget.gadgetbridge_246` | 231 | 4/5 | 159 | 68.8% | 27 | 11.7% | 37 | 16.0% | 8 | 3.5% | 0 | 0.0% | `local.preferences.getter.v1` | 135 | 58.4% |
| 5 | Privacy Browser | `de.monocles.browser_14` | 190 | 4/5 | 102 | 53.7% | 82 | 43.2% | 3 | 1.6% | 3 | 1.6% | 0 | 0.0% | `local.preferences.getter.v1` | 52 | 27.4% |
| 6 | InviZible Pro: 增强您的安全，保护您的隐私 | `pan.alexander.tordnscrypt.stable_26603` | 183 | 4/5 | 115 | 62.8% | 16 | 8.7% | 6 | 3.3% | 46 | 25.1% | 0 | 0.0% | `local.preferences.getter.v1` | 110 | 60.1% |
| 7 | Suntimes | `com.forrestguice.suntimeswidget_128` | 152 | 4/5 | 19 | 12.5% | 54 | 35.5% | 67 | 44.1% | 12 | 7.9% | 0 | 0.0% | `app_entry.intent_extra.v1` | 63 | 41.4% |
| 8 | PodAura | `com.skyd.anivu_29` | 139 | 4/5 | 89 | 64.0% | 39 | 28.1% | 0 | 0.0% | 7 | 5.0% | 4 | 2.9% | `local.dao.return.v1` | 87 | 62.6% |
| 9 | SCEE | `de.westnordost.streetcomplete.expert_6302` | 137 | 5/5 | 49 | 35.8% | 54 | 39.4% | 2 | 1.5% | 11 | 8.0% | 21 | 15.3% | `local.dao.return.v1` | 45 | 32.8% |
| 10 | monocles chat | `de.monocles.chat_20004` | 125 | 5/5 | 44 | 35.2% | 46 | 36.8% | 18 | 14.4% | 11 | 8.8% | 6 | 4.8% | `ui.code.text_getter.v1` | 44 | 35.2% |
| 11 | Calisthenics Memory | `io.github.gonbei774.calisthenicsmemory_28` | 120 | 2/5 | 61 | 50.8% | 59 | 49.2% | 0 | 0.0% | 0 | 0.0% | 0 | 0.0% | `local.dao.return.v1` | 61 | 50.8% |
| 12 | 开支助手 | `org.totschnig.myexpenses_835` | 117 | 5/5 | 33 | 28.2% | 19 | 16.2% | 48 | 41.0% | 15 | 12.8% | 2 | 1.7% | `app_entry.intent_extra.v1` | 45 | 38.5% |
| 13 | Kreate | `me.knighthat.kreate_133` | 111 | 4/5 | 18 | 16.2% | 16 | 14.4% | 0 | 0.0% | 19 | 17.1% | 58 | 52.3% | `remote.response.body.v1` | 55 | 49.5% |
| 14 | Amaze 文件管理器 | `com.amaze.filemanager_124` | 109 | 4/5 | 46 | 42.2% | 42 | 38.5% | 12 | 11.0% | 9 | 8.3% | 0 | 0.0% | `ui.code.text_getter.v1` | 33 | 30.3% |
| 15 | P.CASH Crypto Wallet | `cash.p.terminal_229` | 93 | 4/5 | 59 | 63.4% | 14 | 15.1% | 0 | 0.0% | 10 | 10.8% | 10 | 10.8% | `local.dao.return.v1` | 55 | 59.1% |
| 16 | AndBible: 研经工具 | `net.bible.android.activity_910` | 91 | 3/5 | 48 | 52.7% | 39 | 42.9% | 0 | 0.0% | 4 | 4.4% | 0 | 0.0% | `ui.code.text_getter.v1` | 20 | 22.0% |
| 17 | LightNovelReader | `indi.dmzz_yyhyy.lightnovelreader_10200030` | 82 | 4/5 | 71 | 86.6% | 4 | 4.9% | 0 | 0.0% | 1 | 1.2% | 6 | 7.3% | `local.dao.return.v1` | 68 | 82.9% |
| 18 | Conversations | `eu.siacs.conversations_4217104` | 79 | 5/5 | 26 | 32.9% | 22 | 27.8% | 15 | 19.0% | 11 | 13.9% | 5 | 6.3% | `ui.code.text_getter.v1` | 22 | 27.8% |
| 19 | Quicksy | `im.quicksy.client_4217104` | 79 | 5/5 | 26 | 32.9% | 22 | 27.8% | 15 | 19.0% | 11 | 13.9% | 5 | 6.3% | `ui.code.text_getter.v1` | 22 | 27.8% |
| 20 | AntennaPod | `de.danoeh.antennapod_3110095` | 72 | 5/5 | 14 | 19.4% | 31 | 43.1% | 7 | 9.7% | 3 | 4.2% | 17 | 23.6% | `ui.code.checked_value.v1` | 17 | 23.6% |
| 21 | F-Droid | `org.fdroid.fdroid_1023052` | 72 | 5/5 | 44 | 61.1% | 5 | 6.9% | 10 | 13.9% | 11 | 15.3% | 2 | 2.8% | `local.dao.return.v1` | 24 | 33.3% |
| 22 | app.pachli_46 | `app.pachli_46` | 70 | 5/5 | 47 | 67.1% | 17 | 24.3% | 2 | 2.9% | 3 | 4.3% | 1 | 1.4% | `local.dao.return.v1` | 41 | 58.6% |
| 23 | Etar - 开源日历 | `ws.xsoh.etar_53` | 68 | 4/5 | 30 | 44.1% | 7 | 10.3% | 27 | 39.7% | 4 | 5.9% | 0 | 0.0% | `local.cursor.getter.v1` | 28 | 41.2% |
| 24 | Syncthing-Fork | `com.github.catfriend1.syncthingfork_2001500` | 65 | 4/5 | 48 | 73.8% | 14 | 21.5% | 1 | 1.5% | 2 | 3.1% | 0 | 0.0% | `local.preferences.getter.v1` | 44 | 67.7% |
| 25 | Les Pas - Nextcloud相册应用 | `site.leos.apps.lespas_114` | 63 | 5/5 | 38 | 60.3% | 8 | 12.7% | 4 | 6.3% | 9 | 14.3% | 4 | 6.3% | `local.dao.return.v1` | 36 | 57.1% |
| 26 | KeePassDX 密码库 | `com.kunzisoft.keepass.libre_153` | 61 | 4/5 | 9 | 14.8% | 46 | 75.4% | 4 | 6.6% | 2 | 3.3% | 0 | 0.0% | `ui.code.checked_value.v1` | 33 | 54.1% |
| 27 | Neo Store | `com.machiav3lli.fdroid_1207` | 60 | 4/5 | 52 | 86.7% | 2 | 3.3% | 0 | 0.0% | 5 | 8.3% | 1 | 1.7% | `local.dao.return.v1` | 49 | 81.7% |
| 28 | Squeezer | `uk.org.ngo.squeezer_154` | 60 | 4/5 | 43 | 71.7% | 11 | 18.3% | 1 | 1.7% | 5 | 8.3% | 0 | 0.0% | `local.preferences.getter.v1` | 43 | 71.7% |
| 29 | NewPipe | `org.schabi.newpipe_1009` | 58 | 5/5 | 31 | 53.4% | 14 | 24.1% | 9 | 15.5% | 3 | 5.2% | 1 | 1.7% | `local.preferences.getter.v1` | 28 | 48.3% |
| 30 | Mastodon | `org.joinmastodon.android_165` | 55 | 5/5 | 4 | 7.3% | 16 | 29.1% | 22 | 40.0% | 5 | 9.1% | 8 | 14.5% | `app_entry.intent_extra.v1` | 18 | 32.7% |
| 31 | K-9 Mail | `com.fsck.k9_39034` | 54 | 5/5 | 15 | 27.8% | 17 | 31.5% | 10 | 18.5% | 8 | 14.8% | 4 | 7.4% | `ui.compose.on_value_change.v1` | 11 | 20.4% |
| 32 | Thunderbird：解放收件箱 | `net.thunderbird.android_21` | 54 | 5/5 | 15 | 27.8% | 17 | 31.5% | 10 | 18.5% | 8 | 14.8% | 4 | 7.4% | `ui.compose.on_value_change.v1` | 11 | 20.4% |
| 33 | idTech4A++ | `com.karin.idTech4Amm_11071` | 53 | 4/5 | 35 | 66.0% | 14 | 26.4% | 2 | 3.8% | 2 | 3.8% | 0 | 0.0% | `local.preferences.getter.v1` | 32 | 60.4% |
| 34 | Thunderbird 测试版 | `net.thunderbird.android.beta_47` | 53 | 5/5 | 15 | 28.3% | 16 | 30.2% | 10 | 18.9% | 8 | 15.1% | 4 | 7.5% | `ui.compose.on_value_change.v1` | 10 | 18.9% |
| 35 | Zorin Connect | `com.zorinos.zorin_connect_13304` | 50 | 4/5 | 19 | 38.0% | 4 | 8.0% | 19 | 38.0% | 8 | 16.0% | 0 | 0.0% | `app_entry.intent_extra.v1` | 17 | 34.0% |
| 36 | KDE Connect | `org.kde.kdeconnect_tp_13505` | 50 | 4/5 | 19 | 38.0% | 4 | 8.0% | 19 | 38.0% | 8 | 16.0% | 0 | 0.0% | `app_entry.intent_extra.v1` | 17 | 34.0% |
| 37 | com.keylesspalace.tusky_140 | `com.keylesspalace.tusky_140` | 49 | 4/5 | 11 | 22.4% | 9 | 18.4% | 13 | 26.5% | 0 | 0.0% | 16 | 32.7% | `remote.response.body.v1` | 16 | 32.7% |
| 38 | SD Maid 2/SE - 系统清理工具 | `eu.darken.sdmse_10605000` | 49 | 4/5 | 25 | 51.0% | 12 | 24.5% | 1 | 2.0% | 11 | 22.4% | 0 | 0.0% | `local.dao.return.v1` | 22 | 44.9% |
| 39 | 质感文件 | `me.zhanghai.android.files_39` | 47 | 3/5 | 23 | 48.9% | 23 | 48.9% | 1 | 2.1% | 0 | 0.0% | 0 | 0.0% | `local.preferences.getter.v1` | 23 | 48.9% |
| 40 | Nextcloud Notes | `it.niedermann.owncloud.notes_330000090` | 45 | 5/5 | 5 | 11.1% | 16 | 35.6% | 6 | 13.3% | 2 | 4.4% | 16 | 35.6% | `remote.response.body.v1` | 16 | 35.6% |
| 41 | HeliBoard | `helium314.keyboard_3801` | 43 | 4/5 | 25 | 58.1% | 11 | 25.6% | 1 | 2.3% | 6 | 14.0% | 0 | 0.0% | `local.dao.return.v1` | 12 | 27.9% |
| 42 | Squircle CE - 代码编辑器 | `com.blacksquircle.ui_10028` | 41 | 3/5 | 36 | 87.8% | 1 | 2.4% | 0 | 0.0% | 4 | 9.8% | 0 | 0.0% | `local.preferences.getter.v1` | 19 | 46.3% |
| 43 | Password Store | `app.passwordstore.agrahn_11602` | 40 | 4/5 | 28 | 70.0% | 8 | 20.0% | 2 | 5.0% | 2 | 5.0% | 0 | 0.0% | `local.preferences.getter.v1` | 23 | 57.5% |
| 44 | RedReader | `org.quantumbadger.redreader_116` | 40 | 4/5 | 24 | 60.0% | 12 | 30.0% | 2 | 5.0% | 2 | 5.0% | 0 | 0.0% | `local.preferences.getter.v1` | 16 | 40.0% |
| 45 | Feeder (Play version) | `com.nononsenseapps.feeder.play_3922` | 36 | 5/5 | 23 | 63.9% | 4 | 11.1% | 2 | 5.6% | 2 | 5.6% | 5 | 13.9% | `local.dao.return.v1` | 22 | 61.1% |
| 46 | Feeder | `com.nononsenseapps.feeder_3922` | 36 | 5/5 | 23 | 63.9% | 4 | 11.1% | 2 | 5.6% | 2 | 5.6% | 5 | 13.9% | `local.dao.return.v1` | 22 | 61.1% |
| 47 | Phonograph Plus | `player.phonograph.plus_1120` | 36 | 4/5 | 14 | 38.9% | 6 | 16.7% | 15 | 41.7% | 1 | 2.8% | 0 | 0.0% | `local.cursor.getter.v1` | 8 | 22.2% |
| 48 | Calculator You: Math & Units | `com.marktka.calculatorYou_33` | 34 | 2/5 | 0 | 0.0% | 33 | 97.1% | 0 | 0.0% | 1 | 2.9% | 0 | 0.0% | `ui.code.text_getter.v1` | 32 | 94.1% |
| 49 | baresip | `com.tutpro.baresip_483` | 34 | 4/5 | 7 | 20.6% | 16 | 47.1% | 4 | 11.8% | 7 | 20.6% | 0 | 0.0% | `ui.compose.on_value_change.v1` | 16 | 47.1% |
| 50 | NClientV3 | `com.yosefario.nclientv3_421` | 33 | 5/5 | 2 | 6.1% | 1 | 3.0% | 14 | 42.4% | 1 | 3.0% | 15 | 45.5% | `remote.response.body.v1` | 15 | 45.5% |

## Top 50 按 Source Kind 汇总

| Source kind | Count | Share in Top 50 | Share in full inventory |
|---|---:|---:|---:|
| `persistent_storage` | 2307 | 49.2% | 38.9% |
| `ui_input` | 1078 | 23.0% | 18.2% |
| `icc_payload` | 486 | 10.4% | 8.2% |
| `platform_api` | 344 | 7.3% | 5.8% |
| `remote_payload` | 470 | 10.0% | 7.9% |
| **Total** | **4685** | **100.0%** | **79.0%** |

## 说明

- `TopN 累计覆盖` 中，`累计 source 数` 是 Top N apps 的累计 source occurrence 数；`本组新增 source 数` 是每新增 5 个 app 带来的 source 数。
- `Kind coverage` 表示该 app 覆盖了五类 source kind 中的几类。
- `Top rule` 是该 app 内出现次数最多的 source discovery rule，用来快速识别是否被单一规则主导。
- 表格按 `Total` 降序排序；同分时按源码目录名排序。
