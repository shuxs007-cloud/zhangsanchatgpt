# OpenSMS 文档处理接口

此服务把 OpenSMS 巡检中的“卡槽关联结果表生成与完整复核”拆成远程异步作业。
当前电脑只需上传本轮两份新鲜输入和实时服务器日期，远端完成处理后再下载结果。

## 安全边界

- 只处理 `export4day.xlsx` 与本轮 Google 关联表。
- 不登录 OpenSMS，不读取 Cookie 或 Admin-Token。
- 不修改运营商，不执行上下架，不执行客户绑定。
- **不支持任务B**。所有作业回执固定记录 `task_b_executed: false`。
- 只有完整复核通过的作业才开放结果下载。
- 默认必须使用 Bearer Token；建议仅通过局域网 VPN、Tailscale 或 HTTPS 暴露。

## 部署到另一台电脑

远端电脑需要 Python 3、Node.js，以及 Codex 文档运行时中的
`@oai/artifact-tool`。安装 Codex 桌面版后通常已具备后者。

```bash
unzip opensms_document_api_*.zip
cd opensms_document_api
shasum -a 256 -c PACKAGE_SHA256SUMS.txt
./install.sh
cp config.env.example .env
```

编辑 `.env`，至少修改：

```dotenv
OPENSMS_API_HOST=0.0.0.0
OPENSMS_API_PORT=8787
OPENSMS_DOC_API_TOKEN=一段足够长的随机字符串
OPENSMS_DOCUMENT_WORKERS=auto
```

生成 Token 可使用：

```bash
openssl rand -hex 32
```

启动服务：

```bash
./run_server.sh
```

健康检查不需要 Token：

```bash
curl http://127.0.0.1:8787/healthz
```

生产环境建议由 Tailscale/VPN 或反向代理提供网络隔离与 TLS。也可直接给
`run_server.sh` 传入 `--tls-cert` 和 `--tls-key`。

## 当前电脑提交并下载

部署包中包含 `opensms_document_api_client.py`。`server-date` 必须使用本轮从
`phoneConfig`/`day` 实时读取的后台服务器日期，不能使用本机日期代替。

```bash
export OPENSMS_DOC_API_URL=http://远端电脑IP:8787
export OPENSMS_DOC_API_TOKEN=与远端一致的Token

python3 opensms_document_api_client.py process \
  --export /Volumes/zs01/430/export4day.xlsx \
  --association /本轮新下载的/google_card_slots.xlsx \
  --server-date 2026-08-19 \
  --output-dir /本轮下载目录
```

该命令会：

1. 根据两份输入的 SHA-256 和服务器日期生成幂等键。
2. 上传并取得 `job_id`。
3. 轮询直到成功或失败。
4. 成功后只下载结果 Excel 与处理器完整复核回执，两者并行下载。
5. 对每个下载文件再次执行 SHA-256 复核。

网络中断后可继续查询和下载：

```bash
python3 opensms_document_api_client.py status <job_id>
python3 opensms_document_api_client.py wait <job_id>
python3 opensms_document_api_client.py download <job_id> --output-dir /本轮下载目录
```

## 远端优先与本地回退

`opensms_process_document.py` 是巡检任务的统一入口。它按顺序执行：

1. 从 `OPENSMS_DOC_API_URLS` / `OPENSMS_DOC_API_URL` 或
   `$HOME/Downloads/OPENSMS_LAN_API.md` 读取局域网地址。
2. 依次请求各地址的 `/healthz`。
3. 接口可用时上传、等待、下载并复核 SHA-256。
4. 接口不存在、连接失败或无法下载时，自动调用包内同版本本地处理器。
5. 远端已接受作业且明确返回处理失败时，不用本地结果掩盖该失败。

兼容原四参数命令：

```bash
./opensms_build_card_slot_report.sh \
  /path/export4day.xlsx \
  /path/google_card_slots.xlsx \
  /path/output \
  2026-08-19
```

也可直接调用：

```bash
python3 opensms_process_document.py \
  --export /path/export4day.xlsx \
  --association /path/google_card_slots.xlsx \
  --server-date 2026-08-19 \
  --output-dir /path/output
```

每次都会生成 `opensms_document_dispatch_*_receipt.json`，明确记录
`remote_api` 或 `local_fallback`、探测过程、回退原因、输入 SHA-256 和结果路径。
Token 不写入回执。

## API

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/healthz` | 健康检查 |
| `POST` | `/v1/jobs` | 创建异步处理作业 |
| `GET` | `/v1/jobs/{job_id}` | 查询状态与复核结果 |
| `GET` | `/v1/jobs/{job_id}/files` | 列出可下载文件 |
| `GET` | `/v1/jobs/{job_id}/files/result` | 下载结果 Excel |
| `GET` | `/v1/jobs/{job_id}/files/receipt` | 下载完整处理器回执 |
| `GET` | `/v1/jobs/{job_id}/files/api_receipt` | 下载接口处理摘要 |

创建作业使用 `multipart/form-data`：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `export4day` | 文件 | 是 | 本轮新导出的 `.xlsx` |
| `association` | 文件 | 是 | 本轮新下载的 Google `.xlsx` |
| `server_date` | 文本 | 是 | `YYYY-MM-DD` |

请求头：

```text
Authorization: Bearer <token>
Idempotency-Key: <同一次提交保持一致>
```

服务验证两份上传均为结构完整的 XLSX。相同幂等键与相同输入会返回原作业；
相同幂等键配不同输入会返回 `409`。

## 作业与交付目录

每个作业完全隔离：

```text
data/jobs/<job_id>/
  input/
  output/
  artifacts/snapshots/
  logs/
  job.json
```

远端不再重复复制两份输入，不生成预览、诊断副本、`FILES.tsv`、
`SHA256SUMS.txt` 或整包 ZIP。接口只开放结果 Excel、完整处理器回执和接口摘要。
巡检最终目录所需的源表、Google 表、完整报告与最终 SHA-256 清单仍由当前电脑统一归档。

`OPENSMS_DOCUMENT_WORKERS=auto` 会读取服务器全部逻辑 CPU，让相互隔离的文档作业
同时运行。单个作业内的“生成主表、补充工作表、完整复核”存在数据依赖，仍按顺序
执行；结果与回执下载、文件哈希等独立操作会并发执行。
服务重启时，排队中的作业会继续执行；中断于处理中的作业会明确标记失败，之后可
使用新的幂等键重新提交。作业状态中的 `timings_seconds` 会列出文档流水线、最终
哈希、总耗时以及流水线内部各阶段耗时。

## 验证

```bash
python3 -m py_compile \
  server.py processor.py opensms_document_api_client.py opensms_process_document.py
```

服务固定使用优化 Python 复核器，逐号码独立重算连续统计、3日/4日指标、过期表、
总回复量为0表、已激活未上架、机器汇总、运营商预警及公式错误等全部 22 项检查。
旧 Node 复核器和预览生成分支已删除，避免重复处理。
