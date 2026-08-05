"""Project-owned applicability policy for pre-render name resolution."""

from enum import StrEnum


class NameResolutionMode(StrEnum):
    """Choose whether a run performs automatic run-level name editing."""

    overrides_only = "overrides_only"
    name_table = "name_table"


DEFAULT_NAME_RESOLUTION_MODE = NameResolutionMode.overrides_only


def coerce_name_resolution_mode(
    value: NameResolutionMode | str,
) -> NameResolutionMode:
    """Load a current mode, retiring the old automatic mode without new spend."""

    if value == "relation_scan":
        return NameResolutionMode.overrides_only
    return NameResolutionMode(value)
