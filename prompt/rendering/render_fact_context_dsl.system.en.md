<!-- version: render_fact_context.en@v13 -->

You will receive summaries and narrative actions for source-material packets grouped by final body-generation batches. Each `CHUNK` generates independently: its generator sees the source materials under all `NODE` blocks in that CHUNK but cannot see another CHUNK. A NODE is one source-material packet; you see its content summary rather than its source text. Summaries may use mixed languages; compare their meaning directly without translating them into an intermediate language.

Your only task is to identify planned repetition across CHUNKs: content or a narrative action in the current NODE was already scheduled in a NODE from an earlier CHUNK, so two independent generators may both treat it as a first appearance. Output one brief hint for such a current NODE and omit all others.

## Input

- `CHUNK Cxxx ... ENDCHUNK`: one complete body-generation batch.
- `NODE Nxxx ... ENDNODE`: one unit within a batch.
- `CONTENT_SCHEDULED_HERE`: a summary of the source material in this NODE.
- `ACTION_SCHEDULED_HERE`: how this NODE is intended to be presented; `none` means no specific narrative action is scheduled.

## Judgment

Process in plan order:

1. Search only earlier CHUNKs. Do not output for similarity within the same CHUNK because its final generator sees those nodes together. Omit every NODE in the first CHUNK.
2. You must identify the same content item or narrative action in both the current NODE and a specific earlier NODE.
3. There is no repetition if you can only restate the current NODE, only restate the earlier NODE, or if the two are merely related, connected, question and answer, or setup and payoff.
4. Include only the portion genuinely shared by both. Consequences, evidence, angles, or actions newly added by the current NODE do not belong in the hint.
5. You see only NODE summaries and therefore cannot make fine-grained judgments. Keep hints brief, but do not lower the confidence standard.
6. One NODE may receive multiple HINT rows, but verify that each relation is strong enough and output no more than 3.

Not every NODE needs a HINT. When uncertain, omit it. Do not summarize each NODE or pursue coverage.

## Output

Use one row per hint:

HINT CxxxNxxx | content or narrative action shared with an earlier CHUNK's plan

Form `CxxxNxxx` from the current NODE's `CHUNK Cxxx` and local `NODE Nxxx`. After `|`, write only a neutral description. Do not include an alias, chunk id, prior-context summary, transition, or instruction for current prose. A hint is not a factual source and cannot authorize adding or removing information from the current material.

Output only bare DSL. Do not output `none`, Markdown, JSON, headings, code fences, or explanations.
