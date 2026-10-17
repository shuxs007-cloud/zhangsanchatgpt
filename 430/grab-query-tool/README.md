# Grab ICCID 本地查询工具

双击 `启动Grab查询工具.command`，或者在服务运行时打开 http://127.0.0.1:43881 。

独立 Windows 版见 `windows/README.md`；发行包使用 `Start.cmd`，自带 Windows Node 运行时与公开依赖，不依赖 Mac/Codex。Windows 的数据单独保存在 `%LOCALAPPDATA%\GrabQueryTool`，不会复制 Mac 登录信息或历史。

独立 Mac 完整包见 `macos/README.md`；发行包使用 `Start.command`，内置 Apple 芯片与 Intel 运行时，默认端口 43883，数据在 `~/Library/Application Support/GrabQueryTool`，与本目录旧版隔离。

## 使用

1. 在大文本框粘贴 ICCID，每行一个，也可用空格、逗号、分号分隔。只去掉末尾一个 F/f，保留其他数字；无效项会明确报错，不悄悄删掉。
2. 点击“开始新查询”。每次都是新任务，全部实时访问 Grab，不从历史结果或任务 F 数据库取值。同一任务内按去 F 后 ICCID 去重，原始输入和重复次数仍保存。
3. 页面实时显示总数、已查、剩余、状态和结果。批量搜索后只接受清单中完全相同的 ICCID；未命中的号码再进行完整 ICCID 查询。
4. “暂停”等待当前请求收尾并保存断点；“继续”只查询该任务尚未完成的 ICCID。重启后不会自动恢复业务查询，要手动点击继续。需要刷新全部结果时，复制清单后开始新任务。
5. “停止”不再派发请求，保留已完成结果；停止的任务不可继续，需新建任务。正在进行的请求最多约 60 秒后收尾，停止后不采纳该在途回包。
6. 完成后点击“导出 Excel”。含查询结果、汇总说明、原始清单三张表。已暂停/停止/失败且存在结果时，可导出明确标注的部分结果，未查项显示“未查询”。Excel 中 ICCID 是文本，保留全部位数。
7. 历史查询列表保留开始/结束时间、数量、结果和下载。点击一项可查看；历史下载不会联网重查。

## 登录与 Cookie

- 优先使用已有 BitBrowser 430 的 Grab 会话，正常查询不置前、不导航用户页面。
- 首次未登录或 Cookie 失效时，任务进入“等待登录”，打开独立登录窗口；由你手工登录，不读取、不填写密码，也不处理验证码。
- 430 正在运行时在其配置中打开登录窗口；430 不可用时使用工具专属 Chrome 配置。不会读取其他 Chrome 配置的数据。
- 登录成功后，每 4 秒的只读检测会取得 Cookie 并从断点自动继续；也可以点击“已登录，检查并继续”。手动暂停或停止后不会因登录成功擅自恢复。
- Cookie 只提取到进程内存，用于本机 HTTP Cookie 容器。若网站拒绝独立 HTTP 请求，回退到同一已登录浏览器的后台 Cookie 请求。不会将 Cookie 保存到 JSON、日志、网页或报表。浏览器自身正常保留登录状态。
- 所有任务共享一个受控查询队列。Grab 的表单会话有状态，不并发提交同一会话的搜索；相近 ICCID 分组读取，精确匹配，请求间至少间隔 1 秒。Excel 在独立进程生成，不阻塞网页进度响应。

## 状态口径

| 状态 | 依据 |
|---|---|
| 激活成功 | 明确的 Activation Date 字段有值 |
| 激活中 | Act. Request Date 有值，但 Activation Date 为空 |
| 未激活 | 两个字段均为空 |
| 不存在 | 当前账号中完整 ICCID 查询返回 0 条 |

网络错误、登录失败、身份不符或字段异常不会被记为不存在。激活成功不表示当前套餐仍有效。

## 保存位置与安全

- 程序：`/Users/zhangsan/Downloads/430/grab-query-tool`
- 本机断点与历史：`/Users/zhangsan/Downloads/430/opensms_artifacts/state/grab-query-tool/jobs`
- Excel：`/Users/zhangsan/Downloads/430/outputs/grab-query-tool`，沿用现有外接盘输出目录。外接盘离线时查询记录仍在本机，导出会明确失败；挂载后重试即可。
- 仅监听 `127.0.0.1:43881`；同源请求、会话 Cookie 与防跨站令牌保护写接口，不向局域网开放。
- 本工具不运行 OpenSMS 任务 A–F，不修改运营商，不提交激活、下卡通知或到期标记。
- 不要手工删除历史 JSON。备份时复制整个本机历史目录与 Excel 输出目录；Cookie 不包含在历史记录中。

## 运行与测试

使用现有 Codex 随附 Node 与依赖，不需要安装 npm 包。`node_modules` 是指向随附依赖的链接。

```sh
cd /Users/zhangsan/Downloads/430/grab-query-tool
/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node --test tests/*.test.mjs
```

浏览器端 UI 测试使用独立无头 Chrome 和模拟 Grab 数据；真实站点小样验证单独记录，不会通过测试让现有账号退出登录。
