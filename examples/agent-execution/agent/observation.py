"""Bounded JSON previews and observation framing, without full oversized copies."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from typing import Any

TRUNCATION_MARKER = "\n[TRUNCATED: character limit]\n"
OBSERVATION_BEGIN = "=== BEGIN TOOL OBSERVATION (UNTRUSTED DATA) ===\n"
OBSERVATION_END = "\n=== END TOOL OBSERVATION ==="


def bounded_text(text: str, limit: int) -> str:
    """The marker is part of the character budget."""
    if limit < len(TRUNCATION_MARKER):
        raise ValueError("Character limit is too small for the truncation marker")
    return (
        text if len(text) <= limit else text[: limit - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
    )


def exception_message(error: Exception) -> str:
    """A broken exception formatter must not prevent trace finalization."""
    try:
        return bounded_text(str(error), 1024)
    except Exception:  # noqa: BLE001 -- diagnostic formatting is itself an untrusted boundary.
        return type(error).__name__


def bounded_json(value: Any, limit: int) -> tuple[str, bool]:
    """Serialize finite JSON data only as far as the bounded preview needs.

    A truncated preview is explicitly incomplete JSON. Strings are sliced before
    escaping; container iteration stops at the limit, avoiding a full JSON copy.
    Tool-owned allocations before the boundary are outside this helper's control.
    """
    if limit < len(TRUNCATION_MARKER):
        raise ValueError("Character limit is too small for the truncation marker")
    pieces: list[str] = []
    length = 0
    for piece in _json_pieces(value, limit, set(), 0):
        if length + len(piece) > limit:
            preview = "".join(pieces) + piece[: limit - length]
            return preview[: limit - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER, True
        pieces.append(piece)
        length += len(piece)
    return "".join(pieces), False


def _json_pieces(value: Any, limit: int, active: set[int], depth: int) -> Iterator[str]:
    if depth > 64:
        raise ValueError("JSON nesting exceeds 64 levels")
    if type(value) is str:
        yield json.dumps(value[: limit + 1], ensure_ascii=True)
    elif value is None or type(value) in (bool, int):
        yield json.dumps(value)
    elif type(value) is float and math.isfinite(value):
        yield json.dumps(value, allow_nan=False)
    elif type(value) in (dict, list):
        identity = id(value)
        if identity in active:
            raise ValueError("Circular tool result")
        active.add(identity)
        try:
            yield "{" if isinstance(value, dict) else "["
            for index, entry in enumerate(value.items() if isinstance(value, dict) else value):
                if index:
                    yield ","
                if isinstance(value, dict):
                    key, item = entry
                    if not isinstance(key, str):
                        raise TypeError("Tool result JSON keys must be strings")
                    yield json.dumps(key[: limit + 1])
                    yield ":"
                else:
                    item = entry
                yield from _json_pieces(item, limit, active, depth + 1)
            yield "}" if isinstance(value, dict) else "]"
        finally:
            active.remove(identity)
    else:
        raise TypeError("Tool result must contain finite JSON data")


def format_observation(text: str, limit: int) -> str:
    """Keep both untrusted-data delimiters intact even when truncating."""
    budget = limit - len(OBSERVATION_BEGIN) - len(OBSERVATION_END)
    return OBSERVATION_BEGIN + bounded_text(text, budget) + OBSERVATION_END
