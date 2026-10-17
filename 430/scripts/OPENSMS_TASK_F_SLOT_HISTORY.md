# 任务F未激活卡：历史Excel卡槽查询

入口：`opensms_task_f_slot_history.py`。仅查询证据，不改变任务A/F正常流程，不联网、不更新生产数据库或历史工作簿。

```sh
/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 /Users/zhangsan/Downloads/430/scripts/opensms_task_f_slot_history.py --start 2026-08-24 --end 2026-09-24 --workers 8 --output /Users/zhangsan/Downloads/430/opensms_artifacts/slot_history/另一个未存在的目录
```

- 目标：以只读事务冻结任务F库全部 `activation_missing` ICCID，不把单纯日期为空认定未激活。
- 默认扫描 Downloads、Documents、Desktop、`/Volumes/zs01`；可重复传 `--root` 自定义。包含隐藏工作表，排除开发测试目录、系统回收站和本工具输出。发现目录读取错误则停止，缺失根目录单独记录。
- 日期：优先取距离文件最近的文件名/归档目录日期，无日期时使用修改日期并明确标注；含起止日，不把文件时间当卡槽观测时间。
- 文件：XLSX/XLSM/XLTX/XLTM；扩展名为XLS但内容实际为OOXML也可读。旧二进制XLS/XLSB不猜测解析，列为文件级错误，不能宣称已完整查完。
- 身份：18至22位文本ICCID去末尾恰好一个F/f后精确匹配，不按手机号、后几位或卡槽倒推。公式/数字型ICCID不作为可靠恢复依据。
- 卡槽：必须来自同一行明确ICCID和卡槽表头；保留设备字母前缀和前导零，不合并 `r112` 与 `112`。多ICCID/多卡槽列单独标注；无表头、空卡槽、格式不明列入不能恢复清单。
- 来源：所有有效来源保留文件SHA256、文件日期及依据、sheet、行号、两个单元格地址。Google、系统或历史处理表、库衍生表分开标注；多个历史卡槽不能自动选最后一个冒充当前位置。
- 并发：1至8个有界工作器；哈希I/O线程池、XML解析进程池、内容SHA256去重。相同表内容可在进程内复用解析结果；工作簿输出由单进程负责，随后用openpyxl独立打开逐格核验并检查ZIP CRC。
- 输出：查询结果、精确匹配证据、命中但不能恢复、扫描文件清单四个sheet；另有JSON完整证据和MD报告。目录必须不存在，禁止覆盖旧结果。未找到仅表示本次范围无证据，并不证明从未上架。
- 任何正式恢复应另行核验来源可信度、相同ICCID多位置冲突及最新身份，保留历史时间，不覆盖有效数据，不修改日期、运营商或后台业务。

测试：`python3 -m unittest discover -s /Users/zhangsan/Downloads/430/tests -p test_opensms_task_f_slot_history.py -v`。

## 全部任务F卡、近三个月模式（2026-09-24新增）

独立入口 `opensms_task_f_all_slot_history.py`，保留上述未激活卡入口不变：

```sh
/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 /Users/zhangsan/Downloads/430/scripts/opensms_task_f_all_slot_history.py --start 2026-06-24 --end 2026-09-24 --workers 8 --output /Users/zhangsan/Downloads/430/opensms_artifacts/slot_history/新建三个月查询目录
```

- 范围：只读事务冻结任务F激活库和库存库的ICCID并集，不限于未激活卡；先扫描有通过状态的历史任务F库存/查询回执，补充仅在历史任务F回执出现的精确ICCID，再扫描Excel。
- 时间：最近三个日历月，含首尾日期；沿用明确文件名/归档目录日期优先、缺失时采用mtime的规则，不把文件日期当作卡槽实际生效日期。
- JSON：仅解析明确的 `rows` / `results` 及其 `sourceData` / `sourceOccurrences`，子记录必须有自身有效ICCID；同一对象内ICCID字段冲突即不采用。不读取认证字段，不连接Grab或OpenSMS，不用手机号或尾号推断。
- 状态：文件级失败回执不作可靠恢复依据；部分查询报错但文件状态为 `passed_with_transient_errors` 时，只提取其中原始系统身份及卡槽，不把它解释为激活查询成功。
- 并发：最多8个哈希线程、8个解析进程；进程内缓存最多2张表，有界在途任务。Excel只解析明确的ICCID、卡槽列，避免反复解码全部统计列。
- 存储：完整逐行来源保存在 `逐条证据/<内容SHA256>.jsonl.gz`；每条含工作表/单元格或JSON位置，扫描文件清单保存同内容的全部文件副本。失败文件的部分账本不采用，索引状态为failed。
- Excel：全部任务F卡、未激活卡子集、多卡槽待核对、未找到卡槽、卡槽来源汇总、扫描文件清单、说明。按ICCID/卡槽/来源聚合；原始逐条证据不截断、不塞进超大Excel。
- 计数：文件内容SHA256去重。同卡多卡槽可能是迁移或来源冲突，不合并r前缀、不推断当前位置。原始库存及其嵌套来源可能重复描述同一记录，观测次数不等于独立物理卡数。
- 原始系统库存/F回执、Google、系统或历史处理表、本地库衍生表分别标注。处理表不等于系统原始权威证据。
- 输出校验：独立openpyxl回读全部单元格并比对滚动哈希、类型、总数和ZIP CRC；SQLite索引执行完整性检查。失败文件明确列出，不声称已全部成功读取。
- 本轮仅查找，不更新生产数据库、不自动回填历史或当前卡槽、不改任务A/F在线业务规则。

完整回归：`python3 -m unittest discover -s /Users/zhangsan/Downloads/430/tests -p 'test_opensms_task_f*slot_history.py' -v`。
