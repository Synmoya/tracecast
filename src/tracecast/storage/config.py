"""Runtime configuration model and secure provider-key resolution.

The model mirrors ``configs/runtime.json`` (same shape as
``configs/runtime.example.json``): ``providers``, ``routes`` and per-stage
``stage_budgets``.

Security boundary -- the config stores only the *name* of the environment
variable that holds each gateway key (``api_key_env``), never the secret value.
The secret is read from the
process environment lazily, at the point of use, via
:meth:`ProviderKey.resolve_secret`, and is wrapped in ``pydantic.SecretStr`` so
it is masked in any ``repr``/log. The resolved secret is never a model field, so
``model_dump``/``model_dump_json`` of any config object -- and therefore anything
persisted under ``runs/`` -- can never contain it.

Config is operator-authored, not a code-owned artifact, so the models tolerate
forward-compatible additions (``extra="allow"``) while still validating every
field currently defined by the example config.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

DEFAULT_RUNTIME_CONFIG_PATH = Path("configs/runtime.json")


class _ConfigModel(BaseModel):
    # Operator-owned config: validate known fields but accept new ones rather
    # than hard-failing an otherwise-valid deployment config.
    model_config = ConfigDict(extra="allow")


class ProviderKey(_ConfigModel):
    """One provider API key, referenced by environment-variable name only."""

    key_id: str
    api_key_env: str
    max_concurrency: int | None = Field(default=None, ge=0)

    def resolve_secret(self, env: Mapping[str, str] | None = None) -> SecretStr:
        """Read the secret from the environment at point of use.

        Raises if the variable is unset or empty -- never returns a silent empty
        secret. The returned ``SecretStr`` masks itself in logs/reprs; call
        ``.get_secret_value()`` only at the provider boundary.
        """
        source = os.environ if env is None else env
        try:
            value = source[self.api_key_env]
        except KeyError:
            raise KeyError(
                f"environment variable {self.api_key_env!r} is not set "
                f"(provider key {self.key_id!r})"
            ) from None
        if not value:
            raise ValueError(
                f"environment variable {self.api_key_env!r} is empty "
                f"(provider key {self.key_id!r})"
            )
        return SecretStr(value)


class ProviderLimits(_ConfigModel):
    """Legacy declarations accepted on read; the runtime does not schedule them."""

    max_concurrency: int | None = Field(default=None, ge=0)
    requests_per_minute: int | None = Field(default=None, ge=0)


class ProviderConfig(_ConfigModel):
    """One OpenAI-compatible Chat Completions gateway endpoint."""

    base_url: str
    chat_completions_path: str = "/chat/completions"
    max_output_tokens_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    keys: list[ProviderKey] = Field(default_factory=list)
    limits: ProviderLimits | None = None

    @field_validator("chat_completions_path")
    @classmethod
    def _validate_chat_completions_path(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith("/") or value.startswith("//"):
            raise ValueError("chat_completions_path must start with one '/' character")
        return value

    def key(self, key_id: str) -> ProviderKey:
        for candidate in self.keys:
            if candidate.key_id == key_id:
                return candidate
        raise KeyError(f"unknown provider key id {key_id!r}")


class RouteConfig(_ConfigModel):
    """A named model-config route (the ``operation_route`` of a call)."""

    stage: str
    provider: str
    model: str
    #: Bundled ``prompt/...`` path or explicit external prompt path; the version
    #: header becomes the call's ``prompt_version``.
    system_prompt: str | None = None
    #: Optional path to this route's user-prompt template. Code fills dynamic
    #: placeholders; prose stays in prompt files instead of projection code.
    user_prompt: str | None = None
    #: Optional retry template for program-reported parse/validation failures.
    repair_prompt: str | None = None
    # One-release compatibility for existing runtime files. New gateway
    # configurations use ``extra_body`` for provider-specific request options.
    thinking: str | None = None
    extra_body: dict[str, Any] = Field(default_factory=dict)
    temperature: float | None = None
    max_output_tokens: int | None = Field(default=None, ge=0)
    timeout_seconds: int | None = Field(default=None, ge=0)
    max_concurrency: int | None = Field(default=None, ge=0)
    transport_max_attempts: int = Field(default=3, ge=1)
    transport_backoff_base_seconds: float = Field(default=1.0, gt=0)
    transport_backoff_max_seconds: float = Field(default=30.0, gt=0)
    transport_jitter_ratio: float = Field(default=0.25, ge=0, le=1)
    cost_profile: str | None = None

    @field_validator("extra_body")
    @classmethod
    def _protect_core_request_fields(cls, value: dict[str, Any]) -> dict[str, Any]:
        reserved = {
            "model",
            "messages",
            "stream",
            "temperature",
            "max_tokens",
            "max_completion_tokens",
            "thinking",
        }
        conflicts = sorted(reserved.intersection(value))
        if conflicts:
            raise ValueError(
                "extra_body cannot override core request field(s): " + ", ".join(conflicts)
            )
        return value

    @model_validator(mode="after")
    def _validate_transport_backoff(self) -> RouteConfig:
        if self.transport_backoff_base_seconds > self.transport_backoff_max_seconds:
            raise ValueError(
                "transport_backoff_base_seconds must be "
                "<= transport_backoff_max_seconds"
            )
        return self


class RuntimeConfig(_ConfigModel):
    """Top-level runtime config mirroring ``configs/runtime.json``."""

    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    routes: dict[str, RouteConfig] = Field(default_factory=dict)
    stage_budgets: dict[str, dict[str, bool | int | float]] = Field(default_factory=dict)

    @classmethod
    def load(
        cls,
        path: str | os.PathLike[str] = DEFAULT_RUNTIME_CONFIG_PATH,
        *,
        load_env: bool = True,
        env_file: str | os.PathLike[str] | None = None,
    ) -> RuntimeConfig:
        """Load and validate the runtime config from a JSON file.

        When ``load_env`` is set (default) a ``.env`` file is loaded into the
        process environment first, so the env-var names referenced by
        ``api_key_env`` resolve at point of use. The config JSON itself holds no
        secrets, only those variable names.
        """
        if load_env:
            load_dotenv_file(env_file)
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(data)

    def provider(self, name: str) -> ProviderConfig:
        try:
            return self.providers[name]
        except KeyError:
            raise KeyError(f"unknown provider {name!r}") from None

    def route(self, name: str) -> RouteConfig:
        try:
            return self.routes[name]
        except KeyError:
            raise KeyError(f"unknown route {name!r}") from None


def load_dotenv_file(
    env_file: str | os.PathLike[str] | None = None, *, override: bool = False
) -> None:
    """Load a ``.env`` file into ``os.environ`` for later key resolution.

    With ``override=False`` (default) values already present in the real
    environment win over the file. ``python-dotenv`` (a ``pydantic-settings``
    dependency) is imported lazily so importing ``tracecast.storage`` never
    depends on it.
    """
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError as exc:  # pragma: no cover - dependency always present in env
        raise RuntimeError(
            "python-dotenv is required to load .env files; install pydantic-settings"
        ) from exc
    path = os.fspath(env_file) if env_file is not None else find_dotenv(usecwd=True)
    if path:
        # API keys are opaque bytes represented as text. Variable interpolation
        # would corrupt a legitimate key containing a `${NAME}` sequence even
        # when the assignment is quoted.
        load_dotenv(dotenv_path=path, override=override, interpolate=False)
