"""Independent spec-conformance tests for the generic gateway boundary.

The invariants under test are self-contained: one fresh ``LlmCallRecord`` per
provider attempt, canonical request hashing, transport-only retry, transport status
separate from business parsing, configured cost estimates rather than invoices,
and no API key in persisted payloads.

No real network and no real key: an injected ``httpx.Client`` on a ``MockTransport``
simulates the provider, and a unique sentinel key is supplied via monkeypatched env
with ``RuntimeConfig.load(..., load_env=False)``. The implementation was read only to
exercise the public API and ``LlmCallRecord`` fields.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from tracecast.domain import CostKind, LlmCallRecord, LlmWorkUnitRef, ProviderStatus
from tracecast.llm import (
    CHAT_COMPLETIONS_PATH,
    DEFAULT_COST_PROFILES,
    CostProfile,
    LlmTransportError,
    Usage,
    build_request_envelope,
    call_llm,
    estimate_cost,
    extract_usage,
    resolve_cost_profile,
)
from tracecast.llm.cost import RATES_SOURCE_CONFIG, RATES_SOURCE_DEFAULT
from tracecast.storage import (
    LLM_CALL_RECORDS,
    RunStore,
    RuntimeConfig,
    sha256_json,
    sha256_text,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_CONFIG = REPO_ROOT / "configs" / "runtime.example.json"

# Unique fake key that must never escape the boundary (used to scan persisted files).
SENTINEL_KEY = "sk-SENTINEL-tracecast-must-not-leak-9f1c2e7a4b"
KEY_ENV = "TRACECAST_LLM_API_KEY"

ROUTE = "render_chunk"
PROMPT_VERSION = "render_chunk.zh.v1"
MESSAGES = [
    {"role": "system", "content": "你是中文播客脚本渲染器。"},
    {"role": "user", "content": "请把以下材料渲染成中文正文。"},
]
RESPONSE_TEXT = "这是模型返回的中文正文段落。"

# OpenAI-compatible usage; the derived split below is what the record must carry.
USAGE = {
    "prompt_tokens": 1000,
    "completion_tokens": 200,
    "prompt_cache_hit_tokens": 400,
    "prompt_cache_miss_tokens": 600,
    "completion_tokens_details": {"reasoning_tokens": 50},
}
# input=1000 output=200 cached=400 uncached=600 reasoning=50 visible=150
EXPECTED_USAGE = Usage(
    input_tokens=1000,
    output_tokens=200,
    cached_input_tokens=400,
    uncached_input_tokens=600,
    reasoning_output_tokens=50,
    visible_output_tokens=150,
)
# Non-zero rate profile; cost == (600*2 + 400*1 + 200*3) / 1e6 == 0.0022.
RATES = {
    "uncached_input_per_million": 2.0,
    "cached_input_per_million": 1.0,
    "output_per_million": 3.0,
}
EXPECTED_COST = 0.0022


# --- fixtures & helpers -----------------------------------------------------


@pytest.fixture
def sentinel_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the sentinel key in the process env only -- never on disk, never in .env."""
    monkeypatch.setenv(KEY_ENV, SENTINEL_KEY)


@pytest.fixture
def config(sentinel_key: None) -> RuntimeConfig:
    # load_env=False -> the real .env is never read; the only key source is the env above.
    return RuntimeConfig.load(RUNTIME_CONFIG, load_env=False)


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore.for_run(tmp_path / "runs", "run-test")


def build_config(*, cost_profiles: dict | None = None) -> RuntimeConfig:
    """Real runtime config, optionally with an operator ``cost_profiles`` override."""
    data = json.loads(RUNTIME_CONFIG.read_text(encoding="utf-8"))
    if cost_profiles is not None:
        data["cost_profiles"] = cost_profiles
        data["routes"][ROUTE]["cost_profile"] = "operator-rates"
    return RuntimeConfig.model_validate(data)


def _no_sleep(_seconds: float) -> None:
    """Drop-in for ``time.sleep`` so transport backoff never delays a test."""


Responder = Callable[[httpx.Request], httpx.Response]


class RecordingTransport:
    """MockTransport handler: records every request and plays a scripted outcome.

    Each outcome is a ``(request) -> httpx.Response`` callable (it may raise a
    transport exception). The final outcome repeats for any further attempts, so a
    single failing outcome models a persistently failing provider.
    """

    def __init__(self, *outcomes: Responder) -> None:
        assert outcomes, "at least one outcome required"
        self._outcomes = outcomes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        index = len(self.requests)
        self.requests.append(request)
        return self._outcomes[min(index, len(self._outcomes) - 1)](request)

    @property
    def calls(self) -> int:
        return len(self.requests)


def mock_client(handler: RecordingTransport) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _chat_completion(text: str, *, usage: dict | None, status: int) -> httpx.Response:
    body: dict = {
        "id": "chatcmpl-test-001",
        "object": "chat.completion",
        "model": "your-standard-model",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(status, json=body)


def respond(text: str = RESPONSE_TEXT, *, usage: dict | None = USAGE) -> Responder:
    def outcome(_request: httpx.Request) -> httpx.Response:
        return _chat_completion(text, usage=usage, status=200)

    return outcome


def http(code: int, *, headers: dict[str, str] | None = None) -> Responder:
    def outcome(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            code,
            json={"error": f"status {code}"},
            headers=headers,
        )

    return outcome


def raise_transport(exc_type: type[httpx.RequestError]) -> Responder:
    def outcome(request: httpx.Request) -> httpx.Response:
        raise exc_type("simulated transport failure", request=request)

    return outcome


_FAILURES: dict[str, tuple[Responder, ProviderStatus]] = {
    "http_408": (http(408), ProviderStatus.error),
    "http_503": (http(503), ProviderStatus.error),
    "http_429": (http(429), ProviderStatus.error),
    "timeout": (raise_transport(httpx.ReadTimeout), ProviderStatus.timeout),
    "connect_error": (raise_transport(httpx.ConnectError), ProviderStatus.error),
}


# === 1. Success path: text + fully populated record ==========================


def test_success_returns_text_and_populated_record(config: RuntimeConfig, store: RunStore) -> None:
    handler = RecordingTransport(respond())
    with mock_client(handler) as client:
        text, record = call_llm(
            operation_route=ROUTE,
            messages=MESSAGES,
            config=config,
            store=store,
            prompt_version=PROMPT_VERSION,
            http_client=client,
            sleep=_no_sleep,
        )

    assert isinstance(record, LlmCallRecord)
    assert text == RESPONSE_TEXT  # returned verbatim from the mock body
    assert record.provider_status is ProviderStatus.success

    # identity comes from the route config, not from the caller's free strings
    route = config.route(ROUTE)
    assert record.provider == route.provider == "gateway"
    assert record.model == route.model == "your-standard-model"
    assert record.stage == route.stage == "renderer"  # stage defaults to the route's stage
    assert record.operation_route == ROUTE
    assert record.prompt_version == PROMPT_VERSION
    assert record.key_id == "default"

    # the request hit the OpenAI-compatible chat-completions path on the provider base_url
    assert str(handler.requests[0].url) == (
        "https://gateway.example.com/v1" + CHAT_COMPLETIONS_PATH
    )

    # stable, sha256-prefixed hashes; input_hash is the canonical-envelope hash
    assert record.call_id.startswith("call_")
    assert record.input_hash.startswith("sha256:")
    assert record.output_hash == f"sha256:{sha256_text(RESPONSE_TEXT)}"
    envelope = build_request_envelope(
        stage=route.stage,
        operation_route=ROUTE,
        prompt_version=PROMPT_VERSION,
        provider=route.provider,
        model=route.model,
        thinking=route.thinking,
        temperature=route.temperature,
        max_output_tokens=route.max_output_tokens,
        timeout_seconds=route.timeout_seconds,
        messages=MESSAGES,
    )
    assert record.input_hash == f"sha256:{sha256_json(envelope)}"

    # token counts straight from usage
    assert record.input_tokens == 1000
    assert record.output_tokens == 200
    assert record.cached_input_tokens == 400
    assert record.uncached_input_tokens == 600
    assert record.reasoning_output_tokens == 50
    assert record.visible_output_tokens == 150

    # cost provenance: configured estimate, zero under the placeholder rates
    assert record.cost_kind is CostKind.configured_estimate
    assert record.cost == 0.0

    # raw request + raw response persisted, and the record appended to the ledger
    assert record.raw_request_path is not None and record.raw_request_path.startswith("llm/")
    assert record.raw_response_path is not None and record.raw_response_path.startswith("llm/")
    assert store.read_raw_payload(record.call_id, "request")
    assert store.read_raw_payload(record.call_id, "response")
    assert store.read_records(LLM_CALL_RECORDS) == [record]  # exactly one, appended


def test_effective_default_timeout_is_persisted_in_request_envelope(
    config: RuntimeConfig,
    store: RunStore,
) -> None:
    data = config.model_dump()
    data["routes"][ROUTE]["timeout_seconds"] = None
    configured = RuntimeConfig.model_validate(data)
    handler = RecordingTransport(respond())

    with mock_client(handler) as client:
        _text, record = call_llm(
            operation_route=ROUTE,
            messages=MESSAGES,
            config=configured,
            store=store,
            http_client=client,
            sleep=_no_sleep,
        )

    raw_request = store.read_raw_payload(record.call_id, "request")
    assert raw_request["timeout_seconds"] == 180.0
    assert record.input_hash == f"sha256:{sha256_json(raw_request)}"
    wire_timeout = handler.requests[0].extensions["timeout"]
    assert set(wire_timeout.values()) == {180.0}


@pytest.mark.parametrize(
    ("token_field", "unexpected_field"),
    [
        ("max_tokens", "max_completion_tokens"),
        ("max_completion_tokens", "max_tokens"),
    ],
)
def test_gateway_path_token_field_extra_body_and_hash_match_final_wire_body(
    config: RuntimeConfig,
    store: RunStore,
    token_field: str,
    unexpected_field: str,
) -> None:
    data = config.model_dump()
    data["providers"]["gateway"]["chat_completions_path"] = "/custom/chat"
    data["providers"]["gateway"]["max_output_tokens_field"] = token_field
    data["routes"][ROUTE]["extra_body"] = {
        "reasoning_effort": "medium",
        "metadata": {"suite": "gateway-contract"},
    }
    configured = RuntimeConfig.model_validate(data)
    handler = RecordingTransport(respond())

    with mock_client(handler) as client:
        _text, record = call_llm(
            operation_route=ROUTE,
            messages=MESSAGES,
            config=configured,
            store=store,
            http_client=client,
            sleep=_no_sleep,
        )

    assert str(handler.requests[0].url) == "https://gateway.example.com/v1/custom/chat"
    wire_body = json.loads(handler.requests[0].content)
    assert wire_body[token_field] == configured.route(ROUTE).max_output_tokens
    assert unexpected_field not in wire_body
    assert wire_body["reasoning_effort"] == "medium"
    assert wire_body["metadata"] == {"suite": "gateway-contract"}
    raw_request = store.read_raw_payload(record.call_id, "request")
    assert raw_request["request_body"] == wire_body
    assert record.input_hash == f"sha256:{sha256_json(raw_request)}"


@pytest.mark.parametrize(
    "reserved",
    ["model", "messages", "stream", "temperature", "max_tokens", "max_completion_tokens"],
)
def test_extra_body_rejects_core_gateway_fields(
    config: RuntimeConfig, reserved: str
) -> None:
    data = config.model_dump()
    data["routes"][ROUTE]["extra_body"] = {reserved: "override"}

    with pytest.raises(ValueError, match="extra_body cannot override"):
        RuntimeConfig.model_validate(data)


def test_legacy_thinking_config_remains_wire_compatible(
    config: RuntimeConfig, store: RunStore
) -> None:
    data = config.model_dump()
    data["routes"][ROUTE]["thinking"] = "enabled"
    configured = RuntimeConfig.model_validate(data)
    handler = RecordingTransport(respond())

    with mock_client(handler) as client:
        _text, record = call_llm(
            operation_route=ROUTE,
            messages=MESSAGES,
            config=configured,
            store=store,
            http_client=client,
            sleep=_no_sleep,
        )

    wire_body = json.loads(handler.requests[0].content)
    assert wire_body["thinking"] == {"type": "enabled"}
    raw_request = store.read_raw_payload(record.call_id, "request")
    assert raw_request["thinking"] == "enabled"
    assert raw_request["request_body"] == wire_body


def test_work_unit_is_on_every_attempt_but_excluded_from_request_hash(
    config: RuntimeConfig, tmp_path: Path
) -> None:
    work = LlmWorkUnitRef(
        kind="render_chunk",
        unit_id="chunk_7",
        order_index=6,
        input_refs=["cand_a", "atom_b"],
    )
    store = RunStore.for_run(tmp_path / "runs", "with-work")
    handler = RecordingTransport(http(503), respond())
    with mock_client(handler) as client:
        _text, record = call_llm(
            operation_route=ROUTE,
            messages=MESSAGES,
            config=config,
            store=store,
            prompt_version=PROMPT_VERSION,
            work_unit=work,
            http_client=client,
            sleep=_no_sleep,
        )

    attempts = store.read_records(LLM_CALL_RECORDS)
    assert len(attempts) == 2
    assert all(attempt.work_unit == work for attempt in attempts)
    assert record.work_unit == work
    raw = store.read_raw_payload(record.call_id, "request")
    assert isinstance(raw, dict)
    assert "work_unit" not in raw
    assert record.input_hash == f"sha256:{sha256_json(raw)}"


def test_concurrent_calls_keep_their_own_work_units(
    config: RuntimeConfig, tmp_path: Path
) -> None:
    store = RunStore.for_run(tmp_path / "runs", "concurrent-work")

    def invoke(index: int) -> LlmCallRecord:
        handler = RecordingTransport(respond(text=f"response-{index}"))
        work = LlmWorkUnitRef(
            kind="ingestion_window",
            unit_id=f"window-{index}",
            order_index=index,
            input_refs=[f"piece-{index}"],
        )
        with mock_client(handler) as client:
            _text, record = call_llm(
                operation_route=ROUTE,
                messages=[*MESSAGES, {"role": "user", "content": str(index)}],
                config=config,
                store=store,
                work_unit=work,
                http_client=client,
                sleep=_no_sleep,
            )
        return record

    with ThreadPoolExecutor(max_workers=4) as executor:
        returned = list(executor.map(invoke, range(4)))

    expected = {
        f"window-{index}": [f"piece-{index}"] for index in range(4)
    }
    assert {
        record.work_unit.unit_id: record.work_unit.input_refs
        for record in returned
        if record.work_unit is not None
    } == expected
    persisted = store.read_records(LLM_CALL_RECORDS)
    assert len(persisted) == 4
    assert {
        record.work_unit.unit_id: record.work_unit.input_refs
        for record in persisted
        if record.work_unit is not None
    } == expected


# === 2. KEY SAFETY (critical): the key is used on the wire, never persisted ===


def test_api_key_never_leaks_to_disk_or_record(config: RuntimeConfig, store: RunStore) -> None:
    handler = RecordingTransport(respond())
    with mock_client(handler) as client:
        _text, record = call_llm(
            operation_route=ROUTE,
            messages=MESSAGES,
            config=config,
            store=store,
            http_client=client,
            sleep=_no_sleep,
        )

    # the key DOES reach the provider wire (resolved only at header-build time) ...
    assert handler.requests[0].headers.get("Authorization") == f"Bearer {SENTINEL_KEY}"

    # ... but appears in NO file persisted under the run dir (non-vacuous scan).
    files = [p for p in store.run_dir.rglob("*") if p.is_file()]
    assert len(files) >= 3  # raw request + raw response + the ledger, at minimum
    for path in files:
        blob = path.read_text(encoding="utf-8", errors="replace")
        assert SENTINEL_KEY not in blob, f"sentinel key leaked into {path}"
        assert "Bearer" not in blob, f"auth header leaked into {path}"

    # ... nor in the canonical request envelope ...
    envelope = build_request_envelope(
        stage="renderer",
        operation_route=ROUTE,
        prompt_version=None,
        provider="gateway",
        model="your-standard-model",
        thinking=None,
        temperature=0,
        max_output_tokens=40960,
        timeout_seconds=180,
        messages=MESSAGES,
    )
    dumped = json.dumps(envelope, ensure_ascii=False)
    assert SENTINEL_KEY not in dumped
    assert "api_key" not in dumped and "Authorization" not in dumped

    # ... nor in the record's repr or JSON dump.
    assert SENTINEL_KEY not in repr(record)
    assert SENTINEL_KEY not in record.model_dump_json()


def test_provider_response_echoing_secret_is_redacted_before_return_and_persistence(
    config: RuntimeConfig, store: RunStore
) -> None:
    handler = RecordingTransport(respond(f"echo={SENTINEL_KEY}"))
    with mock_client(handler) as client:
        text, record = call_llm(
            operation_route=ROUTE,
            messages=MESSAGES,
            config=config,
            store=store,
            http_client=client,
            sleep=_no_sleep,
        )

    assert text == "echo=[REDACTED]"
    assert SENTINEL_KEY not in record.model_dump_json()
    assert SENTINEL_KEY not in json.dumps(
        store.read_raw_payload(record.call_id, "response"), ensure_ascii=False
    )


def test_transport_error_echoing_secret_is_redacted_in_error_ledger(
    config: RuntimeConfig, store: RunStore
) -> None:
    def leak(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect with {SENTINEL_KEY}", request=request)

    handler = RecordingTransport(leak)
    with mock_client(handler) as client:
        with pytest.raises(LlmTransportError) as excinfo:
            call_llm(
                operation_route=ROUTE,
                messages=MESSAGES,
                config=config,
                store=store,
                http_client=client,
                max_attempts=1,
                sleep=_no_sleep,
            )

    record = excinfo.value.record
    assert record.error and "[REDACTED]" in record.error
    assert SENTINEL_KEY not in record.model_dump_json()
    assert SENTINEL_KEY not in json.dumps(
        store.read_raw_payload(record.call_id, "response"), ensure_ascii=False
    )


def test_request_envelope_carries_audit_identity_and_no_secrets() -> None:
    envelope = build_request_envelope(
        stage="renderer",
        operation_route=ROUTE,
        prompt_version=PROMPT_VERSION,
        provider="gateway",
        model="your-standard-model",
        thinking=None,
        temperature=0,
        max_output_tokens=40960,
        timeout_seconds=180,
        messages=MESSAGES,
    )
    # Every output-affecting field must be present in the canonical request envelope.
    required = {
        "stage",
        "operation_route",
        "prompt_version",
        "provider",
        "model",
        "thinking",
        "temperature",
        "max_output_tokens",
        "timeout_seconds",
        "messages",
    }
    assert required <= set(envelope)
    assert envelope["messages"] == [dict(m) for m in MESSAGES]
    # ... and no secret or machine-varying output path leaks into the hashed input.
    for forbidden in ("api_key", "Authorization", "raw_request_path", "raw_response_path"):
        assert forbidden not in envelope


# === 3. Transport retry (bounded, transport-only) + one record per attempt ====


@pytest.mark.parametrize("failure", list(_FAILURES))
def test_persistent_transport_failure_retries_then_raises(
    failure: str, config: RuntimeConfig, store: RunStore
) -> None:
    outcome, expected_status = _FAILURES[failure]
    max_attempts = 3
    handler = RecordingTransport(outcome)  # repeats -> persistent failure

    with mock_client(handler) as client:
        with pytest.raises(LlmTransportError) as excinfo:
            call_llm(
                operation_route=ROUTE,
                messages=MESSAGES,
                config=config,
                store=store,
                http_client=client,
                max_attempts=max_attempts,
                sleep=_no_sleep,
            )

    err = excinfo.value
    assert err.attempts == max_attempts
    assert err.record.provider_status is expected_status
    assert err.record.provider_status in {ProviderStatus.error, ProviderStatus.timeout}

    # exactly one record per provider attempt, no overwrite, a fresh call_id each time
    records = store.read_records(LLM_CALL_RECORDS)
    assert handler.calls == max_attempts
    assert len(records) == max_attempts
    assert len({r.call_id for r in records}) == max_attempts
    assert all(r.provider_status is expected_status for r in records)
    assert err.record.call_id == records[-1].call_id  # exception carries the last record
    assert all(record.metadata["retryable"] is True for record in records)
    assert [record.metadata["retry_scheduled"] for record in records] == [
        True,
        True,
        False,
    ]
    assert all(
        "retry_delay_seconds" in record.metadata for record in records[:-1]
    )
    assert "retry_delay_seconds" not in records[-1].metadata


def test_route_transport_policy_applies_when_call_has_no_explicit_override(
    config: RuntimeConfig,
    store: RunStore,
) -> None:
    data = config.model_dump()
    data["routes"][ROUTE].update(
        {
            "transport_max_attempts": 2,
            "transport_backoff_base_seconds": 2.0,
            "transport_backoff_max_seconds": 9.0,
            "transport_jitter_ratio": 0.0,
        }
    )
    configured = RuntimeConfig.model_validate(data)
    sleeps: list[float] = []
    handler = RecordingTransport(http(503))

    with mock_client(handler) as client:
        with pytest.raises(LlmTransportError) as excinfo:
            call_llm(
                operation_route=ROUTE,
                messages=MESSAGES,
                config=configured,
                store=store,
                http_client=client,
                sleep=sleeps.append,
                random_value=lambda: 0.5,
            )

    assert excinfo.value.attempts == 2
    assert sleeps == [2.0]
    [first, second] = store.read_records(LLM_CALL_RECORDS)
    assert first.metadata["transport_backoff_base_seconds"] == 2.0
    assert first.metadata["transport_backoff_max_seconds"] == 9.0
    assert first.metadata["transport_jitter_ratio"] == 0.0
    assert first.metadata["retry_scheduled"] is True
    assert second.metadata["retry_scheduled"] is False


def test_explicit_transport_policy_overrides_route_policy(
    config: RuntimeConfig,
    store: RunStore,
) -> None:
    data = config.model_dump()
    data["routes"][ROUTE].update(
        {
            "transport_max_attempts": 5,
            "transport_backoff_base_seconds": 9.0,
            "transport_backoff_max_seconds": 20.0,
            "transport_jitter_ratio": 0.75,
        }
    )
    configured = RuntimeConfig.model_validate(data)
    sleeps: list[float] = []
    handler = RecordingTransport(http(503))

    with mock_client(handler) as client:
        with pytest.raises(LlmTransportError) as excinfo:
            call_llm(
                operation_route=ROUTE,
                messages=MESSAGES,
                config=configured,
                store=store,
                http_client=client,
                max_attempts=2,
                backoff_seconds=2.0,
                backoff_max_seconds=3.0,
                jitter_ratio=0.0,
                random_value=lambda: 0.5,
                sleep=sleeps.append,
            )

    assert excinfo.value.attempts == 2
    assert sleeps == [2.0]
    records = store.read_records(LLM_CALL_RECORDS)
    assert len(records) == 2
    assert all(record.metadata["max_attempts"] == 2 for record in records)
    assert all(
        record.metadata["transport_backoff_base_seconds"] == 2.0
        for record in records
    )
    assert all(
        record.metadata["transport_backoff_max_seconds"] == 3.0
        for record in records
    )
    assert all(
        record.metadata["transport_jitter_ratio"] == 0.0 for record in records
    )


@pytest.mark.parametrize(
    ("random_sample", "expected_delay"),
    [(0.0, 3.0), (0.5, 4.0), (1.0, 5.0)],
)
def test_exponential_backoff_jitter_is_bounded_and_injectable(
    random_sample: float,
    expected_delay: float,
    config: RuntimeConfig,
    store: RunStore,
) -> None:
    sleeps: list[float] = []
    handler = RecordingTransport(http(503), respond())

    with mock_client(handler) as client:
        text, _record = call_llm(
            operation_route=ROUTE,
            messages=MESSAGES,
            config=config,
            store=store,
            http_client=client,
            max_attempts=2,
            backoff_seconds=4.0,
            backoff_max_seconds=30.0,
            jitter_ratio=0.25,
            random_value=lambda: random_sample,
            sleep=sleeps.append,
        )

    assert text == RESPONSE_TEXT
    assert sleeps == [expected_delay]
    [failed, succeeded] = store.read_records(LLM_CALL_RECORDS)
    assert failed.metadata["retry_delay_seconds"] == expected_delay
    assert failed.metadata["retry_scheduled"] is True
    assert "retry_scheduled" not in succeeded.metadata


@pytest.mark.parametrize(
    ("retry_after", "backoff_cap", "expected_delay", "expected_source"),
    [
        ("7", 30.0, 7.0, "retry_after"),
        ("not-a-delay", 30.0, 1.0, "backoff"),
        ("120", 5.0, 5.0, "retry_after"),
    ],
)
def test_retry_after_seconds_invalid_value_and_cap(
    retry_after: str,
    backoff_cap: float,
    expected_delay: float,
    expected_source: str,
    config: RuntimeConfig,
    store: RunStore,
) -> None:
    sleeps: list[float] = []
    handler = RecordingTransport(
        http(429, headers={"Retry-After": retry_after}),
        respond(),
    )

    with mock_client(handler) as client:
        text, _record = call_llm(
            operation_route=ROUTE,
            messages=MESSAGES,
            config=config,
            store=store,
            http_client=client,
            max_attempts=2,
            backoff_seconds=1.0,
            backoff_max_seconds=backoff_cap,
            jitter_ratio=0.0,
            random_value=lambda: 0.5,
            sleep=sleeps.append,
        )

    assert text == RESPONSE_TEXT
    assert sleeps == [expected_delay]
    [failed, _succeeded] = store.read_records(LLM_CALL_RECORDS)
    assert failed.metadata["retryable"] is True
    assert failed.metadata["retry_scheduled"] is True
    assert failed.metadata["retry_delay_seconds"] == expected_delay
    assert failed.metadata["retry_delay_source"] == expected_source
    if retry_after == "not-a-delay":
        assert "retry_after_seconds" not in failed.metadata
    else:
        assert failed.metadata["retry_after_seconds"] == float(retry_after)


def test_retry_after_http_date_is_honored(
    config: RuntimeConfig,
    store: RunStore,
) -> None:
    retry_at = datetime.now(UTC) + timedelta(seconds=20)
    sleeps: list[float] = []
    handler = RecordingTransport(
        http(503, headers={"Retry-After": format_datetime(retry_at, usegmt=True)}),
        respond(),
    )

    with mock_client(handler) as client:
        call_llm(
            operation_route=ROUTE,
            messages=MESSAGES,
            config=config,
            store=store,
            http_client=client,
            max_attempts=2,
            backoff_seconds=1.0,
            backoff_max_seconds=30.0,
            jitter_ratio=0.0,
            random_value=lambda: 0.5,
            sleep=sleeps.append,
        )

    assert len(sleeps) == 1
    assert 17.0 <= sleeps[0] <= 20.0
    [failed, _succeeded] = store.read_records(LLM_CALL_RECORDS)
    assert failed.metadata["retry_delay_source"] == "retry_after"
    assert failed.metadata["retry_delay_seconds"] == sleeps[0]
    assert failed.metadata["retry_after_seconds"] == pytest.approx(
        sleeps[0], abs=0.01
    )


def test_attempts_bound_tracks_max_attempts_param(
    config: RuntimeConfig, store: RunStore
) -> None:
    handler = RecordingTransport(http(503))
    with mock_client(handler) as client:
        with pytest.raises(LlmTransportError) as excinfo:
            call_llm(
                operation_route=ROUTE,
                messages=MESSAGES,
                config=config,
                store=store,
                http_client=client,
                max_attempts=2,
                sleep=_no_sleep,
            )
    assert excinfo.value.attempts == 2
    assert handler.calls == 2
    assert len(store.read_records(LLM_CALL_RECORDS)) == 2


def test_retry_then_success_appends_one_record_per_attempt(
    config: RuntimeConfig, store: RunStore
) -> None:
    handler = RecordingTransport(http(503), http(503), respond())
    with mock_client(handler) as client:
        text, record = call_llm(
            operation_route=ROUTE,
            messages=MESSAGES,
            config=config,
            store=store,
            http_client=client,
            max_attempts=3,
            sleep=_no_sleep,
        )

    assert text == RESPONSE_TEXT
    assert record.provider_status is ProviderStatus.success
    assert handler.calls == 3
    records = store.read_records(LLM_CALL_RECORDS)
    assert [r.provider_status for r in records] == [
        ProviderStatus.error,
        ProviderStatus.error,
        ProviderStatus.success,
    ]
    assert len({r.call_id for r in records}) == 3  # fresh call_id each attempt
    assert records[-1].call_id == record.call_id  # the returned record is the last one


# === 4. No business retry / no auto-repair ===================================


def test_garbage_business_content_returned_verbatim(
    config: RuntimeConfig, store: RunStore
) -> None:
    garbage = "}}}-not-valid-DSL-乱码-<<<"
    handler = RecordingTransport(respond(garbage))
    with mock_client(handler) as client:
        text, record = call_llm(
            operation_route=ROUTE,
            messages=MESSAGES,
            config=config,
            store=store,
            http_client=client,
            max_attempts=3,
            sleep=_no_sleep,
        )
    assert text == garbage  # returned as-is, content never rewritten
    assert record.provider_status is ProviderStatus.success
    assert handler.calls == 1  # single attempt, no retry on a 2xx
    assert len(store.read_records(LLM_CALL_RECORDS)) == 1


def test_2xx_unexpected_shape_is_transport_success_not_judged(
    config: RuntimeConfig, store: RunStore
) -> None:
    # A 2xx with a non-empty body but no parseable content is transport success with
    # empty text; business validity is the stage's concern, not the observer's.
    def odd_shape(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"weird": "shape", "usage": USAGE})

    handler = RecordingTransport(odd_shape)
    with mock_client(handler) as client:
        text, record = call_llm(
            operation_route=ROUTE,
            messages=MESSAGES,
            config=config,
            store=store,
            http_client=client,
            max_attempts=3,
            sleep=_no_sleep,
        )
    assert text == ""
    assert record.provider_status is ProviderStatus.success
    assert handler.calls == 1  # no business retry


@pytest.mark.parametrize("status_code", [300, 302, 400, 401, 403, 404, 422])
def test_non_retryable_http_status_records_error_without_retry(
    status_code: int,
    config: RuntimeConfig,
    store: RunStore,
) -> None:
    handler = RecordingTransport(http(status_code))
    with mock_client(handler) as client:
        with pytest.raises(LlmTransportError) as excinfo:
            call_llm(
                operation_route=ROUTE,
                messages=MESSAGES,
                config=config,
                store=store,
                http_client=client,
                max_attempts=3,
                sleep=_no_sleep,
            )
    assert excinfo.value.record.provider_status is ProviderStatus.error
    assert handler.calls == 1
    assert excinfo.value.attempts == 1
    [record] = store.read_records(LLM_CALL_RECORDS)
    assert record.metadata["http_status"] == status_code
    assert record.metadata["retryable"] is False
    assert record.metadata["retry_scheduled"] is False
    assert "retry_delay_seconds" not in record.metadata


@pytest.mark.parametrize(
    "exc_type",
    [httpx.LocalProtocolError, httpx.UnsupportedProtocol],
)
def test_local_or_unsupported_protocol_error_is_not_retried(
    exc_type: type[httpx.TransportError],
    config: RuntimeConfig,
    store: RunStore,
) -> None:
    handler = RecordingTransport(raise_transport(exc_type))

    with mock_client(handler) as client:
        with pytest.raises(LlmTransportError) as excinfo:
            call_llm(
                operation_route=ROUTE,
                messages=MESSAGES,
                config=config,
                store=store,
                http_client=client,
                max_attempts=3,
                sleep=_no_sleep,
            )

    assert handler.calls == 1
    assert excinfo.value.attempts == 1
    [record] = store.read_records(LLM_CALL_RECORDS)
    assert record.provider_status is ProviderStatus.error
    assert record.metadata["retryable"] is False
    assert record.metadata["retry_scheduled"] is False


# === 5. Cost is a configured_estimate (0.0 placeholder vs configured rates) ===


def test_extract_usage_reads_token_split_without_fabrication() -> None:
    assert extract_usage({"usage": USAGE}) == EXPECTED_USAGE
    # a missing usage block stays all-None rather than being fabricated as zeros
    assert extract_usage({}) == Usage()


def test_estimate_cost_zero_under_placeholder_rates() -> None:
    usage = extract_usage({"usage": USAGE})
    assert estimate_cost(usage, CostProfile()) == 0.0
    # the shipped defaults are explicit 0.0 placeholders, never fabricated prices
    for profile in DEFAULT_COST_PROFILES.values():
        assert estimate_cost(usage, profile) == 0.0


def test_estimate_cost_uses_usage_tokens_under_configured_rates() -> None:
    usage = extract_usage({"usage": USAGE})
    profile = CostProfile(**RATES)
    assert estimate_cost(usage, profile) == pytest.approx(EXPECTED_COST)


def test_resolve_cost_profile_default_vs_config_override() -> None:
    profile, source = resolve_cost_profile("operator-rates", None)
    assert profile == CostProfile()  # placeholder zeros
    assert source == RATES_SOURCE_DEFAULT

    cfg = build_config(cost_profiles={"operator-rates": RATES})
    overridden, override_source = resolve_cost_profile("operator-rates", cfg)
    assert overridden == CostProfile(**RATES)
    assert override_source == RATES_SOURCE_CONFIG
    assert override_source != source  # source is recorded so estimate != invoice


def test_call_records_nonzero_cost_from_config_override(
    sentinel_key: None, store: RunStore
) -> None:
    cfg = build_config(cost_profiles={"operator-rates": RATES})
    handler = RecordingTransport(respond())
    with mock_client(handler) as client:
        _text, record = call_llm(
            operation_route=ROUTE,
            messages=MESSAGES,
            config=cfg,
            store=store,
            http_client=client,
            sleep=_no_sleep,
        )
    assert record.cost == pytest.approx(EXPECTED_COST)  # computed from usage tokens
    assert record.cost_kind is CostKind.configured_estimate  # always an estimate
