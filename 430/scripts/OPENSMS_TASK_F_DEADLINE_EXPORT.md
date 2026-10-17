# 任务F与截止日模板一键运行

## 单个号码的人工确认截止日并提交（2026-10-14）

当前真实用户明确给出手机号、续费截止日，并要求导入后台时使用：

```bash
/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node /Users/zhangsan/Downloads/430/scripts/opensms_manual_f_explicit_deadline.mjs --phone <10位手机号> --cutoff <yyyy-mm-dd> --authorization-source current-user
```

- 只处理一个号码。通过共用浏览器自有隐藏页和网站/report双锁实时核对后台ID、电话、ICCID、卡槽及原激活日期，保留原激活日，直接使用用户明确的截止日，不根据余额推算，不填写未知余额。
- 沿用原三列F导入模板，单独保存用户来源和本次文档，真实日期显示为yyyy-mm-dd，ICCID保持完整文本；独立逐行回读通过后才提交。
- 使用原导入接口、限速、持久意图和两次独立回读。最多发送一次；已有一致日期不再上传。未解决旧意图阻止新提交，不自动重试、不处理其它号码。
- 本入口只生成单号文件并按当次明确授权更新后台，不更改本地F原始激活库、续费覆盖层、余额、套餐、F查询或A定时器。
- 本地文件已生成但从未创建导入输入、提交回执或提交意图时，`resume --directory <本次精确目录> --authorization-source current-user`只继续同一已核验文件，禁止重生成或重放业务。
- 已提交后的归档异常只能用`reconcile --directory <本次精确目录> --authorization-source current-user`重新只读核验该精确号码和原意图，双回读一致及自有页已关闭才归档，不发送导入。原失败回执不改写，另存核验完成回执。跨盘归档保留本地原意图并保存交付盘哈希一致副本，不跨文件系统重命名。

## 人工核验续费表：离线更新及导出（2026-10-14）

当前用户明确指定续费表时使用：

```bash
/Users/zhangsan/Downloads/430/scripts/run_opensms_task_f.sh import-renewals --source <续费表绝对路径> --identity-state <成功巡检状态绝对路径> --authorization-source current-user
```

- 只读源表的电话、注册状态、余额、截止日期、套餐类型五列，不复制邮箱或密码。用户已确认本表均为续费号码，状态文字仅保留作数据，不沿用旧流程的两种状态筛选。
- 用精确成功巡检原始系统导出的手机号/ICCID映射，验证runId、SHA256、唯一性、OPR和原激活日期；不查询Grab、不访问浏览器、不提交后台。
- F库新增余额 `balance_cad`（未知留空，不默认为30）和套餐类型 `plan_type`（默认30；源表明确29才取29）。已有其他卡的套餐信息不重置。
- 表内有效截止日期直接规范为 `yyyy-mm-dd`，不再按余额加30天。表内日期缺失且余额大于0时，使用本次更新前的F库有效截止日加29天，明确标为临时推算、待人工核查。无余额或缺有效基准日不推算，保留原日期并报告。
- 续费截止日保存在独立、来源绑定的版本2续费覆盖层，优先于原Grab截止日；原激活日期、原激活+29天截止日及查询凭证保持不变。每次冻结快照和独立复核都验证此窄例外，不改变系统日期优先级和自动F导入门禁。
- 同一源文件SHA、源行、ICCID及规则形成唯一事件；再次导入同一文件不会再加29天。新文件可修正临时日期；新事件不会删除旧事件。
- 写前取得原F库排他锁并备份到核验挂载的zs01，SQLite事务写入，独立新连接逐条回读；原始激活/库存字段哈希必须不变。
- 生成本次号码范围的三列后台导入表及含余额、套餐、前后截止日、来源和临时标记的核对表，刷新固定最新本地F目录JSON。两个Excel独立回读及ZIP检查通过后交付；后台上传由用户完成。
- 可加 `--dry-run` 只读预检，不落库、不生成文件。

## 只导出当前数据库

```bash
/Users/zhangsan/Downloads/430/scripts/run_opensms_task_f.sh export --json
```

不联网、不打开浏览器、不运行B/C/D/E。不需要重新下载10日报表。
程序读取当前任务F数据库的全部记录，输出新的Excel和独立校验回执，不覆盖历史文件。
默认目录：`/Users/zhangsan/Downloads/430/outputs/task_f_deadlines/<时间戳>/`。

## 历史独立库存模式：先同步任务F，再导出

仅当前用户明确要求历史独立库存差集模式时使用；它不等于最新报表空日期补查，不作为定时导出入口：

```bash
/Users/zhangsan/Downloads/430/scripts/run_opensms_task_f.sh sync-export --authorization-source current-user
```

复用现有任务F：430后台静默读取本次Kooda库存，比较本地权威记录；只对缺口查询。
请求8个隔离会话，实际工作器不超过缺口数；不能隔离时自动降为1。无缺口不访问Grab&Go。
已有ICCID但未激活不联网重查。同步后落库、快照、导出三列表及报告，全程不运行B/C/D/E。

## 手动报表空日期补查入口

```bash
/Users/zhangsan/Downloads/430/scripts/run_opensms_patrol.sh run --json
```

手动巡检先使用本轮系统日期，Kooda缺失字段读取文档生成前冻结的本地库；仍缺激活日期或截止日的文档行才进入8隔离会话后台补查。后台F不阻塞主报告交付；本轮新结果不改本轮主表，下一次新鲜导出才可补入，系统已有字段绝不覆盖。
正常F只在本次实际新增有效激活日期时生成三列增量文件，列头与格式不变；零新增不生成/提示F文件。增量由提交回执runId、查询回执SHA、ICCID与日期、已提交数据库及Excel独立回读共同验证，不能混入历史记录；不自动上传系统。
本页前述独立`export`命令仍保留全库导出，需要用户明确要求；正常巡检不自动重复生成全库模板。

## 定时任务A与进度入口

```bash
/Users/zhangsan/Downloads/430/scripts/run_opensms_task_a.sh --json
/Users/zhangsan/Downloads/430/scripts/run_opensms_task_f.sh status --state <本轮runner状态绝对路径> --json
```

第一条只执行定时A，BCDE与联网F跳过；允许只读冻结本地日期用于报表补缺，不执行F查询/写库/增量导出。第二条只读运行进度，支持主state验证不可变job后委托后台F子state；不启动任务。F期间每5分钟汇报，完成立即报告，主报表先交付不代表F结束。

## 固定数据合同

| 列序 | 列头 | 内容 |
|---|---|---|
| 1 | ICCID | 规范化18至22位数字文本，移除末尾单个F/f，避免精度损失 |
| 2 | 截止日 | 数据库截止日，yyyy-MM-dd；验证为激活日+29天 |
| 3 | 激活日期 | 数据库确认的激活日期，yyyy-MM-dd |

包含全部记录，不过滤未激活/未找到，缺日期留空。
原模板归档：`/Users/zhangsan/Downloads/430/scripts/templates/task_f_deadline_import.xlsx`。
数据库：`/Users/zhangsan/Downloads/430/opensms_artifacts/state/task_f_kooda_activation.sqlite3`。

一次读取事务绑定完整数据，批量写入Excel；独立回读验证后才发布最终文件。
ICCID重复、日期冲突、模板列头变化、输出已存在或校验失败时返回失败JSON和退出码2，不联网补救。
正常运行不生成预览，也不让AI逐行计算。源数据读取、Excel写入、独立回读互相依赖，保持顺序；不盲目增加线程。
速度以输出JSON `elapsedSeconds` 和 `performance` 实测，不将本地导出耗时冒充联网同步耗时。

## 开发验证

- 本地全库实测：9045条，7174条有日期，1871条空日期；约5.6秒，含生成和逐行独立回读，不含联网任务F。
- 56项相关离线测试通过：新导出器23项、任务F与接入15项、原巡检授权及静态检查18项。
- 已在实际exFAT交付盘验证发布与禁止覆盖：不支持原子排他重命名时，使用独占新建、完整复制及哈希复核；只有核验回执成功发布后才报告成功。已有文件不覆盖。
- 本次开发没有执行真实联网任务F、运营商修改或下卡通知。查询链路复用原程序，新增接入以离线测试验证。
