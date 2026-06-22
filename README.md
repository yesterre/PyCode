# PyCode

PyCode 是一个 Python 代码库理解与改动影响分析 Agent 项目。当前已进入阶段三：代码库问答和轻量 LLM 接入。

现阶段项目基于阶段一索引和阶段二代码图谱选择有限上下文，再通过 LLM 解释这些证据。当前不自动修改代码，不让 LLM 自己读取整个仓库，也不做多 Agent。

## 已完成功能

### 阶段一：代码结构索引 MVP

- 递归扫描指定项目目录下的 `.py` 文件。
- 忽略 `.git`、`.venv`、`venv`、`__pycache__`、`node_modules` 等目录。
- 使用 Python 标准库 `ast` 解析代码结构。
- 提取每个文件中的 `import`、`class`、`function`。
- 使用 `ProjectIndex`、`FileInfo`、`ClassInfo` 组织索引数据。
- 将索引保存为 `.pclens/index.json`。
- 支持从 JSON 文件读取索引。
- 提供 CLI 命令输出项目结构摘要。

### 阶段二：代码关系图谱

- 新增 `GraphNode`、`GraphEdge`、`CodeGraph` 图谱数据结构。
- 提取函数和方法内部的简单调用关系。
- 识别 `if __name__ == "__main__"` 入口线索。
- 将文件、类、函数、方法转换为图谱节点。
- 将包含、导入、调用关系转换为图谱边。
- 将图谱保存为 `.pclens/code_graph.json`。
- 支持读取图谱 JSON 并还原为 dataclass。
- 支持查询文件 imports。
- 支持查询文件被谁 import。
- 支持查询函数或方法 calls。
- 支持初步判断入口候选文件。
- 提供 `examples/demo_project` 示例项目用于阶段二验证。
- 提供 parser、graph_builder、query、storage、cli 等单元测试。

### 阶段三：代码库问答

- 基于 `index.json` 和 `code_graph.json` 选择有限上下文。
- 支持自然语言问答、文件解释、新手阅读顺序和初步影响分析。
- 使用 OpenAI Responses API 做第一版 LLM 接入。
- 回答输出会附带文件路径、节点或图谱关系作为依据位置。

## 项目结构

```text
pycode/
  __init__.py
  cli.py
  scanner.py
  parser.py
  models.py
  storage.py
  graph_builder.py
  query.py
  retriever.py
  prompt_builder.py
  llm_client.py

examples/
  demo_project/
    main.py
    controllers/
      user_controller.py
    services/
      user_service.py
    models/
      user.py
    utils/
      formatting.py
    .pclens/
      index.json
      code_graph.json

tests/
  test_cli.py
  test_graph_builder.py
  test_parser.py
  test_prompt_builder.py
  test_query.py
  test_retriever.py
  test_scanner.py
  test_storage.py

docs/
  stage1_development_record.md
  stage2_development_record.md
```

## 安装依赖

建议先进入项目根目录，并使用虚拟环境中的 Python。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

如果已经激活虚拟环境，也可以执行：

```powershell
python -m pip install -r requirements.txt
```

## 生成索引

扫描示例项目，并生成阶段一索引：

```powershell
.\.venv\Scripts\python.exe -m pycode.cli index .\examples\demo_project
```

默认输出位置：

```text
examples/demo_project/.pclens/index.json
```

命令完成后会输出类似摘要：

```text
PyCode index completed.
Project path: examples\demo_project
Python files: 9
Imports: 17
Classes: 5
Functions: 9
Index file: examples\demo_project\.pclens\index.json
```

## 生成代码图谱

扫描示例项目，并生成阶段二代码图谱：

```powershell
.\.venv\Scripts\python.exe -m pycode.cli graph .\examples\demo_project
```

默认输出位置：

```text
examples/demo_project/.pclens/code_graph.json
```

命令完成后会输出类似摘要：

```text
PyCode graph completed.
Project path: examples\demo_project
Nodes: 55
Edges: 76
File nodes: 9
Class nodes: 5
Function nodes: 27
Method nodes: 10
Import edges: 11
Call edges: 41
Graph file: examples\demo_project\.pclens\code_graph.json
```

## 查询代码图谱

查询 `main.py` 导入了哪些目标：

```powershell
.\.venv\Scripts\python.exe -m pycode.cli query imports .\examples\demo_project main.py
```

查询 `services/user_service.py` 被哪些文件导入：

```powershell
.\.venv\Scripts\python.exe -m pycode.cli query imported-by .\examples\demo_project services/user_service.py
```

查询 `main.py` 中的 `main` 函数调用了哪些目标：

```powershell
.\.venv\Scripts\python.exe -m pycode.cli query calls .\examples\demo_project func:main.py:main
```

查询入口候选文件：

```powershell
.\.venv\Scripts\python.exe -m pycode.cli query entry .\examples\demo_project
```

## 指定输出路径

生成索引时可以使用 `--output` 或 `-o` 指定输出文件：

```powershell
.\.venv\Scripts\python.exe -m pycode.cli index .\examples\demo_project --output .\examples\demo_project\.pclens\index.json
```

生成图谱时也可以指定输出文件：

```powershell
.\.venv\Scripts\python.exe -m pycode.cli graph .\examples\demo_project --output .\examples\demo_project\.pclens\code_graph.json
```

查询时如果图谱文件不在默认位置，可以使用 `--graph` 指定：

```powershell
.\.venv\Scripts\python.exe -m pycode.cli query imports .\examples\demo_project main.py --graph .\examples\demo_project\.pclens\code_graph.json
```

## 阶段三：代码库问答

阶段三命令需要先生成索引和图谱：

```powershell
.\.venv\Scripts\python.exe -m pycode.cli index .\examples\demo_project
.\.venv\Scripts\python.exe -m pycode.cli graph .\examples\demo_project
```

真实调用 LLM 前需要设置 OpenAI API Key：

```powershell
$env:OPENAI_API_KEY="你的 API Key"
```

也可以复制 `.env.example` 为 `.env`，在 `.env` 中配置模型、API Key 和 base URL：

```text
OPENAI_API_KEY=你的 API Key
OPENAI_MODEL=gpt-5.4-mini
OPENAI_API_TYPE=responses
OPENAI_BASE_URL=https://api.openai.com/v1
```

如果使用 DeepSeek、Qwen 或其它 OpenAI-compatible 网关，遇到不支持 `/responses` 的错误时，将 API 类型改为 chat：

```text
OPENAI_MODEL=deepseek-v4-pro-202606
OPENAI_API_TYPE=chat
OPENAI_BASE_URL=https://你的兼容网关/v1
```

配置优先级为：终端环境变量优先于 `.env`，命令行 `--model` 优先于 `OPENAI_MODEL`。

自然语言问答：

```powershell
.\.venv\Scripts\python.exe -m pycode.cli ask .\examples\demo_project "这个项目的入口在哪里？"
```

解释单个文件：

```powershell
.\.venv\Scripts\python.exe -m pycode.cli explain .\examples\demo_project main.py
```

生成新手阅读顺序：

```powershell
.\.venv\Scripts\python.exe -m pycode.cli onboard .\examples\demo_project
```

初步影响分析：

```powershell
.\.venv\Scripts\python.exe -m pycode.cli impact .\examples\demo_project services/user_service.py
```

指定模型：

```powershell
.\.venv\Scripts\python.exe -m pycode.cli ask .\examples\demo_project "这个项目的入口在哪里？" --model gpt-5.5
```

## 运行测试

运行全部测试：

```powershell
.\.venv\Scripts\python.exe -m pytest tests --basetemp=.pytest_tmp --cache-clear
```

如果已经激活虚拟环境，也可以执行：

```powershell
python -m pytest tests --basetemp=.pytest_tmp --cache-clear
```

正常通过时会看到类似：

```text
48 passed
```

## 当前局限

- 当前主要支持 Python 项目。
- 函数调用关系基于静态 AST 分析，无法完全覆盖动态调用。
- 当前不做复杂类型推断，所以 `runner.run()`、`self.service.get_user()` 这类变量方法调用不一定能精确解析到真实方法节点。
- 当前 import 解析以常见项目内部导入为主，对标准库和第三方库只建立外部节点。
- 当前入口判断属于初步判断，主要依据文件名和顶层 `main` 函数。
- 当前 LLM 只解释 PyCode 选择出的有限上下文。
- 当前不自动修改代码。
- 当前不让 LLM 自己读取整个仓库。
- 当前不使用 Neo4j 等图数据库，图谱先保存为 JSON。

## 后续计划

- 阶段三：继续增强代码库问答的上下文选择、函数级片段截取和回答质量。
- 增强调用关系解析，把更多方法调用解析到准确的类方法节点。
- 将 `has_main_guard` 等入口线索写入图谱，提高入口判断准确度。
- 优化 CLI 查询输出，展示更友好的文件路径、节点名称和关系说明。
- 后续再考虑影响分析、Agent 工具调用和可视化展示。
