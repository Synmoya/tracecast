<!-- version: atom_boundary_dsl_repair.en@v3 -->
The program confirmed that every A row in the previous output followed the field protocol, but those rows did not form a complete, non-overlapping partition. This user block contains only one continuous failed span; A rows outside this span remain locked.

Failure reason in the original window:
{{violations}}

Allowed piece alias range for this local span: {{alias_range}}. These aliases were regenerated for this local call.

Re-evaluate atom boundaries and discrete labels within this span, then output only the complete DSL for this local span:
- Output only the local span's DSL, with no explanation.
- A rows within this span must form an exact partition: every alias appears once, with no overlap or omission.
- Do not use aliases outside this call's allowed range.
- If one piece contains both stage directions and dialogue, keep it in one A row, choose the dominant `content_kind`, and place the other property in tags or omit it.
