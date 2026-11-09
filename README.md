# PyCode

> 面向 Git Repository 的 AI 代码分析 Agent：把确定版本的代码转换为可检索索引与 Code Graph，再基于证据完成仓库问答、影响分析和多步骤 Agent Task。

PyCode 用于理解一个现有代码库，而不是把整个仓库直接塞进一次 LLM 请求。系统从公共 HTTPS Git URL 获取 Repository，将分析固定到明确的 commit，构建 Python 代码索引和 Code Graph，并通过 React Web UI 提供项目管理、分析、Trace、历史记录与图谱查询。

当前版本包含完整的 Web/API/Worker 链路，也保留可独立使用的 Python Core 和 CLI。推荐使用 Docker Compose 启动完整系统。

## 项目简介

代码库分析经常遇到两个问题：上下文太大，以及结论和代码版本无法对应。PyCode 在 LLM 前增加了一层由项目自己管理的分析基础设施：

- **Snapshot** 将一次索引和分析绑定到确定的 Git commit。
- **Index** 从 Python AST 中提取文件、class、function、method 等结构。
- **Code Graph** 记录包含、import、call 等关系。
- **Retrieval 与 Evidence** 只选择当前问题需要的上下文，并在回答中保留证据。
- **Agent Runtime** 负责规划、工具调用、多轮决策、项目级 Memory 与 Trace。

Snapshot 保存 commit identity、索引摘要、Code Graph 与 Chunk Artifact；Embedding 产物独立发布并校验。执行分析前，系统会将 Repository checkout 到对应 commit，并校验该 Snapshot 的 Artifact。这样历史结果可以回到产生它的代码版本；这不是为每次任务创建独立 Git worktree。

## 🚀 核心功能

| 功能 | 当前实现 |
| --- | --- |
| Repository 管理 | 登记公共 HTTPS Git URL 与可选 branch；在受控 Workspace 中 clone/fetch，并解析 exact commit。 |
| Index 与 Code Graph | 分析 Python 文件，构建 file/class/function/method 节点以及 import、call 等关系；Artifact 与 Snapshot 绑定。 |
| 统一问答 | Web 使用单一提问入口，异步返回 Run/Task 标识；系统按问题选择 Fast 或 Agent，并保存结果和证据。 |
| Impact Analysis | 文件影响分析作为内部检索意图保留；旧同步 `/impact` 接口仍接受相对 `file_path`。 |
| Agent 执行 | 在既有 Celery Worker 中规划、取证、观察后继续调用工具；证据不足的 Fast 问题可升级一次并复用首轮 Evidence Pack。 |
| Trace 与历史 | 通过认证 SSE 展示实际发生的 LLM、工具、Context 和 Timeline；Fast 不伪造 Plan/工具事件，终态结果与 Trace 可回放。历史保留统一问答及旧入口记录。 |

Web 图谱接口和页面按符号或精确文件路径查询局部关系图，使用 Cytoscape 展示，避免一次向浏览器传输完整图谱。

## 🧠 Agent 工作方式

Web 的统一问答提交到 `POST /api/v1/projects/{project_id}/questions`，返回 HTTP 202 和 `run_id`、`task_id`、`status`。用户无需选择执行模式：服务端规则路由决定 Fast 或 Agent，Worker 使用提交时固定的已授权 ready Snapshot。Project 后来切换当前 Snapshot，不会改写已接受任务的绑定；工具参数不能替换可信 Project/Snapshot/Commit。

Fast 复用统一 Retriever 和 Evidence Pack，证据充分时直接回答；预检发现证据不足时可升级一次到 Agent，并复用首轮 Pack。Agent 使用原有多轮 Runtime，过程如下：

```text
User Task
    │
    ▼
固定已授权 ready Snapshot
    │
    ▼
┌──────────────────────────── Agent Harness ────────────────────────────┐
│ Snapshot Boundary · Planner · Context/Memory · Tool Registry · Policy │
│ Hooks · Trace/Todo · Evidence · Loop/Stop Control                     │
│                                                                       │
│   ┌──────────────────────── Agent Loop ───────────────────────────┐    │
│   │ Plan/State → Context → LLM Decision → Tool Call → Observation │    │
│   │      ▲                                              │         │    │
│   │      └────────── continue / next action ─────────────┘         │    │
│   └─────────────────────────────┬─────────────────────────────────┘    │
│                                 │ complete / failed / stop             │
└─────────────────────────────────┼──────────────────────────────────────┘
                                  ▼
                         Final Result + Trace
```

Agent 可按计划使用已注册的只读分析工具，例如：

- `retrieve_context`：根据问题和意图检索相关代码上下文。
- `query_graph`：查询定义、imports、imported-by、calls、callers/callees 或入口候选，并保留静态解析边界。
- `search_code`：在项目文件中搜索文本或正则表达式。
- `read_file`：读取 Repository 内的 UTF-8 文本文件及指定行范围。

Core 还包含 Git diff、测试运行和内部状态工具，但是否可用由执行策略和任务授权控制。Web Agent 默认执行分析，不把它描述为能够任意修改代码的自主软件工程师。

Keyword、Vector、Graph 通过同一 Retriever 选择证据；Vector 仅查询匹配当前绑定及模型配置的已发布产物，Query 不重建索引。精确命中和已有结构化意图保留原策略，静态关系不代表运行时调用事实。

Agent 的轮数、工具、模型、查询 Embedding 和时长限制共用一次执行预算；完整模型输入另按保守 UTF-8 字节上界估算。进入 Prompt 的证据/工具输出以完整单元选择，超预算会明确舍弃并记录原因，不能把不完整内容伪称为整个定义。估算不是服务端计费 Token，累计输入也不是单次请求上限。

Memory 位于当前 Project/Repository Workspace 的 `.pclens/memory`。用户明确声明的项目背景/偏好、代码派生记录和未验证模型推断具有不同来源与适用范围；旧 Snapshot 的代码事实不能直接当作当前事实，Memory 不能替代当前源码。生命周期包含去重、冲突、失效及撤回，自动提取不能复活适用的撤回记录。它不是跨 Project 的用户画像或 Chat Conversation Memory，切换 Snapshot/归档不会自动清空它；旧未知来源也不会自动升级成可信事实。

兼容入口继续保留，语义分别为：

| API | 执行方式 |
| --- | --- |
| `POST /api/v1/projects/{id}/questions` | 统一异步提交，服务端路由与证据不足升级。 |
| `POST /api/v1/projects/{id}/ask`、`POST /api/v1/projects/{id}/impact` | 原同步分析接口，不自动变成统一异步任务。 |
| `POST /api/v1/agent-runs` | 原异步 Agent 入口，继续执行 Agent Runtime。 |

这些接口、结果查询与 Trace/SSE 都遵守 Project owner 授权。软归档阻止新工作，已接受任务仍按原绑定及恢复规则执行。

## 🏗️ 系统架构

```text
Browser
   │
   ▼
React + TypeScript ──build──▶ Nginx :8080
                                  │ /api + SSE
                                  ▼
                             FastAPI :8000
                              │    │
                  business    │    └────────▶ Redis
                    state     │                 │ queue / live trace
                              ▼                 ▼
                         PostgreSQL       Celery Worker
                                              │
                                              ▼
                                      Shared Workspace
                                              │
                         ┌────────────────────┼───────────────────┐
                         ▼                    ▼                   ▼
                    Git Repository       Snapshot/Artifact   PyCode Agent Core
                                                                  │
                                                                  ▼
                                                                 LLM

Celery Beat ──▶ queued/stale task reconciliation ──▶ Worker
```

- FastAPI 负责 Project、Snapshot、统一异步问答、兼容同步分析、Agent Run、Trace 和任务状态 API。
- PostgreSQL 是 Project、Snapshot、Agent Run、Background Task 与终态 Trace 的持久化来源。
- Redis 用作 Celery broker、项目操作协调和有界实时 Trace stream。
- Backend 与 Worker 共享 Workspace；Worker 执行索引、独立语义索引任务及统一问答的 Fast/Agent 路径。
- Celery Beat 定期触发 queued publish 与 stale execution 的恢复协调。

## 🖥️ Web 功能

React Web UI 使用中文界面，主要包含：

| 页面 | 能力 |
| --- | --- |
| Projects | 创建和浏览 Project，查看 Repository、branch、状态与当前 Snapshot。 |
| Project Workspace | 构建索引；通过单一提问框提交问题，查看执行状态、Answer 和可折叠 Evidence。 |
| Agent Trace | 查看实时/历史 Timeline、Plan、LLM Activities、Tool Calls、Context 和最终 Result。 |
| Analysis History | 按时间查看统一问答与兼容入口的成功结果；历史为空的结构化元数据不会从旧文本中伪造证据。 |
| Code Graph | 按符号或文件查询局部图谱，筛选节点类型并查看节点详情。 |

Analysis History 中的每一项都是独立分析记录，不是多轮对话线程。

## 📦 快速开始

### Docker Compose（推荐）

需要 Docker Engine（或 Docker Desktop）及 Docker Compose plugin。

当前后端的配置预检、就绪探针、初始化与升级/回滚步骤见[后端部署说明](backend/DEPLOYMENT.md)。请先检出要部署的目标版本，下面的复制操作不要覆盖已有 `.env`。

```bash
git clone https://github.com/yesterre/PyCode.git
cd PyCode
# 先检出要部署的固定版本/提交，再继续配置与构建。
cp .env.example .env
```

PowerShell 中复制配置文件：

```powershell
Copy-Item .env.example .env
```

编辑 `.env`，至少配置数据库密码和随机 JWT 密钥；LLM 相关变量只在启用问答/Agent 时需要。不要提交真实密码或 API Key。

```dotenv
POSTGRES_USER=pycode
POSTGRES_PASSWORD=replace-with-a-url-safe-password
POSTGRES_DB=pycode
PYCODE_JWT_SECRET=replace-with-a-generated-secret-of-at-least-32-bytes

OPENAI_API_KEY=replace-with-your-api-key
OPENAI_MODEL=your-model
OPENAI_BASE_URL=
OPENAI_API_TYPE=responses
```

`POSTGRES_PASSWORD` 会同时进入 PostgreSQL 初始化配置和容器内的 SQLAlchemy URL，请使用 URL-safe 值。使用不支持 Responses API 的 OpenAI-compatible provider 时，将 `OPENAI_API_TYPE` 改为 `chat`，并填写相应的 `OPENAI_BASE_URL`。

启动完整系统：

```bash
docker compose up --build -d
```

启动后访问：

- Web UI：<http://127.0.0.1:8080>
- Backend Swagger：<http://127.0.0.1:8000/docs>
- Backend health：<http://127.0.0.1:8000/health>
- Backend readiness：<http://127.0.0.1:8000/ready>（配置、迁移版本和 Redis 就绪）

Compose 会先等待 PostgreSQL healthy，再运行 `alembic upgrade head`；Migration 成功后启动 Backend、Worker、Beat 和 Frontend。PostgreSQL、Redis 与 Workspace 分别使用 named volume。

常用操作：

```bash
# 启动已有环境
docker compose up -d

# 首次启动或代码变更后重新构建
docker compose up --build -d

# 查看服务状态与日志
docker compose ps
docker compose logs -f

# 暂停与恢复
docker compose stop
docker compose start

# 关闭容器，保留 named volume 数据
docker compose down
```

`docker compose down -v` 会删除 Docker 环境中的 PostgreSQL、Redis 和 Workspace 持久化数据，不应作为日常停止命令。

### 本地开发

本地开发建议使用 Python 3.11+、Git、PostgreSQL、Redis 和 Node.js 24（与 CI/Docker 一致）。先启动本机 PostgreSQL 与 Redis，然后在项目根目录执行：

```bash
python -m venv .venv

# Linux / macOS
source .venv/bin/activate

# Windows PowerShell
# .\.venv\Scripts\Activate.ps1

python -m pip install -r requirements.txt
cp .env.example .env
python -m alembic upgrade head
```

Windows PowerShell 可将复制命令替换为 `Copy-Item .env.example .env`。本地 `.env` 中的 `DATABASE_URL`、`REDIS_URL` 应指向 `localhost`；如需运行问答或 Agent，再填写 `OPENAI_*` 配置。

在独立终端启动 Backend、Worker 和 Beat：

```bash
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
python -m celery -A backend.app.worker:app worker --loglevel=INFO
python -m celery -A backend.app.worker:app beat --loglevel=INFO
```

Windows 上的 Celery Worker 使用 `solo` pool：

```powershell
python -m celery -A backend.app.worker:app worker --loglevel=INFO --pool=solo
```

启动 Frontend：

```bash
cd frontend
npm ci
npm run dev
```

开发入口为 <http://127.0.0.1:5173>。Vite 会把 `/api` 代理到 `http://127.0.0.1:8000`。

## ⚙️ 配置

模型配置来自 `.env`：

| 变量 | 说明 |
| --- | --- |
| `OPENAI_API_KEY` | 模型服务凭证；Repository indexing 不需要，问答和 Agent 需要。 |
| `OPENAI_MODEL` | 默认模型名称。 |
| `OPENAI_BASE_URL` | OpenAI-compatible gateway 地址；使用官方默认地址时留空。 |
| `OPENAI_API_TYPE` | `responses` 或 `chat`。 |

Docker Compose 还要求 `POSTGRES_USER`、`POSTGRES_PASSWORD` 和 `POSTGRES_DB`。容器内的 `DATABASE_URL`、`REDIS_URL` 与 `PYCODE_WORKSPACE_ROOT` 由 Compose 覆盖为 service DNS 和共享 Workspace；本地开发则直接使用 `.env` 中的值。

Git timeout、Repository retry、LLM/Redis timeout、项目锁、SSE 上限、任务 lease 和 recovery interval 等高级选项均以 `PYCODE_*` 变量提供，默认值和说明见 [`.env.example`](.env.example)。

Embedding 与 LLM 独立配置，代码默认关闭。启用 `PYCODE_EMBEDDING_ENABLED=true` 后，基础索引成功可触发既有 Worker 中的语义索引任务，可能向所配 Provider 发送代码片段并产生费用；需要单独设置 `PYCODE_EMBEDDING_*`。`PYCODE_RETRIEVAL_MODE` 未设置时代码默认 `keyword`，模板显式选择 `hybrid`；缺少或损坏的向量、配置错误、Provider 失败会带诊断降级到可用的 Keyword/静态 Graph，不撤销基础 Snapshot 的 ready 状态，也不在查询时重建。模型/维度/内容哈希与绑定必须匹配，不能因 Commit 相同就跨 Project/Snapshot 复用产物。

部署、Artifact 版本兼容及预算/Memory 的运行边界见[后端部署说明](backend/DEPLOYMENT.md#agent-检索与现有数据)。

修改模型配置后，重新创建读取该配置的服务：

```bash
docker compose up -d --force-recreate backend worker
```

Nginx 配置在启动时解析 `backend` 地址。如果重建 Backend 后 Web UI 出现 `502`，刷新其 upstream：

```bash
docker compose restart frontend
```

## 后端用户认证

后端提供 `POST /api/v1/auth/register`、`POST /api/v1/auth/login` 和
`GET /api/v1/auth/me`。注册和登录使用 JSON 字段 `account_name`、`password`；
注册密码长度为 6～128 个字符，不要求大小写、数字或特殊符号组合，不自动去除首尾空格或转换大小写。

按 [`.env.example`](.env.example) 配置 `PYCODE_JWT_SECRET`，使用至少 32 字节的随机密钥。
`PYCODE_JWT_ALGORITHM` 仅支持 `HS256`；`PYCODE_ACCESS_TOKEN_TTL_SECONDS` 默认 900 秒，
允许范围为 1～3600 秒。直接运行 API 时缺少有效密钥会使认证入口和 `/ready` 返回 503，`/health` 仍表示进程存活；容器入口会在启动 API 前拒绝无效配置。Worker 不依赖浏览器 JWT。

可通过 Swagger 完成注册和登录，将返回的 `access_token` 填入 Authorize，
再调用 `/api/v1/auth/me`；API 客户端使用 `Authorization: Bearer <access_token>` Header。
Project 列表、创建、详情、索引提交、问答、影响分析、分析历史、Snapshot 和 Graph
接口均需要此 Header，仅访问当前用户拥有的项目及其资源。
旧的无归属项目需通过受控迁移明确归属。AgentRun 提交、Task/AgentRun 查询及 Trace
历史接口及 SSE `/api/v1/agent-runs/{run_id}/events` 也需要此 Header，并按关联项目校验归属。
SSE 使用支持自定义 Header 的客户端或 `fetch` 流，重连时发送 `Last-Event-ID`；
原生 `EventSource` 无法附加 Bearer Header；当前 React 前端已使用认证 `fetch` 流并携带重放序号。不要将 Token 放入 URL。
`POST /api/v1/tasks/health` 不再公开，health 探针任务仅由内部 Service/Worker 调用。

## 🧰 技术栈

| 层 | 技术 |
| --- | --- |
| Frontend | React、TypeScript、Vite、TanStack Query、Cytoscape、Tailwind CSS、Nginx |
| Backend | FastAPI、SQLAlchemy、Alembic、PostgreSQL |
| Async / Realtime | Redis、Celery、SSE |
| Agent Core | Python AST、LLM、Tool Calling、Memory、Code Graph、Rich CLI |
| Infrastructure | Docker、Docker Compose、GitHub Actions |
| Testing | pytest、Vitest、Testing Library、Playwright |

## 📁 项目结构

```text
PyCode/
├── backend/           # FastAPI、DB、Repository lifecycle、Worker integration
├── frontend/          # React + TypeScript Web UI
├── pycode/            # Index、Code Graph、retrieval、Agent Core 与 tools
├── tests/             # Core、Backend、浏览器测试与 fixtures
│   └── fixtures/
│       └── demo_project/
├── compose.yaml       # 完整部署编排
├── .env.example       # 配置模板
└── README.md
```

## 🔎 典型使用流程

1. 在 Projects 页面创建 Project，填写公共 HTTPS Repository URL 和可选 branch。
2. 在 Project Workspace 提交 Index 任务。Worker clone/fetch Repository、解析 commit，并发布 Snapshot、Index、Graph 和 Chunk Artifact。
3. 等待 Project 基础索引 `ready`；语义索引状态独立，可从 Snapshot 详情查看，不影响基础证据可用性。
4. 在统一提问框输入问题；可在正文描述目标文件及影响分析需求，由系统选择路径。
5. 查看任务状态、实际 Trace、最终 Answer/Evidence；刷新后仍可查询结果和回放。
6. 从 Analysis History 回看成功结果，或在 Code Graph 页面查询符号和文件关系。

## 🧪 测试与 CI

Backend 与 Core：

```bash
python -m pytest --basetemp=.pytest_tmp -o cache_dir=.pytest_tmp/.pytest_cache --tb=short
```

Frontend：

```bash
cd frontend
npm ci
npm test
npm run typecheck
npm run build
```

受控浏览器 E2E：

```bash
cd frontend
npm run e2e
```

Playwright 流程使用受控 FastAPI test server、Vite 和 headless Chromium，不调用付费 LLM，也不要求 PostgreSQL 或 Redis。首次运行前需要安装 Playwright Chromium。

GitHub Actions 中的 `backend`、`frontend` 和 `docker` jobs 分别覆盖 Python 配置/迁移/授权与隔离测试、Frontend 测试/类型检查/构建，以及隔离 Compose 的构建、启动、持久化和健康故障恢复验证。

## ⚠️ 当前限制

- Index 与 Code Graph 当前主要面向 Python Repository；调用关系来自静态 AST，无法完整覆盖反射、运行时注入和所有动态分派，unknown/ambiguous 不应被解释为确定调用。
- Git 接入当前面向公共 HTTPS URL，默认允许 `github.com`、`gitlab.com` 和 `gitee.com`；不支持私有 SSH Repository。为控制边界，符号链接、submodule 和 Repository 自带的 `.pclens` 会被拒绝。
- Repository ingestion 不安装依赖，也不运行目标仓库代码；Impact Analysis 是以 `file_path` 为入口的静态分析辅助结果，仍需结合项目语义复核。
- 问答与 Agent 依赖 OpenAI 或 OpenAI-compatible API，执行时间和结果受 provider quota、rate limit、模型能力与网络状态影响。
- Analysis History 是按 Project 保存的独立成功分析记录，不提供多轮 Conversation 语义。
- 旧 ready Snapshot 的 Graph 能力以已发布版本为准；软件升级或对同一 Commit 再次索引不保证原地升级 Graph。兼容读取不是静默重建或迁移，详见部署说明。
- Hybrid 当前对显式符号 `target` 存在被视作文件路径的边界；显式目标优先用项目相对文件路径，符号名可写在问题正文中。
- Fake/Stub 评估验证工程链路，不证明真实回答准确率、Memory 语义收益或线上费用；没有真实回答与人工复核时，质量指标仍未验证。保守 Token 估算、两个本项目规模的测量也不能外推计费或超大仓库表现。
- Python 依赖使用版本范围而非完整 lock，Backend 源码构建不是 byte-for-byte reproducible；Frontend 使用 `package-lock.json` 和 `npm ci`。

---

**Version:** v3.0 开发分支（综合验收待完成） · **Author:** Yesterre
