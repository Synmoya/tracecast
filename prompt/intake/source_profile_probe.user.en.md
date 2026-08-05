<!-- version: source_profile_probe_user.en@v1 -->
## Profile catalog

{{profile_catalog}}

## Source probes

{{source_probe_blocks}}

## Output DSL

For every input `SRC`, output exactly one block:

```text
SRC <alias> | profile=<profile_key_or_unknown> | language=<en|zh|unknown> | status=<ready|needs_review>
TITLE <short source title>
NOTE <short operator-facing source note>
REASON <short reason for this profile decision>
ENDSRC
```

Rules:
- Keep the same `SRC` alias from the input.
- `status=ready` requires a catalog profile and a non-`unknown` language.
- Use `profile=unknown | status=needs_review` for source types not covered by the catalog.
- Do not output `source_id`, `content_hash`, `source_class`, `trust_tier`, or JSON; code will compile those fields.
- `TITLE`, `NOTE`, and `REASON` must not contain `|`.

Now output only the DSL blocks.
