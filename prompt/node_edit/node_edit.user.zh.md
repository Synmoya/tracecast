<!-- version: node_edit_user@v5 -->

本次 candidate alias：{{candidate_alias_range}}
本次 material alias：{{material_alias_range}}

## source aliases

`sp=<source_alias:first-last>` 只表示同一来源内的局部位置，不是呈现顺序。

{{source_aliases}}

## 输入示例

```text
NODE <C> | role=<role> | treatment=<treatment> | fn=<narrative_function> | sp=<range>
DIR <Selection 根据完整 locked path 生成的节点级叙述职责；不是事实>
M <M> | so=<source_order> | k=<content_kind> | tg=<tags>
TXT <唯一可授权下游事实的来源材料>
ENDNODE
```

## 字段速查

{{field_notes}}


## 输出 DSL

```text
NODE <C>
K <M或连续区间>
C <连续M区间> | <一句审计理由>
TXT <紧凑事实记录，不是正文>
O <M或连续区间> | <一句审计理由>
ENDNODE
```

- K 不需要写 reason。
- C 必须连续，下一行必须是 TXT；可选审计理由；TXT 只关联区间内材料已有的信息，并保持源语言，不生成中间翻译。
- O 必须写简短审计理由。
- 非连续 alias 拆行；每条输入 M 必须且只能进入 K/C/O 一次。
- 只能引用当前 NODE 内的 M；不能发明 alias 或字段；不要跨 NODE 引用。

## node blocks

{{node_blocks}}

以上为全部材料。
按顺序执行：
1. 逐条过一遍 编辑中的NODE 内所有 M,对每条初步判断应归入 K/C/O,并同时确认每条 M 最终有且只有一次归属,无遗漏、无重复：
   - K：是否含有当前 K/C 无法替代的事实、关系、限定、顺序、归因或听觉贡献。
   - C：区间材料是否连续同质、可被一条 evidence note 完整覆盖而不丢限定或归因。
   - O：移除后是否确实不留理解、指代、因果、条件、场景或表达价值缺口；reason 是否指向准确。
2. 检查每个 C 区间的 TXT：是否只记录区间内材料已有信息,不混入主持口吻、转场、评价或来源没有的解释；下一行是否紧跟对应 TXT。
3. 回看整体分布：是否因为某一 node 某指令用得多/少而无意识跟随或反向平衡；若有,以 node 材料本身重新判断而非对齐前例。
4. 通读输出格式：未跨 NODE 引用、未发明 alias 或字段、C 后接 TXT、O 均带理由、K 无需理由；仅为裸 DSL,不含 JSON/Markdown/代码围栏/标题/解释。

只输出裸 DSL。
