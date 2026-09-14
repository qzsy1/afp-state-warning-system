from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


def json_safe_value(value: Any) -> Any:
    """Return JSON-compatible data, replacing NaN/Infinity with ``None``."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): json_safe_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [json_safe_value(item) for item in value]
    return value
