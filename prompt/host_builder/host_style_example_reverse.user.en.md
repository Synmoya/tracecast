<!-- version: host_style_example_reverse_user.en.v2 -->
# Current call

## Input structure

Each finished script appears between `EXAMPLE_INPUT <alias> | position=<opening|middle|ending>` and `ENDEXAMPLE_INPUT`. The alias only separates inputs in this call. `position` states whether the script came from the opening, middle, or ending of a full episode; it describes where the script sat in its original episode and does not change how you reconstruct it.

## Output protocol

Output only the following bare DSL. Do not output Markdown, numbering, explanations, or any other fields:

```text
EXAMPLE <alias>
SOURCE
<multiline source>
ENDSOURCE
ENDEXAMPLE
```

- Exactly one `EXAMPLE` block per alias, covering every alias in the input.
- `SOURCE` is that script's plain input, at least one line and possibly several.
- Do not output the finished script itself, the position, commentary, or a lesson.

## Finished scripts

{{examples}}

## Check before output

Confirm that every `source` preserves all events its finished script states and adds no new facts; that every alias appears exactly once; that no `source` is identical to its finished script; and that the final content is bare DSL only.
