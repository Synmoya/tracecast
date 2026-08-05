<!-- version: source_profile_probe_user@v1 -->
## profile catalog

{{profile_catalog}}

## source probes

{{source_probe_blocks}}

## output DSL

For every input SRC, output exactly one block:

```text
SRC <alias> | profile=<profile_key_or_unknown> | language=<en|zh|unknown> | status=<ready|needs_review>
TITLE <short source title>
NOTE <short operator-facing source note>
REASON <short reason for this profile decision>
ENDSRC
```

Rules:
- Keep the same SRC alias from input.
- `status=ready` requires a catalog profile and a non-unknown language.
- Use `profile=unknown | status=needs_review` for source types not covered by the catalog.
- Do not output source_id, content_hash, source_class, trust_tier, or JSON; code will compile those.
- TITLE / NOTE / REASON must not contain `|`.

Now output only the DSL blocks.
