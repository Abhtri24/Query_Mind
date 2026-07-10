"""
profile_context.py
------------------
Compact prompt context for optional database business profiles.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _clean_string_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _clean_glossary(value: Any) -> list[tuple[str, str]]:
    if not isinstance(value, Mapping):
        return []
    pairs = []
    for key, val in value.items():
        clean_key = key.strip() if isinstance(key, str) else None
        clean_val = val.strip() if isinstance(val, str) else None
        if clean_key and clean_val:
            pairs.append((clean_key, clean_val))
    return pairs


def build_profile_context(profile: Any) -> str:
    """
    Convert a DBConnection or profile-like mapping into a compact prompt block.
    Empty fields are omitted.
    """
    get_value = profile.get if isinstance(profile, Mapping) else lambda key, default=None: getattr(profile, key, default)

    sections: list[str] = []

    description = _clean_text(get_value("description"))
    if description:
        sections.append(f"Database Description:\n{description}")

    business_context = _clean_text(get_value("business_context"))
    if business_context:
        sections.append(f"Business Context:\n{business_context}")

    glossary = _clean_glossary(get_value("glossary_json", get_value("glossary")))
    if glossary:
        sections.append("Glossary:\n" + "\n".join(f"{key} = {val}" for key, val in glossary))

    important_tables = _clean_string_list(get_value("important_tables_json", get_value("important_tables")))
    if important_tables:
        sections.append("Important Tables:\n" + "\n".join(important_tables))

    ignored_tables = _clean_string_list(get_value("ignored_tables_json", get_value("ignored_tables")))
    if ignored_tables:
        sections.append("Ignored Tables:\n" + "\n".join(ignored_tables))

    return "\n\n".join(sections)
