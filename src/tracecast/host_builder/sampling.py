"""Deterministic preference-corpus slicing and position-balanced sampling."""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass
from pathlib import Path

from tracecast.domain import derive_id, estimate_tokens
from tracecast.hosts import STYLE_EXAMPLE_POSITIONS, StyleExamplePosition
from tracecast.language import is_bcp47_language_tag
from tracecast.storage import content_hash

from .models import HostBuildInputRecord

DEFAULT_PIECE_MAX_TOKENS = 180
DEFAULT_PROFILE_SAMPLE_TOKEN_BUDGET = 6000
DOMINANT_SCRIPT_MIN_SHARE = 0.60

_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*(?:\n[ \t]*)+")
_SENTENCE_PUNCT = "。．！？；….!?;"
_CLAUSE_PUNCT = "，、：,:"
_LEADING_BOILERPLATE = re.compile(
    r"^(?:哈[喽啰]|嗨|各位.*大家好|大家好|欢迎来到|"
    r"我是[^，,。.!！?？]{0,40}(?:[，,。.!！?？]|$)|"
    r"hey\s+(?:guys|everyone|folks)|"
    r"welcome\s+(?:back\s+)?to\b|"
    r"what'?s\s+up\b)",
    re.IGNORECASE,
)
# Platform CTA / outro vocabulary.  Spoken transcripts insert filler characters
# inside these phrases (点个赞, 投个币), so the CJK entries stay infix-tolerant.
_TRAILING_BOILERPLATE_LINE = re.compile(
    r"(?:点[个下一]?赞|投[个下一]?币|关注|订阅|收藏|一键三连|三连|充电|催更|"
    r"求.{0,4}(?:关注|订阅|三连|弹幕|评论|支持)|"
    r"(?:来|发|走|扣).{0,4}(?:条|个|波).{0,2}弹幕|"
    r"(?:弹幕|评论).{0,20}(?:点赞|关注|订阅|投币)|"
    r"感谢.{0,20}(?:观看|收听|看到这里|陪伴|支持)|"
    r"(?:视频|节目|本期).{0,20}(?:到这里|结束)|"
    r"下期|下次|下(?:个|支)视频见|(?:我们|咱们).{0,6}见|"
    r"再见|拜拜|拜个?年|"
    r"祝.{0,10}(?:快乐|愉快|安康|顺利|健康)|"
    r"我是.{0,20}(?:UP主|up主|主播|博主)|"
    r"\b(?:like\s+and\s+subscribe|subscribe|hit\s+the\s+bell|ring\s+the\s+bell|"
    r"comment\s+below|leave\s+a\s+comment|thanks?\s+for\s+watching|"
    r"see\s+you\s+(?:next|in\s+the\s+next)|patreon|"
    r"smash\s+that\s+like|i'?m\s+your\s+host)\b)",
    re.IGNORECASE,
)
# A spoken outro is a suffix, but subtitle-style corpora break one CTA sentence
# across several lines and slip greetings between them.  The trailing scan may
# therefore step over a bounded number of unmatched lines, while the committed
# boundary always lands on a matched line.
_TRAILING_MAX_GAP = 2
_TRAILING_MAX_LINES = 30
_TRAILING_MAX_SHARE = 0.25


@dataclass(frozen=True)
class PreferencePiece:
    candidate_id: str
    input_path: str
    input_content_hash: str
    span_start: int
    span_end: int
    text: str
    estimated_tokens: int


@dataclass(frozen=True)
class PreferenceDocument:
    path: str
    content_hash: str
    text: str
    record: HostBuildInputRecord
    pieces: tuple[PreferencePiece, ...]


@dataclass(frozen=True)
class PreparedCorpus:
    documents: tuple[PreferenceDocument, ...]
    input_records: tuple[HostBuildInputRecord, ...]
    dominant_script: str
    seed: int


def prepare_corpus(
    inputs: list[str | Path],
    *,
    piece_max_tokens: int = DEFAULT_PIECE_MAX_TOKENS,
    seed: int | None = None,
) -> PreparedCorpus:
    if piece_max_tokens <= 0:
        raise ValueError("piece_max_tokens must be positive")
    paths = expand_input_paths(inputs)
    raw_rows: list[tuple[Path, str, str, dict[str, int]]] = []
    aggregate = {"cjk": 0, "latin": 0}
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            raise ValueError(f"preference input is empty: {path}")
        digest = content_hash(text)
        counts = script_counts(text)
        aggregate["cjk"] += counts["cjk"]
        aggregate["latin"] += counts["latin"]
        raw_rows.append((path, text, digest, counts))
    dominant_script = _dominant_script(aggregate)
    effective_seed = seed if seed is not None else _default_seed(raw_rows)

    records: list[HostBuildInputRecord] = []
    documents: list[PreferenceDocument] = []
    for path, text, digest, counts in raw_rows:
        foreign = "latin" if dominant_script == "cjk" else "cjk"
        eligible = counts[foreign] <= counts[dominant_script] * 2
        reason = None if eligible else f"dominant script is {foreign}, corpus is {dominant_script}"
        trim_start, trim_end = trim_platform_boilerplate(text)
        record = HostBuildInputRecord(
            path=str(path),
            content_hash=digest,
            char_count=len(text),
            script_counts=counts,
            eligible=eligible,
            exclusion_reason=reason,
            trim_start=trim_start,
            trim_end=trim_end,
        )
        records.append(record)
        if not eligible:
            continue
        spans = split_preference_spans(
            text,
            start=trim_start,
            end=trim_end,
            max_tokens=piece_max_tokens,
        )
        pieces = tuple(
            piece
            for start, end in spans
            if (piece := _piece(path, digest, text, start, end)) is not None
        )
        if pieces:
            documents.append(
                PreferenceDocument(
                    path=str(path),
                    content_hash=digest,
                    text=text,
                    record=record,
                    pieces=pieces,
                )
            )
    if not documents:
        raise ValueError("no eligible non-empty preference inputs remain")
    return PreparedCorpus(
        documents=tuple(documents),
        input_records=tuple(records),
        dominant_script=dominant_script,
        seed=effective_seed,
    )


def expand_input_paths(inputs: list[str | Path]) -> list[Path]:
    if not inputs:
        raise ValueError("host-build requires at least one input")
    resolved: dict[str, Path] = {}
    for raw in inputs:
        path = Path(raw)
        if path.is_dir():
            candidates = sorted(path.glob("*.txt"), key=lambda item: item.name.casefold())
        elif path.is_file():
            candidates = [path]
        else:
            raise FileNotFoundError(f"preference input not found: {path}")
        for candidate in candidates:
            absolute = candidate.resolve()
            resolved[str(absolute).casefold()] = absolute
    paths = [resolved[key] for key in sorted(resolved)]
    if not paths:
        raise ValueError("host-build inputs contain no text files")
    return paths


def script_counts(text: str) -> dict[str, int]:
    han = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))
    kana = len(re.findall(r"[\u3040-\u30ff]", text))
    hangul = len(re.findall(r"[\uac00-\ud7af]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    return {"cjk": han + kana + hangul, "latin": latin, "kana": kana, "hangul": hangul}


def trim_platform_boilerplate(text: str) -> tuple[int, int]:
    """Return a continuous substantive span after deterministic edge cleanup."""

    lines = list(re.finditer(r".*(?:\n|$)", text))
    lines = [match for match in lines if match.end() > match.start()]
    start = 0
    for match in lines:
        line = match.group().strip()
        if not line:
            start = match.end()
            continue
        if _LEADING_BOILERPLATE.search(line):
            start = match.end()
            continue
        break

    end = _trailing_boundary(text, lines, start)

    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start >= end:
        return (0, 0)
    return start, end


def split_preference_spans(
    text: str,
    *,
    start: int = 0,
    end: int | None = None,
    max_tokens: int = DEFAULT_PIECE_MAX_TOKENS,
) -> list[tuple[int, int]]:
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    stop = len(text) if end is None else end
    if start < 0 or stop < start or stop > len(text):
        raise ValueError("invalid preference-text span")
    spans: list[tuple[int, int]] = []
    pos = start
    while pos < stop:
        cap = _token_cap_end(text, pos, stop, max_tokens)
        cut = cap if cap == stop else _best_break(text, pos, cap)
        if cut <= pos:
            cut = cap
        spans.append((pos, cut))
        pos = cut
    return spans


def build_position_pools(
    corpus: PreparedCorpus,
) -> dict[StyleExamplePosition, list[PreferencePiece]]:
    pools: dict[StyleExamplePosition, list[PreferencePiece]] = {
        position: [] for position in STYLE_EXAMPLE_POSITIONS
    }
    for document in corpus.documents:
        pieces = document.pieces
        pools["opening"].append(pieces[0])
        pools["ending"].append(pieces[-1])
        pools["middle"].extend(pieces[1:-1])
    return pools


def select_style_examples(
    corpus: PreparedCorpus,
    *,
    examples_per_position: int = 2,
) -> dict[StyleExamplePosition, tuple[PreferencePiece, ...]]:
    if examples_per_position <= 0:
        raise ValueError("examples_per_position must be positive")
    pools = build_position_pools(corpus)
    rng = random.Random(corpus.seed)
    used_ids: set[str] = set()
    used_paths: set[str] = set()
    selected: dict[StyleExamplePosition, tuple[PreferencePiece, ...]] = {}
    for position in STYLE_EXAMPLE_POSITIONS:
        available_count = len(
            {
                row.candidate_id
                for row in pools[position]
                if row.candidate_id not in used_ids
            }
        )
        count = min(examples_per_position, available_count)
        rows = _source_balanced_pick(
            pools[position],
            count=count,
            rng=rng,
            used_ids=used_ids,
            globally_used_paths=used_paths,
        )
        if not rows:
            raise ValueError(
                f"not enough unique {position} candidates: "
                "need at least 1, found 0"
            )
        selected[position] = tuple(rows)
        used_ids.update(row.candidate_id for row in rows)
        used_paths.update(row.input_path for row in rows)
    return selected


def build_profile_sample(
    corpus: PreparedCorpus,
    selected: dict[StyleExamplePosition, tuple[PreferencePiece, ...]],
    *,
    token_budget: int = DEFAULT_PROFILE_SAMPLE_TOKEN_BUDGET,
) -> tuple[PreferencePiece, ...]:
    if token_budget <= 0:
        raise ValueError("profile sample token budget must be positive")
    chosen = [row for position in STYLE_EXAMPLE_POSITIONS for row in selected[position]]
    chosen_ids = {row.candidate_id for row in chosen}
    used_tokens = sum(row.estimated_tokens for row in chosen)
    all_candidates = [
        row
        for pool in build_position_pools(corpus).values()
        for row in pool
        if row.candidate_id not in chosen_ids
    ]
    by_path: dict[str, list[PreferencePiece]] = {}
    for row in all_candidates:
        by_path.setdefault(row.input_path, []).append(row)
    rng = random.Random(corpus.seed ^ 0x484F53544255494C)
    paths = sorted(by_path)
    rng.shuffle(paths)
    while paths:
        next_paths: list[str] = []
        added = False
        for path in paths:
            candidates = by_path[path]
            rng.shuffle(candidates)
            row = candidates.pop()
            if used_tokens + row.estimated_tokens <= token_budget:
                chosen.append(row)
                used_tokens += row.estimated_tokens
                added = True
            if candidates:
                next_paths.append(path)
        if used_tokens >= token_budget or not added:
            break
        paths = next_paths
    return tuple(chosen)


def _trailing_boundary(text: str, lines: list[re.Match[str]], start: int) -> int:
    """Character offset where the trailing CTA/outro block begins.

    Two boundaries are computed from the same word list.  ``strict`` only walks
    the uninterrupted run of matched lines at the very end; ``gapped`` may step
    over up to ``_TRAILING_MAX_GAP`` unmatched lines but still has to land on a
    matched line.  A gapped boundary that would delete more than
    ``_TRAILING_MAX_SHARE`` of the body is a false positive, not an outro, so it
    falls back to the strict boundary.
    """

    strict: int | None = None
    gapped: int | None = None
    gap = 0
    scanned = 0
    strict_open = True
    for line_match in reversed(lines):
        if line_match.end() <= start:
            break
        line = line_match.group().strip()
        if not line:
            continue
        if scanned >= _TRAILING_MAX_LINES:
            break
        scanned += 1
        if _TRAILING_BOILERPLATE_LINE.search(line):
            gapped = line_match.start()
            gap = 0
            if strict_open:
                strict = gapped
            continue
        strict_open = False
        gap += 1
        if gap > _TRAILING_MAX_GAP:
            break

    if gapped is None:
        return len(text)
    body = len(text) - start
    if body > 0 and (len(text) - gapped) / body > _TRAILING_MAX_SHARE:
        return len(text) if strict is None else strict
    return gapped


def _dominant_script(counts: dict[str, int]) -> str:
    total = counts["cjk"] + counts["latin"]
    if total <= 0:
        raise ValueError("preference corpus has no detectable CJK or Latin script")
    script = "cjk" if counts["cjk"] >= counts["latin"] else "latin"
    if counts[script] / total < DOMINANT_SCRIPT_MIN_SHARE:
        raise ValueError("preference corpus has no script family at or above 60%")
    return script


def language_script_family(output_language: str) -> str | None:
    if not is_bcp47_language_tag(output_language):
        return None
    primary = output_language.strip().casefold().split("-", 1)[0]
    if primary in {"zh", "ja", "ko"}:
        return "cjk"
    if primary in {
        "en", "fr", "de", "es", "pt", "it", "nl", "sv", "no", "da", "fi",
        "pl", "cs", "sk", "ro", "hu", "tr", "vi", "id", "ms",
    }:
        return "latin"
    return None


def _default_seed(rows: list[tuple[Path, str, str, dict[str, int]]]) -> int:
    joined = "\x1e".join(f"{path.resolve()}\x1f{digest}" for path, _, digest, _ in rows)
    return int(hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16], 16)


def _piece(
    path: Path, digest: str, text: str, start: int, end: int
) -> PreferencePiece | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start >= end:
        return None
    value = text[start:end]
    return PreferencePiece(
        candidate_id=derive_id("hostpiece", digest, start, end),
        input_path=str(path),
        input_content_hash=digest,
        span_start=start,
        span_end=end,
        text=value,
        estimated_tokens=estimate_tokens(value),
    )


def _source_balanced_pick(
    pool: list[PreferencePiece],
    *,
    count: int,
    rng: random.Random,
    used_ids: set[str],
    globally_used_paths: set[str],
) -> list[PreferencePiece]:
    available = [row for row in pool if row.candidate_id not in used_ids]
    by_path: dict[str, list[PreferencePiece]] = {}
    for row in available:
        by_path.setdefault(row.input_path, []).append(row)
    paths = sorted(by_path, key=lambda path: (path in globally_used_paths, path.casefold()))
    preferred = [path for path in paths if path not in globally_used_paths]
    fallback = [path for path in paths if path in globally_used_paths]
    rng.shuffle(preferred)
    rng.shuffle(fallback)
    result: list[PreferencePiece] = []
    for path in [*preferred, *fallback]:
        candidates = sorted(by_path[path], key=lambda row: row.span_start)
        result.append(rng.choice(candidates))
        if len(result) == count:
            break
    if len(result) < count:
        leftovers = [row for row in available if row not in result]
        rng.shuffle(leftovers)
        result.extend(leftovers[: count - len(result)])
    return result


def _token_cap_end(text: str, start: int, stop: int, max_tokens: int) -> int:
    low = start
    high = min(stop, start + max(64, max_tokens * 4))
    while high < stop and estimate_tokens(text[start:high]) <= max_tokens:
        low = high
        high = min(stop, start + (high - start) * 2)
    if high == stop and estimate_tokens(text[start:high]) <= max_tokens:
        return stop
    while low + 1 < high:
        middle = (low + high) // 2
        if estimate_tokens(text[start:middle]) <= max_tokens:
            low = middle
        else:
            high = middle
    return max(start + 1, low)


def _best_break(text: str, start: int, cap: int) -> int:
    window = text[start:cap]
    paragraph = None
    for match in _PARAGRAPH_BREAK.finditer(window):
        paragraph = start + match.end()
    if paragraph is not None:
        return paragraph
    for chars in (_SENTENCE_PUNCT, _CLAUSE_PUNCT):
        index = max((window.rfind(char) for char in chars), default=-1)
        if index >= 0:
            return start + index + 1
    newline = window.rfind("\n")
    if newline >= 0:
        return start + newline + 1
    for index in range(len(window) - 1, -1, -1):
        if window[index].isspace():
            return start + index + 1
    return cap


__all__ = [
    "DEFAULT_PIECE_MAX_TOKENS",
    "DEFAULT_PROFILE_SAMPLE_TOKEN_BUDGET",
    "PreferenceDocument",
    "PreferencePiece",
    "PreparedCorpus",
    "build_position_pools",
    "build_profile_sample",
    "expand_input_paths",
    "is_bcp47_language_tag",
    "language_script_family",
    "prepare_corpus",
    "script_counts",
    "select_style_examples",
    "split_preference_spans",
    "trim_platform_boilerplate",
]
