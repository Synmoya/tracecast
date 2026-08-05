<!-- version: selector_path_dsl_user@v11 -->
## 任务规模（软参照）

量级参照，不是硬性字数指令，也不逐节点分配额度。

{{source_token_estimate_line}}
{{target_output_estimate_line}}

规模作用于最终编辑路径：它影响候选取舍、role / treatment 与展开力度，但不是固定入选比例、
硬性字数 gate 或逐节点额度。

## persona / feedback

{{persona_block}}

- viewpoint / prefer / avoid 是总编的编辑倾向，不是逐项勾选的要求。让它们实际改变 path、轻重和问题意识。
- human_directive 是 operator 对本轮路径的明确控制。候选图有支持时，必须落实到 node 的入选/排除、行序或轻重，不能留给后续 directive task 代替；若明确列出多个对象，逐项匹配相应 node，不能用一个相邻主题或概括 node 代替。
- expression 在这里只提供轻参考，最终句式、声音和节点 directive 由后续任务完成。
- 明确要求纳入的内容在候选图有支持时必须进入；avoid 可以降低权重或排除。prefer 会抬高相关
  候选的编辑价值，但不要求把其余所有合格 node 一并收入。

## source aliases

`sp=<source_alias:first-last>` 是只读来源位置：source alias 区分来源，first-last 是该来源内的 source_order 范围。它帮助你判断原文次第和距离，不是呈现顺序约束。

{{source_aliases}}

## 输入图例

每个 candidate node 一块（代码围栏仅为排版，与你的输出无关）：

```text
C <alias> | fn=<narrative_function> | m=<material数量> | mt=<material估算token> | sp=<source_alias:first-last>
SUM <节点摘要>
E <edge_type> | <role>=C<xxx> | <role>=C<xxx> | <reason>
```

- m 是该节点挂载的 material 数量。m 越大，下游可展开的材料越多，可作 treatment 力度的参考。
- mt 是该节点覆盖的原始 `MaterialAtom.content` 估算 token 总数，用于理解下游材料池体量。
  它不是入选分数、质量权重或逐节点额度；mt 大不表示必须入选，mt 小也不表示应当舍弃。
- E 行是已有关系，只供理解，不是 path 约束，也不授权拉取未入选节点的材料。方向边跟在起点节点块后（如 setup / cause），对称边跟在较早端点后，因此不要假设 E 行只集中出现在输入末尾。

{{narrative_function_descriptions}}

## 输出 DSL

只输出 accepted 的 P 行，行序即最终呈现顺序：

```text
P <alias> | <role> | <treatment_code>
```

语法规则：

- 只能引用输入里给出的 C alias，不能发明新节点。
- 不要输出未入选节点，程序会补 `not_selected`。
- 不要输出正式 id、分数、置信度、自定义字段或 JSON。
- 行序即呈现顺序，可以顺讲、跳讲、拆开、倒接。`sp` 只是只读参照。
- 每个 accepted node 都输出一条以 `P ` 开头的物理行，即使路径很长也不能省略后续行的 `P `。
- 每个 alias 最多出现一次；需要修订时替换原行，不能在末尾追加同 alias 的第二条修正版。
- 只输出以上三个字段；不要输出 directive、理由、SUM 或正文。

**role**（叙述功能，只能逐字使用以下四项）

- `core`：不讲就无法理解状态、因果、身份、目标或 reveal。
- `bridge`：只交代结果以维持连续。
- `context`：世界、机制、背景或观点支撑。
- `color`：改善听感、人物质感或评论层，省略后不影响事实与理解闭环。只有具备明确听觉回报的
  node 才使用；不能把主线暂时用不上的边缘 node 默认放进这里。

`fn=state_change`、`commentary` 等 narrative_function 值不是 role，不能写进 role 字段。

**treatment_code**（压缩强度，只输出一个小写字母）

- `q`：quote。原文价值高的软信号，提示下游尽量保留该节点原文的表达或金句。你只凭 SUM 判断，最终由下游定夺。
- `p`：paraphrase。正常中文叙述。
- `s`：summarize。这个 node 值得出现，但只压缩到结果或功能；不是让低贡献 node 勉强入选的理由。

role 与 treatment 相互独立，任意组合合法。bridge 常配 `s`，但 persona 认为值得展开的过场可以 bridge + `p` 甚至 `q`；core 也可以因节奏需要 `s`。

## candidate graph

{{graph_blocks}}

以上为全部候选。按顺序执行：

1. 逐个 node 做资格判断，得到可供编辑选择的候选池。
2. 播演整期节目，按主问题、边际贡献、连续性和软规模选择真正进入 path 的 node。
3. 反向做一次连续性审计：保留理解冲突所需的最小前提；所有后续重逢、背叛、兑现和终局
   行动都有最小首现 / setup 与必要因果。只补叙事关节，不把合格候选全部收回；发现漏项时插回
   正确位置并从头重排，绝不在终局后追加补漏。
4. 确定行序与每个入选 node 的 role / treatment。逐条对 `color + summarize` 做删除测试：若删掉
   后只少知识、玩法提示或混杂清单，没有失去听觉回报，就舍弃。
5. 从第一行到最后一行重新播演：首次出现早于重逢/回收，原因早于结果，终局高潮只出现一次，
   其后只有余波、回望或结语；每个 alias 恰好一次。
6. 检查每行 role 只来自四项词表、treatment_code 只来自 `q/p/s`，然后按最终呈现顺序输出裸 P 行。
