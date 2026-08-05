<!-- version: node_edit_block_repair_user.en@v2 -->
# Current single-node repair

The program classified the previous failure as:

{{initial_failure}}

The failure feedback only identifies what to recheck. It supplies no alias from the previous call and authorizes no material fact. This is the node's only repair call.

## Current protocol and input

The following is a complete single-node request rendered from the main user template. Only aliases in this projection are valid. Output one complete `NODE ... ENDNODE` block from the beginning; do not continue or patch the old output.

{{node_edit_prompt}}

## Check before output

Every current material must appear in exactly one of K, C, or O. Output only bare DSL, with no explanation or code fence.
