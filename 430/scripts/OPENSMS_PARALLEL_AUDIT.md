# OpenSMS 全流程并行审计器

`opensms_parallel_audit.py` 用程序读取一轮巡检已经生成的 Excel 和 JSON
回执，完成确定性检查。它是只读程序，不登录后台、不修改运营商、不执行任务B或任务C、
不打开 WPS，也不需要模型逐表查看。

## 最简运行

首次安装固定环境：

```bash
./scripts/install_opensms_parallel_audit.sh
```

以后统一通过固定入口运行：

```bash
./scripts/run_opensms_parallel_audit.sh \
  --delivery-dir '/Volumes/zs01/chatgpt/OpenSMS/20260819/20260819T202600+0800' \
  --workers auto \
  --json
```

固定环境不依赖 WPS，也不需要额外安装 Excel 插件。程序直接读取并校验 XLSX
文件结构、工作表、表头、日期、回执和校验清单。

交付目录中已经包含本轮配置和回执时，也可以直接使用 Python 入口：

```bash
python3 scripts/opensms_parallel_audit.py \
  --delivery-dir '/Volumes/zs01/chatgpt/OpenSMS/20260819/20260819T202600+0800'
```

程序自动找到本轮配置、源表、Google表、结果表及回执。标准输出只有状态、
检查数量、实际修改数量、结果文件和异常；完整诊断写入一个 JSON 回执。

也可以指定配置和并发数：

```bash
python3 scripts/opensms_parallel_audit.py \
  --config /path/to/opensms_patrol_finalize_config.json \
  --delivery-dir /path/to/current-run \
  --workers auto \
  --json
```

## 程序检查范围

- 三份 XLSX 的 ZIP 结构、大小和 SHA-256。
- 结果表工作表、主表字段、字段顺序、最近四日连续性和公式错误。
- 后台导出、Google下载、两个服务器日期及文档接口作业的新鲜度。
- 卡槽关联处理器全部检查项、匹配行数及服务器日期。
- 每个运营商组的候选、验证候选、写入和独立回读数量。
- 同轮候选互斥，避免同一号码在普通流转阶段进入多个候选组。
- E区原运营商备份、下架恢复永久关闭状态、任务B与任务C授权状态。
- 任务C两组候选的4日严格回复率边界、Chatr未换号条件、共同目标及组间互斥。
- 十二组余量（含B3、B1V、B11V、B12V）、首次回复率来源、统计日期、比例重算及自动加量关闭状态。
- 最终交付目录的 `FILES.tsv` 和 `SHA256SUMS.txt`。

## 并发安排

可同时执行：

- 源表、Google表和结果表完整性检查。
- 各类只读 JSON 回执检查。
- 每个运营商组的事后回执一致性检查。
- 候选互斥、余量、下架恢复关闭状态、任务B/任务C授权及交付清单检查。

v61默认 `auto` 使用最多4个检查线程（同时受CPU限制），由同输入1/2/4/8路
实测选择；显式 `max` 不超过CPU与16的较小值。不是线程越多越快。
同次结构检查只解析一次主表XML、合并错误/文本遍历；运营商核对只解码必需两列。
单次文本分类缓存最多4096项，不跨文件/轮次。每次审计仍重新读文件，完整CRC、
SHA-256、公式错误、身份/运营商回读和最终交付检查全部保留。

必须串行：

- 导出完成后才能分析。
- 运营商实际写入必须按业务优先级依次执行。
- 每组必须先验证、再写入、再独立回读。
- 任务B必须等待关联表生成完成，并取得当前授权。
- 任务C必须等待任务B后工作簿刷新完成，取得当前授权并实时分别唯一解析换号区T和换号区C目标。
- 最终 SHA-256 清单必须最后生成。

查看程序内置并发计划：

```bash
python3 scripts/opensms_parallel_audit.py --show-plan
```

## 退出码

- `0`：检查成功，或只有允许保留的业务提示。
- `2`：至少一项检查失败。
- 加上 `--warnings-as-errors` 后，提示也会导致非零退出。
