<!-- version: planner_node_compress_dsl_user@v5 -->

本次允许的 child nodes candidate alias 范围：{{alias_range}}。范围外的 child nodes 不属于本次输入。

## 输入示例

```text
C <alias> | m=<material数量> | sp=<source_alias:first-last>
SUM <seed层节点摘要>
```

- 同一 child block 可以有多行 `SUM`；它们共同描述该 child nodes 当前分组覆盖的已编辑层内容。

本次输入 child node 数：{{input_child_count}}。
本次最多输出 macro node 数：{{target_parent_count}}。

macro node 数是硬上限。必须覆盖全部 child，同时把输出 N 行数压到该上限以内。

## 输出 DSL

```text
N N1 | kids=<C别名逗号或range> | <macro summary>
```

- 每行显式使用本次响应内唯一的 handle：`N1`、`N2`、`N3`……；它不是正式 id。
- `kids` 只能引用输入里的 C alias，支持递增 range，例如
  `C001-C006,C010-C014`；不能越过允许范围。
- 每个输入 child 必须且只能进入一个 N 行，不得遗漏或重复。
- N 行总数必须小于或等于 `{{target_parent_count}}`。
- <macro summary> 直接填写，不要添加前缀。
- 不输出自造id、node-level tags、分数、置信度、或其他自定义字段。
- 只输出裸 DSL，不输出 JSON、Markdown、代码围栏、标题或解释。

## child candidate nodes

{{node_blocks}}

以上为全部材料。
按顺序执行：
1. 覆盖:把所有 kids 展开成 alias 列表,与 {{alias_range}} 逐一比对——无遗漏、无重复、无越界。
2. 配额:先划分好完整分组再开始输出，N 行数不超过 {{target_parent_count}}，不要把未处理的尾部 child nodes 集中塞进最后一组。
3. 顺序:每个 N 行内 kids 递增;N 行之间按各自首个 alias 升序排列;range 写法首尾均在范围内且首小于尾。
4. 语义:每条 摘要的对象、状态、行动、因果、关系均可在其 kids 的摘要中找到对应,无新增人物、数字、动机或未揭示身份。无下游指令与主持人口吻。无长段原文照抄。
5. 形态:仅裸 N 行,无 JSON、Markdown、围栏、标题、解释、自造 id 或额外字段。不输出自检本身。

只输出裸 DSL。
