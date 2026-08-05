# 配置参考

Tracecast 从本地 runtime 配置、项目声明和 Host 文件组合出一次 Run。命令行参数和默认值以虚拟
环境 Python 执行的 `-m tracecast <command> --help` 为准；本页说明这些文件怎样配合。

## Runtime 配置

默认路径是 `configs/runtime.json`。先复制可由正式 loader 读取的样例。

PowerShell：

```powershell
Copy-Item configs/runtime.example.json configs/runtime.json
Copy-Item configs/source_profile_templates.example.json configs/source_profile_templates.json
```

Bash/zsh：

```bash
cp configs/runtime.example.json configs/runtime.json
cp configs/source_profile_templates.example.json configs/source_profile_templates.json
```

`RuntimeConfig` 接受三个主要区块：

- `providers`：OpenAI-compatible gateway 的 `base_url`、`chat_completions_path`、输出 token 字段和
  key 引用；
- `routes`：每种模型操作使用的 provider、model、prompt、输出上限、timeout、并发和 transport
  retry 参数；
- `stage_budgets`：切片、打包和输入预算。

每个 provider key 的 `api_key_env` 只保存环境变量名。默认样例使用
`TRACECAST_LLM_API_KEY`。

PowerShell：

```powershell
$env:TRACECAST_LLM_API_KEY="<your-key>"
```

Bash/zsh：

```bash
export TRACECAST_LLM_API_KEY="<your-key>"
```

也可以把同名赋值写入仓库根目录的本地 `.env`。`RuntimeConfig.load` 默认先加载 `.env`，但已存在
的进程环境变量优先。不要把 secret 写进 `runtime.json`。

如果 gateway 使用 `max_completion_tokens`，把 provider 的 `max_output_tokens_field` 改成该值。
provider 特有但不覆盖 `model`、`messages`、`stream`、`temperature` 或输出 token 字段的请求参数，
可以放入 route 的 `extra_body`。

### 成本费率

Tracecast 不内置 provider 价格。需要本地估算时，在 runtime 顶层增加 `cost_profiles`，再让 route 的
`cost_profile` 指向对应名称：

```json
{
  "cost_profiles": {
    "operator-rates": {
      "uncached_input_per_million": 2.0,
      "cached_input_per_million": 1.0,
      "output_per_million": 3.0
    }
  }
}
```

三个数值使用操作者选择的同一货币单位，每百万 token 计价。这个结果是
`configured_estimate`，不是 provider 账单。没有给 route 配置有效费率时，Console 会显示费率未配置，
而不是把计算出的 `0.0` 当作免费。

## 项目目录

项目位于 `projects/<project_id>/`。`project_id` 必须是 1–64 个小写 ASCII 字母、数字、`_` 或
`-`，并以字母或数字开头。最小目录为：

```text
projects/example/
  sources.json
  raw/
    example_script.txt
```

`sources.json` 的核心结构：

```json
{
  "project_id": "example",
  "output_language": "zh-CN",
  "name_resolution_mode": "overrides_only",
  "sources": [
    {
      "source_id": "example_script",
      "path": "raw/example_script.txt",
      "profile": "script_transcript",
      "language": "en"
    }
  ]
}
```

- `output_language` 必须是具体的 BCP 47 language tag。`tracecast run --output-language` 可以只为
  本次根 Run 覆盖它。
- `path` 相对于项目目录解析。核心 Ingestion 只读取已经落地的 UTF-8 文本，不抓取 URL，也不清洗
  网页或 PDF。
- `profile` 必须存在于 source profile templates；profile 提供默认 `source_class`、`trust_tier`、
  content kind 和 tag 范围。
- `language` 表示来源语言，不是输出语言。
- source row 可以显式提供 `source_type`、`source_class`、`trust_tier`、`title`、`note`、
  `content_hash` 和 `metadata`。

Console 导入文本时会把确认后的正文保存为无 BOM UTF-8。发生转码时，它还会把上传的原始字节保存
到 `imports/original/`。

## 专名方式

`name_resolution_mode` 只允许：

- `overrides_only`：默认值；只应用项目中实际命中的人工 override，不产生专名模型调用；
- `name_table`：在正文生成前，把完整 selected factual materials 投影给一次专名调用；超出输入预算
  或调用/解析失败时，只保留人工 override。

可以在 `sources.json` 中直接写 `name_overrides`：

```json
{
  "name_overrides": [
    {
      "aliases": ["Terra", "Tina"],
      "target_surface": "蒂娜",
      "output_language": "zh-CN"
    }
  ]
}
```

也可以用 `name_overrides_path` 指向项目目录内的 JSON。该文件可以是 entry 数组，或包含
`entries` 数组的对象。同一输出语言下，同一个 alias 不能对应不同 target。

## Host

Host 独立保存在 `hosts/<host-id>.json`，可以被多个项目复用。完整 Run 必须显式选择 Host，且 Host
的 `output_language` 必须与本次有效输出语言完全一致。

Console 提供只读 starter Host。也可以从 UTF-8 偏好文本构建新的 Host。

PowerShell：

```powershell
.\.venv\Scripts\python.exe -m tracecast host-build `
  --host-id my_host D:\path\to\preference-texts
```

Bash/zsh：

```bash
./.venv/bin/python -m tracecast host-build \
  --host-id my_host /path/to/preference-texts
```

目录输入只读取当前目录的 `*.txt`，不递归。Host Builder 使用两次 provider 调用，并在目标 Host ID
已经存在时拒绝覆盖。它对输入做本地切片和抽样；具体上传与留存边界见
[隐私、成本与安全](privacy-cost-safety.md)。

## CLI 路径覆盖

常用默认路径可以显式覆盖：

- `--projects-dir projects`
- `--hosts-dir hosts`
- `--config configs/runtime.json`
- `--profiles configs/source_profile_templates.json`
- `--runs-root runs`

使用虚拟环境 Python 运行 `-m tracecast <command> --help` 查看当前参数全集。
