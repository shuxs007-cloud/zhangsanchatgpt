# 代码导入记录

来源：用户提供的 `OpenSMS_全部代码_20261017T150639+0800.zip`。

保留 `430/` 与 `shared/` 的目录结构。原压缩包含 755 个文件，其中
`MANIFEST.json` 列出的 752 个文件均已通过大小和 SHA-256 校验。
导入的源码保持原样；新增根目录 `.gitignore` 防止提交本地依赖、凭据和运行数据。

以下 155 个非源码或临时文件未纳入 Git，原文件仍保留在用户提供的压缩包中：

- 148 个 `.BC.T_*` 编辑器临时副本。
- `430/inputs/selected_phones_20260926.txt` 手机号输入清单。
- `shared/cloakbrowser_free_test/` 下的 3 张浏览器截图。
- 同目录下的 `receipt.json`、`binding_receipt.json`、`login_browser_status.json` 旧运行回执。

根目录 `MANIFEST.json` 与 `EXCLUDED.json` 是原导出包的来源记录，
并非本次 Git 提交的完整文件清单；上述额外排除项以本说明为准。

导入前通过 Python、JavaScript、Shell 语法检查和 JSON 解析检查。
没有执行巡检、登录、后台写入或定时任务。源码仍依赖原有 macOS 绝对路径、
浏览器、挂载磁盘及部分未包含的业务模板；本次提交不代表已经完成云端运行适配。
