# 任务H：A/B运营商日限额

任务H为独立、手动执行的配置任务，不接入普通导出或定时器，不调用任务F。

| 页面字段 | 公共变量 | 目标值 |
| --- | --- | ---: |
| 日下发限制 | dailyDispatchLimit | 150 |
| 日发送量 | dailySendAmount | 130 |

公共变量：`/Users/zhangsan/Downloads/430/scripts/opensms_task_h_settings.json`。

范围为当前未删除的A/B数字组，包括V组、A2、A21、A3、B3；不包括C组或其它业务区。名称保留原样，不改名称后缀中的数字。已一致配置不重复提交。其它配置、号码归属、任务F、定时器和历史报告保持不变。

## 使用

只读预览，不提交：

```sh
cd /Users/zhangsan/Downloads/430
bash scripts/run_opensms_task_h.sh preview --json
```

仅在本轮人工明确要求执行任务H时：

```sh
cd /Users/zhangsan/Downloads/430
bash scripts/run_opensms_task_h.sh run --force-task-h --authorization-source current-user --json
```

每次读取新鲜全量范围和明细，不复用预览或旧文件。使用原网站/report锁及共用后台隐藏会话，串行限速提交；先保存意图，写后独立回读。回执和中文摘要在独立H目录，每轮新建，不改A/F文件。重复执行时已达目标配置不写。

## 停止条件

未解决的H意图、H锁残留、分页/ID/名称歧义、字段无效、写前配置变化、公共设置改变、接口异常或独立回读失败时停止，不自动重放。后台接受不代表独立成功。不要手工删除`pending.json`、修改历史回执或通过另建台账绕过保护；结果不明需要单独只读核验。

## 字段依据及验收

当前页面已加载的同源运营商前端脚本：`/static/js/chunk-b2c8e884.0a9a6a05.js`，SHA256 `22da91abc966da630be62845760e602895b2a622a2c1159d1712a565693e3053`。
表格字段分别是`sendLimitByDay`与`sendNewLimitByDay`；后者在编辑页标为“每日卡群发量”。不同于`userSendSizeByDay`、`userSendNewChatSizeByDay`、`dailyTotal`和`dailyRemaining`。前端修改流程先`GET /carrier/config/<id>`取明细，再`PUT /carrier/config`提交完整明细；H保留原字段，不填充默认值。

离线验收：

```sh
/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node --test tests/test_opensms_task_h.mjs
```

首次实时只读预览：范围13个，已一致2个（B12/B12V），待修改11个，提交0次；详细值见本轮H回执。仅代表该预览时点，实际执行必须重新读取。
