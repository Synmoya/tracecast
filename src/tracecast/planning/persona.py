"""Host entry layer for Selection.

Selection consumes the Host thinking layer plus run-local feedback, while
expression is only a light path reference and remains primarily a Rendering
concern.

This module owns the **input shape Selection consumes** and how it is rendered
into the replaceable persona block of the selector user prompt. It is an
injected parameter -- the driver supplies the concrete project content; no Host
content is hard-coded here. It is deliberately *not* a domain artifact, so it is
a plain config model rather than a persisted record.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tracecast.control_prompt import ControlPromptLocale, coerce_control_prompt_locale


class ViewpointProfile(BaseModel):
    """The Selection feedback layer: per-run human bias, not a fact source.

    Influences which nodes are *more likely* to enter the path and the leaning
    of role/treatment/directive; it never authorizes facts absent from the
    materials, never reorders reveals, and never writes prose.
    """

    model_config = ConfigDict(extra="forbid")

    prefer: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    human_directive: list[str] = Field(default_factory=list)


class PersonaConfig(BaseModel):
    """Persona/viewpoint/expression config the selector consumes.

    ``persona_id`` is the external Selection input persisted on
    ``SelectionPlan.persona_id``. ``viewpoint`` is the opinion layer (closest to
    Selection); ``expression`` is the expression layer (closest to Rendering,
    carried for completeness and lightly projected); ``viewpoint_profile`` is the
    per-run feedback layer.
    """

    model_config = ConfigDict(extra="forbid")

    persona_id: str
    viewpoint: str | None = None
    expression: str | None = None
    viewpoint_profile: ViewpointProfile = Field(default_factory=ViewpointProfile)
    thinking_rules: list[str] = Field(default_factory=list)
    host_version: str | None = None
    host_content_hash: str | None = None

    @classmethod
    def coerce(cls, persona: PersonaConfig | Mapping[str, Any]) -> PersonaConfig:
        """Accept either a ``PersonaConfig`` or a matching mapping from the driver."""
        if isinstance(persona, cls):
            return persona
        if isinstance(persona, Mapping):
            return cls.model_validate(dict(persona))
        raise TypeError(
            "persona must be a PersonaConfig or a mapping with a 'persona_id' field"
        )


def _bullets(label: str, items: list[str]) -> list[str]:
    if not items:
        return []
    return [f"{label}:", *(f"- {item}" for item in items)]


def render_persona_block(
    persona: PersonaConfig,
    *,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> str:
    """Render the replaceable persona block inserted into the selector user prompt.

    Only non-empty layers are emitted, so an experiment can swap a thin or rich
    persona without changing the fixed system layer.
    """
    locale = coerce_control_prompt_locale(control_prompt_locale)
    lines: list[str] = ["[persona]", f"persona_id: {persona.persona_id}"]
    if locale is ControlPromptLocale.zh:
        viewpoint_label = "观点层(viewpoint, 路径/解释偏向)"
        thinking_label = "host 稳定思维规则(thinking_rules)"
        prefer_label = "本轮更想选择(prefer)"
        avoid_label = "本轮应降低权重(avoid)"
        directive_label = "人工指令(human_directive)"
        expression_label = "表达偏好(expression, selection轻参考)"
        boundary = (
            "把 viewpoint/viewpoint_profile 作为同一位总编的问题意识和取舍偏向；"
            "expression 在 Selection 侧提供主持人姿态的轻参考，最终句式与声音由 Rendering 完成。"
        )
    else:
        viewpoint_label = "viewpoint (path and interpretation preference)"
        thinking_label = "stable host thinking rules"
        prefer_label = "prefer in this run"
        avoid_label = "deprioritize in this run"
        directive_label = "operator path requirements"
        expression_label = "expression (light Selection reference)"
        boundary = (
            "Treat viewpoint and viewpoint_profile as one editor's question and "
            "selection bias. Expression is only a light host-posture reference in "
            "Selection; Rendering owns final sentences and voice."
        )
    if persona.viewpoint:
        lines.append(f"{viewpoint_label}: {persona.viewpoint}")
    lines += _bullets(thinking_label, persona.thinking_rules)
    profile = persona.viewpoint_profile
    lines += _bullets(prefer_label, profile.prefer)
    lines += _bullets(avoid_label, profile.avoid)
    lines += _bullets(directive_label, profile.human_directive)
    if persona.expression:
        lines.append(f"{expression_label}: {persona.expression}")
    lines.append(boundary)
    return "\n".join(lines)
