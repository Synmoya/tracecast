# 安装与首次运行

本页帮助你从源码安装 Tracecast，并通过本地 Console 或 CLI 完成一个小规模 Run。

在连接 provider 前，请先阅读[隐私、成本与安全](privacy-cost-safety.md)。Tracecast 会把材料内容
发送给你配置的 gateway，并把模型请求、响应和中间产物保存在本地。

## 前置条件

- Python 3.12 或更高版本；
- 一个 OpenAI-compatible Chat Completions gateway；
- gateway 的 API key 和实际模型名；
- 允许发送给该 provider 的本地文本材料。

项目目前只支持从源码安装，不通过 PyPI 发布。

## 安装 Console

主路径直接调用虚拟环境中的 Python，不要求激活虚拟环境。

PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install ".[console]"
.\.venv\Scripts\python.exe -m tracecast --version
```

Bash/zsh：

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install '.[console]'
./.venv/bin/python -m tracecast --version
```

版本命令输出版本号表示入口已经安装。只使用 CLI 时，把安装参数 `.[console]` 改为 `.`。激活
虚拟环境只是可选便利；以下命令继续显式使用虚拟环境解释器。

## 通过 Console 创建项目

PowerShell：

```powershell
.\.venv\Scripts\python.exe -m tracecast console
```

Bash/zsh：

```bash
./.venv/bin/python -m tracecast console
```

终端出现 Uvicorn 的启动信息后，打开 `http://127.0.0.1:8765/`。

1. 在首次设置页填写 gateway URL、Chat Completions path、API key 和模型名。
   设置成功后，本地 `configs/runtime.json` 保存连接和模型配置，本地 `.env` 保存
   `TRACECAST_LLM_API_KEY`。
2. 进入新建项目页，导入 `.txt`、`.md`、`.markdown`、`.srt` 或 `.vtt` 文件。
3. 检查每个文件的预览和编码。自动识别不可靠时，手动选择页面提供的编码。
4. 选择手动或自动 source profile：手动方式不调用模型；自动方式先显示 Intake 估算，只有确认后
   才调用 provider。
5. 确认项目 ID、来源语言、输出语言和专名方式，然后创建项目。项目目录会写入 `projects/<id>/`。
6. 新建完整 Run，选择与输出语言完全一致的 Host。检查估算后再确认执行。

Run 成功后，结果页显示脚本和审计信息；同一结果保存在：

```text
runs/<run-id>/assembly/script.md
```

按 `Ctrl+C` 停止 Console。停止服务不会删除项目或已经写入的 Run。

## 通过 CLI 验证小切片

复制无 secret 的样例。

PowerShell：

```powershell
Copy-Item configs/runtime.example.json configs/runtime.json
Copy-Item configs/source_profile_templates.example.json configs/source_profile_templates.json
Copy-Item .env.example .env
```

Bash/zsh：

```bash
cp configs/runtime.example.json configs/runtime.json
cp configs/source_profile_templates.example.json configs/source_profile_templates.json
cp .env.example .env
```

编辑 `configs/runtime.json`，至少替换：

- `providers.gateway.base_url`；
- 各 route 使用的实际 `model`；
- provider 要求不同路径时的 `chat_completions_path`；
- provider 使用 `max_completion_tokens` 时的 `max_output_tokens_field`。

再把 `.env` 中的占位值替换为实际 API key。不要提交 `.env`。

仓库内的 `projects/example/` 和 `hosts/example_host_zh.json` 可以用于连接验证。

PowerShell：

```powershell
.\.venv\Scripts\python.exe -m tracecast run --project example --host example_host_zh `
  --max-sources 1 --limit-source-chars 2000
```

Bash/zsh：

```bash
./.venv/bin/python -m tracecast run --project example --host example_host_zh \
  --max-sources 1 --limit-source-chars 2000
```

成功标志是 CLI 输出非空 `run_id` 和 artifact 目录，并在该目录生成
`assembly/script.md`。小切片只限制来源范围，不保证单次模型调用；请仍按多次调用估算费用。

## 常见可恢复失败

- `configs/runtime.json` 不存在：完成 Console 设置，或从样例复制并填写实际连接。
- 环境变量缺失或为空：设置配置中 `api_key_env` 指向的变量；默认是
  `TRACECAST_LLM_API_KEY`。
- Host 语言不匹配：选择 `output_language` 完全相同的 Host。
- 项目目录或 `sources.json` 不存在：通过 Console 创建项目，或按[配置参考](configuration.md)
  检查目录。
- 显式 `run_id` 已存在：换一个 ID，或省略 `--run-id` 让程序生成新 ID。程序不会续写已有 Run。
- provider 或 DSL 失败：查看 `runs/<run-id>/llm/` 和阶段报告；失败 Run 会保留已写入的诊断，
  重新运行时使用新 Run ID。

完整命令参数以虚拟环境 Python 执行的 `-m tracecast <command> --help` 为准。
