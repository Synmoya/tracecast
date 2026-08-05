<!-- version: host_profile_distill_user.en.v2 -->
# Current call

## Input structure

Each sample appears between `SAMPLE <alias> | position=<opening|middle|ending>` and `ENDSAMPLE`. The alias only separates inputs in this call. The sample text is an external preferred finished script, not material from the current project.

## Output protocol

Output only the following bare DSL, one value per line. Do not output Markdown, numbering, explanations, or any other fields:

```text
OUTPUT_LANGUAGE <bcp47>
VIEWPOINT <text>
PREFER <text>
AVOID <text>
THINKING_RULE <text>
EXPRESSION <text>
EXPRESSION_RULE <text>
```

- `OUTPUT_LANGUAGE` must be the BCP-47 tag that best matches the samples' primary language, exactly once.
- `VIEWPOINT` is the host's overall way of observing and organizing material, exactly once.
- `EXPRESSION` is a descriptive fragment about overall voice, rhythm, transitions, or emotional expression, at least once. You may output multiple complementary fragments; the program will merge them in their original order.
- `PREFER`, `AVOID`, `THINKING_RULE`, and `EXPRESSION_RULE` may repeat or be omitted. An `EXPRESSION_RULE` should be a stable rule that body generation can execute directly.

Minimum shape; angle brackets denote placeholders that must be replaced:

```text
OUTPUT_LANGUAGE <bcp47>
VIEWPOINT <overall way of observing and organizing>
EXPRESSION <overall expressive qualities>
```

## Samples

{{samples}}

## Check before output

Keep only stable traits supported by multiple samples; confirm that both unique fields and at least one `EXPRESSION` are present; output only the bare DSL.
