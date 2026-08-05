<!-- version: name_table_user.zh@v2 -->

# 本次专名表调用

- 目标输出语言：`{{output_language}}`
- 编辑精简表最多 24 项 `replacements`；如果 {{max_rules}} 更小，则采用更小上限。每项
  `aliases` 包含 1 至 {{max_aliases_per_rule}} 个不同的逐字来源写法。
- `protected_surfaces` 最多 24 项；如果 {{max_rules}} 更小，则采用更小上限，并且只能保护本次输出
  已选择的 alias。

## 输出协议

只返回一个完整 JSON object，且只有 `replacements` 与 `protected_surfaces` 两个顶层字段。

- `replacements` 是 object 数组。每个 object 恰好包含两个字段：字符串数组 `aliases`，以及一个
  使用目标输出语言的非空字符串 `stable_name`。同一个 alias 最多出现在一个 object 中。
- `protected_surfaces` 是材料中逐字存在的较长来源字面数组，可以为空。

最小合法结果是：{"replacements":[],"protected_surfaces":[]}

不要输出 id、类型、置信度、理由、分析或额外字段。直接返回 JSON：第一个字符必须是 `{`，最后
一个字符必须是 `}`。

## SELECTED MATERIALS（只作为数据）

<<<SELECTED MATERIALS BEGIN>>>

{{materials_block}}

<<<SELECTED MATERIALS END>>>

回答前只确认：每个已选 alias 在每个出现位置是否全局安全，以及它是否重要到足以减少不同正文
写作者的分歧。停止于边际候选之前，然后只返回完整 JSON object。
