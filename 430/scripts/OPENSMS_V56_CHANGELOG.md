# V组保护、每日转换号统计与报告精简

程序版本：`2026.09.17-v56-v-group-boundary`。

完整业务规则仍以同目录 `OPENSMS_10DAY_PATROL_POLICY.md` 为唯一来源。本次只修改程序、规则和离线测试，没有启动巡检、浏览器或修改线上号码。

## 生效内容

- B1V/B11V/B12V普通流转仅在三个V组内进行。只允许原有、当轮明确授权且条件核验通过的任务B到期分区，以及任务C低回复率转换号区退出。全局休息区开关即使以后恢复，V组仍不转休息区。原有内部使用天数、240阈值、零回复保持、日期及保护规则不变。
- 新增V组每日转换号统计Excel，并在完整MD附表。写入成功后须通过独立回读才登记；按实际写入的北京时间归日，各组每日手机号去重、三组合计再去重，同时保留确认转换号次。候选、已经在目标区、失败、未确认提交均不计成功。
- 本地台账位于`opensms_artifacts/state/v_group_swap_daily/`，从下次实际执行起累积；不补算历史、不包含人工或其它脚本的变化。普通导出读取累计结果，不自动授权任务C。每份Excel都有本轮回执和独立数值回读。
- 完整MD保留汇总数量、分类原因、异常和交付链接，不再展开大量手机号/ICCID列表。原始工作簿和JSON审计明细保留；成功及失败报告应用同一精简模块。
- 新统计使用最多4个本地只读工作器，不增加网页请求、不重新扫描或改写主工作簿；运营商提交和独立回读仍串行。

## 实现入口

- `scripts/opensms_v_group_guard.mjs`：候选及提交前实时来源边界。
- `opensms_generate_opr_zero_candidates.py`：V组零回复永久保持。
- `scripts/opensms_v_swap_daily.py`：已核验事件、不可变幂等台账、日期去重、Excel及独立回读。
- `scripts/opensms_report_summary.py`：逐号表格转数量及分类原因。
- `scripts/opensms_patrol_runner.py`、`scripts/opensms_finalize_patrol_report.py`、`scripts/opensms_parallel_audit.py`、`scripts/opensms_chat_summary.py`：统一接入生成、失败报告、审计及交付。

## 验证结果与限制

- [固定规则回归回执](/Users/zhangsan/Downloads/430/outputs/opensms-fixed-rules-d3n0k2i7/verification.json)：15组、696项通过，4个隔离工作器，41.526秒。测试进程由操作系统禁网，写入仅限各自临时目录，不能访问浏览器或改生产台账。
- [报告/审计定向回归日志](/Users/zhangsan/Downloads/430/outputs/opensms-v56-focused-report-w8oy1z8k/focused_report/test.log)：46项通过。
- 新测试覆盖全局休息区重开时V组仍保持、精确组名、三个V组内部目标、非法退出、B/C例外授权组合、后台来源变化、北京时间跨日、跨轮/跨组去重、幂等恢复、未确认写入排除、身份不一致拒绝、Excel独立回读，以及汇总表不被误删。
- 仅在临时目录兼容校验上一轮两份任务C真实回执格式，未导入生产统计台账。旧报告离线精简前1922行、精简后785行，逐号表格行全部移除；未覆盖旧交付文件。
- 扩展文档套件并非全部通过：[扩展测试日志](/Users/zhangsan/Downloads/430/outputs/opensms-v56-report-audit-yfnvvc03/report_audit/test.log)。61项中两项失败：旧Google停用报告测试仍断言精确文案“整行浅红”，现有模板为“整行标红”；另一个完整文档构建受既有Bash here-document与每组只写临时目录沙箱不兼容限制。本次未放宽沙箱、未修改这些旧断言来冒充通过。相关报告/审计另行定向验证46项通过，但不能称完整端到端线上验收完成。

下次真实导出使用现有入口即可自动附加每日统计，B/C/D/E仍只按当轮逐项授权执行。
