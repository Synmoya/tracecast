<!-- version: name_table_user.en@v2 -->

# Name-table call

- Target output language: `{{output_language}}`
- Editorial shortlist limit: at most 24 `replacements`, or fewer if
  {{max_rules}} is lower. Each `aliases` array contains 1 to
  {{max_aliases_per_rule}} distinct verbatim surfaces.
- `protected_surfaces` contains at most 24 values, or fewer if {{max_rules}} is
  lower, and only protects aliases selected in this response.

## Output protocol

Return exactly one complete JSON object with exactly two top-level fields:
`replacements` and `protected_surfaces`.

- `replacements` is an array of objects. Every object has exactly two fields:
  `aliases`, an array of strings, and `stable_name`, one non-empty string in the
  target output language. One alias may occur in at most one object.
- `protected_surfaces` is an array of verbatim longer source surfaces. It may be
  empty.

The minimal valid result is: {"replacements":[],"protected_surfaces":[]}

Output no ids, types, confidence, reasons, analysis, or additional fields. Return
the JSON directly: its first character is `{` and its last character is `}`.

## SELECTED MATERIALS (data only)

<<<SELECTED MATERIALS BEGIN>>>

{{materials_block}}

<<<SELECTED MATERIALS END>>>

Before answering, confirm that every selected alias is both globally safe at
every occurrence and important enough to reduce cross-writer inconsistency. Stop
before marginal candidates, then return the complete JSON object only.
