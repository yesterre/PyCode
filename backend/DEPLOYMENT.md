# 后端部署、健康检查与升级回滚

以下命令从已经检出的目标源码版本根目录执行。需要 Docker Engine/Compose plugin；本地开发使用 Python 3.12、PostgreSQL、Redis。Python 依赖仍使用现有范围，镜像源码构建不保证字节级一致。

## 首次配置

复制 `.env.example` 为 `.env`，不要覆盖已有配置。首次创建 PostgreSQL volume 前，填写实际的 `POSTGRES_PASSWORD` 与 `PYCODE_JWT_SECRET`：

```bash
python -c "import secrets; print(secrets.token_hex(24))"
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

第一个值可用作 URL-safe PostgreSQL 密码，第二个用于 JWT。保存在本地安全配置中，不提交、打印到 CI 日志或放进 `VITE_*`。JWT 仅支持 HS256，密钥至少 32 字节，access token 默认 900 秒、允许 1～3600 秒。

- `PYCODE_BACKEND_IMAGE` / `PYCODE_FRONTEND_IMAGE`：本地构建可用模板中的 `:local`；实际升级使用新的不可变版本标签，保留旧镜像与同版本 Compose 文件，不覆盖唯一的回滚镜像。
- `PYCODE_API_PORT` / `PYCODE_WEB_PORT` 默认 8000/8080，保持绑定 `127.0.0.1`。PostgreSQL、Redis 没有宿主端口；需对外服务时使用受控入口，不把内部数据库直接暴露。
- `DATABASE_URL` 本地使用 `postgresql+psycopg://...`；Compose 强制使用服务名 `postgres`，同时覆盖 Redis 为内部 `redis:6379`、Workspace 为 named volume。PostgreSQL 密码需 URL-safe，已有 volume 不会因修改环境变量自动改密码。
- `PYCODE_METRICS_TOKEN` 可选，空值禁用 `/metrics`；启用要求 32～256 ASCII 且无空白，使用独立 `Authorization: Bearer` 凭据。HTTP 指标属于 API 进程，Task gauge 来自数据库；没有启用历史缓存。
- 限流默认 index=10、agent=20、analysis=30，每 60 秒一个窗口；每用户 active Task cap=4。Redis 不可用时新工作 fail closed，幂等重放保持原行为。
- Lease 默认 420 秒，heartbeat 30 秒，project lock TTL=300 秒/renew=60 秒，stale scanner 30 秒，broker visibility=21600 秒。预检要求 renew < lock TTL、lease > lock TTL + max(heartbeat, scanner)、visibility > lease，不要独立缩短这些值。
- LLM key 可留空，元数据、认证、索引和健康检查不需要调用模型。问答/Agent 需另配有效 provider。健康检查不会请求 Git 或 LLM。

如需独立配置文件：同时设置 `PYCODE_ENV_FILE` 和 Compose 的 `--env-file`。前者决定容器 env_file，后者决定 Compose 插值；默认均为仓库 `.env`。不要把展开后的 `docker compose config` 输出复制到日志，使用 `config --quiet`。

## 初始化与启动

```bash
docker compose config --quiet
docker compose build backend frontend
# 无外部依赖 I/O 的角色预检，先发现空密钥、占位密码或无效参数。
docker compose run --rm --no-deps --entrypoint python backend -m backend.check_config --role api
docker compose up -d --wait
docker compose ps
```

保留原顺序：PostgreSQL healthy → `migrate` 完成 → Backend/Worker/Beat，Redis 同时必须 healthy；Frontend 等 Backend healthy。迁移失败时不会绕过条件启动应用。首次空库初始化和既有库升级均使用现有 `alembic upgrade head`，不重建/清空生产数据库。

Backend 镜像入口在执行原 uvicorn/celery/alembic 命令前预检，再通过 `exec` 替换进程，保留 PID 1/信号语义。API 检查 JWT、限流与观测配置；Worker 不依赖浏览器 JWT，继续按可信 Task ID 和持久化归属处理已接受任务；Migration 只要求数据库配置。预检只输出配置组错误，不输出环境值。

```bash
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:8000/ready
```

`/health` 保持 `{"status":"ok"}` 的进程存活含义，不访问依赖。`/ready` 无需用户登录：有效认证配置、数据库当前 Alembic heads、Redis PING 均通过才返回 200，否则返回 503 及布尔 checks，不含 URL、密码或异常。它和 `/metrics` 均不占用业务 OpenAPI 路径清单。

数据库探针使用独立 NullPool、连接超时 2 秒、仅该连接的 statement_timeout=1 秒和只读事务；Redis 探针 connect/read 各 1 秒、不重试。不会等待业务池的 30 秒 checkout，不修改业务事务超时。每个 API 进程最多一个探针执行，重叠请求保守返回 503；DNS 等系统解析不构成硬性总时限保证。Compose 使用 10 秒健康检查窗口。

没有默认管理员或旧项目自动归属。通过 JSON 注册/登录取得 JWT，受控旧项目认领沿用 `python -m backend.manage_project_owner`，先 dry-run、确认后 `--apply`；不要批量交给第一个注册用户。

## 本地启动

```bash
python -m pip install -r requirements.txt
python -m backend.check_config --role migration
python -m alembic upgrade head
python -m backend.check_config --role api --exec -- python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

Worker、Beat 使用相同配置与 Workspace；Windows 的现有 solo 模式仍可用：

```bash
python -m backend.check_config --role worker --exec -- python -m celery -A backend.app.worker:app worker --loglevel=INFO --pool=solo
python -m backend.check_config --role worker --exec -- python -m celery -A backend.app.worker:app beat --loglevel=INFO
```

直接启动原 uvicorn 命令仍兼容：缺少有效 JWT 时认证入口为 503、`/ready` 为 503，`/health` 仍为 200；推荐通过预检命令启动以提前发现问题。

## 升级与回滚

1. 记录当前镜像的不可变标签/ID、Compose 文件和安全配置；备份 PostgreSQL 与 Workspace，保留 Redis 持久化数据。备份与恢复应在匹配的静默边界进行，不能仅复制部分 Artifact 目录。
2. 暂停新任务入口，等待 queued/running 工作完成或按既有 lease/恢复规则静默服务；不要强杀 post-Core Agent 再人为重放。
3. 为新版本设置新的镜像标签，构建或拉取已验证镜像；先预检，再执行 `docker compose run --rm migrate`。当前迁移到 `20261104_0010`（Run 的可空、受限元数据），仍沿既有迁移路径，不重建表或清空历史结果。
4. 更新服务并等待 `/ready`，然后重建 Frontend 以刷新 Nginx 的 Backend DNS：

```bash
docker compose up -d --no-deps --no-build --force-recreate --wait backend worker beat
docker compose up -d --no-deps --no-build --force-recreate --wait frontend
```

若需回滚，静默新工作后使用保存的旧镜像、**匹配该镜像的旧 Compose 文件和配置**，保留同一 project name/volumes。B15 前镜像没有 `/ready`，不能直接套用新健康检查。不要只改旧镜像 tag 却继续用不匹配的命令/健康检查。

同 schema 兼容回滚不自动降级数据库。跨 schema 回滚需先验证迁移 downgrade 条件并验证备份；users/归属、archive 和 Run 元数据迁移会拒绝不安全降级。0010 的新字段已有数据时会拒绝直接删列；不要通过清空字段、清空数据、转移归属或关闭约束绕过。当前阶段不提供自动生产回滚执行器。

`docker compose down` 保留数据卷；`down -v` 删除数据，只用于已确认属于自己的临时测试项目。服务健康检查失败不会自动修复配置，也不是重启生产数据库的指令。

## Agent 检索与现有数据

- Web 使用 `POST /api/v1/projects/{id}/questions`，返回202及Run/Task标识；Worker按服务端持久化路由执行Fast或Agent。证据不足可升级一次并复用初始Pack。客户端不提供强制策略，旧同步`/ask`、`/impact`和旧异步`/agent-runs`继续兼容。
- 所有新提交先授权再固定ready Snapshot。已接受任务使用原Snapshot/Commit，不跟随Project后来切换的当前Snapshot；软归档不会清空Memory或改变已接受任务的执行语义。Provider调用在数据库事务外，任务失去执行权时不能转成Memory降级继续运行。
- 基础Index/Graph/Chunk与语义索引分别发布。启用Embedding可在基础索引成功后触发既有`snapshot_embedding`任务；语义任务或Provider失败不会撤销基础ready。Snapshot详情`GET /api/v1/projects/{id}/snapshots/{snapshot_id}`的`semantic_index`记录语义状态。查询只读已发布向量，不负责建索引或修复。
- `PYCODE_EMBEDDING_*`独立于LLM配置，默认关闭；启用后文档构建会向所配Provider发送片段，查询也可能发送问题文本并产生费用。以配置模板及已实现Provider/模型校验为准，不把LLM key自动当作Embedding key，不把密钥放入前端变量。`PYCODE_RETRIEVAL_MODE`未设置时代码默认keyword，模板为hybrid；不可用Vector带诊断降级到可用的Keyword/静态Graph。
- Artifact必须校验Project/Snapshot/Commit、Chunk内容哈希、模型、维度及向量格式；同Commit不等于可跨Project/Snapshot共享。保留整个Workspace及原Manifest，继续沿staging/发布/恢复机制处理失败，不手动删除旧ready目录来“升级”。
- Graph Schema与Snapshot Manifest分别版本化，旧Graph按原能力兼容读取，不因为服务升级就变成最新Graph。相同Project/Commit复用旧ready时，可按现有流程补缺失Chunk；Graph不会因此自动升级，语义产物是否构建/复用取决于独立配置和任务。当前没有独立的Graph原地升级系统；需要新能力时先确认实际Schema与生命周期，另行设计显式升级，不能改Commit唯一约束或篡改Manifest绕过。
- Agent逻辑调用/轮数/时长预算在一次执行内共享；完整请求使用`PYCODE_AGENT_CONTEXT_TOKENS`保守估算上限，工具输出进入模型前按完整单元选择或明确舍弃。保留原ToolResult/Pack，Trace只保存受限安全投影。此估算与逻辑调用数不等于模型账单或传输重试次数。
- Memory继续使用项目Workspace内`.pclens/memory`及可信Scope，来源Run/证据不能由模型声明。用户项目背景/偏好、Snapshot代码事实、模型未验证提示的适用性不同；跨Snapshot保留文件不等于代码事实跨版本适用。冲突/失效/撤回按生命周期过滤，损坏单条或派生索引按原容错机制处理，预算/失去执行权信号仍终止执行。旧未知来源不自动升级为当前可信事实。

静态Graph的unknown/ambiguous不代表已确认运行时调用。Hybrid显式符号target存在被当文件路径的已知边界，调用方可使用相对文件target并把符号写入问题正文。Fake工程结果不构成真实质量或线上成本承诺；真实Provider实验需单独确认数据外发和费用边界。

历史评估绑定的是当时的实现/Prompt与语料指纹。生产源码发生变化后，旧评估的严格指纹校验可能拒绝当前实现；应使用原被测版本/冻结源码回放，或在获授权的评估批次建立新基线，不能修改收据绕过校验或把旧结果当新实现验证。

## CI 和本机隔离验证

CI 的 backend job 初始化专用 `pycode_test`、生成临时签名密钥、运行配置预检和完整迁移/认证授权/隔离/恢复测试。本机服务测试继续使用专用`pycode_test`和Redis DB15，先确认无并行测试/Worker且隔离配置正确；不要连接开发库或清理已有数据。B10 Redis候选测量、B14压测保持显式启用的可选实验。

```bash
python -m scripts.deploy.smoke
```

该脚本构建唯一镜像标签，在本机 Docker socket 上创建随机 `b15-...` 项目，以随机宿主端口启动整个栈，验证迁移、认证/归属、Workspace 权限、Worker ping、依赖故障及恢复。不会使用宿主 `.env` 的凭据，不覆盖根 `.env`，不调用 LLM/远程 Git。已有项目/卷/网络或凭据文件冲突会拒绝采用；结束时只删除自己创建的容器、卷与凭据文件，保留构建镜像。

报告和经过密钥过滤的失败诊断保存在被 Git 忽略的 `docs_v3.0/v3_backend/`；这不是生产部署数据备份。运行失败应先读取报告，不能对已有项目运行 `down -v` 代替诊断。
