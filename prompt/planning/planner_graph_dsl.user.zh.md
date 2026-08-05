<!-- version: planner_graph_dsl_user@v5 -->

本次允许的 material alias 范围：{{alias_range}}。范围外的 material 不属于本次输入。

## 输入示例

```text
M <alias> | src=<source_ref> | so=<source_order> | k=<content_kind> | tg=<atom tags>
TXT <材料正文>
```

- `so` 是当前来源内的原始顺序。
- `k` 与 `tg` 是辅助信号，不是新事实，也不能输出为 content_kind 。
- TXT 是保持源语言的原始材料，用于判断 node 边界并生成 SUM；不要在输出中复制长段正文。

## narrative_function

每个 N 行的 `fn` 必须逐字取自下列集合，且只能有一个主功能：

{{narrative_function_descriptions}}

评论、阐释、interpretation、comparison、review 和制作背景统一使用 `commentary`，严格取用集合，不要输出集合外的近义词。

## 输出 DSL

```text
N N1 | fn=<narrative_function> | mats=<M别名逗号或range> | <summary>
```

- 每行显式使用本次响应内唯一的 handle：`N1`、`N2`、`N3`……；它不是正式 id。
- 每行至少引用一个输入 material；同一 material 至多属于一个 N 行。
- `mats` 只能引用输入里的 M alias，支持递增 range，例如
  `M001-M006,M010-M014`；不能越过允许范围。
- 不输出额外tags、分数、置信度、自造id 或其他自定义字段。
- 只输出裸 DSL，不输出 JSON、Markdown、代码围栏、标题或解释。

## materials blocks

{{material_blocks}}

以上为全部材料。
按顺序执行：
1. 覆盖：除噪声、残片、导航、广告、元信息外，allowed range 内每个 M 都已归入某个 N。
2. 唯一：没有 M 出现在两个 N 行；mats 内无重复、无范围外 alias。
3. 次第：mats 的 range 只用于原本连续且同属一个单元的 alias；不连续时逐个列出。
4. 边界：相邻 N 之间存在可陈述的差别（时机、因果职责、编辑处理力度之一）；
   若说不出差别则合并，若节点内可能需要分别排序则分开。
5. fn：逐字取自给定集合，每行且仅有一个。
6. SUM：只对应本 node 材料支持的内容；无新增人物、数字、动机、因果、未揭示身份；
   无下游指令与主持人口吻；无长段原文照抄。
7. 形式：仅 N 行，handle 从 N1 起连续递增，无tags、置信度、id、围栏或解释。

只输出裸 DSL。
