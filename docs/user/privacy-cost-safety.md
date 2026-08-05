# 隐私、成本与安全

在第一次确认模型调用前，请检查本页。Tracecast 把模型请求发送给你配置的 OpenAI-compatible
Chat Completions gateway；项目本身不决定该 gateway 怎样保留或使用数据。

## 会发送给 provider 的内容

不同命令发送的数据范围不同：

| 操作 | 发送内容 |
|---|---|
| `tracecast intake` | 每个 raw 文件的确定性抽样文本、项目相对路径、source ID 猜测、字符/行数和结构统计，以及可选 source profile 目录；不会上传 raw 全文。 |
| `tracecast run` | Ingestion 按窗口发送所选 source 的原文 pieces，因此应按“所选 raw 文本可能全部发送”处理；后续阶段还会发送由材料生成的候选、编辑和正文投影，以及所选 Host 的阶段投影。 |
| `name_table` 专名方式 | 在普通 Run 调用之外，把完整 selected factual materials 去重后放入一次专名请求；`overrides_only` 不产生这次调用。 |
| `tracecast host-build` | 第一次调用发送按 source 平衡的偏好文本样本，第二次发送入选的 style-example 样本；程序在本地读取和切片全文，但不会把全文作为一个整体上传。 |

所有调用还会发送 route 选择的 system/user prompt、model、temperature、输出上限和允许的
`extra_body`。API key 只在发送请求时进入 `Authorization: Bearer ...` header；Tracecast 不把 key
值写入请求 envelope、call record 或本地 raw payload。

不要把“抽样”理解为匿名化。样本仍可能包含姓名、内部事实、版权文本或其他敏感内容。

## 本地会保留什么

Tracecast 为可追踪性保留以下文件：

- `configs/runtime.json`：gateway、模型和环境变量名，不保存 API key 值；
- `.env`：Console 首次设置写入的 API key；该文件必须保持本地且不得提交；
- `projects/<id>/raw/`：确认后的无 BOM UTF-8 正文；
- `projects/<id>/imports/original/`：只有发生转码时才保存上传的原始字节；
- `runs/<run-id>/`：阶段 artifact、报告、最终脚本和调用账本；
- `runs/<run-id>/llm/raw/`：每次 provider attempt 的完整 canonical request envelope 和 raw
  response，包括 prompt 中的材料内容；
- Host Builder Run：输入路径与 hash、抽样 span、已选样本、模型请求/响应和编译结果。偏好全文
  不复制进 manifest，但原始输入仍保留在你提供的位置。

Transport retry 的每个 attempt 都使用新的 call ID，并分别保存 request、response 和 ledger row。
Tracecast 不自动清理项目或 Run。删除前先确认是否仍需要审计记录；分享或备份 `runs/` 时，应把它
视为包含原始材料片段和模型响应的敏感数据。

## 成本口径

provider 调用可能收费。小切片、Intake 抽样和 Host Builder 抽样只能减少输入量，不能保证一次调用
或固定费用。Transport retry 也可能形成多个 provider attempt；即使本地最终显示失败，已经到达
provider 的 attempt 仍可能计费。

Tracecast 的 `LlmCallRecord.cost_kind` 是 `configured_estimate`。估算使用 provider response 报告的
token usage 和操作者在 runtime 中填写的费率：

- 不内置 provider 价格；
- 未配置费率时，Console 显示 `unconfigured`，不把 `0.0` 当作实际零费用；
- provider 没有返回 usage 时，Tracecast 不补造 token 数；
- configured estimate 不是 invoice，最终费用以 provider 账单为准。

在 Console 中，自动 Intake 会先用实际抽样和 batching 规则生成无调用估算；手动 source profile
方式调用数为零。完整 Run 或派生操作也应在确认页面检查 call 数、模型和费率状态后再执行。

## Console 安全边界

Console 没有用户认证。默认命令只监听 `127.0.0.1:8765`。

PowerShell：

```powershell
.\.venv\Scripts\python.exe -m tracecast console
```

Bash/zsh：

```bash
./.venv/bin/python -m tracecast console
```

不要使用 `--host 0.0.0.0`、局域网地址或公网地址。CLI 在非 loopback 地址上启动时只打印警告，
不会阻止启动。首次设置的 secret 写接口要求 loopback client 和同源浏览器请求，但这不是通用认证，
不能保护一个已经暴露到其他机器的 Console。

还应做到：

- 不在共享机器上让不受信任用户访问 Console 进程、`.env`、`projects/` 或 `runs/`；
- 不把含 secret 的终端输出、浏览器截图或配置文件加入 issue；
- 使用独立、可撤销且权限最小的 provider key；
- 共享脚本前检查正文、artifact、raw request/response 和 Host，而不只检查最终 `script.md`。

## 材料公开责任

仓库的示例配置和示例素材只使用占位内容。你导入的 raw 来源和偏好文本可能受版权、保密协议、
个人信息或 provider 使用条款约束。Tracecast 不判断这些材料是否可以上传或公开；操作者必须在调用、
提交、备份或分享之前完成授权与脱敏检查。
