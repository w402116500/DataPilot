# DataPilot

DataPilot 是一个单人、本地的数据分析 Agent Demo。当前版本包含：

- CSV/SQLite 上传、MySQL 连接、Schema 检查、预览和受控 Data Gateway 查询；
- Opening 协议选择、按需 Discovery、LangChain Tool + LangGraph 动态 Agent Runtime、SQL/Python/可选 DataLink 工具、SSE、取消和历史回放；
- Session 工作台、消息窗口、Session/Run 历史目录、Run 检查台和 Artifact 展示；
- DataLink 独立 REST/MCP 服务、按需建图、版本化语义检索和窄屏工作台；
- 无网络 Docker Python Sandbox、显式 `mask_fields`、SQL Audit 和答案事实校验。



## Requirements

- Python 3.12
- uv
- Node.js 20+
- npm

## 本地启动

先从 `.env.example` 创建 `.env`，并将 `SECRET_MASTER_KEY` 设置为至少 16 个字符的本地开发值。
推荐使用统一脚本启动三个服务（命令从仓库根目录执行）：

```powershell
uv sync
npm install
uv run alembic upgrade head
pwsh -File scripts/dev-services.ps1 -Action Start
```

脚本使用以下本地端口：API `8011`、Web `5175`、DataLink `8100`。状态与停止命令：

```powershell
pwsh -File scripts/dev-services.ps1 -Action Status
pwsh -File scripts/dev-services.ps1 -Action Stop
```

脚本启动后的健康检查地址为 `http://localhost:8011/health`，工作台地址为
`http://localhost:5175`。脚本会将 Web 的 `VITE_API_BASE_URL` 指向
`http://127.0.0.1:8011`。

如果不使用统一脚本，`.env` 中的 `API_PORT=8000` 和 Vite 默认端口 `5173` 仍可用于手工启动；
不要把手工启动端口与脚本端口混用。

## Validation

```powershell
uv run ruff check apps packages services tests
uv run ruff format --check apps packages services tests
uv run pytest tests -q

Set-Location services/datalink
uv run pytest -q
Set-Location ../..

npm run lint
npm run typecheck
npm run build
npm run test
```

配置 `DATALINK_LLM_MODEL`、`DATALINK_LLM_BASE_URL` 和 `DATALINK_LLM_API_KEY` 后，可以从
`services/datalink` 重跑真实模型验收：

```powershell
Set-Location services/datalink
uv run pytest -m live_model -q
Set-Location ../..
```
