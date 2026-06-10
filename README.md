# PyCode

PyCode 是一个 Python 代码库理解与改动影响分析 Agent 项目。当前处于阶段一：代码结构索引 MVP。

阶段一暂不接入 LLM、不做问答、不做代码修改、不做复杂图谱，重点是先完成一个可靠的 Python 项目结构索引能力。

## 阶段一已完成功能

- 递归扫描指定项目目录下的 `.py` 文件。
- 忽略 `.git`、`.venv`、`venv`、`__pycache__`、`node_modules` 等目录。
- 使用 Python 标准库 `ast` 解析代码结构。
- 提取每个文件中的 `import`、`class`、`function`。
- 使用 `ProjectIndex`、`FileInfo`、`ClassInfo` 组织索引数据。
- 将索引保存为 JSON 文件。
- 支持从 JSON 文件读取索引。
- 提供 CLI 命令输出项目结构摘要。
- 提供 `examples/demo_project` 示例项目用于验证。
- 提供 scanner、parser、storage 的单元测试。

## 项目结构

```text
pycode/
  cli.py
  scanner.py
  parser.py
  models.py
  storage.py

examples/
  demo_project/

tests/
  test_scanner.py
  test_parser.py
  test_storage.py
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

扫描示例项目：

```powershell
python -m pycode.cli index ./examples/demo_project
```

默认输出位置是被扫描项目目录下的：

```text
examples/demo_project/.pclens/index.json
```

命令完成后会输出类似摘要：

```text
PyCode index completed.
Project path: examples\demo_project
Python files: 7
Imports: 9
Classes: 4
Functions: 8
Index file: examples\demo_project\.pclens\index.json
```

## 指定索引输出路径

可以使用 `--output` 或 `-o` 指定输出文件：

```powershell
python -m pycode.cli index ./examples/demo_project --output ./examples/demo_project/.pclens/index.json
```

短参数写法：

```powershell
python -m pycode.cli index ./examples/demo_project -o ./examples/demo_project/.pclens/index.json
```

## 运行测试

运行阶段一已有单元测试：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_scanner.py tests/test_parser.py tests/test_storage.py
```

如果已经激活虚拟环境：

```powershell
python -m pytest tests/test_scanner.py tests/test_parser.py tests/test_storage.py
```

如果遇到 pytest 临时目录权限问题，可以指定临时目录：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_scanner.py tests/test_parser.py tests/test_storage.py --basetemp=.pytest_tmp --cache-clear
```

正常通过时会看到类似：

```text
15 passed
```

## 当前局限

- 当前主要支持 Python 项目。
- 当前只提取基础结构信息，不分析函数调用关系。
- 当前索引仍是列表结构，不是图结构。
- 当前不接入 LLM。
- 当前不自动修改代码。

## 后续计划

阶段二将从结构索引升级为代码关系图谱，开始构建 `nodes / edges`，支持文件、类、函数之间的包含、导入和简单调用关系。
