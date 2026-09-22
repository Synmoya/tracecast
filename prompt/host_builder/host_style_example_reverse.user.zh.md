<!-- version: host_style_example_reverse_user.zh.v3 -->
# 本次调用

## 输入结构

每篇成稿位于 `EXAMPLE_INPUT <alias> | position=<opening|middle|ending>` 与 `ENDEXAMPLE_INPUT` 之间。alias 只用于分隔本次输入。`position` 表示该成稿取自整期节目的开头、中段还是结尾，只说明它在原节目中的位置，不改变反推方式。

## 输出协议

只输出下列裸 DSL，不要 Markdown、编号、解释或其他字段：

```text
EXAMPLE <alias>
SOURCE
<多行 source>
ENDSOURCE
ENDEXAMPLE
```

- 每个 alias 恰好一个 `EXAMPLE` 块，必须覆盖输入中的全部 alias。
- `SOURCE` 是该成稿的朴素输入，至少一行，可跨多行；单独一行 `ENDSOURCE` 结束材料，随后单独一行 `ENDEXAMPLE` 结束整个示例。每个块的最后两行依次是这两个结束标记。
- 不输出成稿本身，也不输出 position、说明或学习点。

## 成稿

{{examples}}

## 输出前检查

确认每个 source 保留对应成稿陈述的全部事件、没有新增事实且不与成稿逐字相同；全部 alias 恰好各出现一次；每块以 `ENDSOURCE`、`ENDEXAMPLE` 两行依次结束，最终内容只有裸 DSL。
