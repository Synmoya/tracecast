<!-- version: planner_node_compress_dsl_user.en@v5 -->

Allowed child-node candidate alias range for this call: {{alias_range}}. Child nodes outside this range do not belong to this input.

## Input example

```text
C <alias> | m=<material_count> | sp=<source_alias:first-last>
SUM <seed-level node summary>
```

- One child block may contain multiple `SUM` rows; together they describe the edited-layer content covered by the child's current grouping.

Input child-node count: {{input_child_count}}.
Maximum output macro-node count: {{target_parent_count}}.

The macro-node count is a hard upper bound. Cover every child and reduce the output N-row count to this limit or lower.

## Output DSL

```text
N N1 | kids=<comma-separated C aliases or ranges> | <macro summary>
```

- Give each row a unique response-local handle: `N1`, `N2`, `N3`, and so on. It is not a formal id.
- `kids` may reference only input C aliases and supports increasing ranges such as `C001-C006,C010-C014`; do not exceed the allowed range.
- Every input child must belong to exactly one N row, with no omission or duplication.
- The number of N rows must be less than or equal to `{{target_parent_count}}`.
- Write `<macro summary>` directly, without adding a prefix.
- Do not output invented ids, node-level tags, scores, confidence values, or custom fields.
- Output bare DSL only, without JSON, Markdown, code fences, headings, or explanations.

## Child candidate nodes

{{node_blocks}}

The material above is complete.
Proceed in order:
1. Coverage: expand all `kids` to alias lists and compare them one by one with {{alias_range}}; no omissions, duplicates, or out-of-range aliases.
2. Quota: plan the complete grouping before output; use no more than {{target_parent_count}} N rows and do not dump unprocessed tail children into the last group.
3. Order: `kids` increase within each N row; N rows are ordered by their first alias; every range endpoint is in bounds and the start is less than the end.
4. Semantics: every subject, state, action, cause, and relation in a summary is supported by its children's summaries; add no people, numbers, motives, or unrevealed identities. Include no downstream instructions, host voice, or long copied passages.
5. Shape: output only bare N rows, with no JSON, Markdown, fences, headings, explanations, invented ids, or extra fields. Do not output the check itself.

Output only bare DSL.
