<!-- version: atom_boundary_dsl_repair@v3 -->
程序已确认上一次输出的每条 A 行字段都符合协议，但这些行没有形成完整、无重叠的 partition。当前 user block 只包含一个连续的失败块；块外已通过的 A 行保持锁定。

原 window 的失败原因：
{{violations}}

本次局部块允许的 piece alias 范围：{{alias_range}}。这些 alias 是本次局部调用重新生成的局部 alias。

请重新判断当前块内的 atom 边界与离散标注，并只输出当前局部块的完整 DSL：
- 只输出当前局部块的 DSL，不要解释。
- 当前局部块内的 A 行必须是 exact partition：每个 alias 只能出现一次，不能重叠，不能缺失。
- 不要使用本批次 alias 范围之外的 alias。
- 如果同一个 piece 同时包含舞台说明和对白，只保留一条 A，选择主导 content_kind，另一个属性放进 tags 或省略。
