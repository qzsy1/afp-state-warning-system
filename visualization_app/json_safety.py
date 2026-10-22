from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


def json_safe_value(value: Any) -> Any:
    """Return JSON-compatible data, replacing NaN/Infinity with ``None``."""
    kind = type(value)
    if value is None or kind in (str, int, bool):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if kind is dict or isinstance(value, Mapping):
        return {str(key): json_safe_value(item) for key, item in value.items()}
    if kind is list or (isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))):
        return [json_safe_value(item) for item in value]
    return value
