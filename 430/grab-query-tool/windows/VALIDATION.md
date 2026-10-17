# Windows 发行包验收范围

日期：2026-09-26

## 已完成（macOS 开发机）

- 原 Mac 查询逻辑和跨平台路径合计 26 项自动测试通过。
- 独立发行包自身的公开依赖环境中，26 项测试通过；包括界面输入、暂停/继续/停止、历史恢复、实时重查、登录失效模拟、下载 Excel 和 HTTP 安全检查。
- 发行包 selfcheck：四种状态、原始重复清单、去 F、历史保存、独立 Excel 导出后回读通过。
- 三个 Excel 工作表视觉检查通过；ICCID 作为完整文本保存；公式错误扫描为零。验收号码为人工模拟数据，不是实时 Grab 查询。
- 后台启动、重复启动复用同一服务、中文及空格数据路径、保存后退出均通过开发机测试。
- Windows 路径选择、Edge/Chrome 查找规则和打开浏览器命令已做单元测试。
- Node.exe 来自 Node.js 官方 Windows x64 ZIP，下载档案 SHA256 与官方 SHASUMS256.txt 一致。
- npm 依赖锁定版本。替换 ExcelJS 引入的旧版 uuid 后，发行依赖 npm audit 为零已知漏洞；这不等于保证不存在所有安全问题。
- 程序包不包含历史查询、用户 ICCID 清单、Cookie、浏览器配置、Mac 绝对数据路径或 Codex 私有依赖。

## 尚未完成

目前没有 Windows 实机，因此不能声称原生 Windows 双击启动、Edge/Chrome 手工登录及 Grab 实时查询已经通过实机验收。请在 Windows 11 x64 电脑解压后先运行 Check.cmd，再运行 Start.cmd，以少量 ICCID 试查。

无 Windows 7/8、ARM64 或受企业策略限制浏览器的兼容性承诺。未为本程序签发 Windows 代码签名证书；不要关闭安全防护来绕过组织策略。
