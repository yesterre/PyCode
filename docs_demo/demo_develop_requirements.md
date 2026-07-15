# 项目内容

此项目（PyCode：Python 代码库理解与改动影响分析 Agent）作为我的第一个AI开发项目，总的上会把 Claude Code /Codex 此类Agent框架当成一个底层执行引擎，主要项目开发部分为自己的“中间层”：比如代码图谱构建、影响分析规则、上下文选择策略、任务流程控制、结果可视化等。其开发划分为几个阶段。

最终想做成的“pycode agent”是可以自动分析项目架构和关系，并可以回答用户的任何与项目相关的问题。

强调的点是，这不仅是做这个项目用于简历面试，也让我在项目开发中一边做一遍更深入学习相关知识。

# 阶段划分

## 阶段 0：项目准备阶段 （已完成）

这个阶段不写核心功能，主要是把项目骨架搭起来。

要做的是：

```
创建项目目录
创建虚拟环境
创建 requirements.txt
创建 README.md
创建基础模块文件
准备一个 examples/demo_project 示例项目
```

推荐结构是：

```
pycode/
│
├── pycode/
│   ├── __init__.py
│   ├── cli.py
│   ├── scanner.py
│   ├── parser.py
│   ├── models.py
│   └── storage.py
│
├── examples/
│   └── demo_project/
│       └── main.py
│
├── tests/
│   ├── test_scanner.py
│   └── test_parser.py
│
├── README.md
├── requirements.txt
└── .gitignore
```

这个阶段最重要的不是代码，而是你要明确每个文件的职责。

`scanner.py` 只负责找文件。
`parser.py` 只负责解析代码。
`models.py` 只负责定义数据结构。
`storage.py` 只负责保存和读取索引。
`cli.py` 只负责命令行入口。

这个阶段不要接 LLM，不要接 SDK，不要做 Agent，不要做前端。
完成标准是：能运行项目，项目结构清楚。

---

## 阶段 1：代码结构索引 MVP （已完成）

这是第一个真正的 MVP。

MVP 的意思是 **Minimum Viable Product，最小可行产品**。对你这个项目来说，它不是最终版，而是最小的一版：能扫描一个 Python 项目，并生成一个结构化索引。

这一阶段要做的事情是：

```
输入一个项目路径
递归扫描所有 .py 文件
忽略 .git、.venv、__pycache__、node_modules 等目录
用 ast 解析每个 Python 文件
提取 import、class、function
生成 index.json
用 CLI 打印项目摘要
```

这个阶段的核心模块是：

```
scanner.py
parser.py
models.py
storage.py
cli.py
```

最终希望用户可以运行：

```bash
python -m pycode.cli index ./examples/demo_project
```

然后生成：

```
.pclens/index.json
```

或者先简单一点，直接生成：

```
index.json
```

索引内容大概是这样：

```json
{
  "project_path": "./examples/demo_project",
  "files": [
    {
      "path": "main.py",
      "imports": ["os", "pathlib.Path"],
      "classes": [
        {
          "name": "UserService",
          "methods": ["get_user"]
        }
      ],
      "functions": ["main"]
    }
  ]
}
```

这一阶段不要做：

```
不要接 LLM
不要做问答
不要做代码修改
不要做复杂图谱
不要做可视化
```

完成标准是：

```
可以扫描一个小型 Python 项目
可以正确识别文件、import、class、function
可以生成 index.json
可以通过命令行打印统计结果
```

---

## 阶段 2：代码关系图谱 （已完成）

第一阶段只是“列清单”，第二阶段要开始建立“关系”。

第一阶段能回答：

```
这个项目有哪些文件？
每个文件里有哪些函数？
每个文件里有哪些类？
```

第二阶段要回答：

```
哪个文件 import 了哪个文件？
哪个类包含哪些方法？
哪个函数调用了哪些函数？
哪个文件可能是入口文件？
某个文件可能被哪些文件依赖？
```

这一阶段你的数据结构要从“列表”升级为“图”。

图可以这样理解：

```
节点 node：文件、类、函数
边 edge：包含、导入、调用
```

例如：

```
main.py --contains--> main()
main.py --imports--> user_service.py
UserService --contains--> get_user()
main() --calls--> get_user()
```

这个阶段先不用 Neo4j，甚至先不用真正的数据库。可以继续用 JSON，只是把索引结构设计得更像图。

比如：

```json
{
  "nodes": [
    {"id": "file:main.py", "type": "file", "name": "main.py"},
    {"id": "func:main.py:main", "type": "function", "name": "main"},
    {"id": "class:main.py:UserService", "type": "class", "name": "UserService"}
  ],
  "edges": [
    {"source": "file:main.py", "target": "func:main.py:main", "type": "contains"},
    {"source": "file:main.py", "target": "class:main.py:UserService", "type": "contains"}
  ]
}
```

这个阶段可以逐步加入：

```
import 关系
contains 关系
简单 calls 关系
入口文件判断
反向依赖查询
```

这一阶段可以新增模块：

```
graph_builder.py
query.py
```

项目结构变成：

```
pycode/
│
├── cli.py
├── scanner.py
├── parser.py
├── models.py
├── storage.py
├── graph_builder.py
└── query.py
```

`graph_builder.py` 负责把第一阶段的文件信息转成图结构。
`query.py` 负责查询图，比如“谁依赖了这个文件”。

这一阶段不要做：

```
不要接 LLM
不要做自然语言问答
不要自动修改代码
不要做复杂前端
```

完成标准是：

```
可以构建 nodes / edges
可以查询某个文件的 imports
可以查询某个文件被谁 import
可以查询某个函数内部调用了哪些函数
可以初步判断项目入口
```

这一阶段完成后，项目不只是“扫描器”，而是一个真正的“代码结构分析工具”。

---

## 阶段 3：代码库问答，也就是加入 LLM （已完成）

第三阶段开始接 LLM。

但是注意，LLM 不是用来替代前两阶段的，而是用来解释前两阶段的结果。

程序流程应该是：

```
用户提出问题
↓
根据问题检索 index / graph
↓
找到相关文件、函数、类
↓
读取少量代码片段
↓
组织 prompt
↓
调用 LLM
↓
输出回答，并附带文件路径和函数名
```

例如用户问：

```
这个项目的入口在哪里？
```

程序不应该直接把整个项目丢给 LLM。

正确流程是：

```
先从图里找 main.py、app.py、cli.py、if __name__ == "__main__"
再提取这些文件片段
再让 LLM 判断入口
```

这个阶段要做的功能可以有四个：

```
ask：自然语言问代码库
explain：解释某个文件或函数
onboard：生成新手阅读顺序
impact：分析改动影响
```

命令可以设计成：

```bash
pycode ask "这个项目的入口在哪里？"
pycode explain app/main.py
pycode onboard
pycode impact app/models/user.py
```

这一阶段可以新增模块：

```
retriever.py
llm_client.py
prompt_builder.py
commands/
```

各自职责是：

`retriever.py`：根据用户问题，从索引里找相关文件和函数。
`llm_client.py`：封装模型调用，比如 OpenAI、Claude、DeepSeek、Qwen。
`prompt_builder.py`：负责把问题、代码片段、结构信息拼成 prompt。

你要特别注意：这一阶段的重点不是“能不能调用模型”，而是**上下文选择**。

也就是说，用户问一个问题，你应该把哪些代码交给模型？交多少？按什么顺序？这才是这个项目的价值。

这一阶段不要做：

```
不要让 LLM 自己乱读整个仓库
不要让 LLM 自动改代码
不要做多 Agent
不要接太多模型供应商
```

完成标准是：

```
用户可以问项目相关问题
回答里能给出依据文件路径
回答不是纯猜测，而是基于索引和代码片段
可以解释单个文件
可以生成项目阅读顺序
可以做初步影响分析
```

例如输出应该像这样：

```
这个项目的入口大概率是 app/main.py。

原因：
1. 该文件中存在 main() 函数。
2. 文件底部存在 if __name__ == "__main__" 判断。
3. main() 中初始化了 UserService，并调用了核心流程。

相关位置：
- app/main.py::main
- app/services/user_service.py::UserService
```

这个阶段完成后，项目就从“代码分析工具”变成了“代码理解助手”。

---

## 阶段 4：Agent 化增强

第四阶段才开始真正进入 Agent。

前面第三阶段是：

```
用户问问题
程序检索上下文
LLM 回答
```

第四阶段是：

```
用户提出任务
Agent 自己决定需要查哪些文件、运行哪些工具、是否需要测试、最后给出结果
```

例如用户说：

```
帮我分析这次 git diff 是否会影响登录逻辑。
```

Agent 可以执行：

```
查看 git diff
查找被修改文件
查询这些文件在图谱中的依赖关系
读取相关测试文件
总结影响范围
给出风险点
```

这个阶段可以加入的工具有：

```
read_file：读取文件
search_code：搜索代码
query_graph：查询代码图谱
git_diff：读取 git diff
run_tests：运行 pytest
```

**可以先自己实现一个简单工具系统，不一定马上用 Claude Agent SDK**。

比如定义一个工具注册表：

```python
TOOLS = {
    "read_file": read_file,
    "search_code": search_code,
    "query_graph": query_graph,
}
```

等我理解这个流程后，再接 Claude Agent SDK 会更稳。

这一阶段可以新增模块：

```
tools/
  read_file.py
  search_code.py
  git_tools.py
  test_runner.py

agent/
  planner.py
  executor.py
  prompts.py
```

不要一开始就做很多 Agent。可以先做一个“单 Agent + 多工具”的结构。

这一阶段可以做的任务类型：

```
分析 git diff
分析改动影响
检查某个函数是否有测试覆盖
根据代码图谱推荐修改位置
生成修改建议
运行测试并总结失败原因
```

这一阶段暂时不要做：

```
不要让 Agent 自动大规模修改代码
不要让 Agent 自动提交 git
不要让 Agent 执行危险命令
不要做复杂多 Agent 协作
```

要加权限控制。例如：

```
只允许读取项目目录内文件
默认禁止删除文件
默认禁止执行 rm、del、format 等危险命令
运行测试需要显示命令
写文件前需要用户确认
```

完成标准是：

```
Agent 可以围绕一个开发任务多步调用工具
它的行为不是一次性问答，而是“查找—分析—验证—总结”
能处理 git diff / 测试失败 / 影响分析这类开发场景
```

这个阶段完成后，你的项目才真正可以称为 Agent 应用。

---

## 阶段 5：Agent 内核增强与可观测化

这一阶段暂时不急着做 Web 可视化，而是先把第四阶段已经形成的 Agent 骨架继续加厚。

第四阶段已经有了：

```
单 Agent
工具注册表
planner / executor / runtime
read_file / search_code / query_graph / git_diff / run_tests 等工具
基础权限控制
阶段三上下文检索能力复用
```

第五阶段要做的是把它从“能按规则调工具的开发分析 Agent”，增强为“可观测、可恢复、可规划、可积累项目知识的 Agent 载体”。

这个阶段的重点不是多做几个命令，而是补齐 Agent 工程里的关键基础设施：

```
工具调用生命周期
执行轨迹 Trace
TodoWrite 执行清单
任务 DAG
项目记忆系统
Prompt / Context 分层组装
```

这些能力完成后，再进入可视化阶段时，界面展示的就不只是问答结果，而是可以展示 Agent 的计划、状态、证据、依赖、记忆和工具调用过程。

---

### 阶段 5A：Hook + Trace 工具调用观测系统

这一小阶段的目标是让 Agent 的每一次执行都有清晰的生命周期记录。

第四阶段里，工具调用已经经过 executor 和 policy，但当前更多只是“执行并返回结果”。第五阶段 5A 要把工具调用变成可观测事件流。

可以设计四类 Hook：

```
UserPromptSubmit：用户任务进入 Agent 前
PreToolUse：工具执行前
PostToolUse：工具执行后
Stop：Agent 结束前
```

这些 Hook 可以用于：

```
记录用户任务
记录工具名称和参数摘要
执行权限检查
记录工具耗时
记录工具成功或失败
统计 evidence 数量
对大输出进行提醒或截断
在 Agent 结束时输出运行摘要
```

这一阶段可以新增模块：

```
agent/
  hooks.py
  trace.py
```

也可以扩展：

```
agent/runtime.py
agent/executor.py
agent/policy.py
agent/types.py
```

建议的数据结构可以包括：

```
TraceEvent
ToolTrace
AgentTrace
HookContext
HookResult
```

这一阶段暂时不要做：

```
不要做复杂插件系统
不要让 Hook 绕过权限策略
不要让 Hook 自动修改代码
不要把日志和业务逻辑混在工具实现里
```

完成标准是：

```
每次 agent 执行都能得到结构化 trace
trace 中包含每个工具的开始时间、结束时间、耗时、状态、摘要和错误
权限拒绝也能被记录为 trace event
CLI 可以打印简要执行轨迹
后续可视化阶段可以直接复用 trace 数据
```

---

### 阶段 5B：TodoWrite / Agent 执行清单

这一小阶段的目标是让 Agent 在多步任务中有明确的执行清单，避免复杂任务中途漂移。

当前 planner 会生成 `AgentStep` 列表，但它更像一次性计划。TodoWrite 要解决的是运行过程中的状态管理。

Todo 条目可以采用三态生命周期：

```
pending：尚未开始
in_progress：正在执行
completed：已完成
```

要强制的约束是：

```
同一时间最多只能有一个 in_progress
执行工具前应把对应 todo 标记为 in_progress
工具执行完成后应更新为 completed 或保留错误状态
完成的 todo 不删除，用于回溯
```

这一阶段可以新增模块：

```
agent/
  todo.py
```

也可以新增工具：

```
tools/
  todo_write.py
```

需要考虑两种形态：

```
内存态 Todo：用于单次 agent 执行
文件态 Todo：可选保存到 .pclens/current_todos.json，方便观察和恢复
```

这一阶段暂时不要做：

```
不要把 TodoWrite 做成复杂项目管理系统
不要和任务 DAG 混为一谈
不要让 todo 自动决定工具调用
```

完成标准是：

```
AgentResult 中能看到本次任务的 todos
每个 planned step 能映射到一个 todo
runtime 执行时会更新 todo 状态
如果工具失败，todo 状态和错误能被记录
CLI 能展示 todo 清单和当前执行进度
```

---

### 阶段 5C：基于文件的 Task DAG

这一小阶段的目标是支持更复杂的、带依赖关系的开发分析任务。

TodoWrite 适合当前会话内的扁平执行清单，但它不能表达“某个任务必须等另一个任务完成后才能开始”。Task DAG 要解决的是跨步骤、跨会话的依赖感知任务管理。

可以把每个任务保存为单独 JSON 文件：

```
.pclens/tasks/
  task_001.json
  task_002.json
  task_003.json
```

每个任务可以包含：

```
id
title
description
status
owner
blocked_by
created_at
updated_at
metadata
```

核心操作可以包括：

```
create_task：创建任务
list_tasks：列出任务
get_task：查看任务详情
claim_task：认领任务，只有依赖已完成时才能认领
complete_task：完成任务，并返回新解除阻塞的任务
```

依赖判断规则：

```
如果 blocked_by 为空，可以开始
如果 blocked_by 中所有任务都是 completed，可以开始
如果依赖任务不存在，视为阻塞而不是忽略
pending -> in_progress -> completed 单向推进
```

这一阶段可以新增模块：

```
agent/
  task_dag.py
```

也可以新增工具：

```
tools/
  task_tools.py
```

这一阶段暂时不要做：

```
不要做多 Agent 并发认领
不要做 git worktree 隔离
不要做复杂锁机制
不要把 Task DAG 做成完整项目管理软件
```

完成标准是：

```
可以创建带 blocked_by 的任务
可以根据依赖判断任务是否可开始
claim_task 会拒绝未解除阻塞的任务
complete_task 会返回被解除阻塞的下游任务
任务状态可以通过文件跨进程保存
后续可视化阶段可以画出任务 DAG
```

---

### 阶段 5D：轻量项目记忆系统

这一小阶段的目标是让 PyCode 能积累项目级知识，而不是每次都从零开始分析。

记忆系统不要一开始就做得太大。初版重点是保存 PyCode 自己分析出的项目知识。

可以保存的记忆类型包括：

```
project：项目结构、入口、核心模块说明
workflow：常用命令、测试命令、索引生成命令
analysis：历史影响分析结论
preference：用户对本项目的偏好或约束
```

建议存储结构：

```
.pclens/memory/
  MEMORY.md
  project-entry.md
  test-command.md
  impact-user-service.md
```

其中：

```
MEMORY.md 作为索引，只保存 name、description、type 和文件链接
每个记忆文件用 Markdown 保存正文
可以用简单 frontmatter 保存元数据
```

初期可以先做显式写入，不急着做 LLM 自动抽取：

```
memory_add
memory_list
memory_search
memory_load
```

后续再考虑：

```
根据 AgentResult 自动沉淀分析结论
根据问题选择相关记忆注入 prompt
定期合并重复记忆
```

这一阶段可以新增模块：

```
agent/
  memory.py
```

也可以新增工具：

```
tools/
  memory_tools.py
```

这一阶段暂时不要做：

```
不要做复杂向量数据库
不要默认把所有对话都写入记忆
不要让记忆内容绕过项目权限边界
不要让过期记忆覆盖当前代码事实
```

完成标准是：

```
可以创建、读取、列出和搜索项目记忆
MEMORY.md 索引能自动更新
Agent prompt 可以按需注入相关记忆
记忆与 index / graph / retrieve_context 的证据边界清晰
CLI 能展示当前项目已有记忆
```

---

### 阶段 5E：Prompt / Context 分层组装器

这一小阶段的目标是把 prompt 构建从单个字符串拼接，升级为可维护、可扩展的上下文组装系统。

随着 Hook、Trace、Todo、Task DAG 和 Memory 加入，Agent 可用上下文会变多。如果仍然把所有内容都硬拼进一个 prompt，会很难维护，也会浪费 token。

可以把上下文分成几类：

```
identity：Agent 身份和行为边界
tools：可用工具说明
policy：权限和安全规则
project：项目路径、索引和图谱信息
retrieval：本次检索到的代码上下文和 evidence
trace：本次工具调用轨迹摘要
todo：当前执行清单
tasks：任务 DAG 状态
memory：相关项目记忆
```

建议新增：

```
agent/
  context.py
  prompt_sections.py
```

核心思路是：

```
静态片段：身份、工具说明、权限规则
动态片段：项目状态、检索证据、trace、todo、memory
按需片段：只有存在相关数据时才注入
```

这一阶段暂时不要做：

```
不要做复杂 token 预算优化
不要做完整上下文压缩系统
不要把所有记忆无差别注入 prompt
不要让 prompt_builder 和 agent/prompts.py 继续无限膨胀
```

完成标准是：

```
Agent 总结 prompt 由多个 section 组成
每个 section 的来源清晰
不存在的数据不会注入 prompt
Prompt 中能包含 trace / todo / memory 的摘要
阶段三问答 prompt 和阶段四/五 Agent prompt 边界清楚
```

---

阶段五完成后，PyCode 的 Agent 层应该具备：

```
可观测：能看到工具调用轨迹
可规划：能看到执行清单和任务依赖
可恢复：任务 DAG 和记忆可以落盘
可解释：最终结论能追溯到 evidence、trace 和 memory
可展示：后续可视化层有足够丰富的数据可展示
```

这一阶段完成后，再做可视化 Demo 会更有意义。因为展示层不再只是包装 CLI 输出，而是可以展示一个 Agent 如何计划、执行、查证、记录和沉淀知识。

---

## 阶段 6：可视化和产品化展示

这一阶段不是核心算法，但对简历和展示很重要。

可以做两种展示方式。

第一种是终端增强。

使用 Rich 美化 CLI 输出：

```
表格显示文件统计
树形结构显示项目结构
高亮显示代码位置
漂亮地输出影响分析结果
```

第二种是简单 Web UI。

可以用 Streamlit 做一个页面：

```
左边选择项目路径
中间显示文件树
右边显示问答结果
下方显示代码图谱关系
```

这个阶段不要过早开始。只有当前面功能能跑通之后，展示层才有意义。

这一阶段可以新增：

```
ui/
  streamlit_app.py
```

或者先只做：

```
rich_output.py
```

完成标准是：

```
别人打开你的项目 README，能快速理解你做了什么
别人运行 demo，能看到清楚输出
有截图或 GIF
有示例问题
有项目架构图
```

这一步很重要，因为找实习时，面试官不一定会认真读完你的源码，但他会看 README、截图、功能说明和技术取舍。

---

## 阶段 7：工程化、测试和简历化

最后阶段是把它从“能跑的 demo”变成“像样的项目”。

你要补这些东西：

```
测试用例
异常处理
日志
配置文件
README
技术文档
示例项目
项目架构图
开发路线图
局限性说明
```

测试方面至少要有：

```
scanner 测试
parser 测试
storage 测试
graph_builder 测试
retriever 测试
```

README 里建议写清楚：

```
项目背景
为什么做这个项目
解决什么问题
核心功能
技术栈
项目架构
使用方法
示例输出
当前局限
后续计划
```

局限性一定要写。比如：

```
当前主要支持 Python 项目
函数调用关系基于静态 AST 分析，无法完全覆盖动态调用
暂未支持跨语言项目
暂未支持复杂类型推断
Agent 默认不自动修改代码
```

这些不是缺点，反而显得项目边界很清楚。

完成标准是：

```
项目可以被别人 clone 后运行
README 能让别人看懂
demo 能展示核心能力
你自己能讲清楚技术路线和设计取舍
```

---

**整体路线可以这样理解**

可以把项目成长过程想成这样：

```
阶段 0：搭骨架
↓
阶段 1：能扫描代码
↓
阶段 2：能理解代码关系
↓
阶段 3：能基于代码回答问题
↓
阶段 4：能作为 Agent 调工具完成开发分析任务
↓
阶段 5：能让 Agent 可观测、可恢复、可规划、可积累项目知识
↓
阶段 6：能展示
↓
阶段 7：能写进简历和面试讲解
```

更具体一点：

```
阶段 1 产物：index.json
阶段 2 产物：code_graph.json
阶段 3 产物：代码库问答 CLI
阶段 4 产物：开发任务分析 Agent
阶段 5 产物：Hook/Trace、TodoWrite、Task DAG、Memory、Context Builder
阶段 6 产物：可视化 Demo
阶段 7 产物：完整 GitHub 项目
```

# 代码Agent助手要求

开发过程中我使用 Codex、Claude Code、Cursor 这类 Agent 辅助。

但有一个前提：**用于来提高开发效率，不要用它替代我的项目理解。**

例如可以让 Codex 帮你做这些事：

```
生成某个函数的初版代码
帮你解释报错
帮你写 pytest 测试
帮你重构重复代码
帮你检查 README 表达
帮你生成 CLI 使用示例
帮你定位 bug
```

比如可以这样问 Codex：

```
请帮我实现 scanner.py，功能是递归扫描指定目录下所有 .py 文件，忽略 .venv、__pycache__、.git 目录，返回 pathlib.Path 列表。
```

或者：

```
请帮我为 parse_python_file 函数写 pytest 测试，测试它能正确提取 import、class、function。
```

这些都非常适合交给 Agent。

但不要这样用：

```
帮我完整做一个 Python 代码库理解 Agent 项目。
```

更好的方式是你当“项目负责人”，Codex 当“初级开发助手”。你负责决定：

```
这个阶段做什么
文件结构怎么分
数据结构怎么设计
功能边界在哪里
哪些功能暂时不做
代码是否合并
```

Codex 负责帮你：

```
补局部代码
写测试
修 bug
解释报错
优化实现
```

请牢记。

# demo开发记录规范

每当开启一个新阶段开发开始前，请你在docs里新建一个md文档，专门用于存放该阶段的开发记录。

## 开发前

请你在对应阶段文档中补充以下内容：
1.本阶段要完成的内容，要达到的效果；
2.拆解这一阶段需要进行的工作，实现的功能，具体到各个具体文件;
3.用mermaid画出该阶段各部分之间的流程图/关系图。

## 开发中

请你在对应阶段文档中补充以下内容：
1.每完成或修改一块内容时，向文档中添加或修改一段简单的开发纪要，说明这一步动作做了什么；
2.要保留一块专门的区域，用于记录每次开发中出现的问题，例如报错等，需要总结问题；

### 开发后

请你在对应阶段文档中补充以下内容：
1.根据开发实际情况总结本阶段实际完成内容和完成情况；
2.总结后续该阶段可改进或升级的点；
3.针对这一阶段实现的内容，用一块专门的区域，用于简单明了地给出这一阶段各个已实现的功能或测试的具体运行方法（运行步骤、命令行指令等）。
