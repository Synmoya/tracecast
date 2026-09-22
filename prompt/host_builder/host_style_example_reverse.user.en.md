<!-- version: host_style_example_reverse_user.en.v3 -->
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
- `SOURCE` is that script's plain input, at least one line and possibly several; `ENDSOURCE` on its own line closes the material, followed by `ENDEXAMPLE` on its own line to close the whole example. These two closing markers are the final two lines of every block, in that order.
- Do not output the finished script itself, the position, commentary, or a lesson.

## Finished scripts

{{examples}}

## Check before output

Confirm that every `source` preserves all events its finished script states, adds no new facts, and is not identical to that script; that every alias appears exactly once; and that each block ends with the two lines `ENDSOURCE` then `ENDEXAMPLE`, with bare DSL only in the final content.
