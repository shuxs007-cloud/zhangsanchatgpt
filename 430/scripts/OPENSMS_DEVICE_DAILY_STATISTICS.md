# 任务A：设备每日统计程序

常规任务A已自动接入，无需单独执行第二个程序，也不会再读写一遍大报表。

## 工作表

- 名称：设备每日统计；位于主表之后，每台设备一行。
- 设备号：从卡槽完整前缀提取，例如110_01、110_1.01归为110；r110_34.01归为r110，不与110混在一起。数字设备号规范化，字母前缀保留；无法识别的主表记录单列“未识别设备”，不丢弃。
- 设备号与记录数之后，按主表原日期顺序保留每日5列：发送窗口数、回复窗口数、回复率、总发送量、总回复量。表头、列宽、边框等沿用主表；不套用号码级过期/换号背景色。
- 每日四个计数各自相加；回复率为同设备同日回复窗口数合计除以发送窗口数合计，而不是平均号码回复率。
- 分母为0时回复率留空；某计数存在缺失/非法源值时，该设备当天此项合计留空，不把不完整合计伪装成总量。完整报告记录异常计数。

## 程序及并发

生成器：`services/opensms_document_api/pipeline/device_daily_statistics.py`。

任务A复用已有源行，独立日期默认4线程（环境变量`OPENSMS_DEVICE_DAILY_WORKERS`可设1至8，实际不超过日期数）。工作器只处理只读数据；协调线程写表，合入现有一次保存，不并发修改同一工作簿。此项不改变任务B/C/D/E/F的授权或执行顺序。

独立验证器：`services/opensms_document_api/pipeline/verify_device_daily_statistics.py`，以不同的逐行算法重新计算源表并逐格比对，检查通过才交付。后续运营商刷新保持设备统计工作表不变。

## 给已有工作簿生成离线副本

只需要处理已有文件时，可运行：

```sh
/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -B \
  /Users/zhangsan/Downloads/430/scripts/opensms_device_daily_report.py \
  --input /绝对路径/已有报表.xlsx \
  --output /绝对路径/新增设备统计的副本.xlsx \
  --workers 4 --json
```

此脚本不访问浏览器、不重新导出、不执行任何后台写入。输出路径必须不存在，原文件不变。写入临时文件后独立回读设备统计、核对原工作表的值，再独占发布输出文件，JSON返回来源/输出SHA256及耗时。

已有文件加工结果只能称为离线副本/样例，不能称为本轮新鲜导出。

## 离线回归

新用例在`tests/test_opensms_device_daily_statistics.py`，已加入固定规则quick清单。覆盖分组、重复号码保留、跨年日期顺序、加权回复率、零发送、缺失、串并行一致、样式、篡改阻断、原表保持、真实构建与独立核验以及运营商刷新保留工作表。

```sh
/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -B \
  /Users/zhangsan/Downloads/430/scripts/opensms_verify_fixed_rules.py --mode quick --workers 2 --json
```

该验收入口从操作系统层禁止联网，临时写入隔离，不启动巡检。
