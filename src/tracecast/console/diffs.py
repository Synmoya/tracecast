"""Pure diff functions: selection paths, chunk bodies, prompts, injection blocks.

Everything here is deterministic stdlib ``difflib`` over already-loaded data --
no IO, no LLM. Selection diffs align strictly by ``candidate_id`` (stable across
reruns); ``plan_id`` is never a diff key because it folds in the ordered path and
therefore always changes with it.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping
from typing import Any

from tracecast.domain import SelectionDecision, SelectionPlan, SelectionStatus

#: Chinese sentence endings used for readable chunk-body diffs.
_SENTENCE_ENDINGS = "。！？…\n"

#: Decision fields worth reporting per-node changes for.
_DECISION_FIELDS = ("role", "treatment", "directive")


# --- text diff (chunk bodies) -----------------------------------------------


def split_sentences(text: str) -> list[str]:
    """Split Chinese prose into sentence units, keeping the ending delimiter.

    Chunk bodies rarely contain newlines, so line-based diffing degenerates;
    sentence units give the side-by-side view usable granularity.
    """
    sentences: list[str] = []
    current: list[str] = []
    for char in text:
        current.append(char)
        if char in _SENTENCE_ENDINGS:
            sentence = "".join(current).strip()
            if sentence:
                sentences.append(sentence)
            current = []
    tail = "".join(current).strip()
    if tail:
        sentences.append(tail)
    return sentences


def diff_texts(text_a: str, text_b: str) -> dict[str, Any]:
    """Sentence-level opcode diff of two chunk bodies."""
    sentences_a = split_sentences(text_a)
    sentences_b = split_sentences(text_b)
    matcher = difflib.SequenceMatcher(a=sentences_a, b=sentences_b, autojunk=False)
    ops = [
        {
            "op": tag,
            "a": sentences_a[i1:i2],
            "b": sentences_b[j1:j2],
        }
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
    ]
    return {
        "ops": ops,
        "ratio": round(matcher.ratio(), 4),
        "chars_a": len(text_a),
        "chars_b": len(text_b),
        "sentences_a": len(sentences_a),
        "sentences_b": len(sentences_b),
    }


def unified_text_diff(text_a: str, text_b: str, *, label_a: str, label_b: str) -> list[str]:
    """Line-based unified diff (for prompts and injection blocks)."""
    return list(
        difflib.unified_diff(
            text_a.splitlines(),
            text_b.splitlines(),
            fromfile=label_a,
            tofile=label_b,
            lineterm="",
        )
    )


# --- injection block / prompt diffs -----------------------------------------


def diff_blocks(
    blocks_a: Mapping[str, str],
    blocks_b: Mapping[str, str],
    *,
    label_a: str = "a",
    label_b: str = "b",
) -> list[dict[str, Any]]:
    """Per-key diff of two injection-block dicts (union of keys, sorted)."""
    result: list[dict[str, Any]] = []
    for key in sorted(set(blocks_a) | set(blocks_b)):
        value_a = blocks_a.get(key, "")
        value_b = blocks_b.get(key, "")
        changed = value_a != value_b
        entry: dict[str, Any] = {
            "key": key,
            "changed": changed,
            "in_a": key in blocks_a,
            "in_b": key in blocks_b,
        }
        if changed:
            entry["diff"] = unified_text_diff(
                value_a, value_b, label_a=f"{label_a}:{key}", label_b=f"{label_b}:{key}"
            )
        result.append(entry)
    return result


#: Request-envelope scalars worth surfacing next to a prompt diff.
_PROMPT_META_FIELDS = (
    "operation_route",
    "stage",
    "model",
    "prompt_version",
    "temperature",
    "thinking",
    "max_output_tokens",
)


def diff_prompt_requests(
    request_a: Mapping[str, Any],
    request_b: Mapping[str, Any],
    *,
    label_a: str = "a",
    label_b: str = "b",
) -> dict[str, Any]:
    """Diff two persisted raw request envelopes: per-message text + meta scalars."""
    messages_a = list(request_a.get("messages") or ())
    messages_b = list(request_b.get("messages") or ())
    message_diffs: list[dict[str, Any]] = []
    for index in range(max(len(messages_a), len(messages_b))):
        entry_a = messages_a[index] if index < len(messages_a) else {}
        entry_b = messages_b[index] if index < len(messages_b) else {}
        content_a = str(entry_a.get("content", ""))
        content_b = str(entry_b.get("content", ""))
        changed = content_a != content_b
        item: dict[str, Any] = {
            "index": index,
            "role": entry_a.get("role") or entry_b.get("role"),
            "changed": changed,
        }
        if changed:
            item["diff"] = unified_text_diff(
                content_a,
                content_b,
                label_a=f"{label_a}:messages[{index}]",
                label_b=f"{label_b}:messages[{index}]",
            )
        message_diffs.append(item)
    meta_changed = {
        field: [request_a.get(field), request_b.get(field)]
        for field in _PROMPT_META_FIELDS
        if request_a.get(field) != request_b.get(field)
    }
    return {"messages": message_diffs, "meta_changed": meta_changed}


# --- selection path diff ----------------------------------------------------


def diff_selection(
    plan_a: SelectionPlan,
    plan_b: SelectionPlan,
    *,
    labels: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Diff two selection plans by ``candidate_id``: added/removed/moved/changed.

    ``labels`` maps candidate ids to human-readable node summaries (from the
    candidate graph) so the diff view is readable without id lookups.
    """
    labels = labels or {}
    seq_a = list(plan_a.selected_sequence)
    seq_b = list(plan_b.selected_sequence)
    pos_a = {cid: idx for idx, cid in enumerate(seq_a)}
    pos_b = {cid: idx for idx, cid in enumerate(seq_b)}
    common = set(seq_a) & set(seq_b)
    decisions_a = _accepted_by_id(plan_a)
    decisions_b = _accepted_by_id(plan_b)

    added = [
        _node_entry(cid, labels, position_b=pos_b[cid], decision=decisions_b.get(cid))
        for cid in seq_b
        if cid not in common
    ]
    removed = [
        _node_entry(cid, labels, position_a=pos_a[cid], decision=decisions_a.get(cid))
        for cid in seq_a
        if cid not in common
    ]

    moved = _moved_entries(seq_a, seq_b, common, pos_a, pos_b, labels)

    changed: list[dict[str, Any]] = []
    for cid in seq_b:
        if cid not in common:
            continue
        fields = _field_changes(decisions_a.get(cid), decisions_b.get(cid))
        if fields:
            changed.append(
                {
                    "candidate_id": cid,
                    "position_a": pos_a[cid],
                    "position_b": pos_b[cid],
                    "fields": fields,
                    "summary": labels.get(cid),
                }
            )

    moved_ids = {entry["candidate_id"] for entry in moved}
    changed_ids = {entry["candidate_id"] for entry in changed}
    unchanged = len(common - moved_ids - changed_ids)

    return {
        "plan_a": _plan_summary(plan_a),
        "plan_b": _plan_summary(plan_b),
        "added": added,
        "removed": removed,
        "moved": moved,
        "changed": changed,
        "unchanged": unchanged,
        "sequence_a": seq_a,
        "sequence_b": seq_b,
    }


def _accepted_by_id(plan: SelectionPlan) -> dict[str, SelectionDecision]:
    return {
        decision.candidate_id: decision
        for decision in plan.decisions
        if decision.status is SelectionStatus.accepted
    }


def _plan_summary(plan: SelectionPlan) -> dict[str, Any]:
    return {
        "plan_id": plan.plan_id,
        "project_id": plan.project_id,
        "persona_id": plan.persona_id,
        "selected": len(plan.selected_sequence),
    }


def _node_entry(
    candidate_id: str,
    labels: Mapping[str, str],
    *,
    position_a: int | None = None,
    position_b: int | None = None,
    decision: SelectionDecision | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {"candidate_id": candidate_id, "summary": labels.get(candidate_id)}
    if position_a is not None:
        entry["position_a"] = position_a
    if position_b is not None:
        entry["position_b"] = position_b
    if decision is not None:
        entry["role"] = decision.role.value if decision.role else None
        entry["treatment"] = decision.treatment.value if decision.treatment else None
        entry["directive"] = decision.directive
    return entry


def _moved_entries(
    seq_a: list[str],
    seq_b: list[str],
    common: set[str],
    pos_a: Mapping[str, int],
    pos_b: Mapping[str, int],
    labels: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Ids outside the longest stable alignment of the common subsequences."""
    filtered_a = [cid for cid in seq_a if cid in common]
    filtered_b = [cid for cid in seq_b if cid in common]
    matcher = difflib.SequenceMatcher(a=filtered_a, b=filtered_b, autojunk=False)
    stable: set[str] = set()
    for tag, i1, i2, _j1, _j2 in matcher.get_opcodes():
        if tag == "equal":
            stable.update(filtered_a[i1:i2])
    return [
        {
            "candidate_id": cid,
            "position_a": pos_a[cid],
            "position_b": pos_b[cid],
            "summary": labels.get(cid),
        }
        for cid in filtered_b
        if cid not in stable
    ]


def _field_changes(
    decision_a: SelectionDecision | None,
    decision_b: SelectionDecision | None,
) -> dict[str, list[Any]]:
    if decision_a is None or decision_b is None:
        return {}
    changes: dict[str, list[Any]] = {}
    for field in _DECISION_FIELDS:
        value_a = getattr(decision_a, field)
        value_b = getattr(decision_b, field)
        value_a = value_a.value if hasattr(value_a, "value") else value_a
        value_b = value_b.value if hasattr(value_b, "value") else value_b
        if value_a != value_b:
            changes[field] = [value_a, value_b]
    return changes
