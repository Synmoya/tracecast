<!-- version: node_edit_user@v6 -->

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

以上为全部材料。输出前检查：
1. 各 node 的 K/C 是否足以完成当前 DIR 和展开尺度，既保留必要细节、限定与归因，也实际省去
   无关枝节；所有 TXT 只依据其引用区间，不写主持口吻、转场或新增解释。
2. 每条 M 恰好归入一次 K/C/O；C 区间连续且不跨 NODE，不使用输入之外的 alias。
3. 每条 C 后紧跟 TXT，每条 O 有理由；只输出上述协议允许的裸 DSL，不输出解释。

只输出裸 DSL。
