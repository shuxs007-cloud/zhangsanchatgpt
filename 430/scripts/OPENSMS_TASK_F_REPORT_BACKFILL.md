# 任务F：日期缺口后台补查与本轮增量文件

本说明解释程序和审计接口；完整业务规则以同目录 `OPENSMS_10DAY_PATROL_POLICY.md` 为准。本次开发与离线测试不执行真实查询，不授权任务B/C/D/E。

## 数据流和范围

任务A的“ko未激活表”按2026-09-25要求更名为“ko系统有-未激活”，仍为**任务F未激活与本轮系统库存ICCID交集**：冻结F库`activation_missing`且规范ICCID存在于本轮完整系统Kooda/Koodo/Telus1库存，每ICCID一行。“卡槽”改取本轮系统`slotIdentif`；同ICCID多个非空位置去重换行列全，保留原文本，空白不回填任何历史位置。其它带“记录时”标签的字段保持F历史资料。不限10日主表或上架状态，系统外历史F记录排除；`found/not_found/error`不收录。匹配只去系统ICCID首尾空白及末尾一个F/f，保留数字文本精度，不用手机号/卡槽代替。缺本轮有效库存停止，不退回全库。无交集保留空表头；本轮后台F新结果下一次导出体现。固定模块`ko_unactivated.py`接入生成、初始独立回读、最终写后审计和完整报告，记录双来源SHA256、交集数、系统外排除数及当前卡槽已填/空白/多卡槽数量，不增加正常导出的联网请求或业务写入。本表不是F联网缺口候选或可上传的新增日期文件，F历史状态也不是实时激活确认。

1. 每轮先冻结本地F日期库（只读SQLite事务、runId、SHA-256），再以新鲜系统源表逐字段优先生成主报表。Kooda缺失日期读冻结库；Chatr缺失日期才读本轮Google；已有系统值不覆盖。最终只保留激活日期/截止日两列。
2. 任务F筛选该表`sheet1`内Kooda/Koodo/Telus1日期仍缺失或只有`creation_date_fallback`推算日期的行。兜底来源必须经独立回读证明、绑定本轮库存与回执SHA256，再逐行核验手机号及两日期；不得仅凭日期相等猜测来源。真实日期齐全不查询；非空无效日期拒绝，不冒充空白。只在库存/附件而不在本轮文档的ICCID不查询。
3. 主表手机号末10位与归一化卡槽组成复合身份，必须精确命中本轮系统库存；主表ICCID与源 `cardStore` 去除末尾恰好一个F/f后必须一致。ICCID唯一键为18至22位数字文本；显示列继续保留原F。缺失/非法ICCID逐行排除，不能用手机号代查；身份不符和重复复合键阻断执行。
4. 本地已有有效激活日期和相应有效截止日：复用并列为待人工上传，不联网。无记录、空日期、历史 `activation_missing` 或 `not_found`：允许联网补查；旧权威目录存在空日期不再禁止重查。
   - 再次查询仍未激活或未找到，也更新本地查询时间、完整字段与源数据；已有有效日期不会被空结果、错误或旧目录覆盖。
5. 查询仅使用8个隔离工作器，实际不超过待查数，每个工作器处理一条后至少间隔1秒。独立会话无法建立时回落为单工作器，仍保留三重ICCID身份核验；待查为0时不登录、不查询。
6. 结果仅导入本地SQLite，F独立后台目录保存状态/日志/报告/本轮库；不阻塞主报表最终交付、不修改已发布主清单、不自动上传云端。本次F新日期仅下一次新鲜导出冻结后用于补系统缺失字段，不能覆盖系统值。
7. 数据库提交回执逐行标记`activationDateUpdated`并统计`activationDateUpdatedCount`。`opensms_task_f_delta.py`只导出true且有效的记录，独立核对已提交库后生成三列增量Excel；同日期复用/结构升级/无日期/失败不进入增量。零新增返回`generated=false/output=null`，不制造空表、不提示旧F文件。

## 程序入口

- 普通定时和手动导出入口`run_opensms_export.sh --json`，发现缺失或仅有创建日期兜底的Kooda自动启动F。`run_opensms_task_a.sh --json`只用于用户明确要求仅A、不运行F；B/C/D/E均默认关闭。
- 手动完整巡检仍只运行`run_opensms_patrol.sh run --json`。初始文档独立核验后，F由`opensms_task_f_background.py`派发独立作业；主runner继续普通/BCDE刷新和最终交付，不等待F。B/C/D/E保持当轮逐项授权。
- 只需要数据库三列模板时运行 `scripts/run_opensms_task_f.sh export --json`。此动作只读本地数据库，不联网；列名、顺序仍为ICCID、截止日、激活日期，日期格式 `yyyy-MM-dd`。
- 历史独立任务F和有限诊断模式保留原模式审计，不能当作本轮报表补查回执复用。

## 可核验回执

待查回执记录 `reportBackfillMode=true`、`documentPath`、`documentSha256`、`documentRunId`，绑定本轮合并表及独立回读回执；文件变化、跨轮或缺核验即拒绝。

| 分区 | 记录与计数 |
| --- | --- |
| 主表范围 | `documentKoodaRows`、`documentActivationPresentRows`、`documentMissingActivationRows`、`documentMissingCutoffRows`、`documentMissingDateRows` |
| 推算日期 | `documentCreationFallbackRows`，与实际空白分开计数；`documentLookupNeededRows`为两类需补查行合计 |
| 合格空日期行 | `reportEligibleRecords`逐行保留文档行号、手机号、卡槽、原ICCID、规范ICCID、后台ID |
| 排除 | `documentExcludedRows`及`documentExcludedCount`，逐行说明缺失/非法ICCID |
| 唯一ICCID | `documentMissingDateIccidCount`，激活缺口另记`documentMissingActivationIccidCount`，并记录重复行折叠数 |
| 本地复用 | `alreadyQueried`/`reuseRecords`及`readyForUploadCount` |
| 联网待查 | `rows`及`pendingCount`，原系统源对象和所有文档出现位置必须完整保留 |

数量关系：Kooda行数=激活日期有值行数+激活空白行数；实际日期缺失行数+创建日期兜底行数=需补查行数=合格行数+排除行数；唯一合格ICCID数=本地有效日期复用数+联网待查数。逐行保留`missingFields`，兜底行另记`dateSource`及`lookupReason`；待查回执使用`dateEligibilityMode=missing_or_verified_creation_fallback`并绑定`documentProofPath`和`documentProofSha256`。独立审计重读哈希绑定文档，不把程序自报当作独立证据；旧无截止日列历史格式只按激活缺口兼容，新报表含两列。

## 本地交付和安全边界

- 主报告保存不可变后台作业清单和独立状态/报告路径，queued/running不当F完成，也不使已通过主任务失败。独立F报告列本轮/固定最新库、数据库快照、缺口/复用/查询/落库/排除统计；只在实际新增时列增量Excel链接。
- 固定最新库：`/Users/zhangsan/Downloads/430/opensms_artifacts/state/task_f_kooda_iccid_library_latest.json`。本轮带时间戳副本保存在本轮交付目录，不覆盖历史。
- 主表日期来源为系统优先、Kooda缺失字段读冻结本地库、Chatr缺失字段读本轮Google；`taskFResultsApplyNextExport=true`、`taskFResultsWriteBackToDocument=false`、`taskFCloudUploadAttempted=false`。
- 登录凭据仅存macOS钥匙串，不写入命令、代码、JSON或报告；ICCID不发送给Firecrawl、AI连接器或第三方抓取服务。

## 5分钟进度通知

### 外接盘回执兼容修复（2026-09-07）

F查询程序用硬链接独占发布完整回执；外接交付卷可能返回`ENOTSUP`。runner现在将每批JSON先写入本机`opensms_artifacts/logs/patrol_runner/<runId>/task_f_receipts/`，成功后以独占创建方式归档到交付目录，逐字节SHA-256和JSON独立回读通过后才执行数据库导入。

本机原始回执不会因归档失败被删除。已有本机或归档回执拒绝重查与覆盖，复制/回读失败不会算作落库。`taskFReceiptStaging`在本轮runner状态中记录本机、归档路径与哈希。进度路径、8会话、身份核验、数据库与最终报表屏障保持不变。

真实本机Node空队列发布与外接盘归档测试通过，未登录、未联网、未访问业务数据库；相关runner、补查、进度、审计与模板79项离线回归通过。离线兼容测试不代表真实巡检完成，正式交付仍只认程序最终JSON。

### 通知机制

任务F运行时每5分钟汇报一次，完成或异常即报；无任务不做空轮询。记录刷新和聊天通知是两层：查询程序负责300秒正式进度日志及即时终态，并每10秒只在本地刷新轻量快照（不调用AI）；活动轮次/临时心跳负责把本地统计发到当前聊天，不把日志更新冒充消息已送达。

本地只读入口（不启动业务、不联网）：

```sh
/Users/zhangsan/Downloads/430/scripts/run_opensms_task_f.sh status --state <本轮runner状态绝对路径> --json
```

薄入口仅调用原 `opensms_task_f_progress_status.py`，没有第二套计数逻辑。固定绝对路径绑定本轮，拒绝其它参数，不读取业务数据库。

启动巡检的stderr事件`patrol_started`给出本轮state路径。进度脚本合并总进度与尚未纳入总计的当前批次，返回中文`markdown`和结构化字段；“已落库”取本轮数据库导入回执，不取全库数量、不把查询命中当作落库。错误、缺失、跨目录或陈旧证据必须明确显示未知。

主报表可先结束，F继续后台运行。调用者通过正式自动化工具维护独立“OpenSMS任务F进度”只读心跳，不再把A导出定时任务替换为进度监控；复用已有同用途心跳，绑定准确job/state及SHA，不重复创建。每5分钟只读状态和进程，F完成/失败后停止专用提醒，不因主runner完成提前停止。程序不得直接修改TOML或使用未公开应用接口。详细去重/终态边界以唯一策略为准；机器需保持运行。

官方[定时任务说明](https://learn.chatgpt.com/docs/automations?surface=app)确认聊天内定时支持分钟间隔；依赖本地文件时需保持电脑和应用运行。本次只用该机制安排通知，业务采集仍由本地程序执行。

## 通知变更验证

- 只读进度脚本20项离线测试通过，覆盖跨批去重、查询与落库分离、错误计数、准备阶段、终态冻结、进程消失、错run/目录/批次、重复导入回执和陈旧证据。
- runner18项、系统Kooda集成13项测试通过；JS语法检查通过。全套284项初次通过后，进度测试补充6项并再次通过。既有成功轮只读核验为4条已查询、4条已落库、0条剩余，未新启动业务。
- 正式自动化经产品工具更新并独立读取核对：原时段、目标任务与ACTIVE状态保持，5分钟进度规则已保存。没有为已结束的旧运行启动临时监控。

## 报表补查基线验证（通知变更前）

- Python全套270项测试、Node库存并发6项测试通过；覆盖报表范围、跨轮/哈希篡改、精确身份、空日期重查、数据库升级与不降级、独立审计。
- 同时修复系统新导出61列表头兼容问题，仍兼容旧58列。使用此前失败轮的原始输入离线重建4,316行，24项独立校验全部通过。该回归文件不是新鲜巡检交付，本次未联网补查或上传云端。
