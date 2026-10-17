# 文档核验去重复扫描：开发验证

## 改动范围

共用 `opensms_verify_card_slot_fast.py` 原先额外用 `data_only=False` 打开一次最终工作簿，仅为统计应保持为文本的数字。现在这一项从最终 XLSX 的 OOXML 字符串单元格独立读取，不再创建第二份 openpyxl 单元格模型。

保留完整源表与最终表的独立回读、业务计算、身份/ICCID/来源优先级、日期、红色行、样式、未使用端口、公式错误检查。公式缓存值不当成数字文本；共享字符串及富文本沿用 openpyxl 解码语义，长 ICCID 不转数值。不并发修改同一工作簿，不改变 BCDE 门禁或业务写入顺序。

## 实测

固定输入为失败轮 `20260907T071225+0800` 的本地源表、生成中间表、本轮库存和冻结缓存；只用于离线开发，不作为新鲜业务交付。baseline 已包含本轮 Excel 数值语义误报修复，因此以下只衡量重复扫描优化，不混入修复收益。

| 项目 | 结果 |
| --- | --- |
| 采样 | baseline/current 交替，共各 3 次，不开启 profiler |
| 核验中位数 | 6.601 秒 → 5.615 秒 |
| 此本地核验阶段降幅 | 14.94%，约 0.99 秒 |
| 完整核验 JSON | 全部 6 份 SHA-256 相同 |
| 历史输入 | 所有 SHA-256 前后不变 |

基准回执：`outputs/document_verify_benchmark_20260907/benchmark.json`。测量受机器并行负载影响，单次结果不保证总是更快；不能据此声称联网整轮也提速 14.94%。profile 单独测得 `review_numeric_text` 占 5.50/14.57 秒，profile 开销不纳入上述基准。

完整 `opensms_process_document.py --no-google --force-local` 开发重放通过：7.239 秒，退出码 0，所有 `failed_checks=[]`。shell 阶段整秒计时为生成 5 秒、额外 enrich 0 秒、独立核验 2 秒。回执：`outputs/development_document_replay_20260907_4/replay.json`；此项只有优化后一次完整管线验证，不能当作完整管线前后 A/B 提速证明。

前 3 次开发尝试被过严的 deny-all-writes 沙箱阻止 Bash here-document 临时文件创建；记录保留在前 3 个开发目录，这些不是生产巡检失败。最后一次使用与统一离线测试一致的操作系统隔离：禁止联网及生产状态、日志、历史交付、本轮历史输入写入，允许开发输出与 OS 临时操作；7 份历史输入校验不变。

## 可复用命令

`scripts/benchmark_opensms_document_verify.py --state <历史state绝对路径> --receipt <历史卡槽生成回执绝对路径> --baseline <修复后基线核验脚本> --output <全新开发目录> --repeat 3`

同一脚本加 `--profile --repeat 1` 可定位热点，不用其时间作正式提速数字。加 `--full-pipeline` 可在操作系统隔离下运行完整本地文档处理。所有模式均不访问浏览器、不重跑业务、不发布新鲜报表；输出目录必须不存在。

新增 `tests/test_opensms_numeric_text_ooxml.py` 的 8 项回归已通过，覆盖文本精度、数字/布尔区分、公式缓存和错误、共享/富文本、隐藏表、顺序、路径及独立输入不变；已由统一 `unittest discover` 自动纳入。
