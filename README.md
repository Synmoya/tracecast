# Tracecast

Tracecast 是一个从本地长篇材料生成可追踪播客脚本的工具。它把来源切片、内容规划、材料取舍、
正文生成、审计和拼接分开执行，并为模型调用和中间产物保留本地记录。

Tracecast 适合需要检查材料来源、编辑路径和模型调用的本地工作流。它不会抓取网页或 PDF、不会把
审计问题自动改写进正文，也不提供 TTS。随附 Console 是无鉴权的本地调试工具，不是可以直接部署
到公网的产品前端。

## 安装

需要 Python 3.12 或更高版本。项目目前只支持从源码安装，不通过 PyPI 发布。主路径直接调用虚拟
环境中的 Python，不要求激活虚拟环境。

PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install ".[console]"
.\.venv\Scripts\python.exe -m tracecast --help
```

Bash/zsh：

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install '.[console]'
./.venv/bin/python -m tracecast --help
```

只使用 CLI 时，把安装参数 `.[console]` 改为 `.`。激活虚拟环境只是可选便利；后续示例继续显式使用
该环境的 Python，以保证安装和运行来自同一解释器。

## 用 Console 完成首次运行

在第一次连接 provider 前，请先阅读[隐私、成本与安全](docs/user/privacy-cost-safety.md)。

PowerShell：

```powershell
.\.venv\Scripts\python.exe -m tracecast console
```

Bash/zsh：

```bash
./.venv/bin/python -m tracecast console
```

打开 `http://127.0.0.1:8765/`，然后按页面引导执行以下操作：

1. 配置 OpenAI-compatible Chat Completions gateway、API key 和模型名。
2. 导入本地文本，确认编码、source profile、来源语言和输出语言，然后创建项目。
3. 选择与输出语言完全一致的 Host，确认估算，再启动完整 Run。
4. Run 完成后，在结果页查看脚本。

Console 默认监听 `127.0.0.1:8765`。它没有用户认证；不要把监听地址改成局域网或公网地址。

## CLI 最短路径

先从样例创建本地配置，再把 gateway URL、模型和 `.env` 中的 key 占位符改成实际值。

PowerShell：

```powershell
Copy-Item configs/runtime.example.json configs/runtime.json
Copy-Item configs/source_profile_templates.example.json configs/source_profile_templates.json
Copy-Item .env.example .env

.\.venv\Scripts\python.exe -m tracecast run --project example --host example_host_zh `
  --max-sources 1 --limit-source-chars 2000
```

Bash/zsh：

```bash
cp configs/runtime.example.json configs/runtime.json
cp configs/source_profile_templates.example.json configs/source_profile_templates.json
cp .env.example .env

./.venv/bin/python -m tracecast run --project example --host example_host_zh \
  --max-sources 1 --limit-source-chars 2000
```

成功时 CLI 会输出 `run_id`、artifact 目录、chunk 数和审计项数量。示例限制来源数量和字符数，
便于先验证连接，但仍会产生多次 provider 调用和费用。

## 会得到什么

一次成功 Run 会在 `runs/<run-id>/` 中保留：

- `assembly/script.md`：最终播客脚本；
- `validation/audit_report.jsonl`：跨阶段审计结果；
- `llm/call_records.jsonl` 与 `llm/raw/`：调用账本及原始请求/响应；
- 各阶段的结构化 artifact，供 Console 查看路径、材料取舍和生成依据。

这些记录可能包含原始材料片段和模型响应。分享或备份前，请按安全页检查内容。

## 用户文档

- [安装与首次运行](docs/user/getting-started.md)
- [配置参考](docs/user/configuration.md)
- [隐私、成本与安全](docs/user/privacy-cost-safety.md)

配置样例和 `projects/example/` 只包含占位内容。提交或分享自己的项目、Host 或 Run 前，请检查其中
的原始材料、偏好文本、模型请求和模型响应是否允许公开。

## 许可

Tracecast 使用 [Apache License 2.0](LICENSE)。
