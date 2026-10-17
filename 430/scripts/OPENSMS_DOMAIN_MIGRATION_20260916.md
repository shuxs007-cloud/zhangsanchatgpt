# 430后台域名迁移核验

入口：https://adminsms.smsoug.com/index

同源地址：https://adminsms.smsoug.com

## 配置和代码

核对时，主导出、协议客户端、AES参数读取、隐藏页导航、登录恢复、服务器日期读取、余量采集及策略文件均已指向上述新域名；保留这些现有变更。

本次补齐两个残留：

- `scripts/opensms_reply_rate_today.mjs`：页面筛选改为严格比较自身 `ADMIN_ORIGIN`。
- `scripts/opensms_device_maintenance_force.mjs`：页面筛选改为严格比较自身 `TARGET_URL` 的origin。未运行该维护脚本。

两个辅助脚本不再各自保存旧域名正则。历史交付和历史故障说明未改写。任务授权、静默限制、过期策略、号码保护均保持不变。

## 离线验证

在 `tests/test_opensms_export_entry.py` 增加运行脚本旧域名残留检查与核心同源配置检查，接入原固定规则验证入口。

2026-09-16 21:58:14（北京时间）开始的验证：662项通过，0失败；11组，2路并发，用时18.819秒。操作系统禁止测试联网，并限制每组写入范围；并非线上业务测试。

[完整离线测试回执](/Users/zhangsan/Downloads/430/outputs/opensms-fixed-rules-0myom16s/verification.json)

两个修改后的辅助脚本均通过Node语法检查。当前运行脚本扫描未再发现此前域名残留。

## 本次430静默只读验证

- 只从430已知配置的 `DevToolsActivePort` 解析当前端点，锁定该端点，清除继承的API页面ID。
- 2026-09-16 21:58:59（北京时间），原登录预检函数在新域名 `/day` 完成已认证只读API检查，结果为 `authenticated=true`，未执行登录提交。
- 协议参数由当前新域名应用脚本读取；未使用固定旧参数，未输出或归档令牌、Cookie、密钥。
- 原导出脚本以 `OPENSMS_EXPORT_PREFLIGHT_ONLY=1` 运行：新域名 `/export4day` 就绪，结果 `passed`，用时1539毫秒。
- 临时页面使用隐藏目标；预检记录 `silentBrowserMode=true`、`browserForegroundActivationAttempted=false`、`playwrightBrowserConnectionUsed=false`。未切换可见页面。
- 预检退出后再次只读检查430目标列表，确认本次精确隐藏目标已关闭。
- 页面服务器时间读数为 **2026-09-16 09:59:00**；这是后台时区时间，不是本机北京时间。
- 导出提交0次，号码及到期标记写入0次，未启动任务B/C/D/E/F。

以上证明新站登录、只读接口、当前协议和导出页面就绪；没有运行完整导出，不能当作新鲜报表交付或全部业务操作的线上验收。

## 后续调用

继续使用原统一入口 `scripts/run_opensms_export.sh --json`，定时器无需另换一套程序；业务路径仍为新域名下的 `/day`、`/phoneConfig`、`/export4day` 等。新增域名或后续协议变化仍须重新验证，不复用本次读数冒充实时状态。
