# OpenSMS 任务C

任务C默认关闭，只在当前真实用户消息明确写出“强制执行任务C”，且运行环境中
`OPENSMS_FORCE_TASK_C=1` 时执行。定时巡检、历史授权和任务B授权均不能触发任务C。

## 规则

1. OPR包含Kooda、Koodo或Telus1、4日回复率严格低于5%的号码转“换号区T”。
2. OPR包含Chatr、4日回复率严格低于4%，且换号状态为空或“未换号”的号码转“换号区C”。
3. 等于5%或4%均不命中。发送为0、回复率缺失、Chatr已换号或换号状态未知均不写入。
4. Kooda/Koodo组优先，两组候选必须互斥。
5. 当前来源属于A1、A11、A12、B1、B11或B12时，4日发送窗口数必须大于等于50才进入回复率判断；其它来源仍要求4日发送窗口数大于0。
6. 任务B过期路由优先：已过期、服务器次日到期，以及已经位于过期下架卡C/T或快过期隔离区C/T的号码均不得进入任务C。

## 执行约束

- 只使用本轮新鲜export4day与Google表生成并复核通过的卡槽关联结果主表。
- 先运行 `opensms_resolve_task_c_target.mjs`，从当前后台分别唯一精确解析启用的“换号区T”和“换号区C”。
- 使用 `opensms_generate_task_c_candidates.py` 生成两组候选和manifest。
- 每组统一由 `opensms_set_carrier_candidates.mjs` 验证、写入，再由
  `opensms_verify_carrier_candidates_readonly.mjs` 独立只读回读。
- 报告分别展示候选、后台有效、已在目标、离开来源、缺失、重复、实际写入、成功、
  失败和独立复核结果，并保存全部回执。

未取得授权时，不解析目标、不生成候选、不提交写入，报告“任务C默认关闭，本轮未生成候选、未修改号码”。
