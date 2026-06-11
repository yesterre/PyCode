# 项目内容

此项目（PyCode：Python 代码库理解与改动影响分析 Agent）作为我的第一个AI开发项目，总的上会把 Claude Code /Codex 此类Agent框架当成一个底层执行引擎，主要项目开发部分为自己的“中间层”：比如代码图谱构建、影响分析规则、上下文选择策略、任务流程控制、结果可视化等。其开发划分为几个阶段。

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

## 阶段 2：代码关系图谱

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

## 阶段 3：代码库问答，也就是加入 LLM

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

## 阶段 5：可视化和产品化展示

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

## 阶段 6：工程化、测试和简历化

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
阶段 5：能展示
↓
阶段 6：能写进简历和面试讲解
```

更具体一点：

```
阶段 1 产物：index.json
阶段 2 产物：code_graph.json
阶段 3 产物：代码库问答 CLI
阶段 4 产物：开发任务分析 Agent
阶段 5 产物：可视化 Demo
阶段 6 产物：完整 GitHub 项目
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

# 开发记录规范

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