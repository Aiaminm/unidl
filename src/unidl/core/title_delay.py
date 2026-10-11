"""Parsing and sampling for the optional inter-title download delay.

The delay is deliberately a batch scheduler policy.  It is not a segment
throttle and must not be passed down to individual track workers.
"""

from __future__ import annotations

import random
import re
from collections.abc import Sequence

DEFAULT_TITLE_DELAY = (60, 120)
MIN_TITLE_DELAY = 5
MAX_TITLE_DELAY = 24 * 60 * 60

_OFF_VALUES = {"", "0", "false", "no", "off", "none", "disabled"}
_RANGE = re.compile(r"^(\d+)\s*-\s*(\d+)$")


def parse_title_delay(value: object) -> tuple[int, int] | None:
    """Return a validated ``(minimum, maximum)`` delay range in seconds.

    ``True`` selects the reference default of 60–120 seconds.  A single number
    is accepted as a fixed delay, which is represented canonically as ``N-N``.
    Empty/off values disable the policy.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        return DEFAULT_TITLE_DELAY if value else None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) != 2:
            raise ValueError("title delay must contain two values: MIN-MAX")
        low, high = value
    else:
        raw = str(value).strip().lower()
        if raw in _OFF_VALUES:
            return None
        match = _RANGE.fullmatch(raw)
        if match:
            low, high = match.groups()
        elif raw.isdigit():
            low = high = raw
        else:
            raise ValueError("use off, a number of seconds, or MIN-MAX (for example 20-40)")

    try:
        low = int(low)
        high = int(high)
    except (TypeError, ValueError) as exc:
        raise ValueError("title delay values must be whole seconds") from exc
    if low < MIN_TITLE_DELAY:
        raise ValueError(f"title delay must be at least {MIN_TITLE_DELAY} seconds")
    if high < low:
        raise ValueError("title delay minimum cannot be greater than its maximum")
    if high > MAX_TITLE_DELAY:
        raise ValueError(f"title delay cannot exceed {MAX_TITLE_DELAY} seconds")
    return low, high


def normalize_title_delay(value: object) -> str:
    """Return the stable value stored in settings."""

    parsed = parse_title_delay(value)
    if parsed is None:
        return "off"
    return f"{parsed[0]}-{parsed[1]}"


def sample_title_delay(value: object, *, rng: random.Random | None = None) -> int | None:
    """Choose one delay from a setting value, or ``None`` when disabled."""

    parsed = parse_title_delay(value)
    if parsed is None:
        return None
    low, high = parsed
    return (rng or random).randint(low, high)


__all__ = [
    "DEFAULT_TITLE_DELAY",
    "MAX_TITLE_DELAY",
    "MIN_TITLE_DELAY",
    "normalize_title_delay",
    "parse_title_delay",
    "sample_title_delay",
]
