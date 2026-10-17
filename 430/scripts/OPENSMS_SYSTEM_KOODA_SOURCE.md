# Kooda主报表改用系统数据

本说明记录2026-09-07的字段来源调整。完整业务规则仍以同目录`OPENSMS_10DAY_PATROL_POLICY.md`为准，原v52后台可靠性基线和执行入口不变。

## 字段来源

主表中Kooda/Koodo/Telus1仅使用本轮`POST /phone/config/list`完整库存；不再从Google或任务F数据库填充下列字段。

| 主表字段 | 系统字段 | 处理方式 |
| --- | --- | --- |
| ICCID（主表末列） | cardStore | 原样按文本显示，保留末尾F；独立库比对时仅去除末尾一个F/f |
| 激活日期 | chatrActivateDate | 直接使用有效系统日期；缺失留空 |
| 截止日 | chatrPlanExpire | 直接使用有效系统日期，不按激活日期重新计算 |
| 开卡日期 | 不使用 | 留空，不以Google上卡日期或系统创建时间替代 |

系统原始创建时间随完整源对象保留，不使用创建时间替代开卡日期、激活日期或截止日。原有基于新鲜源表创建时间的换号判断规则保持不变，本次不重设换号标准；双日期空白时仍优先按未换号新卡处理。ICCID标准键仍须为18至22位数字，不使用浮点数或科学记数法匹配。

系统激活日期缺失时整行浅红；激活日期和截止日同时为空时暂按“未换号”的新卡处理，不补造日期。系统只有截止日时保留该截止日，不因缺激活日期清空它。下次新鲜导出系统已提供有效激活日期时取消缺失日期红色。到期分类及B/E等后续计算使用上述主表系统日期和本轮网页服务器日期。

非Kooda行的Google映射及“所有谷歌有-系统没有”“谷歌文档中有激活日期-系统无法使用”等工作表保持原有来源和规则。

## 并发与一致性

- 新鲜导出、Google表、已上架号码、余量、Kooda系统库存共5项只读采集并发执行；全部必要采集校验通过才进入分析。
- 主表、Google表、系统库存JSON三输入并行读取；原分析与文档计算并行流程保留。
- F只可复用同一运行编号、SHA-256和完整分页均已验证的本轮库存；避免同轮重复查询号码系统。独立F流程没有可用同轮库存时仍做新鲜采集。
- 运营商写入、独立回读、工作簿刷新及B→C→D→E阶段屏障保持串行；不得提前计算依赖上一阶段最新文档的候选。
- B/C/D/E默认关闭，仍需当前真实用户逐项明确授权。本次代码调整不授权立即执行业务。

## 任务F仍独立保留

F默认目录同步、8个隔离工作器和库模板导出继续保留。按后续用户修订，F只针对本轮合并表Kooda激活日期为空的ICCID：本地有有效日期直接复用；本地无记录、空日期或未找到的允许补查，包括权威附件中的旧空日期。结果只写本地库，用户要求时导出并由用户人工提交云端；不自动上传或写回本轮/下轮主报表。库中截止日仍为激活日+29个自然日。详见`OPENSMS_TASK_F_REPORT_BACKFILL.md`。

完整报告分别列主报表系统来源路径、SHA-256、库存/匹配/缺激活日期数量，以及F独立库和模板路径。主报表系统日期与F独立库日期不得混称为同一数据来源。

## 下次运行

普通新鲜导出继续只调用：

```sh
/Users/zhangsan/Downloads/430/scripts/run_opensms_patrol.sh run --json
```

此命令会按当轮授权执行既有完整流程，不是仅下载源文件。只导出F本地库模板时仍使用`run_opensms_task_f.sh export --json`，不访问浏览器、不触发其它任务。

独立本地文档入口`opensms_generate_from_downloads.py`现在也必须显式提供第三份本轮系统JSON：`--system-kooda <JSON>`、`--system-kooda-sha256 <SHA256>`、`--run-id <本轮编号>`，或对应环境变量`OPENSMS_SYSTEM_KOODA_PATH`、`OPENSMS_SYSTEM_KOODA_SHA256`、`OPENSMS_RUN_ID`。`--backend-date`另为必填，必须提供本轮已确认的真实后台服务器日期，不从本机日期猜测。完整调用形式为：

```text
<Python运行时> /Users/zhangsan/Downloads/430/scripts/opensms_generate_from_downloads.py <本轮export4day.xlsx> <本轮Google.xlsx> --system-kooda <本轮系统.json> --system-kooda-sha256 <SHA256> --run-id <本轮编号> --backend-date <YYYY-MM-DD> --output-dir <本轮输出目录> --artifact-root <本轮回执目录>
```

入口先校验文件哈希、运行编号、状态、完整行数及后台静默证明，再把确认值传给本地生成器`services/opensms_document_api/pipeline/build_card_slot_report.sh`；不会自动寻找历史快照或联网补采，不执行业务。生成结果必须成功、独立`checks`非空且每项严格通过、`failed_checks`为空才允许发布本入口报告。报告中的Google匹配数使用独立的`google_matched_rows`，不再把系统匹配行计入Google匹配。

## 本次验证

- 2026-09-07：全部231项Python回归测试通过，6项Node库存分页测试通过；Shell及Node语法检查通过。
- 合成数据完整覆盖生成器→独立核验→审计，以及独立本地CLI→Excel与Markdown交付；Kooda没有Google关联时仍按系统数据成功匹配，Chatr继续使用Google。
- 日期或新卡状态篡改、旧runId、错误哈希、不完整分页、后台ID重复、非静默证明都会被拒绝；双日期空白标红/未换号、原始ICCID文本和截止日不重算均有回归验证。
- 定时入口已同步本轮系统来源规则，原定时计划保持不变。没有执行在线巡检、修改号码或触发B/C/D/E。
- 测试耗时不代表线上导出耗时。默认5路只读采集、4路库存分页、3路输入读取及同轮库存复用已落实；真实端到端提速幅度以之后新鲜导出的分阶段日志为准。
