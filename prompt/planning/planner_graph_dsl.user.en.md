<!-- version: planner_graph_dsl_user.en@v5 -->

Allowed material alias range for this call: {{alias_range}}. Materials outside this range do not belong to this input.

## Input example

```text
M <alias> | src=<source_ref> | so=<source_order> | k=<content_kind> | tg=<atom tags>
TXT <material text>
```

- `so` is original order within the current source.
- `k` and `tg` are auxiliary signals, not new facts, and must not be output as `content_kind`.
- `TXT` is original material used to determine node boundaries and create `SUM`. Keep it in the source language; do not copy long passages into the output.

## narrative_function

Each N row's `fn` must be copied exactly from the following set and contain exactly one primary function:

{{narrative_function_descriptions}}

Use `commentary` for commentary, interpretation, comparison, reviews, and production context. Use the supplied literals exactly; do not output synonyms outside the set.

## Output DSL

```text
N N1 | fn=<narrative_function> | mats=<comma-separated M aliases or ranges> | <summary>
```

- Give each row a unique response-local handle: `N1`, `N2`, `N3`, and so on. It is not a formal id.
- Each row must reference at least one input material. A material may belong to at most one N row.
- `mats` may reference only input M aliases and supports increasing ranges such as `M001-M006,M010-M014`; do not exceed the allowed range.
- Do not output extra tags, scores, confidence values, invented ids, or custom fields.
- Output bare DSL only, without JSON, Markdown, code fences, headings, or explanations.

## Material blocks

{{material_blocks}}

The material above is complete.
Proceed in order:
1. Coverage: every M in the allowed range except noise, fragments, navigation, advertisements, or metadata belongs to an N row.
2. Uniqueness: no M appears in two N rows; `mats` contains no duplicate or out-of-range aliases.
3. Order: use ranges only for aliases that were originally continuous and form one unit; list discontinuous aliases individually.
4. Boundaries: adjacent N rows have a describable difference in timing, causal duty, or editorial treatment. Merge if no difference exists; split if the node may need separate ordering.
5. `fn`: copy exactly one value from the supplied set for every row.
6. `SUM`: include only content supported by this node; add no people, numbers, motives, causes, or unrevealed identities; include no downstream instructions, host voice, or long copied passages.
7. Shape: output only N rows with handles increasing continuously from `N1`, and no tags, confidence, ids, fences, or explanation.

Output only bare DSL.
