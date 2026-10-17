# 用户指定ICCID：仅历史Excel卡槽查询

入口：`opensms_find_iccid_slots_in_excel.py`。此入口不读取任务F数据库、不读取库存/查询JSON日志、不访问网络、不运行任务A/B/C/D/E/F；用户文本清单是唯一目标范围。依赖已有Excel解析器和流式输出校验器，但不调用其数据库或日志入口。

```sh
/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 /Users/zhangsan/Downloads/430/scripts/opensms_find_iccid_slots_in_excel.py --iccid-list /绝对路径/ICCID清单.txt --start 2026-06-24 --end 2026-09-24 --workers 8 --output /绝对路径/新建输出目录
```

- 每行一个文本ICCID，允许首行`ICCID`。保留原输入、行号和SHA256；非法行导致停止，不静默删除。查询键允许去末尾一个F/f，保留所有数字，不纠错、不截位、不补前缀、不按尾号匹配。
- 仅搜索日期范围内的XLSX/XLSM/XLTX/XLTM，以及内容确为OOXML的XLS；旧二进制格式不猜测，明确列为未读文件。默认目录为Downloads、Documents、Desktop和`/Volumes/zs01`，可重复`--root`指定。包含隐藏sheet，排除开发测试、回收站、锁文件及本工具历史结果目录，避免拿自己先前汇总的库/日志结果再次当作原始Excel。
- 日期优先文件名或最近归档目录的明确日期，没有才按修改时间，含首尾。文件日期不等于卡槽观测生效时间。源ICCID仅接受文本、非公式；位置来自同一行明确ICCID/卡槽列，保留字母前缀及前导零。多ICCID或多卡槽列的关联单独标记待核对。
- 搜索所有工作表，不只取最新一次卡槽。不同ICCID/卡槽字符串组合列入“所有卡槽位置”，每一次匹配的Excel路径、SHA256、sheet、行号、两个单元格及日期依据全部列入“逐条Excel来源”。Google/处理表/库衍生sheet按Excel实际来源标注，不引入Excel外的记录。
- 8线程哈希、最多8解析进程，有界16个在途任务；SHA256相同内容只解析一次，但保留所有文件副本出处。工作簿单写者生成，独立openpyxl逐格回读哈希、ICCID文本类型、公式错误、行数及ZIP CRC核验。
- 输出7个sheet：ICCID卡槽汇总、所有卡槽位置、逐条Excel来源、未找到、命中但无可用卡槽、扫描文件清单、说明。另输出输入快照、结果、来源、统计JSON，仅作本次查询交付，不是业务输入。输出目录必须不存在，不覆盖原文件。
- 不回填数据库，不把多个历史卡槽选成一个当前卡槽；未找到仅说明本次可读取Excel范围内无可靠位置。

测试：`python3 -m unittest discover -s /Users/zhangsan/Downloads/430/tests -p test_opensms_find_iccid_slots_in_excel.py -v`。集成测试同时放置包含命中信息的JSON日志和不可读数据库，验证两者完全不参与结果。
