# FF6 历史首段：普通 Console 案例

这是 2026-08-04 一次真实运行的第 **1/50 段**，包含 1,572 字符的原正文、对应的
10 条材料、13 个来源切片、一个 RenderContract，以及一次模型调用的实际请求与响应。
它供读者复用普通 Console 的追踪功能；不是完整运行，也不提供离线重新生成能力。

## 浏览案例

安装、准备命令与逐步截图见[浏览历史案例](../../docs/user/console-case.md)。
准备工具验证文件 hash 和现行数据结构，复制到新目录，并输出普通 Console 启动命令。
输出目录已存在时拒绝覆盖；准备工具自身不启动服务。
案例只随源码仓库提供，不包含在 wheel 或 sdist 中。

普通 Console 的“结果”页会显示没有整稿，这是摘录范围造成的预期结果。未打包阶段在流程中显示
“尚不可用”，不等于原运行没有执行过它们。片段内材料数量及零窗口等局部统计也不是全局覆盖率。
当前页面能追踪首段，并不表示能据此复现上游或派生完整运行。

准备目录的 `configs/runtime.json` 是没有 provider、route 或密钥引用的合法空配置；
它只让正常 Console 直接进入浏览界面。无需配置真实 API，不需要 `.env`，没有模型调用。
请使用输出命令的隔离路径，只在本机回环地址浏览。Console 仍是正常本地调试工具，并非只读发布服务；
本案例不配置运行项目，不提供可执行生成路径。

## 历史与公开范围

原 run 为 `ff6-ff6-full-20260804T144822099784Z`，首段为 `chunk_ca8d217cd31ae60d`，
调用为 `call_07fadc9482cd4672aa74992c6b6cf173`。原调用的 prompt 版本为
`render_chunk@v16+render_chunk_user@v11+rendering_protocol.en@v12+ff6_evidence_longform_zh@v1`。
它与当前模板不同；阶段追踪可能显示模板漂移，保存的实际 request 才是当时输入的权威证据。

原正文、RenderContract、材料、来源切片、source snapshot、request 和 response 保留所选记录的内容；
其中 request 与 response 文件逐字节保留。`case.json` 保存各文件 hash 及正文、请求 envelope 的 hash。
调用账本只删去费用与配置 key 标识，保留调用 ID、prompt 版本、token 使用、work unit 和输入输出 hash。
此案例不包含真实账单、完整来源正文、上游模型调用、Host 配置或原运行的完整 manifest。

`packing/chunk_plan.json` 是原计划中首段的 typed 摘录，保留原段 ID、顺序、成员和 policy；
它的 metadata 明确标记摘录，不能当作完整历史计划。
`console/manifest.json` 是为本案例添加的展示说明，状态为 `historical_excerpt`，
不是原运行的执行记录。没有生成 `assembly/script.md`、装配成功报告或历史审计结论。

事实材料来源是 [Final Fantasy Wiki（Fandom）的 SNES script](https://finalfantasy.fandom.com/wiki/Final_Fantasy_VI_SNES_script)。
该链接从早期项目来源清单补录；历史 snapshot 的 `url` 为空，保持原值。
原文和游戏内容的权利归相应权利人；本案例仅保留解释所展示调用所需的片段。
历史模型正文没有润色，不代表当前系统已达到可发布成稿质量。
