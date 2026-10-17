# OpenSMS 一键巡检程序

这个入口把新鲜导出、Google关联表、双页面服务器日期、候选生成、允许的运营商写入、独立回读、卡槽文档、最终运营商状态刷新、报告、清单和并行审计串成一次可审计运行。

正常流程已经全部程序化，不再需要 AI 手工逐步分析或拼文档。程序使用有边界的并发：只读下载、日期、余量、候选分析和文档输入解析可以并发；合并工作簿的统计填充已经合入首次生成并只保存一次；运营商实际写入仍按策略顺序串行，并且每组写后独立回读。

完整业务规则仍只读取 `OPENSMS_10DAY_PATROL_POLICY.md`。程序不复制第二套完整规则，策略文件更新后无需修改定时任务提示。

## 安全检查（不会连接浏览器或执行巡检）

```bash
/Users/zhangsan/Downloads/430/scripts/run_opensms_patrol.sh check
```

## 正常定时巡检

```bash
/Users/zhangsan/Downloads/430/scripts/run_opensms_patrol.sh run --json
```

默认性能参数为 `--io-workers 4 --analysis-workers 3 --refresh-engine targeted-ooxml`。正常取数已改成“精确 BitBrowser 430 Raw CDP + 后台 target”：导出仍等待后台生成完成，但不再建立会耗时约 120 秒的 Playwright 整浏览器连接；`/day` 与 `/phoneConfig` 的服务器日期在导出完成后由一个本地进程并发读取。所有 target 均以 `background=true` 创建，结束后自动关闭，不切换用户当前可见页。通常不要继续增加线程；后台导出生成本身和磁盘吞吐会先成为瓶颈。需要回退到旧的整本工作簿刷新器时，可临时使用 `--refresh-engine artifact-tool`。

任务B、任务C默认关闭。定时器文字和历史消息不能授权它们。任务B获当轮真实用户授权后，候选计算、目标分组、写入、独立回读、最终合并文档刷新和 Markdown 报告均由同一个程序完成，AI只启动程序并整理最终摘要。

任务D默认关闭，只有当前真实用户明确授权并同时满足命令参数与环境门禁才执行。它在普通运营商流程、任务B后文档刷新和任务C后文档刷新全部完成后启动：结果表两日0回复分析与卡保留全部分页读取并发准备；换号区当天标记、卡保留运营商500条分批修改、卡保留加7天标记按顺序串行执行。每个到期标记批次最多1500条，必须保存接口输入/命中/更新会话回执，并从卡保留记录独立回读新增记录。断点状态不确定时程序停止，禁止自动重复提交。

当前用户明确授权任务B时，调用方必须同时提供：

```bash
OPENSMS_FORCE_TASK_B=1 /Users/zhangsan/Downloads/430/scripts/run_opensms_patrol.sh run --force-task-b --authorization-source current-user --json
```

任务C同理使用 `OPENSMS_FORCE_TASK_C=1`、`--force-task-c`。

当任务B与任务C同轮执行时，任务B的已过期和后台服务器次日到期结果为锁定优先级；任务C生成、提交、最终归并和审计均会拒绝把这些号码再次改入换号区。

每轮状态保存在交付目录的 `receipts/opensms_patrol_runner_state_*.json`。异常中断后可用 `--resume-state` 指定该文件继续；已完成且回执仍存在的阶段不会重复执行。

使用 `--json` 时标准输出只返回紧凑高信号 JSON，完整机器结果保存在其中的 `summaryPath`；每轮固定交付新鲜源文件、写入后刷新版合并工作簿和完整 Markdown 报告，完整命令日志、候选、写入回执、回读与审计都保存在本轮交付目录。这样定时调用只需让 AI 读取策略、启动一次程序并转述少量高信号字段，不再把大段空任务结构送入模型。

## 本地耗时剖析

不连接浏览器、不执行写入，只读取一轮已有交付回执：

```bash
/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 \
  /Users/zhangsan/Downloads/430/scripts/opensms_patrol_profile.py \
  /Volumes/zs01/chatgpt/OpenSMS/日期/轮次目录
```

最终摘要 JSON 会保存实际线程数、刷新引擎和阶段耗时。若程序失败，仍以失败报告与断点状态为准，不能复用旧工作簿猜测完成。
