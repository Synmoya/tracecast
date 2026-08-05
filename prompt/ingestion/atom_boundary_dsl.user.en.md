<!-- version: atom_boundary_dsl_user.en@v12 -->

## Piece aliases

Piece alias range: {{alias_range}}

## Allowed content_kind values

{{allowed_content_kinds}}

`content_kind` descriptions:

{{content_kind_descriptions}}

## Allowed tags

{{allowed_tags}}

Tag descriptions:

{{tag_descriptions}}

- Tags are downstream auxiliary signals, not factual sources or measures of importance.
- Use only English labels from the allowed set; leave the field empty when no tag applies.
- Do not copy or avoid a tag merely because it appeared in the preceding atom.

## Output format

```text
A | <piece_range> | <content_kind> | <tags>
A | <piece_range> | <content_kind> | <tags> , <tags>
```

- Use only aliases, `content_kind` values, and tags supplied in this user block.
- Separate multiple tags with ASCII commas.
- Write a continuous multi-piece range as `p0001-p0003` and a single piece as `p0001`. Output A rows in source order without jumping backward.
- Every alias must appear exactly once. Together, all A rows must cover every piece in this batch without gaps or overlaps.
- Do not output source text, other fields, JSON, Markdown, code fences, headings, or explanations.

## Piece blocks

`[NL]` represents a line break in the source text. Keep the material in its source language; this task does not require an intermediate translation.

{{piece_lines}}

The material above is complete.
Proceed in order:
1. Read every piece and identify shifts in scene, time, condition, event, or topic as candidate boundaries.
2. Merge or split according to the editing objective to form a continuous atom sequence.
3. Assign one `content_kind` to each atom and select applicable tags from the allowed set, or leave tags empty.
4. Before output, check each item:
   - Coverage: does every piece alias appear exactly once, with no omissions or duplicates?
   - Order: are A rows strictly in source order, with no backward jump?
   - Continuity: is every atom's piece range continuous, such as `p0001-p0003`, without crossing a boundary already split?
   - Validity: do all `content_kind` values and tags come from the allowed sets?
   - Format: does every row match `A | <piece_range> | <content_kind> | <tags>` with no extra fields or explanatory text?
5. After the check passes, output only bare A rows, not the check itself.

Output only bare A rows:
