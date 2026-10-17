# Grab ICCID 查询工具：Windows 版

## 三步启动

1. 将整个 ZIP 解压到一个普通本地文件夹，例如 `D:\GrabQueryTool`。不要在压缩包内直接双击，不要放到需要管理员权限的 Program Files。
2. 双击 `Start.cmd`。程序会打开 `http://127.0.0.1:43881`，无需安装 Node、npm、Codex 或 Excel，不需要管理员权限。
3. 粘贴 ICCID，点击“开始新查询”。首次需要时会弹出独立 Edge / Chrome 窗口，请手工登录 Grab；登录成功后继续查询。

面向 Windows 11 64 位 Intel/AMD（x64）。本包没有在真实 Windows 机器上完成验收；ARM64、Windows 7/8、精简系统不在本次验证范围。电脑需已有 Microsoft Edge 或 Google Chrome，且能联网访问 Grab。

若浏览器未自动打开，手工访问上述本机地址。工具只监听本机，不需要开放防火墙入站端口。若企业策略禁止浏览器自动化，应联系管理员确认，不能通过关闭安全软件解决。

## 查询、导出与历史

- 支持换行、空格、逗号、分号分隔 ICCID，自动去掉末尾一个 F/f。按去 F 后的 ICCID 去重，保留原始清单和重复次数。
- 每个新任务实时联网查询，不用历史结果充当新结果；继续只处理当前任务尚未完成的项。
- 页面实时显示已查、剩余、结果；可暂停、继续或停止。停止不能继续，但已完成部分仍保留。
- 完成后点击“导出 Excel”，包含查询结果、汇总说明、原始清单。ICCID 以文本保存，完整位数不丢失；可筛选和冻结表头。
- 暂停、停止或失败的任务若已有结果，可导出明确标注的部分报告；未查询不等于不存在。
- 历史列表记录开始/结束时间、数量、结果和下载。关闭程序或重启电脑后仍保存；恢复任务需手工点继续。
- 实时查询状态为：激活成功、激活中、未激活、不存在。网络或登录错误不会写成不存在；不存在仅表示当前账户完整 ICCID 搜索返回零条。
- 本包不包含 Mac 上的历史数据，不导入其他人的登录信息；Windows 历史从空列表开始。

## 登录、安全与效率

第一次未登录或 Cookie 失效时弹出独立登录浏览器。你手工输入密码/验证码，程序不读取或填写这些内容。登录后约每 4 秒检查并继续断点；手动暂停/停止后不自动恢复。

Cookie 提取到内存，用于本机请求，不放入报告、查询 JSON 或日志。浏览器自身会保存登录状态，因此不要向别人发送完整数据目录。查询期间不要关闭专用登录窗口。

Grab 同一会话的查询表单有状态：后台请求串行，间隔至少 1 秒，自动批量检索并精确匹配；Excel 在独立进程生成，页面仍响应。程序不会调用 AI，也不会修改运营商、激活 SIM、写下卡通知或到期标记。

## 保存位置与退出

默认保存在当前 Windows 用户目录，与解压文件夹分离：

- 查询历史和断点：`%LOCALAPPDATA%\GrabQueryTool\jobs`
- Excel：`%LOCALAPPDATA%\GrabQueryTool\reports`
- 日志：`%LOCALAPPDATA%\GrabQueryTool\server.log`
- 独立登录浏览器配置：`%LOCALAPPDATA%\GrabQueryTool\login-browser`（敏感，不要分享）

双击 `OpenData.cmd` 打开目录。双击 `Stop.cmd` 可保存断点并退出后台服务；当前请求可能需要等待约 60 秒。关闭网页本身不会关闭服务。停止服务会关闭工具自己创建的登录浏览器，不会结束其他 Edge/Chrome/比特浏览器。

双击 `Check.cmd` 执行离线自检：只使用临时模拟号码，不登录网站，不读取或改写正式历史，检查结果会显示在终端窗口。

更新版本前先 Stop，然后解压新版本再 Start。查询数据仍留在上述用户目录。备份查询结果时复制 jobs 与 reports，不要把 login-browser 或 server.log 发给他人。

## 可选设置与比特浏览器

一般无需设置。要指定浏览器、修改端口或使用某个比特浏览器配置，可将 `config.example.json` 复制为 `config.json` 再修改；Windows 路径用双反斜杠或正斜杠。

- `browserPath`：完整 Edge/Chrome 可执行文件路径；留空自动查找。
- `bitBrowserPortFile`：你明确选择的配置目录里的 `DevToolsActivePort` 文件；留空只用独立浏览器，不扫描或读取其他浏览器配置。该比特浏览器需已运行并开启本地调试连接。
- `port`：本地网页端口，默认 43881。被其他程序占用时可换为 43882，再次 Start。
- `dataDir` / `reportsDir`：绝对路径；留空使用用户默认目录。自定义后 OpenData.cmd 仍打开默认目录，请按配置找文件。

不要为了运行程序关闭杀毒软件或系统安全策略。若被拦截，先检查压缩包来源及 SHA256，按组织的软件审批方式处理。

## 测试边界与依赖

源代码、全部查询逻辑、浏览器 UI、断点/历史、Cookie 失效流程（模拟）和 Excel 导出已在开发机测试；真实 Windows 双击启动、系统浏览器登录仍需在 Windows 电脑上验收。README 旁的 VALIDATION.md 记录实际测试范围。

本包内置官方 Node.js v24.21.0 x64、Playwright Core 1.62.1 和 ExcelJS 4.4.0，以及其依赖及许可证。Codex 专用表格库不能在独立 Windows 安装，因此独立版采用开源 ExcelJS。查询结果表结构和状态口径保持一致。

公开资料：

- Node 官方发行版及校验值：https://nodejs.org/dist/v24.21.0/
- 浏览器支持：https://playwright.dev/docs/browsers
- Excel 导出库：https://github.com/exceljs/exceljs

没有自动更新、遥测、局域网开放或云端保存查询清单功能。
