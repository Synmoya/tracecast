<!-- version: selector_directive_dsl_user.en@v1 -->
## Persona

{{persona_block}}

## Locked path

The following path is complete and immutable. `Pxxx` is valid only in this call; row order is final presentation order.

```text
P <path_alias> | role=<locked_role> | treatment=<locked_treatment>
SUM <node summary>
```

{{path_blocks}}

## Output DSL

For every input alias, output exactly one row:

```text
D <path_alias> | <directive_or_->
```

- Copy aliases verbatim; do not add, omit, or repeat one.
- Do not output path, `role`, `treatment`, `SUM`, reasons, or body prose.
- A directive cannot contain a line break or `|`.
- The first row opens the podcast and the final row closes it. Judge connection and weight for all middle rows from the complete path.

Output only bare D rows.
