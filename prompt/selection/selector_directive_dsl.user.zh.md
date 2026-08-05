<!-- version: selector_directive_dsl_user@v1 -->
## persona

{{persona_block}}

## locked path

下面是完整且不可修改的最终路径。`Pxxx` 只在本次调用内有效；行序就是最终呈现顺序。

```text
P <path_alias> | role=<locked_role> | treatment=<locked_treatment>
SUM <node summary>
```

{{path_blocks}}

## output DSL

为每个输入 alias 恰好输出一条：

```text
D <path_alias> | <directive_or_->
```

- alias 必须逐字复用，不能新增、遗漏或重复。
- 不能输出 path、role、treatment、SUM、理由或正文。
- directive 不能换行或包含 `|`。
- 第一条负责播客开场，最后一条负责收束；中间各条从完整 path 判断承接与轻重。

只输出裸 D 行。
