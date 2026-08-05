<!-- version: render_fact_context_user.en@v13 -->

Target output language: {{output_language}}

{{node_outline}}

The nodes above are complete.

Before outputting any HINT, strictly check each item:

1. Is the current NODE for this HINT in the second or a later CHUNK, and does the comparison come from an earlier CHUNK rather than the current one?
2. Do the current and earlier NODE identify the same specific content or narrative action rather than merely related content, continuation, question/answer, or setup?
3. Does the HINT include only what both nodes genuinely share, without consequences, evidence, angles, or actions newly added by the current NODE?
4. Is every HINT brief without losing essential information?
5. If one NODE has multiple HINT rows, does each have a sufficiently strong semantic relation, with a strict total of no more than 3?

Omit any HINT that cannot clearly satisfy every item. Do not force a summary for a node, pursue coverage, or output when uncertain.

Output only zero or more rows:

HINT CxxxNxxx | content or narrative action shared with an earlier CHUNK's plan (do not leak aliases)
