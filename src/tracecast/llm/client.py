"""OpenAI-compatible gateway boundary: the single place a real model call happens.

``call_llm`` is the stages' one call point. It resolves an ``operation_route``
against the runtime config, builds a canonical request envelope, performs the
real ``httpx`` chat-completions call against the provider ``base_url``, captures
usage, and returns ``(response_text, LlmCallRecord)`` while persisting the raw
request, raw response and the record through ``RunStore``.

Boundary discipline:

- It builds NO business prompts and parses NO business DSL: callers pass ready
  ``messages``; the returned text is handed back verbatim for the stage to parse.
- Exactly one ``LlmCallRecord`` per provider attempt; a retry never overwrites a
  prior record (each attempt gets a fresh ``call_id``).
- Transport-level retry only (timeout / connection / 5xx / 429 / empty body);
  never business retry, never auto-repair of the response body.
- ``provider_status`` (transport) is kept separate from any business parse
  status, which lives in stage reports.
- The API key is read via ``SecretStr.get_secret_value()`` only at the instant
  the request headers are built, and never enters the envelope, the record, any
  persisted payload, or any log/error string.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from tracecast.domain import (
    CostKind,
    LlmCallRecord,
    LlmWorkUnitRef,
    ProviderStatus,
    new_call_id,
)
from tracecast.storage import LLM_CALL_RECORDS, RunStore, RuntimeConfig, sha256_json, sha256_text

from .cost import Usage, estimate_cost, extract_usage, resolve_cost_profile

#: Backward-compatible default; a gateway may configure another path.
CHAT_COMPLETIONS_PATH = "/chat/completions"
_DEFAULT_TIMEOUT_SECONDS = 180.0


class LlmTransportError(RuntimeError):
    """Raised when every transport attempt for one call fails.

    Carries the final failed ``LlmCallRecord`` (already appended to the ledger)
    and the attempt count, so the calling stage surfaces/records the failure
    instead of silently falling back.
    """

    def __init__(self, message: str, *, record: LlmCallRecord, attempts: int) -> None:
        super().__init__(message)
        self.record = record
        self.attempts = attempts


@dataclass(frozen=True)
class _CallContext:
    """Per-call invariants shared by every attempt's record."""

    stage: str
    operation_route: str
    provider: str
    model: str
    key_id: str
    prompt_version: str | None
    input_hash: str
    cost_profile_name: str | None
    rates_source: str
    max_attempts: int
    backoff_base_seconds: float
    backoff_max_seconds: float
    jitter_ratio: float
    work_unit: LlmWorkUnitRef | None


def build_request_envelope(
    *,
    stage: str,
    operation_route: str,
    prompt_version: str | None,
    provider: str,
    model: str,
    thinking: str | None,
    temperature: float | None,
    max_output_tokens: int | None,
    timeout_seconds: int | float,
    messages: Sequence[Mapping[str, Any]],
    request_body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Canonical provider request envelope -- the basis of ``input_hash``.

    Carries the audit identity (stage / route / prompt_version) and every model
    parameter that affects the output, so resume can tell whether two calls share
    the exact same input. Deliberately excludes the API key, raw output paths and
    any machine-varying temp path.
    """
    body = (
        dict(request_body)
        if request_body is not None
        else _build_body(
            model=model,
            messages=messages,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            max_output_tokens_field="max_tokens",
            thinking=thinking,
            extra_body=None,
        )
    )
    return {
        "stage": stage,
        "operation_route": operation_route,
        "prompt_version": prompt_version,
        "provider": provider,
        "model": model,
        # Retained as semantic audit fields for existing ledger readers. The
        # exact gateway payload below is authoritative for wire-level hashing.
        "thinking": thinking,
        "temperature": temperature,
        "max_output_tokens": max_output_tokens,
        "timeout_seconds": timeout_seconds,
        "messages": [dict(message) for message in messages],
        "request_body": body,
    }


def _thinking_param(thinking: str | None) -> dict[str, Any] | None:
    """Wire representation of the route's thinking flag, or ``None`` when unset.

    This is the one-release compatibility mapping for existing runtime files.
    New gateway configurations should express reasoning controls in ``extra_body``.
    """
    if thinking is None:
        return None
    return {"type": thinking}


def _build_body(
    *,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    temperature: float | None,
    max_output_tokens: int | None,
    max_output_tokens_field: str,
    thinking: str | None,
    extra_body: Mapping[str, Any] | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [dict(message) for message in messages],
        "stream": False,
    }
    if temperature is not None:
        body["temperature"] = temperature
    if max_output_tokens is not None:
        body[max_output_tokens_field] = max_output_tokens
    thinking_param = _thinking_param(thinking)
    if thinking_param is not None:
        body["thinking"] = thinking_param
    if extra_body:
        body.update(extra_body)
    return body


def _extract_text(response_json: Mapping[str, Any]) -> str:
    """Best-effort visible content of the first choice; ``""`` if absent.

    Observer does not business-judge: a 2xx with no parseable content still
    returns successfully (empty text) and lets the stage report the problem. The
    full raw response is on disk regardless.
    """
    try:
        message = response_json["choices"][0]["message"]
        content = message.get("content")
    except (KeyError, IndexError, TypeError):
        return ""
    return content if isinstance(content, str) else ""


def _safe_json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _redact_secret(payload: Any, secret: str) -> Any:
    """Remove an API-key value from provider-controlled payloads before persistence."""

    if isinstance(payload, str):
        return payload.replace(secret, "[REDACTED]")
    if isinstance(payload, Mapping):
        return {
            _redact_secret(key, secret) if isinstance(key, str) else key: _redact_secret(
                value, secret
            )
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [_redact_secret(value, secret) for value in payload]
    return payload


def _error_text(exc: Exception) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _parse_retry_after(value: str | None, *, now: datetime) -> float | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        seconds = float(stripped)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(stripped)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = (retry_at.astimezone(UTC) - now.astimezone(UTC)).total_seconds()
    return seconds if seconds >= 0 else None


def _retry_delay(
    *,
    base: float,
    cap: float,
    jitter_ratio: float,
    attempt: int,
    retry_after: str | None,
    random_value: Callable[[], float],
    now: datetime,
) -> tuple[float, str, float | None]:
    nominal = min(cap, base * (2**attempt))
    sample = min(max(random_value(), 0.0), 1.0)
    factor = 1 + jitter_ratio * (2 * sample - 1)
    backoff = min(cap, nominal * factor)
    retry_after_seconds = _parse_retry_after(retry_after, now=now)
    if retry_after_seconds is not None and retry_after_seconds > backoff:
        return min(cap, retry_after_seconds), "retry_after", retry_after_seconds
    return backoff, "backoff", retry_after_seconds


def _default_key_id(provider_cfg: Any, provider_name: str) -> str:
    if not provider_cfg.keys:
        raise ValueError(f"provider {provider_name!r} has no keys configured")
    return provider_cfg.keys[0].key_id


def _base_metadata(ctx: _CallContext, attempt: int, http_status: int | None) -> dict[str, Any]:
    return {
        "attempt": attempt + 1,
        "max_attempts": ctx.max_attempts,
        "http_status": http_status,
        "cost_profile": ctx.cost_profile_name,
        "cost_rates_source": ctx.rates_source,
        "transport_backoff_base_seconds": ctx.backoff_base_seconds,
        "transport_backoff_max_seconds": ctx.backoff_max_seconds,
        "transport_jitter_ratio": ctx.jitter_ratio,
    }


def _retry_decision(
    ctx: _CallContext,
    *,
    attempt: int,
    failure_kind: str,
    retryable: bool,
    retry_after: str | None,
    random_value: Callable[[], float],
    now: datetime,
) -> tuple[dict[str, Any], float | None]:
    scheduled = retryable and attempt + 1 < ctx.max_attempts
    metadata: dict[str, Any] = {
        "failure_kind": failure_kind,
        "retryable": retryable,
        "retry_scheduled": scheduled,
    }
    if not scheduled:
        return metadata, None
    delay, source, parsed_retry_after = _retry_delay(
        base=ctx.backoff_base_seconds,
        cap=ctx.backoff_max_seconds,
        jitter_ratio=ctx.jitter_ratio,
        attempt=attempt,
        retry_after=retry_after,
        random_value=random_value,
        now=now,
    )
    metadata["retry_delay_seconds"] = delay
    metadata["retry_delay_source"] = source
    if parsed_retry_after is not None:
        metadata["retry_after_seconds"] = parsed_retry_after
    return metadata, delay


def _transport_failure_kind(exc: httpx.TransportError) -> tuple[str, bool]:
    if isinstance(exc, httpx.ProxyError):
        return "proxy_error", True
    if isinstance(exc, httpx.RemoteProtocolError):
        return "remote_protocol_error", True
    if isinstance(exc, httpx.NetworkError):
        return "network_error", True
    if isinstance(exc, httpx.LocalProtocolError):
        return "local_protocol_error", False
    if isinstance(exc, httpx.UnsupportedProtocol):
        return "unsupported_protocol", False
    return "transport_error", False


def _failure_record(
    ctx: _CallContext,
    *,
    call_id: str,
    created_at: datetime,
    attempt: int,
    provider_status: ProviderStatus,
    latency_ms: int,
    raw_request_path: str,
    raw_response_path: str,
    error: str,
    http_status: int | None,
    retry_metadata: Mapping[str, Any],
) -> LlmCallRecord:
    return LlmCallRecord(
        call_id=call_id,
        stage=ctx.stage,
        operation_route=ctx.operation_route,
        provider=ctx.provider,
        model=ctx.model,
        key_id=ctx.key_id,
        prompt_version=ctx.prompt_version,
        input_hash=ctx.input_hash,
        provider_status=provider_status,
        created_at=created_at,
        raw_request_path=raw_request_path,
        raw_response_path=raw_response_path,
        latency_ms=latency_ms,
        error=error,
        work_unit=ctx.work_unit,
        metadata={
            **_base_metadata(ctx, attempt, http_status),
            **retry_metadata,
        },
    )


def _success_record(
    ctx: _CallContext,
    *,
    call_id: str,
    created_at: datetime,
    attempt: int,
    latency_ms: int,
    raw_request_path: str,
    raw_response_path: str,
    response_text: str,
    usage: Usage,
    cost: float,
    http_status: int,
) -> LlmCallRecord:
    return LlmCallRecord(
        call_id=call_id,
        stage=ctx.stage,
        operation_route=ctx.operation_route,
        provider=ctx.provider,
        model=ctx.model,
        key_id=ctx.key_id,
        prompt_version=ctx.prompt_version,
        input_hash=ctx.input_hash,
        provider_status=ProviderStatus.success,
        created_at=created_at,
        raw_request_path=raw_request_path,
        raw_response_path=raw_response_path,
        output_hash=f"sha256:{sha256_text(response_text)}",
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        uncached_input_tokens=usage.uncached_input_tokens,
        reasoning_output_tokens=usage.reasoning_output_tokens,
        visible_output_tokens=usage.visible_output_tokens,
        cost=cost,
        cost_kind=CostKind.configured_estimate,
        latency_ms=latency_ms,
        error=None,
        work_unit=ctx.work_unit,
        metadata=_base_metadata(ctx, attempt, http_status),
    )


def _persist_failure(
    store: RunStore,
    ctx: _CallContext,
    *,
    call_id: str,
    created_at: datetime,
    attempt: int,
    provider_status: ProviderStatus,
    latency_ms: int,
    raw_request_path: str,
    raw_response: Mapping[str, Any] | str,
    error: str,
    http_status: int | None,
    retry_metadata: Mapping[str, Any],
) -> LlmCallRecord:
    raw_response_path = store.write_raw_payload(call_id, "response", raw_response)
    record = _failure_record(
        ctx,
        call_id=call_id,
        created_at=created_at,
        attempt=attempt,
        provider_status=provider_status,
        latency_ms=latency_ms,
        raw_request_path=raw_request_path,
        raw_response_path=raw_response_path,
        error=error,
        http_status=http_status,
        retry_metadata=retry_metadata,
    )
    store.append_record(LLM_CALL_RECORDS, record)
    return record


def call_llm(
    *,
    operation_route: str,
    messages: Sequence[Mapping[str, Any]],
    config: RuntimeConfig,
    store: RunStore,
    stage: str | None = None,
    prompt_version: str | None = None,
    key_id: str | None = None,
    work_unit: LlmWorkUnitRef | None = None,
    max_attempts: int | None = None,
    http_client: httpx.Client | None = None,
    env: Mapping[str, str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    backoff_seconds: float | None = None,
    backoff_max_seconds: float | None = None,
    jitter_ratio: float | None = None,
    random_value: Callable[[], float] = random.random,
) -> tuple[str, LlmCallRecord]:
    """Resolve a route, call the provider, return ``(response_text, record)``.

    The stages' single provider-call entry point. ``operation_route`` is looked
    up in ``config`` for the model config; ``messages`` is the already-built
    OpenAI-style chat request. ``stage`` defaults to the route's declared stage.
    ``key_id`` defaults to the provider's first key.

    Persists one raw request, one raw response and one ``LlmCallRecord`` per
    provider attempt through ``store``. Retries transport failures only, up to
    ``max_attempts``; on terminal failure raises :class:`LlmTransportError`
    carrying the last (already-persisted) record. Inject ``http_client`` (e.g. an
    ``httpx.Client`` on a ``MockTransport``) and ``env`` to exercise this without
    any network call or real secret.
    """
    route = config.route(operation_route)
    effective_max_attempts = (
        route.transport_max_attempts if max_attempts is None else max_attempts
    )
    effective_backoff_base = (
        route.transport_backoff_base_seconds
        if backoff_seconds is None
        else backoff_seconds
    )
    effective_backoff_max = (
        route.transport_backoff_max_seconds
        if backoff_max_seconds is None
        else backoff_max_seconds
    )
    effective_jitter_ratio = (
        route.transport_jitter_ratio if jitter_ratio is None else jitter_ratio
    )
    if effective_max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if effective_backoff_base <= 0 or effective_backoff_max <= 0:
        raise ValueError("transport backoff seconds must be positive")
    if effective_backoff_base > effective_backoff_max:
        raise ValueError("backoff_seconds must be <= backoff_max_seconds")
    if not 0 <= effective_jitter_ratio <= 1:
        raise ValueError("jitter_ratio must be in [0, 1]")

    provider_cfg = config.provider(route.provider)
    record_stage = stage if stage is not None else route.stage
    resolved_key_id = (
        key_id if key_id is not None else _default_key_id(provider_cfg, route.provider)
    )
    # Resolve to a masked SecretStr now; the raw value is only read at header build.
    secret = provider_cfg.key(resolved_key_id).resolve_secret(env=env)
    secret_value = secret.get_secret_value()

    timeout = (
        route.timeout_seconds
        if route.timeout_seconds is not None
        else _DEFAULT_TIMEOUT_SECONDS
    )
    url = provider_cfg.base_url.rstrip("/") + provider_cfg.chat_completions_path

    body = _build_body(
        model=route.model,
        messages=messages,
        temperature=route.temperature,
        max_output_tokens=route.max_output_tokens,
        max_output_tokens_field=provider_cfg.max_output_tokens_field,
        thinking=route.thinking,
        extra_body=route.extra_body,
    )

    envelope = build_request_envelope(
        stage=record_stage,
        operation_route=operation_route,
        prompt_version=prompt_version,
        provider=route.provider,
        model=route.model,
        thinking=route.thinking,
        temperature=route.temperature,
        max_output_tokens=route.max_output_tokens,
        timeout_seconds=timeout,
        messages=messages,
        request_body=body,
    )
    input_hash = f"sha256:{sha256_json(envelope)}"
    cost_profile, rates_source = resolve_cost_profile(route.cost_profile, config)
    ctx = _CallContext(
        stage=record_stage,
        operation_route=operation_route,
        provider=route.provider,
        model=route.model,
        key_id=resolved_key_id,
        prompt_version=prompt_version,
        input_hash=input_hash,
        cost_profile_name=route.cost_profile,
        rates_source=rates_source,
        max_attempts=effective_max_attempts,
        backoff_base_seconds=effective_backoff_base,
        backoff_max_seconds=effective_backoff_max,
        jitter_ratio=effective_jitter_ratio,
        work_unit=work_unit,
    )

    owns_client = http_client is None
    client = httpx.Client() if http_client is None else http_client
    last_record: LlmCallRecord | None = None
    last_exc: Exception | None = None
    attempts_made = 0
    try:
        for attempt in range(effective_max_attempts):
            attempts_made = attempt + 1
            call_id = new_call_id()
            created_at = datetime.now(tz=UTC)
            raw_request_path = store.write_raw_payload(call_id, "request", envelope)
            # The only point the raw secret materialises: a local header dict that
            # is never persisted, logged, or placed in the envelope/record.
            headers = {
                "Authorization": f"Bearer {secret_value}",
                "Content-Type": "application/json",
            }
            started = time.perf_counter()
            try:
                response = client.post(url, json=body, headers=headers, timeout=timeout)
            except httpx.TimeoutException as exc:
                last_exc = exc
                error_text = _redact_secret(_error_text(exc), secret_value)
                retry_metadata, retry_delay = _retry_decision(
                    ctx,
                    attempt=attempt,
                    failure_kind="timeout",
                    retryable=True,
                    retry_after=None,
                    random_value=random_value,
                    now=datetime.now(tz=UTC),
                )
                last_record = _persist_failure(
                    store,
                    ctx,
                    call_id=call_id,
                    created_at=created_at,
                    attempt=attempt,
                    provider_status=ProviderStatus.timeout,
                    latency_ms=_elapsed_ms(started),
                    raw_request_path=raw_request_path,
                    raw_response={"error": error_text},
                    error=error_text,
                    http_status=None,
                    retry_metadata=retry_metadata,
                )
                if retry_delay is not None:
                    sleep(retry_delay)
                    continue
                break
            except httpx.TransportError as exc:
                last_exc = exc
                error_text = _redact_secret(_error_text(exc), secret_value)
                failure_kind, retryable = _transport_failure_kind(exc)
                retry_metadata, retry_delay = _retry_decision(
                    ctx,
                    attempt=attempt,
                    failure_kind=failure_kind,
                    retryable=retryable,
                    retry_after=None,
                    random_value=random_value,
                    now=datetime.now(tz=UTC),
                )
                last_record = _persist_failure(
                    store,
                    ctx,
                    call_id=call_id,
                    created_at=created_at,
                    attempt=attempt,
                    provider_status=ProviderStatus.error,
                    latency_ms=_elapsed_ms(started),
                    raw_request_path=raw_request_path,
                    raw_response={"error": error_text},
                    error=error_text,
                    http_status=None,
                    retry_metadata=retry_metadata,
                )
                if retry_delay is not None:
                    sleep(retry_delay)
                    continue
                break

            latency_ms = _elapsed_ms(started)
            status_code = response.status_code

            if status_code >= 300:
                last_exc = None
                error_text = f"provider returned HTTP {status_code}"
                retryable = (
                    status_code == 408
                    or status_code == 429
                    or 500 <= status_code <= 599
                )
                retry_metadata, retry_delay = _retry_decision(
                    ctx,
                    attempt=attempt,
                    failure_kind="http_status",
                    retryable=retryable,
                    retry_after=response.headers.get("Retry-After"),
                    random_value=random_value,
                    now=datetime.now(tz=UTC),
                )
                last_record = _persist_failure(
                    store,
                    ctx,
                    call_id=call_id,
                    created_at=created_at,
                    attempt=attempt,
                    provider_status=ProviderStatus.error,
                    latency_ms=latency_ms,
                    raw_request_path=raw_request_path,
                    raw_response=_redact_secret(response.text, secret_value),
                    error=error_text,
                    http_status=status_code,
                    retry_metadata=retry_metadata,
                )
                if retry_delay is not None:
                    sleep(retry_delay)
                    continue
                break

            if not response.text.strip():
                last_exc = None
                error_text = "provider returned an empty response body"
                retry_metadata, retry_delay = _retry_decision(
                    ctx,
                    attempt=attempt,
                    failure_kind="empty_response_body",
                    retryable=True,
                    retry_after=response.headers.get("Retry-After"),
                    random_value=random_value,
                    now=datetime.now(tz=UTC),
                )
                last_record = _persist_failure(
                    store,
                    ctx,
                    call_id=call_id,
                    created_at=created_at,
                    attempt=attempt,
                    provider_status=ProviderStatus.error,
                    latency_ms=latency_ms,
                    raw_request_path=raw_request_path,
                    raw_response={"error": error_text},
                    error=error_text,
                    http_status=status_code,
                    retry_metadata=retry_metadata,
                )
                if retry_delay is not None:
                    sleep(retry_delay)
                    continue
                break

            # 2xx with a non-empty body -> transport success. Content validity is
            # the stage's concern, not the observer's.
            response_json = _safe_json(response)
            if response_json is not None:
                safe_response_json = _redact_secret(response_json, secret_value)
                raw_response_path = store.write_raw_payload(
                    call_id, "response", safe_response_json
                )
                response_text = _extract_text(safe_response_json)
                usage = extract_usage(safe_response_json)
            else:
                response_text = _redact_secret(response.text, secret_value)
                raw_response_path = store.write_raw_payload(
                    call_id, "response", response_text
                )
                usage = extract_usage({})
            cost = estimate_cost(usage, cost_profile)
            record = _success_record(
                ctx,
                call_id=call_id,
                created_at=created_at,
                attempt=attempt,
                latency_ms=latency_ms,
                raw_request_path=raw_request_path,
                raw_response_path=raw_response_path,
                response_text=response_text,
                usage=usage,
                cost=cost,
                http_status=status_code,
            )
            store.append_record(LLM_CALL_RECORDS, record)
            return response_text, record
    finally:
        if owns_client:
            client.close()

    raise LlmTransportError(
        f"all {attempts_made} transport attempt(s) for route {operation_route!r} failed",
        record=last_record,
        attempts=attempts_made,
    ) from last_exc
