<!-- version: selector_path_dsl_user.en@v11 -->
## Task scale (soft reference)

These are scale references, not a hard length instruction or per-node allocation.

{{source_token_estimate_line}}
{{target_output_estimate_line}}

Scale affects the final editorial path: it informs candidate selection, `role` / `treatment`, and expansion level, but is not a fixed selected ratio, hard length gate, or per-node budget.

## Persona / feedback

{{persona_block}}

- `viewpoint`, `prefer`, and `avoid` are editorial tendencies, not checklist items. Make them actually change the path, weighting, and guiding question.
- `human_directive` is the operator's explicit path control for this run. When the candidate graph supports it, apply it through node inclusion/exclusion, order, or weighting rather than leaving it for the later directive task. If it explicitly names multiple subjects, match each to its corresponding node; one adjacent topic or broad node cannot substitute for all of them.
- `expression` is only a light reference here. The later task handles final sentences, voice, and node directives.
- Content explicitly requested for inclusion must enter when supported by the graph. `avoid` may reduce weight or exclude. `prefer` raises related candidates' editorial value but does not require selecting every other eligible node.

## Source aliases

`sp=<source_alias:first-last>` is a read-only source position: the source alias separates sources, and `first-last` is a `source_order` range within that source. It helps interpret original order and distance but does not constrain presentation order.

{{source_aliases}}

## Input legend

Each candidate node is one block. The code fence is for layout only and is unrelated to your output:

```text
C <alias> | fn=<narrative_function> | m=<material_count> | mt=<estimated_material_tokens> | sp=<source_alias:first-last>
SUM <node summary>
E <edge_type> | <role>=C<xxx> | <role>=C<xxx> | <reason>
```

- `m` is the number of materials attached to the node. More materials may permit more downstream expansion and can inform treatment strength.
- `mt` is the estimated token count of the original `MaterialAtom.content` covered by the node. It describes downstream pool size, not an inclusion score, quality weight, or per-node budget. A large `mt` does not require inclusion, and a small one does not require exclusion.
- An E row is an existing relationship for understanding only. It neither constrains the path nor authorizes material from excluded nodes. Directed edges follow their starting-node block, such as `setup` or `cause`; symmetric edges follow the earlier endpoint, so do not assume E rows appear only at the end.

{{narrative_function_descriptions}}

## Output DSL

Output only accepted P rows. Row order is final presentation order:

```text
P <alias> | <role> | <treatment_code>
```

Syntax rules:

- Reference only C aliases from the input; do not invent nodes.
- Do not output excluded nodes; the program will add `not_selected`.
- Do not output formal ids, scores, confidence, custom fields, or JSON.
- Row order is presentation order and may follow, skip, split, or reverse source order. `sp` is only a read-only reference.
- Every accepted node gets one physical row beginning with `P `, even on a long path; never omit `P ` from later rows.
- Use each alias at most once. To revise, replace the original row instead of appending a duplicate correction.
- Output only the three fields above; do not output directives, reasons, `SUM`, or body prose.

**role** (narrative function; copy exactly one of these four literals)

- `core`: without it, state, cause, identity, objective, or reveal cannot be understood.
- `bridge`: preserve continuity by stating only the result.
- `context`: support for the world, mechanism, background, or viewpoint.
- `color`: improves listening texture, character detail, or commentary and can be omitted without breaking facts or comprehension. Use only for a node with a clear listening payoff, not as a default home for an edge node unused by the main line.

Values such as `fn=state_change` and `commentary` are `narrative_function` values, not roles, and cannot appear in the `role` field.

**treatment_code** (compression strength; output one lowercase letter)

- `q`: `quote`. A soft signal that the original wording has value and downstream stages should try to retain the node's language or quotable line. You judge only from `SUM`; downstream stages make the final decision.
- `p`: `paraphrase`. Normal narrative treatment.
- `s`: `summarize`. The node deserves to appear, but only as a compressed result or function; it cannot justify admitting a low-contribution node.

`role` and `treatment` are independent, and any combination is valid. A bridge often uses `s`, but a transition worth expanding for the persona may use bridge + `p` or even `q`; a core may use `s` when rhythm requires it.

## Candidate graph

{{graph_blocks}}

The candidates above are complete. Proceed in order:

1. Judge every node's eligibility to form the editorial candidate pool.
2. Mentally play the complete episode; select the nodes that enter the path by central question, marginal contribution, continuity, and soft scale.
3. Audit continuity backward: retain the minimum premise needed for the conflict and the minimum first appearance/setup and causes needed for later reunions, betrayals, fulfillments, and final actions. Restore only narrative joints, not every eligible candidate. Insert any omission at its correct position and reorder from the beginning; never append it after the finale.
4. Set row order and `role` / `treatment` for each accepted node. Apply the deletion test to every `color + summarize`: if removing it loses only a factoid, gameplay hint, or mixed list and no listening payoff, exclude it.
5. Play the path again from first row to last: first appearances precede reunions or recoveries, causes precede results, the final climax appears once, and only aftermath, reflection, or closing follows it. Every alias appears once.
6. Confirm every `role` comes from the four-item vocabulary and every `treatment_code` from `q/p/s`, then output bare P rows in final presentation order.
