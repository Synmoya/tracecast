<!-- version: host_profile_distill_user.zh.v2 -->
# 本次调用

## 输入结构

每个样本位于 `SAMPLE <alias> | position=<opening|middle|ending>` 与 `ENDSAMPLE` 之间。alias 只用于分隔本次输入；样本文字是外部偏好成稿，不是当前项目材料。

## 输出协议

只输出下列裸 DSL，每个值占一行，不要 Markdown、编号、解释或其他字段：

```text
OUTPUT_LANGUAGE <bcp47>
VIEWPOINT <text>
PREFER <text>
AVOID <text>
THINKING_RULE <text>
EXPRESSION <text>
EXPRESSION_RULE <text>
```

- `OUTPUT_LANGUAGE` 使用最符合样本主体语言的 BCP-47 tag，恰好一行。
- `VIEWPOINT` 是主持人观察和组织材料的总体方式，恰好一行。
- `EXPRESSION` 是整体声音、节奏、过渡或情绪表达的描述片段，至少一行；可输出多个互补片段，程序会按原顺序合并。
- `PREFER`、`AVOID`、`THINKING_RULE`、`EXPRESSION_RULE` 可重复或省略。`EXPRESSION_RULE` 应是正文生成时可直接执行的稳定表达规则。

最小形状示例；尖括号表示必须替换的占位符：

```text
OUTPUT_LANGUAGE <bcp47>
VIEWPOINT <总体观察与组织方式>
EXPRESSION <整体表达特征>
```

## 样本

{{samples}}

## 输出前检查

只保留能由多个样本支持的稳定特征；确认两个唯一字段和至少一个 `EXPRESSION` 均已输出；最终内容只有裸 DSL。
