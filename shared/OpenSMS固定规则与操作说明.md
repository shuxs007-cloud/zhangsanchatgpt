# OpenSMS 固定规则与统一入口

更新：2026-09-26。脚本目录可整体搬移；入口自动定位目录和 Node.js。没有创建新定时器，没有把历史临时指令变成自动任务。

## 已固化的规则

| 项目 | 行为 |
|---|---|
| 绑定 | 仅补缺失，保留其它绑定；重复调用不重复新增 |
| 只绑定 | 只保留明确指定集合，删除多余及重复关系 |
| 全部取消 | 仅取消明确指定运营商，B12 不匹配 B12V |
| 身份核对 | 账号必须精确且唯一匹配；运营商按完整名称或短码边界唯一匹配 |
| 浏览器 | 只静默复用430已登录的新域名目标；不激活、不置前、不新建标签 |
| 限速 | 串行请求，响应后至少间隔2秒；从 opensms_rules.json 的 defaults.minIntervalMs 读取，最低2秒 |
| 跨窗口互斥 | 统一入口、原生绑定脚本和低余量绑定脚本共享 ~/.opensms/locks 下的430会话锁，含查询和复核；占用时等待，子进程复用锁。异常退出残留锁须核查后清理，不自动抢锁。其它脚本或浏览器工具必须接入 opensms_site_lock.mjs，不能绕过锁直接请求；锁不能阻止未接入的其它AI操作。 |
| 登录与错误 | 不输出凭据；登录失效停止；403先冷却至少30秒，再后台刷新重试一次，仍失败停止；验证码需人工处理 |
| 证据 | 唯一请求目录、修改前快照、业务回执、独立查询、中文Markdown报告 |
| 重试 | 相同request-id禁止重放，包括上次不确定或失败；先查回执和当前状态 |
| 并发 | 统一入口有互斥锁；不要同时绕过入口运行旧脚本 |

## 手动组合

这些是便捷命令，不是对所有账号永久生效的定时规则；最新明确指令优先。

| 名称 | 行为 |
|---|---|
| jingliao | 指定账号加绑 A1、A12、B1、B11、B12、B1V、B11V、B12V |
| a3-only | 当前A3账号只绑定 A3、B1、B11、B12、B3 |
| c2-only | 当前C2账号只绑定 C2 |
| stock | 指定账号加绑 A2、A21，不自动处理所有含关键字账号 |
| b1-b12 | 当前B1全部账号加绑精确B12 |

## 用法

入口：本目录的 `opensms`。默认只预览；写入必须明确加 `--apply`。

```sh
./opensms preset b1-b12 --apply --request-id b1-b12-20260926
./opensms preset jingliao 'YR02中控' --apply
./opensms add-users '账号一,账号二' 'A1,A12,B1'
./opensms only-users '账号' 'B1,B11,B12' --apply
./opensms add-groups 'A1,B1' 'B12' --apply
./opensms preset a3-only --apply
./opensms preset c2-only --apply
./opensms clear-groups 'A11,A12' --apply
./opensms query-user 'syt'
./opensms query-group B12
./opensms add-prefix 'QYQX,小梦精聊' 'B1,B11' --apply
./opensms mirror-users 'syt' 'shangshuiop精聊' --apply
./opensms restore-users /绝对路径/before_accounts.json --apply
./opensms rollback-batch /绝对路径/精确新增批次.json --apply
```

`mirror-users` 为追加参考账号的运营商，不删除目标账号已有绑定。恢复命令要求本入口生成的账号快照，并验证账号ID；空集合或已不存在的运营商必须人工复核。批次删除要求每条配置ID、账号ID和运营商ID均为字符串，只删除记录里的关系。

结果在 `opensms_runs/<request-id>/`：`before_accounts.json`/`before_groups.json`、所有协议调用回执、独立复核、`result.json` 和 `报告.md`。失败不会自动重跑写入；即便业务返回成功，独立复核失败也报失败。

## 唯一现行自动绑定策略

四组 **A3+B1+B11+B3** 的 `dailyRemaining` 求和除以 `dailyTotal` 求和，严格低于10%才可触发。要求四组唯一、数据有效，不取百分比平均。

- 每次写入前校验真实北京时间在01:00:00至10:00:00（含边界）。
- 当前四组账号去重并集，仅补缺失的精确B12，保留所有已有绑定。
- 本轮新增关系独立存批次；原有B12不记录，不覆盖旧批次。
- 当天13:00起，仅按批次精确ID撤销，已不存在记alreadyGone，不删除后来重建的关系。
- created=0不建批次、不建任务。
- 旧A3单独触发A1/A11、旧三组策略、18点清理和下架恢复均不由此入口执行。

```sh
./opensms auto-checkpoint 20260926_0730 --apply --request-id checkpoint-20260926-0730
./opensms cancel-today 20260926 --apply --request-id cancel-20260926-1300
```

这里只固化执行逻辑，不创建、删除、重开或更改Codex定时器。当前定时器状态须由Codex自动化工具实时检查，不能从历史文件推断。自动绑定有新增时，仍必须由调用任务确认当天13:00调度已经安排。旧路径下或迁移前的待撤销批次应先核对后放到当前目录，不能遗漏或凭账号重建ID。

## 提速范围

来源/目标分页列表集合比较，已有绑定直接跳过；代理清单在单次进程内缓存；统一入口自动解析、记录和独立复核，减少临时脚本及手工步骤。不会省略校验或提高接口并发。大量真实新增/取消仍受后台限速约束。

## 测试

```sh
node --test opensms_ops.test.mjs opensms_binding_protocol.test.mjs
```

测试为离线运行，覆盖精确匹配、账号歧义、加法保留、只绑定集合、重复ID、13点门禁、大整数ID、默认预览、实际命令路由、独立回读及重复请求/互斥保护，不改后台数据。
