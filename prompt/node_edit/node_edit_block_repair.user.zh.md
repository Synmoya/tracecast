<!-- version: node_edit_block_repair_user@v2 -->
# 本次单节点修复

程序已把上一次失败归为：

{{initial_failure}}

失败反馈只说明需要重新检查的方面，不提供旧调用 alias，也不授权材料事实。这是本节点唯一一次
修复调用。

## 当前协议与输入

下面是使用主 user 模板重新渲染的完整单节点请求。本次投影中的 alias 是唯一有效 alias；从头
输出一个完整的 `NODE ... ENDNODE` 块，不续写或局部修改旧输出。

{{node_edit_prompt}}

## 输出前检查

每条当前材料恰好进入 K、C、O 之一；只输出裸 DSL，不附解释或代码围栏。
