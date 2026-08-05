<!-- version: atom_boundary_dsl_user@v12 -->

## piece alias

piece alias 范围：{{alias_range}}

## 允许的 content_kind

{{allowed_content_kinds}}

content_kind 说明：

{{content_kind_descriptions}}

## 允许的 tags

{{allowed_tags}}

tags 说明：

{{tag_descriptions}}

- tags 是下游辅助信号，不是事实来源，不表示重要程度。
- 只能使用允许集合中的英文标签；没有合适标签时留空。
- 不要只因某个标签在前一 atom 出现过就跟随或回避。

## 输出格式

```text
A | <piece范围> | <content_kind> | <tags>
A | <piece范围> | <content_kind> | <tags> , <tags>
```

- 只使用 user block 给出的 alias、content_kind 和 tags。
- 若有多个 tags 使用英文逗号连接。
- 连续多片写 `p0001-p0003`，单片写 `p0001`；A 行按原文顺序输出，不得回跳。
- 每个 alias 必须且只能出现一次；全部 A 行无遗漏、无重叠地覆盖本批次所有 pieces。
- 不输出正文、其他字段、JSON、Markdown、代码围栏、标题或解释。

## pieces blocks

`[NL]` 表示原文换行。材料保持源语言，本任务不产生中间翻译。

{{piece_lines}}

以上为全部材料。
按顺序执行：
1. 通读全部 piece，识别场景/时间/条件/事件/话题切换点，标出候选边界。
2. 按编辑目标合并或拆分，形成连续 atom 序列。
3. 为每个 atom 判定 content_kind，并从允许集合中选取适用 tags，无合适项则留空。
4. 输出前自检，逐项确认：
   - 覆盖：本批次每个 piece alias 是否恰好出现一次，无遗漏、无重复；
   - 顺序：A 行是否严格按原文顺序排列，无回跳；
   - 连续性：同一 atom 内的 piece 范围是否连续（如 p0001-p0003），未跨越已拆分的边界；
   - 合法性：content_kind 和 tags 是否均取自允许集合。
   - 格式：每行是否符合 `A | <piece范围> | <content_kind> | <tags>` 结构，无多余字段或说明文字。
5. 自检通过后，只输出裸 A 行，不输出检查过程自身。

只输出裸 A 行：
